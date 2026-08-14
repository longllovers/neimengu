#!/usr/bin/env python3
"""整理带县代码的 TIFF，并生成项目格式的 country_data 目录。

输入文件名前 6 位为县代码，例如：
    150302_T48SXJ_20260604T033529.tif
    150922_S2B_MSIL2A_20260625T030519_...SAFE.tif

输出目录格式：
    country_data/乌海市/海勃湾区/
        150302_T48SXJ_20260604T033529.tif
        150302_T48SXJ_20260604T033529.jpeg

脚本默认复制源 TIFF，不移动、不删除 E:\\image_CLIP_2 中的任何文件。
输出 TIFF 会先将 NoData 设置为 0，再按波段 3/2/1 生成真彩色 JPEG。
"""

from __future__ import annotations

import argparse
import re
import shutil
import sys
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path


BASE_DIR = Path(__file__).resolve().parent
DEFAULT_INPUT_ROOT = Path(r"E:\image_CLIP_2")
DEFAULT_OUTPUT_ROOT = Path(r"E:\country_data")
DEFAULT_COUNTY_LAYER = BASE_DIR / "00县边界" / "15_县边界.shp"
DEFAULT_CITY_LAYER = BASE_DIR / "00市边界" / "15_市边界.shp"
THUMBNAIL_MAX_SIZE = 1200

INPUT_NAME_RE = re.compile(
    r"^(?P<county_code>\d{6})_.+\.tif$",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class Region:
    city_name: str
    county_name: str


@dataclass(frozen=True)
class Job:
    source: Path
    destination: Path
    region: Region


def safe_directory_name(value: object) -> str:
    """生成适用于 Windows 和 Linux 的安全目录名。"""
    text = str(value).strip()
    return re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", text).rstrip(". ")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="把 E:\\image_CLIP_2 中带县代码的 TIFF 整理到 country_data，并生成 JPEG。"
    )
    parser.add_argument(
        "--input-root",
        type=Path,
        default=DEFAULT_INPUT_ROOT,
        help=r"输入 TIFF 根目录，默认 E:\image_CLIP_2",
    )
    parser.add_argument(
        "--output-root",
        type=Path,
        default=DEFAULT_OUTPUT_ROOT,
        help=r"输出根目录，默认 E:\country_data",
    )
    parser.add_argument(
        "--county-layer",
        type=Path,
        default=DEFAULT_COUNTY_LAYER,
        help="县界 Shapefile",
    )
    parser.add_argument(
        "--city-layer",
        type=Path,
        default=DEFAULT_CITY_LAYER,
        help="市界 Shapefile",
    )
    parser.add_argument("--county-code-field", default="area_code", help="县代码字段")
    parser.add_argument("--county-name-field", default="area_name", help="县名称字段")
    parser.add_argument("--city-code-field", default="市代码", help="市代码字段")
    parser.add_argument("--city-name-field", default="市名称", help="市名称字段")
    parser.add_argument(
        "--max-workers",
        type=int,
        default=2,
        help="并发复制和生成 JPEG 的任务数，默认 2",
    )
    parser.add_argument("--overwrite", action="store_true", help="覆盖已有 TIFF 和 JPEG")
    parser.add_argument("--dry-run", action="store_true", help="只显示整理计划，不写文件")
    return parser.parse_args()


def six_digit_code(value: object) -> str:
    """兼容 DBF 中的字符串、整数和浮点数字段。"""
    text = str(value).strip()
    if re.fullmatch(r"\d+\.0+", text):
        text = text.split(".", 1)[0]
    digits = re.sub(r"\D", "", text)
    return digits[:6] if len(digits) >= 6 else ""


def four_digit_code(value: object) -> str:
    text = str(value).strip()
    if re.fullmatch(r"\d+\.0+", text):
        text = text.split(".", 1)[0]
    digits = re.sub(r"\D", "", text)
    return digits[:4] if len(digits) >= 4 else ""


def load_region_index(args: argparse.Namespace) -> dict[str, Region]:
    try:
        import geopandas as gpd
    except ImportError as exc:
        raise RuntimeError("缺少 geopandas，无法读取行政区 Shapefile") from exc

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
        city_code = four_digit_code(row[args.city_code_field])
        city_name = str(row[args.city_name_field]).strip()
        if city_code and city_name:
            city_names[city_code] = city_name

    regions: dict[str, Region] = {}
    for _, row in counties.iterrows():
        county_code = six_digit_code(row[args.county_code_field])
        county_name = str(row[args.county_name_field]).strip()
        city_name = city_names.get(county_code[:4])
        if not county_code or not county_name or not city_name:
            continue
        region = Region(city_name=city_name, county_name=county_name)
        previous = regions.get(county_code)
        if previous is not None and previous != region:
            raise ValueError(
                f"县代码 {county_code} 同时对应 {previous.county_name} 和 {county_name}"
            )
        regions[county_code] = region

    if not regions:
        raise ValueError("行政区文件中没有可用的市县代码")
    return regions


def find_input_tifs(input_root: Path, output_root: Path) -> list[Path]:
    output_resolved = output_root.resolve()
    files: list[Path] = []
    for path in input_root.rglob("*.tif"):
        if not path.is_file() or path.name.startswith("."):
            continue
        try:
            path.resolve().relative_to(output_resolved)
        except ValueError:
            files.append(path)
    return sorted(files)


def build_jobs(
    input_files: list[Path],
    output_root: Path,
    regions: dict[str, Region],
) -> tuple[list[Job], list[Path], list[tuple[Path, str]]]:
    jobs: list[Job] = []
    invalid_names: list[Path] = []
    unknown_codes: list[tuple[Path, str]] = []

    for source in input_files:
        match = INPUT_NAME_RE.fullmatch(source.name)
        if match is None:
            invalid_names.append(source)
            continue
        county_code = match.group("county_code")
        region = regions.get(county_code)
        if region is None:
            unknown_codes.append((source, county_code))
            continue
        destination = (
            output_root
            / safe_directory_name(region.city_name)
            / safe_directory_name(region.county_name)
            / source.name
        )
        jobs.append(Job(source=source, destination=destination, region=region))

    return jobs, invalid_names, unknown_codes


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
    finally:
        if temporary.exists():
            temporary.unlink()
    return "copied"


def set_tif_nodata_zero(tif_path: Path, rasterio) -> None:
    """把输出 TIFF 的 NoData 元数据设置为 0。"""
    with rasterio.Env(GDAL_PAM_ENABLED="NO"):
        with rasterio.open(tif_path, "r+") as dataset:
            dataset.nodata = 0


def create_qgis_style_jpeg(
    tif_path: Path,
    rasterio,
    numpy,
    image_class,
    resampling,
    max_size: int = THUMBNAIL_MAX_SIZE,
) -> Path:
    """按 QGIS 的 3/2/1 多波段彩色和 0~最大值拉伸生成 JPEG。"""
    jpeg_path = tif_path.with_suffix(".jpeg")
    temporary_jpeg = jpeg_path.with_name(f".{jpeg_path.stem}.tmp.jpeg")
    if temporary_jpeg.exists():
        temporary_jpeg.unlink()

    try:
        with rasterio.Env(GDAL_PAM_ENABLED="NO"):
            with rasterio.open(tif_path) as source:
                scale = min(1.0, max_size / max(source.width, source.height))
                width = max(1, round(source.width * scale))
                height = max(1, round(source.height * scale))

                if source.count >= 3:
                    # 与截图中的 QGIS 设置一致：红=波段3，绿=波段2，蓝=波段1。
                    band_indexes = [3, 2, 1]
                else:
                    band_indexes = [1, 1, 1]

                data = source.read(
                    band_indexes,
                    out_shape=(3, height, width),
                    resampling=resampling.bilinear,
                ).astype("float32")
                valid = source.dataset_mask(
                    out_shape=(height, width),
                    resampling=resampling.nearest,
                ) > 0
                # 即使源文件带有“全部有效”的旧掩膜，也明确排除三个显示波段全为 0 的背景。
                valid &= numpy.any(numpy.isfinite(data) & (data != 0), axis=0)

        valid_rows, valid_columns = numpy.where(valid)
        if valid_rows.size == 0:
            raise ValueError(f"TIFF 没有非零有效影像像元：{tif_path}")

        # 去掉影像外部的空白边缘，内部 NoData 区域仍保留为白色。
        top, bottom = valid_rows.min(), valid_rows.max() + 1
        left, right = valid_columns.min(), valid_columns.max() + 1
        data = data[:, top:bottom, left:right]
        valid = valid[top:bottom, left:right]
        height, width = valid.shape

        rgb = numpy.full((height, width, 3), 255, dtype="uint8")
        for channel in range(3):
            band = data[channel]
            sample = band[valid & numpy.isfinite(band) & (band > 0)]
            if sample.size == 0:
                continue
            # 每个颜色波段按有效非零像元的最小值到最大值进行拉伸。
            minimum = float(sample.min())
            maximum = float(sample.max())
            if maximum <= minimum:
                maximum = minimum + 1.0
            normalized = numpy.nan_to_num(
                numpy.clip((band - minimum) / (maximum - minimum), 0, 1),
                nan=0.0,
                posinf=1.0,
                neginf=0.0,
            )
            rgb[:, :, channel] = (normalized * 255.0).astype("uint8")
        rgb[~valid] = 255

        image = image_class.fromarray(rgb)
        image.save(
            temporary_jpeg,
            format="JPEG",
            quality=88,
            subsampling=2,
            optimize=True,
            progressive=True,
        )
        temporary_jpeg.replace(jpeg_path)
        return jpeg_path
    finally:
        if temporary_jpeg.exists():
            temporary_jpeg.unlink()


def run_job(
    job: Job,
    overwrite: bool,
    rasterio,
    numpy,
    image_class,
    resampling,
) -> tuple[str, str]:
    tif_status = install_tif(job.source, job.destination, overwrite)
    set_tif_nodata_zero(job.destination, rasterio)
    jpeg_path = job.destination.with_suffix(".jpeg")
    if overwrite or not jpeg_path.exists():
        create_qgis_style_jpeg(
            job.destination,
            rasterio,
            numpy,
            image_class,
            resampling,
        )
        jpeg_status = "created"
    else:
        jpeg_status = "existing"
    return tif_status, jpeg_status


def main() -> int:
    args = parse_args()
    if args.max_workers < 1:
        print("错误：--max-workers 必须大于或等于 1", file=sys.stderr)
        return 2

    input_root = args.input_root.expanduser().resolve()
    output_root = args.output_root.expanduser().resolve()
    if not input_root.is_dir():
        print(f"错误：输入目录不存在：{input_root}", file=sys.stderr)
        return 1
    if input_root == output_root:
        print("错误：输入目录和输出目录不能相同", file=sys.stderr)
        return 2

    try:
        regions = load_region_index(args)
        input_files = find_input_tifs(input_root, output_root)
        jobs, invalid_names, unknown_codes = build_jobs(input_files, output_root, regions)
    except Exception as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 1

    print(f"扫描到 TIFF {len(input_files)} 个，可整理 {len(jobs)} 个。")
    for path in invalid_names:
        print(f"跳过（文件名中没有县代码）：{path.name}")
    for path, county_code in unknown_codes:
        print(f"跳过（县代码 {county_code} 未匹配行政区）：{path.name}")

    if args.dry_run:
        for job in jobs:
            print(f"计划：{job.source} -> {job.destination}")
        print(
            f"仅预览：计划 {len(jobs)}，文件名不匹配 {len(invalid_names)}，"
            f"未知县代码 {len(unknown_codes)}。"
        )
        return 0

    try:
        import numpy
        import rasterio
        from PIL import Image
        from rasterio.enums import Resampling
    except ImportError as exc:
        print(
            "错误：无法加载 JPEG 生成依赖，请安装 rasterio、numpy、Pillow 和 rich。"
            f"\n详细信息：{exc}",
            file=sys.stderr,
        )
        return 1

    copied = existing_tif = created_jpeg = existing_jpeg = failed = 0
    completed = 0

    def record(job: Job, result=None, error: Exception | None = None) -> None:
        nonlocal copied, existing_tif, created_jpeg, existing_jpeg, failed, completed
        completed += 1
        if error is not None:
            failed += 1
            print(f"失败：{job.source.name}：{error}", file=sys.stderr)
        else:
            tif_status, jpeg_status = result
            copied += tif_status == "copied"
            existing_tif += tif_status == "existing"
            created_jpeg += jpeg_status == "created"
            existing_jpeg += jpeg_status == "existing"
            print(
                f"[{completed}/{len(jobs)}] {job.region.city_name}/"
                f"{job.region.county_name}/{job.destination.name}"
            )

    try:
        if args.max_workers == 1:
            for job in jobs:
                try:
                    record(
                        job,
                        run_job(
                            job,
                            args.overwrite,
                            rasterio,
                            numpy,
                            Image,
                            Resampling,
                        ),
                    )
                except Exception as exc:
                    record(job, error=exc)
        else:
            with ThreadPoolExecutor(max_workers=args.max_workers) as executor:
                futures = {
                    executor.submit(
                        run_job,
                        job,
                        args.overwrite,
                        rasterio,
                        numpy,
                        Image,
                        Resampling,
                    ): job
                    for job in jobs
                }
                for future in as_completed(futures):
                    job = futures[future]
                    try:
                        record(job, future.result())
                    except Exception as exc:
                        record(job, error=exc)
    except KeyboardInterrupt:
        print("已停止处理。", file=sys.stderr)
        return 130

    print(
        f"处理完成：复制 TIFF {copied}，已有 TIFF {existing_tif}，"
        f"新建 JPEG {created_jpeg}，已有 JPEG {existing_jpeg}，失败 {failed}。"
    )
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
