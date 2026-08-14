#!/usr/bin/env python3
"""使用市级边界，汇总业务 Shapefile 在每个市内的图斑面积（亩）。

源数据和市界只读。每个业务 Shapefile 独立计算，失败时丢弃该文件的全部
临时结果、记录错误，然后继续处理其余文件。
"""

from __future__ import annotations

import argparse
import csv
import multiprocessing as mp
import os
import sys
import time
from collections import defaultdict
from concurrent.futures import ProcessPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator

import fiona
from pyproj import CRS, Transformer
from shapely import make_valid
from shapely.geometry import shape
from shapely.ops import transform, unary_union
from shapely.prepared import prep


SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_INPUT_DIR = Path(
    r"阶段成果数据"
)
DEFAULT_CITY_SHP = SCRIPT_DIR / "00市边界" / "15_市边界.shp"
DEFAULT_OUTPUT_CSV = SCRIPT_DIR / "每个市总图斑亩数.csv"
SQUARE_METRES_TO_MU = 0.0015  # 1 亩 = 666.666... 平方米

# 统一在等积投影下求交和计算面积。源文件不会被重投影或修改。
AREA_CRS = CRS.from_proj4(
    "+proj=aea +lat_1=25 +lat_2=47 +lat_0=0 +lon_0=105 "
    "+datum=WGS84 +units=m +no_defs"
)


@dataclass(frozen=True)
class City:
    code: str
    name: str
    geometry: Any
    prepared_geometry: Any


@dataclass(frozen=True)
class FileResult:
    feature_count: int
    polygon_part_count: int
    empty_geometry_count: int
    non_polygon_count: int
    repaired_geometry_count: int
    source_crs: str
    city_area_m2: dict[str, float]
    city_hit_count: dict[str, int]


@dataclass(frozen=True)
class ScanError:
    path: str
    message: str


@dataclass(frozen=True)
class WorkerResult:
    """子进程结果；异常也转为普通结果，便于主进程继续处理其他文件。"""

    index: int
    path: str
    elapsed: float
    result: FileResult | None
    error: str


# 每个子进程各自初始化一次。不要把 Shapely prepared geometry 逐任务传输。
_WORKER_CITIES: list[City] | None = None


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="按市级边界汇总所有业务 .shp 图斑在每个市内的面积（亩）。"
    )
    parser.add_argument(
        "input_dir",
        nargs="?",
        type=Path,
        default=DEFAULT_INPUT_DIR,
        help=f"业务 Shapefile 根目录（默认：{DEFAULT_INPUT_DIR}）",
    )
    parser.add_argument(
        "--city-shp",
        type=Path,
        default=DEFAULT_CITY_SHP,
        help=f"市级边界 Shapefile（默认：{DEFAULT_CITY_SHP}）",
    )
    parser.add_argument(
        "-o",
        "--output",
        type=Path,
        default=DEFAULT_OUTPUT_CSV,
        help=f"市级汇总 CSV（默认：{DEFAULT_OUTPUT_CSV}）",
    )
    parser.add_argument(
        "--city-name-field",
        default="市名称",
        help="市界中的市名称字段（默认：市名称）",
    )
    parser.add_argument(
        "--city-code-field",
        default="市代码",
        help="市界中的市代码字段（默认：市代码）",
    )
    parser.add_argument(
        "-j",
        "--workers",
        type=int,
        default=min(16, os.cpu_count() or 1),
        help="并行子进程数（默认：CPU 核数与 16 的较小值；设为 1 可串行运行）",
    )
    return parser.parse_args()


def format_error(exc: BaseException) -> str:
    message = " ".join(str(exc).split()) or repr(exc)
    return f"{type(exc).__name__}: {message}"


def details_path(output_csv: Path) -> Path:
    return output_csv.with_name(f"{output_csv.stem}_处理明细{output_csv.suffix}")


def find_shapefiles(root: Path) -> tuple[list[Path], list[ScanError]]:
    shapefiles: list[Path] = []
    errors: list[ScanError] = []

    def on_walk_error(exc: OSError) -> None:
        errors.append(ScanError(exc.filename or str(root), format_error(exc)))

    for directory, _, filenames in os.walk(root, onerror=on_walk_error):
        for filename in filenames:
            if filename.lower().endswith(".shp"):
                shapefiles.append(Path(directory) / filename)
    shapefiles.sort(key=lambda path: str(path).casefold())
    return shapefiles, errors


def collection_crs(source: fiona.Collection, label: str) -> CRS:
    value = source.crs_wkt or source.crs
    if not value:
        raise ValueError(f"{label}缺少坐标系信息（通常是 .prj 缺失）")
    return CRS.from_user_input(value)


def polygon_parts(geometry: Any) -> Iterator[Any]:
    if geometry.geom_type == "Polygon":
        yield geometry
    elif geometry.geom_type in {"MultiPolygon", "GeometryCollection"}:
        for child in geometry.geoms:
            yield from polygon_parts(child)


def geometry_to_area_crs(geometry: Any, transformer: Transformer | None) -> Any:
    if transformer is not None:
        geometry = transform(transformer.transform, geometry)
    return geometry


def load_cities(city_shp: Path, name_field: str, code_field: str) -> list[City]:
    """加载市界、按市代码合并，并转换到统一等积投影。"""
    grouped: dict[tuple[str, str], list[Any]] = defaultdict(list)
    with fiona.open(city_shp, mode="r") as source:
        fields = source.schema.get("properties", {})
        missing = [field for field in (name_field, code_field) if field not in fields]
        if missing:
            raise ValueError(
                f"市界缺少字段 {missing}；现有字段：{list(fields)}"
            )
        source_crs = collection_crs(source, "市界")
        transformer = None
        if not source_crs.equals(AREA_CRS):
            transformer = Transformer.from_crs(source_crs, AREA_CRS, always_xy=True)

        for feature in source:
            properties = feature["properties"]
            name = str(properties.get(name_field) or "").strip()
            code = str(properties.get(code_field) or "").strip()
            if not name or not code:
                raise ValueError(f"市界存在空的市名称或市代码：{dict(properties)}")
            geometry_mapping = feature.get("geometry")
            if geometry_mapping is None:
                raise ValueError(f"市界 {name}（{code}）的几何为空")
            geometry = geometry_to_area_crs(shape(geometry_mapping), transformer)
            if not geometry.is_valid:
                geometry = make_valid(geometry)
            parts = list(polygon_parts(geometry))
            if not parts:
                raise ValueError(f"市界 {name}（{code}）不是面几何")
            grouped[(code, name)].extend(parts)

    cities: list[City] = []
    for (code, name), geometries in sorted(grouped.items()):
        geometry = unary_union(geometries)
        cities.append(City(code, name, geometry, prep(geometry)))
    if not cities:
        raise ValueError(f"市界文件中没有可用的市级面：{city_shp}")
    return cities


def calculate_file_by_city(shp_path: Path, cities: list[City]) -> FileResult:
    """计算一个业务 Shapefile 在各市范围内的面积；结果提交前仅存在内存中。"""
    city_area_m2: dict[str, float] = defaultdict(float)
    city_hit_count: dict[str, int] = defaultdict(int)
    feature_count = 0
    part_count = 0
    empty_count = 0
    non_polygon_count = 0
    repaired_count = 0

    with fiona.open(shp_path, mode="r") as source:
        source_crs = collection_crs(source, "业务 Shapefile")
        transformer = None
        if not source_crs.equals(AREA_CRS):
            transformer = Transformer.from_crs(source_crs, AREA_CRS, always_xy=True)

        for feature in source:
            feature_count += 1
            geometry_mapping = feature.get("geometry")
            if geometry_mapping is None:
                empty_count += 1
                continue
            geometry = shape(geometry_mapping)
            if geometry.is_empty:
                empty_count += 1
                continue
            geometry = geometry_to_area_crs(geometry, transformer)
            if not geometry.is_valid:
                geometry = make_valid(geometry)
                repaired_count += 1
            parts = list(polygon_parts(geometry))
            if not parts:
                non_polygon_count += 1
                continue

            part_count += len(parts)
            for polygon in parts:
                for city in cities:
                    if not city.prepared_geometry.intersects(polygon):
                        continue
                    intersection = polygon.intersection(city.geometry)
                    area = intersection.area
                    if area > 0:
                        city_area_m2[city.code] += area
                        city_hit_count[city.code] += 1

    return FileResult(
        feature_count=feature_count,
        polygon_part_count=part_count,
        empty_geometry_count=empty_count,
        non_polygon_count=non_polygon_count,
        repaired_geometry_count=repaired_count,
        source_crs=source_crs.name,
        city_area_m2=dict(city_area_m2),
        city_hit_count=dict(city_hit_count),
    )


def init_worker(city_shp: str, name_field: str, code_field: str) -> None:
    """在子进程内加载市界，规避 GDAL/Fiona fork 状态及重复序列化问题。"""
    global _WORKER_CITIES
    _WORKER_CITIES = load_cities(Path(city_shp), name_field, code_field)


def calculate_file_worker(index: int, shp_path: str) -> WorkerResult:
    """子进程入口：计算单个文件，并把可恢复异常返回给主进程。"""
    started = time.monotonic()
    try:
        if _WORKER_CITIES is None:
            raise RuntimeError("并行工作进程尚未载入市界")
        result = calculate_file_by_city(Path(shp_path), _WORKER_CITIES)
        return WorkerResult(
            index=index,
            path=shp_path,
            elapsed=time.monotonic() - started,
            result=result,
            error="",
        )
    except Exception as exc:
        return WorkerResult(
            index=index,
            path=shp_path,
            elapsed=time.monotonic() - started,
            result=None,
            error=format_error(exc),
        )


def write_summary(
    output_csv: Path,
    cities: list[City],
    total_area: dict[str, float],
    city_file_count: dict[str, int],
    city_hit_count: dict[str, int],
) -> None:
    fieldnames = [
        "市代码", "市名称", "总图斑面积_亩", "总图斑面积_平方米",
        "有面积贡献的shp数", "相交图斑部件次数",
    ]
    with output_csv.open("w", newline="", encoding="utf-8-sig") as csv_file:
        writer = csv.DictWriter(csv_file, fieldnames=fieldnames)
        writer.writeheader()
        for city in cities:
            area_m2 = total_area.get(city.code, 0.0)
            writer.writerow(
                {
                    "市代码": city.code,
                    "市名称": city.name,
                    "总图斑面积_亩": f"{area_m2 * SQUARE_METRES_TO_MU:.6f}",
                    "总图斑面积_平方米": f"{area_m2:.4f}",
                    "有面积贡献的shp数": city_file_count.get(city.code, 0),
                    "相交图斑部件次数": city_hit_count.get(city.code, 0),
                }
            )


def run(
    input_dir: Path,
    city_shp: Path,
    output_csv: Path,
    name_field: str,
    code_field: str,
    workers: int,
) -> int:
    if workers < 1:
        raise ValueError("--workers 必须大于或等于 1")
    if not input_dir.is_dir():
        raise FileNotFoundError(f"业务数据目录不存在、无法访问或不是目录：{input_dir}")
    if not city_shp.is_file():
        raise FileNotFoundError(f"市界 Shapefile 不存在或无法访问：{city_shp}")

    started = time.monotonic()
    detail_csv = details_path(output_csv)
    output_csv.parent.mkdir(parents=True, exist_ok=True)
    print(f"开始时间：{datetime.now():%Y-%m-%d %H:%M:%S}")
    print(f"业务数据：{input_dir}")
    print(f"市界文件：{city_shp}")
    print(f"汇总 CSV：{output_csv.resolve()}")
    print(f"明细 CSV：{detail_csv.resolve()}")
    print("面积方法：统一转换到中国 Albers 等积投影后，按市界求交")
    print("换算关系：1 亩 = 666.6666667 平方米")

    cities = load_cities(city_shp, name_field, code_field)
    print(f"载入市级边界：{len(cities)} 个")
    for city in cities:
        print(f"  - {city.code} {city.name}")

    shapefiles, scan_errors = find_shapefiles(input_dir)
    print(f"找到业务 .shp：{len(shapefiles)} 个")
    if not shapefiles:
        raise FileNotFoundError(f"业务目录及可访问子目录中未找到 .shp：{input_dir}")
    actual_workers = min(workers, len(shapefiles))
    print(f"计算进程：{actual_workers} 个（可用 -j/--workers 调整）")

    total_area: dict[str, float] = defaultdict(float)
    city_file_count: dict[str, int] = defaultdict(int)
    city_hit_count: dict[str, int] = defaultdict(int)
    success_count = 0
    failed_count = 0
    detail_fields = [
        "序号", "市代码", "市名称", "来源shp", "来源相对路径",
        "该shp在该市面积_亩", "该shp在该市面积_平方米", "相交图斑部件次数",
        "源要素数", "源面部件数", "空几何数", "非面要素数", "修复几何数",
        "源坐标系", "状态", "耗时_秒", "错误信息",
    ]

    with detail_csv.open("w", newline="", encoding="utf-8-sig") as detail_file:
        writer = csv.DictWriter(detail_file, fieldnames=detail_fields)
        writer.writeheader()
        for error in scan_errors:
            writer.writerow(
                {"状态": "目录扫描失败", "来源相对路径": error.path, "错误信息": error.message}
            )
        detail_file.flush()

        # spawn 对 Fiona/GDAL 更安全；各进程在 initializer 中只加载一次市界。
        context = mp.get_context("spawn")
        with ProcessPoolExecutor(
            max_workers=actual_workers,
            mp_context=context,
            initializer=init_worker,
            initargs=(str(city_shp), name_field, code_field),
        ) as executor:
            future_to_task = {
                executor.submit(calculate_file_worker, index, str(shp_path)): (index, shp_path)
                for index, shp_path in enumerate(shapefiles, start=1)
            }

            completed = 0
            for future in as_completed(future_to_task):
                fallback_index, fallback_path = future_to_task[future]
                completed += 1
                try:
                    worker_result = future.result()
                except Exception as exc:
                    worker_result = WorkerResult(
                        index=fallback_index,
                        path=str(fallback_path),
                        elapsed=0.0,
                        result=None,
                        error=f"工作进程异常退出：{format_error(exc)}",
                    )

                index = worker_result.index
                shp_path = Path(worker_result.path)
                elapsed = worker_result.elapsed
                try:
                    relative_path = str(shp_path.relative_to(input_dir))
                except ValueError:
                    relative_path = str(shp_path)
                print(
                    f"[{completed}/{len(shapefiles)} 完成；原序号 {index}] {relative_path}",
                    flush=True,
                )
                if worker_result.result is not None:
                    result = worker_result.result
                    file_total_m2 = sum(result.city_area_m2.values())

                    # 只有整个文件成功后才提交到全局汇总，防止失败文件留下部分面积。
                    for city in cities:
                        area_m2 = result.city_area_m2.get(city.code, 0.0)
                        hit_count = result.city_hit_count.get(city.code, 0)
                        if area_m2 <= 0:
                            continue
                        total_area[city.code] += area_m2
                        city_file_count[city.code] += 1
                        city_hit_count[city.code] += hit_count
                        writer.writerow(
                            {
                                "序号": index, "市代码": city.code, "市名称": city.name,
                                "来源shp": shp_path.name, "来源相对路径": relative_path,
                                "该shp在该市面积_亩": f"{area_m2 * SQUARE_METRES_TO_MU:.6f}",
                                "该shp在该市面积_平方米": f"{area_m2:.4f}",
                                "相交图斑部件次数": hit_count,
                                "源要素数": result.feature_count,
                                "源面部件数": result.polygon_part_count,
                                "空几何数": result.empty_geometry_count,
                                "非面要素数": result.non_polygon_count,
                                "修复几何数": result.repaired_geometry_count,
                                "源坐标系": result.source_crs, "状态": "成功",
                                "耗时_秒": f"{elapsed:.3f}", "错误信息": "",
                            }
                        )
                    if file_total_m2 <= 0:
                        writer.writerow(
                            {
                                "序号": index, "来源shp": shp_path.name,
                                "来源相对路径": relative_path,
                                "源要素数": result.feature_count,
                                "源面部件数": result.polygon_part_count,
                                "空几何数": result.empty_geometry_count,
                                "非面要素数": result.non_polygon_count,
                                "修复几何数": result.repaired_geometry_count,
                                "源坐标系": result.source_crs,
                                "状态": "成功（未落入市界）",
                                "耗时_秒": f"{elapsed:.3f}", "错误信息": "",
                            }
                        )
                    success_count += 1
                    print(
                        f"           成功：分配到市界内 {file_total_m2 * SQUARE_METRES_TO_MU:,.4f} 亩 | "
                        f"源要素 {result.feature_count:,} 个 | 耗时 {elapsed:.3f} 秒",
                        flush=True,
                    )
                else:
                    failed_count += 1
                    error_message = worker_result.error
                    writer.writerow(
                        {
                            "序号": index, "来源shp": shp_path.name,
                            "来源相对路径": relative_path, "状态": "失败",
                            "耗时_秒": f"{elapsed:.3f}", "错误信息": error_message,
                        }
                    )
                    print(
                        f"           失败，整文件已跳过：{error_message} | 耗时 {elapsed:.3f} 秒",
                        file=sys.stderr,
                        flush=True,
                    )
                detail_file.flush()

    write_summary(output_csv, cities, total_area, city_file_count, city_hit_count)
    elapsed = time.monotonic() - started
    total_mu = sum(total_area.values()) * SQUARE_METRES_TO_MU
    print("=" * 78)
    for city in cities:
        print(f"{city.code} {city.name}：{total_area.get(city.code, 0.0) * SQUARE_METRES_TO_MU:,.4f} 亩")
    print("-" * 78)
    print(f"成功文件：{success_count} 个；失败文件：{failed_count} 个")
    print(f"目录扫描错误：{len(scan_errors)} 处")
    print(f"各市面积合计：{total_mu:,.4f} 亩")
    print(f"总耗时：{elapsed:.3f} 秒")
    print(f"汇总 CSV：{output_csv.resolve()}")
    print(f"处理明细：{detail_csv.resolve()}")
    return 0


def main() -> int:
    args = parse_args()
    try:
        return run(
            args.input_dir,
            args.city_shp,
            args.output,
            args.city_name_field,
            args.city_code_field,
            args.workers,
        )
    except KeyboardInterrupt:
        print("\n用户中断运行；明细 CSV 中已写入的结果仍然保留。", file=sys.stderr)
        return 130
    except Exception as exc:
        print(f"致命错误：{format_error(exc)}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
