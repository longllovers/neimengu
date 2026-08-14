from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path

import fiona
import rasterio

from .counties import load_counties
from .crs_check import check_albers_crs
from .models import CheckResult
from .specs import SCHEMAS
from .vector_check import check_vector, check_vector_crs

ROOT_RE = re.compile(r"^EL_(\d{6})_(\d{4})$")
GDB_RE = re.compile(r"^ELCLTB(\d{6})_(\d{4})(?:_(\d+))?\.gdb$", re.IGNORECASE)
LAYER_RE = re.compile(r"^ELCLTB_?(\d{6})(?:_(.+))?$", re.IGNORECASE)
IMAGE_RE = re.compile(
    r"^ELDOM(?P<county>\d{6})_(?P<start>\d{8})-(?P<end>\d{8})_"
    r"(?P<resolution>\d+(?:\.\d+)?)m\.tif$",
    re.IGNORECASE,
)
SYSTEM_LAYER_RE = re.compile(r"^T_\d+_(DirtyAreas|PointErrors|LineErrors|PolyErrors)$")
REQUIRED_SHP_PARTS = {".shp", ".shx", ".dbf", ".prj"}


def _validate_shapefile_parts(result: CheckResult, shp: Path) -> None:
    existing = {item.suffix.lower() for item in shp.parent.glob(shp.stem + ".*") if item.is_file()}
    missing = sorted(REQUIRED_SHP_PARTS - existing)
    if missing:
        result.add(
            "ERROR",
            "SHP_PARTS_MISSING",
            shp,
            "Shapefile 配套文件不完整",
            expected=".shp、.shx、.dbf、.prj",
            actual="缺少 " + "、".join(missing),
        )


def _county_dir(
    result: CheckResult,
    folder: Path,
    counties: dict[str, str],
) -> str | None:
    code = folder.name
    if not re.fullmatch(r"\d{6}", code):
        result.add("ERROR", "COUNTY_FOLDER_NAME", folder, "县目录必须使用 6 位县代码")
        return None
    if code not in counties:
        result.add("ERROR", "COUNTY_FOLDER_CODE", folder, "县目录代码不在 00县边界 中")
        return None
    return code


def _check_named_county_shps(
    result: CheckResult,
    base: Path,
    prefix: str,
    year: str,
    counties: dict[str, str],
    schema_name: str,
) -> None:
    if not base.is_dir():
        result.add("ERROR", "FOLDER_MISSING", base, f"缺少目录 {base.name}")
        return
    county_folders = [item for item in base.iterdir() if item.is_dir()]
    if not county_folders:
        result.add("ERROR", "COUNTY_FOLDERS_MISSING", base, "没有县级子目录")
    for folder in county_folders:
        county = _county_dir(result, folder, counties)
        if not county:
            continue
        expected = folder / f"{prefix}{county}_{year}.shp"
        shps = list(folder.glob("*.shp"))
        if not expected.is_file():
            result.add(
                "ERROR",
                "SHP_NAME",
                folder,
                "未找到符合命名规范的 Shapefile",
                expected=expected.name,
                actual="、".join(item.name for item in shps) or "无 .shp 文件",
            )
        for shp in shps:
            _validate_shapefile_parts(result, shp)
            if shp.name != expected.name:
                result.add(
                    "ERROR",
                    "SHP_NAME",
                    shp,
                    "Shapefile 文件名不符合规范",
                    expected=expected.name,
                    actual=shp.name,
                )
            check_vector(
                result,
                shp,
                SCHEMAS[schema_name],
                schema_name=schema_name,
                valid_counties=set(counties),
                expected_county=county,
            )


def _check_images(
    result: CheckResult,
    base: Path,
    province: str,
    year: str,
    counties: dict[str, str],
) -> None:
    if not base.is_dir():
        result.add("ERROR", "FOLDER_MISSING", base, "缺少测量用影像目录 ELCLDOM")
        return
    for folder in [item for item in base.iterdir() if item.is_dir() and item.name != "ELDOMMD"]:
        county = _county_dir(result, folder, counties)
        if not county:
            continue
        images = list(folder.glob("*.tif"))
        if len(images) < 2:
            result.add(
                "ERROR",
                "IMAGE_COUNT",
                folder,
                f"县级测量影像少于两景：当前 {len(images)} 景",
                expected="一般至少两景全覆盖影像",
                actual=str(len(images)),
            )
        for image in images:
            try:
                with rasterio.open(image) as raster:
                    check_albers_crs(result, image, raster.crs)
            except Exception as exc:
                result.add("ERROR", "RASTER_OPEN", image, f"无法读取影像坐标系：{exc}")
            match = IMAGE_RE.match(image.name)
            if not match:
                result.add(
                    "ERROR",
                    "IMAGE_NAME",
                    image,
                    "影像文件名不符合规范",
                    expected=f"ELDOM{county}_YYYYMMDD-YYYYMMDD_分辨率m.tif",
                    actual=image.name,
                )
                continue
            if match.group("county") != county:
                result.add("ERROR", "IMAGE_COUNTY_MISMATCH", image, "影像名县代码与所在县目录不一致")
            try:
                start = datetime.strptime(match.group("start"), "%Y%m%d")
                end = datetime.strptime(match.group("end"), "%Y%m%d")
                if start > end:
                    raise ValueError("开始日期晚于结束日期")
            except ValueError as exc:
                result.add("ERROR", "IMAGE_DATE", image, f"影像日期范围无效：{exc}")

        for auxiliary in folder.glob("*.ovr"):
            base_name = auxiliary.name[:-4]
            if base_name.lower().endswith(".tif"):
                source = folder / base_name
            else:
                source = folder / (base_name + ".tif")
            if not source.exists():
                result.add("WARNING", "ORPHAN_AUXILIARY", auxiliary, "发现没有对应 .tif 的影像辅助文件")

    metadata_dir = base / "ELDOMMD"
    expected = metadata_dir / f"ELDOMMD{province}_{year}.shp"
    if not expected.is_file():
        actual = "、".join(item.name for item in metadata_dir.glob("*.shp")) if metadata_dir.exists() else "目录不存在"
        result.add(
            "ERROR",
            "ELDOMMD_MISSING",
            metadata_dir,
            "影像元文件缺失或命名不正确",
            expected=expected.name,
            actual=actual or "无 .shp 文件",
        )
    else:
        _validate_shapefile_parts(result, expected)
        check_vector_crs(result, expected)


def _check_gdbs(
    result: CheckResult,
    root: Path,
    province: str,
    year: str,
    counties: dict[str, str],
    schema_name: str,
) -> None:
    gdbs = [item for item in root.iterdir() if item.is_dir() and item.suffix.lower() == ".gdb"]
    if not gdbs:
        result.add("ERROR", "GDB_MISSING", root, "没有找到成果 GDB")
        return
    for gdb in gdbs:
        match = GDB_RE.match(gdb.name)
        if not match or match.group(1) != province or match.group(2) != year:
            result.add(
                "ERROR",
                "GDB_NAME",
                gdb,
                "GDB 名称不符合规范",
                expected=f"ELCLTB{province}_{year}.gdb（分块可加 _1、_2）",
                actual=gdb.name,
            )
        try:
            layers = fiona.listlayers(gdb)
        except Exception as exc:
            result.add("ERROR", "GDB_OPEN", gdb, f"无法打开 GDB：{exc}")
            continue
        business_layers = [layer for layer in layers if not SYSTEM_LAYER_RE.match(layer)]
        if not business_layers:
            result.add("ERROR", "GDB_LAYER_MISSING", gdb, "GDB 中没有成果要素类")
            continue
        gdb_records = 0
        for layer in business_layers:
            layer_match = LAYER_RE.match(layer)
            county: str | None = None
            if not layer_match:
                result.add(
                    "ERROR",
                    "GDB_LAYER_NAME",
                    f"{gdb} :: {layer}",
                    "成果要素类名称不符合 ELCLTB_<6位县代码>_<县名> 形式",
                )
            else:
                county = layer_match.group(1)
                if county not in counties:
                    result.add("ERROR", "GDB_LAYER_COUNTY", f"{gdb} :: {layer}", "要素类县代码不在县界中")
                supplied_name = (layer_match.group(2) or "").strip()
                expected_name = counties.get(county, "")
                if supplied_name and expected_name and supplied_name != expected_name:
                    result.add(
                        "ERROR",
                        "GDB_LAYER_COUNTY_NAME",
                        f"{gdb} :: {layer}",
                        "要素类中的县名与县界不一致",
                        expected=expected_name,
                        actual=supplied_name,
                    )
            gdb_records += check_vector(
                result,
                gdb,
                SCHEMAS[schema_name],
                layer=layer,
                schema_name=schema_name,
                valid_counties=set(counties),
                expected_county=county,
            )
        if gdb_records > 1_000_000:
            result.add(
                "ERROR",
                "GDB_RECORD_LIMIT",
                gdb,
                "单个 GDB 记录数超过 100 万条",
                expected="不超过 1000000",
                actual=str(gdb_records),
            )


def _check_zpj(
    result: CheckResult,
    base: Path,
    province: str,
    year: str,
    counties: dict[str, str],
    schema_name: str,
) -> None:
    if not base.is_dir():
        result.add("ERROR", "FOLDER_MISSING", base, "缺少精度自评目录 ELJDZPJ")
        return
    expected = base / f"ELJDZPJ{province}_{year}.shp"
    shps = list(base.glob("*.shp"))
    if not expected.is_file():
        result.add(
            "ERROR",
            "ZPJ_MISSING",
            base,
            "精度自评 Shapefile 缺失或命名不正确",
            expected=expected.name,
            actual="、".join(item.name for item in shps) or "无 .shp 文件",
        )
    for shp in shps:
        _validate_shapefile_parts(result, shp)
        if shp.name != expected.name:
            result.add(
                "ERROR",
                "ZPJ_NAME",
                shp,
                "精度自评文件名不符合规范",
                expected=expected.name,
                actual=shp.name,
            )
        check_vector(
            result,
            shp,
            SCHEMAS[schema_name],
            schema_name=schema_name,
            valid_counties=set(counties),
            dynamic_metrics=True,
        )


def check_delivery(
    root: Path,
    county_boundary: Path,
    *,
    province_code: str = "150000",
    gdb_schema: str = "5-1",
    zpj_schema: str = "5-4",
) -> CheckResult:
    root = root.resolve()
    match = ROOT_RE.match(root.name)
    year = match.group(2) if match else "未知"
    result = CheckResult(str(root), province_code, year, gdb_schema, zpj_schema)

    if not root.is_dir():
        result.add("ERROR", "ROOT_MISSING", root, "待检成果目录不存在")
        return result
    if not match or match.group(1) != province_code:
        result.add(
            "ERROR",
            "ROOT_NAME",
            root,
            "成果根目录名称不符合规范",
            expected=f"EL_{province_code}_<4位年份>",
            actual=root.name,
        )
    if gdb_schema not in {"5-1", "6-1"}:
        result.add("ERROR", "OPTION", root, "GDB 只能选择表 5-1 或表 6-1")
        return result
    if zpj_schema not in {"5-4", "6-3"}:
        result.add("ERROR", "OPTION", root, "ELJDZPJ 只能选择表 5-4 或表 6-3")
        return result

    try:
        counties = load_counties(county_boundary)
        result.county_count = len(counties)
    except Exception as exc:
        result.add("ERROR", "COUNTY_BOUNDARY", county_boundary, str(exc))
        return result

    _check_gdbs(result, root, province_code, year, counties, gdb_schema)
    _check_images(result, root / "ELCLDOM", province_code, year, counties)
    _check_named_county_shps(result, root / "ELJDCJYB", "ELJDCJYB", year, counties, "5-1")
    _check_named_county_shps(result, root / "ELJDCZYB", "ELJDCZYB", year, counties, "5-3")
    _check_zpj(result, root / "ELJDZPJ", province_code, year, counties, zpj_schema)

    expected_entries = {"ELCLDOM", "ELJDCJYB", "ELJDCZYB", "ELJDZPJ"}
    for entry in root.iterdir():
        if entry.name in expected_entries or entry.suffix.lower() == ".gdb":
            continue
        result.add("WARNING", "ROOT_EXTRA", entry, "成果根目录中存在规范未说明的项目")
    return result
