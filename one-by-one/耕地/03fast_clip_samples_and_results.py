#!/usr/bin/env python
# -*- coding: utf-8 -*-
from __future__ import annotations

import argparse
import concurrent.futures
import os
import re
import sys
import warnings
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

import fiona
import geopandas as gpd
import pandas as pd
from pyproj import CRS
from shapely.geometry import GeometryCollection, MultiPolygon, Polygon, box
from shapely.ops import transform, unary_union

try:
    from shapely import make_valid as shapely_make_valid
except ImportError:  # Shapely < 2.0
    shapely_make_valid = None


SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_RESULT_ROOT = SCRIPT_DIR / "00分类结果"
DEFAULT_SAMPLE_DIR = SCRIPT_DIR / "01生成样方"
DEFAULT_OUTPUT_ROOT = SCRIPT_DIR
DEFAULT_BOUNDARY_REF = SCRIPT_DIR / "00县边界"
DEFAULT_CITY_BOUNDARY_REF = SCRIPT_DIR / "00市边界"
DEFAULT_STAGING_DIR_NAME = "中间裁剪结果"
DEFAULT_DELIVERY_DIR = Path("03测量值")

FINAL_FIELDS = ("QXMC", "QXDM", "CUNMC", "CUNDM", "YGCUNDM", "TBLXMC", "TBLXDM", "TBMJ", "CLYX", "BZ")
FINAL_TEXT_FIELDS = ("QXMC", "QXDM", "CUNMC", "CUNDM", "YGCUNDM", "TBLXMC", "TBLXDM", "BZ")
FINAL_FLOAT_FIELDS = ("TBMJ", "CLYX")
FINAL_SCHEMA = {
    "geometry": "Polygon",
    "properties": {
        "QXMC": "str:30",
        "QXDM": "str:6",
        "CUNMC": "str:100",
        "CUNDM": "str:12",
        "YGCUNDM": "str:12",
        "TBLXMC": "str:50",
        "TBLXDM": "str:9",
        "TBMJ": "float:24.2",
        "CLYX": "float:24.8",
        "BZ": "str:100",
    },
}
SHAPEFILE_SUFFIXES = {
    ".shp",
    ".shx",
    ".dbf",
    ".prj",
    ".cpg",
    ".sbn",
    ".sbx",
    ".qix",
    ".fix",
    ".shp.xml",
}

CITY_BY_PREFIX = {
    "3201": "南京市",
    "3202": "无锡市",
    "3203": "徐州市",
    "3204": "常州市",
    "3205": "苏州市",
    "3206": "南通市",
    "3207": "连云港市",
    "3208": "淮安市",
    "3209": "盐城市",
    "3210": "扬州市",
    "3211": "镇江市",
    "3212": "泰州市",
    "3213": "宿迁市",
}
CITY_BOUNDARY_NAME_FIELDS = ("市名称", "市名", "市级名称", "city_name", "CITY_NAME", "NAME", "name")
CLYX_VALUE = 0.5
CUNMC_FIELD_CANDIDATES = ("CUNMC", "QHMC", "村名称", "村名", "村社区名")
CUNDM_FIELD_CANDIDATES = ("CUNDM", "QHDM", "村代码", "村级代码", "行政区划代码")
TBLXMC_FIELD_CANDIDATES = ("TBLXMC", "sample_cls")
TBLXDM_FIELD_CANDIDATES = ("TBLXDM",)


@dataclass(frozen=True)
class BoundaryUnit:
    city: str
    name: str
    code: str
    bounds: tuple[float, float, float, float]
    geometry: object


@dataclass(frozen=True)
class ResultEntry:
    path: str
    code: str
    name: str
    bounds: tuple[float, float, float, float]
    geometry: object


@dataclass(frozen=True)
class ClipTask:
    sample_path: str
    relative_parent: str
    city: str
    county: str
    qxmc: str
    qxdm: str
    result_paths: tuple[str, ...]
    staging_path: str
    delivery_path: str
    encoding: str
    input_encoding: str | None
    area_epsilon: float
    write_staging: bool


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Fast sample clipping/fill workflow with county code/name from the new boundary layer."
    )
    parser.add_argument("result_root", type=Path, nargs="?", default=DEFAULT_RESULT_ROOT, help="结果矢量根目录，递归查找 .shp；默认 自检样方/00分类结果。")
    parser.add_argument("--source_root", type=Path, default=None, help="与 01generate_county_samples_by_city.py 的 --source_root 保持一致；传入后覆盖 result_root。")
    parser.add_argument("sample_dir", type=Path, nargs="?", default=DEFAULT_SAMPLE_DIR, help="样方根目录，递归查找 *_样方.shp；默认 自检样方/01生成样方。")
    parser.add_argument("output_root", type=Path, nargs="?", default=DEFAULT_OUTPUT_ROOT, help="输出根目录；默认 自检样方。")
    parser.add_argument("--boundary-ref", type=Path, default=DEFAULT_BOUNDARY_REF, help="县边界 shp 或目录；默认 自检样方/00县边界。")
    parser.add_argument("--city-boundary-ref", type=Path, default=DEFAULT_CITY_BOUNDARY_REF, help="市边界 shp 或目录；默认 自检样方/00市边界。")
    parser.add_argument("--sample-pattern", default="*_样方.shp", help="样方 shp 通配符。")
    parser.add_argument("--staging-dir", type=Path, default=None, help="中间结果目录，默认 output_root/中间裁剪结果。")
    parser.add_argument("--delivery-dir", type=Path, default=None, help="交付结果目录，默认 output_root/03测量值。")
    parser.add_argument("--output-suffix", default="_裁剪补齐", help="中间结果文件名后缀。")
    parser.add_argument("--workers", type=int, default=max(1, min((os.cpu_count() or 1), 6)), help="并行 worker 数。")
    parser.add_argument("--encoding", default="UTF-8", help="输出 Shapefile 编码。")
    parser.add_argument("--input-encoding", default=None, help="输入 Shapefile 编码。")
    parser.add_argument("--area-epsilon", type=float, default=0.0, help="忽略面积小于等于该值的碎小面。")
    parser.add_argument(
        "--result-match",
        choices=("auto", "exact", "spatial"),
        default="auto",
        help="结果矢量匹配方式：auto 优先县代码/县名，缺失时空间兜底。",
    )
    parser.add_argument(
        "--only-boundary-name",
        nargs="*",
        default=None,
        help="只处理指定区县；可写 区县名 或 市-区县名，例如 仪征市 宿迁市-宿迁湖滨新区。",
    )
    parser.add_argument("--mode", choices=("skip", "overwrite"), default="skip", help="skip 跳过已有测量值；overwrite 重新裁剪并覆盖。")
    parser.add_argument("--no-staging", action="store_true", help="只写交付目录，不写中间裁剪结果。")
    args = parser.parse_args()
    if args.source_root is not None:
        args.result_root = args.source_root
    return args


def resolve_reference_shp(reference: Path) -> Path:
    if reference.is_file():
        if reference.suffix.lower() != ".shp":
            raise ValueError(f"参考文件必须是 .shp：{reference}")
        return reference
    shp_paths = sorted(path for path in reference.rglob("*.shp") if path.is_file())
    if not shp_paths:
        raise FileNotFoundError(f"参考目录下没有 .shp：{reference}")
    if len(shp_paths) > 1:
        print(f"[提示] 参考目录有多个 shp，将使用第一个：{shp_paths[0]}")
    return shp_paths[0]


def six_digit_code(value: object) -> str:
    if value is None or pd.isna(value):
        return ""
    text = str(value).strip()
    if text.endswith(".0"):
        text = text[:-2]
    digits = re.sub(r"\D", "", text)
    return digits[:6]


def non_empty_text(value: object) -> str:
    if value is None or pd.isna(value):
        return ""
    text = str(value).strip()
    return "" if text.lower() == "nan" else text


def normalized_code(value: object, max_length: int | None = None) -> str:
    text = non_empty_text(value)
    if text.endswith(".0"):
        text = text[:-2]
    digits = re.sub(r"\D", "", text)
    code = digits if digits else text
    return code[:max_length] if max_length is not None else code


def first_row_value(row, candidates: tuple[str, ...]) -> object:
    lower_lookup = {str(column).lower(): column for column in row.index}
    for candidate in candidates:
        field = candidate if candidate in row.index else lower_lookup.get(candidate.lower())
        if field is None:
            continue
        value = row.get(field)
        if non_empty_text(value):
            return value
    return None


def remove_existing_shapefile(path: Path) -> None:
    stem = path.stem
    for suffix in SHAPEFILE_SUFFIXES:
        related_path = path.with_name(f"{stem}{suffix}")
        if related_path.exists():
            related_path.unlink()


def geometry_schema_type(gdf: gpd.GeoDataFrame) -> str:
    geom_types = set(gdf.geometry.geom_type.dropna())
    if any("Multi" in geom_type for geom_type in geom_types):
        return "MultiPolygon"
    return "Polygon"


def write_final_shp(gdf: gpd.GeoDataFrame, output_path: Path, encoding: str) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    remove_existing_shapefile(output_path)
    schema = dict(FINAL_SCHEMA)
    schema["geometry"] = geometry_schema_type(gdf)
    gdf.to_file(output_path, driver="ESRI Shapefile", encoding=encoding, engine="fiona", schema=schema)


def make_valid_geom(geom):
    if geom is None or geom.is_empty:
        return None
    try:
        if geom.is_valid:
            return geom
    except Exception:
        pass
    try:
        geom = shapely_make_valid(geom) if shapely_make_valid is not None else geom.buffer(0)
    except Exception:
        try:
            geom = geom.buffer(0)
        except Exception:
            return None
    if geom is None or geom.is_empty:
        return None
    return geom


def polygonal_only(geom):
    geom = make_valid_geom(geom)
    if geom is None or geom.is_empty:
        return None
    if isinstance(geom, (Polygon, MultiPolygon)):
        return geom
    if isinstance(geom, GeometryCollection):
        polygons = [part for part in geom.geoms if isinstance(part, (Polygon, MultiPolygon)) and not part.is_empty]
        if not polygons:
            return None
        return unary_union(polygons)
    return None


def iter_polygon_parts(geom) -> Iterable[Polygon]:
    geom = polygonal_only(geom)
    if geom is None:
        return
    if isinstance(geom, Polygon):
        yield geom
    elif isinstance(geom, MultiPolygon):
        for part in geom.geoms:
            if not part.is_empty:
                yield part


def crop_code_from_text(value: object, fill: bool = False) -> str:
    text = non_empty_text(value)
    if text.endswith(".0"):
        text = text[:-2]
    digits = re.sub(r"\D", "", text)
    return digits if digits else text[:9]

def crop_name_from_code(code: str) -> str:
    return code


def crop_name_from_text(value: object, code: str) -> str:
    return code or non_empty_text(value)

def class_to_int(value: object) -> int | None:
    if value is None or pd.isna(value):
        return None
    try:
        return int(float(str(value).strip()))
    except (TypeError, ValueError):
        return None


def crop_info_from_sample(row) -> tuple[str, str]:
    crop_code = crop_code_from_text(first_row_value(row, TBLXDM_FIELD_CANDIDATES), fill=False)
    crop_name = non_empty_text(first_row_value(row, TBLXMC_FIELD_CANDIDATES)) or crop_code
    return crop_name[:50], crop_code[:9]


def reference_code_from_sample(row) -> str:
    return crop_code_from_text(first_row_value(row, TBLXDM_FIELD_CANDIDATES), fill=False)


def crop_info_from_result(row) -> tuple[str, str]:
    code = crop_code_from_text(row.get("TBLXDM") if "TBLXDM" in row.index else None, fill=False)
    name = non_empty_text(row.get("TBLXMC") if "TBLXMC" in row.index else None) or code
    return name[:50], code[:9]

def find_field(columns: Iterable[str], candidates: tuple[str, ...], label: str) -> str:
    columns = list(columns)
    lower_lookup = {str(column).lower(): column for column in columns}
    for candidate in candidates:
        if candidate in columns:
            return candidate
        lower_candidate = candidate.lower()
        if lower_candidate in lower_lookup:
            return lower_lookup[lower_candidate]
    raise ValueError(f"缺少 {label} 字段，候选={candidates}，实际字段={columns}")


def read_common_crs(result_entries: list[Path], boundary_shp: Path) -> CRS:
    if result_entries:
        with fiona.open(result_entries[0]) as src:
            if src.crs_wkt:
                return CRS.from_wkt(src.crs_wkt)
            if src.crs:
                return CRS.from_user_input(src.crs)
    boundary = gpd.read_file(boundary_shp, rows=1)
    if boundary.crs is None:
        raise ValueError(f"新边界缺少 CRS：{boundary_shp}")
    return CRS.from_user_input(boundary.crs)


def load_city_units(city_boundary_ref: Path | None, common_crs: CRS) -> list[dict[str, object]]:
    if city_boundary_ref is None or not city_boundary_ref.exists():
        return []
    city_shp = resolve_reference_shp(city_boundary_ref)
    city_boundary = gpd.read_file(city_shp)
    if city_boundary.empty or city_boundary.crs is None:
        return []
    if CRS.from_user_input(city_boundary.crs) != common_crs:
        city_boundary = city_boundary.to_crs(common_crs)
    name_field = find_field(city_boundary.columns, CITY_BOUNDARY_NAME_FIELDS, "市名称")
    city_units: list[dict[str, object]] = []
    for row_index, row in city_boundary.iterrows():
        geom = polygonal_only(row.geometry)
        if geom is None:
            continue
        name = non_empty_text(row.get(name_field)) or f"{city_shp.stem}_{row_index + 1}"
        city_units.append({"city": name, "geometry": geom})
    return city_units


def city_name_from_boundary(county_geom, city_units: list[dict[str, object]], fallback: str) -> str:
    best_city = ""
    best_area = 0.0
    for city_unit in city_units:
        overlap_area = geometry_overlap_area(county_geom, city_unit["geometry"])
        if overlap_area > best_area:
            best_area = overlap_area
            best_city = str(city_unit["city"])
    return best_city or fallback


def load_boundary_units(boundary_ref: Path, common_crs: CRS, city_boundary_ref: Path | None = None) -> tuple[dict[tuple[str, str], BoundaryUnit], dict[str, list[BoundaryUnit]]]:
    boundary_shp = resolve_reference_shp(boundary_ref)
    boundary = gpd.read_file(boundary_shp)
    if boundary.empty:
        raise ValueError(f"新边界为空：{boundary_shp}")
    if boundary.crs is None:
        raise ValueError(f"新边界缺少 CRS：{boundary_shp}")
    if CRS.from_user_input(boundary.crs) != common_crs:
        boundary = boundary.to_crs(common_crs)

    name_field = find_field(
        boundary.columns,
        ("area_name", "QXMC", "区县名称_", "区县名称", "县名称", "县级名称", "NAME", "name"),
        "区县名称",
    )
    code_field = find_field(
        boundary.columns,
        ("area_code", "QXDM", "区县代码_", "区县代码", "县代码", "县级代码", "XZQDM"),
        "区县代码",
    )

    city_units = load_city_units(city_boundary_ref, common_crs)
    by_city_name: dict[tuple[str, str], BoundaryUnit] = {}
    by_name: dict[str, list[BoundaryUnit]] = {}
    for _, row in boundary.iterrows():
        geom = polygonal_only(row.geometry)
        if geom is None:
            continue
        code = six_digit_code(row.get(code_field))
        name = non_empty_text(row.get(name_field))
        if not code or not name:
            continue
        city = city_name_from_boundary(geom, city_units, CITY_BY_PREFIX.get(code[:4], ""))
        unit = BoundaryUnit(city=city, name=name, code=code, bounds=geom.bounds, geometry=geom)
        by_city_name[(city, name)] = unit
        by_name.setdefault(name, []).append(unit)
    return by_city_name, by_name


def parse_result_name(path: Path) -> tuple[str, str]:
    folder_name = path.parent.name
    match = re.match(r"^(\d{6})(.+)$", folder_name)
    if match:
        return match.group(1), match.group(2)
    stem = re.sub(r"^\d{6}", "", path.stem)
    return "", stem


def transform_bounds(bounds: tuple[float, float, float, float], source_crs, target_crs: CRS) -> tuple[float, float, float, float]:
    if not source_crs or CRS.from_user_input(source_crs) == target_crs:
        return bounds
    geom = gpd.GeoSeries([box(*bounds)], crs=source_crs).to_crs(target_crs).iloc[0]
    return geom.bounds


def collect_result_entries(result_root: Path, common_crs: CRS) -> list[ResultEntry]:
    entries: list[ResultEntry] = []
    for shp_path in sorted(path for path in result_root.rglob("*.shp") if path.is_file()):
        try:
            with fiona.open(shp_path) as src:
                source_crs = src.crs_wkt or src.crs
                bounds = transform_bounds(tuple(src.bounds), source_crs, common_crs)
            gdf = gpd.read_file(shp_path)
            if gdf.empty:
                continue
            if gdf.crs is None:
                print(f"[跳过] 结果矢量缺少 CRS：{shp_path}", file=sys.stderr)
                continue
            if CRS.from_user_input(gdf.crs) != common_crs:
                gdf = gdf.to_crs(common_crs)
            gdf["geometry"] = gdf.geometry.apply(polygonal_only)
            gdf = gdf[gdf.geometry.notna() & ~gdf.geometry.is_empty].copy()
            if gdf.empty:
                continue
            geom = polygonal_only(unary_union(gdf.geometry))
            if geom is None:
                continue
        except Exception as exc:
            print(f"[跳过] 无法读取结果范围：{shp_path}；原因：{exc}", file=sys.stderr)
            continue
        code, name = parse_result_name(shp_path)
        entries.append(ResultEntry(path=str(shp_path), code=code, name=name, bounds=bounds, geometry=geom))
    if not entries:
        raise FileNotFoundError(f"结果目录下没有可用 shp：{result_root}")
    return entries


def sample_county_name(sample_path: Path) -> str:
    stem = sample_path.stem
    return stem[:-3] if stem.endswith("_样方") else stem


def sample_matches_filter(city: str, county: str, filters: list[str] | None) -> bool:
    if not filters:
        return True
    for value in filters:
        text = str(value).strip()
        if not text:
            continue
        if "-" in text:
            filter_city, filter_county = text.split("-", 1)
            if city == filter_city.strip() and county == filter_county.strip():
                return True
        elif county == text:
            return True
    return False


def resolve_boundary_unit(city: str, county: str, by_city_name, by_name) -> BoundaryUnit:
    unit = by_city_name.get((city, county))
    if unit is not None:
        return unit
    candidates = by_name.get(county, [])
    if len(candidates) == 1:
        return candidates[0]
    if candidates:
        names = ", ".join(f"{item.city}-{item.name}-{item.code}" for item in candidates)
        raise ValueError(f"区县名重复，请用城市目录区分：{county}；候选：{names}")
    raise ValueError(f"新边界中找不到样方对应区县：{city}-{county}")


def bbox_intersects(a: tuple[float, float, float, float], b: tuple[float, float, float, float]) -> bool:
    return not (a[2] < b[0] or a[0] > b[2] or a[3] < b[1] or a[1] > b[3])


def geometry_overlap_area(a, b) -> float:
    if a is None or b is None or not bbox_intersects(a.bounds, b.bounds):
        return 0.0
    try:
        return float(a.intersection(b).area)
    except Exception:
        valid_a = make_valid_geom(a)
        valid_b = make_valid_geom(b)
        if valid_a is None or valid_b is None:
            return 0.0
        return float(valid_a.intersection(valid_b).area)


def assign_results_to_boundary_units(
    entries: list[ResultEntry],
    units: list[BoundaryUnit],
) -> dict[str, list[ResultEntry]]:
    assigned: dict[str, list[ResultEntry]] = {}
    for entry in entries:
        best_unit: BoundaryUnit | None = None
        best_area = 0.0
        for unit in units:
            overlap_area = geometry_overlap_area(entry.geometry, unit.geometry)
            if overlap_area > best_area:
                best_area = overlap_area
                best_unit = unit
        if best_unit is not None and best_area > 0:
            assigned.setdefault(best_unit.code, []).append(entry)
            print(f"[匹配] {Path(entry.path).name} -> {best_unit.city}-{best_unit.name}-{best_unit.code} overlap={best_area:.2f}")
    return assigned


def choose_result_paths(unit: BoundaryUnit, entries: list[ResultEntry], match_mode: str) -> tuple[str, ...]:
    exact: list[ResultEntry] = []
    if match_mode in ("auto", "exact"):
        exact = [entry for entry in entries if entry.code == unit.code or (entry.name and entry.name == unit.name)]
        if exact or match_mode == "exact":
            return tuple(entry.path for entry in exact)
    spatial = [entry for entry in entries if geometry_overlap_area(entry.geometry, unit.geometry) > 0]
    return tuple(entry.path for entry in spatial)


def batch_output_path(sample_path: Path, sample_dir: Path, staging_dir: Path, output_suffix: str) -> Path:
    relative_path = sample_path.relative_to(sample_dir)
    return staging_dir / relative_path.parent / f"{sample_path.stem}{output_suffix}.shp"


def delivery_output_path(delivery_dir: Path, county_code: str) -> Path:
    return delivery_dir / county_code / f"KFJDCZYB{county_code}_2026_XL.shp"


def build_tasks(args: argparse.Namespace, common_crs: CRS, result_entries: list[ResultEntry]) -> list[ClipTask]:
    print(f"[03] 开始构建裁剪任务：result_entries={len(result_entries)}", flush=True)
    by_city_name, by_name = load_boundary_units(args.boundary_ref, common_crs, args.city_boundary_ref)
    all_units = list({unit.code: unit for unit in list(by_city_name.values()) + [item for values in by_name.values() for item in values]}.values())
    result_entries_by_county = assign_results_to_boundary_units(result_entries, all_units)
    sample_paths = sorted(path for path in args.sample_dir.rglob(args.sample_pattern) if path.is_file() and "样点" not in path.stem)
    print(f"[03] 找到样方文件数：{len(sample_paths)}；样方目录={args.sample_dir}", flush=True)
    if not sample_paths:
        raise FileNotFoundError(f"样方目录下没有匹配到 {args.sample_pattern}：{args.sample_dir}")

    staging_dir = args.staging_dir or args.output_root / DEFAULT_STAGING_DIR_NAME
    delivery_dir = args.delivery_dir or args.output_root / DEFAULT_DELIVERY_DIR
    tasks: list[ClipTask] = []
    for sample_path in sample_paths:
        city = sample_path.parent.name
        county = sample_county_name(sample_path)
        if not sample_matches_filter(city, county, args.only_boundary_name):
            continue
        print(f"[03] 准备样方任务：{city}-{county}，文件={sample_path.name}", flush=True)
        unit = resolve_boundary_unit(city, county, by_city_name, by_name)
        county_entries = result_entries_by_county.get(unit.code, [])
        print(f"[03] 匹配边界：{unit.name}-{unit.code}；候选结果文件数={len(county_entries)}", flush=True)
        result_paths = choose_result_paths(unit, county_entries, args.result_match)
        if not result_paths:
            print(f"[跳过] 未匹配到结果矢量：{city}-{county}-{unit.code}", file=sys.stderr)
            continue
        staging_path = batch_output_path(sample_path, args.sample_dir, staging_dir, args.output_suffix)
        delivery_path = delivery_output_path(delivery_dir, unit.code)
        if args.mode == "skip" and delivery_path.exists():
            print(f"[跳过] 测量值已存在：{delivery_path}")
            continue
        print(f"[03] 新增裁剪任务：输出={delivery_path}", flush=True)
        tasks.append(
            ClipTask(
                sample_path=str(sample_path),
                relative_parent=str(sample_path.relative_to(args.sample_dir).parent),
                city=city,
                county=county,
                qxmc=unit.name,
                qxdm=unit.code,
                result_paths=result_paths,
                staging_path=str(staging_path),
                delivery_path=str(delivery_path),
                encoding=args.encoding,
                input_encoding=args.input_encoding,
                area_epsilon=args.area_epsilon,
                write_staging=not args.no_staging,
            )
        )
    print(f"[03] 裁剪任务构建完成：tasks={len(tasks)}", flush=True)
    return tasks


def read_sample(sample_path: Path) -> gpd.GeoDataFrame:
    sample = gpd.read_file(sample_path)
    if sample.empty:
        raise ValueError(f"样方为空：{sample_path}")
    if sample.crs is None:
        raise ValueError(f"样方缺少 CRS：{sample_path}")
    sample = sample.copy()
    sample["geometry"] = sample.geometry.apply(polygonal_only)
    sample = sample[sample.geometry.notna() & ~sample.geometry.is_empty].copy()
    if sample.empty:
        raise ValueError(f"样方没有有效面：{sample_path}")
    return sample.reset_index(drop=True)


def read_result_layer(path: Path, bbox_value: tuple[float, float, float, float], target_crs, input_encoding: str | None) -> gpd.GeoDataFrame:
    read_bbox = bbox_value
    try:
        with fiona.open(path) as src:
            source_crs = src.crs_wkt or src.crs
        if source_crs and target_crs is not None:
            source_crs_obj = CRS.from_user_input(source_crs)
            target_crs_obj = CRS.from_user_input(target_crs)
            if source_crs_obj != target_crs_obj:
                read_bbox = transform_bounds(bbox_value, target_crs_obj, source_crs_obj)
    except Exception:
        read_bbox = bbox_value

    kwargs = {"bbox": read_bbox}
    if input_encoding:
        kwargs["encoding"] = input_encoding
    gdf = gpd.read_file(path, **kwargs)
    if gdf.empty:
        return gpd.GeoDataFrame(columns=["TBLXMC", "TBLXDM", "geometry"], geometry="geometry", crs=target_crs)
    if gdf.crs is None:
        raise ValueError(f"结果矢量缺少 CRS：{path}")
    if target_crs is not None and CRS.from_user_input(gdf.crs) != CRS.from_user_input(target_crs):
        gdf = gdf.to_crs(target_crs)
    tblxdm_field = find_field(gdf.columns, ("TBLXDM",), "TBLXDM")
    tblxmc_field = find_field(gdf.columns, ("TBLXMC",), "TBLXMC")
    gdf = gdf[[tblxmc_field, tblxdm_field, "geometry"]].rename(columns={tblxmc_field: "TBLXMC", tblxdm_field: "TBLXDM"}).copy()
    gdf["geometry"] = gdf.geometry.apply(polygonal_only)
    gdf = gdf[gdf.geometry.notna() & ~gdf.geometry.is_empty].copy()
    return gdf


def load_results(result_paths: Iterable[str], bbox_value: tuple[float, float, float, float], target_crs, input_encoding: str | None) -> gpd.GeoDataFrame:
    parts: list[gpd.GeoDataFrame] = []
    for path_text in result_paths:
        path = Path(path_text)
        try:
            part = read_result_layer(path, bbox_value, target_crs, input_encoding)
        except Exception as exc:
            print(f"[跳过] 读取结果失败：{path}；原因：{exc}", file=sys.stderr)
            continue
        if not part.empty:
            part["src_file"] = path.name
            parts.append(part)
    if not parts:
        return gpd.GeoDataFrame(columns=["TBLXMC", "TBLXDM", "src_file", "geometry"], geometry="geometry", crs=target_crs)
    return gpd.GeoDataFrame(pd.concat(parts, ignore_index=True, sort=False), geometry="geometry", crs=parts[0].crs)


def build_remark(qxdm: str, cundm: str, existing_remark: object = None) -> str:
    remarks = []
    existing = non_empty_text(existing_remark)
    if existing:
        remarks.append(existing)
    cundm_prefix = normalized_code(cundm, 6)
    qxdm_code = normalized_code(qxdm, 6)
    if cundm_prefix and qxdm_code and cundm_prefix != qxdm_code and not any("村代码有误" in item for item in remarks):
        remarks.append("村代码有误")
    return "；".join(remarks)[:100]


def make_output_row(qxmc: str, qxdm: str, sample_row, geom, crop_override: tuple[str, str] | None = None) -> dict[str, object]:
    area = round(float(geom.area), 2)
    cundm = normalized_code(first_row_value(sample_row, CUNDM_FIELD_CANDIDATES), 12)
    crop_name, crop_code = crop_override or crop_info_from_sample(sample_row)
    if not non_empty_text(crop_code):
        crop_name, crop_code = crop_info_from_sample(sample_row)
    return {
        "QXMC": qxmc,
        "QXDM": qxdm,
        "CUNMC": non_empty_text(first_row_value(sample_row, CUNMC_FIELD_CANDIDATES))[:100],
        "CUNDM": cundm,
        "YGCUNDM": "",
        "TBLXMC": crop_name,
        "TBLXDM": crop_code,
        "TBMJ": area,
        "CLYX": CLYX_VALUE,
        "BZ": build_remark(qxdm, cundm, sample_row.get("BZ") if "BZ" in sample_row.index else None),
        "geometry": geom,
    }


def clip_samples_to_results(sample: gpd.GeoDataFrame, results: gpd.GeoDataFrame, task: ClipTask) -> gpd.GeoDataFrame:
    rows: list[dict[str, object]] = []
    if not results.empty:
        spatial_index = results.sindex
    else:
        spatial_index = None

    for sample_index, sample_row in sample.iterrows():
        sample_geom = polygonal_only(sample_row.geometry)
        if sample_geom is None:
            continue
        occupied_geoms = []

        if spatial_index is not None:
            try:
                candidate_indices = spatial_index.query(sample_geom, predicate="intersects")
            except Exception:
                candidate_indices = results[results.intersects(sample_geom)].index
            for result_index in list(candidate_indices):
                result_row = results.iloc[int(result_index)]
                result_geom = polygonal_only(result_row.geometry)
                if result_geom is None:
                    continue
                result_crop = crop_info_from_result(result_row)
                try:
                    clipped_geom = polygonal_only(result_geom.intersection(sample_geom))
                except Exception:
                    clipped_geom = polygonal_only(make_valid_geom(result_geom).intersection(make_valid_geom(sample_geom)))
                if clipped_geom is None or clipped_geom.area <= task.area_epsilon:
                    continue
                for part in iter_polygon_parts(clipped_geom):
                    if part.area <= task.area_epsilon:
                        continue
                    occupied_geoms.append(part)
                    rows.append(make_output_row(task.qxmc, task.qxdm, sample_row, part, result_crop))

        if occupied_geoms:
            try:
                fill_geom = polygonal_only(sample_geom.difference(unary_union(occupied_geoms)))
            except Exception:
                valid_parts = [make_valid_geom(geom) for geom in occupied_geoms if geom is not None and not geom.is_empty]
                fill_geom = polygonal_only(make_valid_geom(sample_geom).difference(unary_union(valid_parts)))
        else:
            fill_geom = sample_geom

        if fill_geom is not None and fill_geom.area > task.area_epsilon:
            for part in iter_polygon_parts(fill_geom):
                if part.area <= task.area_epsilon:
                    continue
                rows.append(make_output_row(task.qxmc, task.qxdm, sample_row, part, crop_info_from_sample(sample_row)))

    output = gpd.GeoDataFrame(rows, geometry="geometry", crs=sample.crs)
    if output.empty:
        return gpd.GeoDataFrame(columns=list(FINAL_FIELDS) + ["geometry"], geometry="geometry", crs=sample.crs)
    for field in FINAL_TEXT_FIELDS:
        output[field] = output[field].where(output[field].isna(), output[field].astype(str)).astype("object")
    for field in FINAL_FLOAT_FIELDS:
        output[field] = pd.to_numeric(output[field], errors="coerce").astype("float64")
    return output[list(FINAL_FIELDS) + ["geometry"]]


def process_task(task: ClipTask) -> dict[str, object]:
    print(f"[03] 开始处理任务：{task.city}-{task.county} -> {task.delivery_path}", flush=True)
    warnings.filterwarnings("ignore", category=UserWarning)
    sample_path = Path(task.sample_path)
    sample = read_sample(sample_path)
    print(f"[03] 样方读取完成：{sample_path.name}，features={len(sample)}", flush=True)
    bbox_value = tuple(sample.total_bounds)
    results = load_results(task.result_paths, bbox_value, sample.crs, task.input_encoding)
    output = clip_samples_to_results(sample, results, task)
    output = output[output.geometry.notna() & ~output.geometry.is_empty].copy()
    print(f"[03] 裁剪输出要素数：{len(output)}", flush=True)
    if output.empty:
        raise ValueError(f"裁剪结果为空：{sample_path}")

    delivery_path = Path(task.delivery_path)
    write_final_shp(output, delivery_path, task.encoding)
    if task.write_staging:
        write_final_shp(output, Path(task.staging_path), task.encoding)
    return {
        "sample": task.sample_path,
        "delivery": task.delivery_path,
        "staging": task.staging_path if task.write_staging else "",
        "features": len(output),
        "result_files": len(task.result_paths),
    }


def main() -> int:
    args = parse_args()
    print(f"[03] result_root={args.result_root}；sample_dir={args.sample_dir}；output_root={args.output_root}", flush=True)
    if not args.result_root.exists():
        raise FileNotFoundError(f"结果目录不存在：{args.result_root}")
    if not args.sample_dir.exists():
        raise FileNotFoundError(f"样方目录不存在：{args.sample_dir}")
    args.output_root.mkdir(parents=True, exist_ok=True)

    result_shps = sorted(path for path in args.result_root.rglob("*.shp") if path.is_file())
    print(f"[03] 发现结果 shp 数：{len(result_shps)}", flush=True)
    if not result_shps:
        raise FileNotFoundError(f"结果目录下没有 shp：{args.result_root}")
    common_crs = read_common_crs(result_shps, resolve_reference_shp(args.boundary_ref))
    result_entries = collect_result_entries(args.result_root, common_crs)
    tasks = build_tasks(args, common_crs, result_entries)
    if not tasks:
        print("[跳过模式] 没有需要新增裁剪的测量值。")
        return 0

    print(f"[准备] 结果 shp={len(result_entries)}；样方任务={len(tasks)}；workers={args.workers}")
    success_count = 0
    failed: list[tuple[str, str]] = []
    with concurrent.futures.ProcessPoolExecutor(max_workers=max(1, args.workers)) as executor:
        future_to_task = {executor.submit(process_task, task): task for task in tasks}
        for index, future in enumerate(concurrent.futures.as_completed(future_to_task), start=1):
            task = future_to_task[future]
            try:
                info = future.result()
                success_count += 1
                print(
                    f"[完成 {index}/{len(tasks)}] {task.city}-{task.county} "
                    f"features={info['features']} result_files={info['result_files']} -> {info['delivery']}"
                )
            except Exception as exc:
                failed.append((f"{task.city}-{task.county}", str(exc)))
                print(f"[失败 {index}/{len(tasks)}] {task.city}-{task.county}：{exc}", file=sys.stderr)

    print(f"Done. Success={success_count}, failed={len(failed)}")
    if failed:
        print("Failed list:")
        for name, reason in failed:
            print(f"  {name}: {reason}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
