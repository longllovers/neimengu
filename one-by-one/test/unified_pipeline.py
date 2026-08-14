#!/usr/bin/env python3
"""按“城市 + 日期 + 卫星”串联下载、市级合并和县级裁剪。"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path

from rich.console import Console

import download


console = Console()
BASE_DIR = Path(__file__).resolve().parent
ANSI_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
PRODUCT_GROUP_RE = re.compile(
    r"^(?P<satellite>S2[A-Z])_.*?_(?P<date>\d{8})T\d{6}_",
    re.IGNORECASE,
)
READY_MARKER = "市级影像就绪："


@dataclass
class DownloadGroup:
    city: str
    date: str
    satellite: str
    products: list[dict]
    output_dir: Path
    temp_output_dir: Path
    index: int = 0
    total: int = 0

    @property
    def label(self) -> str:
        return f"{self.city} {self.date} {self.satellite}"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="完整执行 Sentinel-2 下载、市级合并和县级裁剪流水线。"
    )
    parser.add_argument("--config-file", default="config.json", help="流水线配置文件")
    parser.add_argument("--collection", default="sentinel-2-l2a", help="下载数据集")
    parser.add_argument("--contains", help="产品名必须包含的字符串")
    parser.add_argument("--limit", type=int, default=0, help="每次检索最多返回多少景；0 为全部")
    parser.add_argument("--max-retries", type=int, default=5, help="单景每轮最大尝试次数")
    parser.add_argument("--temp-dir", default="./temp_data", help="下载 ZIP 临时根目录")
    parser.add_argument("--shp-root", default="./shp", help="下载检索边界输出目录")
    parser.add_argument(
        "--city-layer",
        type=Path,
        default=download.CITY_LAYER,
        help="市级边界文件",
    )
    parser.add_argument(
        "--county-layer",
        type=Path,
        default=download.COUNTY_LAYER,
        help="县级边界文件",
    )
    parser.add_argument("--tile-id", action="append", default=[], help="按瓦片编号过滤")
    parser.add_argument("--overwrite", action="store_true", help="覆盖已有市级和县级结果")
    parser.add_argument("--search-only", action="store_true", help="只检索并展示任务分组")
    return parser.parse_args()


def product_group(product: dict) -> tuple[str, str] | None:
    props = product.get("properties") or {}
    name = str(
        product.get("Name")
        or props.get("title")
        or props.get("productIdentifier")
        or ""
    )
    match = PRODUCT_GROUP_RE.match(name)
    if match is None:
        return None
    return match.group("date"), match.group("satellite").upper()


def stream_command(command: list[str], title: str) -> tuple[int, Path | None]:
    """实时转发子进程日志，并提取 merge 脚本给出的唯一市级 TIFF。"""
    console.print(f"[dim]{title}命令：{' '.join(command)}[/dim]")
    env = os.environ.copy()
    env.setdefault("COLUMNS", "300")
    process = subprocess.Popen(
        command,
        cwd=BASE_DIR,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
        env=env,
    )
    ready_path: Path | None = None
    assert process.stdout is not None
    for raw_line in process.stdout:
        line = raw_line.rstrip("\r\n")
        if line:
            console.print(line, markup=False, highlight=False)
        clean_line = ANSI_RE.sub("", line).strip()
        marker_index = clean_line.find(READY_MARKER)
        if marker_index >= 0:
            value = clean_line[marker_index + len(READY_MARKER) :].strip()
            candidate = Path(value)
            if not candidate.is_absolute():
                candidate = BASE_DIR / candidate
            ready_path = candidate.resolve()
    return process.wait(), ready_path


def process_group(
    group: DownloadGroup,
    source_root: Path,
    city_output_root: Path,
    country_output_root: Path,
    city_layer: Path,
    county_layer: Path,
    overwrite: bool,
) -> tuple[bool, str]:
    prefix = f"第 {group.index}/{group.total} 个任务"
    console.print(f"\n[bold cyan]🔷 {prefix}开始 merge：{group.label}[/bold cyan]")
    merge_command = [
        sys.executable,
        "-u",
        str(BASE_DIR / "merge_city_tifs.py"),
        "--input-root",
        str(source_root),
        "--output-root",
        str(city_output_root),
        "--city-layer",
        str(city_layer),
        "--city",
        group.city,
        "--date",
        group.date,
        "--satellite",
        group.satellite,
        "--max-workers",
        "1",
    ]
    if overwrite:
        merge_command.append("--overwrite")

    merge_code, merged_path = stream_command(merge_command, f"{prefix} merge ")
    if merge_code != 0:
        message = f"{prefix} merge 失败，退出码：{merge_code}（{group.label}）"
        console.print(f"[bold red]❌ {message}[/bold red]")
        return False, message
    if merged_path is None or not merged_path.is_file():
        message = f"{prefix} merge 未返回有效市级 TIFF（{group.label}）"
        console.print(f"[bold red]❌ {message}[/bold red]")
        return False, message

    console.print(
        f"[bold green]✅ {prefix} merge 完成：{merged_path.name}[/bold green]"
    )
    console.print(f"[bold cyan]🔶 {prefix}开始 clip：{group.label}[/bold cyan]")
    clip_command = [
        sys.executable,
        "-u",
        str(BASE_DIR / "clip_county_tifs.py"),
        "--input-root",
        str(city_output_root),
        "--input-file",
        str(merged_path),
        "--output-root",
        str(country_output_root),
        "--county-layer",
        str(county_layer),
        "--city",
        merged_path.parent.name,
        "--max-workers",
        "1",
    ]
    if overwrite:
        clip_command.append("--overwrite")

    clip_code, _ = stream_command(clip_command, f"{prefix} clip ")
    if clip_code != 0:
        message = f"{prefix} clip 失败，退出码：{clip_code}（{group.label}）"
        console.print(f"[bold red]❌ {message}[/bold red]")
        return False, message

    console.print(f"[bold green]✅ {prefix} clip 完成：{group.label}[/bold green]")
    return True, group.label


def collect_groups(
    tasks: list[dict],
    client_pool: download.AccountPool,
    settings: dict,
    args: argparse.Namespace,
) -> list[DownloadGroup]:
    groups: dict[tuple[str, str, str], DownloadGroup] = {}
    seen_by_city: dict[str, set[str]] = {}

    for task in tasks:
        city = task["city"]
        roi_file = task["roi_file"]
        console.print(f"\n[bold cyan]========== 检索 {city} ==========[/bold cyan]")
        geometry = download.load_roi_geometry(roi_file=roi_file)
        output_dir = Path(
            download.build_output_dir(settings["output_dir"], roi_file=roi_file)
        )
        temp_output_dir = Path(
            download.build_output_dir(args.temp_dir, roi_file=roi_file)
        )
        date_ranges = settings.get("date_ranges_by_city", {}).get(
            download.normalize_region_name(city)
        ) or [{"start_date": settings["start_date"], "end_date": settings["end_date"]}]

        city_seen = seen_by_city.setdefault(download.normalize_region_name(city), set())
        for range_index, date_range in enumerate(date_ranges, 1):
            start_date = date_range["start_date"]
            end_date = date_range["end_date"]
            console.print(
                f"[cyan]📅 {city} 时间段 {range_index}/{len(date_ranges)}："
                f"{start_date} ～ {end_date}[/cyan]"
            )
            products = client_pool.search_products(
                collection=args.collection,
                start_date=start_date,
                end_date=end_date,
                limit=args.limit,
                geometry=geometry,
                contains=args.contains,
                cloud_cover=settings.get("cloud_cover"),
                tile_ids=args.tile_id,
            )
            for product in products:
                product_id = str(product.get("Id") or product.get("id") or "").strip()
                if not product_id or product_id in city_seen:
                    continue
                key_part = product_group(product)
                if key_part is None:
                    console.print(
                        f"[yellow]⚠️ 无法识别产品日期或卫星，已跳过："
                        f"{product.get('Name', product_id)}[/yellow]"
                    )
                    continue
                city_seen.add(product_id)
                date, satellite = key_part
                key = (city, date, satellite)
                if key not in groups:
                    groups[key] = DownloadGroup(
                        city=city,
                        date=date,
                        satellite=satellite,
                        products=[],
                        output_dir=output_dir,
                        temp_output_dir=temp_output_dir,
                    )
                groups[key].products.append(product)

    city_order = {
        download.normalize_region_name(task["city"]): index
        for index, task in enumerate(tasks)
    }
    ordered = sorted(
        groups.values(),
        key=lambda item: (
            city_order.get(download.normalize_region_name(item.city), len(city_order)),
            item.date,
            item.satellite,
        ),
    )
    total = len(ordered)
    for index, group in enumerate(ordered, 1):
        group.index = index
        group.total = total
    return ordered


def main() -> int:
    args = parse_args()
    try:
        accounts, cities, settings = download.load_config(args.config_file)
        if not settings.get("output_dir"):
            raise ValueError("config.json 中的 output_dir 不能为空")
        tasks = download.prepare_region_shapefiles(
            cities,
            shp_root=args.shp_root,
            city_layer=args.city_layer,
            county_layer=args.county_layer,
        )
    except Exception as exc:
        console.print(f"[bold red]❌ 配置或边界读取失败：{exc}[/bold red]")
        return 1

    source_root = Path(settings["output_dir"]).expanduser().resolve()
    city_output_root = Path(settings["city_output_dir"]).expanduser().resolve()
    country_output_root = Path(settings["country_output_dir"]).expanduser().resolve()
    processing_workers = settings["processing_workers"]
    download_workers = settings.get("max_workers") or 3
    temp_root = download.reset_temp_dir(args.temp_dir)
    client_pool = download.AccountPool(
        accounts,
        max_active_accounts=settings["max_active_accounts"],
    )

    console.print("[bold cyan]🌍 下载 → 市级合并 → 县级裁剪联合流水线[/bold cyan]")
    console.print(f"[blue]下载根目录：{source_root}[/blue]")
    console.print(f"[blue]市级输出目录：{city_output_root}[/blue]")
    console.print(f"[blue]县级输出目录：{country_output_root}[/blue]")
    console.print(
        f"[blue]下载线程：{download_workers}；完整 merge→clip 线程："
        f"{processing_workers}[/blue]"
    )

    try:
        groups = collect_groups(tasks, client_pool, settings, args)
        if not groups:
            console.print("[bold red]❌ 没有找到可下载的日期卫星任务[/bold red]")
            return 1

        console.print(
            f"\n[bold green]✅ 共生成 {len(groups)} 个下载任务；"
            "每个任务完整下载后才进入 merge→clip[/bold green]"
        )
        for group in groups:
            console.print(
                f"[green]  第 {group.index}/{group.total} 个任务："
                f"{group.label}，{len(group.products)} 景[/green]"
            )
        if args.search_only:
            return 0

        city_output_root.mkdir(parents=True, exist_ok=True)
        country_output_root.mkdir(parents=True, exist_ok=True)
        processing_futures = {}
        download_failures: list[str] = []
        with ThreadPoolExecutor(
            max_workers=processing_workers,
            thread_name_prefix="merge-clip",
        ) as processing_pool:
            for group in groups:
                prefix = f"第 {group.index}/{group.total} 个任务"
                console.print(
                    f"\n[bold blue]📥 {prefix}开始 download：{group.label}，"
                    f"共 {len(group.products)} 景[/bold blue]"
                )
                stats = client_pool.batch_download(
                    group.products,
                    temp_output_dir=group.temp_output_dir,
                    final_output_dir=group.output_dir,
                    max_workers=download_workers,
                    max_retries=args.max_retries,
                )
                completed = stats["success"] + stats["skipped"]
                if stats["failed"] or completed != len(group.products):
                    message = (
                        f"{prefix} download 未完整完成：应有 {len(group.products)} 景，"
                        f"完成 {completed} 景"
                    )
                    download_failures.append(message)
                    console.print(f"[bold red]❌ {message}，不进入 merge[/bold red]")
                    continue

                console.print(
                    f"[bold green]✅ {prefix} download 完整完成，"
                    "现已交给同一 worker 顺序执行 merge→clip[/bold green]"
                )
                future = processing_pool.submit(
                    process_group,
                    group,
                    source_root,
                    city_output_root,
                    country_output_root,
                    args.city_layer.resolve(),
                    args.county_layer.resolve(),
                    args.overwrite,
                )
                processing_futures[future] = group

            processing_failures: list[str] = []
            for future in as_completed(processing_futures):
                group = processing_futures[future]
                try:
                    success, message = future.result()
                except Exception as exc:
                    success = False
                    message = f"第 {group.index}/{group.total} 个任务异常：{exc}"
                    console.print(f"[bold red]❌ {message}[/bold red]")
                if not success:
                    processing_failures.append(message)

        console.print("\n[bold cyan]========== 联合流水线结果 ==========[/bold cyan]")
        console.print(f"任务总数：{len(groups)}")
        console.print(f"download 未交接：{len(download_failures)}")
        console.print(f"merge/clip 失败：{len(processing_failures)}")
        if download_failures or processing_failures:
            console.print("[bold yellow]⚠️ 流水线结束，但存在未完成任务[/bold yellow]")
            return 1
        console.print("[bold green]🎉 所有 download→merge→clip 任务均已完成[/bold green]")
        return 0
    except KeyboardInterrupt:
        console.print("\n[bold yellow]⚠️ 已停止联合流水线[/bold yellow]")
        return 130
    finally:
        if temp_root.exists():
            shutil.rmtree(temp_root)
            console.print(f"[blue]🧹 已删除临时目录：{temp_root}[/blue]")


if __name__ == "__main__":
    sys.exit(main())
