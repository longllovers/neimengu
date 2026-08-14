"""按县执行 TIFF 配准镶嵌、SHP 同步合并和边界断口融合。

目录约定
--------
input_tif/
    五原县/
        K48E017023_2025.tif
        ...
    其他县/
        ...
input_shp/
    K48E017023_2025.shp
    ...                         # 所有县共用一个 SHP 目录
output/
    五原县/
        aligned_mosaic_all_axis.tif
        aligned_mosaic_all_axis.shp
        aligned_mosaic_all_axis_fixed.shp
        aligned_mosaic_all_axis_fixed_repair_report.json

本脚本不重复实现两套算法，而是依次调用：

1. ``align_and_mosaic_multiple.py`` 完成原有 TIFF/SHP 配准合并；
2. ``fix_mosaic_shp_boundary_gaps.py`` 对合并 SHP 做断边直接融合。

因此两份现有脚本的主体算法和参数含义保持不变。

常用命令
--------
处理全部县：

    .venv\\Scripts\\python.exe align_mosaic_and_fix_by_county.py

只处理一个县：

    .venv\\Scripts\\python.exe align_mosaic_and_fix_by_county.py --county 五原县

只检查输入和命令，不执行：

    .venv\\Scripts\\python.exe align_mosaic_and_fix_by_county.py --dry-run

覆盖已有结果并重新处理：

    .venv\\Scripts\\python.exe align_mosaic_and_fix_by_county.py --overwrite
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import datetime
from pathlib import Path


ROOT = Path(__file__).resolve().parent
DEFAULT_TIF_ROOT = ROOT / "input_tif"
DEFAULT_SHP_DIR = ROOT / "input_shp"
DEFAULT_OUTPUT_ROOT = ROOT / "output"
DEFAULT_ALIGN_SCRIPT = ROOT / "align_and_mosaic_multiple.py"
DEFAULT_FIX_SCRIPT = ROOT / "fix_mosaic_shp_boundary_gaps.py"

MOSAIC_TIF_NAME = "aligned_mosaic_all_axis.tif"
MERGED_SHP_NAME = "aligned_mosaic_all_axis.shp"
FIXED_SHP_NAME = "aligned_mosaic_all_axis_fixed.shp"


def tif_files(directory: Path) -> list[Path]:
    """列出县目录下的 TIFF，不递归读取其他县。"""
    return sorted(
        [
            *directory.glob("*.tif"),
            *directory.glob("*.tiff"),
        ],
        key=lambda path: path.name.lower(),
    )


def discover_counties(
    tif_root: Path,
    selected_names: list[str] | None,
) -> list[Path]:
    """发现包含 TIFF 的县级子目录。"""
    if not tif_root.is_dir():
        raise FileNotFoundError(f"输入 TIFF 根目录不存在：{tif_root}")

    directories = {
        path.name: path
        for path in tif_root.iterdir()
        if path.is_dir() and tif_files(path)
    }
    if selected_names:
        missing = [name for name in selected_names if name not in directories]
        if missing:
            raise FileNotFoundError(
                "以下县目录不存在或没有 TIFF：" + "、".join(missing)
            )
        return [directories[name] for name in selected_names]
    if not directories:
        raise FileNotFoundError(
            f"{tif_root} 下没有包含 TIFF 的县级子目录。"
        )
    return [directories[name] for name in sorted(directories)]


def validate_county_inputs(
    county_dir: Path,
    shp_dir: Path,
) -> list[Path]:
    """确认每幅 TIFF 在公共 input_shp 中都有同名 SHP。"""
    files = tif_files(county_dir)
    if len(files) < 2:
        raise ValueError(
            f"{county_dir.name} 只有 {len(files)} 幅 TIFF，"
            "至少需要 2 幅才能镶嵌。"
        )
    missing = [
        f"{path.stem}.shp"
        for path in files
        if not (shp_dir / f"{path.stem}.shp").is_file()
    ]
    if missing:
        raise FileNotFoundError(
            f"{county_dir.name} 缺少 {len(missing)} 个同名 SHP："
            + "、".join(missing)
        )
    return files


def shapefile_exists(path: Path) -> bool:
    """检查 Shapefile 的三个必要组成文件。"""
    return all(
        path.with_suffix(suffix).is_file()
        for suffix in (".shp", ".shx", ".dbf")
    )


def printable_command(command: list[str]) -> str:
    """生成适合复制查看的 Windows 命令文本。"""
    return subprocess.list2cmdline(command)


def run_command(
    command: list[str],
    dry_run: bool,
) -> None:
    print(f"  命令：{printable_command(command)}")
    if not dry_run:
        subprocess.run(command, cwd=ROOT, check=True)


def alignment_command(
    args: argparse.Namespace,
    county_dir: Path,
    mosaic_tif: Path,
    merged_shp: Path,
) -> list[str]:
    command = [
        sys.executable,
        str(args.align_script),
        str(county_dir),
        "--output",
        str(mosaic_tif),
        "--shp-dir",
        str(args.shp_dir),
        "--shp-output",
        str(merged_shp),
        "--order",
        args.order,
        "--model",
        args.model,
        "--max-shift",
        str(args.max_shift),
        "--min-response",
        str(args.min_response),
        "--threads",
        str(args.threads),
        "--stripe-rows",
        str(args.stripe_rows),
        "--shp-seam-overlap-pixels",
        str(args.shp_seam_overlap_pixels),
    ]
    if args.keep_intermediate:
        command.append("--keep-intermediate")
    if not args.build_overviews:
        command.append("--no-build-overviews")
    if args.check_only:
        command.append("--check-only")
    if args.overwrite:
        command.append("--overwrite")
    return command


def fix_command(
    args: argparse.Namespace,
    county_dir: Path,
    merged_shp: Path,
    fixed_shp: Path,
) -> list[str]:
    command = [
        sys.executable,
        str(args.fix_script),
        "--input",
        str(merged_shp),
        "--output",
        str(fixed_shp),
        "--tif-dir",
        str(county_dir),
        "--shp-dir",
        str(args.shp_dir),
        "--max-gap-pixels",
        str(args.max_gap_pixels),
        "--overlap-pixels",
        str(args.fix_overlap_pixels),
        "--min-merge-contact-pixels",
        str(args.min_merge_contact_pixels),
    ]
    if args.overwrite:
        command.append("--overwrite")
    return command


def process_county(
    args: argparse.Namespace,
    county_dir: Path,
) -> dict:
    """执行一个县的完整流水线。"""
    county = county_dir.name
    files = validate_county_inputs(county_dir, args.shp_dir)
    county_output = args.output_root / county
    mosaic_tif = county_output / MOSAIC_TIF_NAME
    merged_shp = county_output / MERGED_SHP_NAME
    fixed_shp = county_output / FIXED_SHP_NAME

    print(f"\n{'=' * 72}\n处理县：{county}（{len(files)} 幅 TIFF）")
    print(f"  输入 TIFF：{county_dir}")
    print(f"  公共 SHP：{args.shp_dir}")
    print(f"  输出目录：{county_output}")

    align_cmd = alignment_command(
        args, county_dir, mosaic_tif, merged_shp
    )
    repair_cmd = fix_command(
        args, county_dir, merged_shp, fixed_shp
    )
    record = {
        "county": county,
        "tif_count": len(files),
        "input_tif_dir": str(county_dir),
        "output_dir": str(county_output),
        "mosaic_tif": str(mosaic_tif),
        "merged_shp": str(merged_shp),
        "fixed_shp": str(fixed_shp),
        "alignment_command": align_cmd,
        "repair_command": repair_cmd,
        "status": "pending",
    }

    if args.dry_run:
        print("  [DRY-RUN] 配准镶嵌：")
        run_command(align_cmd, dry_run=True)
        if not args.check_only:
            print("  [DRY-RUN] 断边融合：")
            run_command(repair_cmd, dry_run=True)
        record["status"] = "dry-run"
        return record

    county_output.mkdir(parents=True, exist_ok=True)

    if args.check_only:
        print("  只检查输入，不生成输出。")
        run_command(align_cmd, dry_run=False)
        record["status"] = "checked"
        return record

    if args.fix_only:
        if not mosaic_tif.is_file() or not shapefile_exists(merged_shp):
            raise FileNotFoundError(
                f"{county} 缺少已有的合成 TIFF/SHP，不能执行 --fix-only。"
            )
        print("  使用已有合成结果，只执行断边融合。")
    elif (
        not args.overwrite
        and mosaic_tif.is_file()
        and shapefile_exists(merged_shp)
    ):
        print("  已有完整合成 TIFF/SHP，跳过配准，继续断边融合。")
    else:
        print("  第 1/2 步：配准镶嵌 TIFF，并同步合并 SHP。")
        run_command(align_cmd, dry_run=False)

    if not mosaic_tif.is_file() or not shapefile_exists(merged_shp):
        raise RuntimeError(
            f"{county} 配准步骤结束后未生成完整 TIFF/SHP。"
        )

    if shapefile_exists(fixed_shp) and not args.overwrite:
        print(f"  最终融合 SHP 已存在，跳过：{fixed_shp}")
        record["status"] = "skipped-existing"
        return record

    print("  第 2/2 步：对合并 SHP 执行断边直接融合。")
    run_command(repair_cmd, dry_run=False)
    if not shapefile_exists(fixed_shp):
        raise RuntimeError(f"{county} 未生成完整的最终融合 SHP。")

    print(f"  完成：{fixed_shp}")
    record["status"] = "completed"
    return record


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--tif-root",
        type=Path,
        default=DEFAULT_TIF_ROOT,
        help="包含各县子目录的 TIFF 根目录",
    )
    parser.add_argument(
        "--shp-dir",
        type=Path,
        default=DEFAULT_SHP_DIR,
        help="所有县共用的同名输入 SHP 目录",
    )
    parser.add_argument(
        "--output-root",
        type=Path,
        default=DEFAULT_OUTPUT_ROOT,
        help="输出根目录；脚本会在其中按县建立子目录",
    )
    parser.add_argument(
        "--county",
        action="append",
        help="只处理指定县；可重复提供多次，默认处理全部县",
    )
    parser.add_argument(
        "--align-script",
        type=Path,
        default=DEFAULT_ALIGN_SCRIPT,
        help="配准镶嵌脚本路径",
    )
    parser.add_argument(
        "--fix-script",
        type=Path,
        default=DEFAULT_FIX_SCRIPT,
        help="断边融合脚本路径",
    )

    # align_and_mosaic_multiple.py 的主要可调参数。
    parser.add_argument(
        "--order", choices=("name", "spatial"), default="spatial"
    )
    parser.add_argument(
        "--model",
        choices=("rubber", "translation"),
        default="rubber",
    )
    parser.add_argument("--max-shift", type=float, default=30.0)
    parser.add_argument("--min-response", type=float, default=0.40)
    parser.add_argument("--threads", type=int, default=1)
    parser.add_argument("--stripe-rows", type=int, default=512)
    parser.add_argument(
        "--shp-seam-overlap-pixels",
        type=float,
        default=2.0,
    )
    parser.add_argument(
        "--build-overviews",
        action=argparse.BooleanOptionalAction,
        default=True,
    )
    parser.add_argument("--keep-intermediate", action="store_true")

    # fix_mosaic_shp_boundary_gaps.py 的主要可调参数。
    parser.add_argument("--max-gap-pixels", type=float, default=20.0)
    parser.add_argument(
        "--fix-overlap-pixels",
        type=float,
        default=0.05,
    )
    parser.add_argument(
        "--min-merge-contact-pixels",
        type=float,
        default=20.0,
    )

    parser.add_argument(
        "--fix-only",
        action="store_true",
        help="不重新配准，只对各县已有的合成 SHP 执行断边融合",
    )
    parser.add_argument(
        "--check-only",
        action="store_true",
        help="只调用原配准脚本检查输入，不生成文件",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="只验证目录、同名 SHP 并显示命令，不运行算法",
    )
    parser.add_argument(
        "--continue-on-error",
        action="store_true",
        help="某个县失败后继续处理其他县",
    )
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="允许两步都覆盖已有输出",
    )
    args = parser.parse_args()

    args.tif_root = args.tif_root.resolve()
    args.shp_dir = args.shp_dir.resolve()
    args.output_root = args.output_root.resolve()
    args.align_script = args.align_script.resolve()
    args.fix_script = args.fix_script.resolve()

    if not args.shp_dir.is_dir():
        parser.error(f"SHP 目录不存在：{args.shp_dir}")
    for script, label in (
        (args.align_script, "配准脚本"),
        (args.fix_script, "断边融合脚本"),
    ):
        if not script.is_file():
            parser.error(f"{label}不存在：{script}")
    if args.fix_only and args.check_only:
        parser.error("--fix-only 不能与 --check-only 同时使用。")
    if args.max_shift <= 0:
        parser.error("--max-shift 必须大于 0。")
    if not 0 < args.min_response <= 1:
        parser.error("--min-response 必须在 (0, 1] 范围内。")
    if args.threads < 1:
        parser.error("--threads 必须至少为 1。")
    if args.stripe_rows < 128:
        parser.error("--stripe-rows 必须至少为 128。")
    if (
        args.shp_seam_overlap_pixels < 0
        or args.max_gap_pixels <= 0
        or args.fix_overlap_pixels < 0
        or args.min_merge_contact_pixels <= 0
    ):
        parser.error("SHP 缝隙相关参数不能为负，宽度参数必须大于 0。")
    return args


def main() -> None:
    args = parse_args()
    try:
        counties = discover_counties(args.tif_root, args.county)
    except (OSError, ValueError) as exc:
        raise SystemExit(f"输入检查失败：{exc}") from exc

    print(
        f"发现 {len(counties)} 个待处理县："
        + "、".join(path.name for path in counties)
    )
    records: list[dict] = []
    failed = 0
    for county_dir in counties:
        try:
            records.append(process_county(args, county_dir))
        except (
            OSError,
            ValueError,
            RuntimeError,
            subprocess.CalledProcessError,
        ) as exc:
            failed += 1
            print(f"\n[失败] {county_dir.name}：{exc}", file=sys.stderr)
            records.append(
                {
                    "county": county_dir.name,
                    "status": "failed",
                    "error": str(exc),
                }
            )
            if not args.continue_on_error:
                break

    if not args.dry_run:
        args.output_root.mkdir(parents=True, exist_ok=True)
        report = {
            "generated_at": datetime.now().astimezone().isoformat(),
            "tif_root": str(args.tif_root),
            "shp_dir": str(args.shp_dir),
            "output_root": str(args.output_root),
            "counties_total": len(counties),
            "counties_failed": failed,
            "records": records,
        }
        report_path = args.output_root / "county_pipeline_report.json"
        report_path.write_text(
            json.dumps(report, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        print(f"\n批处理报告：{report_path}")

    if failed:
        raise SystemExit(1)
    print(f"\n全部完成：成功处理 {len(records)} 个县。")


if __name__ == "__main__":
    main()
