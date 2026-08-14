# -*- coding: utf-8 -*-
"""分两阶段为 TIF 构建外部 OVR 和内部金字塔。

主进程只负责扫描、调度和汇总。每个文件、每个阶段都由一个一次性的独立
Python 子进程处理；同一子进程不会继续处理下一个文件。
"""

from __future__ import annotations

import argparse
import ctypes
import ctypes.util
import json
import logging
import os
import subprocess
import sys
import time
from collections import Counter
from dataclasses import asdict, dataclass
from datetime import timedelta
from pathlib import Path
from typing import Iterator

os.environ.setdefault("OPENBLAS_NUM_THREADS", "1")
os.environ.setdefault("OMP_NUM_THREADS", "1")
os.environ.setdefault("MKL_NUM_THREADS", "1")

try:
    import rasterio
    from rasterio.enums import Resampling
except ImportError as exc:  # pragma: no cover
    raise SystemExit("缺少依赖，请先安装 rasterio") from exc


DEFAULT_TIF_DIR = r"/mnt/data/4np/0.5m_转投影"
DEFAULT_RECURSIVE = False
DEFAULT_RESAMPLING = "nearest"
DEFAULT_MAX_FACTOR = 256
DEFAULT_WORKERS = min(8, os.cpu_count() or 4)
DEFAULT_GDAL_CACHE_MB = 512
MAX_FACTOR = 256
TIF_SUFFIXES = {".tif", ".tiff"}
LOGGER = logging.getLogger("pyramid_builder")


@dataclass(frozen=True)
class Result:
    path: str
    stage: str
    status: str
    elapsed: float
    detail: str


def convert_network_path(path: str | None) -> str | None:
    """把已知 Windows UNC 共享路径转换为服务器上的 Linux 挂载路径。"""
    if path is None:
        return None
    normalized = str(path).strip().replace("\\", "/")
    if not normalized:
        return normalized
    share_mounts = {
        "data": "/media/cangling/nas_folder",
        "新建卷": "/media/cangling/xinjianjuan",
        "datadisk2": "/media/cangling/EAGET",
        "新加卷": "/media/cangling/xinjiajuan",
    }
    for host_index in range(1, 256):
        for share_name, linux_prefix in share_mounts.items():
            for subnet in ("169.254.51", "10.10.10"):
                for prefix in (
                    f"//{subnet}.{host_index}/{share_name}",
                    f"/{subnet}.{host_index}/{share_name}",
                    f"{subnet}.{host_index}/{share_name}",
                ):
                    if normalized == prefix:
                        return linux_prefix
                    if normalized.startswith(prefix + "/"):
                        return linux_prefix + normalized[len(prefix) :]
    return normalized


def positive_int(value: str) -> int:
    number = int(value)
    if number < 1:
        raise argparse.ArgumentTypeError("必须是大于 0 的整数")
    return number


def max_factor(value: str) -> int:
    number = positive_int(value)
    if number > MAX_FACTOR or number < 2 or number & (number - 1):
        raise argparse.ArgumentTypeError("最高倍数必须是 2 到 256 之间的 2 的幂")
    return number


def factors_to(maximum: int) -> list[int]:
    result: list[int] = []
    factor = 2
    while factor <= maximum:
        result.append(factor)
        factor *= 2
    return result


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="先构建外部 OVR，再构建 TIF 内部金字塔。")
    parser.add_argument("path", nargs="?", help="单个 TIF 或文件夹")
    parser.add_argument("--input", "--tif-file", "--tif-dir", dest="input_path")
    parser.add_argument("--recursive", action="store_true", default=DEFAULT_RECURSIVE)
    parser.add_argument(
        "--resampling",
        choices=sorted(name for name in Resampling.__members__ if name != "rms"),
        default=DEFAULT_RESAMPLING,
    )
    parser.add_argument("--max-factor", type=max_factor, default=DEFAULT_MAX_FACTOR)
    parser.add_argument("--workers", type=positive_int, default=DEFAULT_WORKERS)
    parser.add_argument("--gdal-cache-mb", type=positive_int, default=DEFAULT_GDAL_CACHE_MB)
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--log-file")

    # 以下参数只供本脚本创建的一次性子进程使用。
    parser.add_argument("--worker", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--stage", choices=("external", "internal"), help=argparse.SUPPRESS)
    parser.add_argument("--file", help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.path and args.input_path:
        parser.error("位置参数 path 和 --input 只能使用一个")
    args.input_path = args.input_path or args.path or DEFAULT_TIF_DIR
    if args.worker and (not args.stage or not args.file):
        parser.error("子进程缺少 --stage 或 --file")
    return args


def setup_logging(log_file: str | None) -> None:
    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stdout)]
    if log_file:
        log_path = Path(log_file).expanduser()
        log_path.parent.mkdir(parents=True, exist_ok=True)
        handlers.append(logging.FileHandler(log_path, encoding="utf-8"))
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
        handlers=handlers,
        force=True,
    )


def iter_tifs(folder: Path, recursive: bool) -> list[Path]:
    iterator = folder.rglob("*") if recursive else folder.iterdir()
    return sorted(
        (path for path in iterator if path.is_file() and path.suffix.lower() in TIF_SUFFIXES),
        key=lambda path: str(path).casefold(),
    )


def resolve_input_paths(input_path: Path, recursive: bool) -> list[Path]:
    if input_path.is_file():
        if input_path.suffix.lower() not in TIF_SUFFIXES:
            raise ValueError(f"输入文件不是 .tif/.tiff：{input_path}")
        return [input_path]
    if input_path.is_dir():
        return iter_tifs(input_path, recursive)
    raise FileNotFoundError(f"输入路径不存在或类型不受支持：{input_path}")


def usable_levels(path: Path, maximum: int) -> list[int]:
    with rasterio.open(path, "r") as dataset:
        longest = max(dataset.width, dataset.height)
        return [factor for factor in factors_to(maximum) if longest // factor >= 1]


def find_gdal_library() -> str:
    package_dir = Path(rasterio.__file__).resolve().parent
    patterns = (
        package_dir.parent / "rasterio.libs" / "gdal-*.dll",
        package_dir.parent / "rasterio.libs" / "libgdal-*.so*",
        package_dir / ".libs" / "libgdal*.so*",
    )
    for pattern in patterns:
        matches = list(pattern.parent.glob(pattern.name))
        if matches:
            return str(matches[0])
    found = ctypes.util.find_library("gdal")
    if found:
        return found
    raise RuntimeError("找不到 GDAL 动态库，无法创建外部 OVR")


def configure_gdal_api(gdal: ctypes.CDLL) -> None:
    gdal.GDALAllRegister.argtypes = []
    gdal.GDALOpenEx.argtypes = [ctypes.c_char_p, ctypes.c_uint, ctypes.POINTER(ctypes.c_char_p), ctypes.POINTER(ctypes.c_char_p), ctypes.POINTER(ctypes.c_char_p)]
    gdal.GDALOpenEx.restype = ctypes.c_void_p
    gdal.GDALBuildOverviews.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_int, ctypes.POINTER(ctypes.c_int), ctypes.c_int, ctypes.POINTER(ctypes.c_int), ctypes.c_void_p, ctypes.c_void_p]
    gdal.GDALBuildOverviews.restype = ctypes.c_int
    gdal.GDALClose.argtypes = [ctypes.c_void_p]
    gdal.CPLGetLastErrorMsg.argtypes = []
    gdal.CPLGetLastErrorMsg.restype = ctypes.c_char_p
    gdal.GDALAllRegister()


def clear_internal_overviews(gdal: ctypes.CDLL, path: Path) -> None:
    """清除旧内部层级，使只读打开时可以生成真正的外部 OVR。"""
    dataset = gdal.GDALOpenEx(os.fsencode(path), 0x03, None, None, None)  # RASTER | UPDATE
    if not dataset:
        raise RuntimeError(f"GDAL 无法更新打开：{path}")
    try:
        error_code = gdal.GDALBuildOverviews(dataset, b"NONE", 0, None, 0, None, None, None)
        if error_code:
            message = (gdal.CPLGetLastErrorMsg() or b"Cannot remove internal overviews").decode("utf-8", "replace")
            raise RuntimeError(message)
    finally:
        gdal.GDALClose(dataset)


def build_external(path: Path, levels: list[int], resampling: str, force: bool, dry_run: bool) -> Result:
    started = time.perf_counter()
    ovr_path = Path(str(path) + ".ovr")
    if ovr_path.exists() and not force:
        return Result(str(path), "external", "skip_exists", time.perf_counter() - started, "外部 OVR 已存在")
    if dry_run:
        return Result(str(path), "external", "dry_run", time.perf_counter() - started, f"levels={levels}")
    if not levels:
        return Result(str(path), "external", "skip_small", time.perf_counter() - started, "影像尺寸不足")

    gdal = ctypes.CDLL(find_gdal_library())
    configure_gdal_api(gdal)
    if force and ovr_path.exists():
        ovr_path.unlink()
    # GDAL 不允许内部和外部 overview 同时创建。旧内部层级在本阶段清除，
    # 随后的 internal 阶段会按相同目标完整重建。
    clear_internal_overviews(gdal, path)

    dataset = gdal.GDALOpenEx(os.fsencode(path), 0x02, None, None, None)  # GDAL_OF_RASTER，保持只读以生成 .ovr
    if not dataset:
        raise RuntimeError(f"GDAL 无法只读打开：{path}")
    level_array = (ctypes.c_int * len(levels))(*levels)
    try:
        error_code = gdal.GDALBuildOverviews(
            dataset,
            resampling.upper().encode("ascii"),
            len(levels),
            level_array,
            0,
            None,
            None,
            None,
        )
        if error_code:
            message = (gdal.CPLGetLastErrorMsg() or b"GDALBuildOverviews failed").decode("utf-8", "replace")
            raise RuntimeError(message)
    finally:
        gdal.GDALClose(dataset)
    if not ovr_path.exists():
        raise RuntimeError("GDAL 未生成外部 .ovr 文件")
    return Result(str(path), "external", "built", time.perf_counter() - started, f"levels={levels}")


def build_internal(path: Path, levels: list[int], resampling: str, force: bool, dry_run: bool, cache_mb: int) -> Result:
    started = time.perf_counter()
    if not levels:
        return Result(str(path), "internal", "skip_small", time.perf_counter() - started, "影像尺寸不足")
    if dry_run:
        return Result(str(path), "internal", "dry_run", time.perf_counter() - started, f"levels={levels}")

    # 暂时移开外部 OVR，确保 GDAL 把本阶段的层级写进 TIF，而不是更新 sidecar。
    ovr_path = Path(str(path) + ".ovr")
    held_ovr = Path(str(ovr_path) + f".hold-{os.getpid()}")
    if ovr_path.exists():
        ovr_path.replace(held_ovr)
    try:
        with rasterio.Env(GDAL_NUM_THREADS="1", GDAL_CACHEMAX=cache_mb * 1024 * 1024):
            with rasterio.open(path, "r+") as dataset:
                old_levels = list(dataset.overviews(1)) if dataset.count else []
                if old_levels and not force and set(levels).issubset(old_levels):
                    return Result(str(path), "internal", "skip_exists", time.perf_counter() - started, f"levels={old_levels}")
                dataset.build_overviews(levels, Resampling[resampling])
                dataset.update_tags(ns="rio_overview", resampling=resampling)
    finally:
        if held_ovr.exists():
            held_ovr.replace(ovr_path)
    return Result(str(path), "internal", "built", time.perf_counter() - started, f"levels={levels}")


def worker_main(args: argparse.Namespace) -> int:
    path = Path(args.file)
    try:
        levels = usable_levels(path, args.max_factor)
        if args.stage == "external":
            result = build_external(path, levels, args.resampling, args.force, args.dry_run)
        else:
            result = build_internal(path, levels, args.resampling, args.force, args.dry_run, args.gdal_cache_mb)
    except Exception as exc:  # noqa: BLE001
        result = Result(str(path), args.stage, "fail", 0.0, f"{type(exc).__name__}: {exc}")
    print(json.dumps(asdict(result), ensure_ascii=False), flush=True)
    return 1 if result.status == "fail" else 0


def child_command(path: Path, stage: str, args: argparse.Namespace) -> list[str]:
    command = [
        sys.executable,
        "-u",
        str(Path(__file__).resolve()),
        "--worker",
        "--stage",
        stage,
        "--file",
        str(path),
        "--max-factor",
        str(args.max_factor),
        "--resampling",
        args.resampling,
        "--gdal-cache-mb",
        str(args.gdal_cache_mb),
    ]
    if args.force:
        command.append("--force")
    if args.dry_run:
        command.append("--dry-run")
    return command


def run_stage(paths: list[Path], stage: str, args: argparse.Namespace) -> Iterator[Result]:
    waiting = iter(paths)
    running: list[tuple[Path, subprocess.Popen[str]]] = []

    def start_next() -> bool:
        try:
            path = next(waiting)
        except StopIteration:
            return False
        env = os.environ.copy()
        env["PYTHONUNBUFFERED"] = "1"
        process = subprocess.Popen(
            child_command(path, stage, args),
            cwd=Path(__file__).resolve().parent,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=env,
        )
        running.append((path, process))
        return True

    for _ in range(min(args.workers, len(paths))):
        start_next()
    while running:
        completed_index = next((index for index, (_, process) in enumerate(running) if process.poll() is not None), None)
        if completed_index is None:
            time.sleep(0.05)
            continue
        path, process = running.pop(completed_index)
        stdout, stderr = process.communicate()
        try:
            payload = json.loads(stdout.strip().splitlines()[-1])
            result = Result(**payload)
        except (IndexError, json.JSONDecodeError, TypeError):
            detail = stderr.strip() or stdout.strip() or f"子进程异常退出（代码 {process.returncode}）"
            result = Result(str(path), stage, "fail", 0.0, detail)
        yield result
        start_next()


def format_seconds(seconds: float) -> str:
    return str(timedelta(seconds=max(0, round(seconds))))


def main() -> int:
    args = parse_args()
    if args.worker:
        return worker_main(args)

    setup_logging(args.log_file)
    converted = convert_network_path(args.input_path)
    if not converted:
        LOGGER.error("请提供 TIF 文件或文件夹")
        return 2
    input_path = Path(converted).expanduser().resolve()
    try:
        paths = resolve_input_paths(input_path, args.recursive)
    except (FileNotFoundError, ValueError) as exc:
        LOGGER.error("%s", exc)
        return 2
    if not paths:
        LOGGER.warning("没有找到 TIF/TIFF：%s", input_path)
        return 0

    levels = factors_to(args.max_factor)
    LOGGER.info("找到 %d 个 TIF；目标倍数：%s", len(paths), ", ".join(map(str, levels)))
    LOGGER.info("阶段顺序：先构建全部外部 OVR，再构建全部内部金字塔")
    started = time.perf_counter()
    summaries: dict[str, Counter[str]] = {}
    failed = False

    for stage, label in (("external", "外部 OVR"), ("internal", "内部 TIF")):
        LOGGER.info("开始 %s 阶段", label)
        summary: Counter[str] = Counter()
        for completed, result in enumerate(run_stage(paths, stage, args), start=1):
            summary[result.status] += 1
            failed = failed or result.status == "fail"
            log_level = logging.ERROR if result.status == "fail" else logging.INFO
            LOGGER.log(
                log_level,
                "[%s %d/%d] %s；%s；%.2f 秒；%s",
                label,
                completed,
                len(paths),
                Path(result.path).name,
                result.status,
                result.elapsed,
                result.detail,
            )
        summaries[stage] = summary
        LOGGER.info("%s 阶段完成：%s", label, "，".join(f"{key}={value}" for key, value in sorted(summary.items())))

    LOGGER.info("全部完成，总耗时 %s", format_seconds(time.perf_counter() - started))
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
