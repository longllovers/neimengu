"""并行处理文件夹内的 TIF，并把同一行政区的中间结果统一合并。"""

import argparse
import csv
from concurrent.futures import ThreadPoolExecutor, as_completed
from collections import deque
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import uuid


BASE_DIR = Path(__file__).resolve().parent
REGIONAL_PIPELINE_SCRIPT = BASE_DIR / "regional_pipeline.py"
MERGE_SCRIPT = BASE_DIR / "merge_geodata.py"
VOTE_SCRIPT = BASE_DIR / "vote.py"
TIF_SUFFIXES = {".tif", ".tiff"}
SHAPEFILE_SUFFIXES = (".shp", ".shx", ".dbf", ".prj", ".cpg", ".qix", ".sbn", ".sbx", ".shp.xml")


def hidden_subprocess_options():
    if os.name != "nt":
        return {}
    startupinfo = subprocess.STARTUPINFO()
    startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    startupinfo.wShowWindow = subprocess.SW_HIDE
    return {
        "startupinfo": startupinfo,
        "creationflags": subprocess.CREATE_NO_WINDOW,
    }


def collect_tifs(input_dir):
    root = Path(input_dir)
    if not root.is_dir():
        raise NotADirectoryError(f"输入路径不是文件夹: {root}")
    paths = sorted(
        (path for path in root.rglob("*") if path.is_file() and path.suffix.lower() in TIF_SUFFIXES),
        key=lambda path: str(path).lower(),
    )
    if not paths:
        raise FileNotFoundError(f"输入文件夹及其子文件夹中没有找到 TIF: {root}")
    return paths


def run_child(command, label, suppress_area_summary=False):
    """静默启动子任务；仅转发业务输出，不显示命令、PID 等运行信息。"""
    process = subprocess.Popen(
        command,
        cwd=BASE_DIR,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
        env={
            **os.environ,
            "PYTHONIOENCODING": "utf-8",
            "PYTHONUNBUFFERED": "1",
        },
        **hidden_subprocess_options(),
    )
    tail = deque(maxlen=40)
    assert process.stdout is not None
    for line in process.stdout:
        tail.append(line.rstrip())
        if suppress_area_summary and line.startswith("__AREA_SUMMARY__"):
            continue
        print(f"[{label}] {line}", end="", flush=True)
    returncode = process.wait()
    if returncode:
        details = "\n".join(tail) or "没有具体错误信息"
        raise RuntimeError(f"{label} 处理失败：\n{details}")


def build_tif_command(args, tif_path, job_dir, child_concurrency):
    command = [
        sys.executable,
        str(REGIONAL_PIPELINE_SCRIPT),
        "--cls_tif", str(tif_path),
        "--temp-dir", str(job_dir / "work"),
        "--shp_dir", args.shp_dir,
        "--region-name", args.region_name,
        "--output-dir", str(job_dir / "results"),
        "--MIN_BACKGROUND_THRESHOLD", str(args.MIN_BACKGROUND_THRESHOLD),
        "--MIN_CLASS_AREA_MU", str(args.MIN_CLASS_AREA_MU),
        "--concurrency-count", str(child_concurrency),
    ]
    if args.multi_class:
        command.extend(["--multi-class", "--class-mapping", args.class_mapping])
    return command


def process_one_tif(index, total, tif_path, args, child_concurrency):
    job_dir = Path(args.temp_dir) / "tif_jobs" / f"{index:03d}"
    (job_dir / "work").mkdir(parents=True, exist_ok=True)
    (job_dir / "results").mkdir(parents=True, exist_ok=True)
    label = f"TIF {index}/{total}：{tif_path.name}"
    run_child(
        build_tif_command(args, tif_path, job_dir, child_concurrency),
        label,
        suppress_area_summary=True,
    )
    return index, tif_path, sorted((job_dir / "results").glob("*.shp"))


def region_group_key(shp_path):
    """regional_pipeline 的文件名以行政代码开头；同一代码必须归为一组。"""
    code = Path(shp_path).stem.split("_", 1)[0].strip()
    if not code:
        raise ValueError(f"无法从文件名识别行政区代码: {shp_path}")
    return code


def stage_group_inputs(grouped_outputs, temp_dir):
    """按行政代码归组，并给同县中间文件添加 _001、_002 等编号。"""
    merge_root = Path(temp_dir) / "merge_inputs"
    staged = {}
    for region_code, sources in sorted(grouped_outputs.items()):
        sources = sorted(sources, key=lambda path: (path.name.lower(), str(path).lower()))
        output_name = sources[0].name
        group_dir = merge_root / region_code
        group_dir.mkdir(parents=True, exist_ok=True)
        for number, source in enumerate(sources, start=1):
            numbered_stem = f"{source.stem}_{number:03d}"
            for component in source.parent.glob(source.stem + ".*"):
                suffix = component.name[len(source.stem):]
                shutil.copy2(component, group_dir / f"{numbered_stem}{suffix}")
        staged[output_name] = group_dir
    return staged


def archive_competing_region_outputs(output_path, temp_dir):
    """把同一行政代码的旧主文件移入临时区，确保最终目录一个县一个 SHP。"""
    output_path = Path(output_path)
    region_code = region_group_key(output_path)
    current = output_path.resolve()
    archive_dir = Path(temp_dir) / "replaced_final_outputs" / region_code
    for candidate in output_path.parent.glob(f"{region_code}_*.shp"):
        if candidate.resolve() == current:
            continue
        archive_dir.mkdir(parents=True, exist_ok=True)
        candidate_stem = candidate.with_suffix("")
        for suffix in SHAPEFILE_SUFFIXES:
            component = Path(str(candidate_stem) + suffix)
            if component.exists():
                shutil.move(str(component), str(archive_dir / component.name))
        print(f"[旧结果已移入临时区] {candidate.name}", flush=True)


def merge_region(output_name, input_dir, args):
    output_path = Path(args.output_dir) / output_name
    summary_path = Path(args.temp_dir) / "merge_summaries" / f"{Path(output_name).stem}.json"
    summary_path.parent.mkdir(parents=True, exist_ok=True)
    command = [
        sys.executable,
        str(MERGE_SCRIPT),
        "--input-dir", str(input_dir),
        "--output", str(output_path),
        "--schema-mode", "union",
        "--geometry-mode", "promote-multi",
        "--overwrite",
        "--summary-json", str(summary_path),
    ]
    run_child(command, f"统一合并：{output_name}")
    archive_competing_region_outputs(output_path, args.temp_dir)
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    stem = Path(output_name).stem
    region_name = stem.split("_", 1)[1] if "_" in stem else stem
    return {
        "name": region_name,
        "area_mu": float(summary.get("area_mu", 0.0)),
        "output_path": str(output_path),
    }


def write_area_summary(output_dir, rows):
    csv_path = Path(output_dir) / "市县面积统计.csv"
    temp_path = csv_path.with_name(f".{csv_path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temp_path.open("w", encoding="utf-8-sig", newline="") as csv_file:
            writer = csv.writer(csv_file)
            writer.writerow(["市/县名字", "亩数"])
            for row in sorted(rows, key=lambda item: item["name"]):
                writer.writerow([row["name"], f'{row["area_mu"]:.4f}'])
        os.replace(temp_path, csv_path)
    finally:
        try:
            temp_path.unlink()
        except FileNotFoundError:
            pass
    return csv_path


def parse_args():
    parser = argparse.ArgumentParser(description="批量 TIF 投票并统一合并。")
    parser.add_argument("--input-dir", required=True)
    parser.add_argument("--temp-dir", required=True)
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--shp_dir", required=True)
    parser.add_argument("--region-name", default="")
    parser.add_argument("--MIN_BACKGROUND_THRESHOLD", type=float, default=0.5)
    parser.add_argument("--MIN_CLASS_AREA_MU", type=float, default=999999999)
    parser.add_argument("--concurrency-count", type=int, default=4)
    parser.add_argument("--multi-class", action="store_true")
    parser.add_argument("--class-mapping", default="")
    return parser.parse_args()


def main():
    args = parse_args()
    if not 1 <= args.concurrency_count <= 96:
        raise ValueError("并发数必须在 1 到 96 之间。")
    if args.multi_class and not args.class_mapping.strip():
        raise ValueError("启用多分类后必须填写类别映射。")
    tif_files = collect_tifs(args.input_dir)
    Path(args.temp_dir).mkdir(parents=True, exist_ok=True)
    Path(args.output_dir).mkdir(parents=True, exist_ok=True)

    tif_workers = min(args.concurrency_count, len(tif_files))
    child_concurrency = max(1, args.concurrency_count // tif_workers)
    print(
        f"[批量任务] 找到 {len(tif_files)} 个 TIF，同时处理 {tif_workers} 个，"
        f"每个 TIF 内部并发数 {child_concurrency}。",
        flush=True,
    )

    # 并发启动各 TIF 前只预检一次索引，避免多个子任务同时创建缓存。
    run_child(
        [sys.executable, str(VOTE_SCRIPT), "--shp_dir", args.shp_dir, "--ensure-shp-cache-only"],
        "索引预检",
    )

    grouped_outputs = {}
    failures = []
    with ThreadPoolExecutor(max_workers=tif_workers) as executor:
        futures = {
            executor.submit(
                process_one_tif,
                index,
                len(tif_files),
                tif_path,
                args,
                child_concurrency,
            ): (index, tif_path)
            for index, tif_path in enumerate(tif_files, start=1)
        }
        for future in as_completed(futures):
            index, tif_path = futures[future]
            try:
                _, _, outputs = future.result()
                for output_path in outputs:
                    grouped_outputs.setdefault(region_group_key(output_path), []).append(output_path)
                print(f"[TIF 完成] {index}/{len(tif_files)}：{tif_path.name}", flush=True)
            except Exception as exc:
                failures.append((tif_path, exc))
                print(f"[TIF 失败] {tif_path.name}：{exc}", flush=True)

    if not grouped_outputs:
        raise RuntimeError("所有 TIF 均未产生可合并的 Shapefile。")

    for sources in grouped_outputs.values():
        sources.sort(key=lambda path: str(path).lower())
    staged_groups = stage_group_inputs(grouped_outputs, args.temp_dir)
    rows = [
        merge_region(output_name, input_dir, args)
        for output_name, input_dir in sorted(staged_groups.items())
    ]
    csv_path = write_area_summary(args.output_dir, rows)
    print("__AREA_SUMMARY__" + json.dumps({
        "csv_path": str(csv_path),
        "rows": [
            {"name": row["name"], "area_mu": round(row["area_mu"], 4)}
            for row in sorted(rows, key=lambda item: item["name"])
        ],
    }, ensure_ascii=False), flush=True)
    print(f"[完成] 已统一合并 {len(rows)} 个市/县结果，汇总表：{csv_path}", flush=True)
    if failures:
        details = "；".join(f"{path.name}: {exc}" for path, exc in failures)
        raise RuntimeError(f"{len(failures)}/{len(tif_files)} 个 TIF 处理失败：{details}")


if __name__ == "__main__":
    main()
