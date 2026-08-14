#!/usr/bin/env python
"""根据市边界与分幅表，查找城市覆盖的图幅号并拼接影像文件后缀。"""

from __future__ import annotations

import argparse
from pathlib import Path
import sys

import geopandas as gpd


ROOT = Path(__file__).resolve().parent
DATA_DIR = ROOT / "市边界和分幅"
DEFAULT_CITY_SHP = DATA_DIR / "15_市边界.shp"
DEFAULT_SHEET_SHP = DATA_DIR / "5w分幅成果结合表.shp"
DEFAULT_SUFFIX = "_2025.tif"
CITY_FIELD = "市名称"
SHEET_FIELD = "PLANE_NAME"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "输入城市名称，查找与城市实际相交的分幅，并生成类似 "
            "J48E001019_2025.tif 的影像文件名。"
        )
    )
    parser.add_argument(
        "city",
        nargs="?",
        help="城市名称，例如：乌海市；省略时由程序提示输入",
    )
    parser.add_argument(
        "--suffix",
        default=DEFAULT_SUFFIX,
        help=f"追加到图幅号后的文件名后缀，默认 {DEFAULT_SUFFIX}",
    )
    parser.add_argument(
        "-o",
        "--output",
        type=Path,
        help="可选：把结果保存为文本文件（UTF-8 with BOM）",
    )
    parser.add_argument(
        "--city-shp",
        type=Path,
        default=DEFAULT_CITY_SHP,
        help=f"市边界 SHP，默认 {DEFAULT_CITY_SHP}",
    )
    parser.add_argument(
        "--sheet-shp",
        type=Path,
        default=DEFAULT_SHEET_SHP,
        help=f"分幅 SHP，默认 {DEFAULT_SHEET_SHP}",
    )
    parser.add_argument(
        "--include-touches",
        action="store_true",
        help="同时保留只接触城市边界、但相交面积为零的分幅",
    )
    parser.add_argument(
        "--list-cities",
        action="store_true",
        help="列出市边界文件中的全部城市后退出",
    )
    return parser.parse_args()


def require_layer(path: Path, label: str) -> Path:
    resolved = path.resolve()
    if not resolved.is_file():
        raise FileNotFoundError(f"{label}不存在：{resolved}")
    return resolved


def normalized_city_name(value: str, available: set[str]) -> str:
    name = value.strip()
    if not name:
        raise ValueError("城市名称不能为空。")
    if name in available:
        return name
    with_suffix = name if name.endswith("市") else f"{name}市"
    if with_suffix in available:
        return with_suffix
    related = sorted(item for item in available if name in item or item in name)
    hint = f"；可能是：{', '.join(related)}" if related else ""
    raise ValueError(f"市边界中找不到城市 {name!r}{hint}")


def find_sheet_names(
    city_layer: gpd.GeoDataFrame,
    sheet_layer: gpd.GeoDataFrame,
    city_name: str,
    include_touches: bool,
) -> list[str]:
    if city_layer.crs is None:
        raise ValueError("市边界缺少坐标系。")
    if sheet_layer.crs is None:
        raise ValueError("分幅图层缺少坐标系。")
    if CITY_FIELD not in city_layer.columns:
        raise ValueError(f"市边界缺少字段：{CITY_FIELD}")
    if SHEET_FIELD not in sheet_layer.columns:
        raise ValueError(f"分幅图层缺少字段：{SHEET_FIELD}")

    city_values = city_layer[CITY_FIELD].fillna("").astype(str).str.strip()
    selected = city_layer.loc[city_values.eq(city_name)].copy()
    if selected.empty:
        raise ValueError(f"市边界中找不到城市：{city_name}")
    selected = selected.to_crs(sheet_layer.crs)
    selected.geometry = selected.geometry.make_valid()
    city_geometry = selected.geometry.union_all()
    if city_geometry.is_empty:
        raise ValueError(f"{city_name} 的边界几何为空。")

    candidates = sheet_layer.loc[sheet_layer.geometry.intersects(city_geometry)].copy()
    if not include_touches and not candidates.empty:
        intersection_area = candidates.geometry.intersection(city_geometry).area
        candidates = candidates.loc[intersection_area > 0].copy()

    values = candidates[SHEET_FIELD].dropna().astype(str).str.strip()
    return sorted({value for value in values if value})


def main() -> None:
    args = parse_args()
    city_path = require_layer(args.city_shp, "市边界 SHP")
    sheet_path = require_layer(args.sheet_shp, "分幅 SHP")
    city_layer = gpd.read_file(city_path)
    if CITY_FIELD not in city_layer.columns:
        raise ValueError(f"市边界缺少字段：{CITY_FIELD}")
    available = {
        value.strip()
        for value in city_layer[CITY_FIELD].dropna().astype(str)
        if value.strip()
    }

    if args.list_cities:
        print("\n".join(sorted(available)))
        return

    requested_city = args.city
    if requested_city is None:
        try:
            requested_city = input("请输入城市名称（例如乌海市）：")
        except EOFError as exc:
            raise ValueError("没有输入城市名称。") from exc
    city_name = normalized_city_name(requested_city, available)
    sheet_layer = gpd.read_file(sheet_path)
    sheet_names = find_sheet_names(
        city_layer,
        sheet_layer,
        city_name,
        args.include_touches,
    )
    tif_names = [f"{sheet_name}{args.suffix}" for sheet_name in sheet_names]

    print(f"城市：{city_name}")
    print(f"相交图幅数量：{len(sheet_names)}")
    for tif_name in tif_names:
        print(tif_name)

    if args.output is not None:
        output = args.output.resolve()
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text("\n".join(tif_names) + "\n", encoding="utf-8-sig")
        print(f"结果已保存：{output}")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"错误：{exc}", file=sys.stderr)
        raise SystemExit(1)
