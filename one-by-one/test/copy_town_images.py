r"""
根据乡镇 TXT 中记录的影像路径，将影像复制到对应旗县的乡镇影像目录。

输出示例：
    赤峰市种植用地两个镇选择/
        红山区/
            红庙子镇影像/
                K50E010021_2025.tif

默认仅预览，不复制文件；添加 --execute 后正式复制。
"""

import argparse
import os
import re
import subprocess
import shutil
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path


DEFAULT_ROOT = (
    r"\\169.254.51.68\data\专题2_农作物种植用地遥感测量"
    r"\第四批跟班学习（赤峰市_乌海市）\赤峰市"
    r"\赤峰市种植用地两个镇选择"
)

TOWN_TO_COUNTY = {
    "红庙子镇": "红山区",
    "安庆镇": "松山区",
    "十三敖包镇": "巴林左旗",
    "文钟镇": "红山区",
    "新苏莫苏木": "翁牛特旗",
    "四道湾子镇": "敖汉旗",
    "五分地镇": "翁牛特旗",
    "元宝山镇": "元宝山区",
    "太平地镇": "松山区",
    "汐子镇": "宁城县",
    "巴彦花镇": "阿鲁科尔沁旗",
    "隆昌镇": "巴林左旗",
    "天山口镇": "阿鲁科尔沁旗",
    "西拉沐沦苏木": "巴林右旗",
    "巴彦塔拉苏木": "巴林右旗",
    "忙农镇": "宁城县",
    "统部镇": "林西县",
    "小牛群镇": "喀喇沁旗",
    "古鲁板蒿镇": "敖汉旗",
    "乃林镇": "喀喇沁旗",
    "土城子镇": "克什克腾旗",
    "小五家乡": "元宝山区",
    "新城子镇": "林西县",
    "万合永镇": "克什克腾旗",
}


def convert_network_path(path):
    """将 169.254.51.x 的 Windows NAS 路径转换为 Linux 挂载路径。"""
    if path is None:
        return path

    path = str(path).strip().strip('"')
    if not path:
        return path

    normalized = path.replace("\\", "/")
    mappings = {
        "data": "/media/cangling/nas_folder",
        "新建卷": "/media/cangling/xinjianjuan",
        "datadisk2": "/media/cangling/EAGET",
        "新加卷": "/media/cangling/xinjiajuan",
    }

    parts = normalized.lstrip("/").split("/")
    if len(parts) < 2 or not parts[0].startswith("169.254.51."):
        # 已经是 /media/... 路径时原样返回。
        return normalized if normalized.startswith("/") else path

    try:
        last_ip_number = int(parts[0].rsplit(".", 1)[1])
    except (IndexError, ValueError):
        return path

    if not 1 <= last_ip_number <= 255:
        return path

    share_name = parts[1]
    linux_prefix = next(
        (
            target
            for name, target in mappings.items()
            if share_name.casefold() == name.casefold()
        ),
        None,
    )
    if linux_prefix is None:
        return path

    relative_path = "/".join(parts[2:])
    return f"{linux_prefix}/{relative_path}" if relative_path else linux_prefix


def read_txt_lines(txt_path):
    """读取 TXT 中的非空行，兼容 UTF-8 BOM 和 GB18030。"""
    for encoding in ("utf-8-sig", "gb18030"):
        try:
            with txt_path.open("r", encoding=encoding) as file:
                return [
                    line.strip().strip('"')
                    for line in file
                    if line.strip().strip('"')
                ]
        except UnicodeDecodeError:
            continue
    raise UnicodeError(f"无法识别 TXT 编码：{txt_path}")


def identify_town(txt_path):
    """兼容“元宝山镇.txt”和“元宝山镇边界.txt”等文件名。"""
    matches = [town for town in TOWN_TO_COUNTY if town in txt_path.stem]
    return max(matches, key=len) if matches else None


def source_from_line(line):
    """将 TXT 中的一行转换成可访问的影像源路径。"""
    return Path(convert_network_path(line))


def same_file(source, destination):
    """以文件大小快速判断目标文件是否已经复制。"""
    return destination.exists() and source.stat().st_size == destination.stat().st_size


def format_bytes(byte_count):
    """将字节数转换为便于阅读的容量或速度。"""
    value = float(byte_count)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            return f"{value:.2f} {unit}"
        value /= 1024


def rsync_one_image(index, total, source, destination):
    """一个线程调用一次 rsync，只传输一个影像。"""
    destination.parent.mkdir(parents=True, exist_ok=True)
    file_size = source.stat().st_size
    started_at = time.perf_counter()

    process = subprocess.Popen(
        [
            "rsync",
            "--archive",
            "--human-readable",
            "--info=progress2",
            str(source),
            str(destination),
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        bufsize=0,
        env={**os.environ, "LC_ALL": "C"},
    )

    # rsync 的进度使用回车符 \r 刷新。逐条读取并限制为每秒打印一次，
    # 避免多线程传输时产生过密的终端输出。
    progress_pattern = re.compile(
        r"(\d+)%\s+([0-9.,]+\s*[kKMGT]?B/s)",
        re.IGNORECASE,
    )
    output_buffer = bytearray()
    last_output = ""
    last_report_at = 0.0

    while True:
        chunk = process.stdout.read(1)
        if not chunk:
            break

        if chunk not in (b"\r", b"\n"):
            output_buffer.extend(chunk)
            continue

        if not output_buffer:
            continue

        output_line = output_buffer.decode("utf-8", errors="replace").strip()
        output_buffer.clear()
        if not output_line:
            continue

        last_output = output_line
        match = progress_pattern.search(output_line)
        current_time = time.perf_counter()
        if match and current_time - last_report_at >= 1.0:
            percent, current_speed = match.groups()
            print(
                f"[传输中 {index}/{total}] {source.name} | "
                f"进度 {percent}% | 当前速度 {current_speed.replace(' ', '')}",
                flush=True,
            )
            last_report_at = current_time

    # 处理结尾没有换行或回车的少量输出。
    if output_buffer:
        last_output = output_buffer.decode("utf-8", errors="replace").strip()

    returncode = process.wait()

    elapsed = max(time.perf_counter() - started_at, 0.001)
    average_speed = file_size / elapsed
    return {
        "index": index,
        "total": total,
        "source": source,
        "destination": destination,
        "size": file_size,
        "elapsed": elapsed,
        "speed": average_speed,
        "success": returncode == 0,
        "returncode": returncode,
        "output": last_output,
    }


def scan_tasks(root_directory):
    """递归扫描全部 TXT，生成影像复制任务。"""
    tasks = []
    ignored_txt = []
    stats = defaultdict(lambda: {"txt_count": 0, "line_count": 0})

    for current_path, _, filenames in os.walk(root_directory):
        current_directory = Path(current_path)

        for filename in filenames:
            if not filename.casefold().endswith(".txt"):
                continue

            txt_path = current_directory / filename
            town = identify_town(txt_path)
            if town is None:
                ignored_txt.append(txt_path)
                continue

            lines = read_txt_lines(txt_path)
            stats[town]["txt_count"] += 1
            stats[town]["line_count"] += len(lines)

            for line_number, line in enumerate(lines, start=1):
                source = source_from_line(line)
                tasks.append((town, source, txt_path, line_number))

    return tasks, stats, ignored_txt


def run(root_path, execute=False, workers=4):
    root_directory = Path(convert_network_path(root_path))

    print("=" * 78)
    print("需求：读取全部乡镇 TXT，并把每行对应的影像复制到旗县下的乡镇目录")
    print(f"TXT及输出根目录：{root_directory}")
    print("输出规则：旗县名称/镇名影像/影像文件")
    print(f"运行模式：{'正式复制' if execute else '仅预览，不修改文件'}")
    print(f"复制工具：rsync；并发线程：{workers}")
    print("=" * 78)

    if not root_directory.is_dir():
        raise NotADirectoryError(f"根目录不存在或不是文件夹：{root_directory}")

    tasks, stats, ignored_txt = scan_tasks(root_directory)

    if ignored_txt:
        print(f"\n以下 {len(ignored_txt)} 个 TXT 无法从字典识别镇名，已忽略：")
        for txt_path in ignored_txt:
            print(f"  {txt_path}")

    if not tasks:
        print("\n没有找到可以处理的影像路径。")
        return

    # 同一镇内重复记录同一源文件时，只复制一次。
    unique_tasks = {}
    for task in tasks:
        town, source, _, _ = task
        try:
            source_key = str(source.resolve(strict=False)).casefold()
        except OSError:
            source_key = str(source).casefold()
        unique_tasks.setdefault((town, source_key), task)

    found_count = 0
    existing_count = 0
    missing = []
    conflicts = []
    pending = []
    planned_destinations = set()

    print("\n影像处理明细：")
    for town, source, txt_path, line_number in unique_tasks.values():
        county = TOWN_TO_COUNTY[town]
        destination_directory = root_directory / county / f"{town}影像"

        if not source.is_file():
            missing.append((town, txt_path, line_number, source))
            print(
                f"[缺失] {source}\n"
                f"       来自：{txt_path} 第 {line_number} 行"
            )
            continue

        found_count += 1
        destination = destination_directory / source.name

        if same_file(source, destination):
            existing_count += 1
            print(f"[已存在] {destination}")
            continue

        if destination.exists():
            conflicts.append((source, destination))
            print(f"[冲突-未覆盖] {destination}")
            continue

        destination_key = str(destination).casefold()
        if destination_key in planned_destinations:
            conflicts.append((source, destination))
            print(f"[任务冲突-未复制] 多个源影像对应同一目标：{destination}")
            continue

        planned_destinations.add(destination_key)
        pending.append((source, destination))
        print(f"[待复制] {source}")
        print(f"        -> {destination}")

    copied_count = 0
    failed = []
    transferred_bytes = 0
    transfer_elapsed = 0.0

    if execute and pending:
        if shutil.which("rsync") is None:
            raise RuntimeError(
                "系统中没有找到 rsync，请先安装 rsync 后再使用 --execute。"
            )

        print("\n" + "=" * 78)
        print(f"开始使用 {workers} 个线程并发复制 {len(pending)} 个影像……")
        transfer_started_at = time.perf_counter()

        with ThreadPoolExecutor(max_workers=workers) as executor:
            future_to_task = {}
            total_pending = len(pending)
            completed_count = 0

            for index, (source, destination) in enumerate(pending, start=1):
                print(
                    f"[提交 第 {index}/{total_pending} 个] "
                    f"{source.name} -> {destination.parent}"
                )
                future = executor.submit(
                    rsync_one_image,
                    index,
                    total_pending,
                    source,
                    destination,
                )
                future_to_task[future] = (source, destination)

            for future in as_completed(future_to_task):
                source, destination = future_to_task[future]
                completed_count += 1
                try:
                    result = future.result()
                except Exception as error:
                    failed.append((source, destination, str(error)))
                    print(
                        f"[失败 | 总进度 {completed_count}/{total_pending}] "
                        f"{source.name}：{error}"
                    )
                    continue

                if result["success"]:
                    copied_count += 1
                    transferred_bytes += result["size"]
                    current_total_elapsed = max(
                        time.perf_counter() - transfer_started_at,
                        0.001,
                    )
                    file_speed_mb = result["speed"] / 1024 / 1024
                    total_speed_mb = (
                        transferred_bytes / current_total_elapsed / 1024 / 1024
                    )
                    print(
                        f"[完成任务 {result['index']}/{result['total']} | "
                        f"总进度 {completed_count}/{total_pending}] "
                        f"{source.name} | 大小 {format_bytes(result['size'])} | "
                        f"耗时 {result['elapsed']:.2f} 秒 | "
                        f"本文件速度 {file_speed_mb:.2f} MB/s | "
                        f"总体速度 {total_speed_mb:.2f} MB/s"
                    )
                else:
                    error_detail = result["output"].splitlines()[-1] if result["output"] else "无错误输出"
                    failed.append((source, destination, error_detail))
                    print(
                        f"[失败任务 {result['index']}/{result['total']} | "
                        f"总进度 {completed_count}/{total_pending}] "
                        f"{source.name} | rsync退出码 {result['returncode']} | "
                        f"{error_detail}"
                    )

        transfer_elapsed = max(time.perf_counter() - transfer_started_at, 0.001)

    print("\n" + "=" * 78)
    print("按乡镇统计：")
    for town in sorted(stats):
        line_count = stats[town]["line_count"]
        unique_count = sum(1 for key in unique_tasks if key[0] == town)
        duplicate_count = line_count - unique_count
        message = (
            f"  {town}（{TOWN_TO_COUNTY[town]}）："
            f"TXT路径 {line_count} 条，唯一影像 {unique_count} 个"
        )
        if duplicate_count:
            message += f"，重复路径 {duplicate_count} 条"
        print(message)

    print("-" * 78)
    print(f"TXT路径总数：{len(tasks)} 条")
    print(f"唯一影像任务：{len(unique_tasks)} 个")
    print(f"源影像已找到：{found_count} 个")
    print(f"源影像缺失：{len(missing)} 个")
    print(f"目标已存在且大小相同：{existing_count} 个")
    print(f"目标同名冲突且未覆盖：{len(conflicts)} 个")
    if execute:
        print(f"本次成功复制：{copied_count} 个")
        print(f"本次复制失败：{len(failed)} 个")
        print(f"成功传输数据量：{format_bytes(transferred_bytes)}")
        if copied_count:
            print(f"并发总耗时：{transfer_elapsed:.2f} 秒")
            print(
                f"总体有效传输速度："
                f"{format_bytes(transferred_bytes / transfer_elapsed)}/s"
            )
    else:
        print(f"等待复制：{len(pending)} 个")
        print("当前为预览模式，没有复制文件；确认后添加 --execute。")


def main():
    parser = argparse.ArgumentParser(
        description="根据乡镇 TXT 中的路径复制对应影像"
    )
    parser.add_argument(
        "--root",
        default=DEFAULT_ROOT,
        help="TXT 所在根目录，同时作为输出根目录",
    )
    parser.add_argument(
        "--execute",
        action="store_true",
        help="正式复制；不添加该参数时只预览",
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=4,
        help="rsync 并发线程数，默认 4",
    )
    args = parser.parse_args()
    if args.workers < 1:
        parser.error("--workers 必须大于或等于 1")
    run(args.root, args.execute, args.workers)


if __name__ == "__main__":
    main()
