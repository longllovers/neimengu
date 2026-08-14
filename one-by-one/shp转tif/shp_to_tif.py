#!/usr/bin/env python3
"""处理中稻地块掩膜并生成矢量、栅格，在控制台或网页输出面积对比。

需求：
1. 递归读取“地块掩膜”中的全部 Shapefile，将所有要素的 class 设为指定值，
   按原相对目录和文件名写入“0809地块掩膜”，不修改源文件。
2. 将新 Shapefile 合并为 EPSG:4326 的 rice_10m_result_0809.shp。
3. 在 Albers 等面积投影中按全部图斑范围建立严格 10 m × 10 m 网格，通过
   8×8 子像元超采样估算覆盖率，按覆盖率由高到低选择与矢量净面积相匹配的
   像元并写为指定 class、其他写为0；采用最近邻金字塔。
4. 分块融合矢量重叠区域，并仅在内存中转换到 Albers 等面积投影，计算矢量
   净覆盖面积、TIFF 中 class 像元面积及差异；不生成投影后的中间文件。

输出 TIFF 的坐标系、分辨率、数据类型、NoData、压缩和金字塔规则均由脚本配置。
"""

from __future__ import annotations

import math
import re
import time
import uuid
from concurrent.futures import (
    FIRST_COMPLETED,
    ProcessPoolExecutor,
    ThreadPoolExecutor,
    as_completed,
    wait,
)
from dataclasses import dataclass
from multiprocessing import get_context
from pathlib import Path
from typing import Callable, Iterable

import fiona
import numpy as np
import rasterio
from affine import Affine
from pyproj import CRS, Transformer
from rasterio.enums import Resampling
from rasterio.features import rasterize
from rasterio.transform import from_origin
from rasterio.windows import bounds as window_bounds
from rasterio.windows import transform as window_transform
from shapely import make_valid, transform as vectorized_transform, union_all
from shapely.errors import GEOSException
from shapely.geometry import GeometryCollection, MultiPolygon, Polygon, box, mapping, shape
from shapely.strtree import STRtree


WGS84 = CRS.from_epsg(4326)
# 覆盖中国及内蒙古的 Albers 等面积投影，只在内存中用于面积计算。
AREA_CRS = CRS.from_proj4(
    "+proj=aea +lat_1=25 +lat_2=47 +lat_0=0 +lon_0=105 "
    "+datum=WGS84 +units=m +no_defs"
)
ALBERS_PIXEL_SIZE = 10.0
RASTER_DRIVER = "GTiff"
RASTER_DTYPE = "uint8"
RASTER_NODATA = 0
RASTER_FOREGROUND_VALUE = 255
RASTER_BAND_COUNT = 1
RASTER_COMPRESSION = "LZW"
RASTER_BLOCK_SIZE = 512
RASTER_BIGTIFF = "IF_SAFER"
RASTER_INTERLEAVE = "band"
RASTER_TILED = True
RASTER_OVERVIEW_RESAMPLING = Resampling.nearest
DEFAULT_SHP_THREADS = 10
TIF_THREADS = 8


def convert_network_path(path):
    """把指定网段的 Windows 共享路径转换为服务器上的 Linux 挂载路径。"""
    if path is None:
        return path
    path = str(path).strip()
    if not path:
        return path
    path = path.replace("\\", "/")

    share_mapping = (
        ("data", "/media/cangling/nas_folder"),
        ("新建卷", "/media/cangling/xinjianjuan"),
        ("datadisk2", "/media/cangling/EAGET"),
        ("新加卷", "/media/cangling/xinjiajuan"),
    )
    for i in range(1, 256):
        for share_name, linux_prefix in share_mapping:
            for windows_prefix in (
                f"//10.10.10.{i}/{share_name}",
                f"/10.10.10.{i}/{share_name}",
                f"10.10.10.{i}/{share_name}",
            ):
                if path == windows_prefix:
                    return linux_prefix
                if path.startswith(windows_prefix + "/"):
                    return linux_prefix + path[len(windows_prefix):]
    return path


def get_ip_from_source_root(source_root):
    if source_root is None:
        return ""
    match = re.search(r"10\.10\.10\.\d+", str(source_root).strip())
    return match.group(0) if match else ""


def convert_linux_path_to_network_path(path, source_root=""):
    """把结果路径转换回输入路径所用 IP 对应的 Windows 网络路径。"""
    if path is None:
        return path
    path = str(path).strip()
    if not path:
        return path
    ip = get_ip_from_source_root(source_root)
    if not ip:
        return path
    path = path.replace("\\", "/")
    prefix_mapping = (
        ("/media/cangling/nas_folder", f"//{ip}/data"),
        ("/media/cangling/xinjianjuan", f"//{ip}/新建卷"),
        ("/media/cangling/EAGET", f"//{ip}/datadisk2"),
        ("/media/cangling/xinjiajuan", f"//{ip}/新加卷"),
    )
    for linux_prefix, windows_prefix in prefix_mapping:
        if path == linux_prefix:
            return windows_prefix.replace("/", "\\")
        if path.startswith(linux_prefix + "/"):
            return (windows_prefix + path[len(linux_prefix):]).replace("/", "\\")
    return path.replace("/", "\\")


def find_shapefiles(root: Path) -> list[Path]:
    return sorted(
        (p for p in root.rglob("*") if p.is_file() and p.suffix.lower() == ".shp"),
        key=lambda p: str(p).lower(),
    )


def collection_crs(src: fiona.Collection) -> CRS:
    value = src.crs_wkt or src.crs
    if not value:
        raise ValueError(f"{src.path} 缺少坐标系信息（.prj）")
    return CRS.from_user_input(value)


def delete_shapefile_family(path: Path) -> None:
    """只删除指定输出 Shapefile 的同名组成文件。"""
    candidates = retry_nas_io(
        lambda: list(path.parent.glob(f"{path.stem}.*")),
        f"列出旧 Shapefile 组件：{path}",
    )
    for candidate in candidates:
        try:
            retry_nas_io(
                candidate.unlink,
                f"删除旧 Shapefile 组件：{candidate}",
            )
        except FileNotFoundError:
            pass


def retry_nas_io(operation, description: str, attempts: int = 5):
    """对 NAS/CIFS 偶发的 Errno 5 使用短暂退避重试。"""
    for attempt in range(1, attempts + 1):
        try:
            return operation()
        except OSError as exc:
            if exc.errno != 5 or attempt == attempts:
                raise
            time.sleep(min(2 ** (attempt - 1), 8))


def ensure_output_available(path: Path, overwrite: bool, shapefile: bool = False) -> None:
    exists = retry_nas_io(
        lambda: (
            any(path.parent.glob(f"{path.stem}.*"))
            if shapefile
            else path.exists()
        ),
        f"检查输出文件：{path}",
    )
    if not exists:
        return
    if not overwrite:
        raise FileExistsError(f"输出已存在：{path}；如需覆盖请勾选“覆盖已有结果”")
    if shapefile:
        delete_shapefile_family(path)
    else:
        path.unlink()


def class_field_name(properties: dict[str, str]) -> str:
    for name in properties:
        if name.lower() == "class":
            return name
    return "class"


def rewrite_class(
    source: Path,
    target: Path,
    overwrite: bool,
    class_value: int,
    output_prepared: bool = False,
) -> int:
    """复制一个 Shapefile，并把 class 字段统一写成指定整数。"""
    if not output_prepared:
        retry_nas_io(
            lambda: target.parent.mkdir(parents=True, exist_ok=True),
            f"创建 SHP 输出目录：{target.parent}",
        )
        ensure_output_available(target, overwrite, shapefile=True)

    with fiona.open(source) as src:
        schema = src.schema.copy()
        properties = dict(schema["properties"])
        field_name = class_field_name(properties)
        properties[field_name] = "int:18"
        schema["properties"] = properties
        crs_wkt = collection_crs(src).to_wkt()

        count = 0
        with fiona.open(
            target,
            "w",
            driver="ESRI Shapefile",
            schema=schema,
            crs_wkt=crs_wkt,
            encoding="UTF-8",
        ) as dst:
            def records():
                nonlocal count
                for feature in src:
                    attrs = dict(feature["properties"])
                    attrs[field_name] = class_value
                    count += 1
                    yield {
                        "geometry": feature["geometry"],
                        "properties": attrs,
                    }

            dst.writerecords(records())
    return count


def rewrite_class_in_place(source: Path, class_value: int) -> int:
    """先生成并验证临时 SHP，再用可回滚交换安全覆盖原始输入。"""
    token = uuid.uuid4().hex
    temporary = source.with_name(f".{source.stem}.{token}.building.shp")
    backup_stem = f".{source.stem}.{token}.backup"
    replace_extensions = {".shp", ".shx", ".dbf", ".prj", ".cpg"}
    stale_index_extensions = {".qix", ".sbn", ".sbx"}
    moved_originals: list[tuple[Path, Path]] = []
    installed_files: list[Path] = []

    try:
        count = rewrite_class(
            source,
            temporary,
            overwrite=True,
            class_value=class_value,
        )
        with fiona.open(temporary) as check:
            if len(check) != count:
                raise RuntimeError(f"临时 SHP 要素数校验失败：{source}")

        original_family = retry_nas_io(
            lambda: list(source.parent.glob(f"{source.stem}.*")),
            f"列出原始 Shapefile 组件：{source}",
        )
        original_family = [
            path
            for path in original_family
            if path.suffix.lower() in replace_extensions | stale_index_extensions
        ]
        temporary_family = retry_nas_io(
            lambda: list(temporary.parent.glob(f"{temporary.stem}.*")),
            f"列出临时 Shapefile 组件：{temporary}",
        )

        try:
            for original in original_family:
                suffix = original.name[len(source.stem):]
                backup = source.parent / f"{backup_stem}{suffix}"
                retry_nas_io(
                    lambda original=original, backup=backup: original.replace(backup),
                    f"备份原始 Shapefile 组件：{original}",
                )
                moved_originals.append((original, backup))

            for generated in temporary_family:
                suffix = generated.name[len(temporary.stem):]
                destination = source.parent / f"{source.stem}{suffix}"
                retry_nas_io(
                    lambda generated=generated, destination=destination: generated.replace(
                        destination
                    ),
                    f"安装新 Shapefile 组件：{destination}",
                )
                installed_files.append(destination)

            with fiona.open(source) as check:
                if len(check) != count:
                    raise RuntimeError(f"覆盖后的 SHP 要素数校验失败：{source}")
        except Exception:
            for installed in installed_files:
                try:
                    installed.unlink(missing_ok=True)
                except OSError:
                    pass
            for original, backup in reversed(moved_originals):
                try:
                    retry_nas_io(
                        lambda original=original, backup=backup: backup.replace(
                            original
                        ),
                        f"恢复原始 Shapefile 组件：{original}",
                    )
                except FileNotFoundError:
                    pass
            raise

        # 新文件验证通过后才删除备份；索引文件不保留，避免使用旧空间索引。
        for _, backup in moved_originals:
            try:
                retry_nas_io(backup.unlink, f"清理原始 SHP 临时备份：{backup}")
            except (FileNotFoundError, OSError):
                pass
        return count
    finally:
        try:
            delete_shapefile_family(temporary)
        except OSError:
            pass


def rewrite_class_with_retry(
    source: Path,
    target: Path,
    class_value: int,
    attempts: int = 4,
) -> tuple[int, int]:
    """处理单个 SHP；NAS 瞬时 I/O 失败时清理半成品并重试。"""
    for attempt in range(1, attempts + 1):
        try:
            if source == target:
                count = rewrite_class_in_place(source, class_value)
            else:
                count = rewrite_class(
                    source,
                    target,
                    overwrite=attempt > 1,
                    class_value=class_value,
                    output_prepared=attempt == 1,
                )
            return count, attempt - 1
        except OSError as exc:
            if exc.errno != 5 or attempt == attempts:
                raise
            time.sleep(min(2 ** (attempt - 1), 8))
    raise RuntimeError(f"SHP 重试次数耗尽：{source}")


def polygonal_part(geometry):
    """提取面类型；GeometryCollection 中的面合成 MultiPolygon。"""
    if geometry is None or geometry.is_empty:
        return None
    if isinstance(geometry, (Polygon, MultiPolygon)):
        return geometry
    if isinstance(geometry, GeometryCollection):
        polygons: list[Polygon] = []
        for part in geometry.geoms:
            selected = polygonal_part(part)
            if isinstance(selected, Polygon):
                polygons.append(selected)
            elif isinstance(selected, MultiPolygon):
                polygons.extend(selected.geoms)
        return MultiPolygon(polygons) if polygons else None
    return None


def transformed(geometry, transformer: Transformer):
    """使用 Shapely 2 的数组式坐标转换，减少逐坐标 Python 调用。"""
    return vectorized_transform(
        geometry, transformer.transform, interleaved=False
    )


def prepare_shapefile_for_merge(source: Path) -> tuple[list, int]:
    """读取一个 SHP 并将有效面转换为 WGS84，供多线程合并准备使用。"""
    prepared: list = []
    skipped = 0
    with fiona.open(source) as src:
        src_crs = collection_crs(src)
        to_wgs84 = None
        if not src_crs.equals(WGS84):
            to_wgs84 = Transformer.from_crs(src_crs, WGS84, always_xy=True)
        for feature in src:
            if feature["geometry"] is None:
                skipped += 1
                continue
            geometry = polygonal_part(shape(feature["geometry"]))
            if geometry is None:
                skipped += 1
                continue
            if to_wgs84 is not None:
                geometry = transformed(geometry, to_wgs84)
            if geometry.is_empty:
                skipped += 1
                continue
            prepared.append(geometry)
    return prepared, skipped


def merge_shapefiles(
    sources: Iterable[Path],
    target: Path,
    overwrite: bool,
    class_value: int,
    threads: int,
    log: Callable[[str], None] = print,
) -> tuple[list, float, int, int]:
    """并行准备各源文件，再批量合并为 WGS84 Shapefile。"""
    target.parent.mkdir(parents=True, exist_ok=True)
    ensure_output_available(target, overwrite, shapefile=True)
    schema = {"geometry": "Polygon", "properties": {"class": "int:18"}}
    geometries: list = []
    written = 0
    skipped = 0
    source_list = list(sources)

    worker_count = min(max(1, threads), len(source_list))
    prepared_sources: dict[int, tuple[list, int]] = {}
    log(
        f"合并准备：使用 {worker_count} 个独立进程并行读取和转换 "
        f"{len(source_list)} 个 SHP……"
    )
    with ProcessPoolExecutor(
        max_workers=worker_count,
        mp_context=get_context("spawn"),
    ) as executor:
        future_details = {
            executor.submit(prepare_shapefile_for_merge, source): (number, source)
            for number, source in enumerate(source_list, 1)
        }
        completed = 0
        for future in as_completed(future_details):
            number, source = future_details[future]
            prepared, source_skipped = future.result()
            prepared_sources[number] = (prepared, source_skipped)
            completed += 1
            log(
                f"[合并准备 {completed}/{len(source_list)}] {source.name}："
                f"有效 {len(prepared):,}，跳过 {source_skipped:,}"
            )

    with fiona.open(
        target,
        "w",
        driver="ESRI Shapefile",
        schema=schema,
        crs_wkt=WGS84.to_wkt(),
        encoding="UTF-8",
    ) as dst:
        for source_number, source in enumerate(source_list, 1):
            source_geometries, source_skipped = prepared_sources.pop(source_number)
            dst.writerecords(
                {
                    "geometry": mapping(geometry),
                    "properties": {"class": class_value},
                }
                for geometry in source_geometries
            )
            geometries.extend(source_geometries)
            source_written = len(source_geometries)
            written += source_written
            skipped += source_skipped
            log(
                f"[合并 {source_number}/{len(source_list)}] {source.name}："
                f"批量写入 {source_written:,}，"
                f"跳过 {source_skipped:,}，累计 {written:,} 个要素"
            )

    return geometries, 0.0, written, skipped


def load_merged_shapefile(path: Path) -> tuple[list, float, int, int]:
    """读取现有合并 Shapefile，供 --tif-only 使用，不重写矢量文件。"""
    geometries: list = []
    raw_area_m2 = 0.0
    written = 0
    skipped = 0
    area_transformer = Transformer.from_crs(WGS84, AREA_CRS, always_xy=True)
    with fiona.open(path) as src:
        src_crs = collection_crs(src)
        to_wgs84 = None
        if not src_crs.equals(WGS84):
            to_wgs84 = Transformer.from_crs(src_crs, WGS84, always_xy=True)
        for feature in src:
            if feature["geometry"] is None:
                skipped += 1
                continue
            geometry = polygonal_part(shape(feature["geometry"]))
            if geometry is None:
                skipped += 1
                continue
            if to_wgs84 is not None:
                geometry = transformed(geometry, to_wgs84)
            if geometry.is_empty:
                skipped += 1
                continue
            geometries.append(geometry)
            raw_area_m2 += transformed(geometry, area_transformer).area
            written += 1
    return geometries, raw_area_m2, written, skipped


def unique_vector_area_m2(
    geometries: list,
    tile_size_degrees: float = 0.25,
    log: Callable[[str], None] = print,
) -> float:
    """分块融合图斑后计算净覆盖面积，任何重叠部分均只计算一次。

    直接对几十万个图斑做全局 union 容易占用大量内存。这里以经纬度网格切块，
    每块内先裁切、融合，再转到等面积投影求面积。网格块互不重叠，因此各块
    面积可以安全求和；块边界只会产生零面积的公共边，不会重复计入。
    """
    if not geometries:
        return 0.0
    if tile_size_degrees <= 0:
        raise ValueError("面积去重网格大小必须大于 0")

    spatial_index = STRtree(geometries)
    minx = min(g.bounds[0] for g in geometries)
    miny = min(g.bounds[1] for g in geometries)
    maxx = max(g.bounds[2] for g in geometries)
    maxy = max(g.bounds[3] for g in geometries)
    first_col = math.floor(minx / tile_size_degrees)
    last_col = math.ceil(maxx / tile_size_degrees)
    first_row = math.floor(miny / tile_size_degrees)
    last_row = math.ceil(maxy / tile_size_degrees)
    area_transformer = Transformer.from_crs(WGS84, AREA_CRS, always_xy=True)
    total_area_m2 = 0.0
    total_tiles = (last_row - first_row) * (last_col - first_col)
    progress_interval = max(1, math.ceil(total_tiles / 20))
    processed_tiles = 0
    occupied_tiles = 0
    log(
        f"面积融合范围共 {total_tiles:,} 个网格，"
        f"待处理 {len(geometries):,} 个图斑。"
    )

    for row in range(first_row, last_row):
        bottom = row * tile_size_degrees
        top = (row + 1) * tile_size_degrees
        for col in range(first_col, last_col):
            processed_tiles += 1
            if (
                processed_tiles == 1
                or processed_tiles % progress_interval == 0
                or processed_tiles == total_tiles
            ):
                log(
                    f"[面积融合 {processed_tiles:,}/{total_tiles:,}] "
                    f"{processed_tiles / total_tiles * 100:.1f}%"
                )
            left = col * tile_size_degrees
            right = (col + 1) * tile_size_degrees
            tile = box(left, bottom, right, top)
            candidate_indices = spatial_index.query(tile)
            if len(candidate_indices) == 0:
                continue
            occupied_tiles += 1

            clipped = []
            for index in candidate_indices:
                geometry = geometries[int(index)]
                try:
                    part = geometry.intersection(tile)
                except GEOSException:
                    part = make_valid(geometry).intersection(tile)
                part = polygonal_part(part)
                if part is not None and not part.is_empty:
                    clipped.append(part)
            if not clipped:
                continue

            try:
                dissolved = union_all(clipped)
            except GEOSException:
                dissolved = union_all([make_valid(g) for g in clipped])
            dissolved = polygonal_part(dissolved)
            if dissolved is not None and not dissolved.is_empty:
                total_area_m2 += transformed(dissolved, area_transformer).area

    log(
        f"面积融合完成：有效网格 {occupied_tiles:,}/{total_tiles:,}，"
        f"净覆盖面积 {total_area_m2:,.3f} m²。"
    )
    return total_area_m2


def rasterize_to_tif(
    geometries: list,
    output_path: Path,
    overwrite: bool,
    threads: int,
    supersample: int,
    target_vector_area_m2: float,
    log: Callable[[str], None] = print,
) -> tuple[float, int, tuple[float, float]]:
    """按覆盖率排序选择像元，使二值 TIFF 面积尽量匹配矢量净面积。"""
    if output_path.exists() and not overwrite:
        raise FileExistsError(
            f"输出已存在：{output_path}；如需覆盖请勾选“覆盖已有结果”"
        )
    working_path = output_path.with_name(f"{output_path.stem}.building.tif")
    coverage_path = output_path.with_name(f"{output_path.stem}.coverage.building.tif")
    if working_path.exists():
        working_path.unlink()
    if coverage_path.exists():
        coverage_path.unlink()
    if not geometries:
        raise ValueError("合并结果中没有可栅格化的面要素")

    to_albers = Transformer.from_crs(WGS84, AREA_CRS, always_xy=True)
    projected_geometries = [transformed(g, to_albers) for g in geometries]
    spatial_index = STRtree(projected_geometries)

    minx = min(g.bounds[0] for g in projected_geometries)
    miny = min(g.bounds[1] for g in projected_geometries)
    maxx = max(g.bounds[2] for g in projected_geometries)
    maxy = max(g.bounds[3] for g in projected_geometries)
    left = math.floor(minx / ALBERS_PIXEL_SIZE) * ALBERS_PIXEL_SIZE
    bottom = math.floor(miny / ALBERS_PIXEL_SIZE) * ALBERS_PIXEL_SIZE
    right = math.ceil(maxx / ALBERS_PIXEL_SIZE) * ALBERS_PIXEL_SIZE
    top = math.ceil(maxy / ALBERS_PIXEL_SIZE) * ALBERS_PIXEL_SIZE
    width = int(round((right - left) / ALBERS_PIXEL_SIZE))
    height = int(round((top - bottom) / ALBERS_PIXEL_SIZE))
    output_transform = from_origin(
        left, top, ALBERS_PIXEL_SIZE, ALBERS_PIXEL_SIZE
    )

    # 每个图斑保留一个内部代表像元。若多个图斑落在同一像元，只保留一次。
    reserved_cells: set[int] = set()
    for geometry in projected_geometries:
        point = geometry.representative_point()
        col = int(math.floor((point.x - left) / ALBERS_PIXEL_SIZE))
        row = int(math.floor((top - point.y) / ALBERS_PIXEL_SIZE))
        col = min(max(col, 0), width - 1)
        row = min(max(row, 0), height - 1)
        reserved_cells.add(row * width + col)

    block_size = RASTER_BLOCK_SIZE
    reserved_by_block: dict[tuple[int, int], list[tuple[int, int]]] = {}
    for linear_index in reserved_cells:
        row, col = divmod(linear_index, width)
        key = (row // block_size, col // block_size)
        reserved_by_block.setdefault(key, []).append(
            (row % block_size, col % block_size)
        )

    maximum_coverage = supersample * supersample
    coverage_histogram = np.zeros(maximum_coverage + 1, dtype=np.int64)

    # 输出信息全部由脚本顶部的栅格配置变量确定。
    profile = {
        "driver": RASTER_DRIVER,
        "count": RASTER_BAND_COUNT,
        "dtype": RASTER_DTYPE,
        "nodata": RASTER_NODATA,
        "crs": AREA_CRS,
        "transform": output_transform,
        "width": width,
        "height": height,
        "tiled": RASTER_TILED,
        "blockxsize": RASTER_BLOCK_SIZE,
        "blockysize": RASTER_BLOCK_SIZE,
        "compress": RASTER_COMPRESSION,
        "interleave": RASTER_INTERLEAVE,
        "num_threads": threads,
        "BIGTIFF": RASTER_BIGTIFF,
    }
    total_blocks = math.ceil(height / block_size) * math.ceil(width / block_size)
    block_progress_interval = max(1, math.ceil(total_blocks / 20))
    log(
        f"输出栅格：{width:,} 列 × {height:,} 行，共 {total_blocks:,} 个数据块；"
        f"分辨率 {ALBERS_PIXEL_SIZE:g} m × {ALBERS_PIXEL_SIZE:g} m。"
    )

    def calculate_coverage(window):
        """计算单个块的超采样覆盖率；由有界线程池并行调用。"""
        block_left, block_bottom, block_right, block_top = window_bounds(
            window, output_transform
        )
        candidate_indices = spatial_index.query(
            box(block_left, block_bottom, block_right, block_top)
        )
        candidates = [projected_geometries[int(i)] for i in candidate_indices]
        block_shape = (int(window.height), int(window.width))
        if not candidates:
            return window, np.zeros(block_shape, dtype=np.uint16)

        fine_shape = (
            block_shape[0] * supersample,
            block_shape[1] * supersample,
        )
        fine_transform = window_transform(
            window, output_transform
        ) * Affine.scale(1.0 / supersample, 1.0 / supersample)
        fine_data = rasterize(
            ((mapping(g), 1) for g in candidates),
            out_shape=fine_shape,
            transform=fine_transform,
            fill=0,
            dtype="uint8",
            all_touched=False,
        )
        coverage = fine_data.reshape(
            block_shape[0],
            supersample,
            block_shape[1],
            supersample,
        ).sum(axis=(1, 3), dtype=np.uint16)
        return window, coverage

    with rasterio.Env():
        output_path.parent.mkdir(parents=True, exist_ok=True)
        gdal_threads = str(max(1, threads))
        with rasterio.Env(GDAL_NUM_THREADS=gdal_threads):
            coverage_profile = profile.copy()
            coverage_profile.update(
                dtype="uint16",
                nodata=0,
                compress="LZW",
                sparse_ok=True,
            )

            log(
                f"使用 {threads} 个线程和 {supersample}×{supersample} "
                "超采样计算像元覆盖率……"
            )
            with rasterio.open(coverage_path, "w", **coverage_profile) as coverage_dst:
                windows = (window for _, window in coverage_dst.block_windows(1))
                pending = set()
                with ThreadPoolExecutor(
                    max_workers=threads, thread_name_prefix="tif-coverage"
                ) as executor:
                    # 仅保留少量待处理块，避免大型细分数组占满内存。
                    for _ in range(threads * 2):
                        try:
                            pending.add(
                                executor.submit(calculate_coverage, next(windows))
                            )
                        except StopIteration:
                            break

                    block_number = 0
                    while pending:
                        completed_futures, pending = wait(
                            pending, return_when=FIRST_COMPLETED
                        )
                        for future in completed_futures:
                            window, coverage = future.result()
                            block_number += 1
                            try:
                                pending.add(
                                    executor.submit(calculate_coverage, next(windows))
                                )
                            except StopIteration:
                                pass

                            coverage_dst.write(coverage, 1, window=window)
                            block_shape = (
                                int(window.height),
                                int(window.width),
                            )
                            block_key = (
                                int(window.row_off) // block_size,
                                int(window.col_off) // block_size,
                            )
                            reserved_mask = np.zeros(block_shape, dtype=bool)
                            for local_row, local_col in reserved_by_block.get(
                                block_key, []
                            ):
                                if (
                                    local_row < block_shape[0]
                                    and local_col < block_shape[1]
                                ):
                                    reserved_mask[local_row, local_col] = True
                            non_reserved_coverage = coverage[~reserved_mask]
                            coverage_histogram += np.bincount(
                                non_reserved_coverage,
                                minlength=maximum_coverage + 1,
                            )[: maximum_coverage + 1]
                            if (
                                block_number == 1
                                or block_number % block_progress_interval == 0
                                or block_number == total_blocks
                            ):
                                log(
                                    f"[TIF  {block_number:,}/"
                                    f"{total_blocks:,}] 覆盖率计算 "
                                    f"{block_number / total_blocks * 100:.1f}%"
                                )

            target_pixel_count = int(
                round(target_vector_area_m2 / (ALBERS_PIXEL_SIZE * ALBERS_PIXEL_SIZE))
            )
            reserved_count = len(reserved_cells)
            if reserved_count > target_pixel_count:
                raise ValueError(
                    "需要保留的图斑代表像元数已超过面积允许的目标像元数；"
                    "无法同时保证每个图斑可见和总面积匹配"
                )
            target_non_reserved = target_pixel_count - reserved_count
            available_non_reserved = int(coverage_histogram[1:].sum())
            if target_non_reserved > available_non_reserved:
                raise ValueError(
                    "超采样找到的相交像元不足以匹配矢量面积，请提高 --supersample"
                )

            remaining = target_non_reserved
            cutoff_coverage = maximum_coverage + 1
            cutoff_needed = 0
            cutoff_total = 0
            for coverage_level in range(maximum_coverage, 0, -1):
                level_count = int(coverage_histogram[coverage_level])
                if remaining >= level_count:
                    remaining -= level_count
                    continue
                cutoff_coverage = coverage_level
                cutoff_needed = remaining
                cutoff_total = level_count
                remaining = 0
                break
            if remaining != 0:
                raise RuntimeError("无法根据覆盖率选出目标数量的像元")
            if target_non_reserved == available_non_reserved:
                cutoff_coverage = 0

            log(
                "按覆盖率由高到低选择像元；"
                f"目标 {target_pixel_count:,} 个，其中图斑代表像元 {reserved_count:,} 个……"
            )
            pixel_count = 0
            cutoff_seen = 0
            with rasterio.open(coverage_path) as coverage_src, rasterio.open(
                working_path, "w", **profile
            ) as dst:
                # 栅格与 SHP 的 class 属性解耦：0 为背景，255 代表图斑。
                dst.write_colormap(
                    1,
                    {
                        0: (0, 0, 0, 0),
                        RASTER_FOREGROUND_VALUE: (0, 180, 0, 255),
                    },
                )
                for block_number, (_, window) in enumerate(
                    coverage_src.block_windows(1), 1
                ):
                    block_shape = (int(window.height), int(window.width))
                    coverage = coverage_src.read(1, window=window)
                    block_key = (
                        int(window.row_off) // block_size,
                        int(window.col_off) // block_size,
                    )
                    reserved_mask = np.zeros(block_shape, dtype=bool)
                    for local_row, local_col in reserved_by_block.get(block_key, []):
                        if local_row < block_shape[0] and local_col < block_shape[1]:
                            reserved_mask[local_row, local_col] = True

                    selected = reserved_mask | (coverage > cutoff_coverage)
                    if cutoff_needed and cutoff_coverage > 0:
                        candidates_flat = np.flatnonzero(
                            (coverage == cutoff_coverage) & ~reserved_mask
                        )
                        candidate_count = int(candidates_flat.size)
                        before = cutoff_seen * cutoff_needed // cutoff_total
                        after = (
                            (cutoff_seen + candidate_count)
                            * cutoff_needed
                            // cutoff_total
                        )
                        choose_count = after - before
                        if choose_count:
                            positions = np.floor(
                                (np.arange(choose_count) + 0.5)
                                * candidate_count
                                / choose_count
                            ).astype(np.int64)
                            selected.flat[candidates_flat[positions]] = True
                        cutoff_seen += candidate_count

                    data = np.where(
                        selected, RASTER_FOREGROUND_VALUE, RASTER_NODATA
                    ).astype(RASTER_DTYPE)
                    dst.write(data, 1, window=window)
                    pixel_count += int(np.count_nonzero(selected))
                    if (
                        block_number == 1
                        or block_number % block_progress_interval == 0
                        or block_number == total_blocks
                    ):
                        log(
                            f"[TIF  {block_number:,}/{total_blocks:,}] "
                            f"像元写入 {block_number / total_blocks * 100:.1f}%"
                        )

            if pixel_count != target_pixel_count:
                raise RuntimeError(
                    f"面积约束像元数校验失败：目标 {target_pixel_count:,}，"
                    f"实际 {pixel_count:,}"
                )

            # 写入结束并关闭数据集后，再以更新模式构建内部金字塔，避免不完整 TIFF。
            overview_levels: list[int] = []
            factor = 2
            while min(width, height) // factor >= 128:
                overview_levels.append(factor)
                factor *= 2
            if overview_levels:
                log(
                    f"正在使用 {gdal_threads} 个线程构建内部金字塔："
                    + ", ".join(f"1:{level}" for level in overview_levels)
                )
                with rasterio.Env(
                    GDAL_NUM_THREADS=gdal_threads,
                    COMPRESS_OVERVIEW="LZW",
                    GDAL_TIFF_OVR_BLOCKSIZE="512",
                ):
                    with rasterio.open(working_path, "r+", num_threads=threads) as dst:
                        dst.build_overviews(
                            overview_levels, RASTER_OVERVIEW_RESAMPLING
                        )
                        dst.update_tags(ns="rio_overview", resampling="nearest")

            # 完整关闭后重新打开验证；成功后才替换正式结果，避免留下打不开的半成品。
            with rasterio.open(working_path) as check:
                if check.driver != "GTiff" or check.count != 1:
                    raise RuntimeError("生成的 TIFF 驱动或波段数校验失败")
                if not CRS.from_user_input(check.crs).equals(AREA_CRS):
                    raise RuntimeError("生成的 TIFF 不是要求的 Albers 投影")
                if not (
                    math.isclose(abs(check.transform.a), ALBERS_PIXEL_SIZE)
                    and math.isclose(abs(check.transform.e), ALBERS_PIXEL_SIZE)
                ):
                    raise RuntimeError("生成的 TIFF 不是严格的 10 m × 10 m 网格")
                if check.compression is None or "lzw" not in str(check.compression).lower():
                    raise RuntimeError("生成的 TIFF 未成功使用 LZW 压缩")
                actual_overviews = check.overviews(1)
                overviews_valid = len(actual_overviews) == len(overview_levels) and all(
                    abs(actual - requested) <= max(1, requested * 0.01)
                    for actual, requested in zip(actual_overviews, overview_levels)
                )
                if not overviews_valid:
                    raise RuntimeError("生成的 TIFF 内部金字塔校验失败")
                check.read(1, window=((0, 1), (0, 1)))

        working_path.replace(output_path)
        coverage_path.unlink()
        log(f"GeoTIFF 完整性校验通过，已生成正式文件：{output_path}")

    raster_area_m2 = pixel_count * ALBERS_PIXEL_SIZE * ALBERS_PIXEL_SIZE
    return raster_area_m2, pixel_count, (ALBERS_PIXEL_SIZE, ALBERS_PIXEL_SIZE)


def build_report(
    vector_unique_area_m2: float,
    raster_area_m2: float,
    feature_count: int,
    skipped_count: int,
    supersample: int,
) -> str:
    difference_m2 = raster_area_m2 - vector_unique_area_m2
    absolute_difference_m2 = abs(difference_m2)
    difference_percent = (
        absolute_difference_m2 / vector_unique_area_m2 * 100.0
        if vector_unique_area_m2 > 0
        else math.nan
    )
    lines = [
        "中稻 0809 地块掩膜面积对比报告",
        "=" * 42,
        f"合并面要素数: {feature_count:,}",
        f"栅格化规则: {supersample}×{supersample} 超采样，"
        "按覆盖率排序并约束总面积",
        "标称分辨率: 10 m × 10 m",
        "",
        f"矢量净覆盖面积（重叠只算一次）: {vector_unique_area_m2:,.3f} m²",
        f"TIFF 值为 {RASTER_FOREGROUND_VALUE} 区域面积: {raster_area_m2:,.3f} m²",
        f"面积差（TIFF - 矢量）: {difference_m2:,.3f} m²",
        f"差异比例（相对矢量）: {difference_percent:.6f}%",
        "",
        "说明：面积均在内存中转换到 Albers 等面积投影计算；矢量重叠部分只算一次。",
    ]
    if skipped_count:
        lines.insert(3, f"跳过的空/非面要素数: {skipped_count:,}")
    return "\n".join(lines) + "\n"


@dataclass(frozen=True)
class ProcessingOptions:
    input_dir: Path
    rewritten_dir: Path
    output_dir: Path
    merged_shp_name: str
    output_tif_name: str
    class_value: int = 2
    overwrite: bool = False
    tif_only: bool = False
    supersample: int = 8
    shp_threads: int = DEFAULT_SHP_THREADS
    source_root_text: str = ""


def result_names(shp_name: str, tif_name: str) -> tuple[str, str]:
    """校验结果文件名；任填一个时根据主文件名推导另一个。"""
    shp_name = shp_name.strip()
    tif_name = tif_name.strip()
    if not shp_name and not tif_name:
        raise ValueError("合并 SHP 名称和输出 TIF 名称至少填写一个")

    def filename_only(value: str) -> str:
        return value.replace("\\", "/").rsplit("/", 1)[-1].strip()

    if shp_name:
        shp_name = filename_only(shp_name)
        shp_name = str(Path(shp_name).with_suffix(".shp"))
    if tif_name:
        tif_name = filename_only(tif_name)
        tif_name = str(Path(tif_name).with_suffix(".tif"))
    if not shp_name:
        shp_name = str(Path(tif_name).with_suffix(".shp"))
    if not tif_name:
        tif_name = str(Path(shp_name).with_suffix(".tif"))
    return shp_name, tif_name


def run_processing(options: ProcessingOptions, log: Callable[[str], None] = print) -> None:
    input_dir = options.input_dir
    output_dir = options.output_dir
    if options.shp_threads < 1:
        raise ValueError("SHP 处理线程数必须大于或等于 1")
    if not 1 <= options.supersample <= 16:
        raise ValueError("超采样倍数必须在 1 到 16 之间")

    output_dir.mkdir(parents=True, exist_ok=True)
    merged_shp = output_dir / options.merged_shp_name
    log("=" * 58)
    log(f"输入 SHP 目录：{input_dir}")
    if options.rewritten_dir == input_dir:
        log("SHP 处理方式：安全覆盖原始输入 SHP")
    else:
        log(f"SHP 处理方式：另存到 {options.rewritten_dir}")
    log(f"输出目录：{output_dir}")
    log(f"合并 SHP：{options.merged_shp_name}")
    log(f"输出 TIF：{options.output_tif_name}")
    log(f"SHP class：{options.class_value}")
    log(f"SHP 并行进程设置：{options.shp_threads}；TIF 线程固定：{TIF_THREADS}")
    log("=" * 58)
    rewritten_features: int | None = None
    if options.tif_only:
        if not merged_shp.is_file():
            raise FileNotFoundError(f"没有找到现有合并 Shapefile：{merged_shp}")
        log(f"仅生成 TIFF：读取现有合并 Shapefile：{merged_shp}")
        geometries, _, merged_count, skipped_count = load_merged_shapefile(merged_shp)
    else:
        if not input_dir.is_dir():
            raise FileNotFoundError(f"输入目录不存在或无法访问：{input_dir}")
        source_files = find_shapefiles(input_dir)
        if not source_files:
            raise FileNotFoundError(f"输入目录内没有找到 .shp：{input_dir}")
        worker_count = min(options.shp_threads, len(source_files))
        log(
            f"找到 {len(source_files)} 个 Shapefile，使用 {worker_count} 个独立进程；"
            f"每个进程处理一个 SHP，统一设置 class={options.class_value}……"
        )
        rewrite_results: dict[int, tuple[Path, int]] = {}
        rewrite_tasks = []
        log("正在串行准备各 SHP 输出，避免 NAS 并发目录操作……")
        for number, source in enumerate(source_files, 1):
            relative = source.relative_to(input_dir)
            target = options.rewritten_dir / relative
            if source != target:
                retry_nas_io(
                    lambda target=target: target.parent.mkdir(
                        parents=True, exist_ok=True
                    ),
                    f"创建 SHP 输出目录：{target.parent}",
                )
                ensure_output_available(
                    target, options.overwrite, shapefile=True
                )
            rewrite_tasks.append((number, source, relative, target))
        log(f"SHP 输出准备完成，共 {len(rewrite_tasks)} 个处理任务。")

        # Fiona/GDAL 的 Shapefile 写入在单个 Python 进程内受 GIL/驱动锁影响，
        # 使用 spawn 多进程才能真正并行占用多个 CPU 核；也避免从 HTTP 工作线程 fork。
        with ProcessPoolExecutor(
            max_workers=worker_count,
            mp_context=get_context("spawn"),
        ) as executor:
            future_details = {}
            for number, source, relative, target in rewrite_tasks:
                future = executor.submit(
                    rewrite_class_with_retry,
                    source,
                    target,
                    options.class_value,
                )
                future_details[future] = (number, relative, target)

            completed = 0
            for future in as_completed(future_details):
                number, relative, target = future_details[future]
                count, retry_count = future.result()
                rewrite_results[number] = (target, count)
                completed += 1
                retry_text = (
                    f"，NAS I/O 重试 {retry_count} 次" if retry_count else ""
                )
                log(
                    f"[{completed}/{len(source_files)}] {relative}："
                    f"{count:,} 个要素{retry_text}"
                )

        rewritten_files = [
            rewrite_results[number][0]
            for number in range(1, len(source_files) + 1)
        ]
        rewritten_features = sum(result[1] for result in rewrite_results.values())
        log(f"改写 SHP 已全部保存，后续合并使用上述保存结果。")
        log(f"正在合并为：{merged_shp}")
        geometries, _, merged_count, skipped_count = merge_shapefiles(
            rewritten_files,
            merged_shp,
            options.overwrite,
            options.class_value,
            options.shp_threads,
            log,
        )

    log("正在分块融合图斑并计算矢量净覆盖面积（重叠部分只算一次）……")
    vector_unique_area_m2 = unique_vector_area_m2(geometries, log=log)
    output_tif = output_dir / options.output_tif_name
    log(
        f"正在使用固定的 {TIF_THREADS} 个 GDAL 线程按 10 m 网格生成 TIFF："
        f"{output_tif}"
    )
    raster_area_m2, _, _ = rasterize_to_tif(
        geometries,
        output_tif,
        options.overwrite,
        TIF_THREADS,
        options.supersample,
        vector_unique_area_m2,
        log,
    )
    log("")
    log(
        build_report(
            vector_unique_area_m2,
            raster_area_m2,
            merged_count,
            skipped_count,
            options.supersample,
        ).rstrip()
    )
    if rewritten_features is None:
        log("处理完成；本次未重写或重新合并任何 Shapefile。")
    else:
        log(f"处理完成。共重写 {rewritten_features:,} 个要素。")
    log(
        "合并 Shapefile："
        + convert_linux_path_to_network_path(merged_shp, options.source_root_text)
    )
    log(
        "输出 TIFF："
        + convert_linux_path_to_network_path(output_tif, options.source_root_text)
    )
