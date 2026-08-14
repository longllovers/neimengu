#!/usr/bin/env python3
"""处理中稻地块掩膜并生成 0809 版矢量、栅格和面积对比报告。

需求：
1. 递归读取“地块掩膜”中的全部 Shapefile，将所有要素的 class 设为数值 2，
   按原相对目录和文件名写入“0809地块掩膜”，不修改源文件。
2. 将新 Shapefile 合并为 EPSG:4326 的 rice_10m_result_0809.shp。
3. 在 Albers 等面积投影中按全部图斑范围建立严格 10 m × 10 m 网格，通过
   8×8 子像元超采样估算覆盖率，按覆盖率由高到低选择与矢量净面积相匹配的
   像元并写为2、其他写为0；采用最近邻金字塔并保持像元值仅为0/2。
4. 分块融合矢量重叠区域，并仅在内存中转换到 Albers 等面积投影，计算矢量
   净覆盖面积、TIFF 中值为 2 的像元面积及差异；不生成投影后的中间文件。

参考 TIFF 仅用于继承适用的 TIFF 标签；旧统计值、坐标系和经纬度网格不会复制。
"""

from __future__ import annotations

import argparse
import math
import os
import sys
from pathlib import Path
from typing import Iterable

import fiona
import numpy as np
import rasterio
from affine import Affine
from pyproj import CRS, Transformer
from rasterio.enums import Resampling
from rasterio.features import rasterize
from rasterio.transform import from_origin
from rasterio.windows import bounds as window_bounds
from rasterio.windows import transform as window_transform
from shapely import make_valid, union_all
from shapely.errors import GEOSException
from shapely.geometry import GeometryCollection, MultiPolygon, Polygon, box, mapping, shape
from shapely.ops import transform as transform_geometry
from shapely.strtree import STRtree


DEFAULT_BASE_DIR = Path(
    r"\\10.10.10.11\data\专题7_内蒙作物分类结果\像素10m初步结果\104中稻"
)
DEFAULT_INPUT_DIR = DEFAULT_BASE_DIR / "地块掩膜"
DEFAULT_OUTPUT_DIR = DEFAULT_BASE_DIR / "0809地块掩膜"
DEFAULT_REFERENCE_TIF = DEFAULT_BASE_DIR / "rice_10m_result.tif"

MERGED_SHP_NAME = "rice_10m_result_0809.shp"
OUTPUT_TIF_NAME = "rice_10m_result_0809.tif"
REPORT_NAME = "rice_10m_result_0809_area_report.txt"

WGS84 = CRS.from_epsg(4326)
# 覆盖中国及内蒙古的 Albers 等面积投影，只在内存中用于面积计算。
AREA_CRS = CRS.from_proj4(
    "+proj=aea +lat_1=25 +lat_2=47 +lat_0=0 +lon_0=105 "
    "+datum=WGS84 +units=m +no_defs"
)
M2_PER_MU = 2000.0 / 3.0
ALBERS_PIXEL_SIZE = 10.0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input-dir", type=Path, default=DEFAULT_INPUT_DIR)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--reference-tif", type=Path, default=DEFAULT_REFERENCE_TIF)
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="允许覆盖输出目录内本脚本将生成的同名文件",
    )
    parser.add_argument(
        "--tif-only",
        action="store_true",
        help="直接使用已合并的 Shapefile，仅重建 TIFF 和面积报告",
    )
    parser.add_argument(
        "--supersample",
        type=int,
        default=8,
        help="每个10米像元的边长细分倍数，用于估算覆盖率并排序（默认：8）",
    )
    parser.add_argument(
        "--threads",
        type=int,
        default=max(1, os.cpu_count() or 1),
        help="GeoTIFF 压缩和金字塔构建线程数（默认使用全部 CPU 线程）",
    )
    return parser.parse_args()


def find_shapefiles(root: Path) -> list[Path]:
    return sorted(
        (p for p in root.rglob("*") if p.is_file() and p.suffix.lower() == ".shp"),
        key=lambda p: str(p).lower(),
    )


def collection_crs(src: fiona.Collection) -> CRS:
    value = src.crs_wkt or src.crs
    if not value:
        raise ValueError(f"{src.path} 缺少坐标系信息（.prj）")
    return CRS.from_user_input(value)


def delete_shapefile_family(path: Path) -> None:
    """只删除指定输出 Shapefile 的同名组成文件。"""
    for candidate in path.parent.glob(f"{path.stem}.*"):
        if candidate.is_file():
            candidate.unlink()


def ensure_output_available(path: Path, overwrite: bool, shapefile: bool = False) -> None:
    exists = any(path.parent.glob(f"{path.stem}.*")) if shapefile else path.exists()
    if not exists:
        return
    if not overwrite:
        raise FileExistsError(f"输出已存在：{path}；如需覆盖请增加 --overwrite")
    if shapefile:
        delete_shapefile_family(path)
    else:
        path.unlink()


def class_field_name(properties: dict[str, str]) -> str:
    for name in properties:
        if name.lower() == "class":
            return name
    return "class"


def rewrite_class(source: Path, target: Path, overwrite: bool) -> int:
    """复制一个 Shapefile，并把 class 字段统一写成整数 2。"""
    target.parent.mkdir(parents=True, exist_ok=True)
    ensure_output_available(target, overwrite, shapefile=True)

    with fiona.open(source) as src:
        schema = src.schema.copy()
        properties = dict(schema["properties"])
        field_name = class_field_name(properties)
        properties[field_name] = "int:9"
        schema["properties"] = properties
        crs_wkt = collection_crs(src).to_wkt()

        count = 0
        with fiona.open(
            target,
            "w",
            driver="ESRI Shapefile",
            schema=schema,
            crs_wkt=crs_wkt,
            encoding="UTF-8",
        ) as dst:
            for feature in src:
                attrs = dict(feature["properties"])
                attrs[field_name] = 2
                dst.write({"geometry": feature["geometry"], "properties": attrs})
                count += 1
    return count


def polygonal_part(geometry):
    """提取面类型；GeometryCollection 中的面合成 MultiPolygon。"""
    if geometry is None or geometry.is_empty:
        return None
    if isinstance(geometry, (Polygon, MultiPolygon)):
        return geometry
    if isinstance(geometry, GeometryCollection):
        polygons: list[Polygon] = []
        for part in geometry.geoms:
            selected = polygonal_part(part)
            if isinstance(selected, Polygon):
                polygons.append(selected)
            elif isinstance(selected, MultiPolygon):
                polygons.extend(selected.geoms)
        return MultiPolygon(polygons) if polygons else None
    return None


def transformed(geometry, transformer: Transformer):
    return transform_geometry(transformer.transform, geometry)


def merge_shapefiles(
    sources: Iterable[Path], target: Path, overwrite: bool
) -> tuple[list, float, int, int]:
    """合并为 WGS84，并返回几何列表、面积和、写入数、跳过数。"""
    target.parent.mkdir(parents=True, exist_ok=True)
    ensure_output_available(target, overwrite, shapefile=True)
    schema = {"geometry": "Polygon", "properties": {"class": "int:9"}}
    area_transformer = Transformer.from_crs(WGS84, AREA_CRS, always_xy=True)
    geometries: list = []
    area_sum_m2 = 0.0
    written = 0
    skipped = 0

    with fiona.open(
        target,
        "w",
        driver="ESRI Shapefile",
        schema=schema,
        crs_wkt=WGS84.to_wkt(),
        encoding="UTF-8",
    ) as dst:
        for source in sources:
            with fiona.open(source) as src:
                src_crs = collection_crs(src)
                to_wgs84 = None
                if not src_crs.equals(WGS84):
                    to_wgs84 = Transformer.from_crs(src_crs, WGS84, always_xy=True)

                for feature in src:
                    if feature["geometry"] is None:
                        skipped += 1
                        continue
                    geometry = polygonal_part(shape(feature["geometry"]))
                    if geometry is None:
                        skipped += 1
                        continue
                    if to_wgs84 is not None:
                        geometry = transformed(geometry, to_wgs84)
                    if geometry.is_empty:
                        skipped += 1
                        continue
                    dst.write({"geometry": mapping(geometry), "properties": {"class": 2}})
                    geometries.append(geometry)
                    area_sum_m2 += transformed(geometry, area_transformer).area
                    written += 1

    return geometries, area_sum_m2, written, skipped


def load_merged_shapefile(path: Path) -> tuple[list, float, int, int]:
    """读取现有合并 Shapefile，供 --tif-only 使用，不重写矢量文件。"""
    geometries: list = []
    raw_area_m2 = 0.0
    written = 0
    skipped = 0
    area_transformer = Transformer.from_crs(WGS84, AREA_CRS, always_xy=True)
    with fiona.open(path) as src:
        src_crs = collection_crs(src)
        to_wgs84 = None
        if not src_crs.equals(WGS84):
            to_wgs84 = Transformer.from_crs(src_crs, WGS84, always_xy=True)
        for feature in src:
            if feature["geometry"] is None:
                skipped += 1
                continue
            geometry = polygonal_part(shape(feature["geometry"]))
            if geometry is None:
                skipped += 1
                continue
            if to_wgs84 is not None:
                geometry = transformed(geometry, to_wgs84)
            if geometry.is_empty:
                skipped += 1
                continue
            geometries.append(geometry)
            raw_area_m2 += transformed(geometry, area_transformer).area
            written += 1
    return geometries, raw_area_m2, written, skipped


def unique_vector_area_m2(geometries: list, tile_size_degrees: float = 0.25) -> float:
    """分块融合图斑后计算净覆盖面积，任何重叠部分均只计算一次。

    直接对几十万个图斑做全局 union 容易占用大量内存。这里以经纬度网格切块，
    每块内先裁切、融合，再转到等面积投影求面积。网格块互不重叠，因此各块
    面积可以安全求和；块边界只会产生零面积的公共边，不会重复计入。
    """
    if not geometries:
        return 0.0
    if tile_size_degrees <= 0:
        raise ValueError("面积去重网格大小必须大于 0")

    spatial_index = STRtree(geometries)
    minx = min(g.bounds[0] for g in geometries)
    miny = min(g.bounds[1] for g in geometries)
    maxx = max(g.bounds[2] for g in geometries)
    maxy = max(g.bounds[3] for g in geometries)
    first_col = math.floor(minx / tile_size_degrees)
    last_col = math.ceil(maxx / tile_size_degrees)
    first_row = math.floor(miny / tile_size_degrees)
    last_row = math.ceil(maxy / tile_size_degrees)
    area_transformer = Transformer.from_crs(WGS84, AREA_CRS, always_xy=True)
    total_area_m2 = 0.0

    for row in range(first_row, last_row):
        bottom = row * tile_size_degrees
        top = (row + 1) * tile_size_degrees
        for col in range(first_col, last_col):
            left = col * tile_size_degrees
            right = (col + 1) * tile_size_degrees
            tile = box(left, bottom, right, top)
            candidate_indices = spatial_index.query(tile)
            if len(candidate_indices) == 0:
                continue

            clipped = []
            for index in candidate_indices:
                geometry = geometries[int(index)]
                try:
                    part = geometry.intersection(tile)
                except GEOSException:
                    part = make_valid(geometry).intersection(tile)
                part = polygonal_part(part)
                if part is not None and not part.is_empty:
                    clipped.append(part)
            if not clipped:
                continue

            try:
                dissolved = union_all(clipped)
            except GEOSException:
                dissolved = union_all([make_valid(g) for g in clipped])
            dissolved = polygonal_part(dissolved)
            if dissolved is not None and not dissolved.is_empty:
                total_area_m2 += transformed(dissolved, area_transformer).area

    return total_area_m2


def rasterize_to_reference(
    geometries: list,
    reference_path: Path,
    output_path: Path,
    overwrite: bool,
    threads: int,
    supersample: int,
    target_vector_area_m2: float,
) -> tuple[float, int, tuple[float, float]]:
    """按覆盖率排序选择像元，使二值 TIFF 面积尽量匹配矢量净面积。"""
    if output_path.exists() and not overwrite:
        raise FileExistsError(f"输出已存在：{output_path}；如需覆盖请增加 --overwrite")
    working_path = output_path.with_name(f"{output_path.stem}.building.tif")
    coverage_path = output_path.with_name(f"{output_path.stem}.coverage.building.tif")
    if working_path.exists():
        working_path.unlink()
    if coverage_path.exists():
        coverage_path.unlink()
    if not geometries:
        raise ValueError("合并结果中没有可栅格化的面要素")

    to_albers = Transformer.from_crs(WGS84, AREA_CRS, always_xy=True)
    projected_geometries = [transformed(g, to_albers) for g in geometries]
    spatial_index = STRtree(projected_geometries)

    minx = min(g.bounds[0] for g in projected_geometries)
    miny = min(g.bounds[1] for g in projected_geometries)
    maxx = max(g.bounds[2] for g in projected_geometries)
    maxy = max(g.bounds[3] for g in projected_geometries)
    left = math.floor(minx / ALBERS_PIXEL_SIZE) * ALBERS_PIXEL_SIZE
    bottom = math.floor(miny / ALBERS_PIXEL_SIZE) * ALBERS_PIXEL_SIZE
    right = math.ceil(maxx / ALBERS_PIXEL_SIZE) * ALBERS_PIXEL_SIZE
    top = math.ceil(maxy / ALBERS_PIXEL_SIZE) * ALBERS_PIXEL_SIZE
    width = int(round((right - left) / ALBERS_PIXEL_SIZE))
    height = int(round((top - bottom) / ALBERS_PIXEL_SIZE))
    output_transform = from_origin(
        left, top, ALBERS_PIXEL_SIZE, ALBERS_PIXEL_SIZE
    )

    # 每个图斑保留一个内部代表像元。若多个图斑落在同一像元，只保留一次。
    reserved_cells: set[int] = set()
    for geometry in projected_geometries:
        point = geometry.representative_point()
        col = int(math.floor((point.x - left) / ALBERS_PIXEL_SIZE))
        row = int(math.floor((top - point.y) / ALBERS_PIXEL_SIZE))
        col = min(max(col, 0), width - 1)
        row = min(max(row, 0), height - 1)
        reserved_cells.add(row * width + col)

    block_size = 512
    reserved_by_block: dict[tuple[int, int], list[tuple[int, int]]] = {}
    for linear_index in reserved_cells:
        row, col = divmod(linear_index, width)
        key = (row // block_size, col // block_size)
        reserved_by_block.setdefault(key, []).append(
            (row % block_size, col % block_size)
        )

    maximum_coverage = supersample * supersample
    coverage_histogram = np.zeros(maximum_coverage + 1, dtype=np.int64)

    with rasterio.open(reference_path) as reference:
        # 参考影像很可能是 0/1 数据，不能把旧统计值和直方图复制到新的 0/2 TIFF，
        # 否则 QGIS 会继续按 0~1 拉伸，类别 2 被显示成与画布相同的白色。
        ignored_tag_prefixes = ("STATISTICS_", "HISTOGRAM_")
        dataset_tags = {
            key: value
            for key, value in reference.tags().items()
            if not key.upper().startswith(ignored_tag_prefixes)
        }
        band_tags = {
            key: value
            for key, value in reference.tags(1).items()
            if not key.upper().startswith(ignored_tag_prefixes)
        }
        band_description = reference.descriptions[0] if reference.descriptions else None
        band_unit = reference.units[0] if reference.units else None
        profile = reference.profile.copy()
        profile.update(
            driver="GTiff",
            count=1,
            dtype="uint8",
            nodata=0,
            crs=AREA_CRS,
            transform=output_transform,
            width=width,
            height=height,
            tiled=True,
            blockxsize=512,
            blockysize=512,
            compress="LZW",
            interleave="band",
            num_threads=threads,
            BIGTIFF="IF_SAFER",
        )
        # 部分源文件的参数只适用于原数据类型/波段结构。
        profile.pop("photometric", None)
        profile.pop("predictor", None)

        output_path.parent.mkdir(parents=True, exist_ok=True)
        gdal_threads = str(max(1, threads))
        with rasterio.Env(GDAL_NUM_THREADS=gdal_threads):
            coverage_profile = profile.copy()
            coverage_profile.update(
                dtype="uint16",
                nodata=0,
                compress="LZW",
                sparse_ok=True,
            )

            print(
                f"第一遍：使用 {supersample}×{supersample} 超采样计算像元覆盖率……"
            )
            with rasterio.open(coverage_path, "w", **coverage_profile) as coverage_dst:
                for _, window in coverage_dst.block_windows(1):
                    block_left, block_bottom, block_right, block_top = window_bounds(
                        window, coverage_dst.transform
                    )
                    candidate_indices = spatial_index.query(
                        box(block_left, block_bottom, block_right, block_top)
                    )
                    candidates = [projected_geometries[int(i)] for i in candidate_indices]
                    block_shape = (int(window.height), int(window.width))
                    if candidates:
                        fine_shape = (
                            block_shape[0] * supersample,
                            block_shape[1] * supersample,
                        )
                        fine_transform = window_transform(
                            window, coverage_dst.transform
                        ) * Affine.scale(1.0 / supersample, 1.0 / supersample)
                        fine_data = rasterize(
                            ((mapping(g), 1) for g in candidates),
                            out_shape=fine_shape,
                            transform=fine_transform,
                            fill=0,
                            dtype="uint8",
                            all_touched=False,
                        )
                        coverage = fine_data.reshape(
                            block_shape[0],
                            supersample,
                            block_shape[1],
                            supersample,
                        ).sum(axis=(1, 3), dtype=np.uint16)
                    else:
                        coverage = np.zeros(block_shape, dtype=np.uint16)
                    coverage_dst.write(coverage, 1, window=window)

                    block_key = (
                        int(window.row_off) // block_size,
                        int(window.col_off) // block_size,
                    )
                    reserved_mask = np.zeros(block_shape, dtype=bool)
                    for local_row, local_col in reserved_by_block.get(block_key, []):
                        if local_row < block_shape[0] and local_col < block_shape[1]:
                            reserved_mask[local_row, local_col] = True
                    non_reserved_coverage = coverage[~reserved_mask]
                    coverage_histogram += np.bincount(
                        non_reserved_coverage,
                        minlength=maximum_coverage + 1,
                    )[: maximum_coverage + 1]

            target_pixel_count = int(
                round(target_vector_area_m2 / (ALBERS_PIXEL_SIZE * ALBERS_PIXEL_SIZE))
            )
            reserved_count = len(reserved_cells)
            if reserved_count > target_pixel_count:
                raise ValueError(
                    "需要保留的图斑代表像元数已超过面积允许的目标像元数；"
                    "无法同时保证每个图斑可见和总面积匹配"
                )
            target_non_reserved = target_pixel_count - reserved_count
            available_non_reserved = int(coverage_histogram[1:].sum())
            if target_non_reserved > available_non_reserved:
                raise ValueError(
                    "超采样找到的相交像元不足以匹配矢量面积，请提高 --supersample"
                )

            remaining = target_non_reserved
            cutoff_coverage = maximum_coverage + 1
            cutoff_needed = 0
            cutoff_total = 0
            for coverage_level in range(maximum_coverage, 0, -1):
                level_count = int(coverage_histogram[coverage_level])
                if remaining >= level_count:
                    remaining -= level_count
                    continue
                cutoff_coverage = coverage_level
                cutoff_needed = remaining
                cutoff_total = level_count
                remaining = 0
                break
            if remaining != 0:
                raise RuntimeError("无法根据覆盖率选出目标数量的像元")
            if target_non_reserved == available_non_reserved:
                cutoff_coverage = 0

            print(
                "第二遍：按覆盖率由高到低选择像元；"
                f"目标 {target_pixel_count:,} 个，其中图斑代表像元 {reserved_count:,} 个……"
            )
            pixel_count = 0
            cutoff_seen = 0
            with rasterio.open(coverage_path) as coverage_src, rasterio.open(
                working_path, "w", **profile
            ) as dst:
                if dataset_tags:
                    dst.update_tags(**dataset_tags)
                if band_tags:
                    dst.update_tags(1, **band_tags)
                if band_description:
                    dst.set_band_description(1, band_description)
                if band_unit:
                    dst.set_band_unit(1, band_unit)
                # 写入分类颜色表：背景 0 透明，class=2 使用绿色，打开即可看见。
                dst.write_colormap(
                    1,
                    {
                        0: (0, 0, 0, 0),
                        2: (0, 180, 0, 255),
                    },
                )
                for _, window in coverage_src.block_windows(1):
                    block_shape = (int(window.height), int(window.width))
                    coverage = coverage_src.read(1, window=window)
                    block_key = (
                        int(window.row_off) // block_size,
                        int(window.col_off) // block_size,
                    )
                    reserved_mask = np.zeros(block_shape, dtype=bool)
                    for local_row, local_col in reserved_by_block.get(block_key, []):
                        if local_row < block_shape[0] and local_col < block_shape[1]:
                            reserved_mask[local_row, local_col] = True

                    selected = reserved_mask | (coverage > cutoff_coverage)
                    if cutoff_needed and cutoff_coverage > 0:
                        candidates_flat = np.flatnonzero(
                            (coverage == cutoff_coverage) & ~reserved_mask
                        )
                        candidate_count = int(candidates_flat.size)
                        before = cutoff_seen * cutoff_needed // cutoff_total
                        after = (
                            (cutoff_seen + candidate_count)
                            * cutoff_needed
                            // cutoff_total
                        )
                        choose_count = after - before
                        if choose_count:
                            positions = np.floor(
                                (np.arange(choose_count) + 0.5)
                                * candidate_count
                                / choose_count
                            ).astype(np.int64)
                            selected.flat[candidates_flat[positions]] = True
                        cutoff_seen += candidate_count

                    data = np.where(selected, 2, 0).astype("uint8")
                    dst.write(data, 1, window=window)
                    pixel_count += int(np.count_nonzero(selected))

            if pixel_count != target_pixel_count:
                raise RuntimeError(
                    f"面积约束像元数校验失败：目标 {target_pixel_count:,}，"
                    f"实际 {pixel_count:,}"
                )

            # 写入结束并关闭数据集后，再以更新模式构建内部金字塔，避免不完整 TIFF。
            overview_levels: list[int] = []
            factor = 2
            while min(width, height) // factor >= 128:
                overview_levels.append(factor)
                factor *= 2
            if overview_levels:
                print(
                    f"正在使用 {gdal_threads} 个线程构建内部金字塔："
                    + ", ".join(f"1:{level}" for level in overview_levels)
                )
                with rasterio.Env(
                    GDAL_NUM_THREADS=gdal_threads,
                    COMPRESS_OVERVIEW="LZW",
                    GDAL_TIFF_OVR_BLOCKSIZE="512",
                ):
                    with rasterio.open(working_path, "r+", num_threads=threads) as dst:
                        dst.build_overviews(overview_levels, Resampling.nearest)
                        dst.update_tags(ns="rio_overview", resampling="nearest")

            # 完整关闭后重新打开验证；成功后才替换正式结果，避免留下打不开的半成品。
            with rasterio.open(working_path) as check:
                if check.driver != "GTiff" or check.count != 1:
                    raise RuntimeError("生成的 TIFF 驱动或波段数校验失败")
                if not CRS.from_user_input(check.crs).equals(AREA_CRS):
                    raise RuntimeError("生成的 TIFF 不是要求的 Albers 投影")
                if not (
                    math.isclose(abs(check.transform.a), ALBERS_PIXEL_SIZE)
                    and math.isclose(abs(check.transform.e), ALBERS_PIXEL_SIZE)
                ):
                    raise RuntimeError("生成的 TIFF 不是严格的 10 m × 10 m 网格")
                if check.compression is None or "lzw" not in str(check.compression).lower():
                    raise RuntimeError("生成的 TIFF 未成功使用 LZW 压缩")
                actual_overviews = check.overviews(1)
                overviews_valid = len(actual_overviews) == len(overview_levels) and all(
                    abs(actual - requested) <= max(1, requested * 0.01)
                    for actual, requested in zip(actual_overviews, overview_levels)
                )
                if not overviews_valid:
                    raise RuntimeError("生成的 TIFF 内部金字塔校验失败")
                check.read(1, window=((0, 1), (0, 1)))

        working_path.replace(output_path)
        coverage_path.unlink()
        print(f"GeoTIFF 完整性校验通过，已生成正式文件：{output_path}")

    raster_area_m2 = pixel_count * ALBERS_PIXEL_SIZE * ALBERS_PIXEL_SIZE
    return raster_area_m2, pixel_count, (ALBERS_PIXEL_SIZE, ALBERS_PIXEL_SIZE)


def write_report(
    path: Path,
    vector_unique_area_m2: float,
    raster_area_m2: float,
    feature_count: int,
    skipped_count: int,
    supersample: int,
) -> str:
    difference_m2 = raster_area_m2 - vector_unique_area_m2
    absolute_difference_m2 = abs(difference_m2)
    difference_percent = (
        absolute_difference_m2 / vector_unique_area_m2 * 100.0
        if vector_unique_area_m2 > 0
        else math.nan
    )
    lines = [
        "中稻 0809 地块掩膜面积对比报告",
        "=" * 42,
        f"合并面要素数: {feature_count:,}",
        f"栅格化规则: {supersample}×{supersample} 超采样，"
        "按覆盖率排序并约束总面积",
        "标称分辨率: 10 m × 10 m",
        "",
        f"矢量净覆盖面积（重叠只算一次）: {vector_unique_area_m2:,.3f} m²",
        f"矢量净覆盖面积（重叠只算一次）: {vector_unique_area_m2 / M2_PER_MU:,.6f} 亩",
        f"TIFF 值为 2 区域面积: {raster_area_m2:,.3f} m²",
        f"TIFF 值为 2 区域面积: {raster_area_m2 / M2_PER_MU:,.6f} 亩",
        f"面积差（TIFF - 矢量）: {difference_m2:,.3f} m²",
        f"差异比例（相对矢量）: {difference_percent:.6f}%",
        "",
        "说明：面积均在内存中转换到 Albers 等面积投影计算；矢量重叠部分只算一次。",
    ]
    if skipped_count:
        lines.insert(3, f"跳过的空/非面要素数: {skipped_count:,}")
    text = "\n".join(lines) + "\n"
    path.write_text(text, encoding="utf-8-sig")
    return text


def main() -> int:
    args = parse_args()
    input_dir = args.input_dir.resolve()
    output_dir = args.output_dir.resolve()
    reference_tif = args.reference_tif.resolve()

    if not reference_tif.is_file():
        raise FileNotFoundError(f"参考 TIFF 不存在或无法访问：{reference_tif}")
    if args.threads < 1:
        raise ValueError("--threads 必须大于或等于 1")
    if args.supersample < 1 or args.supersample > 16:
        raise ValueError("--supersample 必须在 1 到 16 之间")

    merged_shp = output_dir / MERGED_SHP_NAME
    rewritten_features: int | None = None
    if args.tif_only:
        if not merged_shp.is_file():
            raise FileNotFoundError(f"没有找到现有合并 Shapefile：{merged_shp}")
        print(f"--tif-only：跳过源 Shapefile 复制和合并，读取：{merged_shp}")
        geometries, _vector_raw_area_m2, merged_count, skipped_count = (
            load_merged_shapefile(merged_shp)
        )
    else:
        if not input_dir.is_dir():
            raise FileNotFoundError(f"输入目录不存在或无法访问：{input_dir}")
        if input_dir == output_dir:
            raise ValueError("输入目录和输出目录不能相同，以免覆盖源文件")
        source_files = find_shapefiles(input_dir)
        if not source_files:
            raise FileNotFoundError(f"输入目录内没有找到 .shp：{input_dir}")

        print(f"找到 {len(source_files)} 个 Shapefile，开始复制并设置 class=2……")
        rewritten_files: list[Path] = []
        rewritten_features = 0
        for number, source in enumerate(source_files, 1):
            relative = source.relative_to(input_dir)
            target = output_dir / relative
            count = rewrite_class(source, target, args.overwrite)
            rewritten_files.append(target)
            rewritten_features += count
            print(f"[{number}/{len(source_files)}] {relative}：{count:,} 个要素")

        print(f"正在合并为：{merged_shp}")
        geometries, _vector_raw_area_m2, merged_count, skipped_count = (
            merge_shapefiles(rewritten_files, merged_shp, args.overwrite)
        )
    print("正在分块融合图斑并计算矢量净覆盖面积（重叠部分只算一次）……")
    vector_unique_area_m2 = unique_vector_area_m2(geometries)

    output_tif = output_dir / OUTPUT_TIF_NAME
    print(f"正在按参考网格分块栅格化：{output_tif}")
    raster_area_m2, _pixel_count, _resolution_m = rasterize_to_reference(
        geometries,
        reference_tif,
        output_tif,
        args.overwrite,
        args.threads,
        args.supersample,
        vector_unique_area_m2,
    )

    report_path = output_dir / REPORT_NAME
    ensure_output_available(report_path, args.overwrite)
    report = write_report(
        report_path,
        vector_unique_area_m2,
        raster_area_m2,
        merged_count,
        skipped_count,
        args.supersample,
    )
    print("\n" + report)
    if rewritten_features is None:
        print("处理完成；本次未重写或重新合并任何 Shapefile。")
    else:
        print(f"处理完成。共重写 {rewritten_features:,} 个要素。")
    print(f"合并 Shapefile：{merged_shp}")
    print(f"输出 TIFF：{output_tif}")
    print(f"面积报告：{report_path}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"错误：{exc}", file=sys.stderr)
        raise SystemExit(1)
