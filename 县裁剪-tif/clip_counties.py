#!/usr/bin/env python
"""用持久化影像空间索引和 GDAL VRT 批量裁剪县级影像。"""

from __future__ import annotations

import argparse
import base64
import ctypes
import json
import logging
import math
import multiprocessing
import os
import re
import shutil
import signal
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from concurrent.futures import Future, ProcessPoolExecutor, ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator, Sequence

from osgeo import gdal, ogr, osr


LOG = logging.getLogger("clip_counties")
TIFF_SUFFIXES = {".tif", ".tiff"}
DATE_RE = re.compile(r"^\d{8}$")
RESOLUTION_RE = re.compile(r"^[^\\/:*?\"<>|]+$")
_thread_state = threading.local()
_event_lock = threading.Lock()
_events_enabled = False
_WORKER_RESULT_PREFIX = "@@COUNTY_WORKER_RESULT@@"
gdal.UseExceptions()
ogr.UseExceptions()


class StopRequested(Exception):
    """收到页面或终端发来的温和停止信号。"""


def install_stop_signal_handlers() -> None:
    def request_stop(signum: int, _frame: object) -> None:
        raise StopRequested(f"收到停止信号 {signum}")

    for signal_name in ("SIGINT", "SIGTERM", "SIGBREAK"):
        candidate = getattr(signal, signal_name, None)
        if candidate is not None:
            signal.signal(candidate, request_stop)


@dataclass(frozen=True)
class RasterRecord:
    path: str
    size: int
    mtime_ns: int
    crs_wkt: str
    footprint_wkb: bytes
    minx: float
    maxx: float
    miny: float
    maxy: float


@dataclass(frozen=True)
class CountyTask:
    code: str
    name: str
    geometry_wgs84_wkb: bytes
    ordinal: int
    total: int


@dataclass(frozen=True)
class RuntimeConfig:
    index_path: Path
    output_dir: Path
    temp_root: Path
    date1: str
    date2: str
    resolution: str
    name_template: str
    gdalbuildvrt: str
    gdalwarp: str
    gdaladdo: str
    overwrite: bool
    pixel_size: float | None
    resampling: str
    creation_options: tuple[str, ...]
    threads_per_job: int
    memory_per_job_mb: int
    overview_max_factor: int


class InterProcessFileLock:
    """用一个稳定的旁路文件为目标文件提供跨 Python 并发数互斥。"""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._handle: object | None = None

    def __enter__(self) -> InterProcessFileLock:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = open(self.path, "a+b")
        handle.seek(0, os.SEEK_END)
        if handle.tell() == 0:
            handle.write(b"0")
            handle.flush()
        handle.seek(0)
        if os.name == "nt":
            import msvcrt

            while True:
                try:
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                    break
                except OSError:
                    time.sleep(0.1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        self._handle = handle
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        handle = self._handle
        if handle is None:
            return
        handle.seek(0)
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        handle.close()
        self._handle = None


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="按县界检索相关 TIFF，建立临时 VRT，并用 gdalwarp 一次裁剪。",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--imagery-dir", required=True, type=Path, help="影像根目录，可为 UNC 路径")
    parser.add_argument("--boundary", type=Path, default=Path("00县边界"), help="县界文件或文件夹")
    parser.add_argument("--output-dir", required=True, type=Path, help="输出目录")
    parser.add_argument("--date1", required=True, help="八位日期，例如 20250101")
    parser.add_argument("--date2", required=True, help="八位日期，例如 20251231")
    parser.add_argument("--resolution", required=True, help="文件名分辨率标记，例如 0.5m 或 2m")
    parser.add_argument(
        "--name-template",
        default="ELDOM{code}_{date1}_{date2}_{resolution}.tif",
        help="可用字段：code、date1、date2、resolution、name",
    )
    parser.add_argument("--code-field", help="县代码字段；不指定时自动识别")
    parser.add_argument("--name-field", help="县名字段；不指定时自动识别")
    parser.add_argument("--county", action="append", help="只处理指定六位县代码，可重复使用")
    parser.add_argument("--index", type=Path, default=Path("imagery_index.sqlite"), help="SQLite 空间索引")
    parser.add_argument(
        "--index-mode",
        choices=("auto", "skip", "rebuild"),
        default="auto",
        help="auto 增量更新；skip 完全跳过扫描；rebuild 完整重建",
    )
    parser.add_argument("--index-workers", type=int, default=4, help="并行读取新增影像元数据的线程数")
    parser.add_argument("--workers", type=int, default=4, help="同时处理的最大县数")
    parser.add_argument(
        "--cpu-percent",
        type=float,
        default=75.0,
        help="本任务及其全部子并发数最高可调用的 CPU 资源百分比",
    )
    parser.add_argument(
        "--gdal-memory-gb",
        type=float,
        default=8.0,
        help="所有并发县任务合计可使用的 GDAL 内存预算（GB）",
    )
    parser.add_argument("--pixel-size", type=float, help="显式指定输出像元大小；省略则保持 VRT 分辨率")
    parser.add_argument(
        "--resampling",
        default="near",
        choices=("near", "bilinear", "cubic", "cubicspline", "lanczos", "average", "mode"),
        help="GDAL 重采样算法",
    )
    parser.add_argument("--overwrite", action="store_true", help="覆盖已有结果")
    parser.add_argument("--temp-dir", type=Path, help="临时 VRT 目录，默认使用系统临时目录")
    parser.add_argument("--gdal-bin", type=Path, help="包含 gdalbuildvrt 和 gdalwarp 的目录")
    parser.add_argument(
        "--overview-max-factor",
        type=int,
        default=256,
        help="外部和内部金字塔的最高倍率，必须是 2 到 256 的 2 次幂",
    )
    parser.add_argument(
        "--creation-option",
        action="append",
        default=[],
        help="额外 GTiff 创建选项，例如 PREDICTOR=2，可重复使用",
    )
    parser.add_argument("--log-file", type=Path, help="日志文件；默认写入输出目录")
    parser.add_argument("--emit-progress-events", action="store_true", help=argparse.SUPPRESS)
    return parser.parse_args(argv)


def emit_event(event: str, **payload: object) -> None:
    """向 Web 包装器输出单行、可解析的进度事件。"""
    if not _events_enabled:
        return
    message = json.dumps({"event": event, **payload}, ensure_ascii=False, separators=(",", ":"))
    with _event_lock:
        print(f"@@CLIP_EVENT@@{message}", flush=True)


def validate_args(args: argparse.Namespace) -> None:
    for label, value in (("date1", args.date1), ("date2", args.date2)):
        if not DATE_RE.fullmatch(value):
            raise ValueError(f"--{label} 必须是 8 位数字，当前为 {value!r}")
        time.strptime(value, "%Y%m%d")
    if not RESOLUTION_RE.fullmatch(args.resolution):
        raise ValueError("--resolution 含 Windows 文件名非法字符")
    if not 0 < args.cpu_percent <= 100:
        raise ValueError("--cpu-percent 必须在 (0, 100] 范围内")
    if args.gdal_memory_gb < 0.125:
        raise ValueError("--gdal-memory-gb 不能小于 0.125 GB")
    if args.workers < 1 or args.index_workers < 1:
        raise ValueError("--workers 和 --index-workers 必须大于等于 1")
    if not 2 <= args.overview_max_factor <= 256 or (
        args.overview_max_factor & (args.overview_max_factor - 1)
    ):
        raise ValueError("--overview-max-factor 必须是 2 到 256 的 2 次幂")
    if args.pixel_size is not None and args.pixel_size <= 0:
        raise ValueError("--pixel-size 必须大于 0")
    try:
        args.name_template.format(
            code="150102", date1=args.date1, date2=args.date2,
            resolution=args.resolution, name="新城区",
        )
    except (KeyError, ValueError) as exc:
        raise ValueError(f"--name-template 无效：{exc}") from exc


def limit_cpu_affinity(cpu_percent: float) -> tuple[int, int]:
    """限制当前并发数可运行的 CPU；县级 Python/GDAL 子并发数会继承该限制。"""
    logical_cpus = os.cpu_count() or 1
    requested = max(1, math.floor(logical_cpus * cpu_percent / 100.0))
    if os.name == "nt":
        kernel32 = ctypes.windll.kernel32
        kernel32.GetCurrentProcess.restype = ctypes.c_void_p
        kernel32.GetProcessAffinityMask.argtypes = [
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_size_t),
            ctypes.POINTER(ctypes.c_size_t),
        ]
        kernel32.GetProcessAffinityMask.restype = ctypes.c_int
        kernel32.SetProcessAffinityMask.argtypes = [ctypes.c_void_p, ctypes.c_size_t]
        kernel32.SetProcessAffinityMask.restype = ctypes.c_int
        process = kernel32.GetCurrentProcess()
        process_mask = ctypes.c_size_t()
        system_mask = ctypes.c_size_t()
        if not kernel32.GetProcessAffinityMask(
            process, ctypes.byref(process_mask), ctypes.byref(system_mask)
        ):
            raise OSError("GetProcessAffinityMask 调用失败")
        available = [index for index in range(logical_cpus) if process_mask.value & (1 << index)]
        selected = available[: min(requested, len(available))]
        selected_mask = sum(1 << index for index in selected)
        if not selected_mask or not kernel32.SetProcessAffinityMask(process, selected_mask):
            raise OSError("SetProcessAffinityMask 调用失败")
        return logical_cpus, len(selected)
    if hasattr(os, "sched_getaffinity") and hasattr(os, "sched_setaffinity"):
        available = sorted(os.sched_getaffinity(0))
        selected_count = min(requested, len(available))
        os.sched_setaffinity(0, set(available[:selected_count]))
        return logical_cpus, selected_count
    LOG.warning("当前操作系统不支持 CPU 亲和性；仅使用线程数限制 CPU 资源")
    return logical_cpus, requested


def setup_logging(log_path: Path) -> None:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    formatter = logging.Formatter("%(asctime)s | %(levelname)s | %(message)s")
    stream = logging.StreamHandler()
    stream.setFormatter(formatter)
    file_handler = logging.FileHandler(log_path, encoding="utf-8")
    file_handler.setFormatter(formatter)
    LOG.setLevel(logging.INFO)
    LOG.handlers[:] = [stream, file_handler]


def resolve_boundary(path: Path) -> Path:
    if path.is_file():
        return path
    if not path.is_dir():
        raise FileNotFoundError(f"县界路径不存在：{path}")
    candidates = sorted(
        p for p in path.iterdir()
        if p.suffix.lower() in {".shp", ".gpkg", ".geojson", ".json"}
    )
    if len(candidates) != 1:
        raise ValueError(f"县界文件夹中应有且仅有一个矢量文件，实际找到 {len(candidates)} 个")
    return candidates[0]


def spatial_ref(value: str | int) -> osr.SpatialReference:
    reference = osr.SpatialReference()
    if isinstance(value, int):
        reference.ImportFromEPSG(value)
    else:
        reference.SetFromUserInput(value)
    reference.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
    return reference


def choose_field(
    columns: list[str],
    samples: dict[str, list[object]],
    requested: str | None,
    kind: str,
) -> str | None:
    if requested:
        if requested not in columns:
            raise ValueError(f"字段 {requested!r} 不存在；可用字段：{columns}")
        return requested
    preferred = (
        ("area_code", "县代码", "行政区划代码", "adcode", "code")
        if kind == "code"
        else ("area_name", "县名称", "行政区名称", "name", "NAME")
    )
    for field in preferred:
        if field in columns:
            return field
    if kind == "name":
        return None
    for field in columns:
        values = [str(value) for value in samples[field] if value is not None]
        if values and sum(bool(re.search(r"(?<!\d)\d{6}", value)) for value in values) / len(values) > 0.9:
            return field
    raise ValueError(f"无法自动识别县代码字段；请用 --code-field 指定。可用字段：{columns}")


def six_digit_code(value: object) -> str:
    text = re.sub(r"\.0$", "", str(value).strip())
    match = re.search(r"(?<!\d)(\d{6})", text)
    if not match:
        raise ValueError(f"无法从 {value!r} 提取六位县代码")
    return match.group(1)


def load_counties(
    boundary: Path,
    code_field: str | None,
    name_field: str | None,
    selected_codes: set[str] | None,
) -> list[CountyTask]:
    dataset = gdal.OpenEx(str(boundary), gdal.OF_VECTOR | gdal.OF_READONLY)
    if dataset is None:
        raise ValueError(f"无法打开县界文件：{boundary}")
    layer = dataset.GetLayer(0)
    source_srs = layer.GetSpatialRef()
    if source_srs is None:
        raise ValueError("县界文件没有坐标系定义")
    source_srs = source_srs.Clone()
    source_srs.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
    wgs84 = spatial_ref(4326)
    coordinate_transform = osr.CoordinateTransformation(source_srs, wgs84)
    definition = layer.GetLayerDefn()
    columns = [
        definition.GetFieldDefn(index).GetName()
        for index in range(definition.GetFieldCount())
    ]
    samples = {field: [] for field in columns}
    layer.ResetReading()
    for index, feature in enumerate(layer):
        for field in columns:
            samples[field].append(feature.GetField(field))
        if index >= 99:
            break
    code_field = choose_field(columns, samples, code_field, "code")
    name_field = choose_field(columns, samples, name_field, "name")
    grouped: dict[str, tuple[str, ogr.Geometry]] = {}
    layer.ResetReading()
    for feature in layer:
        geometry = feature.GetGeometryRef()
        if geometry is None or geometry.IsEmpty():
            continue
        code = six_digit_code(feature.GetField(code_field))
        if selected_codes and code not in selected_codes:
            continue
        name = str(feature.GetField(name_field)) if name_field else code
        county_geometry = geometry.Clone()
        county_geometry.Transform(coordinate_transform)
        if not county_geometry.IsValid():
            county_geometry = county_geometry.MakeValid()
        if code in grouped:
            old_name, old_geometry = grouped[code]
            grouped[code] = (old_name, old_geometry.Union(county_geometry))
        else:
            grouped[code] = (name, county_geometry)
    if selected_codes:
        missing = selected_codes - set(grouped)
        if missing:
            raise ValueError(f"县界中找不到指定代码：{sorted(missing)}")
    tasks: list[CountyTask] = []
    total = len(grouped)
    for ordinal, code in enumerate(sorted(grouped), start=1):
        name, geometry = grouped[code]
        tasks.append(CountyTask(code, name, bytes(geometry.ExportToWkb()), ordinal, total))
    LOG.info("县界：%s；代码字段：%s；待处理县数：%d", boundary, code_field, len(tasks))
    return tasks


def iter_tiffs(root: Path) -> Iterator[Path]:
    if not root.is_dir():
        raise FileNotFoundError(f"影像目录不存在或不可访问：{root}")
    stack = [root]
    while stack:
        current = stack.pop()
        with os.scandir(current) as entries:
            for entry in entries:
                try:
                    if entry.is_dir(follow_symlinks=False):
                        stack.append(Path(entry.path))
                    elif entry.is_file(follow_symlinks=False) and Path(entry.name).suffix.lower() in TIFF_SUFFIXES:
                        yield Path(entry.path)
                except OSError as exc:
                    LOG.warning("跳过无法访问的目录项 %s：%s", entry.path, exc)


def pixel_to_map(transform_values: tuple[float, ...], pixel: float, line: float) -> tuple[float, float]:
    return (
        transform_values[0] + pixel * transform_values[1] + line * transform_values[2],
        transform_values[3] + pixel * transform_values[4] + line * transform_values[5],
    )


def densified_raster_footprint(
    width: int,
    height: int,
    transform_values: tuple[float, ...],
    n: int = 16,
) -> ogr.Geometry:
    pixels: list[tuple[float, float]] = []
    for index in range(n + 1):
        pixels.append((width * index / n, 0))
    for index in range(1, n + 1):
        pixels.append((width, height * index / n))
    for index in range(1, n + 1):
        pixels.append((width * (1 - index / n), height))
    for index in range(1, n):
        pixels.append((0, height * (1 - index / n)))
    ring = ogr.Geometry(ogr.wkbLinearRing)
    for pixel, line in pixels:
        ring.AddPoint_2D(*pixel_to_map(transform_values, pixel, line))
    ring.CloseRings()
    polygon = ogr.Geometry(ogr.wkbPolygon)
    polygon.AddGeometry(ring)
    return polygon


def inspect_raster(path: Path, stat: os.stat_result | None = None) -> RasterRecord:
    stat = stat or path.stat()
    dataset = gdal.OpenEx(str(path), gdal.OF_RASTER | gdal.OF_READONLY)
    if dataset is None:
        raise ValueError("GDAL 无法打开")
    source_srs = dataset.GetSpatialRef()
    if source_srs is None:
        raise ValueError("缺少坐标系")
    source_srs = source_srs.Clone()
    source_srs.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
    transform_values = dataset.GetGeoTransform(True)
    if transform_values is None:
        raise ValueError("缺少有效地理变换")
    footprint = densified_raster_footprint(
        dataset.RasterXSize, dataset.RasterYSize, transform_values,
    )
    coordinate_transform = osr.CoordinateTransformation(source_srs, spatial_ref(4326))
    footprint.Transform(coordinate_transform)
    if not footprint.IsValid():
        footprint = footprint.MakeValid()
    minx, maxx, miny, maxy = footprint.GetEnvelope()
    record = RasterRecord(
        path=str(path.resolve()),
        size=stat.st_size,
        mtime_ns=stat.st_mtime_ns,
        crs_wkt=source_srs.ExportToWkt(),
        footprint_wkb=bytes(footprint.ExportToWkb()),
        minx=minx,
        maxx=maxx,
        miny=miny,
        maxy=maxy,
    )
    dataset = None
    return record


def connect_index(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path, timeout=60)
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA synchronous=NORMAL")
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS rasters (
            id INTEGER PRIMARY KEY,
            path TEXT NOT NULL UNIQUE,
            size INTEGER NOT NULL,
            mtime_ns INTEGER NOT NULL,
            crs_wkt TEXT NOT NULL,
            footprint_wkb BLOB NOT NULL
        );
        CREATE VIRTUAL TABLE IF NOT EXISTS raster_rtree USING rtree(
            id, minx, maxx, miny, maxy
        );
        CREATE TABLE IF NOT EXISTS metadata (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );
        """
    )
    return connection


def upsert_record(connection: sqlite3.Connection, record: RasterRecord) -> None:
    old = connection.execute("SELECT id FROM rasters WHERE path = ?", (record.path,)).fetchone()
    if old:
        raster_id = int(old[0])
        connection.execute(
            """UPDATE rasters SET size=?, mtime_ns=?, crs_wkt=?, footprint_wkb=? WHERE id=?""",
            (record.size, record.mtime_ns, record.crs_wkt, record.footprint_wkb, raster_id),
        )
        connection.execute("DELETE FROM raster_rtree WHERE id=?", (raster_id,))
    else:
        cursor = connection.execute(
            """INSERT INTO rasters(path,size,mtime_ns,crs_wkt,footprint_wkb)
               VALUES(?,?,?,?,?)""",
            (record.path, record.size, record.mtime_ns, record.crs_wkt, record.footprint_wkb),
        )
        raster_id = int(cursor.lastrowid)
    connection.execute(
        "INSERT INTO raster_rtree(id,minx,maxx,miny,maxy) VALUES(?,?,?,?,?)",
        (raster_id, record.minx, record.maxx, record.miny, record.maxy),
    )


def update_index(index_path: Path, imagery_dir: Path, mode: str, workers: int) -> None:
    lock_path = index_path.with_name(f".{index_path.name}.lock")
    with InterProcessFileLock(lock_path):
        _update_index_locked(index_path, imagery_dir, mode, workers)


def _update_index_locked(index_path: Path, imagery_dir: Path, mode: str, workers: int) -> None:
    if mode == "skip":
        if not index_path.is_file():
            raise FileNotFoundError(f"--index-mode skip 需要已有索引：{index_path}")
        with connect_index(index_path) as connection:
            count = connection.execute("SELECT COUNT(*) FROM rasters").fetchone()[0]
        LOG.info("跳过目录扫描，直接使用索引（%d 幅）：%s", count, index_path)
        return

    with connect_index(index_path) as connection:
        if mode == "rebuild":
            connection.execute("DELETE FROM raster_rtree")
            connection.execute("DELETE FROM rasters")
            connection.commit()
        existing = {
            row[0]: (int(row[1]), int(row[2]))
            for row in connection.execute("SELECT path,size,mtime_ns FROM rasters")
        }
        seen: set[str] = set()
        changed: list[tuple[Path, os.stat_result]] = []
        scan_errors = 0
        for path in iter_tiffs(imagery_dir):
            try:
                stat = path.stat()
                normalized = str(path.resolve())
                seen.add(normalized)
                if existing.get(normalized) != (stat.st_size, stat.st_mtime_ns):
                    changed.append((path, stat))
            except OSError as exc:
                scan_errors += 1
                LOG.warning("无法读取影像文件状态 %s：%s", path, exc)
        deleted = set(existing) - seen
        LOG.info(
            "影像扫描完成：发现 %d 幅，新增/变化 %d 幅，删除 %d 幅，状态错误 %d 个",
            len(seen), len(changed), len(deleted), scan_errors,
        )
        failures: list[str] = []
        if changed:
            # GDAL 数据集读取放入 spawn 子并发数，避免多个线程共享 GDAL 驱动状态。
            # SQLite 写入仍只发生在当前并发数，杜绝并发写索引文件。
            with ProcessPoolExecutor(
                max_workers=workers,
                mp_context=multiprocessing.get_context("spawn"),
            ) as pool:
                future_map = {
                    pool.submit(inspect_raster, path, stat): path for path, stat in changed
                }
                completed = 0
                for future in as_completed(future_map):
                    path = future_map[future]
                    try:
                        upsert_record(connection, future.result())
                    except Exception as exc:
                        failures.append(str(path))
                        LOG.error("影像索引失败 %s：%s", path, exc)
                    completed += 1
                    if completed % 50 == 0:
                        connection.commit()
                        LOG.info("索引元数据进度：%d/%d", completed, len(changed))
        for path in deleted:
            row = connection.execute("SELECT id FROM rasters WHERE path=?", (path,)).fetchone()
            if row:
                connection.execute("DELETE FROM raster_rtree WHERE id=?", (row[0],))
                connection.execute("DELETE FROM rasters WHERE id=?", (row[0],))
        connection.execute(
            "INSERT OR REPLACE INTO metadata(key,value) VALUES('imagery_dir',?)",
            (str(imagery_dir.resolve()),),
        )
        connection.execute(
            "INSERT OR REPLACE INTO metadata(key,value) VALUES('updated_at',?)",
            (time.strftime("%Y-%m-%d %H:%M:%S"),),
        )
        connection.commit()
        count = connection.execute("SELECT COUNT(*) FROM rasters").fetchone()[0]
        LOG.info("空间索引已保存：%s（有效影像 %d 幅）", index_path, count)
        if failures:
            raise RuntimeError(f"{len(failures)} 幅影像无法建立索引；详见日志")


def query_rasters(index_path: Path, county_geometry: ogr.Geometry) -> list[tuple[str, str]]:
    connection: sqlite3.Connection | None = getattr(_thread_state, "index_connection", None)
    if connection is None:
        connection = sqlite3.connect(index_path, timeout=60)
        _thread_state.index_connection = connection
    minx, maxx, miny, maxy = county_geometry.GetEnvelope()
    rows = connection.execute(
        """
        SELECT r.path, r.crs_wkt, r.footprint_wkb
        FROM raster_rtree x JOIN rasters r ON r.id=x.id
        WHERE x.minx <= ? AND x.maxx >= ? AND x.miny <= ? AND x.maxy >= ?
        """,
        (maxx, minx, maxy, miny),
    )
    selected: list[tuple[str, str]] = []
    for path, crs_wkt, footprint_blob in rows:
        footprint = ogr.CreateGeometryFromWkb(footprint_blob)
        if footprint.Intersects(county_geometry):
            selected.append((path, crs_wkt))
    return selected


def resolve_gdal_command(name: str, gdal_bin: Path | None) -> str:
    names = (f"{name}.exe", name) if os.name == "nt" else (name,)
    if gdal_bin:
        for candidate_name in names:
            candidate = gdal_bin / candidate_name
            if candidate.is_file():
                return str(candidate.resolve())
        raise FileNotFoundError(f"{gdal_bin} 中找不到 {name}")
    for candidate_name in names:
        found = shutil.which(candidate_name)
        if found:
            return found
    raise FileNotFoundError(
        f"找不到 {name}。请安装 GDAL 命令行工具并加入 PATH，"
        "或用 --gdal-bin 指向其 bin 目录。"
    )


def safe_output_name(template: str, task: CountyTask, args: argparse.Namespace) -> str:
    name = template.format(
        code=task.code,
        date1=args.date1,
        date2=args.date2,
        resolution=args.resolution,
        name=task.name,
    )
    if Path(name).name != name or not name.lower().endswith((".tif", ".tiff")):
        raise ValueError(f"输出模板必须生成单个 TIFF 文件名，当前为：{name!r}")
    if not RESOLUTION_RE.fullmatch(name):
        raise ValueError(f"输出文件名含非法字符：{name!r}")
    return name


def run_checked(command: list[str], env: dict[str, str], log_prefix: str) -> None:
    LOG.debug("%s：%s", log_prefix, subprocess.list2cmdline(command))
    completed = subprocess.run(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=env,
        check=False,
    )
    if completed.returncode:
        output = completed.stdout[-12000:].strip()
        raise RuntimeError(f"{log_prefix} 失败（退出码 {completed.returncode}）：\n{output}")
    if completed.stdout.strip():
        LOG.debug("%s 输出：%s", log_prefix, completed.stdout.strip())


def overview_levels(max_factor: int) -> list[int]:
    """返回 2、4、8……直到配置的最高金字塔倍率。"""
    levels: list[int] = []
    factor = 2
    while factor <= max_factor:
        levels.append(factor)
        factor *= 2
    return levels


def build_external_and_internal_overviews(
    raster_path: Path,
    config: RuntimeConfig,
    env: dict[str, str],
    task: CountyTask,
) -> Path:
    """先建外部 .ovr，再隐藏它并构建内部金字塔，最终同时保留二者。"""
    levels = [str(level) for level in overview_levels(config.overview_max_factor)]
    external_path = Path(f"{raster_path}.ovr")
    saved_external = raster_path.with_name(f".{raster_path.name}.external.{uuid.uuid4().hex}.ovr")
    common = [
        "--config", "GDAL_NUM_THREADS", str(config.threads_per_job),
        "--config", "COMPRESS_OVERVIEW", "DEFLATE",
        "--config", "BIGTIFF_OVERVIEW", "YES",
        "-r", config.resampling,
    ]
    LOG.info(
        "[%s %s] 正在构建外部 .ovr 金字塔（最高 %sx）",
        task.code, task.name, config.overview_max_factor,
    )
    run_checked(
        [config.gdaladdo, *common, "-ro", str(raster_path), *levels],
        env,
        f"[{task.code}] gdaladdo 外部金字塔",
    )
    if not external_path.is_file() or external_path.stat().st_size == 0:
        raise RuntimeError("gdaladdo 未生成有效的外部 .ovr 金字塔")

    # 已存在外部 .ovr 时，GDAL 可能继续更新外部层。临时移走它，强制写入 TIFF 内部。
    os.replace(external_path, saved_external)
    try:
        LOG.info(
            "[%s %s] 外部金字塔完成，正在构建内部金字塔",
            task.code, task.name,
        )
        run_checked(
            [config.gdaladdo, *common, str(raster_path), *levels],
            env,
            f"[{task.code}] gdaladdo 内部金字塔",
        )
        overview_dataset = gdal.OpenEx(str(raster_path), gdal.OF_RASTER | gdal.OF_READONLY)
        if overview_dataset is None or overview_dataset.RasterCount < 1:
            raise RuntimeError("无法重新打开 TIFF 检查内部金字塔")
        overview_band = overview_dataset.GetRasterBand(1)
        actual_count = overview_band.GetOverviewCount()
        overview_dataset = None
        if actual_count < len(levels):
            raise RuntimeError(
                f"内部金字塔层数不足：期望 {len(levels)} 层，实际 {actual_count} 层"
            )
    finally:
        if saved_external.exists():
            os.replace(saved_external, external_path)
    return external_path


def process_county(task: CountyTask, config: RuntimeConfig) -> tuple[str, str]:
    emit_event(
        "county_started",
        code=task.code,
        name=task.name,
        ordinal=task.ordinal,
        total=task.total,
    )
    output_name = config.name_template.format(
        code=task.code, date1=config.date1, date2=config.date2,
        resolution=config.resolution, name=task.name,
    )
    output_path = config.output_dir / output_name
    lock_path = output_path.with_name(f".{output_path.name}.lock")
    with InterProcessFileLock(lock_path):
        return _process_county_locked(task, config, output_path)


def _process_county_locked(
    task: CountyTask,
    config: RuntimeConfig,
    output_path: Path,
) -> tuple[str, str]:
    if output_path.is_file() and output_path.stat().st_size > 0 and not config.overwrite:
        LOG.info("[%s %s] 跳过已有结果：%s", task.code, task.name, output_path)
        return task.code, "skipped"

    county_wgs84 = ogr.CreateGeometryFromWkb(task.geometry_wgs84_wkb)
    raster_rows = query_rasters(config.index_path, county_wgs84)
    if not raster_rows:
        LOG.warning("[%s %s] 无相交影像", task.code, task.name)
        return task.code, "no_image"
    image_srs = spatial_ref(raster_rows[0][1])
    for _, candidate_wkt in raster_rows[1:]:
        if not image_srs.IsSame(spatial_ref(candidate_wkt)):
            raise RuntimeError(
                f"[{task.code} {task.name}] 相交影像包含多种坐标系；"
                "请先统一投影后再处理"
            )
    cutline_geometry = county_wgs84.Clone()
    cutline_geometry.Transform(osr.CoordinateTransformation(spatial_ref(4326), image_srs))
    if not cutline_geometry.IsValid():
        cutline_geometry = cutline_geometry.MakeValid()
    paths = sorted(row[0] for row in raster_rows)
    config.output_dir.mkdir(parents=True, exist_ok=True)
    partial = output_path.with_name(f".{output_path.stem}.partial.{uuid.uuid4().hex}.tif")
    partial_ovr = Path(f"{partial}.ovr")
    output_ovr = Path(f"{output_path}.ovr")

    with tempfile.TemporaryDirectory(prefix=f"county_{task.code}_", dir=config.temp_root) as temp:
        temp_path = Path(temp)
        file_list = temp_path / "inputs.txt"
        vrt_path = temp_path / "mosaic.vrt"
        cutline_path = temp_path / "cutline.gpkg"
        file_list.write_text("".join(f"{path}\n" for path in paths), encoding="utf-8")
        geopackage_driver = ogr.GetDriverByName("GPKG")
        if geopackage_driver is None:
            raise RuntimeError("当前 GDAL 缺少 GPKG（GeoPackage）矢量驱动")
        cutline_dataset = geopackage_driver.CreateDataSource(str(cutline_path))
        if cutline_dataset is None:
            raise RuntimeError(f"无法创建临时裁剪线：{cutline_path}")
        cutline_layer = cutline_dataset.CreateLayer(
            "cutline",
            image_srs,
            cutline_geometry.GetGeometryType(),
        )
        if cutline_layer is None:
            raise RuntimeError("无法在临时 GeoPackage 中创建 cutline 图层")
        cutline_layer.CreateField(ogr.FieldDefn("code", ogr.OFTString))
        cutline_layer.CreateField(ogr.FieldDefn("name", ogr.OFTString))
        cutline_feature = ogr.Feature(cutline_layer.GetLayerDefn())
        cutline_feature.SetField("code", task.code)
        cutline_feature.SetField("name", task.name)
        cutline_feature.SetGeometry(cutline_geometry)
        cutline_layer.CreateFeature(cutline_feature)
        cutline_feature = None
        cutline_dataset = None
        env = os.environ.copy()
        cache_memory_mb = max(32, config.memory_per_job_mb // 4)
        warp_memory_mb = max(64, config.memory_per_job_mb - cache_memory_mb)
        # GDAL 对无单位大数存在兼容解析规则：>=10000 可能按字节解释。
        # 始终显式添加 MB，避免 24153 被误读为约 24 KB。
        env["GDAL_CACHEMAX"] = f"{cache_memory_mb}MB"
        env["GDAL_NUM_THREADS"] = str(config.threads_per_job)
        env["CPL_VSIL_CURL_ALLOWED_EXTENSIONS"] = ".tif,.tiff"
        build_command = [
            config.gdalbuildvrt,
            "-overwrite",
            "-resolution", "highest",
            "-input_file_list", str(file_list),
            str(vrt_path),
        ]
        LOG.info("[%s %s] 相关影像 %d 幅，正在建立临时 VRT", task.code, task.name, len(paths))
        run_checked(build_command, env, f"[{task.code}] gdalbuildvrt")
        warp_command = [
            config.gdalwarp,
            "-overwrite",
            "-multi",
            "-wo", f"NUM_THREADS={config.threads_per_job}",
            "-wm", f"{warp_memory_mb}MB",
            "-cutline", str(cutline_path),
            "-cl", "cutline",
            "-cutline_srs", image_srs.ExportToWkt(),
            "-crop_to_cutline",
            "-r", config.resampling,
            "-of", "GTiff",
        ]
        if config.pixel_size is not None:
            warp_command.extend(["-tr", str(config.pixel_size), str(config.pixel_size)])
        for option in config.creation_options:
            warp_command.extend(["-co", option])
        warp_command.extend([str(vrt_path), str(partial)])
        try:
            run_checked(warp_command, env, f"[{task.code}] gdalwarp")
            if not partial.is_file() or partial.stat().st_size == 0:
                raise RuntimeError("gdalwarp 未生成有效输出文件")
            build_external_and_internal_overviews(partial, config, env, task)
            os.replace(partial, output_path)
            os.replace(partial_ovr, output_ovr)
        finally:
            if partial.exists():
                partial.unlink()
            if partial_ovr.exists():
                partial_ovr.unlink()
    LOG.info("[%s %s] 完成：%s", task.code, task.name, output_path)
    return task.code, "success"


def county_worker_payload(task: CountyTask, config: RuntimeConfig) -> str:
    return json.dumps(
        {
            "task": {
                "code": task.code,
                "name": task.name,
                "geometry_wgs84_wkb": base64.b64encode(task.geometry_wgs84_wkb).decode("ascii"),
                "ordinal": task.ordinal,
                "total": task.total,
            },
            "config": {
                "index_path": str(config.index_path),
                "output_dir": str(config.output_dir),
                "temp_root": str(config.temp_root),
                "date1": config.date1,
                "date2": config.date2,
                "resolution": config.resolution,
                "name_template": config.name_template,
                "gdalbuildvrt": config.gdalbuildvrt,
                "gdalwarp": config.gdalwarp,
                "gdaladdo": config.gdaladdo,
                "overwrite": config.overwrite,
                "pixel_size": config.pixel_size,
                "resampling": config.resampling,
                "creation_options": list(config.creation_options),
                "threads_per_job": config.threads_per_job,
                "memory_per_job_mb": config.memory_per_job_mb,
                "overview_max_factor": config.overview_max_factor,
            },
        },
        ensure_ascii=False,
    )


def run_county_subprocess(task: CountyTask, config: RuntimeConfig) -> tuple[str, str]:
    """在线程调度器中启动一个隔离的 Python 县任务并发数。"""
    emit_event(
        "county_started",
        code=task.code,
        name=task.name,
        ordinal=task.ordinal,
        total=task.total,
    )
    command = [sys.executable, str(Path(__file__).resolve()), "--county-worker"]
    process = subprocess.Popen(
        command,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
    )
    assert process.stdin is not None
    assert process.stdout is not None
    process.stdin.write(county_worker_payload(task, config))
    process.stdin.close()
    result_line: str | None = None
    output_tail: list[str] = []
    for raw_line in process.stdout:
        line = raw_line.rstrip("\r\n")
        if line.startswith(_WORKER_RESULT_PREFIX):
            result_line = line
        elif line:
            LOG.info("[%s 子并发数] %s", task.code, line)
        output_tail.append(line)
        if len(output_tail) > 200:
            del output_tail[:100]
    return_code = process.wait()
    if return_code or result_line is None:
        detail = "\n".join(output_tail)[-12000:].strip()
        raise RuntimeError(
            f"[{task.code} {task.name}] Python 子并发数失败（退出码 {return_code}）：\n{detail}"
        )
    result = json.loads(result_line[len(_WORKER_RESULT_PREFIX):])
    return str(result["code"]), str(result["status"])


def county_worker_main() -> int:
    """隐藏的子并发数入口；任务通过 stdin 传入，结果以单行 JSON 返回。"""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(message)s",
        stream=sys.stderr,
    )
    try:
        payload = json.load(sys.stdin)
        task_data = payload["task"]
        config_data = payload["config"]
        task = CountyTask(
            code=task_data["code"],
            name=task_data["name"],
            geometry_wgs84_wkb=base64.b64decode(task_data["geometry_wgs84_wkb"]),
            ordinal=int(task_data["ordinal"]),
            total=int(task_data["total"]),
        )
        config = RuntimeConfig(
            index_path=Path(config_data["index_path"]),
            output_dir=Path(config_data["output_dir"]),
            temp_root=Path(config_data["temp_root"]),
            date1=config_data["date1"],
            date2=config_data["date2"],
            resolution=config_data["resolution"],
            name_template=config_data["name_template"],
            gdalbuildvrt=config_data["gdalbuildvrt"],
            gdalwarp=config_data["gdalwarp"],
            gdaladdo=config_data["gdaladdo"],
            overwrite=bool(config_data["overwrite"]),
            pixel_size=config_data["pixel_size"],
            resampling=config_data["resampling"],
            creation_options=tuple(config_data["creation_options"]),
            threads_per_job=int(config_data["threads_per_job"]),
            memory_per_job_mb=int(config_data["memory_per_job_mb"]),
            overview_max_factor=int(config_data["overview_max_factor"]),
        )
        code, status = process_county(task, config)
        print(
            _WORKER_RESULT_PREFIX
            + json.dumps({"code": code, "status": status}, ensure_ascii=False),
            flush=True,
        )
        return 0
    except StopRequested as exc:
        LOG.warning("县任务已停止：%s", exc)
        return 130
    except Exception as exc:
        LOG.exception("县任务子并发数失败：%s", exc)
        return 1


def calculate_resources(args: argparse.Namespace) -> tuple[int, int, int, int]:
    cpu_slots = int(getattr(
        args,
        "cpu_slots",
        max(1, math.floor((os.cpu_count() or 1) * args.cpu_percent / 100.0)),
    ))
    memory_budget_mb = max(128, math.floor(args.gdal_memory_gb * 1024))
    memory_workers = max(1, memory_budget_mb // 128)
    effective_workers = max(1, min(args.workers, cpu_slots, memory_workers))
    threads_per_job = max(1, cpu_slots // effective_workers)
    memory_per_job_mb = max(64, memory_budget_mb // effective_workers)
    return effective_workers, threads_per_job, memory_per_job_mb, memory_budget_mb


def main(argv: Sequence[str] | None = None) -> int:
    global _events_enabled
    args = parse_args(argv)
    _events_enabled = args.emit_progress_events
    try:
        validate_args(args)
        logical_cpus, args.cpu_slots = limit_cpu_affinity(args.cpu_percent)
        args.output_dir = args.output_dir.resolve()
        args.output_dir.mkdir(parents=True, exist_ok=True)
        log_path = (args.log_file or (args.output_dir / "clip_counties.log")).resolve()
        setup_logging(log_path)
        boundary = resolve_boundary(args.boundary.resolve())
        index_path = args.index.resolve()
        temp_root = (args.temp_dir.resolve() if args.temp_dir else Path(tempfile.gettempdir()))
        temp_root.mkdir(parents=True, exist_ok=True)
        gdalbuildvrt = resolve_gdal_command("gdalbuildvrt", args.gdal_bin)
        gdalwarp = resolve_gdal_command("gdalwarp", args.gdal_bin)
        gdaladdo = resolve_gdal_command("gdaladdo", args.gdal_bin)
        selected_codes = {six_digit_code(code) for code in args.county} if args.county else None
        counties = load_counties(
            boundary, args.code_field, args.name_field, selected_codes,
        )
        output_names: dict[str, str] = {}
        for task in counties:
            output_name = safe_output_name(args.name_template, task, args)
            normalized_name = os.path.normcase(output_name)
            previous_code = output_names.get(normalized_name)
            if previous_code is not None:
                raise ValueError(
                    f"输出文件名冲突：县 {previous_code} 与 {task.code} 都会写入 {output_name}"
                )
            output_names[normalized_name] = task.code
        effective_workers, threads_per_job, memory_per_job_mb, memory_budget_mb = (
            calculate_resources(args)
        )
        cpu_slots = args.cpu_slots
        effective_index_workers = min(args.index_workers, cpu_slots)
        LOG.info(
            "资源计划：逻辑 CPU=%d，CPU 最高可调用=%.1f%%（亲和核心=%d），"
            "请求并发=%d，有效并发=%d，"
            "每任务线程=%d，索引并发数=%d；GDAL 总内存 %.2f GB（每任务 %d MB）",
            logical_cpus, args.cpu_percent, cpu_slots, args.workers, effective_workers,
            threads_per_job, effective_index_workers, args.gdal_memory_gb,
            memory_per_job_mb,
        )
        emit_event(
            "job_plan",
            total=len(counties),
            workers=effective_workers,
            threads_per_job=threads_per_job,
            memory_per_job_mb=memory_per_job_mb,
            counties=[
                {
                    "code": task.code,
                    "name": task.name,
                    "ordinal": task.ordinal,
                }
                for task in counties
            ],
        )
        emit_event("stage", name="index", message="正在检查或建立影像空间索引")
        update_index(index_path, args.imagery_dir, args.index_mode, effective_index_workers)
        emit_event("stage", name="clipping", message="空间索引就绪，开始按县裁剪")
        creation_options = ["TILED=YES", "COMPRESS=DEFLATE", "BIGTIFF=YES"]
        creation_options.extend(args.creation_option)
        config = RuntimeConfig(
            index_path=index_path,
            output_dir=args.output_dir,
            temp_root=temp_root,
            date1=args.date1,
            date2=args.date2,
            resolution=args.resolution,
            name_template=args.name_template,
            gdalbuildvrt=gdalbuildvrt,
            gdalwarp=gdalwarp,
            gdaladdo=gdaladdo,
            overwrite=args.overwrite,
            pixel_size=args.pixel_size,
            resampling=args.resampling,
            creation_options=tuple(creation_options),
            threads_per_job=threads_per_job,
            memory_per_job_mb=memory_per_job_mb,
            overview_max_factor=args.overview_max_factor,
        )
        counts = {"success": 0, "skipped": 0, "no_image": 0, "failed": 0}
        # 线程只负责调度和收集；GDAL/OGR 县任务在独立 Python 子并发数中执行。
        with ThreadPoolExecutor(max_workers=effective_workers, thread_name_prefix="county") as pool:
            futures: dict[Future[tuple[str, str]], CountyTask] = {
                pool.submit(run_county_subprocess, task, config): task for task in counties
            }
            completed_count = 0
            for future in as_completed(futures):
                task = futures[future]
                try:
                    _, status = future.result()
                    counts[status] += 1
                    error_message = ""
                except Exception as exc:
                    counts["failed"] += 1
                    status = "failed"
                    error_message = str(exc)
                    LOG.exception("[%s %s] 处理失败", task.code, task.name)
                completed_count += 1
                emit_event(
                    "county_finished",
                    code=task.code,
                    name=task.name,
                    ordinal=task.ordinal,
                    total=len(counties),
                    completed=completed_count,
                    percent=round(completed_count * 100.0 / max(1, len(counties)), 2),
                    status=status,
                    error=error_message,
                )
        LOG.info(
            "全部结束：成功 %d，跳过 %d，无影像 %d，失败 %d",
            counts["success"], counts["skipped"], counts["no_image"], counts["failed"],
        )
        emit_event("job_finished", counts=counts, exit_code=1 if counts["failed"] else 0)
        return 1 if counts["failed"] else 0
    except StopRequested as exc:
        if LOG.handlers:
            LOG.warning("任务已按请求停止：%s", exc)
        else:
            print(f"任务已停止：{exc}", file=sys.stderr)
        emit_event("job_cancelled", message=str(exc))
        return 130
    except Exception as exc:
        if LOG.handlers:
            LOG.exception("程序终止：%s", exc)
        else:
            print(f"错误：{exc}", file=sys.stderr)
        emit_event("job_error", message=str(exc))
        return 2


if __name__ == "__main__":
    install_stop_signal_handlers()
    if len(sys.argv) == 2 and sys.argv[1] == "--county-worker":
        raise SystemExit(county_worker_main())
    raise SystemExit(main())
