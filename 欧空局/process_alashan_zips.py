#!/usr/bin/env python3
"""单独处理阿拉善盟目录中已有的 Sentinel-2 L2A SAFE ZIP。

处理流程：
1. 从 ZIP 中抽取 B02/B03/B04/B08 四个 10 米波段，生成 GeoTIFF；
2. 按市界识别、合并并裁剪市级影像；
3. 按县界裁剪县级影像。

默认输出：
    F:\\sentinel_data
    F:\\city_data
    F:\\country_data

原始 ZIP 和同名 TXT 不会被删除；已有结果默认跳过。
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path


BASE_DIR = Path(__file__).resolve().parent
ZIP_DIR = BASE_DIR / "temp_data" / "阿拉善盟"
SENTINEL_OUTPUT_DIR = Path(r"F:\sentinel_data")
CITY_OUTPUT_DIR = Path(r"F:\city_data")
COUNTRY_OUTPUT_DIR = Path(r"F:\country_data")
CITY_LAYER = BASE_DIR / "00市边界" / "15_市边界.shp"
COUNTY_LAYER = BASE_DIR / "00县边界" / "15_县边界.shp"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="处理 temp_data/阿拉善盟 中的 SAFE ZIP，生成瓦片、市级和县级 TIFF"
    )
    parser.add_argument(
        "--max-workers",
        type=int,
        default=5,
        help="市级合并和县级裁剪的最大并发数（默认：5）",
    )
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="覆盖已经存在的 TIFF 及市、县级结果",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="只检查 ZIP 和空间匹配关系，不生成文件",
    )
    return parser.parse_args()


def require_path(path: Path, description: str) -> None:
    if not path.exists():
        raise FileNotFoundError(f"{description}不存在：{path}")


def main() -> int:
    args = parse_args()
    if args.max_workers < 1:
        raise ValueError("--max-workers 必须大于或等于 1")

    pipeline = BASE_DIR / "process_zip_pipeline.py"
    require_path(pipeline, "处理流水线脚本")
    require_path(ZIP_DIR, "ZIP 输入目录")
    require_path(CITY_LAYER, "市界文件")
    require_path(COUNTY_LAYER, "县界文件")

    command = [
        sys.executable,
        "-u",
        str(pipeline),
        "--zip-dir",
        str(ZIP_DIR),
        "--sentinel-root",
        str(SENTINEL_OUTPUT_DIR),
        "--city-root",
        str(CITY_OUTPUT_DIR),
        "--country-root",
        str(COUNTRY_OUTPUT_DIR),
        "--city-layer",
        str(CITY_LAYER),
        "--county-layer",
        str(COUNTY_LAYER),
        "--only-city",
        "阿拉善盟",
        "--max-workers",
        str(args.max_workers),
        "--keep-skipped",
    ]
    if args.overwrite:
        command.append("--overwrite")
    if args.dry_run:
        command.append("--dry-run")

    print("阿拉善盟 ZIP 专用处理任务", flush=True)
    print(f"ZIP 输入：{ZIP_DIR}", flush=True)
    print(f"瓦片输出：{SENTINEL_OUTPUT_DIR}", flush=True)
    print(f"市级输出：{CITY_OUTPUT_DIR}", flush=True)
    print(f"县级输出：{COUNTRY_OUTPUT_DIR}", flush=True)
    print(flush=True)

    completed = subprocess.run(command, cwd=BASE_DIR)
    return completed.returncode


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("\n用户已停止。")
        raise SystemExit(130)
    except Exception as exc:
        print(f"\n处理失败：{exc}")
        raise SystemExit(1)
