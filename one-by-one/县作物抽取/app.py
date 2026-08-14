from __future__ import annotations

import argparse
import json
import multiprocessing
import os
import re
import threading
import traceback
from collections import OrderedDict
from concurrent.futures import ProcessPoolExecutor
from datetime import datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from itertools import islice
from typing import Any
from urllib.parse import parse_qs, urlparse

import fiona
from pyproj import CRS, Transformer
from shapely import make_valid
from shapely.geometry import MultiPolygon, shape
from shapely.ops import transform
from shapely.strtree import STRtree


BASE_DIR = Path(__file__).resolve().parent
COUNTY_DIR = BASE_DIR / "00县边界"
CITY_DIR = BASE_DIR / "00市边界"
COUNTY_NAME_FIELD = "area_name"
COUNTY_CODE_FIELD = "area_code"
CITY_NAME_FIELD = "市名称"
CITY_CODE_FIELD = "市代码"
CROP_FIELD = "class"
MAX_OPEN_WRITERS = 24
GEOMETRY_CONCURRENCY = 5
GEOMETRY_CHUNK_SIZE = 100
WORK_BATCH_SIZE = 20_000
WRITE_BATCH_SIZE = 500
CROP_NAMES = {
    1: "春玉米",
    2: "中稻",
    3: "大豆",
    4: "春小麦",
    5: "马铃薯",
    6: "油菜",
    7: "向日葵籽",
}
DEFAULT_CROP_NAMES_TEXT = "\n".join(
    f"{number}={name}" for number, name in CROP_NAMES.items()
)


class TaskState:
    def __init__(self, task_id: str = "local") -> None:
        self.task_id = task_id
        self.lock = threading.Lock()
        self.running = False
        self.status = "idle"
        self.logs: list[str] = []
        self.error = ""

    def reset_and_start(self) -> bool:
        with self.lock:
            if self.running:
                return False
            self.running = True
            self.status = "running"
            self.logs = []
            self.error = ""
            return True

    def log(self, message: str) -> None:
        line = f"[{datetime.now():%H:%M:%S}][任务 {self.task_id[:8]}] {message}"
        with self.lock:
            self.logs.append(line)
        print(line, flush=True)

    def finish(self, error: str = "") -> None:
        with self.lock:
            self.running = False
            self.status = "error" if error else "completed"
            self.error = error

    def snapshot(self, since: int) -> dict[str, Any]:
        with self.lock:
            safe_since = min(max(since, 0), len(self.logs))
            return {
                "status": self.status,
                "running": self.running,
                "error": self.error,
                "logs": self.logs[safe_since:],
                "next": len(self.logs),
            }

TASKS: dict[str, TaskState] = {}
TASKS_LOCK = threading.Lock()
ACTIVE_OUTPUTS: dict[str, str] = {}

_CLIP_COUNTY_NAMES: list[Any] | None = None
_CLIP_COUNTY_GEOMS: list[Any] | None = None
_CLIP_COUNTY_TREE: STRtree | None = None
_CLIP_CROP_FIELD: str | None = None
_CLIP_CROP_NAMES: dict[int, str] | None = None


def convert_network_path(path: Any) -> Any:
    """Convert supported Windows NAS paths to their Linux mount paths."""
    if path is None:
        return path

    path = str(path).strip()
    if not path:
        return path

    path = path.replace("\\", "/")
    share_mapping = {
        "data": "/media/cangling/nas_folder",
        "新建卷": "/media/cangling/xinjianjuan",
        "datadisk2": "/media/cangling/EAGET",
        "新加卷": "/media/cangling/xinjiajuan",
    }

    for index in range(1, 256):
        host = f"10.10.10.{index}"
        for share_name, linux_prefix in share_mapping.items():
            for windows_prefix in (
                f"//{host}/{share_name}",
                f"/{host}/{share_name}",
                f"{host}/{share_name}",
            ):
                # Match the complete share name so "data" cannot match "datadisk2".
                if path == windows_prefix:
                    return linux_prefix
                if path.startswith(windows_prefix + "/"):
                    relative_path = path[len(windows_prefix):]
                    return linux_prefix + relative_path

    return path


def safe_part(value: Any) -> str:
    text = str(value).strip() or "空值"
    text = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", text)
    text = re.sub(r"\s+", "_", text)
    return text[:80].rstrip(". ") or "空值"


def parse_crop_names(value: str) -> dict[int, str]:
    mapping: dict[int, str] = {}
    for line_number, raw_line in enumerate(value.splitlines(), start=1):
        line = raw_line.strip()
        if not line:
            continue
        if "=" not in line:
            raise ValueError(f"作物分类第 {line_number} 行格式错误，应为：编号=名称")
        number_text, name = line.split("=", 1)
        try:
            number = int(number_text.strip())
        except ValueError as exc:
            raise ValueError(f"作物分类第 {line_number} 行编号不是整数。") from exc
        name = name.strip()
        if not name:
            raise ValueError(f"作物分类第 {line_number} 行名称不能为空。")
        if number in mapping:
            raise ValueError(f"作物分类编号 {number} 重复。")
        mapping[number] = name
    if not mapping:
        raise ValueError("作物分类定义不能为空。")
    return mapping


def crop_name(value: Any, crop_names: dict[int, str] | None = None) -> str:
    """Return the configured Chinese crop name while tolerating numeric strings."""
    crop_names = crop_names or CROP_NAMES
    try:
        numeric = int(value)
    except (TypeError, ValueError):
        return f"未知作物_{safe_part(value)}"
    return crop_names.get(numeric, f"未知作物_{numeric}")


def find_county_shapefile() -> Path:
    """Find the county boundary shp beside app.py, under 00县边界."""
    if not COUNTY_DIR.is_dir():
        raise ValueError(f"县界目录不存在：{COUNTY_DIR}")
    shapefiles = sorted(COUNTY_DIR.glob("*.shp"))
    if not shapefiles:
        raise ValueError(f"县界目录中没有 shp 文件：{COUNTY_DIR}")
    return shapefiles[0].resolve()


def find_city_shapefile() -> Path:
    if not CITY_DIR.is_dir():
        raise ValueError(f"市界目录不存在：{CITY_DIR}")
    shapefiles = sorted(CITY_DIR.glob("*.shp"))
    if not shapefiles:
        raise ValueError(f"市界目录中没有 shp 文件：{CITY_DIR}")
    return shapefiles[0].resolve()


def parse_city_names(value: str) -> list[str]:
    names = [name.strip() for name in re.split(r"[;；]", value) if name.strip()]
    return list(dict.fromkeys(names))


def find_city_codes(city_names: list[str]) -> dict[str, str]:
    with fiona.open(find_city_shapefile()) as cities:
        fields = cities.schema["properties"]
        if CITY_NAME_FIELD not in fields or CITY_CODE_FIELD not in fields:
            raise ValueError(
                f"市界 shp 必须包含 {CITY_NAME_FIELD!r} 和 {CITY_CODE_FIELD!r} 字段。"
            )
        available = {
            str(feature["properties"][CITY_NAME_FIELD]).strip():
            str(feature["properties"][CITY_CODE_FIELD]).strip()
            for feature in cities
        }
    missing = [name for name in city_names if name not in available]
    if missing:
        raise ValueError(f"市界中找不到以下城市：{'、'.join(missing)}")
    return {name: available[name] for name in city_names}


def output_path_key(path: str) -> str:
    """Return a canonical key used to prevent concurrent writes to one folder."""
    converted = convert_network_path(path)
    resolved = Path(converted).expanduser().resolve()
    return os.path.normcase(str(resolved))


def reserve_task(
    task_id: str, output_dir: str
) -> tuple[TaskState | None, str, str]:
    """Atomically reserve an output folder for one browser-tab task."""
    reserved_output = output_path_key(output_dir)
    with TASKS_LOCK:
        output_owner = ACTIVE_OUTPUTS.get(reserved_output)
        if output_owner and output_owner != task_id:
            return (
                None,
                reserved_output,
                "该输出文件夹正被另一个标签页使用，请等待任务完成或更换输出文件夹。",
            )
        task_state = TASKS.get(task_id)
        if task_state is None:
            task_state = TaskState(task_id)
            TASKS[task_id] = task_state
        if not task_state.reset_and_start():
            return (
                None,
                reserved_output,
                "当前标签页已有任务正在运行，请等待完成。",
            )
        ACTIVE_OUTPUTS[reserved_output] = task_id
    return task_state, reserved_output, ""


def release_output(task_id: str, reserved_output: str) -> None:
    with TASKS_LOCK:
        if ACTIVE_OUTPUTS.get(reserved_output) == task_id:
            ACTIVE_OUTPUTS.pop(reserved_output, None)


class WriterPool:
    """Limit open shapefile handles and reopen older files in append mode."""

    def __init__(
        self,
        output_dir: Path,
        schema: dict[str, Any],
        crs_wkt: str | None,
        encoding: str,
    ) -> None:
        self.output_dir = output_dir
        self.schema = schema
        self.crs_wkt = crs_wkt
        self.encoding = encoding
        self.handles: OrderedDict[Path, Any] = OrderedDict()
        self.initialized: set[Path] = set()
        self.buffers: dict[Path, list[dict[str, Any]]] = {}

    def _open(self, path: Path):
        if len(self.handles) >= MAX_OPEN_WRITERS:
            _, oldest = self.handles.popitem(last=False)
            oldest.close()
        if path in self.initialized:
            writer = fiona.open(path, mode="a")
        else:
            writer = fiona.open(
                path,
                mode="w",
                driver="ESRI Shapefile",
                schema=self.schema,
                crs_wkt=self.crs_wkt,
                encoding=self.encoding,
            )
            self.initialized.add(path)
        self.handles[path] = writer
        return writer

    def write(self, path: Path, feature: dict[str, Any]) -> bool:
        # Overlay operations can occasionally produce a GeometryCollection
        # (for example, a polygon plus a boundary line).  Fiona validates every
        # record against the source schema, so enforce polygon-only geometry at
        # the final output boundary as well as after the intersection.
        geometry = feature.get("geometry")
        normalized = polygonal_geometry(shape(geometry)) if geometry else None
        if normalized is None:
            return False
        feature = {**feature, "geometry": normalized.__geo_interface__}
        records = self.buffers.setdefault(path, [])
        records.append(feature)
        if len(records) >= WRITE_BATCH_SIZE:
            self._flush(path)
        return True

    def _flush(self, path: Path) -> None:
        records = self.buffers.get(path)
        if not records:
            return
        writer = self.handles.pop(path, None)
        if writer is None:
            writer = self._open(path)
        else:
            self.handles[path] = writer
        writer.writerecords(records)
        records.clear()

    def close(self) -> None:
        for path in list(self.buffers):
            self._flush(path)
        for writer in self.handles.values():
            writer.close()
        self.handles.clear()
        self.buffers.clear()


def polygonal_geometry(geometry: Any) -> Any | None:
    """Return only valid, non-empty polygonal parts of a geometry."""
    if geometry is None or geometry.is_empty:
        return None

    repaired = geometry if geometry.is_valid else make_valid(geometry)
    polygons: list[Any] = []

    def collect_parts(part: Any) -> None:
        if part.is_empty:
            return
        if part.geom_type == "Polygon":
            if part.area > 0:
                polygons.append(part)
            return
        if part.geom_type in {"MultiPolygon", "GeometryCollection"}:
            for child in part.geoms:
                collect_parts(child)

    collect_parts(repaired)
    if not polygons:
        return None
    if len(polygons) == 1:
        return polygons[0]
    return MultiPolygon(polygons)


def load_counties(
    county_path: Path,
    county_name_field: str,
    source_crs: Any,
    city_codes: set[str] | None = None,
) -> tuple[list[Any], list[Any], STRtree]:
    with fiona.open(county_path) as counties:
        if county_name_field not in counties.schema["properties"]:
            fields = ", ".join(counties.schema["properties"])
            raise ValueError(f"县名字段 {county_name_field!r} 不存在；可用字段：{fields}")
        if city_codes and COUNTY_CODE_FIELD not in counties.schema["properties"]:
            raise ValueError(f"县界 shp 缺少行政区划字段 {COUNTY_CODE_FIELD!r}。")
        if not counties.crs:
            raise ValueError("县界 shp 缺少坐标系信息，无法与地块数据匹配。")
        transformer = None
        if CRS.from_user_input(counties.crs) != CRS.from_user_input(source_crs):
            transformer = Transformer.from_crs(
                counties.crs, source_crs, always_xy=True
            ).transform
        names: list[Any] = []
        geometries: list[Any] = []
        for feature in counties:
            if city_codes:
                county_code = str(feature["properties"][COUNTY_CODE_FIELD]).strip()
                if not any(county_code.startswith(code) for code in city_codes):
                    continue
            if feature["geometry"] is None:
                continue
            geom = polygonal_geometry(shape(feature["geometry"]))
            if geom is None:
                continue
            if transformer:
                geom = transform(transformer, geom)
                geom = polygonal_geometry(geom)
                if geom is None:
                    continue
            geometries.append(geom)
            names.append(feature["properties"][county_name_field])
    if not geometries:
        raise ValueError("县界 shp 中没有有效几何。")
    return names, geometries, STRtree(geometries)


def clip_feature_to_counties(
    geometry: Any,
    properties: dict[str, Any],
    crop_field: str,
    county_names: list[Any],
    county_geoms: list[Any],
    county_tree: STRtree,
    crop_names: dict[int, str],
) -> list[tuple[str, str, dict[str, Any]]]:
    """Calculate county intersections without writing output files."""
    if geometry is None:
        return []
    geom = polygonal_geometry(shape(geometry))
    if geom is None:
        return []
    candidates = county_tree.query(geom, predicate="intersects")
    if len(candidates) == 0:
        return []

    crop = crop_name(properties.get(crop_field), crop_names)
    results: list[tuple[str, str, dict[str, Any]]] = []
    for county_index in candidates:
        county_geom = county_geoms[int(county_index)]
        # Most parcels lie wholly inside one county. Avoid an expensive overlay
        # unless the parcel actually crosses the boundary.
        clipped = geom if county_geom.covers(geom) else geom.intersection(county_geom)
        clipped = polygonal_geometry(clipped)
        if clipped is None:
            continue
        county = str(county_names[int(county_index)]).strip() or "空县名"
        results.append((
            county,
            crop,
            {"geometry": clipped.__geo_interface__, "properties": properties},
        ))
    return results


def initialize_clip_worker(
    county_names: list[Any],
    county_geoms: list[Any],
    crop_field: str,
    crop_names: dict[int, str],
) -> None:
    global _CLIP_COUNTY_NAMES
    global _CLIP_COUNTY_GEOMS
    global _CLIP_COUNTY_TREE
    global _CLIP_CROP_FIELD
    global _CLIP_CROP_NAMES

    _CLIP_COUNTY_NAMES = county_names
    _CLIP_COUNTY_GEOMS = county_geoms
    _CLIP_COUNTY_TREE = STRtree(county_geoms)
    _CLIP_CROP_FIELD = crop_field
    _CLIP_CROP_NAMES = crop_names


def clip_feature_concurrently(
    item: tuple[Any, dict[str, Any]],
) -> list[tuple[str, str, dict[str, Any]]]:
    if (
        _CLIP_COUNTY_NAMES is None
        or _CLIP_COUNTY_GEOMS is None
        or _CLIP_COUNTY_TREE is None
        or _CLIP_CROP_FIELD is None
        or _CLIP_CROP_NAMES is None
    ):
        raise RuntimeError("空间相交并发环境初始化失败。")

    geometry, properties = item
    return clip_feature_to_counties(
        geometry,
        properties,
        _CLIP_CROP_FIELD,
        _CLIP_COUNTY_NAMES,
        _CLIP_COUNTY_GEOMS,
        _CLIP_COUNTY_TREE,
        _CLIP_CROP_NAMES,
    )


def split_shapefile(
    config: dict[str, str], task_state: TaskState | None = None
) -> None:
    task_state = task_state or TaskState()
    source_input = config["source_path"]
    output_input = config["output_dir"]
    converted_source = convert_network_path(source_input)
    converted_output = convert_network_path(output_input)
    source_path = Path(converted_source).expanduser().resolve()
    output_dir = Path(converted_output).expanduser().resolve()
    county_path_value = config.get("county_path", "").strip()
    county_path = (
        Path(county_path_value).expanduser().resolve()
        if county_path_value
        else find_county_shapefile()
    )
    county_field = config.get("county_field", COUNTY_NAME_FIELD).strip()
    crop_field = config.get("crop_field", CROP_FIELD).strip()
    city_names_value = config.get("city_names", "").strip()
    city_names = parse_city_names(city_names_value) if city_names_value else []
    city_code_mapping = find_city_codes(city_names) if city_names else {}
    crop_names = parse_crop_names(
        config.get("crop_names", DEFAULT_CROP_NAMES_TEXT)
    )

    if source_path.suffix.lower() != ".shp" or not source_path.is_file():
        raise ValueError(f"源 shp 不存在：{source_path}")
    if county_path.suffix.lower() != ".shp" or not county_path.is_file():
        raise ValueError(f"县界 shp 不存在：{county_path}")
    if not county_field or not crop_field:
        raise ValueError("县名字段和作物字段不能为空。")
    output_dir.parent.mkdir(parents=True, exist_ok=True)

    if converted_source != str(source_input).strip():
        task_state.log(f"源路径已转换：{source_input} -> {converted_source}")
    if converted_output != str(output_input).strip():
        task_state.log(f"输出路径已转换：{output_input} -> {converted_output}")
    task_state.log(f"源文件：{source_path}")
    task_state.log(f"输出目录：{output_dir}")
    if city_code_mapping:
        selected = "、".join(
            f"{name}（{code}）" for name, code in city_code_mapping.items()
        )
        task_state.log(f"处理城市：{selected}")
    else:
        task_state.log("处理城市：未指定，按源 shp 与县界相交结果处理全部市。")
    with fiona.open(source_path) as source:
        if crop_field not in source.schema["properties"]:
            fields = ", ".join(source.schema["properties"])
            raise ValueError(f"作物字段 {crop_field!r} 不存在；可用字段：{fields}")
        if not source.crs:
            raise ValueError("源 shp 缺少坐标系信息。")

        task_state.log("正在读取县界并建立空间索引……")
        county_names, county_geoms, county_tree = load_counties(
            county_path, county_field, source.crs, set(city_code_mapping.values())
        )
        task_state.log(f"已载入 {len(county_geoms)} 个县界。")

        encoding = source.encoding or "UTF-8"
        pool = WriterPool(output_dir, source.schema, source.crs_wkt, encoding)
        total = len(source)
        written = 0
        unmatched = 0
        group_counts: dict[tuple[str, str], int] = {}
        used_paths: dict[tuple[str, str], Path] = {}
        try:
            task_state.log(f"空间相交并发数：{GEOMETRY_CONCURRENCY}。")
            processed = 0
            with ProcessPoolExecutor(
                max_workers=GEOMETRY_CONCURRENCY,
                mp_context=multiprocessing.get_context("spawn"),
                initializer=initialize_clip_worker,
                initargs=(county_names, county_geoms, crop_field, crop_names),
            ) as executor:
                source_iterator = iter(source)
                while True:
                    batch = list(islice(source_iterator, WORK_BATCH_SIZE))
                    if not batch:
                        break
                    batch_inputs = [
                        (
                            feature["geometry"].__geo_interface__
                            if feature["geometry"] is not None
                            else None,
                            dict(feature["properties"]),
                        )
                        for feature in batch
                    ]
                    batch_results = executor.map(
                        clip_feature_concurrently,
                        batch_inputs,
                        chunksize=GEOMETRY_CHUNK_SIZE,
                    )
                    for results in batch_results:
                        processed += 1
                        if not results:
                            unmatched += 1
                        for county, crop, clipped_feature in results:
                            group = (county, crop)
                            path = used_paths.get(group)
                            if path is None:
                                stem = f"{safe_part(county)}__{safe_part(crop)}"
                                county_output_dir = (
                                    output_dir
                                    / safe_part(county)
                                    / "矢量成果"
                                    / "原始"
                                )
                                county_output_dir.mkdir(parents=True, exist_ok=True)
                                path = county_output_dir / f"{stem}.shp"
                                occupied = set(used_paths.values())
                                suffix = 2
                                while path in occupied:
                                    path = county_output_dir / f"{stem}_{suffix}.shp"
                                    suffix += 1
                                used_paths[group] = path
                            # This is the only output-writing path.
                            if pool.write(path, clipped_feature):
                                group_counts[group] = group_counts.get(group, 0) + 1
                                written += 1

                        if processed % 10_000 == 0 or processed == total:
                            percent = processed / total * 100 if total else 100.0
                            task_state.log(
                                f"进度 {processed:,}/{total:,}（{percent:.2f}%），"
                                f"已写入 {written:,}，"
                                f"未匹配县界 {unmatched:,}。"
                            )
        finally:
            pool.close()

    task_state.log(f"共生成 {len(group_counts)} 个 shp，写入 {written:,} 个裁切地块。")
    if unmatched:
        task_state.log(f"警告：{unmatched:,} 个地块未匹配到县界，未输出。")
    for (county, crop), count in sorted(group_counts.items()):
        task_state.log(f"  {county} / {crop}：{count:,} 个地块")


def run_task(
    config: dict[str, str], task_state: TaskState, reserved_output: str
) -> None:
    try:
        task_state.log("任务开始，正在运行……")
        split_shapefile(config, task_state)
    except Exception as exc:
        message = f"运行失败：{exc}"
        task_state.log(message)
        task_state.log(traceback.format_exc().rstrip())
        task_state.finish(message)
    else:
        task_state.log("运行完成。")
        task_state.finish()
    finally:
        release_output(task_state.task_id, reserved_output)


HTML = r"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>县级作物 Shapefile 拆分工具</title>
  <style>
    :root { color-scheme:light; --ink:#10213d; --muted:#71839d; --line:#d5deea;
      --panel:#fff; --field:#f7faff; --blue:#2867e8; --green:#169a62; --red:#c93737; }
    * { box-sizing:border-box }
    body { margin:0; min-height:100vh; font-family:system-ui,"Microsoft YaHei",sans-serif;
      color:var(--ink); background:linear-gradient(135deg,#e9f1ff 0%,#f5f8fc 44%,#edf2f7 100%); }
    main { width:min(1480px,calc(100% - 56px)); margin:42px auto; }
    .page-title { margin:0 0 30px; font-size:34px; line-height:1.2; letter-spacing:-.5px; font-weight:800 }
    .workspace { display:grid; grid-template-columns:minmax(430px,.92fr) minmax(520px,1.08fr);
      gap:26px; align-items:start }
    .card { background:rgba(255,255,255,.96); border:1px solid var(--line); border-radius:12px;
      padding:28px; box-shadow:0 18px 42px rgba(49,70,101,.10) }
    .card-title { margin:0; font-size:23px; font-weight:800 }
    .card-subtitle { margin:7px 0 24px; color:var(--muted); font-size:14px }
    .grid { display:grid; grid-template-columns:1fr; gap:18px }
    .wide { min-width:0 }
    label { display:block; margin-bottom:8px; color:var(--ink); font-size:14px; font-weight:700 }
    input, textarea { width:100%; padding:13px 14px; border:1px solid #ccd7e5; border-radius:9px;
      background:var(--field); color:var(--ink); outline:none; font:inherit; transition:.16s ease }
    input:focus, textarea:focus { background:#fff; border-color:var(--blue);
      box-shadow:0 0 0 3px rgba(40,103,232,.10) }
    textarea { min-height:150px; resize:vertical; line-height:1.55 }
    details.crop-config { border:1px solid #ccd7e5; border-radius:9px; padding:13px 14px; background:var(--field) }
    details.crop-config summary { cursor:pointer; font-size:14px; color:var(--ink); font-weight:700; user-select:none }
    details.crop-config textarea { margin-top:13px; background:#fff }
    .actions { display:flex; flex-direction:column; align-items:stretch; gap:12px; margin-top:22px }
    button { width:100%; border:0; border-radius:9px; padding:14px 24px; font-size:16px; font-weight:800;
      cursor:pointer; color:#fff; background:var(--blue); box-shadow:0 8px 18px rgba(40,103,232,.20) }
    button:hover { background:#1f58cf } button:disabled { opacity:.55; cursor:not-allowed }
    .output-card { padding:28px }
    .output-head { display:flex; align-items:center; justify-content:space-between; gap:18px; margin-bottom:16px }
    .output-head .card-title { font-size:21px }
    #state { flex:0 0 auto; padding:7px 13px; border-radius:999px; color:#52647d;
      background:#edf1f6; font-size:13px; font-weight:800 }
    #state.running { color:#8a5a00; background:#fff1c9 }
    #state.completed { color:#087c49; background:#d9f8e8 }
    #state.error { color:var(--red); background:#ffe2e2 }
    pre { height:540px; overflow:auto; margin:0; padding:18px; border:1px solid #17243a;
      border-radius:8px; color:#dce9ff; background:#091221;
      font:13px/1.62 Consolas,"Microsoft YaHei",monospace; white-space:pre-wrap;
      scrollbar-color:#738096 #091221 }
    @media(max-width:980px) { main{width:min(760px,calc(100% - 28px));margin:28px auto}
      .workspace{grid-template-columns:1fr}.page-title{font-size:28px;margin-bottom:22px}pre{height:420px} }
  </style>
</head>
<body><main>
  <div class="workspace">
  <section class="card control-card">
    <h2 class="card-title">县级作物 Shapefile 拆分</h2>
    <p class="card-subtitle">按“县 × 作物”组合输出独立 Shapefile</p>
    <div class="grid">
      <div class="wide"><label for="source">源地块 shp 路径</label>
        <input id="source" value="" placeholder="请输入源地块 shp 路径" autocomplete="off"></div>
      <div class="wide"><label for="output">输出文件夹</label>
        <input id="output" value="" placeholder="请输入输出文件夹路径" autocomplete="off"></div>
      <div class="wide"><label for="cities">市名称（可选，多个市用；分隔）</label>
        <input id="cities" value="" placeholder="留空处理相交到的全部市；例如：赤峰市；通辽市" autocomplete="off"></div>
      <div class="wide"><details class="crop-config">
        <summary>作物分类定义（每行：编号=名称）</summary>
        <textarea id="cropNames" spellcheck="false" aria-label="作物分类定义">__CROP_NAMES__</textarea>
      </details></div>
    </div>
    <div class="actions"><button id="run">运行拆分脚本</button></div>
  </section>
  <section class="card output-card">
    <div class="output-head"><h2 class="card-title">运行输出：app.py</h2><span id="state">等待运行</span></div>
    <pre id="log">尚未运行。</pre>
  </section>
  </div>
</main>
<script>
  const runButton = document.querySelector('#run');
  const state = document.querySelector('#state');
  const log = document.querySelector('#log');
  const taskId = crypto.randomUUID ? crypto.randomUUID() :
    (Date.now().toString(36) + Math.random().toString(36).slice(2));
  let cursor = 0;
  function setState(value) {
    state.className = value;
    state.textContent = ({idle:'等待运行',running:'正在运行…',completed:'运行完成',error:'运行失败'})[value] || value;
    runButton.disabled = value === 'running';
  }
  async function poll() {
    try {
      const response = await fetch('/api/status?task_id=' + encodeURIComponent(taskId) +
        '&since=' + cursor, {cache:'no-store'});
      const data = await response.json();
      if (data.logs.length) {
        if (cursor === 0) log.textContent = '';
        log.textContent += data.logs.join('\n') + '\n';
        log.scrollTop = log.scrollHeight;
      }
      cursor = data.next;
      setState(data.status);
    } catch (_) {}
  }
  runButton.addEventListener('click', async () => {
    setState('running'); log.textContent = '正在提交任务…\n'; cursor = 0;
    const payload = {
      task_id: taskId,
      source_path: document.querySelector('#source').value,
      output_dir: document.querySelector('#output').value,
      city_names: document.querySelector('#cities').value,
      crop_names: document.querySelector('#cropNames').value
    };
    try {
      const response = await fetch('/api/run', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(payload)});
      const data = await response.json();
      if (!response.ok) { log.textContent += data.error + '\n'; setState('error'); }
      else { log.textContent = ''; }
    } catch (error) { log.textContent += '提交失败：' + error + '\n'; setState('error'); }
  });
  poll(); setInterval(poll, 800);
</script></body></html>"""


class RequestHandler(BaseHTTPRequestHandler):
    server_version = "ShpSplitter/1.0"

    def log_message(self, format: str, *args: Any) -> None:
        # Deliberately suppress HTTP access logs; task logs use TaskState.log.
        return

    def send_json(self, data: dict[str, Any], status: int = 200) -> None:
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/":
            body = HTML.replace("__CROP_NAMES__", DEFAULT_CROP_NAMES_TEXT).encode("utf-8")
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif parsed.path == "/api/status":
            query = parse_qs(parsed.query)
            task_id = query.get("task_id", [""])[0]
            try:
                since = int(query.get("since", ["0"])[0])
            except ValueError:
                since = 0
            with TASKS_LOCK:
                task_state = TASKS.get(task_id)
            if task_state is None:
                self.send_json({
                    "status": "idle", "running": False, "error": "",
                    "logs": [], "next": 0,
                })
            else:
                self.send_json(task_state.snapshot(since))
        else:
            self.send_json({"error": "Not found"}, HTTPStatus.NOT_FOUND)

    def do_POST(self) -> None:
        if urlparse(self.path).path != "/api/run":
            self.send_json({"error": "Not found"}, HTTPStatus.NOT_FOUND)
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length <= 0 or length > 64 * 1024:
                raise ValueError("请求内容为空或过大。")
            config = json.loads(self.rfile.read(length).decode("utf-8"))
            required = (
                "task_id", "source_path", "output_dir", "city_names", "crop_names"
            )
            if not all(isinstance(config.get(key), str) for key in required):
                raise ValueError("输入参数不完整。")
            if not config["source_path"].strip() or not config["output_dir"].strip():
                raise ValueError("源地块 shp 路径和输出文件夹不能为空。")
            parse_crop_names(config["crop_names"])
            task_id = config["task_id"].strip()
            if not task_id or len(task_id) > 100:
                raise ValueError("任务 ID 无效。")
        except (ValueError, json.JSONDecodeError, UnicodeDecodeError) as exc:
            self.send_json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
            return
        task_state, reserved_output, conflict_message = reserve_task(
            task_id, config["output_dir"]
        )
        if conflict_message:
            self.send_json({"error": conflict_message}, HTTPStatus.CONFLICT)
            return
        assert task_state is not None
        thread = threading.Thread(
            target=run_task,
            args=(config, task_state, reserved_output),
            daemon=True,
        )
        thread.start()
        self.send_json({"ok": True}, HTTPStatus.ACCEPTED)


def main() -> None:
    parser = argparse.ArgumentParser(description="按县和作物拆分 Shapefile 的网页工具")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8898)
    args = parser.parse_args()
    server = ThreadingHTTPServer((args.host, args.port), RequestHandler)
    print(f"脚本页面：http://10.10.10.240:{args.port}", flush=True)
    print("按 Ctrl+C 停止服务。", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("服务已停止。", flush=True)
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
