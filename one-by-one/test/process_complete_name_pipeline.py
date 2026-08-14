#!/usr/bin/env python3
"""处理完整 Sentinel 产品名 TIFF 的多线程市县流水线。

默认输入：E:\\complete_name_tif
默认输出：E:\\sentinel_data、E:\\city_data、E:\\country_data

每个线程独立负责一个“成像日期 + 卫星号”分组的完整流程：
整理 Sentinel TIFF -> 市级合并裁剪 -> 县级裁剪 -> 生成全部 JPEG。
"""

from __future__ import annotations

import argparse
import re
import shutil
import sys
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path

import process_image_to_city_country as core


BASE_DIR = Path(__file__).resolve().parent
DEFAULT_INPUT_ROOT = Path(r"E:\complete_name_tif")
DEFAULT_SENTINEL_ROOT = Path(r"E:\sentinel_data")
DEFAULT_CITY_ROOT = Path(r"E:\city_data")
DEFAULT_COUNTRY_ROOT = Path(r"E:\country_data")
DEFAULT_CITY_LAYER = BASE_DIR / "00市边界" / "15_市边界.shp"
DEFAULT_COUNTY_LAYER = BASE_DIR / "00县边界" / "15_县边界.shp"

PRODUCT_RE = re.compile(
    r"^(?P<satellite>S2[A-Z])_"
    r"(?P<level>MSIL[12][AC])_"
    r"(?P<sensing>\d{8}T\d{6})_"
    r"(?P<baseline>N\d{4})_"
    r"(?P<orbit>R\d{3})_"
    r"(?P<tile>T\d{2}[A-Z]{3})_"
    r"(?P<generation>\d{8}T\d{6})"
    r"(?:\.SAFE)?\.tif$",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class ProductInfo:
    raster: core.RasterInfo
    satellite: str
    level: str
    sensing: str
    baseline: str
    orbit: str
    generation: str

    @property
    def date(self) -> str:
        return self.sensing[:8]

    @property
    def scene(self) -> str:
        return f"T{self.sensing[9:]}"

    @property
    def group_key(self) -> tuple[str, str]:
        return self.date, self.satellite

    @property
    def city_prefix(self) -> str:
        return (
            f"{self.satellite}_{self.level}_{self.sensing}_"
            f"{self.baseline}_{self.orbit}"
        )

    @property
    def sentinel_name(self) -> str:
        name = self.raster.path.name
        return re.sub(r"\.SAFE(?=\.tif$)", "", name, flags=re.IGNORECASE)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="按日期和卫星号多线程处理完整产品名 TIFF。"
    )
    parser.add_argument("--input-root", type=Path, default=DEFAULT_INPUT_ROOT)
    parser.add_argument("--sentinel-root", type=Path, default=DEFAULT_SENTINEL_ROOT)
    parser.add_argument("--city-root", type=Path, default=DEFAULT_CITY_ROOT)
    parser.add_argument("--country-root", type=Path, default=DEFAULT_COUNTRY_ROOT)
    parser.add_argument("--city-layer", type=Path, default=DEFAULT_CITY_LAYER)
    parser.add_argument("--county-layer", type=Path, default=DEFAULT_COUNTY_LAYER)
    parser.add_argument("--city-name-field", default="市名称")
    parser.add_argument("--city-code-field", default="市代码")
    parser.add_argument("--county-name-field", default="area_name")
    parser.add_argument("--county-code-field", default="area_code")
    parser.add_argument("--resolution", type=float, default=10.0)
    parser.add_argument(
        "--max-workers",
        type=int,
        default=2,
        help="完整流水线线程数，默认 2；每个线程负责一个日期+卫星分组",
    )
    parser.add_argument("--all-touched", action="store_true")
    parser.add_argument("--overwrite", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    return parser.parse_args()


def scan_products(input_root: Path, boundary_crs, d: dict):
    rasterio = d["rasterio"]
    transform_bounds = d["transform_bounds"]
    box = d["box"]
    candidates = sorted(input_root.rglob("*.tif"))
    products: list[ProductInfo] = []
    invalid: list[Path] = []
    core.log(f"开始扫描完整产品名 TIFF：候选文件 {len(candidates)} 个")

    for index, path in enumerate(candidates, 1):
        core.log(f"[扫描 {index}/{len(candidates)}] {path.name}")
        if not path.is_file() or path.name.startswith("."):
            continue
        match = PRODUCT_RE.fullmatch(path.name)
        if match is None:
            invalid.append(path)
            core.log(f"文件名不匹配，跳过：{path.name}", "WARN")
            continue
        with rasterio.Env(GDAL_PAM_ENABLED="NO"), rasterio.open(path) as source:
            if source.crs is None:
                raise ValueError(f"输入 TIFF 没有坐标系：{path}")
            projected_bounds = transform_bounds(
                source.crs,
                boundary_crs,
                *source.bounds,
                densify_pts=21,
            )
            raster = core.RasterInfo(
                path=path,
                tile=match.group("tile").upper(),
                sensing_time=match.group("sensing"),
                crs=source.crs,
                bounds=tuple(source.bounds),
                boundary_footprint=box(*projected_bounds),
            )
        product = ProductInfo(
            raster=raster,
            satellite=match.group("satellite").upper(),
            level=match.group("level").upper(),
            sensing=match.group("sensing"),
            baseline=match.group("baseline").upper(),
            orbit=match.group("orbit").upper(),
            generation=match.group("generation"),
        )
        products.append(product)
        core.log(
            f"[扫描 {index}/{len(candidates)}] 已识别："
            f"{product.date} + {product.satellite} + {raster.tile}"
        )
    core.log(f"扫描结束：有效 {len(products)}，不匹配 {len(invalid)}")
    return products, invalid


def group_products(products: list[ProductInfo]):
    groups: dict[tuple[str, str], list[ProductInfo]] = {}
    for product in products:
        groups.setdefault(product.group_key, []).append(product)
    return [
        (key, tuple(sorted(items, key=lambda item: item.raster.path.name)))
        for key, items in sorted(groups.items())
    ]


def matching_cities(products: tuple[ProductInfo, ...], cities):
    matches = []
    for _, city in cities.iterrows():
        selected = tuple(
            product
            for product in products
            if product.raster.boundary_footprint.intersects(city.geometry)
        )
        if selected:
            matches.append((city, selected))
    return matches


def install_sentinel(
    product: ProductInfo,
    city_name: str,
    sentinel_root: Path,
    overwrite: bool,
    d: dict,
) -> tuple[Path, str]:
    rasterio = d["rasterio"]
    output = sentinel_root / core.safe_name(city_name) / product.sentinel_name
    if output.exists() and not overwrite:
        with rasterio.Env(GDAL_PAM_ENABLED="NO"), rasterio.open(output, "r+") as dst:
            dst.nodata = 0
        if not output.with_suffix(".jpeg").exists():
            core.create_minmax_jpeg(output, d)
        return output, "existing"

    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_name(
        f".{output.stem}.{uuid.uuid4().hex}.tmp{output.suffix}"
    )
    try:
        core.log(f"复制 Sentinel TIFF：{product.raster.path} -> {output}")
        shutil.copy2(product.raster.path, temporary)
        with rasterio.Env(GDAL_PAM_ENABLED="NO"), rasterio.open(temporary, "r+") as dst:
            dst.nodata = 0
        temporary.replace(output)
        core.create_minmax_jpeg(output, d)
        return output, "created"
    finally:
        if temporary.exists():
            temporary.unlink()


def installed_raster(product: ProductInfo, installed_path: Path) -> core.RasterInfo:
    source = product.raster
    return core.RasterInfo(
        path=installed_path,
        tile=source.tile,
        sensing_time=source.sensing_time,
        crs=source.crs,
        bounds=source.bounds,
        boundary_footprint=source.boundary_footprint,
    )


def choose_group_metadata(products: tuple[ProductInfo, ...]):
    prefixes = sorted({product.city_prefix for product in products})
    selected = min(products, key=lambda product: (product.sensing, product.city_prefix))
    if len(prefixes) > 1:
        core.log(
            f"同一日期+卫星分组包含 {len(prefixes)} 个产品公共前缀；"
            f"合并输出使用最早前缀 {selected.city_prefix}",
            "WARN",
        )
    return selected


def process_group(
    group_number: int,
    total_groups: int,
    key: tuple[str, str],
    products: tuple[ProductInfo, ...],
    cities,
    counties,
    args: argparse.Namespace,
    d: dict,
):
    started_at = time.monotonic()
    date, satellite = key
    core.log(
        f"[完整流程 {group_number}/{total_groups}] 开始："
        f"日期 {date}，卫星 {satellite}，输入 TIFF {len(products)} 个"
    )
    city_matches = matching_cities(products, cities)
    core.log(
        f"[完整流程 {group_number}/{total_groups}] 空间匹配城市 {len(city_matches)} 个"
    )
    result = {
        "key": key,
        "sentinel_created": 0,
        "sentinel_existing": 0,
        "city_created": 0,
        "city_existing": 0,
        "city_no_coverage": 0,
        "country_created": 0,
        "country_existing": 0,
        "country_no_coverage": 0,
    }

    for city_number, (city, selected_products) in enumerate(city_matches, 1):
        city_name = city["_city_name"]
        city_code = city["_city_code"]
        core.log(
            f"[流程 {group_number}/{total_groups}] "
            f"[城市 {city_number}/{len(city_matches)}] 开始 {city_name}（{city_code}），"
            f"相关瓦片 {len(selected_products)} 个"
        )
        installed_infos = []
        for sentinel_number, product in enumerate(selected_products, 1):
            core.log(
                f"[流程 {group_number}/{total_groups}] "
                f"[城市 {city_number}/{len(city_matches)}] "
                f"[Sentinel {sentinel_number}/{len(selected_products)}] "
                f"{product.raster.path.name}"
            )
            output, status = install_sentinel(
                product,
                city_name,
                args.sentinel_root,
                args.overwrite,
                d,
            )
            result[f"sentinel_{status}"] += 1
            installed_infos.append(installed_raster(product, output))

        metadata = choose_group_metadata(selected_products)
        task = core.CityTask(
            city_name=city_name,
            city_code=city_code,
            city_geometry=city.geometry,
            sensing_time=metadata.sensing,
            sources=tuple(installed_infos),
        )
        city_output = (
            args.city_root
            / core.safe_name(city_name)
            / f"{metadata.city_prefix}_MERGED_{city_code}.tif"
        )
        city_status = core.merge_city_task(
            task,
            city_output,
            cities.crs,
            args.resolution,
            args.overwrite,
            args.all_touched,
            d,
        )
        result[f"city_{city_status}"] += 1
        if city_status == "no_coverage":
            continue

        selected_counties = counties[counties["_city_code"] == city_code]
        county_total = len(selected_counties)
        for county_number, (_, county) in enumerate(selected_counties.iterrows(), 1):
            county_name = county["_county_name"]
            county_code = county["_county_code"]
            core.log(
                f"[流程 {group_number}/{total_groups}] "
                f"[城市 {city_number}/{len(city_matches)}] "
                f"[县 {county_number}/{county_total}] "
                f"{county_name}（{county_code}）"
            )
            country_output = (
                args.country_root
                / core.safe_name(city_name)
                / core.safe_name(county_name)
                / (
                    f"CQDOM{county_code}_{metadata.date}_{metadata.scene}_"
                    f"{metadata.satellite}_10m.tif"
                )
            )
            country_status = core.clip_county(
                city_output,
                country_output,
                county.geometry,
                counties.crs,
                county_name,
                county_code,
                metadata.sensing,
                args.overwrite,
                args.all_touched,
                d,
            )
            result[f"country_{country_status}"] += 1
            core.log(
                f"[流程 {group_number}/{total_groups}] "
                f"[县 {county_number}/{county_total}] 完成，状态 {country_status}"
            )

        core.log(
            f"[流程 {group_number}/{total_groups}] "
            f"[城市 {city_number}/{len(city_matches)}] 完成 {city_name}"
        )

    core.log(
        f"[完整流程 {group_number}/{total_groups}] 完成 {date}+{satellite}，"
        f"耗时 {core.elapsed_text(started_at)}"
    )
    return result


def main() -> int:
    run_started_at = time.monotonic()
    args = parse_args()
    if args.max_workers < 1 or args.resolution <= 0:
        print("错误：max-workers 和 resolution 必须大于 0", file=sys.stderr)
        return 2
    for name in ("input_root", "sentinel_root", "city_root", "country_root"):
        setattr(args, name, getattr(args, name).expanduser().resolve())
    if not args.input_root.is_dir():
        print(f"错误：输入目录不存在：{args.input_root}", file=sys.stderr)
        return 1

    boundary_args = argparse.Namespace(
        city_layer=args.city_layer,
        county_layer=args.county_layer,
        city_name_field=args.city_name_field,
        city_code_field=args.city_code_field,
        county_name_field=args.county_name_field,
        county_code_field=args.county_code_field,
    )
    try:
        d = core.require_dependencies()
        cities, counties = core.load_boundaries(boundary_args, d)
        products, invalid = scan_products(args.input_root, cities.crs, d)
        groups = group_products(products)
    except Exception as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 1

    core.log(
        f"任务统计：有效 TIFF {len(products)}，不匹配 {len(invalid)}，"
        f"日期+卫星完整流程 {len(groups)} 个，线程数 {args.max_workers}"
    )
    if args.dry_run:
        for group_number, (key, items) in enumerate(groups, 1):
            matches = matching_cities(items, cities)
            city_names = "、".join(city["_city_name"] for city, _ in matches) or "无"
            core.log(
                f"[预览 {group_number}/{len(groups)}] {key[0]}+{key[1]}："
                f"TIFF {len(items)}，匹配城市 {len(matches)}（{city_names}）"
            )
        core.log("仅预览，未写入任何 TIFF 或 JPEG")
        return 0

    totals = {
        "sentinel_created": 0,
        "sentinel_existing": 0,
        "city_created": 0,
        "city_existing": 0,
        "city_no_coverage": 0,
        "country_created": 0,
        "country_existing": 0,
        "country_no_coverage": 0,
    }
    failed = completed = 0

    with ThreadPoolExecutor(max_workers=args.max_workers) as executor:
        futures = {
            executor.submit(
                process_group,
                group_number,
                len(groups),
                key,
                items,
                cities,
                counties,
                args,
                d,
            ): (group_number, key)
            for group_number, (key, items) in enumerate(groups, 1)
        }
        try:
            for future in as_completed(futures):
                group_number, key = futures[future]
                completed += 1
                try:
                    result = future.result()
                    for name in totals:
                        totals[name] += result[name]
                    core.log(
                        f"[总体 {completed}/{len(groups)}] 完整流程成功："
                        f"{key[0]}+{key[1]}"
                    )
                except Exception as exc:
                    failed += 1
                    core.log(
                        f"[总体 {completed}/{len(groups)}] 完整流程失败："
                        f"{key[0]}+{key[1]}：{exc}",
                        "ERROR",
                    )
        except KeyboardInterrupt:
            for future in futures:
                future.cancel()
            core.log("收到停止信号，正在结束线程", "WARN")
            return 130

    core.log(
        "全部处理完成："
        f"Sentinel 新建 {totals['sentinel_created']}、已有 {totals['sentinel_existing']}；"
        f"市级新建 {totals['city_created']}、已有 {totals['city_existing']}、"
        f"无覆盖 {totals['city_no_coverage']}；"
        f"县级新建 {totals['country_created']}、已有 {totals['country_existing']}、"
        f"无覆盖 {totals['country_no_coverage']}；"
        f"失败流程 {failed}；总耗时 {core.elapsed_text(run_started_at)}"
    )
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
