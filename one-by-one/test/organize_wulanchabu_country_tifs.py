#!/usr/bin/env python3
"""整理 E:\\乌兰察布市影像 中的县级 TIFF，并生成 1/2/3 波段 JPEG。

输出结构：E:\\country_data\\市名\\县名\\原 TIFF 文件名
处理规则：复制 TIFF、设置 NoData=0、生成同名 JPEG。
JPEG 通道：红=波段1、绿=波段2、蓝=波段3，按 0 到各波段最大值拉伸。
"""

from __future__ import annotations

import argparse
import re
import shutil
import sys
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path


BASE_DIR = Path(__file__).resolve().parent
DEFAULT_INPUT_ROOT = Path(r"E:\乌兰察布市影像")
DEFAULT_OUTPUT_ROOT = Path(r"E:\country_data")
DEFAULT_COUNTY_LAYER = BASE_DIR / "00县边界" / "15_县边界.shp"
DEFAULT_CITY_LAYER = BASE_DIR / "00市边界" / "15_市边界.shp"
THUMBNAIL_MAX_SIZE = 1200
INPUT_RE = re.compile(r"^(?P<county_code>\d{6})_.+\.tif$", re.IGNORECASE)
LOG_LOCK = threading.Lock()


@dataclass(frozen=True)
class Region:
    city_name: str
    county_name: str


@dataclass(frozen=True)
class Job:
    source: Path
    destination: Path
    region: Region


def log(message: str, level: str = "INFO") -> None:
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with LOG_LOCK:
        print(f"[{timestamp}] [{level}] {message}", flush=True)


def elapsed_text(started_at: float) -> str:
    seconds = max(0, round(time.monotonic() - started_at))
    minutes, seconds = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours}小时{minutes}分{seconds}秒"
    if minutes:
        return f"{minutes}分{seconds}秒"
    return f"{seconds}秒"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="整理 E:\\乌兰察布市影像 到 country_data，并生成 1/2/3 波段 JPEG。"
    )
    parser.add_argument("--input-root", type=Path, default=DEFAULT_INPUT_ROOT)
    parser.add_argument("--output-root", type=Path, default=DEFAULT_OUTPUT_ROOT)
    parser.add_argument("--county-layer", type=Path, default=DEFAULT_COUNTY_LAYER)
    parser.add_argument("--city-layer", type=Path, default=DEFAULT_CITY_LAYER)
    parser.add_argument("--county-code-field", default="area_code")
    parser.add_argument("--county-name-field", default="area_name")
    parser.add_argument("--city-code-field", default="市代码")
    parser.add_argument("--city-name-field", default="市名称")
    parser.add_argument("--max-workers", type=int, default=4)
    parser.add_argument("--overwrite", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    return parser.parse_args()


def require_dependencies():
    try:
        import geopandas as gpd
        import numpy as np
        import rasterio
        from PIL import Image
        from rasterio.enums import Resampling
    except ImportError as exc:
        raise RuntimeError(
            "缺少依赖，请安装 geopandas、rasterio、numpy 和 Pillow"
        ) from exc
    return gpd, np, rasterio, Image, Resampling


def safe_name(value: object) -> str:
    text = str(value).strip()
    return re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", text).rstrip(". ")


def numeric_code(value: object, length: int) -> str:
    text = str(value).strip()
    if re.fullmatch(r"\d+\.0+", text):
        text = text.split(".", 1)[0]
    digits = re.sub(r"\D", "", text)
    return digits[:length] if len(digits) >= length else ""


def load_regions(args: argparse.Namespace, gpd) -> dict[str, Region]:
    counties = gpd.read_file(args.county_layer, ignore_geometry=True)
    cities = gpd.read_file(args.city_layer, ignore_geometry=True)
    for frame, fields, label in (
        (counties, (args.county_code_field, args.county_name_field), "县界"),
        (cities, (args.city_code_field, args.city_name_field), "市界"),
    ):
        missing = [field for field in fields if field not in frame.columns]
        if missing:
            raise ValueError(f"{label}文件缺少字段：{', '.join(missing)}")

    city_names: dict[str, str] = {}
    for _, row in cities.iterrows():
        code = numeric_code(row[args.city_code_field], 4)
        name = str(row[args.city_name_field]).strip()
        if code and name:
            city_names[code] = name

    regions: dict[str, Region] = {}
    for _, row in counties.iterrows():
        county_code = numeric_code(row[args.county_code_field], 6)
        county_name = str(row[args.county_name_field]).strip()
        city_name = city_names.get(county_code[:4])
        if county_code and county_name and city_name:
            regions[county_code] = Region(city_name, county_name)
    if not regions:
        raise ValueError("行政区文件中没有可用的市县代码")
    return regions


def scan_jobs(
    input_root: Path,
    output_root: Path,
    regions: dict[str, Region],
) -> tuple[list[Job], list[Path], list[tuple[Path, str]]]:
    files = sorted(input_root.rglob("*.tif"))
    jobs: list[Job] = []
    invalid: list[Path] = []
    unknown: list[tuple[Path, str]] = []
    output_resolved = output_root.resolve()
    log(f"开始扫描：发现 TIFF {len(files)} 个")

    for index, source in enumerate(files, 1):
        try:
            source.resolve().relative_to(output_resolved)
            continue
        except ValueError:
            pass
        match = INPUT_RE.fullmatch(source.name)
        if match is None:
            invalid.append(source)
            log(f"[扫描 {index}/{len(files)}] 文件名无县代码：{source.name}", "WARN")
            continue
        county_code = match.group("county_code")
        region = regions.get(county_code)
        if region is None:
            unknown.append((source, county_code))
            log(f"[扫描 {index}/{len(files)}] 未知县代码 {county_code}", "WARN")
            continue
        destination = (
            output_root
            / safe_name(region.city_name)
            / safe_name(region.county_name)
            / source.name
        )
        jobs.append(Job(source, destination, region))
        log(
            f"[扫描 {index}/{len(files)}] {county_code} -> "
            f"{region.city_name}/{region.county_name}"
        )
    return jobs, invalid, unknown


def install_tif(source: Path, destination: Path, overwrite: bool) -> str:
    if destination.exists() and not overwrite:
        return "existing"
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(
        f".{destination.stem}.{uuid.uuid4().hex}.tmp{destination.suffix}"
    )
    try:
        shutil.copy2(source, temporary)
        temporary.replace(destination)
        return "created"
    finally:
        if temporary.exists():
            temporary.unlink()


def set_nodata_zero(path: Path, rasterio) -> None:
    with rasterio.Env(GDAL_PAM_ENABLED="NO"), rasterio.open(path, "r+") as dataset:
        dataset.nodata = 0


def create_123_jpeg(path: Path, np, rasterio, Image, Resampling) -> Path:
    """红=1、绿=2、蓝=3，并按截图从 0 拉伸到各波段最大值。"""
    jpeg = path.with_suffix(".jpeg")
    temporary = jpeg.with_name(f".{jpeg.stem}.{uuid.uuid4().hex}.tmp.jpeg")
    started_at = time.monotonic()
    try:
        with rasterio.Env(GDAL_PAM_ENABLED="NO"), rasterio.open(path) as source:
            scale = min(1.0, THUMBNAIL_MAX_SIZE / max(source.width, source.height))
            width = max(1, round(source.width * scale))
            height = max(1, round(source.height * scale))
            indexes = [1, 2, 3] if source.count >= 3 else [1, 1, 1]
            data = source.read(
                indexes,
                out_shape=(3, height, width),
                resampling=Resampling.bilinear,
            ).astype("float32")
            valid = source.dataset_mask(
                out_shape=(height, width),
                resampling=Resampling.nearest,
            ) > 0
            valid &= np.any(np.isfinite(data) & (data != 0), axis=0)

        rows, columns = np.where(valid)
        if rows.size == 0:
            raise ValueError(f"TIFF 没有非零有效像元：{path}")
        top, bottom = rows.min(), rows.max() + 1
        left, right = columns.min(), columns.max() + 1
        data = data[:, top:bottom, left:right]
        valid = valid[top:bottom, left:right]

        rgb = np.full((*valid.shape, 3), 255, dtype="uint8")
        for channel in range(3):
            band = data[channel]
            sample = band[valid & np.isfinite(band) & (band > 0)]
            if sample.size == 0:
                continue
            maximum = float(sample.max())
            if maximum <= 0:
                continue
            normalized = np.nan_to_num(
                np.clip(band / maximum, 0, 1),
                nan=0.0,
                posinf=1.0,
                neginf=0.0,
            )
            rgb[:, :, channel] = (normalized * 255).astype("uint8")
        rgb[~valid] = 255
        Image.fromarray(rgb).save(
            temporary,
            format="JPEG",
            quality=88,
            subsampling=2,
            optimize=True,
            progressive=True,
        )
        temporary.replace(jpeg)
        log(f"JPEG 完成：{jpeg}，耗时 {elapsed_text(started_at)}")
        return jpeg
    finally:
        if temporary.exists():
            temporary.unlink()


def run_job(job: Job, args: argparse.Namespace, dependencies) -> tuple[str, str]:
    _, np, rasterio, Image, Resampling = dependencies
    tif_status = install_tif(job.source, job.destination, args.overwrite)
    set_nodata_zero(job.destination, rasterio)
    jpeg = job.destination.with_suffix(".jpeg")
    if args.overwrite or not jpeg.exists():
        create_123_jpeg(job.destination, np, rasterio, Image, Resampling)
        jpeg_status = "created"
    else:
        jpeg_status = "existing"
    return tif_status, jpeg_status


def main() -> int:
    started_at = time.monotonic()
    args = parse_args()
    if args.max_workers < 1:
        print("错误：max-workers 必须大于或等于 1", file=sys.stderr)
        return 2
    args.input_root = args.input_root.expanduser().resolve()
    args.output_root = args.output_root.expanduser().resolve()
    if not args.input_root.is_dir():
        print(f"错误：输入目录不存在：{args.input_root}", file=sys.stderr)
        return 1

    try:
        if args.dry_run:
            try:
                import geopandas as gpd
            except ImportError as exc:
                raise RuntimeError("缺少 geopandas，无法读取行政区文件") from exc
            dependencies = None
        else:
            dependencies = require_dependencies()
            gpd = dependencies[0]
        regions = load_regions(args, gpd)
        jobs, invalid, unknown = scan_jobs(args.input_root, args.output_root, regions)
    except Exception as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 1

    log(
        f"扫描统计：可处理 {len(jobs)}，文件名不匹配 {len(invalid)}，"
        f"未知县代码 {len(unknown)}"
    )
    if args.dry_run:
        for index, job in enumerate(jobs, 1):
            log(f"[预览 {index}/{len(jobs)}] {job.source} -> {job.destination}")
        log("仅预览，未复制 TIFF 或生成 JPEG")
        return 0

    tif_created = tif_existing = jpeg_created = jpeg_existing = failed = completed = 0
    with ThreadPoolExecutor(max_workers=args.max_workers) as executor:
        futures = {executor.submit(run_job, job, args, dependencies): job for job in jobs}
        try:
            for future in as_completed(futures):
                job = futures[future]
                completed += 1
                try:
                    tif_status, jpeg_status = future.result()
                    tif_created += tif_status == "created"
                    tif_existing += tif_status == "existing"
                    jpeg_created += jpeg_status == "created"
                    jpeg_existing += jpeg_status == "existing"
                    log(
                        f"[总体 {completed}/{len(jobs)}] 完成："
                        f"{job.region.city_name}/{job.region.county_name}/{job.destination.name}，"
                        f"TIFF {tif_status}，JPEG {jpeg_status}"
                    )
                except Exception as exc:
                    failed += 1
                    log(
                        f"[总体 {completed}/{len(jobs)}] 失败：{job.source}：{exc}",
                        "ERROR",
                    )
        except KeyboardInterrupt:
            for future in futures:
                future.cancel()
            log("收到停止信号，正在结束线程", "WARN")
            return 130

    log(
        f"处理完成：TIFF 新建 {tif_created}、已有 {tif_existing}；"
        f"JPEG 新建 {jpeg_created}、已有 {jpeg_existing}；失败 {failed}；"
        f"总耗时 {elapsed_text(started_at)}"
    )
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
