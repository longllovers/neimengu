"""基于 Python 标准库的 Shapefile 检查 Web 服务。

HTTP 层只使用标准库 ThreadingHTTPServer；空间检查复用 check_shp.py。
"""

from __future__ import annotations

import argparse
import csv
import json
import mimetypes
import tempfile
import threading
import webbrowser
from email.parser import BytesParser
from email.policy import default
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path, PurePath
from urllib.parse import unquote, urlparse

from check_shp import (
    DEFAULT_MIN_OVERLAP_SQM,
    check_shp,
    merge_small_features,
)


ROOT = Path(__file__).resolve().parent
WEB_ROOT = ROOT / "web"
DEFAULT_OUTPUT_DIR = ROOT / "CSV"
DEFAULT_MERGE_OUTPUT_DIR = ROOT / "合并结果"
MAX_UPLOAD_BYTES = 1024 * 1024 * 1024
ALLOWED_EXTENSIONS = {
    ".shp",
    ".shx",
    ".dbf",
    ".prj",
    ".cpg",
    ".qix",
    ".sbn",
    ".sbx",
}
OUTPUT_LOCKS: dict[str, threading.Lock] = {}
OUTPUT_LOCKS_GUARD = threading.Lock()


def convert_network_path(path: object | None) -> str | None:
    """把指定网段的 Windows 网络共享路径转换为 Linux 挂载路径。"""
    if path is None:
        return path

    path = str(path).strip()
    if not path:
        return path

    # 把 Windows 的反斜杠转换成 Linux 风格斜杠。
    path = path.replace("\\", "/")

    prefix_mapping: list[tuple[str, str]] = []
    for i in range(1, 256):
        mappings = (
            ("data", "/media/cangling/nas_folder"),
            ("新建卷", "/media/cangling/xinjianjuan"),
            ("datadisk2", "/media/cangling/EAGET"),
            ("新加卷", "/media/cangling/xinjiajuan"),
        )
        for share_name, linux_prefix in mappings:
            prefix_mapping.append(
                (f"//10.10.10.{i}/{share_name}", linux_prefix)
            )
            prefix_mapping.append(
                (f"/10.10.10.{i}/{share_name}", linux_prefix)
            )
            prefix_mapping.append(
                (f"10.10.10.{i}/{share_name}", linux_prefix)
            )

    for windows_prefix, linux_prefix in prefix_mapping:
        # 必须完整匹配共享目录名，避免 data 错误匹配 datadisk2。
        if path == windows_prefix:
            return linux_prefix
        if path.startswith(windows_prefix + "/"):
            relative_path = path[len(windows_prefix) :]
            return linux_prefix + relative_path

    return path


def output_lock(path: Path) -> threading.Lock:
    key = str(path.resolve()).lower()
    with OUTPUT_LOCKS_GUARD:
        return OUTPUT_LOCKS.setdefault(key, threading.Lock())


def safe_upload_name(filename: str) -> str:
    """只保留文件名，阻止上传路径跳出临时目录。"""
    name = PurePath(filename.replace("\\", "/")).name
    if not name or name in {".", ".."}:
        raise ValueError("上传文件名无效。")
    return name


def read_csv(path: Path) -> tuple[list[str], list[dict[str, str]]]:
    with path.open("r", encoding="utf-8-sig", newline="") as file:
        reader = csv.DictReader(file)
        return list(reader.fieldnames or []), list(reader)


class ShpRequestHandler(BaseHTTPRequestHandler):
    server_version = "ShpCheckServer/1.0"

    def log_message(self, format: str, *args: object) -> None:
        message = format % args
        print(f"[{threading.current_thread().name}] {self.address_string()} {message}")

    def send_json(self, data: object, status: HTTPStatus = HTTPStatus.OK) -> None:
        payload = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(payload)

    def send_static(self, relative_path: str) -> None:
        requested = (WEB_ROOT / relative_path).resolve()
        try:
            requested.relative_to(WEB_ROOT.resolve())
        except ValueError:
            self.send_error(HTTPStatus.NOT_FOUND)
            return

        if not requested.is_file():
            self.send_error(HTTPStatus.NOT_FOUND)
            return

        content = requested.read_bytes()
        content_type, _ = mimetypes.guess_type(requested.name)
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type or "application/octet-stream")
        self.send_header("Content-Length", str(len(content)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(content)

    def do_GET(self) -> None:
        path = unquote(urlparse(self.path).path)
        if path == "/":
            self.send_static("index.html")
        elif path == "/api/config":
            self.send_json(
                {
                    "defaultOutputDir": str(DEFAULT_OUTPUT_DIR),
                    "defaultMergeOutputDir": str(DEFAULT_MERGE_OUTPUT_DIR),
                }
            )
        elif path.startswith("/static/"):
            self.send_static(path.removeprefix("/static/"))
        else:
            self.send_error(HTTPStatus.NOT_FOUND)

    def parse_multipart(self) -> tuple[dict[str, str], list[tuple[str, bytes]]]:
        content_type = self.headers.get("Content-Type", "")
        if "multipart/form-data" not in content_type:
            raise ValueError("请求必须使用 multipart/form-data。")

        try:
            content_length = int(self.headers.get("Content-Length", "0"))
        except ValueError as exc:
            raise ValueError("Content-Length 无效。") from exc
        if content_length <= 0:
            raise ValueError("没有收到上传内容。")
        if content_length > MAX_UPLOAD_BYTES:
            raise ValueError("上传内容超过 1 GB 限制。")

        body = self.rfile.read(content_length)
        envelope = (
            f"Content-Type: {content_type}\r\nMIME-Version: 1.0\r\n\r\n".encode()
            + body
        )
        message = BytesParser(policy=default).parsebytes(envelope)
        fields: dict[str, str] = {}
        files: list[tuple[str, bytes]] = []

        for part in message.iter_parts():
            field_name = part.get_param("name", header="content-disposition")
            filename = part.get_filename()
            payload = part.get_payload(decode=True) or b""
            if filename is not None:
                files.append((safe_upload_name(filename), payload))
            elif field_name:
                charset = part.get_content_charset() or "utf-8"
                fields[field_name] = payload.decode(charset, errors="replace")
        return fields, files

    def do_POST(self) -> None:
        if urlparse(self.path).path != "/api/check":
            self.send_error(HTTPStatus.NOT_FOUND)
            return

        try:
            fields, files = self.parse_multipart()
            if not files:
                raise ValueError("请拖入 Shapefile 文件。")

            output_text = str(
                convert_network_path(fields.get("output_dir", "")) or ""
            ).strip()
            merge_output_text = str(
                convert_network_path(fields.get("merge_output_dir", "")) or ""
            ).strip()
            merge_small = fields.get("merge_small", "").lower() in {
                "1",
                "true",
                "yes",
                "on",
            }
            output_dir = (
                Path(output_text).expanduser()
                if output_text
                else DEFAULT_OUTPUT_DIR
            )
            if not output_dir.is_absolute():
                output_dir = (ROOT / output_dir).resolve()
            output_dir.mkdir(parents=True, exist_ok=True)

            merge_output_dir = (
                Path(merge_output_text).expanduser()
                if merge_output_text
                else DEFAULT_MERGE_OUTPUT_DIR
            )
            if not merge_output_dir.is_absolute():
                merge_output_dir = (ROOT / merge_output_dir).resolve()
            if merge_small:
                merge_output_dir.mkdir(parents=True, exist_ok=True)

            results: list[dict[str, object]] = []
            with tempfile.TemporaryDirectory(
                prefix="shp-check-", dir=ROOT
            ) as temp_name:
                temp_dir = Path(temp_name)
                seen_names: set[str] = set()
                for filename, payload in files:
                    suffix = Path(filename).suffix.lower()
                    if suffix not in ALLOWED_EXTENSIONS:
                        continue
                    key = filename.lower()
                    if key in seen_names:
                        raise ValueError(f"存在同名上传文件：{filename}")
                    seen_names.add(key)
                    (temp_dir / filename).write_bytes(payload)

                shp_files = sorted(temp_dir.glob("*.shp"))
                if not shp_files:
                    raise ValueError("没有找到 .shp 文件。")

                for shp_path in shp_files:
                    missing = [
                        suffix
                        for suffix in (".shx", ".dbf", ".prj")
                        if not shp_path.with_suffix(suffix).exists()
                    ]
                    if missing:
                        missing_text = "、".join(missing)
                        raise ValueError(
                            f"{shp_path.name} 缺少配套文件：{missing_text}。"
                            "请把同名的 .shp、.shx、.dbf、.prj 一起拖入。"
                        )

                    csv_path = output_dir / f"{shp_path.stem}.csv"
                    with output_lock(csv_path):
                        merge_report: dict[str, object] | None = None
                        check_path = shp_path
                        if merge_small:
                            modified_path = merge_output_dir / shp_path.name
                            merge_report = merge_small_features(
                                shp_path,
                                modified_path,
                                id_field=None,
                                min_mu=0.1,
                            )
                            # 勾选合并时，CSV 必须反映合并后的实际检查结果。
                            check_path = modified_path

                        overlap_count, small_count = check_shp(
                            check_path,
                            csv_path,
                            id_field=None,
                            min_mu=0.1,
                            min_overlap_sqm=DEFAULT_MIN_OVERLAP_SQM,
                        )

                        # 浏览器不会提供源文件的本机绝对路径，因此记录上传文件名；
                        # 同时把可确认的 CSV 完整保存路径写入每一行。
                        columns, rows = read_csv(csv_path)
                        columns.append("CSV保存路径")
                        display_source = shp_path.name
                        saved_csv_path = str(csv_path.resolve())

                        if merge_small:
                            columns.extend(["自动合并到ID", "合并后SHP路径"])

                        for row in rows:
                            row["文件路径"] = display_source
                            row["CSV保存路径"] = saved_csv_path
                            if merge_small:
                                row["自动合并到ID"] = ""
                                row["合并后SHP路径"] = str(
                                    merge_report["output_path"]
                                )

                        # 保留合并审计信息，但不再把它标记为“小于 0.1 亩”。
                        if merge_report:
                            for assignment in merge_report["assignments"]:
                                rows.append(
                                    {
                                        "文件路径": display_source,
                                        "问题类型": "已自动合并",
                                        "ID_1": str(assignment["source_id"]),
                                        "ID_2": str(assignment["target_id"]),
                                        "要素面积_亩": "",
                                        "重叠面积_亩": "",
                                        "说明": (
                                            "小面积面已合并，目标选择方式："
                                            + str(assignment["method"])
                                        ),
                                        "CSV保存路径": saved_csv_path,
                                        "自动合并到ID": str(
                                            assignment["target_id"]
                                        ),
                                        "合并后SHP路径": str(
                                            merge_report["output_path"]
                                        ),
                                    }
                                )
                            for deleted_id in merge_report["deleted_ids"]:
                                rows.append(
                                    {
                                        "文件路径": display_source,
                                        "问题类型": "已自动删除",
                                        "ID_1": str(deleted_id),
                                        "ID_2": "",
                                        "要素面积_亩": "",
                                        "重叠面积_亩": "",
                                        "说明": (
                                            "小面积面没有接触或相交的"
                                            "非小面积面，已从结果中删除"
                                        ),
                                        "CSV保存路径": saved_csv_path,
                                        "自动合并到ID": "",
                                        "合并后SHP路径": str(
                                            merge_report["output_path"]
                                        ),
                                    }
                                )
                        with csv_path.open(
                            "w", encoding="utf-8-sig", newline=""
                        ) as file:
                            writer = csv.DictWriter(file, fieldnames=columns)
                            writer.writeheader()
                            writer.writerows(rows)

                    results.append(
                        {
                            "sourceName": shp_path.name,
                            "csvPath": saved_csv_path,
                            "overlapCount": overlap_count,
                            "smallCount": small_count,
                            "columns": columns,
                            "rows": rows,
                            "mergeEnabled": merge_small,
                            "mergedCount": (
                                int(merge_report["merged_count"])
                                if merge_report
                                else 0
                            ),
                            "skippedCount": (
                                int(merge_report["skipped_count"])
                                if merge_report
                                else 0
                            ),
                            "nearestCount": (
                                int(merge_report["nearest_count"])
                                if merge_report
                                else 0
                            ),
                            "deletedCount": (
                                int(merge_report["deleted_count"])
                                if merge_report
                                else 0
                            ),
                            "modifiedShpPath": (
                                str(merge_report["output_path"])
                                if merge_report
                                else ""
                            ),
                        }
                    )

            self.send_json({"ok": True, "results": results})
        except Exception as exc:
            self.send_json(
                {"ok": False, "error": str(exc)},
                status=HTTPStatus.BAD_REQUEST,
            )


class ShpThreadingHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="启动 Shapefile 检查页面。")
    parser.add_argument("--host", default="0.0.0.0", help="监听地址。")
    parser.add_argument("--port", type=int, default=9001, help="监听端口。")
    parser.add_argument(
        "--no-browser",
        action="store_true",
        help="启动后不自动打开浏览器。",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    DEFAULT_OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    server = ShpThreadingHTTPServer((args.host, args.port), ShpRequestHandler)
    url = f"http://127.0.0.1:{args.port}"
    print(f"Shapefile 检查页面已启动：{url}")
    print("按 Ctrl+C 停止服务。")
    if not args.no_browser:
        threading.Timer(0.7, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n正在停止服务……")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
