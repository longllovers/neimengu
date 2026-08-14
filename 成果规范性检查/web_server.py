from __future__ import annotations

import argparse
import json
import mimetypes
import re
import threading
import traceback
import uuid
from dataclasses import dataclass, field, fields
from datetime import datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from checker.report import write_json, write_pdf
from checker.scanner import check_delivery

PROJECT_ROOT = Path(__file__).resolve().parent
WEB_ROOT = PROJECT_ROOT / "web"
TAB_ID_RE = re.compile(r"^[A-Za-z0-9_-]{8,80}$")
PDF_LOCK = threading.Lock()


def convert_network_path(path: object) -> str | None:
    if path is None:
        return path

    path = str(path).strip().replace("\\", "/")
    if not path:
        return path

    prefix_mapping = (
        ("//10.10.10.11/data", "/mnt/nas_data"),
        ("//10.10.10.10/4np_share", "/mnt/data/4np/"),
        ("//10.10.10.10/nas_data", "/mnt/nas_data"),
    )
    for windows_prefix, linux_prefix in prefix_mapping:
        if path == windows_prefix:
            return linux_prefix
        if path.startswith(windows_prefix + "/"):
            return linux_prefix.rstrip("/") + path[len(windows_prefix):]
    return path



def get_ip_from_source_root(source_root: object) -> str:
    if source_root is None:
        return ""
    match = re.search(r"10\.10\.10\.\d+", str(source_root).strip())
    return match.group(0) if match else ""


def convert_linux_path_to_network_path(path: object, source_root: object = "") -> str | None:
    if path is None:
        return path

    path = str(path).strip().replace("\\", "/")
    if not path:
        return path

    source_text = str(source_root or "").strip().replace("\\", "/")
    nas_windows_root = (
        "//10.10.10.10/nas_data"
        if source_text == "//10.10.10.10/nas_data"
        or source_text.startswith("//10.10.10.10/nas_data/")
        else "//10.10.10.11/data"
    )
    prefix_mapping = (
        ("/mnt/data/4np", "//10.10.10.10/4np_share"),
        ("/mnt/nas_data", nas_windows_root),
    )
    for linux_prefix, windows_prefix in prefix_mapping:
        if path == linux_prefix or path.startswith(linux_prefix + "/"):
            return (windows_prefix + path[len(linux_prefix):]).replace("/", "\\")
    return path.replace("/", "\\")

class JobState:
    tab_id: str
    status: str = "idle"
    message: str = "等待运行"
    source_root: str = ""
    converted_root: str = ""
    output_root: str = ""
    converted_output_root: str = ""
    gdb_schema: str = "5-1"
    zpj_schema: str = "5-4"
    run_id: str = ""
    report_path: str = ""
    report_network_path: str = ""
    json_path: str = ""
    started_at: str = ""
    finished_at: str = ""
    passed: bool | None = None
    errors: int = 0
    warnings: int = 0
    exception: str = ""
    version: int = 0
    lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def public(self) -> dict[str, object]:
        data = {
            item.name: getattr(self, item.name)
            for item in fields(self)
            if item.name != "lock"
        }
        if self.report_path:
            data["pdf_url"] = f"/api/pdf?tab_id={self.tab_id}&v={self.version}"
        else:
            data["pdf_url"] = ""
        return data


class JobRegistry:
    def __init__(self) -> None:
        self._jobs: dict[str, JobState] = {}
        self._lock = threading.RLock()

    def get_or_create(self, tab_id: str) -> JobState:
        with self._lock:
            return self._jobs.setdefault(tab_id, JobState(tab_id=tab_id))

    def get(self, tab_id: str) -> JobState | None:
        with self._lock:
            return self._jobs.get(tab_id)


REGISTRY = JobRegistry()


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def run_check_job(
    state: JobState,
    source_root: str,
    output_root: str,
    county_boundary: Path,
    province_code: str,
    gdb_schema: str,
    zpj_schema: str,
) -> None:
    converted = convert_network_path(source_root) or ""
    converted_output = convert_network_path(output_root) or ""
    if not converted_output:
        source_path = Path(converted)
        converted_output = str(source_path.parent / f"{source_path.name}_检查结果")
    run_id = datetime.now().strftime("%Y%m%d_%H%M%S_") + uuid.uuid4().hex[:8]
    run_dir = Path(converted_output)
    report_path = run_dir / f"检查报告_{run_id}.pdf"
    json_path = run_dir / f"检查明细_{run_id}.json"
    with state.lock:
        state.status = "running"
        state.message = "正在运行"
        state.source_root = source_root
        state.converted_root = converted
        state.output_root = output_root
        state.converted_output_root = converted_output
        state.gdb_schema = gdb_schema
        state.zpj_schema = zpj_schema
        state.run_id = run_id
        state.report_path = ""
        state.report_network_path = ""
        state.json_path = ""
        state.started_at = _now()
        state.finished_at = ""
        state.passed = None
        state.errors = 0
        state.warnings = 0
        state.exception = ""

    print(f"[{_now()}] [任务 {state.tab_id}] 开始运行", flush=True)
    print(f"[任务 {state.tab_id}] 输入目录：{converted}", flush=True)
    print(f"[任务 {state.tab_id}] 输出目录：{converted_output}", flush=True)
    print(
        f"[任务 {state.tab_id}] 核验方案：GDB 表 {gdb_schema}；ELJDZPJ 表 {zpj_schema}",
        flush=True,
    )

    try:
        result = check_delivery(
            Path(converted),
            county_boundary,
            province_code=province_code,
            gdb_schema=gdb_schema,
            zpj_schema=zpj_schema,
        )
        write_json(result, json_path)
        # Matplotlib 使用全局字体缓存；只串行化 PDF 绘制，数据检查仍可并行。
        with PDF_LOCK:
            write_pdf(result, report_path)
        summary = result.to_dict()["summary"]
        with state.lock:
            state.status = "completed"
            state.message = "运行完成"
            state.report_path = str(report_path.resolve())
            state.report_network_path = (
                convert_linux_path_to_network_path(report_path.resolve(), source_root)
                or str(report_path.resolve())
            )
            state.json_path = str(json_path.resolve())
            state.finished_at = _now()
            state.passed = result.passed
            state.errors = int(summary["errors"])
            state.warnings = int(summary["warnings"])
            state.version += 1
        print(
            f"[{_now()}] [任务 {state.tab_id}] 运行完成："
            f"{'通过' if result.passed else '不通过'}；"
            f"错误 {summary['errors']} 项，警告 {summary['warnings']} 项",
            flush=True,
        )
        print(f"[任务 {state.tab_id}] PDF：{report_path.resolve()}", flush=True)
    except Exception as exc:
        with state.lock:
            state.status = "failed"
            state.message = "运行失败"
            state.finished_at = _now()
            state.exception = f"{type(exc).__name__}: {exc}"
        print(
            f"[{_now()}] [任务 {state.tab_id}] 运行失败：{type(exc).__name__}: {exc}",
            flush=True,
        )
        traceback.print_exc()


class CheckerHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(
        self,
        server_address: tuple[str, int],
        handler: type[BaseHTTPRequestHandler],
        *,
        county_boundary: Path,
        province_code: str,
    ) -> None:
        super().__init__(server_address, handler)
        self.county_boundary = county_boundary
        self.province_code = province_code


class RequestHandler(BaseHTTPRequestHandler):
    server_version = "RemoteCheckHTTP/1.0"

    @property
    def app_server(self) -> CheckerHTTPServer:
        return self.server  # type: ignore[return-value]

    def log_message(self, format: str, *args: object) -> None:
        # 页面每秒轮询任务状态；关闭 HTTP 访问日志，避免终端被请求信息刷屏。
        return

    def _send_bytes(
        self,
        content: bytes,
        content_type: str,
        status: HTTPStatus = HTTPStatus.OK,
        *,
        disposition: str | None = None,
    ) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(content)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        if disposition:
            self.send_header("Content-Disposition", disposition)
        self.end_headers()
        self.wfile.write(content)

    def _send_json(self, data: object, status: HTTPStatus = HTTPStatus.OK) -> None:
        self._send_bytes(
            json.dumps(data, ensure_ascii=False).encode("utf-8"),
            "application/json; charset=utf-8",
            status,
        )

    def _read_json(self) -> dict[str, object]:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError as exc:
            raise ValueError("Content-Length 无效") from exc
        if length <= 0 or length > 64 * 1024:
            raise ValueError("请求内容为空或过大")
        raw = self.rfile.read(length)
        data = json.loads(raw.decode("utf-8"))
        if not isinstance(data, dict):
            raise ValueError("请求必须是 JSON 对象")
        return data

    @staticmethod
    def _tab_id(query_or_data: dict[str, object]) -> str:
        value = query_or_data.get("tab_id", "")
        tab_id = str(value).strip()
        if not TAB_ID_RE.fullmatch(tab_id):
            raise ValueError("标签页 ID 无效")
        return tab_id

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        if parsed.path == "/":
            return self._serve_static("index.html")
        if parsed.path.startswith("/static/"):
            return self._serve_static(parsed.path.removeprefix("/static/"))
        query = {key: values[0] for key, values in parse_qs(parsed.query).items()}
        try:
            if parsed.path == "/api/status":
                tab_id = self._tab_id(query)
                state = REGISTRY.get_or_create(tab_id)
                with state.lock:
                    return self._send_json(state.public())
            if parsed.path == "/api/pdf":
                tab_id = self._tab_id(query)
                state = REGISTRY.get(tab_id)
                if not state:
                    return self._send_json({"error": "任务不存在"}, HTTPStatus.NOT_FOUND)
                with state.lock:
                    report_path = Path(state.report_path) if state.report_path else None
                if not report_path or not report_path.is_file():
                    return self._send_json({"error": "PDF 尚未生成"}, HTTPStatus.NOT_FOUND)
                return self._send_bytes(
                    report_path.read_bytes(),
                    "application/pdf",
                    disposition='inline; filename="check-report.pdf"',
                )
            if parsed.path == "/api/details":
                tab_id = self._tab_id(query)
                state = REGISTRY.get(tab_id)
                if not state:
                    return self._send_json({"error": "任务不存在"}, HTTPStatus.NOT_FOUND)
                with state.lock:
                    json_path = Path(state.json_path) if state.json_path else None
                if not json_path or not json_path.is_file():
                    return self._send_json({"error": "明细尚未生成"}, HTTPStatus.NOT_FOUND)
                return self._send_bytes(json_path.read_bytes(), "application/json; charset=utf-8")
            return self._send_json({"error": "接口不存在"}, HTTPStatus.NOT_FOUND)
        except ValueError as exc:
            return self._send_json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)

    def _serve_static(self, relative: str) -> None:
        path = (WEB_ROOT / relative).resolve()
        try:
            path.relative_to(WEB_ROOT.resolve())
        except ValueError:
            return self._send_json({"error": "非法路径"}, HTTPStatus.FORBIDDEN)
        if not path.is_file():
            return self._send_json({"error": "文件不存在"}, HTTPStatus.NOT_FOUND)
        content_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        if content_type.startswith("text/") or content_type in {"application/javascript", "application/json"}:
            content_type += "; charset=utf-8"
        self._send_bytes(path.read_bytes(), content_type)

    def do_POST(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        if parsed.path != "/api/run":
            return self._send_json({"error": "接口不存在"}, HTTPStatus.NOT_FOUND)
        try:
            data = self._read_json()
            tab_id = self._tab_id(data)
            source_root = str(data.get("source_root", "")).strip()
            output_root = str(data.get("output_root", "")).strip()
            gdb_schema = str(data.get("gdb_schema", "5-1"))
            zpj_schema = str(data.get("zpj_schema", "5-4"))
            if not source_root:
                raise ValueError("请输入一级成果文件夹路径")
            if not output_root:
                raise ValueError("请输入结果输出文件夹路径")
            if gdb_schema not in {"5-1", "6-1"}:
                raise ValueError("GDB 核验方案无效")
            if zpj_schema not in {"5-4", "6-3"}:
                raise ValueError("ELJDZPJ 核验方案无效")
            state = REGISTRY.get_or_create(tab_id)
            with state.lock:
                if state.status in {"queued", "running"}:
                    return self._send_json(
                        {"error": "当前标签页已有任务正在运行"}, HTTPStatus.CONFLICT
                    )
                state.status = "queued"
                state.message = "任务已提交"
            thread = threading.Thread(
                target=run_check_job,
                name=f"check-{tab_id[:12]}",
                args=(
                    state,
                    source_root,
                    output_root,
                    self.app_server.county_boundary,
                    self.app_server.province_code,
                    gdb_schema,
                    zpj_schema,
                ),
                daemon=True,
            )
            thread.start()
            with state.lock:
                return self._send_json(state.public(), HTTPStatus.ACCEPTED)
        except (ValueError, json.JSONDecodeError, UnicodeDecodeError) as exc:
            return self._send_json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="遥感测量成果检查 Web 页面")
    parser.add_argument("--host", default="0.0.0.0", help="监听地址，默认 0.0.0.0")
    parser.add_argument("--port", type=int, default=9003, help="监听端口，默认 9003")
    parser.add_argument(
        "--county-boundary",
        type=Path,
        default=PROJECT_ROOT / "00县边界" / "15_县边界.shp",
        help="县界 Shapefile 路径",
    )
    parser.add_argument("--province-code", default="150000", help="6 位省代码")
    return parser


def main() -> None:
    args = build_parser().parse_args()
    server = CheckerHTTPServer(
        (args.host, args.port),
        RequestHandler,
        county_boundary=args.county_boundary.resolve(),
        province_code=args.province_code,
    )
    print(f"服务已启动：http://{args.host}:{args.port}")
    print(f"县界文件：{server.county_boundary}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n正在停止服务...")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
