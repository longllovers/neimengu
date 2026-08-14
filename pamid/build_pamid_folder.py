# -*- coding: utf-8 -*-
"""
Build internal pyramids/overviews for all TIF images in a folder.

说明：内部金字塔会写入 TIF 文件本身，但不会修改原始像元值。程序会跳过已经
存在金字塔的影像；如果影像打不开或构建失败，会记录日志并继续处理下一个。
"""

from __future__ import annotations

import argparse
import logging
from pathlib import Path
from typing import Iterable, List, Sequence

try:
    import rasterio
    from rasterio.enums import Resampling
except ImportError as exc:  # pragma: no cover
    raise SystemExit("缺少依赖，请先安装：rasterio tqdm") from exc

try:
    from tqdm import tqdm
except ImportError:  # pragma: no cover
    tqdm = None


# 可以直接在这里填写路径；命令行参数优先。
DEFAULT_TIF_DIR = r"/mnt/nas_data/县级影像/2m挑出影像/按县组织影像/150425克什克腾旗/150425"
DEFAULT_RECURSIVE = False
DEFAULT_RESAMPLING = "nearest"
DEFAULT_MIN_SIZE = 256
DEFAULT_FORCE = False

TIF_SUFFIXES = {".tif", ".tiff"}
LOGGER = logging.getLogger("build_pamid_folder")


def convert_network_path(path: str | None) -> str | None:
    if path is None:
        return path

    path = str(path).strip().replace("\\", "/")
    if not path:
        return path

    prefix_mapping = (
        ("//10.10.10.11/data", "/mnt/nas_data"),
        ("//10.10.10.10/4np_share", "/mnt/data/4np/"),
        ("//10.10.10.10/nas_data", "/mnt/nas_data"),
    )
    for windows_prefix, linux_prefix in prefix_mapping:
        if path == windows_prefix:
            return linux_prefix
        if path.startswith(windows_prefix + "/"):
            return linux_prefix.rstrip("/") + path[len(windows_prefix):]
    return path



def setup_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="为文件夹中的 TIF 影像构建内部金字塔。")
    parser.add_argument("--tif-dir", default=DEFAULT_TIF_DIR, help="TIF 影像文件夹。")
    parser.add_argument("--recursive", action="store_true", default=DEFAULT_RECURSIVE, help="递归处理子文件夹。")
    parser.add_argument(
        "--resampling",
        default=DEFAULT_RESAMPLING,
        choices=sorted(name for name in Resampling.__members__ if not name.startswith("rms")),
        help="金字塔重采样方式，默认 nearest。分类/地块类影像建议 nearest，普通影像可用 average。",
    )
    parser.add_argument("--min-size", type=int, default=DEFAULT_MIN_SIZE, help="最小金字塔层尺寸阈值，默认 256。")
    parser.add_argument("--force", action="store_true", default=DEFAULT_FORCE, help="即使已有金字塔也重新构建。")
    parser.add_argument("--dry-run", action="store_true", help="只显示将要处理的影像，不实际写入。")
    return parser.parse_args()


def progress_iter(items: Sequence[Path], desc: str) -> Iterable[Path]:
    if tqdm is not None:
        return tqdm(items, total=len(items), desc=desc, unit="张")
    total = len(items)

    def generator() -> Iterable[Path]:
        for index, item in enumerate(items, start=1):
            LOGGER.info("%s %s/%s：%s", desc, index, total, item.name)
            yield item

    return generator()


def iter_tifs(tif_dir: Path, recursive: bool) -> List[Path]:
    iterator = tif_dir.rglob("*") if recursive else tif_dir.iterdir()
    return sorted(path for path in iterator if path.is_file() and path.suffix.lower() in TIF_SUFFIXES)


def overview_levels(width: int, height: int, min_size: int) -> List[int]:
    max_size = max(width, height)
    levels: List[int] = []
    level = 2
    while max_size // level >= min_size:
        levels.append(level)
        level *= 2
    return levels


def existing_overview_count(dataset) -> int:
    if dataset.count <= 0:
        return 0
    return len(dataset.overviews(1))


def build_internal_pyramid(tif_path: Path, resampling: Resampling, min_size: int, force: bool, dry_run: bool) -> str:
    try:
        with rasterio.open(tif_path, "r+") as dataset:
            if dataset.width <= 0 or dataset.height <= 0 or dataset.count <= 0:
                LOGGER.warning("影像尺寸或波段异常，跳过：%s", tif_path)
                return "fail"

            old_overviews = existing_overview_count(dataset)
            if old_overviews > 0 and not force:
                LOGGER.info("已有金字塔，跳过：%s，层数 %s", tif_path.name, old_overviews)
                return "skip_exists"

            levels = overview_levels(dataset.width, dataset.height, min_size)
            if not levels:
                LOGGER.info("影像尺寸较小，无需构建金字塔：%s，尺寸 %sx%s", tif_path.name, dataset.width, dataset.height)
                return "skip_small"

            if dry_run:
                LOGGER.info("dry-run：将为 %s 构建内部金字塔 levels=%s", tif_path, levels)
                return "dry_run"

            LOGGER.info("开始构建内部金字塔：%s，levels=%s，resampling=%s", tif_path.name, levels, resampling.name)
            dataset.build_overviews(levels, resampling)
            dataset.update_tags(ns="rio_overview", resampling=resampling.name)
            LOGGER.info("完成：%s", tif_path)
            return "built"
    except Exception as exc:  # noqa: BLE001
        LOGGER.error("构建金字塔失败：%s；原因：%s", tif_path, exc)
        return "fail"


def main() -> int:
    setup_logging()
    args = parse_args()

    if not args.tif_dir:
        LOGGER.error("请提供 --tif-dir，或在代码中填写 DEFAULT_TIF_DIR。")
        return 2

    converted_tif_dir = convert_network_path(args.tif_dir)
    assert converted_tif_dir is not None
    if converted_tif_dir != args.tif_dir:
        LOGGER.info("路径转换：%s -> %s", args.tif_dir, converted_tif_dir)

    tif_dir = Path(converted_tif_dir).expanduser().resolve()
    if not tif_dir.exists() or not tif_dir.is_dir():
        LOGGER.error("TIF 文件夹不存在：%s", tif_dir)
        return 2

    tif_paths = iter_tifs(tif_dir, args.recursive)
    if not tif_paths:
        LOGGER.warning("没有找到 TIF 影像：%s", tif_dir)
        return 0

    resampling = Resampling[args.resampling]
    LOGGER.info("TIF 文件夹：%s", tif_dir)
    LOGGER.info("影像数量：%s 张；递归：%s；重采样：%s；force：%s", len(tif_paths), args.recursive, resampling.name, args.force)
    LOGGER.info("注意：内部金字塔会写入 TIF 文件本身，但不会修改原始像元值。")

    summary: dict[str, int] = {}
    for index, tif_path in enumerate(progress_iter(tif_paths, "构建金字塔"), start=1):
        LOGGER.info("总进度 [%s/%s]：%s", index, len(tif_paths), tif_path.name)
        status = build_internal_pyramid(
            tif_path=tif_path,
            resampling=resampling,
            min_size=args.min_size,
            force=args.force,
            dry_run=args.dry_run,
        )
        summary[status] = summary.get(status, 0) + 1

    LOGGER.info("处理完成。")
    for status, count in sorted(summary.items()):
        LOGGER.info("%s: %s", status, count)
    return 0 if summary.get("fail", 0) == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
