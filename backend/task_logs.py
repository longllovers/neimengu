"""Append-only text logs for each workbench task."""

from __future__ import annotations

import sys
import re
from pathlib import Path
from typing import Any


LOG_DIRECTORY = Path.cwd() / "log" / "tasks"
_reported_errors: set[str] = set()


def _safe_filename(value: str) -> str:
    cleaned = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", str(value)).strip(" .")
    return cleaned or "未命名工具"


def relative_path(task_id: str, tool_name: str, created_at: str) -> str:
    date = created_at[:10] if re.fullmatch(r"\d{4}-\d{2}-\d{2}.*", created_at) else "未知日期"
    return f"log/tasks/{date}/{task_id}-{_safe_filename(tool_name)}.log"


def append_event(task_id: str, tool_name: str, created_at: str, event: dict[str, Any]) -> None:
    """Append user-relevant task output without affecting task execution on I/O failure."""
    event_type = str(event.get("type", ""))
    if event_type == "log":
        label = str(event.get("level", "info")).upper()
        message = str(event.get("message", ""))
    elif event_type == "status":
        label = "STATUS"
        status = str(event.get("status", ""))
        message = f"{status} {event.get('message', '')}".strip()
    else:
        return

    try:
        relative = Path(relative_path(task_id, tool_name, created_at)).relative_to("log/tasks")
        path = LOG_DIRECTORY / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8", newline="\n") as stream:
            stream.write(f"{event.get('time', '')} [{label}] {message}\n")
    except OSError as exc:
        error_key = f"{task_id}:{type(exc).__name__}:{exc}"
        if error_key not in _reported_errors:
            _reported_errors.add(error_key)
            log_file = relative_path(task_id, tool_name, created_at)
            print(f"任务日志写入失败（{log_file}）：{exc}", file=sys.stderr, flush=True)
