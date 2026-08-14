#!/usr/bin/env python3
"""用 64 个独立 Python 子进程批量重采样 GeoTIFF。

默认处理规则：
* 递归读取 /mnt/data/4np/0.5m_转投影 下的 .tif/.tiff；
* 转为 CGCS2000 Albers 米制投影，将像元大小重采样为 0.5 m × 0.5 m；
* 输出为 LZW 压缩、256 × 256 像素分块的 GeoTIFF；
* 同时建立 2、4、8、16、32、64、128、256 倍内部金字塔和 ArcGIS .ovr；
* 保留相对目录结构，先写临时文件，成功后再原子改名。

依赖：pip install rasterio，并确保系统能运行 gdaladdo。
"""

from __future__ import annotations

import argparse
import concurrent.futures
import logging
import logging.handlers
import math
import multiprocessing as mp
import os
import signal
import shutil
import subprocess
import sys
import time
import traceback
import unicodedata
import uuid
from pathlib import Path

import rasterio
from rasterio.enums import Resampling
from rasterio.warp import calculate_default_transform, reproject


DEFAULT_INPUT_DIR = Path("/mnt/data/4np/0.5m_转投影")
DEFAULT_OUTPUT_DIR = Path("/mnt/data/4np/0.5m_最终版")
DEFAULT_WORKERS = 64
RESOLUTION = 0.5
BLOCK_SIZE = 256
OVERVIEW_FACTORS = (2, 4, 8, 16, 32, 64, 128, 256)
STALE_LOCK_SECONDS = 24 * 60 * 60

# EPSG:4490 是经纬度坐标系，单位为度，无法表达 0.5 m 像元。本脚本使用
# CGCS2000 基准的 Albers 等积圆锥投影，坐标单位为米，适合内蒙古全区。
TARGET_CRS_WKT = """PROJCS["CGCS2000_Albers",
GEOGCS["GCS_China_Geodetic_Coordinate_System_2000",
DATUM["D_China_2000",
SPHEROID["CGCS2000",6378137,298.257222101]],
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

_worker_logger: logging.Logger | None = None
_active_subprocess: subprocess.Popen[str] | None = None


def stop_active_worker_subprocess() -> None:
    """终止工作进程当前启动的 gdaladdo，防止其成为孤儿进程。"""
    global _active_subprocess
    process = _active_subprocess
    if process is None or process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=3)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=3)


def worker_terminate_handler(signum: int, _frame) -> None:
    stop_active_worker_subprocess()
    raise SystemExit(128 + signum)


def main_interrupt_handler(_signum: int, _frame) -> None:
    raise KeyboardInterrupt


def truncate_terminal_text(value: str, max_width: int) -> str:
    width = 0
    output: list[str] = []
    for character in value:
        if unicodedata.combining(character):
            char_width = 0
        else:
            char_width = 2 if unicodedata.east_asian_width(character) in {"W", "F"} else 1
        if width + char_width > max_width:
            if max_width >= 2 and output:
                output[-1] = "…"
            break
        output.append(character)
        width += char_width
    return "".join(output)


class MainLogFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        return record.name == "resample_main"


class ConciseFileFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        return (
            record.name == "resample_main"
            or bool(getattr(record, "progress_final", False))
            or record.levelno >= logging.WARNING
        )


class ProgressDashboardHandler(logging.Handler):
    """TTY 中每个工作进程固定一行；重定向时仅输出最终状态和警告。"""

    def __init__(self, stream, status_path: Path) -> None:
        super().__init__(logging.INFO)
        self.stream = stream
        self.status_path = status_path
        self.is_tty = bool(getattr(stream, "isatty", lambda: False)())
        self.pid_to_slot: dict[int, int] = {}
        self.lines: list[str] = []
        self.rendered_lines = 0
        try:
            self.status_path.write_text("", encoding="utf-8")
        except OSError:
            pass

    def write_status_snapshot(self) -> None:
        temporary = self.status_path.with_name(self.status_path.name + ".tmp")
        try:
            temporary.write_text("\n".join(self.lines) + "\n", encoding="utf-8")
            os.replace(temporary, self.status_path)
        except OSError:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass

    def emit(self, record: logging.LogRecord) -> None:
        if not hasattr(record, "progress_index"):
            return
        try:
            line = record.getMessage().replace("\n", " ").replace("\r", " ")
            slot = self.pid_to_slot.get(record.process)
            if slot is None:
                slot = len(self.lines)
                self.pid_to_slot[record.process] = slot
                self.lines.append(line)
            else:
                self.lines[slot] = line
            self.write_status_snapshot()

            if not self.is_tty:
                if getattr(record, "progress_final", False) or record.levelno >= logging.WARNING:
                    self.stream.write(line + "\n")
                    self.flush()
                return

            columns = max(40, shutil.get_terminal_size(fallback=(160, 40)).columns)
            if self.rendered_lines:
                self.stream.write(f"\x1b[{self.rendered_lines}F")
            for status in self.lines:
                # 清除整行，限制长度，防止自动折行破坏固定行布局。
                shortened = truncate_terminal_text(status, max(1, columns - 1))
                self.stream.write("\x1b[2K" + shortened + "\n")
            self.rendered_lines = len(self.lines)
            self.flush()
        except Exception:
            self.handleError(record)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="64 进程转 CGCS2000 Albers、重采样为 0.5 m，并建立内外金字塔。"
    )
    parser.add_argument("--input-dir", type=Path, default=DEFAULT_INPUT_DIR)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--workers", type=int, default=DEFAULT_WORKERS)
    parser.add_argument(
        "--resampling",
        choices=("nearest", "bilinear", "cubic"),
        default="bilinear",
        help="主影像重采样算法，连续影像建议 bilinear；分类数据请用 nearest",
    )
    parser.add_argument(
        "--overview-resampling",
        choices=("nearest", "average", "bilinear"),
        default="average",
        help="金字塔重采样算法",
    )
    parser.add_argument(
        "--retries",
        type=int,
        default=3,
        help="单个 TIFF 遇到临时读写错误后的重试次数（默认：3）",
    )
    parser.add_argument(
        "--retry-delay",
        type=float,
        default=10.0,
        help="第一次重试等待秒数，后续指数退避（默认：10）",
    )
    parser.add_argument(
        "--gdal-cache-mb",
        type=int,
        default=256,
        help="每个进程的 GDAL 缓存 MiB（默认：256；64进程约16GiB）",
    )
    parser.add_argument("--overwrite", action="store_true", help="覆盖已有输出")
    return parser.parse_args()


def setup_main_logger(output_dir: Path) -> tuple[logging.Logger, Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    log_path = output_dir / time.strftime("重采样_0.5m_%Y%m%d_%H%M%S.log")
    logger = logging.getLogger("resample_main")
    logger.setLevel(logging.INFO)
    logger.handlers.clear()
    logger.propagate = False
    formatter = logging.Formatter(
        "%(asctime)s | %(levelname)s | PID=%(process)d | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    main_console = logging.StreamHandler(sys.stdout)
    main_console.setFormatter(formatter)
    main_console.addFilter(MainLogFilter())
    logger.addHandler(main_console)

    dashboard = ProgressDashboardHandler(
        sys.stdout,
        output_dir / "当前处理状态.txt",
    )
    logger.addHandler(dashboard)

    file_handler = logging.FileHandler(log_path, encoding="utf-8")
    file_handler.setFormatter(formatter)
    file_handler.addFilter(ConciseFileFilter())
    logger.addHandler(file_handler)
    return logger, log_path


def init_worker(log_queue: mp.Queue) -> None:
    """每个子进程初始化一次，将日志安全地发回主进程。"""
    global _worker_logger
    logger = logging.getLogger("resample_worker")
    logger.setLevel(logging.INFO)
    logger.handlers.clear()
    logger.propagate = False
    logger.addHandler(logging.handlers.QueueHandler(log_queue))
    _worker_logger = logger

    # 终端 Ctrl+C 只由主进程处理；主进程随后向工作进程发送 SIGTERM。
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    signal.signal(signal.SIGTERM, worker_terminate_handler)

    # 64 个文件级进程已经足够；禁止 GDAL 在每个进程里继续大量开线程。
    os.environ["GDAL_NUM_THREADS"] = "1"
    os.environ["OMP_NUM_THREADS"] = "1"
    os.environ["OPENBLAS_NUM_THREADS"] = "1"


def worker_status(
    index: int,
    total: int,
    filename: str,
    step: str,
    *,
    level: int = logging.INFO,
    final: bool = False,
) -> None:
    if _worker_logger is not None:
        _worker_logger.log(
            level,
            "[%d/%d] %s | %s",
            index,
            total,
            filename,
            step,
            extra={
                "progress_index": index,
                "progress_total": total,
                "progress_filename": filename,
                "progress_step": step,
                "progress_final": final,
            },
        )


def scan_tifs(input_dir: Path, output_dir: Path) -> list[Path]:
    output_resolved = output_dir.resolve()
    found: list[Path] = []
    for root, dirs, names in os.walk(input_dir):
        root_path = Path(root)
        kept_dirs: list[str] = []
        for name in dirs:
            candidate = (root_path / name).resolve()
            try:
                candidate.relative_to(output_resolved)
            except ValueError:
                kept_dirs.append(name)
        dirs[:] = kept_dirs
        for name in names:
            if Path(name).suffix.lower() in {".tif", ".tiff"}:
                found.append(root_path / name)
    return sorted(found, key=lambda p: str(p).casefold())


def predictor_for(dtype: str) -> int:
    return 3 if dtype.startswith(("float", "complex")) else 2


def source_fingerprint(path: Path) -> tuple[int, int]:
    """用大小和纳秒级修改时间判断处理期间源文件是否被改写。"""
    stat = path.stat()
    return stat.st_size, stat.st_mtime_ns


def process_is_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def acquire_output_lock(lock_path: Path) -> None:
    """跨进程、跨脚本实例独占目标，防止两个程序同时写同一 TIFF。"""
    for lock_attempt in range(2):
        try:
            descriptor = os.open(
                lock_path,
                os.O_CREAT | os.O_EXCL | os.O_WRONLY,
                0o644,
            )
            break
        except FileExistsError as exc:
            owner_pid = 0
            try:
                content = lock_path.read_text(encoding="utf-8", errors="replace")
                for item in content.split():
                    if item.startswith("pid="):
                        owner_pid = int(item[4:])
                        break
            except (OSError, ValueError):
                pass
            if owner_pid and process_is_alive(owner_pid):
                raise RuntimeError(
                    f"目标正由 PID {owner_pid} 处理，锁文件：{lock_path}"
                ) from exc
            if not owner_pid:
                try:
                    lock_age = time.time() - lock_path.stat().st_mtime
                except OSError:
                    lock_age = 0.0
                if lock_age < STALE_LOCK_SECONDS:
                    # 另一个进程可能刚创建锁、尚未来得及写入 PID，不能误删。
                    raise RuntimeError(
                        f"目标锁刚创建或无法读取，暂不删除：{lock_path}"
                    ) from exc
            if lock_attempt == 0:
                # 锁的所属进程已不存在，清除崩溃或断电留下的陈旧锁。
                try:
                    lock_path.unlink()
                except FileNotFoundError:
                    pass
                continue
            raise RuntimeError(f"无法取得目标锁：{lock_path}") from exc
    else:
        raise RuntimeError(f"无法取得目标锁：{lock_path}")
    try:
        content = f"pid={os.getpid()} time={time.strftime('%Y-%m-%d %H:%M:%S')}\n"
        os.write(descriptor, content.encode("utf-8"))
    finally:
        os.close(descriptor)


def copy_metadata(source: rasterio.io.DatasetReader, target) -> None:
    tags = source.tags()
    if tags:
        target.update_tags(**tags)
    for band in range(1, source.count + 1):
        band_tags = source.tags(band)
        if band_tags:
            target.update_tags(band, **band_tags)
        description = source.descriptions[band - 1]
        if description:
            target.set_band_description(band, description)
        unit = source.units[band - 1]
        if unit:
            target.set_band_unit(band, unit)
        try:
            color_map = source.colormap(band)
        except ValueError:
            color_map = None
        if color_map:
            target.write_colormap(band, color_map)


def valid_overview_factors(width: int, height: int) -> list[int]:
    # GDAL 要求缩小后至少仍有 1 个像元。
    return [factor for factor in OVERVIEW_FACTORS if width // factor >= 1 and height // factor >= 1]


def output_is_complete(path: Path) -> bool:
    """校验主 TIFF 的参数、内部金字塔以及外部 .ovr。"""
    external_ovr = path.with_name(path.name + ".ovr")
    try:
        with rasterio.open(path) as dataset:
            compression = dataset.compression
            expected_overview_count = len(
                valid_overview_factors(dataset.width, dataset.height)
            )
            main_is_complete = (
                dataset.width > 0
                and dataset.height > 0
                and dataset.crs == TARGET_CRS
                and math.isclose(abs(dataset.transform.a), RESOLUTION, abs_tol=1e-9)
                and math.isclose(abs(dataset.transform.e), RESOLUTION, abs_tol=1e-9)
                and compression is not None
                and compression.value.upper() == "LZW"
                and all(shape == (BLOCK_SIZE, BLOCK_SIZE) for shape in dataset.block_shapes)
                and all(
                    len(dataset.overviews(band)) == expected_overview_count
                    for band in range(1, dataset.count + 1)
                )
            )
        if not main_is_complete or not external_ovr.is_file():
            return False
        with rasterio.open(external_ovr) as overview_dataset:
            return overview_dataset.width > 0 and overview_dataset.height > 0
    except (OSError, rasterio.errors.RasterioError):
        return False


def build_external_overviews(
    base_tif: Path,
    factors: list[int],
    resampling_name: str,
    predictor: int,
    index: int,
    total: int,
    filename: str,
) -> Path:
    """用 gdaladdo 只读打开主 TIFF，生成 ArcGIS 可识别的 .tif.ovr。"""
    global _active_subprocess
    external_ovr = Path(str(base_tif) + ".ovr")
    command = [
        "gdaladdo",
        "-ro",
        "-r",
        resampling_name,
        "--config",
        "COMPRESS_OVERVIEW",
        "LZW",
        "--config",
        "PREDICTOR_OVERVIEW",
        str(predictor),
        "--config",
        "INTERLEAVE_OVERVIEW",
        "BAND",
        "--config",
        "BIGTIFF_OVERVIEW",
        "IF_SAFER",
        "--config",
        "GDAL_TIFF_OVR_BLOCKSIZE",
        str(BLOCK_SIZE),
        str(base_tif),
        *(str(factor) for factor in factors),
    ]
    worker_status(index, total, filename, "建立 ArcGIS 外部 OVR 金字塔")
    process = subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    _active_subprocess = process
    try:
        output, _ = process.communicate()
    finally:
        _active_subprocess = None
    output = output.strip()
    if process.returncode != 0:
        raise RuntimeError(f"gdaladdo 失败，退出码 {process.returncode}：{output}")
    if not external_ovr.is_file() or external_ovr.stat().st_size == 0:
        raise RuntimeError(f"gdaladdo 未生成外部金字塔：{external_ovr}")
    return external_ovr


def process_one_attempt(
    source_path_text: str,
    destination_text: str,
    index: int,
    total: int,
    resampling_name: str,
    overview_resampling_name: str,
    gdal_cache_mb: int,
    attempt: int,
    overwrite: bool,
) -> dict[str, object]:
    source_path = Path(source_path_text)
    destination = Path(destination_text)
    job_id = uuid.uuid4().hex
    temporary = destination.with_name(
        f".{destination.stem}.{job_id}.part.tif"
    )
    temporary_external_ovr = Path(str(temporary) + ".ovr")
    stashed_external_ovr = destination.with_name(
        f".{destination.name}.{job_id}.external-ovr-part"
    )
    destination_external_ovr = Path(str(destination) + ".ovr")
    lock_path = destination.with_name(destination.name + ".processing.lock")
    lock_acquired = False
    started = time.monotonic()
    filename = source_path.name
    try:
        destination.parent.mkdir(parents=True, exist_ok=True)
        acquire_output_lock(lock_path)
        lock_acquired = True
        # 可能有另一个脚本实例在本任务排队期间已经完成了同一目标。
        if not overwrite and destination.exists() and output_is_complete(destination):
            worker_status(
                index,
                total,
                filename,
                "完成（其他进程已生成，跳过）",
                final=True,
            )
            return {"status": "success", "index": index, "seconds": 0.0}
        initial_source_fingerprint = source_fingerprint(source_path)
        if attempt == 1:
            worker_status(index, total, filename, "准备处理")
        else:
            worker_status(index, total, filename, f"准备重试（第 {attempt} 次）")

        with rasterio.open(source_path, sharing=False) as source:
            if source.crs is None:
                raise ValueError("源 TIFF 没有 CRS")
            transform, width, height = calculate_default_transform(
                source.crs,
                TARGET_CRS,
                source.width,
                source.height,
                *source.bounds,
                resolution=(RESOLUTION, RESOLUTION),
            )
            dtype = source.dtypes[0]
            predictor = predictor_for(dtype)
            profile = source.profile.copy()
            # 避免继承只适用于 JPEG 等编码的源 PHOTOMETRIC 设置。
            profile.pop("photometric", None)
            profile.update(
                driver="GTiff",
                crs=TARGET_CRS,
                transform=transform,
                width=width,
                height=height,
                compress="LZW",
                predictor=predictor,
                tiled=True,
                blockxsize=BLOCK_SIZE,
                blockysize=BLOCK_SIZE,
                interleave="band",
                BIGTIFF="IF_SAFER",
            )
            resampling = getattr(Resampling, resampling_name)
            overview_resampling = getattr(Resampling, overview_resampling_name)
            factors = valid_overview_factors(width, height)
            env_options = {
                "GDAL_NUM_THREADS": "1",
                "GDAL_CACHEMAX": gdal_cache_mb,
                "GDAL_DISABLE_READDIR_ON_OPEN": "EMPTY_DIR",
                "COMPRESS_OVERVIEW": "LZW",
                "PREDICTOR_OVERVIEW": predictor,
                "INTERLEAVE_OVERVIEW": "BAND",
                "BIGTIFF_OVERVIEW": "IF_SAFER",
            }
            with rasterio.Env(**env_options):
                with rasterio.open(temporary, "w", **profile) as target:
                    for band in range(1, source.count + 1):
                        worker_status(
                            index,
                            total,
                            filename,
                            f"重投影并重采样（波段 {band}/{source.count}）",
                        )
                        nodata = source.nodatavals[band - 1]
                        reproject(
                            source=rasterio.band(source, band),
                            destination=rasterio.band(target, band),
                            src_transform=source.transform,
                            src_crs=source.crs,
                            src_nodata=nodata,
                            dst_transform=transform,
                            dst_crs=TARGET_CRS,
                            dst_nodata=nodata,
                            resampling=resampling,
                            num_threads=1,
                            init_dest_nodata=True,
                        )
                    copy_metadata(source, target)

                if factors:
                    # 此时主 TIFF 尚无内部金字塔。先用只读方式生成 .ovr，
                    # 随后把 .ovr 暂存起来，避免其影响内部金字塔的创建。
                    created_ovr = build_external_overviews(
                        temporary,
                        factors,
                        overview_resampling_name,
                        predictor,
                        index,
                        total,
                        filename,
                    )
                    os.replace(created_ovr, stashed_external_ovr)
                    worker_status(index, total, filename, "建立 TIFF 内部金字塔")
                    with rasterio.open(temporary, "r+") as target:
                        target.build_overviews(factors, overview_resampling)
                        target.update_tags(ns="rio_overview", resampling=overview_resampling_name)

        worker_status(index, total, filename, "校验源文件及发布输出")
        final_source_fingerprint = source_fingerprint(source_path)
        if final_source_fingerprint != initial_source_fingerprint:
            raise RuntimeError(
                "处理期间源 TIFF 的大小或修改时间发生变化，拒绝发布本次输出"
            )

        os.replace(temporary, destination)
        if factors:
            os.replace(stashed_external_ovr, destination_external_ovr)
        elapsed = time.monotonic() - started
        size_mb = destination.stat().st_size / (1024 * 1024)
        ovr_size_mb = (
            destination_external_ovr.stat().st_size / (1024 * 1024)
            if destination_external_ovr.exists()
            else 0.0
        )
        worker_status(
            index,
            total,
            filename,
            f"成功（{elapsed:.1f}秒，TIFF {size_mb:.1f}MB，OVR {ovr_size_mb:.1f}MB）",
            final=True,
        )
        return {"status": "success", "index": index, "seconds": elapsed}
    except BaseException as exc:
        for unfinished in (
            temporary,
            temporary_external_ovr,
            stashed_external_ovr,
        ):
            try:
                unfinished.unlink(missing_ok=True)
            except OSError:
                pass
        if isinstance(exc, (KeyboardInterrupt, SystemExit)):
            raise
        details = "".join(traceback.format_exception_only(type(exc), exc)).strip()
        worker_status(
            index,
            total,
            filename,
            f"第 {attempt} 次尝试失败：{details}",
            level=logging.ERROR,
        )
        return {"status": "failed", "index": index, "error": details}
    finally:
        if lock_acquired:
            try:
                lock_path.unlink(missing_ok=True)
            except OSError as exc:
                worker_status(
                    index,
                    total,
                    filename,
                    f"警告：无法删除锁文件：{exc}",
                    level=logging.WARNING,
                )


def process_one(
    source_path_text: str,
    destination_text: str,
    index: int,
    total: int,
    resampling_name: str,
    overview_resampling_name: str,
    retries: int,
    retry_delay: float,
    gdal_cache_mb: int,
    overwrite: bool,
) -> dict[str, object]:
    """处理一个 TIFF；临时 I/O 失败时在同一文件任务内自动重试。"""
    last_result: dict[str, object] = {
        "status": "failed",
        "index": index,
        "error": "未开始",
    }
    for attempt in range(1, retries + 2):
        last_result = process_one_attempt(
            source_path_text,
            destination_text,
            index,
            total,
            resampling_name,
            overview_resampling_name,
            gdal_cache_mb,
            attempt,
            overwrite,
        )
        if last_result["status"] == "success":
            return last_result
        if attempt <= retries:
            delay = retry_delay * (2 ** (attempt - 1))
            worker_status(
                index,
                total,
                Path(source_path_text).name,
                f"{delay:.0f}秒后重试（下一次 {attempt + 1}/{retries + 1}）",
                level=logging.WARNING,
            )
            time.sleep(delay)
    worker_status(
        index,
        total,
        Path(source_path_text).name,
        f"最终失败：{last_result.get('error', '未知错误')}",
        level=logging.ERROR,
        final=True,
    )
    return last_result


def stop_process_pool(
    executor: concurrent.futures.ProcessPoolExecutor,
    futures: list[concurrent.futures.Future],
    logger: logging.Logger,
) -> None:
    """取消排队任务并在有限时间内结束所有工作进程。"""
    for future in futures:
        future.cancel()

    processes = list(getattr(executor, "_processes", {}).values())
    executor.shutdown(wait=False, cancel_futures=True)
    logger.warning("正在终止 %d 个工作进程……", len(processes))

    for process in processes:
        try:
            if process.is_alive():
                process.terminate()
        except (OSError, ValueError):
            pass

    deadline = time.monotonic() + 6.0
    for process in processes:
        try:
            remaining = max(0.0, deadline - time.monotonic())
            process.join(timeout=remaining)
        except (OSError, ValueError):
            pass

    forced = 0
    for process in processes:
        try:
            if process.is_alive():
                process.kill()
                forced += 1
        except (AttributeError, OSError, ValueError):
            pass
    for process in processes:
        try:
            process.join(timeout=2)
        except (OSError, ValueError):
            pass

    manager_thread = getattr(executor, "_executor_manager_thread", None)
    if manager_thread is not None and manager_thread.is_alive():
        manager_thread.join(timeout=3)
    logger.warning("工作进程停止完成 | 强制结束=%d", forced)


def main() -> int:
    signal.signal(signal.SIGINT, main_interrupt_handler)
    signal.signal(signal.SIGTERM, main_interrupt_handler)
    args = parse_args()
    input_dir = args.input_dir.expanduser().resolve()
    output_dir = args.output_dir.expanduser().resolve()
    if not input_dir.is_dir():
        print(f"错误：输入目录不存在或不是目录：{input_dir}", file=sys.stderr)
        return 2
    if input_dir == output_dir:
        print("错误：输入目录与输出目录不能相同。", file=sys.stderr)
        return 2
    if args.workers < 1:
        print("错误：--workers 必须大于等于 1。", file=sys.stderr)
        return 2
    if args.retries < 0:
        print("错误：--retries 不能小于 0。", file=sys.stderr)
        return 2
    if args.retry_delay < 0:
        print("错误：--retry-delay 不能小于 0。", file=sys.stderr)
        return 2
    if args.gdal_cache_mb < 32:
        print("错误：--gdal-cache-mb 不能小于 32。", file=sys.stderr)
        return 2
    if shutil.which("gdaladdo") is None:
        print(
            "错误：系统中找不到 gdaladdo，无法生成 ArcGIS 外部 .ovr 金字塔。",
            file=sys.stderr,
        )
        return 2

    logger, log_path = setup_main_logger(output_dir)
    sources = scan_tifs(input_dir, output_dir)
    total = len(sources)
    logger.info("任务开始 | 输入=%s | 输出=%s", input_dir, output_dir)
    logger.info(
        "配置 | TIFF=%d | 子进程=%d | 目标=CGCS2000_Albers | 分辨率=%.1fm | LZW | 分块=%dx%d",
        total, args.workers, RESOLUTION, BLOCK_SIZE, BLOCK_SIZE,
    )
    logger.info(
        "算法 | 主影像=%s | 内部+ArcGIS外部金字塔=%s | 最大256倍 | 单进程 GDAL 线程=1 | 日志=%s",
        args.resampling, args.overview_resampling, log_path,
    )
    logger.info(
        "稳定性 | 每文件最多尝试=%d | 首次重试等待=%.1f秒 | 每进程GDAL缓存=%dMiB | 输出独占锁=开启",
        args.retries + 1,
        args.retry_delay,
        args.gdal_cache_mb,
    )
    logger.info("实时状态文件：%s", output_dir / "当前处理状态.txt")
    if not sources:
        logger.warning("没有找到 TIFF，任务结束。")
        return 0

    jobs: list[tuple[int, Path, Path]] = []
    skipped = 0
    for index, source in enumerate(sources, start=1):
        destination = output_dir / source.relative_to(input_dir)
        if destination.exists() and not args.overwrite and output_is_complete(destination):
            skipped += 1
            continue
        jobs.append((index, source, destination))

    succeeded = 0
    failed = 0
    started = time.monotonic()
    context = mp.get_context("spawn")
    log_queue = context.Queue()
    queue_listener = logging.handlers.QueueListener(
        log_queue, *logger.handlers, respect_handler_level=True
    )
    queue_listener.start()
    executor: concurrent.futures.ProcessPoolExecutor | None = None
    all_futures: list[concurrent.futures.Future] = []
    interrupted = False
    try:
        executor = concurrent.futures.ProcessPoolExecutor(
            max_workers=min(args.workers, max(1, len(jobs))),
            mp_context=context,
            initializer=init_worker,
            initargs=(log_queue,),
        )
        futures = {
            executor.submit(
                process_one,
                str(source),
                str(destination),
                index,
                total,
                args.resampling,
                args.overview_resampling,
                args.retries,
                args.retry_delay,
                args.gdal_cache_mb,
                args.overwrite,
            ): (index, source)
            for index, source, destination in jobs
        }
        all_futures = list(futures)
        for future in concurrent.futures.as_completed(futures):
            index, source = futures[future]
            try:
                result = future.result()
            except Exception as exc:
                failed += 1
                logger.error("[%d/%d] 子进程异常退出 | %s | %s", index, total, source.name, exc)
            else:
                if result["status"] == "success":
                    succeeded += 1
                else:
                    failed += 1
        executor.shutdown(wait=True)
        executor = None
    except KeyboardInterrupt:
        interrupted = True
        # 防止用户再次按 Ctrl+C 打断清理，造成 multiprocessing 在 atexit 中等待。
        signal.signal(signal.SIGINT, signal.SIG_IGN)
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        logger.warning("收到中断，正在取消任务并结束所有 Python/GDAL 子进程。")
        if executor is not None:
            stop_process_pool(executor, all_futures, logger)
            executor = None
    finally:
        if executor is not None:
            executor.shutdown(wait=False, cancel_futures=True)
        queue_listener.stop()
        log_queue.close()
        log_queue.join_thread()

    if interrupted:
        logger.warning("任务已中断，子进程清理完成。")
        return 130

    elapsed = time.monotonic() - started
    logger.info(
        "任务结束 | 总数=%d 成功=%d 失败=%d 跳过=%d | 总用时=%.1f 秒",
        total, succeeded, failed, skipped, elapsed,
    )
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
