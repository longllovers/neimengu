#!/usr/bin/env python3
r"""递归将 GeoTIFF 转为 Albers 等积圆锥投影。

Windows 默认输入目录：
    E:\0.5m_buchong
Windows 默认输出目录：
    E:\转投影影像

规则：
1. 递归扫描输入目录中的 .tif 和 .tiff（不扫描输出目录）。
2. 输出文件全部直接放在输出目录，文件名保持不变。
3. 同名源文件只处理排序后的第一个，其余跳过；已存在的输出也跳过。
4. 源影像已经是目标投影时使用 Python 分块复制，否则使用 rasterio 重投影。
5. 每次运行同时输出终端日志和输出目录中的日志文件。
"""

from __future__ import annotations

import argparse
import logging
import os
import shutil
import sys
import time
import uuid
from pathlib import Path

import rasterio
from pyproj import CRS
from rasterio.enums import Resampling
from rasterio.warp import calculate_default_transform, reproject


# Windows 路径必须使用原始字符串，避免把 "\0" 等内容解析为转义字符。
DEFAULT_INPUT_DIR = Path(r"F:\新替换数据")
DEFAULT_OUTPUT_DIR = Path(r"F:\转投影影像")
COPY_BUFFER_SIZE = 16 * 1024 * 1024

# 中国区域常用 Albers 等积圆锥参数：
# 基准 WGS84，中央经线 105°，标准纬线 25°/47°，原点纬线 0°。
# 使用显式 WKT 是为了让投影名称写为用户要求的 Albers_Conic_Equal_Aera。
TARGET_CRS_WKT = """PROJCS["Albers_Conic_Equal_Aera",
GEOGCS["GCS_WGS_1984",
DATUM["D_WGS_1984",
SPHEROID["WGS_1984",6378137,298.257223563]],
PRIMEM["Greenwich",0],
UNIT["Degree",0.0174532925199433]],
PROJECTION["Albers_Conic_Equal_Area"],
PARAMETER["False_Easting",0],
PARAMETER["False_Northing",0],
PARAMETER["Central_Meridian",105],
PARAMETER["Standard_Parallel_1",25],
PARAMETER["Standard_Parallel_2",47],
PARAMETER["Latitude_Of_Origin",0],
UNIT["Meter",1]]"""

TARGET_CRS = rasterio.crs.CRS.from_wkt(TARGET_CRS_WKT)
TARGET_PYPROJ_CRS = CRS.from_wkt(TARGET_CRS_WKT)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="递归复制或重投影所有 TIFF 到 Albers_Conic_Equal_Aera。"
    )
    parser.add_argument(
        "--input-dir",
        type=Path,
        default=DEFAULT_INPUT_DIR,
        help=f"输入根目录（默认：{DEFAULT_INPUT_DIR}）",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=DEFAULT_OUTPUT_DIR,
        help=f"输出目录（默认：{DEFAULT_OUTPUT_DIR}）",
    )
    parser.add_argument(
        "--threads",
        type=int,
        default=max(1, min(16, os.cpu_count())),
        help="单个影像重投影使用的线程数（默认：最多 16）",
    )
    parser.add_argument(
        "--resampling",
        choices=("nearest", "bilinear", "cubic"),
        default="bilinear",
        help="重采样方法（默认：bilinear）",
    )
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="覆盖已经存在的目标文件（默认：跳过）",
    )
    return parser.parse_args()


def setup_logger(output_dir: Path) -> tuple[logging.Logger, Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    timestamp = time.strftime("%Y%m%d_%H%M%S")
    log_path = output_dir / f"转投影_{timestamp}.log"

    logger = logging.getLogger("reproject_tifs")
    logger.setLevel(logging.INFO)
    logger.handlers.clear()
    formatter = logging.Formatter(
        fmt="%(asctime)s | %(levelname)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(formatter)
    logger.addHandler(console)

    file_handler = logging.FileHandler(log_path, encoding="utf-8")
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)
    return logger, log_path


def is_within(path: Path, directory: Path) -> bool:
    """判断 path 是否位于 directory 内（包含 directory 本身）。"""
    try:
        path.resolve().relative_to(directory.resolve())
        return True
    except ValueError:
        return False


def scan_tifs(input_dir: Path, output_dir: Path) -> list[Path]:
    """递归扫描 TIFF，明确排除输出目录。"""
    files: list[Path] = []
    for root, dirs, names in os.walk(input_dir):
        root_path = Path(root)
        dirs[:] = [
            name
            for name in dirs
            if not is_within(root_path / name, output_dir)
        ]
        for name in names:
            if Path(name).suffix.lower() in {".tif", ".tiff"}:
                files.append(root_path / name)
    return sorted(files, key=lambda path: str(path).casefold())


def same_crs(source_crs: rasterio.crs.CRS | None) -> bool:
    """用 pyproj 的等价判断，避免仅因 WKT 写法不同而误判。"""
    if source_crs is None:
        return False
    try:
        return CRS.from_user_input(source_crs).equals(
            TARGET_PYPROJ_CRS, ignore_axis_order=True
        )
    except Exception:
        return False


def format_mb(byte_count: int) -> str:
    return f"{byte_count / (1024 * 1024):.2f} MB"


def windows_copy(
    source: Path,
    destination: Path,
    index: int,
    total: int,
    logger: logging.Logger,
) -> tuple[float, float]:
    """在 Windows 下分块复制到临时文件，完成后原子替换目标文件。"""
    temporary = destination.with_name(
        f".{destination.name}.{uuid.uuid4().hex}.copy-part"
    )
    started = time.monotonic()
    source_size = source.stat().st_size
    copied_size = 0
    interval_size = 0
    interval_started = started

    try:
        with source.open("rb") as source_file, temporary.open("wb") as target_file:
            while chunk := source_file.read(COPY_BUFFER_SIZE):
                target_file.write(chunk)
                chunk_size = len(chunk)
                copied_size += chunk_size
                interval_size += chunk_size

                now = time.monotonic()
                interval_elapsed = now - interval_started
                if interval_elapsed >= 1:
                    speed = interval_size / interval_elapsed / (1024 * 1024)
                    percent = min(
                        100.0,
                        copied_size * 100 / max(source_size, 1),
                    )
                    logger.info(
                        "[%d/%d] Windows 复制中 | %s | %.1f%% | %.2f MB/s"
                        " | 已写入 %s",
                        index,
                        total,
                        source.name,
                        percent,
                        speed,
                        format_mb(copied_size),
                    )
                    interval_size = 0
                    interval_started = now

        # 尽量保留源文件的访问时间、修改时间和权限信息。
        try:
            shutil.copystat(source, temporary)
        except OSError as error:
            logger.warning(
                "[%d/%d] 文件内容已复制，但元数据复制失败 | %s | %s",
                index,
                total,
                source,
                error,
            )
        temporary.replace(destination)
    except BaseException:
        # 同时处理 Ctrl+C，避免中断后遗留巨大的临时文件。
        temporary.unlink(missing_ok=True)
        raise

    elapsed = max(time.monotonic() - started, 0.001)
    average_speed = source_size / elapsed / (1024 * 1024)
    return elapsed, average_speed


def copy_metadata(source: rasterio.io.DatasetReader, destination) -> None:
    """复制数据集、波段标签和颜色表；不覆盖新生成的空间参考。"""
    dataset_tags = source.tags()
    if dataset_tags:
        destination.update_tags(**dataset_tags)

    for band in range(1, source.count + 1):
        tags = source.tags(band)
        if tags:
            destination.update_tags(band, **tags)
        try:
            color_map = source.colormap(band)
        except ValueError:
            color_map = None
        if color_map:
            destination.write_colormap(band, color_map)
        if source.descriptions[band - 1]:
            destination.set_band_description(
                band, source.descriptions[band - 1]
            )
        if source.units[band - 1]:
            destination.set_band_unit(band, source.units[band - 1])


def reproject_tif(
    source_path: Path,
    destination: Path,
    threads: int,
    resampling: Resampling,
    index: int,
    total: int,
    logger: logging.Logger,
) -> tuple[float, float]:
    """将单个 TIFF 重投影到临时文件，成功后原子替换。"""
    temporary = destination.with_name(
        f".{destination.stem}.{uuid.uuid4().hex}.warp-part.tif"
    )
    started = time.monotonic()
    try:
        with rasterio.open(source_path) as source:
            if source.crs is None:
                raise ValueError("源影像没有 CRS，无法重投影")
            transform, width, height = calculate_default_transform(
                source.crs,
                TARGET_CRS,
                source.width,
                source.height,
                *source.bounds,
            )
            profile = source.profile.copy()
            profile.update(
                driver="GTiff",
                crs=TARGET_CRS,
                transform=transform,
                width=width,
                height=height,
                compress="DEFLATE",
                predictor=2 if source.dtypes[0].startswith(("int", "uint")) else 3,
                # reproject() 是逐波段写入。若继承源文件的 PIXEL 交错，
                # 写第 2、3 波段时必须反复读取并解压已经写好的 DEFLATE
                # 瓦片，再修改和重压；在 CIFS/NAS 上不仅很慢，还可能出现
                # ZIPDecode/TIFFReadEncodedTile 错误。BAND 交错可让每个波段
                # 独立顺序写入，避免压缩块的读-改-写。
                interleave="band",
                tiled=True,
                blockxsize=512,
                blockysize=512,
                BIGTIFF="IF_SAFER",
            )

            logger.info(
                "[%d/%d] 开始重投影 | %s | %dx%d -> %dx%d | %d 个波段"
                " | 输出交错方式 BAND",
                index,
                total,
                source_path,
                source.width,
                source.height,
                width,
                height,
                source.count,
            )
            with rasterio.open(temporary, "w", **profile) as target:
                for band in range(1, source.count + 1):
                    band_started = time.monotonic()
                    reproject(
                        source=rasterio.band(source, band),
                        destination=rasterio.band(target, band),
                        src_transform=source.transform,
                        src_crs=source.crs,
                        src_nodata=source.nodatavals[band - 1],
                        dst_transform=transform,
                        dst_crs=TARGET_CRS,
                        dst_nodata=source.nodatavals[band - 1],
                        resampling=resampling,
                        num_threads=threads,
                        init_dest_nodata=True,
                    )
                    logger.info(
                        "[%d/%d] 重投影波段 %d/%d 完成 | %s | 用时 %.1f 秒",
                        index,
                        total,
                        band,
                        source.count,
                        source_path.name,
                        time.monotonic() - band_started,
                    )
                copy_metadata(source, target)

        temporary.replace(destination)
    except BaseException:
        # 同时处理 Ctrl+C，避免中断后遗留巨大的临时文件。
        temporary.unlink(missing_ok=True)
        raise

    elapsed = max(time.monotonic() - started, 0.001)
    average_speed = source_path.stat().st_size / elapsed / (1024 * 1024)
    return elapsed, average_speed


def main() -> int:
    args = parse_args()
    input_dir = args.input_dir.expanduser().resolve()
    output_dir = args.output_dir.expanduser().resolve()

    if not input_dir.is_dir():
        print(f"错误：输入目录不存在或不是目录：{input_dir}", file=sys.stderr)
        return 2
    if input_dir == output_dir:
        print("错误：输入目录和输出目录不能相同。", file=sys.stderr)
        return 2
    if args.threads < 1:
        print("错误：--threads 必须大于或等于 1。", file=sys.stderr)
        return 2
    logger, log_path = setup_logger(output_dir)
    resampling = {
        "nearest": Resampling.nearest,
        "bilinear": Resampling.bilinear,
        "cubic": Resampling.cubic,
    }[args.resampling]

    logger.info("任务开始")
    logger.info("输入目录：%s", input_dir)
    logger.info("输出目录：%s", output_dir)
    logger.info("目标投影：Albers_Conic_Equal_Aera")
    logger.info("投影参数：中央经线 105°，标准纬线 25°/47°，WGS84")
    logger.info("重采样：%s；线程数：%d", args.resampling, args.threads)
    logger.info("日志文件：%s", log_path)

    all_sources = scan_tifs(input_dir, output_dir)
    total = len(all_sources)
    logger.info("扫描完成，共发现 %d 个 TIFF。", total)

    copied = 0
    reprojected = 0
    skipped_existing = 0
    skipped_duplicate = 0
    failed = 0
    seen_names: dict[str, Path] = {}
    task_started = time.monotonic()

    for index, source_path in enumerate(all_sources, start=1):
        name_key = source_path.name.casefold()
        previous = seen_names.get(name_key)
        if previous is not None:
            skipped_duplicate += 1
            logger.warning(
                "[%d/%d] 跳过同名 TIFF | %s | 已采用：%s",
                index,
                total,
                source_path,
                previous,
            )
            continue
        seen_names[name_key] = source_path

        destination = output_dir / source_path.name
        if destination.exists() and not args.overwrite:
            skipped_existing += 1
            logger.info(
                "[%d/%d] 跳过，目标已存在 | %s",
                index,
                total,
                destination,
            )
            continue

        try:
            with rasterio.open(source_path) as source:
                source_crs = source.crs
                source_size = source_path.stat().st_size
                logger.info(
                    "[%d/%d] 检查影像 | %s | 大小 %s | 源 CRS：%s",
                    index,
                    total,
                    source_path,
                    format_mb(source_size),
                    source_crs if source_crs else "无",
                )

            if same_crs(source_crs):
                logger.info(
                    "[%d/%d] 已是目标投影，开始 Windows 分块复制 | %s",
                    index,
                    total,
                    source_path,
                )
                elapsed, speed = windows_copy(
                    source_path, destination, index, total, logger
                )
                copied += 1
                logger.info(
                    "[%d/%d] Windows 复制完成 | %s | %.2f MB/s | 用时 %.1f 秒",
                    index,
                    total,
                    destination,
                    speed,
                    elapsed,
                )
            else:
                elapsed, speed = reproject_tif(
                    source_path,
                    destination,
                    args.threads,
                    resampling,
                    index,
                    total,
                    logger,
                )
                reprojected += 1
                logger.info(
                    "[%d/%d] 重投影完成 | %s | 源文件平均处理速度 %.2f MB/s"
                    " | 用时 %.1f 秒",
                    index,
                    total,
                    destination,
                    speed,
                    elapsed,
                )
        except Exception:
            failed += 1
            logger.exception(
                "[%d/%d] 处理失败 | %s",
                index,
                total,
                source_path,
            )

    logger.info(
        "任务结束 | 扫描 %d | 直接复制 %d | 重投影 %d | "
        "跳过已存在 %d | 跳过同名 %d | 失败 %d | 总用时 %.1f 秒",
        total,
        copied,
        reprojected,
        skipped_existing,
        skipped_duplicate,
        failed,
        time.monotonic() - task_started,
    )
    logger.info("完整日志：%s", log_path)
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
