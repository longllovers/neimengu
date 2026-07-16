#!/usr/bin/env python3
"""把市级 Sentinel 合成影像按县界分块裁剪。"""

from __future__ import annotations

import argparse
import re
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import geopandas as gpd
import rasterio
from rasterio.errors import WindowError
from rasterio.features import geometry_mask, geometry_window
from rasterio.windows import Window
from rich.console import Console
from shapely.geometry import mapping


console = Console()

DEFAULT_INPUT_ROOT = Path("./merge_data")
DEFAULT_OUTPUT_ROOT = Path("./county_data")
DEFAULT_COUNTY_LAYER = Path("./00县边界/15_县边界.shp")

MERGED_TIF_RE = re.compile(
    r"^(?P<satellite>S2[A-Z])_MSIL[12][AC]_"
    r"(?P<date>\d{8})(?P<scene>T\d{6})_"
    r"N\d{4}_R\d{3}_MERGED_(?P<city_code>\d{4})\.tif$",
    re.IGNORECASE,
)


def normalize_name(value: object) -> str:
    return str(value).strip()


def safe_filename(value: object) -> str:
    text = re.sub(r'[<>:"/\\|?*]', "_", str(value).strip()).strip(" .")
    return text or "UNKNOWN"


def parse_merged_filename(path: Path) -> dict[str, str] | None:
    match = MERGED_TIF_RE.match(path.name)
    return match.groupdict() if match else None


def load_counties(
    path: Path,
    name_field: str,
    code_field: str,
) -> gpd.GeoDataFrame:
    if not path.exists():
        raise FileNotFoundError(f"县边界不存在：{path}")
    counties = gpd.read_file(path)
    if counties.empty:
        raise ValueError(f"县边界为空：{path}")
    missing = [field for field in (name_field, code_field) if field not in counties.columns]
    if missing:
        raise ValueError(f"县边界缺少字段：{', '.join(missing)}")
    if counties.crs is None:
        raise ValueError(f"县边界没有坐标系：{path}")

    counties = counties[counties.geometry.notna() & ~counties.geometry.is_empty].copy()
    counties["_county_name"] = counties[name_field].map(normalize_name)
    counties["_full_code"] = counties[code_field].astype(str).str.strip()
    counties["_county_code"] = counties["_full_code"].str[:6]
    counties["_city_code"] = counties["_county_code"].str[:4]
    invalid = ~counties["_county_code"].str.fullmatch(r"\d{6}")
    if invalid.any():
        bad_values = ", ".join(counties.loc[invalid, "_full_code"].head(5))
        raise ValueError(f"存在无法提取前 6 位县代码的数据：{bad_values}")
    return counties.reset_index(drop=True)


def county_output_name(metadata: dict[str, str], county_code: str, resolution: str) -> str:
    return (
        f"CQDOM{county_code}_{metadata['date']}_{metadata['scene']}_"
        f"{metadata['satellite'].upper()}_{safe_filename(resolution)}.tif"
    )


def clip_one_county(
    source_path: Path,
    output_path: Path,
    county_geometry,
    county_name: str,
    county_code: str,
    metadata: dict[str, str],
    nodata: int,
    overwrite: bool,
    all_touched: bool,
) -> str:
    if output_path.exists() and not overwrite:
        return "skipped"

    output_path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = output_path.with_name(f".{output_path.stem}.tmp.tif")
    if temporary_path.exists():
        temporary_path.unlink()

    try:
        with rasterio.open(source_path) as source:
            if source.crs is None:
                raise ValueError(f"源影像没有坐标系：{source_path}")

            # main() 已将县界转换到源影像坐标系。
            geometry_in_raster_crs = county_geometry
            try:
                crop_window = geometry_window(
                    source,
                    [mapping(geometry_in_raster_crs)],
                ).round_offsets().round_lengths()
                crop_window = crop_window.intersection(
                    Window(0, 0, source.width, source.height)
                )
            except WindowError as exc:
                raise ValueError("县界与市级合成影像不相交") from exc

            profile = source.profile.copy()
            profile.update(
                driver="GTiff",
                width=int(crop_window.width),
                height=int(crop_window.height),
                transform=source.window_transform(crop_window),
                nodata=nodata,
                compress="deflate",
                predictor=2,
                tiled=True,
                blockxsize=512,
                blockysize=512,
                BIGTIFF="IF_SAFER",
            )

            with rasterio.Env(GDAL_TIFF_INTERNAL_MASK="YES"), rasterio.open(
                temporary_path, "w", **profile
            ) as target:
                for _, target_window in target.block_windows(1):
                    source_window = Window(
                        crop_window.col_off + target_window.col_off,
                        crop_window.row_off + target_window.row_off,
                        target_window.width,
                        target_window.height,
                    )
                    data = source.read(window=source_window)
                    inside = geometry_mask(
                        [mapping(geometry_in_raster_crs)],
                        out_shape=(target_window.height, target_window.width),
                        transform=target.window_transform(target_window),
                        invert=True,
                        all_touched=all_touched,
                    )
                    data[:, ~inside] = nodata
                    target.write(data, window=target_window)
                    valid = inside & (data != nodata).any(axis=0)
                    target.write_mask(valid.astype("uint8") * 255, window=target_window)

                target.descriptions = source.descriptions
                target.update_tags(
                    source_file=source_path.name,
                    county_name=county_name,
                    county_code=county_code,
                    acquisition_date=metadata["date"],
                    scene_id=metadata["scene"],
                    satellite=metadata["satellite"].upper(),
                )

        temporary_path.replace(output_path)
        return "created"
    finally:
        if temporary_path.exists():
            try:
                temporary_path.unlink()
            except OSError as exc:
                console.print(f"[yellow]⚠️ 临时文件删除失败：{temporary_path}，{exc}[/yellow]")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="将 merge_data 中的市级合成 TIFF 按县边界裁剪。"
    )
    parser.add_argument("--input-root", type=Path, default=DEFAULT_INPUT_ROOT, help="市级合成 TIFF 根目录")
    parser.add_argument("--output-root", type=Path, default=DEFAULT_OUTPUT_ROOT, help="县级 TIFF 输出根目录")
    parser.add_argument("--county-layer", type=Path, default=DEFAULT_COUNTY_LAYER, help="县边界文件")
    parser.add_argument("--county-name-field", default="area_name", help="县名称字段")
    parser.add_argument("--county-code-field", default="area_code", help="县代码字段，取前 6 位")
    parser.add_argument("--city", action="append", help="只处理指定市文件夹；可重复传入")
    parser.add_argument(
        "--input-file",
        action="append",
        type=Path,
        help="只裁剪指定的市级 TIFF；可重复传入",
    )
    parser.add_argument("--county", action="append", help="只处理指定县名或 6 位县代码；可重复传入")
    parser.add_argument("--input-pattern", default="*.tif", help="输入文件匹配模式")
    parser.add_argument("--resolution-label", default="10m", help="写入文件名的分辨率标识")
    parser.add_argument("--nodata", type=int, default=0, help="输出 NoData，默认 0")
    parser.add_argument("--all-touched", action="store_true", help="边界接触到的所有像元都保留")
    parser.add_argument("--max-workers", type=int, default=1, help="并发裁剪线程数，默认 1（顺序处理）")
    parser.add_argument("--overwrite", action="store_true", help="覆盖已有县级 TIFF")
    parser.add_argument("--dry-run", action="store_true", help="只展示计划，不执行裁剪")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.max_workers < 1:
        console.print("[red]❌ max-workers 必须大于或等于 1[/red]")
        return 2
    if not args.input_file and not args.input_root.exists():
        console.print(f"[red]❌ 输入目录不存在：{args.input_root}[/red]")
        return 1

    input_files: list[Path] = []
    if args.input_file:
        input_files = [path.expanduser().resolve() for path in args.input_file]
        missing_files = [path for path in input_files if not path.is_file()]
        if missing_files:
            console.print(f"[red]❌ 指定的市级 TIFF 不存在：{missing_files[0]}[/red]")
            return 1

    try:
        counties = load_counties(
            args.county_layer,
            args.county_name_field,
            args.county_code_field,
        )
    except Exception as exc:
        console.print(f"[red]❌ 读取县边界失败：{exc}[/red]")
        return 1

    wanted_cities = {normalize_name(value) for value in (args.city or [])}
    wanted_counties = {normalize_name(value) for value in (args.county or [])}
    if input_files:
        city_dirs = sorted({path.parent for path in input_files})
    else:
        city_dirs = sorted(path for path in args.input_root.iterdir() if path.is_dir())
    if wanted_cities:
        city_dirs = [path for path in city_dirs if path.name in wanted_cities]

    created = skipped = failed = 0
    source_count = 0
    matched_count = 0
    pending_count = 0
    jobs: list[dict] = []
    for city_dir in city_dirs:
        if input_files:
            source_files = sorted(path for path in input_files if path.parent == city_dir)
        else:
            source_files = sorted(
                path
                for path in city_dir.glob(args.input_pattern)
                if not path.name.startswith(".") and ".tmp." not in path.name.lower()
            )
        for source_path in source_files:
            metadata = parse_merged_filename(source_path)
            if metadata is None:
                console.print(f"[yellow]⚠️ 文件名格式不匹配，跳过：{source_path.name}[/yellow]")
                continue
            source_count += 1

            selected = counties[counties["_city_code"] == metadata["city_code"]].copy()
            if wanted_counties:
                selected = selected[
                    selected["_county_name"].isin(wanted_counties)
                    | selected["_county_code"].isin(wanted_counties)
                ]
            if selected.empty:
                console.print(
                    f"[yellow]⚠️ 市代码 {metadata['city_code']} 没有匹配到县：{source_path.name}[/yellow]"
                )
                continue

            with rasterio.open(source_path) as source:
                selected = selected.to_crs(source.crs)

            source_existing = 0
            source_pending = 0
            for _, county in selected.iterrows():
                matched_count += 1
                county_name = county["_county_name"]
                county_code = county["_county_code"]
                output_name = county_output_name(
                    metadata,
                    county_code,
                    args.resolution_label,
                )
                output_path = (
                    args.output_root
                    / safe_filename(city_dir.name)
                    / safe_filename(county_name)
                    / output_name
                )
                if output_path.exists() and not args.overwrite:
                    skipped += 1
                    source_existing += 1
                    console.print(
                        f"[yellow]⏭️ 已存在，跳过：{city_dir.name}/{county_name}/{output_name}[/yellow]"
                    )
                    continue

                if args.dry_run:
                    console.print(f"[cyan]计划：{county_name} -> {output_path}[/cyan]")
                    source_pending += 1
                    pending_count += 1
                    continue

                source_pending += 1
                pending_count += 1
                jobs.append(
                    {
                        "source_path": source_path,
                        "output_path": output_path,
                        "county_geometry": county.geometry,
                        "county_name": county_name,
                        "county_code": county_code,
                        "metadata": metadata,
                        "city_name": city_dir.name,
                    }
                )

            console.print(
                f"[bold cyan]{city_dir.name} {metadata['date']}：匹配 {len(selected)} 个县，"
                f"已有 {source_existing}，待裁剪 {source_pending}[/bold cyan]"
            )

    console.print(
        f"\n[bold]扫描完成：市级 TIFF {source_count} 个，县级任务 {matched_count} 个，"
        f"已有跳过 {skipped} 个，待裁剪 {pending_count} 个[/bold]"
    )

    if args.dry_run:
        console.print("[bold cyan]仅检查模式，未执行裁剪。[/bold cyan]")
        return 0

    completed_jobs = 0

    def run_county_job(job: dict) -> str:
        return clip_one_county(
            job["source_path"],
            job["output_path"],
            job["county_geometry"],
            job["county_name"],
            job["county_code"],
            job["metadata"],
            args.nodata,
            args.overwrite,
            args.all_touched,
        )

    def record_county_result(job: dict, status: str | None, error: Exception | None) -> None:
        nonlocal created, skipped, failed, completed_jobs
        if error is None:
            if status == "created":
                created += 1
                console.print(
                    f"[green]✅ {job['city_name']}/{job['county_name']}："
                    f"{job['output_path'].name}[/green]"
                )
            else:
                skipped += 1
                console.print(f"[yellow]⏭️ 已存在，跳过：{job['output_path']}[/yellow]")
        else:
            failed += 1
            console.print(
                f"[red]❌ {job['city_name']}/{job['county_name']} 裁剪失败：{error}[/red]"
            )

        completed_jobs += 1
        percent = completed_jobs / len(jobs) * 100
        console.print(
            f"[cyan]县级裁剪进度：{completed_jobs}/{len(jobs)}，{percent:.2f}%[/cyan]"
        )

    if jobs:
        if args.max_workers == 1:
            try:
                for job in jobs:
                    try:
                        record_county_result(job, run_county_job(job), None)
                    except Exception as exc:
                        record_county_result(job, None, exc)
            except KeyboardInterrupt:
                console.print("\n[yellow]⚠️ 已停止县级裁剪。[/yellow]")
                return 130
        else:
            with ThreadPoolExecutor(max_workers=args.max_workers) as executor:
                futures = {
                    executor.submit(run_county_job, job): job
                    for job in jobs
                }
                for future in as_completed(futures):
                    job = futures[future]
                    try:
                        record_county_result(job, future.result(), None)
                    except Exception as exc:
                        record_county_result(job, None, exc)

    console.print(f"\n[bold]处理结束：新建 {created}，跳过 {skipped}，失败 {failed}[/bold]")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
