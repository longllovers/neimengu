from __future__ import annotations

import json
import os
import tempfile
from http import HTTPStatus
from http.server import HTTPServer, SimpleHTTPRequestHandler
from pathlib import Path
from threading import Lock
from urllib.parse import urlparse


ROOT = Path(__file__).resolve().parent
CONFIG_FILE = ROOT / "config.json"
WEB_DIR = ROOT / "web"
CONFIG_LOCK = Lock()


def read_config() -> list[dict[str, str]]:
    with CONFIG_LOCK:
        with CONFIG_FILE.open("r", encoding="utf-8") as file:
            data = json.load(file)
    if not isinstance(data, list):
        raise ValueError("config.json 的根节点必须是数组")
    return data


def write_config(data: list[dict[str, str]]) -> None:
    """Write atomically so an interrupted save cannot damage config.json."""
    with CONFIG_LOCK:
        fd, temporary_name = tempfile.mkstemp(
            prefix="config-", suffix=".json.tmp", dir=ROOT
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as file:
                json.dump(data, file, ensure_ascii=False, indent=4)
                file.write("\n")
                file.flush()
                os.fsync(file.fileno())
            os.replace(temporary_name, CONFIG_FILE)
        except Exception:
            try:
                os.unlink(temporary_name)
            except FileNotFoundError:
                pass
            raise


def validate_item(body: object) -> dict[str, str]:
    if not isinstance(body, dict):
        raise ValueError("请求内容格式不正确")
    name = body.get("name")
    value = body.get("value")
    help_text = body.get("help", "")
    if not isinstance(name, str) or not name.strip():
        raise ValueError("名称不能为空")
    if not isinstance(value, str) or not value.strip():
        raise ValueError("地址不能为空")
    if not isinstance(help_text, str):
        raise ValueError("帮助内容必须是文本")
    name, value = name.strip(), value.strip()
    candidate = value if "://" in value else f"http://{value}"
    parsed = urlparse(candidate)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("请输入有效的 HTTP 或 HTTPS 地址")
    return {"name": name, "value": value, "help": help_text.strip()}


class PortalHandler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(WEB_DIR), **kwargs)

    def send_json(self, payload: object, status: HTTPStatus = HTTPStatus.OK) -> None:
        content = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(content)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(content)

    def read_json(self) -> object:
        length = int(self.headers.get("Content-Length", "0"))
        if length <= 0 or length > 1024 * 1024:
            raise ValueError("请求内容为空或过大")
        return json.loads(self.rfile.read(length).decode("utf-8"))

    def do_GET(self) -> None:
        if self.path == "/api/items":
            try:
                self.send_json(read_config())
            except (OSError, json.JSONDecodeError, ValueError) as error:
                self.send_json({"error": str(error)}, HTTPStatus.INTERNAL_SERVER_ERROR)
            return
        super().do_GET()

    def do_POST(self) -> None:
        if self.path != "/api/items":
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        try:
            item = validate_item(self.read_json())
            items = read_config()
            items.append(item)
            write_config(items)
            self.send_json(item, HTTPStatus.CREATED)
        except (ValueError, json.JSONDecodeError) as error:
            self.send_json({"error": str(error)}, HTTPStatus.BAD_REQUEST)
        except OSError as error:
            self.send_json({"error": f"保存失败：{error}"}, HTTPStatus.INTERNAL_SERVER_ERROR)

    def do_PUT(self) -> None:
        parts = self.path.strip("/").split("/")
        if len(parts) != 3 or parts[:2] != ["api", "items"]:
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        try:
            index = int(parts[2])
            item = validate_item(self.read_json())
            items = read_config()
            if index < 0 or index >= len(items):
                self.send_json({"error": "要修改的项目不存在"}, HTTPStatus.NOT_FOUND)
                return
            items[index] = item
            write_config(items)
            self.send_json(item)
        except (ValueError, json.JSONDecodeError) as error:
            self.send_json({"error": str(error)}, HTTPStatus.BAD_REQUEST)
        except OSError as error:
            self.send_json({"error": f"保存失败：{error}"}, HTTPStatus.INTERNAL_SERVER_ERROR)

    def do_DELETE(self) -> None:
        parts = self.path.strip("/").split("/")
        if len(parts) != 3 or parts[:2] != ["api", "items"]:
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        try:
            index = int(parts[2])
            items = read_config()
            if index < 0 or index >= len(items):
                self.send_json({"error": "要删除的项目不存在"}, HTTPStatus.NOT_FOUND)
                return
            deleted_item = items.pop(index)
            write_config(items)
            self.send_json(deleted_item)
        except ValueError as error:
            self.send_json({"error": str(error)}, HTTPStatus.BAD_REQUEST)
        except OSError as error:
            self.send_json({"error": f"删除失败：{error}"}, HTTPStatus.INTERNAL_SERVER_ERROR)


def run(host: str = "127.0.0.1", port: int = 8080) -> None:
    server = HTTPServer((host, port), PortalHandler)
    print(f"管理页面已启动：http://{host}:{port}")
    print("按 Ctrl+C 停止服务")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n服务已停止")
    finally:
        server.server_close()


if __name__ == "__main__":
    run()
