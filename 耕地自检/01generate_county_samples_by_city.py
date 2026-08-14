from __future__ import annotations

import argparse
import bisect
import random
import re
import time
from pathlib import Path

import geopandas as gpd
import pandas as pd
from pyproj import CRS
from shapely.geometry import MultiPolygon, Point, Polygon, box
from shapely.ops import unary_union
from shapely.prepared import prep
BASE_DIR = Path(__file__).resolve().parent
DEFAULT_SOURCE_ROOT = BASE_DIR / "00分类结果"
DEFAULT_OUTPUT_ROOT = BASE_DIR / "01生成样方"
NEW_ADMIN_BOUNDARY_PATH = BASE_DIR / "00县边界"
DEFAULT_CITY_BOUNDARY_PATH = BASE_DIR / "00市边界"

DEFAULT_SAMPLE_COUNT = 100
DEFAULT_MIN_DISTANCE = 100.0
DEFAULT_SQUARE_SIZE = 50.0
DEFAULT_MIN_OVERLAP_RATIO = 0.2
DEFAULT_MAX_ATTEMPTS = 200_000
DEFAULT_MAX_NO_PROGRESS_ATTEMPTS = 30_000
DEFAULT_OTHER_SEARCH_BUFFER = 1_000.0
DEFAULT_FIELD_MAPPING_PATH = BASE_DIR / "_flex_field_mapping.csv"
HARDCODED_TARGET_CRS_WKT = (
    'PROJCRS["CGCS_2000_Albers",'
    'BASEGEOGCRS["China Geodetic Coordinate System 2000",'
    'DATUM["China 2000",'
    'ELLIPSOID["CGCS2000",6378137,298.257222101,LENGTHUNIT["metre",1]],'
    'ID["EPSG",1043]],'
    'PRIMEM["Greenwich",0,ANGLEUNIT["Degree",0.0174532925199433]]],'
    'CONVERSION["unnamed",'
    'METHOD["Albers Equal Area",ID["EPSG",9822]],'
    'PARAMETER["Latitude of false origin",0,ANGLEUNIT["Degree",0.0174532925199433],ID["EPSG",8821]],'
    'PARAMETER["Longitude of false origin",105,ANGLEUNIT["Degree",0.0174532925199433],ID["EPSG",8822]],'
    'PARAMETER["Latitude of 1st standard parallel",25,ANGLEUNIT["Degree",0.0174532925199433],ID["EPSG",8823]],'
    'PARAMETER["Latitude of 2nd standard parallel",47,ANGLEUNIT["Degree",0.0174532925199433],ID["EPSG",8824]],'
    'PARAMETER["Easting at false origin",0,LENGTHUNIT["metre",1],ID["EPSG",8826]],'
    'PARAMETER["Northing at false origin",0,LENGTHUNIT["metre",1],ID["EPSG",8827]]],'
    'CS[Cartesian,2],'
    'AXIS["(E)",east,ORDER[1],LENGTHUNIT["metre",1,ID["EPSG",9001]]],'
    'AXIS["(N)",north,ORDER[2],LENGTHUNIT["metre",1,ID["EPSG",9001]]]]'
)
GRID_CELL_SIZE_FACTOR = 1.0
SQUARE_AREA_EPSILON = 1e-9
CLASS_WHEAT = "小麦样方"
CLASS_RAPE = "油菜样方"
CLASS_OTHER = "其它样方"
CLASS_FIELD_NAME = "class"
CLASS_WHEAT_VALUE = 1
CLASS_RAPE_VALUE = 2
CLASS_OTHER_VALUES = (3, 0)
ZZLX_FIELD_NAME = "ZZLX"
ZWMC_FIELD_NAME = "ZWMC"
TBLXDM_FIELD_NAME = "TBLXDM"
TBLXMC_FIELD_NAME = "TBLXMC"
RAPE_VALUE = "油菜"
MAPPING_SOURCE_FIELD = "SOURCE_FIELD"
MAPPING_SOURCE_VALUE = "SOURCE_VALUE"
MAPPING_ZWMC_FIELD = "ZWMC"
MAPPING_ZWDM_FIELD = "ZWDM"
FLEX_MAPPING_CLASS_FIELD = "_flex_class"
FLEX_MAPPING_ZZLX_FIELD = "_flex_zzlx"
RAPE_SAMPLE_COUNTY_NAMES = {
    "大丰",
    "大丰区",
    "东台",
    "东台市",
    "通州",
    "通州区",
    "海门",
    "海门区",
    "如东",
    "如东县",
    "启东",
    "启东市",
    "句容",
    "句容市",
    "溧阳",
    "溧阳市",
    "兴化",
    "兴化市",
}
ADMIN_NAME_FIELDS = (
    "area_name",
    "name",
    "Name",
    "NAME",
    "县",
    "区",
    "县区",
    "区县",
    "县名",
    "区县名",
    "XZQMC",
    "XZQDM",
    "XZQHMC",
    "PAC",
    "区县名称_",
)
ADMIN_CITY_FIELDS = (
    "市名称",
    "市名",
    "市级名称",
    "city_name",
    "city",
    "City",
    "CITY",
    "市",
    "地市",
    "市名",
    "地市名",
    "SJ",
    "SJMC",
    "DSMC",
)
CITY_BOUNDARY_NAME_FIELDS = ("市名称", "市名", "市级名称", "city_name", "CITY_NAME", "NAME", "name")
ADMIN_CODE_FIELDS = (
    "area_code",
    "区县代码_",
    "区县代码",
    "XZQDM",
    "PAC",
)
JIANGSU_CITY_CODE_TO_NAME = {
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
    ".ain",
    ".aih",
    ".ixs",
    ".mxs",
    ".atx",
    ".shp.xml",
}


def clean_city_name(folder_name: str) -> str | None:
    if "市" not in folder_name:
        return None
    return folder_name.split("市", 1)[0] + "市"


def extract_admin_name(text: str, suffixes: tuple[str, ...]) -> str | None:
    for suffix in suffixes:
        match = re.search(rf"[^\\/]+?{suffix}", text)
        if match:
            return match.group(0)
    return None


def get_city_name(shp_path: Path, source_root: Path) -> str:
    for part in shp_path.parts:
        city_name = clean_city_name(part)
        if city_name:
            return city_name

    city_name = clean_city_name(source_root.name)
    if city_name:
        return city_name

    return "未知市"


def get_county_name(shp_path: Path, city_name: str) -> str:
    for part in reversed(shp_path.parent.parts):
        county_name = extract_admin_name(part, ("区", "县", "市"))
        if county_name and county_name != city_name:
            return county_name

    county_name = extract_admin_name(shp_path.stem, ("区", "县", "市"))
    if county_name and county_name != city_name:
        return county_name

    return shp_path.stem


def safe_filename(name: str) -> str:
    return re.sub(r'[<>:"/\\|?*]+', "_", name).strip() or "未命名"


def normalize_county_name(name: object) -> str:
    text = str(name).strip()
    return re.sub(r"(市|区|县)$", "", text)


def should_generate_rape_samples(county_name: object) -> bool:
    text = str(county_name).strip()
    return text in RAPE_SAMPLE_COUNTY_NAMES or normalize_county_name(text) in RAPE_SAMPLE_COUNTY_NAMES


def parse_failed_sample_units(log_path: Path) -> list[tuple[str, str]]:
    if not log_path.exists():
        raise FileNotFoundError(f"失败日志不存在: {log_path}")

    units: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()
    pattern = re.compile(r"FAILED:\s*([^-]+)-(.+?)\s+-")
    for line in log_path.read_text(encoding="utf-8", errors="ignore").splitlines():
        match = pattern.search(line)
        if not match:
            continue
        city_name = match.group(1).strip()
        county_name = match.group(2).strip()
        key = (city_name, county_name)
        if city_name and county_name and key not in seen:
            seen.add(key)
            units.append(key)
    if not units:
        raise ValueError(f"失败日志中没有解析到 FAILED 区划: {log_path}")
    return units


def parse_boundary_filter_values(values: list[str] | None) -> tuple[set[tuple[str, str]], set[str]]:
    city_county_filters: set[tuple[str, str]] = set()
    county_filters: set[str] = set()
    for value in values or []:
        text = str(value).strip()
        if not text:
            continue
        if "-" in text:
            city_name, county_name = text.split("-", 1)
            city_county_filters.add((city_name.strip(), county_name.strip()))
        else:
            county_filters.add(text)
    return city_county_filters, county_filters


def filter_admin_units(
    admin_units: list[dict[str, object]],
    city_county_filters: set[tuple[str, str]],
    county_filters: set[str],
) -> list[dict[str, object]]:
    if not city_county_filters and not county_filters:
        return admin_units

    filtered_units: list[dict[str, object]] = []
    for unit in admin_units:
        city_name = str(unit["city"])
        county_name = str(unit["county"])
        if (city_name, county_name) in city_county_filters or county_name in county_filters:
            filtered_units.append(unit)
    return filtered_units


def format_elapsed(seconds: float) -> str:
    if seconds < 60:
        return f"{seconds:.1f}s"
    minutes, remaining_seconds = divmod(seconds, 60)
    if minutes < 60:
        return f"{int(minutes)}m{remaining_seconds:.0f}s"
    hours, remaining_minutes = divmod(minutes, 60)
    return f"{int(hours)}h{int(remaining_minutes)}m"


def fast_union(geometries: object) -> object:
    if hasattr(geometries, "union_all"):
        return geometries.union_all()
    return unary_union(list(geometries))


def extract_polygonal_geometry(geom: object) -> object | None:
    if geom is None or geom.is_empty:
        return None

    if isinstance(geom, Polygon):
        return geom if geom.area > 0 else None

    if isinstance(geom, MultiPolygon):
        polygons = [part for part in geom.geoms if part.area > 0]
        if not polygons:
            return None
        return MultiPolygon(polygons)

    if geom.geom_type == "GeometryCollection":
        polygons = []
        for part in geom.geoms:
            polygonal_part = extract_polygonal_geometry(part)
            if polygonal_part is None:
                continue
            if isinstance(polygonal_part, Polygon):
                polygons.append(polygonal_part)
            elif isinstance(polygonal_part, MultiPolygon):
                polygons.extend(list(polygonal_part.geoms))
        if not polygons:
            return None
        return MultiPolygon(polygons)

    return None


def keep_polygonal_geometries(gdf: gpd.GeoDataFrame) -> gpd.GeoDataFrame:
    if gdf.empty:
        return gdf

    filtered_gdf = gdf.copy()
    filtered_gdf["geometry"] = filtered_gdf.geometry.apply(extract_polygonal_geometry)
    filtered_gdf = filtered_gdf[filtered_gdf.geometry.notna()]
    filtered_gdf = filtered_gdf[~filtered_gdf.geometry.is_empty]
    return filtered_gdf


def find_input_shps(source_root: Path, only_dir_name: str | None) -> list[Path]:
    shp_paths = sorted(source_root.rglob("*.shp"))
    if not only_dir_name:
        return shp_paths

    return [
        shp_path
        for shp_path in shp_paths
        if any(parent.name == only_dir_name for parent in shp_path.parents)
    ]


def remove_existing_shapefile(path: Path) -> None:
    stem = path.stem
    for suffix in SHAPEFILE_SUFFIXES:
        related_path = path.with_name(f"{stem}{suffix}")
        if related_path.exists():
            related_path.unlink()


def resolve_target_crs() -> CRS:
    return CRS.from_wkt(HARDCODED_TARGET_CRS_WKT)


def to_metric_crs(gdf: gpd.GeoDataFrame, target_crs: CRS | str | None) -> gpd.GeoDataFrame:
    if gdf.crs is None and not target_crs:
        raise ValueError("输入 shp 没有 CRS，且脚本没有可用的固定目标坐标系。")

    if target_crs:
        return gdf.to_crs(target_crs) if gdf.crs else gdf.set_crs(target_crs)

    if gdf.crs and gdf.crs.is_projected:
        return gdf

    estimated_crs = gdf.estimate_utm_crs()
    if estimated_crs is None:
        raise ValueError("无法自动估算米制投影。")

    return gdf.to_crs(estimated_crs)


def text_matches_county(value: object, county_name: str) -> bool:
    text = str(value).strip()
    if not text or text.lower() == "nan":
        return False
    return county_name in text or text in county_name


def first_non_empty_value(row: object, fields: tuple[str, ...]) -> str | None:
    for field in fields:
        if field not in row.index:
            continue
        value = str(row[field]).strip()
        if value and value.lower() != "nan":
            return value
    return None


def get_admin_county_name(row: object, source_path: Path, fallback_index: int) -> str:
    value = first_non_empty_value(row, ADMIN_NAME_FIELDS)
    if value:
        county_name = extract_admin_name(value, ("区", "县", "市"))
        return county_name or value

    county_name = extract_admin_name(source_path.stem, ("区", "县", "市"))
    if county_name:
        return county_name

    return f"{source_path.stem}_{fallback_index + 1}"


def get_admin_city_name(row: object, source_path: Path, fallback_city_name: str | None) -> str:
    if fallback_city_name:
        return fallback_city_name

    value = first_non_empty_value(row, ADMIN_CITY_FIELDS)
    if value:
        city_name = clean_city_name(value)
        return city_name or value

    code_value = first_non_empty_value(row, ADMIN_CODE_FIELDS)
    if code_value:
        city_name = JIANGSU_CITY_CODE_TO_NAME.get(code_value[:4])
        if city_name:
            return city_name

    for part in source_path.parts:
        city_name = clean_city_name(part)
        if city_name:
            return city_name

    return "未知市"


def load_city_units(city_boundary: Path | None, target_crs: CRS | str | None) -> list[dict[str, object]]:
    if city_boundary is None or not city_boundary.exists():
        return []

    city_units: list[dict[str, object]] = []
    for shp_path in iter_admin_shps(city_boundary):
        city_gdf = gpd.read_file(shp_path)
        if target_crs:
            city_gdf = city_gdf.to_crs(target_crs) if city_gdf.crs else city_gdf.set_crs(target_crs)
        for row_index, row in city_gdf.iterrows():
            geom = row.geometry
            if geom is None or geom.is_empty:
                continue
            city_name = first_non_empty_value(row, CITY_BOUNDARY_NAME_FIELDS)
            if not city_name:
                city_name = get_admin_city_name(row, shp_path, None)
            city_units.append(
                {
                    "city": str(city_name).strip() or f"{shp_path.stem}_{row_index + 1}",
                    "geometry": geom,
                }
            )
    return city_units


def city_name_from_boundary(county_geom: object, city_units: list[dict[str, object]], fallback: str) -> str:
    if not city_units:
        return fallback

    best_city = ""
    best_area = 0.0
    for city_unit in city_units:
        city_geom = city_unit["geometry"]
        if not city_geom.bounds or not county_geom.intersects(city_geom):
            continue
        try:
            overlap_area = county_geom.intersection(city_geom).area
        except Exception:
            overlap_area = county_geom.buffer(0).intersection(city_geom.buffer(0)).area
        if overlap_area > best_area:
            best_area = overlap_area
            best_city = str(city_unit["city"])
    return best_city or fallback


def select_admin_rows(admin_gdf: gpd.GeoDataFrame, county_name: str) -> gpd.GeoDataFrame:
    for field in ADMIN_NAME_FIELDS:
        if field not in admin_gdf.columns:
            continue
        mask = admin_gdf[field].apply(lambda value: text_matches_county(value, county_name))
        selected = admin_gdf[mask].copy()
        if not selected.empty:
            return selected
    return admin_gdf.iloc[0:0].copy()


def read_admin_shp(admin_shp: Path, county_name: str, target_crs: CRS | str | None) -> gpd.GeoDataFrame:
    admin_gdf = gpd.read_file(admin_shp)
    if target_crs:
        admin_gdf = admin_gdf.to_crs(target_crs) if admin_gdf.crs else admin_gdf.set_crs(target_crs)
    selected = select_admin_rows(admin_gdf, county_name)
    if not selected.empty:
        return selected
    return admin_gdf


def load_admin_boundary(
    admin_boundary: Path | None,
    county_name: str,
    target_crs: CRS | str | None,
) -> object | None:
    if not admin_boundary:
        return None
    if not admin_boundary.exists():
        raise FileNotFoundError(f"行政边界不存在: {admin_boundary}")

    if admin_boundary.is_file():
        admin_gdf = read_admin_shp(admin_boundary, county_name, target_crs)
        return valid_union(admin_gdf, f"行政边界没有有效几何: {admin_boundary}")

    shp_paths = sorted(admin_boundary.rglob("*.shp"))
    if not shp_paths:
        raise FileNotFoundError(f"行政边界目录下没有 shp: {admin_boundary}")

    path_matched = [path for path in shp_paths if county_name in str(path)]
    if path_matched:
        admin_gdfs = [read_admin_shp(path, county_name, target_crs) for path in path_matched]
        admin_gdf = gpd.GeoDataFrame(
            geometry=[geom for gdf in admin_gdfs for geom in gdf.geometry],
            crs=admin_gdfs[0].crs,
        )
        return valid_union(admin_gdf, f"行政边界没有有效几何: {county_name}")

    matched_gdfs = []
    for shp_path in shp_paths:
        admin_gdf = gpd.read_file(shp_path)
        if target_crs:
            admin_gdf = admin_gdf.to_crs(target_crs) if admin_gdf.crs else admin_gdf.set_crs(target_crs)
        selected = select_admin_rows(admin_gdf, county_name)
        if not selected.empty:
            matched_gdfs.append(selected)

    if not matched_gdfs:
        raise ValueError(f"未在行政边界中匹配到区县: {county_name}")

    admin_gdf = gpd.GeoDataFrame(
        geometry=[geom for gdf in matched_gdfs for geom in gdf.geometry],
        crs=matched_gdfs[0].crs,
    )
    return valid_union(admin_gdf, f"行政边界没有有效几何: {county_name}")


def iter_admin_shps(admin_boundary: Path) -> list[Path]:
    if not admin_boundary.exists():
        raise FileNotFoundError(f"行政边界不存在: {admin_boundary}")
    if admin_boundary.is_file():
        if admin_boundary.suffix.lower() != ".shp":
            raise ValueError(f"行政边界文件必须是 shp: {admin_boundary}")
        return [admin_boundary]

    shp_paths = sorted(admin_boundary.rglob("*.shp"))
    if not shp_paths:
        raise FileNotFoundError(f"行政边界目录下没有 shp: {admin_boundary}")
    return shp_paths


def load_admin_units(
    admin_boundary: Path,
    target_crs: CRS | str | None,
    fallback_city_name: str | None,
    city_boundary: Path | None = None,
) -> list[dict[str, object]]:
    grouped: dict[tuple[str, str], dict[str, object]] = {}
    city_units = load_city_units(city_boundary, target_crs)

    for shp_path in iter_admin_shps(admin_boundary):
        admin_gdf = gpd.read_file(shp_path)
        if target_crs:
            admin_gdf = admin_gdf.to_crs(target_crs) if admin_gdf.crs else admin_gdf.set_crs(target_crs)

        for row_index, row in admin_gdf.iterrows():
            geom = row.geometry
            if geom is None or geom.is_empty:
                continue

            county_name = get_admin_county_name(row, shp_path, row_index)
            fallback_city = get_admin_city_name(row, shp_path, fallback_city_name)
            city_name = city_name_from_boundary(geom, city_units, fallback_city)
            key = (city_name, county_name)

            if key not in grouped:
                grouped[key] = {
                    "city": city_name,
                    "county": county_name,
                    "source": shp_path.name,
                    "geometries": [],
                }
            grouped[key]["geometries"].append(geom)

    admin_units = []
    for item in grouped.values():
        geom = fast_union(gpd.GeoSeries(item["geometries"]))
        if geom is None or geom.is_empty:
            continue
        admin_units.append(
            {
                "city": item["city"],
                "county": item["county"],
                "source": item["source"],
                "geometry": geom,
            }
        )

    if not admin_units:
        raise ValueError(f"行政边界没有有效区县几何: {admin_boundary}")

    return admin_units


def build_admin_lookup(
    admin_boundary: Path,
    target_crs: CRS | str | None,
    fallback_city_name: str | None,
) -> dict[tuple[str | None, str], object]:
    lookup: dict[tuple[str | None, str], object] = {}
    for unit in load_admin_units(admin_boundary, target_crs, fallback_city_name):
        city_name = str(unit["city"])
        county_name = str(unit["county"])
        lookup[(city_name, county_name)] = unit["geometry"]
        lookup[(None, county_name)] = unit["geometry"]
    return lookup


def get_admin_geom_from_cache(args: argparse.Namespace, city_name: str, county_name: str) -> object | None:
    admin_lookup = getattr(args, "admin_lookup", None)
    if not admin_lookup:
        return None
    return admin_lookup.get((city_name, county_name)) or admin_lookup.get((None, county_name))


def load_classification_gdf(
    shp_paths: list[Path],
    target_crs: CRS | str | None,
    class_mapping: list[dict[str, object]] | None = None,
) -> gpd.GeoDataFrame:
    gdfs = []
    print(f"[01] 开始读取分类 shp，共 {len(shp_paths)} 个", flush=True)
    for shp_index, shp_path in enumerate(shp_paths, start=1):
        print(f"[01] 读取分类 shp {shp_index}/{len(shp_paths)}：{shp_path}", flush=True)
        gdf = gpd.read_file(shp_path)
        print(f"[01] 原始要素数：{len(gdf)}；字段：{list(gdf.columns)}", flush=True)
        if gdf.empty:
            continue
        gdf = normalize_class_column(normalize_zzlx_column(gdf), class_mapping)
        print(f"[01] TBLXDM 有效要素数：{len(gdf)}", flush=True)
        gdf = to_metric_crs(gdf, target_crs)
        gdf = keep_polygonal_geometries(gdf)
        print(f"[01] 有效面要素数：{len(gdf)}", flush=True)
        if gdf.empty:
            continue
        gdf["src_name"] = shp_path.name
        keep_columns = ["geometry", "src_name"]
        if CLASS_FIELD_NAME in gdf.columns:
            keep_columns.append(CLASS_FIELD_NAME)
        if TBLXDM_FIELD_NAME in gdf.columns:
            keep_columns.append(TBLXDM_FIELD_NAME)
        if TBLXMC_FIELD_NAME in gdf.columns:
            keep_columns.append(TBLXMC_FIELD_NAME)
        if ZZLX_FIELD_NAME in gdf.columns:
            keep_columns.append(ZZLX_FIELD_NAME)
        gdf = gdf[keep_columns]
        gdfs.append(gdf)

    if not gdfs:
        raise ValueError("分类矢量均为空，无法生成样方。")

    merged_gdf = gpd.GeoDataFrame(pd.concat(gdfs, ignore_index=True), geometry="geometry", crs=gdfs[0].crs)
    merged_gdf = merged_gdf[merged_gdf.geometry.notna()]
    merged_gdf = merged_gdf[~merged_gdf.geometry.is_empty]
    if merged_gdf.empty:
        raise ValueError("分类矢量没有有效几何，无法生成样方。")

    print(f"[01] 分类数据合并完成：要素数={len(merged_gdf)}，CRS={merged_gdf.crs}", flush=True)
    return merged_gdf


def select_classification_for_admin(
    classification_gdf: gpd.GeoDataFrame,
    admin_geom: object,
    clip_to_admin: bool = True,
) -> gpd.GeoDataFrame:
    bounds = admin_geom.bounds
    print(f"[01] 按行政边界筛选分类图斑：bounds={tuple(round(v, 2) for v in bounds)}", flush=True)
    candidate_index = list(classification_gdf.sindex.intersection(bounds))
    print(f"[01] 空间索引候选图斑数：{len(candidate_index)}", flush=True)
    if not candidate_index:
        return classification_gdf.iloc[0:0].copy()

    candidates = classification_gdf.iloc[candidate_index].copy()
    candidates = candidates[candidates.geometry.intersects(admin_geom)].copy()
    print(f"[01] 与行政边界相交图斑数：{len(candidates)}", flush=True)
    if candidates.empty or not clip_to_admin:
        return candidates

    print("[01] 开始裁切分类图斑到行政边界", flush=True)
    candidates["geometry"] = candidates.geometry.intersection(admin_geom)
    candidates = keep_polygonal_geometries(candidates)
    print(f"[01] 裁切后有效图斑数：{len(candidates)}", flush=True)
    return candidates


def filter_admin_units_by_classification(
    admin_units: list[dict[str, object]],
    classification_gdf: gpd.GeoDataFrame,
) -> list[dict[str, object]]:
    matched_by_key: dict[tuple[str, str], dict[str, object]] = {}
    for src_name, source_gdf in classification_gdf.groupby("src_name", sort=False):
        source_gdf = source_gdf[source_gdf.geometry.notna() & ~source_gdf.geometry.is_empty].copy()
        if source_gdf.empty:
            continue

        source_index = source_gdf.sindex
        best_unit: dict[str, object] | None = None
        best_area = 0.0
        print(
            f"[01] 开始匹配分类文件到行政区：{src_name}；图斑数={len(source_gdf)}；行政区数={len(admin_units)}",
            flush=True,
        )
        for unit_index, unit in enumerate(admin_units, start=1):
            admin_geom = unit["geometry"]
            candidate_indices = list(source_index.intersection(admin_geom.bounds))
            if not candidate_indices:
                continue
            candidates = source_gdf.iloc[candidate_indices]
            try:
                candidates = candidates[candidates.geometry.intersects(admin_geom)]
            except Exception:
                candidates = candidates[candidates.geometry.apply(lambda geom: geom.intersects(admin_geom))]
            if candidates.empty:
                continue
            try:
                overlap_area = float(candidates.geometry.intersection(admin_geom).area.sum())
            except Exception:
                overlap_area = 0.0
                valid_admin = admin_geom.buffer(0)
                for geom in candidates.geometry:
                    try:
                        overlap_area += float(geom.buffer(0).intersection(valid_admin).area)
                    except Exception:
                        continue
            print(
                f"[01] 分类归属候选 {unit_index}/{len(admin_units)}：{unit['city']}-{unit['county']} "
                f"候选图斑={len(candidate_indices)} 相交图斑={len(candidates)} overlap={overlap_area:.2f}",
                flush=True,
            )
            if overlap_area > best_area:
                best_area = overlap_area
                best_unit = unit

        if best_unit is not None and best_area > 0:
            key = (str(best_unit["city"]), str(best_unit["county"]))
            matched_by_key[key] = best_unit
            print(
                f"Classification owner: {src_name} -> "
                f"{best_unit['city']}-{best_unit['county']} overlap={best_area:.2f}",
                flush=True,
            )
        else:
            print(f"[01] 分类文件未匹配到行政区：{src_name}", flush=True)

    return list(matched_by_key.values())

class CenterDistanceIndex:
    def __init__(self, min_distance: float) -> None:
        self.min_distance = min_distance
        self.min_distance_sq = min_distance * min_distance
        self.cell_size = max(min_distance * GRID_CELL_SIZE_FACTOR, 1.0)
        self.grid: dict[tuple[int, int], list[tuple[float, float]]] = {}
        self.centers: list[tuple[float, float]] = []

    def _cell_key(self, x: float, y: float) -> tuple[int, int]:
        return int(x // self.cell_size), int(y // self.cell_size)

    def can_add(self, x: float, y: float) -> bool:
        cell_x, cell_y = self._cell_key(x, y)
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                for old_x, old_y in self.grid.get((cell_x + dx, cell_y + dy), []):
                    if (x - old_x) ** 2 + (y - old_y) ** 2 <= self.min_distance_sq:
                        return False
        return True

    def add(self, x: float, y: float) -> None:
        self.centers.append((x, y))
        self.grid.setdefault(self._cell_key(x, y), []).append((x, y))


def square_inside_admin(sample_square: object, admin_geom: object | None) -> bool:
    if admin_geom is None:
        return True
    return admin_geom.contains(sample_square)


def square_inside_prepared_admin(sample_square: object, prepared_admin_geom: object | None) -> bool:
    if prepared_admin_geom is None:
        return True
    return prepared_admin_geom.contains(sample_square)


def qc_center_distances(samples_gdf: gpd.GeoDataFrame, min_distance: float) -> float:
    centers = [
        (float(row["center_x"]), float(row["center_y"]), int(row["sample_id"]))
        for _, row in samples_gdf.iterrows()
    ]
    if len(centers) < 2:
        return float("inf")

    min_found_distance = float("inf")
    min_pair: tuple[int, int] | None = None

    for index, (x1, y1, sample_id1) in enumerate(centers):
        for x2, y2, sample_id2 in centers[index + 1 :]:
            distance = ((x1 - x2) ** 2 + (y1 - y2) ** 2) ** 0.5
            if distance < min_found_distance:
                min_found_distance = distance
                min_pair = (sample_id1, sample_id2)

    if min_found_distance <= min_distance:
        raise ValueError(
            f"样方中心点距离质检不通过: sample_id {min_pair[0]} 和 {min_pair[1]} "
            f"距离为 {min_found_distance:.3f}m，必须大于 {min_distance}m。"
        )

    return min_found_distance


def qc_samples_inside_admin(samples_gdf: gpd.GeoDataFrame, admin_geom: object | None) -> None:
    if admin_geom is None:
        return

    invalid_ids = []
    for _, row in samples_gdf.iterrows():
        if not square_inside_admin(row.geometry, admin_geom):
            invalid_ids.append(int(row["sample_id"]))

    if invalid_ids:
        preview = ", ".join(str(sample_id) for sample_id in invalid_ids[:10])
        raise ValueError(f"行政边界质检不通过，样方不完全在行政范围内部: {preview}")


def valid_union(gdf: gpd.GeoDataFrame, empty_message: str) -> object:
    gdf = keep_polygonal_geometries(gdf)
    geometries = gdf.geometry.dropna()
    geometries = geometries[~geometries.is_empty]
    if geometries.empty:
        raise ValueError(empty_message)

    union_geom = fast_union(geometries)
    if union_geom.is_empty:
        raise ValueError(empty_message)

    return union_geom



def normalize_mapping_text(value: object) -> str:
    if value is None or pd.isna(value):
        return ""
    text = str(value).strip()
    return "" if text.lower() == "nan" else text


def find_column_case_insensitive(columns, requested_name: object) -> str | None:
    requested = normalize_mapping_text(requested_name)
    if not requested:
        return None
    for column in columns:
        if str(column) == requested:
            return column
    lower_lookup = {str(column).lower(): column for column in columns}
    return lower_lookup.get(requested.lower())


def infer_class_from_mapping_value(zwmc: object, zwdm: object) -> int | None:
    return None


def infer_class_from_zwmc(value: object) -> int | None:
    return None

def crop_sample_class_from_gdf(gdf: gpd.GeoDataFrame, default_name: str) -> str:
    if ZZLX_FIELD_NAME not in gdf.columns:
        return default_name
    names = sorted({normalize_mapping_text(value) for value in gdf[ZZLX_FIELD_NAME] if normalize_mapping_text(value)})
    if len(names) != 1:
        return default_name
    name = names[0]
    if "春小麦" in name:
        return "春小麦样方"
    if "冬小麦" in name:
        return "冬小麦样方"
    if "油菜" in name:
        return CLASS_RAPE
    return default_name

def load_flexible_class_mapping(mapping_path: Path | None) -> list[dict[str, object]]:
    if mapping_path is None:
        return []
    mapping_path = Path(mapping_path)
    if not mapping_path.exists():
        print(f"[mapping] flexible field mapping not found, fallback to class field: {mapping_path}")
        return []

    mapping_df = pd.read_csv(mapping_path, dtype=str, encoding="utf-8-sig").fillna("")
    mapping_df.columns = [str(column).strip() for column in mapping_df.columns]
    source_field_column = find_column_case_insensitive(mapping_df.columns, MAPPING_SOURCE_FIELD)
    source_value_column = (
        find_column_case_insensitive(mapping_df.columns, MAPPING_SOURCE_VALUE)
        or find_column_case_insensitive(mapping_df.columns, "ZZLX")
        or find_column_case_insensitive(mapping_df.columns, "VALUE")
    )
    if source_field_column is None or source_value_column is None:
        raise ValueError(
            "Mapping CSV missing required columns: SOURCE_FIELD and SOURCE_VALUE "
            f"(ZZLX/VALUE is also accepted for SOURCE_VALUE); path={mapping_path}; "
            f"columns={list(mapping_df.columns)}"
        )

    mappings: list[dict[str, object]] = []
    for row_number, row in mapping_df.iterrows():
        source_field = normalize_mapping_text(row.get(source_field_column))
        source_value = normalize_mapping_text(row.get(source_value_column))
        zwmc = normalize_mapping_text(row.get(MAPPING_ZWMC_FIELD)) if MAPPING_ZWMC_FIELD in mapping_df.columns else ""
        zwdm = normalize_mapping_text(row.get(MAPPING_ZWDM_FIELD)) if MAPPING_ZWDM_FIELD in mapping_df.columns else ""
        mapped_class = infer_class_from_mapping_value(zwmc, zwdm)
        if not source_field or not source_value:
            print(f"[mapping] skip row {row_number + 2}: empty SOURCE_FIELD/SOURCE_VALUE")
            continue
        if mapped_class is None:
            print(f"[mapping] skip row {row_number + 2}: cannot infer class from ZWMC/ZWDM")
            continue
        mappings.append(
            {
                "source_field": source_field,
                "source_value": source_value,
                "zwmc": zwmc,
                "zwdm": zwdm,
                "class": mapped_class,
            }
        )

    print(f"[mapping] loaded {len(mappings)} flexible mapping rows from {mapping_path}")
    return mappings


def apply_flexible_class_mapping(
    gdf: gpd.GeoDataFrame,
    class_mapping: list[dict[str, object]] | None,
) -> gpd.GeoDataFrame:
    if not class_mapping:
        return gdf

    output = gdf.copy()
    mapped_class = pd.Series(pd.NA, index=output.index, dtype="object")
    mapped_zzlx = pd.Series(pd.NA, index=output.index, dtype="object")
    source_fields_seen: set[str] = set()
    missing_source_fields: set[str] = set()

    for mapping in class_mapping:
        source_field = str(mapping["source_field"])
        source_column = find_column_case_insensitive(output.columns, source_field)
        if source_column is None:
            missing_source_fields.add(source_field)
            continue
        source_fields_seen.add(str(source_column))
        source_value = str(mapping["source_value"])
        source_values = output[source_column].map(normalize_mapping_text)
        if source_value == "*":
            mask = source_values != ""
        else:
            mask = source_values == source_value
        if not bool(mask.any()):
            continue
        mapped_class.loc[mask] = int(mapping["class"])
        mapped_zzlx.loc[mask] = mapping.get("zwmc") or source_value

    if not source_fields_seen:
        if missing_source_fields:
            print(f"[mapping] none of SOURCE_FIELD exists in current shp: {', '.join(sorted(missing_source_fields))}")
        return output

    # When a mapping source field is present, unmatched values are intentionally treated as other samples.
    output[CLASS_FIELD_NAME] = pd.to_numeric(mapped_class.where(mapped_class.notna(), 3), errors="coerce").fillna(3).astype(int)
    if ZZLX_FIELD_NAME not in output.columns:
        output[ZZLX_FIELD_NAME] = mapped_zzlx.fillna("其它")
    else:
        output[ZZLX_FIELD_NAME] = output[ZZLX_FIELD_NAME].fillna(mapped_zzlx).fillna("其它")
    return output


def crop_code_text(value: object) -> str:
    text = normalize_mapping_text(value)
    if text.endswith(".0"):
        text = text[:-2]
    digits = re.sub(r"\D", "", text)
    return digits if digits else text


def find_tblxdm_column(gdf: gpd.GeoDataFrame) -> str | None:
    return find_column_case_insensitive(gdf.columns, TBLXDM_FIELD_NAME)


def normalize_tblxdm_column(gdf: gpd.GeoDataFrame) -> gpd.GeoDataFrame:
    tblxdm_column = find_tblxdm_column(gdf)
    if tblxdm_column is None:
        raise ValueError(f"分类矢量缺少 {TBLXDM_FIELD_NAME} 字段，无法按作物代码生成样方。")
    output = gdf.copy()
    output[TBLXDM_FIELD_NAME] = output[tblxdm_column].map(crop_code_text)
    output = output[output[TBLXDM_FIELD_NAME] != ""].copy()
    if output.empty:
        raise ValueError(f"分类矢量 {TBLXDM_FIELD_NAME} 字段没有有效作物代码。")
    output[CLASS_FIELD_NAME] = output[TBLXDM_FIELD_NAME]
    output[ZZLX_FIELD_NAME] = output[TBLXDM_FIELD_NAME]
    return output


def find_zzlx_column(gdf: gpd.GeoDataFrame) -> str | None:
    for column in gdf.columns:
        if column.upper() == ZZLX_FIELD_NAME:
            return column
    for column in gdf.columns:
        if column.upper() == ZWMC_FIELD_NAME:
            return column
    return None


def has_rape_value(gdf: gpd.GeoDataFrame, zzlx_column: str | None) -> bool:
    if not zzlx_column:
        return False
    return gdf[zzlx_column].astype(str).str.contains(RAPE_VALUE, na=False).any()


def normalize_zzlx_column(gdf: gpd.GeoDataFrame) -> gpd.GeoDataFrame:
    zzlx_column = find_zzlx_column(gdf)
    if not zzlx_column:
        return gdf
    if zzlx_column == ZZLX_FIELD_NAME:
        return gdf

    gdf = gdf.copy()
    if ZZLX_FIELD_NAME not in gdf.columns:
        gdf[ZZLX_FIELD_NAME] = gdf[zzlx_column]
    else:
        gdf[ZZLX_FIELD_NAME] = gdf[ZZLX_FIELD_NAME].fillna(gdf[zzlx_column])
    return gdf


def find_class_column(gdf: gpd.GeoDataFrame) -> str | None:
    lower_lookup = {str(column).lower(): column for column in gdf.columns}
    return lower_lookup.get(CLASS_FIELD_NAME)


def normalize_class_from_zwmc(gdf: gpd.GeoDataFrame) -> gpd.GeoDataFrame:
    zwmc_column = find_column_case_insensitive(gdf.columns, ZWMC_FIELD_NAME)
    if zwmc_column is None:
        return gdf

    output = gdf.copy()
    inferred_class = output[zwmc_column].map(infer_class_from_zwmc)
    has_inferred = inferred_class.notna()
    if not bool(has_inferred.any()):
        return output

    if CLASS_FIELD_NAME not in output.columns:
        output[CLASS_FIELD_NAME] = inferred_class
    else:
        output[CLASS_FIELD_NAME] = output[CLASS_FIELD_NAME].where(output[CLASS_FIELD_NAME].notna(), inferred_class)

    if ZZLX_FIELD_NAME not in output.columns:
        output[ZZLX_FIELD_NAME] = output[zwmc_column]
    else:
        output[ZZLX_FIELD_NAME] = output[ZZLX_FIELD_NAME].where(output[ZZLX_FIELD_NAME].notna(), output[zwmc_column])
    return output


def normalize_class_column(
    gdf: gpd.GeoDataFrame,
    class_mapping: list[dict[str, object]] | None = None,
) -> gpd.GeoDataFrame:
    return normalize_tblxdm_column(gdf)

def class_numeric_series(gdf: gpd.GeoDataFrame) -> pd.Series:
    if TBLXDM_FIELD_NAME not in gdf.columns:
        raise ValueError(f"分类矢量缺少 {TBLXDM_FIELD_NAME} 字段，无法按作物代码生成样方。")
    return gdf[TBLXDM_FIELD_NAME].map(crop_code_text)

def add_sample_record(
    records: list[dict[str, object]],
    city_name: str,
    county_name: str,
    source_name: str,
    sample_class: str,
    sample_class_code: object,
    overlap_area: float,
    x: float,
    y: float,
    sample_square: object,
) -> None:
    records.append(
        {
            "sample_id": len(records) + 1,
            "city": city_name,
            "county": county_name,
            "sample_cls": sample_class,
            "class": sample_class_code,
            TBLXDM_FIELD_NAME: crop_code_text(sample_class_code),
            TBLXMC_FIELD_NAME: str(sample_class),
            "overlap": round(float(overlap_area), 3),
            "center_x": float(x),
            "center_y": float(y),
            "src_name": source_name[:80],
            "geometry": sample_square,
        }
    )


def build_overlap_samples(
    target_geom: object,
    admin_geom: object | None,
    city_name: str,
    county_name: str,
    source_name: str,
    sample_class: str,
    sample_class_code: object,
    rng: random.Random,
    sample_count: int,
    min_distance: float,
    square_size: float,
    min_overlap_area: float | None,
    min_overlap_ratio: float,
    max_attempts: int,
    max_no_progress_attempts: int,
    center_index: CenterDistanceIndex,
    records: list[dict[str, object]],
) -> int:
    prepared_geom = prep(target_geom)
    prepared_admin_geom = prep(admin_geom) if admin_geom is not None else None
    minx, miny, maxx, maxy = target_geom.bounds
    if minx == maxx or miny == maxy:
        raise ValueError(f"{sample_class} 目标图斑范围异常，无法生成样方。")

    half_size = square_size / 2.0
    square_area = square_size * square_size
    effective_min_overlap_area = min_overlap_area if min_overlap_area is not None else square_area * min_overlap_ratio
    start_count = len(records)
    attempts = 0
    attempts_since_success = 0

    while (
        len(records) - start_count < sample_count
        and attempts < max_attempts
        and attempts_since_success < max_no_progress_attempts
    ):
        attempts += 1
        attempts_since_success += 1
        x = rng.uniform(minx, maxx)
        y = rng.uniform(miny, maxy)

        if not center_index.can_add(x, y):
            continue

        sample_square = box(x - half_size, y - half_size, x + half_size, y + half_size)
        if not square_inside_prepared_admin(sample_square, prepared_admin_geom):
            continue

        if not prepared_geom.intersects(sample_square):
            continue

        if prepared_geom.contains(sample_square):
            overlap_area = square_area
        else:
            overlap_area = sample_square.intersection(target_geom).area
        if overlap_area < effective_min_overlap_area:
            continue

        center_index.add(x, y)
        attempts_since_success = 0
        add_sample_record(
            records=records,
            city_name=city_name,
            county_name=county_name,
            source_name=source_name,
            sample_class=sample_class,
            sample_class_code=sample_class_code,
            overlap_area=overlap_area,
            x=x,
            y=y,
            sample_square=sample_square,
        )

    generated_count = len(records) - start_count
    success_rate = generated_count / attempts if attempts else 0
    print(
        f"{county_name} {sample_class}: generated {generated_count}/{sample_count}, "
        f"attempts {attempts}, success rate {success_rate:.2%}"
    )
    if generated_count < sample_count:
        print(
            f"WARNING: {county_name} {sample_class} only generated "
            f"{generated_count}/{sample_count} samples after {attempts} attempts. "
            f"Effective overlap limit: {effective_min_overlap_area:.3f} sq.m."
        )
    return generated_count


def build_non_overlap_samples(
    all_geom: object,
    admin_geom: object | None,
    city_name: str,
    county_name: str,
    source_name: str,
    rng: random.Random,
    sample_count: int,
    min_distance: float,
    square_size: float,
    max_attempts: int,
    max_no_progress_attempts: int,
    other_search_buffer: float,
    center_index: CenterDistanceIndex,
    records: list[dict[str, object]],
) -> int:
    prepared_all_geom = prep(all_geom)
    prepared_admin_geom = prep(admin_geom) if admin_geom is not None else None
    if admin_geom is None:
        minx, miny, maxx, maxy = all_geom.bounds
        minx -= other_search_buffer
        miny -= other_search_buffer
        maxx += other_search_buffer
        maxy += other_search_buffer
    else:
        minx, miny, maxx, maxy = admin_geom.bounds

    half_size = square_size / 2.0
    start_count = len(records)
    attempts = 0
    attempts_since_success = 0

    while (
        len(records) - start_count < sample_count
        and attempts < max_attempts
        and attempts_since_success < max_no_progress_attempts
    ):
        attempts += 1
        attempts_since_success += 1
        x = rng.uniform(minx, maxx)
        y = rng.uniform(miny, maxy)

        if not center_index.can_add(x, y):
            continue

        sample_square = box(x - half_size, y - half_size, x + half_size, y + half_size)
        if not square_inside_prepared_admin(sample_square, prepared_admin_geom):
            continue

        if prepared_all_geom.intersects(sample_square):
            continue

        center_index.add(x, y)
        attempts_since_success = 0
        add_sample_record(
            records=records,
            city_name=city_name,
            county_name=county_name,
            source_name=source_name,
            sample_class=CLASS_OTHER,
            sample_class_code=None,
            overlap_area=0.0,
            x=x,
            y=y,
            sample_square=sample_square,
        )

    generated_count = len(records) - start_count
    success_rate = generated_count / attempts if attempts else 0
    print(
        f"{county_name} {CLASS_OTHER}: generated {generated_count}/{sample_count}, "
        f"attempts {attempts}, success rate {success_rate:.2%}"
    )
    if generated_count < sample_count:
        print(
            f"WARNING: {county_name} {CLASS_OTHER} only generated "
            f"{generated_count}/{sample_count} samples after {attempts} attempts."
        )
    return generated_count


def assign_sample_crop_codes(samples_gdf: gpd.GeoDataFrame, crop_gdf: gpd.GeoDataFrame) -> gpd.GeoDataFrame:
    keep_fields = [field for field in (TBLXMC_FIELD_NAME, TBLXDM_FIELD_NAME, "geometry") if field in crop_gdf.columns]
    crop_gdf = crop_gdf[keep_fields].copy()
    spatial_index = crop_gdf.sindex
    output = samples_gdf.copy()
    for sample_index, sample_row in output.iterrows():
        sample_geom = sample_row.geometry
        best_code = ""
        best_name = ""
        best_area = 0.0
        try:
            candidate_indices = spatial_index.query(sample_geom, predicate="intersects")
        except Exception:
            candidate_indices = crop_gdf[crop_gdf.intersects(sample_geom)].index
        for candidate_index in list(candidate_indices):
            crop_row = crop_gdf.iloc[int(candidate_index)]
            crop_geom = extract_polygonal_geometry(crop_row.geometry)
            if crop_geom is None:
                continue
            try:
                overlap_area = float(sample_geom.intersection(crop_geom).area)
            except Exception:
                continue
            if overlap_area > best_area:
                best_area = overlap_area
                best_code = crop_code_text(crop_row[TBLXDM_FIELD_NAME])
                best_name = normalize_mapping_text(crop_row.get(TBLXMC_FIELD_NAME)) or best_code
        if not best_code:
            best_code = crop_code_text(sample_row.get(TBLXDM_FIELD_NAME))
        if not best_name:
            best_name = normalize_mapping_text(sample_row.get(TBLXMC_FIELD_NAME)) or best_code
        output.at[sample_index, TBLXDM_FIELD_NAME] = best_code
        output.at[sample_index, TBLXMC_FIELD_NAME] = best_name
        output.at[sample_index, CLASS_FIELD_NAME] = best_code
        output.at[sample_index, "sample_cls"] = best_name
    return output

def build_samples(
    county_gdf: gpd.GeoDataFrame,
    admin_geom: object | None,
    city_name: str,
    county_name: str,
    source_name: str,
    rng: random.Random,
    sample_count: int,
    min_distance: float,
    square_size: float,
    min_overlap_area: float | None,
    min_overlap_ratio: float,
    max_attempts: int,
    max_no_progress_attempts: int,
    other_search_buffer: float,
) -> tuple[gpd.GeoDataFrame, bool]:
    crop_gdf = county_gdf[county_gdf[TBLXDM_FIELD_NAME].map(crop_code_text) != ""].copy()
    crop_gdf = keep_polygonal_geometries(crop_gdf)
    if crop_gdf.empty:
        raise ValueError("输入 shp 没有有效农作物用地图斑。")

    crop_gdf["_sample_area"] = crop_gdf.geometry.area.astype(float)
    crop_gdf = crop_gdf[crop_gdf["_sample_area"] > 0].copy().reset_index(drop=True)
    if crop_gdf.empty:
        raise ValueError("输入 shp 没有面积大于 0 的农作物用地图斑。")

    total_area = float(crop_gdf["_sample_area"].sum())
    code_counts = crop_gdf[TBLXDM_FIELD_NAME].value_counts().head(10).to_dict()
    print(f"[01] 农作物图斑总面积={total_area:.2f}；TBLXDM 前10类计数={code_counts}", flush=True)
    cumulative_areas = []
    running_area = 0.0
    for area in crop_gdf["_sample_area"]:
        running_area += float(area)
        cumulative_areas.append(running_area)

    crop_index = crop_gdf.sindex
    prepared_admin_geom = prep(admin_geom) if admin_geom is not None else None
    center_index = CenterDistanceIndex(min_distance)
    records: list[dict[str, object]] = []
    half_size = square_size / 2.0
    square_area = square_size * square_size
    effective_min_overlap_area = min_overlap_area if min_overlap_area is not None else square_area * min_overlap_ratio
    attempts = 0
    attempts_since_success = 0

    while (
        len(records) < sample_count
        and attempts < max_attempts
        and attempts_since_success < max_no_progress_attempts
    ):
        attempts += 1
        attempts_since_success += 1
        if attempts % 5000 == 0:
            print(f"{county_name} 农作物样方: generated {len(records)}/{sample_count}, attempts {attempts}", flush=True)

        row_pos = bisect.bisect_left(cumulative_areas, rng.random() * total_area)
        if row_pos >= len(crop_gdf):
            row_pos = len(crop_gdf) - 1
        target_row = crop_gdf.iloc[row_pos]
        target_geom = extract_polygonal_geometry(target_row.geometry)
        if target_geom is None:
            continue

        minx, miny, maxx, maxy = target_geom.bounds
        if minx == maxx or miny == maxy:
            continue
        x = rng.uniform(minx, maxx)
        y = rng.uniform(miny, maxy)
        if not target_geom.intersects(Point(x, y)):
            continue
        if not center_index.can_add(x, y):
            continue

        sample_square = box(x - half_size, y - half_size, x + half_size, y + half_size)
        if not square_inside_prepared_admin(sample_square, prepared_admin_geom):
            continue

        try:
            candidate_indices = list(crop_index.query(sample_square, predicate="intersects"))
        except Exception:
            candidate_indices = list(crop_gdf[crop_gdf.intersects(sample_square)].index)
        if not candidate_indices:
            continue

        overlap_area = 0.0
        best_code = ""
        best_name = ""
        best_area = 0.0
        for candidate_index in candidate_indices:
            candidate_row = crop_gdf.iloc[int(candidate_index)]
            candidate_geom = extract_polygonal_geometry(candidate_row.geometry)
            if candidate_geom is None:
                continue
            try:
                part_area = float(sample_square.intersection(candidate_geom).area)
            except Exception:
                continue
            if part_area <= 0:
                continue
            overlap_area += part_area
            if part_area > best_area:
                best_area = part_area
                best_code = crop_code_text(candidate_row[TBLXDM_FIELD_NAME])
                best_name = normalize_mapping_text(candidate_row.get(TBLXMC_FIELD_NAME)) or best_code

        if overlap_area < effective_min_overlap_area:
            continue

        center_index.add(x, y)
        attempts_since_success = 0
        add_sample_record(
            records=records,
            city_name=city_name,
            county_name=county_name,
            source_name=source_name,
            sample_class=best_name or best_code,
            sample_class_code=best_code,
            overlap_area=overlap_area,
            x=x,
            y=y,
            sample_square=sample_square,
        )

    success_rate = len(records) / attempts if attempts else 0
    print(
        f"{county_name} 农作物样方: generated {len(records)}/{sample_count}, "
        f"attempts {attempts}, success rate {success_rate:.2%}",
        flush=True,
    )
    if not records:
        raise ValueError("未生成任何合格样方，请检查矢量面积、坐标系或参数。")
    if len(records) < sample_count:
        print(
            f"WARNING: {county_name} 农作物样方 only generated "
            f"{len(records)}/{sample_count} samples after {attempts} attempts. "
            f"Effective overlap limit: {effective_min_overlap_area:.3f} sq.m.",
            flush=True,
        )

    samples = gpd.GeoDataFrame(records, geometry="geometry", crs=county_gdf.crs)
    return samples, False

def build_center_points(samples_gdf: gpd.GeoDataFrame) -> gpd.GeoDataFrame:
    point_records = []
    for _, row in samples_gdf.iterrows():
        point_records.append(
            {
                "sample_id": row["sample_id"],
                "city": row["city"],
                "county": row["county"],
                "sample_cls": row["sample_cls"],
                "overlap": row["overlap"],
                "geometry": Point(float(row["center_x"]), float(row["center_y"])),
            }
        )
    return gpd.GeoDataFrame(point_records, geometry="geometry", crs=samples_gdf.crs)


def generate_for_shp(
    shp_path: Path,
    source_root: Path,
    output_root: Path,
    args: argparse.Namespace,
    rng: random.Random,
) -> bool:
    city_name = args.city_name or get_city_name(shp_path, source_root)
    county_name = get_county_name(shp_path, city_name)
    city_output_dir = output_root / safe_filename(city_name)
    city_output_dir.mkdir(parents=True, exist_ok=True)

    print(f"Processing: {shp_path}")
    raw_gdf = gpd.read_file(shp_path)
    raw_gdf = normalize_class_column(normalize_zzlx_column(raw_gdf), getattr(args, "flex_class_mapping", None))
    county_gdf = to_metric_crs(raw_gdf, args.resolved_target_crs)
    admin_geom = get_admin_geom_from_cache(args, city_name, county_name)
    if admin_geom is None:
        admin_geom = load_admin_boundary(getattr(args, "admin_boundary", None), county_name, args.resolved_target_crs)

    samples_gdf, has_rape = build_samples(
        county_gdf=county_gdf,
        admin_geom=admin_geom,
        city_name=city_name,
        county_name=county_name,
        source_name=shp_path.name,
        rng=rng,
        sample_count=args.sample_count,
        min_distance=args.min_distance,
        square_size=args.square_size,
        min_overlap_area=args.min_overlap_area,
        min_overlap_ratio=args.min_overlap_ratio,
        max_attempts=args.max_attempts,
        max_no_progress_attempts=args.max_no_progress_attempts,
        other_search_buffer=args.other_search_buffer,
    )
    min_center_distance = qc_center_distances(samples_gdf, args.min_distance)
    print(f"Center distance QC passed. Minimum distance: {min_center_distance:.3f}m")
    qc_samples_inside_admin(samples_gdf, admin_geom)
    if admin_geom is not None:
        print("Admin boundary QC passed. All samples are inside the administrative boundary.")

    county_file_name = safe_filename(county_name)
    sample_path = city_output_dir / f"{county_file_name}_样方.shp"
    if sample_path.exists():
        remove_existing_shapefile(sample_path)
    samples_gdf.to_file(sample_path, encoding="UTF-8")
    print(f"Saved samples: {sample_path}")

    if args.write_points:
        point_path = city_output_dir / f"{county_file_name}_样点.shp"
        if point_path.exists():
            remove_existing_shapefile(point_path)
        build_center_points(samples_gdf).to_file(point_path, encoding="UTF-8")
        print(f"Saved points: {point_path}")

    print(f"Sample class count: {samples_gdf['sample_cls'].value_counts().to_dict()}")
    return has_rape


def save_sample_outputs(
    samples_gdf: gpd.GeoDataFrame,
    city_name: str,
    county_name: str,
    output_root: Path,
    write_points: bool,
) -> None:
    city_output_dir = output_root / safe_filename(city_name)
    city_output_dir.mkdir(parents=True, exist_ok=True)

    county_file_name = safe_filename(county_name)
    sample_path = city_output_dir / f"{county_file_name}_样方.shp"
    if sample_path.exists():
        remove_existing_shapefile(sample_path)
    samples_gdf.to_file(sample_path, encoding="UTF-8")
    print(f"Saved samples: {sample_path}")

    if write_points:
        point_path = city_output_dir / f"{county_file_name}_样点.shp"
        if point_path.exists():
            remove_existing_shapefile(point_path)
        build_center_points(samples_gdf).to_file(point_path, encoding="UTF-8")
        print(f"Saved points: {point_path}")


def sample_output_path(output_root: Path, city_name: str, county_name: str) -> Path:
    return output_root / safe_filename(city_name) / f"{safe_filename(county_name)}_样方.shp"


def generate_for_admin_unit(
    admin_unit: dict[str, object],
    classification_gdf: gpd.GeoDataFrame,
    output_root: Path,
    args: argparse.Namespace,
    rng: random.Random,
) -> bool:
    city_name = str(admin_unit["city"])
    county_name = str(admin_unit["county"])
    admin_geom = admin_unit["geometry"]

    print(f"Processing new admin boundary: {city_name}-{county_name}")
    sample_path = sample_output_path(output_root, city_name, county_name)
    if args.mode == "skip" and sample_path.exists():
        print(f"[跳过] 样方已存在：{sample_path}")
        return False

    print(f"[01] {city_name}-{county_name} 开始从分类总表筛选图斑", flush=True)
    county_gdf = select_classification_for_admin(classification_gdf, admin_geom)
    print(f"[01] {city_name}-{county_name} 筛选后图斑数：{len(county_gdf)}", flush=True)
    if county_gdf.empty:
        raise ValueError("新行政边界内没有匹配到任何分类图斑。")

    samples_gdf, has_rape = build_samples(
        county_gdf=county_gdf,
        admin_geom=admin_geom,
        city_name=city_name,
        county_name=county_name,
        source_name=f"新边界裁切:{admin_unit['source']}",
        rng=rng,
        sample_count=args.sample_count,
        min_distance=args.min_distance,
        square_size=args.square_size,
        min_overlap_area=args.min_overlap_area,
        min_overlap_ratio=args.min_overlap_ratio,
        max_attempts=args.max_attempts,
        max_no_progress_attempts=args.max_no_progress_attempts,
        other_search_buffer=args.other_search_buffer,
    )

    min_center_distance = qc_center_distances(samples_gdf, args.min_distance)
    print(f"Center distance QC passed. Minimum distance: {min_center_distance:.3f}m")
    qc_samples_inside_admin(samples_gdf, admin_geom)
    print("Admin boundary QC passed. All samples are inside the new administrative boundary.")

    save_sample_outputs(
        samples_gdf=samples_gdf,
        city_name=city_name,
        county_name=county_name,
        output_root=output_root,
        write_points=args.write_points,
    )
    print(f"Sample class count: {samples_gdf['sample_cls'].value_counts().to_dict()}")
    return has_rape


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="按 00县边界 的 area_name 和空间范围，从 00分类结果 的 TBLXDM 作物代码生成区县自检样方。"
    )
    parser.add_argument(
        "--source_root",
        type=Path,
        nargs="?",
        default=DEFAULT_SOURCE_ROOT,
        help="原始分类矢量根目录，递归查找分类 shp；默认 自检样方/00分类结果。",
    )
    parser.add_argument(
        "--output_root",
        type=Path,
        nargs="?",
        default=DEFAULT_OUTPUT_ROOT,
        help="输出根目录，结果按市级文件夹保存；默认 自检样方/01生成样方。",
    )
    parser.add_argument("--sample-count", type=int, default=DEFAULT_SAMPLE_COUNT, help="每个区县生成的样方数量。")
    parser.add_argument("--min-distance", type=float, default=DEFAULT_MIN_DISTANCE, help="样点最小间距，单位米。")
    parser.add_argument("--square-size", type=float, default=DEFAULT_SQUARE_SIZE, help="样方边长，单位米。")
    parser.add_argument(
        "--min-overlap-ratio",
        type=float,
        default=DEFAULT_MIN_OVERLAP_RATIO,
        help="样方与农作物用地图斑的最小重叠比例，默认 0.2，即样方面积的 20%%。",
    )
    parser.add_argument(
        "--min-overlap-area",
        type=float,
        default=None,
        help="手动指定最小重叠面积，单位平方米。指定后优先于 --min-overlap-ratio。",
    )
    parser.add_argument("--max-attempts", type=int, default=DEFAULT_MAX_ATTEMPTS, help="每个区县最大随机尝试次数。")
    parser.add_argument(
        "--max-no-progress-attempts",
        type=int,
        default=DEFAULT_MAX_NO_PROGRESS_ATTEMPTS,
        help="连续多少次没有新增合格样方后提前停止，适合小区划或图斑很少的情况。",
    )
    parser.add_argument(
        "--other-search-buffer",
        type=float,
        default=DEFAULT_OTHER_SEARCH_BUFFER,
        help="兼容旧参数，当前 TBLXDM 农作物样方流程不再使用。",
    )
    parser.add_argument("--only-dir-name", default=None, help="只处理位于指定文件夹名下的 shp。")
    parser.add_argument(
        "--field-mapping",
        type=Path,
        default=DEFAULT_FIELD_MAPPING_PATH,
        help="可选分类映射 CSV。当前流程优先使用分类矢量 TBLXDM 字段。",
    )
    parser.add_argument(
        "--failed-log",
        type=Path,
        default=None,
        help="只重跑日志中 FAILED 的区划，例如 .\\问题部分.txt。按 市-区县 精确匹配新边界。",
    )
    parser.add_argument(
        "--only-boundary-name",
        nargs="*",
        default=None,
        help="只处理指定区划；可写 区县名 或 市-区县，例如 仪征市 或 南京市-鼓楼区。",
    )
    parser.add_argument("--city-name", default=None, help="手动指定市级输出文件夹名，例如 常州市。适合输入路径中没有市级目录时使用。")
    parser.add_argument(
        "--city-boundary",
        type=Path,
        default=DEFAULT_CITY_BOUNDARY_PATH,
        help="市边界 shp 或目录，用于按空间位置给县匹配市名；默认 自检样方/00市边界。",
    )
    parser.add_argument("--write-points", action="store_true", help="同时输出样方中心点 shp。")
    parser.add_argument("--mode", choices=("skip", "overwrite"), default="skip", help="skip 跳过已有样方；overwrite 重新生成并覆盖。")
    parser.add_argument("--seed", type=int, default=20260606, help="随机种子，保证重复运行结果可复现。")
    return parser.parse_args()

def get_source_root():
    args = parse_args()
    source_root = args.source_root
    return source_root


def main() -> None:
    args = parse_args()
    source_root = args.source_root
    output_root = args.output_root

    if not source_root.exists():
        raise FileNotFoundError(f"输入目录不存在: {source_root}")
    if not 0 <= args.min_overlap_ratio <= 1:
        raise ValueError("--min-overlap-ratio 必须在 0 到 1 之间，例如 20% 写 0.2。")

    args.resolved_target_crs = resolve_target_crs()
    args.flex_class_mapping = load_flexible_class_mapping(args.field_mapping)

    total_start_time = time.perf_counter()
    rng = random.Random(args.seed)
    success_count = 0
    failed_count = 0
    rape_count = 0
    rape_regions: list[str] = []

    shp_paths = find_input_shps(source_root, args.only_dir_name)
    if not shp_paths:
        raise FileNotFoundError(f"没有找到需要处理的 shp: {source_root}")
    if not NEW_ADMIN_BOUNDARY_PATH.exists():
        raise FileNotFoundError(f"新行政边界不存在: {NEW_ADMIN_BOUNDARY_PATH}")

    print(f"Hard-coded target CRS: {args.resolved_target_crs.name}")
    print(f"Hard-coded new admin boundary: {NEW_ADMIN_BOUNDARY_PATH}")
    print(f"City boundary reference: {args.city_boundary}")
    load_start_time = time.perf_counter()
    classification_gdf = load_classification_gdf(shp_paths, args.resolved_target_crs, args.flex_class_mapping)
    admin_units = load_admin_units(NEW_ADMIN_BOUNDARY_PATH, args.resolved_target_crs, args.city_name, args.city_boundary)
    city_county_filters, county_filters = parse_boundary_filter_values(args.only_boundary_name)
    if args.failed_log is not None:
        city_county_filters.update(parse_failed_sample_units(args.failed_log))
    if city_county_filters or county_filters:
        original_admin_count = len(admin_units)
        admin_units = filter_admin_units(admin_units, city_county_filters, county_filters)
        if not admin_units:
            raise ValueError("按 --failed-log/--only-boundary-name 没有匹配到任何新边界区划。")
        target_names = [f"{unit['city']}-{unit['county']}" for unit in admin_units]
        print(
            f"Filtered admin units: {len(admin_units)}/{original_admin_count}; "
            f"targets={', '.join(target_names)}"
        )
    original_admin_count = len(admin_units)
    admin_units = filter_admin_units_by_classification(admin_units, classification_gdf)
    if not admin_units:
        raise ValueError("00县边界 中没有任何区县与 00分类结果 的矢量空间相交。")
    if len(admin_units) != original_admin_count:
        print(f"Spatially matched admin units: {len(admin_units)}/{original_admin_count}")
    print(f"Loaded source classification shp count: {len(shp_paths)}")
    print(f"Loaded new admin units: {len(admin_units)}")
    print(f"Data loading elapsed: {format_elapsed(time.perf_counter() - load_start_time)}")
    print("Start sample generation from new admin boundary.")

    for index, admin_unit in enumerate(admin_units, start=1):
        city_name = str(admin_unit["city"])
        county_name = str(admin_unit["county"])
        try:
            unit_start_time = time.perf_counter()
            print(f"[Sample {index}/{len(admin_units)}] Start: {city_name}-{county_name}")
            has_rape = generate_for_admin_unit(admin_unit, classification_gdf, output_root, args, rng)
            success_count += 1
            print(
                f"[Sample {index}/{len(admin_units)}] Finished in "
                f"{format_elapsed(time.perf_counter() - unit_start_time)}"
            )
            if has_rape:
                rape_count += 1
                rape_regions.append(f"{city_name}-{county_name}")
        except Exception as exc:
            failed_count += 1
            print(f"FAILED: {city_name}-{county_name} - {exc}")

    print(f"Done. Success: {success_count}, failed: {failed_count}")
    print(f"Total elapsed: {format_elapsed(time.perf_counter() - total_start_time)}")
    print(f"TBLXDM crop sample regions: {success_count}")
    if rape_regions:
        print("Rape region list:")
        for region in rape_regions:
            print(f"  {region}")


if __name__ == "__main__":
    main()


