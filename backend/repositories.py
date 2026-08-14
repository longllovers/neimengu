"""Read-only code repository browser configuration and safe filesystem helpers."""

from __future__ import annotations

import mimetypes
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]

# 后续增加同类页面时，只需在这里增加一项，并在前端 REPOSITORIES 增加入口。
CODE_REPOSITORIES = {
    "esa": {"name": "欧空局完整代码", "root": PROJECT_ROOT / "欧空局", "ignore": set()},
    "town-clip": {
        "name": "镇裁切完整代码", "root": PROJECT_ROOT / "镇裁切",
        "ignore": {".venv", "__pycache__", ".git", ".pytest_cache"},
    },
}

PREVIEW_LIMIT = 2 * 1024 * 1024
TEXT_EXTENSIONS = {
    ".py", ".js", ".jsx", ".ts", ".tsx", ".html", ".css", ".scss", ".json",
    ".md", ".txt", ".toml", ".yaml", ".yml", ".xml", ".sh", ".ini", ".cfg",
    ".conf", ".lock", ".csv", ".gitignore",
}


def repository(repo_id: str) -> dict[str, Any]:
    item = CODE_REPOSITORIES.get(repo_id)
    if item is None or not item["root"].is_dir():
        raise KeyError(repo_id)
    return item


def safe_path(repo_id: str, relative: str = "") -> tuple[dict[str, Any], Path]:
    item = repository(repo_id)
    root = item["root"].resolve()
    target = (root / relative.replace("\\", "/")).resolve()
    try:
        target.relative_to(root)
    except ValueError as exc:
        raise PermissionError("路径超出代码库范围") from exc
    return item, target


def file_node(path: Path, root: Path, ignored: set[str]) -> dict[str, Any]:
    relative = path.relative_to(root).as_posix()
    if path.is_dir():
        children = sorted((child for child in path.iterdir() if child.name not in ignored), key=lambda value: (value.is_file(), value.name.lower()))
        return {"name": path.name, "path": relative, "type": "directory", "children": [file_node(child, root, ignored) for child in children]}
    return {"name": path.name, "path": relative, "type": "file", "size": path.stat().st_size}


def repository_tree(repo_id: str) -> dict[str, Any]:
    item = repository(repo_id)
    root = item["root"].resolve()
    ignored = item.get("ignore", set())
    children = sorted((child for child in root.iterdir() if child.name not in ignored), key=lambda value: (value.is_file(), value.name.lower()))
    return {
        "id": repo_id, "name": item["name"], "folder": root.name,
        "tree": [file_node(child, root, ignored) for child in children],
    }


def preview_file(repo_id: str, relative: str) -> dict[str, Any]:
    _, target = safe_path(repo_id, relative)
    if not target.is_file():
        raise FileNotFoundError(relative)
    size = target.stat().st_size
    suffix = target.suffix.lower() or target.name.lower()
    mime = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
    if size > PREVIEW_LIMIT or (suffix not in TEXT_EXTENSIONS and not mime.startswith("text/")):
        return {"path": relative, "name": target.name, "size": size, "previewable": False, "mime": mime}
    raw = target.read_bytes()
    if b"\x00" in raw[:8192]:
        return {"path": relative, "name": target.name, "size": size, "previewable": False, "mime": mime}
    content = raw.decode("utf-8", errors="replace")
    return {"path": relative, "name": target.name, "size": size, "previewable": True, "mime": mime, "content": content}
