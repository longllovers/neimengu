"""Run legacy function-style tools in an isolated, cancellable process."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

from .adapters import TaskRuntime, WORKER_EVENT_PREFIX, run_custom


def emit(event_type: str, data: dict[str, Any]) -> None:
    print(
        WORKER_EVENT_PREFIX + json.dumps({"type": event_type, **data}, ensure_ascii=False),
        flush=True,
    )


def main() -> None:
    if len(sys.argv) != 3:
        raise SystemExit("用法: python -m backend.custom_worker <tool_id> <payload.json>")
    tool_id, payload_file = sys.argv[1], Path(sys.argv[2])
    payload = json.loads(payload_file.read_text(encoding="utf-8"))
    result = run_custom(TaskRuntime(emit=emit), tool_id, payload)
    emit("result", result or {})


if __name__ == "__main__":
    main()
