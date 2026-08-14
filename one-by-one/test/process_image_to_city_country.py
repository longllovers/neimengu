#!/usr/bin/env python3
"""把 E:\\image 中的 Sentinel 瓦片整理为 city_data 和 country_data。

处理流程：
1. 按文件名中的成像时间分组；
2. 根据栅格范围和市界筛选瓦片，合并后按市界裁剪；
3. 从市级结果按县界裁剪县级影像；
4. TIFF 的 NoData 统一为 0；
5. 为市、县 TIFF 生成波段 3/2/1、最小值到最大值拉伸的 JPEG。

输入文件名示例：T49SCD_20260621T032509.tif
输出目录示例：
    E:\\city_data\\包头市\\1502_20260621T032509.tif
    E:\\country_data\\包头市\\青山区\\150204_20260621T032509.tif
"""

from __future__ import annotations

import argparse
import re
import sys
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path


BASE_DIR = Path(__file__).resolve().parent
DEFAULT_INPUT_ROOT = Path(r"E:\image")
DEFAULT_CITY_OUTPUT_ROOT = Path(r"E:\city_data")
DEFAULT_COUNTRY_OUTPUT_ROOT = Path(r"E:\country_data")
DEFAULT_CITY_LAYER = BASE_DIR / "00市边界" / "15_市边界.shp"
DEFAULT_COUNTY_LAYER = BASE_DIR / "00县边界" / "15_县边界.shp"
THUMBNAIL_MAX_SIZE = 1200
LOG_LOCK = threading.Lock()

INPUT_NAME_RE = re.compile(
    r"^(?P<tile>T\d{2}[A-Z]{3})_(?P<sensing_time>\d{8}T\d{6})\.tif$",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class RasterInfo:
    path: Path
    tile: str
    sensing_time: str
    crs: object
    bounds: tuple[float, float, float, float]
    boundary_footprint: object


@dataclass(frozen=True)
class CityTask:
    city_name: str
    city_code: str
    city_geometry: object
    sensing_time: str
    sources: tuple[RasterInfo, ...]


def log(message: str, level: str = "INFO") -> None:
    """输出带时间戳的线程安全日志，避免并发任务的文字互相穿插。"""
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with LOG_LOCK:
        print(f"[{timestamp}] [{level}] {message}", flush=True)


def elapsed_text(started_at: float) -> str:
    seconds = max(0, round(time.monotonic() - started_at))
    hours, remainder = divmod(seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    if hours:
        return f"{hours}小时{minutes}分{seconds}秒"
    if minutes:
        return f"{minutes}分{seconds}秒"
    return f"{seconds}秒"


@contextmanager
def log_heartbeat(label: str, interval_seconds: int = 30):
    """为无法报告内部进度的长任务定时输出存活和耗时信息。"""
    stopped = threading.Event()
    started_at = time.monotonic()

    def heartbeat_worker() -> None:
        while not stopped.wait(interval_seconds):
            log(f"{label}仍在运行，已耗时 {elapsed_text(started_at)}")

    thread = threading.Thread(target=heartbeat_worker, daemon=True)
    thread.start()
    try:
        yield
    finally:
        stopped.set()
        thread.join(timeout=1)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="将 E:\\image 中的瓦片合并裁剪为 city_data 和 country_data。"
    )
    parser.add_argument("--input-root", type=Path, default=DEFAULT_INPUT_ROOT)
    parser.add_argument(
        "--city-output-root", type=Path, default=DEFAULT_CITY_OUTPUT_ROOT
    )
    parser.add_argument(
        "--country-output-root", type=Path, default=DEFAULT_COUNTRY_OUTPUT_ROOT
    )
    parser.add_argument("--city-layer", type=Path, default=DEFAULT_CITY_LAYER)
    parser.add_argument("--county-layer", type=Path, default=DEFAULT_COUNTY_LAYER)
    parser.add_argument("--city-name-field", default="市名称")
    parser.add_argument("--city-code-field", default="市代码")
    parser.add_argument("--county-name-field", default="area_name")
    parser.add_argument("--county-code-field", default="area_code")
    parser.add_argument(
        "--resolution",
        type=float,
        default=10.0,
        help="输出像元大小，默认 10 米",
    )
    parser.add_argument(
        "--max-workers",
        type=int,
        default=1,
        help="并发处理的市级任务数，默认 1",
    )
    parser.add_argument("--all-touched", action="store_true")
    parser.add_argument("--overwrite", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    return parser.parse_args()


def require_dependencies():
    try:
        import geopandas as gpd
        import numpy as np
        import rasterio
        from PIL import Image
        from rasterio.enums import Resampling
        from rasterio.errors import WindowError
        from rasterio.features import geometry_mask, geometry_window
        from rasterio.merge import merge
        from rasterio.vrt import WarpedVRT
        from rasterio.warp import transform_bounds
        from rasterio.windows import Window
        from shapely import union_all
        from shapely.geometry import box, mapping
    except ImportError as exc:
        raise RuntimeError(
            "缺少影像处理依赖，请安装 geopandas、rasterio、numpy、Pillow 和 shapely"
        ) from exc

    return {
        "gpd": gpd,
        "np": np,
        "rasterio": rasterio,
        "Image": Image,
        "Resampling": Resampling,
        "WindowError": WindowError,
        "geometry_mask": geometry_mask,
        "geometry_window": geometry_window,
        "merge": merge,
        "WarpedVRT": WarpedVRT,
        "transform_bounds": transform_bounds,
        "Window": Window,
        "union_all": union_all,
        "box": box,
        "mapping": mapping,
    }


def safe_name(value: object) -> str:
    text = str(value).strip()
    return re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", text).rstrip(". ")


def numeric_code(value: object, length: int) -> str:
    text = str(value).strip()
    if re.fullmatch(r"\d+\.0+", text):
        text = text.split(".", 1)[0]
    digits = re.sub(r"\D", "", text)
    return digits[:length] if len(digits) >= length else ""


def load_boundaries(args: argparse.Namespace, d: dict):
    gpd = d["gpd"]
    cities = gpd.read_file(args.city_layer)
    counties = gpd.read_file(args.county_layer)
    for frame, fields, label in (
        (cities, (args.city_name_field, args.city_code_field), "市界"),
        (counties, (args.county_name_field, args.county_code_field), "县界"),
    ):
        missing = [field for field in fields if field not in frame.columns]
        if missing:
            raise ValueError(f"{label}文件缺少字段：{', '.join(missing)}")
        if frame.crs is None:
            raise ValueError(f"{label}文件没有坐标系")

    cities = cities[[args.city_name_field, args.city_code_field, "geometry"]].copy()
    cities["_city_name"] = cities[args.city_name_field].astype(str).str.strip()
    cities["_city_code"] = cities[args.city_code_field].map(
        lambda value: numeric_code(value, 4)
    )
    counties = counties[
        [args.county_name_field, args.county_code_field, "geometry"]
    ].copy()
    counties["_county_name"] = counties[args.county_name_field].astype(str).str.strip()
    counties["_county_code"] = counties[args.county_code_field].map(
        lambda value: numeric_code(value, 6)
    )
    counties["_city_code"] = counties["_county_code"].str[:4]

    cities = cities[
        cities["_city_code"].str.fullmatch(r"\d{4}") & ~cities.geometry.is_empty
    ].reset_index(drop=True)
    counties = counties[
        counties["_county_code"].str.fullmatch(r"\d{6}")
        & ~counties.geometry.is_empty
    ].reset_index(drop=True)
    if cities.empty or counties.empty:
        raise ValueError("行政区文件中没有可用的市县要素")
    return cities, counties


def scan_rasters(input_root: Path, boundary_crs, d: dict) -> tuple[list[RasterInfo], list[Path]]:
    rasterio = d["rasterio"]
    transform_bounds = d["transform_bounds"]
    box = d["box"]
    rasters: list[RasterInfo] = []
    invalid: list[Path] = []

    candidates = sorted(input_root.rglob("*.tif"))
    log(f"开始扫描输入 TIFF，共发现 {len(candidates)} 个候选文件")
    for index, path in enumerate(candidates, 1):
        log(f"[扫描 {index}/{len(candidates)}] 检查 {path.name}")
        if not path.is_file() or path.name.startswith("."):
            continue
        match = INPUT_NAME_RE.fullmatch(path.name)
        if match is None:
            invalid.append(path)
            continue
        with rasterio.Env(GDAL_PAM_ENABLED="NO"), rasterio.open(path) as source:
            if source.crs is None:
                raise ValueError(f"输入 TIFF 没有坐标系：{path}")
            boundary_bounds = transform_bounds(
                source.crs,
                boundary_crs,
                *source.bounds,
                densify_pts=21,
            )
            rasters.append(
                RasterInfo(
                    path=path,
                    tile=match.group("tile").upper(),
                    sensing_time=match.group("sensing_time"),
                    crs=source.crs,
                    bounds=tuple(source.bounds),
                    boundary_footprint=box(*boundary_bounds),
                )
            )
            log(
                f"[扫描 {index}/{len(candidates)}] 已识别："
                f"瓦片 {match.group('tile').upper()}，时间 {match.group('sensing_time')}"
            )
    log(f"输入扫描结束：有效 {len(rasters)}，文件名不匹配 {len(invalid)}")
    return rasters, invalid


def build_city_tasks(rasters: list[RasterInfo], cities) -> list[CityTask]:
    groups: dict[str, list[RasterInfo]] = {}
    for info in rasters:
        groups.setdefault(info.sensing_time, []).append(info)

    tasks: list[CityTask] = []
    sorted_groups = sorted(groups.items())
    log(f"开始匹配行政区：成像时间分组 {len(sorted_groups)} 个，市级要素 {len(cities)} 个")
    for group_index, (sensing_time, group) in enumerate(sorted_groups, 1):
        log(
            f"[时间分组 {group_index}/{len(sorted_groups)}] {sensing_time}，"
            f"包含瓦片 {len(group)} 个"
        )
        for _, city in cities.iterrows():
            matching = tuple(
                info
                for info in group
                if info.boundary_footprint.intersects(city.geometry)
            )
            if matching:
                tasks.append(
                    CityTask(
                        city_name=city["_city_name"],
                        city_code=city["_city_code"],
                        city_geometry=city.geometry,
                        sensing_time=sensing_time,
                        sources=matching,
                    )
                )
                log(
                    f"[时间分组 {group_index}/{len(sorted_groups)}] 匹配城市："
                    f"{city['_city_name']}，使用瓦片 {len(matching)} 个"
                )
    log(f"行政区匹配结束：生成市级任务 {len(tasks)} 个")
    return tasks


def aligned_bounds(bounds, resolution: float) -> tuple[float, float, float, float]:
    left, bottom, right, top = bounds
    return (
        int(left // resolution) * resolution,
        int(bottom // resolution) * resolution,
        int(-(-right // resolution)) * resolution,
        int(-(-top // resolution)) * resolution,
    )


def mask_outside_geometry(
    path: Path,
    geometry,
    all_touched: bool,
    d: dict,
) -> bool:
    np = d["np"]
    rasterio = d["rasterio"]
    geometry_mask = d["geometry_mask"]
    mapping = d["mapping"]
    has_valid_data = False
    with rasterio.Env(GDAL_TIFF_INTERNAL_MASK="YES", GDAL_PAM_ENABLED="NO"):
        with rasterio.open(path, "r+") as dataset:
            dataset.nodata = 0
            block_height, block_width = dataset.block_shapes[0]
            total_blocks = (
                ((dataset.height + block_height - 1) // block_height)
                * ((dataset.width + block_width - 1) // block_width)
            )
            report_every = max(1, total_blocks // 20)
            for block_number, (_, window) in enumerate(dataset.block_windows(1), 1):
                inside = geometry_mask(
                    [mapping(geometry)],
                    out_shape=(window.height, window.width),
                    transform=dataset.window_transform(window),
                    invert=True,
                    all_touched=all_touched,
                )
                data = dataset.read(window=window)
                data[:, ~inside] = 0
                dataset.write(data, window=window)
                valid = inside & np.any(data != 0, axis=0)
                if valid.any():
                    has_valid_data = True
                dataset.write_mask(valid.astype("uint8") * 255, window=window)
                if (
                    block_number == 1
                    or block_number == total_blocks
                    or block_number % report_every == 0
                ):
                    percent = block_number / total_blocks * 100
                    log(
                        f"市界掩膜进度：{block_number}/{total_blocks} 块，"
                        f"{percent:.1f}%"
                    )
    return has_valid_data


def create_minmax_jpeg(tif_path: Path, d: dict) -> Path:
    """按截图设置：红=3、绿=2、蓝=1，各波段有效最小值到最大值。"""
    np = d["np"]
    rasterio = d["rasterio"]
    Image = d["Image"]
    Resampling = d["Resampling"]
    jpeg_path = tif_path.with_suffix(".jpeg")
    temporary = jpeg_path.with_name(f".{jpeg_path.stem}.{uuid.uuid4().hex}.tmp.jpeg")
    started_at = time.monotonic()
    log(f"开始生成 JPEG：{jpeg_path}")
    try:
        with rasterio.Env(GDAL_PAM_ENABLED="NO"), rasterio.open(tif_path) as source:
            scale = min(1.0, THUMBNAIL_MAX_SIZE / max(source.width, source.height))
            width = max(1, round(source.width * scale))
            height = max(1, round(source.height * scale))
            indexes = [3, 2, 1] if source.count >= 3 else [1, 1, 1]
            data = source.read(
                indexes,
                out_shape=(3, height, width),
                resampling=Resampling.bilinear,
            ).astype("float32")
            valid = source.dataset_mask(
                out_shape=(height, width),
                resampling=Resampling.nearest,
            ) > 0
            valid &= np.any(np.isfinite(data) & (data != 0), axis=0)

        rows, columns = np.where(valid)
        if rows.size == 0:
            raise ValueError(f"TIFF 没有非零有效影像像元：{tif_path}")
        top, bottom = rows.min(), rows.max() + 1
        left, right = columns.min(), columns.max() + 1
        data = data[:, top:bottom, left:right]
        valid = valid[top:bottom, left:right]

        rgb = np.full((*valid.shape, 3), 255, dtype="uint8")
        for channel in range(3):
            band = data[channel]
            sample = band[valid & np.isfinite(band) & (band > 0)]
            if sample.size == 0:
                continue
            low = float(sample.min())
            high = float(sample.max())
            if high <= low:
                high = low + 1.0
            normalized = np.nan_to_num(
                np.clip((band - low) / (high - low), 0, 1),
                nan=0.0,
                posinf=1.0,
                neginf=0.0,
            )
            rgb[:, :, channel] = (normalized * 255).astype("uint8")
        rgb[~valid] = 255
        Image.fromarray(rgb).save(
            temporary,
            format="JPEG",
            quality=88,
            subsampling=2,
            optimize=True,
            progressive=True,
        )
        temporary.replace(jpeg_path)
        log(f"JPEG 生成完成：{jpeg_path}，耗时 {elapsed_text(started_at)}")
        return jpeg_path
    finally:
        if temporary.exists():
            temporary.unlink()


def city_output_path(task: CityTask, output_root: Path) -> Path:
    return (
        output_root
        / safe_name(task.city_name)
        / f"{task.city_code}_{task.sensing_time}.tif"
    )


def merge_city_task(
    task: CityTask,
    output_path: Path,
    boundary_crs,
    resolution: float,
    overwrite: bool,
    all_touched: bool,
    d: dict,
) -> str:
    started_at = time.monotonic()
    if output_path.exists() and not overwrite:
        log(f"市级 TIFF 已存在，检查 NoData 和 JPEG：{output_path}")
        with d["rasterio"].open(output_path, "r+") as existing:
            existing.nodata = 0
        if not output_path.with_suffix(".jpeg").exists():
            create_minmax_jpeg(output_path, d)
        return "existing"

    gpd = d["gpd"]
    rasterio = d["rasterio"]
    Resampling = d["Resampling"]
    WarpedVRT = d["WarpedVRT"]
    merge = d["merge"]
    transform_bounds = d["transform_bounds"]
    box = d["box"]
    union_all = d["union_all"]

    target_crs = task.sources[0].crs
    projected_city = gpd.GeoSeries([task.city_geometry], crs=boundary_crs).to_crs(
        target_crs
    ).iloc[0]
    source_boxes = []
    for info in task.sources:
        bounds = (
            info.bounds
            if info.crs == target_crs
            else transform_bounds(info.crs, target_crs, *info.bounds, densify_pts=21)
        )
        source_boxes.append(box(*bounds))
    processing_geometry = projected_city.intersection(union_all(source_boxes))
    if processing_geometry.is_empty:
        log(f"市级任务没有有效空间交集：{task.city_name} {task.sensing_time}", "WARN")
        return "no_coverage"

    bounds = aligned_bounds(processing_geometry.bounds, resolution)
    log(
        f"开始合并市级影像：{task.city_name} {task.sensing_time}，"
        f"来源瓦片 {len(task.sources)} 个，分辨率 {resolution:g} 米"
    )
    # 先在 city_data 根目录写临时文件；确认有有效像元后才创建市目录。
    temporary_root = output_path.parents[1]
    temporary_root.mkdir(parents=True, exist_ok=True)
    temporary = temporary_root / f".{output_path.stem}.{uuid.uuid4().hex}.tmp.tif"
    try:
        with ExitStack() as stack:
            sources = []
            descriptions = None
            first_count = None
            first_dtype = None
            for info in task.sources:
                source = stack.enter_context(rasterio.open(info.path))
                if first_count is None:
                    first_count = source.count
                    first_dtype = source.dtypes[0]
                    descriptions = source.descriptions
                elif source.count != first_count or source.dtypes[0] != first_dtype:
                    raise ValueError(f"波段数或数据类型不一致：{info.path.name}")
                sources.append(
                    stack.enter_context(
                        WarpedVRT(
                            source,
                            crs=target_crs,
                            resolution=resolution,
                            src_nodata=0,
                            nodata=0,
                            resampling=Resampling.nearest,
                        )
                    )
                )

            with log_heartbeat(
                f"合并 {task.city_name} {task.sensing_time}"
            ):
                merge(
                    sources,
                    bounds=bounds,
                    res=resolution,
                    nodata=0,
                    dtype=first_dtype,
                    method="first",
                    target_aligned_pixels=True,
                    mem_limit=256,
                    dst_path=temporary,
                    dst_kwds={
                        "driver": "GTiff",
                        "compress": "deflate",
                        "predictor": 2,
                        "tiled": True,
                        "blockxsize": 512,
                        "blockysize": 512,
                        "BIGTIFF": "YES",
                    },
                )

        log(f"市级瓦片合并完成，开始按市界掩膜：{task.city_name} {task.sensing_time}")
        if not mask_outside_geometry(temporary, projected_city, all_touched, d):
            log(f"市界内没有非零有效像元：{task.city_name} {task.sensing_time}", "WARN")
            return "no_coverage"
        with rasterio.open(temporary, "r+") as output:
            output.nodata = 0
            if descriptions:
                output.descriptions = descriptions
            output.update_tags(
                city_name=task.city_name,
                city_code=task.city_code,
                sensing_time=task.sensing_time,
                source_tiles=",".join(sorted({item.tile for item in task.sources})),
                clipped_to_city_boundary="true",
            )
        output_path.parent.mkdir(parents=True, exist_ok=True)
        temporary.replace(output_path)
        create_minmax_jpeg(output_path, d)
        log(
            f"市级任务输出完成：{output_path}，耗时 {elapsed_text(started_at)}"
        )
        return "created"
    finally:
        if temporary.exists():
            temporary.unlink()


def clip_county(
    city_tif: Path,
    output_path: Path,
    county_geometry,
    boundary_crs,
    county_name: str,
    county_code: str,
    sensing_time: str,
    overwrite: bool,
    all_touched: bool,
    d: dict,
) -> str:
    rasterio = d["rasterio"]
    if output_path.exists() and not overwrite:
        log(f"县级 TIFF 已存在，检查 NoData 和 JPEG：{output_path}")
        with rasterio.open(output_path, "r+") as existing:
            existing.nodata = 0
        if not output_path.with_suffix(".jpeg").exists():
            create_minmax_jpeg(output_path, d)
        return "existing"

    gpd = d["gpd"]
    np = d["np"]
    geometry_window = d["geometry_window"]
    geometry_mask = d["geometry_mask"]
    WindowError = d["WindowError"]
    Window = d["Window"]
    mapping = d["mapping"]

    # 先在 country_data 根目录写临时文件；确认有有效像元后才创建市/县目录。
    temporary_root = output_path.parents[2]
    temporary_root.mkdir(parents=True, exist_ok=True)
    temporary = temporary_root / f".{output_path.stem}.{uuid.uuid4().hex}.tmp.tif"
    try:
        log(f"开始按县界裁剪：{county_name}（{county_code}）")
        with rasterio.open(city_tif) as source:
            geometry = gpd.GeoSeries([county_geometry], crs=boundary_crs).to_crs(
                source.crs
            ).iloc[0]
            try:
                crop_window = geometry_window(source, [mapping(geometry)])
                crop_window = crop_window.round_offsets().round_lengths().intersection(
                    Window(0, 0, source.width, source.height)
                )
            except WindowError:
                log(f"县界与市级影像不相交：{county_name}（{county_code}）", "WARN")
                return "no_coverage"

            profile = source.profile.copy()
            profile.update(
                driver="GTiff",
                width=int(crop_window.width),
                height=int(crop_window.height),
                transform=source.window_transform(crop_window),
                nodata=0,
                compress="deflate",
                predictor=2,
                tiled=True,
                blockxsize=512,
                blockysize=512,
                BIGTIFF="IF_SAFER",
            )
            has_valid_data = False
            with rasterio.Env(GDAL_TIFF_INTERNAL_MASK="YES", GDAL_PAM_ENABLED="NO"):
                with rasterio.open(temporary, "w", **profile) as target:
                    block_height, block_width = target.block_shapes[0]
                    total_blocks = (
                        ((target.height + block_height - 1) // block_height)
                        * ((target.width + block_width - 1) // block_width)
                    )
                    report_every = max(1, total_blocks // 20)
                    for block_number, (_, target_window) in enumerate(
                        target.block_windows(1), 1
                    ):
                        source_window = Window(
                            crop_window.col_off + target_window.col_off,
                            crop_window.row_off + target_window.row_off,
                            target_window.width,
                            target_window.height,
                        )
                        data = source.read(window=source_window)
                        inside = geometry_mask(
                            [mapping(geometry)],
                            out_shape=(target_window.height, target_window.width),
                            transform=target.window_transform(target_window),
                            invert=True,
                            all_touched=all_touched,
                        )
                        data[:, ~inside] = 0
                        target.write(data, window=target_window)
                        valid = inside & np.any(data != 0, axis=0)
                        if valid.any():
                            has_valid_data = True
                        target.write_mask(valid.astype("uint8") * 255, window=target_window)
                        if (
                            block_number == 1
                            or block_number == total_blocks
                            or block_number % report_every == 0
                        ):
                            percent = block_number / total_blocks * 100
                            log(
                                f"县级裁剪进度 {county_name}："
                                f"{block_number}/{total_blocks} 块，{percent:.1f}%"
                            )
                    target.descriptions = source.descriptions
                    target.update_tags(
                        county_name=county_name,
                        county_code=county_code,
                        sensing_time=sensing_time,
                        source_city_tif=city_tif.name,
                        clipped_to_county_boundary="true",
                    )

        if not has_valid_data:
            log(f"县界内没有非零有效像元：{county_name}（{county_code}）", "WARN")
            return "no_coverage"
        output_path.parent.mkdir(parents=True, exist_ok=True)
        temporary.replace(output_path)
        create_minmax_jpeg(output_path, d)
        return "created"
    finally:
        if temporary.exists():
            temporary.unlink()


def process_task(
    task: CityTask,
    task_number: int,
    total_tasks: int,
    cities_crs,
    counties,
    args: argparse.Namespace,
    d: dict,
) -> dict:
    task_started_at = time.monotonic()
    log(
        f"[市级任务 {task_number}/{total_tasks}] 开始处理 {task.city_name} "
        f"{task.sensing_time}，来源瓦片 {len(task.sources)} 个"
    )
    city_tif = city_output_path(task, args.city_output_root)
    city_status = merge_city_task(
        task,
        city_tif,
        cities_crs,
        args.resolution,
        args.overwrite,
        args.all_touched,
        d,
    )
    result = {
        "task": task,
        "city_status": city_status,
        "county_created": 0,
        "county_existing": 0,
        "county_no_coverage": 0,
    }
    if city_status == "no_coverage":
        log(
            f"[市级任务 {task_number}/{total_tasks}] 无有效覆盖，结束："
            f"{task.city_name} {task.sensing_time}"
        )
        return result

    selected = counties[counties["_city_code"] == task.city_code]
    county_total = len(selected)
    log(
        f"[市级任务 {task_number}/{total_tasks}] 市级结果 {city_status}，"
        f"开始处理县级任务，共 {county_total} 个县"
    )
    for county_number, (_, county) in enumerate(selected.iterrows(), 1):
        county_name = county["_county_name"]
        county_code = county["_county_code"]
        log(
            f"[市级任务 {task_number}/{total_tasks}] "
            f"[县级任务 {county_number}/{county_total}] "
            f"处理 {task.city_name}/{county_name}（{county_code}）"
        )
        output_path = (
            args.country_output_root
            / safe_name(task.city_name)
            / safe_name(county_name)
            / f"{county_code}_{task.sensing_time}.tif"
        )
        status = clip_county(
            city_tif,
            output_path,
            county.geometry,
            counties.crs,
            county_name,
            county_code,
            task.sensing_time,
            args.overwrite,
            args.all_touched,
            d,
        )
        result[f"county_{status}"] += 1
        log(
            f"[市级任务 {task_number}/{total_tasks}] "
            f"[县级任务 {county_number}/{county_total}] 完成，状态：{status}"
        )
    log(
        f"[市级任务 {task_number}/{total_tasks}] 全部完成：{task.city_name} "
        f"{task.sensing_time}，耗时 {elapsed_text(task_started_at)}"
    )
    return result


def main() -> int:
    run_started_at = time.monotonic()
    args = parse_args()
    if args.max_workers < 1 or args.resolution <= 0:
        print("错误：max-workers 和 resolution 必须大于 0", file=sys.stderr)
        return 2
    args.input_root = args.input_root.expanduser().resolve()
    args.city_output_root = args.city_output_root.expanduser().resolve()
    args.country_output_root = args.country_output_root.expanduser().resolve()
    if not args.input_root.is_dir():
        print(f"错误：输入目录不存在：{args.input_root}", file=sys.stderr)
        return 1

    try:
        d = require_dependencies()
        cities, counties = load_boundaries(args, d)
        rasters, invalid = scan_rasters(args.input_root, cities.crs, d)
        tasks = build_city_tasks(rasters, cities)
    except Exception as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 1

    log(
        f"扫描完成：有效瓦片 {len(rasters)}，文件名不匹配 {len(invalid)}，"
        f"市级处理任务 {len(tasks)}。"
    )
    for path in invalid:
        log(f"跳过（文件名不匹配）：{path.name}", "WARN")
    if args.dry_run:
        for task_number, task in enumerate(tasks, 1):
            source_names = ", ".join(info.path.name for info in task.sources)
            log(
                f"[预览 {task_number}/{len(tasks)}] "
                f"{task.city_name} {task.sensing_time} <- {source_names}\n"
                f"      {city_output_path(task, args.city_output_root)}"
            )
        log("仅预览，未创建 TIFF 或 JPEG。")
        return 0

    city_created = city_existing = city_no_coverage = 0
    county_created = county_existing = county_no_coverage = failed = 0
    completed = 0

    def record(result=None, error: Exception | None = None, task=None) -> None:
        nonlocal city_created, city_existing, city_no_coverage
        nonlocal county_created, county_existing, county_no_coverage, failed, completed
        completed += 1
        if error is not None:
            failed += 1
            log(
                f"[总体进度 {completed}/{len(tasks)}] 失败："
                f"{task.city_name} {task.sensing_time}：{error}",
                "ERROR",
            )
            return
        current = result["task"]
        city_status = result["city_status"]
        if city_status == "created":
            city_created += 1
        elif city_status == "existing":
            city_existing += 1
        else:
            city_no_coverage += 1
        county_created += result["county_created"]
        county_existing += result["county_existing"]
        county_no_coverage += result["county_no_coverage"]
        log(
            f"[总体进度 {completed}/{len(tasks)}] "
            f"{current.city_name} {current.sensing_time}："
            f"市级 {city_status}，县级新建 {result['county_created']}，"
            f"已有 {result['county_existing']}，无覆盖 {result['county_no_coverage']}"
        )

    try:
        if args.max_workers == 1:
            for task_number, task in enumerate(tasks, 1):
                try:
                    record(
                        process_task(
                            task,
                            task_number,
                            len(tasks),
                            cities.crs,
                            counties,
                            args,
                            d,
                        )
                    )
                except Exception as exc:
                    record(error=exc, task=task)
        else:
            with ThreadPoolExecutor(max_workers=args.max_workers) as executor:
                futures = {
                    executor.submit(
                        process_task,
                        task,
                        task_number,
                        len(tasks),
                        cities.crs,
                        counties,
                        args,
                        d,
                    ): (task_number, task)
                    for task_number, task in enumerate(tasks, 1)
                }
                for future in as_completed(futures):
                    _, task = futures[future]
                    try:
                        record(future.result())
                    except Exception as exc:
                        record(error=exc, task=task)
    except KeyboardInterrupt:
        print("已停止处理。", file=sys.stderr)
        return 130

    log(
        f"处理完成：市级新建 {city_created}、已有 {city_existing}、"
        f"无覆盖 {city_no_coverage}；县级新建 {county_created}、"
        f"已有 {county_existing}、无覆盖 {county_no_coverage}；失败 {failed}；"
        f"总耗时 {elapsed_text(run_started_at)}。"
    )
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
