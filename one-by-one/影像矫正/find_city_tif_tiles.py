#!/usr/bin/env python
"""根据市级边界查询相交图幅，并拼接影像文件名后缀。"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from pathlib import Path
import re

import geopandas as gpd


ROOT = Path(__file__).resolve().parent
DATA_DIR = ROOT / "市边界和分幅"
DEFAULT_BOUNDARY = DATA_DIR / "15_市边界.shp"
DEFAULT_TILES = DATA_DIR / "5w分幅成果结合表.shp"
DEFAULT_SUFFIX = "_2025.tif"
DEFAULT_CITY_FIELD = "市名称"
DEFAULT_TILE_FIELD = "PLANE_NAME"


@dataclass(frozen=True)
class CityTileSelection:
    city_name: str
    city_code: str
    tile_numbers: list[str]
    boundary_geometry: object
    boundary_crs: object


def clean_text(value: object) -> str:
    return str(value).strip() if value is not None else ""


def short_region_name(value: str) -> str:
    """移除常见行政区后缀，允许输入“乌海”匹配“乌海市”。"""
    return re.sub(r"(?:蒙古族藏族自治州|回族自治区|自治州|地区|市|盟)$", "", value)


def select_city(
    boundaries: gpd.GeoDataFrame,
    city_field: str,
    requested_city: str,
) -> tuple[str, gpd.GeoDataFrame]:
    if city_field not in boundaries.columns:
        raise KeyError(
            f"市边界缺少字段 {city_field!r}；现有字段："
            f"{list(boundaries.columns)}"
        )
    requested = clean_text(requested_city)
    if not requested:
        raise ValueError("城市名称不能为空。")

    names = boundaries[city_field].map(clean_text)
    selected = boundaries[names == requested]
    if selected.empty:
        requested_short = short_region_name(requested)
        short_names = names.map(short_region_name)
        candidates = sorted(set(names[short_names == requested_short]))
        if len(candidates) == 1:
            selected = boundaries[names == candidates[0]]
        elif len(candidates) > 1:
            raise ValueError(
                f"城市名称 {requested!r} 匹配到多个行政区："
                + "、".join(candidates)
            )
        else:
            available = "、".join(sorted(set(names)))
            raise ValueError(
                f"没有找到城市 {requested!r}。可用名称：{available}"
            )
    return clean_text(selected.iloc[0][city_field]), selected


def find_intersecting_tiles(
    boundary_path: Path,
    tile_path: Path,
    city: str,
    city_field: str,
    tile_field: str,
) -> tuple[str, list[str]]:
    selection = find_city_selection(
        boundary_path,
        tile_path,
        city,
        city_field,
        "市代码",
        tile_field,
    )
    return selection.city_name, selection.tile_numbers


def find_city_selection(
    boundary_path: Path,
    tile_path: Path,
    city: str,
    city_field: str = DEFAULT_CITY_FIELD,
    city_code_field: str = "市代码",
    tile_field: str = DEFAULT_TILE_FIELD,
) -> CityTileSelection:
    """返回城市边界、代码和相交图幅，供影像/SHP 流水线共同使用。"""
    if not boundary_path.is_file():
        raise FileNotFoundError(f"市边界文件不存在：{boundary_path}")
    if not tile_path.is_file():
        raise FileNotFoundError(f"分幅文件不存在：{tile_path}")

    boundaries = gpd.read_file(boundary_path)
    tiles = gpd.read_file(tile_path)
    if boundaries.crs is None:
        raise ValueError(f"市边界缺少坐标系：{boundary_path}")
    if tiles.crs is None:
        raise ValueError(f"分幅文件缺少坐标系：{tile_path}")
    if tile_field not in tiles.columns:
        raise KeyError(
            f"分幅文件缺少字段 {tile_field!r}；现有字段：{list(tiles.columns)}"
        )

    matched_city, selected = select_city(boundaries, city_field, city)
    selected = selected[selected.geometry.notna() & ~selected.geometry.is_empty]
    if selected.empty:
        raise ValueError(f"{matched_city} 的边界几何为空。")

    # 同一个城市即使由多个面或多条记录组成，也合并后统一判断。
    city_geometry = selected.to_crs(tiles.crs).geometry.union_all()
    intersecting = tiles[
        tiles.geometry.notna()
        & ~tiles.geometry.is_empty
        & tiles.geometry.intersects(city_geometry)
    ]
    tile_numbers = sorted(
        {
            clean_text(value)
            for value in intersecting[tile_field]
            if clean_text(value)
        }
    )
    city_code = (
        clean_text(selected.iloc[0][city_code_field])
        if city_code_field in selected.columns
        else ""
    )
    return CityTileSelection(
        city_name=matched_city,
        city_code=city_code,
        tile_numbers=tile_numbers,
        boundary_geometry=selected.geometry.union_all(),
        boundary_crs=selected.crs,
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "city",
        nargs="?",
        help="城市名称，例如：乌海市；省略时进入交互输入",
    )
    parser.add_argument(
        "--suffix",
        default=DEFAULT_SUFFIX,
        help="图幅号后拼接的文件名后缀，默认 _2025.tif",
    )
    parser.add_argument(
        "--boundary",
        type=Path,
        default=DEFAULT_BOUNDARY,
        help="市边界 SHP",
    )
    parser.add_argument(
        "--tiles",
        type=Path,
        default=DEFAULT_TILES,
        help="分幅 SHP",
    )
    parser.add_argument("--city-field", default=DEFAULT_CITY_FIELD)
    parser.add_argument("--tile-field", default=DEFAULT_TILE_FIELD)
    parser.add_argument(
        "--output",
        type=Path,
        help="输出 TXT；默认保存到 output/城市名_相交影像列表.txt",
    )
    parser.add_argument(
        "--no-save",
        action="store_true",
        help="只在控制台显示，不保存 TXT",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    city = args.city or input("请输入城市名称（例如乌海市）：").strip()
    matched_city, tile_numbers = find_intersecting_tiles(
        args.boundary.resolve(),
        args.tiles.resolve(),
        city,
        args.city_field,
        args.tile_field,
    )
    image_names = [f"{tile_number}{args.suffix}" for tile_number in tile_numbers]

    print(f"\n城市：{matched_city}")
    print(f"相交图幅数量：{len(tile_numbers)}")
    for image_name in image_names:
        print(image_name)

    if not args.no_save:
        output = (
            args.output.resolve()
            if args.output is not None
            else ROOT / "output" / f"{matched_city}_相交影像列表.txt"
        )
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(
            "\n".join(image_names) + ("\n" if image_names else ""),
            encoding="utf-8-sig",
        )
        print(f"\n列表已保存：{output}")


if __name__ == "__main__":
    main()
