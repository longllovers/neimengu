import argparse
import subprocess
import sys
from pathlib import Path
import os 
import shutil 

BASE_DIR = Path(__file__).resolve().parent
dirs_path = [BASE_DIR / "01生成样本", BASE_DIR / "02参考真值",
              BASE_DIR / "03测量值", BASE_DIR / "04精度评价"]

# 删除文件夹
for dir_path in dirs_path:
    if dir_path.exists():
        shutil.rmtree(dir_path)
        print(f"已删除文件夹：{dir_path}")


def run_cmd(cmd):
    print(f"\n========== 正在运行：{' '.join(cmd)} ==========")

    result = subprocess.run(
        cmd,
        cwd=BASE_DIR,
        text=True
    )

    if result.returncode != 0:
        print(f"\n❌ 运行失败：{' '.join(cmd)}")
        print(f"错误码：{result.returncode}")
        sys.exit(result.returncode)

    print("✅ 运行完成")


def main():

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

    for cmd in commands:
        run_cmd(cmd)

    print("\n🎉 所有脚本全部运行完成")


if __name__ == "__main__":
    main()
