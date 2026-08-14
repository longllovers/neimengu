import argparse
import subprocess
import sys
import time
from pathlib import Path
import os 
import shutil 

BASE_DIR = Path(__file__).resolve().parent
dirs_path = [BASE_DIR / "01生成样方", BASE_DIR / "02参考真值",
              BASE_DIR / "03测量值", BASE_DIR / "04评价精度结果"]

# 删除文件夹
for dir_path in dirs_path:
    if dir_path.exists():
        shutil.rmtree(dir_path)
        print(f"已删除文件夹：{dir_path}")
    else:
        print(f"无需删除，文件夹不存在：{dir_path}")


def run_cmd(cmd):
    start_time = time.perf_counter()
    print(f"\n========== 正在运行：{' '.join(cmd)} ==========")
    print(f"工作目录：{BASE_DIR}")

    result = subprocess.run(
        cmd,
        cwd=BASE_DIR,
        text=True
    )

    elapsed = time.perf_counter() - start_time
    if result.returncode != 0:
        print(f"\n❌ 运行失败：{' '.join(cmd)}")
        print(f"错误码：{result.returncode}")
        print(f"失败前用时：{elapsed:.1f}s")
        sys.exit(result.returncode)

    print(f"✅ 运行完成，用时 {elapsed:.1f}s")


def main():
    total_start = time.perf_counter()
    print(f"流水线开始：{BASE_DIR}")

    commands = [
        [
            "uv", "run",
            str(BASE_DIR / "01generate_county_samples_by_city.py"),
            "--mode",'skip'
        ],
        [
            "uv", "run",
            str(BASE_DIR / "02fast_clip_samples_and_yangfang.py")
        ],
        [
            "uv", "run",
            str(BASE_DIR / "03fast_clip_samples_and_results.py")
        ],
        [
            "uv", "run", 
            str(BASE_DIR / "04_calculate_accuracy_to_boundary.py")
        ],
    ]

    print(f"待执行脚本数量：{len(commands)}")
    for index, cmd in enumerate(commands, start=1):
        print(f"\n----- 步骤 {index}/{len(commands)} -----")
        run_cmd(cmd)

    total_elapsed = time.perf_counter() - total_start
    print(f"\n🎉 所有脚本全部运行完成，总用时 {total_elapsed:.1f}s")


if __name__ == "__main__":
    main()