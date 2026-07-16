#!/usr/bin/env python3
"""
从已下载的 Sentinel-2 L2A zip 中抽取 10m 波段，合成四波段 GeoTIFF。

输出波段顺序：
1. B02 蓝
2. B03 绿
3. B04 红
4. B08 近红外
"""

import argparse
import shutil
import sys
import tempfile
import warnings
import zipfile
from pathlib import Path

from rich.console import Console
from rich.progress import BarColumn, Progress, TaskProgressColumn, TextColumn, TimeRemainingColumn


console = Console()
TEN_M_BANDS = ("B02", "B03", "B04", "B08")


def require_rasterio():
    try:
        import rasterio
    except ImportError:
        console.print("[bold red]缺少依赖 rasterio，无法写带坐标的 GeoTIFF。[/bold red]")
        console.print("请先运行：uv add rasterio")
        sys.exit(1)
    warnings.filterwarnings(
        "ignore",
        message="Setting the shape on a NumPy array has been deprecated.*",
        category=DeprecationWarning,
    )
    return rasterio


def find_band_members(zip_path):
    """在 zip 里查找 R10m 的 B02/B03/B04/B08 jp2。"""
    band_members = {}
    with zipfile.ZipFile(zip_path) as zf:
        for member in zf.namelist():
            normalized = member.replace("\\", "/")
            if "/IMG_DATA/R10m/" not in normalized or not normalized.lower().endswith(".jp2"):
                continue

            for band in TEN_M_BANDS:
                if f"_{band}_10m.jp2" in normalized:
                    band_members[band] = member

    missing = [band for band in TEN_M_BANDS if band not in band_members]
    if missing:
        raise FileNotFoundError(f"缺少 10m 波段：{', '.join(missing)}")

    return band_members


def extract_members(zip_path, band_members, temp_dir):
    """只解压需要的四个 jp2 到临时目录。"""
    extracted = {}
    with zipfile.ZipFile(zip_path) as zf:
        for band, member in band_members.items():
            output_path = Path(temp_dir) / Path(member).name
            with zf.open(member) as src, open(output_path, "wb") as dst:
                shutil.copyfileobj(src, dst, length=1024 * 1024)
            extracted[band] = output_path
    return extracted


def make_output_path(zip_path, output_root=None):
    city_dir = zip_path.parent
    out_dir = Path(output_root) / city_dir.name if output_root else city_dir
    out_dir.mkdir(parents=True, exist_ok=True)

    name = zip_path.name
    if name.endswith(".SAFE.zip"):
        tif_name = name[: -len(".SAFE.zip")] + ".tif"
    else:
        tif_name = zip_path.stem + ".tif"

    return out_dir / tif_name


def convert_zip_to_tif(zip_path, output_path, overwrite=False):
    rasterio = require_rasterio()

    if output_path.exists() and not overwrite:
        return "skipped", output_path

    with tempfile.TemporaryDirectory(prefix="s2_10m_") as temp_dir:
        band_members = find_band_members(zip_path)
        extracted = extract_members(zip_path, band_members, temp_dir)

        with rasterio.Env(GDAL_PAM_ENABLED="NO"):
            with rasterio.open(extracted["B02"]) as first:
                profile = first.profile.copy()
                profile.update(
                    driver="GTiff",
                    count=4,
                    compress="deflate",
                    predictor=2,
                    tiled=True,
                    BIGTIFF="IF_SAFER",
                )

                output_path.parent.mkdir(parents=True, exist_ok=True)
                with rasterio.open(output_path, "w", **profile) as dst:
                    for band_index, band in enumerate(TEN_M_BANDS, 1):
                        with rasterio.open(extracted[band]) as src:
                            if src.width != first.width or src.height != first.height:
                                raise ValueError(f"{band} 尺寸与 B02 不一致")
                            dst.write(src.read(1), band_index)
                            dst.set_band_description(band_index, band)

    return "created", output_path


def iter_zip_files(input_dir, city=None):
    root = Path(input_dir)
    if city:
        root = root / city

    return sorted(path for path in root.rglob("*.zip") if path.is_file())


def parse_args():
    parser = argparse.ArgumentParser(description="抽取 Sentinel-2 10m 蓝绿红近红外四波段并合成 tif")
    parser.add_argument("--input-dir", default="./sentinel_data", help="下载数据根目录")
    parser.add_argument("--city", help="只处理某个城市目录，例如 赤峰市")
    parser.add_argument("--output-root", help="统一 tif 输出根目录；默认直接放到每个城市目录下")
    parser.add_argument("--overwrite", action="store_true", help="覆盖已存在 tif")
    parser.add_argument("--delete-zip", action="store_true", help="抽取成功后删除对应 zip")
    parser.add_argument("--limit", type=int, default=0, help="最多处理多少个 zip；0 表示全部")
    return parser.parse_args()


def main():
    args = parse_args()
    zip_files = iter_zip_files(args.input_dir, args.city)
    if args.limit > 0:
        zip_files = zip_files[: args.limit]

    if not zip_files:
        console.print("[yellow]没有找到 zip 文件。[/yellow]")
        return

    stats = {"created": 0, "skipped": 0, "failed": 0}
    console.print(f"[bold cyan]找到 {len(zip_files)} 个 zip，开始抽取 10m 四波段...[/bold cyan]")

    with Progress(
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        TaskProgressColumn(),
        TimeRemainingColumn(),
        console=console,
    ) as progress:
        task = progress.add_task("处理进度", total=len(zip_files))
        for zip_path in zip_files:
            output_path = make_output_path(zip_path, args.output_root)
            try:
                progress.update(task, description=f"处理 {zip_path.name[:48]}")
                status, path = convert_zip_to_tif(zip_path, output_path, overwrite=args.overwrite)
                stats[status] += 1
                if status == "created":
                    console.print(f"[green]已生成：{path}[/green]")
                    if args.delete_zip:
                        zip_path.unlink()
                        marker = zip_path.with_suffix(".txt")
                        if marker.exists():
                            marker.unlink()
                else:
                    console.print(f"[yellow]已存在，跳过：{path}[/yellow]")
            except Exception as e:
                stats["failed"] += 1
                console.print(f"[red]处理失败：{zip_path} - {e}[/red]")
            finally:
                progress.update(task, advance=1)

    console.print()
    console.print("[bold]完成统计[/bold]")
    console.print(f"  新生成：{stats['created']}")
    console.print(f"  跳过：{stats['skipped']}")
    console.print(f"  失败：{stats['failed']}")


if __name__ == "__main__":
    main()
