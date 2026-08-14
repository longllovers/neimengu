#!/usr/bin/env python
"""每城市独立进程，生成模型、人工、人工掩膜 SHP 后执行接缝补充。"""

from __future__ import annotations

import argparse
from concurrent.futures import ProcessPoolExecutor, as_completed
import multiprocessing as mp
import os
from pathlib import Path
import subprocess
import sys
import tomllib


ROOT = Path(__file__).resolve().parent
DEFAULT_CONFIG = ROOT / "sqlite_pipeline.toml"
MODEL_SCRIPT = ROOT / "apply_sqlite_alignment_to_shp.py"
PERSON_SCRIPT = ROOT / "apply_sqlite_alignment_to_person_shp.py"
EXTRACT_MASK_SCRIPT = ROOT / "extract_mask_shp_from_tile_index.py"
MASK_SCRIPT = ROOT / "apply_sqlite_alignment_to_mask_shp.py"
FILL_SCRIPT = ROOT / "fill_person_shp_gaps_from_model.py"


def configured_cities(config: Path) -> list[str]:
    with config.open("rb") as stream:
        document = tomllib.load(stream)
    mosaic = document.get("mosaic")
    if not isinstance(mosaic, dict):
        raise ValueError(f"配置文件缺少 [mosaic]：{config}")
    value = mosaic.get("cities", [])
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ValueError("[mosaic].cities 必须是城市字符串或字符串数组。")
    return list(dict.fromkeys(item.strip() for item in value if item.strip()))


def stage_command(
    script: Path,
    config: Path,
    city: str,
    check_only: bool,
    overwrite: bool,
) -> list[str]:
    command = [
        sys.executable,
        str(script),
        "--config",
        str(config),
        "--cities",
        city,
    ]
    if check_only:
        command.append("--check-only")
    if overwrite:
        command.append("--overwrite")
    return command


def run_city_pipeline(
    config: Path,
    city: str,
    position: int,
    total: int,
    check_only: bool,
    overwrite: bool,
) -> None:
    """一个城市进程内并行启动模型和人工两个独立 Python 进程。"""
    prefix = f"[城市 {position}/{total}][{city}]"
    print(
        f"\n{prefix} 城市流水线进程启动，PID={os.getpid()}",
        flush=True,
    )
    model_command = stage_command(
        MODEL_SCRIPT, config, city, check_only, overwrite
    )
    person_command = stage_command(
        PERSON_SCRIPT, config, city, check_only, overwrite
    )

    print(f"{prefix} 同时启动模型 SHP 与人工 SHP 合成进程。", flush=True)
    model_process = subprocess.Popen(model_command, cwd=ROOT)
    person_process = subprocess.Popen(person_command, cwd=ROOT)
    print(
        f"{prefix} 模型 PID={model_process.pid}；人工 PID={person_process.pid}",
        flush=True,
    )

    model_code = model_process.wait()
    person_code = person_process.wait()
    failures = []
    if model_code != 0:
        failures.append(f"模型 SHP 进程退出码={model_code}")
    if person_code != 0:
        failures.append(f"人工 SHP 进程退出码={person_code}")
    if failures:
        raise RuntimeError(f"{prefix} " + "；".join(failures))

    print(
        f"{prefix} 模型和人工 SHP 均完成，开始在独立 Python 进程中"
        "矫正、合成并按市界裁剪掩膜。",
        flush=True,
    )
    subprocess.run(
        stage_command(MASK_SCRIPT, config, city, check_only, overwrite),
        cwd=ROOT,
        check=True,
    )
    print(f"{prefix} 人工覆盖掩膜完成，开始执行模型减掩膜补充。", flush=True)
    subprocess.run(
        stage_command(FILL_SCRIPT, config, city, check_only, overwrite),
        cwd=ROOT,
        check=True,
    )
    print(f"{prefix} 模型、人工、掩膜、补缝四阶段全部完成。", flush=True)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument(
        "--cities",
        nargs="*",
        help="可选；覆盖配置文件中的城市列表",
    )
    parser.add_argument(
        "--check-only",
        action="store_true",
        help="所有城市和阶段均只检查输入，不生成成果",
    )
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="所有城市和阶段均允许覆盖已有成果",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    config = args.config.resolve()
    if not config.is_file():
        raise FileNotFoundError(f"配置文件不存在：{config}")
    for script in (
        MODEL_SCRIPT,
        PERSON_SCRIPT,
        EXTRACT_MASK_SCRIPT,
        MASK_SCRIPT,
        FILL_SCRIPT,
    ):
        if not script.is_file():
            raise FileNotFoundError(f"阶段脚本不存在：{script}")

    cities = (
        configured_cities(config)
        if args.cities is None
        else list(dict.fromkeys(city.strip() for city in args.cities if city.strip()))
    )
    if not cities:
        raise ValueError("至少需要一个城市。")

    extract_command = [
        sys.executable,
        str(EXTRACT_MASK_SCRIPT),
        "--config",
        str(config),
        "--cities",
        *cities,
    ]
    if args.check_only:
        extract_command.append("--check-only")
    if args.overwrite:
        extract_command.append("--overwrite")
    print(
        f"先统一提取 {len(cities)} 个城市涉及的唯一 5 万分幅掩膜；"
        "默认最多使用 32 个 Python 子进程。",
        flush=True,
    )
    subprocess.run(extract_command, cwd=ROOT, check=True)

    print(
        f"SHP 城市多进程模式：{len(cities)} 个城市，"
        f"启动 {len(cities)} 个城市子进程；"
        "每市再同时启动模型和人工两个 Python 子进程。",
        flush=True,
    )
    failures: list[tuple[str, Exception]] = []
    with ProcessPoolExecutor(
        max_workers=len(cities),
        mp_context=mp.get_context("spawn"),
    ) as executor:
        futures = {
            executor.submit(
                run_city_pipeline,
                config,
                city,
                position,
                len(cities),
                args.check_only,
                args.overwrite,
            ): (position, city)
            for position, city in enumerate(cities, start=1)
        }
        for future in as_completed(futures):
            position, city = futures[future]
            try:
                future.result()
                print(
                    f"[城市 {position}/{len(cities)}][{city}] 城市任务完成",
                    flush=True,
                )
            except Exception as exc:
                failures.append((city, exc))
                print(
                    f"[城市 {position}/{len(cities)}][{city}] 城市任务失败：{exc}",
                    file=sys.stderr,
                    flush=True,
                )

    if failures:
        detail = "；".join(f"{city}: {error}" for city, error in failures)
        raise RuntimeError(f"{len(failures)} 个城市处理失败：{detail}")
    print(f"全部 {len(cities)} 个城市 SHP 流水线处理完成。", flush=True)


if __name__ == "__main__":
    main()
