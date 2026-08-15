"""统一 FastAPI 服务入口。

Linux 启动示例：uvicorn backend.main:app --host 127.0.0.1 --port 9000
"""

from __future__ import annotations

import asyncio
import json
import mimetypes
import os
import signal
import subprocess
import tempfile
import threading
import time
import uuid
import zipfile
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from starlette.background import BackgroundTask
from pydantic import BaseModel, Field

from . import task_store
from .adapters import TaskRuntime, execute, path_value
from .catalog import TOOL_MAP, public_catalog
from .repositories import preview_file, repository_tree, safe_path
from .result_gallery import (
    archive_paths, asset_path, csv_preview, delete_all, delete_window,
    excel_preview, json_preview, list_windows, publish_directory, shapefile_geojson,
)


PROJECT_ROOT = Path(__file__).resolve().parents[1]
FRONTEND_DIST = PROJECT_ROOT / "frontend" / "dist"


app = FastAPI(
    title="地理数据处理工作台 API",
    version="1.0.0",
    docs_url="/api/docs",
    openapi_url="/api/openapi.json",
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
    allow_methods=["*"],
    allow_headers=["*"],
)


class TaskRequest(BaseModel):
    parameters: dict[str, Any] = Field(default_factory=dict)


@dataclass
class TaskRecord:
    id: str
    tool_id: str
    tool_name: str
    parameters: dict[str, Any]
    status: str = "queued"
    created_at: str = field(default_factory=lambda: datetime.now().isoformat(timespec="seconds"))
    started_at: str | None = None
    finished_at: str | None = None
    result: dict[str, Any] | None = None
    error: str | None = None
    events: deque[dict[str, Any]] = field(default_factory=lambda: deque(maxlen=20_000))
    next_sequence: int = 1
    runtime: TaskRuntime | None = None
    lock: threading.RLock = field(default_factory=threading.RLock, repr=False)

    def emit(self, event_type: str, data: dict[str, Any]) -> None:
        with self.lock:
            event = {
                "sequence": self.next_sequence,
                "type": event_type,
                "time": datetime.now().isoformat(timespec="seconds"),
                **data,
            }
            self.next_sequence += 1
            self.events.append(event)
            snapshot = self.snapshot(include_parameters=True)
        task_store.save_emission(snapshot, event)

    def snapshot(self, include_parameters: bool = False) -> dict[str, Any]:
        with self.lock:
            data = {
                "id": self.id, "tool_id": self.tool_id, "tool_name": self.tool_name,
                "status": self.status, "created_at": self.created_at,
                "started_at": self.started_at, "finished_at": self.finished_at,
                "result": self.result, "error": self.error,
                "last_sequence": self.next_sequence - 1,
            }
            if include_parameters:
                data["parameters"] = self.parameters
            return data


TASKS: dict[str, TaskRecord] = {}
TASKS_LOCK = threading.RLock()
MAX_TASKS = 300


def restore_tasks() -> None:
    task_store.initialize()
    for saved in task_store.load_tasks(MAX_TASKS):
        restored = TaskRecord(
            id=saved["id"], tool_id=saved["tool_id"], tool_name=saved["tool_name"],
            parameters=saved["parameters"], status=saved["status"],
            created_at=saved["created_at"], started_at=saved["started_at"],
            finished_at=saved["finished_at"], result=saved["result"], error=saved["error"],
            events=deque(saved["events"], maxlen=20_000),
            next_sequence=max((int(event["sequence"]) for event in saved["events"]), default=0) + 1,
        )
        TASKS[restored.id] = restored
        if restored.status in {"queued", "running", "cancelling"}:
            restored.status = "failed"
            restored.error = "服务重启，原运行进程已中断"
            restored.finished_at = datetime.now().isoformat(timespec="seconds")
            restored.emit("status", {"status": "failed", "message": restored.error})


restore_tasks()


def get_task(task_id: str) -> TaskRecord:
    with TASKS_LOCK:
        task = TASKS.get(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="任务不存在")
    return task


def run_task(task: TaskRecord) -> None:
    tool = TOOL_MAP[task.tool_id]
    runtime = TaskRuntime(emit=task.emit)
    with task.lock:
        if task.status == "cancelled":
            return
        task.runtime = runtime
        task.status = "running"
        task.started_at = datetime.now().isoformat(timespec="seconds")
    task.emit("status", {"status": "running", "message": "任务开始运行"})
    execution_started = time.time()
    try:
        result = execute(runtime, tool, task.parameters)
    except InterruptedError as exc:
        with task.lock:
            task.status = "cancelled"; task.error = str(exc)
        task.emit("status", {"status": "cancelled", "message": str(exc)})
    except Exception as exc:
        with task.lock:
            task.status = "failed"; task.error = f"{type(exc).__name__}: {exc}"
        task.emit("log", {"message": task.error, "level": "error"})
        task.emit("status", {"status": "failed", "message": str(exc)})
    else:
        gallery_policy = None
        if task.tool_id == "vote" and task.parameters.get("operation", "run") == "run":
            gallery_policy = ("out_dir", {"csv"})
        elif task.tool_id == "county-clip-shp":
            gallery_policy = ("output_dir", {"svg"})
        elif task.tool_id in {"farmland-check", "wheat-check", "greenhouse-check", "multicrop-check"} and task.parameters.get("stage") == "evaluate":
            gallery_policy = ("work_root", {"csv"})
        elif task.tool_id == "delivery-check":
            gallery_policy = ("output_dir", {"pdf", "json"})
        elif task.tool_id == "topology":
            gallery_policy = ("output_dir", {"csv"})
        elif task.tool_id == "shp-overlap":
            gallery_policy = ("output_dir", {"csv"})
        elif task.tool_id == "shp-compare":
            gallery_policy = ("output_path", {"csv"})
        elif task.tool_id == "county-crop":
            gallery_policy = ("output_root", {"shp"})
        elif task.tool_id in {"livestock", "livestock2"}:
            gallery_policy = ("out_shp", {"shp"})
        if gallery_policy:
            output_field, kinds = gallery_policy
            if task.parameters.get(output_field):
                try:
                    gallery_target = path_value(task.parameters, output_field)
                    if task.tool_id in {"farmland-check", "wheat-check", "greenhouse-check", "multicrop-check"}:
                        result_folder = "04评价精度结果" if task.tool_id in {"farmland-check", "greenhouse-check"} else "04精度评价"
                        gallery_target = str(Path(gallery_target) / result_folder / "精度评价汇总.csv")
                    gallery = publish_directory(
                        task.tool_id, task.tool_name, task.id,
                        gallery_target, kinds=kinds,
                        newer_than=execution_started,
                    )
                    if gallery:
                        result = {**result, "gallery_window_id": gallery["id"]}
                except Exception as exc:
                    task.emit("log", {"message": f"结果展示发布失败，不影响原任务成果：{exc}", "level": "warning"})
        with task.lock:
            task.status = "completed"; task.result = result
        task.emit("status", {"status": "completed", "message": "任务运行完成", "result": result})
    finally:
        with task.lock:
            task.finished_at = datetime.now().isoformat(timespec="seconds")
            task.runtime = None
            snapshot = task.snapshot(include_parameters=True)
        task_store.save_task(snapshot)


@app.get("/api/health")
def health() -> dict[str, Any]:
    with TASKS_LOCK:
        running = sum(1 for task in TASKS.values() if task.status in {"queued", "running", "cancelling"})
    return {"ok": True, "service": "geo-workbench", "tools": len(TOOL_MAP), "running_tasks": running}


@app.get("/api/tools")
def tools() -> dict[str, Any]:
    return {"tools": public_catalog()}


@app.get("/api/results")
def result_windows() -> dict[str, Any]:
    return {"windows": list_windows()}


@app.delete("/api/results")
def clear_result_windows() -> dict[str, Any]:
    delete_all()
    return {"ok": True}


@app.delete("/api/results/{window_id}")
def remove_result_window(window_id: str) -> dict[str, Any]:
    try:
        delete_window(window_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="展示窗口不存在") from None
    return {"ok": True}


@app.get("/api/results/{window_id}/csv/{asset_id}")
def result_csv(window_id: str, asset_id: str) -> dict[str, Any]:
    try:
        return csv_preview(window_id, asset_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="结果文件不存在") from None
    except FileNotFoundError:
        raise HTTPException(status_code=410, detail="原始结果文件已不存在") from None
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None


@app.get("/api/results/{window_id}/shp/{asset_id}")
def result_shapefile(window_id: str, asset_id: str) -> dict[str, Any]:
    try:
        return shapefile_geojson(window_id, asset_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="结果文件不存在") from None
    except FileNotFoundError:
        raise HTTPException(status_code=410, detail="原始结果文件已不存在") from None
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None


@app.get("/api/results/{window_id}/excel/{asset_id}")
def result_excel(window_id: str, asset_id: str) -> dict[str, Any]:
    try:
        return excel_preview(window_id, asset_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="结果文件不存在") from None
    except FileNotFoundError:
        raise HTTPException(status_code=410, detail="原始结果文件已不存在") from None
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None


@app.get("/api/results/{window_id}/json/{asset_id}")
def result_json(window_id: str, asset_id: str) -> dict[str, Any]:
    try:
        return json_preview(window_id, asset_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="结果文件不存在") from None
    except FileNotFoundError:
        raise HTTPException(status_code=410, detail="原始结果文件已不存在") from None
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None


@app.get("/api/results/{window_id}/file/{asset_id}")
def result_file(window_id: str, asset_id: str) -> FileResponse:
    try:
        asset, path = asset_path(window_id, asset_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="结果文件不存在") from None
    except FileNotFoundError:
        raise HTTPException(status_code=410, detail="原始结果文件已不存在") from None
    media_types = {"pdf": "application/pdf", "json": "application/json", "csv": "text/csv", "svg": "image/svg+xml"}
    return FileResponse(path, media_type=media_types.get(asset["kind"], "application/octet-stream"))


@app.get("/api/results/{window_id}/download")
def download_result_window(window_id: str) -> FileResponse:
    try:
        files = archive_paths(window_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="展示窗口不存在") from None
    if not files:
        raise HTTPException(status_code=410, detail="原始结果文件已不存在")
    if len(files) == 1:
        path, _ = files[0]
        media_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        return FileResponse(path, filename=path.name, media_type=media_type)
    temp_dir = Path(tempfile.mkdtemp(prefix="geo-result-archive-"))
    archive = temp_dir / "results.zip"
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as stream:
        for path, name in files:
            stream.write(path, name)
    return FileResponse(
        archive, filename="result-window.zip", media_type="application/zip",
        background=BackgroundTask(__import__("shutil").rmtree, temp_dir, ignore_errors=True),
    )


@app.get("/api/repositories/{repo_id}")
def code_repository(repo_id: str) -> dict[str, Any]:
    try:
        return repository_tree(repo_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="代码库不存在") from None


@app.get("/api/repositories/{repo_id}/preview")
def code_preview(repo_id: str, path: str) -> dict[str, Any]:
    try:
        return preview_file(repo_id, path)
    except KeyError:
        raise HTTPException(status_code=404, detail="代码库不存在") from None
    except (FileNotFoundError, IsADirectoryError):
        raise HTTPException(status_code=404, detail="文件不存在") from None
    except PermissionError:
        raise HTTPException(status_code=403, detail="无权访问该路径") from None


@app.get("/api/repositories/{repo_id}/download")
def code_download(repo_id: str, path: str) -> FileResponse:
    try:
        _, target = safe_path(repo_id, path)
    except KeyError:
        raise HTTPException(status_code=404, detail="代码库不存在") from None
    except PermissionError:
        raise HTTPException(status_code=403, detail="无权访问该路径") from None
    if not target.is_file():
        raise HTTPException(status_code=404, detail="文件不存在")
    return FileResponse(target, filename=target.name, media_type="application/octet-stream")


@app.get("/api/repositories/{repo_id}/archive")
def code_archive(repo_id: str) -> FileResponse:
    import shutil
    try:
        item, root = safe_path(repo_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="代码库不存在") from None
    temp_dir = Path(tempfile.mkdtemp(prefix="geo-code-archive-"))
    archive = Path(shutil.make_archive(str(temp_dir / root.name), "zip", root_dir=root))
    return FileResponse(
        archive, filename=f"{root.name}.zip", media_type="application/zip",
        background=BackgroundTask(shutil.rmtree, temp_dir, ignore_errors=True),
    )


@app.get("/api/tasks")
def recent_tasks(limit: int = 30) -> dict[str, Any]:
    limit = max(1, min(limit, 100))
    with TASKS_LOCK:
        items = list(TASKS.values())[-limit:]
    return {"tasks": [task.snapshot() for task in reversed(items)]}


@app.post("/api/tasks/{tool_id}", status_code=202)
def create_task(tool_id: str, body: TaskRequest) -> dict[str, Any]:
    tool = TOOL_MAP.get(tool_id)
    if tool is None:
        raise HTTPException(status_code=404, detail="工具不存在")
    task = TaskRecord(uuid.uuid4().hex, tool_id, tool["name"], body.parameters)
    task.emit("status", {"status": "queued", "message": "任务已提交"})
    with TASKS_LOCK:
        if len(TASKS) >= MAX_TASKS:
            removable = [key for key, value in TASKS.items() if value.status not in {"queued", "running", "cancelling"}]
            removed = removable[: max(1, len(TASKS) - MAX_TASKS + 1)]
            for key in removed:
                TASKS.pop(key, None)
            task_store.delete_tasks(removed)
        TASKS[task.id] = task
    threading.Thread(target=run_task, args=(task,), name=f"task-{task.id[:8]}", daemon=True).start()
    return task.snapshot(include_parameters=True)


@app.get("/api/tasks/{task_id}")
def task_status(task_id: str) -> dict[str, Any]:
    return get_task(task_id).snapshot(include_parameters=True)


@app.post("/api/tasks/{task_id}/cancel")
def cancel_task(task_id: str) -> dict[str, Any]:
    task = get_task(task_id)
    with task.lock:
        if task.status == "cancelling":
            return task.snapshot()
        if task.status not in {"queued", "running"}:
            raise HTTPException(status_code=409, detail="任务当前不可取消")
        if task.status == "queued" and task.runtime is None:
            task.status = "cancelled"
            task.error = "任务已终止"
            task.finished_at = datetime.now().isoformat(timespec="seconds")
            task.emit("status", {"status": "cancelled", "message": "任务已终止"})
            return task.snapshot()
        runtime = task.runtime
        if runtime is not None:
            runtime.cancel_requested = True
            with runtime.lock:
                process = runtime.process
        else:
            process = None
        task.status = "cancelling"
        task.emit("status", {"status": "cancelling", "message": "正在终止当前任务"})

    if process is not None and process.poll() is None:
        _signal_process_tree(process, force=False)
        threading.Thread(
            target=_force_stop_after,
            args=(task, runtime, process),
            name=f"stop-{task.id[:8]}",
            daemon=True,
        ).start()
    return task.snapshot()


def _signal_process_tree(process: subprocess.Popen[str], *, force: bool) -> None:
    """Stop only this task's process group, including grandchildren."""
    if process.poll() is not None:
        return
    try:
        if os.name == "nt":
            if force:
                subprocess.run(
                    ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    timeout=5, check=False,
                )
            else:
                process.send_signal(signal.CTRL_BREAK_EVENT)
        else:
            os.killpg(os.getpgid(process.pid), signal.SIGKILL if force else signal.SIGTERM)
    except (OSError, ProcessLookupError, subprocess.SubprocessError, ValueError):
        try:
            process.kill() if force else process.terminate()
        except OSError:
            pass


def _force_stop_after(
    task: TaskRecord,
    runtime: TaskRuntime,
    process: subprocess.Popen[str],
    grace_seconds: float = 6.0,
) -> None:
    try:
        process.wait(timeout=grace_seconds)
        return
    except subprocess.TimeoutExpired:
        pass
    if runtime.cancel_requested and process.poll() is None:
        task.emit("log", {"message": "温和终止超时，正在强制结束该任务的子并发数", "level": "warning"})
        _signal_process_tree(process, force=True)


@app.get("/api/tasks/{task_id}/events")
async def task_events(task_id: str, request: Request, after: int = 0) -> StreamingResponse:
    task = get_task(task_id)

    async def stream():
        cursor = max(0, after)
        quiet_since = time.monotonic()
        while True:
            if await request.is_disconnected():
                break
            with task.lock:
                events = [event for event in task.events if int(event["sequence"]) > cursor]
                finished = task.status in {"completed", "failed", "cancelled"}
            if events:
                for event in events:
                    cursor = int(event["sequence"])
                    payload = json.dumps(event, ensure_ascii=False)
                    yield f"id: {cursor}\nevent: {event['type']}\ndata: {payload}\n\n"
                quiet_since = time.monotonic()
            elif finished:
                break
            elif time.monotonic() - quiet_since >= 15:
                yield ": keepalive\n\n"
                quiet_since = time.monotonic()
            await asyncio.sleep(0.25)

    return StreamingResponse(stream(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.get("/favicon.ico", include_in_schema=False)
def favicon() -> Response:
    """页面暂未使用独立 favicon，避免浏览器产生无意义的 404。"""
    return Response(status_code=204)


@app.get("/{full_path:path}", include_in_schema=False)
def frontend(full_path: str):
    """本地开发时直接提供 React 构建产物，并支持 React Router 回退。

    生产环境仍可由 Nginx 直接提供同一份 dist；此路由不会影响 /api/*。
    """
    if full_path == "api" or full_path.startswith("api/"):
        return JSONResponse({"detail": "Not Found"}, status_code=404)

    index_path = FRONTEND_DIST / "index.html"
    if not index_path.is_file():
        return JSONResponse(
            {
                "detail": "前端尚未构建",
                "hint": "请在 frontend 目录运行 npm install 和 npm run build",
            },
            status_code=503,
        )

    requested = (FRONTEND_DIST / full_path).resolve()
    try:
        requested.relative_to(FRONTEND_DIST.resolve())
    except ValueError:
        return JSONResponse({"detail": "Not Found"}, status_code=404)

    if full_path and requested.is_file():
        return FileResponse(requested)
    return FileResponse(index_path)
