#!/usr/bin/env python
"""
逐幅配准并镶嵌多个具有重叠区的正射 TIFF。

本文件不会修改原来的 align_and_mosaic.py，而是复用其中经过验证的基础函数。
处理方式为：

    第 1 幅 + 第 2 幅 -> 临时累计影像
    临时累计影像 + 第 3 幅 -> 新的临时累计影像
    ...
    临时累计影像 + 最后一幅 -> 最终输出

默认使用局部匹配生成 GCP，并以 TPS（薄板样条/橡皮片）校正每一幅新增影像。
也可以使用 --model translation，只进行稳健的整体平移。

输入中已经是 Albers 的 TIFF 原样使用；非 Albers TIFF 会通过临时
Warped VRT 自动转换到统一的 CGCS2000 Albers 网格，不覆盖原始文件。

示例：
    python align_and_mosaic_multiple.py input_tif -o output/aligned_all.tif

只检查输入和处理顺序，不生成影像：
    python align_and_mosaic_multiple.py input_tif --check-only
"""

from __future__ import annotations

import argparse
import math
import tempfile
import warnings
from dataclasses import dataclass
from pathlib import Path

import cv2
import geopandas as gpd
import numpy as np
import pandas as pd
import rasterio
import shapely
from affine import Affine
from rasterio.control import GroundControlPoint
from rasterio.crs import CRS
from rasterio.enums import Resampling
from rasterio.shutil import copy as copy_raster
from rasterio.vrt import WarpedVRT
from rasterio.warp import calculate_default_transform, reproject, transform_bounds
from rasterio.windows import Window
from shapely.geometry import Polygon, box

# 复用原程序，不复制、也不修改它的基础实现。
from align_and_mosaic import (
    read_edge_image,
    same_grid,
    snapped_union_grid,
    write_gcp_vrt,
)


CGCS2000_ALBERS = CRS.from_string(
    "+proj=aea +lat_0=0 +lon_0=105 +lat_1=25 +lat_2=47 "
    "+x_0=0 +y_0=0 +ellps=GRS80 +units=m +no_defs"
)


@dataclass(frozen=True)
class RasterInfo:
    path: Path
    left: float
    bottom: float
    right: float
    top: float

    @property
    def bounds(self) -> tuple[float, float, float, float]:
        return self.left, self.bottom, self.right, self.top


@dataclass(frozen=True)
class AxisDisplacementModel:
    """与原双图代码一致的单轴局部位移模型。"""

    axis: str
    knots: np.ndarray
    dx: np.ndarray
    dy: np.ndarray


def intersection_area(
    a: tuple[float, float, float, float],
    b: tuple[float, float, float, float],
) -> float:
    """返回两个包围盒的相交面积。"""
    width = min(a[2], b[2]) - max(a[0], b[0])
    height = min(a[3], b[3]) - max(a[1], b[1])
    return max(0.0, width) * max(0.0, height)


def discover_files(inputs: list[Path]) -> list[Path]:
    """接受一个目录，或按用户给定顺序接受多个 TIFF。"""
    if len(inputs) == 1 and inputs[0].is_dir():
        directory = inputs[0]
        files = sorted(
            [*directory.glob("*.tif"), *directory.glob("*.tiff")],
            key=lambda p: p.name.lower(),
        )
    else:
        files = inputs

    files = [p.resolve() for p in files]
    if len(files) < 2:
        raise ValueError("至少需要两幅 tif/tiff。")
    missing = [str(p) for p in files if not p.is_file()]
    if missing:
        raise FileNotFoundError("找不到输入文件：" + "；".join(missing))
    if len(set(files)) != len(files):
        raise ValueError("输入列表中存在重复 TIFF。")
    return files


def is_albers_crs(crs) -> bool:
    """判断 CRS 是否采用 Albers 等积圆锥投影。"""
    if crs is None:
        return False
    try:
        return crs.to_dict().get("proj") == "aea"
    except (AttributeError, KeyError, TypeError):
        return "Albers" in str(crs)


def albers_grid(files: list[Path]) -> tuple[CRS, float, float, float, float]:
    """
    选择统一 Albers CRS、分辨率和网格锚点。

    若输入中已有 Albers TIFF，完全沿用第一幅 Albers 的 CRS 和网格；
    否则采用 CGCS2000 Albers，并按第一幅影像估算米制分辨率。
    """
    for path in files:
        with rasterio.open(path, sharing=False) as ds:
            if ds.crs is None:
                raise ValueError(f"影像缺少 CRS：{path}")
            if is_albers_crs(ds.crs):
                if any(abs(value) > 1e-12 for value in (
                    ds.transform.b, ds.transform.d
                )):
                    raise ValueError(
                        f"已有 Albers 影像带旋转项，无法作为统一网格：{path}"
                    )
                return (
                    ds.crs,
                    abs(float(ds.transform.a)),
                    abs(float(ds.transform.e)),
                    float(ds.transform.c),
                    float(ds.transform.f),
                )

    with rasterio.open(files[0], sharing=False) as ds:
        if ds.crs is None:
            raise ValueError(f"影像缺少 CRS：{files[0]}")
        transform, _, _ = calculate_default_transform(
            ds.crs,
            CGCS2000_ALBERS,
            ds.width,
            ds.height,
            *ds.bounds,
        )
        xres = abs(float(transform.a))
        yres = abs(float(transform.e))
        if xres <= 0 or yres <= 0:
            raise ValueError("无法计算 Albers 目标像元大小。")
        # 没有现成 Albers 网格时以坐标原点作为公共锚点。
        return CGCS2000_ALBERS, xres, yres, 0.0, 0.0


def aligned_warp_grid(
    dataset: rasterio.DatasetReader,
    target_crs: CRS,
    xres: float,
    yres: float,
    anchor_x: float,
    anchor_y: float,
) -> tuple[Affine, int, int]:
    """计算覆盖原影像、并与统一 Albers 网格整数对齐的目标范围。"""
    left, bottom, right, top = transform_bounds(
        dataset.crs,
        target_crs,
        *dataset.bounds,
        densify_pts=41,
    )
    aligned_left = anchor_x + math.floor(
        (left - anchor_x) / xres
    ) * xres
    aligned_right = anchor_x + math.ceil(
        (right - anchor_x) / xres
    ) * xres
    aligned_bottom = anchor_y + math.floor(
        (bottom - anchor_y) / yres
    ) * yres
    aligned_top = anchor_y + math.ceil(
        (top - anchor_y) / yres
    ) * yres
    width = int(round((aligned_right - aligned_left) / xres))
    height = int(round((aligned_top - aligned_bottom) / yres))
    if width <= 0 or height <= 0:
        raise ValueError(f"重投影后的影像尺寸无效：{dataset.name}")
    transform = Affine(
        xres, 0.0, aligned_left,
        0.0, -yres, aligned_top,
    )
    return transform, width, height


def normalize_inputs_to_albers(
    files: list[Path],
) -> tuple[list[Path], tempfile.TemporaryDirectory[str] | None]:
    """
    将非 Albers TIFF 以临时 Warped VRT 接入后续流程。

    原始 TIFF 不会改写；已有 Albers TIFF 路径保持不变。VRT 文件只有
    数 KB，影像像元在后续读取时由 GDAL 按需重投影。
    """
    target_crs, xres, yres, anchor_x, anchor_y = albers_grid(files)
    normalized: list[Path] = []
    temporary: tempfile.TemporaryDirectory[str] | None = None
    stems: set[str] = set()
    converted_count = 0

    print(
        "统一工作坐标系：CGCS2000 Albers；"
        f"像元大小约 {xres:.6f} x {yres:.6f} 米"
    )
    for path in files:
        if path.stem in stems:
            raise ValueError(
                f"存在同名 TIFF，无法唯一匹配 SHP：{path.stem}"
            )
        stems.add(path.stem)
        with rasterio.open(path, sharing=False) as ds:
            if ds.crs is None:
                raise ValueError(f"影像缺少 CRS：{path}")
            if is_albers_crs(ds.crs):
                if ds.crs != target_crs:
                    raise ValueError(
                        "输入中存在定义不同的 Albers 坐标系：\n"
                        f"{target_crs}\n{ds.crs}"
                    )
                normalized.append(path)
                print(f"  已是 Albers，原样使用：{path.name}")
                continue

            if temporary is None:
                temporary = tempfile.TemporaryDirectory(
                    prefix="albers_inputs_"
                )
            vrt_path = Path(temporary.name) / f"{path.stem}.vrt"
            transform, width, height = aligned_warp_grid(
                ds,
                target_crs,
                xres,
                yres,
                anchor_x,
                anchor_y,
            )
            vrt_options = {
                "crs": target_crs,
                "transform": transform,
                "width": width,
                "height": height,
                "resampling": Resampling.bilinear,
            }
            if ds.nodata is not None:
                vrt_options["src_nodata"] = ds.nodata
                vrt_options["nodata"] = ds.nodata
            with WarpedVRT(ds, **vrt_options) as warped:
                copy_raster(warped, vrt_path, driver="VRT")
            normalized.append(vrt_path)
            converted_count += 1
            print(f"  非 Albers，临时转换：{path.name}")

    if converted_count:
        print(
            f"已为 {converted_count} 幅非 Albers 影像创建临时 VRT；"
            "原始 TIFF 不变。"
        )
    return normalized, temporary


def read_infos(files: list[Path]) -> list[RasterInfo]:
    infos: list[RasterInfo] = []
    with rasterio.open(files[0], sharing=False) as reference:
        for path in files:
            with rasterio.open(path, sharing=False) as ds:
                same_grid(reference, ds)
                infos.append(
                    RasterInfo(
                        path=path,
                        left=ds.bounds.left,
                        bottom=ds.bounds.bottom,
                        right=ds.bounds.right,
                        top=ds.bounds.top,
                    )
                )
    return infos


def spatial_order(infos: list[RasterInfo]) -> list[RasterInfo]:
    """
    从西北角开始，每次选择与已处理影像重叠面积最大的下一幅。

    这比简单的文件名排序更适合文件名不含规则图幅编号的数据。
    """
    remaining = list(infos)
    first = max(remaining, key=lambda item: (item.top, -item.left))
    remaining.remove(first)
    ordered = [first]

    while remaining:
        def score(candidate: RasterInfo) -> float:
            return sum(
                intersection_area(candidate.bounds, done.bounds)
                for done in ordered
            )

        chosen = max(remaining, key=lambda item: (score(item), item.top, -item.left))
        if score(chosen) <= 0:
            raise ValueError(
                f"{chosen.path.name} 与此前影像均无重叠，无法建立连续配准链。"
            )
        remaining.remove(chosen)
        ordered.append(chosen)
    return ordered


def overlap_windows(
    reference: rasterio.DatasetReader,
    moving: rasterio.DatasetReader,
    limit_bounds: tuple[float, float, float, float] | None = None,
) -> tuple[int, int, int, int, int, int]:
    """计算两幅栅格包围盒重叠区在各自影像中的整数窗口。"""
    left = max(reference.bounds.left, moving.bounds.left)
    bottom = max(reference.bounds.bottom, moving.bounds.bottom)
    right = min(reference.bounds.right, moving.bounds.right)
    top = min(reference.bounds.top, moving.bounds.top)
    if limit_bounds is not None:
        left = max(left, limit_bounds[0])
        bottom = max(bottom, limit_bounds[1])
        right = min(right, limit_bounds[2])
        top = min(top, limit_bounds[3])
    if left >= right or bottom >= top:
        raise ValueError("当前累计影像与下一幅 TIFF 没有重叠区。")

    reference_col, reference_row = (~reference.transform) * (left, top)
    moving_col, moving_row = (~moving.transform) * (left, top)
    values = np.asarray(
        [reference_col, reference_row, moving_col, moving_row],
        dtype=np.float64,
    )
    if np.max(np.abs(values - np.round(values))) > 0.10:
        raise ValueError(
            "两幅图没有落在近似整数像元网格上，请先统一分辨率和原始网格。"
        )

    width = int(round((right - left) / reference.res[0]))
    height = int(round((top - bottom) / abs(reference.res[1])))
    return (
        int(round(reference_col)),
        int(round(reference_row)),
        int(round(moving_col)),
        int(round(moving_row)),
        width,
        height,
    )


def valid_fraction(ds: rasterio.DatasetReader, window: Window) -> float:
    """读取内部掩膜，避免在累计影像的空白矩形区域做匹配。"""
    with warnings.catch_warnings():
        warnings.filterwarnings(
            "ignore",
            category=DeprecationWarning,
            message="Setting the shape on a NumPy array.*",
        )
        mask = ds.read_masks(1, window=window)
    return float(np.count_nonzero(mask)) / float(mask.size)


def estimate_local_shifts(
    reference: rasterio.DatasetReader,
    moving: rasterio.DatasetReader,
    max_shift: float,
    min_response: float,
    limit_bounds: tuple[float, float, float, float] | None = None,
) -> tuple[
    float,
    float,
    list[tuple[float, float, float, float, float]],
]:
    """
    在累计影像与新增影像的有效重叠区中进行多窗口相位相关。

    返回 moving 内容相对于 reference 内容的 (dx, dy)，以及保留下来的
    (重叠区 x, 重叠区 y, dx, dy, response) 局部匹配。
    """
    ref_col, ref_row, mov_col, mov_row, width, height = overlap_windows(
        reference, moving, limit_bounds
    )

    if width >= height:
        # 南北拼接：完全沿用原代码，沿横向密集采样。
        patch_width = min(1800, width - 2)
        patch_height = min(800, height - 2)
    else:
        # 东西拼接：使用原代码的转置形式，沿纵向密集采样。
        patch_width = min(800, width - 2)
        patch_height = min(1800, height - 2)
    if patch_width < 256 or patch_height < 256:
        raise ValueError(
            f"重叠区太窄（{width} x {height} 像元），不足以可靠配准。"
        )

    margin_x = patch_width // 2 + 1
    margin_y = patch_height // 2 + 1
    if width >= height:
        nx = min(30, max(5, width // patch_width))
        ny = min(3, max(1, height // patch_height))
    else:
        nx = min(3, max(1, width // patch_width))
        ny = min(30, max(5, height // patch_height))
    xs = np.linspace(margin_x, width - margin_x, nx)
    ys = np.linspace(margin_y, height - margin_y, ny)
    hann = cv2.createHanningWindow(
        (patch_width, patch_height), cv2.CV_32F
    )

    matches: list[tuple[float, float, float, float, float]] = []
    for y in ys:
        for x in xs:
            x0 = int(round(x - patch_width / 2))
            y0 = int(round(y - patch_height / 2))
            ref_window = Window(
                ref_col + x0, ref_row + y0, patch_width, patch_height
            )
            mov_window = Window(
                mov_col + x0, mov_row + y0, patch_width, patch_height
            )

            a = read_edge_image(reference, ref_window)
            b = read_edge_image(moving, mov_window)
            shift, response = cv2.phaseCorrelate(a, b, hann)
            dx, dy = map(float, shift)
            if (
                response >= min_response
                and abs(dx) <= max_shift
                and abs(dy) <= max_shift
            ):
                matches.append((float(x), float(y), dx, dy, float(response)))

    if len(matches) < 3:
        raise RuntimeError(
            f"可靠匹配块只有 {len(matches)} 个；可尝试增大 --max-shift、"
            "降低 --min-response，或调整影像处理顺序。"
        )

    values = np.asarray(matches, dtype=np.float64)
    median = np.median(values[:, 2:4], axis=0)
    radial_error = np.linalg.norm(values[:, 2:4] - median, axis=1)
    radial_median = np.median(radial_error)
    mad = np.median(np.abs(radial_error - radial_median))
    cutoff = max(2.5, radial_median + 3.0 * 1.4826 * mad)
    good = values[radial_error <= cutoff]
    if len(good) < 3:
        good = values

    dx, dy = np.median(good[:, 2:4], axis=0)
    return float(dx), float(dy), [tuple(row) for row in good]


def best_neighbor_overlap(
    processed: list[RasterInfo],
    moving: RasterInfo,
) -> tuple[float, float, float, float]:
    """选择与新增图幅重叠面积最大的已处理原始邻图。"""
    neighbor = max(
        processed,
        key=lambda item: intersection_area(item.bounds, moving.bounds),
    )
    if intersection_area(neighbor.bounds, moving.bounds) <= 0:
        raise ValueError(f"{moving.path.name} 与已处理图幅均无重叠。")
    return (
        max(neighbor.left, moving.left),
        max(neighbor.bottom, moving.bottom),
        min(neighbor.right, moving.right),
        min(neighbor.top, moving.top),
    )


def build_axis_displacement_model(
    reference: rasterio.DatasetReader,
    moving: rasterio.DatasetReader,
    matches: list[tuple[float, float, float, float, float]],
    limit_bounds: tuple[float, float, float, float],
    model: str,
    global_dx: float,
    global_dy: float,
) -> AxisDisplacementModel:
    """
    复刻原代码的按列模型，并为左右拼接提供对称的按行模型。

    南北相邻的重叠区宽而矮：位移沿列变化；东西相邻的重叠区窄而高：
    位移沿行变化。正交方向复制控制关系，使 TPS 和 SHP 使用同一模型。
    """
    _, _, moving_col, moving_row, width, height = overlap_windows(
        reference, moving, limit_bounds
    )
    values = np.asarray(matches, dtype=np.float64)
    if width >= height:
        axis = "col"
        relative = values[:, 0]
        offset = float(moving_col)
        axis_size = moving.width
    else:
        axis = "row"
        relative = values[:, 1]
        offset = float(moving_row)
        axis_size = moving.height

    rows: list[tuple[float, float, float]] = []
    for value in np.unique(relative):
        selected = values[np.isclose(relative, value)]
        if model == "translation":
            local_dx, local_dy = global_dx, global_dy
        else:
            local_dx = float(np.median(selected[:, 2]))
            local_dy = float(np.median(selected[:, 3]))
        rows.append((offset + float(value), local_dx, local_dy))
    rows.sort()
    if len(rows) < 4:
        raise RuntimeError(
            f"{axis} 方向只有 {len(rows)} 个可靠控制位置，至少需要 4 个。"
        )

    # 像原双图代码一样把端点延伸到新增影像两端，防止 TPS 外插发散。
    if rows[0][0] > 1.0:
        rows.insert(0, (0.0, rows[0][1], rows[0][2]))
    if rows[-1][0] < axis_size - 2:
        rows.append(
            (float(axis_size - 1), rows[-1][1], rows[-1][2])
        )
    array = np.asarray(rows, dtype=np.float64)
    return AxisDisplacementModel(
        axis=axis,
        knots=array[:, 0],
        dx=array[:, 1],
        dy=array[:, 2],
    )


def make_axis_gcps(
    moving: rasterio.DatasetReader,
    displacement: AxisDisplacementModel,
) -> list[GroundControlPoint]:
    """从与 SHP 共用的单轴位移模型生成 TPS 控制点。"""
    gcps: list[GroundControlPoint] = []
    if displacement.axis == "col":
        cross_positions = np.linspace(0.0, float(moving.height - 1), 4)
        for knot, dx, dy in zip(
            displacement.knots,
            displacement.dx,
            displacement.dy,
            strict=True,
        ):
            for row in cross_positions:
                geo_x, geo_y = moving.transform * (float(knot), float(row))
                gcps.append(
                    GroundControlPoint(
                        row=float(row) + float(dy),
                        col=float(knot) + float(dx),
                        x=geo_x,
                        y=geo_y,
                    )
                )
    else:
        cross_positions = np.linspace(0.0, float(moving.width - 1), 4)
        for knot, dx, dy in zip(
            displacement.knots,
            displacement.dx,
            displacement.dy,
            strict=True,
        ):
            for col in cross_positions:
                geo_x, geo_y = moving.transform * (float(col), float(knot))
                gcps.append(
                    GroundControlPoint(
                        row=float(knot) + float(dy),
                        col=float(col) + float(dx),
                        x=geo_x,
                        y=geo_y,
                    )
                )
    return gcps


def idw_displacement(
    sample_points: np.ndarray,
    sample_values: np.ndarray,
    query_col: float,
    query_row: float,
) -> tuple[float, float]:
    """用距离反比权重把重叠区的局部偏移平滑延伸到整幅新增影像。"""
    distances = np.hypot(
        sample_points[:, 0] - query_col,
        sample_points[:, 1] - query_row,
    )
    nearest = int(np.argmin(distances))
    if distances[nearest] < 1e-9:
        return tuple(map(float, sample_values[nearest]))
    weights = 1.0 / np.maximum(distances, 1.0) ** 2
    result = np.sum(sample_values * weights[:, None], axis=0) / np.sum(weights)
    return float(result[0]), float(result[1])


def make_grid_gcps(
    reference: rasterio.DatasetReader,
    moving: rasterio.DatasetReader,
    matches: list[tuple[float, float, float, float, float]],
) -> list[GroundControlPoint]:
    """
    把局部偏移转换为覆盖新增影像的规则 GCP 网格。

    原程序针对南北拼接沿列复制控制点；多图版本可能同时遇到横向、纵向和
    L 形有效重叠，因此使用二维距离反比插值生成稳定的 5x5 控制网格。
    """
    _, _, moving_col, moving_row, _, _ = overlap_windows(reference, moving)
    values = np.asarray(matches, dtype=np.float64)
    sample_points = np.column_stack(
        (moving_col + values[:, 0], moving_row + values[:, 1])
    )
    sample_values = values[:, 2:4]

    cols = np.linspace(0.0, float(moving.width - 1), 5)
    rows = np.linspace(0.0, float(moving.height - 1), 5)
    gcps: list[GroundControlPoint] = []
    for row in rows:
        for col in cols:
            dx, dy = idw_displacement(
                sample_points, sample_values, float(col), float(row)
            )
            geo_x, geo_y = moving.transform * (float(col), float(row))
            gcps.append(
                GroundControlPoint(
                    row=float(row) + dy,
                    col=float(col) + dx,
                    x=geo_x,
                    y=geo_y,
                )
            )
    return gcps


def displacement_samples(
    reference: rasterio.DatasetReader,
    moving: rasterio.DatasetReader,
    matches: list[tuple[float, float, float, float, float]],
    model: str,
    dx: float,
    dy: float,
) -> tuple[np.ndarray, np.ndarray]:
    """返回位于 moving 目标像素网格中的位移采样点和值。"""
    _, _, moving_col, moving_row, _, _ = overlap_windows(reference, moving)
    values = np.asarray(matches, dtype=np.float64)
    points = np.column_stack(
        (moving_col + values[:, 0], moving_row + values[:, 1])
    )
    if model == "translation":
        shifts = np.tile(np.asarray([[dx, dy]], dtype=np.float64), (len(points), 1))
    else:
        shifts = values[:, 2:4].copy()
    return points, shifts


def interpolate_displacements(
    sample_points: np.ndarray,
    sample_values: np.ndarray,
    query_cols: np.ndarray,
    query_rows: np.ndarray,
) -> tuple[np.ndarray, np.ndarray]:
    """
    对大量矢量顶点进行低内存二维距离反比插值。

    逐个样本累计权重，避免构造“全部顶点 x 全部匹配点”的大型矩阵。
    """
    weight_sum = np.zeros(len(query_cols), dtype=np.float64)
    dx_sum = np.zeros(len(query_cols), dtype=np.float64)
    dy_sum = np.zeros(len(query_cols), dtype=np.float64)
    for (sample_col, sample_row), (sample_dx, sample_dy) in zip(
        sample_points, sample_values, strict=True
    ):
        distance_squared = (
            (query_cols - sample_col) ** 2
            + (query_rows - sample_row) ** 2
        )
        weights = 1.0 / np.maximum(distance_squared, 1.0)
        weight_sum += weights
        dx_sum += weights * sample_dx
        dy_sum += weights * sample_dy
    return dx_sum / weight_sum, dy_sum / weight_sum


def move_geometry_chunk(
    geometries,
    moving: rasterio.DatasetReader,
    sample_points: np.ndarray,
    sample_values: np.ndarray,
):
    """
    按新增 TIFF 的位移场移动一批矢量几何。

    q 是原始 SHP/TIFF 中的源像素，p 是校正后的目标像素。位移定义为
    q = p + displacement(p)，因此通过不动点迭代求 p。
    """
    inverse = ~moving.transform

    def transform_coordinates(coords: np.ndarray) -> np.ndarray:
        source_col = (
            inverse.a * coords[:, 0]
            + inverse.b * coords[:, 1]
            + inverse.c
        )
        source_row = (
            inverse.d * coords[:, 0]
            + inverse.e * coords[:, 1]
            + inverse.f
        )
        target_col = source_col.copy()
        target_row = source_row.copy()
        for _ in range(5):
            local_dx, local_dy = interpolate_displacements(
                sample_points,
                sample_values,
                target_col,
                target_row,
            )
            target_col = source_col - local_dx
            target_row = source_row - local_dy

        result = np.empty_like(coords)
        result[:, 0] = (
            moving.transform.a * target_col
            + moving.transform.b * target_row
            + moving.transform.c
        )
        result[:, 1] = (
            moving.transform.d * target_col
            + moving.transform.e * target_row
            + moving.transform.f
        )
        return result

    return shapely.transform(
        np.asarray(geometries, dtype=object),
        transform_coordinates,
    )


def move_geodataframe(
    frame: gpd.GeoDataFrame,
    moving: rasterio.DatasetReader,
    sample_points: np.ndarray,
    sample_values: np.ndarray,
    chunk_size: int = 5000,
) -> gpd.GeoDataFrame:
    """分批移动 GeoDataFrame，避免大型面数据一次占用过多内存。"""
    moved_parts: list[np.ndarray] = []
    geometries = frame.geometry.array
    for start in range(0, len(frame), chunk_size):
        moved_parts.append(
            move_geometry_chunk(
                geometries[start:start + chunk_size],
                moving,
                sample_points,
                sample_values,
            )
        )
    moved = frame.copy()
    if moved_parts:
        moved.geometry = np.concatenate(moved_parts)
    return moved


def move_geometry_chunk_axis(
    geometries,
    moving: rasterio.DatasetReader,
    displacement: AxisDisplacementModel,
):
    """严格按原双图代码的不动点公式移动一批矢量顶点。"""
    inverse = ~moving.transform

    def transform_coordinates(coords: np.ndarray) -> np.ndarray:
        source_col = (
            inverse.a * coords[:, 0]
            + inverse.b * coords[:, 1]
            + inverse.c
        )
        source_row = (
            inverse.d * coords[:, 0]
            + inverse.e * coords[:, 1]
            + inverse.f
        )
        target_col = source_col.copy()
        target_row = source_row.copy()
        for _ in range(5):
            query = (
                target_col
                if displacement.axis == "col"
                else target_row
            )
            local_dx = np.interp(
                query,
                displacement.knots,
                displacement.dx,
                left=displacement.dx[0],
                right=displacement.dx[-1],
            )
            local_dy = np.interp(
                query,
                displacement.knots,
                displacement.dy,
                left=displacement.dy[0],
                right=displacement.dy[-1],
            )
            target_col = source_col - local_dx
            target_row = source_row - local_dy

        result = np.empty_like(coords)
        result[:, 0] = (
            moving.transform.a * target_col
            + moving.transform.b * target_row
            + moving.transform.c
        )
        result[:, 1] = (
            moving.transform.d * target_col
            + moving.transform.e * target_row
            + moving.transform.f
        )
        return result

    return shapely.transform(
        np.asarray(geometries, dtype=object),
        transform_coordinates,
    )


def move_geodataframe_axis(
    frame: gpd.GeoDataFrame,
    moving: rasterio.DatasetReader,
    displacement: AxisDisplacementModel,
    chunk_size: int = 5000,
) -> gpd.GeoDataFrame:
    """按与 TIFF GCP 相同的单轴模型分批移动矢量。"""
    moved_parts: list[np.ndarray] = []
    geometries = frame.geometry.array
    for start in range(0, len(frame), chunk_size):
        moved_parts.append(
            move_geometry_chunk_axis(
                geometries[start:start + chunk_size],
                moving,
                displacement,
            )
        )
    moved = frame.copy()
    if moved_parts:
        moved.geometry = np.concatenate(moved_parts)
    return moved


def raster_footprint(
    ds: rasterio.DatasetReader,
    sample_points: np.ndarray | None = None,
    sample_values: np.ndarray | None = None,
) -> Polygon:
    """
    创建带加密边界的栅格覆盖面；提供位移场时同步校正覆盖面。

    边界加密后再移动，可以近似 TPS 产生的轻微弯曲边缘。
    """
    cols = np.linspace(0.0, float(ds.width), 33)
    rows = np.linspace(0.0, float(ds.height), 33)
    pixel_ring = (
        [(float(col), 0.0) for col in cols]
        + [(float(ds.width), float(row)) for row in rows[1:]]
        + [(float(col), float(ds.height)) for col in cols[-2::-1]]
        + [(0.0, float(row)) for row in rows[-2:0:-1]]
    )
    world_ring = [ds.transform * point for point in pixel_ring]
    footprint = Polygon(world_ring)
    if sample_points is not None and sample_values is not None:
        footprint = move_geometry_chunk(
            np.asarray([footprint], dtype=object),
            ds,
            sample_points,
            sample_values,
        )[0]
    return footprint


def raster_footprint_axis(
    ds: rasterio.DatasetReader,
    displacement: AxisDisplacementModel | None = None,
) -> Polygon:
    """创建栅格覆盖面，并按共用单轴模型同步校正边界。"""
    cols = np.linspace(0.0, float(ds.width), 33)
    rows = np.linspace(0.0, float(ds.height), 33)
    pixel_ring = (
        [(float(col), 0.0) for col in cols]
        + [(float(ds.width), float(row)) for row in rows[1:]]
        + [(float(col), float(ds.height)) for col in cols[-2::-1]]
        + [(0.0, float(row)) for row in rows[-2:0:-1]]
    )
    footprint = Polygon([ds.transform * point for point in pixel_ring])
    if displacement is not None:
        footprint = move_geometry_chunk_axis(
            np.asarray([footprint], dtype=object),
            ds,
            displacement,
        )[0]
    return footprint


def matching_shapefiles(
    tif_paths: list[Path],
    shp_dir: Path,
) -> dict[Path, Path]:
    """按 TIFF 文件名主干查找一一对应的 SHP。"""
    if not shp_dir.is_dir():
        raise FileNotFoundError(f"SHP 目录不存在：{shp_dir}")
    result = {
        tif_path: (shp_dir / f"{tif_path.stem}.shp").resolve()
        for tif_path in tif_paths
    }
    missing = [
        shp_path.name
        for shp_path in result.values()
        if not shp_path.is_file()
    ]
    if missing:
        raise FileNotFoundError(
            f"缺少 {len(missing)} 个同名 SHP：" + "；".join(missing)
        )
    return result


def read_vector_in_raster_crs(
    shp_path: Path,
    raster: rasterio.DatasetReader,
) -> tuple[gpd.GeoDataFrame, object]:
    """读取 SHP，并转换到对应 TIFF 的坐标系中进行像素位移。"""
    frame = gpd.read_file(shp_path)
    if frame.crs is None:
        raise ValueError(f"SHP 缺少 CRS：{shp_path}")
    original_crs = frame.crs
    return frame.to_crs(raster.crs), original_crs


def overlap_half_for_new(
    old_footprint,
    new_footprint,
    owned_overlap,
):
    """在两图重叠区中线处，返回靠近新增图幅一侧的半区。"""
    footprint_overlap = old_footprint.intersection(new_footprint)
    if footprint_overlap.is_empty:
        return owned_overlap.intersection(Polygon())
    left, bottom, right, top = footprint_overlap.bounds
    width = right - left
    height = top - bottom
    old_center = old_footprint.centroid
    new_center = new_footprint.centroid

    if width <= height:
        seam = (left + right) / 2.0
        if new_center.x >= old_center.x:
            half = box(seam, bottom, right, top)
        else:
            half = box(left, bottom, seam, top)
    else:
        seam = (bottom + top) / 2.0
        if new_center.y >= old_center.y:
            half = box(left, seam, right, top)
        else:
            half = box(left, bottom, right, seam)
    return owned_overlap.intersection(half)


def prepare_moving_vector(
    shp_path: Path,
    moving_path: Path,
    moving: rasterio.DatasetReader,
    displacement: AxisDisplacementModel,
    frames: list[gpd.GeoDataFrame],
    footprints: list,
    ownerships: list,
    seam_overlap_pixels: float,
) -> tuple[gpd.GeoDataFrame, object, object]:
    """
    校正新增 SHP，并在每对图幅的重叠区中线重新分配矢量所有权。

    不再把接缝放在旧图幅最外边缘；中线远离两侧数据坏边。写出几何时在
    共享接缝两侧各保留少量像元搭接，避免浮点裁切产生渲染白线。
    """
    moving_vector, _ = read_vector_in_raster_crs(shp_path, moving)
    moving_vector = move_geodataframe_axis(
        moving_vector,
        moving,
        displacement,
    )
    corrected_footprint = raster_footprint_axis(
        moving,
        displacement,
    )
    current_coverage = shapely.union_all(np.asarray(ownerships, dtype=object))
    new_ownership = corrected_footprint.difference(current_coverage)

    for index, (old_footprint, old_ownership) in enumerate(
        zip(footprints, ownerships, strict=True)
    ):
        owned_overlap = old_ownership.intersection(corrected_footprint)
        if owned_overlap.is_empty:
            continue
        new_claim = overlap_half_for_new(
            old_footprint,
            corrected_footprint,
            owned_overlap,
        )
        if new_claim.is_empty:
            continue
        ownerships[index] = old_ownership.difference(new_claim)
        new_ownership = new_ownership.union(new_claim)

        tolerance = seam_overlap_pixels * max(
            abs(moving.transform.a), abs(moving.transform.e)
        )
        old_clip_mask = ownerships[index]
        if tolerance > 0:
            old_clip_mask = old_clip_mask.buffer(tolerance).intersection(
                old_footprint
            )
        frames[index] = frames[index].clip(
            old_clip_mask,
            keep_geom_type=True,
        )
        frames[index] = frames[index][
            frames[index].geometry.notna()
            & ~frames[index].geometry.is_empty
        ].copy()

    moving_clip_mask = new_ownership
    if seam_overlap_pixels > 0:
        tolerance = seam_overlap_pixels * max(
            abs(moving.transform.a), abs(moving.transform.e)
        )
        moving_clip_mask = moving_clip_mask.buffer(tolerance).intersection(
            corrected_footprint
        )
    moving_vector = moving_vector.clip(
        moving_clip_mask,
        keep_geom_type=True,
    )
    moving_vector = moving_vector[
        moving_vector.geometry.notna() & ~moving_vector.geometry.is_empty
    ].copy()
    moving_vector["src_tif"] = moving_path.stem
    return moving_vector, corrected_footprint, new_ownership


def write_merged_shapefile(
    frames: list[gpd.GeoDataFrame],
    raster_crs,
    output_crs,
    output: Path,
) -> int:
    """合并各阶段矢量，修复几何并写出最终 ESRI Shapefile。"""
    merged = gpd.GeoDataFrame(
        pd.concat(frames, ignore_index=True, sort=False),
        geometry="geometry",
        crs=raster_crs,
    )
    merged = merged[merged.geometry.notna() & ~merged.geometry.is_empty].copy()
    if not merged.geometry.is_valid.all():
        merged.geometry = shapely.make_valid(
            merged.geometry.array,
            method="structure",
            keep_collapsed=False,
        )
        merged = merged[
            merged.geometry.notna() & ~merged.geometry.is_empty
        ].copy()
    merged = merged.to_crs(output_crs)
    output.parent.mkdir(parents=True, exist_ok=True)
    merged.to_file(output, driver="ESRI Shapefile", encoding="UTF-8")
    return len(merged)


def write_pair_mosaic(
    reference: rasterio.DatasetReader,
    moving: rasterio.DatasetReader,
    output: Path,
    moving_transform: Affine,
    moving_gcps: list[GroundControlPoint] | None,
    step_number: int,
    source_name: str,
    displacement_axis: str,
    num_threads: int = 1,
    stripe_rows: int = 512,
) -> None:
    """
    把一幅新增影像写入累计影像，再用累计影像的有效像元覆盖重叠部分。

    这样此前已经确定的镶嵌结果始终是几何基准，新增影像只扩展外部区域。
    """
    dst_transform, width, height = snapped_union_grid(
        reference, moving, moving_transform
    )
    nodata = reference.nodata if reference.nodata is not None else 0
    profile = reference.profile.copy()
    profile.update(
        driver="GTiff",
        width=width,
        height=height,
        transform=dst_transform,
        crs=reference.crs,
        nodata=nodata,
        tiled=True,
        blockxsize=512,
        blockysize=512,
        compress="DEFLATE",
        predictor=2,
        BIGTIFF="YES",
        interleave="pixel",
    )
    output.parent.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(
        prefix="multi_gcp_", dir=output.parent
    ) as vrt_temp:
        moving_source = moving
        moving_vrt = None
        if moving_gcps:
            vrt_path = Path(vrt_temp) / "moving_gcps.vrt"
            write_gcp_vrt(moving, moving_gcps, vrt_path)
            moving_vrt = rasterio.open(vrt_path, sharing=False)
            moving_source = moving_vrt

        try:
            with rasterio.open(output, "w", **profile) as dst:
                stripe_count = math.ceil(height / stripe_rows)
                print(
                    f"  开始分条带写入: {width} x {height}，"
                    f"{stripe_count} 个条带，线程={num_threads}",
                    flush=True,
                )
                for stripe_index, row_offset in enumerate(
                    range(0, height, stripe_rows), start=1
                ):
                    rows = min(stripe_rows, height - row_offset)
                    window = Window(0, row_offset, width, rows)
                    stripe_transform = dst_transform * Affine.translation(
                        0, row_offset
                    )
                    for band in range(1, reference.count + 1):
                        data = np.full(
                            (rows, width),
                            nodata,
                            dtype=reference.dtypes[band - 1],
                        )

                        moving_args = dict(
                            source=rasterio.band(moving_source, band),
                            destination=data,
                            src_nodata=moving.nodata,
                            dst_transform=stripe_transform,
                            dst_crs=reference.crs,
                            dst_nodata=nodata,
                            resampling=Resampling.bilinear,
                            init_dest_nodata=True,
                            num_threads=num_threads,
                            warp_mem_limit=128,
                        )
                        if moving_gcps:
                            moving_args.update(MAX_GCP_ORDER=-1)
                        else:
                            moving_args.update(
                                src_transform=moving_transform,
                                src_crs=moving.crs,
                            )
                        reproject(**moving_args)

                        # 累计影像后写入同一数组；其 nodata 不覆盖新增影像。
                        reproject(
                            source=rasterio.band(reference, band),
                            destination=data,
                            src_transform=reference.transform,
                            src_crs=reference.crs,
                            src_nodata=reference.nodata,
                            dst_transform=stripe_transform,
                            dst_crs=reference.crs,
                            dst_nodata=nodata,
                            resampling=Resampling.bilinear,
                            init_dest_nodata=False,
                            num_threads=num_threads,
                            warp_mem_limit=128,
                        )
                        dst.write(data, band, window=window)

                    if (
                        stripe_index == 1
                        or stripe_index == stripe_count
                        or stripe_index % max(1, stripe_count // 20) == 0
                    ):
                        percent = stripe_index * 100.0 / stripe_count
                        print(
                            f"    TIFF 写入进度: {stripe_index}/"
                            f"{stripe_count} ({percent:.1f}%)",
                            flush=True,
                        )

                dst.update_tags(
                    REGISTRATION=(
                        "sequential overlap phase correlation; "
                        + (
                            "2D GCP thin plate spline"
                            if moving_gcps
                            else "robust translation"
                        )
                    ),
                    MOSAIC_STEP=str(step_number),
                    ADDED_SOURCE=source_name,
                    ALIGNMENT_MODEL_VERSION="shared-axis-v1",
                    DISPLACEMENT_AXIS=displacement_axis,
                )
        finally:
            if moving_vrt is not None:
                moving_vrt.close()


def build_overviews(path: Path) -> None:
    with rasterio.open(path, "r+") as dst:
        max_factor = max(2, min(dst.width, dst.height) // 256)
        factors = [
            factor
            for factor in (2, 4, 8, 16, 32, 64, 128, 256)
            if factor <= max_factor
        ]
        if factors:
            dst.build_overviews(factors, Resampling.average)
            dst.update_tags(ns="rio_overview", resampling="average")


def print_order(infos: list[RasterInfo]) -> None:
    print(f"共发现 {len(infos)} 幅 TIFF，处理顺序：")
    for index, info in enumerate(infos, start=1):
        print(
            f"  {index:02d}. {info.path.name} "
            f"[{info.left:.7f}, {info.bottom:.7f}, "
            f"{info.right:.7f}, {info.top:.7f}]"
        )


def rebuild_vectors_only(
    infos: list[RasterInfo],
    shp_paths: dict[Path, Path],
    reference_mosaic: Path,
    output: Path,
    model: str,
    max_shift: float,
    min_response: float,
    seam_overlap_pixels: float,
) -> None:
    """利用已经完成的镶嵌 TIFF 重新配准并无缝合并全部 SHP。"""
    input_paths = [info.path for info in infos]
    frames: list[gpd.GeoDataFrame] = []
    footprints: list = []
    ownerships: list = []

    with rasterio.open(
        reference_mosaic, sharing=False
    ) as mosaic_reference, rasterio.open(
        input_paths[0], sharing=False
    ) as first_raster:
        same_grid(mosaic_reference, first_raster)
        first_vector, output_crs = read_vector_in_raster_crs(
            shp_paths[input_paths[0]], first_raster
        )
        raster_crs = first_raster.crs
        first_footprint = raster_footprint_axis(first_raster)
        first_vector = first_vector.clip(
            first_footprint, keep_geom_type=True
        )
        first_vector["src_tif"] = input_paths[0].stem
        frames.append(first_vector)
        footprints.append(first_footprint)
        ownerships.append(first_footprint)
        print(
            f"首幅 SHP: {shp_paths[input_paths[0]].name}，"
            f"保留 {len(first_vector)} 个要素"
        )

        for index, moving_path in enumerate(input_paths[1:], start=2):
            overlap_bounds = best_neighbor_overlap(
                infos[:index - 1],
                infos[index - 1],
            )
            print(
                f"\n[SHP {index}/{len(input_paths)}] "
                f"以最终镶嵌 TIFF 配准 {moving_path.name}"
            )
            with rasterio.open(moving_path, sharing=False) as moving:
                same_grid(mosaic_reference, moving)
                dx, dy, matches = estimate_local_shifts(
                    mosaic_reference,
                    moving,
                    max_shift=max_shift,
                    min_response=min_response,
                    limit_bounds=overlap_bounds,
                )
                displacement = build_axis_displacement_model(
                    mosaic_reference,
                    moving,
                    matches,
                    overlap_bounds,
                    model,
                    dx,
                    dy,
                )
                moving_vector, moving_footprint, moving_ownership = (
                    prepare_moving_vector(
                        shp_paths[moving_path],
                        moving_path,
                        moving,
                        displacement,
                        frames,
                        footprints,
                        ownerships,
                        seam_overlap_pixels,
                    )
                )
                frames.append(moving_vector)
                footprints.append(moving_footprint)
                ownerships.append(moving_ownership)
                print(
                    f"  匹配块={len(matches)}，dx={dx:.3f}，"
                    f"dy={dy:.3f}；无缝裁切后 {len(moving_vector)} 个要素"
                )

    count = write_merged_shapefile(
        frames,
        raster_crs,
        output_crs,
        output,
    )
    print(f"\nSHP 无缝重建完成: {output}（{count} 个要素）")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "inputs",
        nargs="+",
        type=Path,
        help="一个包含多个 TIFF 的目录，或按期望顺序列出的多个 TIFF",
    )
    parser.add_argument(
        "-o",
        "--output",
        type=Path,
        default=Path("output/aligned_mosaic_all.tif"),
        help="最终镶嵌 TIFF",
    )
    parser.add_argument(
        "--shp-dir",
        type=Path,
        help="与各 TIFF 同名的输入 SHP 所在目录",
    )
    parser.add_argument(
        "--shp-output",
        type=Path,
        default=Path("output/aligned_mosaic_all.shp"),
        help="同步校正并合并后的最终 Shapefile",
    )
    parser.add_argument(
        "--shp-seam-overlap-pixels",
        type=float,
        default=2.0,
        help="SHP 共享接缝两侧保留的微小搭接宽度，默认 2 像元",
    )
    parser.add_argument(
        "--vector-only",
        action="store_true",
        help="不生成 TIFF，仅以现有最终镶嵌 TIFF 无缝重建 SHP",
    )
    parser.add_argument(
        "--reference-mosaic",
        type=Path,
        help="--vector-only 使用的现有最终镶嵌 TIFF",
    )
    parser.add_argument(
        "--order",
        choices=("name", "spatial"),
        default="spatial",
        help="目录输入的排序方式；默认按文件名，spatial 为自动空间排序",
    )
    parser.add_argument(
        "--model",
        choices=("rubber", "translation"),
        default="rubber",
        help="rubber=二维 GCP/TPS 局部校正（默认）；translation=整体平移",
    )
    parser.add_argument("--max-shift", type=float, default=30.0)
    parser.add_argument(
        "--min-response",
        type=float,
        default=0.40,
        help="相位相关最低响应值，默认 0.40",
    )
    parser.add_argument(
        "--threads",
        type=int,
        default=1,
        help="GDAL 重投影线程数；大影像默认 1 最稳定",
    )
    parser.add_argument(
        "--stripe-rows",
        type=int,
        default=512,
        help="分条带写入的单条高度；默认 512 行",
    )
    parser.add_argument(
        "--keep-intermediate",
        action="store_true",
        help="在最终输出旁的 *_stages 目录保留每一步累计 TIFF",
    )
    parser.add_argument(
        "--resume-raster",
        type=Path,
        help="从已完成的累计阶段 TIFF 继续，需同时提供 --resume-count",
    )
    parser.add_argument(
        "--resume-count",
        type=int,
        help="续跑 TIFF 已包含的原始影像数量，例如 stage_002 对应 2",
    )
    parser.add_argument(
        "--build-overviews",
        action=argparse.BooleanOptionalAction,
        default=True,
        help=(
            "最终 TIFF 完成后自动构建 2--256 级金字塔（默认开启）；"
            "使用 --no-build-overviews 可关闭"
        ),
    )
    parser.add_argument(
        "--check-only",
        action="store_true",
        help="只检查输入兼容性并显示顺序，不执行配准镶嵌",
    )
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="允许覆盖已经存在的最终输出",
    )
    args = parser.parse_args()

    if args.max_shift <= 0:
        parser.error("--max-shift 必须大于 0。")
    if not 0 < args.min_response <= 1:
        parser.error("--min-response 必须在 (0, 1] 范围内。")
    if args.threads < 1:
        parser.error("--threads 必须至少为 1。")
    if args.stripe_rows < 128:
        parser.error("--stripe-rows 必须至少为 128。")
    if args.shp_seam_overlap_pixels < 0:
        parser.error("--shp-seam-overlap-pixels 不能小于 0。")
    if (args.resume_raster is None) != (args.resume_count is None):
        parser.error("--resume-raster 和 --resume-count 必须同时提供。")
    if args.vector_only and args.shp_dir is None:
        parser.error("--vector-only 必须同时提供 --shp-dir。")
    if args.vector_only and args.reference_mosaic is None:
        parser.error("--vector-only 必须同时提供 --reference-mosaic。")
    if args.vector_only and args.resume_raster is not None:
        parser.error("--vector-only 不能与续跑参数同时使用。")
    if not args.vector_only and args.reference_mosaic is not None:
        parser.error("--reference-mosaic 只用于 --vector-only。")

    albers_inputs_temporary: tempfile.TemporaryDirectory[str] | None = None
    try:
        files = discover_files(args.inputs)
        files, albers_inputs_temporary = normalize_inputs_to_albers(files)
        infos = read_infos(files)
        if args.order == "spatial":
            infos = spatial_order(infos)
        print_order(infos)
        input_paths = [info.path for info in infos]
        shp_paths = (
            matching_shapefiles(input_paths, args.shp_dir.resolve())
            if args.shp_dir is not None
            else None
        )
        if shp_paths:
            print(f"已找到 {len(shp_paths)} 个同名 SHP：{args.shp_dir.resolve()}")
        resume_path = (
            args.resume_raster.resolve()
            if args.resume_raster is not None
            else None
        )
        reference_mosaic = (
            args.reference_mosaic.resolve()
            if args.reference_mosaic is not None
            else None
        )
        if reference_mosaic is not None and not reference_mosaic.is_file():
            raise FileNotFoundError(
                f"最终镶嵌参考 TIFF 不存在：{reference_mosaic}"
            )
        if reference_mosaic is not None:
            with rasterio.open(
                reference_mosaic, sharing=False
            ) as reference_ds:
                if reference_ds.tags().get(
                    "ALIGNMENT_MODEL_VERSION"
                ) != "shared-axis-v1":
                    raise ValueError(
                        "--reference-mosaic 来自旧的二维 IDW 模型，"
                        "无法保证 SHP 与 TIFF 对齐；请先用当前版本重新生成。"
                    )
        if resume_path is not None:
            if not resume_path.is_file():
                raise FileNotFoundError(f"续跑 TIFF 不存在：{resume_path}")
            if not 2 <= args.resume_count < len(input_paths):
                raise ValueError(
                    "--resume-count 必须至少为 2，且小于输入 TIFF 总数。"
                )
            if shp_paths and args.resume_count > 2:
                raise ValueError(
                    "当前带 SHP 的续跑支持从 stage_002（--resume-count 2）"
                    "恢复；更晚阶段需要对应的矢量检查点。"
                )
            with rasterio.open(resume_path, sharing=False) as resume_ds, \
                    rasterio.open(input_paths[0], sharing=False) as first_ds:
                same_grid(resume_ds, first_ds)
                recorded_step = resume_ds.tags().get("MOSAIC_STEP")
                expected_step = str(args.resume_count - 1)
                if recorded_step != expected_step:
                    raise ValueError(
                        f"续跑 TIFF 的 MOSAIC_STEP={recorded_step!r}，"
                        f"但 --resume-count {args.resume_count} "
                        f"要求 MOSAIC_STEP={expected_step!r}。"
                    )
                model_version = resume_ds.tags().get(
                    "ALIGNMENT_MODEL_VERSION"
                )
                if model_version != "shared-axis-v1":
                    raise ValueError(
                        "续跑 TIFF 来自旧的二维 IDW 模型，不能与当前"
                        " TIFF/SHP 共用单轴模型混合；请从第 1 幅重新运行。"
                    )
            print(
                f"将从阶段 TIFF 续跑：{resume_path} "
                f"（已包含 {args.resume_count} 幅）"
            )
    except (OSError, ValueError) as exc:
        parser.error(str(exc))

    if args.check_only:
        print("检查完成：所有影像的 CRS、波段、数据类型和像元网格兼容。")
        if shp_paths:
            print("SHP 对应关系检查完成。")
        if albers_inputs_temporary is not None:
            albers_inputs_temporary.cleanup()
        return

    shp_output = args.shp_output.resolve()
    if shp_paths and shp_output.exists() and not args.overwrite:
        parser.error(
            f"SHP 输出已存在：{shp_output}；如需覆盖请添加 --overwrite。"
        )
    if shp_paths and shp_output in set(shp_paths.values()):
        parser.error("SHP 输出不能覆盖任何输入 SHP。")

    if args.vector_only:
        try:
            rebuild_vectors_only(
                infos=infos,
                shp_paths=shp_paths,
                reference_mosaic=reference_mosaic,
                output=shp_output,
                model=args.model,
                max_shift=args.max_shift,
                min_response=args.min_response,
                seam_overlap_pixels=args.shp_seam_overlap_pixels,
            )
        finally:
            if albers_inputs_temporary is not None:
                albers_inputs_temporary.cleanup()
        return

    output = args.output.resolve()
    if output.exists() and not args.overwrite:
        parser.error(f"输出已存在：{output}；如需覆盖请添加 --overwrite。")
    output.parent.mkdir(parents=True, exist_ok=True)

    stages_dir: Path | None = None
    temporary: tempfile.TemporaryDirectory[str] | None = None
    if args.keep_intermediate:
        stages_dir = output.parent / f"{output.stem}_stages"
        stages_dir.mkdir(parents=True, exist_ok=True)
    else:
        temporary = tempfile.TemporaryDirectory(
            prefix=f"{output.stem}_stages_", dir=output.parent
        )
        stages_dir = Path(temporary.name)

    completed_count = args.resume_count if resume_path is not None else 1
    current_path = resume_path if resume_path is not None else input_paths[0]
    vector_frames: list[gpd.GeoDataFrame] | None = None
    vector_footprints: list | None = None
    vector_ownerships: list | None = None
    vector_output_crs = None
    vector_raster_crs = None
    if shp_paths:
        with rasterio.open(input_paths[0], sharing=False) as first_raster:
            first_vector, vector_output_crs = read_vector_in_raster_crs(
                shp_paths[input_paths[0]], first_raster
            )
            vector_raster_crs = first_raster.crs
            first_footprint = raster_footprint_axis(first_raster)
            first_vector = first_vector.clip(
                first_footprint, keep_geom_type=True
            )
            first_vector["src_tif"] = input_paths[0].stem
            vector_frames = [first_vector]
            vector_footprints = [first_footprint]
            vector_ownerships = [first_footprint]
            print(
                f"首幅 SHP: {shp_paths[input_paths[0]].name}，"
                f"保留 {len(first_vector)} 个要素"
            )

    # 当前可恢复的真实案例是 stage_002：重做一次轻量匹配即可重建前两幅
    # SHP 的累计状态，不需要重新生成 15 GB 的阶段 TIFF。
    if resume_path is not None and shp_paths and args.resume_count == 2:
        with rasterio.open(
            input_paths[0], sharing=False
        ) as first_raster, rasterio.open(
            input_paths[1], sharing=False
        ) as second_raster:
            overlap_bounds = best_neighbor_overlap(infos[:1], infos[1])
            dx, dy, matches = estimate_local_shifts(
                first_raster,
                second_raster,
                max_shift=args.max_shift,
                min_response=args.min_response,
                limit_bounds=overlap_bounds,
            )
            displacement = build_axis_displacement_model(
                first_raster,
                second_raster,
                matches,
                overlap_bounds,
                args.model,
                dx,
                dy,
            )
            second_vector, second_footprint, second_ownership = (
                prepare_moving_vector(
                shp_paths[input_paths[1]],
                input_paths[1],
                second_raster,
                displacement,
                vector_frames,
                vector_footprints,
                vector_ownerships,
                args.shp_seam_overlap_pixels,
                )
            )
            vector_frames.append(second_vector)
            vector_footprints.append(second_footprint)
            vector_ownerships.append(second_ownership)
            print(
                f"已重建 stage_002 的 SHP 状态：第二幅保留 "
                f"{len(second_vector)} 个要素"
            )

    try:
        for index, moving_path in enumerate(
            input_paths[completed_count:],
            start=completed_count + 1,
        ):
            is_last = index == len(input_paths)
            stage_output = (
                output
                if is_last
                else stages_dir / f"stage_{index:03d}.tif"
            )
            previous_stage = current_path
            print(
                f"\n[{index - 1}/{len(input_paths) - 1}] "
                f"累计影像 + {moving_path.name}"
            )
            with rasterio.open(
                current_path, sharing=False
            ) as reference, rasterio.open(
                moving_path, sharing=False
            ) as moving:
                same_grid(reference, moving)
                overlap_bounds = best_neighbor_overlap(
                    infos[:index - 1],
                    infos[index - 1],
                )
                dx, dy, matches = estimate_local_shifts(
                    reference,
                    moving,
                    max_shift=args.max_shift,
                    min_response=args.min_response,
                    limit_bounds=overlap_bounds,
                )
                corrected_transform = moving.transform * Affine.translation(
                    -dx, -dy
                )
                local = np.asarray(matches, dtype=np.float64)[:, 2:4]
                residual = np.linalg.norm(
                    local - np.asarray([dx, dy]), axis=1
                )
                displacement = build_axis_displacement_model(
                    reference,
                    moving,
                    matches,
                    overlap_bounds,
                    args.model,
                    dx,
                    dy,
                )
                gcps = (
                    make_axis_gcps(moving, displacement)
                    if args.model == "rubber"
                    else None
                )

                print(
                    f"  可靠匹配块: {len(matches)}；"
                    f"dx={dx:.3f}, dy={dy:.3f} 像元；"
                    f"残差 P90={np.percentile(residual, 90):.3f} 像元"
                )
                if gcps:
                    print(
                        f"  {displacement.axis} 轴 TPS 控制位置: "
                        f"{len(displacement.knots)}（{len(gcps)} 个 GCP）"
                    )

                if (
                    shp_paths
                    and vector_frames is not None
                    and vector_footprints is not None
                    and vector_ownerships is not None
                ):
                    (
                        moving_vector,
                        moving_footprint,
                        moving_ownership,
                    ) = prepare_moving_vector(
                        shp_paths[moving_path],
                        moving_path,
                        moving,
                        displacement,
                        vector_frames,
                        vector_footprints,
                        vector_ownerships,
                        args.shp_seam_overlap_pixels,
                    )
                    vector_frames.append(moving_vector)
                    vector_footprints.append(moving_footprint)
                    vector_ownerships.append(moving_ownership)
                    print(
                        f"  SHP 校正并裁掉重复区后保留: "
                        f"{len(moving_vector)} 个要素"
                    )

                write_pair_mosaic(
                    reference=reference,
                    moving=moving,
                    output=stage_output,
                    moving_transform=corrected_transform,
                    moving_gcps=gcps,
                    step_number=index - 1,
                    source_name=moving_path.name,
                    displacement_axis=displacement.axis,
                    num_threads=args.threads,
                    stripe_rows=args.stripe_rows,
                )
            current_path = stage_output
            # 默认只保留下一轮真正需要的最新累计影像，控制磁盘峰值。
            # 原始输入永远不会进入 stages_dir，因此不会被此处删除。
            if (
                not args.keep_intermediate
                and previous_stage.parent == stages_dir
                and previous_stage.is_file()
            ):
                previous_stage.unlink()
            print(f"  阶段输出完成: {stage_output}")

        if args.build_overviews:
            print("\n正在为最终影像创建金字塔……")
            build_overviews(output)
        if (
            shp_paths
            and vector_frames is not None
            and vector_output_crs is not None
            and vector_raster_crs is not None
        ):
            print("\n正在合并并写出最终 Shapefile……")
            vector_count = write_merged_shapefile(
                vector_frames,
                vector_raster_crs,
                vector_output_crs,
                shp_output,
            )
            print(f"SHP 输出完成: {shp_output}（{vector_count} 个要素）")
        print(f"\n全部完成: {output}")
    finally:
        if temporary is not None:
            temporary.cleanup()
        if albers_inputs_temporary is not None:
            albers_inputs_temporary.cleanup()


if __name__ == "__main__":
    main()
