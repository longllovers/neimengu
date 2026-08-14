from __future__ import annotations

import json
import os
import queue
import subprocess
import sys
import threading
import time
import uuid
from dataclasses import dataclass, field
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlparse


ROOT = Path(__file__).resolve().parent
WEB_ROOT = ROOT / "web"
SCRIPT = ROOT / "build_pamid_folder_multithread.py"


@dataclass
class Task:
    task_id: str
    command: list[str]
    created_at: float = field(default_factory=time.time)
    state: str = "running"
    return_code: int | None = None
    events: list[dict[str, Any]] = field(default_factory=list)
    subscribers: list[queue.Queue[dict[str, Any]]] = field(default_factory=list)
    process: subprocess.Popen[str] | None = None
    lock: threading.Lock = field(default_factory=threading.Lock)

    def publish(self, event_type: str, **data: Any) -> None:
        event = {"type": event_type, **data}
        with self.lock:
            self.events.append(event)
            subscribers = list(self.subscribers)
        for subscriber in subscribers:
            subscriber.put(event)

    def subscribe(self) -> tuple[list[dict[str, Any]], queue.Queue[dict[str, Any]] | None]:
        with self.lock:
            history = list(self.events)
            if self.state != "running":
                return history, None
            subscriber: queue.Queue[dict[str, Any]] = queue.Queue()
            self.subscribers.append(subscriber)
            return history, subscriber

    def unsubscribe(self, subscriber: queue.Queue[dict[str, Any]]) -> None:
        with self.lock:
            if subscriber in self.subscribers:
                self.subscribers.remove(subscriber)


TASKS: dict[str, Task] = {}
TASKS_LOCK = threading.Lock()


def convert_network_path(path: str | None) -> str | None:
    """把指定 Windows 文件服务器路径转换为 Linux 挂载路径。"""
    if path is None:
        return path

    original = str(path).strip()
    if not original:
        return original

    # 仅使用标准化路径进行匹配，未命中时返回原路径。
    normalized = original.replace("\\", "/")
    share_mounts = {
        "data": "/media/cangling/nas_folder",
        "新建卷": "/media/cangling/xinjianjuan",
        "datadisk2": "/media/cangling/EAGET",
        "新加卷": "/media/cangling/xinjiajuan",
    }

    for host_index in range(1, 256):
        for share_name, linux_prefix in share_mounts.items():
            for windows_prefix in (
                f"//10.10.10.{host_index}/{share_name}",
                f"/10.10.10.{host_index}/{share_name}",
                f"10.10.10.{host_index}/{share_name}",
            ):
                # 完整匹配共享目录名，避免 data 错误匹配 datadisk2。
                if normalized == windows_prefix:
                    return linux_prefix
                if normalized.startswith(windows_prefix + "/"):
                    return linux_prefix + normalized[len(windows_prefix) :]

    return original


def run_task(task: Task) -> None:
    task.publish("status", state="running", message="正在运行")
    try:
        env = os.environ.copy()
        env["PYTHONUNBUFFERED"] = "1"
        process = subprocess.Popen(
            task.command,
            cwd=ROOT,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            env=env,
        )
        task.process = process
        assert process.stdout is not None
        for line in iter(process.stdout.readline, ""):
            task.publish("output", text=line, stream="stdout")
        process.stdout.close()
        return_code = process.wait()
        task.return_code = return_code
        task.state = "success" if return_code == 0 else "failed"
        message = "运行成功" if return_code == 0 else f"运行失败（退出码 {return_code}）"
        task.publish("status", state=task.state, message=message, return_code=return_code)
    except Exception as exc:  # noqa: BLE001
        task.return_code = -1
        task.state = "failed"
        task.publish("output", text=f"无法启动脚本：{exc}\n", stream="system")
        task.publish("status", state="failed", message="运行失败", return_code=-1)
    finally:
        task.publish("done", state=task.state, return_code=task.return_code)


def parse_run_options(payload: dict[str, Any]) -> list[str]:
    tif_dir = str(payload.get("tifDir", "")).strip()
    if not tif_dir:
        raise ValueError("请填写 TIF 文件夹")
    converted_tif_dir = convert_network_path(tif_dir)
    assert converted_tif_dir is not None

    resampling = str(payload.get("resampling", "nearest"))
    allowed_resampling = {"nearest", "average", "bilinear", "cubic", "mode"}
    if resampling not in allowed_resampling:
        raise ValueError("不支持的重采样方式")

    try:
        max_factor = int(payload.get("maxFactor", 256))
    except (TypeError, ValueError) as exc:
        raise ValueError("最高倍数必须是整数") from exc
    if max_factor not in {2, 4, 8, 16, 32, 64, 128, 256}:
        raise ValueError("最高倍数必须是 2 到 256 之间的 2 的幂")

    try:
        workers = int(payload.get("workers", min(8, os.cpu_count() or 4)))
    except (TypeError, ValueError) as exc:
        raise ValueError("子进程数必须是整数") from exc
    if not 1 <= workers <= 64:
        raise ValueError("子进程数应在 1 到 64 之间")

    command = [
        sys.executable,
        "-u",
        str(SCRIPT),
        "--tif-dir",
        converted_tif_dir,
        "--resampling",
        resampling,
        "--max-factor",
        str(max_factor),
        "--workers",
        str(workers),
    ]
    for field_name, argument in (
        ("recursive", "--recursive"),
        ("force", "--force"),
        ("dryRun", "--dry-run"),
    ):
        if payload.get(field_name) is True:
            command.append(argument)
    return command


class AppHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, format: str, *args: Any) -> None:
        # HTTP 访问日志会淹没真正的脚本输出，仅记录服务启动信息。
        return

    def send_bytes(self, content: bytes, content_type: str, status: int = 200) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(content)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(content)

    def send_json(self, payload: Any, status: int = 200) -> None:
        self.send_bytes(
            json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            "application/json; charset=utf-8",
            status,
        )

    def do_GET(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        if path == "/":
            self.serve_file(WEB_ROOT / "index.html", "text/html; charset=utf-8")
            return
        if path == "/app.css":
            self.serve_file(WEB_ROOT / "app.css", "text/css; charset=utf-8")
            return
        if path == "/app.js":
            self.serve_file(WEB_ROOT / "app.js", "text/javascript; charset=utf-8")
            return
        if path.startswith("/api/tasks/") and path.endswith("/events"):
            task_id = path.split("/")[3]
            self.stream_events(task_id)
            return
        self.send_error(HTTPStatus.NOT_FOUND)

    def do_POST(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        if path != "/api/tasks":
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length > 64 * 1024:
                raise ValueError("请求内容过大")
            payload = json.loads(self.rfile.read(length) or b"{}")
            command = parse_run_options(payload)
        except (ValueError, json.JSONDecodeError) as exc:
            self.send_json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
            return

        task_id = uuid.uuid4().hex
        task = Task(task_id=task_id, command=command)
        with TASKS_LOCK:
            TASKS[task_id] = task
        threading.Thread(target=run_task, args=(task,), daemon=True).start()
        self.send_json({"taskId": task_id}, HTTPStatus.CREATED)

    def serve_file(self, path: Path, content_type: str) -> None:
        try:
            self.send_bytes(path.read_bytes(), content_type)
        except FileNotFoundError:
            self.send_error(HTTPStatus.NOT_FOUND)

    def write_sse(self, event: dict[str, Any]) -> None:
        data = json.dumps(event, ensure_ascii=False)
        self.wfile.write(f"data: {data}\n\n".encode("utf-8"))
        self.wfile.flush()

    def stream_events(self, task_id: str) -> None:
        with TASKS_LOCK:
            task = TASKS.get(task_id)
        if task is None:
            self.send_json({"error": "任务不存在"}, HTTPStatus.NOT_FOUND)
            return

        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()

        history, subscriber = task.subscribe()
        try:
            for event in history:
                self.write_sse(event)
            if subscriber is None:
                return
            while True:
                try:
                    event = subscriber.get(timeout=15)
                    self.write_sse(event)
                    if event["type"] == "done":
                        return
                except queue.Empty:
                    self.wfile.write(b": keep-alive\n\n")
                    self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            self.close_connection = True
            if subscriber is not None:
                task.unsubscribe(subscriber)


def main() -> None:
    host = "0.0.0.0"
    port = 8899
    server = ThreadingHTTPServer((host, port), AppHandler)
    print(f"金字塔任务界面：http://127.0.0.1:{port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n服务已停止。")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
