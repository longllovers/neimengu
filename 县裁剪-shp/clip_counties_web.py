#!/usr/bin/env python
"""县级 Shapefile 裁剪的 Web 控制界面（标准库 HTTPServer + SSE）。"""

from __future__ import annotations

import argparse
import html
import json
import os
import re
import signal
import socket
import subprocess
import sys
import threading
import time
import uuid
import webbrowser
from collections import OrderedDict
from dataclasses import dataclass, field
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse


BASE_DIR = Path(__file__).resolve().parent
SCRIPT_PATH = BASE_DIR / "clip_counties.py"
EVENT_PREFIX = "@@CLIP_EVENT@@"
FONT_PATH = next(
    (path for path in (BASE_DIR / "simhei.ttf", BASE_DIR / "SimHei.ttf") if path.is_file()),
    BASE_DIR / "simhei.ttf",
)
PREVIEW_LOCK = threading.Lock()
PREVIEW_CACHE: OrderedDict[tuple[object, ...], bytes] = OrderedDict()
PREVIEW_CACHE_BYTES = 0
PREVIEW_CACHE_MAX_BYTES = 48 * 1024 * 1024
PREVIEW_CACHE_MAX_ITEMS = 12
DEFAULT_SOURCE = (
    r"\\10.10.10.11\data\专题2_农作物种植用地遥感测量"
    r"\加密0711_乌兰察布-道_已全部完成并解密\完成成果解密"
)
DEFAULT_FORM = {
    "shp_dir": DEFAULT_SOURCE,
    "boundary": str(BASE_DIR / "00县边界"),
    "output_dir": str(BASE_DIR / "县级SHP裁剪结果"),
    "index": str(BASE_DIR / "shapefile_index.sqlite"),
    "index_mode": "auto",
    "index_workers": "4",
    "county": "",
    "workers": "4",
    "cpu_percent": "15.0",
    "temp_dir": "",
    "overwrite": "",
    "render_preview": "",
}


class EventBroker:
    def __init__(self) -> None:
        self._condition = threading.Condition()
        self._events: list[tuple[int, dict[str, object]]] = []
        self._sequence = 0

    def publish(self, payload: dict[str, object]) -> None:
        with self._condition:
            self._sequence += 1
            self._events.append((self._sequence, payload))
            if len(self._events) > 20_000:
                self._events = self._events[-20_000:]
            self._condition.notify_all()

    def wait_after(
        self, sequence: int, timeout: float = 15.0,
    ) -> list[tuple[int, dict[str, object]]]:
        with self._condition:
            events = [item for item in self._events if item[0] > sequence]
            if events:
                return events
            self._condition.wait(timeout)
            return [item for item in self._events if item[0] > sequence]


class JobController:
    def __init__(self, broker: EventBroker) -> None:
        self.broker = broker
        self.lock = threading.Lock()
        self.process: subprocess.Popen[str] | None = None
        self.stopping = False
        self.form = dict(DEFAULT_FORM)

    def snapshot(self) -> dict[str, object]:
        with self.lock:
            running = self.process is not None and self.process.poll() is None
            return {"running": running, "stopping": self.stopping, "form": dict(self.form)}

    def start(self, form: dict[str, str]) -> tuple[bool, str]:
        try:
            command = build_command(form)
        except Exception as exc:
            return False, str(exc)
        with self.lock:
            if self.process is not None and self.process.poll() is None:
                return False, "当前页面已有任务正在运行。"
            flags = 0
            process_kwargs: dict[str, object] = {}
            if os.name == "nt":
                flags = subprocess.CREATE_NEW_PROCESS_GROUP
            else:
                process_kwargs["start_new_session"] = True
            try:
                process = subprocess.Popen(
                    command,
                    cwd=BASE_DIR,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    bufsize=1,
                    creationflags=flags,
                    **process_kwargs,
                )
            except OSError as exc:
                return False, f"无法启动裁剪脚本：{exc}"
            self.process = process
            self.stopping = False
            self.form = dict(form)
        self.broker.publish({"kind": "reset"})
        self.broker.publish({
            "kind": "terminal",
            "line": "启动命令：" + subprocess.list2cmdline(command),
            "level": "command",
        })
        threading.Thread(
            target=self._read_process,
            args=(process,),
            name=f"shp-output-{process.pid}",
            daemon=True,
        ).start()
        return True, "SHP 裁剪任务已启动。"

    def _read_process(self, process: subprocess.Popen[str]) -> None:
        assert process.stdout is not None
        for raw_line in process.stdout:
            line = raw_line.rstrip("\r\n")
            if line.startswith(EVENT_PREFIX):
                try:
                    event = json.loads(line[len(EVENT_PREFIX):])
                    self.broker.publish({"kind": "clip_event", "data": event})
                except json.JSONDecodeError:
                    self.broker.publish({"kind": "terminal", "line": line, "level": "warning"})
            else:
                level = "error" if "ERROR" in line or "错误" in line else "normal"
                self.broker.publish({"kind": "terminal", "line": line, "level": level})
        exit_code = process.wait()
        with self.lock:
            was_stopping = self.stopping
            self.stopping = False
            if self.process is process:
                self.process = None
        self.broker.publish({
            "kind": "process_exit",
            "exit_code": exit_code,
            "stopped": was_stopping,
        })

    def stop(self) -> tuple[bool, str]:
        with self.lock:
            process = self.process
            if process is None or process.poll() is not None:
                return False, "当前页面没有运行中的任务。"
            if self.stopping:
                return True, "任务正在停止，请稍候。"
            self.stopping = True
        self.broker.publish({"kind": "job_stopping", "pid": process.pid})
        self.broker.publish({
            "kind": "terminal",
            "line": f"正在停止裁剪任务及其全部子并发数（PID {process.pid}）…",
            "level": "warning",
        })
        process_group: int | None = None
        try:
            if os.name == "nt":
                # Windows 没有可靠的并发数组 SIGTERM；直接强制结束整棵并发数树。
                completed = subprocess.run(
                    ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    check=False,
                    timeout=5,
                )
                if completed.returncode and process.poll() is None:
                    process.kill()
            else:
                process_group = os.getpgid(process.pid)
                os.killpg(process_group, signal.SIGTERM)
        except (OSError, ProcessLookupError, subprocess.TimeoutExpired) as exc:
            if process.poll() is None:
                with self.lock:
                    self.stopping = False
                self.broker.publish({"kind": "stop_failed", "message": str(exc)})
                return False, f"无法终止裁剪并发数：{exc}"
        threading.Thread(
            target=self._ensure_stopped,
            args=(process, process_group),
            name=f"shp-stop-{process.pid}",
            daemon=True,
        ).start()
        return True, "终止信号已发送，正在确认所有裁剪并发数退出。"

    def _ensure_stopped(
        self,
        process: subprocess.Popen[str],
        process_group: int | None,
    ) -> None:
        try:
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                pass
            if os.name != "nt" and process_group is not None:
                # 即使 Python 主并发数已先退出，也要清掉仍留在原并发数组中的子并发数。
                try:
                    os.killpg(process_group, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            elif process.poll() is None:
                completed = subprocess.run(
                    ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    check=False,
                    timeout=5,
                )
                if completed.returncode and process.poll() is None:
                    process.kill()
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self.broker.publish({
                    "kind": "terminal",
                    "line": f"并发数 PID {process.pid} 未按时退出，请检查系统并发数。",
                    "level": "error",
                })
        except (OSError, ProcessLookupError):
            return


@dataclass
class PageSession:
    page_id: str
    broker: EventBroker = field(default_factory=EventBroker)
    controller: JobController = field(init=False)
    last_access: float = field(default_factory=time.time)

    def __post_init__(self) -> None:
        self.controller = JobController(self.broker)


class SessionRegistry:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.sessions: dict[str, PageSession] = {}

    def create(self) -> PageSession:
        with self.lock:
            self._cleanup()
            page_id = uuid.uuid4().hex
            session = PageSession(page_id)
            self.sessions[page_id] = session
            return session

    def get(self, page_id: str) -> PageSession | None:
        with self.lock:
            session = self.sessions.get(page_id)
            if session:
                session.last_access = time.time()
            return session

    def _cleanup(self) -> None:
        now = time.time()
        expired = [
            page_id for page_id, session in self.sessions.items()
            if (
                not bool(session.controller.snapshot()["running"])
                and now - session.last_access > 24 * 60 * 60
            )
        ]
        for page_id in expired:
            del self.sessions[page_id]


SESSIONS = SessionRegistry()


def convert_network_path(path: str | Path | None) -> str | None:
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



def single_value(values: dict[str, list[str]], name: str) -> str:
    return values.get(name, [""])[0].strip()


def parse_form(body: bytes) -> dict[str, str]:
    values = parse_qs(body.decode("utf-8"), keep_blank_values=True)
    result = {key: single_value(values, key) for key in DEFAULT_FORM}
    result["overwrite"] = "1" if "overwrite" in values else ""
    result["render_preview"] = "1" if "render_preview" in values else ""
    result["page_id"] = single_value(values, "page_id")
    return result


def build_command(form: dict[str, str]) -> list[str]:
    required = {
        "shp_dir": "输入 SHP 目录",
        "boundary": "县界路径",
        "output_dir": "输出目录",
        "index": "空间索引",
    }
    for field, label in required.items():
        if not form.get(field, "").strip():
            raise ValueError(f"{label}不能为空。")
    for field in ("workers", "cpu_percent", "index_workers"):
        try:
            if float(form[field]) <= 0:
                raise ValueError
        except (TypeError, ValueError):
            raise ValueError(f"{field} 必须是大于 0 的数字。") from None
    paths = {
        field: str(convert_network_path(form.get(field, "")) or "")
        for field in ("shp_dir", "boundary", "output_dir", "index", "temp_dir")
    }
    command = [
        sys.executable,
        str(SCRIPT_PATH),
        "--emit-progress-events",
        "--shp-dir", paths["shp_dir"],
        "--boundary", paths["boundary"],
        "--output-dir", paths["output_dir"],
        "--index", paths["index"],
        "--index-mode", form["index_mode"],
        "--index-workers", form["index_workers"],
        "--workers", form["workers"],
        "--cpu-percent", form["cpu_percent"],
    ]
    if form.get("temp_dir"):
        command.extend(["--temp-dir", paths["temp_dir"]])
    counties = [
        value for value in re.split(r"[\s,，;；]+", form.get("county", "")) if value
    ]
    for county in counties:
        command.extend(["--county", county])
    if form.get("overwrite"):
        command.append("--overwrite")
    return command


def escaped(form: dict[str, str], key: str) -> str:
    return html.escape(form.get(key, ""), quote=True)


def resolve_preview_boundary(path: Path) -> Path:
    if path.is_file():
        return path
    if not path.is_dir():
        raise FileNotFoundError(f"县界路径不存在：{path}")
    candidates = sorted(
        item for item in path.iterdir()
        if item.suffix.lower() in {".shp", ".gpkg", ".geojson", ".json"}
    )
    if len(candidates) != 1:
        raise ValueError(f"县界目录中应有且仅有一个矢量文件，实际找到 {len(candidates)} 个")
    return candidates[0]


def preview_spatial_ref(reference: object) -> object:
    """复制空间参考并固定传统 GIS 轴顺序。"""
    from osgeo import osr

    result = reference.Clone()
    result.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
    return result


def svg_number(value: float) -> str:
    return f"{value:.2f}".rstrip("0").rstrip(".")


def geometry_svg_path(
    geometry: object,
    minx: float,
    maxy: float,
    scale_x: float,
    scale_y: float,
) -> str:
    """把 OGR 几何转换为紧凑 SVG path；多边形环使用 evenodd 填充。"""
    from osgeo import ogr

    if geometry is None or geometry.IsEmpty():
        return ""

    def point_text(x: float, y: float) -> str:
        return f"{svg_number((x - minx) * scale_x)} {svg_number((maxy - y) * scale_y)}"

    def line_text(line: object, close: bool) -> str:
        count = line.GetPointCount()
        if count < (3 if close else 2):
            return ""
        commands: list[str] = []
        previous = ""
        for index in range(count):
            current = point_text(line.GetX(index), line.GetY(index))
            if current == previous:
                continue
            commands.append(("M" if not commands else "L") + current)
            previous = current
        if len(commands) < (3 if close else 2):
            return ""
        if close:
            commands.append("Z")
        return "".join(commands)

    flattened = ogr.GT_Flatten(geometry.GetGeometryType())
    if flattened == ogr.wkbPolygon:
        return "".join(
            line_text(geometry.GetGeometryRef(index), True)
            for index in range(geometry.GetGeometryCount())
        )
    if flattened == ogr.wkbLineString:
        return line_text(geometry, False)
    if flattened == ogr.wkbPoint:
        x = (geometry.GetX() - minx) * scale_x
        y = (maxy - geometry.GetY()) * scale_y
        return (
            f"M{svg_number(x - 2)} {svg_number(y)}"
            "a2 2 0 1 0 4 0a2 2 0 1 0 -4 0Z"
        )
    return "".join(
        geometry_svg_path(
            geometry.GetGeometryRef(index), minx, maxy, scale_x, scale_y,
        )
        for index in range(geometry.GetGeometryCount())
    )


def mask_to_svg_path(mask: bytes, width: int, height: int) -> str:
    """把覆盖掩膜转成紧凑 SVG 矩形路径。

    相同的水平游程会向下合并，避免大片连续覆盖产生海量的
    1 像素高矩形。
    """
    parts: list[str] = []
    active: dict[tuple[int, int], int] = {}
    for y in range(height):
        offset = y * width
        x = 0
        runs: set[tuple[int, int]] = set()
        while x < width:
            while x < width and mask[offset + x] == 0:
                x += 1
            start = x
            while x < width and mask[offset + x] != 0:
                x += 1
            if x > start:
                runs.add((start, x))
        for run, start_y in tuple(active.items()):
            if run not in runs:
                start, end = run
                parts.append(f"M{start} {start_y}h{end-start}v{y-start_y}h-{end-start}Z")
                del active[run]
        for run in runs:
            active.setdefault(run, y)
    for (start, end), start_y in active.items():
        parts.append(f"M{start} {start_y}h{end-start}v{height-start_y}h-{end-start}Z")
    return "".join(parts)


def preview_cache_get(key: tuple[object, ...]) -> bytes | None:
    content = PREVIEW_CACHE.get(key)
    if content is not None:
        PREVIEW_CACHE.move_to_end(key)
    return content


def preview_cache_put(key: tuple[object, ...], content: bytes) -> None:
    """按内存容量和条目数维护最近使用的高清视窗。"""
    global PREVIEW_CACHE_BYTES
    previous = PREVIEW_CACHE.pop(key, None)
    if previous is not None:
        PREVIEW_CACHE_BYTES -= len(previous)
    PREVIEW_CACHE[key] = content
    PREVIEW_CACHE_BYTES += len(content)
    while (
        len(PREVIEW_CACHE) > PREVIEW_CACHE_MAX_ITEMS
        or PREVIEW_CACHE_BYTES > PREVIEW_CACHE_MAX_BYTES
    ):
        _, removed = PREVIEW_CACHE.popitem(last=False)
        PREVIEW_CACHE_BYTES -= len(removed)


def render_preview_svg(
    session: PageSession,
    requested_view: tuple[float, float, float, float] | None = None,
) -> bytes:
    """把县级 SHP 绘制为可按当前视窗刷新细节的纯 SVG。"""
    from osgeo import gdal, ogr

    snapshot = session.controller.snapshot()
    form = snapshot["form"]
    if not isinstance(form, dict):
        raise RuntimeError("页面参数不可用")
    boundary_path = resolve_preview_boundary(Path(str(
        convert_network_path(form.get("boundary", "")) or ""
    )))
    output_directory = Path(str(
        convert_network_path(form.get("output_dir", "")) or ""
    ))
    result_files = sorted(
        item for item in output_directory.iterdir()
        if item.is_file()
        and item.suffix.lower() == ".shp"
        and re.match(r"^\d{6}_", item.name)
    )
    if not result_files:
        raise FileNotFoundError(f"输出目录中没有县级 SHP：{output_directory}")
    boundary_dataset = gdal.OpenEx(str(boundary_path), gdal.OF_VECTOR | gdal.OF_READONLY)
    if boundary_dataset is None:
        raise RuntimeError("GDAL 无法打开县界 SHP")
    boundary_layer = boundary_dataset.GetLayer(0)
    boundary_srs = boundary_layer.GetSpatialRef()
    if boundary_srs is None:
        raise RuntimeError("县界缺少坐标系定义")
    boundary_srs = preview_spatial_ref(boundary_srs)
    extent = boundary_layer.GetExtent(force=1)
    if extent is None:
        raise RuntimeError("无法读取县界范围")
    minx, maxx, miny, maxy = extent
    width, height = 1400, 850
    padding_x = max((maxx - minx) * 0.035, 1e-9)
    padding_y = max((maxy - miny) * 0.035, 1e-9)
    minx, maxx = minx - padding_x, maxx + padding_x
    miny, maxy = miny - padding_y, maxy + padding_y
    target_ratio = width / height
    current_ratio = (maxx - minx) / (maxy - miny)
    if current_ratio < target_ratio:
        extra = ((maxy - miny) * target_ratio - (maxx - minx)) / 2
        minx, maxx = minx - extra, maxx + extra
    else:
        extra = ((maxx - minx) / target_ratio - (maxy - miny)) / 2
        miny, maxy = miny - extra, maxy + extra
    scale_x = width / (maxx - minx)
    scale_y = height / (maxy - miny)

    view_x, view_y, view_width, view_height = requested_view or (
        0.0, 0.0, float(width), float(height),
    )
    # 限制视窗在全图范围内，并防止过小请求造成资源浪费。
    view_width = min(float(width), max(float(width) / 16, view_width))
    view_height = min(float(height), max(float(height) / 16, view_height))
    view_x = min(float(width) - view_width, max(0.0, view_x))
    view_y = min(float(height) - view_height, max(0.0, view_y))

    boundary_stat = boundary_path.stat()
    result_signature = tuple(
        (item.name, item.stat().st_size, item.stat().st_mtime_ns)
        for item in result_files
    )
    cache_key: tuple[object, ...] = (
        str(boundary_path), boundary_stat.st_size, boundary_stat.st_mtime_ns,
        str(output_directory), result_signature,
        *(round(value, 2) for value in (view_x, view_y, view_width, view_height)),
    )
    cached = preview_cache_get(cache_key)
    if cached is not None:
        boundary_dataset = None
        return cached

    result_names = {
        match.group(1): match.group(2)
        for item in result_files
        if (match := re.match(r"^(\d{6})_(.+)$", item.stem))
    }
    boundary_definition = boundary_layer.GetLayerDefn()
    boundary_fields = {
        boundary_definition.GetFieldDefn(index).GetName().casefold():
        boundary_definition.GetFieldDefn(index).GetName()
        for index in range(boundary_definition.GetFieldCount())
    }
    code_field = next(
        (boundary_fields[name.casefold()] for name in (
            "area_code", "县代码", "行政区划代码", "adcode", "code",
        ) if name.casefold() in boundary_fields),
        None,
    )

    boundary_parts: list[str] = []
    label_parts: list[str] = []
    marker_parts: list[str] = []
    labeled_codes: set[str] = set()
    label_size = 4 * view_width / width
    label_stroke = 0.1 * view_width / width
    marker_radius = 2.2 * view_width / width
    marker_stroke = 0.8 * view_width / width
    boundary_layer.ResetReading()
    for feature in boundary_layer:
        source_geometry = feature.GetGeometryRef()
        if source_geometry is None or source_geometry.IsEmpty():
            continue
        geometry = source_geometry.Clone()
        path_data = geometry_svg_path(geometry, minx, maxy, scale_x, scale_y)
        if path_data:
            boundary_parts.append(path_data)
        if code_field is None:
            continue
        code_match = re.search(
            r"(?<!\d)(\d{6})", re.sub(r"\.0$", "", str(feature.GetField(code_field))),
        )
        if not code_match:
            continue
        code = code_match.group(1)
        if code not in result_names or code in labeled_codes:
            continue
        label_point = geometry.PointOnSurface()
        if label_point is None or label_point.IsEmpty():
            label_point = geometry.Centroid()
        if label_point is None or label_point.IsEmpty():
            continue
        label_x = (label_point.GetX() - minx) * scale_x
        label_y = (maxy - label_point.GetY()) * scale_y
        marker_parts.append(
            f'<circle cx="{svg_number(label_x)}" cy="{svg_number(label_y)}" '
            f'r="{svg_number(marker_radius)}"/>'
        )
        label_parts.append(
            f'<text x="{svg_number(label_x)}" '
            f'y="{svg_number(label_y - marker_radius - label_size * 0.75)}">'
            f'{html.escape(result_names[code])}</text>'
        )
        labeled_codes.add(code)

    # 明细逐要素转 SVG 可能产生数百 MB 的 path。仍使用 GDAL
    # 内存覆盖掩膜，但每次只按当前视窗以 2 倍显示分辨率绘制。
    # 浏览器缩放或拖动结束后会请求新视窗，因此放大到 16 倍仍清晰。
    mask_width, mask_height = width * 2, height * 2
    geo_minx = minx + view_x / scale_x
    geo_maxx = minx + (view_x + view_width) / scale_x
    geo_maxy = maxy - view_y / scale_y
    geo_miny = maxy - (view_y + view_height) / scale_y
    mask_dataset = gdal.GetDriverByName("MEM").Create(
        "", mask_width, mask_height, 1, gdal.GDT_Byte,
    )
    mask_dataset.SetProjection(boundary_srs.ExportToWkt())
    mask_dataset.SetGeoTransform((
        geo_minx, (geo_maxx - geo_minx) / mask_width, 0,
        geo_maxy, 0, -(geo_maxy - geo_miny) / mask_height,
    ))
    mask_dataset.GetRasterBand(1).Fill(0)
    rendered_features = 0
    for result_file in result_files:
        dataset = gdal.OpenEx(str(result_file), gdal.OF_VECTOR | gdal.OF_READONLY)
        if dataset is None:
            continue
        layer = dataset.GetLayer(0)
        rendered_features += max(0, layer.GetFeatureCount(force=0))
        options = gdal.RasterizeOptions(
            bands=[1], burnValues=[1], allTouched=True,
        )
        if gdal.Rasterize(mask_dataset, dataset, options=options) is None:
            raise RuntimeError(f"无法聚合渲染：{result_file}")
        dataset = None

    mask_dataset.FlushCache()
    mask_band = mask_dataset.GetRasterBand(1)
    mask_bytes = mask_band.ReadRaster(
        0, 0, mask_width, mask_height,
        buf_xsize=mask_width,
        buf_ysize=mask_height,
        buf_type=gdal.GDT_Byte,
    )
    if mask_bytes is None:
        raise RuntimeError("无法读取成果覆盖掩膜")
    result_mask_data = mask_to_svg_path(mask_bytes, mask_width, mask_height)
    mask_dataset = None

    boundary_path_data = "".join(boundary_parts)
    label_path_data = "".join(label_parts)
    marker_path_data = "".join(marker_parts)
    mask_scale_x = view_width / mask_width
    mask_scale_y = view_height / mask_height
    view_text = " ".join(svg_number(value) for value in (
        view_x, view_y, view_width, view_height,
    ))
    svg = f'''<svg class="preview-svg" xmlns="http://www.w3.org/2000/svg"
      width="{width}" height="{height}" viewBox="{view_text}"
      data-full-view="0 0 {width} {height}">
      <style>
        @font-face {{ font-family: SimHeiPreview; src: url('/simhei.ttf') format('truetype'); }}
      </style>
      <rect width="{width}" height="{height}" fill="#fff"/>
      <path d="{boundary_path_data}" fill="#be4b4d" stroke="none" fill-rule="evenodd"/>
      <path d="{result_mask_data}" transform="translate({svg_number(view_x)} {svg_number(view_y)}) scale({svg_number(mask_scale_x)} {svg_number(mask_scale_y)})" fill="#171b20"/>
      <path d="{boundary_path_data}" fill="none" stroke="#16191d" stroke-width="1.15" vector-effect="non-scaling-stroke"/>
      <g class="county-markers" fill="#ffd43b" stroke="#111827"
        stroke-width="{svg_number(marker_stroke)}">
        {marker_path_data}
      </g>
      <g class="county-labels" font-family="SimHeiPreview,SimHei,sans-serif"
        font-size="{svg_number(label_size)}" font-weight="700" text-anchor="middle"
        dominant-baseline="central" fill="#111827" stroke="#fff"
        stroke-width="{svg_number(label_stroke)}" stroke-linejoin="round" paint-order="stroke fill">
        {label_path_data}
      </g>
      <g class="map-legend" font-family="SimHeiPreview,SimHei,sans-serif" font-size="17" fill="#111827"
        transform="translate({svg_number(view_x)} {svg_number(view_y)}) scale({svg_number(view_width / width)} {svg_number(view_height / height)})">
        <rect x="28" y="28" width="250" height="75" rx="8" fill="#fff" fill-opacity=".9" stroke="#d0d5dd"/>
        <rect x="45" y="47" width="24" height="16" fill="#be4b4d" stroke="#16191d"/><text x="80" y="61">尚无成果覆盖</text>
        <rect x="45" y="76" width="24" height="16" fill="#171b20"/><text x="80" y="90">实际成果要素</text>
        <text x="1370" y="820" text-anchor="end" font-size="16" fill="#475467">
          共 {len(result_files)} 个县级 SHP，{rendered_features} 个要素</text>
      </g>
    </svg>'''
    boundary_dataset = None
    content = svg.encode("utf-8")
    preview_cache_put(cache_key, content)
    return content


def render_page(session: PageSession, message: str = "") -> bytes:
    snapshot = session.controller.snapshot()
    running = bool(snapshot["running"])
    form = snapshot["form"]
    assert isinstance(form, dict)
    index_options = "".join(
        f'<option value="{value}"{" selected" if form.get("index_mode") == value else ""}>'
        f'{label}</option>'
        for value, label in (
            ("auto", "auto（增量更新）"),
            ("skip", "skip（直接使用已有索引）"),
            ("rebuild", "rebuild（完整重建）"),
        )
    )
    notice = f'<div class="notice">{html.escape(message)}</div>' if message else ""
    checked = " checked" if form.get("overwrite") else ""
    preview_checked = " checked" if form.get("render_preview") else ""
    content = f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>县级 SHP 裁剪控制台</title>
<style>
:root{{--bg:#f5f7fa;--panel:#fff;--line:#dfe4ea;--text:#18212f;--muted:#667085;
--blue:#1769e0;--green:#16803d;--red:#c52b2b;--amber:#a15c00}}
*{{box-sizing:border-box}} body{{margin:0;background:var(--bg);color:var(--text);
font:14px/1.5 system-ui,-apple-system,"Segoe UI","Microsoft YaHei",sans-serif}}
header{{height:64px;padding:0 28px;background:#fff;border-bottom:1px solid var(--line);
display:flex;align-items:center;justify-content:space-between}} h1{{margin:0;font-size:20px}}
.subtitle{{color:var(--muted);font-size:13px;margin-left:12px}}
.badge{{border:1px solid var(--line);border-radius:999px;padding:6px 12px;font-weight:700}}
.badge.running{{color:var(--green)}} main{{display:grid;grid-template-columns:minmax(350px,430px)
minmax(600px,1fr);gap:18px;padding:18px;max-width:1800px;margin:auto}}
.panel{{background:var(--panel);border:1px solid var(--line);border-radius:12px;
box-shadow:0 1px 2px #1018280a}} .controls{{padding:20px;align-self:start}}
.section{{font-size:15px;margin:0 0 14px}} .section.next{{margin-top:22px;padding-top:18px;
border-top:1px solid #edf0f3}} .grid{{display:grid;grid-template-columns:1fr 1fr;gap:12px}}
.field{{display:flex;flex-direction:column;gap:5px}} .full{{grid-column:1/-1}}
label{{font-size:12px;font-weight:700;color:#344054}} input,select{{width:100%;
border:1px solid #cfd6df;border-radius:7px;padding:9px 10px;font:inherit}}
.hint{{font-size:11px;color:var(--muted)}} .check{{flex-direction:row;align-items:center}}
.check input{{width:auto}} .buttons{{display:flex;gap:10px;margin-top:20px}}
button{{border:0;border-radius:8px;padding:10px 17px;font:inherit;font-weight:700;cursor:pointer}}
.primary{{background:var(--blue);color:#fff;flex:1}} .danger{{background:var(--red);color:#fff;
border:1px solid var(--red)}} button:disabled{{opacity:.48;cursor:not-allowed}}
.danger:disabled{{background:#fff;color:var(--red);border-color:#efc5c5;opacity:.38}}
.notice{{margin-bottom:14px;padding:10px 12px;background:#eaf2ff;color:#174f9f;border-radius:7px}}
.workspace{{min-width:0;display:grid;gap:18px;align-content:start}} .summary{{padding:20px}}
.summary-top{{display:flex;justify-content:space-between}} .current{{font-size:20px;font-weight:750}}
.muted{{color:var(--muted)}} .percent{{font-size:28px;font-weight:780;color:var(--blue)}}
.track{{margin-top:14px;height:10px;background:#edf1f5;border-radius:999px;overflow:hidden}}
.bar{{height:100%;width:0;background:var(--blue);transition:width .25s}} .stats{{display:flex;
gap:22px;margin-top:13px;color:var(--muted);font-size:12px}} .stats strong{{color:var(--text)}}
.panel-head{{padding:14px 18px;border-bottom:1px solid var(--line);display:flex;
justify-content:space-between}} .panel-head h2{{font-size:15px;margin:0}} .table-wrap{{max-height:430px;overflow:auto}}
table{{width:100%;border-collapse:collapse}} th{{position:sticky;top:0;background:#f9fafb;
color:var(--muted);text-align:left;font-size:11px;padding:9px 12px}} td{{padding:9px 12px;
border-top:1px solid #edf0f3}} tr.active{{background:#f3f7ff}} .success{{color:var(--green)}}
.failed{{color:var(--red)}} .skipped,.no_data{{color:var(--amber)}} .terminal{{margin:0;
height:300px;overflow:auto;padding:14px 16px;background:#fbfcfd;font:12px/1.65 Consolas,monospace;
white-space:pre-wrap;word-break:break-all}} .terminal .error{{color:var(--red)}}
.terminal .command{{color:#155db1;font-weight:700}} @media(max-width:1050px){{main{{grid-template-columns:1fr}}}}
.preview-tools{{display:flex;gap:7px;align-items:center;flex-wrap:wrap;justify-content:flex-end}} .preview-tools button{{padding:5px 10px;
background:#fff;border:1px solid var(--line);color:var(--text)}} .map-viewport{{height:540px;overflow:hidden;
position:relative;background:#fff;cursor:grab;user-select:none}} .map-viewport.dragging{{cursor:grabbing}}
.map-canvas{{position:absolute;inset:0;width:100%;height:100%}}
.map-canvas svg{{display:block;width:100%;height:100%}} .map-placeholder{{height:100%;display:grid;
place-items:center;color:var(--muted);padding:30px;text-align:center}} .preview-title{{font-weight:700;color:#344054}}
.preview-detail{{font-size:11px;color:var(--green);font-weight:700}} body.preview-open{{overflow:hidden}}
#previewPanel.preview-floating{{position:fixed;inset:14px;z-index:1000;display:flex;flex-direction:column;
box-shadow:0 22px 70px #10182855;border-color:#b8c4d3}} #previewPanel.preview-floating .panel-head{{flex:0 0 auto;
background:#fff;border-radius:12px 12px 0 0}} #previewPanel.preview-floating .map-viewport{{height:auto;flex:1}}
#previewPanel.preview-floating #mapFloat{{background:#172b4d;color:#fff}}
@media(max-width:700px){{#previewPanel.preview-floating{{inset:5px}}.preview-title{{display:none}}}}
</style></head><body>
<header><h1>县级 SHP 裁剪<span class="subtitle">递归扫描 · SQLite RTree · 按县合并输出</span></h1>
<div id="badge" class="badge{" running" if running else ""}">{"运行中" if running else "空闲"}</div></header>
<main><form id="taskForm" class="panel controls" method="post" action="/start">
<input type="hidden" name="page_id" value="{session.page_id}">{notice}
<h2 class="section">路径</h2><div class="grid">
<div class="field full"><label>输入 SHP 根目录</label>
<input name="shp_dir" required value="{escaped(form, "shp_dir")}">
<span class="hint">会递归查找所有子目录中的 .shp，后续新增文件可用 auto 增量更新。</span></div>
<div class="field full"><label>县界文件或文件夹</label>
<input name="boundary" required value="{escaped(form, "boundary")}"></div>
<div class="field full"><label>输出目录</label>
<input name="output_dir" required value="{escaped(form, "output_dir")}">
<span class="hint">每县输出：六位县代码_县名称.shp，并生成 .shx、.dbf、.prj、.cpg。</span></div>
</div><h2 class="section next">索引与处理范围</h2><div class="grid">
<div class="field full"><label>SQLite 空间索引文件</label>
<input name="index" required value="{escaped(form, "index")}"></div>
<div class="field"><label>索引模式</label><select name="index_mode">{index_options}</select></div>
<div class="field"><label>索引线程数</label>
<input name="index_workers" type="number" min="1" value="{escaped(form, "index_workers")}"></div>
<div class="field full"><label>只处理这些县（可空）</label>
<input name="county" value="{escaped(form, "county")}" placeholder="150102, 150103">
<span class="hint">空白表示全部；多个六位代码用逗号或空格分隔。</span></div>
</div><h2 class="section next">资源与输出</h2><div class="grid">
<div class="field"><label>最大并发县数</label>
<input name="workers" type="number" min="1" value="{escaped(form, "workers")}"></div>
<div class="field"><label>CPU 使用比例（%）</label>
<input name="cpu_percent" type="number" min="1" max="100" value="{escaped(form, "cpu_percent")}"></div>
<div class="field full"><label>临时目录（可空）</label>
<input name="temp_dir" value="{escaped(form, "temp_dir")}"></div>
<label class="field full check"><input type="checkbox" name="overwrite" value="1"{checked}>
覆盖已有县级 Shapefile 文件组</label>
<label class="field full check"><input id="renderPreviewToggle" type="checkbox" name="render_preview" value="1"{preview_checked}>
全部完成后一次性渲染所有县级 SHP</label></div>
<div class="buttons"><button id="start" class="primary" type="submit"{" disabled" if running else ""}>开始处理</button>
<button id="stop" class="danger" type="button"{" disabled" if not running else ""}>停止任务</button></div>
</form><section class="workspace">
<div class="panel summary"><div class="summary-top"><div><div id="stage" class="muted">等待开始</div>
<div id="current" class="current">第 0 / 0 个县</div><div id="name" class="muted">尚无任务</div></div>
<div id="percent" class="percent">0%</div></div><div class="track"><div id="bar" class="bar"></div></div>
<div class="stats"><span>已完成 <strong id="completed">0</strong></span>
<span>总数 <strong id="total">0</strong></span><span>成功 <strong id="success">0</strong></span>
<span>失败 <strong id="failed">0</strong></span></div></div>
<div class="panel"><div class="panel-head"><h2>逐县处理状态</h2><span class="muted">每县固定一行</span></div>
<div class="table-wrap"><table><thead><tr><th>#</th><th>县代码</th><th>县名</th><th>状态</th><th>说明</th></tr></thead>
<tbody id="body"><tr id="empty"><td colspan="5" class="muted">尚无县级任务</td></tr></tbody></table></div></div>
<div class="panel"><div class="panel-head"><h2>脚本输出</h2><span id="stream" class="muted">正在连接 SSE…</span></div>
<pre id="terminal" class="terminal"></pre></div>
<div id="previewPanel" class="panel"><div class="panel-head"><h2>县级结果预览</h2>
<div class="preview-tools"><span id="previewTitle" class="preview-title">等待生成结果</span>
<span id="previewDetail" class="preview-detail"></span>
<button id="mapZoomOut" type="button" title="缩小">−</button><button id="mapReset" type="button">复位</button>
<button id="mapZoomIn" type="button" title="放大">＋</button>
<button id="mapFloat" type="button" title="在大窗口中查看">悬浮查看</button></div></div>
<div id="mapViewport" class="map-viewport"><div id="mapCanvas" class="map-canvas">
<div class="map-placeholder">默认不渲染。勾选左侧渲染选项后，<br>全部县处理完成时一次性显示所有已有县级 SHP。</div>
</div></div></div></section></main>
<script>
const pageId={json.dumps(session.page_id)}, rows=new Map();
let total=0,success=0,failed=0;
const byId=id=>document.getElementById(id), terminal=byId("terminal");
const mapViewport=byId("mapViewport"), mapCanvas=byId("mapCanvas"),previewPanel=byId("previewPanel");
const fullView={{x:0,y:0,w:1400,h:850}};let mapView={{...fullView}};
let mapDragging=false,mapStartX=0,mapStartY=0,mapStartView=null,previewSerial=0,detailTimer=null;
let stopRequested=false,previewAbortController=null,jobFinished=false,previewLoading=false,previewLoaded=false;
const renderPreviewToggle=byId("renderPreviewToggle");
function setProcessingState(running){{byId("start").disabled=running;byId("stop").disabled=!running}}
byId("taskForm").addEventListener("submit",()=>setProcessingState(true));
function previewEnabled(){{return Boolean(renderPreviewToggle&&renderPreviewToggle.checked)}}
function badge(text,running){{byId("badge").textContent=text;byId("badge").className="badge"+(running?" running":"")}}
function progress(done,value){{let p=Math.max(0,Math.min(100,Number(value)||0));byId("bar").style.width=p+"%";
byId("percent").textContent=(Number.isInteger(p)?p:p.toFixed(2))+"%";byId("completed").textContent=done||0;
byId("total").textContent=total}}
function reset(){{rows.clear();total=success=failed=0;byId("body").innerHTML='<tr id="empty"><td colspan="5">正在读取任务…</td></tr>';
 terminal.textContent="";progress(0,0);byId("stage").textContent="任务正在启动";badge("运行中",true);setProcessingState(true);
stopRequested=false;jobFinished=false;previewLoading=false;previewLoaded=false;
if(previewAbortController)previewAbortController.abort();previewAbortController=null;
byId("previewTitle").textContent=previewEnabled()?"等待全部处理结束":"预览未启用";
byId("previewDetail").textContent="";
mapCanvas.innerHTML='<div class="map-placeholder">'+(previewEnabled()?"全部县处理结束后，将一次性渲染所有已有县级 SHP。":"默认不渲染，请勾选左侧“渲染县级结果预览”。")+'</div>';resetMap()}}
function row(c){{if(rows.has(c.code))return rows.get(c.code);let empty=byId("empty");if(empty)empty.remove();
let r=document.createElement("tr");[c.ordinal,c.code,c.name,"等待",""].forEach(v=>{{let d=document.createElement("td");
d.textContent=v??"";r.appendChild(d)}});r.dataset.code=c.code;r.dataset.name=c.name;
r.addEventListener("click",()=>{{if(!previewEnabled()){{byId("previewTitle").textContent="请先勾选渲染预览";return}}
if(jobFinished)loadPreviewAll();else byId("previewTitle").textContent="请等待全部县处理结束"}});
byId("body").appendChild(r);rows.set(c.code,r);return r}}
function log(line,level){{let s=document.createElement("span");s.className=level||"";s.textContent=line+"\\n";
terminal.appendChild(s);while(terminal.childNodes.length>5000)terminal.firstChild.remove();terminal.scrollTop=terminal.scrollHeight}}
function clampView(){{mapView.w=Math.min(fullView.w,Math.max(fullView.w/16,mapView.w));
mapView.h=Math.min(fullView.h,Math.max(fullView.h/16,mapView.h));
mapView.x=Math.min(fullView.w-mapView.w,Math.max(0,mapView.x));
mapView.y=Math.min(fullView.h-mapView.h,Math.max(0,mapView.y))}}
function applyMap(){{let svg=mapCanvas.querySelector("svg");if(svg)svg.setAttribute("viewBox",`${{mapView.x}} ${{mapView.y}} ${{mapView.w}} ${{mapView.h}}`);
let zoom=fullView.w/mapView.w;byId("previewDetail").textContent=previewLoaded?`自适应高清 · ${{zoom.toFixed(1)}}×`:""}}
function resetMap(refresh=false){{mapView={{...fullView}};applyMap();if(refresh&&previewLoaded)scheduleDetail(0)}}
function svgPoint(clientX,clientY){{let svg=mapCanvas.querySelector("svg");if(!svg||!svg.getScreenCTM())return null;
let point=svg.createSVGPoint();point.x=clientX;point.y=clientY;return point.matrixTransform(svg.getScreenCTM().inverse())}}
function zoomMap(factor,clientX=null,clientY=null){{if(!previewLoaded)return;let oldZoom=fullView.w/mapView.w;
let nextZoom=Math.max(1,Math.min(16,oldZoom*factor));if(Math.abs(nextZoom-oldZoom)<.001)return;
let rect=mapViewport.getBoundingClientRect(),anchor=svgPoint(clientX===null?rect.left+rect.width/2:clientX,
clientY===null?rect.top+rect.height/2:clientY)||{{x:mapView.x+mapView.w/2,y:mapView.y+mapView.h/2}};
let nextW=fullView.w/nextZoom,nextH=fullView.h/nextZoom,ratio=nextW/mapView.w;
mapView.x=anchor.x-(anchor.x-mapView.x)*ratio;mapView.y=anchor.y-(anchor.y-mapView.y)*ratio;
mapView.w=nextW;mapView.h=nextH;clampView();applyMap();scheduleDetail()}}
function scheduleDetail(delay=260){{clearTimeout(detailTimer);detailTimer=setTimeout(()=>loadPreviewAll(true),delay)}}
async function loadPreviewAll(refresh=false){{if(stopRequested||!previewEnabled()||!jobFinished||(!refresh&&(previewLoading||previewLoaded)))return;
if(previewAbortController)previewAbortController.abort();previewLoading=true;
previewAbortController=new AbortController();let serial=++previewSerial;
byId("previewTitle").textContent=refresh?"正在加载当前视窗高清细节…":"正在一次性渲染全部结果…";
try{{let query=new URLSearchParams({{page_id:pageId,_:Date.now()}});
query.set("view",[mapView.x,mapView.y,mapView.w,mapView.h].map(v=>v.toFixed(4)).join(","));
let response=await fetch("/preview?"+query.toString(),{{signal:previewAbortController.signal}});if(!response.ok)throw new Error(await response.text());
let svg=await response.text();if(serial!==previewSerial)return;mapCanvas.innerHTML=svg;previewLoading=false;previewLoaded=true;
let rendered=mapCanvas.querySelector("svg"),values=rendered.getAttribute("viewBox").trim().split(/\\s+/).map(Number);
if(values.length===4&&values.every(Number.isFinite))mapView={{x:values[0],y:values[1],w:values[2],h:values[3]}};
byId("previewTitle").textContent="全部已有县级 SHP";applyMap()}}
catch(error){{previewLoading=false;if(serial!==previewSerial||error.name==="AbortError")return;byId("previewTitle").textContent="预览失败";
if(refresh&&previewLoaded){{byId("previewTitle").textContent="高清细节刷新失败，已保留当前图";return}}
mapCanvas.innerHTML='<div class="map-placeholder"></div>';mapCanvas.firstChild.textContent=String(error)}}}}
byId("mapZoomIn").addEventListener("click",()=>zoomMap(1.35));
byId("mapZoomOut").addEventListener("click",()=>zoomMap(1/1.35));byId("mapReset").addEventListener("click",()=>resetMap(true));
mapViewport.addEventListener("wheel",event=>{{event.preventDefault();zoomMap(event.deltaY<0?1.2:1/1.2,event.clientX,event.clientY)}},{{passive:false}});
mapViewport.addEventListener("pointerdown",event=>{{if(!previewLoaded||mapView.w>=fullView.w)return;mapDragging=true;
mapViewport.classList.add("dragging");mapStartX=event.clientX;mapStartY=event.clientY;mapStartView={{...mapView}};
mapViewport.setPointerCapture(event.pointerId)}});
mapViewport.addEventListener("pointermove",event=>{{if(!mapDragging)return;let rect=mapViewport.getBoundingClientRect();
mapView.x=mapStartView.x-(event.clientX-mapStartX)*mapStartView.w/rect.width;
mapView.y=mapStartView.y-(event.clientY-mapStartY)*mapStartView.h/rect.height;clampView();applyMap()}});
function endMapDrag(){{if(!mapDragging)return;mapDragging=false;mapViewport.classList.remove("dragging");scheduleDetail()}}
mapViewport.addEventListener("pointerup",endMapDrag);mapViewport.addEventListener("pointercancel",endMapDrag);
byId("mapFloat").addEventListener("click",()=>{{let open=previewPanel.classList.toggle("preview-floating");
document.body.classList.toggle("preview-open",open);byId("mapFloat").textContent=open?"退出悬浮":"悬浮查看";setTimeout(applyMap,0)}});
document.addEventListener("keydown",event=>{{if(event.key==="Escape"&&previewPanel.classList.contains("preview-floating"))byId("mapFloat").click()}});
renderPreviewToggle.addEventListener("change",()=>{{if(previewEnabled()){{byId("previewTitle").textContent="预览已启用";
if(jobFinished)loadPreviewAll();
else mapCanvas.innerHTML='<div class="map-placeholder">全部县处理结束后，将一次性渲染所有已有县级 SHP。</div>'}}
else{{previewSerial++;previewLoading=false;previewLoaded=false;if(previewAbortController)previewAbortController.abort();
byId("previewTitle").textContent="预览未启用";mapCanvas.innerHTML='<div class="map-placeholder">默认不渲染，请勾选左侧“渲染县级结果预览”。</div>';resetMap()}}}});
const labels={{success:"完成",skipped:"已跳过",no_data:"无相交数据",failed:"失败"}};
function event(e){{if(e.event==="job_plan"){{total=Number(e.total)||0;byId("body").innerHTML="";rows.clear();
(e.counties||[]).forEach(row);progress(0,0);byId("stage").textContent=`任务计划：并发 ${{e.workers}}`}}
else if(e.event==="stage")byId("stage").textContent=e.message||e.name;
else if(e.event==="county_started"){{let r=row(e);r.className="active";r.children[3].textContent="处理中";
byId("current").textContent=`第 ${{e.ordinal}} / ${{e.total}} 个县`;byId("name").textContent=`${{e.code}} · ${{e.name}}`}}
else if(e.event==="county_finished"){{let r=row(e);r.className="";r.children[3].textContent=labels[e.status]||e.status;
r.children[3].className=e.status;r.children[4].textContent=e.error||"";r.dataset.status=e.status;if(e.status==="success")success++;
if(e.status==="failed")failed++;byId("success").textContent=success;byId("failed").textContent=failed;progress(e.completed,e.percent);
}}
else if(e.event==="job_finished"){{jobFinished=true;byId("stage").textContent="全部县处理结束";
badge(e.exit_code===0?"已完成":"完成但有失败",false);if(!stopRequested&&previewEnabled())loadPreviewAll()}}
else if(e.event==="job_error"){{byId("stage").textContent="任务异常终止";log(e.message||"未知错误","error")}}}}
byId("stop").addEventListener("click",async()=>{{byId("stop").disabled=true;byId("stop").textContent="正在停止…";
stopRequested=true;previewSerial++;if(previewAbortController)previewAbortController.abort();
badge("正在停止",true);byId("stage").textContent="正在立即终止全部裁剪并发数";
let response=await fetch("/stop",{{method:"POST",headers:{{"Content-Type":"application/x-www-form-urlencoded",
"Accept":"application/json"}},body:new URLSearchParams({{page_id:pageId}})}});let result=await response.json();
log(result.message,result.ok?"warning":"error");if(!result.ok){{stopRequested=false;byId("stop").disabled=false;
byId("stop").textContent="停止任务"}}}});
const source=new EventSource("/events?page_id="+encodeURIComponent(pageId));
source.onopen=()=>byId("stream").textContent="输出流已连接（SSE）";
source.onerror=()=>byId("stream").textContent="输出流连接中断，正在重连";
source.onmessage=m=>{{let p=JSON.parse(m.data);if(p.kind==="reset")reset();else if(p.kind==="terminal")log(p.line,p.level);
else if(p.kind==="clip_event")event(p.data);else if(p.kind==="job_stopping"){{stopRequested=true;badge("正在停止",true);
byId("stage").textContent="正在终止裁剪并发数组 PID "+p.pid}}
else if(p.kind==="stop_failed"){{stopRequested=false;badge("停止失败",true);byId("stage").textContent="停止失败："+p.message;
byId("stop").disabled=false;byId("stop").textContent="再次停止"}}
else if(p.kind==="process_exit"){{let stopped=Boolean(p.stopped);badge(stopped?"已停止":(p.exit_code===0?"已完成":"异常退出"),false);
byId("stage").textContent=stopped?"任务已停止，所有裁剪并发数均已退出":"脚本已退出，退出码 "+p.exit_code;
 if(stopped)log("裁剪任务已完全停止。","warning");setProcessingState(false);
byId("stop").textContent="停止任务"}}}};
</script></body></html>"""
    return content.encode("utf-8")


class RequestHandler(BaseHTTPRequestHandler):
    server_version = "CountyShpClipHTTP/1.0"

    def log_message(self, fmt: str, *args: object) -> None:
        return

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/":
            page_id = single_value(parse_qs(parsed.query), "page_id")
            session = SESSIONS.get(page_id) if page_id else None
            if session is None:
                session = SESSIONS.create()
            self._html(render_page(session))
        elif parsed.path == "/events":
            page_id = single_value(parse_qs(parsed.query), "page_id")
            session = SESSIONS.get(page_id)
            if session is None:
                self.send_response(HTTPStatus.NO_CONTENT)
                self.end_headers()
                return
            self._events(session.broker)
        elif parsed.path == "/preview":
            values = parse_qs(parsed.query)
            session = SESSIONS.get(single_value(values, "page_id"))
            if session is None:
                self.send_error(HTTPStatus.GONE, "页面会话已失效")
                return
            try:
                requested_view = None
                view_text = single_value(values, "view")
                if view_text:
                    view_values = tuple(float(value) for value in view_text.split(","))
                    if len(view_values) != 4 or not all(
                        value == value and abs(value) != float("inf")
                        for value in view_values
                    ):
                        raise ValueError("视窗参数无效")
                    requested_view = view_values
                with PREVIEW_LOCK:
                    content = render_preview_svg(session, requested_view)
            except Exception as exc:
                message = f"预览生成失败：{exc}".encode("utf-8", errors="replace")
                self._binary(message, "text/plain; charset=utf-8", HTTPStatus.INTERNAL_SERVER_ERROR)
                return
            self._binary(content, "image/svg+xml; charset=utf-8")
        elif parsed.path == "/simhei.ttf":
            if not FONT_PATH.is_file():
                self.send_error(HTTPStatus.NOT_FOUND, "找不到 simhei.ttf")
                return
            self._binary(
                FONT_PATH.read_bytes(),
                "font/ttf",
                cache_control="public, max-age=86400",
            )
        else:
            self.send_error(HTTPStatus.NOT_FOUND)

    def do_POST(self) -> None:
        path = urlparse(self.path).path
        if path not in {"/start", "/stop"}:
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        length = int(self.headers.get("Content-Length", "0"))
        if length > 1_000_000:
            self.send_error(HTTPStatus.REQUEST_ENTITY_TOO_LARGE)
            return
        form = parse_form(self.rfile.read(length))
        session = SESSIONS.get(form.get("page_id", ""))
        if session is None:
            session = SESSIONS.create()
            self._html(render_page(session, "原页面会话已失效，请重新开始。"))
            return
        if path == "/start":
            ok, message = session.controller.start(form)
            self._html(render_page(session, message))
        else:
            ok, message = session.controller.stop()
            content = json.dumps({"ok": ok, "message": message}, ensure_ascii=False).encode("utf-8")
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(content)))
            self.end_headers()
            self.wfile.write(content)

    def _html(self, content: bytes) -> None:
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(content)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(content)

    def _binary(
        self,
        content: bytes,
        content_type: str,
        status: HTTPStatus = HTTPStatus.OK,
        cache_control: str = "no-store",
    ) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(content)))
        self.send_header("Cache-Control", cache_control)
        self.end_headers()
        self.wfile.write(content)

    def _events(self, broker: EventBroker) -> None:
        try:
            sequence = int(self.headers.get("Last-Event-ID", "0"))
        except ValueError:
            sequence = 0
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()
        try:
            while True:
                events = broker.wait_after(sequence)
                if not events:
                    self.wfile.write(b": heartbeat\n\n")
                    self.wfile.flush()
                    continue
                for sequence, payload in events:
                    data = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
                    self.wfile.write(f"id: {sequence}\ndata: {data}\n\n".encode("utf-8"))
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            return


def parse_server_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="启动县级 SHP 裁剪 Web 控制界面")
    parser.add_argument("--host", default="0.0.0.0", help="监听地址")
    parser.add_argument("--port", type=int, default=9008, help="监听端口")
    parser.add_argument("--open-browser", action="store_true", help="启动后自动打开浏览器")
    parser.add_argument("--render-svg", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--boundary", help=argparse.SUPPRESS)
    parser.add_argument("--output-dir", help=argparse.SUPPRESS)
    parser.add_argument("--svg-output", help=argparse.SUPPRESS)
    return parser.parse_args()


def get_lan_ip() -> str:
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        probe.connect(("10.10.10.11", 9))
        return str(probe.getsockname()[0])
    except OSError:
        return "127.0.0.1"
    finally:
        probe.close()


def main() -> int:
    args = parse_server_args()
    if args.render_svg:
        if not args.boundary or not args.output_dir or not args.svg_output:
            print("生成 SVG 缺少 boundary、output-dir 或 svg-output", file=sys.stderr)
            return 2
        session = PageSession(uuid.uuid4().hex)
        session.controller.form.update({
            "boundary": args.boundary,
            "output_dir": args.output_dir,
        })
        destination = Path(args.svg_output)
        destination.parent.mkdir(parents=True, exist_ok=True)
        with PREVIEW_LOCK:
            content = render_preview_svg(session)
        temporary = destination.with_suffix(destination.suffix + ".tmp")
        temporary.write_bytes(content)
        os.replace(temporary, destination)
        print(f"县级裁剪成果 SVG：{destination}", flush=True)
        return 0
    if not SCRIPT_PATH.is_file():
        print(f"找不到裁剪脚本：{SCRIPT_PATH}", file=sys.stderr)
        return 2
    server = ThreadingHTTPServer((args.host, args.port), RequestHandler)
    host = get_lan_ip() if args.host in {"0.0.0.0", "::"} else args.host
    url = f"http://{host}:{args.port}/"
    print(f"县级 SHP 裁剪界面已启动：{url}", flush=True)
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
