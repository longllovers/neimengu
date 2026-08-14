"""按市/县拆分投票任务，并把每个行政区保存为独立 Shapefile。"""

import argparse
import csv
from concurrent.futures import ProcessPoolExecutor, as_completed
from contextlib import contextmanager
from collections import deque
from dataclasses import dataclass
import json
import multiprocessing as mp
import os
from pathlib import Path
import re
import subprocess
import sys
import time
import uuid

import fiona
from pyproj import Transformer
from shapely.geometry import box, shape
from shapely.ops import transform


SPAWN_CONTEXT = mp.get_context("spawn")
BASE_DIR = Path(__file__).resolve().parent
VOTE_SCRIPT = BASE_DIR / "vote.py"
MERGE_SCRIPT = BASE_DIR / "merge_geodata.py"
SHAPEFILE_SUFFIXES = (".shp", ".shx", ".dbf", ".prj", ".cpg", ".qix", ".sbn", ".sbx", ".shp.xml")


def hidden_subprocess_options():
    """在 Windows 上启动 Python 子任务时不创建控制台窗口。"""
    if os.name != "nt":
        return {}
    startupinfo = subprocess.STARTUPINFO()
    startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    startupinfo.wShowWindow = subprocess.SW_HIDE
    return {
        "startupinfo": startupinfo,
        "creationflags": subprocess.CREATE_NO_WINDOW,
    }


@dataclass(frozen=True)
class Region:
    level: str
    name: str
    code: str
    boundary_path: Path
    name_field: str

    @property
    def output_stem(self):
        return f"{self.code}_{sanitize_filename(self.name)}"


def sanitize_filename(value):
    return re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", str(value)).strip(" .")


def normalize_name(value):
    return re.sub(r"\s+", "", str(value or ""))


def aliases(value):
    name = normalize_name(value)
    result = {name}
    for suffix in ("自治旗", "市辖区", "市", "盟", "县", "旗", "区"):
        if name.endswith(suffix) and len(name) > len(suffix):
            result.add(name[: -len(suffix)])
            break
    return result


def split_names(value):
    result = []
    seen = set()
    for item in re.split(r"[;；]", str(value or "")):
        name = normalize_name(item)
        if name and name not in seen:
            result.append(name)
            seen.add(name)
    return result


def find_boundary_path(level):
    dirname = "00市边界" if level == "city" else "00县边界"
    candidates = sorted((BASE_DIR / dirname).glob("*.shp"))
    if not candidates:
        candidates = sorted(BASE_DIR.glob(f"**/{dirname}/*.shp"))
    if not candidates:
        raise FileNotFoundError(f"找不到 {dirname} 下的行政区边界 Shapefile。")
    return candidates[0]


def load_regions(level):
    boundary_path = find_boundary_path(level)
    name_fields = ("市名称", "市名", "area_name", "县名称", "县名", "QXMC", "NAME", "name")
    code_fields = ("市代码", "area_code", "县代码", "QXDM", "CODE", "code")
    regions = []
    with fiona.open(boundary_path) as source:
        crs = source.crs_wkt or source.crs
        for feature in source:
            props = feature.get("properties") or {}
            name_field = next((field for field in name_fields if props.get(field) is not None), None)
            code_field = next((field for field in code_fields if props.get(field) is not None), None)
            if not name_field or not code_field or not feature.get("geometry"):
                continue
            digits = re.sub(r"\D", "", str(props[code_field]))
            width = 4 if level == "city" else 6
            if len(digits) < width:
                continue
            regions.append((
                Region(level, normalize_name(props[name_field]), digits[:width], boundary_path, name_field),
                shape(feature["geometry"]),
                crs,
            ))
    if not regions:
        raise ValueError(f"行政区边界中没有读到有效的名称、代码和几何: {boundary_path}")
    return regions


def select_regions(region_text, cls_tif):
    cities = load_regions("city")
    counties = load_regions("county")
    requested = split_names(region_text)
    if requested:
        selected = []
        used = set()
        for query in requested:
            query_aliases = aliases(query)
            matches = [item[0] for item in cities + counties if query_aliases & aliases(item[0].name)]
            unique = {(item.level, item.code): item for item in matches}
            if not unique:
                raise ValueError(f"市/县边界中找不到名称：{query}")
            if len(unique) > 1:
                labels = "、".join(f"{item.code}_{item.name}" for item in unique.values())
                raise ValueError(f"市/县名称 {query!r} 匹配到多个行政区，请填写完整名称：{labels}")
            region = next(iter(unique.values()))
            key = (region.level, region.code)
            if key not in used:
                selected.append(region)
                used.add(key)
        return selected

    import rasterio

    with rasterio.open(cls_tif) as source:
        tif_bounds = box(*source.bounds)
        tif_crs = source.crs
    selected = []
    for region, geometry, boundary_crs in counties:
        test_bounds = tif_bounds
        if tif_crs and boundary_crs and str(tif_crs) != str(boundary_crs):
            transformer = Transformer.from_crs(tif_crs, boundary_crs, always_xy=True).transform
            test_bounds = transform(transformer, tif_bounds)
        if geometry.intersects(test_bounds) and not geometry.intersection(test_bounds).is_empty:
            selected.append(region)
    if not selected:
        raise ValueError("输入 TIF 范围未与任何县级行政区相交。")
    return selected


def stream_subprocess(command, prefix):
    process = subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
        **hidden_subprocess_options(),
    )
    assert process.stdout is not None
    tail = deque(maxlen=30)
    for line in process.stdout:
        tail.append(line.rstrip())
        print(f"[{prefix}] {line}", end="", flush=True)
    returncode = process.wait()
    if returncode:
        details = "\n".join(tail) or "任务没有输出具体错误信息"
        raise RuntimeError(
            f"任务返回码 {returncode}，最后输出如下：\n{details}"
        )


@contextmanager
def output_lock(output_path, timeout=3600, stale_seconds=21600):
    lock_path = output_path.with_name(output_path.name + ".lock")
    lock_token = uuid.uuid4().hex
    deadline = time.monotonic() + timeout
    while True:
        try:
            descriptor = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(descriptor, lock_token.encode("ascii"))
            os.close(descriptor)
            break
        except FileExistsError:
            try:
                if time.time() - lock_path.stat().st_mtime > stale_seconds:
                    lock_path.unlink()
                    continue
            except FileNotFoundError:
                continue
            if time.monotonic() >= deadline:
                raise TimeoutError(f"等待输出文件锁超时: {lock_path}")
            time.sleep(0.5)
    try:
        yield
    finally:
        try:
            if lock_path.read_text(encoding="ascii") == lock_token:
                lock_path.unlink()
        except (FileNotFoundError, OSError, UnicodeDecodeError):
            pass


def remove_shapefile_components(output_path):
    stem = Path(output_path).with_suffix("")
    for suffix in SHAPEFILE_SUFFIXES:
        candidate = Path(str(stem) + suffix)
        if candidate.exists():
            candidate.unlink()


def promote_shapefile(staged_path, output_path):
    """整套替换 Shapefile；任一伴随文件移动失败时恢复旧结果。"""
    staged_stem = Path(staged_path).with_suffix("")
    output_stem = Path(output_path).with_suffix("")
    backup_stem = output_stem.with_name(f".{output_stem.name}.backup.{uuid.uuid4().hex}")
    old_moves = []
    new_moves = []
    try:
        for suffix in SHAPEFILE_SUFFIXES:
            current = Path(str(output_stem) + suffix)
            backup = Path(str(backup_stem) + suffix)
            if current.exists():
                os.replace(current, backup)
                old_moves.append((current, backup))
        for suffix in SHAPEFILE_SUFFIXES:
            staged = Path(str(staged_stem) + suffix)
            current = Path(str(output_stem) + suffix)
            if staged.exists():
                os.replace(staged, current)
                new_moves.append(current)
        if not output_path.exists():
            raise RuntimeError(f"暂存结果缺少主 .shp 文件: {staged_path}")
    except Exception:
        for current in reversed(new_moves):
            try:
                current.unlink()
            except FileNotFoundError:
                pass
        for current, backup in reversed(old_moves):
            if backup.exists():
                os.replace(backup, current)
        raise
    finally:
        remove_shapefile_components(staged_path)
        remove_shapefile_components(Path(str(backup_stem) + ".shp"))


def process_region(region, args, vote_concurrency):
    label = region.output_stem
    region_temp = Path(args.temp_dir) / label
    vote_dir = region_temp / "vote"
    vote_dir.mkdir(parents=True, exist_ok=True)
    output_path = Path(args.output_dir) / f"{label}.shp"
    vote_command = [
        sys.executable, str(VOTE_SCRIPT), "--cls_tif", args.cls_tif,
        "--out_dir", str(vote_dir), "--shp_dir", args.shp_dir,
        "--region-name", region.name,
        "--MIN_BACKGROUND_THRESHOLD", str(args.MIN_BACKGROUND_THRESHOLD),
        "--MIN_CLASS_AREA_MU", str(args.MIN_CLASS_AREA_MU),
        "--concurrency-count", str(vote_concurrency),
    ]
    if args.multi_class:
        vote_command.extend(["--multi-class", "--class-mapping", args.class_mapping])
    stream_subprocess(vote_command, f"{label}/投票")
    with output_lock(output_path):
        if not any(vote_dir.glob("*.shp")):
            remove_shapefile_components(output_path)
            return {
                "name": region.name,
                "area_mu": 0.0,
                "output_path": str(output_path),
                "skipped": True,
            }
        staged_path = output_path.with_name(f".{output_path.stem}.stage.{uuid.uuid4().hex}.shp")
        summary_json = region_temp / "merge_summary.json"
        merge_command = [
            sys.executable, str(MERGE_SCRIPT), "--input-dir", str(vote_dir),
            "--output", str(staged_path), "--schema-mode", "union",
            "--geometry-mode", "promote-multi", "--overwrite",
            "--clip-boundary", str(region.boundary_path),
            "--clip-name-field", region.name_field, "--clip-name", region.name,
            "--summary-json", str(summary_json),
        ]
        try:
            stream_subprocess(merge_command, f"{label}/合并")
            if not summary_json.is_file():
                raise RuntimeError(f"合并成功但没有生成面积统计文件: {summary_json}")
            summary = json.loads(summary_json.read_text(encoding="utf-8"))
            if int(summary.get("output_count", 0)) == 0:
                remove_shapefile_components(output_path)
                return {
                    "name": region.name,
                    "area_mu": 0.0,
                    "output_path": str(output_path),
                    "skipped": True,
                }
            promote_shapefile(staged_path, output_path)
        finally:
            remove_shapefile_components(staged_path)
    return {
        "name": region.name,
        "area_mu": float(summary["area_mu"]),
        "output_path": str(output_path),
        "skipped": False,
    }


def write_area_summary(output_dir, rows):
    """写固定两列 CSV；同一输出文件夹的并发任务通过文件锁串行替换。"""
    csv_path = Path(output_dir) / "市县面积统计.csv"
    with output_lock(csv_path):
        temp_path = csv_path.with_name(f".{csv_path.name}.{uuid.uuid4().hex}.tmp")
        try:
            with temp_path.open("w", encoding="utf-8-sig", newline="") as csv_file:
                writer = csv.writer(csv_file)
                writer.writerow(["市/县名字", "亩数"])
                for row in rows:
                    writer.writerow([row["name"], f'{row["area_mu"]:.4f}'])
            os.replace(temp_path, csv_path)
        finally:
            try:
                temp_path.unlink()
            except FileNotFoundError:
                pass
    return csv_path


def parse_args():
    parser = argparse.ArgumentParser(description="按市/县分别投票并输出 Shapefile。")
    parser.add_argument("--shp_dir", required=True)
    parser.add_argument("--cls_tif", required=True)
    parser.add_argument("--temp-dir", required=True)
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--region-name", default="")
    parser.add_argument("--MIN_BACKGROUND_THRESHOLD", type=float, default=0.5)
    parser.add_argument("--MIN_CLASS_AREA_MU", type=float, default=999999999)
    parser.add_argument("--multi-class", action="store_true")
    parser.add_argument("--class-mapping", default="")
    parser.add_argument(
        "--concurrency-count",
        dest="concurrency_count",
        type=int,
        default=4,
        help="区域与投票任务共用的总并发数，范围 1 到 96。",
    )
    return parser.parse_args()


def main():
    args = parse_args()
    if not str(args.shp_dir).strip():
        raise ValueError("输入 SHP 根目录不能为空。")
    if not str(args.cls_tif).strip():
        raise ValueError("输入 TIF 路径不能为空。")
    if not str(args.output_dir).strip():
        raise ValueError("最终输出文件夹不能为空。")
    if not 0 <= args.MIN_BACKGROUND_THRESHOLD <= 1:
        raise ValueError("--MIN_BACKGROUND_THRESHOLD 必须在 0 到 1 之间。")
    if args.MIN_CLASS_AREA_MU < 0:
        raise ValueError("--MIN_CLASS_AREA_MU 必须大于等于 0。")
    if args.multi_class and not args.class_mapping.strip():
        raise ValueError("启用多分类后必须填写类别映射。")
    if not 1 <= args.concurrency_count <= 96:
        raise ValueError("并发数必须在 1 到 96 之间。")
    if not Path(args.output_dir).is_dir():
        Path(args.output_dir).mkdir(parents=True, exist_ok=True)
    regions = select_regions(args.region_name, args.cls_tif)
    print("[需求解析] " + ("已填写市/县，按输入项分别输出" if split_names(args.region_name) else "未填写市/县，按 TIF 范围内全部县输出"), flush=True)
    print(f"[行政区] 共 {len(regions)} 个：" + "；".join(region.output_stem for region in regions), flush=True)
    # 并发任务启动前先完成/读取索引，保证首次运行只有一个任务写 save.json。
    stream_subprocess(
        [sys.executable, str(VOTE_SCRIPT), "--shp_dir", args.shp_dir, "--ensure-shp-cache-only"],
        "索引预检",
    )
    active_concurrency = min(args.concurrency_count, len(regions))
    vote_concurrency = max(1, args.concurrency_count // active_concurrency)
    print(
        f"[并发设置] 总并发数={args.concurrency_count}，区域并发数={active_concurrency}，"
        f"单区域投票并发数={vote_concurrency}",
        flush=True,
    )
    outputs = []
    failures = []
    with ProcessPoolExecutor(
        max_workers=active_concurrency,
        mp_context=SPAWN_CONTEXT,
    ) as executor:
        futures = {
            executor.submit(process_region, region, args, vote_concurrency): region
            for region in regions
        }
        for future in as_completed(futures):
            region = futures[future]
            try:
                result = future.result()
                if result.get("skipped"):
                    print(
                        f"[跳过] {region.output_stem}：行政区内没有有效要素，不生成 SHP，不计入 CSV。",
                        flush=True,
                    )
                else:
                    outputs.append(result)
                    print(
                        f'[完成] {region.output_stem} -> {result["output_path"]}，'
                        f'面积 {result["area_mu"]:.4f} 亩',
                        flush=True,
                    )
            except Exception as exc:
                failures.append((region, exc))
                print(f"[失败] {region.output_stem}: {exc}", flush=True)
    outputs.sort(key=lambda row: row["name"])
    csv_path = write_area_summary(args.output_dir, outputs)
    summary_payload = {
        "csv_path": str(csv_path),
        "rows": [
            {"name": row["name"], "area_mu": round(row["area_mu"], 4)}
            for row in outputs
        ],
    }
    print("__AREA_SUMMARY__" + json.dumps(summary_payload, ensure_ascii=False), flush=True)
    print(f"[面积统计] {csv_path}", flush=True)
    if failures:
        details = "；".join(f"{region.output_stem}: {exc}" for region, exc in failures)
        raise RuntimeError(f"{len(failures)}/{len(regions)} 个行政区处理失败：{details}")
    print(f"[SUMMARY] 全部完成，共输出 {len(outputs)} 个 Shapefile 到 {Path(args.output_dir)}", flush=True)


if __name__ == "__main__":
    main()
