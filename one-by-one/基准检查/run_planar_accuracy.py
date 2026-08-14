#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""批量检测0.5米影像相对多幅TIF基准影像的平面配准偏移。

基准目录中的 ``J47E001014.tif``（也兼容 ``47E001014.tif``）会与待检目录中的
``47E001014_2025.tif`` 配对。主体检测仍从有效、纹理丰富区域采样，使用
SIFT + RANSAC 估计待检影像内容需要施加的平移。
"""

from __future__ import annotations

import os

# Windows 多进程下限制底层库内部线程，避免 workers × BLAS/OpenCV 线程过度抢占。
for _name in (
    "OMP_NUM_THREADS",
    "OPENBLAS_NUM_THREADS",
    "MKL_NUM_THREADS",
    "NUMEXPR_NUM_THREADS",
):
    os.environ.setdefault(_name, "1")

import argparse
import concurrent.futures
import hashlib
import json
import math
import re
import shutil
import sys
import time
import traceback
from collections import defaultdict
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Sequence

import cv2
import numpy as np
import pandas as pd
import rasterio
from pyproj import CRS as PyprojCRS
from rasterio.enums import Resampling
from rasterio.vrt import WarpedVRT
from rasterio.windows import Window
from rasterio.warp import transform as warp_transform

cv2.setNumThreads(1)


DEFAULT_REFERENCE = str(Path(__file__).resolve().parent / "reference")
DEFAULT_INPUT_ROOT = r"\\10.10.10.11\data\原始影像\0.5m影像"
DEFAULT_OUTPUT = (
    r"D:\private\lx\平面精度检测"
    if os.name == "nt"
    else str(Path(__file__).resolve().parent / "平面精度检测")
)
SCENE_RE = re.compile(
    r"^CQDOM(?P<county>\d{6})_(?P<scene>.+)_0\.5m\.tif$", re.IGNORECASE
)
GRID_TILE_RE = re.compile(r"^R_\d+_\d+_\d+_\d+$", re.IGNORECASE)
TARGET_YEAR_SUFFIX_RE = re.compile(r"_2025$", re.IGNORECASE)


@dataclass(frozen=True)
class Config:
    reference: str
    input_root: str
    output: str
    workers: int = 8
    max_candidates_per_scene: int = 10
    coarse_max_side: int = 640
    reference_resolution_m: float = 2.0
    cluster_radius_px: float = 5.0
    max_abs_shift_px: float = 250.0


def convert_network_path(path: Any) -> Any:
    """把 10.10.10.* 的 Windows 共享路径转换为 Linux 挂载路径。"""
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
    for index in range(1, 256):
        for share_name, linux_prefix in share_mapping:
            for windows_prefix in (
                f"//10.10.10.{index}/{share_name}",
                f"/10.10.10.{index}/{share_name}",
                f"10.10.10.{index}/{share_name}",
            ):
                if path == windows_prefix:
                    return linux_prefix
                if path.startswith(windows_prefix + "/"):
                    return linux_prefix + path[len(windows_prefix) :]
    return path


def get_ip_from_source_root(source_root: Any) -> str:
    if source_root is None:
        return ""
    match = re.search(r"10\.10\.10\.\d+", str(source_root).strip())
    return match.group(0) if match else ""


def convert_linux_path_to_network_path(path: Any, source_root: Any = "") -> Any:
    """根据 source_root 中的 IP 把 Linux 挂载路径还原为 Windows UNC 路径。"""
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
            relative_path = path[len(linux_prefix) :]
            return (windows_prefix + relative_path).replace("/", "\\")
    return path.replace("/", "\\")


def runtime_path(path: Any, source_root: Any = "") -> str:
    """按当前操作系统选择实际可访问的路径形式。"""
    if os.name == "nt":
        text = str(path)
        if text.replace("\\", "/").startswith("/media/cangling/"):
            return str(convert_linux_path_to_network_path(text, source_root))
        return text
    return str(convert_network_path(path))


def pairing_key(stem: str) -> str:
    """提取基准和待检文件共有的图号，如 J47E001014 -> 47E001014。"""
    key = TARGET_YEAR_SUFFIX_RE.sub("", stem.strip())
    if re.fullmatch(r"J\d{2}[A-Z]\d{6}", key, re.IGNORECASE):
        key = key[1:]
    return key.upper()


def dataset_resolution_m(dataset: Any) -> tuple[float, float]:
    """返回栅格横、纵方向的近似米分辨率，兼容投影坐标和经纬度坐标。"""
    resolution_x = abs(float(dataset.res[0]))
    resolution_y = abs(float(dataset.res[1]))
    if dataset.crs is None:
        return resolution_x, resolution_y

    crs = PyprojCRS.from_user_input(dataset.crs)
    if crs.is_geographic:
        center_x = (float(dataset.bounds.left) + float(dataset.bounds.right)) / 2.0
        center_y = (float(dataset.bounds.bottom) + float(dataset.bounds.top)) / 2.0
        geod = crs.get_geod()
        _, _, distance_x = geod.inv(
            center_x,
            center_y,
            center_x + resolution_x,
            center_y,
        )
        _, _, distance_y = geod.inv(
            center_x,
            center_y,
            center_x,
            center_y + resolution_y,
        )
        return abs(float(distance_x)), abs(float(distance_y))

    axis_info = crs.axis_info
    factor_x = (
        float(axis_info[0].unit_conversion_factor)
        if len(axis_info) >= 1 and axis_info[0].unit_conversion_factor
        else 1.0
    )
    factor_y = (
        float(axis_info[1].unit_conversion_factor)
        if len(axis_info) >= 2 and axis_info[1].unit_conversion_factor
        else factor_x
    )
    return resolution_x * factor_x, resolution_y * factor_y


def coordinates_in_crs(
    x: float,
    y: float,
    source_crs: Any,
    destination_crs: Any,
) -> tuple[float, float]:
    """把一个地图坐标转换到目标 CRS；相同 CRS 时原样返回。"""
    if (
        source_crs is None
        or destination_crs is None
        or source_crs == destination_crs
    ):
        return float(x), float(y)
    transformed_x, transformed_y = warp_transform(
        source_crs,
        destination_crs,
        [float(x)],
        [float(y)],
    )
    return float(transformed_x[0]), float(transformed_y[0])


def now_text() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def log(message: str) -> None:
    print(f"[{now_text()}] {message}", flush=True)


def safe_float(value: Any, default: float = math.nan) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return default
    return result if math.isfinite(result) else default


def weighted_median(values: Sequence[float], weights: Sequence[float]) -> float:
    values_array = np.asarray(values, dtype=np.float64)
    weights_array = np.asarray(weights, dtype=np.float64)
    order = np.argsort(values_array)
    values_array = values_array[order]
    weights_array = weights_array[order]
    total = weights_array.sum()
    if total <= 0:
        return float(np.median(values_array))
    cutoff = total / 2.0
    return float(values_array[np.searchsorted(np.cumsum(weights_array), cutoff)])


def scene_parts(scene_key: str) -> tuple[str, str]:
    if GRID_TILE_RE.fullmatch(scene_key):
        return "20260509", "SV0.5m镶嵌"
    pieces = scene_key.split("_")
    date = pieces[0] if pieces and re.fullmatch(r"\d{8}", pieces[0]) else ""
    sensor = pieces[-1] if pieces else ""
    return date, sensor


def parse_tif(path: Path) -> dict[str, str]:
    match = SCENE_RE.match(path.name)
    if match:
        scene_key = match.group("scene")
        county_code = match.group("county")
    else:
        scene_key = path.stem
        county_code = ""
    date, sensor = scene_parts(scene_key)
    county_dir = path.parent.name
    if GRID_TILE_RE.fullmatch(scene_key):
        county_name = "0.5m网格影像"
    else:
        county_name = (
            county_dir[len(county_code) :]
            if county_code and county_dir.startswith(county_code)
            else county_dir
        )
    return {
        "scene_key": scene_key,
        "county_code": county_code,
        "county_name": county_name,
        "county_dir": county_dir,
        "date": date,
        "sensor": sensor,
    }


def normalize_gray(gray: np.ndarray, valid: np.ndarray) -> np.ndarray:
    values = gray[valid]
    if values.size < 256:
        return np.zeros(gray.shape, dtype=np.uint8)
    low, high = np.percentile(values, (2.0, 98.0))
    if high <= low + 1e-6:
        high = low + 1.0
    normalized = np.clip((gray - low) * 255.0 / (high - low), 0, 255).astype(
        np.uint8
    )
    return cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8)).apply(normalized)


def reference_gray(array: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    data = array.astype(np.float32)
    valid = np.any(data > 0, axis=0)
    if data.shape[0] >= 3:
        gray = 0.299 * data[0] + 0.587 * data[1] + 0.114 * data[2]
    else:
        gray = data.mean(axis=0)
    return normalize_gray(gray, valid), valid


def target_gray(array: np.ndarray, total_band_count: int) -> tuple[np.ndarray, np.ndarray]:
    data = array.astype(np.float32)
    valid = np.any(data > 0, axis=0)
    if total_band_count >= 8 and data.shape[0] >= 4:
        # ZY1E/ZY1F 等 8 波段数据用前四波段均值，实测跨时相匹配更稳定。
        gray = data[:4].mean(axis=0)
    elif data.shape[0] >= 3:
        gray = 0.299 * data[0] + 0.587 * data[1] + 0.114 * data[2]
    else:
        gray = data.mean(axis=0)
    return normalize_gray(gray, valid), valid


def local_box_mean(array: np.ndarray, width: int, height: int) -> np.ndarray:
    width = max(1, int(width))
    height = max(1, int(height))
    if width % 2 == 0:
        width += 1
    if height % 2 == 0:
        height += 1
    return cv2.boxFilter(
        array.astype(np.float32),
        ddepth=-1,
        ksize=(width, height),
        normalize=True,
        borderType=cv2.BORDER_CONSTANT,
    )


def suppress_and_pick(
    score: np.ndarray,
    count: int,
    radius_x: int,
    radius_y: int,
) -> list[tuple[int, int, float]]:
    work = score.copy()
    selected: list[tuple[int, int, float]] = []
    for _ in range(count):
        flat_index = int(np.argmax(work))
        value = float(work.flat[flat_index])
        if not math.isfinite(value) or value <= 0:
            break
        row, col = np.unravel_index(flat_index, work.shape)
        selected.append((int(row), int(col), value))
        y0 = max(0, row - radius_y)
        y1 = min(work.shape[0], row + radius_y + 1)
        x0 = max(0, col - radius_x)
        x1 = min(work.shape[1], col + radius_x + 1)
        work[y0:y1, x0:x1] = 0
    return selected


def inspect_file_and_candidates(
    path_text: str,
    coarse_max_side: int,
    reference_resolution_m: float,
    reference_crs: Any,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    path = Path(path_text)
    parsed = parse_tif(path)
    metadata: dict[str, Any] = {
        "path": str(path),
        "file_name": path.name,
        **parsed,
        "inspect_error": "",
    }
    candidates: list[dict[str, Any]] = []
    try:
        with rasterio.open(path) as source:
            source_resolution_x_m, source_resolution_y_m = dataset_resolution_m(source)
            scale = max(source.width, source.height) / float(coarse_max_side)
            scale = max(1.0, scale)
            coarse_width = max(32, int(round(source.width / scale)))
            coarse_height = max(32, int(round(source.height / scale)))
            read_count = min(source.count, 4 if source.count >= 8 else 3)
            indexes = list(range(1, read_count + 1))
            coarse = source.read(
                indexes,
                out_shape=(read_count, coarse_height, coarse_width),
                resampling=Resampling.average,
            )
            gray, valid = target_gray(coarse, source.count)
            valid_fraction = float(valid.mean())
            pixel_area = source_resolution_x_m * source_resolution_y_m
            valid_area_km2 = (
                valid_fraction * source.width * source.height * pixel_area / 1_000_000.0
            )
            metadata.update(
                {
                    "width": int(source.width),
                    "height": int(source.height),
                    "band_count": int(source.count),
                    "resolution_x_m": source_resolution_x_m,
                    "resolution_y_m": source_resolution_y_m,
                    "resolution_x_native": float(abs(source.res[0])),
                    "resolution_y_native": float(abs(source.res[1])),
                    "resolution_native_unit": (
                        "degree"
                        if source.crs is not None
                        and PyprojCRS.from_user_input(source.crs).is_geographic
                        else "projected"
                    ),
                    "valid_fraction_est": valid_fraction,
                    "valid_area_km2_est": valid_area_km2,
                    "file_size_bytes": int(path.stat().st_size),
                    "crs": str(source.crs),
                }
            )
            if valid.sum() < 16:
                return metadata, candidates

            gradient_x = cv2.Sobel(gray, cv2.CV_32F, 1, 0, ksize=3)
            gradient_y = cv2.Sobel(gray, cv2.CV_32F, 0, 1, ksize=3)
            gradient = cv2.magnitude(gradient_x, gradient_y)
            valid_float = valid.astype(np.float32)

            for patch_size in (768, 512):
                patch_width_source = (
                    patch_size * reference_resolution_m / source_resolution_x_m
                )
                patch_height_source = (
                    patch_size * reference_resolution_m / source_resolution_y_m
                )
                kernel_width = max(
                    3, int(round(patch_width_source * coarse_width / source.width))
                )
                kernel_height = max(
                    3, int(round(patch_height_source * coarse_height / source.height))
                )
                local_valid = local_box_mean(
                    valid_float, kernel_width, kernel_height
                )
                local_gradient_sum = local_box_mean(
                    gradient * valid_float, kernel_width, kernel_height
                )
                local_texture = local_gradient_sum / np.maximum(local_valid, 0.05)
                minimum_valid = 0.30 if patch_size == 768 else 0.22
                score = (
                    local_valid
                    * np.log1p(np.maximum(local_texture, 0))
                    * valid_float
                )
                score[local_valid < minimum_valid] = 0
                picks = suppress_and_pick(
                    score,
                    count=2,
                    radius_x=max(6, kernel_width // 2),
                    radius_y=max(6, kernel_height // 2),
                )
                for coarse_row, coarse_col, score_value in picks:
                    source_row = min(
                        source.height - 1,
                        (coarse_row + 0.5) * source.height / coarse_height,
                    )
                    source_col = min(
                        source.width - 1,
                        (coarse_col + 0.5) * source.width / coarse_width,
                    )
                    source_x, source_y = source.xy(source_row, source_col)
                    map_x, map_y = coordinates_in_crs(
                        source_x,
                        source_y,
                        source.crs,
                        reference_crs,
                    )
                    candidates.append(
                        {
                            "file_path": str(path),
                            "file_name": path.name,
                            "county_code": parsed["county_code"],
                            "county_name": parsed["county_name"],
                            "map_x": float(map_x),
                            "map_y": float(map_y),
                            "patch_size": int(patch_size),
                            "candidate_score": float(score_value),
                            "local_valid_fraction_est": float(
                                local_valid[coarse_row, coarse_col]
                            ),
                            "local_texture_est": float(
                                local_texture[coarse_row, coarse_col]
                            ),
                            "file_valid_area_km2_est": valid_area_km2,
                        }
                    )

            if not candidates:
                # 极小碎片回退：仍保留一个低覆盖候选，结果会被标为低置信度。
                patch_size = 512
                patch_width_source = (
                    patch_size * reference_resolution_m / source_resolution_x_m
                )
                patch_height_source = (
                    patch_size * reference_resolution_m / source_resolution_y_m
                )
                kernel_width = max(
                    3, int(round(patch_width_source * coarse_width / source.width))
                )
                kernel_height = max(
                    3, int(round(patch_height_source * coarse_height / source.height))
                )
                local_valid = local_box_mean(
                    valid_float, kernel_width, kernel_height
                )
                fallback_score = local_valid * np.log1p(gradient) * valid_float
                fallback = suppress_and_pick(
                    fallback_score,
                    count=1,
                    radius_x=max(4, kernel_width // 2),
                    radius_y=max(4, kernel_height // 2),
                )
                for coarse_row, coarse_col, score_value in fallback:
                    source_row = min(
                        source.height - 1,
                        (coarse_row + 0.5) * source.height / coarse_height,
                    )
                    source_col = min(
                        source.width - 1,
                        (coarse_col + 0.5) * source.width / coarse_width,
                    )
                    source_x, source_y = source.xy(source_row, source_col)
                    map_x, map_y = coordinates_in_crs(
                        source_x,
                        source_y,
                        source.crs,
                        reference_crs,
                    )
                    candidates.append(
                        {
                            "file_path": str(path),
                            "file_name": path.name,
                            "county_code": parsed["county_code"],
                            "county_name": parsed["county_name"],
                            "map_x": float(map_x),
                            "map_y": float(map_y),
                            "patch_size": patch_size,
                            "candidate_score": float(score_value) * 0.5,
                            "local_valid_fraction_est": float(
                                local_valid[coarse_row, coarse_col]
                            ),
                            "local_texture_est": float(gradient[coarse_row, coarse_col]),
                            "file_valid_area_km2_est": valid_area_km2,
                        }
                    )
    except Exception as exc:
        metadata["inspect_error"] = f"{type(exc).__name__}: {exc}"
    metadata["candidate_count"] = len(candidates)
    return metadata, candidates


def candidate_distance(a: dict[str, Any], b: dict[str, Any]) -> float:
    return math.hypot(a["map_x"] - b["map_x"], a["map_y"] - b["map_y"])


def choose_scene_candidates(
    all_candidates: list[dict[str, Any]], max_candidates: int
) -> list[dict[str, Any]]:
    if not all_candidates:
        return []
    for candidate in all_candidates:
        candidate["selection_quality"] = float(
            candidate["candidate_score"]
            * (0.8 + 0.2 * min(1.0, candidate["file_valid_area_km2_est"] / 5.0))
        )

    by_file: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for candidate in all_candidates:
        by_file[candidate["file_path"]].append(candidate)
    for values in by_file.values():
        values.sort(key=lambda item: item["selection_quality"], reverse=True)

    file_order = sorted(
        by_file,
        key=lambda path: max(
            item["file_valid_area_km2_est"] for item in by_file[path]
        ),
        reverse=True,
    )
    selected: list[dict[str, Any]] = []

    def add_if_spatially_new(candidate: dict[str, Any], minimum_distance: float) -> bool:
        if any(candidate_distance(candidate, old) < minimum_distance for old in selected):
            return False
        selected.append(candidate)
        return True

    # 先从多个区县切片各取一个点，防止只检查场景局部。
    for file_path in file_order[: min(8, len(file_order))]:
        for candidate in by_file[file_path]:
            if add_if_spatially_new(candidate, minimum_distance=500.0):
                break
        if len(selected) >= max_candidates:
            return selected

    remaining = sorted(
        all_candidates, key=lambda item: item["selection_quality"], reverse=True
    )
    while remaining and len(selected) < max_candidates:
        best_index = -1
        best_value = -math.inf
        for index, candidate in enumerate(remaining):
            if selected:
                min_distance = min(
                    candidate_distance(candidate, old) for old in selected
                )
            else:
                min_distance = 10_000.0
            if min_distance < 400.0:
                continue
            spread_bonus = 1.0 + min(1.5, min_distance / 4_000.0)
            value = candidate["selection_quality"] * spread_bonus
            if value > best_value:
                best_value = value
                best_index = index
        if best_index < 0:
            break
        selected.append(remaining.pop(best_index))
    return selected


def estimate_patch_shift(
    reference_array: np.ndarray,
    target_array: np.ndarray,
    total_target_bands: int,
    maximum_abs_shift_px: float,
) -> dict[str, Any]:
    reference_image, reference_valid = reference_gray(reference_array)
    target_image, target_valid = target_gray(target_array, total_target_bands)
    overlap = reference_valid & target_valid
    overlap_fraction = float(overlap.mean())
    base: dict[str, Any] = {
        "match_ok": False,
        "match_reason": "",
        "overlap_fraction": overlap_fraction,
    }
    if overlap_fraction < 0.22:
        base["match_reason"] = "有效重叠不足"
        return base

    mask = cv2.erode(
        overlap.astype(np.uint8) * 255,
        np.ones((7, 7), dtype=np.uint8),
        iterations=1,
    )
    valid_pixels = mask > 0
    if valid_pixels.sum() < 2_000:
        base["match_reason"] = "有效像素不足"
        return base

    reference_texture = float(
        cv2.Laplacian(reference_image, cv2.CV_32F)[valid_pixels].std()
    )
    target_texture = float(
        cv2.Laplacian(target_image, cv2.CV_32F)[valid_pixels].std()
    )
    base.update(
        {
            "reference_texture": reference_texture,
            "target_texture": target_texture,
        }
    )
    if min(reference_texture, target_texture) < 5.0:
        base["match_reason"] = "纹理不足"
        return base

    patch_size = reference_image.shape[0]
    feature_limit = 5_000 if patch_size >= 700 else 3_500
    sift = cv2.SIFT_create(
        nfeatures=feature_limit,
        nOctaveLayers=3,
        contrastThreshold=0.012,
        edgeThreshold=15,
        sigma=1.6,
    )
    reference_keypoints, reference_descriptors = sift.detectAndCompute(
        reference_image, mask
    )
    target_keypoints, target_descriptors = sift.detectAndCompute(target_image, mask)
    base.update(
        {
            "reference_keypoints": len(reference_keypoints),
            "target_keypoints": len(target_keypoints),
        }
    )
    if (
        reference_descriptors is None
        or target_descriptors is None
        or len(reference_keypoints) < 10
        or len(target_keypoints) < 10
    ):
        base["match_reason"] = "特征点不足"
        return base

    matcher = cv2.BFMatcher(cv2.NORM_L2)
    knn_matches = matcher.knnMatch(target_descriptors, reference_descriptors, k=2)
    good_matches = [
        first
        for first, second in knn_matches
        if first.distance < 0.78 * second.distance
    ]
    base["ratio_test_matches"] = len(good_matches)
    if len(good_matches) < 6:
        base["match_reason"] = "候选匹配不足"
        return base

    target_points = np.float32(
        [target_keypoints[match.queryIdx].pt for match in good_matches]
    )
    reference_points = np.float32(
        [reference_keypoints[match.trainIdx].pt for match in good_matches]
    )
    affine, inlier_mask = cv2.estimateAffinePartial2D(
        target_points,
        reference_points,
        method=cv2.RANSAC,
        ransacReprojThreshold=3.5,
        maxIters=10_000,
        confidence=0.999,
        refineIters=20,
    )
    if affine is None or inlier_mask is None:
        base["match_reason"] = "RANSAC失败"
        return base

    inliers = inlier_mask.ravel().astype(bool)
    inlier_count = int(inliers.sum())
    differences = reference_points[inliers] - target_points[inliers]
    if inlier_count:
        shift_col_px = float(np.median(differences[:, 0]))
        shift_row_px = float(np.median(differences[:, 1]))
        residuals = np.hypot(
            differences[:, 0] - shift_col_px,
            differences[:, 1] - shift_row_px,
        )
        residual_median_px = float(np.median(residuals))
        source_inliers = target_points[inliers]
        span_x_px = float(
            np.percentile(source_inliers[:, 0], 90)
            - np.percentile(source_inliers[:, 0], 10)
        )
        span_y_px = float(
            np.percentile(source_inliers[:, 1], 90)
            - np.percentile(source_inliers[:, 1], 10)
        )
    else:
        shift_col_px = shift_row_px = residual_median_px = math.nan
        span_x_px = span_y_px = 0.0

    affine_a = float(affine[0, 0])
    affine_b = float(affine[0, 1])
    scale = math.hypot(affine_a, affine_b)
    rotation_deg = math.degrees(math.atan2(affine_b, affine_a))
    inlier_ratio = inlier_count / max(1, len(good_matches))
    span_max_px = max(span_x_px, span_y_px)
    geometry_ok = (
        inlier_count >= 6
        and (inlier_ratio >= 0.08 or inlier_count >= 15)
        and residual_median_px <= 4.0
        and abs(scale - 1.0) <= 0.025
        and abs(rotation_deg) <= 1.2
        and abs(shift_col_px) <= maximum_abs_shift_px
        and abs(shift_row_px) <= maximum_abs_shift_px
        and span_max_px >= 55.0
    )
    base.update(
        {
            "match_ok": bool(geometry_ok),
            "match_reason": "通过" if geometry_ok else "匹配几何质量不足",
            "inlier_count": inlier_count,
            "inlier_ratio": float(inlier_ratio),
            "shift_col_px_ref": shift_col_px,
            "shift_row_px_ref": shift_row_px,
            "shift_magnitude_px_ref": float(
                math.hypot(shift_col_px, shift_row_px)
            ),
            "residual_median_px": residual_median_px,
            "scale": float(scale),
            "rotation_deg": float(rotation_deg),
            "inlier_span_x_px": span_x_px,
            "inlier_span_y_px": span_y_px,
        }
    )
    return base


def cluster_patch_results(
    patch_results: list[dict[str, Any]],
    cluster_radius_px: float,
    reference_resolution_m: float,
) -> dict[str, Any]:
    accepted = [row for row in patch_results if row.get("match_ok")]
    for row in patch_results:
        row["used_in_scene_result"] = False
    if not accepted:
        return {
            "determined": False,
            "confidence": "无法判定",
            "reason": "没有通过几何质量检查的匹配窗口",
        }

    points = np.asarray(
        [
            [row["shift_col_px_ref"], row["shift_row_px_ref"]]
            for row in accepted
        ],
        dtype=np.float64,
    )
    weights = np.asarray(
        [
            max(1.0, row["inlier_count"])
            * max(0.08, row["inlier_ratio"])
            / (1.0 + row["residual_median_px"]) ** 2
            for row in accepted
        ],
        dtype=np.float64,
    )
    distances = np.linalg.norm(points[:, None, :] - points[None, :, :], axis=2)
    neighborhood_scores = (
        (distances <= cluster_radius_px).astype(np.float64) * weights[None, :]
    ).sum(axis=1)
    seed_index = int(np.argmax(neighborhood_scores))
    cluster_mask = distances[seed_index] <= cluster_radius_px
    cluster_indexes = np.where(cluster_mask)[0]

    center_col = weighted_median(
        points[cluster_indexes, 0], weights[cluster_indexes]
    )
    center_row = weighted_median(
        points[cluster_indexes, 1], weights[cluster_indexes]
    )
    refined_distance = np.hypot(
        points[:, 0] - center_col, points[:, 1] - center_row
    )
    cluster_indexes = np.where(refined_distance <= cluster_radius_px)[0]
    center_col = weighted_median(
        points[cluster_indexes, 0], weights[cluster_indexes]
    )
    center_row = weighted_median(
        points[cluster_indexes, 1], weights[cluster_indexes]
    )
    cluster_distance = np.hypot(
        points[cluster_indexes, 0] - center_col,
        points[cluster_indexes, 1] - center_row,
    )
    cluster_rows = [accepted[index] for index in cluster_indexes]
    for row in cluster_rows:
        row["used_in_scene_result"] = True

    cluster_count = len(cluster_rows)
    unique_files = len({row["file_path"] for row in cluster_rows})
    total_inliers = int(sum(row["inlier_count"] for row in cluster_rows))
    spread_median_px = float(np.median(cluster_distance))
    spread_p90_px = float(np.percentile(cluster_distance, 90))

    if (
        cluster_count >= 5
        and unique_files >= 2
        and total_inliers >= 45
        and spread_p90_px <= 3.0
    ):
        confidence = "高"
    elif (
        cluster_count >= 3
        and total_inliers >= 20
        and spread_p90_px <= 4.5
    ):
        confidence = "中"
    elif cluster_count >= 2 and total_inliers >= 12:
        confidence = "低"
    else:
        confidence = "低（单点）"

    shift_east_m = center_col * reference_resolution_m
    shift_north_m = -center_row * reference_resolution_m
    magnitude_px = math.hypot(center_col, center_row)
    magnitude_m = magnitude_px * reference_resolution_m
    return {
        "determined": True,
        "confidence": confidence,
        "reason": "",
        "shift_col_px_ref": float(center_col),
        "shift_row_px_ref": float(center_row),
        "shift_east_m": float(shift_east_m),
        "shift_north_m": float(shift_north_m),
        "shift_magnitude_px_ref": float(magnitude_px),
        "shift_magnitude_m": float(magnitude_m),
        "accepted_patch_count": len(accepted),
        "cluster_patch_count": cluster_count,
        "cluster_unique_file_count": unique_files,
        "cluster_total_inliers": total_inliers,
        "cluster_spread_median_px": spread_median_px,
        "cluster_spread_p90_px": spread_p90_px,
    }


def screening_class(magnitude_m: float, determined: bool) -> str:
    if not determined or not math.isfinite(magnitude_m):
        return "无法判定"
    if magnitude_m <= 1.0:
        return "未见明显偏移（≤1m）"
    if magnitude_m <= 2.0:
        return "轻微偏移（1-2m）"
    return "严重偏移（>2m）"


def correction_direction(east_m: float, north_m: float) -> str:
    if not (math.isfinite(east_m) and math.isfinite(north_m)):
        return ""
    horizontal = (
        f"向东{abs(east_m):.1f}m"
        if east_m > 0
        else f"向西{abs(east_m):.1f}m"
        if east_m < 0
        else "东西向0m"
    )
    vertical = (
        f"向北{abs(north_m):.1f}m"
        if north_m > 0
        else f"向南{abs(north_m):.1f}m"
        if north_m < 0
        else "南北向0m"
    )
    return f"{horizontal}，{vertical}"


def process_scene(
    scene_key: str,
    path_texts: list[str],
    reference_path_text: str,
    config_dict: dict[str, Any],
) -> dict[str, Any]:
    config = Config(**config_dict)
    cv2.setNumThreads(1)
    date, sensor = scene_parts(scene_key)
    started = time.time()
    result: dict[str, Any] = {
        "scene_key": scene_key,
        "date": date,
        "sensor": sensor,
        "file_count": len(path_texts),
        "reference_path": reference_path_text,
        "files": [],
        "patches": [],
        "process_error": "",
    }
    try:
        with rasterio.open(reference_path_text) as reference_info:
            reference_crs = reference_info.crs
            reference_resolution_x_m, reference_resolution_y_m = (
                dataset_resolution_m(reference_info)
            )
        reference_resolution_m = (
            reference_resolution_x_m + reference_resolution_y_m
        ) / 2.0
        result["reference_resolution_m"] = reference_resolution_m
        result["reference_crs"] = str(reference_crs)

        all_candidates: list[dict[str, Any]] = []
        for path_text in path_texts:
            metadata, candidates = inspect_file_and_candidates(
                path_text,
                coarse_max_side=config.coarse_max_side,
                reference_resolution_m=reference_resolution_m,
                reference_crs=reference_crs,
            )
            result["files"].append(metadata)
            all_candidates.extend(candidates)
        selected_candidates = choose_scene_candidates(
            all_candidates, config.max_candidates_per_scene
        )
        result["candidate_count_total"] = len(all_candidates)
        result["candidate_count_selected"] = len(selected_candidates)

        candidates_by_file: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for candidate in selected_candidates:
            candidates_by_file[candidate["file_path"]].append(candidate)

        with rasterio.Env(GDAL_CACHEMAX=256):
            with rasterio.open(reference_path_text) as reference:
                for file_path, candidates in candidates_by_file.items():
                    try:
                        with rasterio.open(file_path) as source:
                            read_count = min(
                                source.count, 4 if source.count >= 8 else 3
                            )
                            indexes = list(range(1, read_count + 1))
                            with WarpedVRT(
                                source,
                                crs=reference.crs,
                                transform=reference.transform,
                                width=reference.width,
                                height=reference.height,
                                resampling=Resampling.bilinear,
                                src_nodata=0,
                                nodata=0,
                            ) as warped:
                                for candidate_index, candidate in enumerate(candidates):
                                    patch_row: dict[str, Any] = {
                                        **candidate,
                                        "candidate_index_in_file": candidate_index,
                                        "match_ok": False,
                                        "match_reason": "",
                                    }
                                    try:
                                        center_row, center_col = reference.index(
                                            candidate["map_x"], candidate["map_y"]
                                        )
                                        patch_size = int(candidate["patch_size"])
                                        half = patch_size // 2
                                        window = Window(
                                            int(center_col - half),
                                            int(center_row - half),
                                            patch_size,
                                            patch_size,
                                        )
                                        if (
                                            window.col_off < 0
                                            or window.row_off < 0
                                            or window.col_off + window.width
                                            > reference.width
                                            or window.row_off + window.height
                                            > reference.height
                                        ):
                                            patch_row["match_reason"] = "窗口超出参考影像"
                                        else:
                                            reference_array = reference.read(
                                                [1, 2, 3], window=window
                                            )
                                            target_array = warped.read(
                                                indexes, window=window
                                            )
                                            patch_row.update(
                                                estimate_patch_shift(
                                                    reference_array,
                                                    target_array,
                                                    source.count,
                                                    config.max_abs_shift_px,
                                                )
                                            )
                                            patch_row["reference_center_col"] = int(
                                                center_col
                                            )
                                            patch_row["reference_center_row"] = int(
                                                center_row
                                            )
                                    except Exception as exc:
                                        patch_row["match_reason"] = (
                                            f"{type(exc).__name__}: {exc}"
                                        )
                                    result["patches"].append(patch_row)
                    except Exception as exc:
                        result["patches"].append(
                            {
                                "file_path": file_path,
                                "file_name": Path(file_path).name,
                                "match_ok": False,
                                "match_reason": f"打开影像失败: {type(exc).__name__}: {exc}",
                            }
                        )

        aggregation = cluster_patch_results(
            result["patches"],
            cluster_radius_px=config.cluster_radius_px,
            reference_resolution_m=reference_resolution_m,
        )
        result.update(aggregation)
        result["screening_class"] = screening_class(
            safe_float(result.get("shift_magnitude_m")),
            bool(result.get("determined")),
        )
        if result.get("confidence") == "低（单点）":
            result["screening_class"] = "单点疑似偏移（需人工复核）"
        result["correction_direction"] = correction_direction(
            safe_float(result.get("shift_east_m")),
            safe_float(result.get("shift_north_m")),
        )
    except Exception as exc:
        result.update(
            {
                "determined": False,
                "confidence": "无法判定",
                "reason": f"{type(exc).__name__}: {exc}",
                "screening_class": "无法判定",
                "process_error": traceback.format_exc(),
            }
        )
    result["seconds"] = float(time.time() - started)
    return result


def checkpoint_path(checkpoint_dir: Path, scene_key: str) -> Path:
    digest = hashlib.sha1(scene_key.encode("utf-8")).hexdigest()[:12]
    safe_prefix = re.sub(r'[<>:"/\\|?*]+', "_", scene_key)[:70]
    return checkpoint_dir / f"{safe_prefix}_{digest}.json"


def write_json_atomic(path: Path, data: Any) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(data, ensure_ascii=False, indent=2, allow_nan=True),
        encoding="utf-8",
    )
    temporary.replace(path)


def flatten_scene_row(result: dict[str, Any]) -> dict[str, Any]:
    return {
        "场景标识": result.get("scene_key", ""),
        "基准影像路径": result.get("reference_path", ""),
        "成像日期": result.get("date", ""),
        "传感器": result.get("sensor", ""),
        "关联影像数": result.get("file_count", 0),
        "候选窗口数": result.get("candidate_count_selected", 0),
        "通过匹配窗口数": result.get("accepted_patch_count", 0),
        "用于汇总窗口数": result.get("cluster_patch_count", 0),
        "用于汇总区县切片数": result.get("cluster_unique_file_count", 0),
        "置信度": result.get("confidence", "无法判定"),
        "筛查分级": result.get("screening_class", "无法判定"),
        "校正列偏移_参考像素": result.get("shift_col_px_ref", math.nan),
        "校正行偏移_参考像素": result.get("shift_row_px_ref", math.nan),
        "合成偏移_参考像素": result.get("shift_magnitude_px_ref", math.nan),
        "建议东移_m": result.get("shift_east_m", math.nan),
        "建议北移_m": result.get("shift_north_m", math.nan),
        "合成偏移_m": result.get("shift_magnitude_m", math.nan),
        "建议校正方向": result.get("correction_direction", ""),
        "窗口离散中位数_像素": result.get(
            "cluster_spread_median_px", math.nan
        ),
        "窗口离散P90_像素": result.get("cluster_spread_p90_px", math.nan),
        "汇总内点数": result.get("cluster_total_inliers", 0),
        "无法判定原因": result.get("reason", ""),
        "处理耗时_秒": result.get("seconds", math.nan),
        "处理异常": result.get("process_error", ""),
    }


def flatten_image_rows(result: dict[str, Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    east_m = safe_float(result.get("shift_east_m"))
    north_m = safe_float(result.get("shift_north_m"))
    magnitude_m = safe_float(result.get("shift_magnitude_m"))
    for file_row in result.get("files", []):
        resolution_x = safe_float(file_row.get("resolution_x_m"))
        resolution_y = safe_float(file_row.get("resolution_y_m"))
        native_col = (
            east_m / resolution_x
            if math.isfinite(east_m) and resolution_x > 0
            else math.nan
        )
        # 栅格行号向下为正；北移为正，因此本影像行偏移取相反号。
        native_row = (
            -north_m / resolution_y
            if math.isfinite(north_m) and resolution_y > 0
            else math.nan
        )
        native_magnitude = (
            math.hypot(native_col, native_row)
            if math.isfinite(native_col) and math.isfinite(native_row)
            else math.nan
        )
        rows.append(
            {
                "区县代码": file_row.get("county_code", ""),
                "区县名称": file_row.get("county_name", ""),
                "场景标识": result.get("scene_key", ""),
                "基准影像路径": result.get("reference_path", ""),
                "成像日期": result.get("date", ""),
                "传感器": result.get("sensor", ""),
                "文件名": file_row.get("file_name", ""),
                "完整路径": file_row.get("path", ""),
                "宽度_像素": file_row.get("width", math.nan),
                "高度_像素": file_row.get("height", math.nan),
                "波段数": file_row.get("band_count", math.nan),
                "分辨率X_m": resolution_x,
                "分辨率Y_m": resolution_y,
                "估算有效比例": file_row.get("valid_fraction_est", math.nan),
                "估算有效面积_km2": file_row.get(
                    "valid_area_km2_est", math.nan
                ),
                "置信度": result.get("confidence", "无法判定"),
                "筛查分级": result.get("screening_class", "无法判定"),
                "校正列偏移_参考像素": result.get(
                    "shift_col_px_ref", math.nan
                ),
                "校正行偏移_参考像素": result.get(
                    "shift_row_px_ref", math.nan
                ),
                "合成偏移_参考像素": result.get(
                    "shift_magnitude_px_ref", math.nan
                ),
                "校正列偏移_本影像像素": native_col,
                "校正行偏移_本影像像素": native_row,
                "合成偏移_本影像像素": native_magnitude,
                "建议东移_m": east_m,
                "建议北移_m": north_m,
                "合成偏移_m": magnitude_m,
                "建议校正方向": result.get("correction_direction", ""),
                "无法判定原因": result.get("reason", ""),
                "文件检查异常": file_row.get("inspect_error", ""),
            }
        )
    return rows


def flatten_patch_rows(result: dict[str, Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    reference_resolution_m = safe_float(
        result.get("reference_resolution_m"), 2.0
    )
    for index, patch in enumerate(result.get("patches", []), start=1):
        col = safe_float(patch.get("shift_col_px_ref"))
        row = safe_float(patch.get("shift_row_px_ref"))
        rows.append(
            {
                "场景标识": result.get("scene_key", ""),
                "基准影像路径": result.get("reference_path", ""),
                "成像日期": result.get("date", ""),
                "传感器": result.get("sensor", ""),
                "窗口序号": index,
                "区县代码": patch.get("county_code", ""),
                "区县名称": patch.get("county_name", ""),
                "文件名": patch.get("file_name", ""),
                "完整路径": patch.get("file_path", ""),
                "中心X": patch.get("map_x", math.nan),
                "中心Y": patch.get("map_y", math.nan),
                "窗口尺寸_参考像素": patch.get("patch_size", math.nan),
                "估算有效比例": patch.get(
                    "local_valid_fraction_est", math.nan
                ),
                "实际有效重叠比例": patch.get("overlap_fraction", math.nan),
                "匹配通过": bool(patch.get("match_ok", False)),
                "用于场景汇总": bool(
                    patch.get("used_in_scene_result", False)
                ),
                "匹配说明": patch.get("match_reason", ""),
                "校正列偏移_参考像素": col,
                "校正行偏移_参考像素": row,
                "合成偏移_参考像素": (
                    math.hypot(col, row)
                    if math.isfinite(col) and math.isfinite(row)
                    else math.nan
                ),
                "建议东移_m": (
                    col * reference_resolution_m
                    if math.isfinite(col)
                    else math.nan
                ),
                "建议北移_m": (
                    -row * reference_resolution_m
                    if math.isfinite(row)
                    else math.nan
                ),
                "候选匹配数": patch.get("ratio_test_matches", 0),
                "RANSAC内点数": patch.get("inlier_count", 0),
                "内点比例": patch.get("inlier_ratio", math.nan),
                "残差中位数_像素": patch.get(
                    "residual_median_px", math.nan
                ),
                "尺度": patch.get("scale", math.nan),
                "旋转角_度": patch.get("rotation_deg", math.nan),
                "参考特征点数": patch.get("reference_keypoints", 0),
                "待检特征点数": patch.get("target_keypoints", 0),
            }
        )
    return rows


def build_sensor_summary(scene_df: pd.DataFrame) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    for sensor, group in scene_df.groupby("传感器", dropna=False):
        determined = group[group["置信度"] != "无法判定"]
        reliable = determined[determined["置信度"].isin(["高", "中"])]
        values = reliable["合成偏移_m"].dropna()
        rows.append(
            {
                "传感器": sensor,
                "场景总数": len(group),
                "可判定场景数": len(determined),
                "高或中置信场景数": len(reliable),
                "偏移中位数_m": values.median() if len(values) else math.nan,
                "偏移P90_m": values.quantile(0.9) if len(values) else math.nan,
                "最大偏移_m": values.max() if len(values) else math.nan,
                "大于1m场景数": int((values > 1).sum()),
                "大于2m场景数": int((values > 2).sum()),
            }
        )
    return pd.DataFrame(rows).sort_values("场景总数", ascending=False)


def build_county_summary(image_df: pd.DataFrame) -> pd.DataFrame:
    county_scene = image_df.drop_duplicates(["区县代码", "场景标识"])
    rows: list[dict[str, Any]] = []
    for (code, name), group in county_scene.groupby(
        ["区县代码", "区县名称"], dropna=False
    ):
        determined = group[group["置信度"] != "无法判定"]
        reliable = determined[determined["置信度"].isin(["高", "中"])]
        values = reliable["合成偏移_m"].dropna()
        rows.append(
            {
                "区县代码": code,
                "区县名称": name,
                "涉及场景数": len(group),
                "可判定场景数": len(determined),
                "高或中置信场景数": len(reliable),
                "偏移中位数_m": values.median() if len(values) else math.nan,
                "偏移P90_m": values.quantile(0.9) if len(values) else math.nan,
                "最大偏移_m": values.max() if len(values) else math.nan,
                "大于1m场景数": int((values > 1).sum()),
                "大于2m场景数": int((values > 2).sum()),
            }
        )
    return pd.DataFrame(rows).sort_values(["区县代码", "区县名称"])


def save_csv(frame: pd.DataFrame, path: Path) -> None:
    frame.to_csv(path, index=False, encoding="utf-8-sig")


def save_excel(
    output_path: Path,
    frames: list[tuple[str, pd.DataFrame]],
    notes: list[str],
) -> None:
    notes_frame = pd.DataFrame({"说明": notes})
    with pd.ExcelWriter(output_path, engine="openpyxl") as writer:
        for sheet_name, frame in frames:
            frame.to_excel(writer, sheet_name=sheet_name, index=False)
        notes_frame.to_excel(writer, sheet_name="说明", index=False)
        for sheet in writer.book.worksheets:
            sheet.freeze_panes = "A2"
            sheet.auto_filter.ref = sheet.dimensions
            for column_cells in sheet.columns:
                letter = column_cells[0].column_letter
                max_length = 0
                for cell in column_cells[: min(len(column_cells), 500)]:
                    value = "" if cell.value is None else str(cell.value)
                    max_length = max(max_length, len(value))
                sheet.column_dimensions[letter].width = min(
                    45, max(10, max_length * 1.1 + 2)
                )


def configure_matplotlib() -> None:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib import font_manager

    font_names: list[str] = []
    local_font = Path(__file__).resolve().parent / "SimHei.ttf"
    if local_font.exists():
        font_manager.fontManager.addfont(str(local_font))
        local_font_name = font_manager.FontProperties(
            fname=str(local_font)
        ).get_name()
        local_font_path = local_font.resolve()
        font_manager.fontManager.ttflist[:] = [
            entry
            for entry in font_manager.fontManager.ttflist
            if entry.name != local_font_name
            or Path(entry.fname).resolve() == local_font_path
        ]
        font_manager.fontManager._findfont_cached.cache_clear()
        font_names.append(local_font_name)
    font_names.extend([
        "Microsoft YaHei",
        "SimHei",
        "Arial Unicode MS",
        "DejaVu Sans",
    ])
    plt.rcParams["font.sans-serif"] = font_names
    plt.rcParams["axes.unicode_minus"] = False


def create_charts(scene_df: pd.DataFrame, output_dir: Path) -> list[Path]:
    configure_matplotlib()
    import matplotlib.pyplot as plt

    reliable = scene_df[scene_df["置信度"].isin(["高", "中"])].copy()
    chart_paths: list[Path] = []
    if reliable.empty:
        return chart_paths

    path = output_dir / "01_场景合成偏移分布.png"
    fig, ax = plt.subplots(figsize=(10, 6), dpi=160)
    upper = max(3.0, float(reliable["合成偏移_m"].quantile(0.98)) * 1.1)
    bins = np.linspace(0, upper, 26)
    ax.hist(
        reliable["合成偏移_m"].clip(upper=upper),
        bins=bins,
        color="#4472C4",
        edgecolor="white",
    )
    for threshold, color in ((1, "#70AD47"), (2, "#C00000")):
        ax.axvline(threshold, color=color, linestyle="--", linewidth=1.5)
    ax.set_title("高/中置信场景合成偏移分布")
    ax.set_xlabel("合成偏移（米）")
    ax.set_ylabel("场景数")
    ax.grid(axis="y", alpha=0.25)
    fig.tight_layout()
    fig.savefig(path)
    plt.close(fig)
    chart_paths.append(path)

    path = output_dir / "02_场景校正矢量散点.png"
    fig, ax = plt.subplots(figsize=(8, 8), dpi=160)
    class_colors = {
        "未见明显偏移（≤1m）": "#70AD47",
        "轻微偏移（1-2m）": "#FFC000",
        "严重偏移（>2m）": "#C00000",
    }
    for class_name, group in reliable.groupby("筛查分级"):
        ax.scatter(
            group["建议东移_m"],
            group["建议北移_m"],
            s=24,
            alpha=0.75,
            label=class_name,
            color=class_colors.get(class_name, "#808080"),
        )
    radius_max = max(3.0, float(reliable["合成偏移_m"].max()) * 1.1)
    for radius in (1, 2):
        circle = plt.Circle(
            (0, 0), radius, fill=False, linestyle="--", linewidth=1, color="gray"
        )
        ax.add_patch(circle)
    ax.axhline(0, color="black", linewidth=0.7)
    ax.axvline(0, color="black", linewidth=0.7)
    ax.set_xlim(-radius_max, radius_max)
    ax.set_ylim(-radius_max, radius_max)
    ax.set_aspect("equal", adjustable="box")
    ax.set_title("建议校正矢量（正东、正北）")
    ax.set_xlabel("建议东移（米；负值为西移）")
    ax.set_ylabel("建议北移（米；负值为南移）")
    ax.legend(fontsize=8, loc="best")
    ax.grid(alpha=0.2)
    fig.tight_layout()
    fig.savefig(path)
    plt.close(fig)
    chart_paths.append(path)

    path = output_dir / "03_偏移最大场景.png"
    top = reliable.nlargest(min(30, len(reliable)), "合成偏移_m").sort_values(
        "合成偏移_m"
    )
    fig_height = max(6, len(top) * 0.30)
    fig, ax = plt.subplots(figsize=(12, fig_height), dpi=160)
    colors = [class_colors.get(value, "#808080") for value in top["筛查分级"]]
    ax.barh(top["场景标识"], top["合成偏移_m"], color=colors)
    ax.axvline(1, color="#70AD47", linestyle="--", linewidth=1)
    ax.axvline(2, color="#C00000", linestyle="--", linewidth=1)
    ax.set_title("高/中置信场景合成偏移最大值")
    ax.set_xlabel("合成偏移（米）")
    ax.grid(axis="x", alpha=0.25)
    fig.tight_layout()
    fig.savefig(path)
    plt.close(fig)
    chart_paths.append(path)
    return chart_paths


def markdown_table(frame: pd.DataFrame, columns: Sequence[str]) -> str:
    if frame.empty:
        return "无"
    selected = frame.loc[:, columns].copy()
    for column in selected.columns:
        if pd.api.types.is_numeric_dtype(selected[column]):
            selected[column] = selected[column].map(
                lambda value: ""
                if pd.isna(value)
                else f"{value:.2f}"
                if isinstance(value, (float, np.floating))
                else str(value)
            )
    header = "| " + " | ".join(selected.columns) + " |"
    divider = "| " + " | ".join(["---"] * len(selected.columns)) + " |"
    rows = [
        "| "
        + " | ".join(str(value).replace("|", "/") for value in row)
        + " |"
        for row in selected.itertuples(index=False, name=None)
    ]
    return "\n".join([header, divider, *rows])


def write_report(
    output_dir: Path,
    config: Config,
    scene_df: pd.DataFrame,
    image_df: pd.DataFrame,
    sensor_df: pd.DataFrame,
    county_df: pd.DataFrame,
    chart_paths: Sequence[Path],
    elapsed_seconds: float,
) -> Path:
    determined = scene_df[scene_df["置信度"] != "无法判定"]
    reliable = scene_df[scene_df["置信度"].isin(["高", "中"])]
    low = scene_df[scene_df["置信度"].isin(["低", "低（单点）"])]
    undetermined = scene_df[scene_df["置信度"] == "无法判定"]
    class_counts = reliable["筛查分级"].value_counts()
    values = reliable["合成偏移_m"].dropna()
    top = reliable.nlargest(min(25, len(reliable)), "合成偏移_m")
    image_confidence_counts = image_df["置信度"].value_counts()

    lines = [
        "# 待检影像平面精度自动筛查报告",
        "",
        f"- 生成时间：{now_text()}",
        f"- 基准影像目录：`{config.reference}`",
        f"- 待检目录：`{config.input_root}`",
        f"- 待检影像文件：**{len(image_df)} 幅**",
        f"- 检测单元（影像/场景）：**{len(scene_df)} 景**",
        f"- 总处理耗时：**{elapsed_seconds / 60.0:.1f} 分钟**",
        "",
        "## 一、结论摘要",
        "",
        f"- 可给出偏移估计：**{len(determined)} 景**；其中高/中置信度 **{len(reliable)} 景**，低置信度待复核 **{len(low)} 景**。",
        f"- 无法可靠判定：**{len(undetermined)} 景**，通常由有效覆盖过小、纹理不足或跨时相匹配点不足导致。",
        f"- 按影像文件统计：高/中置信度 **{int(image_confidence_counts.get('高', 0) + image_confidence_counts.get('中', 0))} 幅**，低置信度 **{int(image_confidence_counts.get('低', 0) + image_confidence_counts.get('低（单点）', 0))} 幅**，无法判定 **{int(image_confidence_counts.get('无法判定', 0))} 幅**。",
    ]
    if len(values):
        lines.extend(
            [
                f"- 高/中置信场景合成偏移中位数：**{values.median():.2f} m（{values.median() / 2.0:.2f} 个参考像素）**。",
                f"- 高/中置信场景合成偏移 P90：**{values.quantile(0.9):.2f} m（{values.quantile(0.9) / 2.0:.2f} 个参考像素）**。",
                f"- 高/中置信场景最大合成偏移：**{values.max():.2f} m（{values.max() / 2.0:.2f} 个参考像素）**。",
                f"- 未见明显偏移（≤1m）：**{int(class_counts.get('未见明显偏移（≤1m）', 0))} 景**。",
                f"- 轻微偏移（1–2m）：**{int(class_counts.get('轻微偏移（1-2m）', 0))} 景**。",
                f"- 严重偏移（>2m）：**{int(class_counts.get('严重偏移（>2m）', 0))} 景**。",
            ]
        )
    lines.extend(
        [
            "",
            "## 二、偏移量与像素定义",
            "",
            "- `基准影像路径`：该检测单元实际配对使用的基准TIF。",
            "- `校正列偏移_参考像素`：以基准影像的2米网格计算；正值表示待检影像内容需要向右（东）移动。",
            "- `校正行偏移_参考像素`：以基准影像的2米网格计算；正值表示待检影像内容需要向下（南）移动。",
            "- `合成偏移_参考像素`：列、行偏移的平方和开方。",
            "- `校正列/行偏移_本影像像素`：按每幅待检影像自身的实际米分辨率换算；经纬度影像会先将度分辨率换算为米。",
            "- `建议东移_m`、`建议北移_m`：将待检影像配准到参考影像时建议施加的地图坐标平移。待检影像当前相对参考影像的偏移方向与建议校正方向相反。",
            "",
            "## 三、方法",
            "",
            f"1. 按图号配对基准TIF与 *_2025.tif，共检查{len(image_df)}组影像。",
            "2. 在各切片有效且纹理丰富的区域自动选取多个512或768像素检查窗口。",
            "3. 将待检影像动态重采样到对应基准TIF的坐标系和2米网格。",
            "4. 使用SIFT特征匹配与RANSAC剔除误匹配，获得各窗口的列、行平移。",
            "5. 对同一场景多个区县、多个窗口的平移结果做空间聚类和稳健加权中位数汇总。",
            "6. 把场景结果回填至全部影像文件，并按每幅影像自身分辨率换算像素偏移。",
            "",
            "## 四、筛查分级",
            "",
            "| 合成偏移 | 自动筛查分级 |",
            "| --- | --- |",
            "| ≤1m | 未见明显偏移 |",
            "| 1–2m | 轻微偏移 |",
            "| >2m | 严重偏移 |",
            "",
            "> 上述阈值用于快速筛查，不替代项目正式技术设计或法定验收标准。自动匹配还会受到跨年份地物变化、云雾、农作物物候和参考影像自身误差影响。",
            "",
            "## 五、偏移较大场景（高/中置信）",
            "",
            markdown_table(
                top,
                [
                    "场景标识",
                    "传感器",
                    "置信度",
                    "筛查分级",
                    "校正列偏移_参考像素",
                    "校正行偏移_参考像素",
                    "合成偏移_参考像素",
                    "建议东移_m",
                    "建议北移_m",
                    "合成偏移_m",
                    "建议校正方向",
                ],
            ),
            "",
            "## 六、传感器统计",
            "",
            markdown_table(
                sensor_df,
                [
                    "传感器",
                    "场景总数",
                    "可判定场景数",
                    "高或中置信场景数",
                    "偏移中位数_m",
                    "偏移P90_m",
                    "最大偏移_m",
                    "大于1m场景数",
                    "大于2m场景数",
                ],
            ),
            "",
            "## 七、成果文件",
            "",
            "- `平面精度检测结果.xlsx`：场景、影像、控制窗口、传感器和区县统计。",
            "- `场景平面偏移汇总.csv`：每个检测单元（影像/场景）一行。",
            "- `影像平面偏移明细.csv`：全部待检TIF逐文件结果，包含本影像像素偏移。",
            "- `匹配窗口明细.csv`：所有检查窗口的匹配质量、内点和偏移。",
            "- `传感器统计.csv`、`区县统计.csv`。",
            "- `需人工复核场景.csv`：低置信、无法判定或高/中置信但偏移超过1米的场景。",
        ]
    )
    if chart_paths:
        lines.extend(
            [
                "",
                "## 八、统计图",
                "",
                *[f"- `{path.name}`" for path in chart_paths],
            ]
        )
    report_path = output_dir / "平面精度检测报告.md"
    report_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return report_path


def export_results(
    results: list[dict[str, Any]],
    config: Config,
    elapsed_seconds: float,
) -> dict[str, Any]:
    output_dir = Path(config.output)
    output_dir.mkdir(parents=True, exist_ok=True)
    report_dir = output_dir / "01_报告与说明"
    table_dir = output_dir / "02_统计明细"
    chart_dir = output_dir / "03_图件"
    program_dir = output_dir / "90_程序与参数"
    for directory in (report_dir, table_dir, chart_dir, program_dir):
        directory.mkdir(parents=True, exist_ok=True)
    scene_df = pd.DataFrame([flatten_scene_row(result) for result in results])
    scene_df = scene_df.sort_values(["传感器", "成像日期", "场景标识"]).reset_index(
        drop=True
    )
    image_df = pd.DataFrame(
        [
            row
            for result in results
            for row in flatten_image_rows(result)
        ]
    )
    image_df = image_df.sort_values(
        ["区县代码", "场景标识", "文件名"]
    ).reset_index(drop=True)
    patch_df = pd.DataFrame(
        [
            row
            for result in results
            for row in flatten_patch_rows(result)
        ]
    )
    if not patch_df.empty:
        patch_df = patch_df.sort_values(["场景标识", "窗口序号"]).reset_index(
            drop=True
        )
    sensor_df = build_sensor_summary(scene_df)
    county_df = build_county_summary(image_df)
    review_df = scene_df[
        (scene_df["置信度"].isin(["低", "低（单点）", "无法判定"]))
        | (scene_df["合成偏移_m"] > 1)
    ].sort_values(
        ["置信度", "合成偏移_m"], ascending=[True, False], na_position="last"
    )

    csv_files = {
        "scene_csv": table_dir / "场景平面偏移汇总.csv",
        "image_csv": table_dir / "影像平面偏移明细.csv",
        "patch_csv": table_dir / "匹配窗口明细.csv",
        "sensor_csv": table_dir / "传感器统计.csv",
        "county_csv": table_dir / "区县统计.csv",
        "review_csv": table_dir / "需人工复核场景.csv",
    }
    save_csv(scene_df, csv_files["scene_csv"])
    save_csv(image_df, csv_files["image_csv"])
    save_csv(patch_df, csv_files["patch_csv"])
    save_csv(sensor_df, csv_files["sensor_csv"])
    save_csv(county_df, csv_files["county_csv"])
    save_csv(review_df, csv_files["review_csv"])

    notes = [
        "偏移量均表示：为使待检影像内容与2024参考影像对齐，建议对待检影像施加的平移。",
        "参考影像像素大小为2米。列偏移正值=向东/右移动；行偏移正值=向南/下移动。",
        "待检影像当前相对参考影像的偏移方向，与建议校正方向相反。",
        "本影像像素偏移按每幅影像自身的实际米分辨率换算；经纬度影像会自动将度分辨率换算为米。",
        "≤1m、1–2m、>2m为自动筛查分级，并非法定验收标准。",
        "正式处置前应优先人工复核偏移>1m、低置信和无法判定场景。",
    ]
    excel_path = output_dir / "平面精度检测结果.xlsx"
    save_excel(
        excel_path,
        [
            ("场景汇总", scene_df),
            ("影像明细", image_df),
            ("匹配窗口", patch_df),
            ("传感器统计", sensor_df),
            ("区县统计", county_df),
            ("需人工复核", review_df),
        ],
        notes,
    )
    try:
        chart_paths = create_charts(scene_df, chart_dir)
    except Exception:
        chart_paths = []
        (chart_dir / "统计图生成异常.log").write_text(
            traceback.format_exc(), encoding="utf-8"
        )
    report_path = write_report(
        report_dir,
        config,
        scene_df,
        image_df,
        sensor_df,
        county_df,
        chart_paths,
        elapsed_seconds,
    )
    summary = {
        "generated_at": now_text(),
        "reference": config.reference,
        "input_root": config.input_root,
        "image_count": int(len(image_df)),
        "scene_count": int(len(scene_df)),
        "determined_scene_count": int(
            (scene_df["置信度"] != "无法判定").sum()
        ),
        "reliable_scene_count": int(
            scene_df["置信度"].isin(["高", "中"]).sum()
        ),
        "low_confidence_scene_count": int(
            scene_df["置信度"].isin(["低", "低（单点）"]).sum()
        ),
        "undetermined_scene_count": int(
            (scene_df["置信度"] == "无法判定").sum()
        ),
        "elapsed_seconds": elapsed_seconds,
        "outputs": {
            **{key: str(value) for key, value in csv_files.items()},
            "excel": str(excel_path),
            "report": str(report_path),
            "charts": [str(path) for path in chart_paths],
        },
    }
    reliable_values = scene_df.loc[
        scene_df["置信度"].isin(["高", "中"]), "合成偏移_m"
    ].dropna()
    if len(reliable_values):
        summary.update(
            {
                "reliable_median_m": float(reliable_values.median()),
                "reliable_p90_m": float(reliable_values.quantile(0.9)),
                "reliable_max_m": float(reliable_values.max()),
                "reliable_over_1m": int((reliable_values > 1).sum()),
                "reliable_over_2m": int((reliable_values > 2).sum()),
            }
        )
    write_json_atomic(program_dir / "运行摘要.json", summary)
    write_json_atomic(
        program_dir / "运行参数.json",
        {
            **asdict(config),
            "screening_thresholds_m": [1, 2],
            "pixel_sign_definition": {
                "shift_col_px_ref_positive": "向右/东校正",
                "shift_row_px_ref_positive": "向下/南校正",
                "reference_pixel_size_m": config.reference_resolution_m,
            },
        },
    )
    return summary


def run_self_test() -> None:
    rng = np.random.default_rng(20260715)
    size = 512
    base = rng.integers(12, 28, size=(size, size), dtype=np.uint8)
    for _ in range(220):
        center = tuple(int(value) for value in rng.integers(15, size - 15, size=2))
        radius = int(rng.integers(2, 10))
        color = int(rng.integers(50, 240))
        cv2.circle(base, center, radius, color, -1)
    for _ in range(80):
        start = tuple(int(value) for value in rng.integers(0, size, size=2))
        end = tuple(int(value) for value in rng.integers(0, size, size=2))
        cv2.line(base, start, end, int(rng.integers(60, 220)), 1)
    reference = np.stack([base, base, base])
    imposed_col = 12.0
    imposed_row = -7.0
    transform = np.float32([[1, 0, imposed_col], [0, 1, imposed_row]])
    shifted = cv2.warpAffine(base, transform, (size, size), borderValue=0)
    target = np.stack([shifted, shifted, shifted])
    result = estimate_patch_shift(reference, target, 3, maximum_abs_shift_px=50)
    expected_col = -imposed_col
    expected_row = -imposed_row
    if not result.get("match_ok"):
        raise RuntimeError(f"自检匹配未通过: {result}")
    if (
        abs(result["shift_col_px_ref"] - expected_col) > 0.8
        or abs(result["shift_row_px_ref"] - expected_row) > 0.8
    ):
        raise RuntimeError(
            "自检偏移符号或数值错误: "
            f"expected=({expected_col},{expected_row}), result={result}"
        )
    log(
        "自检通过：人工施加"
        f"({imposed_col:+.1f},{imposed_row:+.1f})像素，"
        "检测得到建议校正"
        f"({result['shift_col_px_ref']:+.2f},{result['shift_row_px_ref']:+.2f})像素"
    )


def discover_groups(
    reference_root: Path,
    input_root: Path,
) -> dict[str, tuple[str, list[str]]]:
    """按图号建立“一个基准TIF + 一个2025待检TIF”的检测组。"""
    if reference_root.is_file():
        reference_paths = [reference_root]
    else:
        reference_paths = sorted(
            path
            for path in reference_root.rglob("*")
            if path.is_file() and path.suffix.lower() in {".tif", ".tiff"}
        )

    target_index: dict[str, list[Path]] = defaultdict(list)
    for path in sorted(input_root.rglob("*")):
        if (
            path.is_file()
            and path.suffix.lower() in {".tif", ".tiff"}
            and TARGET_YEAR_SUFFIX_RE.search(path.stem)
        ):
            target_index[pairing_key(path.stem)].append(path)

    groups: dict[str, tuple[str, list[str]]] = {}
    missing: list[str] = []
    for reference_path in reference_paths:
        key = pairing_key(reference_path.stem)
        matches = target_index.get(key, [])
        if not matches:
            missing.append(reference_path.name)
            continue
        if len(matches) > 1:
            log(
                f"基准 {reference_path.name} 找到 {len(matches)} 个同名待检影像，"
                f"按路径排序选择第一个: {matches[0]}"
            )
        target_path = matches[0]
        scene_key = target_path.stem
        groups[scene_key] = (str(reference_path), [str(target_path)])

    if missing:
        log(
            f"有 {len(missing)} 幅基准未找到对应的 *_2025.tif，已跳过："
            + "、".join(missing[:10])
            + ("……" if len(missing) > 10 else "")
        )
    if not reference_paths:
        raise RuntimeError(f"基准路径中没有TIF: {reference_root}")
    if not groups:
        raise RuntimeError(
            "没有找到可配对影像；示例配对为 "
            "reference/J47E001014.tif -> input-root/47E001014_2025.tif"
        )
    return groups


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--reference", default=DEFAULT_REFERENCE)
    parser.add_argument("--input-root", default=DEFAULT_INPUT_ROOT)
    parser.add_argument("--output", default=DEFAULT_OUTPUT)
    parser.add_argument("--workers", type=int, default=32)
    parser.add_argument("--max-candidates", type=int, default=10)
    parser.add_argument("--limit-scenes", type=int, default=0)
    parser.add_argument("--scene-filter", default="")
    parser.add_argument("--overwrite", action="store_true")
    parser.add_argument("--report-only", action="store_true")
    parser.add_argument("--skip-self-test", action="store_true")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    original_input_root = args.input_root
    config = Config(
        reference=runtime_path(args.reference, original_input_root),
        input_root=runtime_path(args.input_root, original_input_root),
        output=runtime_path(args.output, original_input_root),
        workers=max(1, args.workers),
        max_candidates_per_scene=max(1, args.max_candidates),
    )
    output_dir = Path(config.output)
    checkpoint_dir = output_dir / "99_过程检查点"
    output_dir.mkdir(parents=True, exist_ok=True)
    checkpoint_dir.mkdir(parents=True, exist_ok=True)
    start_time = time.time()

    if not args.skip_self_test:
        run_self_test()

    if not Path(config.reference).exists():
        raise FileNotFoundError(f"基准影像路径不存在: {config.reference}")
    if not Path(config.input_root).exists():
        raise FileNotFoundError(f"待检目录不存在: {config.input_root}")

    groups = discover_groups(Path(config.reference), Path(config.input_root))
    if args.scene_filter:
        groups = {
            key: values
            for key, values in groups.items()
            if args.scene_filter.lower() in key.lower()
        }
    if args.limit_scenes:
        groups = dict(list(sorted(groups.items()))[: args.limit_scenes])
    log(
        f"成功配对 {len(groups)} 组基准TIF和2025待检TIF"
    )

    existing_results: dict[str, dict[str, Any]] = {}
    pending: dict[str, list[str]] = {}
    for scene_key, (_, paths) in groups.items():
        checkpoint = checkpoint_path(checkpoint_dir, scene_key)
        if checkpoint.exists() and not args.overwrite:
            try:
                existing_results[scene_key] = json.loads(
                    checkpoint.read_text(encoding="utf-8")
                )
                continue
            except Exception:
                checkpoint.unlink(missing_ok=True)
        pending[scene_key] = paths

    if args.report_only and pending:
        raise RuntimeError(
            f"--report-only 但仍有 {len(pending)} 个场景没有检查点结果"
        )
    log(
        f"载入已有检查点 {len(existing_results)} 个，"
        f"本次待处理 {len(pending)} 个"
    )

    completed = 0
    if pending:
        config_dict = asdict(config)
        with concurrent.futures.ProcessPoolExecutor(
            max_workers=min(config.workers, len(pending))
        ) as executor:
            futures = {
                executor.submit(
                    process_scene,
                    scene_key,
                    paths,
                    groups[scene_key][0],
                    config_dict,
                ): scene_key
                for scene_key, paths in pending.items()
            }
            for future in concurrent.futures.as_completed(futures):
                scene_key = futures[future]
                try:
                    result = future.result()
                except Exception as exc:
                    result = {
                        "scene_key": scene_key,
                        "date": scene_parts(scene_key)[0],
                        "sensor": scene_parts(scene_key)[1],
                        "file_count": len(pending[scene_key]),
                        "reference_path": groups[scene_key][0],
                        "files": [
                            {
                                "path": path,
                                "file_name": Path(path).name,
                                **parse_tif(Path(path)),
                                "inspect_error": "",
                            }
                            for path in pending[scene_key]
                        ],
                        "patches": [],
                        "determined": False,
                        "confidence": "无法判定",
                        "reason": f"场景任务异常: {type(exc).__name__}: {exc}",
                        "screening_class": "无法判定",
                        "process_error": traceback.format_exc(),
                    }
                existing_results[scene_key] = result
                write_json_atomic(
                    checkpoint_path(checkpoint_dir, scene_key), result
                )
                completed += 1
                magnitude = safe_float(result.get("shift_magnitude_m"))
                magnitude_text = (
                    f"{magnitude:.2f}m" if math.isfinite(magnitude) else "N/A"
                )
                log(
                    f"[{completed}/{len(pending)}] {scene_key}: "
                    f"{result.get('confidence', '无法判定')}, "
                    f"{result.get('screening_class', '无法判定')}, "
                    f"{magnitude_text}, {result.get('seconds', 0):.1f}s"
                )

    results = [existing_results[key] for key in sorted(groups)]
    summary = export_results(
        results, config, elapsed_seconds=float(time.time() - start_time)
    )
    log(
        "全部完成："
        f"{summary['image_count']} 幅影像，{summary['scene_count']} 个场景，"
        f"高/中置信 {summary['reliable_scene_count']} 个，"
        f"低置信 {summary['low_confidence_scene_count']} 个，"
        f"无法判定 {summary['undetermined_scene_count']} 个"
    )
    log(f"成果目录: {config.output}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        log("用户中断")
        raise
    except Exception:
        traceback.print_exc()
        raise SystemExit(1)
