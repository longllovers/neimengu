"""跨文件写入保护。"""

from __future__ import annotations

import os
import time
import uuid
from contextlib import contextmanager
from pathlib import Path


@contextmanager
def output_file_lock(
    target: Path,
    *,
    timeout: float = 6 * 60 * 60,
    poll_interval: float = 0.5,
):
    """同一目标文件同一时间只允许一个写入。

    锁文件超过 ``timeout`` 会被视为异常退出遗留的陈旧锁。
    """
    target = Path(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    lock_path = target.with_name(f".{target.name}.lock")
    token = f"{os.getpid()}:{uuid.uuid4().hex}"
    started = time.monotonic()

    while True:
        try:
            descriptor = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            try:
                lock_age = time.time() - lock_path.stat().st_mtime
                if lock_age > timeout:
                    lock_path.unlink()
                    continue
            except FileNotFoundError:
                continue
            if time.monotonic() - started >= timeout:
                raise TimeoutError(f"等待文件写入锁超时：{target}")
            time.sleep(poll_interval)
            continue

        try:
            os.write(descriptor, token.encode("ascii"))
        finally:
            os.close(descriptor)
        break

    try:
        yield
    finally:
        try:
            if lock_path.read_text(encoding="ascii") == token:
                lock_path.unlink()
        except FileNotFoundError:
            pass


def temporary_output_path(target: Path) -> Path:
    """返回与目标同目录、不会与其他重名的临时路径。"""
    target = Path(target)
    return target.with_name(f".{target.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
