#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""核验平面精度检测最终成果的内部一致性和关键文件。"""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path

import pandas as pd
from PIL import Image


def require(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", required=True)
    args = parser.parse_args()

    root = Path(args.output)
    detail = root / "02_统计明细"
    figures = root / "03_图件"
    programs = root / "90_程序与参数"
    checkpoints = root / "99_过程检查点"

    summary = json.loads((programs / "运行摘要.json").read_text(encoding="utf-8"))
    scenes = pd.read_csv(detail / "场景平面偏移汇总.csv", encoding="utf-8-sig")
    images = pd.read_csv(detail / "影像平面偏移明细.csv", encoding="utf-8-sig")
    patches = pd.read_csv(detail / "匹配窗口明细.csv", encoding="utf-8-sig")
    review = pd.read_csv(detail / "需人工复核场景.csv", encoding="utf-8-sig")

    expected_scenes = int(summary["scene_count"])
    expected_images = int(summary["image_count"])
    require(len(scenes) == expected_scenes, f"场景数异常: {len(scenes)}")
    require(len(images) == expected_images, f"影像数异常: {len(images)}")
    require(images["完整路径"].nunique() == expected_images, "影像路径存在重复")
    require("基准影像路径" in scenes.columns, "场景汇总缺少基准影像路径")
    require(scenes["基准影像路径"].fillna("").ne("").all(), "存在未记录基准路径的场景")

    confidence = scenes["置信度"].value_counts().to_dict()
    require(sum(confidence.values()) == expected_scenes, "置信度统计与场景数不一致")

    reliable = scenes[scenes["置信度"].isin(["高", "中"])].copy()
    require(
        len(reliable) == int(summary["reliable_scene_count"]),
        "高/中置信场景数与运行摘要不一致",
    )
    values = reliable["合成偏移_m"]
    ranges = {
        "≤1m": int((values <= 1).sum()),
        "1–2m": int(((values > 1) & (values <= 2)).sum()),
        ">2m": int((values > 2).sum()),
    }
    require(sum(ranges.values()) == len(reliable), "可靠结果偏移分段不完整")
    if len(values):
        require(
            math.isclose(values.median(), float(summary["reliable_median_m"]), abs_tol=1e-8),
            "中位数与运行摘要不一致",
        )
        require(
            math.isclose(values.quantile(0.9), float(summary["reliable_p90_m"]), abs_tol=1e-8),
            "P90与运行摘要不一致",
        )

    expected_review = scenes[
        scenes["置信度"].isin(["低", "低（单点）", "无法判定"])
        | (scenes["合成偏移_m"] > 1)
    ]
    require(len(review) == len(expected_review), "人工复核清单数量异常")
    require(scenes["处理异常"].fillna("").eq("").all(), "场景汇总存在处理异常")
    require(images["文件检查异常"].fillna("").eq("").all(), "影像明细存在文件检查异常")
    require(
        len(list(checkpoints.glob("*.json"))) == expected_scenes,
        "过程检查点数量异常",
    )

    expected_files = [
        root / "00_请先看_平面精度检测结论.md",
        root / "成果目录说明.txt",
        root / "平面精度检测结果.xlsx",
        root / "01_报告与说明" / "平面精度检测报告.md",
        root / "01_报告与说明" / "检测方法与图件阅读说明.md",
        programs / "运行参数.json",
        programs / "运行摘要.json",
    ]
    expected_files.extend(Path(path) for path in summary.get("outputs", {}).get("charts", []))
    for path in expected_files:
        require(path.exists() and path.stat().st_size > 0, f"缺少或空文件: {path}")

    image_sizes = {}
    for path in expected_files:
        if path.suffix.lower() == ".png":
            with Image.open(path) as image:
                image.verify()
            with Image.open(path) as image:
                image_sizes[path.name] = list(image.size)

    workbook = root / "平面精度检测结果.xlsx"
    excel = pd.ExcelFile(workbook)
    sheet_rows = {}
    for sheet in excel.sheet_names:
        sheet_rows[sheet] = len(pd.read_excel(workbook, sheet_name=sheet))
    require(
        any(rows == expected_scenes for rows in sheet_rows.values()),
        f"Excel中未发现{expected_scenes}行的逐景表",
    )
    require(
        any(rows == len(patches) for rows in sheet_rows.values()),
        f"Excel中未发现{len(patches)}行的窗口明细表",
    )

    result = {
        "status": "通过",
        "场景数": len(scenes),
        "影像数": len(images),
        "置信度": confidence,
        "可靠结果偏移分段": ranges,
        "匹配窗口": {
            "总数": len(patches),
            "通过": int(patches["匹配通过"].sum()),
            "用于汇总": int(patches["用于场景汇总"].sum()),
        },
        "人工复核清单": len(review),
        "检查点": len(list(checkpoints.glob("*.json"))),
        "Excel工作表行数": sheet_rows,
        "PNG尺寸": image_sizes,
    }
    report_path = programs / "最终成果核验.json"
    report_path.write_text(
        json.dumps(result, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
