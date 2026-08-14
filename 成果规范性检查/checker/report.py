from __future__ import annotations

import json
import os
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.backends.backend_pdf import PdfPages
from matplotlib.font_manager import FontProperties

from .models import CheckResult


def write_json(result: CheckResult, output: Path) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")


def _font(size: float, *, bold: bool = False) -> FontProperties:
    project_root = Path(__file__).resolve().parent.parent
    configured_font = os.environ.get("SIMHEI_FONT", "").strip()
    candidates = []
    if configured_font:
        candidates.append(Path(configured_font))
    candidates.extend(
        [
        project_root / "SimHei.ttf",
        project_root / "simhei.ttf",
        Path.cwd() / "SimHei.ttf",
        Path.cwd() / "simhei.ttf",
        Path(r"C:\Windows\Fonts\msyhbd.ttc" if bold else r"C:\Windows\Fonts\msyh.ttc"),
        Path(r"C:\Windows\Fonts\simhei.ttf"),
        Path(r"C:\Windows\Fonts\simsun.ttc"),
        ]
    )
    for candidate in candidates:
        if candidate.exists():
            return FontProperties(fname=str(candidate), size=size)
    return FontProperties(family="DejaVu Sans", size=size, weight="bold" if bold else "normal")


def _wrap(text: str, width: int = 76) -> list[str]:
    text = str(text)
    if not text:
        return [""]
    lines: list[str] = []
    for paragraph in text.splitlines() or [""]:
        while len(paragraph) > width:
            lines.append(paragraph[:width])
            paragraph = paragraph[width:]
        lines.append(paragraph)
    return lines


def _new_page(pdf: PdfPages, page_number: int) -> tuple[plt.Figure, plt.Axes, float]:
    fig = plt.figure(figsize=(8.27, 11.69), facecolor="white")
    ax = fig.add_axes([0, 0, 1, 1])
    ax.set_xlim(0, 1)
    ax.set_ylim(0, 1)
    ax.axis("off")
    if page_number == 1:
        ax.text(
            0.07,
            0.965,
            "遥感测量数据成果规范检查报告",
            fontproperties=_font(16, bold=True),
            color="black",
            va="top",
            zorder=10,
        )
        content_y = 0.905
    else:
        # Matplotlib 的 TTC 字体子集在重复中文页眉时偶尔会漏字；
        # 延续页采用简洁分隔线，避免依赖重复字形且便于识别分页。
        ax.plot([0.07, 0.93], [0.955, 0.955], color="#888888", linewidth=0.6)
        content_y = 0.925
    ax.text(0.93, 0.035, f"第 {page_number} 页", fontproperties=_font(8), ha="right")
    return fig, ax, content_y


def write_pdf(result: CheckResult, output: Path) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    with PdfPages(output) as pdf:
        if result.passed:
            fig = plt.figure(figsize=(8.27, 11.69), facecolor="white")
            ax = fig.add_axes([0, 0, 1, 1])
            ax.axis("off")
            ax.text(0.5, 0.56, "通过", fontproperties=_font(36, bold=True), ha="center", va="center")
            pdf.savefig(fig)
            plt.close(fig)
            return

        errors = sum(item.severity == "ERROR" for item in result.issues)
        warnings = sum(item.severity == "WARNING" for item in result.issues)
        page = 1
        fig, ax, y = _new_page(pdf, page)

        summary = [
            f"结论：{'通过' if result.passed else '不通过'}",
            f"待检目录：{result.root}",
            f"省代码 / 年份：{result.province_code} / {result.year}",
            f"核验方案：GDB 按表 {result.gdb_schema}；ELJDZPJ 按表 {result.zpj_schema}",
            f"检查属性表：{result.checked_vectors} 个；检查记录：{result.checked_records} 条",
            f"错误：{errors}；警告：{warnings}",
        ]
        for line in summary:
            for wrapped in _wrap(line):
                ax.text(0.07, y, wrapped, fontproperties=_font(10.5), va="top")
                y -= 0.025
        y -= 0.015

        visible_issues = [item for item in result.issues if item.severity != "INFO"]
        if not visible_issues:
            visible_issues = result.issues
        for index, issue in enumerate(visible_issues, 1):
            block = [
                f"{index}. [{issue.severity}] {issue.message}",
                f"位置：{issue.location}",
            ]
            if issue.expected:
                block.append(f"期望：{issue.expected}")
            if issue.actual:
                block.append(f"实际：{issue.actual}")
            if issue.details:
                block.append("样例/明细：" + json.dumps(issue.details, ensure_ascii=False))
            block_lines: list[str] = []
            for line in block:
                block_lines.extend(_wrap(line))
            needed = len(block_lines) * 0.021 + 0.018
            if y - needed < 0.075:
                pdf.savefig(fig)
                plt.close(fig)
                page += 1
                fig, ax, y = _new_page(pdf, page)
            for line_index, line in enumerate(block_lines):
                prop = _font(9.5, bold=(line_index == 0))
                ax.text(0.07, y, line, fontproperties=prop, va="top")
                y -= 0.021
            y -= 0.012

        pdf.savefig(fig)
        plt.close(fig)
