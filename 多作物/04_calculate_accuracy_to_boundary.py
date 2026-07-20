#!/usr/bin/env python
# -*- coding: utf-8 -*-
from __future__ import annotations

import argparse
import gc
import json
import math
import re
import sys
from datetime import date
from pathlib import Path
from typing import Iterable

import fiona
import geopandas as gpd
import pandas as pd
from shapely.geometry import GeometryCollection, MultiPolygon, Polygon
from shapely.ops import unary_union

try:
    from shapely import make_valid as shapely_make_valid
except ImportError:  # Shapely < 2.0
    shapely_make_valid = None


SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_TRUTH_ROOT = SCRIPT_DIR / "02参考真值"
DEFAULT_MEASURE_ROOT = SCRIPT_DIR / "03测量值"
DEFAULT_BOUNDARY_SHP = SCRIPT_DIR / "00县边界"
DEFAULT_RESULT_ROOT = SCRIPT_DIR / "04精度评价"
DEFAULT_BOUNDARY_OUTPUT = DEFAULT_RESULT_ROOT / "精度评价边界.shp"
DEFAULT_CSV_OUTPUT = DEFAULT_RESULT_ROOT / "精度评价汇总.csv"
DEFAULT_CROP_CODE_PATH = SCRIPT_DIR / "zuowucode.json"

TRUTH_CODE_FIELD = "CKDWLX"
MEASURE_CODE_FIELD = "ZWDM"
COUNTY_NAME_FIELD = "QXMC"
COUNTY_CODE_FIELD = "QXDM"
REVIEWER_FIELD = "PJR"
REVIEW_DATE_FIELD = "PJRQ"
IGNORED_CODES = {"", "0", "000", "900", "901", "None", "none", "nan", "<NA>"}
SHAPEFILE_SUFFIXES = (".shp", ".shx", ".dbf", ".prj", ".cpg", ".qix", ".fix", ".sbn", ".sbx", ".shp.xml")
METRIC_FIELD_PATTERN = re.compile(r"^(MA|IoU)(.+)$", re.IGNORECASE)
DEFAULT_MA_CAP_CODE = ""
DEFAULT_MA_CAP_MAX = None
EXCLUDED_OUTPUT_CODES: set[str] = set()
REQUIRED_OUTPUT_CODES: set[str] = set()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="0627流程适配：按 CKDWLX 真值和 ZWDM 测量值计算 MA/IoU，写回新边界 shp 并导出 CSV。"
    )
    parser.add_argument("--truth-root", type=Path, default=DEFAULT_TRUTH_ROOT, help="参考真值样本目录；默认 自检样方/02参考真值。")
    parser.add_argument("--measure-root", type=Path, default=DEFAULT_MEASURE_ROOT, help="测量结果目录；默认 自检样方/03测量值。")
    parser.add_argument("--boundary-shp", type=Path, default=DEFAULT_BOUNDARY_SHP, help="县边界 shp 或目录；默认 自检样方/00县边界。")
    parser.add_argument("--boundary-output", type=Path, default=DEFAULT_BOUNDARY_OUTPUT, help="边界结果另存路径；默认 自检样方/04精度评价/精度评价边界.shp。")
    parser.add_argument("--csv-output", type=Path, default=DEFAULT_CSV_OUTPUT, help="CSV 汇总输出路径；默认 自检样方/04精度评价/精度评价汇总.csv。")
    parser.add_argument("--reviewer", default="", help="精度评价人 PJR。")
    parser.add_argument("--review-date", default=date.today().strftime("%Y%m%d"), help="精度评价日期 PJRQ，默认今天。")
    parser.add_argument("--encoding", default="UTF-8", help="写出 Shapefile/CSV 编码，默认 UTF-8。")
    parser.add_argument("--input-encoding", default=None, help="读取 Shapefile 编码，中文异常时可试 GBK。")
    parser.add_argument("--area-epsilon", type=float, default=0.0, help="忽略面积小于等于该值的碎小面。")
    parser.add_argument("--ma-cap-code", default=DEFAULT_MA_CAP_CODE, help="需要限制 MA 上限的作物代码；默认关闭。")
    parser.add_argument("--ma-cap-max", type=float, default=DEFAULT_MA_CAP_MAX, help="MA 上限；默认关闭。")
    parser.add_argument("--crop-code-json", type=Path, default=DEFAULT_CROP_CODE_PATH, help="7 种作物代码 JSON，格式为 作物名: 作物代码；默认 zuowucode.json。")
    parser.add_argument("--mode", choices=("skip", "overwrite"), default="skip", help="skip 跳过已有 MA/IoU 的区县；overwrite 重算并覆盖已有精度。")
    parser.add_argument("--dry-run", action="store_true", help="只计算并打印概览，不写回 shp/csv。")
    return parser.parse_args()


def read_vector(path: Path, input_encoding: str | None = None) -> gpd.GeoDataFrame:
    if input_encoding:
        return gpd.read_file(path, encoding=input_encoding)
    return gpd.read_file(path)


def non_empty_text(value: object) -> str:
    if value is None or pd.isna(value):
        return ""
    text = str(value).strip()
    return "" if text.lower() == "nan" else text


def six_digit_code(value: object) -> str:
    text = non_empty_text(value)
    if text.endswith(".0"):
        text = text[:-2]
    digits = re.sub(r"\D", "", text)
    return digits[:6]


def crop_code(value: object) -> str:
    text = non_empty_text(value)
    if text.endswith(".0"):
        text = text[:-2]
    digits = re.sub(r"\D", "", text)
    if digits:
        return digits
    return text


def load_target_crop_codes(path: Path) -> list[str]:
    path = Path(path)
    if not path.exists() and path.name == "zuowucode.json":
        alternate = path.with_name("zuowudaima.json")
        if alternate.exists():
            path = alternate
    if not path.exists():
        raise FileNotFoundError(f"作物代码 JSON 不存在: {path}")
    with path.open("r", encoding="utf-8") as handle:
        data = json.load(handle)
    if not isinstance(data, dict):
        raise ValueError(f"作物代码 JSON 必须是 作物名->代码 的对象: {path}")
    codes: list[str] = []
    for value in data.values():
        code = crop_code(value)
        if code and is_metric_code(code) and code not in codes:
            codes.append(code)
    if not codes:
        raise ValueError(f"作物代码 JSON 没有有效作物代码: {path}")
    return codes


def is_metric_code(code: str) -> bool:
    return crop_code(code) not in IGNORED_CODES


def metric_field_names(code: str) -> tuple[str, str]:
    code = crop_code(code)
    ma_field = f"MA{code}"
    iou_field = f"IoU{code}"
    if len(ma_field) > 10 or len(iou_field) > 10:
        raise ValueError(f"作物代码过长，无法写入 Shapefile 10 字段名限制：{code}")
    return ma_field, iou_field


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


def normalize_geometry(gdf: gpd.GeoDataFrame) -> gpd.GeoDataFrame:
    gdf = gdf.copy()
    gdf["geometry"] = gdf.geometry.apply(polygonal_only)
    return gdf[gdf.geometry.notna() & ~gdf.geometry.is_empty].copy().reset_index(drop=True)


def union_geometry(geoms: Iterable):
    valid_geoms = [polygonal_only(geom) for geom in geoms if geom is not None and not geom.is_empty]
    valid_geoms = [geom for geom in valid_geoms if geom is not None and not geom.is_empty]
    if not valid_geoms:
        return None
    return polygonal_only(unary_union(valid_geoms))


def query_intersections(gdf: gpd.GeoDataFrame, geom) -> gpd.GeoDataFrame:
    if gdf.empty:
        return gdf.iloc[[]].copy()
    try:
        indices = gdf.sindex.query(geom, predicate="intersects")
    except Exception:
        indices = gdf[gdf.intersects(geom)].index
    return gdf.iloc[list(indices)].copy()


def clip_to_scope(gdf: gpd.GeoDataFrame, scope_geom, area_epsilon: float) -> gpd.GeoDataFrame:
    scope_geom = polygonal_only(scope_geom)
    if scope_geom is None or gdf.empty:
        return gdf.iloc[[]].copy()
    rows: list[dict[str, object]] = []
    for _, row in query_intersections(gdf, scope_geom).iterrows():
        geom = polygonal_only(row.geometry)
        if geom is None:
            continue
        try:
            clipped = polygonal_only(geom.intersection(scope_geom))
        except Exception:
            clipped = polygonal_only(make_valid_geom(geom).intersection(make_valid_geom(scope_geom)))
        if clipped is None or clipped.area <= area_epsilon:
            continue
        record = row.drop(labels="geometry").to_dict()
        record["geometry"] = clipped
        rows.append(record)
    if not rows:
        return gpd.GeoDataFrame(columns=gdf.columns, geometry="geometry", crs=gdf.crs)
    return gpd.GeoDataFrame(rows, geometry="geometry", crs=gdf.crs).reset_index(drop=True)


def collect_shps(root: Path) -> list[Path]:
    if not root.exists():
        raise FileNotFoundError(f"目录不存在：{root}")
    return sorted(path for path in root.rglob("*.shp") if path.is_file())


def code_from_path(path: Path) -> str:
    for part in reversed(path.parts):
        code = six_digit_code(part)
        if code:
            return code
    return ""


def collect_by_county(root: Path) -> dict[str, list[Path]]:
    by_code: dict[str, list[Path]] = {}
    for shp_path in collect_shps(root):
        code = code_from_path(shp_path)
        if not code:
            print(f"[跳过] 文件路径中未识别到 6 位区县代码：{shp_path}", file=sys.stderr)
            continue
        by_code.setdefault(code, []).append(shp_path)
    return by_code


def resolve_boundary_shp(boundary_ref: Path) -> Path:
    if boundary_ref.is_file():
        if boundary_ref.suffix.lower() != ".shp":
            raise ValueError(f"边界文件必须是 shp：{boundary_ref}")
        return boundary_ref
    if not boundary_ref.exists():
        raise FileNotFoundError(f"新边界 shp/目录不存在：{boundary_ref}")
    shp_paths = sorted(path for path in boundary_ref.rglob("*.shp") if path.is_file())
    if not shp_paths:
        raise FileNotFoundError(f"新边界目录下没有 shp：{boundary_ref}")
    if len(shp_paths) > 1:
        print(f"[提示] 新边界目录有多个 shp，将使用第一个：{shp_paths[0]}")
    return shp_paths[0]


def read_layers(paths: list[Path], required_field: str, target_crs, input_encoding: str | None) -> gpd.GeoDataFrame:
    parts: list[gpd.GeoDataFrame] = []
    for path in paths:
        gdf = normalize_geometry(read_vector(path, input_encoding))
        if gdf.empty:
            print(f"[跳过] 空图层：{path}", file=sys.stderr)
            continue
        if required_field not in gdf.columns:
            raise ValueError(f"缺少字段 {required_field}：{path}")
        if gdf.crs is None:
            raise ValueError(f"缺少 CRS：{path}")
        if target_crs is not None and gdf.crs != target_crs:
            gdf = gdf.to_crs(target_crs)
        gdf[required_field] = gdf[required_field].apply(crop_code)
        parts.append(gdf[[required_field, "geometry"]])
    if not parts:
        return gpd.GeoDataFrame(columns=[required_field, "geometry"], geometry="geometry", crs=target_crs)
    return gpd.GeoDataFrame(pd.concat(parts, ignore_index=True, sort=False), geometry="geometry", crs=target_crs)


def compute_area_accuracy(reference_area: float, measure_area: float) -> float | None:
    if reference_area <= 0:
        return None
    return (1 - abs(1 - measure_area / reference_area)) * 100


def compute_iou(reference_geom, measure_geom) -> float | None:
    if reference_geom is None and measure_geom is None:
        return None
    if reference_geom is None or measure_geom is None:
        return 0.0
    try:
        intersection_area = float(reference_geom.intersection(measure_geom).area)
        union_area = float(reference_geom.union(measure_geom).area)
    except Exception:
        reference_geom = make_valid_geom(reference_geom)
        measure_geom = make_valid_geom(measure_geom)
        intersection_area = float(reference_geom.intersection(measure_geom).area)
        union_area = float(reference_geom.union(measure_geom).area)
    if union_area <= 0:
        return None
    return intersection_area / union_area * 100



def compute_county_metrics(
    county_code: str,
    truth_paths: list[Path],
    measure_paths: list[Path],
    area_crs,
    input_encoding: str | None,
    area_epsilon: float,
) -> tuple[dict[str, object], dict[str, dict[str, float | None]]]:
    truth = read_layers(truth_paths, TRUTH_CODE_FIELD, area_crs, input_encoding)
    measure = read_layers(measure_paths, MEASURE_CODE_FIELD, area_crs, input_encoding)
    if truth.empty:
        raise ValueError(f"真值图层为空：{county_code}")

    truth_scope = union_geometry(truth.geometry)
    measure = clip_to_scope(measure, truth_scope, area_epsilon)
    codes = sorted(
        {
            code
            for code in list(truth[TRUTH_CODE_FIELD].dropna().unique()) + list(measure[MEASURE_CODE_FIELD].dropna().unique())
            if is_metric_code(str(code))
        },
        key=lambda value: (len(str(value)), str(value)),
    )

    metrics: dict[str, object] = {}
    detail: dict[str, dict[str, float | None]] = {}
    for code in codes:
        ma_field, iou_field = metric_field_names(code)
        reference = truth[truth[TRUTH_CODE_FIELD] == code]
        measured = measure[measure[MEASURE_CODE_FIELD] == code]
        reference_geom = union_geometry(reference.geometry)
        measure_geom = union_geometry(measured.geometry)
        reference_area = float(reference_geom.area) if reference_geom is not None else 0.0
        measure_area = float(measure_geom.area) if measure_geom is not None else 0.0
        ma_value = compute_area_accuracy(reference_area, measure_area)
        iou_value = compute_iou(reference_geom, measure_geom)
        metrics[ma_field] = ma_value
        metrics[iou_field] = iou_value
        inter_area = None
        union_area = None
        if reference_geom is not None and measure_geom is not None:
            inter_area = float(reference_geom.intersection(measure_geom).area)
            union_area = float(reference_geom.union(measure_geom).area)
        detail[code] = {
            "reference_area": reference_area,
            "measure_area": measure_area,
            "intersection_area": inter_area,
            "union_area": union_area,
            "MA": ma_value,
            "IoU": iou_value,
        }
    return metrics, detail


def apply_ma_cap(
    metrics: dict[str, object],
    details: dict[str, dict[str, float | None]],
    cap_code: str,
    cap_max: float | None,
) -> None:
    code = crop_code(cap_code)
    if not code or cap_max is None:
        return
    ma_field, _ = metric_field_names(code)
    value = metrics.get(ma_field)
    if value is not None and not pd.isna(value):
        metrics[ma_field] = min(float(value), float(cap_max))
    if code in details:
        detail_value = details[code].get("MA")
        if detail_value is not None and not pd.isna(detail_value):
            details[code]["MA"] = min(float(detail_value), float(cap_max))


def read_boundary(boundary_shp: Path, input_encoding: str | None) -> gpd.GeoDataFrame:
    if not boundary_shp.exists():
        raise FileNotFoundError(f"新边界 shp 不存在：{boundary_shp}")
    boundary = normalize_geometry(read_vector(boundary_shp, input_encoding))
    if boundary.empty:
        raise ValueError(f"新边界 shp 为空：{boundary_shp}")
    if boundary.crs is None:
        raise ValueError(f"新边界 shp 缺少 CRS：{boundary_shp}")

    name_field = "QXMC" if "QXMC" in boundary.columns else "area_name"
    code_field = "QXDM" if "QXDM" in boundary.columns else "area_code"
    if name_field not in boundary.columns or code_field not in boundary.columns:
        raise ValueError(f"新边界缺少区县字段，需有 area_name/area_code 或 QXMC/QXDM：{boundary_shp}")

    boundary = boundary.copy()
    boundary[COUNTY_NAME_FIELD] = boundary[name_field].apply(lambda value: non_empty_text(value)[:30])
    boundary[COUNTY_CODE_FIELD] = boundary[code_field].apply(six_digit_code)

    keep_fields = [COUNTY_NAME_FIELD, COUNTY_CODE_FIELD]
    for field in boundary.columns:
        if field in keep_fields or field == "geometry":
            continue
        if METRIC_FIELD_PATTERN.match(str(field)) or field in (REVIEWER_FIELD, REVIEW_DATE_FIELD):
            keep_fields.append(field)
    return boundary[keep_fields + ["geometry"]].copy()

def metric_codes_from_columns(columns: Iterable[str]) -> set[str]:
    codes: set[str] = set()
    for column in columns:
        match = METRIC_FIELD_PATTERN.match(str(column))
        if match:
            code = crop_code(match.group(2))
            if is_metric_code(code):
                codes.add(code)
    return codes


def metric_columns(columns: Iterable[str]) -> list[str]:
    return [str(column) for column in columns if METRIC_FIELD_PATTERN.match(str(column))]


def already_computed_counties(boundary: gpd.GeoDataFrame) -> set[str]:
    fields = metric_columns(boundary.columns)
    if not fields:
        return set()
    done: set[str] = set()
    for _, row in boundary.iterrows():
        qxdm = non_empty_text(row.get(COUNTY_CODE_FIELD))
        if not qxdm:
            continue
        for field in fields:
            value = row.get(field)
            if value is not None and not pd.isna(value) and str(value).strip() != "":
                done.add(qxdm)
                break
    return done

def round_metric(value: object) -> float | None:
    if value is None or pd.isna(value):
        return None
    if isinstance(value, float) and (math.isnan(value) or math.isinf(value)):
        return None
    return round(float(value), 2)


def build_output_boundary(
    boundary: gpd.GeoDataFrame,
    county_metrics: dict[str, dict[str, object]],
    crop_codes: list[str],
    reviewer: str,
    review_date: str,
    preserve_existing: bool = False,
) -> gpd.GeoDataFrame:
    output = boundary[[COUNTY_NAME_FIELD, COUNTY_CODE_FIELD, "geometry"]].copy()
    metric_fields: list[str] = []
    for code in crop_codes:
        ma_field, iou_field = metric_field_names(code)
        metric_fields.extend([ma_field, iou_field])
        for field in (ma_field, iou_field):
            if preserve_existing and field in boundary.columns:
                output[field] = boundary[field]
            else:
                output[field] = pd.NA

    if preserve_existing and REVIEWER_FIELD in boundary.columns:
        output[REVIEWER_FIELD] = boundary[REVIEWER_FIELD].fillna("").astype(str)
    else:
        output[REVIEWER_FIELD] = ""
    if preserve_existing and REVIEW_DATE_FIELD in boundary.columns:
        output[REVIEW_DATE_FIELD] = boundary[REVIEW_DATE_FIELD].fillna("").astype(str)
    else:
        output[REVIEW_DATE_FIELD] = ""

    for index, row in output.iterrows():
        qxdm = non_empty_text(row[COUNTY_CODE_FIELD])
        metrics = county_metrics.get(qxdm)
        if not metrics:
            continue
        for field in metric_fields:
            output.at[index, field] = round_metric(metrics.get(field))
        output.at[index, REVIEWER_FIELD] = reviewer[:30]
        output.at[index, REVIEW_DATE_FIELD] = review_date[:30]

    for field in metric_fields:
        output[field] = pd.to_numeric(output[field], errors="coerce").astype("float64")
    for field in (COUNTY_NAME_FIELD, COUNTY_CODE_FIELD, REVIEWER_FIELD, REVIEW_DATE_FIELD):
        output[field] = output[field].fillna("").astype(str)
    return output[[COUNTY_NAME_FIELD, COUNTY_CODE_FIELD] + metric_fields + [REVIEWER_FIELD, REVIEW_DATE_FIELD] + ["geometry"]]

def output_metric_schema(code: str) -> str:
    return "float:9.2" if crop_code(code) == "107" else "float:8.2"


def output_schema(crop_codes: list[str]) -> dict[str, object]:
    properties: dict[str, str] = {
        COUNTY_NAME_FIELD: "str:30",
        COUNTY_CODE_FIELD: "str:6",
    }
    for code in crop_codes:
        ma_field, iou_field = metric_field_names(code)
        properties[ma_field] = output_metric_schema(code)
        properties[iou_field] = output_metric_schema(code)
    properties[REVIEWER_FIELD] = "str:30"
    properties[REVIEW_DATE_FIELD] = "str:30"
    return {"geometry": "Polygon", "properties": properties}

def remove_existing_shp(path: Path) -> None:
    stem = path.stem
    for suffix in SHAPEFILE_SUFFIXES:
        component = path.with_name(f"{stem}{suffix}")
        if component.exists():
            component.unlink()


def replace_shp_components(source_path: Path, target_path: Path) -> None:
    for suffix in SHAPEFILE_SUFFIXES:
        source_component = source_path.with_name(f"{source_path.stem}{suffix}")
        target_component = target_path.with_name(f"{target_path.stem}{suffix}")
        if target_component.exists():
            target_component.unlink()
        if source_component.exists():
            source_component.replace(target_component)


def write_boundary(gdf: gpd.GeoDataFrame, output_path: Path, crop_codes: list[str], encoding: str) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    schema = output_schema(crop_codes)
    remove_existing_shp(output_path)
    gdf.to_file(output_path, driver="ESRI Shapefile", encoding=encoding, engine="fiona", schema=schema)


def update_boundary_in_place(
    gdf: gpd.GeoDataFrame,
    boundary_shp: Path,
    boundary_output: Path | None,
    crop_codes: list[str],
    encoding: str,
) -> Path:
    if boundary_output is not None and boundary_output.resolve() != boundary_shp.resolve():
        write_boundary(gdf, boundary_output, crop_codes, encoding)
        return boundary_output

    temp_path = boundary_shp.with_name(f"{boundary_shp.stem}_tmp_accuracy.shp")
    write_boundary(gdf, temp_path, crop_codes, encoding)
    del gdf
    gc.collect()
    replace_shp_components(temp_path, boundary_shp)
    return boundary_shp


def write_csv(
    gdf: gpd.GeoDataFrame,
    csv_path: Path,
    encoding: str,
    county_metrics: dict[str, dict[str, object]] | None = None,
    crop_codes: list[str] | None = None,
) -> None:
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    table = pd.DataFrame(gdf.drop(columns="geometry"))
    metric_fields = [field for field in table.columns if METRIC_FIELD_PATTERN.match(str(field))]
    if metric_fields:
        table = table[table[metric_fields].notna().any(axis=1)]
    table.to_csv(csv_path, index=False, encoding="utf-8-sig" if encoding.upper() == "UTF-8" else encoding)

def main() -> int:
    args = parse_args()
    if args.mode == "skip" and args.boundary_output is not None and args.boundary_output.exists():
        boundary_shp = resolve_boundary_shp(args.boundary_output)
        print(f"[跳过模式] 使用已有精度边界判断增量：{boundary_shp}")
    else:
        boundary_shp = resolve_boundary_shp(args.boundary_shp)
    boundary = read_boundary(boundary_shp, args.input_encoding)
    area_crs = boundary.crs
    target_crop_codes = load_target_crop_codes(args.crop_code_json)
    truth_by_code = collect_by_county(args.truth_root)
    measure_by_code = collect_by_county(args.measure_root)

    common_counties = sorted(set(truth_by_code) & set(measure_by_code))
    if args.mode == "skip":
        done_counties = already_computed_counties(boundary)
        common_counties = [qxdm for qxdm in common_counties if qxdm not in done_counties]
        if done_counties:
            print(f"[跳过模式] 新边界中已有精度的区县={len(done_counties)}，本次不重复计算。")
    missing_measure = sorted(set(truth_by_code) - set(measure_by_code))
    if missing_measure:
        print(f"[提示] {len(missing_measure)} 个真值区县未找到测量结果，将不写入精度：{', '.join(missing_measure[:20])}")
    if not common_counties:
        if args.mode == "skip":
            print("[跳过模式] 没有需要新增计算的区县，文件保持不变。")
            return 0
        raise ValueError("没有找到可配对的真值/测量结果区县。")
    county_metrics: dict[str, dict[str, object]] = {}
    detail_rows: list[dict[str, object]] = []
    all_codes: set[str] = set()
    for index, qxdm in enumerate(common_counties, start=1):
        metrics, details = compute_county_metrics(
            qxdm,
            truth_by_code[qxdm],
            measure_by_code[qxdm],
            area_crs,
            args.input_encoding,
            args.area_epsilon,
        )
        apply_ma_cap(metrics, details, args.ma_cap_code, args.ma_cap_max)
        county_metrics[qxdm] = metrics
        for code in details:
            if code not in target_crop_codes or not is_metric_code(code):
                continue
            all_codes.add(code)
            detail_rows.append({COUNTY_CODE_FIELD: qxdm, "ZWDM": code, **details[code]})
        print(f"[计算 {index}/{len(common_counties)}] {qxdm}：{', '.join(metrics.keys())}")

    crop_codes = [code for code in target_crop_codes if is_metric_code(code) and code not in EXCLUDED_OUTPUT_CODES]
    output = build_output_boundary(
        boundary,
        county_metrics,
        crop_codes,
        args.reviewer,
        args.review_date,
        preserve_existing=args.mode == "skip",
    )
    if args.dry_run:
        print(f"[预检查] 可配对区县={len(common_counties)}；作物代码={', '.join(crop_codes)}")
        return 0

    updated_path = update_boundary_in_place(output, boundary_shp, args.boundary_output, crop_codes, args.encoding)
    write_csv(output, args.csv_output, args.encoding, county_metrics, crop_codes)
    detail_csv = args.csv_output.with_name(f"{args.csv_output.stem}_明细{args.csv_output.suffix}")
    pd.DataFrame(detail_rows).to_csv(detail_csv, index=False, encoding="utf-8-sig")
    print(f"[完成] 已更新新边界：{updated_path}")
    print(f"[完成] 已导出 CSV：{args.csv_output}")
    print(f"[完成] 已导出明细 CSV：{detail_csv}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"[错误] {exc}", file=sys.stderr)
        raise SystemExit(1)











