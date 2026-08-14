#!/usr/bin/env python3
"""为省级边界 Shapefile 生成指定边长的规则网格。

默认读取 ``00省边界`` 中的所有 .shp，生成 2 km × 2 km 网格，并把结果
写到 ``2公里网格``。计算过程使用米制 Albers 等积投影，因此不能直接把
WGS84 经纬度误当成米。

默认 ``intersects`` 模式保留所有与省界相交的完整方格，使网格完整覆盖整个
省界，同时确保每个要素都是严格的 2 km × 2 km 方格。边缘方格允许伸到省界外。
"""

from __future__ import annotations

import argparse
import math
import sys
from pathlib import Path
from typing import Iterable

import fiona
import geopandas as gpd
import numpy as np
import shapely
from pyproj import CRS
from shapely.geometry import mapping


SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_INPUT = SCRIPT_DIR / "00省边界"
DEFAULT_OUTPUT_DIR = SCRIPT_DIR / "2公里网格"
DEFAULT_GRID_SIZE = 2_000.0

# 与本项目影像重投影脚本一致的中国 Albers 等积圆锥投影，单位为米。
TARGET_CRS = CRS.from_proj4(
    "+proj=aea +lat_1=25 +lat_2=47 +lat_0=0 +lon_0=105 "
    "+x_0=0 +y_0=0 +datum=WGS84 +units=m +no_defs"
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="在省界范围内生成规则网格 Shapefile（默认 2 km × 2 km）。"
    )
    parser.add_argument(
        "--input",
        type=Path,
        default=DEFAULT_INPUT,
        help="输入 .shp 或包含 .shp 的目录（默认：00省边界）。",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=DEFAULT_OUTPUT_DIR,
        help="输出目录（默认：2公里网格）。",
    )
    parser.add_argument(
        "--size",
        type=float,
        default=DEFAULT_GRID_SIZE,
        help="网格边长，单位为米（默认：2000）。",
    )
    parser.add_argument(
        "--mode",
        choices=("clip", "intersects", "centroid", "within"),
        default="intersects",
        help=(
            "边界处理方式：clip=裁掉省界外部分；intersects=保留相交完整格；"
            "centroid=保留中心点在省界内的完整格；within=仅保留完全在省界内的格。"
            "默认：intersects。"
        ),
    )
    parser.add_argument(
        "--chunk-rows",
        type=int,
        default=100,
        help="每批处理的网格行数，用于控制内存占用（默认：100）。",
    )
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="覆盖已存在的同名网格 Shapefile。",
    )
    return parser.parse_args()


def find_shapefiles(input_path: Path) -> list[Path]:
    """返回待处理的 Shapefile；目录模式不递归扫描。"""
    input_path = input_path.expanduser().resolve()
    if input_path.is_file():
        if input_path.suffix.lower() != ".shp":
            raise ValueError(f"输入文件不是 .shp：{input_path}")
        return [input_path]
    if not input_path.is_dir():
        raise FileNotFoundError(f"输入路径不存在：{input_path}")

    shapefiles = sorted(
        (path for path in input_path.iterdir() if path.suffix.lower() == ".shp"),
        key=lambda path: path.name,
    )
    if not shapefiles:
        raise FileNotFoundError(f"目录中没有找到 .shp：{input_path}")
    return shapefiles


def remove_shapefile(output_path: Path) -> None:
    """删除一个 Shapefile 的已有配套文件。"""
    for path in output_path.parent.glob(f"{output_path.stem}.*"):
        if path.is_file():
            path.unlink()


def polygonal_parts(geometries: np.ndarray) -> np.ndarray:
    """从叠加结果中提取面要素，并丢弃空几何。"""
    parts: list[object] = []
    for geometry in geometries:
        if geometry is None or geometry.is_empty:
            continue
        if geometry.geom_type in {"Polygon", "MultiPolygon"}:
            parts.append(geometry)
            continue
        if geometry.geom_type == "GeometryCollection":
            polygons = [
                item
                for item in geometry.geoms
                if item.geom_type in {"Polygon", "MultiPolygon"} and not item.is_empty
            ]
            if polygons:
                parts.append(shapely.union_all(polygons))
    return np.asarray(parts, dtype=object)


def choose_cells(
    cells: np.ndarray, boundary: object, mode: str
) -> tuple[np.ndarray, np.ndarray]:
    """筛选/裁剪一批网格，返回几何和它们在原批次中的位置。"""
    if mode == "centroid":
        selected = shapely.covers(boundary, shapely.centroid(cells))
    elif mode == "within":
        selected = shapely.covers(boundary, cells)
    else:
        selected = shapely.intersects(cells, boundary)

    positions = np.flatnonzero(selected)
    result = cells[selected]
    if mode == "clip" and len(result):
        clipped = shapely.intersection(result, boundary)
        kept_geometries: list[object] = []
        kept_positions: list[int] = []
        for position, geometry in zip(positions, clipped, strict=True):
            polygon = polygonal_parts(np.asarray([geometry], dtype=object))
            if len(polygon) and polygon[0].area > 0:
                kept_geometries.append(polygon[0])
                kept_positions.append(int(position))
        result = np.asarray(kept_geometries, dtype=object)
        positions = np.asarray(kept_positions, dtype=np.int64)
    return result, positions


def iter_grid_batches(
    boundary: object, size: float, chunk_rows: int, mode: str
) -> Iterable[list[dict[str, object]]]:
    """分批生成并筛选规则网格，避免一次性保存全部候选格。"""
    min_x, min_y, max_x, max_y = boundary.bounds
    start_x = math.floor(min_x / size) * size
    start_y = math.floor(min_y / size) * size
    end_x = math.ceil(max_x / size) * size
    end_y = math.ceil(max_y / size) * size
    column_count = int(round((end_x - start_x) / size))
    row_count = int(round((end_y - start_y) / size))

    x_values = start_x + np.arange(column_count, dtype=float) * size
    for first_row in range(0, row_count, chunk_rows):
        rows_in_batch = min(chunk_rows, row_count - first_row)
        local_rows = np.repeat(np.arange(rows_in_batch), column_count)
        columns = np.tile(np.arange(column_count), rows_in_batch)
        x_min = x_values[columns]
        y_min = start_y + (first_row + local_rows) * size
        cells = shapely.box(x_min, y_min, x_min + size, y_min + size)

        geometries, positions = choose_cells(cells, boundary, mode)
        features: list[dict[str, object]] = []
        for geometry, position in zip(geometries, positions, strict=True):
            local_row = int(local_rows[position])
            column = int(columns[position])
            row = first_row + local_row
            features.append(
                {
                    "geometry": mapping(geometry),
                    "properties": {
                        "grid_id": f"R{row:05d}C{column:05d}",
                        "row": row,
                        "col": column,
                        "xmin_m": float(x_min[position]),
                        "ymin_m": float(y_min[position]),
                        "size_m": float(size),
                        "area_m2": float(geometry.area),
                    },
                }
            )
        if features:
            yield features


def load_projected_boundary(input_path: Path) -> object:
    """读取、修复并合并边界，然后投影到目标米制坐标系。"""
    source = gpd.read_file(input_path)
    if source.empty:
        raise ValueError(f"输入 Shapefile 没有要素：{input_path}")
    if source.crs is None:
        raise ValueError(f"输入 Shapefile 缺少坐标系（.prj）：{input_path}")

    source = source[source.geometry.notna() & ~source.geometry.is_empty].copy()
    source.geometry = source.geometry.make_valid()
    projected = source.to_crs(TARGET_CRS)
    boundary = shapely.union_all(projected.geometry.to_numpy())
    if boundary.is_empty:
        raise ValueError(f"输入边界没有有效的面几何：{input_path}")
    return boundary


def generate_grid(
    input_path: Path,
    output_path: Path,
    size: float,
    mode: str,
    chunk_rows: int,
    overwrite: bool,
) -> int:
    if output_path.exists() and not overwrite:
        print(f"跳过（输出已存在）：{output_path}")
        return 0
    if output_path.exists():
        remove_shapefile(output_path)

    boundary = load_projected_boundary(input_path)
    schema = {
        "geometry": "Polygon",
        "properties": {
            "grid_id": "str:20",
            "row": "int",
            "col": "int",
            "xmin_m": "float:20.3",
            "ymin_m": "float:20.3",
            "size_m": "float:12.3",
            "area_m2": "float:20.3",
        },
    }

    output_path.parent.mkdir(parents=True, exist_ok=True)
    count = 0
    try:
        with fiona.open(
            output_path,
            mode="w",
            driver="ESRI Shapefile",
            schema=schema,
            crs_wkt=TARGET_CRS.to_wkt(),
            encoding="UTF-8",
        ) as destination:
            for features in iter_grid_batches(boundary, size, chunk_rows, mode):
                destination.writerecords(features)
                count += len(features)
                print(f"\r  已写入 {count:,} 个网格", end="", flush=True)
    except Exception:
        remove_shapefile(output_path)
        raise

    print()
    print(f"完成：{input_path.name} -> {output_path}（{count:,} 个网格）")
    return count


def main() -> int:
    args = parse_args()
    if not math.isfinite(args.size) or args.size <= 0:
        print("错误：--size 必须是大于 0 的有限数值。", file=sys.stderr)
        return 2
    if args.chunk_rows <= 0:
        print("错误：--chunk-rows 必须大于 0。", file=sys.stderr)
        return 2

    try:
        shapefiles = find_shapefiles(args.input)
        output_dir = args.output_dir.expanduser().resolve()
        total = 0
        for index, input_path in enumerate(shapefiles, start=1):
            suffix = f"_{args.size / 1000:g}km_grid"
            output_path = output_dir / f"{input_path.stem}{suffix}.shp"
            print(f"[{index}/{len(shapefiles)}] 处理：{input_path}")
            total += generate_grid(
                input_path=input_path,
                output_path=output_path,
                size=args.size,
                mode=args.mode,
                chunk_rows=args.chunk_rows,
                overwrite=args.overwrite,
            )
        print(f"全部完成，共生成 {total:,} 个网格要素。")
        return 0
    except (OSError, ValueError) as error:
        print(f"错误：{error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
