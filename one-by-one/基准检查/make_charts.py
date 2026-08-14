#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""根据已生成的场景汇总 CSV 绘制平面偏移统计图。"""

from __future__ import annotations

import argparse
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib import font_manager
import numpy as np
import pandas as pd


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--output", default=r"D:\private\lx\平面精度检测"
    )
    args = parser.parse_args()
    output = Path(args.output)
    data_dir = (
        output / "02_统计明细"
        if (output / "02_统计明细" / "场景平面偏移汇总.csv").exists()
        else output
    )
    chart_dir = output / "03_图件" if (output / "03_图件").exists() else output
    scene = pd.read_csv(data_dir / "场景平面偏移汇总.csv", encoding="utf-8-sig")
    reliable = scene[scene["置信度"].isin(["高", "中"])].copy()
    if reliable.empty:
        raise RuntimeError("没有高/中置信场景，无法绘图")

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
    class_colors = {
        "未见明显偏移（≤1m）": "#70AD47",
        "轻微偏移（1-2m）": "#FFC000",
        "严重偏移（>2m）": "#C00000",
    }

    path = chart_dir / "01_场景合成偏移分布.png"
    figure, axis = plt.subplots(figsize=(10, 6), dpi=180)
    upper = max(3.0, float(reliable["合成偏移_m"].max()) * 1.08)
    axis.hist(
        reliable["合成偏移_m"],
        bins=np.linspace(0, upper, 24),
        color="#4472C4",
        edgecolor="white",
    )
    for threshold, color in ((1, "#70AD47"), (2, "#C00000")):
        axis.axvline(threshold, color=color, linestyle="--", linewidth=1.5)
    axis.set_title("高/中置信场景合成偏移分布")
    axis.set_xlabel("合成偏移（米）")
    axis.set_ylabel("场景数")
    axis.grid(axis="y", alpha=0.25)
    figure.tight_layout()
    figure.savefig(path)
    plt.close(figure)

    path = chart_dir / "02_场景校正矢量散点.png"
    figure, axis = plt.subplots(figsize=(8, 8), dpi=180)
    for class_name, group in reliable.groupby("筛查分级"):
        axis.scatter(
            group["建议东移_m"],
            group["建议北移_m"],
            s=30,
            alpha=0.8,
            label=class_name,
            color=class_colors.get(class_name, "#808080"),
        )
    radius_max = max(3.0, float(reliable["合成偏移_m"].max()) * 1.15)
    for radius in (1, 2):
        axis.add_patch(
            plt.Circle(
                (0, 0),
                radius,
                fill=False,
                linestyle="--",
                linewidth=1,
                color="gray",
            )
        )
    axis.axhline(0, color="black", linewidth=0.7)
    axis.axvline(0, color="black", linewidth=0.7)
    axis.set_xlim(-radius_max, radius_max)
    axis.set_ylim(-radius_max, radius_max)
    axis.set_aspect("equal", adjustable="box")
    axis.set_title("建议校正矢量（正东、正北）")
    axis.set_xlabel("建议东移（米；负值为西移）")
    axis.set_ylabel("建议北移（米；负值为南移）")
    axis.legend(fontsize=8, loc="best")
    axis.grid(alpha=0.2)
    figure.tight_layout()
    figure.savefig(path)
    plt.close(figure)

    path = chart_dir / "03_偏移最大场景.png"
    top = reliable.nlargest(min(30, len(reliable)), "合成偏移_m").sort_values(
        "合成偏移_m"
    )
    figure, axis = plt.subplots(
        figsize=(12, max(7, len(top) * 0.32)), dpi=180
    )
    colors = [
        class_colors.get(value, "#808080") for value in top["筛查分级"]
    ]
    axis.barh(top["场景标识"], top["合成偏移_m"], color=colors)
    axis.axvline(1, color="#70AD47", linestyle="--", linewidth=1)
    axis.axvline(2, color="#C00000", linestyle="--", linewidth=1)
    axis.set_title("高/中置信场景合成偏移最大值")
    axis.set_xlabel("合成偏移（米）")
    axis.grid(axis="x", alpha=0.25)
    figure.tight_layout()
    figure.savefig(path)
    plt.close(figure)

    report = (
        output / "01_报告与说明" / "平面精度检测报告.md"
        if (output / "01_报告与说明" / "平面精度检测报告.md").exists()
        else output / "平面精度检测报告.md"
    )
    if report.exists():
        text = report.read_text(encoding="utf-8")
        if "01_场景合成偏移分布.png" not in text:
            text += (
                "\n## 八、统计图\n\n"
                "- `01_场景合成偏移分布.png`\n"
                "- `02_场景校正矢量散点.png`\n"
                "- `03_偏移最大场景.png`\n"
            )
            report.write_text(text, encoding="utf-8")
    (output / "统计图生成异常.log").unlink(missing_ok=True)
    (chart_dir / "统计图生成异常.log").unlink(missing_ok=True)


if __name__ == "__main__":
    main()
