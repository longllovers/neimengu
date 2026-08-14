#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""为偏移较大的可靠场景制作校正前后 PNG 对比图，并输出简明结论摘要。"""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path

import cv2
import numpy as np
import pandas as pd
import rasterio
from PIL import Image, ImageDraw, ImageFont
from rasterio.enums import Resampling
from rasterio.vrt import WarpedVRT
from rasterio.windows import Window

from run_planar_accuracy import dataset_resolution_m

cv2.setNumThreads(1)


def font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont:
    candidates = [
        Path(__file__).resolve().parent / "SimHei.ttf",
        Path(r"C:\Windows\Fonts\msyhbd.ttc" if bold else r"C:\Windows\Fonts\msyh.ttc"),
        Path(r"C:\Windows\Fonts\simhei.ttf"),
    ]
    for candidate in candidates:
        if candidate.exists():
            return ImageFont.truetype(str(candidate), size=size)
    return ImageFont.load_default()


def stretch_rgb(array: np.ndarray) -> np.ndarray:
    data = array[:3].astype(np.float32)
    valid = np.any(data > 0, axis=0)
    output = np.zeros((data.shape[1], data.shape[2], 3), dtype=np.uint8)
    for index in range(3):
        channel = data[index]
        values = channel[valid]
        if values.size < 100:
            continue
        low, high = np.percentile(values, (2, 98))
        high = max(high, low + 1)
        output[:, :, index] = np.clip(
            (channel - low) * 255.0 / (high - low), 0, 255
        ).astype(np.uint8)
    output[~valid] = 0
    return output


def stretch_gray(array: np.ndarray, band_count: int) -> tuple[np.ndarray, np.ndarray]:
    data = array.astype(np.float32)
    valid = np.any(data > 0, axis=0)
    if band_count >= 8 and data.shape[0] >= 4:
        gray = data[:4].mean(axis=0)
    elif data.shape[0] >= 3:
        gray = 0.299 * data[0] + 0.587 * data[1] + 0.114 * data[2]
    else:
        gray = data.mean(axis=0)
    values = gray[valid]
    if values.size < 100:
        return np.zeros(gray.shape, dtype=np.uint8), valid
    low, high = np.percentile(values, (2, 98))
    high = max(high, low + 1)
    gray = np.clip((gray - low) * 255.0 / (high - low), 0, 255).astype(
        np.uint8
    )
    gray = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8)).apply(gray)
    gray[~valid] = 0
    return gray, valid


def shift_image(
    image: np.ndarray, mask: np.ndarray, shift_col: float, shift_row: float
) -> tuple[np.ndarray, np.ndarray]:
    matrix = np.float32([[1, 0, shift_col], [0, 1, shift_row]])
    shifted_image = cv2.warpAffine(
        image,
        matrix,
        (image.shape[1], image.shape[0]),
        flags=cv2.INTER_LINEAR,
        borderMode=cv2.BORDER_CONSTANT,
        borderValue=0,
    )
    shifted_mask = cv2.warpAffine(
        mask.astype(np.uint8),
        matrix,
        (mask.shape[1], mask.shape[0]),
        flags=cv2.INTER_NEAREST,
        borderMode=cv2.BORDER_CONSTANT,
        borderValue=0,
    ).astype(bool)
    return shifted_image, shifted_mask


def edge_overlay(
    reference_gray: np.ndarray,
    target_gray: np.ndarray,
    reference_mask: np.ndarray,
    target_mask: np.ndarray,
) -> np.ndarray:
    valid = reference_mask & target_mask
    reference_blur = cv2.GaussianBlur(reference_gray, (3, 3), 0)
    target_blur = cv2.GaussianBlur(target_gray, (3, 3), 0)
    reference_edges = cv2.Canny(reference_blur, 45, 115) > 0
    target_edges = cv2.Canny(target_blur, 45, 115) > 0
    reference_edges &= valid
    target_edges &= valid
    kernel = np.ones((2, 2), dtype=np.uint8)
    reference_edges = cv2.dilate(
        reference_edges.astype(np.uint8), kernel, iterations=1
    ).astype(bool)
    target_edges = cv2.dilate(
        target_edges.astype(np.uint8), kernel, iterations=1
    ).astype(bool)

    base = (
        0.25 * reference_gray.astype(np.float32)
        + 0.25 * target_gray.astype(np.float32)
        + 35
    )
    base = np.clip(base, 0, 150).astype(np.uint8)
    output = np.stack([base, base, base], axis=2)
    output[reference_edges] = (0, 230, 255)  # 青：参考
    output[target_edges] = (255, 45, 45)  # 红：待检
    output[reference_edges & target_edges] = (255, 255, 255)
    output[~valid] = (20, 20, 20)
    return output


def best_crop(mask: np.ndarray, desired_size: int) -> tuple[slice, slice]:
    height, width = mask.shape
    desired_size = min(desired_size, height - 4, width - 4)
    if desired_size <= 32:
        return slice(0, height), slice(0, width)
    distance = cv2.distanceTransform(mask.astype(np.uint8), cv2.DIST_L2, 5)
    if distance.max() > 0:
        center_row, center_col = np.unravel_index(np.argmax(distance), distance.shape)
    else:
        center_row, center_col = height // 2, width // 2
    half = desired_size // 2
    row0 = max(0, min(height - desired_size, center_row - half))
    col0 = max(0, min(width - desired_size, center_col - half))
    return slice(row0, row0 + desired_size), slice(col0, col0 + desired_size)


def resize_panel(array: np.ndarray, size: int) -> Image.Image:
    image = Image.fromarray(array)
    return image.resize((size, size), Image.Resampling.LANCZOS)


def draw_arrow(
    draw: ImageDraw.ImageDraw,
    start: tuple[float, float],
    end: tuple[float, float],
    color: tuple[int, int, int],
    width: int = 5,
) -> None:
    draw.line([start, end], fill=color, width=width)
    angle = math.atan2(end[1] - start[1], end[0] - start[0])
    length = 16
    for delta in (2.55, -2.55):
        point = (
            end[0] + length * math.cos(angle + delta),
            end[1] + length * math.sin(angle + delta),
        )
        draw.line([end, point], fill=color, width=width)


def pick_patch(
    scene_row: pd.Series, patches: pd.DataFrame
) -> pd.Series:
    group = patches[
        (patches["场景标识"] == scene_row["场景标识"])
        & (patches["用于场景汇总"] == True)  # noqa: E712
    ].copy()
    if group.empty:
        raise RuntimeError(f"场景没有可视化窗口: {scene_row['场景标识']}")
    group["距场景汇总偏移"] = np.hypot(
        group["校正列偏移_参考像素"] - scene_row["校正列偏移_参考像素"],
        group["校正行偏移_参考像素"] - scene_row["校正行偏移_参考像素"],
    )
    group["选择评分"] = (
        group["距场景汇总偏移"]
        + 4.0 / np.maximum(group["RANSAC内点数"].astype(float), 1.0)
        + group["残差中位数_像素"].fillna(4.0) * 0.08
    )
    return group.sort_values("选择评分").iloc[0]


def load_patch(
    reference_path: Path,
    scene_row: pd.Series,
    patch_row: pd.Series,
) -> dict[str, np.ndarray | int]:
    target_path = Path(str(patch_row["完整路径"]))
    map_x = float(patch_row["中心X"])
    map_y = float(patch_row["中心Y"])
    patch_size = int(patch_row["窗口尺寸_参考像素"])
    with rasterio.open(reference_path) as reference, rasterio.open(
        target_path
    ) as target:
        target_resolution_x_m, target_resolution_y_m = dataset_resolution_m(
            target
        )
        center_row, center_col = reference.index(map_x, map_y)
        half = patch_size // 2
        window = Window(
            int(center_col - half),
            int(center_row - half),
            patch_size,
            patch_size,
        )
        reference_array = reference.read([1, 2, 3], window=window)
        read_count = min(target.count, 4 if target.count >= 8 else 3)
        indexes = list(range(1, read_count + 1))
        with WarpedVRT(
            target,
            crs=reference.crs,
            transform=reference.transform,
            width=reference.width,
            height=reference.height,
            resampling=Resampling.bilinear,
            src_nodata=0,
            nodata=0,
        ) as warped:
            target_array = warped.read(indexes, window=window)
        return {
            "reference": reference_array,
            "target": target_array,
            "target_band_count": target.count,
            "target_resolution_m": float(
                (target_resolution_x_m + target_resolution_y_m) / 2.0
            ),
            "patch_size": patch_size,
        }


def make_comparison(
    reference_path: Path,
    scene_row: pd.Series,
    patch_row: pd.Series,
    output_path: Path,
) -> None:
    data = load_patch(reference_path, scene_row, patch_row)
    reference_array = data["reference"]
    target_array = data["target"]
    target_band_count = int(data["target_band_count"])
    target_resolution_m = float(data["target_resolution_m"])
    patch_size = int(data["patch_size"])

    reference_rgb = stretch_rgb(reference_array)
    target_rgb = stretch_rgb(target_array)
    reference_gray, reference_mask = stretch_gray(reference_array, 3)
    target_gray, target_mask = stretch_gray(target_array, target_band_count)

    shift_col = float(scene_row["校正列偏移_参考像素"])
    shift_row = float(scene_row["校正行偏移_参考像素"])
    corrected_gray, corrected_mask = shift_image(
        target_gray, target_mask, shift_col, shift_row
    )
    before_overlay = edge_overlay(
        reference_gray, target_gray, reference_mask, target_mask
    )
    after_overlay = edge_overlay(
        reference_gray, corrected_gray, reference_mask, corrected_mask
    )

    desired_crop = 640 if patch_size >= 700 else 460
    crop_rows, crop_cols = best_crop(reference_mask & target_mask, desired_crop)
    reference_rgb = reference_rgb[crop_rows, crop_cols]
    target_rgb = target_rgb[crop_rows, crop_cols]
    before_overlay = before_overlay[crop_rows, crop_cols]
    after_overlay = after_overlay[crop_rows, crop_cols]
    crop_size = reference_rgb.shape[0]

    panel_size = 620
    panel_gap = 22
    margin = 32
    label_height = 48
    header_height = 145
    footer_height = 80
    canvas_width = margin * 2 + panel_size * 2 + panel_gap
    canvas_height = (
        header_height
        + (label_height + panel_size) * 2
        + panel_gap
        + footer_height
    )
    canvas = Image.new("RGB", (canvas_width, canvas_height), "white")
    draw = ImageDraw.Draw(canvas)
    title_font = font(29, bold=True)
    subtitle_font = font(21)
    label_font = font(22, bold=True)
    note_font = font(18)

    scene_name = str(scene_row["场景标识"])
    magnitude_m = float(scene_row["合成偏移_m"])
    magnitude_px = float(scene_row["合成偏移_参考像素"])
    target_magnitude_px = magnitude_m / target_resolution_m
    direction = str(scene_row["建议校正方向"])
    draw.text(
        (margin, 18),
        f"平面偏移对比：{scene_name}",
        font=title_font,
        fill=(20, 20, 20),
    )
    draw.text(
        (margin, 63),
        f"置信度：{scene_row['置信度']}    合成偏移：{magnitude_m:.2f} m"
        f"（{magnitude_px:.2f} 个2米参考像素；"
        f"{target_magnitude_px:.2f} 个{target_resolution_m:g}米待检像素）",
        font=subtitle_font,
        fill=(35, 35, 35),
    )
    draw.text(
        (margin, 101),
        f"建议校正：{direction}；列 {shift_col:+.2f} px，行 {shift_row:+.2f} px",
        font=subtitle_font,
        fill=(35, 35, 35),
    )

    panels = [
        ("2024参考影像", resize_panel(reference_rgb, panel_size)),
        ("待检影像（当前坐标）", resize_panel(target_rgb, panel_size)),
        (
            "校正前边缘叠加（红=待检，青=参考）",
            resize_panel(before_overlay, panel_size),
        ),
        (
            "按统计偏移校正后的边缘叠加",
            resize_panel(after_overlay, panel_size),
        ),
    ]
    positions = [
        (margin, header_height),
        (margin + panel_size + panel_gap, header_height),
        (margin, header_height + label_height + panel_size + panel_gap),
        (
            margin + panel_size + panel_gap,
            header_height + label_height + panel_size + panel_gap,
        ),
    ]
    for (label, panel), (x, y) in zip(panels, positions):
        draw.text((x, y), label, font=label_font, fill=(20, 20, 20))
        canvas.paste(panel, (x, y + label_height))

    # 在校正前叠加图上绘制放大的建议校正方向箭头。
    before_x, before_y = positions[2]
    scale = panel_size / float(crop_size)
    amplification = 12.0
    start = (
        before_x + panel_size * 0.5,
        before_y + label_height + panel_size * 0.5,
    )
    end = (
        start[0] + shift_col * scale * amplification,
        start[1] + shift_row * scale * amplification,
    )
    draw_arrow(draw, start, end, color=(255, 235, 0), width=6)

    footer_y = canvas_height - footer_height + 8
    draw.text(
        (margin, footer_y),
        "黄色箭头表示建议校正方向，为便于观察已放大12倍；"
        "边缘由红、青分离变为接近白色，表示校正后重合度提高。",
        font=note_font,
        fill=(40, 40, 40),
    )
    draw.text(
        (margin, footer_y + 34),
        (
            f"可视化窗口来源：{patch_row['文件名']}；"
            if pd.isna(patch_row["区县代码"])
            or not str(patch_row["区县代码"]).strip()
            else f"可视化窗口：{patch_row['区县代码']} {patch_row['区县名称']}；"
        )
        + f"窗口内RANSAC内点 {int(patch_row['RANSAC内点数'])} 个。",
        font=note_font,
        fill=(70, 70, 70),
    )
    output_path.parent.mkdir(parents=True, exist_ok=True)
    canvas.save(output_path, format="PNG", optimize=True)


def write_summary(
    output_dir: Path,
    scenes: pd.DataFrame,
    images: pd.DataFrame,
    comparison_paths: list[Path],
) -> Path:
    reliable = scenes[scenes["置信度"].isin(["高", "中"])].copy()
    high = scenes[scenes["置信度"] == "高"]
    medium = scenes[scenes["置信度"] == "中"]
    low = scenes[scenes["置信度"] == "低"]
    low_single = scenes[scenes["置信度"] == "低（单点）"]
    undetermined = scenes[scenes["置信度"] == "无法判定"]
    normal = reliable[reliable["合成偏移_m"] <= 1]
    slight = reliable[
        (reliable["合成偏移_m"] > 1) & (reliable["合成偏移_m"] <= 2)
    ].sort_values("合成偏移_m", ascending=False)
    severe = reliable[reliable["合成偏移_m"] > 2]
    shifted = pd.concat([slight, severe]).sort_values(
        "合成偏移_m", ascending=False
    )
    native_pixels = (
        images.sort_values("合成偏移_m", ascending=False)
        .drop_duplicates("场景标识")
        .set_index("场景标识")["合成偏移_本影像像素"]
        .to_dict()
    )

    lines = [
        "# 平面精度检测结论摘要",
        "",
        "## 一、最终结论",
        "",
        f"共检查 **{len(scenes)} 景**。以偏移超过1米作为存在偏移，且只采用高、中置信结果：",
        "",
        f"- 未见明显偏移（≤1米）：**{len(normal)} 景**",
        f"- 轻微偏移（1–2米）：**{len(slight)} 景**",
        f"- 严重偏移（>2米）：**{len(severe)} 景**",
        f"- 低置信：**{len(low)} 景**",
        f"- 低置信（仅一个有效窗口）：**{len(low_single)} 景**",
        f"- 无法可靠判定：**{len(undetermined)} 景**",
        "",
        f"可靠结果共 **{len(reliable)} 景**，其中 {len(normal)} 景不超过1米，"
        f"{len(slight)} 景为1–2米，{len(severe)} 景超过2米。",
        "",
        f"> 最准确的表述：共{len(scenes)}景，其中{len(slight)}景为1–2米轻微偏移，"
        f"{len(severe)}景超过2米；"
        f"另有{len(low) + len(low_single) + len(undetermined)}景因证据不足需要人工复核。",
        "",
        "## 二、分级表",
        "",
        "| 等级 | 偏移范围 | 场景数 | 说明 |",
        "| --- | ---: | ---: | --- |",
        f"| 正常 | ≤1m | {len(normal)} | 未见明显偏移 |",
        f"| 轻微偏移 | 1–2m | {len(slight)} | 建议重点复核 |",
        f"| 严重偏移 | >2m | {len(severe)} | 建议优先处理 |",
        f"| 低置信 | — | {len(low)} | 匹配证据不足，不能直接定性 |",
        f"| 低置信（单点） | — | {len(low_single)} | 只有一个有效窗口，不能直接定性 |",
        f"| 无法判定 | — | {len(undetermined)} | 需人工检查 |",
        "",
        "## 三、置信度与可靠结果统计",
        "",
        f"- 高置信：**{len(high)}景**；中置信：**{len(medium)}景**；"
        f"可靠结果合计：**{len(reliable)}景**。",
        f"- 合成偏移中位数：**{reliable['合成偏移_m'].median():.2f}米**，"
        f"约 **{reliable['合成偏移_参考像素'].median():.2f}个参考像素**。",
        f"- P90：**{reliable['合成偏移_m'].quantile(0.9):.2f}米**，"
        f"约 **{reliable['合成偏移_参考像素'].quantile(0.9):.2f}个参考像素**。",
        f"- 最大可靠偏移：**{reliable['合成偏移_m'].max():.2f}米**，"
        f"约 **{reliable['合成偏移_参考像素'].max():.2f}个参考像素**。",
        "",
        f"## 四、{len(shifted)}景可靠偏移超过1米清单",
        "",
        "| 场景标识 | 分级 | 置信度 | 2米参考像素 | 0.5米待检像素 | 合成偏移（米） | 建议校正方向 |",
        "| --- | --- | --- | ---: | ---: | ---: | --- |",
    ]
    for _, row in shifted.iterrows():
        lines.append(
            f"| {row['场景标识']} | {row['筛查分级']} | {row['置信度']} | "
            f"{row['合成偏移_参考像素']:.2f} | "
            f"{float(native_pixels.get(row['场景标识'], float('nan'))):.2f} | "
            f"{row['合成偏移_m']:.2f} | "
            f"{row['建议校正方向']} |"
        )
    lines.extend(
        [
            "",
            "## 五、人工复核说明",
            "",
            f"- `需人工复核场景.csv` 共{len(shifted) + len(low) + len(low_single) + len(undetermined)}景："
            f"{len(slight) + len(severe)}景可靠偏移超过1米、"
            f"{len(low)}景低置信、{len(low_single)}景低置信（单点）、"
            f"{len(undetermined)}景无法判定。",
            "- 单点匹配产生的大偏移只能列为疑似，不能直接作为确认偏移，应以人工判读为准。",
            "- 本分级用于快速筛查，不代替项目正式技术设计或法定验收标准。",
            "- 参考影像网格为2米，因此小于1米的结果宜理解为“偏移很小”，"
            "不应当作测绘控制点级或亚米级绝对精度结论。",
            "",
            "## 六、偏移对比图",
            "",
        ]
    )
    for path in comparison_paths:
        lines.append(f"- `{path.relative_to(output_dir)}`")
    summary_path = output_dir / "00_请先看_平面精度检测结论.md"
    summary_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return summary_path


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--output", default=r"D:\private\lx\平面精度检测"
    )
    parser.add_argument("--top", type=int, default=2)
    args = parser.parse_args()
    output_dir = Path(args.output)
    data_dir = (
        output_dir / "02_统计明细"
        if (output_dir / "02_统计明细" / "场景平面偏移汇总.csv").exists()
        else output_dir
    )
    program_dir = (
        output_dir / "90_程序与参数"
        if (output_dir / "90_程序与参数" / "运行参数.json").exists()
        else output_dir
    )
    figure_dir = (
        output_dir / "03_图件"
        if (output_dir / "03_图件").exists()
        else output_dir
    )
    parameters = json.loads((program_dir / "运行参数.json").read_text(encoding="utf-8"))
    default_reference_path = Path(parameters["reference"])
    scenes = pd.read_csv(
        data_dir / "场景平面偏移汇总.csv", encoding="utf-8-sig"
    )
    patches = pd.read_csv(
        data_dir / "匹配窗口明细.csv", encoding="utf-8-sig"
    )
    images = pd.read_csv(
        data_dir / "影像平面偏移明细.csv", encoding="utf-8-sig"
    )
    selected = (
        scenes[scenes["置信度"].isin(["高", "中"])]
        .nlargest(args.top, "合成偏移_m")
        .reset_index(drop=True)
    )
    comparison_dir = figure_dir / "明显偏移对比图"
    comparison_paths: list[Path] = []
    for index, scene_row in selected.iterrows():
        patch_row = pick_patch(scene_row, patches)
        reference_value = scene_row.get("基准影像路径", "")
        reference_path = (
            Path(str(reference_value))
            if pd.notna(reference_value) and str(reference_value).strip()
            else default_reference_path
        )
        safe_scene = str(scene_row["场景标识"]).replace(":", "_")
        output_path = comparison_dir / (
            f"对比图_{index + 1:02d}_{safe_scene}_偏移"
            f"{float(scene_row['合成偏移_m']):.2f}m.png"
        )
        make_comparison(reference_path, scene_row, patch_row, output_path)
        comparison_paths.append(output_path)
    summary_path = write_summary(output_dir, scenes, images, comparison_paths)
    print(summary_path)
    for path in comparison_paths:
        print(path)


if __name__ == "__main__":
    main()
