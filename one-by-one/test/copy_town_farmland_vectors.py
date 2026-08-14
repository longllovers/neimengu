r"""
根据乡镇 TXT 中的影像路径，复制对应的耕地矢量文件。

目标结构示例：
    赤峰市种植用地两个镇选择/
        红山区/
            红庙子镇耕地矢量/
                K50E010021_2025.shp
                K50E010021_2025.shx
                K50E010021_2025.dbf
                K50E010021_2025.prj
                K50E010021_2025.cpg

默认仅预览；添加 --execute 后才会正式复制。
"""

import argparse
import os
import shutil
from collections import defaultdict
from pathlib import Path, PurePosixPath, PureWindowsPath


DEFAULT_TXT_ROOT = (
    r"\\169.254.51.68\data\专题2_农作物种植用地遥感测量"
    r"\第四批跟班学习（赤峰市_乌海市）\赤峰市"
    r"\赤峰市种植用地两个镇选择"
)

DEFAULT_VECTOR_ROOT = (
    r"\\169.254.51.68\data\北京预测结果传递\地块结果"
    r"\所有地块结果最新-去除接边"
)

VECTOR_EXTENSIONS = (".shp", ".shx", ".dbf", ".prj", ".cpg")

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
    """将指定的 Windows NAS 路径转换为 Linux 挂载路径。"""
    if path is None:
        return path

    path = str(path).strip()
    if not path:
        return path

    normalized = path.replace("\\", "/")
    mappings = {
        "data": "/media/cangling/nas_folder",
        "新建卷": "/media/cangling/xinjianjuan",
        "datadisk2": "/media/cangling/EAGET",
        "新加卷": "/media/cangling/xinjiajuan",
    }

    # 去除 UNC 开头的斜杠后分段：169.254.51.x/共享名/相对路径。
    parts = normalized.lstrip("/").split("/")
    if len(parts) < 2 or not parts[0].startswith("169.254.51."):
        return path

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
    """兼容常见的 UTF-8、UTF-8 BOM 和中文 Windows 文本编码。"""
    for encoding in ("utf-8-sig", "gb18030"):
        try:
            with txt_path.open("r", encoding=encoding) as file:
                return [line.strip().strip('"') for line in file if line.strip()]
        except UnicodeDecodeError:
            continue
    raise UnicodeError(f"无法识别 TXT 编码：{txt_path}")


def identify_town(txt_path):
    """从 TXT 文件名识别字典中的镇名。"""
    name = txt_path.stem.strip()
    matches = [town for town in TOWN_TO_COUNTY if town in name]
    if not matches:
        return None
    # 若名称意外包含多个候选，优先使用最长、最具体的名称。
    return max(matches, key=len)


def image_stem_from_line(line):
    """从 Linux 或 Windows 路径中提取不带扩展名的影像名称。"""
    normalized = line.strip().strip('"').replace("\\", "/")
    filename = PurePosixPath(normalized).name
    if not filename:
        # 极端情况下再按 Windows 路径解析一次。
        filename = PureWindowsPath(line).name
    return Path(filename).stem if filename else None


def scan_txt_files(txt_root):
    """递归扫描所有 TXT，返回复制任务和统计信息。"""
    tasks = []
    stats = defaultdict(lambda: {
        "txt_files": 0,
        "line_count": 0,
        "unique_images": set(),
    })
    ignored_txt = []

    for current_path, _, filenames in os.walk(txt_root):
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
            stats[town]["txt_files"] += 1

            for line_number, line in enumerate(lines, start=1):
                image_stem = image_stem_from_line(line)
                if not image_stem:
                    print(f"[无效路径] {txt_path} 第 {line_number} 行：{line}")
                    continue

                stats[town]["line_count"] += 1
                stats[town]["unique_images"].add(image_stem)
                tasks.append((town, image_stem, txt_path, line_number))

    return tasks, stats, ignored_txt


def files_are_same(source, destination):
    """用文件大小判断目标是否已有同一文件。"""
    return destination.exists() and source.stat().st_size == destination.stat().st_size


def run(txt_root_path, vector_root_path, execute=False):
    txt_root = Path(convert_network_path(txt_root_path))
    vector_root = Path(convert_network_path(vector_root_path))

    print("=" * 78)
    print("需求：读取乡镇 TXT 中的影像编号，复制对应的 5 种耕地矢量文件")
    print(f"TXT及输出根目录：{txt_root}")
    print(f"矢量源目录：{vector_root}")
    print("所需文件：.shp、.shx、.dbf、.prj、.cpg")
    print("输出规则：旗县名称/镇名耕地矢量/")
    print(f"运行模式：{'正式复制' if execute else '仅预览，不修改文件'}")
    print("=" * 78)

    if not txt_root.is_dir():
        raise NotADirectoryError(f"TXT 根目录不存在或不是文件夹：{txt_root}")
    if not vector_root.is_dir():
        raise NotADirectoryError(f"矢量源目录不存在或不是文件夹：{vector_root}")

    tasks, stats, ignored_txt = scan_txt_files(txt_root)
    if ignored_txt:
        print(f"\n以下 {len(ignored_txt)} 个 TXT 无法从字典识别镇名，已忽略：")
        for path in ignored_txt:
            print(f"  {path}")

    if not tasks:
        print("\n没有发现可以处理的影像路径。")
        return

    # 同一镇内重复出现相同影像时只处理一次，否则会重复复制同一组文件。
    unique_tasks = {}
    for town, image_stem, txt_path, line_number in tasks:
        unique_tasks.setdefault(
            (town, image_stem),
            (town, image_stem, txt_path, line_number),
        )

    found_count = 0
    copied_count = 0
    existing_count = 0
    missing = []
    conflicts = []

    print("\n文件处理明细：")
    for town, image_stem, txt_path, line_number in unique_tasks.values():
        county = TOWN_TO_COUNTY[town]
        destination_directory = txt_root / county / f"{town}耕地矢量"

        for extension in VECTOR_EXTENSIONS:
            source = vector_root / f"{image_stem}{extension}"
            destination = destination_directory / source.name

            if not source.is_file():
                missing.append((town, txt_path, line_number, source))
                print(f"[缺失] {source}")
                continue

            found_count += 1
            if files_are_same(source, destination):
                existing_count += 1
                print(f"[已存在] {destination}")
                continue

            if destination.exists():
                conflicts.append((source, destination))
                print(f"[冲突-未覆盖] {destination}")
                continue

            print(f"[复制] {source}")
            print(f"    -> {destination}")
            if execute:
                destination_directory.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, destination)
                copied_count += 1

    print("\n" + "=" * 78)
    print("按乡镇核对理论文件数：")
    for town in sorted(stats):
        info = stats[town]
        line_count = info["line_count"]
        unique_count = len(info["unique_images"])
        expected = unique_count * len(VECTOR_EXTENSIONS)
        duplicate_count = line_count - unique_count
        print(
            f"  {town}（{TOWN_TO_COUNTY[town]}）："
            f"TXT有效路径 {line_count} 条，唯一影像 {unique_count} 个，"
            f"理论文件 {expected} 个"
            + (f"，重复路径 {duplicate_count} 条" if duplicate_count else "")
        )

    print("-" * 78)
    print(f"唯一影像任务：{len(unique_tasks)} 组")
    print(f"理论文件总数：{len(unique_tasks) * len(VECTOR_EXTENSIONS)} 个")
    print(f"源文件已找到：{found_count} 个")
    print(f"源文件缺失：{len(missing)} 个")
    print(f"目标已存在且大小相同：{existing_count} 个")
    print(f"目标同名冲突且未覆盖：{len(conflicts)} 个")
    if execute:
        print(f"本次成功复制：{copied_count} 个")
    else:
        print("当前是预览模式，没有复制文件；确认无误后添加 --execute。")


def main():
    parser = argparse.ArgumentParser(
        description="根据乡镇 TXT 复制对应的耕地矢量五件套"
    )
    parser.add_argument(
        "--txt-root",
        default=DEFAULT_TXT_ROOT,
        help="TXT 所在根目录，也是最终输出根目录",
    )
    parser.add_argument(
        "--vector-root",
        default=DEFAULT_VECTOR_ROOT,
        help="所有地块矢量结果所在目录",
    )
    parser.add_argument(
        "--execute",
        action="store_true",
        help="正式复制；不添加该参数时只预览",
    )
    args = parser.parse_args()
    run(args.txt_root, args.vector_root, args.execute)


if __name__ == "__main__":
    main()
