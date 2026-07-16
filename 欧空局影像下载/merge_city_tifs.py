#!/usr/bin/env python3
"""按城市和成像日期拼接 Sentinel GeoTIFF，并按市界裁剪。"""

from __future__ import annotations

import argparse
import re
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import ExitStack
from pathlib import Path

import geopandas as gpd
import rasterio
from rasterio.features import geometry_mask
from rasterio.merge import merge
from rasterio.vrt import WarpedVRT
from rasterio.warp import Resampling, transform_bounds
from rich.console import Console
from shapely import union_all
from shapely.geometry import box, mapping


console = Console()

DEFAULT_INPUT_ROOT = Path("./sentinel_data")
DEFAULT_OUTPUT_ROOT = Path("./merge_data")
DEFAULT_CITY_LAYER = Path("./00市边界/15_市边界.shp")
DEFAULT_CITY_FIELD = "市名称"
DEFAULT_CITY_CODE_FIELD = "市代码"

# Sentinel-2 产品名中的第一个时间是成像时间，最后一个时间是产品生成时间。
SENSING_TIME_RE = re.compile(r"_(?P<date>\d{8})T\d{6}_")
PRODUCT_RE = re.compile(
    r"^(?P<satellite>S2[A-Z])_.*?_(?P<sensing>\d{8}T\d{6})_.*?"
    r"_(?P<tile>T\d{2}[A-Z]{3})_(?P<produced>\d{8}T\d{6})\.tif$",
    re.IGNORECASE,
)
MERGED_NAME_RE = re.compile(
    r"^(?P<prefix>S2[A-Z]_MSIL[12][AC]_\d{8}T\d{6}_N\d{4}_R\d{3})_"
    r"T\d{2}[A-Z]{3}_\d{8}T\d{6}\.tif$",
    re.IGNORECASE,
)


def normalize_region_name(name: object) -> str:
    """忽略常见行政区后缀，方便目录名与边界名称匹配。"""
    text = str(name).strip()
    for suffix in ("市", "盟", "地区", "自治州"):
        if text.endswith(suffix):
            return text[: -len(suffix)]
    return text


def load_city_boundaries(path: Path, name_field: str) -> gpd.GeoDataFrame:
    if not path.exists():
        raise FileNotFoundError(f"市边界文件不存在：{path}")
    cities = gpd.read_file(path)
    if cities.empty:
        raise ValueError(f"市边界文件为空：{path}")
    if name_field not in cities.columns:
        raise ValueError(f"市边界缺少名称字段 {name_field!r}，现有字段：{list(cities.columns)}")
    if cities.crs is None:
        raise ValueError(f"市边界没有坐标系信息：{path}")
    cities = cities[cities.geometry.notna() & ~cities.geometry.is_empty].copy()
    cities["_normalized_name"] = cities[name_field].map(normalize_region_name)
    return cities


def find_city_geometry(cities: gpd.GeoDataFrame, city_name: str):
    wanted = normalize_region_name(city_name)
    matched = cities[cities["_normalized_name"] == wanted]
    if matched.empty:
        return None
    geometry = matched.geometry.union_all() if hasattr(matched.geometry, "union_all") else matched.unary_union
    if not geometry.is_valid:
        geometry = geometry.buffer(0)
    return geometry


def find_boundary_value(
    cities: gpd.GeoDataFrame, city_name: str, field: str
) -> str | None:
    """按城市匹配边界，并读取边界属性表中的指定字段。"""
    wanted = normalize_region_name(city_name)
    matched = cities[cities["_normalized_name"] == wanted]
    if matched.empty or field not in matched.columns:
        return None
    value = str(matched.iloc[0][field]).strip()
    return value or None


def sensing_date(path: Path) -> str | None:
    match = SENSING_TIME_RE.search(path.name)
    return match.group("date") if match else None


def merged_output_name(city_name: str, date: str, paths: list[Path]) -> str:
    """生成接近 Sentinel 产品格式、同时明确表示市域合成的文件名。"""
    prefixes = {
        match.group("prefix")
        for path in paths
        if (match := MERGED_NAME_RE.match(path.name)) is not None
    }
    safe_city = re.sub(r'[<>:"/\\|?*]', "_", city_name).strip(" .") or "CITY"
    if len(prefixes) == 1:
        return f"{next(iter(prefixes))}_MERGED_{safe_city}.tif"
    return f"S2_MERGED_{date}_MULTI_{safe_city}.tif"


def select_latest_products(paths: list[Path]) -> tuple[list[Path], int]:
    """返回当天全部 TIFF；同瓦片的不同产品可能含有互补的有效像元。"""
    return sorted(paths), 0


def group_city_files(city_dir: Path) -> dict[str, list[Path]]:
    groups: dict[str, list[Path]] = {}
    for path in sorted(city_dir.glob("*.tif")):
        date = sensing_date(path)
        if date is None:
            console.print(f"[yellow]⚠️ 无法从文件名提取成像日期，已跳过：{path.name}[/yellow]")
            continue
        groups.setdefault(date, []).append(path)
    return groups


def projected_city_geometry(cities: gpd.GeoDataFrame, city_geometry):
    one_city = gpd.GeoDataFrame(geometry=[city_geometry], crs=cities.crs)
    target_crs = one_city.estimate_utm_crs()
    if target_crs is None:
        raise ValueError("无法根据市边界自动确定投影坐标系")
    projected = one_city.to_crs(target_crs).geometry.iloc[0]
    return target_crs, projected


def source_footprint(path: Path, target_crs):
    with rasterio.open(path) as src:
        if src.crs is None:
            raise ValueError(f"栅格没有坐标系：{path}")
        left, bottom, right, top = transform_bounds(
            src.crs, target_crs, *src.bounds, densify_pts=21
        )
    return box(left, bottom, right, top)


def calculate_coverage(paths: list[Path], city_geometry, target_crs) -> tuple[float, float]:
    footprints = [source_footprint(path, target_crs) for path in paths]
    covered = union_all(footprints).intersection(city_geometry)
    city_area = city_geometry.area
    if city_area <= 0:
        raise ValueError("市边界面积为 0，无法计算覆盖率")
    coverage = min(100.0, max(0.0, covered.area / city_area * 100.0))
    missing_km2 = max(0.0, city_area - covered.area) / 1_000_000
    return coverage, missing_km2


def aligned_bounds(bounds, resolution: float) -> tuple[float, float, float, float]:
    left, bottom, right, top = bounds
    return (
        int(left // resolution) * resolution,
        int(bottom // resolution) * resolution,
        int(-(-right // resolution)) * resolution,
        int(-(-top // resolution)) * resolution,
    )


def mask_outside_city(
    path: Path,
    city_geometry,
    nodata: int = 0,
) -> None:
    """逐块掩膜，避免把一个市的完整影像一次性读入内存。"""
    shapes = [mapping(city_geometry)]
    with rasterio.Env(GDAL_TIFF_INTERNAL_MASK="YES"), rasterio.open(path, "r+") as dataset:
        for _, window in dataset.block_windows(1):
            inside = geometry_mask(
                shapes,
                out_shape=(window.height, window.width),
                transform=dataset.window_transform(window),
                invert=True,
            )
            data = dataset.read(window=window)
            if not inside.all():
                data[:, ~inside] = nodata
                dataset.write(data, window=window)
            valid_data = inside & (data != nodata).any(axis=0)
            dataset.write_mask(valid_data.astype("uint8") * 255, window=window)


def merge_one_group(
    paths: list[Path],
    output_path: Path,
    city_geometry,
    target_crs,
    resolution: float,
    overwrite: bool,
    coverage: float,
) -> str:
    if output_path.exists() and not overwrite:
        return "skipped"

    output_path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = output_path.with_name(f".{output_path.stem}.tmp.tif")
    if temporary_path.exists():
        temporary_path.unlink()

    bounds = aligned_bounds(city_geometry.bounds, resolution)
    try:
        with ExitStack() as stack:
            sources = []
            first_count = None
            first_dtype = None
            descriptions = None
            for path in paths:
                src = stack.enter_context(rasterio.open(path))
                if src.crs is None:
                    raise ValueError(f"栅格没有坐标系：{path}")
                if first_count is None:
                    first_count = src.count
                    first_dtype = src.dtypes[0]
                    descriptions = src.descriptions
                elif src.count != first_count or src.dtypes[0] != first_dtype:
                    raise ValueError(f"波段数或数据类型不一致：{path.name}")
                vrt = stack.enter_context(
                    WarpedVRT(
                        src,
                        crs=target_crs,
                        resolution=resolution,
                        src_nodata=0,
                        nodata=0,
                        resampling=Resampling.nearest,
                    )
                )
                sources.append(vrt)

            merge(
                sources,
                bounds=bounds,
                res=resolution,
                nodata=0,
                dtype=first_dtype,
                method="first",
                target_aligned_pixels=True,
                mem_limit=256,
                dst_path=temporary_path,
                dst_kwds={
                    "driver": "GTiff",
                    "compress": "deflate",
                    "predictor": 2,
                    "tiled": True,
                    "blockxsize": 512,
                    "blockysize": 512,
                    "BIGTIFF": "YES",
                },
            )

            mask_outside_city(temporary_path, city_geometry)
        with rasterio.open(temporary_path, "r+") as output:
            if descriptions:
                output.descriptions = descriptions
            output.update_tags(
                city_coverage_percent=f"{coverage:.6f}",
                source_count=str(len(paths)),
                clipped_to_city_boundary="true",
            )
        temporary_path.replace(output_path)
    finally:
        if temporary_path.exists():
            try:
                temporary_path.unlink()
                console.print(f"[dim]已删除临时文件：{temporary_path.name}[/dim]")
            except OSError as exc:
                console.print(f"[yellow]⚠️ 临时文件删除失败：{temporary_path}，{exc}[/yellow]")
    return "created"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="按市和成像日期合并 GeoTIFF，按市界裁剪，并报告市域覆盖率。"
    )
    parser.add_argument("--input-root", type=Path, default=DEFAULT_INPUT_ROOT, help="输入根目录")
    parser.add_argument("--output-root", type=Path, default=DEFAULT_OUTPUT_ROOT, help="输出根目录")
    parser.add_argument("--city-layer", type=Path, default=DEFAULT_CITY_LAYER, help="市边界文件")
    parser.add_argument("--city-field", default=DEFAULT_CITY_FIELD, help="市名称字段")
    parser.add_argument("--city-code-field", default=DEFAULT_CITY_CODE_FIELD, help="市代码字段")
    parser.add_argument("--city", action="append", help="只处理指定市；可重复使用")
    parser.add_argument("--resolution", type=float, default=10.0, help="输出分辨率（米）")
    parser.add_argument(
        "--coverage-threshold",
        type=float,
        default=99.9,
        help="判为完整覆盖的最低百分比，默认 99.9",
    )
    parser.add_argument("--overwrite", action="store_true", help="覆盖已有合成结果")
    parser.add_argument("--max-workers", type=int, default=1, help="并发合成任务数，默认 1（顺序处理）")
    return parser.parse_args()


def print_summary(results: list[dict], created: int, skipped: int, failed: int) -> None:
    """在所有日期处理完成后，统一输出覆盖和合成结果。"""
    console.print("\n[bold cyan]========== 处理结果汇总 ==========[/bold cyan]")
    complete_count = 0
    incomplete_count = 0

    for result in results:
        coverage = result.get("coverage")
        if coverage is None:
            console.print(
                f"[red]❌ {result['city']} {result['date']}：合成失败，{result['error']}[/red]"
            )
            continue

        is_complete = result["complete"]
        if is_complete:
            complete_count += 1
            coverage_text = "已完整覆盖"
            color = "green"
        else:
            incomplete_count += 1
            coverage_text = f"未完整覆盖，约缺失 {result['missing_km2']:.2f} km²"
            color = "yellow"

        status_text = {
            "created": "合成完成",
            "skipped": "输出已存在，已跳过",
            "failed": f"合成失败：{result['error']}",
        }[result["status"]]
        duplicate_text = (
            f"；忽略旧版本 {result['duplicate_count']} 个"
            if result["duplicate_count"]
            else ""
        )
        console.print(
            f"[{color}]{result['city']} {result['date']}：覆盖率 {coverage:.4f}%，"
            f"{coverage_text}[/{color}]"
        )
        console.print(
            f"结果：{status_text}；总共 {result['total_count']} 个 tif，"
            f"用上 {result['source_count']} 个 tif{duplicate_text}"
        )
        console.print(f"[dim]输出：{result['output']}[/dim]")

    console.print("-" * 60)
    console.print(
        f"[bold]日期数 {len(results)}，完整覆盖 {complete_count}，"
        f"未完整覆盖 {incomplete_count}；新建 {created}，跳过 {skipped}，失败 {failed}[/bold]"
    )


def main() -> int:
    args = parse_args()
    if args.max_workers < 1:
        console.print("[red]❌ max-workers 必须大于或等于 1[/red]")
        return 2
    if args.resolution <= 0:
        console.print("[red]❌ resolution 必须大于 0[/red]")
        return 2
    if not 0 < args.coverage_threshold <= 100:
        console.print("[red]❌ coverage-threshold 必须在 0 到 100 之间[/red]")
        return 2
    if not args.input_root.exists():
        console.print(f"[red]❌ 输入目录不存在：{args.input_root}[/red]")
        return 1

    try:
        cities = load_city_boundaries(args.city_layer, args.city_field)
    except Exception as exc:
        console.print(f"[red]❌ 读取市边界失败：{exc}[/red]")
        return 1
    if args.city_code_field not in cities.columns:
        console.print(f"[red]❌ 市边界缺少代码字段：{args.city_code_field}[/red]")
        return 1

    wanted = {normalize_region_name(item) for item in (args.city or [])}
    city_dirs = sorted(path for path in args.input_root.iterdir() if path.is_dir())
    if wanted:
        city_dirs = [path for path in city_dirs if normalize_region_name(path.name) in wanted]

    total_created = total_skipped = total_failed = 0
    results: list[dict] = []
    jobs: list[dict] = []
    for city_dir in city_dirs:
        city_geometry = find_city_geometry(cities, city_dir.name)
        boundary_city_name = find_boundary_value(cities, city_dir.name, args.city_field)
        boundary_city_code = find_boundary_value(cities, city_dir.name, args.city_code_field)
        if city_geometry is None or boundary_city_name is None or boundary_city_code is None:
            console.print(f"[red]❌ 未在市边界中找到 {city_dir.name}，已跳过该目录[/red]")
            total_failed += 1
            continue

        groups = group_city_files(city_dir)
        if not groups:
            console.print(f"[yellow]⚠️ {city_dir.name} 没有可处理的 tif[/yellow]")
            continue

        try:
            target_crs, projected_boundary = projected_city_geometry(cities, city_geometry)
        except Exception as exc:
            console.print(f"[red]❌ {city_dir.name} 投影转换失败：{exc}[/red]")
            total_failed += len(groups)
            continue

        console.print(
            f"\n[bold cyan]========== {boundary_city_name}（{target_crs}）==========[/bold cyan]"
        )
        for date, raw_paths in sorted(groups.items()):
            paths, duplicate_count = select_latest_products(raw_paths)
            output = args.output_root / boundary_city_name / merged_output_name(
                boundary_city_code, date, paths
            )
            coverage = None
            missing_km2 = 0.0
            try:
                coverage, missing_km2 = calculate_coverage(paths, projected_boundary, target_crs)
                date_label = date[4:] if len(date) == 8 else date
                console.print(
                    f"[cyan]{date_label}：总共 {len(raw_paths)} 个 tif，"
                    f"用上 {len(paths)} 个 tif，覆盖率：{coverage:.4f}%[/cyan]"
                )

                result = {
                    "city": boundary_city_name,
                    "date": date,
                    "coverage": coverage,
                    "missing_km2": missing_km2,
                    "complete": coverage >= args.coverage_threshold,
                    "status": "pending",
                    "total_count": len(raw_paths),
                    "source_count": len(paths),
                    "duplicate_count": duplicate_count,
                    "output": output,
                    "error": "",
                }
                if output.exists() and not args.overwrite:
                    result["status"] = "skipped"
                    total_skipped += 1
                    results.append(result)
                    console.print(f"[green]市级影像就绪：{output}[/green]")
                else:
                    jobs.append(
                        {
                            "paths": paths,
                            "boundary": projected_boundary,
                            "target_crs": target_crs,
                            "result": result,
                        }
                    )
            except Exception as exc:
                total_failed += 1
                results.append(
                    {
                        "city": boundary_city_name,
                        "date": date,
                        "coverage": coverage,
                        "missing_km2": missing_km2,
                        "complete": False,
                        "status": "failed",
                        "total_count": len(raw_paths),
                        "source_count": len(paths),
                        "duplicate_count": duplicate_count,
                        "output": output,
                        "error": str(exc),
                    }
                )

    mode_text = "单线程顺序处理" if args.max_workers == 1 else f"并发线程 {args.max_workers}"
    console.print(
        f"\n[bold]扫描完成：待合成 {len(jobs)} 个，已有跳过 {total_skipped} 个，"
        f"准备失败 {total_failed} 个；{mode_text}[/bold]"
    )

    completed_jobs = 0

    def run_merge_job(job: dict) -> str:
        return merge_one_group(
            job["paths"],
            job["result"]["output"],
            job["boundary"],
            job["target_crs"],
            args.resolution,
            args.overwrite,
            job["result"]["coverage"],
        )

    def record_merge_result(job: dict, status: str | None, error: Exception | None) -> None:
        nonlocal total_created, total_skipped, total_failed, completed_jobs
        result = job["result"]
        if error is None:
            result["status"] = status
            if status == "skipped":
                total_skipped += 1
                status_text = "已跳过"
            else:
                total_created += 1
                status_text = "合成完成"
            console.print(f"[green]✅ {result['city']} {result['date']}：{status_text}[/green]")
            console.print(f"[green]市级影像就绪：{result['output']}[/green]")
        else:
            total_failed += 1
            result["status"] = "failed"
            result["error"] = str(error)
            console.print(f"[red]❌ {result['city']} {result['date']} 合成失败：{error}[/red]")

        results.append(result)
        completed_jobs += 1
        percent = completed_jobs / len(jobs) * 100
        console.print(
            f"[cyan]市级合成进度：{completed_jobs}/{len(jobs)}，{percent:.2f}%[/cyan]"
        )

    if jobs:
        if args.max_workers == 1:
            try:
                for job in jobs:
                    try:
                        status = run_merge_job(job)
                        record_merge_result(job, status, None)
                    except Exception as exc:
                        record_merge_result(job, None, exc)
            except KeyboardInterrupt:
                console.print("\n[yellow]⚠️ 已停止市级合成。[/yellow]")
                return 130
        else:
            with ThreadPoolExecutor(max_workers=args.max_workers) as executor:
                futures = {
                    executor.submit(run_merge_job, job): job
                    for job in jobs
                }
                for future in as_completed(futures):
                    job = futures[future]
                    try:
                        record_merge_result(job, future.result(), None)
                    except Exception as exc:
                        record_merge_result(job, None, exc)

    results.sort(key=lambda item: (item["city"], item["date"]))
    print_summary(results, total_created, total_skipped, total_failed)
    return 1 if total_failed else 0


if __name__ == "__main__":
    sys.exit(main())
