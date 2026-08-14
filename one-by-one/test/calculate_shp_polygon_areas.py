#!/usr/bin/env python3
"""递归统计目录中所有 Shapefile 图斑的面积（平方米）。

源数据若已经是 Albers 等积投影，则直接计算；否则仅在内存中转换到
中国 Albers 等积投影后计算。脚本只读取 Shapefile，不会修改或覆盖源文件。
"""

from __future__ import annotations

import argparse
import concurrent.futures
import os
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import fiona
from pyproj import CRS, Transformer
from shapely.geometry import shape
from shapely.ops import transform


DEFAULT_INPUT_DIR = Path(
    "/media/cangling/nas_folder/北京预测结果传递/地块结果/所有地块结果最新-去除接边"
)

# 与本项目现有影像处理脚本保持一致的中国 Albers 等积投影，单位为米。
TARGET_CRS = CRS.from_proj4(
    "+proj=aea +lat_1=25 +lat_2=47 +lat_0=0 +lon_0=105 "
    "+datum=WGS84 +units=m +no_defs"
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "递归统计所有 .shp 中每个图斑的面积（平方米）；必要时只在内存中转为 Albers。"
        )
    )
    parser.add_argument(
        "input_dir",
        nargs="?",
        type=Path,
        default=DEFAULT_INPUT_DIR,
        help=f"Shapefile 根目录（默认：{DEFAULT_INPUT_DIR}）",
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=max(1, min(4, os.cpu_count() or 1)),
        help="并行线程数；每个线程处理一个 Shapefile（默认：最多 4）",
    )
    return parser.parse_args()


def is_albers(crs: CRS) -> bool:
    """判断 CRS 的投影方法是否为 Albers Equal Area。"""
    operation = crs.coordinate_operation
    method_name = operation.method_name.lower() if operation else ""
    if "albers" in method_name:
        return True

    # 兼容部分使用非标准投影名称的 .prj 文件。
    try:
        if "albers" in crs.to_wkt().lower():
            return True
    except Exception:
        pass
    return crs.is_projected and "albers" in crs.name.lower()


def horizontal_unit_factor(crs: CRS) -> float:
    """返回投影坐标单位到米的换算系数。"""
    if not crs.axis_info:
        raise ValueError("无法确定 Albers 投影的坐标单位")
    factors = [axis.unit_conversion_factor for axis in crs.axis_info[:2]]
    if len(factors) < 2 or any(factor is None for factor in factors):
        raise ValueError("无法将 Albers 投影的坐标单位换算为米")
    if abs(factors[0] - factors[1]) > 1e-12:
        raise ValueError("横纵坐标单位不一致，无法可靠计算面积")
    return float(factors[0])


def polygon_part_count(geometry: Any) -> int:
    """统计 Polygon/MultiPolygon（含 GeometryCollection）中的面部件数。"""
    if geometry.geom_type == "Polygon":
        return 1
    if geometry.geom_type == "MultiPolygon":
        return len(geometry.geoms)
    if geometry.geom_type == "GeometryCollection":
        return sum(polygon_part_count(part) for part in geometry.geoms)
    return 0


def find_shapefiles(input_dir: Path) -> list[Path]:
    return sorted(
        (path for path in input_dir.rglob("*") if path.is_file() and path.suffix.lower() == ".shp"),
        key=lambda path: str(path).lower(),
    )


def source_crs(collection: fiona.Collection) -> CRS:
    crs_value = collection.crs_wkt or collection.crs
    if not crs_value:
        raise ValueError("缺少坐标系信息（.prj）")
    return CRS.from_user_input(crs_value)


@dataclass
class AreaStatistics:
    area_m2: float
    feature_count: int
    polygon_count: int
    empty_geometry_count: int
    non_polygon_count: int
    source_crs_name: str
    already_albers: bool


def sum_collection_area(src: fiona.Collection) -> AreaStatistics:
    """逐个读取数据集中的图斑并求和。"""
    total_area = 0.0
    feature_count = 0
    polygon_count = 0
    empty_geometry_count = 0
    non_polygon_count = 0
    crs = source_crs(src)
    already_albers = is_albers(crs)
    transformer = None
    area_scale = 1.0
    if already_albers:
        # Albers 不重投影；若坐标单位不是米，则仅换算面积单位。
        unit_factor = horizontal_unit_factor(crs)
        area_scale = unit_factor * unit_factor
    else:
        transformer = Transformer.from_crs(crs, TARGET_CRS, always_xy=True)

    for feature in src:
        feature_count += 1
        geometry_mapping = feature.get("geometry")
        if geometry_mapping is None:
            empty_geometry_count += 1
            continue

        geometry = shape(geometry_mapping)
        if polygon_part_count(geometry) == 0:
            non_polygon_count += 1
            continue
        polygon_count += 1
        if transformer is not None:
            geometry = transform(transformer.transform, geometry)
        total_area += geometry.area * area_scale
    return AreaStatistics(
        area_m2=total_area,
        feature_count=feature_count,
        polygon_count=polygon_count,
        empty_geometry_count=empty_geometry_count,
        non_polygon_count=non_polygon_count,
        source_crs_name=crs.name,
        already_albers=already_albers,
    )


def sum_shapefile_area(shp_path: Path) -> AreaStatistics:
    """流式读取单个 Shapefile，避免一次性装入内存。"""
    with fiona.open(shp_path) as src:
        return sum_collection_area(src)


def process_shapefile(shp_path: Path) -> tuple[AreaStatistics, float]:
    """在线程中处理一个 Shapefile，并返回统计值和实际处理用时。"""
    start_time = time.monotonic()
    statistics = sum_shapefile_area(shp_path)
    return statistics, time.monotonic() - start_time


def run(input_dir: Path, workers: int) -> float:
    if not input_dir.is_dir():
        raise FileNotFoundError(f"输入目录不存在或无法访问：{input_dir}")
    if workers < 1:
        raise ValueError("--workers 必须大于或等于 1")

    shapefiles = find_shapefiles(input_dir)
    if not shapefiles:
        raise FileNotFoundError(f"目录及其子目录中没有找到 .shp：{input_dir}")

    print(f"输入目录：{input_dir}")
    print(f"找到 Shapefile：{len(shapefiles)} 个")
    actual_workers = min(workers, len(shapefiles))
    print(f"并行线程：{actual_workers} 个（每个线程一次处理一个 Shapefile）")
    print("面积计算投影：中国 Albers 等积投影（WGS84，中央经线 105°，标准纬线 25°/47°）")
    print("说明：非 Albers 数据仅在内存中重投影，不修改源文件。")
    print("=" * 80, flush=True)

    start_time = time.monotonic()
    total_area = 0.0
    total_features = 0
    total_polygons = 0
    total_empty = 0
    total_non_polygon = 0
    total_files = len(shapefiles)
    completed_files = 0
    failures: list[str] = []
    with concurrent.futures.ThreadPoolExecutor(
        max_workers=actual_workers, thread_name_prefix="shp-area"
    ) as executor:
        future_to_path = {
            executor.submit(process_shapefile, shp_path): shp_path
            for shp_path in shapefiles
        }
        for future in concurrent.futures.as_completed(future_to_path):
            shp_path = future_to_path[future]
            relative_path = shp_path.relative_to(input_dir)
            completed_files += 1
            print(f"[{completed_files}/{total_files}] 完成：{relative_path}", flush=True)
            try:
                statistics, file_elapsed = future.result()
            except Exception as exc:
                message = f"{relative_path}：{exc}"
                failures.append(message)
                print(f"  状态：失败")
                print(f"  原因：{exc}")
                print("-" * 80, flush=True)
                continue

            total_area += statistics.area_m2
            total_features += statistics.feature_count
            total_polygons += statistics.polygon_count
            total_empty += statistics.empty_geometry_count
            total_non_polygon += statistics.non_polygon_count
            projection_action = (
                "源数据已是 Albers，直接计算"
                if statistics.already_albers
                else "源数据不是 Albers，已在内存中转换"
            )
            print(f"  状态：成功")
            print(f"  源坐标系：{statistics.source_crs_name}")
            print(f"  投影处理：{projection_action}")
            print(
                f"  要素总数：{statistics.feature_count:,}；"
                f"面图斑：{statistics.polygon_count:,}；"
                f"空几何：{statistics.empty_geometry_count:,}；"
                f"非面要素：{statistics.non_polygon_count:,}"
            )
            print(f"  文件面积：{statistics.area_m2:,.6f} 平方米")
            print(f"  当前已完成文件累计面积：{total_area:,.6f} 平方米")
            print(f"  该线程处理用时：{file_elapsed:.2f} 秒")
            print("-" * 80, flush=True)

    if failures:
        failure_details = "\n".join(f"  - {message}" for message in failures)
        raise RuntimeError(
            f"有 {len(failures)} 个 Shapefile 处理失败，未输出不完整的总面积：\n"
            f"{failure_details}"
        )

    print("统计完成")
    print(f"成功处理文件：{total_files:,} 个")
    print(f"要素总数：{total_features:,}")
    print(f"面图斑总数：{total_polygons:,}")
    print(f"空几何总数：{total_empty:,}")
    print(f"非面要素总数：{total_non_polygon:,}")
    print(f"总用时：{time.monotonic() - start_time:.2f} 秒")
    return total_area


def main() -> int:
    args = parse_args()
    try:
        total_area = run(args.input_dir.resolve(), args.workers)
    except Exception as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 1

    print(f"所有耕地图斑总面积：{total_area:.6f} 平方米")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
