#!/usr/bin/env python
"""
先利用两幅正射影像的重叠区估计地物偏移，再按统一网格镶嵌。

依赖:
    pip install rasterio opencv-python numpy

只检测偏移（不会生成大文件）:
    python align_and_mosaic.py input_tif --estimate-only

配准并镶嵌:
    python align_and_mosaic.py input_tif -o aligned_mosaic.tif
"""

from __future__ import annotations

import argparse
import math
import tempfile
import warnings
from pathlib import Path
from xml.etree import ElementTree as ET

import cv2
import geopandas as gpd
import numpy as np
import pandas as pd
import rasterio
import shapely
from affine import Affine
from rasterio.enums import Resampling
from rasterio.control import GroundControlPoint
from rasterio.transform import array_bounds
from rasterio.warp import reproject
from rasterio.windows import Window
from shapely.geometry import box


def same_grid(a: rasterio.DatasetReader, b: rasterio.DatasetReader) -> None:
    """检查本方法所需的基本条件。"""
    if a.crs != b.crs:
        raise ValueError(f"两幅影像 CRS 不同：\n{a.crs}\n{b.crs}")
    if a.count != b.count or a.dtypes != b.dtypes:
        raise ValueError("波段数或数据类型不同，需要先统一。")
    if not np.allclose(a.res, b.res, rtol=0, atol=1e-12):
        raise ValueError(f"像元大小不同：{a.res} 与 {b.res}")
    if any(abs(v) > 1e-12 for v in (a.transform.b, a.transform.d,
                                     b.transform.b, b.transform.d)):
        raise ValueError("输入影像带旋转项，请先重投影为 north-up 栅格。")


def overlap_geometry(a: rasterio.DatasetReader, b: rasterio.DatasetReader):
    left = max(a.bounds.left, b.bounds.left)
    bottom = max(a.bounds.bottom, b.bounds.bottom)
    right = min(a.bounds.right, b.bounds.right)
    top = min(a.bounds.top, b.bounds.top)
    if left >= right or bottom >= top:
        raise ValueError("两幅影像没有重叠区，无法自动估计地物偏移。")

    # 同一地理位置在两幅图上的像素坐标。当前数据恰好落在整数像元网格上。
    ca, ra = (~a.transform) * (left, top)
    cb, rb = (~b.transform) * (left, top)
    vals = np.array([ca, ra, cb, rb])
    if np.max(np.abs(vals - np.round(vals))) > 0.05:
        raise ValueError(
            "两幅图的原始网格不是近似整数对齐；请先重投影到同一像元网格。"
        )

    width = int(round((right - left) / a.res[0]))
    height = int(round((top - bottom) / abs(a.res[1])))
    return (
        int(round(ca)), int(round(ra)),
        int(round(cb)), int(round(rb)),
        width, height,
    )


def read_edge_image(ds: rasterio.DatasetReader, window: Window) -> np.ndarray:
    """读取一个波段并转为对亮度差异不敏感的梯度图。"""
    # rasterio 1.5 + NumPy 2.5 读取窗口时会发出一个无害的 shape 弃用警告。
    with warnings.catch_warnings():
        warnings.filterwarnings(
            "ignore", category=DeprecationWarning,
            message="Setting the shape on a NumPy array.*",
        )
        img = ds.read(1, window=window).astype(np.float32)
    valid = img != ds.nodata if ds.nodata is not None else np.isfinite(img)
    img[~valid] = 0
    gx = cv2.Sobel(img, cv2.CV_32F, 1, 0, ksize=3)
    gy = cv2.Sobel(img, cv2.CV_32F, 0, 1, ksize=3)
    edge = cv2.magnitude(gx, gy)
    edge[~valid] = 0
    return np.ascontiguousarray(edge)


def estimate_shift(
    reference: rasterio.DatasetReader,
    moving: rasterio.DatasetReader,
    max_shift: float = 30.0,
) -> tuple[float, float, list[tuple[float, float, float, float, float]]]:
    """
    返回 moving 相对于 reference 的内容偏移 (dx, dy)，单位为像元。

    OpenCV 的约定是：
      moving 中的同名点 = reference 中的同名点 + (dx, dy)
    """
    cr, rr, cm, rm, ow, oh = overlap_geometry(reference, moving)
    # 较密的横向采样用于捕获截图中沿接缝变化的局部偏移。
    patch_w = min(1800, ow - 2)
    patch_h = min(800, oh - 2)
    if patch_w < 256 or patch_h < 256:
        raise ValueError(f"重叠区太窄（{ow} x {oh} 像元），不足以可靠配准。")

    margin_x = patch_w // 2 + 1
    margin_y = patch_h // 2 + 1
    nx = min(30, max(5, ow // patch_w))
    ny = min(3, max(1, oh // patch_h))
    xs = np.linspace(margin_x, ow - margin_x, nx)
    ys = np.linspace(margin_y, oh - margin_y, ny)
    hann = cv2.createHanningWindow((patch_w, patch_h), cv2.CV_32F)

    matches: list[tuple[float, float, float, float, float]] = []
    for y in ys:
        for x in xs:
            x0 = int(round(x - patch_w / 2))
            y0 = int(round(y - patch_h / 2))
            wa = Window(cr + x0, rr + y0, patch_w, patch_h)
            wb = Window(cm + x0, rm + y0, patch_w, patch_h)
            a = read_edge_image(reference, wa)
            b = read_edge_image(moving, wb)
            shift, response = cv2.phaseCorrelate(a, b, hann)
            dx, dy = map(float, shift)
            if (
                response >= 0.40
                and abs(dx) <= max_shift
                and abs(dy) <= max_shift
            ):
                matches.append((float(x), float(y), dx, dy, float(response)))

    if len(matches) < 3:
        raise RuntimeError(
            f"可靠匹配块只有 {len(matches)} 个；可尝试增大 --max-shift。"
        )

    arr = np.asarray(matches)
    med = np.median(arr[:, 2:4], axis=0)
    radial_error = np.linalg.norm(arr[:, 2:4] - med, axis=1)
    mad = np.median(np.abs(radial_error - np.median(radial_error)))
    cutoff = max(2.5, np.median(radial_error) + 3.0 * 1.4826 * mad)
    good = arr[radial_error <= cutoff]
    if len(good) < 3:
        good = arr

    # 中位数不易被局部地形视差、云和变化地物拉偏。
    dx, dy = np.median(good[:, 2:4], axis=0)
    return float(dx), float(dy), [tuple(row) for row in good]


def make_rubber_gcps(
    reference: rasterio.DatasetReader,
    moving: rasterio.DatasetReader,
    matches: list[tuple[float, float, float, float, float]],
) -> tuple[list[GroundControlPoint], list[tuple[float, float, float]]]:
    """
    把重叠带中的局部偏移变成 TPS 控制点。

    重叠区几乎覆盖本数据的完整宽度，因此按列估计 dx/dy；在影像高度方向
    复制这些控制关系，使校正量向南稳定延伸，避免 TPS 在控制点凸包之外发散。
    """
    cr, rr, cm, rm, ow, _ = overlap_geometry(reference, moving)
    del cr, rr, rm
    arr = np.asarray(matches, dtype=np.float64)

    # estimate_shift 在相同 x 上会取 1--3 个不同 y 的窗口；先按列稳健汇总。
    columns: list[tuple[float, float, float]] = []
    for x in np.unique(arr[:, 0]):
        rows = arr[np.isclose(arr[:, 0], x)]
        columns.append(
            (float(x), float(np.median(rows[:, 2])), float(np.median(rows[:, 3])))
        )
    columns.sort()
    if len(columns) < 4:
        raise RuntimeError("局部形变模型至少需要 4 列可靠匹配控制点。")

    # 给左右边缘增加端点，防止边缘区域外插。
    if columns[0][0] > 1:
        columns.insert(0, (0.0, columns[0][1], columns[0][2]))
    if columns[-1][0] < ow - 2:
        columns.append((float(ow - 1), columns[-1][1], columns[-1][2]))

    gcps: list[GroundControlPoint] = []
    base_rows = np.linspace(0.0, float(moving.height - 1), 4)
    for rel_x, dx, dy in columns:
        base_col = float(cm) + rel_x
        for base_row in base_rows:
            # moving 中 (base_col+dx, base_row+dy) 的地物，应该落到原网格
            # (base_col, base_row) 的地理位置。
            geo_x, geo_y = moving.transform * (base_col, base_row)
            gcps.append(
                GroundControlPoint(
                    row=base_row + dy,
                    col=base_col + dx,
                    x=geo_x,
                    y=geo_y,
                )
            )
    return gcps, columns


def write_gcp_vrt(
    source: rasterio.DatasetReader,
    gcps: list[GroundControlPoint],
    path: Path,
) -> None:
    """创建一个只引用原始像素、但以 GCP/TPS 作为定位依据的轻量 VRT。"""
    dtype_names = {
        "uint8": "Byte",
        "uint16": "UInt16",
        "int16": "Int16",
        "uint32": "UInt32",
        "int32": "Int32",
        "float32": "Float32",
        "float64": "Float64",
    }
    root = ET.Element(
        "VRTDataset",
        rasterXSize=str(source.width),
        rasterYSize=str(source.height),
    )
    gcp_list = ET.SubElement(root, "GCPList", Projection=source.crs.to_wkt())
    for i, gcp in enumerate(gcps):
        ET.SubElement(
            gcp_list,
            "GCP",
            Id=str(i),
            Pixel=f"{gcp.col:.12f}",
            Line=f"{gcp.row:.12f}",
            X=f"{gcp.x:.15f}",
            Y=f"{gcp.y:.15f}",
            Z="0",
        )

    source_path = str(Path(source.name).resolve())
    for band in range(1, source.count + 1):
        vrt_band = ET.SubElement(
            root,
            "VRTRasterBand",
            dataType=dtype_names[source.dtypes[band - 1]],
            band=str(band),
        )
        if source.nodata is not None:
            ET.SubElement(vrt_band, "NoDataValue").text = str(source.nodata)
        simple = ET.SubElement(vrt_band, "SimpleSource")
        ET.SubElement(
            simple, "SourceFilename", relativeToVRT="0"
        ).text = source_path
        ET.SubElement(simple, "SourceBand").text = str(band)
        ET.SubElement(
            simple,
            "SrcRect",
            xOff="0",
            yOff="0",
            xSize=str(source.width),
            ySize=str(source.height),
        )
        ET.SubElement(
            simple,
            "DstRect",
            xOff="0",
            yOff="0",
            xSize=str(source.width),
            ySize=str(source.height),
        )
    ET.ElementTree(root).write(path, encoding="utf-8", xml_declaration=True)


def write_crop_vrt(
    source: rasterio.DatasetReader,
    height: int,
    path: Path,
) -> None:
    """创建从影像顶部到指定行的 VRT，用于把接缝放在重叠区内部。"""
    dtype_names = {
        "uint8": "Byte", "uint16": "UInt16", "int16": "Int16",
        "uint32": "UInt32", "int32": "Int32",
        "float32": "Float32", "float64": "Float64",
    }
    root = ET.Element(
        "VRTDataset",
        rasterXSize=str(source.width),
        rasterYSize=str(height),
    )
    ET.SubElement(root, "SRS").text = source.crs.to_wkt()
    t = source.transform
    ET.SubElement(root, "GeoTransform").text = ", ".join(
        f"{v:.15g}" for v in (t.c, t.a, t.b, t.f, t.d, t.e)
    )
    source_path = str(Path(source.name).resolve())
    for band in range(1, source.count + 1):
        vrt_band = ET.SubElement(
            root, "VRTRasterBand",
            dataType=dtype_names[source.dtypes[band - 1]],
            band=str(band),
        )
        if source.nodata is not None:
            ET.SubElement(vrt_band, "NoDataValue").text = str(source.nodata)
        simple = ET.SubElement(vrt_band, "SimpleSource")
        ET.SubElement(
            simple, "SourceFilename", relativeToVRT="0"
        ).text = source_path
        ET.SubElement(simple, "SourceBand").text = str(band)
        for name in ("SrcRect", "DstRect"):
            ET.SubElement(
                simple, name, xOff="0", yOff="0",
                xSize=str(source.width), ySize=str(height),
            )
    ET.ElementTree(root).write(path, encoding="utf-8", xml_declaration=True)


def move_geometry_like_raster(
    geometries,
    moving: rasterio.DatasetReader,
    columns: list[tuple[float, float, float]],
    overlap_col: int,
):
    """
    按 moving TIFF 的局部位移场移动矢量顶点。

    columns 中 dx/dy 的含义是：
      moving 源像素 q = 校正后目标像素 p + displacement(p)
    因而对每个源矢量点 q，迭代求 p = q - displacement(p)。
    """
    knots = np.asarray(columns, dtype=np.float64)
    knot_cols = overlap_col + knots[:, 0]
    knot_dx = knots[:, 1]
    knot_dy = knots[:, 2]
    inv = ~moving.transform

    def transform_coordinates(coords: np.ndarray) -> np.ndarray:
        source_col = inv.a * coords[:, 0] + inv.b * coords[:, 1] + inv.c
        source_row = inv.d * coords[:, 0] + inv.e * coords[:, 1] + inv.f
        target_col = source_col.copy()
        target_row = source_row.copy()

        # 位移只有数个像元，4 次不动点迭代足以达到远小于 0.01 像元。
        for _ in range(4):
            dx = np.interp(
                target_col, knot_cols, knot_dx,
                left=knot_dx[0], right=knot_dx[-1],
            )
            dy = np.interp(
                target_col, knot_cols, knot_dy,
                left=knot_dy[0], right=knot_dy[-1],
            )
            target_col = source_col - dx
            target_row = source_row - dy

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


def mosaic_shapefiles(
    reference: rasterio.DatasetReader,
    moving: rasterio.DatasetReader,
    columns: list[tuple[float, float, float]],
    shp_dir: Path,
    output: Path,
) -> None:
    """同步校正 moving SHP，并在影像使用的同一条接缝上拼接两个 SHP。"""
    reference_path = shp_dir / f"{Path(reference.name).stem}.shp"
    moving_path = shp_dir / f"{Path(moving.name).stem}.shp"
    missing = [str(p) for p in (reference_path, moving_path) if not p.exists()]
    if missing:
        raise FileNotFoundError("找不到对应 SHP：" + "；".join(missing))

    reference_gdf = gpd.read_file(reference_path)
    moving_gdf = gpd.read_file(moving_path)
    if reference_gdf.crs is None or moving_gdf.crs is None:
        raise ValueError("SHP 缺少 CRS，不能安全地同步影像位移。")
    if reference_gdf.crs != moving_gdf.crs:
        raise ValueError("两份 SHP 的 CRS 不一致。")
    output_crs = reference_gdf.crs

    # 在 TIFF 坐标系内执行像素位移和接缝裁切。
    reference_geo = reference_gdf.to_crs(reference.crs)
    moving_geo = moving_gdf.to_crs(moving.crs)
    _, _, moving_overlap_col, _, _, overlap_rows = overlap_geometry(
        reference, moving
    )
    moving_geo.geometry = move_geometry_like_raster(
        moving_geo.geometry.array,
        moving,
        columns,
        moving_overlap_col,
    )

    _, reference_overlap_row, _, _, _, _ = overlap_geometry(reference, moving)
    reference_seam_row = reference_overlap_row + overlap_rows // 2
    _, seam_y = reference.transform * (0.0, float(reference_seam_row))
    union_left = min(reference.bounds.left, moving.bounds.left) - 1.0
    union_right = max(reference.bounds.right, moving.bounds.right) + 1.0
    north_mask = box(union_left, seam_y, union_right, 90.0)
    south_mask = box(union_left, -90.0, union_right, seam_y)

    # 只保留各自在接缝一侧的要素，裁掉 2516 行重叠区中的重复部分。
    reference_geo = reference_geo.clip(north_mask, keep_geom_type=True)
    moving_geo = moving_geo.clip(south_mask, keep_geom_type=True)
    reference_geo["src_tif"] = Path(reference.name).stem
    moving_geo["src_tif"] = Path(moving.name).stem
    merged = gpd.GeoDataFrame(
        pd.concat([reference_geo, moving_geo], ignore_index=True),
        geometry="geometry",
        crs=reference.crs,
    )
    merged = merged[~merged.geometry.is_empty & merged.geometry.notna()].copy()
    if not merged.geometry.is_valid.all():
        merged.geometry = shapely.make_valid(merged.geometry.array)
    merged = merged.to_crs(output_crs)

    output.parent.mkdir(parents=True, exist_ok=True)
    merged.to_file(output, driver="ESRI Shapefile", encoding="UTF-8")
    print(
        f"SHP 输出完成: {output.resolve()} "
        f"（reference {len(reference_geo)} + moving {len(moving_geo)} "
        f"= {len(merged)} 个要素）"
    )


def snapped_union_grid(
    reference: rasterio.DatasetReader,
    moving: rasterio.DatasetReader,
    moving_transform: Affine,
) -> tuple[Affine, int, int]:
    mb = array_bounds(moving.height, moving.width, moving_transform)
    left = min(reference.bounds.left, mb[0])
    bottom = min(reference.bounds.bottom, mb[1])
    right = max(reference.bounds.right, mb[2])
    top = max(reference.bounds.top, mb[3])
    xres, yres = reference.res[0], abs(reference.res[1])

    col0 = math.floor((left - reference.transform.c) / xres)
    col1 = math.ceil((right - reference.transform.c) / xres)
    row0 = math.floor((reference.transform.f - top) / yres)
    row1 = math.ceil((reference.transform.f - bottom) / yres)
    transform = reference.transform * Affine.translation(col0, row0)
    return transform, col1 - col0, row1 - row0


def mosaic(
    reference: rasterio.DatasetReader,
    moving: rasterio.DatasetReader,
    moving_transform: Affine,
    output: Path,
    build_overviews: bool,
    moving_gcps: list[GroundControlPoint] | None = None,
    feather_pixels: int = 0,
) -> None:
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

    # rasterio 对“已有 GeoTransform 的 DatasetBand + 外部 GCP”会优先使用原
    # GeoTransform。用临时 VRT 去掉旧变换并内嵌 GCP，确保 TPS 真正生效。
    with tempfile.TemporaryDirectory(
        prefix="rubber_", dir=output.parent
    ) as temp_dir:
        moving_source = moving
        moving_vrt = None
        if moving_gcps:
            vrt_path = Path(temp_dir) / "moving_gcps.vrt"
            write_gcp_vrt(moving, moving_gcps, vrt_path)
            moving_vrt = rasterio.open(vrt_path, sharing=False)
            moving_source = moving_vrt

        # 在重叠带中线切开北幅，不再使用它带暗边的最后一行。
        _, ref_overlap_row, _, _, _, overlap_height = overlap_geometry(
            reference, moving
        )
        reference_cut_height = ref_overlap_row + overlap_height // 2
        seam_y = (
            reference.transform
            * (0.0, float(reference_cut_height))
        )[1]
        reference_vrt_path = Path(temp_dir) / "reference_cut.vrt"
        write_crop_vrt(reference, reference_cut_height, reference_vrt_path)
        reference_cut = rasterio.open(reference_vrt_path, sharing=False)

        try:
            with rasterio.open(output, "w", **profile) as dst:
                # 先写待校正影像，再写基准影像；重叠处以基准影像为准。
                for src, src_transform, initialize, use_tps in (
                    (moving_source, moving_transform, True, bool(moving_gcps)),
                    (reference_cut, reference.transform, False, False),
                ):
                    for band in range(1, reference.count + 1):
                        warp_args = dict(
                            source=rasterio.band(src, band),
                            destination=rasterio.band(dst, band),
                            src_nodata=moving.nodata if use_tps else src.nodata,
                            dst_transform=dst_transform,
                            dst_crs=reference.crs,
                            dst_nodata=nodata,
                            resampling=Resampling.bilinear,
                            init_dest_nodata=initialize,
                            num_threads=4,
                            warp_mem_limit=512,
                        )
                        if use_tps:
                            warp_args.update(MAX_GCP_ORDER=-1)
                        else:
                            warp_args.update(
                                src_transform=src_transform,
                                src_crs=src.crs,
                            )
                        reproject(**warp_args)

                # 在重叠带中线两侧渐变融合，消除一像元黑线和明显色阶跳变。
                if feather_pixels > 0:
                    seam_row = int(
                        round((dst_transform.f - seam_y) / abs(dst_transform.e))
                    )
                    row0 = max(0, seam_row - feather_pixels)
                    row1 = min(height, seam_row + feather_pixels)
                    blend_height = row1 - row0
                    blend_window = Window(0, row0, width, blend_height)
                    blend_transform = dst_transform * Affine.translation(0, row0)
                    south_weight = np.linspace(
                        0.0, 1.0, blend_height, dtype=np.float32
                    )[:, None]

                    for band in range(1, reference.count + 1):
                        north = np.full(
                            (blend_height, width), nodata,
                            dtype=reference.dtypes[band - 1],
                        )
                        south = np.full_like(north, nodata)
                        reproject(
                            source=rasterio.band(reference, band),
                            destination=north,
                            src_transform=reference.transform,
                            src_crs=reference.crs,
                            src_nodata=reference.nodata,
                            dst_transform=blend_transform,
                            dst_crs=reference.crs,
                            dst_nodata=nodata,
                            resampling=Resampling.bilinear,
                            num_threads=4,
                        )
                        south_args = dict(
                            source=rasterio.band(moving_source, band),
                            destination=south,
                            src_nodata=moving.nodata,
                            dst_transform=blend_transform,
                            dst_crs=reference.crs,
                            dst_nodata=nodata,
                            resampling=Resampling.bilinear,
                            num_threads=4,
                        )
                        if moving_gcps:
                            south_args.update(MAX_GCP_ORDER=-1)
                        else:
                            south_args.update(
                                src_transform=moving_transform,
                                src_crs=moving.crs,
                            )
                        reproject(**south_args)

                        north_valid = north != nodata
                        south_valid = south != nodata
                        mixed = (
                            north.astype(np.float32) * (1.0 - south_weight)
                            + south.astype(np.float32) * south_weight
                        )
                        mixed[~north_valid & south_valid] = south[
                            ~north_valid & south_valid
                        ]
                        mixed[north_valid & ~south_valid] = north[
                            north_valid & ~south_valid
                        ]
                        mixed[~north_valid & ~south_valid] = nodata
                        dst.write(
                            np.rint(mixed).astype(reference.dtypes[band - 1]),
                            band,
                            window=blend_window,
                        )
                dst.update_tags(
                    REGISTRATION=(
                        "overlap phase correlation; GCP thin plate spline"
                        if moving_gcps
                        else "overlap phase correlation; robust translation"
                    ),
                    MOVING_TRANSFORM=str(moving_transform),
                    SEAM="middle of overlap with linear feather",
                )
        finally:
            if moving_vrt is not None:
                moving_vrt.close()
            reference_cut.close()

    if build_overviews:
        with rasterio.open(output, "r+") as dst:
            factors = [2, 4, 8, 16, 32, 64]
            dst.build_overviews(factors, Resampling.average)
            dst.update_tags(ns="rio_overview", resampling="average")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "input",
        type=Path,
        help="包含两幅 tif/tiff 的目录，或第一幅 tif",
    )
    parser.add_argument("second", type=Path, nargs="?", help="第二幅 tif")
    parser.add_argument("-o", "--output", type=Path, default=Path("aligned_mosaic.tif"))
    parser.add_argument("--max-shift", type=float, default=30.0)
    parser.add_argument("--estimate-only", action="store_true")
    parser.add_argument(
        "--vector-only",
        action="store_true",
        help="只同步校正并拼接 SHP，不重新生成 TIFF",
    )
    parser.add_argument("--build-overviews", action="store_true")
    parser.add_argument(
        "--model",
        choices=("rubber", "translation"),
        default="rubber",
        help="rubber=沿接缝局部橡皮片校正（默认）；translation=仅整体平移",
    )
    parser.add_argument(
        "--feather",
        type=int,
        default=0,
        help="接缝单侧融合宽度（像元）；默认 0，先保证道路几何连续",
    )
    parser.add_argument(
        "--shp-dir",
        type=Path,
        help="同名 SHP 所在目录，例如 input_shp",
    )
    parser.add_argument(
        "--shp-output",
        type=Path,
        default=Path("output/aligned_mosaic.shp"),
        help="拼接后 SHP 输出路径",
    )
    args = parser.parse_args()
    if args.vector_only and args.shp_dir is None:
        parser.error("--vector-only 必须同时提供 --shp-dir。")

    if args.input.is_dir():
        files = sorted([*args.input.glob("*.tif"), *args.input.glob("*.tiff")])
    else:
        files = [args.input, args.second] if args.second else []
    if len(files) != 2 or any(p is None for p in files):
        parser.error("必须恰好提供两幅 tif，或一个恰好包含两幅 tif 的目录。")

    with rasterio.open(files[0], sharing=False) as first, rasterio.open(
        files[1], sharing=False
    ) as second:
        same_grid(first, second)

        # 本例上下拼接：北侧图作为几何基准，南侧图作为待校正影像。
        reference, moving = (
            (first, second)
            if first.bounds.top >= second.bounds.top
            else (second, first)
        )
        dx, dy, matches = estimate_shift(reference, moving, args.max_shift)
        corrected = moving.transform * Affine.translation(-dx, -dy)
        _, ref_row, _, moving_row, _, overlap_rows = overlap_geometry(
            reference, moving
        )
        ref_seam_row = ref_row + overlap_rows // 2
        moving_seam_row = moving_row + overlap_rows // 2
        local = np.asarray(matches)[:, 2:4]
        residual = np.linalg.norm(local - np.array([dx, dy]), axis=1)
        gcps = None
        columns = None
        if args.model == "rubber":
            gcps, columns = make_rubber_gcps(reference, moving, matches)

        print(f"基准影像: {Path(reference.name).name}")
        print(f"待校正影像: {Path(moving.name).name}")
        print(f"可靠匹配块: {len(matches)}")
        print(
            f"重叠高度: {overlap_rows} 行；拼接对应行: "
            f"reference={ref_seam_row}, moving={moving_seam_row}"
        )
        print(f"测得 moving 内容偏移: dx={dx:.3f}, dy={dy:.3f} 像元")
        print(f"平移校正后的局部残差 P90: {np.percentile(residual, 90):.3f} 像元")
        if columns:
            print(f"局部 TPS 控制列: {len(columns)}（共 {len(gcps)} 个 GCP）")
            print(
                "各列 dx 范围 "
                f"{min(v[1] for v in columns):.3f} .. "
                f"{max(v[1] for v in columns):.3f}，dy 范围 "
                f"{min(v[2] for v in columns):.3f} .. "
                f"{max(v[2] for v in columns):.3f} 像元"
            )
        print(
            "对 moving 地理定位的修正: "
            f"X={-dx * moving.res[0]:+.10f}, "
            f"Y={dy * abs(moving.res[1]):+.10f} 坐标单位"
        )
        print("修正后仿射变换: " + ", ".join(f"{v:.12g}" for v in corrected[:6]))

        if not args.estimate_only and not args.vector_only:
            mosaic(
                reference,
                moving,
                corrected,
                args.output,
                args.build_overviews,
                moving_gcps=gcps,
                feather_pixels=args.feather,
            )
            print(f"输出完成: {args.output.resolve()}")
        if not args.estimate_only and args.shp_dir is not None:
            if not columns:
                raise ValueError("同步 SHP 需要使用 --model rubber。")
            mosaic_shapefiles(
                reference,
                moving,
                columns,
                args.shp_dir,
                args.shp_output,
            )


if __name__ == "__main__":
    main()
