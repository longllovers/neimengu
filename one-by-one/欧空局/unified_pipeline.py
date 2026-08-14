#!/usr/bin/env python3
"""全局并发下载影像，并按“城市 + 日期 + 卫星”触发合并和裁剪。"""

from __future__ import annotations

import argparse
import multiprocessing as mp
import os
import re
import shutil
import signal
import subprocess
import sys
import threading
from concurrent.futures import (
    FIRST_COMPLETED,
    ProcessPoolExecutor,
    ThreadPoolExecutor,
    as_completed,
    wait,
)
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from rich.progress import Progress, ProgressColumn, Task, TextColumn
from rich.text import Text

import download


console = download.console
BASE_DIR = Path(__file__).resolve().parent
ANSI_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
PRODUCT_GROUP_RE = re.compile(
    r"^(?P<satellite>S2[A-Z])_.*?_(?P<date>\d{8})T\d{6}_",
    re.IGNORECASE,
)
READY_MARKER = "市级影像就绪："
GRACEFUL_STOP = threading.Event()


def ignore_sigint() -> None:
    """工作进程忽略 Ctrl+C，由主进程统一安排安全收尾。"""
    signal.signal(signal.SIGINT, signal.SIG_IGN)


class MegabytesPerSecondColumn(ProgressColumn):
    """始终以 MB/s 显示当前下载速度。"""

    def render(self, task: Task) -> Text:
        speed = (task.speed or 0.0) / (1024 * 1024)
        return Text(f"{speed:.2f} MB/s")


class DownloadSizeGBColumn(ProgressColumn):
    """以 GB 显示已下载量和文件总大小。"""

    def render(self, task: Task) -> Text:
        downloaded = task.completed / (1024 ** 3)
        if task.total:
            total = task.total / (1024 ** 3)
            return Text(f"{downloaded:.2f}/{total:.2f} GB")
        return Text(f"{downloaded:.2f} GB")


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


@dataclass
class FailureDetail:
    """流水线最终失败明细；一条记录对应一个 ZIP。"""

    date: str
    city: str
    zip_name: str
    stage: str
    reason: str


def group_failure_details(
    group: DownloadGroup,
    stage: str,
    reason: str,
) -> list[FailureDetail]:
    """将按组发生的 merge/clip 失败展开为逐 ZIP 明细。"""
    return [
        FailureDetail(
            date=group.date,
            city=group.city,
            zip_name=f"{download.safe_filename(product_display_name(product))}.zip",
            stage=stage,
            reason=reason,
        )
        for product in group.products
    ]


def print_failure_details(failures: list[FailureDetail]) -> None:
    """在流水线末尾打印便于定位和复制的失败清单。"""
    if not failures:
        return

    console.print("\n[bold red]========== 最终失败 ZIP 明细 ==========[/bold red]")
    for index, failure in enumerate(failures, 1):
        console.print(f"[bold red]失败 {index}/{len(failures)}[/bold red]")
        console.print(f"  日期：{failure.date}")
        console.print(f"  城市：{failure.city}")
        console.print(f"  ZIP：{failure.zip_name}")
        console.print(f"  阶段：{failure.stage}")
        console.print(f"  原因：{failure.reason}")
    console.print("[bold red]========================================[/bold red]")


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
    """实时转发日志，并提取 merge 脚本给出的唯一市级 TIFF。"""
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
        creationflags=(
            subprocess.CREATE_NEW_PROCESS_GROUP
            if os.name == "nt"
            else 0
        ),
        start_new_session=os.name != "nt",
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
) -> tuple[bool, list[FailureDetail]]:
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
        reason = f"merge 退出码为 {merge_code}"
        message = f"{prefix} merge 失败，{reason}（{group.label}）"
        console.print(f"[bold red]❌ {message}[/bold red]")
        return False, group_failure_details(group, "merge", reason)
    if merged_path is None or not merged_path.is_file():
        reason = "merge 未返回有效市级 TIFF"
        message = f"{prefix} {reason}（{group.label}）"
        console.print(f"[bold red]❌ {message}[/bold red]")
        return False, group_failure_details(group, "merge", reason)
    if merged_path.stat().st_size == 0:
        remove_zero_byte_file(merged_path, "市级 TIFF")
        console.print(
            f"[bold yellow]🔁 {prefix} merge 结果为 0 字节，自动重做一次："
            f"{group.label}[/bold yellow]"
        )
        merge_code, merged_path = stream_command(
            merge_command,
            f"{prefix} merge 重做 ",
        )
        if merge_code != 0:
            reason = f"0 字节结果重做 merge 后，退出码为 {merge_code}"
            message = f"{prefix} merge 失败，{reason}（{group.label}）"
            console.print(f"[bold red]❌ {message}[/bold red]")
            return False, group_failure_details(group, "merge", reason)
        if merged_path is None or not merged_path.is_file():
            reason = "0 字节结果重做 merge 后，仍未返回有效市级 TIFF"
            message = f"{prefix} {reason}（{group.label}）"
            console.print(f"[bold red]❌ {message}[/bold red]")
            return False, group_failure_details(group, "merge", reason)
        if merged_path.stat().st_size == 0:
            remove_zero_byte_file(merged_path, "市级 TIFF")
            reason = "重做 merge 后市级 TIFF 仍为 0 字节"
            message = f"{prefix} merge 失败，{reason}（{group.label}）"
            console.print(f"[bold red]❌ {message}[/bold red]")
            return False, group_failure_details(group, "merge", reason)

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
        reason = f"clip 退出码为 {clip_code}"
        message = f"{prefix} clip 失败，{reason}（{group.label}）"
        console.print(f"[bold red]❌ {message}[/bold red]")
        return False, group_failure_details(group, "clip", reason)

    console.print(f"[bold green]✅ {prefix} clip 完成：{group.label}[/bold green]")
    return True, []


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


def product_display_name(product: dict) -> str:
    props = product.get("properties") or {}
    return str(
        product.get("Name")
        or props.get("title")
        or props.get("productIdentifier")
        or product.get("Id")
        or product.get("id")
        or "未知产品"
    )


def remove_zero_byte_file(path: Path, label: str) -> bool:
    """删除会被“文件已存在”逻辑误判为完成的 0 字节文件。"""
    try:
        if not path.is_file() or path.stat().st_size != 0:
            return False
        path.unlink()
    except FileNotFoundError:
        return False
    except OSError as exc:
        raise OSError(f"无法删除 0 字节{label}：{path}，{exc}") from exc

    console.print(
        f"[bold yellow]🧹 已删除 0 字节{label}，将重新下载或生成：{path}[/bold yellow]"
    )
    return True


def cleanup_zero_byte_files(
    source_root: Path,
    city_output_root: Path,
    country_output_root: Path,
    temp_root: Path,
) -> int:
    """启动时清理各阶段遗留的空 TIFF/ZIP，避免被当作有效成果跳过。"""
    targets = (
        (source_root, {".tif"}, "源 TIFF"),
        (city_output_root, {".tif"}, "市级 TIFF"),
        (country_output_root, {".tif"}, "县级 TIFF"),
        (temp_root, {".zip"}, "临时 ZIP"),
    )
    removed = 0
    for root, suffixes, label in targets:
        if not root.exists():
            continue
        walk_errors: list[OSError] = []
        for directory, _, filenames in os.walk(
            root,
            onerror=walk_errors.append,
        ):
            for filename in filenames:
                path = Path(directory) / filename
                if path.suffix.lower() in suffixes:
                    removed += int(remove_zero_byte_file(path, label))
        for error in walk_errors:
            console.print(
                f"[bold yellow]⚠️ 扫描 0 字节文件时无法读取目录："
                f"{error.filename or root}，{error}[/bold yellow]"
            )
    return removed


def download_one_scene(
    client,
    group: DownloadGroup,
    scene_index: int,
    global_scene_index: int,
    global_scene_total: int,
    product: dict,
    max_retries: int,
    progress: Progress,
) -> tuple[str, str]:
    """只下载一景 ZIP；TIFF 抽取由独立队列处理。"""
    scene_total = len(group.products)
    prefix = (
        f"第 {group.index}/{group.total} 个任务｜"
        f"第 {scene_index}/{scene_total} 景"
    )
    product_id = str(product.get("Id") or product.get("id") or "").strip()
    display_name = product_display_name(product)
    file_id = download.safe_filename(display_name)
    output_file = group.temp_output_dir / f"{file_id}.zip"
    final_tif = download.tif_output_path(output_file, group.output_dir)
    remove_zero_byte_file(final_tif, "源 TIFF")
    remove_zero_byte_file(output_file, "临时 ZIP")
    console.print(
        f"[bold blue]📥 {prefix}开始：{group.label}｜{display_name}[/bold blue]"
    )
    status, file_id = client.download_product(
        product_id,
        temp_output_dir=group.temp_output_dir,
        final_output_dir=group.output_dir,
        display_name=display_name,
        max_retries=max_retries,
        progress=progress,
        product_index=global_scene_index,
        product_total=global_scene_total,
        defer_tif=True,
    )
    if status == "downloaded":
        console.print(
            f"[bold green]✅ {prefix}ZIP 就绪，等待抽取 TIFF："
            f"{group.label}｜{file_id}[/bold green]"
        )
    elif status == "skipped":
        console.print(
            f"[yellow]⏭️ {prefix}已有 TIFF：{group.label}｜{file_id}[/yellow]"
        )
    else:
        console.print(
            f"[bold red]❌ {prefix}本轮未完成：{group.label}｜{file_id}[/bold red]"
        )
    return status, file_id


def extract_one_scene(
    group: DownloadGroup,
    scene_index: int,
    product: dict,
) -> tuple[str, str]:
    """从下载队列留下的完整 ZIP 抽取 TIFF。"""
    display_name = product_display_name(product)
    file_id = download.safe_filename(display_name)
    output_file = group.temp_output_dir / f"{file_id}.zip"
    marker_file = group.temp_output_dir / f"{file_id}.txt"
    final_tif = download.tif_output_path(output_file, group.output_dir)
    prefix = (
        f"第 {group.index}/{group.total} 个任务｜"
        f"第 {scene_index}/{len(group.products)} 景｜"
    )
    status, _ = download.finalize_downloaded_product(
        output_file,
        final_tif,
        marker_file,
        scene_label=prefix,
        file_id=file_id,
        quiet=True,
    )
    return status, file_id


def download_groups_concurrently(
    groups: list[DownloadGroup],
    client_pool: download.AccountPool,
    max_workers: int,
    tif_extraction_workers: int,
    max_retries: int,
    on_group_ready: Callable[[DownloadGroup], None],
) -> bool:
    """跨日期填满下载槽；某组全部 TIFF 就绪后立即通知处理池。"""
    requested_workers, planned_workers, planned_accounts = (
        client_pool.concurrency_plan(max_workers)
    )
    active_clients = client_pool.login_active_clients(planned_accounts)
    account_count = len(active_clients)
    actual_workers = min(
        planned_workers,
        account_count * download.THREADS_PER_ACCOUNT,
    )
    worker_limits = [download.THREADS_PER_ACCOUNT] * account_count
    worker_limits[-1] = (
        actual_workers
        - download.THREADS_PER_ACCOUNT * (account_count - 1)
    )

    if requested_workers > planned_workers:
        console.print(
            f"[bold yellow]⚠️ 请求 {requested_workers} 个下载并发，但账号配置最多支持 "
            f"{planned_workers} 个，已自动调整[/bold yellow]"
        )
    if account_count < planned_accounts:
        console.print(
            f"[bold yellow]⚠️ 计划启用 {planned_accounts} 个账号，但只有 "
            f"{account_count} 个可用；下载并发调整为 {actual_workers}[/bold yellow]"
        )

    total_scenes = sum(len(group.products) for group in groups)
    console.print(
        f"\n[bold green]🚀 全局下载队列启动：{len(groups)} 个任务，"
        f"{total_scenes} 景，{actual_workers} 个并发槽位，"
        f"{account_count} 个账号[/bold green]"
    )
    for client, worker_limit in zip(active_clients, worker_limits):
        console.print(
            f"[blue]  账号 {client.account_id}：{worker_limit} 个并发槽位[/blue]"
        )
    tif_workers = max(1, int(tif_extraction_workers))
    console.print(
        f"[blue]  TIFF 抽取队列：{tif_workers} 个并发；"
        "转换期间下载槽继续工作[/blue]"
    )

    # 按任务顺序摊平；前一个日期景数不足时，空余槽位自然由后续日期补满。
    pending = []
    global_index = 0
    for group in groups:
        for scene_index, product in enumerate(group.products, 1):
            global_index += 1
            worker_slot = (global_index - 1) % actual_workers
            client_index = min(
                worker_slot // download.THREADS_PER_ACCOUNT,
                account_count - 1,
            )
            pending.append(
                (group, scene_index, global_index, product, client_index)
            )

    completed_by_group = {group.index: 0 for group in groups}
    handed_off_groups: set[int] = set()

    def mark_completed(group: DownloadGroup) -> None:
        completed_by_group[group.index] += 1
        completed = completed_by_group[group.index]
        console.print(
            f"[cyan]📊 第 {group.index}/{group.total} 个任务进度："
            f"{group.label}｜{completed}/{len(group.products)} 景[/cyan]"
        )
        if (
            completed == len(group.products)
            and group.index not in handed_off_groups
        ):
            handed_off_groups.add(group.index)
            console.print(
                f"[bold green]✅ 第 {group.index}/{group.total} 个任务全部 "
                "TIFF 就绪，立即提交 merge→clip；全局下载继续[/bold green]"
            )
            on_group_ready(group)

    retry_round = 0
    progress = Progress(
        TextColumn("[progress.description]{task.description}"),
        TextColumn("大小"),
        DownloadSizeGBColumn(),
        TextColumn("速度"),
        MegabytesPerSecondColumn(),
        console=download.console,
    )
    progress.start()

    try:
        while pending:
            if GRACEFUL_STOP.is_set():
                break
            retry_round += 1
            if retry_round > 1:
                console.print(
                    f"\n[bold yellow]🔁 全局队列重试第 {retry_round} 轮："
                    f"尚有 {len(pending)} 景未完成[/bold yellow]"
                )

            failed_items = []
            executors = [
                ThreadPoolExecutor(
                    max_workers=worker_limit,
                    thread_name_prefix=f"account-{client.account_id}",
                )
                for client, worker_limit in zip(active_clients, worker_limits)
            ]
            tif_executor = ProcessPoolExecutor(
                max_workers=tif_workers,
                mp_context=mp.get_context("spawn"),
                initializer=ignore_sigint,
            )
            try:
                download_futures = {}
                for (
                    group,
                    scene_index,
                    global_scene_index,
                    product,
                    client_index,
                ) in pending:
                    future = executors[client_index].submit(
                        download_one_scene,
                        active_clients[client_index],
                        group,
                        scene_index,
                        global_scene_index,
                        total_scenes,
                        product,
                        max_retries,
                        progress,
                    )
                    download_futures[future] = (
                        group,
                        scene_index,
                        global_scene_index,
                        product,
                        client_index,
                    )

                tif_futures = {}
                while download_futures or tif_futures:
                    if GRACEFUL_STOP.is_set() and download_futures:
                        cancelled = 0
                        for future in list(download_futures):
                            if future.cancel():
                                download_futures.pop(future)
                                cancelled += 1
                        if cancelled:
                            console.print(
                                f"[bold yellow]⏹️ 已取消 {cancelled} 个尚未开始的下载；"
                                "正在运行的任务继续安全收尾[/bold yellow]"
                            )
                    active_futures = set(download_futures) | set(tif_futures)
                    if not active_futures:
                        break
                    done, _ = wait(
                        active_futures,
                        return_when=FIRST_COMPLETED,
                    )
                    for future in done:
                        if future in download_futures:
                            item = download_futures.pop(future)
                            group = item[0]
                            try:
                                status, _ = future.result()
                            except Exception as exc:
                                status = "failed"
                                console.print(
                                    f"[bold red]❌ 第 {group.index}/{group.total} 个任务｜"
                                    f"第 {item[1]}/{len(group.products)} 景下载任务异常："
                                    f"{exc}[/bold red]"
                                )

                            if status == "failed":
                                failed_items.append(item)
                            elif status == "downloaded":
                                tif_future = tif_executor.submit(
                                    extract_one_scene,
                                    group,
                                    item[1],
                                    item[3],
                                )
                                tif_futures[tif_future] = item
                                running = min(len(tif_futures), tif_workers)
                                queued = max(0, len(tif_futures) - running)
                                console.print(
                                    f"[cyan]🧩 TIFF 抽取并发 {running}/{tif_workers}｜"
                                    f"排队 {queued}[/cyan]"
                                )
                            else:
                                mark_completed(group)
                            continue

                        item = tif_futures.pop(future)
                        group = item[0]
                        try:
                            status, _ = future.result()
                        except Exception as exc:
                            status = "failed"
                            console.print(
                                f"[bold red]❌ 第 {group.index}/{group.total} 个任务｜"
                                f"第 {item[1]}/{len(group.products)} 景 TIFF 抽取异常："
                                f"{exc}[/bold red]"
                            )

                        if status == "failed":
                            failed_items.append(item)
                        else:
                            mark_completed(group)
                        running = min(len(tif_futures), tif_workers)
                        queued = max(0, len(tif_futures) - running)
                        console.print(
                            f"[cyan]🧩 TIFF 抽取并发 {running}/{tif_workers}｜"
                            f"排队 {queued}[/cyan]"
                        )
            finally:
                tif_executor.shutdown(wait=True)
                for executor in executors:
                    executor.shutdown(wait=True)

            pending = failed_items
            if GRACEFUL_STOP.is_set():
                break
            if pending:
                console.print(
                    f"[yellow]⏳ 本轮仍有 {len(pending)} 景未完成；"
                    "其他日期已继续下载，30 秒后重试这些失败景[/yellow]"
                )
                if GRACEFUL_STOP.wait(30):
                    break
    finally:
        progress.stop()
    return GRACEFUL_STOP.is_set()


def main() -> int:
    args = parse_args()
    original_sigint_handler = signal.getsignal(signal.SIGINT)
    graceful_handler_installed = False
    try:
        accounts, cities, settings = download.load_config(args.config_file)
        if not settings.get("output_dir"):
            raise ValueError("config.json 中的 output_dir 不能为空")
        args.temp_dir = settings.get("temp_dir") or args.temp_dir
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
        bypass_proxy=settings["bypass_proxy"],
    )

    console.print("[bold cyan]🌍 下载 → 市级合并 → 县级裁剪联合流水线[/bold cyan]")
    console.print(f"[blue]下载根目录：{source_root}[/blue]")
    console.print(f"[blue]市级输出目录：{city_output_root}[/blue]")
    console.print(f"[blue]县级输出目录：{country_output_root}[/blue]")
    console.print(
        f"[blue]下载并发：{download_workers}；TIFF 抽取："
        f"{settings['tif_extraction_workers']}；完整 merge→clip ："
        f"{processing_workers}[/blue]"
    )

    try:
        groups = collect_groups(tasks, client_pool, settings, args)
        if not groups:
            console.print("[bold red]❌ 没有找到可下载的日期卫星任务[/bold red]")
            return 1

        console.print(
            f"\n[bold green]✅ 共生成 {len(groups)} 个下载任务；"
            "所有影像进入同一个全局下载队列，任务就绪后立即进入 "
            "merge→clip[/bold green]"
        )
        for group in groups:
            console.print(
                f"[green]  第 {group.index}/{group.total} 个任务："
                f"{group.label}，{len(group.products)} 景[/green]"
            )
        if args.search_only:
            return 0

        GRACEFUL_STOP.clear()

        def request_graceful_stop(signum, frame) -> None:
            del signum, frame
            if GRACEFUL_STOP.is_set():
                console.print(
                    "[bold yellow]⏳ 正在安全收尾，请等待当前文件和处理任务完成…"
                    "[/bold yellow]"
                )
                return
            GRACEFUL_STOP.set()
            console.print(
                "\n[bold yellow]⏹️ 收到 Ctrl+C：停止启动新任务；"
                "当前下载、TIFF 抽取及已提交的 merge/clip 将安全收尾…"
                "[/bold yellow]"
            )

        signal.signal(signal.SIGINT, request_graceful_stop)
        graceful_handler_installed = True

        try:
            removed_zero_byte_files = cleanup_zero_byte_files(
                source_root,
                city_output_root,
                country_output_root,
                temp_root,
            )
        except OSError as exc:
            console.print(f"[bold red]❌ 清理 0 字节文件失败：{exc}[/bold red]")
            return 1
        console.print(
            f"[blue]启动时已清理 0 字节 TIFF/ZIP："
            f"{removed_zero_byte_files} 个[/blue]"
        )
        if GRACEFUL_STOP.is_set():
            console.print("[bold green]✅ 已安全停止，尚未启动下载任务[/bold green]")
            return 130

        city_output_root.mkdir(parents=True, exist_ok=True)
        country_output_root.mkdir(parents=True, exist_ok=True)
        processing_futures = {}
        with ProcessPoolExecutor(
            max_workers=processing_workers,
            mp_context=mp.get_context("spawn"),
            initializer=ignore_sigint,
        ) as processing_pool:
            def submit_processing(group: DownloadGroup) -> None:
                if GRACEFUL_STOP.is_set():
                    console.print(
                        f"[bold yellow]⏹️ {group.label} TIFF 已就绪；"
                        "因正在安全退出，本次不再启动 merge→clip[/bold yellow]"
                    )
                    return
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
                running = min(len(processing_futures), processing_workers)
                queued = max(0, len(processing_futures) - running)
                console.print(
                    f"[magenta]⚙️ merge→clip 并发 {running}/{processing_workers}｜"
                    f"排队 {queued}[/magenta]"
                )

            graceful_stopped = download_groups_concurrently(
                groups,
                client_pool,
                max_workers=download_workers,
                tif_extraction_workers=settings["tif_extraction_workers"],
                max_retries=args.max_retries,
                on_group_ready=submit_processing,
            )

            processing_failures: list[FailureDetail] = []
            completed_processing = 0
            for future in as_completed(processing_futures):
                group = processing_futures[future]
                try:
                    success, failure_details = future.result()
                except Exception as exc:
                    success = False
                    reason = f"merge/clip 处理异常：{type(exc).__name__}: {exc}"
                    message = f"第 {group.index}/{group.total} 个任务异常：{reason}"
                    failure_details = group_failure_details(
                        group,
                        "merge/clip",
                        reason,
                    )
                    console.print(f"[bold red]❌ {message}[/bold red]")
                if not success:
                    processing_failures.extend(failure_details)
                completed_processing += 1
                remaining = len(processing_futures) - completed_processing
                running = min(remaining, processing_workers)
                queued = max(0, remaining - running)
                console.print(
                    f"[magenta]⚙️ merge→clip 并发 {running}/{processing_workers}｜"
                    f"已完成 {completed_processing}/{len(processing_futures)}｜"
                    f"排队 {queued}[/magenta]"
                )

        if graceful_stopped or GRACEFUL_STOP.is_set():
            console.print(
                "\n[bold green]✅ 已安全退出：正在运行的文件写入和已提交处理均已收尾；"
                "未开始的任务将在下次运行时继续[/bold green]"
            )
            return 130

        console.print("\n[bold cyan]========== 联合流水线结果 ==========[/bold cyan]")
        console.print(f"任务总数：{len(groups)}")
        console.print("download 未交接：0")
        console.print(f"merge/clip 失败 ZIP：{len(processing_failures)}")
        if processing_failures:
            print_failure_details(processing_failures)
            console.print("[bold yellow]⚠️ 流水线结束，但存在未完成任务[/bold yellow]")
            return 1
        console.print("[bold green]🎉 所有 download→merge→clip 任务均已完成[/bold green]")
        return 0
    except KeyboardInterrupt:
        console.print("\n[bold yellow]⚠️ 已停止联合流水线[/bold yellow]")
        return 130
    finally:
        if graceful_handler_installed:
            signal.signal(signal.SIGINT, original_sigint_handler)
        if temp_root.exists():
            console.print(
                f"[blue]💾 已保留临时目录，未完成文件可在下次续传："
                f"{temp_root}[/blue]"
            )


if __name__ == "__main__":
    sys.exit(main())
