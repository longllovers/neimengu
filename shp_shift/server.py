"""Small multi-tab web UI for Shapefile translation, using only stdlib HTTP."""

from __future__ import annotations

import argparse
import json
import os
import posixpath
import sys
import threading
import traceback
import uuid
from dataclasses import dataclass, field
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

# Prefer the server's QGIS environment. This must happen before importing the
# processing module, because that environment intentionally provides GDAL
# instead of Fiona.
PREFERRED_PYTHON = os.environ.get(
    "SHP_SHIFT_PYTHON",
    "/home/cangling/miniforge3/envs/qgis/bin/python",
)
if __name__ == "__main__" and os.environ.get("SHP_SHIFT_PYTHON_ACTIVE") != "1":
    preferred = Path(PREFERRED_PYTHON)
    if preferred.is_file() and Path(sys.executable).resolve() != preferred.resolve():
        selected_environment = os.environ.copy()
        selected_environment["SHP_SHIFT_PYTHON_ACTIVE"] = "1"
        print(f"使用指定 Python：{preferred}", flush=True)
        try:
            os.execve(str(preferred), [str(preferred), *sys.argv], selected_environment)
        except OSError as exc:
            print(f"指定 Python 启动失败，继续使用 {sys.executable}：{exc}", flush=True)

from shp_shift import processing_engine_info, shift_shapefile


ROOT = Path(__file__).resolve().parent
CPU_COUNT = os.cpu_count() or 1
DEFAULT_CONCURRENCY = min(16, CPU_COUNT)

# Windows share paths entered in the browser are translated to Linux mounts.
PATH_MAPPINGS = (
    (r"\\10.10.10.11\data", "/mnt/nas_data"),
    (r"\\10.10.10.10\4np_share", "/mnt/data/4np"),
    (r"\\10.10.10.10\nas_data", "/mnt/nas_data"),
)


def map_server_path(value: str) -> str:
    """Translate a configured UNC prefix and leave Linux/local paths intact."""
    raw = str(value).strip()
    comparable = raw.replace("/", "\\")
    for unc_prefix, linux_prefix in PATH_MAPPINGS:
        if comparable.lower() == unc_prefix.lower():
            return linux_prefix
        prefix_with_separator = unc_prefix + "\\"
        if comparable.lower().startswith(prefix_with_separator.lower()):
            remainder = comparable[len(prefix_with_separator):].replace("\\", "/")
            return f"{linux_prefix.rstrip('/')}/{remainder}"
    return raw


@dataclass
class Task:
    id: str
    params: dict[str, Any]
    state: dict[str, Any] = field(default_factory=lambda: {"status": "queued", "processed": 0, "total": 0, "percent": 0})
    cancel: threading.Event = field(default_factory=threading.Event)
    lock: threading.Lock = field(default_factory=threading.Lock)
    last_log_percent: int = -5

    def update(self, values: dict[str, Any]) -> None:
        with self.lock:
            self.state.update(values)

    def snapshot(self) -> dict[str, Any]:
        with self.lock:
            return {"id": self.id, "params": self.params, **self.state}

    def progress(self, values: dict[str, Any]) -> None:
        self.update(values)
        percent = int(values.get("percent", 0))
        bucket = (percent // 5) * 5
        if bucket >= self.last_log_percent + 5 or values.get("status") == "completed":
            self.last_log_percent = bucket
            speed_text = (
                f"{values.get('byte_rate', 0) / 1048576:.1f} MB/秒"
                if values.get("byte_rate")
                else f"{values.get('rate', 0):.0f} 要素/秒"
            )
            print(
                f"[任务 {self.id}] {values.get('stage', '处理中')}，"
                f"{values.get('processed', 0)}/{values.get('total', 0)} "
                f"({values.get('percent', 0):.2f}%)，{speed_text}",
                flush=True,
            )


TASKS: dict[str, Task] = {}
TASKS_LOCK = threading.Lock()


def _run_task(task: Task) -> None:
    task.update({"status": "running"})
    try:
        p = task.params
        print(f"[任务 {task.id}] 开始", flush=True)
        if p["input_entered"] != p["input_path"]:
            print(f"[任务 {task.id}] 输入路径映射：{p['input_entered']} -> {p['input_path']}", flush=True)
        if not p["overwrite"] and p["output_entered"] != p["output_directory"]:
            print(f"[任务 {task.id}] 输出目录映射：{p['output_entered']} -> {p['output_directory']}", flush=True)
        if p["overwrite"]:
            print(f"[任务 {task.id}] 已启用安全覆盖", flush=True)
        print(f"[任务 {task.id}] 输出文件：{p['output_path']}", flush=True)
        print(
            f"[任务 {task.id}] 位移 dx={p['correct_x'] - p['original_x']}, "
            f"dy={p['correct_y'] - p['original_y']}，并发数={p['workers']}，批大小={p['batch_size']}",
            flush=True,
        )
        result = shift_shapefile(
            p["input_path"], p["output_path"],
            (p["original_x"], p["original_y"]),
            (p["correct_x"], p["correct_y"]),
            mode="process", workers=p["workers"], batch_size=p["batch_size"],
            progress=task.progress, cancel_event=task.cancel, overwrite=p["overwrite"],
        )
        task.update({"status": "completed", "result": result, "percent": 100})
        print(f"[任务 {task.id}] 完成，输出：{result['output']}，耗时 {result['elapsed_seconds']:.1f} 秒", flush=True)
    except InterruptedError as exc:
        task.update({"status": "cancelled", "error": str(exc)})
        print(f"[任务 {task.id}] 已取消", flush=True)
    except BaseException as exc:
        task.update({"status": "failed", "error": str(exc), "traceback": traceback.format_exc()})
        print(f"[任务 {task.id}] 失败：{exc}", flush=True)


def _validate(payload: dict[str, Any]) -> dict[str, Any]:
    overwrite = payload.get("overwrite", False) is True
    required = ["input_path", "original_x", "original_y", "correct_x", "correct_y"]
    if not overwrite:
        required.append("output_directory")
    missing = [key for key in required if payload.get(key) in (None, "")]
    if missing:
        raise ValueError("缺少参数：" + ", ".join(missing))
    input_entered = str(payload["input_path"]).strip()
    mapped_input = map_server_path(input_entered)
    input_filename = Path(mapped_input).name
    if not input_filename.lower().endswith(".shp"):
        raise ValueError("输入路径必须指向 .shp 文件")
    if overwrite:
        output_entered = ""
        mapped_output_directory = str(Path(mapped_input).parent)
        output_path = mapped_input
    else:
        output_entered = str(payload["output_directory"]).strip()
        mapped_output_directory = map_server_path(output_entered)
        output_path = (
            posixpath.join(mapped_output_directory, input_filename)
            if mapped_output_directory.startswith("/")
            else str(Path(mapped_output_directory) / input_filename)
        )
    result = {
        "input_entered": input_entered,
        "output_entered": output_entered,
        "input_path": mapped_input,
        "output_directory": mapped_output_directory,
        "output_path": output_path,
        "overwrite": overwrite,
        "original_x": float(payload["original_x"]), "original_y": float(payload["original_y"]),
        "correct_x": float(payload["correct_x"]), "correct_y": float(payload["correct_y"]),
        "workers": int(payload.get("workers", DEFAULT_CONCURRENCY)),
        "batch_size": int(payload.get("batch_size", 1000)),
    }
    if not 1 <= result["workers"] <= 128:
        raise ValueError("并发数必须为 1～128")
    if not 1 <= result["batch_size"] <= 100000:
        raise ValueError("批大小必须为 1～100000")
    return result


class Handler(BaseHTTPRequestHandler):
    server_version = "ShpShift/1.0"

    def log_message(self, fmt: str, *args: Any) -> None:
        # The page polls task state frequently. Suppress request noise; task
        # lifecycle and processing progress are printed by _run_task instead.
        return

    def _json(self, data: Any, status: HTTPStatus = HTTPStatus.OK) -> None:
        body = json.dumps(data, ensure_ascii=False, allow_nan=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _body(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0"))
        if length > 1_000_000:
            raise ValueError("请求过大")
        return json.loads(self.rfile.read(length).decode("utf-8"))

    def do_GET(self) -> None:
        path = urlparse(self.path).path
        if path == "/":
            body = (ROOT / "index.html").read_bytes()
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif path == "/api/tasks":
            with TASKS_LOCK:
                tasks = [task.snapshot() for task in TASKS.values()]
            self._json(tasks)
        elif path == "/api/config":
            self._json({
                "cpu_count": CPU_COUNT,
                "default_concurrency": DEFAULT_CONCURRENCY,
                "path_mappings": [{"source": source, "target": target} for source, target in PATH_MAPPINGS],
            })
        elif path.startswith("/api/tasks/"):
            task_id = path.rsplit("/", 1)[-1]
            with TASKS_LOCK:
                task = TASKS.get(task_id)
            self._json(task.snapshot() if task else {"error": "任务不存在"}, HTTPStatus.OK if task else HTTPStatus.NOT_FOUND)
        else:
            self._json({"error": "路径不存在"}, HTTPStatus.NOT_FOUND)

    def do_POST(self) -> None:
        path = urlparse(self.path).path
        try:
            if path == "/api/tasks":
                params = _validate(self._body())
                task = Task(uuid.uuid4().hex[:12], params)
                with TASKS_LOCK:
                    TASKS[task.id] = task
                threading.Thread(target=_run_task, args=(task,), name=f"shp-task-{task.id}", daemon=True).start()
                self._json(task.snapshot(), HTTPStatus.ACCEPTED)
            elif path.startswith("/api/tasks/") and path.endswith("/cancel"):
                task_id = path.split("/")[-2]
                with TASKS_LOCK:
                    task = TASKS.get(task_id)
                if not task:
                    self._json({"error": "任务不存在"}, HTTPStatus.NOT_FOUND)
                    return
                task.cancel.set()
                self._json({"ok": True, "message": "已请求取消；当前批次结束后生效"})
            else:
                self._json({"error": "路径不存在"}, HTTPStatus.NOT_FOUND)
        except (ValueError, TypeError, json.JSONDecodeError) as exc:
            self._json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)


class ReusableThreadingHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True
    request_queue_size = 128


def main() -> None:
    parser = argparse.ArgumentParser(description="Shapefile 偏移 Web 服务")
    parser.add_argument("--host", default="0.0.0.0", help="服务器部署可设为 0.0.0.0")
    parser.add_argument("--port", type=int, default=9010)
    args = parser.parse_args()
    server = ReusableThreadingHTTPServer((args.host, args.port), Handler)
    print(f"Python：{sys.executable}", flush=True)
    print(f"处理引擎：{processing_engine_info()}", flush=True)
    print(f"服务已启动：http://127.0.0.1:{args.port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n正在停止服务……")
    finally:
        server.server_close()


if __name__ == "__main__":
    # This guard is required by ProcessPoolExecutor on Windows.
    main()
