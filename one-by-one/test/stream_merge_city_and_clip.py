#!/usr/bin/env python3
r"""逐组完成 Sentinel 市级合并、真彩色 JPEG 和县级裁剪。

与 ``merge_city_tifs.py`` 的“先扫描全部分组、再统一合并”不同，本脚本按
“城市 + 成像日期 + 卫星”依次处理：发现一个可用分组后立即生成市级成果，
随后立即裁剪该市所辖县级成果，再继续扫描下一个分组。

默认目录：
    \\169.254.51.68\data\原始影像\sentinel_data
    \\169.254.51.68\data\原始影像\city_data
    \\169.254.51.68\data\原始影像\country_data
"""

from __future__ import annotations

import argparse
import math
import os
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import numpy as np
import rasterio
from PIL import Image
from rasterio.enums import Resampling
from rich.console import Console

from clip_county_tifs import (
    clip_one_county,
    county_output_name,
    load_counties,
    parse_merged_filename,
    safe_filename,
)
from merge_city_tifs import (
    calculate_coverage,
    find_boundary_value,
    find_city_geometry,
    group_city_files,
    load_city_boundaries,
    merge_one_group,
    merged_output_name,
    projected_city_geometry,
    select_latest_products,
)


console = Console()
BASE_DIR = Path(__file__).resolve().parent
DEFAULT_INPUT_ROOT = (
    Path(r"\\169.254.51.68\data\原始影像\sentinel_data")
    if os.name == "nt"
    else Path("/media/cangling/nas_folder/原始影像/sentinel_data")
)
DEFAULT_CITY_ROOT = (
    Path(r"\\169.254.51.68\data\原始影像\city_data")
    if os.name == "nt"
    else Path("/media/cangling/nas_folder/原始影像/city_data")
)
DEFAULT_COUNTRY_ROOT = (
    Path(r"\\169.254.51.68\data\原始影像\country_data")
    if os.name == "nt"
    else Path("/media/cangling/nas_folder/原始影像/country_data")
)
DEFAULT_CITY_LAYER = BASE_DIR / "00市边界" / "15_市边界.shp"
DEFAULT_COUNTY_LAYER = BASE_DIR / "00县边界" / "15_县边界.shp"
JPEG_MAX_SIZE = 1200
SOURCE_SENSING_RE = re.compile(r"_(?P<date>\d{8})(?P<scene>T\d{6})_")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "发现一个城市/日期/卫星分组就立即合并、生成 JPEG，"
            "并立即按县界裁剪。"
        )
    )
    parser.add_argument(
        "--input-root",
        type=Path,
        default=DEFAULT_INPUT_ROOT,
        help="按城市存放单景 TIFF 的 sentinel_data",
    )
    parser.add_argument(
        "--city-root",
        type=Path,
        default=DEFAULT_CITY_ROOT,
        help="市级 TIFF/JPEG 输出目录",
    )
    parser.add_argument(
        "--country-root",
        type=Path,
        default=DEFAULT_COUNTRY_ROOT,
        help="县级 TIFF/JPEG 输出目录",
    )
    parser.add_argument(
        "--city-layer",
        type=Path,
        default=DEFAULT_CITY_LAYER,
        help="市界 Shapefile",
    )
    parser.add_argument(
        "--county-layer",
        type=Path,
        default=DEFAULT_COUNTY_LAYER,
        help="县界 Shapefile",
    )
    parser.add_argument("--city-name-field", default="市名称")
    parser.add_argument("--city-code-field", default="市代码")
    parser.add_argument("--county-name-field", default="area_name")
    parser.add_argument("--county-code-field", default="area_code")
    parser.add_argument("--city", action="append", help="只处理指定市；可重复")
    parser.add_argument(
        "--date",
        action="append",
        help="只处理指定成像日期 YYYYMMDD；可重复",
    )
    parser.add_argument(
        "--satellite",
        action="append",
        help="只处理指定卫星，如 S2A、S2B、S2C；可重复",
    )
    parser.add_argument("--resolution", type=float, default=10.0)
    parser.add_argument("--resolution-label", default="10m")
    parser.add_argument("--coverage-threshold", type=float, default=99.9)
    parser.add_argument("--nodata", type=int, default=0)
    parser.add_argument(
        "--group-workers",
        type=int,
        default=2,
        help="同时执行完整处理流水线的分组数，默认 2",
    )
    parser.add_argument(
        "--county-workers",
        type=int,
        default=1,
        help="同一市级影像的县级裁剪并发数，默认 1",
    )
    parser.add_argument("--all-touched", action="store_true")
    parser.add_argument("--overwrite", action="store_true")
    parser.add_argument(
        "--limit-groups",
        type=int,
        default=0,
        help="测试时最多处理多少个分组；0 表示不限",
    )
    return parser.parse_args()


def normalize_city_filter(value: object) -> str:
    text = str(value).strip()
    for suffix in ("市", "盟", "地区", "自治州"):
        if text.endswith(suffix):
            return text[: -len(suffix)]
    return text


def streamed_output_name(
    city_code: str,
    date: str,
    satellite: str,
    paths: list[Path],
) -> str:
    """生成并行安全的市级文件名，MULTI 分组显式包含卫星号。"""
    name = merged_output_name(city_code, date, paths)
    if name.upper().startswith("S2_MERGED_"):
        safe_satellite = re.sub(r"[^A-Z0-9]", "_", satellite.upper())
        return f"{safe_satellite}_MERGED_{date}_MULTI_{city_code}.tif"
    return name


def elapsed_text(started_at: float) -> str:
    seconds = max(0, round(time.monotonic() - started_at))
    minutes, seconds = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours:d}:{minutes:02d}:{seconds:02d}"
    return f"{minutes:02d}:{seconds:02d}"


def true_color_indexes(source: rasterio.io.DatasetReader) -> list[int]:
    """B04/B03/B02；无波段描述时按第3/2/1波段显示。"""
    descriptions = {
        str(description).strip().upper(): index
        for index, description in enumerate(source.descriptions, 1)
        if description
    }
    if all(name in descriptions for name in ("B04", "B03", "B02")):
        return [
            descriptions["B04"],
            descriptions["B03"],
            descriptions["B02"],
        ]
    if source.count >= 3:
        return [3, 2, 1]
    return [1, 1, 1]


def create_true_color_jpeg(
    tif_path: Path,
    max_size: int = JPEG_MAX_SIZE,
) -> Path:
    """使用 B04/B03/B02 和 2%~98% 拉伸生成真彩色 JPEG。"""
    jpeg_path = tif_path.with_suffix(".jpeg")
    temporary_path = jpeg_path.with_name(f".{jpeg_path.stem}.tmp.jpeg")
    temporary_path.unlink(missing_ok=True)

    try:
        with rasterio.open(tif_path) as source:
            scale = min(1.0, max_size / max(source.width, source.height))
            width = max(1, round(source.width * scale))
            height = max(1, round(source.height * scale))
            indexes = true_color_indexes(source)
            console.print(
                f"[cyan]JPEG 波段：红={indexes[0]}，绿={indexes[1]}，"
                f"蓝={indexes[2]} | {tif_path.name}[/cyan]"
            )
            data = source.read(
                indexes,
                out_shape=(3, height, width),
                resampling=Resampling.bilinear,
            ).astype("float32")
            nodata = 0 if source.nodata is None else source.nodata
            valid = np.any(np.isfinite(data) & (data != nodata), axis=0)

        rows, columns = np.where(valid)
        if rows.size == 0:
            raise ValueError(f"TIFF 没有有效影像像元：{tif_path}")
        top, bottom = int(rows.min()), int(rows.max()) + 1
        left, right = int(columns.min()), int(columns.max()) + 1
        data = data[:, top:bottom, left:right]
        valid = valid[top:bottom, left:right]

        rgb = np.full((*valid.shape, 3), 255, dtype="uint8")
        for channel in range(3):
            band = data[channel]
            sample = band[valid & np.isfinite(band) & (band > 0)]
            if sample.size == 0:
                continue
            low, high = np.percentile(sample, (2.0, 98.0))
            if not math.isfinite(float(low)) or not math.isfinite(float(high)):
                continue
            if high <= low:
                high = low + 1.0
            stretched = np.clip((band - low) / (high - low), 0, 1)
            rgb[:, :, channel] = (stretched * 255).astype("uint8")
        rgb[~valid] = 255

        Image.fromarray(rgb).save(
            temporary_path,
            format="JPEG",
            quality=88,
            subsampling=2,
            optimize=True,
            progressive=True,
        )
        temporary_path.replace(jpeg_path)
        console.print(f"[green]🖼️ 真彩色 JPEG：{jpeg_path}[/green]")
        return jpeg_path
    finally:
        temporary_path.unlink(missing_ok=True)


def clip_merged_group(
    merged_path: Path,
    city_name: str,
    country_root: Path,
    counties,
    fallback_metadata: dict[str, str],
    args: argparse.Namespace,
) -> tuple[int, int, int, int]:
    """立即裁剪一个市级成果，并返回新建/跳过/无覆盖/失败数量。"""
    metadata = parse_merged_filename(merged_path) or fallback_metadata

    selected = counties[
        counties["_city_code"] == metadata["city_code"]
    ].copy()
    if selected.empty:
        raise ValueError(
            f"市代码 {metadata['city_code']} 未匹配到任何县：{merged_path.name}"
        )
    with rasterio.open(merged_path) as source:
        if source.crs is None:
            raise ValueError(f"市级影像没有坐标系：{merged_path}")
        selected = selected.to_crs(source.crs)

    jobs: list[dict] = []
    county_total = len(selected)
    for county_index, (_, county) in enumerate(selected.iterrows(), 1):
        county_name = county["_county_name"]
        county_code = county["_county_code"]
        output_path = (
            country_root
            / safe_filename(city_name)
            / safe_filename(county_name)
            / county_output_name(
                metadata,
                county_code,
                args.resolution_label,
            )
        )
        jobs.append(
            {
                "output_path": output_path,
                "county_geometry": county.geometry,
                "county_name": county_name,
                "county_code": county_code,
                "county_index": county_index,
                "county_total": county_total,
            }
        )

    def run(job: dict) -> tuple[dict, str]:
        console.print(
            f"[cyan][县级 {job['county_index']}/{job['county_total']}] "
            f"开始：{city_name}/{job['county_name']}[/cyan]"
        )
        status = clip_one_county(
            merged_path,
            job["output_path"],
            job["county_geometry"],
            job["county_name"],
            job["county_code"],
            metadata,
            args.nodata,
            args.overwrite,
            args.all_touched,
        )
        if status in ("created", "skipped"):
            create_true_color_jpeg(job["output_path"])
        console.print(
            f"[cyan][县级 {job['county_index']}/{job['county_total']}] "
            f"结束：{city_name}/{job['county_name']}，状态 {status}[/cyan]"
        )
        return job, status

    created = skipped = no_coverage = failed = 0
    if args.county_workers == 1:
        completed = []
        for job in jobs:
            try:
                completed.append(run(job))
            except Exception as exc:
                failed += 1
                console.print(
                    f"[red]❌ 县级裁剪失败：{city_name}/"
                    f"{job['county_name']}，{exc}[/red]"
                )
    else:
        completed = []
        with ThreadPoolExecutor(max_workers=args.county_workers) as executor:
            futures = {executor.submit(run, job): job for job in jobs}
            for future in as_completed(futures):
                job = futures[future]
                try:
                    completed.append(future.result())
                except Exception as exc:
                    failed += 1
                    console.print(
                        f"[red]❌ 县级裁剪失败：{city_name}/"
                        f"{job['county_name']}，{exc}[/red]"
                    )

    for job, status in completed:
        if status == "created":
            created += 1
            console.print(
                f"[green]✅ [县级 {job['county_index']}/"
                f"{job['county_total']}] 成果：{job['output_path']}[/green]"
            )
        elif status == "skipped":
            skipped += 1
            console.print(
                f"[yellow]⏭️ [县级 {job['county_index']}/"
                f"{job['county_total']}] 已存在："
                f"{job['output_path']}[/yellow]"
            )
        else:
            no_coverage += 1

    return created, skipped, no_coverage, failed


def process_group_pipeline(
    job: dict,
    total_groups: int,
    city_root: Path,
    country_root: Path,
    counties,
    args: argparse.Namespace,
    pipeline_started: float,
) -> dict[str, int]:
    """在一个工作线程中执行一个分组的完整流水线。"""
    group_index = job["group_index"]
    city_name = job["city_name"]
    city_code = job["city_code"]
    date = job["date"]
    satellite = job["satellite"]
    raw_paths = job["raw_paths"]
    group_started = time.monotonic()
    label = f"[分组 {group_index}/{total_groups}]"

    console.print(
        f"\n[bold cyan]========== {label} {city_name} "
        f"{date} {satellite} ==========[/bold cyan]"
    )
    paths, duplicate_count = select_latest_products(raw_paths)
    console.print(
        f"[cyan]{label}[1/4 有效性检查] "
        f"开始读取 {len(paths)} 个源 TIFF[/cyan]"
    )
    (
        paths,
        processing_geometry,
        coverage,
        missing_km2,
    ) = calculate_coverage(
        paths,
        job["projected_boundary"],
        job["target_crs"],
    )
    output_path = (
        city_root
        / city_name
        / streamed_output_name(city_code, date, satellite, paths)
    )
    console.print(
        f"[cyan]{label} 源 TIFF {len(raw_paths)}，有效源 {len(paths)}，"
        f"重复 {duplicate_count}，市域覆盖率 {coverage:.4f}%，"
        f"缺失约 {missing_km2:.2f} km²[/cyan]"
    )
    if coverage < args.coverage_threshold:
        console.print(
            f"[yellow]{label} 覆盖率低于 "
            f"{args.coverage_threshold:.4f}%，仍按现有数据生成[/yellow]"
        )

    console.print(
        f"[cyan]{label}[2/4 市级合并] 开始写入：{output_path}[/cyan]"
    )
    city_status = merge_one_group(
        paths,
        output_path,
        job["projected_boundary"],
        processing_geometry,
        job["target_crs"],
        args.resolution,
        args.overwrite,
        coverage,
    )
    city_jpeg = create_true_color_jpeg(output_path)
    console.print(f"[green]{label} ✅ 市级 TIF：{output_path}[/green]")
    console.print(f"[green]{label} ✅ 市级 JPEG：{city_jpeg}[/green]")

    console.print(f"[cyan]{label}[3/4 县级裁剪] 开始[/cyan]")
    source_match = SOURCE_SENSING_RE.search(paths[0].name)
    counts = clip_merged_group(
        output_path,
        city_name,
        country_root,
        counties,
        {
            "satellite": satellite,
            "date": date,
            "scene": (
                source_match.group("scene") if source_match else "T000000"
            ),
            "city_code": city_code,
        },
        args,
    )
    console.print(
        f"[cyan]{label}[4/4 完成检查] 市级和县级成果已落盘[/cyan]"
    )
    console.print(
        f"[bold green]{label} 完成：{city_name} {date} {satellite} | "
        f"县级新建 {counts[0]}、跳过 {counts[1]}、"
        f"无覆盖 {counts[2]}、失败 {counts[3]} | "
        f"本组用时 {elapsed_text(group_started)} | "
        f"总用时 {elapsed_text(pipeline_started)}[/bold green]"
    )
    return {
        "city_created": int(city_status == "created"),
        "city_skipped": int(city_status != "created"),
        "county_created": counts[0],
        "county_skipped": counts[1],
        "county_no_coverage": counts[2],
        "county_failed": counts[3],
    }


def validate_args(args: argparse.Namespace) -> None:
    if not args.input_root.is_dir():
        raise FileNotFoundError(f"sentinel_data 不存在：{args.input_root}")
    for boundary in (args.city_layer, args.county_layer):
        if not boundary.is_file():
            raise FileNotFoundError(f"边界文件不存在：{boundary}")
    if args.resolution <= 0:
        raise ValueError("--resolution 必须大于 0")
    if args.county_workers < 1:
        raise ValueError("--county-workers 必须大于或等于 1")
    if args.group_workers < 1:
        raise ValueError("--group-workers 必须大于或等于 1")
    if not 0 < args.coverage_threshold <= 100:
        raise ValueError("--coverage-threshold 必须在 0 到 100 之间")


def main() -> int:
    args = parse_args()
    validate_args(args)

    input_root = args.input_root
    city_root = args.city_root
    country_root = args.country_root
    wanted_cities = {
        normalize_city_filter(value) for value in (args.city or [])
    }
    wanted_dates = {
        str(value).replace("-", "") for value in (args.date or [])
    }
    invalid_dates = sorted(
        value for value in wanted_dates if not re.fullmatch(r"\d{8}", value)
    )
    if invalid_dates:
        raise ValueError(f"日期必须是 YYYYMMDD：{invalid_dates[0]}")
    wanted_satellites = {
        str(value).upper() for value in (args.satellite or [])
    }

    console.print(f"[bold]sentinel_data：{input_root}[/bold]")
    console.print(f"[bold]city_data：{city_root}[/bold]")
    console.print(f"[bold]country_data：{country_root}[/bold]")
    console.print("[bold cyan]处理模式：发现一组，立即合并并裁剪[/bold cyan]")

    cities = load_city_boundaries(args.city_layer, args.city_name_field)
    if args.city_code_field not in cities.columns:
        raise ValueError(f"市界缺少字段：{args.city_code_field}")
    counties = load_counties(
        args.county_layer,
        args.county_name_field,
        args.county_code_field,
    )

    city_dirs = sorted(path for path in input_root.iterdir() if path.is_dir())
    if wanted_cities:
        city_dirs = [
            path
            for path in city_dirs
            if normalize_city_filter(path.name) in wanted_cities
        ]

    # 这里只读取目录和文件名以得到准确进度，不读取栅格像元，也不计算覆盖率。
    groups_by_city: dict[Path, dict[tuple[str, str], list[Path]]] = {}
    console.print(
        f"\n[bold cyan][快速统计] 开始：城市目录 {len(city_dirs)} 个；"
        "只读取文件名，不读取影像像元[/bold cyan]"
    )
    for city_index, city_dir in enumerate(city_dirs, 1):
        groups = group_city_files(city_dir)
        if wanted_dates:
            groups = {
                key: paths
                for key, paths in groups.items()
                if key[0] in wanted_dates
            }
        if wanted_satellites:
            groups = {
                key: paths
                for key, paths in groups.items()
                if key[1] in wanted_satellites
            }
        groups_by_city[city_dir] = groups
        console.print(
            f"[cyan][快速统计 城市 {city_index}/{len(city_dirs)}] "
            f"{city_dir.name}：发现 {len(groups)} 个分组[/cyan]"
        )
    total_groups = sum(len(groups) for groups in groups_by_city.values())
    if args.limit_groups:
        total_groups = min(total_groups, args.limit_groups)
    console.print(
        f"[bold green][快速统计] 完成：城市目录 {len(city_dirs)} 个，"
        f"待处理分组 {total_groups} 个；现在开始逐组落盘[/bold green]"
    )

    started_at = time.monotonic()
    city_created = city_skipped = failed = 0
    county_created = county_skipped = county_no_coverage = county_failed = 0
    jobs: list[dict] = []
    for city_index, city_dir in enumerate(city_dirs, 1):
        console.print(
            f"\n[bold magenta]########## 城市 {city_index}/"
            f"{len(city_dirs)}：{city_dir.name} ##########[/bold magenta]"
        )
        city_geometry = find_city_geometry(cities, city_dir.name)
        city_name = find_boundary_value(
            cities,
            city_dir.name,
            args.city_name_field,
        )
        city_code = find_boundary_value(
            cities,
            city_dir.name,
            args.city_code_field,
        )
        if city_geometry is None or city_name is None or city_code is None:
            failed += 1
            console.print(
                f"[red]❌ 市界中找不到城市或代码：{city_dir.name}[/red]"
            )
            continue

        try:
            target_crs, projected_boundary = projected_city_geometry(
                cities,
                city_geometry,
            )
        except Exception as exc:
            failed += 1
            console.print(f"[red]❌ {city_name} 投影转换失败：{exc}[/red]")
            continue

        groups = groups_by_city[city_dir]
        for (date, satellite), raw_paths in sorted(groups.items()):
            if args.limit_groups and len(jobs) >= args.limit_groups:
                break
            jobs.append(
                {
                    "group_index": len(jobs) + 1,
                    "city_name": city_name,
                    "city_code": city_code,
                    "date": date,
                    "satellite": satellite,
                    "raw_paths": raw_paths,
                    "projected_boundary": projected_boundary,
                    "target_crs": target_crs,
                }
            )
        if args.limit_groups and len(jobs) >= args.limit_groups:
            break

    total_groups = len(jobs)
    console.print(
        f"\n[bold cyan]开始并行流水线：分组 {total_groups} 个，"
        f"工作线程 {min(args.group_workers, max(1, total_groups))} 个，"
        f"每组县级线程 {args.county_workers} 个[/bold cyan]"
    )
    completed_groups = 0

    def record_result(job: dict, result: dict[str, int] | None, error) -> None:
        nonlocal city_created, city_skipped, failed, completed_groups
        nonlocal county_created, county_skipped
        nonlocal county_no_coverage, county_failed
        completed_groups += 1
        if error is not None:
            failed += 1
            console.print(
                f"[red]❌ [分组 {job['group_index']}/{total_groups}] "
                f"{job['city_name']} {job['date']} {job['satellite']} "
                f"流水线失败：{type(error).__name__}: {error}[/red]"
            )
        else:
            city_created += result["city_created"]
            city_skipped += result["city_skipped"]
            county_created += result["county_created"]
            county_skipped += result["county_skipped"]
            county_no_coverage += result["county_no_coverage"]
            county_failed += result["county_failed"]
        console.print(
            f"[bold cyan]总进度：完成 {completed_groups}/{total_groups}，"
            f"{completed_groups / total_groups * 100:.2f}%[/bold cyan]"
        )

    if jobs:
        if args.group_workers == 1:
            for job in jobs:
                try:
                    result = process_group_pipeline(
                        job,
                        total_groups,
                        city_root,
                        country_root,
                        counties,
                        args,
                        started_at,
                    )
                    record_result(job, result, None)
                except Exception as exc:
                    record_result(job, None, exc)
        else:
            with ThreadPoolExecutor(
                max_workers=min(args.group_workers, total_groups)
            ) as executor:
                futures = {
                    executor.submit(
                        process_group_pipeline,
                        job,
                        total_groups,
                        city_root,
                        country_root,
                        counties,
                        args,
                        started_at,
                    ): job
                    for job in jobs
                }
                for future in as_completed(futures):
                    job = futures[future]
                    try:
                        record_result(job, future.result(), None)
                    except Exception as exc:
                        record_result(job, None, exc)

    console.print("\n[bold cyan]========== 最终汇总 ==========[/bold cyan]")
    console.print(
        f"分组 {total_groups}；市级新建 {city_created}，"
        f"市级跳过 {city_skipped}，分组失败 {failed}"
    )
    console.print(
        f"县级新建 {county_created}，跳过 {county_skipped}，"
        f"无覆盖 {county_no_coverage}，失败 {county_failed}"
    )
    console.print(f"总用时：{elapsed_text(started_at)}")
    console.print(f"city_data：{city_root}")
    console.print(f"country_data：{country_root}")
    return 1 if failed or county_failed else 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        console.print("\n[yellow]⚠️ 用户中断。[/yellow]")
        raise SystemExit(130)
    except Exception as exc:
        console.print(f"\n[red]处理失败：{type(exc).__name__}: {exc}[/red]")
        raise SystemExit(1)
