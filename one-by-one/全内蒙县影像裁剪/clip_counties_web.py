#!/usr/bin/env python
"""县级影像裁剪的本地 Web 控制界面（标准库 HTTPServer + SSE）。"""

from __future__ import annotations

import argparse
import html
import json
import os
import re
import secrets
import signal
import socket
import subprocess
import sys
import threading
import time
import webbrowser
from collections import deque
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse


EVENT_PREFIX = "@@CLIP_EVENT@@"
BASE_DIR = Path(__file__).resolve().parent
SCRIPT_PATH = BASE_DIR / "clip_counties.py"


DEFAULT_FORM = {
    "imagery_dir": "",
    "boundary": str(BASE_DIR / "00县边界"),
    "output_dir": str(BASE_DIR / "输出_0.5m"),
    "date1": "20250101",
    "date2": "20251231",
    "resolution": "0.5m",
    "name_template": "ELDOM{code}_{date1}_{date2}_{resolution}.tif",
    "index": str(BASE_DIR / "0.5m影像索引.sqlite"),
    "index_mode": "auto",
    "workers": "4",
    "buffer_distance_m": "50",
    "cpu_percent": "75",
    "gdal_memory_gb": "8",
    "index_workers": "4",
    "county": "",
    "pixel_size": "",
    "resampling": "near",
    "temp_dir": "",
    "gdal_bin": "",
    "overview_max_factor": "256",
    "creation_options": "",
    "overwrite": "",
}


class EventBroker:
    """保存有限事件历史，并通过条件变量推送给 SSE 客户端。"""

    def __init__(self, max_events: int = 20_000) -> None:
        self._condition = threading.Condition()
        self._events: deque[tuple[int, dict[str, object]]] = deque(maxlen=max_events)
        self._sequence = 0

    def publish(self, payload: dict[str, object]) -> int:
        with self._condition:
            self._sequence += 1
            self._events.append((self._sequence, payload))
            self._condition.notify_all()
            return self._sequence

    def wait_after(
        self,
        last_sequence: int,
        timeout: float = 15.0,
    ) -> list[tuple[int, dict[str, object]]]:
        with self._condition:
            if self._sequence <= last_sequence:
                self._condition.wait(timeout)
            return [(seq, payload) for seq, payload in self._events if seq > last_sequence]


class JobController:
    def __init__(self, broker: EventBroker) -> None:
        self._lock = threading.Lock()
        self.broker = broker
        self.process: subprocess.Popen[str] | None = None
        self.reader_thread: threading.Thread | None = None
        self.last_form = DEFAULT_FORM.copy()
        self.started_at: float | None = None
        self.last_exit_code: int | None = None
        self.stopping = False

    def snapshot(self) -> dict[str, object]:
        with self._lock:
            process = self.process
            running = process is not None and process.poll() is None
            return {
                "running": running,
                "pid": process.pid if running else None,
                "stopping": self.stopping and running,
                "started_at": self.started_at,
                "last_exit_code": self.last_exit_code,
                "form": self.last_form.copy(),
            }

    def start(self, form: dict[str, str]) -> tuple[bool, str]:
        with self._lock:
            if self.process is not None and self.process.poll() is None:
                return False, "已有裁剪任务正在运行，请先等待完成或停止任务。"
            try:
                command = build_command(form)
            except ValueError as exc:
                return False, str(exc)
            self.last_form = {**DEFAULT_FORM, **form}
            environment = os.environ.copy()
            environment["PYTHONUNBUFFERED"] = "1"
            popen_options: dict[str, object] = {}
            if os.name == "nt":
                popen_options["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
            else:
                popen_options["start_new_session"] = True
            self.broker.publish({"kind": "reset"})
            self.broker.publish({
                "kind": "terminal",
                "line": f"$ {subprocess.list2cmdline(command)}",
                "level": "command",
            })
            try:
                self.process = subprocess.Popen(
                    command,
                    cwd=BASE_DIR,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    bufsize=1,
                    env=environment,
                    **popen_options,
                )
            except OSError as exc:
                self.process = None
                return False, f"无法启动裁剪脚本：{exc}"
            self.started_at = time.time()
            self.last_exit_code = None
            self.stopping = False
            process = self.process
            self.reader_thread = threading.Thread(
                target=self._read_output,
                args=(process,),
                name="clip-output-reader",
                daemon=True,
            )
            self.reader_thread.start()
            return True, "任务已启动，正在并发处理。"

    def _read_output(self, process: subprocess.Popen[str]) -> None:
        assert process.stdout is not None
        try:
            for raw_line in process.stdout:
                line = raw_line.rstrip("\r\n")
                if line.startswith(EVENT_PREFIX):
                    try:
                        event = json.loads(line[len(EVENT_PREFIX):])
                        self.broker.publish({"kind": "clip_event", "data": event})
                    except json.JSONDecodeError:
                        print(line, flush=True)
                        self.broker.publish({"kind": "terminal", "line": line, "level": "error"})
                else:
                    # HTTP 服务所在终端同步显示裁剪脚本的原始输出。
                    print(line, flush=True)
                    self.broker.publish({"kind": "terminal", "line": line, "level": "normal"})
        finally:
            exit_code = process.wait()
            with self._lock:
                if self.process is process:
                    self.last_exit_code = exit_code
                    self.stopping = False
            self.broker.publish({"kind": "process_exit", "exit_code": exit_code})

    def stop(self) -> tuple[bool, str]:
        with self._lock:
            process = self.process
            if process is None or process.poll() is not None:
                return False, "当前没有正在运行的任务。"
            if self.stopping:
                return True, "任务正在停止，请稍候。"
            self.stopping = True
            terminate_thread = threading.Thread(
                target=self._terminate_process_tree,
                args=(process,),
                name=f"clip-stop-{process.pid}",
                daemon=True,
            )
            terminate_thread.start()
            return True, (
                "停止请求已接收，正在温和停止并清理并发任务；"
                "最多等待 8 秒。"
            )

    def _terminate_process_tree(self, process: subprocess.Popen[str]) -> None:
        """先温和停止全部并发任务，8 秒未退出再强制结束。"""
        pid = process.pid
        forced = False
        error_message = ""
        try:
            if os.name == "nt":
                try:
                    process.send_signal(signal.CTRL_BREAK_EVENT)
                    process.wait(timeout=8)
                except subprocess.TimeoutExpired:
                    forced = True
                    try:
                        subprocess.run(
                            ["taskkill", "/PID", str(pid), "/T", "/F"],
                            stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL,
                            check=False,
                            timeout=4,
                        )
                    except subprocess.TimeoutExpired:
                        process.kill()
            else:
                try:
                    process_group = os.getpgid(pid)
                    os.killpg(process_group, signal.SIGTERM)
                except ProcessLookupError:
                    return
                deadline = time.monotonic() + 8
                while time.monotonic() < deadline:
                    try:
                        os.killpg(process_group, 0)
                    except ProcessLookupError:
                        break
                    time.sleep(0.1)
                else:
                    forced = True
                    os.killpg(process_group, signal.SIGKILL)
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                forced = True
                process.kill()
        except OSError as exc:
            error_message = str(exc)
            try:
                process.kill()
                forced = True
            except OSError:
                pass
        finally:
            if error_message:
                line = f"停止并发任务时出现异常：{error_message}"
                level = "error"
            elif forced:
                line = "任务在 8 秒内未退出，已强制结束全部并发任务"
                level = "warning"
            else:
                line = "任务已完成清理并停止"
                level = "warning"
            self.broker.publish({"kind": "terminal", "line": line, "level": level})


class PageSession:
    """一个浏览器标签页对应一个完全独立的任务和事件流。"""

    def __init__(self, page_id: str) -> None:
        self.page_id = page_id
        self.broker = EventBroker()
        self.controller = JobController(self.broker)
        self.last_access = time.time()


class SessionRegistry:
    def __init__(self, idle_seconds: float = 24 * 60 * 60) -> None:
        self._lock = threading.Lock()
        self._sessions: dict[str, PageSession] = {}
        self._idle_seconds = idle_seconds

    def create(self) -> PageSession:
        with self._lock:
            self._cleanup_locked()
            page_id = secrets.token_urlsafe(24)
            session = PageSession(page_id)
            self._sessions[page_id] = session
            return session

    def get(self, page_id: str) -> PageSession | None:
        if not re.fullmatch(r"[A-Za-z0-9_-]{20,64}", page_id):
            return None
        with self._lock:
            session = self._sessions.get(page_id)
            if session is not None:
                session.last_access = time.time()
            return session

    def _cleanup_locked(self) -> None:
        now = time.time()
        expired: list[str] = []
        for page_id, session in self._sessions.items():
            running = bool(session.controller.snapshot()["running"])
            if not running and now - session.last_access > self._idle_seconds:
                expired.append(page_id)
        for page_id in expired:
            del self._sessions[page_id]


SESSIONS = SessionRegistry()


def convert_network_path(path: str | Path | None) -> str | None:
    """只转换两条已配置的 Windows UNC 共享路径，其他路径原样返回。"""
    if path is None:
        return path

    original = str(path).strip()
    if not original:
        return original

    normalized = original.replace("\\", "/")
    prefix_mapping = (
        ("//10.10.10.11/data", "/mnt/nas_data"),
        ("//10.10.10.10/4np_share", "/mnt/data/4np/"),
        ("//10.10.10.10/nas_data", "/mnt/nas_data")
    )

    for network_prefix, linux_prefix in prefix_mapping:
        # 只匹配共享根目录或其子目录，避免相似共享名被误转换。
        if normalized == network_prefix:
            return linux_prefix
        if normalized.startswith(network_prefix + "/"):
            relative_path = normalized[len(network_prefix):]
            return linux_prefix.rstrip("/") + relative_path

    return original


def get_ip_from_source_root(source_root: str | Path | None) -> str:
    if source_root is None:
        return ""
    match = re.search(r"10\.10\.10\.\d+", str(source_root).strip())
    return match.group(0) if match else ""


def convert_linux_path_to_network_path(
    path: str | Path | None,
    source_root: str | Path | None = "",
) -> str | None:
    """把两条已配置的 Linux 挂载路径还原成 Windows UNC，其他路径原样返回。"""
    if path is None:
        return path

    original = str(path).strip()
    if not original:
        return original

    normalized = original.replace("\\", "/")
    prefix_mapping = (
        ("/mnt/nas_data", "//10.10.10.11/data"),
        ("/mnt/data/4np", "//10.10.10.10/4np_share"),
        ("/mnt/nas_data", "//10.10.10.10/nas_data"),
    )
    for linux_prefix, network_prefix in prefix_mapping:
        if normalized == linux_prefix:
            return network_prefix.replace("/", "\\")
        if normalized.startswith(linux_prefix + "/"):
            relative_path = normalized[len(linux_prefix):]
            return (network_prefix + relative_path).replace("/", "\\")

    return original


def single_value(values: dict[str, list[str]], name: str) -> str:
    return values.get(name, [""])[0].strip()


def parse_form(body: bytes) -> dict[str, str]:
    values = parse_qs(body.decode("utf-8"), keep_blank_values=True)
    result = {key: single_value(values, key) for key in DEFAULT_FORM}
    result["overwrite"] = "1" if "overwrite" in values else ""
    result["page_id"] = single_value(values, "page_id")
    return result


def build_command(form: dict[str, str]) -> list[str]:
    required = {
        "imagery_dir": "影像目录",
        "boundary": "县界路径",
        "output_dir": "输出目录",
        "date1": "日期1",
        "date2": "日期2",
        "resolution": "分辨率标记",
        "index": "空间索引",
    }
    for field, label in required.items():
        if not form.get(field, "").strip():
            raise ValueError(f"{label}不能为空。")
    for field in ("date1", "date2"):
        if not re.fullmatch(r"\d{8}", form[field]):
            raise ValueError(f"{field} 必须是八位数字。")
    numeric_fields = (
        "workers",
        "cpu_percent",
        "gdal_memory_gb",
        "index_workers",
        "overview_max_factor",
    )
    for field in numeric_fields:
        try:
            if float(form[field]) <= 0:
                raise ValueError
        except (TypeError, ValueError):
            raise ValueError(f"{field} 必须是大于 0 的数字。") from None
    try:
        if float(form["buffer_distance_m"]) < 0:
            raise ValueError
    except (TypeError, ValueError):
        raise ValueError("县界外扩距离必须是大于或等于 0 的数字。") from None
    if float(form["gdal_memory_gb"]) < 0.125:
        raise ValueError("GDAL 总内存预算不能小于 0.125 GB。")
    try:
        overview_max_factor = int(form["overview_max_factor"])
    except (TypeError, ValueError):
        raise ValueError("金字塔最高倍数必须是整数。") from None
    if not 2 <= overview_max_factor <= 256 or (
        overview_max_factor & (overview_max_factor - 1)
    ):
        raise ValueError("金字塔最高倍数必须是 2 到 256 的 2 次幂。")

    # 页面可以填写 Windows UNC 路径；启动脚本前转换为本机 Linux 挂载路径。
    converted_paths = {
        field: str(convert_network_path(form.get(field, "")) or "")
        for field in (
            "imagery_dir",
            "boundary",
            "output_dir",
            "index",
            "temp_dir",
            "gdal_bin",
        )
    }

    command = [
        sys.executable,
        str(SCRIPT_PATH),
        "--emit-progress-events",
        "--imagery-dir", converted_paths["imagery_dir"],
        "--boundary", converted_paths["boundary"],
        "--output-dir", converted_paths["output_dir"],
        "--date1", form["date1"],
        "--date2", form["date2"],
        "--resolution", form["resolution"],
        "--name-template", form["name_template"],
        "--index", converted_paths["index"],
        "--index-mode", form["index_mode"],
        "--workers", form["workers"],
        "--cpu-percent", form["cpu_percent"],
        "--gdal-memory-gb", form["gdal_memory_gb"],
        "--index-workers", form["index_workers"],
        "--resampling", form["resampling"],
        "--overview-max-factor", form["overview_max_factor"],
        "--buffer-distance-m", form["buffer_distance_m"],
    ]
    optional_pairs = (
        ("pixel_size", "--pixel-size"),
        ("temp_dir", "--temp-dir"),
        ("gdal_bin", "--gdal-bin"),
    )
    for field, option in optional_pairs:
        if form.get(field):
            value = converted_paths[field] if field in converted_paths else form[field]
            command.extend([option, value])
    county_values = [
        value for value in re.split(r"[\s,，;；]+", form.get("county", "")) if value
    ]
    for county in county_values:
        command.extend(["--county", county])
    for option in form.get("creation_options", "").splitlines():
        option = option.strip()
        if option:
            command.extend(["--creation-option", option])
    if form.get("overwrite"):
        command.append("--overwrite")
    return command


def escaped_value(form: dict[str, str], key: str) -> str:
    return html.escape(form.get(key, ""), quote=True)


def option(value: str, label: str, selected: str) -> str:
    marker = " selected" if value == selected else ""
    return f'<option value="{html.escape(value)}"{marker}>{html.escape(label)}</option>'


def render_page(
    snapshot: dict[str, object],
    page_id: str,
    notice: str = "",
) -> bytes:
    form = snapshot["form"]
    assert isinstance(form, dict)
    running = bool(snapshot["running"])
    status_text = "运行中" if running else "空闲"
    status_class = "running" if running else "idle"
    checked = " checked" if form.get("overwrite") else ""
    index_options = "".join([
        option("auto", "auto — 增量检查", str(form["index_mode"])),
        option("skip", "skip — 完全跳过扫描", str(form["index_mode"])),
        option("rebuild", "rebuild — 完整重建", str(form["index_mode"])),
    ])
    resampling_options = "".join(
        option(value, label, str(form["resampling"]))
        for value, label in (
            ("near", "near（最近邻）"),
            ("bilinear", "bilinear（双线性）"),
            ("cubic", "cubic（三次）"),
            ("cubicspline", "cubicspline"),
            ("lanczos", "lanczos"),
            ("average", "average"),
            ("mode", "mode"),
        )
    )
    notice_html = (
        f'<div class="notice">{html.escape(notice)}</div>' if notice else ""
    )
    document = f"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>县级影像裁剪控制台</title>
<script>
(() => {{
  const currentPageId = {json.dumps(page_id)};
  const prefix = "county-clip-page:";
  const savedPageId = window.name.startsWith(prefix) ? window.name.slice(prefix.length) : "";
  const navigation = performance.getEntriesByType("navigation")[0];
  const isReload = navigation && navigation.type === "reload";
  if (isReload && savedPageId && savedPageId !== currentPageId) {{
    const resumeForm = document.createElement("form");
    resumeForm.method = "post";
    resumeForm.action = "/resume";
    const hidden = document.createElement("input");
    hidden.type = "hidden";
    hidden.name = "page_id";
    hidden.value = savedPageId;
    resumeForm.appendChild(hidden);
    document.documentElement.appendChild(resumeForm);
    resumeForm.submit();
  }} else {{
    window.name = prefix + currentPageId;
  }}
}})();
</script>
<style>
:root {{
  color-scheme: light;
  --bg:#f5f7fa; --panel:#fff; --line:#dfe4ea; --text:#18212f;
  --muted:#667085; --primary:#1769e0; --primary-soft:#eaf2ff;
  --green:#16803d; --amber:#a15c00; --red:#c52b2b;
}}
* {{ box-sizing:border-box; }}
body {{ margin:0; background:var(--bg); color:var(--text);
  font:14px/1.5 system-ui,-apple-system,"Segoe UI","Microsoft YaHei",sans-serif; }}
header {{ height:64px; padding:0 28px; background:#fff; border-bottom:1px solid var(--line);
  display:flex; align-items:center; justify-content:space-between; position:sticky; top:0; z-index:5; }}
h1 {{ margin:0; font-size:20px; }}
.subtitle {{ color:var(--muted); margin-left:12px; font-size:13px; }}
.badge {{ display:inline-flex; align-items:center; gap:7px; border:1px solid var(--line);
  border-radius:999px; padding:6px 11px; background:#fff; font-weight:650; }}
.badge::before {{ content:""; width:8px; height:8px; border-radius:50%; background:#98a2b3; }}
.badge.running::before {{ background:var(--green); box-shadow:0 0 0 4px #e7f7ed; }}
main {{ display:grid; grid-template-columns:minmax(350px,430px) minmax(600px,1fr);
  gap:18px; padding:18px; max-width:1800px; margin:auto; }}
.panel {{ background:var(--panel); border:1px solid var(--line); border-radius:12px;
  box-shadow:0 1px 2px rgba(16,24,40,.04); }}
.controls {{ padding:20px; align-self:start; }}
.section-title {{ margin:0 0 14px; font-size:15px; }}
.section-title:not(:first-child) {{ margin-top:23px; padding-top:19px; border-top:1px solid #edf0f3; }}
.grid {{ display:grid; grid-template-columns:1fr 1fr; gap:12px; }}
.field {{ display:flex; flex-direction:column; gap:5px; }}
.field.full {{ grid-column:1/-1; }}
label {{ font-size:12px; font-weight:650; color:#344054; }}
input,select,textarea {{ width:100%; border:1px solid #cfd6df; background:#fff; color:var(--text);
  border-radius:7px; padding:9px 10px; font:inherit; outline:none; }}
input:focus,select:focus,textarea:focus {{ border-color:#7aa7e8; box-shadow:0 0 0 3px #eaf2ff; }}
textarea {{ resize:vertical; min-height:62px; }}
.hint {{ color:var(--muted); font-size:11px; }}
.check {{ flex-direction:row; align-items:center; gap:8px; padding-top:3px; }}
.check input {{ width:auto; }}
.buttons {{ display:flex; gap:10px; margin-top:20px; }}
button {{ border:0; border-radius:8px; padding:10px 17px; font:inherit; font-weight:700; cursor:pointer; }}
.primary {{ background:var(--primary); color:#fff; flex:1; }}
.danger {{ background:#fff; color:var(--red); border:1px solid #efc5c5; }}
button:disabled {{ opacity:.48; cursor:not-allowed; }}
.notice {{ margin:0 0 14px; padding:10px 12px; border-radius:7px; background:var(--primary-soft);
  color:#174f9f; border:1px solid #cfe0fb; }}
.workspace {{ min-width:0; display:grid; gap:18px; align-content:start; }}
.summary {{ padding:20px; }}
.summary-top {{ display:flex; align-items:start; justify-content:space-between; gap:18px; }}
.current {{ font-size:20px; font-weight:750; margin:3px 0; }}
.stage {{ color:var(--muted); }}
.percent {{ font-size:28px; font-weight:780; color:var(--primary); }}
.progress-track {{ margin-top:14px; width:100%; height:10px; background:#edf1f5;
  border-radius:999px; overflow:hidden; }}
.progress-bar {{ height:100%; width:0; background:linear-gradient(90deg,#2b7de9,#1769e0);
  border-radius:inherit; transition:width .25s ease; }}
.stats {{ display:flex; gap:22px; margin-top:13px; color:var(--muted); font-size:12px; }}
.stats strong {{ color:var(--text); font-size:14px; }}
.table-panel {{ overflow:hidden; }}
.panel-head {{ padding:14px 18px; border-bottom:1px solid var(--line);
  display:flex; align-items:center; justify-content:space-between; }}
.panel-head h2 {{ font-size:15px; margin:0; }}
.table-wrap {{ max-height:430px; overflow:auto; }}
table {{ width:100%; border-collapse:collapse; }}
th {{ position:sticky; top:0; background:#f9fafb; color:var(--muted); text-align:left;
  font-size:11px; letter-spacing:.03em; padding:9px 12px; border-bottom:1px solid var(--line); }}
td {{ padding:9px 12px; border-bottom:1px solid #edf0f3; }}
tr.active {{ background:#f3f7ff; }}
.county-status {{ font-weight:650; }}
.status-success {{ color:var(--green); }} .status-failed {{ color:var(--red); }}
.status-no_image,.status-skipped {{ color:var(--amber); }}
.terminal-panel {{ overflow:hidden; }}
.terminal {{ margin:0; height:300px; overflow:auto; padding:14px 16px; background:#fbfcfd;
  color:#26364a; font:12px/1.65 ui-monospace,SFMono-Regular,Consolas,monospace;
  white-space:pre-wrap; word-break:break-all; }}
.terminal .error {{ color:var(--red); }} .terminal .warning {{ color:var(--amber); }}
.terminal .command {{ color:#155db1; font-weight:650; }}
@media (max-width:1050px) {{ main {{ grid-template-columns:1fr; }} }}
</style>
</head>
<body>
<header>
  <div><h1>县级影像裁剪<span class="subtitle">空间索引 · 临时 VRT · GDAL Warp</span></h1></div>
  <div id="serverBadge" class="badge {status_class}">{status_text}</div>
</header>
<main>
  <form class="panel controls" method="post" action="/start">
    <input type="hidden" name="page_id" value="{html.escape(page_id, quote=True)}">
    {notice_html}
    <h2 class="section-title">路径与命名</h2>
    <div class="grid">
      <div class="field full"><label>影像目录</label>
        <input name="imagery_dir" required value="{escaped_value(form, 'imagery_dir')}" placeholder="/data/0.5m影像_转投影影像"></div>
      <div class="field full"><label>县界文件或文件夹</label>
        <input name="boundary" required value="{escaped_value(form, 'boundary')}"></div>
      <div class="field full"><label>输出目录</label>
        <input name="output_dir" required value="{escaped_value(form, 'output_dir')}"></div>
      <div class="field"><label>日期 1</label>
        <input name="date1" required pattern="[0-9]{{8}}" value="{escaped_value(form, 'date1')}"></div>
      <div class="field"><label>日期 2</label>
        <input name="date2" required pattern="[0-9]{{8}}" value="{escaped_value(form, 'date2')}"></div>
      <div class="field"><label>分辨率标记</label>
        <input name="resolution" required value="{escaped_value(form, 'resolution')}"></div>
      <div class="field"><label>真正输出像元大小（可空）</label>
        <input name="pixel_size" type="number" min="0" step="any" value="{escaped_value(form, 'pixel_size')}" placeholder="留空则不重采样"></div>
      <div class="field full"><label>文件名模板</label>
        <input name="name_template" required value="{escaped_value(form, 'name_template')}">
        <span class="hint">可用：{{code}} {{name}} {{date1}} {{date2}} {{resolution}}</span></div>
    </div>

    <h2 class="section-title">索引与处理范围</h2>
    <div class="grid">
      <div class="field full"><label>SQLite 空间索引文件</label>
        <input name="index" required value="{escaped_value(form, 'index')}"></div>
      <div class="field"><label>索引模式</label><select name="index_mode">{index_options}</select></div>
      <div class="field"><label>索引并发数</label>
        <input name="index_workers" type="number" min="1" value="{escaped_value(form, 'index_workers')}"></div>
      <div class="field full"><label>只处理这些县（可空）</label>
        <input name="county" value="{escaped_value(form, 'county')}" placeholder="150102, 150103">
        <span class="hint">空白表示全部；多个代码用逗号或空格分隔</span></div>
      <div class="field"><label>县界外扩距离（米）</label>
        <input name="buffer_distance_m" type="number" min="0" step="any" value="{escaped_value(form, 'buffer_distance_m')}">
        <span class="hint">默认 50 米；影像覆盖不足时只保留实际覆盖部分</span></div>
    </div>

    <h2 class="section-title">资源与 GDAL</h2>
    <div class="grid">
      <div class="field"><label>最大并发县数</label>
        <input name="workers" type="number" min="1" value="{escaped_value(form, 'workers')}"></div>
      <div class="field"><label>CPU 最高可调用资源（%）</label>
        <input name="cpu_percent" type="number" min="1" max="100" step="1" value="{escaped_value(form, 'cpu_percent')}">
      </div>
      <div class="field"><label>GDAL 总内存预算（GB）</label>
        <input name="gdal_memory_gb" type="number" min="0.125" step="0.125" value="{escaped_value(form, 'gdal_memory_gb')}"></div>
      <div class="field"><label>重采样算法</label><select name="resampling">{resampling_options}</select></div>
      <div class="field"><label>金字塔最高倍数</label>
        <input name="overview_max_factor" type="number" min="2" max="256" step="2" value="{escaped_value(form, 'overview_max_factor')}">
        <span class="hint">填写 2 的次幂：2、4、8…256</span></div>
      <div class="field"><label>临时目录（可空）</label>
        <input name="temp_dir" value="{escaped_value(form, 'temp_dir')}"></div>
      <div class="field full"><label>GDAL bin 目录（需含 warp/addo，通常可空）</label>
        <input name="gdal_bin" value="{escaped_value(form, 'gdal_bin')}"></div>
      <div class="field full"><label>附加创建选项（每行一个）</label>
        <textarea name="creation_options" placeholder="PREDICTOR=2">{html.escape(str(form.get('creation_options', '')))}</textarea></div>
      <label class="field full check"><input type="checkbox" name="overwrite" value="1"{checked}>覆盖已有县级结果</label>
    </div>
    <div class="buttons">
      <button id="startButton" class="primary" type="submit"{" disabled" if running else ""}>开始处理</button>
      <button id="stopButton" class="danger" type="button"{" disabled" if not running else ""}>停止任务</button>
    </div>
  </form>

  <section class="workspace">
    <div class="panel summary">
      <div class="summary-top">
        <div>
          <div class="stage" id="stageText">等待开始</div>
          <div class="current" id="currentCounty">第 0 / 0 个县</div>
          <div class="stage" id="currentName">尚无任务</div>
        </div>
        <div class="percent" id="percentText">0%</div>
      </div>
      <div class="progress-track"><div id="progressBar" class="progress-bar"></div></div>
      <div class="stats">
        <span>已完成 <strong id="completedCount">0</strong></span>
        <span>总数 <strong id="totalCount">0</strong></span>
        <span>成功 <strong id="successCount">0</strong></span>
        <span>失败 <strong id="failedCount">0</strong></span>
      </div>
    </div>

    <div class="panel table-panel">
      <div class="panel-head"><h2>逐县处理状态</h2><span class="stage">并发任务的县级结果汇总</span></div>
      <div class="table-wrap">
        <table><thead><tr><th>#</th><th>县代码</th><th>县名</th><th>状态</th><th>说明</th></tr></thead>
          <tbody id="countyBody"><tr id="emptyRow"><td colspan="5" class="stage">尚无县级任务</td></tr></tbody>
        </table>
      </div>
    </div>

    <div class="panel terminal-panel">
      <div class="panel-head"><h2>脚本输出</h2><span id="streamState" class="stage">正在连接输出流…</span></div>
      <pre id="terminal" class="terminal"></pre>
    </div>
  </section>
</main>
<script>
(() => {{
  const pageId = {json.dumps(page_id)};
  const terminal = document.getElementById("terminal");
  const startButton = document.getElementById("startButton");
  const stopButton = document.getElementById("stopButton");
  const rows = new Map();
  let total = 0;
  let success = 0;
  let failed = 0;

  function setServerState(text, running) {{
    const badge = document.getElementById("serverBadge");
    badge.textContent = text;
    badge.className = "badge " + (running ? "running" : "idle");
  }}
  function resetView() {{
    rows.clear(); total = 0; success = 0; failed = 0;
    document.getElementById("countyBody").innerHTML =
      '<tr id="emptyRow"><td colspan="5" class="stage">正在读取任务…</td></tr>';
    terminal.textContent = "";
    setProgress(0, 0);
    document.getElementById("stageText").textContent = "任务正在启动";
    document.getElementById("currentCounty").textContent = "第 0 / 0 个县";
    document.getElementById("currentName").textContent = "等待脚本输出";
    document.getElementById("successCount").textContent = "0";
    document.getElementById("failedCount").textContent = "0";
    setServerState("运行中", true);
  }}
  function setProgress(completed, value) {{
    const percent = Math.max(0, Math.min(100, Number(value) || 0));
    document.getElementById("progressBar").style.width = percent + "%";
    document.getElementById("percentText").textContent =
      (Number.isInteger(percent) ? percent : percent.toFixed(2)) + "%";
    document.getElementById("completedCount").textContent = String(completed || 0);
    document.getElementById("totalCount").textContent = String(total || 0);
  }}
  function ensureRow(county) {{
    let row = rows.get(county.code);
    if (row) return row;
    const empty = document.getElementById("emptyRow");
    if (empty) empty.remove();
    row = document.createElement("tr");
    row.dataset.code = county.code;
    for (const value of [county.ordinal, county.code, county.name, "等待", ""]) {{
      const cell = document.createElement("td");
      cell.textContent = value == null ? "" : String(value);
      row.appendChild(cell);
    }}
    row.children[3].className = "county-status";
    document.getElementById("countyBody").appendChild(row);
    rows.set(county.code, row);
    return row;
  }}
  function appendTerminal(line, level) {{
    const span = document.createElement("span");
    span.className = level || "normal";
    span.textContent = line + "\\n";
    terminal.appendChild(span);
    while (terminal.childNodes.length > 5000) terminal.firstChild.remove();
    terminal.scrollTop = terminal.scrollHeight;
  }}
  function statusLabel(status) {{
    return {{
      success:"完成", skipped:"已跳过", no_image:"无相交影像", failed:"失败"
    }}[status] || status;
  }}
  function handleClipEvent(event) {{
    if (event.event === "job_plan") {{
      total = Number(event.total) || 0;
      document.getElementById("countyBody").innerHTML = "";
      rows.clear();
      for (const county of event.counties || []) ensureRow(county);
      setProgress(0, 0);
      document.getElementById("stageText").textContent =
        `任务计划：并发 ${{event.workers}}，每任务 ${{event.threads_per_job}} 线程`;
    }} else if (event.event === "stage") {{
      document.getElementById("stageText").textContent = event.message || event.name;
    }} else if (event.event === "county_started") {{
      const row = ensureRow(event);
      row.className = "active";
      row.children[3].textContent = "处理中";
      row.children[3].className = "county-status";
      row.children[4].textContent = "";
      document.getElementById("currentCounty").textContent =
        `第 ${{event.ordinal}} / ${{event.total}} 个县`;
      document.getElementById("currentName").textContent =
        `${{event.code}} · ${{event.name}}`;
    }} else if (event.event === "county_finished") {{
      const row = ensureRow(event);
      row.className = "";
      row.children[3].textContent = statusLabel(event.status);
      row.children[3].className = "county-status status-" + event.status;
      row.children[4].textContent = event.error || "";
      if (event.status === "success") success += 1;
      if (event.status === "failed") failed += 1;
      document.getElementById("successCount").textContent = String(success);
      document.getElementById("failedCount").textContent = String(failed);
      setProgress(event.completed, event.percent);
    }} else if (event.event === "job_finished") {{
      document.getElementById("stageText").textContent = "全部县处理结束";
      setServerState(event.exit_code === 0 ? "已完成" : "完成但有失败", false);
    }} else if (event.event === "job_error") {{
      document.getElementById("stageText").textContent = "任务异常终止";
      appendTerminal(event.message || "未知错误", "error");
    }}
  }}

  stopButton.addEventListener("click", async () => {{
    if (stopButton.disabled) return;
    stopButton.disabled = true;
    stopButton.textContent = "正在停止…";
    setServerState("正在停止", true);
    document.getElementById("stageText").textContent = "正在停止并发任务";
    try {{
      const body = new URLSearchParams({{page_id: pageId}});
      const response = await fetch("/stop", {{
        method: "POST",
        headers: {{
          "Content-Type": "application/x-www-form-urlencoded;charset=UTF-8",
          "Accept": "application/json"
        }},
        body: body.toString()
      }});
      const result = await response.json();
      if (!result.ok) {{
        appendTerminal(result.message || "停止任务失败", "error");
        stopButton.disabled = false;
        stopButton.textContent = "停止任务";
      }}
    }} catch (error) {{
      appendTerminal("停止请求失败：" + error, "error");
      stopButton.disabled = false;
      stopButton.textContent = "停止任务";
    }}
  }});

  const source = new EventSource("/events?page_id=" + encodeURIComponent(pageId));
  source.onopen = () => {{
    document.getElementById("streamState").textContent = "输出流已连接（SSE）";
  }};
  source.onerror = () => {{
    document.getElementById("streamState").textContent = "输出流连接中断，正在自动重连";
  }};
  source.onmessage = message => {{
    const payload = JSON.parse(message.data);
    if (payload.kind === "reset") resetView();
    else if (payload.kind === "terminal") appendTerminal(payload.line, payload.level);
    else if (payload.kind === "clip_event") handleClipEvent(payload.data);
    else if (payload.kind === "process_exit") {{
      setServerState(payload.exit_code === 0 ? "已完成" : "已停止 / 异常", false);
      document.getElementById("stageText").textContent =
        payload.exit_code === 0 ? "任务处理完成" : "任务已停止或发生异常";
      startButton.disabled = false;
      stopButton.disabled = true;
      stopButton.textContent = "停止任务";
    }}
  }};
}})();
</script>
</body></html>"""
    return document.encode("utf-8")


class RequestHandler(BaseHTTPRequestHandler):
    server_version = "CountyClipHTTP/1.0"

    def log_message(self, fmt: str, *args: object) -> None:
        # 静默 HTTP 访问日志，避免 GET /events、页面刷新等信息污染脚本终端。
        # 裁剪脚本 stdout/stderr 由 JobController 单独读取并推送到页面。
        return

    def do_GET(self) -> None:
        path = urlparse(self.path).path
        if path == "/":
            session = SESSIONS.create()
            self._send_html(render_page(session.controller.snapshot(), session.page_id))
        elif path == "/events":
            values = parse_qs(urlparse(self.path).query)
            page_id = single_value(values, "page_id")
            session = SESSIONS.get(page_id)
            if session is None:
                # 服务重启后，旧页面仍可能携带失效 ID 重连 SSE。
                # EventSource 收到 204 后会停止重连；不要把中文放进 HTTP
                # 状态原因，因为 BaseHTTPRequestHandler 状态行仅支持 Latin-1。
                self._send_no_content()
                return
            self._stream_events(session.broker)
        else:
            self.send_error(HTTPStatus.NOT_FOUND)

    def do_POST(self) -> None:
        path = urlparse(self.path).path
        if path not in {"/start", "/stop", "/resume"}:
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        length = int(self.headers.get("Content-Length", "0"))
        if length > 1_000_000:
            self.send_error(HTTPStatus.REQUEST_ENTITY_TOO_LARGE)
            return
        body = self.rfile.read(length)
        form = parse_form(body)
        page_id = form.get("page_id", "")
        session = SESSIONS.get(page_id)
        if session is None:
            session = SESSIONS.create()
            message = "原页面会话不存在或已过期，已创建新的独立页面会话。"
            self._send_html(
                render_page(session.controller.snapshot(), session.page_id, message)
            )
            return
        if path == "/start":
            ok, message = session.controller.start(form)
        elif path == "/stop":
            ok, message = session.controller.stop()
        elif path == "/resume":
            self._send_html(render_page(session.controller.snapshot(), session.page_id))
            return
        else:
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        session.broker.publish({
            "kind": "terminal",
            "line": message,
            "level": "normal" if ok else "error",
        })
        if path == "/stop" and "application/json" in self.headers.get("Accept", ""):
            self._send_json({"ok": ok, "message": message})
            return
        self._send_html(
            render_page(session.controller.snapshot(), session.page_id, message)
        )

    def _send_html(self, content: bytes) -> None:
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(content)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(content)

    def _send_json(self, payload: dict[str, object]) -> None:
        content = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(content)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(content)

    def _send_no_content(self) -> None:
        self.send_response(HTTPStatus.NO_CONTENT)
        self.send_header("Content-Length", "0")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        self.end_headers()

    def _stream_events(self, broker: EventBroker) -> None:
        try:
            last_sequence = int(self.headers.get("Last-Event-ID", "0"))
        except ValueError:
            last_sequence = 0
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()
        try:
            while True:
                events = broker.wait_after(last_sequence)
                if not events:
                    self.wfile.write(b": heartbeat\n\n")
                    self.wfile.flush()
                    continue
                for sequence, payload in events:
                    data = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
                    packet = f"id: {sequence}\ndata: {data}\n\n".encode("utf-8")
                    self.wfile.write(packet)
                    last_sequence = sequence
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            return


def parse_server_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="启动县级影像裁剪 Web 控制界面")
    parser.add_argument("--host", default="0.0.0.0", help="监听地址")
    parser.add_argument("--port", type=int, default=9007, help="监听端口")
    parser.add_argument("--open-browser", action="store_true", help="启动后自动打开浏览器")
    return parser.parse_args()


def get_lan_ip() -> str:
    """获取访问当前主机所用的局域网 IPv4 地址。"""
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        # UDP connect 不会发送数据，只用于选择访问该网段的本机网卡。
        probe.connect(("10.10.10.11", 9))
        return str(probe.getsockname()[0])
    except OSError:
        try:
            return socket.gethostbyname(socket.gethostname())
        except OSError:
            return "127.0.0.1"
    finally:
        probe.close()


def main() -> int:
    args = parse_server_args()
    if not SCRIPT_PATH.is_file():
        print(f"找不到裁剪脚本：{SCRIPT_PATH}", file=sys.stderr)
        return 2
    server = ThreadingHTTPServer((args.host, args.port), RequestHandler)
    url_host = get_lan_ip() if args.host in {"0.0.0.0", "::"} else args.host
    url = f"http://{url_host}:{args.port}/"
    print(f"县级影像裁剪界面已启动：{url}", flush=True)
    if args.host in {"0.0.0.0", "::"}:
        print(f"本机也可以访问：http://127.0.0.1:{args.port}/", flush=True)
    print("按 Ctrl+C 停止 HTTP 服务。", flush=True)
    if args.open_browser:
        threading.Timer(0.5, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n正在停止 HTTP 服务…", flush=True)
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
