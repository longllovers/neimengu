#!/usr/bin/env python3
"""市级合并与县级裁剪 Web 控制台（仅使用 Python 标准库 HTTP 服务）。"""

from __future__ import annotations

import argparse
import atexit
import json
import os
import re
import signal
import subprocess
import sys
import threading
from collections import deque
from datetime import datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from queue import Queue
from urllib.parse import parse_qs, urlparse


BASE_DIR = Path(__file__).resolve().parent
UI_FILE = BASE_DIR / "processing_ui.html"
DEFAULT_MOUNT_POINT = Path("/mnt/usb_disk")
DEFAULT_MOUNT_SCRIPT = Path("/media/cangling/nas_folder/code/copy_txt/mount_new.sh")
MAX_REQUEST_BYTES = 1024 * 1024


def normalize_region_name(name: object) -> str:
    text = str(name).strip()
    for suffix in ("市", "盟", "地区", "自治州"):
        if text.endswith(suffix):
            return text[: -len(suffix)]
    return text


class ApplicationState:
    def __init__(self, mount_script: Path, mount_point: Path):
        self.mount_script = mount_script
        self.mount_point = mount_point
        self.lock = threading.RLock()
        self.logs: deque[tuple[int, str]] = deque(maxlen=10000)
        self.next_log_id = 1
        self.mount_running = False
        self.pipeline_running = False
        self.mount_path: str | None = None
        self.mount_version = 0
        self.stopping = False
        self.active_processes: set[subprocess.Popen] = set()

    def log(self, message: str) -> None:
        text = str(message).rstrip("\r\n")
        if not text:
            return
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with self.lock:
            for line in text.splitlines():
                formatted = f"[{timestamp}] {line}"
                self.logs.append((self.next_log_id, formatted))
                self.next_log_id += 1
                print(formatted, flush=True)

    def logs_after(self, after: int) -> tuple[list[dict], int]:
        with self.lock:
            items = [
                {"id": log_id, "message": message}
                for log_id, message in self.logs
                if log_id > after
            ]
            newest = self.next_log_id - 1
        return items, newest

    def status(self) -> dict:
        with self.lock:
            return {
                "mount_running": self.mount_running,
                "pipeline_running": self.pipeline_running,
                "mount_path": self.mount_path,
                "mount_version": self.mount_version,
            }

    def register_process(self, process: subprocess.Popen) -> bool:
        with self.lock:
            if self.stopping:
                return False
            self.active_processes.add(process)
            return True

    def unregister_process(self, process: subprocess.Popen) -> None:
        with self.lock:
            self.active_processes.discard(process)

    def is_stopping(self) -> bool:
        with self.lock:
            return self.stopping

    def terminate_all_processes(self) -> None:
        """停止由服务启动的所有外部命令及其子进程。"""
        with self.lock:
            self.stopping = True
            processes = list(self.active_processes)

        for process in processes:
            terminate_process_tree(process)


STATE: ApplicationState


def terminate_process_tree(process: subprocess.Popen) -> None:
    """跨平台终止一个由本服务创建的进程组。"""
    if process.poll() is not None:
        return

    if os.name == "nt":
        subprocess.run(
            ["taskkill", "/PID", str(process.pid), "/T", "/F"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        # 虚拟环境的 python.exe 可能是启动器：taskkill 已终止其子树，
        # 但 Popen 所持的启动器进程仍可能短暂存活，需要再明确收尾。
        if process.poll() is None:
            process.kill()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            pass
        return

    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    except OSError:
        process.terminate()

    try:
        process.wait(timeout=5)
        return
    except subprocess.TimeoutExpired:
        pass

    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    except OSError:
        process.kill()


def stream_command(command: list[str], label: str, on_line=None) -> int:
    """执行命令并把合并后的 stdout/stderr 逐行写入网页日志。"""
    if STATE.is_stopping():
        STATE.log(f"⛔ {label}未启动：服务正在停止")
        return 130

    STATE.log(f"{label}命令：{' '.join(command)}")
    env = os.environ.copy()
    env.update({"PYTHONUNBUFFERED": "1", "NO_COLOR": "1", "COLUMNS": "160"})
    popen_kwargs = {
        "cwd": BASE_DIR,
        "stdout": subprocess.PIPE,
        "stderr": subprocess.STDOUT,
        "text": True,
        "encoding": "utf-8",
        "errors": "replace",
        "bufsize": 1,
        "env": env,
    }
    if os.name == "nt":
        popen_kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        popen_kwargs["start_new_session"] = True
    try:
        process = subprocess.Popen(command, **popen_kwargs)
    except Exception as exc:
        STATE.log(f"❌ {label}无法启动：{exc}")
        return 1

    if not STATE.register_process(process):
        terminate_process_tree(process)
        STATE.log(f"⛔ {label}已终止：服务正在停止")
        return 130

    try:
        assert process.stdout is not None
        for line in process.stdout:
            STATE.log(line)
            if on_line is not None:
                on_line(line.rstrip("\r\n"))
        return_code = process.wait()
    finally:
        STATE.unregister_process(process)

    if STATE.is_stopping():
        STATE.log(f"⛔ {label}已随服务停止")
        return 130
    if return_code == 0:
        STATE.log(f"✅ {label}完成")
    else:
        STATE.log(f"❌ {label}失败，退出码：{return_code}")
    return return_code


def mount_worker() -> None:
    detected_mount_path: list[str] = []

    def capture_mount_path(line: str) -> None:
        match = re.search(r"挂载路径：\s*(.+?)\s*$", line)
        if match:
            detected_mount_path[:] = [match.group(1)]

    try:
        STATE.log("开始挂载新硬盘；请按照日志提示插入硬盘。")
        command = ["sudo", "-n", "/usr/bin/bash", str(STATE.mount_script)]
        return_code = stream_command(command, "挂载硬盘", on_line=capture_mount_path)
        if return_code == 0:
            if detected_mount_path:
                final_mount = detected_mount_path[0]
                with STATE.lock:
                    STATE.mount_path = final_mount
                    STATE.mount_version += 1
                STATE.log(f"挂载路径已发送到页面：{final_mount}")
            else:
                STATE.log("⚠️ 挂载脚本成功结束，但没有找到“挂载路径：...”输出。")
    finally:
        with STATE.lock:
            STATE.mount_running = False


def official_output_city(output_root: Path, requested_city: str) -> str | None:
    """合并脚本可能把目录名规范为边界中的正式市名。"""
    if not output_root.exists():
        return None
    wanted = normalize_region_name(requested_city)
    matches = sorted(
        path.name
        for path in output_root.iterdir()
        if path.is_dir() and normalize_region_name(path.name) == wanted
    )
    return matches[0] if matches else None


def pipeline_worker(config: dict) -> None:
    source_root = Path(config["source_path"])
    city_output = Path(config["city_output_path"])
    county_output = Path(config["county_output_path"])
    cities = config["cities"]
    clip_queue: Queue[Path | None] = Queue()
    queued_files: set[Path] = set()
    clip_failures: list[Path] = []

    def enqueue_ready_file(line: str) -> None:
        clean_line = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", line).strip()
        marker = "市级影像就绪："
        marker_index = clean_line.find(marker)
        if marker_index < 0:
            return
        output_path = Path(clean_line[marker_index + len(marker) :].strip()).resolve()
        if output_path in queued_files:
            return
        if not output_path.is_file():
            STATE.log(f"❌ 市级影像就绪信息对应的文件不存在：{output_path}")
            return
        queued_files.add(output_path)
        clip_queue.put(output_path)
        STATE.log(f"↪️ 已加入县级裁剪队列：{output_path.name}")

    def clip_queue_worker() -> None:
        while True:
            source_path = clip_queue.get()
            try:
                if source_path is None:
                    return
                if STATE.is_stopping():
                    continue

                city_name = source_path.parent.name
                STATE.log(
                    f"========== {city_name}：开始裁剪已就绪市级影像 "
                    f"{source_path.name} =========="
                )
                county_command = [
                    sys.executable,
                    "-u",
                    str(BASE_DIR / "clip_county_tifs.py"),
                    "--input-root",
                    str(city_output),
                    "--input-file",
                    str(source_path),
                    "--output-root",
                    str(county_output),
                    "--city",
                    city_name,
                    "--max-workers",
                    str(config["county_workers"]),
                ]
                if config["county_overwrite"]:
                    county_command.append("--overwrite")
                clip_code = stream_command(
                    county_command,
                    f"{city_name} {source_path.name} 县级裁剪",
                )
                if clip_code not in (0, 130):
                    clip_failures.append(source_path)
            except Exception as exc:
                if source_path is not None:
                    clip_failures.append(source_path)
                STATE.log(f"❌ 县级裁剪队列异常：{type(exc).__name__}: {exc}")
            finally:
                clip_queue.task_done()

    clip_thread = threading.Thread(
        target=clip_queue_worker,
        name="county-clip-queue",
        daemon=True,
    )
    clip_thread.start()

    try:
        city_output.mkdir(parents=True, exist_ok=True)
        county_output.mkdir(parents=True, exist_ok=True)
        STATE.log(
            f"任务正常运行：共 {len(cities)} 个城市；"
            f"市级线程 {config['city_workers']}，县级线程 {config['county_workers']}"
        )

        for index, city in enumerate(cities, 1):
            STATE.log(f"========== [{index}/{len(cities)}] {city}：开始市级合并 ==========")
            merge_command = [
                sys.executable,
                "-u",
                str(BASE_DIR / "merge_city_tifs.py"),
                "--input-root",
                str(source_root),
                "--output-root",
                str(city_output),
                "--city",
                city,
                "--max-workers",
                str(config["city_workers"]),
            ]
            if config["city_overwrite"]:
                merge_command.append("--overwrite")
            merge_code = stream_command(
                merge_command,
                f"{city} 市级合并",
                on_line=enqueue_ready_file,
            )
            if STATE.is_stopping():
                return

            if merge_code != 0:
                STATE.log(f"⚠️ {city} 市级合并存在失败项，队列仍会裁剪已成功生成的影像。")

        STATE.log("所有市级合并已结束，正在等待县级裁剪队列完成……")
    except Exception as exc:
        STATE.log(f"❌ 流水线异常结束：{type(exc).__name__}: {exc}")
    finally:
        clip_queue.put(None)
        clip_queue.join()
        clip_thread.join()
        if not STATE.is_stopping():
            if clip_failures:
                STATE.log(f"⚠️ 流水线结束：县级裁剪失败 {len(clip_failures)} 个市级影像。")
            else:
                STATE.log("🎉 运行完成：市级合并与县级裁剪流水线已全部结束。")
        with STATE.lock:
            STATE.pipeline_running = False


def validate_run_config(data: dict) -> dict:
    source_text = str(data.get("source_path", "")).strip()
    city_output_text = str(data.get("city_output_path", "")).strip()
    county_output_text = str(data.get("county_output_path", "")).strip()
    if not source_text:
        raise ValueError("请填写来源影像路径")
    if not city_output_text:
        raise ValueError("请填写市级输出路径")
    if not county_output_text:
        raise ValueError("请填写县级输出路径")

    source_path = Path(source_text).expanduser()
    city_output = Path(city_output_text).expanduser()
    county_output = Path(county_output_text).expanduser()
    if not source_path.is_dir():
        raise ValueError(f"来源影像目录不存在：{source_path}")

    available = {
        path.name
        for path in source_path.iterdir()
        if path.is_dir() and not path.name.startswith(".")
    }
    requested = data.get("cities") or []
    if not isinstance(requested, list):
        raise ValueError("城市列表格式错误")
    cities = list(dict.fromkeys(str(city) for city in requested if str(city) in available))
    if not cities:
        raise ValueError("请至少选择一个有效城市")

    try:
        city_workers = int(data.get("city_workers", 1))
        county_workers = int(data.get("county_workers", 1))
    except (TypeError, ValueError) as exc:
        raise ValueError("线程数必须是整数") from exc
    if not 1 <= city_workers <= 32 or not 1 <= county_workers <= 32:
        raise ValueError("线程数必须在 1 到 32 之间")

    return {
        "source_path": str(source_path.resolve()),
        "city_output_path": str(city_output.resolve()),
        "county_output_path": str(county_output.resolve()),
        "cities": cities,
        "city_workers": city_workers,
        "county_workers": county_workers,
        "city_overwrite": bool(data.get("city_overwrite", False)),
        "county_overwrite": bool(data.get("county_overwrite", False)),
    }


class RequestHandler(BaseHTTPRequestHandler):
    server_version = "ImageryProcessingServer/1.0"

    def log_message(self, format_string: str, *args) -> None:
        # 页面每秒轮询日志，不在终端重复打印这些成功访问。
        message = format_string % args
        if '"GET /api/logs?' in message and '" 200 ' in message:
            return
        sys.stderr.write(f"{self.address_string()} - {format_string % args}\n")
        sys.stderr.flush()

    def send_json(self, payload: dict, status: HTTPStatus = HTTPStatus.OK) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def read_json(self) -> dict:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError as exc:
            raise ValueError("Content-Length 无效") from exc
        if length <= 0 or length > MAX_REQUEST_BYTES:
            raise ValueError("请求内容为空或过大")
        try:
            value = json.loads(self.rfile.read(length).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError("请求不是有效 JSON") from exc
        if not isinstance(value, dict):
            raise ValueError("JSON 顶层必须是对象")
        return value

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/":
            try:
                body = UI_FILE.read_bytes()
            except OSError as exc:
                self.send_json({"ok": False, "error": str(exc)}, HTTPStatus.INTERNAL_SERVER_ERROR)
                return
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)
            return

        if parsed.path == "/api/logs":
            query = parse_qs(parsed.query)
            try:
                after = int(query.get("after", ["0"])[0])
            except ValueError:
                after = 0
            logs, newest = STATE.logs_after(after)
            self.send_json({"ok": True, "logs": logs, "newest": newest, **STATE.status()})
            return

        self.send_json({"ok": False, "error": "接口不存在"}, HTTPStatus.NOT_FOUND)

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        try:
            data = self.read_json()
            if parsed.path == "/api/mount":
                self.handle_mount()
            elif parsed.path == "/api/cities":
                self.handle_cities(data)
            elif parsed.path == "/api/run":
                self.handle_run(data)
            else:
                self.send_json({"ok": False, "error": "接口不存在"}, HTTPStatus.NOT_FOUND)
        except ValueError as exc:
            self.send_json({"ok": False, "error": str(exc)}, HTTPStatus.BAD_REQUEST)
        except Exception as exc:
            STATE.log(f"❌ HTTP 接口异常：{type(exc).__name__}: {exc}")
            self.send_json({"ok": False, "error": str(exc)}, HTTPStatus.INTERNAL_SERVER_ERROR)

    def handle_mount(self) -> None:
        with STATE.lock:
            if STATE.mount_running:
                self.send_json({"ok": False, "error": "挂载任务正在运行"}, HTTPStatus.CONFLICT)
                return
            if STATE.pipeline_running:
                self.send_json({"ok": False, "error": "影像任务运行中，不能重新挂载硬盘"}, HTTPStatus.CONFLICT)
                return
            STATE.mount_running = True
            STATE.mount_path = None
        threading.Thread(target=mount_worker, name="mount-worker", daemon=True).start()
        self.send_json(
            {
                "ok": True,
                "message": "挂载任务已启动，请查看日志并插入新硬盘",
            },
            HTTPStatus.ACCEPTED,
        )

    def handle_cities(self, data: dict) -> None:
        source = Path(str(data.get("source_path", "")).strip()).expanduser()
        if not source.is_dir():
            raise ValueError(f"来源影像目录不存在：{source}")
        cities = sorted(
            path.name
            for path in source.iterdir()
            if path.is_dir() and not path.name.startswith(".")
        )
        self.send_json({"ok": True, "cities": cities, "count": len(cities)})

    def handle_run(self, data: dict) -> None:
        config = validate_run_config(data)
        with STATE.lock:
            if STATE.pipeline_running:
                self.send_json({"ok": False, "error": "已有影像任务正在运行"}, HTTPStatus.CONFLICT)
                return
            if STATE.mount_running:
                self.send_json({"ok": False, "error": "请等待硬盘挂载完成"}, HTTPStatus.CONFLICT)
                return
            STATE.pipeline_running = True
        threading.Thread(
            target=pipeline_worker,
            args=(config,),
            name="imagery-pipeline",
            daemon=True,
        ).start()
        self.send_json({"ok": True, "message": "任务已正常启动"}, HTTPStatus.ACCEPTED)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="市级合并和县级裁剪 Web 控制台")
    parser.add_argument("--host", default="0.0.0.0", help="监听地址，默认仅本机")
    parser.add_argument("--port", type=int, default=8895, help="监听端口，默认 8895")
    parser.add_argument("--mount-point", type=Path, default=DEFAULT_MOUNT_POINT, help="挂载点")
    parser.add_argument("--mount-script", type=Path, default=DEFAULT_MOUNT_SCRIPT, help="免密 sudo 挂载脚本")
    return parser.parse_args()


def main() -> int:
    global STATE
    args = parse_args()
    if not UI_FILE.exists():
        print(f"界面文件不存在：{UI_FILE}", file=sys.stderr)
        return 1
    if not 1 <= args.port <= 65535:
        print("端口必须在 1 到 65535 之间", file=sys.stderr)
        return 2

    STATE = ApplicationState(args.mount_script, args.mount_point)
    atexit.register(STATE.terminate_all_processes)

    def stop_on_signal(signum, frame) -> None:
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, stop_on_signal)
    if hasattr(signal, "SIGBREAK"):
        signal.signal(signal.SIGBREAK, stop_on_signal)

    server = ThreadingHTTPServer((args.host, args.port), RequestHandler)
    server.daemon_threads = True
    STATE.log(f"Web 服务已启动：http://{args.host}:{args.port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n正在停止 Web 服务……")
    finally:
        server.server_close()
        STATE.terminate_all_processes()
        print("已停止 Web 服务启动的所有处理进程。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
