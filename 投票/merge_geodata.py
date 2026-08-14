import argparse
import json
import logging
import os
from dataclasses import dataclass
from pathlib import Path


LOGGER = logging.getLogger("merge_geodata")

CGCS_2000_ALBERS_WKT = (
    'PROJCS["CGCS_2000_Albers",'
    'GEOGCS["GCS_China_Geodetic_Coordinate_System_2000",'
    'DATUM["D_China_2000",'
    'SPHEROID["CGCS2000",6378137.0,298.257222101]],'
    'PRIMEM["Greenwich",0.0],'
    'UNIT["Degree",0.0174532925199433]],'
    'PROJECTION["Albers"],'
    'PARAMETER["False_Easting",0.0],'
    'PARAMETER["False_Northing",0.0],'
    'PARAMETER["Central_Meridian",105.0],'
    'PARAMETER["Standard_Parallel_1",25.0],'
    'PARAMETER["Standard_Parallel_2",47.0],'
    'PARAMETER["Latitude_Of_Origin",0.0],'
    'UNIT["Meter",1.0]]'
)
MU_SQUARE_METERS = 666.6666666667


@dataclass
class MergeSummary:
    input_count: int
    output_count: int
    output_path: Path
    area_mu: float | None = None


def parse_args():
    parser = argparse.ArgumentParser(
        description="批量合并地理数据。当前支持 Shapefile，命令结构预留 GeoTIFF 等数据类型扩展。"
    )
    parser.add_argument(
        "--data-type",
        choices=["shp", "tif"],
        default="shp",
        help="需要合并的数据类型，当前 shp 已实现，tif 为预留入口。默认 shp。",
    )
    parser.add_argument("--input-dir", required=True, help="待合并文件所在文件夹。")
    parser.add_argument(
        "--output",
        required=True,
        help="输出文件路径或文件名。shp 模式下未写 .shp 后缀时会自动补上。",
    )
    parser.add_argument("--recursive", action="store_true", help="递归搜索子文件夹中的输入文件。")
    parser.add_argument(
        "--pattern",
        default=None,
        help="输入文件匹配规则，例如 *.shp。默认按 data-type 自动选择。",
    )
    parser.add_argument(
        "--merge-mode",
        choices=["append"],
        default="append",
        help="合并方式。append 表示把多个文件的要素追加到同一个输出文件中。",
    )
    parser.add_argument(
        "--schema-mode",
        choices=["strict", "union", "intersection"],
        default="union",
        help=(
            "属性表结构处理方式：strict 要求字段完全一致；union 保留所有字段，缺失字段填空；"
            "intersection 只保留所有文件共有字段。默认 union。"
        ),
    )
    parser.add_argument(
        "--crs-mode",
        choices=["strict", "to-first"],
        default="strict",
        help="坐标系处理方式：strict 要求 CRS 完全一致；to-first 将后续文件重投影到第一个文件 CRS。默认 strict。",
    )
    parser.add_argument(
        "--geometry-mode",
        choices=["strict", "promote-multi"],
        default="strict",
        help="几何类型处理方式：strict 要求几何类型一致；promote-multi 将 Polygon/LineString 等转为 Multi 类型后合并。",
    )
    parser.add_argument("--encoding", default="utf-8", help="读取和输出 Shapefile 的属性编码，默认 utf-8。")
    parser.add_argument(
        "--target-crs",
        default="CGCS_2000_Albers",
        help=(
            "输出 Shapefile 的目标投影。默认 CGCS_2000_Albers。"
            "可填写 EPSG:xxxx、完整 WKT、.prj 文件路径，或 CGCS_2000_Albers。"
        ),
    )
    parser.add_argument(
        "--area-crs",
        default="CGCS_2000_Albers",
        help=(
            "面积计算使用的投影。默认 CGCS_2000_Albers，面积单位按平方米换算为亩。"
            "可填写 EPSG:xxxx、完整 WKT、.prj 文件路径，或 CGCS_2000_Albers。"
        ),
    )
    parser.add_argument(
        "--add-source-field",
        default=None,
        help="可选。写入来源文件名的字段名，例如 SOURCE。",
    )
    parser.add_argument("--overwrite", action="store_true", help="允许覆盖已存在的输出文件。")
    parser.add_argument("--clip-boundary", default=None, help="可选。用于裁切输出的行政区边界 Shapefile。")
    parser.add_argument("--clip-name-field", default=None, help="边界文件中的行政区名称字段。")
    parser.add_argument("--clip-name", default=None, help="需要从边界文件中选择的行政区名称。")
    parser.add_argument("--summary-json", default=None, help="可选。把输出要素数、路径和亩数写入 JSON。")
    parser.add_argument(
        "--log-level",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        default="INFO",
        help="日志级别，默认 INFO。",
    )
    return parser.parse_args()


def configure_logging(level):
    logging.basicConfig(level=level, format="%(levelname)s: %(message)s")


def resolve_output_path(output, data_type):
    output_path = Path(output)
    if data_type == "shp" and output_path.suffix.lower() != ".shp":
        output_path = output_path.with_suffix(".shp")
    return output_path


def collect_input_files(input_dir, pattern, recursive):
    input_dir = Path(input_dir)
    if not input_dir.is_dir():
        raise NotADirectoryError(f"输入文件夹不存在或不是文件夹: {input_dir}")

    glob_method = input_dir.rglob if recursive else input_dir.glob
    return sorted(path for path in glob_method(pattern) if path.is_file())


def ensure_output_can_be_written(output_path, overwrite):
    if output_path.exists() and not overwrite:
        raise FileExistsError(f"输出文件已存在，如需覆盖请添加 --overwrite: {output_path}")
    output_path.parent.mkdir(parents=True, exist_ok=True)


def parse_crs(crs_text):
    from pyproj import CRS

    if crs_text == "CGCS_2000_Albers":
        return CRS.from_wkt(CGCS_2000_ALBERS_WKT)

    crs_path = Path(crs_text)
    if crs_path.is_file():
        return CRS.from_wkt(crs_path.read_text(encoding="utf-8"))

    return CRS.from_user_input(crs_text)


def project_geodataframe(gdf, target_crs_text):
    target_crs = parse_crs(target_crs_text)
    if gdf.crs is None:
        raise ValueError("合并后的 Shapefile 没有 CRS，无法重投影。请先给输入数据补充正确坐标系。")
    return gdf.to_crs(target_crs)


def calculate_area_mu(gdf, area_crs_text):
    area_gdf = project_geodataframe(gdf, area_crs_text)
    total_square_meters = float(area_gdf.geometry.area.fillna(0).sum())
    return total_square_meters / MU_SQUARE_METERS


def clip_to_boundary(gdf, boundary_path, name_field, region_name):
    import fiona
    from pyproj import CRS, Transformer
    from shapely.geometry import MultiPolygon, shape
    from shapely.ops import transform, unary_union
    from shapely.validation import make_valid

    def polygonal_part(geometry):
        """Shapefile 面图层不能写 GeometryCollection，只保留其中的面。"""
        if geometry is None or geometry.is_empty:
            return None
        geometry = make_valid(geometry)
        if geometry.geom_type in {"Polygon", "MultiPolygon"}:
            return geometry
        if geometry.geom_type == "GeometryCollection":
            polygons = []
            for part in geometry.geoms:
                if part.geom_type == "Polygon":
                    polygons.append(part)
                elif part.geom_type == "MultiPolygon":
                    polygons.extend(part.geoms)
            if polygons:
                return MultiPolygon(polygons) if len(polygons) > 1 else polygons[0]
        return None

    selected_geometries = []
    with fiona.open(boundary_path, "r") as source:
        if name_field not in source.schema.get("properties", {}):
            raise ValueError(f"行政区边界缺少名称字段 {name_field!r}: {boundary_path}")
        boundary_crs = source.crs_wkt or source.crs
        for feature in source:
            properties = feature.get("properties") or {}
            if str(properties.get(name_field, "")).strip() == str(region_name).strip():
                geometry = feature.get("geometry")
                if geometry:
                    selected_geometries.append(make_valid(shape(geometry)))
    if not selected_geometries:
        raise ValueError(f"行政区边界中找不到 {region_name!r}: {boundary_path}")
    if gdf.crs is None or not boundary_crs:
        raise ValueError("输入 SHP 或行政区边界没有 CRS，无法执行行政区裁切。")
    clip_geometry = unary_union(selected_geometries)
    source_boundary_bounds = tuple(round(value, 6) for value in clip_geometry.bounds)
    if CRS.from_user_input(boundary_crs) != CRS.from_user_input(gdf.crs):
        transformer = Transformer.from_crs(boundary_crs, gdf.crs, always_xy=True).transform
        clip_geometry = transform(transformer, clip_geometry)
    target_boundary_bounds = tuple(round(value, 3) for value in clip_geometry.bounds)
    result_bounds = tuple(round(value, 3) for value in gdf.total_bounds)
    LOGGER.info(
        "行政区裁切诊断：名称=%s，投票结果CRS=%s，边界CRS=%s",
        region_name,
        gdf.crs,
        boundary_crs,
    )
    LOGGER.info(
        "行政区裁切诊断：投票结果范围=%s，边界原始范围=%s，边界转换后范围=%s",
        result_bounds,
        source_boundary_bounds,
        target_boundary_bounds,
    )
    valid_geometry = gdf.geometry.apply(
        lambda geometry: make_valid(geometry)
        if geometry is not None and not geometry.is_empty
        else geometry
    )
    result = gdf[valid_geometry.intersects(clip_geometry)].copy()
    result.geometry = valid_geometry.loc[result.index].apply(
        lambda geometry: polygonal_part(geometry.intersection(clip_geometry))
    )
    result = result[~result.geometry.is_empty & result.geometry.notna()].copy()
    if result.empty:
        LOGGER.warning(
            "投票结果在行政区 %r 内没有可输出要素；该行政区将被跳过。",
            region_name,
        )
        return gdf.iloc[0:0].copy()
    result.geometry = result.geometry.apply(promote_to_multi_geometry)
    return result

def normalize_schema_columns(gdfs, schema_mode):
    geometry_name = gdfs[0].geometry.name
    field_sets = [set(gdf.columns) - {gdf.geometry.name} for gdf in gdfs]
    field_orders = [[col for col in gdf.columns if col != gdf.geometry.name] for gdf in gdfs]

    if schema_mode == "strict":
        first_order = field_orders[0]
        mismatches = [
            index
            for index, columns in enumerate(field_orders, start=1)
            if columns != first_order
        ]
        if mismatches:
            raise ValueError(
                "属性表结构不一致，strict 模式停止合并。"
                f"不一致的文件序号: {mismatches}。可改用 --schema-mode union 或 intersection。"
            )
        selected_columns = first_order
    elif schema_mode == "intersection":
        common_fields = set.intersection(*field_sets) if field_sets else set()
        selected_columns = [col for col in field_orders[0] if col in common_fields]
        LOGGER.warning("属性表结构不一致时使用 intersection，仅保留 %s 个共有字段。", len(selected_columns))
    else:
        selected_columns = []
        seen = set()
        for columns in field_orders:
            for col in columns:
                if col not in seen:
                    selected_columns.append(col)
                    seen.add(col)
        LOGGER.warning("属性表结构不一致时使用 union，缺失字段将填为空值。")

    return [gdf.reindex(columns=selected_columns + [geometry_name]) for gdf in gdfs]


def base_geometry_type(gdf):
    geom_types = gdf.geometry.dropna().geom_type
    if geom_types.empty:
        return None
    return geom_types.iloc[0].replace("Multi", "")


def promote_to_multi_geometry(geometry):
    from shapely.geometry import MultiLineString, MultiPoint, MultiPolygon

    if geometry is None or geometry.is_empty or geometry.geom_type.startswith("Multi"):
        return geometry
    if geometry.geom_type == "Point":
        return MultiPoint([geometry])
    if geometry.geom_type == "LineString":
        return MultiLineString([geometry])
    if geometry.geom_type == "Polygon":
        return MultiPolygon([geometry])
    return geometry


def normalize_geometries(gdfs, geometry_mode):
    base_type = base_geometry_type(gdfs[0])
    if base_type is None:
        raise ValueError("第一个 Shapefile 没有有效几何，无法确定输出几何类型。")

    for index, gdf in enumerate(gdfs, start=1):
        current_type = base_geometry_type(gdf)
        if current_type != base_type:
            raise ValueError(
                f"第 {index} 个 Shapefile 几何类型为 {current_type}，与第一个文件 {base_type} 不一致。"
            )

    if geometry_mode == "promote-multi":
        for gdf in gdfs:
            gdf.geometry = gdf.geometry.apply(promote_to_multi_geometry)

    return gdfs


def normalize_crs(gdfs, crs_mode):
    first_crs = gdfs[0].crs
    for index, gdf in enumerate(gdfs[1:], start=2):
        if gdf.crs != first_crs:
            if crs_mode == "strict":
                raise ValueError(
                    f"第 {index} 个 Shapefile 的 CRS 与第一个文件不一致。"
                    "可改用 --crs-mode to-first 自动重投影。"
                )
            if first_crs is None:
                raise ValueError("第一个 Shapefile 没有 CRS，无法将其他文件重投影到第一个 CRS。")
            LOGGER.warning("第 %s 个 Shapefile 将重投影到第一个文件的 CRS。", index)
            gdfs[index - 1] = gdf.to_crs(first_crs)
    return gdfs


def merge_shapefiles(args):
    import geopandas as gpd
    import pandas as pd

    pattern = args.pattern or "*.shp"
    input_files = collect_input_files(args.input_dir, pattern, args.recursive)
    output_path = resolve_output_path(args.output, "shp")
    input_files = [path for path in input_files if path.resolve() != output_path.resolve()]

    if not input_files:
        raise FileNotFoundError(f"未找到待合并的 Shapefile: {Path(args.input_dir) / pattern}")

    ensure_output_can_be_written(output_path, args.overwrite)

    gdfs = []
    failed_files = []
    progress_step = max(1, len(input_files) // 10)
    LOGGER.info("开始读取 %s 个 Shapefile", len(input_files))
    for processed, path in enumerate(input_files, start=1):
        try:
            gdf = gpd.read_file(path, encoding=args.encoding)
            if args.add_source_field:
                gdf[args.add_source_field] = path.name
            gdfs.append(gdf)
        except Exception as exc:
            failed_files.append((path, exc))
        if processed == 1 or processed % progress_step == 0 or processed == len(input_files):
            LOGGER.info("读取进度：%s/%s", processed, len(input_files))

    if failed_files:
        message = "\n".join(f"- {path}: {exc}" for path, exc in failed_files)
        raise RuntimeError(f"部分 Shapefile 读取失败，已停止合并:\n{message}")

    gdfs = normalize_crs(gdfs, args.crs_mode)
    gdfs = normalize_geometries(gdfs, args.geometry_mode)
    gdfs = normalize_schema_columns(gdfs, args.schema_mode)

    merged_gdf = gpd.GeoDataFrame(
        pd.concat(gdfs, ignore_index=True),
        crs=gdfs[0].crs,
        geometry=gdfs[0].geometry.name,
    )
    if args.clip_boundary:
        if not args.clip_name_field or not args.clip_name:
            raise ValueError("使用 --clip-boundary 时必须同时提供 --clip-name-field 和 --clip-name。")
        merged_gdf = clip_to_boundary(
            merged_gdf, args.clip_boundary, args.clip_name_field, args.clip_name
        )
    area_mu = calculate_area_mu(merged_gdf, args.area_crs)
    output_gdf = project_geodataframe(merged_gdf, args.target_crs)
    if output_gdf.empty:
        LOGGER.warning("合并结果为空，不生成 Shapefile: %s", output_path)
    else:
        output_gdf.to_file(output_path, driver="ESRI Shapefile", encoding=args.encoding)

    return MergeSummary(len(input_files), len(output_gdf), output_path, area_mu)

def merge_tifs(_args):
    raise NotImplementedError("tif 合并入口已预留，但当前版本尚未实现。")


def main():
    args = parse_args()
    configure_logging(args.log_level)

    if args.data_type == "shp":
        summary = merge_shapefiles(args)
    elif args.data_type == "tif":
        summary = merge_tifs(args)
    else:
        raise ValueError(f"不支持的数据类型: {args.data_type}")

    LOGGER.info(
        "合并完成：输入 %s 个文件，输出 %s 个要素 -> %s",
        summary.input_count,
        summary.output_count,
        summary.output_path,
    )
    if summary.area_mu is not None:
        LOGGER.info("合并后总面积：%.4f 亩", summary.area_mu)
    if args.summary_json:
        summary_path = Path(args.summary_json)
        summary_path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = summary_path.with_name(f".{summary_path.name}.{os.getpid()}.tmp")
        temp_path.write_text(
            json.dumps(
                {
                    "input_count": summary.input_count,
                    "output_count": summary.output_count,
                    "output_path": str(summary.output_path),
                    "area_mu": summary.area_mu,
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        temp_path.replace(summary_path)

if __name__ == "__main__":
    main()
