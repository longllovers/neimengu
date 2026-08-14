r"""
按乡镇整理边界 Shapefile 文件。

输入示例：
    \\169.254.51.68\data\专题2_农作物种植用地遥感测量\第四批跟班学习（赤峰市_乌海市）\赤峰市\赤峰市种植用地两个镇选择

整理前：
    赤峰市种植用地两个镇选择/
        阿鲁科尔沁旗/
            巴彦花镇边界.shp
            巴彦花镇边界.dbf
            天山口镇边界.shp
            天山口镇边界.dbf

整理后：
    赤峰市种植用地两个镇选择/
        阿鲁科尔沁旗/
            巴彦花镇/
                巴彦花镇边界.shp
                巴彦花镇边界.dbf
            天山口镇/
                天山口镇边界.shp
                天山口镇边界.dbf

默认只预览，不会修改文件；增加 --execute 才会正式执行。
"""

import argparse
import os
import re
import shutil
from pathlib import Path


DEFAULT_INPUT_PATH = (
    r"\\169.254.51.68\data\专题2_农作物种植用地遥感测量"
    r"\第四批跟班学习（赤峰市_乌海市）\赤峰市\赤峰市种植用地两个镇选择"
)

# 一套 Shapefile 中可能出现的文件扩展名。
SHAPEFILE_EXTENSIONS = {
    ".shp",
    ".shx",
    ".dbf",
    ".prj",
    ".cpg",
    ".sbn",
    ".sbx",
    ".qix",
    ".fix",
    ".ain",
    ".aih",
    ".ixs",
    ".mxs",
    ".atx",
    ".shp.xml",
}


def convert_network_path(path):
    """把 Windows 网络共享路径转换为 Linux 挂载路径。"""
    if path is None:
        return path

    path = str(path).strip()
    if not path:
        return path

    path = path.replace("\\", "/")

    share_mapping = {
        "data": "/media/cangling/nas_folder",
        "新建卷": "/media/cangling/xinjianjuan",
        "datadisk2": "/media/cangling/EAGET",
        "新加卷": "/media/cangling/xinjiajuan",
    }

    pattern = re.compile(
        r"^/*169\.254\.51\.(?:[1-9]|[1-9]\d|1\d{2}|2[0-4]\d|25[0-5])"
        r"/([^/]+)(?:/(.*))?$",
        re.IGNORECASE,
    )
    match = pattern.match(path)
    if not match:
        return path

    share_name, relative_path = match.groups()
    linux_prefix = next(
        (
            target
            for name, target in share_mapping.items()
            if share_name.casefold() == name.casefold()
        ),
        None,
    )
    if linux_prefix is None:
        return path

    return f"{linux_prefix}/{relative_path}" if relative_path else linux_prefix


def complete_extension(filename):
    """正确取得扩展名，包括 .shp.xml 这种双扩展名。"""
    if filename.casefold().endswith(".shp.xml"):
        return ".shp.xml"
    return Path(filename).suffix.casefold()


def extract_town_name(filename):
    """从“巴彦花镇边界.shp”中取得“巴彦花镇”。"""
    if complete_extension(filename) not in SHAPEFILE_EXTENSIONS:
        return None

    match = re.match(r"^(.+?(?:镇|乡|苏木|街道))边界(?:\.|$)", filename)
    return match.group(1) if match else None


def same_file(source, destination):
    """快速判断两个文件是否可以视为相同文件。"""
    return (
        destination.exists()
        and source.stat().st_size == destination.stat().st_size
    )


def unique_destination(source, town_directory):
    """避免覆盖同名但内容大小不同的文件。"""
    destination = town_directory / source.name
    if not destination.exists():
        return destination
    if same_file(source, destination):
        return None

    extension = complete_extension(source.name)
    base_name = source.name[: -len(extension)]
    number = 1
    while True:
        candidate = town_directory / f"{base_name}_重复{number}{extension}"
        if not candidate.exists():
            return candidate
        number += 1


def collect_tasks(root_directory):
    """收集各旗县目录内的文件，并整理到其所属旗县的乡镇目录。"""
    tasks = []

    for current_path, directory_names, file_names in os.walk(root_directory):
        current_directory = Path(current_path)

        # 已经生成的乡镇目录不再扫描，避免重复整理。
        directory_names[:] = [
            name
            for name in directory_names
            if not re.search(r"(?:镇|乡|苏木|街道)$", name)
        ]

        relative_directory = current_directory.relative_to(root_directory)

        # 总根目录中的文件不属于某个旗县，不处理。
        if relative_directory == Path("."):
            continue

        # 相对路径的第一级就是该文件所属的旗/县/区目录。
        county_directory = root_directory / relative_directory.parts[0]

        for filename in file_names:
            town_name = extract_town_name(filename)
            if not town_name:
                continue

            source = current_directory / filename
            town_directory = county_directory / town_name

            if source.parent == town_directory:
                continue

            tasks.append((source, town_directory))

    return tasks


def organize(input_path, mode="copy", execute=False):
    converted_path = convert_network_path(input_path)
    root_directory = Path(converted_path)

    print("=" * 72)
    print("需求：在每个旗县目录内创建乡镇文件夹并整理对应边界文件")
    print(f"输入路径：{input_path}")
    print(f"实际路径：{root_directory}")
    print(f"输出位置：{root_directory}/旗县名称/乡镇名称/")
    print(f"处理方式：{'复制（保留原文件）' if mode == 'copy' else '移动'}")
    print(f"当前模式：{'正式执行' if execute else '预览，不修改文件'}")
    print("=" * 72)

    if not root_directory.exists():
        raise FileNotFoundError(f"目录不存在：{root_directory}")
    if not root_directory.is_dir():
        raise NotADirectoryError(f"输入路径不是目录：{root_directory}")

    tasks = collect_tasks(root_directory)
    if not tasks:
        print("没有找到符合“乡镇名称+边界+Shapefile扩展名”的文件。")
        return

    town_count = len({directory.name for _, directory in tasks})
    print(f"\n共发现 {town_count} 个乡镇、{len(tasks)} 个文件。\n")

    handled = 0
    skipped = 0
    for source, town_directory in tasks:
        destination = unique_destination(source, town_directory)
        if destination is None:
            print(f"[跳过-已存在] {town_directory / source.name}")
            skipped += 1
            continue

        action = "复制" if mode == "copy" else "移动"
        print(f"[{action}] {source}")
        print(f"       -> {destination}")

        if execute:
            town_directory.mkdir(parents=True, exist_ok=True)
            if mode == "copy":
                shutil.copy2(source, destination)
            else:
                shutil.move(str(source), str(destination))
            handled += 1

    print("\n" + "=" * 72)
    if execute:
        print(f"完成：成功处理 {handled} 个文件，跳过 {skipped} 个已存在文件。")
    else:
        print("预览结束，没有修改文件。确认无误后增加 --execute 正式执行。")


def main():
    parser = argparse.ArgumentParser(description="按乡镇汇总边界 Shapefile 文件")
    parser.add_argument(
        "input_path",
        nargs="?",
        default=DEFAULT_INPUT_PATH,
        help="输入根目录；不填写时使用代码中的默认网络路径",
    )
    parser.add_argument(
        "--mode",
        choices=("copy", "move"),
        default="copy",
        help="copy=复制并保留原文件，move=移动原文件；默认 copy",
    )
    parser.add_argument(
        "--execute",
        action="store_true",
        help="正式执行；不加该参数时仅预览",
    )
    args = parser.parse_args()
    organize(args.input_path, args.mode, args.execute)


if __name__ == "__main__":
    main()
