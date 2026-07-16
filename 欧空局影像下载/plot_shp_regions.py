#!/usr/bin/env python3
"""
将 shp 目录下的每一个区域分别绘制成 PNG 图片。

默认：
- 输入目录：./shp
- 输出目录：./img
- 每个要素单独保存一张图
"""

import argparse
import math
import re
from pathlib import Path

import geopandas as gpd
import pandas as pd
from PIL import Image, ImageDraw, ImageFont
from shapely.geometry import GeometryCollection, MultiPolygon, Polygon, box


VECTOR_SUFFIXES = {".shp", ".geojson", ".json"}
NAME_FIELD_CANDIDATES = (
    "area_name",
    "name",
    "Name",
    "NAME",
    "名称",
    "市名称",
    "县名称",
    "区名称",
    "旗名称",
    "盟名称",
    "乡名称",
    "省名称",
    "市",
    "县",
    "区",
    "旗",
)

DEFAULT_COUNTY_LAYER = Path("./00县边界/15_县边界.shp")
DEFAULT_CITY_LAYER = Path("./00市边界/15_市边界.shp")


def find_vector_files(shp_dir):
    return sorted(
        p for p in Path(shp_dir).rglob("*")
        if p.is_file() and p.suffix.lower() in VECTOR_SUFFIXES
    )


def read_all_regions(shp_dir):
    frames = []
    for vector_file in find_vector_files(shp_dir):
        if vector_file.name.startswith(".") or "_县边界" in vector_file.stem:
            continue
        gdf = gpd.read_file(vector_file)
        if gdf.empty:
            continue
        if gdf.crs and str(gdf.crs).upper() != "EPSG:4326":
            gdf = gdf.to_crs(epsg=4326)
        elif not gdf.crs:
            gdf = gdf.set_crs(epsg=4326)
        gdf = gdf.copy()
        gdf["_source_file"] = vector_file.stem
        frames.append(gdf)

    if not frames:
        raise FileNotFoundError(f"未在 {shp_dir} 下找到 .shp/.geojson/.json 文件")

    return gpd.GeoDataFrame(
        pd.concat(frames, ignore_index=True),
        geometry="geometry",
        crs="EPSG:4326",
    )


def read_layer(layer_path):
    gdf = gpd.read_file(layer_path)
    if gdf.empty:
        return gdf
    if gdf.crs and str(gdf.crs).upper() != "EPSG:4326":
        gdf = gdf.to_crs(epsg=4326)
    elif not gdf.crs:
        gdf = gdf.set_crs(epsg=4326)
    return gdf[gdf.geometry.notna() & ~gdf.geometry.is_empty].reset_index(drop=True)


def read_optional_layers(paths):
    layers = []
    for layer_path in paths or []:
        path = Path(layer_path)
        if path.exists():
            gdf = read_layer(path)
            if not gdf.empty:
                layers.append(gdf)
    return layers


def add_default_map_layers(args):
    if args.no_auto_layers:
        return

    if not args.county_layer and DEFAULT_COUNTY_LAYER.exists():
        args.county_layer.append(str(DEFAULT_COUNTY_LAYER))

    if not args.city_layer and DEFAULT_CITY_LAYER.exists():
        args.city_layer.append(str(DEFAULT_CITY_LAYER))


def total_bounds_for_layers(layers):
    valid_bounds = [gdf.total_bounds for gdf in layers if gdf is not None and not gdf.empty]
    if not valid_bounds:
        return None
    bounds = pd.DataFrame(valid_bounds, columns=["minx", "miny", "maxx", "maxy"])
    return (
        bounds["minx"].min(),
        bounds["miny"].min(),
        bounds["maxx"].max(),
        bounds["maxy"].max(),
    )


def choose_name_field(gdf, requested_field=None):
    if requested_field:
        if requested_field not in gdf.columns:
            raise ValueError(f"字段不存在：{requested_field}，可用字段：{list(gdf.columns)}")
        return requested_field

    for field in NAME_FIELD_CANDIDATES:
        if field in gdf.columns and gdf[field].notna().any():
            return field

    for field in gdf.columns:
        if field != "geometry" and gdf[field].notna().any():
            return field

    return None


def safe_filename(value):
    text = str(value).strip() or "region"
    text = re.sub(r'[\\/:*?"<>|\s]+', "_", text)
    return text.strip("._") or "region"


def cleanup_numbered_images(output_dir):
    """清理旧版本生成的 城市名_001.png 这类编号图片。"""
    pattern = re.compile(r".+_\d{3}\.png$", re.IGNORECASE)
    removed = []
    for image_file in Path(output_dir).glob("*.png"):
        if pattern.match(image_file.name):
            image_file.unlink()
            removed.append(image_file)
    return removed


def get_region_name(row, name_field, fallback_index=None):
    if name_field and row.get(name_field) is not None:
        text = str(row[name_field]).strip()
        if text and text.lower() != "none":
            return text

    source_file = str(row.get("_source_file", "")).strip()
    if source_file:
        return source_file

    if fallback_index is not None:
        return f"region_{fallback_index + 1:03d}"
    return "region"


def load_font(size):
    font_paths = [
        r"C:\Windows\Fonts\msyh.ttc",
        r"C:\Windows\Fonts\simhei.ttf",
        r"C:\Windows\Fonts\simsun.ttc",
        r"C:\Windows\Fonts\arial.ttf",
    ]
    for font_path in font_paths:
        if Path(font_path).exists():
            return ImageFont.truetype(font_path, size=size)
    return ImageFont.load_default()


def iter_polygons(geom):
    if geom is None or geom.is_empty:
        return
    if isinstance(geom, Polygon):
        yield geom
    elif isinstance(geom, MultiPolygon):
        yield from geom.geoms
    elif isinstance(geom, GeometryCollection):
        for part in geom.geoms:
            yield from iter_polygons(part)


def expand_bounds(bounds, ratio=0.35):
    minx, miny, maxx, maxy = bounds
    width = maxx - minx
    height = maxy - miny
    if width == 0:
        width = 0.01
    if height == 0:
        height = 0.01
    pad_x = width * ratio
    pad_y = height * ratio
    return minx - pad_x, miny - pad_y, maxx + pad_x, maxy + pad_y


def fit_bounds_to_image(bounds, image_width, image_height, margin):
    minx, miny, maxx, maxy = bounds
    data_width = maxx - minx
    data_height = maxy - miny
    draw_width = image_width - margin * 2
    draw_height = image_height - margin * 2
    image_ratio = draw_width / draw_height
    data_ratio = data_width / data_height

    if data_ratio > image_ratio:
        new_height = data_width / image_ratio
        delta = (new_height - data_height) / 2
        miny -= delta
        maxy += delta
    else:
        new_width = data_height * image_ratio
        delta = (new_width - data_width) / 2
        minx -= delta
        maxx += delta

    return minx, miny, maxx, maxy


class Projector:
    def __init__(self, bounds, image_width, image_height, margin):
        self.minx, self.miny, self.maxx, self.maxy = bounds
        self.image_width = image_width
        self.image_height = image_height
        self.margin = margin

    def point(self, x, y):
        px = self.margin + (x - self.minx) / (self.maxx - self.minx) * (
            self.image_width - self.margin * 2
        )
        py = self.image_height - self.margin - (y - self.miny) / (self.maxy - self.miny) * (
            self.image_height - self.margin * 2
        )
        return px, py

    def ring(self, coords):
        return [self.point(x, y) for x, y in coords]


def draw_dashed_line(draw, start, end, fill, width=1, dash=8, gap=8):
    x1, y1 = start
    x2, y2 = end
    length = math.hypot(x2 - x1, y2 - y1)
    if length == 0:
        return
    dx = (x2 - x1) / length
    dy = (y2 - y1) / length
    distance = 0
    while distance < length:
        segment_end = min(distance + dash, length)
        sx = x1 + dx * distance
        sy = y1 + dy * distance
        ex = x1 + dx * segment_end
        ey = y1 + dy * segment_end
        draw.line((sx, sy, ex, ey), fill=fill, width=width)
        distance += dash + gap


def draw_grid(draw, projector, bounds, image_width, image_height, margin, grid_step=None):
    minx, miny, maxx, maxy = bounds
    lon_step = grid_step or nice_step((maxx - minx) / 4)
    lat_step = grid_step or nice_step((maxy - miny) / 4)

    lon = math.ceil(minx / lon_step) * lon_step
    while lon < maxx:
        x, _ = projector.point(lon, miny)
        draw_dashed_line(draw, (x, margin), (x, image_height - margin), fill=(185, 185, 185), width=1)
        lon += lon_step

    lat = math.ceil(miny / lat_step) * lat_step
    while lat < maxy:
        _, y = projector.point(minx, lat)
        draw_dashed_line(draw, (margin, y), (image_width - margin, y), fill=(185, 185, 185), width=1)
        lat += lat_step


def nice_step(raw_step):
    if raw_step <= 0:
        return 1
    exponent = math.floor(math.log10(raw_step))
    fraction = raw_step / (10 ** exponent)
    if fraction <= 1:
        nice = 1
    elif fraction <= 2:
        nice = 2
    elif fraction <= 5:
        nice = 5
    else:
        nice = 10
    return nice * (10 ** exponent)


def draw_geometry(draw, projector, geom, outline, fill=None, width=1):
    for polygon in iter_polygons(geom):
        exterior = projector.ring(polygon.exterior.coords)
        if fill is not None:
            draw.polygon(exterior, fill=fill)
        draw.line(exterior + [exterior[0]], fill=outline, width=width, joint="curve")

        for interior in polygon.interiors:
            ring = projector.ring(interior.coords)
            draw.polygon(ring, fill=(250, 250, 250, 255))
            draw.line(ring + [ring[0]], fill=outline, width=max(1, width - 1))


def draw_label(base_image, xy, text, font):
    draw = ImageDraw.Draw(base_image, "RGBA")
    bbox = draw.textbbox((0, 0), text, font=font)
    text_width = bbox[2] - bbox[0]
    text_height = bbox[3] - bbox[1]
    x, y = xy
    pad_x = 8
    pad_y = 5
    rect = (
        x - text_width / 2 - pad_x,
        y - text_height / 2 - pad_y,
        x + text_width / 2 + pad_x,
        y + text_height / 2 + pad_y,
    )
    draw.rounded_rectangle(rect, radius=5, fill=(240, 248, 255, 210), outline=(44, 101, 175, 220), width=1)
    draw.text(
        (x - text_width / 2, y - text_height / 2 - 2),
        text,
        font=font,
        fill=(30, 82, 160, 255),
    )


def layer_intersects_bounds(layer, bounds):
    minx, miny, maxx, maxy = bounds
    extent_geom = box(minx, miny, maxx, maxy)
    try:
        return layer[layer.intersects(extent_geom)]
    except Exception:
        return layer


def draw_layer(draw, projector, layer, outline, fill=None, width=1, bounds=None):
    if layer is None or layer.empty:
        return
    layer_to_draw = layer_intersects_bounds(layer, bounds) if bounds else layer
    for geom in layer_to_draw.geometry:
        draw_geometry(draw, projector, geom, outline=outline, fill=fill, width=width)


def render_region(
    gdf,
    row_index,
    name_field,
    output_dir,
    width,
    height,
    margin,
    mode="simple",
    province_layers=None,
    city_layers=None,
    county_layers=None,
    coverage_layers=None,
    grid_step=None,
    extent_pad_x=2.0,
    extent_pad_y=1.0,
):
    row = gdf.iloc[row_index]
    target_geom = row.geometry
    if target_geom is None or target_geom.is_empty:
        return None

    region_name = get_region_name(row, name_field, row_index)
    output_name = f"{safe_filename(region_name)}.png"
    output_path = Path(output_dir) / output_name

    province_layers = province_layers or []
    city_layers = city_layers or []
    county_layers = county_layers or []
    coverage_layers = coverage_layers or []

    if mode == "map":
        source_bounds = total_bounds_for_layers(coverage_layers) or target_geom.bounds
        draw_bounds = (
            source_bounds[0] - extent_pad_x,
            source_bounds[1] - extent_pad_y,
            source_bounds[2] + extent_pad_x,
            source_bounds[3] + extent_pad_y,
        )
    else:
        target_bounds = expand_bounds(target_geom.bounds, ratio=1.2)
        all_bounds = gdf.total_bounds
        clipped_bounds = (
            max(target_bounds[0], all_bounds[0]),
            max(target_bounds[1], all_bounds[1]),
            min(target_bounds[2], all_bounds[2]),
            min(target_bounds[3], all_bounds[3]),
        )

        if clipped_bounds[0] >= clipped_bounds[2] or clipped_bounds[1] >= clipped_bounds[3]:
            clipped_bounds = target_bounds

        draw_bounds = expand_bounds(clipped_bounds, ratio=0.15)

    draw_bounds = fit_bounds_to_image(draw_bounds, width, height, margin)
    projector = Projector(draw_bounds, width, height, margin)

    image = Image.new("RGBA", (width, height), (250, 250, 250, 255))
    draw = ImageDraw.Draw(image, "RGBA")

    draw_grid(draw, projector, draw_bounds, width, height, margin, grid_step=grid_step)

    coverage_overlay = Image.new("RGBA", image.size, (0, 0, 0, 0))
    coverage_draw = ImageDraw.Draw(coverage_overlay, "RGBA")

    if coverage_layers:
        for coverage_layer in coverage_layers:
            draw_layer(
                coverage_draw,
                projector,
                coverage_layer,
                outline=(102, 190, 102, 65),
                fill=(115, 220, 115, 42),
                width=2,
                bounds=draw_bounds,
            )
    elif mode == "simple":
        coverage = box(*expand_bounds(target_geom.bounds, ratio=0.35))
        draw_geometry(
            coverage_draw,
            projector,
            coverage,
            outline=(102, 190, 102, 65),
            fill=(115, 220, 115, 38),
            width=2,
        )

    image = Image.alpha_composite(image, coverage_overlay)
    draw = ImageDraw.Draw(image, "RGBA")

    if mode == "map":
        for county_layer in county_layers:
            draw_layer(draw, projector, county_layer, outline=(220, 220, 220, 190), fill=None, width=1, bounds=draw_bounds)
        for city_layer in city_layers:
            draw_layer(draw, projector, city_layer, outline=(120, 120, 120, 215), fill=None, width=2, bounds=draw_bounds)
        for province_layer in province_layers:
            draw_layer(draw, projector, province_layer, outline=(40, 40, 40, 230), fill=None, width=2, bounds=draw_bounds)
    else:
        for idx, geom in enumerate(gdf.geometry):
            if idx == row_index:
                continue
            draw_geometry(draw, projector, geom, outline=(210, 210, 210, 180), fill=None, width=1)

    target_overlay = Image.new("RGBA", image.size, (0, 0, 0, 0))
    target_draw = ImageDraw.Draw(target_overlay, "RGBA")
    draw_geometry(target_draw, projector, target_geom, outline=(45, 45, 45, 210), fill=(120, 210, 130, 18), width=2)
    image = Image.alpha_composite(image, target_overlay)
    draw = ImageDraw.Draw(image, "RGBA")
    draw_geometry(draw, projector, target_geom, outline=(35, 95, 175, 255), fill=None, width=3)

    centroid = target_geom.representative_point()
    draw_label(image, projector.point(centroid.x, centroid.y), region_name, load_font(28))

    draw.rectangle((margin, margin, width - margin, height - margin), outline=(35, 35, 35, 220), width=2)

    image.convert("RGB").save(output_path, quality=95)
    return output_path, False


def parse_args():
    parser = argparse.ArgumentParser(description="绘制 shp 目录下每一个区域并保存为 PNG")
    parser.add_argument("--shp-dir", default="./shp", help="输入 shp/geojson 根目录")
    parser.add_argument("--output-dir", default="./img", help="图片输出目录")
    parser.add_argument("--mode", choices=("simple", "map"), default="map", help="simple 为普通模式，map 为地图模式")
    parser.add_argument("--name-field", help="用于图片文件名和标注的字段名，默认自动识别")
    parser.add_argument("--province-layer", action="append", default=[], help="省级边界图层，可重复指定")
    parser.add_argument("--city-layer", action="append", default=[], help="市级边界图层，可重复指定")
    parser.add_argument("--county-layer", action="append", default=[], help="县级边界图层，可重复指定")
    parser.add_argument("--coverage-layer", action="append", default=[], help="影像覆盖范围图层，可重复指定")
    parser.add_argument("--no-auto-layers", action="store_true", help="不自动加载 ./00县边界 和 ./00市边界")
    parser.add_argument("--grid-step", type=float, help="经纬网间隔，例如 2")
    parser.add_argument("--extent-pad-x", type=float, default=2.0, help="地图模式下东西方向范围扩展，经度单位")
    parser.add_argument("--extent-pad-y", type=float, default=1.0, help="地图模式下南北方向范围扩展，纬度单位")
    parser.add_argument("--width", type=int, default=1300, help="输出图片宽度")
    parser.add_argument("--height", type=int, default=900, help="输出图片高度")
    parser.add_argument("--margin", type=int, default=28, help="地图边距")
    parser.add_argument("--overwrite", action="store_true", help="覆盖已存在图片")
    return parser.parse_args()


def main():
    args = parse_args()
    add_default_map_layers(args)
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    removed = cleanup_numbered_images(output_dir)
    for image_file in removed:
        print(f"已清理旧编号图片: {image_file}")

    gdf = read_all_regions(args.shp_dir)
    gdf = gdf[gdf.geometry.notna() & ~gdf.geometry.is_empty].reset_index(drop=True)
    if gdf.empty:
        raise ValueError("没有可绘制的几何")

    name_field = choose_name_field(gdf, args.name_field)
    province_layers = read_optional_layers(args.province_layer)
    city_layers = read_optional_layers(args.city_layer)
    county_layers = read_optional_layers(args.county_layer)
    coverage_layers = read_optional_layers(args.coverage_layer)

    print(f"读取区域数量: {len(gdf)}")
    print(f"标注字段: {name_field or '自动编号'}")
    if args.mode == "map":
        print(
            "地图模式图层: "
            f"省级 {len(province_layers)}，市级 {len(city_layers)}，"
            f"县级 {len(county_layers)}，覆盖范围 {len(coverage_layers)}"
        )

    saved = []
    skipped = []
    for idx in range(len(gdf)):
        region_name = get_region_name(gdf.iloc[idx], name_field, idx)
        output_path = output_dir / f"{safe_filename(region_name)}.png"
        if output_path.exists() and not args.overwrite:
            skipped.append(output_path)
            print(f"已存在，跳过: {output_path}")
            continue

        output_path = render_region(
            gdf=gdf,
            row_index=idx,
            name_field=name_field,
            output_dir=output_dir,
            width=args.width,
            height=args.height,
            margin=args.margin,
            mode=args.mode,
            province_layers=province_layers,
            city_layers=city_layers,
            county_layers=county_layers,
            coverage_layers=coverage_layers,
            grid_step=args.grid_step,
            extent_pad_x=args.extent_pad_x,
            extent_pad_y=args.extent_pad_y,
        )
        if output_path:
            if isinstance(output_path, tuple):
                output_path, was_skipped = output_path
            else:
                was_skipped = False
            if was_skipped:
                skipped.append(output_path)
                print(f"已存在，跳过: {output_path}")
            else:
                saved.append(output_path)
                print(f"已保存: {output_path}")

    print(f"完成，新保存 {len(saved)} 张，跳过 {len(skipped)} 张，输出目录 {output_dir}")


if __name__ == "__main__":
    main()
