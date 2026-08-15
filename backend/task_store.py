"""SQLite persistence for task history, parameters, and emitted UI events."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any


DATABASE_PATH = Path(__file__).resolve().parents[1] / ".geo_workbench_tasks.sqlite3"


def _connect() -> sqlite3.Connection:
    connection = sqlite3.connect(DATABASE_PATH, timeout=30)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA busy_timeout=30000")
    return connection


def initialize() -> None:
    with _connect() as connection:
        connection.execute("PRAGMA journal_mode=WAL")
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS tasks (
                id TEXT PRIMARY KEY,
                tool_id TEXT NOT NULL,
                tool_name TEXT NOT NULL,
                parameters_json TEXT NOT NULL,
                status TEXT NOT NULL,
                created_at TEXT NOT NULL,
                started_at TEXT,
                finished_at TEXT,
                result_json TEXT,
                error TEXT,
                last_sequence INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS task_events (
                task_id TEXT NOT NULL,
                sequence INTEGER NOT NULL,
                event_json TEXT NOT NULL,
                PRIMARY KEY (task_id, sequence),
                FOREIGN KEY (task_id) REFERENCES tasks(id) ON DELETE CASCADE
            );
            CREATE INDEX IF NOT EXISTS idx_tasks_created_at ON tasks(created_at DESC);
            """
        )


def _save_task(connection: sqlite3.Connection, task: dict[str, Any]) -> None:
    connection.execute(
        """
        INSERT INTO tasks (
            id, tool_id, tool_name, parameters_json, status, created_at,
            started_at, finished_at, result_json, error, last_sequence
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(id) DO UPDATE SET
            tool_id=excluded.tool_id,
            tool_name=excluded.tool_name,
            parameters_json=excluded.parameters_json,
            status=excluded.status,
            created_at=excluded.created_at,
            started_at=excluded.started_at,
            finished_at=excluded.finished_at,
            result_json=excluded.result_json,
            error=excluded.error,
            last_sequence=excluded.last_sequence
        """,
        (
            task["id"], task["tool_id"], task["tool_name"],
            json.dumps(task.get("parameters", {}), ensure_ascii=False),
            task["status"], task["created_at"], task.get("started_at"),
            task.get("finished_at"),
            json.dumps(task.get("result"), ensure_ascii=False),
            task.get("error"), int(task.get("last_sequence", 0)),
        ),
    )


def save_task(task: dict[str, Any]) -> None:
    with _connect() as connection:
        _save_task(connection, task)


def save_event(task_id: str, event: dict[str, Any]) -> None:
    with _connect() as connection:
        connection.execute(
            "INSERT OR REPLACE INTO task_events (task_id, sequence, event_json) VALUES (?, ?, ?)",
            (task_id, int(event["sequence"]), json.dumps(event, ensure_ascii=False)),
        )


def save_emission(task: dict[str, Any], event: dict[str, Any]) -> None:
    """Persist a task snapshot and its new event in one transaction."""
    with _connect() as connection:
        _save_task(connection, task)
        connection.execute(
            "INSERT OR REPLACE INTO task_events (task_id, sequence, event_json) VALUES (?, ?, ?)",
            (task["id"], int(event["sequence"]), json.dumps(event, ensure_ascii=False)),
        )


def load_tasks(limit: int = 300) -> list[dict[str, Any]]:
    with _connect() as connection:
        rows = connection.execute(
            "SELECT * FROM tasks ORDER BY created_at DESC LIMIT ?", (limit,)
        ).fetchall()
        records: list[dict[str, Any]] = []
        for row in reversed(rows):
            events = connection.execute(
                "SELECT event_json FROM task_events WHERE task_id = ? ORDER BY sequence",
                (row["id"],),
            ).fetchall()
            records.append(
                {
                    "id": row["id"], "tool_id": row["tool_id"],
                    "tool_name": row["tool_name"],
                    "parameters": json.loads(row["parameters_json"] or "{}"),
                    "status": row["status"], "created_at": row["created_at"],
                    "started_at": row["started_at"], "finished_at": row["finished_at"],
                    "result": json.loads(row["result_json"]) if row["result_json"] else None,
                    "error": row["error"],
                    "events": [json.loads(item["event_json"]) for item in events],
                }
            )
        return records


def delete_tasks(task_ids: list[str]) -> None:
    if not task_ids:
        return
    placeholders = ",".join("?" for _ in task_ids)
    with _connect() as connection:
        connection.execute(f"DELETE FROM task_events WHERE task_id IN ({placeholders})", task_ids)
        connection.execute(f"DELETE FROM tasks WHERE id IN ({placeholders})", task_ids)
