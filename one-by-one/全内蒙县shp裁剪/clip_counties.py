#!/usr/bin/env python
"""递归索引 Shapefile，并按县界裁剪、合并为每县一个 Shapefile。"""

from __future__ import annotations

import argparse
import base64
import errno
import json
import logging
import math
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator, Sequence

from osgeo import gdal, ogr, osr


LOG = logging.getLogger("clip_county_shapefiles")
SAFE_NAME_RE = re.compile(r"^[^\\/:*?\"<>|]+$")
SHAPEFILE_PARTS = {
    ".shp", ".shx", ".dbf", ".prj", ".cpg", ".qix", ".sbn", ".sbx",
}
INDEX_VERSION = "2"
WORKER_RESULT_PREFIX = "@@COUNTY_WORKER_RESULT@@"
_thread_state = threading.local()
_event_lock = threading.Lock()
_events_enabled = False
_created_temp_dirs: set[Path] = set()
_created_temp_dirs_lock = threading.Lock()

gdal.UseExceptions()
ogr.UseExceptions()


@dataclass(frozen=True)
class VectorRecord:
    path: str
    signature: str
    crs_wkt: str
    geometry_type: int
    feature_count: int
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
    name_template: str
    overwrite: bool
    excluded_source_paths: tuple[str, ...] = ()


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="递归索引输入 SHP，并按县界裁剪、合并成每县一个完整 Shapefile。",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--shp-dir", required=True, type=Path, help="输入 SHP 根目录，递归扫描")
    parser.add_argument("--boundary", type=Path, default=Path("00县边界"), help="县界文件或文件夹")
    parser.add_argument("--output-dir", required=True, type=Path, help="县级 Shapefile 输出目录")
    parser.add_argument(
        "--name-template",
        default="{code}_{name}.shp",
        help="输出文件名模板，可用字段：code、name",
    )
    parser.add_argument("--code-field", help="县代码字段；不指定时自动识别")
    parser.add_argument("--name-field", help="县名字段；不指定时自动识别")
    parser.add_argument("--county", action="append", help="只处理指定六位县代码，可重复使用")
    parser.add_argument(
        "--index", type=Path, default=Path("shapefile_index.sqlite"), help="SQLite 空间索引",
    )
    parser.add_argument(
        "--index-mode",
        choices=("auto", "skip", "rebuild"),
        default="auto",
        help="auto 增量更新；skip 跳过扫描；rebuild 完整重建",
    )
    parser.add_argument("--index-workers", type=int, default=4, help="并行读取 SHP 元数据的线程数")
    parser.add_argument("--workers", type=int, default=4, help="同时处理的最大县数")
    parser.add_argument("--cpu-percent", type=float, default=75.0, help="用于限制有效并发的 CPU 比例")
    parser.add_argument("--overwrite", action="store_true", help="覆盖已有县级 Shapefile 文件组")
    parser.add_argument("--temp-dir", type=Path, help="临时目录；默认在脚本所在根目录内创建")
    parser.add_argument("--log-file", type=Path, help="日志文件；默认写入输出目录")
    parser.add_argument("--emit-progress-events", action="store_true", help=argparse.SUPPRESS)
    return parser.parse_args(argv)


def emit_event(event: str, **payload: object) -> None:
    if not _events_enabled:
        return
    message = json.dumps({"event": event, **payload}, ensure_ascii=False, separators=(",", ":"))
    with _event_lock:
        print(f"@@CLIP_EVENT@@{message}", flush=True)


def validate_args(args: argparse.Namespace) -> None:
    if args.workers < 1 or args.index_workers < 1:
        raise ValueError("--workers 和 --index-workers 必须大于等于 1")
    if not 0 < args.cpu_percent <= 100:
        raise ValueError("--cpu-percent 必须在 (0, 100] 范围内")
    try:
        example = args.name_template.format(code="150102", name="新城区")
    except (KeyError, ValueError) as exc:
        raise ValueError(f"--name-template 无效：{exc}") from exc
    if Path(example).name != example or not example.lower().endswith(".shp"):
        raise ValueError("--name-template 必须生成单个 .shp 文件名")
    if not SAFE_NAME_RE.fullmatch(example):
        raise ValueError("--name-template 生成了含非法字符的文件名")


def setup_logging(log_path: Path) -> None:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    formatter = logging.Formatter("%(asctime)s | %(levelname)s | %(message)s")
    stream = logging.StreamHandler()
    stream.setFormatter(formatter)
    file_handler = logging.FileHandler(log_path, encoding="utf-8")
    file_handler.setFormatter(formatter)
    LOG.setLevel(logging.INFO)
    LOG.handlers[:] = [stream, file_handler]


def spatial_ref(value: str | int) -> osr.SpatialReference:
    reference = osr.SpatialReference()
    if isinstance(value, int):
        reference.ImportFromEPSG(value)
    else:
        reference.SetFromUserInput(value)
    reference.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
    return reference


def resolve_boundary(path: Path) -> Path:
    if path.is_file():
        return path
    if not path.is_dir():
        raise FileNotFoundError(f"县界路径不存在：{path}")
    candidates = sorted(
        item for item in path.iterdir()
        if item.suffix.lower() in {".shp", ".gpkg", ".geojson", ".json"}
    )
    if len(candidates) != 1:
        raise ValueError(f"县界文件夹中应有且仅有一个矢量文件，实际找到 {len(candidates)} 个")
    return candidates[0]


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
        matched = sum(bool(re.search(r"(?<!\d)\d{6}", value)) for value in values)
        if values and matched / len(values) > 0.9:
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
    transform = osr.CoordinateTransformation(source_srs, spatial_ref(4326))
    definition = layer.GetLayerDefn()
    columns = [
        definition.GetFieldDefn(index).GetName()
        for index in range(definition.GetFieldCount())
    ]
    samples = {field: [] for field in columns}
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
        name = str(feature.GetField(name_field)).strip() if name_field else code
        county = geometry.Clone()
        county.Transform(transform)
        if not county.IsValid():
            county = county.MakeValid()
        if code in grouped:
            previous_name, previous_geometry = grouped[code]
            grouped[code] = (previous_name, previous_geometry.Union(county))
        else:
            grouped[code] = (name, county)
    if selected_codes:
        missing = selected_codes - set(grouped)
        if missing:
            raise ValueError(f"县界中找不到指定代码：{sorted(missing)}")
    total = len(grouped)
    tasks = [
        CountyTask(code, name, bytes(geometry.ExportToWkb()), ordinal, total)
        for ordinal, (code, (name, geometry)) in enumerate(sorted(grouped.items()), start=1)
    ]
    LOG.info("县界：%s；待处理县数：%d", boundary, len(tasks))
    return tasks


def iter_shapefiles(root: Path) -> Iterator[Path]:
    if not root.is_dir():
        raise FileNotFoundError(f"输入 SHP 目录不存在或不可访问：{root}")
    stack = [root]
    while stack:
        current = stack.pop()
        try:
            with os.scandir(current) as entries:
                for entry in entries:
                    try:
                        if entry.is_dir(follow_symlinks=False):
                            stack.append(Path(entry.path))
                        elif (
                            entry.is_file(follow_symlinks=False)
                            and Path(entry.name).suffix.lower() == ".shp"
                        ):
                            yield Path(entry.path)
                    except OSError as exc:
                        LOG.warning("跳过无法访问的目录项 %s：%s", entry.path, exc)
        except OSError as exc:
            LOG.warning("跳过无法访问的目录 %s：%s", current, exc)


def shapefile_signature(path: Path) -> str:
    parts: list[str] = []
    for item in sorted(
        (
            candidate for candidate in path.parent.iterdir()
            if (
                candidate.is_file()
                and candidate.stem.casefold() == path.stem.casefold()
                and candidate.suffix.lower() in SHAPEFILE_PARTS
            )
        ),
        key=lambda candidate: candidate.suffix.lower(),
    ):
        try:
            stat = item.stat()
        except FileNotFoundError:
            continue
        parts.append(f"{item.suffix.lower()}:{stat.st_size}:{stat.st_mtime_ns}")
    return "|".join(parts)


def envelope_polygon(
    extent: tuple[float, float, float, float],
    segments_per_edge: int = 64,
) -> ogr.Geometry:
    """建立加密的范围多边形，避免投影转换后的弯曲边界被四角法截短。"""
    minx, maxx, miny, maxy = extent
    ring = ogr.Geometry(ogr.wkbLinearRing)
    for index in range(segments_per_edge + 1):
        ratio = index / segments_per_edge
        ring.AddPoint_2D(minx + (maxx - minx) * ratio, miny)
    for index in range(1, segments_per_edge + 1):
        ratio = index / segments_per_edge
        ring.AddPoint_2D(maxx, miny + (maxy - miny) * ratio)
    for index in range(1, segments_per_edge + 1):
        ratio = index / segments_per_edge
        ring.AddPoint_2D(maxx - (maxx - minx) * ratio, maxy)
    for index in range(1, segments_per_edge + 1):
        ratio = index / segments_per_edge
        ring.AddPoint_2D(minx, maxy - (maxy - miny) * ratio)
    ring.CloseRings()
    polygon = ogr.Geometry(ogr.wkbPolygon)
    polygon.AddGeometry(ring)
    return polygon


def inspect_shapefile(path: Path, signature: str | None = None) -> VectorRecord:
    signature = signature or shapefile_signature(path)
    dataset = gdal.OpenEx(str(path), gdal.OF_VECTOR | gdal.OF_READONLY)
    if dataset is None:
        raise ValueError("GDAL 无法打开")
    layer = dataset.GetLayer(0)
    source_srs = layer.GetSpatialRef()
    if source_srs is None:
        raise ValueError("缺少坐标系（.prj）")
    source_srs = source_srs.Clone()
    source_srs.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
    extent = layer.GetExtent(force=1)
    if extent is None:
        raise ValueError("无法读取空间范围")
    footprint = envelope_polygon(extent)
    footprint.Transform(osr.CoordinateTransformation(source_srs, spatial_ref(4326)))
    if not footprint.IsValid():
        footprint = footprint.MakeValid()
    minx, maxx, miny, maxy = footprint.GetEnvelope()
    record = VectorRecord(
        path=str(path.resolve()),
        signature=signature,
        crs_wkt=source_srs.ExportToWkt(),
        geometry_type=layer.GetGeomType(),
        feature_count=layer.GetFeatureCount(force=1),
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
        CREATE TABLE IF NOT EXISTS vectors (
            id INTEGER PRIMARY KEY,
            path TEXT NOT NULL UNIQUE,
            signature TEXT NOT NULL,
            crs_wkt TEXT NOT NULL,
            geometry_type INTEGER NOT NULL,
            feature_count INTEGER NOT NULL,
            footprint_wkb BLOB NOT NULL
        );
        CREATE VIRTUAL TABLE IF NOT EXISTS vector_rtree USING rtree(
            id, minx, maxx, miny, maxy
        );
        CREATE TABLE IF NOT EXISTS metadata (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );
        """
    )
    return connection


def upsert_record(connection: sqlite3.Connection, record: VectorRecord) -> None:
    old = connection.execute("SELECT id FROM vectors WHERE path=?", (record.path,)).fetchone()
    values = (
        record.signature, record.crs_wkt, record.geometry_type,
        record.feature_count, record.footprint_wkb,
    )
    if old:
        vector_id = int(old[0])
        connection.execute(
            """UPDATE vectors SET signature=?,crs_wkt=?,geometry_type=?,
               feature_count=?,footprint_wkb=? WHERE id=?""",
            (*values, vector_id),
        )
        connection.execute("DELETE FROM vector_rtree WHERE id=?", (vector_id,))
    else:
        cursor = connection.execute(
            """INSERT INTO vectors(path,signature,crs_wkt,geometry_type,
               feature_count,footprint_wkb) VALUES(?,?,?,?,?,?)""",
            (record.path, *values),
        )
        vector_id = int(cursor.lastrowid)
    connection.execute(
        "INSERT INTO vector_rtree(id,minx,maxx,miny,maxy) VALUES(?,?,?,?,?)",
        (vector_id, record.minx, record.maxx, record.miny, record.maxy),
    )


def update_index(index_path: Path, shp_dir: Path, mode: str, workers: int) -> None:
    if mode == "skip":
        if not index_path.is_file():
            raise FileNotFoundError(f"--index-mode skip 需要已有索引：{index_path}")
        with connect_index(index_path) as connection:
            version_row = connection.execute(
                "SELECT value FROM metadata WHERE key='index_version'"
            ).fetchone()
            if version_row is None or version_row[0] != INDEX_VERSION:
                raise RuntimeError(
                    "已有索引版本过旧，不能使用 skip；请改用 auto 自动重建索引"
                )
            count = connection.execute("SELECT COUNT(*) FROM vectors").fetchone()[0]
        LOG.info("跳过目录扫描，直接使用索引（%d 个 SHP）：%s", count, index_path)
        return

    with connect_index(index_path) as connection:
        version_row = connection.execute(
            "SELECT value FROM metadata WHERE key='index_version'"
        ).fetchone()
        version_changed = version_row is None or version_row[0] != INDEX_VERSION
        if mode == "rebuild" or version_changed:
            if version_changed:
                LOG.info("检测到旧版空间索引，正在自动完整重建以修正投影范围")
            connection.execute("DELETE FROM vector_rtree")
            connection.execute("DELETE FROM vectors")
            connection.commit()
        existing = {
            row[0]: row[1]
            for row in connection.execute("SELECT path,signature FROM vectors")
        }
        seen: set[str] = set()
        changed: list[tuple[Path, str]] = []
        for path in iter_shapefiles(shp_dir):
            normalized = str(path.resolve())
            seen.add(normalized)
            try:
                signature = shapefile_signature(path)
                if existing.get(normalized) != signature:
                    changed.append((path, signature))
            except OSError as exc:
                LOG.warning("无法读取文件组状态 %s：%s", path, exc)
        deleted = set(existing) - seen
        LOG.info(
            "SHP 扫描完成：发现 %d 个，新增/变化 %d 个，删除 %d 个",
            len(seen), len(changed), len(deleted),
        )
        failures: list[str] = []
        if changed:
            with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="shp-index") as pool:
                futures = {
                    pool.submit(inspect_shapefile, path, signature): path
                    for path, signature in changed
                }
                for completed, future in enumerate(as_completed(futures), start=1):
                    path = futures[future]
                    try:
                        upsert_record(connection, future.result())
                    except Exception as exc:
                        failures.append(str(path))
                        LOG.error("SHP 索引失败 %s：%s", path, exc)
                    if completed % 50 == 0:
                        connection.commit()
                        LOG.info("索引元数据进度：%d/%d", completed, len(changed))
        for path in deleted:
            row = connection.execute("SELECT id FROM vectors WHERE path=?", (path,)).fetchone()
            if row:
                connection.execute("DELETE FROM vector_rtree WHERE id=?", (row[0],))
                connection.execute("DELETE FROM vectors WHERE id=?", (row[0],))
        connection.execute(
            "INSERT OR REPLACE INTO metadata(key,value) VALUES('shp_dir',?)",
            (str(shp_dir.resolve()),),
        )
        connection.execute(
            "INSERT OR REPLACE INTO metadata(key,value) VALUES('updated_at',?)",
            (time.strftime("%Y-%m-%d %H:%M:%S"),),
        )
        connection.execute(
            "INSERT OR REPLACE INTO metadata(key,value) VALUES('index_version',?)",
            (INDEX_VERSION,),
        )
        connection.commit()
        count = connection.execute("SELECT COUNT(*) FROM vectors").fetchone()[0]
        LOG.info("空间索引已保存：%s（有效 SHP %d 个）", index_path, count)
        if failures:
            raise RuntimeError(f"{len(failures)} 个 SHP 无法建立索引；详见日志")


def query_vectors(index_path: Path, county: ogr.Geometry) -> list[tuple[str, str, int]]:
    connection: sqlite3.Connection | None = getattr(_thread_state, "index_connection", None)
    if connection is None:
        index_uri = f"{index_path.resolve().as_uri()}?mode=ro"
        connection = sqlite3.connect(index_uri, timeout=60, uri=True)
        _thread_state.index_connection = connection
    minx, maxx, miny, maxy = county.GetEnvelope()
    rows = connection.execute(
        """
        SELECT v.path,v.crs_wkt,v.geometry_type
        FROM vector_rtree x JOIN vectors v ON v.id=x.id
        WHERE x.minx<=? AND x.maxx>=? AND x.miny<=? AND x.maxy>=?
        """,
        (maxx, minx, maxy, miny),
    )
    selected: list[tuple[str, str, int]] = []
    # RTree 只负责生成“宁可多选、不能漏选”的候选集。逐要素真实相交
    # 会在裁剪阶段完成，不在这里用近似范围多边形做第二次排除。
    for path, crs_wkt, geometry_type in rows:
        selected.append((path, crs_wkt, int(geometry_type)))
    return sorted(selected)


def geometry_family(geometry_type: int) -> str:
    flattened = ogr.GT_Flatten(geometry_type)
    if flattened in {ogr.wkbPoint, ogr.wkbMultiPoint}:
        return "point"
    if flattened in {ogr.wkbLineString, ogr.wkbMultiLineString}:
        return "line"
    if flattened in {ogr.wkbPolygon, ogr.wkbMultiPolygon}:
        return "polygon"
    raise ValueError(f"不支持的几何类型：{ogr.GeometryTypeToName(geometry_type)}")


def output_geometry_type(family: str) -> int:
    return {
        "point": ogr.wkbMultiPoint,
        "line": ogr.wkbMultiLineString,
        "polygon": ogr.wkbMultiPolygon,
    }[family]


def collect_family_parts(geometry: ogr.Geometry, family: str) -> list[ogr.Geometry]:
    if geometry is None or geometry.IsEmpty():
        return []
    try:
        current_family = geometry_family(geometry.GetGeometryType())
    except ValueError:
        current_family = ""
    if current_family == family:
        flattened = ogr.GT_Flatten(geometry.GetGeometryType())
        single_type = {
            "point": ogr.wkbPoint,
            "line": ogr.wkbLineString,
            "polygon": ogr.wkbPolygon,
        }[family]
        if flattened == single_type:
            return [geometry.Clone()]
        return [
            geometry.GetGeometryRef(index).Clone()
            for index in range(geometry.GetGeometryCount())
        ]
    parts: list[ogr.Geometry] = []
    for index in range(geometry.GetGeometryCount()):
        parts.extend(collect_family_parts(geometry.GetGeometryRef(index), family))
    return parts


def force_multi(geometry: ogr.Geometry, family: str) -> ogr.Geometry | None:
    parts = collect_family_parts(geometry, family)
    if not parts:
        return None
    multi = ogr.Geometry(output_geometry_type(family))
    for part in parts:
        multi.AddGeometry(part)
    return multi


def safe_output_name(template: str, task: CountyTask) -> str:
    name = template.format(code=task.code, name=task.name)
    if Path(name).name != name or not name.lower().endswith(".shp"):
        raise ValueError(f"输出模板必须生成单个 SHP 文件名，当前为：{name!r}")
    if not SAFE_NAME_RE.fullmatch(name):
        raise ValueError(f"输出文件名含非法字符：{name!r}")
    return name


def create_union_fields(
    output_layer: ogr.Layer,
    sources: list[tuple[str, str, int]],
) -> dict[str, str]:
    """建立输入字段并集，返回小写源字段名到实际输出字段名的映射。"""
    mapping: dict[str, str] = {}
    for path, _, _ in sources:
        dataset = gdal.OpenEx(path, gdal.OF_VECTOR | gdal.OF_READONLY)
        if dataset is None:
            raise RuntimeError(f"无法打开输入 SHP：{path}")
        definition = dataset.GetLayer(0).GetLayerDefn()
        for index in range(definition.GetFieldCount()):
            source_field = definition.GetFieldDefn(index)
            key = source_field.GetName().casefold()
            if key in mapping:
                continue
            field_copy = ogr.FieldDefn(source_field.GetName(), source_field.GetType())
            field_copy.SetWidth(source_field.GetWidth())
            field_copy.SetPrecision(source_field.GetPrecision())
            if output_layer.CreateField(field_copy) != ogr.OGRERR_NONE:
                raise RuntimeError(f"无法创建输出字段：{source_field.GetName()}")
            actual_definition = output_layer.GetLayerDefn()
            actual_name = actual_definition.GetFieldDefn(
                actual_definition.GetFieldCount() - 1
            ).GetName()
            mapping[key] = actual_name
        dataset = None
    return mapping


def remove_output_family(shp_path: Path) -> None:
    for item in shp_path.parent.iterdir():
        if (
            item.is_file()
            and item.stem.casefold() == shp_path.stem.casefold()
            and item.suffix.lower() in SHAPEFILE_PARTS
        ):
            item.unlink()


def publish_shapefile(temp_shp: Path, final_shp: Path) -> None:
    parts = [
        item for item in temp_shp.parent.glob(f"{temp_shp.stem}.*")
        if item.is_file() and item.suffix.lower() in SHAPEFILE_PARTS
    ]
    required = {".shp", ".shx", ".dbf", ".prj", ".cpg"}
    present = {item.suffix.lower() for item in parts}
    if not required.issubset(present):
        raise RuntimeError(f"临时输出缺少 Shapefile 必需文件：{sorted(required - present)}")
    remove_output_family(final_shp)
    # 主 .shp 最后发布；默认断点续跑以它是否存在作为完成标记。
    parts.sort(key=lambda item: item.suffix.lower() == ".shp")
    for item in parts:
        target = final_shp.with_suffix(item.suffix.lower())
        try:
            os.replace(item, target)
        except OSError as exc:
            if exc.errno != errno.EXDEV:
                raise
            # 脚本根目录与输出目录可能位于不同挂载点。
            # 先复制到目标目录的隐藏文件，再在同一文件系统内发布。
            staged = target.with_name(f".{target.name}.{uuid.uuid4().hex}.tmp")
            try:
                # 部分 NAS/CIFS 挂载允许写入文件内容，却禁止通过 utime
                # 保留源文件时间戳。copy2() 会调用 copystat()，从而导致
                # PermissionError；这里只需复制内容，改用 copyfile()。
                shutil.copyfile(item, staged)
                os.replace(staged, target)
                item.unlink()
            finally:
                staged.unlink(missing_ok=True)


def remember_temp_dir(path: Path) -> None:
    """记录本次进程创建的临时目录，供程序退出时做兜底清理。"""
    with _created_temp_dirs_lock:
        _created_temp_dirs.add(path)


def cleanup_created_temp_dirs() -> None:
    """只清理本进程创建的 county_shp 临时目录，不触碰正式成果。"""
    with _created_temp_dirs_lock:
        paths = tuple(_created_temp_dirs)
        _created_temp_dirs.clear()
    for path in paths:
        if not path.name.startswith("county_shp_"):
            continue
        last_error: OSError | None = None
        for attempt in range(5):
            try:
                shutil.rmtree(path)
                LOG.info("已清理临时目录：%s", path)
                last_error = None
                break
            except FileNotFoundError:
                last_error = None
                break
            except OSError as exc:
                last_error = exc
                if attempt < 4:
                    time.sleep(0.2)
        if last_error is not None:
            LOG.warning("临时目录清理失败：%s（%s）", path, last_error)


def process_county(task: CountyTask, config: RuntimeConfig) -> tuple[str, str]:
    emit_event(
        "county_started", code=task.code, name=task.name,
        ordinal=task.ordinal, total=task.total,
    )
    output_path = config.output_dir / safe_output_name(config.name_template, task)
    if output_path.is_file() and output_path.stat().st_size > 0 and not config.overwrite:
        LOG.info("[%s %s] 跳过已有结果：%s", task.code, task.name, output_path)
        return task.code, "skipped"

    county_wgs84 = ogr.CreateGeometryFromWkb(task.geometry_wgs84_wkb)
    sources = query_vectors(config.index_path, county_wgs84)
    if config.excluded_source_paths:
        excluded = {os.path.normcase(path) for path in config.excluded_source_paths}
        sources = [
            source for source in sources
            if os.path.normcase(str(Path(source[0]).resolve())) not in excluded
        ]
    if not sources:
        LOG.warning("[%s %s] 无相交 SHP", task.code, task.name)
        return task.code, "no_data"
    families = {geometry_family(row[2]) for row in sources}
    if len(families) != 1:
        raise RuntimeError(
            f"[{task.code} {task.name}] 相交输入混有多种几何类别：{sorted(families)}；"
            "点、线、面数据请分别运行"
        )
    family = next(iter(families))
    output_srs = spatial_ref(sources[0][1])
    config.output_dir.mkdir(parents=True, exist_ok=True)
    temp_parent = config.temp_root if config.temp_root else config.output_dir

    with tempfile.TemporaryDirectory(
        prefix=f"county_shp_{task.code}_", dir=temp_parent,
    ) as temp:
        temp_dir = Path(temp)
        remember_temp_dir(temp_dir)
        temp_shp = temp_dir / f"result_{uuid.uuid4().hex}.shp"
        driver = ogr.GetDriverByName("ESRI Shapefile")
        if driver is None:
            raise RuntimeError("当前 GDAL 缺少 ESRI Shapefile 驱动")
        output_dataset = driver.CreateDataSource(str(temp_shp))
        if output_dataset is None:
            raise RuntimeError(f"无法创建临时 Shapefile：{temp_shp}")
        output_layer = output_dataset.CreateLayer(
            temp_shp.stem,
            output_srs,
            output_geometry_type(family),
            options=["ENCODING=UTF-8", "RESIZE=YES"],
        )
        if output_layer is None:
            raise RuntimeError("无法创建输出图层")
        field_mapping = create_union_fields(output_layer, sources)
        written = 0

        for path, crs_wkt, _ in sources:
            source_dataset = gdal.OpenEx(path, gdal.OF_VECTOR | gdal.OF_READONLY)
            if source_dataset is None:
                raise RuntimeError(f"无法打开输入 SHP：{path}")
            source_layer = source_dataset.GetLayer(0)
            source_srs = spatial_ref(crs_wkt)
            county_source = county_wgs84.Clone()
            county_source.Transform(
                osr.CoordinateTransformation(spatial_ref(4326), source_srs)
            )
            if not county_source.IsValid():
                county_source = county_source.MakeValid()
            source_layer.SetSpatialFilter(county_source)
            to_output = (
                None if source_srs.IsSame(output_srs)
                else osr.CoordinateTransformation(source_srs, output_srs)
            )
            source_definition = source_layer.GetLayerDefn()
            for source_feature in source_layer:
                geometry = source_feature.GetGeometryRef()
                if geometry is None or geometry.IsEmpty():
                    continue
                working_geometry = geometry.Clone()
                if not working_geometry.IsValid():
                    working_geometry = working_geometry.MakeValid()
                if (
                    working_geometry is None
                    or working_geometry.IsEmpty()
                    or not working_geometry.Intersects(county_source)
                ):
                    continue
                clipped = working_geometry.Intersection(county_source)
                if clipped is None or clipped.IsEmpty():
                    continue
                if to_output is not None:
                    clipped.Transform(to_output)
                clipped_multi = force_multi(clipped, family)
                if clipped_multi is None or clipped_multi.IsEmpty():
                    continue
                output_feature = ogr.Feature(output_layer.GetLayerDefn())
                for index in range(source_definition.GetFieldCount()):
                    if not source_feature.IsFieldSetAndNotNull(index):
                        continue
                    source_name = source_definition.GetFieldDefn(index).GetName()
                    target_name = field_mapping.get(source_name.casefold())
                    if target_name:
                        output_feature.SetField(target_name, source_feature.GetField(index))
                output_feature.SetGeometry(clipped_multi)
                if output_layer.CreateFeature(output_feature) != ogr.OGRERR_NONE:
                    raise RuntimeError(f"写入裁剪要素失败：{path}")
                written += 1
                output_feature = None
            source_layer.SetSpatialFilter(None)
            source_dataset = None

        output_layer.SyncToDisk()
        output_layer = None
        output_dataset = None
        if written == 0:
            LOG.warning("[%s %s] 候选 SHP 中没有实际相交要素", task.code, task.name)
            return task.code, "no_data"
        if not temp_shp.with_suffix(".cpg").exists():
            temp_shp.with_suffix(".cpg").write_text("UTF-8\n", encoding="ascii")
        publish_shapefile(temp_shp, output_path)
    LOG.info(
        "[%s %s] 完成：%s（%d 个要素，候选 SHP %d 个）",
        task.code, task.name, output_path, written, len(sources),
    )
    return task.code, "success"


def county_task_payload(task: CountyTask, config: RuntimeConfig) -> dict[str, object]:
    return {
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
            "name_template": config.name_template,
            "overwrite": config.overwrite,
            "excluded_source_paths": list(config.excluded_source_paths),
        },
    }


def county_task_from_payload(payload: dict[str, object]) -> tuple[CountyTask, RuntimeConfig]:
    task_data = payload["task"]
    config_data = payload["config"]
    if not isinstance(task_data, dict) or not isinstance(config_data, dict):
        raise ValueError("县级子进程任务文件格式无效")
    task = CountyTask(
        code=str(task_data["code"]),
        name=str(task_data["name"]),
        geometry_wgs84_wkb=base64.b64decode(str(task_data["geometry_wgs84_wkb"])),
        ordinal=int(task_data["ordinal"]),
        total=int(task_data["total"]),
    )
    excluded = config_data.get("excluded_source_paths", [])
    if not isinstance(excluded, list):
        raise ValueError("县级子进程排除路径格式无效")
    config = RuntimeConfig(
        index_path=Path(str(config_data["index_path"])),
        output_dir=Path(str(config_data["output_dir"])),
        temp_root=Path(str(config_data["temp_root"])),
        name_template=str(config_data["name_template"]),
        overwrite=bool(config_data["overwrite"]),
        excluded_source_paths=tuple(str(path) for path in excluded),
    )
    return task, config


def setup_worker_logging() -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter("%(levelname)s | %(message)s"))
    LOG.setLevel(logging.INFO)
    LOG.handlers[:] = [handler]


def county_worker_main(task_file: Path) -> int:
    """在独立 Python 进程中执行一个县，结果通过标准输出返回给父进程。"""
    setup_worker_logging()
    try:
        payload = json.loads(task_file.read_text(encoding="utf-8"))
        task, config = county_task_from_payload(payload)
        code, status = process_county(task, config)
        result = {"code": code, "status": status, "error": ""}
        print(WORKER_RESULT_PREFIX + json.dumps(result, ensure_ascii=False), flush=True)
        return 0
    except Exception as exc:
        LOG.exception("县级子进程处理失败：%s", exc)
        result = {"code": "", "status": "failed", "error": str(exc)}
        print(WORKER_RESULT_PREFIX + json.dumps(result, ensure_ascii=False), flush=True)
        return 1
    finally:
        cleanup_created_temp_dirs()


def log_worker_line(task: CountyTask, line: str) -> None:
    prefix = f"[{task.code} {task.name}] 子进程："
    if line.startswith("ERROR | "):
        LOG.error("%s%s", prefix, line[8:])
    elif line.startswith("WARNING | "):
        LOG.warning("%s%s", prefix, line[10:])
    elif line.startswith("INFO | "):
        LOG.info("%s%s", prefix, line[7:])
    else:
        LOG.info("%s%s", prefix, line)


def process_county_subprocess(
    task: CountyTask,
    config: RuntimeConfig,
) -> tuple[str, str]:
    """用独立解释器处理一个县；当前线程只负责进程调度和日志转发。"""
    emit_event(
        "county_started", code=task.code, name=task.name,
        ordinal=task.ordinal, total=task.total,
    )
    token = uuid.uuid4().hex
    task_file = config.temp_root / f"county_task_{task.code}_{token}.json"
    staged_file = config.temp_root / f".county_task_{task.code}_{token}.tmp"
    payload = county_task_payload(task, config)
    try:
        staged_file.write_text(
            json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
            encoding="utf-8",
        )
        os.replace(staged_file, task_file)
        environment = os.environ.copy()
        environment["PYTHONIOENCODING"] = "utf-8"
        process = subprocess.Popen(
            [sys.executable, str(Path(__file__).resolve()), "--county-worker-task", str(task_file)],
            cwd=Path(__file__).resolve().parent,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            env=environment,
        )
        result: dict[str, object] | None = None
        assert process.stdout is not None
        for raw_line in process.stdout:
            line = raw_line.rstrip("\r\n")
            if line.startswith(WORKER_RESULT_PREFIX):
                try:
                    parsed = json.loads(line[len(WORKER_RESULT_PREFIX):])
                    if isinstance(parsed, dict):
                        result = parsed
                except json.JSONDecodeError:
                    log_worker_line(task, line)
            elif line:
                log_worker_line(task, line)
        exit_code = process.wait()
        if result is None:
            raise RuntimeError(f"县级子进程异常退出（退出码 {exit_code}），未返回处理结果")
        error = str(result.get("error", ""))
        status = str(result.get("status", "failed"))
        if exit_code != 0 or status == "failed":
            raise RuntimeError(error or f"县级子进程退出码 {exit_code}")
        if status not in {"success", "skipped", "no_data"}:
            raise RuntimeError(f"县级子进程返回未知状态：{status}")
        return task.code, status
    finally:
        staged_file.unlink(missing_ok=True)
        task_file.unlink(missing_ok=True)


def main(argv: Sequence[str] | None = None) -> int:
    global _events_enabled
    args = parse_args(argv)
    _events_enabled = args.emit_progress_events
    try:
        validate_args(args)
        args.output_dir = args.output_dir.resolve()
        args.output_dir.mkdir(parents=True, exist_ok=True)
        setup_logging((args.log_file or args.output_dir / "clip_county_shapefiles.log").resolve())
        boundary = resolve_boundary(args.boundary.resolve())
        index_path = args.index.resolve()
        # 默认把 county_shp_* 工作目录放在脚本所在根目录，
        # 不再混入正式输出目录；显式 --temp-dir 仍优先。
        temp_root = (
            args.temp_dir.resolve()
            if args.temp_dir
            else Path(__file__).resolve().parent
        )
        temp_root.mkdir(parents=True, exist_ok=True)
        selected = {six_digit_code(code) for code in args.county} if args.county else None
        counties = load_counties(boundary, args.code_field, args.name_field, selected)
        output_paths = [
            args.output_dir / safe_output_name(args.name_template, task)
            for task in counties
        ]
        normalized_outputs = [
            os.path.normcase(str(path.resolve())) for path in output_paths
        ]
        if len(set(normalized_outputs)) != len(normalized_outputs):
            raise ValueError("输出文件名存在重复；不同县不能由子进程同时写入同一文件")

        cpu_slots = max(1, math.floor((os.cpu_count() or 1) * args.cpu_percent / 100.0))
        workers = min(args.workers, cpu_slots)
        index_workers = min(args.index_workers, cpu_slots)
        LOG.info(
            "资源计划：逻辑 CPU=%d，CPU=%.1f%%，请求并发=%d，县级子进程=%d，索引线程=%d",
            os.cpu_count() or 1, args.cpu_percent, args.workers, workers, index_workers,
        )
        emit_event(
            "job_plan",
            total=len(counties),
            workers=workers,
            counties=[
                {"code": task.code, "name": task.name, "ordinal": task.ordinal}
                for task in counties
            ],
        )
        emit_event("stage", name="index", message="正在检查或建立 SHP 空间索引")
        update_index(index_path, args.shp_dir.resolve(), args.index_mode, index_workers)
        emit_event("stage", name="clipping", message="空间索引就绪，开始按县裁剪 SHP")

        config = RuntimeConfig(
            index_path=index_path,
            output_dir=args.output_dir,
            temp_root=temp_root,
            name_template=args.name_template,
            overwrite=args.overwrite,
            # 输出目录可能位于输入扫描目录内。所有本轮目标都不得再作为
            # 任何县级子进程的输入源，避免一个进程写、另一个进程读取。
            excluded_source_paths=tuple(normalized_outputs),
        )
        counts = {"success": 0, "skipped": 0, "no_data": 0, "failed": 0}
        completed = 0
        # 调度和日志收集仍使用线程；每个县的 GDAL 裁剪在独立 Python
        # 子进程中运行，隔离 GDAL 状态和内存，并保留受控的最大并发数。
        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="county-process") as pool:
            futures = {
                pool.submit(process_county_subprocess, task, config): task
                for task in counties
            }
            for future in as_completed(futures):
                task = futures[future]
                error = ""
                try:
                    _, status = future.result()
                except Exception as exc:
                    status = "failed"
                    error = str(exc)
                    LOG.exception("[%s %s] 处理失败：%s", task.code, task.name, exc)
                counts[status] += 1
                completed += 1
                emit_event(
                    "county_finished",
                    code=task.code,
                    name=task.name,
                    ordinal=task.ordinal,
                    total=task.total,
                    status=status,
                    error=error,
                    completed=completed,
                    percent=round(completed * 100 / max(1, len(counties)), 2),
                )
        LOG.info(
            "全部结束：成功 %d，跳过 %d，无相交数据 %d，失败 %d",
            counts["success"], counts["skipped"], counts["no_data"], counts["failed"],
        )
        exit_code = 1 if counts["failed"] else 0
        emit_event("job_finished", exit_code=exit_code, counts=counts)
        return exit_code
    except KeyboardInterrupt:
        LOG.warning("用户中断任务")
        emit_event("job_error", message="用户中断任务")
        return 130
    except Exception as exc:
        if LOG.handlers:
            LOG.exception("任务启动或运行失败：%s", exc)
        else:
            print(f"错误：{exc}", file=sys.stderr)
        emit_event("job_error", message=str(exc))
        return 2
    finally:
        # 所有工作线程结束、GDAL 数据集释放后，再兜底删除本次运行遗留的临时目录。
        cleanup_created_temp_dirs()


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "--county-worker-task":
        raise SystemExit(county_worker_main(Path(sys.argv[2])))
    raise SystemExit(main())
