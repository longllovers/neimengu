#!/usr/bin/env python3
r"""处理本地 Sentinel-2 SAFE ZIP，依次生成瓦片、市级和县级影像。

默认目录（都与 ZIP 输入目录同级）：
    E:\data             SAFE ZIP 输入
    E:\sentinel_data    按城市归类的 10m B02/B03/B04/B08 GeoTIFF
    E:\city_data        按市界合并、裁剪后的 GeoTIFF
    E:\country_data     按县界裁剪后的 GeoTIFF

原 ZIP 及同名 TXT 不会被删除。
"""

from __future__ import annotations

import argparse
import multiprocessing as mp
import os
import re
import shutil
import subprocess
import sys
import time
import uuid
from concurrent.futures import FIRST_COMPLETED, ProcessPoolExecutor, wait
from pathlib import Path

import geopandas as gpd
import rasterio
from shapely.geometry import box

from clip_county_tifs import parse_merged_filename
from extract_s2_10m_tif import convert_zip_to_tif, find_band_members
from file_write_guard import output_file_lock


BASE_DIR = Path(__file__).resolve().parent
DEFAULT_ZIP_DIR = Path(r"/media/cangling/nas_folder/原始影像/10m影像")
DEFAULT_CITY_LAYER = BASE_DIR / "00市边界" / "15_市边界.shp"
DEFAULT_COUNTY_LAYER = BASE_DIR / "00县边界" / "15_县边界.shp"
CITY_NAME_FIELD = "市名称"
PRODUCT_RE = re.compile(
    r"^(?P<satellite>S2[A-Z])_.*?_(?P<date>\d{8})T\d{6}_",
    re.IGNORECASE,
)
HEARTBEAT_SECONDS = 15


def format_size(size: int) -> str:
    value = float(size)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if value < 1024 or unit == "TiB":
            return f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} TiB"


def elapsed_text(started_at: float) -> str:
    elapsed = max(0, round(time.monotonic() - started_at))
    minutes, seconds = divmod(elapsed, 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours:d}:{minutes:02d}:{seconds:02d}"
    return f"{minutes:02d}:{seconds:02d}"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="从本地 SAFE ZIP 生成 sentinel_data、city_data 和 country_data"
    )
    parser.add_argument("--zip-dir", type=Path, default=DEFAULT_ZIP_DIR, help="SAFE ZIP 输入目录")
    parser.add_argument("--sentinel-root", type=Path, help="10m 瓦片输出根目录")
    parser.add_argument("--city-root", type=Path, help="市级影像输出根目录")
    parser.add_argument("--country-root", type=Path, help="县级影像输出根目录")
    parser.add_argument("--skipped-root", type=Path, help="跳过 ZIP 整理目录，默认为输入目录同级的“跳过zip”")
    parser.add_argument("--city-layer", type=Path, default=DEFAULT_CITY_LAYER, help="市界 Shapefile")
    parser.add_argument("--county-layer", type=Path, default=DEFAULT_COUNTY_LAYER, help="县界 Shapefile")
    parser.add_argument(
        "--only-city",
        action="append",
        default=[],
        help="只处理指定城市；可重复传入，默认处理 ZIP 覆盖的所有城市",
    )
    parser.add_argument("--max-workers", type=int, default=1, help="合并/裁剪并发数，默认 1")
    parser.add_argument(
        "--extract-workers",
        type=int,
        default=0,
        help="ZIP 抽取并发数；0 表示使用 --max-workers",
    )
    parser.add_argument("--overwrite", action="store_true", help="覆盖已有的 TIFF 和派生结果")
    parser.add_argument("--dry-run", action="store_true", help="只显示 ZIP 与城市的匹配计划")
    parser.add_argument(
        "--keep-skipped",
        action="store_true",
        help="保留不匹配市界或非 MSIL2A 的 ZIP，不移动到“跳过zip”目录",
    )
    return parser.parse_args()


def product_name(zip_path: Path) -> str:
    name = zip_path.name
    return name[: -len(".SAFE.zip")] if name.lower().endswith(".safe.zip") else zip_path.stem


def product_metadata(zip_path: Path) -> tuple[str, str]:
    match = PRODUCT_RE.match(zip_path.name)
    if match is None:
        raise ValueError(f"无法从 ZIP 文件名识别卫星和成像日期：{zip_path.name}")
    return match.group("date"), match.group("satellite").upper()


def zip_footprint(zip_path: Path):
    """不完整解压 ZIP，直接读取 B02 的坐标系和范围。"""
    member = find_band_members(zip_path)["B02"]
    virtual_path = f"/vsizip/{zip_path.as_posix()}/{member}"
    with rasterio.open(virtual_path) as source:
        if source.crs is None:
            raise ValueError(f"ZIP 内 B02 缺少坐标系：{zip_path}")
        return box(*source.bounds), source.crs


def matching_cities(zip_path: Path, cities: gpd.GeoDataFrame) -> list[str]:
    footprint, footprint_crs = zip_footprint(zip_path)
    projected = gpd.GeoSeries([footprint], crs=footprint_crs).to_crs(cities.crs).iloc[0]
    matches = cities.loc[cities.intersects(projected), CITY_NAME_FIELD]
    return sorted({str(value).strip() for value in matches if str(value).strip()})


def valid_raster(path: Path) -> bool:
    if not path.is_file() or path.stat().st_size == 0:
        return False
    try:
        with rasterio.open(path) as source:
            return source.count >= 4 and source.width > 0 and source.height > 0
    except Exception:
        return False


def install_file(source: Path, target: Path) -> None:
    """用同卷硬链接安装；不支持时回退为复制，最后原子替换。"""
    target.parent.mkdir(parents=True, exist_ok=True)
    with output_file_lock(target):
        temporary = target.with_name(f".{target.name}.{uuid.uuid4().hex}.tmp")
        try:
            try:
                os.link(source, temporary)
            except OSError:
                shutil.copy2(source, temporary)
            os.replace(temporary, target)
        finally:
            if temporary.exists():
                temporary.unlink()


def unique_destination(path: Path) -> Path:
    """目标已存在时返回不覆盖旧文件的新名称。"""
    if not path.exists():
        return path
    for index in range(1, 10000):
        candidate = path.with_name(f"{path.stem}.{index}{path.suffix}")
        if not candidate.exists():
            return candidate
    raise RuntimeError(f"无法为跳过文件生成不重名的目标：{path}")


def move_skipped_archives(
    skipped: list[tuple[Path, str]],
    zip_dir: Path,
    skipped_root: Path,
) -> tuple[int, list[str]]:
    """在主流程全部成功后，整理跳过的 ZIP 和同名 TXT。"""
    moved = 0
    failures: list[str] = []
    total = len(skipped)
    if not skipped:
        print("\n[整理跳过 ZIP] 无需移动：0/0", flush=True)
        return moved, failures

    print(
        f"\n[整理跳过 ZIP] 开始：0/{total} | 目标 {skipped_root}",
        flush=True,
    )
    for index, (zip_path, reason) in enumerate(skipped, 1):
        label = f"[整理跳过 ZIP {index}/{total}]"
        if not zip_path.exists():
            failures.append(f"{zip_path}: 源 ZIP 不存在")
            print(f"{label} 失败：源 ZIP 不存在：{zip_path}", flush=True)
            continue

        try:
            try:
                relative = zip_path.relative_to(zip_dir)
            except ValueError:
                relative = Path(zip_path.name)
            destination = unique_destination(skipped_root / relative)
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(zip_path), str(destination))

            txt_path = zip_path.with_suffix(".txt")
            txt_message = ""
            if txt_path.is_file():
                txt_destination = destination.with_suffix(".txt")
                if txt_destination.exists():
                    txt_destination = unique_destination(txt_destination)
                shutil.move(str(txt_path), str(txt_destination))
                txt_message = f"；TXT -> {txt_destination.name}"

            moved += 1
            print(
                f"{label} 已移动：{zip_path.name} -> {destination} | "
                f"原因：{reason}{txt_message}",
                flush=True,
            )
        except Exception as exc:
            failures.append(f"{zip_path}: {exc}")
            print(f"{label} 移动失败：{zip_path} - {exc}", flush=True)

    print(
        f"[整理跳过 ZIP] 完成：{total}/{total} | "
        f"成功 {moved} | 失败 {len(failures)}",
        flush=True,
    )
    return moved, failures


def extract_for_cities(
    zip_path: Path,
    city_names: list[str],
    sentinel_root: Path,
    overwrite: bool,
    progress_label: str,
    verbose: bool = True,
) -> list[Path]:
    tif_name = f"{product_name(zip_path)}.tif"
    targets = [sentinel_root / city_name / tif_name for city_name in city_names]
    reusable = next((path for path in targets if valid_raster(path)), None)

    if overwrite or reusable is None:
        staging_dir = sentinel_root / ".extracting"
        staging_dir.mkdir(parents=True, exist_ok=True)
        staging_path = staging_dir / f"{product_name(zip_path)}.{uuid.uuid4().hex}.tif"
        started_at = time.monotonic()
        if verbose:
            print(
                f"{progress_label} 开始抽取：{zip_path.name} | "
                f"ZIP {format_size(zip_path.stat().st_size)} | 目标城市 {len(city_names)} 个",
                flush=True,
            )
        try:
            convert_zip_to_tif(zip_path, staging_path, overwrite=True)
            if not valid_raster(staging_path):
                raise RuntimeError(f"抽取结果无效：{staging_path}")
            if verbose:
                print(
                    f"{progress_label} TIFF 抽取完成 | "
                    f"已用时 {elapsed_text(started_at)} | "
                    f"大小 {format_size(staging_path.stat().st_size)} | "
                    f"开始安装到 {len(targets)} 个城市目录",
                    flush=True,
                )
            for target in targets:
                install_file(staging_path, target)
            reusable = targets[0]
        finally:
            if staging_path.exists():
                staging_path.unlink()
            try:
                staging_dir.rmdir()
            except OSError:
                pass
    else:
        if verbose:
            print(
                f"{progress_label} 发现已有有效 TIFF，复用：{reusable}",
                flush=True,
            )
        for target in targets:
            if not valid_raster(target):
                if verbose:
                    print(f"{progress_label} 补齐城市目录：{target}", flush=True)
                install_file(reusable, target)

    return targets


def run_command(command: list[str], title: str) -> None:
    print(f"\n========== {title} ==========", flush=True)
    print(" ".join(command), flush=True)
    result = subprocess.run(command, cwd=BASE_DIR)
    if result.returncode != 0:
        raise RuntimeError(f"{title}失败，退出码 {result.returncode}")


def collect_merged_outputs(
    city_root: Path,
    cities: set[str],
    product_groups: set[tuple[str, str]],
) -> list[Path]:
    outputs: list[Path] = []
    for city_name in sorted(cities):
        city_dir = city_root / city_name
        if not city_dir.is_dir():
            continue
        for path in sorted(city_dir.glob("*.tif")):
            metadata = parse_merged_filename(path)
            if metadata is None:
                continue
            if (metadata["date"], metadata["satellite"].upper()) in product_groups:
                outputs.append(path)
    return outputs


def main() -> int:
    # 即使通过 tee/nohup/调度器运行，也尽量立即刷新每一行日志。
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(line_buffering=True, write_through=True)
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(line_buffering=True, write_through=True)

    args = parse_args()
    if args.max_workers < 1:
        raise ValueError("--max-workers 必须大于或等于 1")

    zip_dir = args.zip_dir.expanduser().resolve()
    if not zip_dir.is_dir():
        raise FileNotFoundError(f"ZIP 输入目录不存在：{zip_dir}")

    output_parent = zip_dir.parent
    sentinel_root = (args.sentinel_root or output_parent / "sentinel_data").expanduser().resolve()
    city_root = (args.city_root or output_parent / "city_data").expanduser().resolve()
    country_root = (args.country_root or output_parent / "country_data").expanduser().resolve()
    skipped_root = (args.skipped_root or output_parent / "跳过zip").expanduser().resolve()
    city_layer = args.city_layer.expanduser().resolve()
    county_layer = args.county_layer.expanduser().resolve()

    for boundary in (city_layer, county_layer):
        if not boundary.is_file():
            raise FileNotFoundError(f"边界文件不存在：{boundary}")

    print(f"[初始化] 正在读取市界：{city_layer}", flush=True)
    cities = gpd.read_file(city_layer)
    if CITY_NAME_FIELD not in cities.columns or cities.crs is None:
        raise ValueError(f"市界必须包含 {CITY_NAME_FIELD} 字段和有效坐标系")
    only_cities = {name.strip() for name in args.only_city if name.strip()}
    if only_cities:
        known_cities = {
            str(value).strip() for value in cities[CITY_NAME_FIELD] if str(value).strip()
        }
        unknown_cities = only_cities - known_cities
        if unknown_cities:
            raise ValueError(f"市界中不存在指定城市：{', '.join(sorted(unknown_cities))}")

    scan_started = time.monotonic()
    print(f"[扫描] 正在递归扫描 ZIP：{zip_dir}", flush=True)
    zip_paths = sorted(
        path
        for path in zip_dir.rglob("*.zip")
        if path.is_file() and path != skipped_root and skipped_root not in path.parents
    )
    print(
        f"[扫描] 完成：找到 {len(zip_paths)} 个 ZIP | "
        f"已用时 {elapsed_text(scan_started)}",
        flush=True,
    )
    if not zip_paths:
        print(f"没有找到 ZIP：{zip_dir}")
        return 1

    print(f"ZIP 输入：{zip_dir}")
    print(f"sentinel_data：{sentinel_root}")
    print(f"city_data：{city_root}")
    print(f"country_data：{country_root}")
    print(f"跳过 ZIP 最终目录：{skipped_root}")
    print(f"共找到 {len(zip_paths)} 个 ZIP。\n")

    plans: list[tuple[Path, list[str], tuple[str, str]]] = []
    skipped_archives: list[tuple[Path, str]] = []
    preparation_failures: list[str] = []
    no_city_count = 0
    unsupported_count = 0
    preparation_started = time.monotonic()
    print(f"\n[空间识别] 开始：0/{len(zip_paths)}", flush=True)
    for index, zip_path in enumerate(zip_paths, 1):
        item_started = time.monotonic()
        progress_label = f"[空间识别 {index}/{len(zip_paths)}]"
        print(
            f"{progress_label} 正在读取：{zip_path.name} | "
            f"{format_size(zip_path.stat().st_size)}",
            flush=True,
        )
        try:
            if "_MSIL2A_" not in zip_path.name.upper():
                unsupported_count += 1
                reason = "非 MSIL2A 产品"
                skipped_archives.append((zip_path, reason))
                print(
                    f"{progress_label} 跳过：{reason} | "
                    f"已用时 {elapsed_text(item_started)}",
                    flush=True,
                )
                continue
            group = product_metadata(zip_path)
            city_names = matching_cities(zip_path, cities)
            if only_cities:
                city_names = [name for name in city_names if name in only_cities]
            if city_names:
                plans.append((zip_path, city_names, group))
                print(
                    f"{progress_label} 识别完成：{group[0]} {group[1]} -> "
                    f"{', '.join(city_names)} | 已用时 {elapsed_text(item_started)}",
                    flush=True,
                )
            else:
                no_city_count += 1
                skipped_archives.append((zip_path, "不与市界相交"))
                print(
                    f"{progress_label} 跳过：不与市界相交 | "
                    f"已用时 {elapsed_text(item_started)}",
                    flush=True,
                )
        except Exception as exc:
            preparation_failures.append(f"{zip_path}: {exc}")
            print(
                f"{progress_label} 识别失败：{exc} | "
                f"已用时 {elapsed_text(item_started)}",
                flush=True,
            )

    print(
        f"[空间识别] 完成：{len(zip_paths)}/{len(zip_paths)} | "
        f"参与处理 {len(plans)} | 非 MSIL2A {unsupported_count} | "
        f"无市界 {no_city_count} | 待整理 {len(skipped_archives)} | "
        f"失败 {len(preparation_failures)} | "
        f"已用时 {elapsed_text(preparation_started)}",
        flush=True,
    )

    if preparation_failures:
        print("\n存在无法识别的 ZIP，为避免生成不完整合并结果，已停止。")
        return 1
    if not plans:
        if args.dry_run:
            skipped_action = "保留" if args.keep_skipped else "移动"
            print(
                f"\ndry-run 完成：没有可处理 ZIP；"
                f"计划{skipped_action} {len(skipped_archives)} 个跳过 ZIP；"
                "未写入任何文件。"
            )
            return 0
        if args.keep_skipped:
            moved, move_failures = 0, []
            print(f"没有可抽取 ZIP；已保留跳过 ZIP {len(skipped_archives)} 个。")
        else:
            moved, move_failures = move_skipped_archives(
                skipped_archives, zip_dir, skipped_root
            )
            print(f"没有可抽取 ZIP；已整理跳过 ZIP {moved} 个。")
        return 1 if move_failures else 0
    if args.dry_run:
        skipped_action = "保留" if args.keep_skipped else "移动"
        print(
            f"\ndry-run 完成：计划处理 {len(plans)} 个 ZIP；"
            f"计划{skipped_action} {len(skipped_archives)} 个跳过 ZIP；"
            "未写入任何文件。"
        )
        return 0

    touched_cities: set[str] = set()
    product_groups: set[tuple[str, str]] = set()
    extraction_failures: list[str] = []
    extraction_started = time.monotonic()
    extract_workers = max(1, args.extract_workers or args.max_workers)
    print(
        f"\n[ZIP 抽取] 开始：0/{len(plans)} | 并发 0/{extract_workers}",
        flush=True,
    )
    with ProcessPoolExecutor(
        max_workers=extract_workers,
        mp_context=mp.get_context("spawn"),
    ) as extraction_pool:
        extraction_futures = {}
        for index, (zip_path, city_names, group) in enumerate(plans, 1):
            progress_label = f"[ZIP 抽取 {index}/{len(plans)}]"
            future = extraction_pool.submit(
                extract_for_cities,
                zip_path,
                city_names,
                sentinel_root,
                args.overwrite,
                progress_label,
                False,
            )
            extraction_futures[future] = (index, zip_path, city_names, group)

        completed_count = 0
        pending_futures = set(extraction_futures)
        while pending_futures:
            done, pending_futures = wait(
                pending_futures,
                timeout=HEARTBEAT_SECONDS,
                return_when=FIRST_COMPLETED,
            )
            if not done:
                running = min(len(pending_futures), extract_workers)
                queued = max(0, len(pending_futures) - running)
                print(
                    f"[ZIP 抽取] 运行并发 {running}/{extract_workers} | "
                    f"已完成 {completed_count}/{len(plans)} | 排队 {queued}",
                    flush=True,
                )
                continue

            for future in done:
                index, zip_path, city_names, group = extraction_futures[future]
                progress_label = f"[ZIP 抽取 {index}/{len(plans)}]"
                completed_count += 1
                try:
                    outputs = future.result()
                    touched_cities.update(city_names)
                    product_groups.add(group)
                    print(
                        f"{progress_label} 完成：{zip_path.name} -> "
                        f"{len(outputs)} 个城市目录 | 总进度 "
                        f"{completed_count}/{len(plans)}",
                        flush=True,
                    )
                except Exception as exc:
                    extraction_failures.append(f"{zip_path}: {exc}")
                    print(
                        f"{progress_label} 失败：{zip_path} - {exc} | 总进度 "
                        f"{completed_count}/{len(plans)}",
                        flush=True,
                    )

    print(
        f"[ZIP 抽取] 完成：{len(plans)}/{len(plans)} | "
        f"成功 {len(plans) - len(extraction_failures)} | "
        f"失败 {len(extraction_failures)} | "
        f"已用时 {elapsed_text(extraction_started)}",
        flush=True,
    )

    if extraction_failures:
        print("\n存在抽取失败，为避免不完整市级影像，未继续合并和裁剪。")
        return 1

    python = sys.executable
    merge_command = [
        python,
        "-u",
        str(BASE_DIR / "merge_city_tifs.py"),
        "--input-root",
        str(sentinel_root),
        "--output-root",
        str(city_root),
        "--city-layer",
        str(city_layer),
        "--max-workers",
        str(args.max_workers),
    ]
    for city_name in sorted(touched_cities):
        merge_command.extend(("--city", city_name))
    for date in sorted({date for date, _ in product_groups}):
        merge_command.extend(("--date", date))
    for satellite in sorted({satellite for _, satellite in product_groups}):
        merge_command.extend(("--satellite", satellite))
    if args.overwrite:
        merge_command.append("--overwrite")
    run_command(merge_command, "市级合并")

    merged_outputs = collect_merged_outputs(city_root, touched_cities, product_groups)
    if not merged_outputs:
        raise RuntimeError("市级合并后没有找到本批次的 TIFF")

    clip_command = [
        python,
        "-u",
        str(BASE_DIR / "clip_county_tifs.py"),
        "--output-root",
        str(country_root),
        "--county-layer",
        str(county_layer),
        "--max-workers",
        str(args.max_workers),
    ]
    for merged_path in merged_outputs:
        clip_command.extend(("--input-file", str(merged_path)))
    if args.overwrite:
        clip_command.append("--overwrite")
    run_command(clip_command, "县级裁剪")

    if args.keep_skipped:
        moved_skipped, move_failures = 0, []
        print(f"\n[整理跳过 ZIP] 已禁用；原位置保留 {len(skipped_archives)} 个。")
    else:
        moved_skipped, move_failures = move_skipped_archives(
            skipped_archives, zip_dir, skipped_root
        )

    print("\n全部处理完成。")
    print(
        f"ZIP：{len(zip_paths)} 个；参与处理：{len(plans)} 个；"
        f"城市：{len(touched_cities)} 个；跳过：{len(skipped_archives)} 个；"
        f"已移动：{moved_skipped} 个；移动失败：{len(move_failures)} 个"
    )
    print(f"sentinel_data：{sentinel_root}")
    print(f"city_data：{city_root}")
    print(f"country_data：{country_root}")
    print(f"跳过zip：{skipped_root}")
    return 1 if move_failures else 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("\n已停止。")
        raise SystemExit(130)
    except Exception as exc:
        print(f"\n处理失败：{exc}")
        raise SystemExit(1)
