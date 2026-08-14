#!/usr/bin/env python
"""从 5 万分幅索引并行提取逐幅掩膜 SHP。"""

from __future__ import annotations

import argparse
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path
import re
import sys
import tomllib

import geopandas as gpd
import shapely

from apply_sqlite_alignment_to_shp import (
    city_database,
    load_run,
    remove_shapefile,
    shapefile_exists,
)


ROOT = Path(__file__).resolve().parent
DEFAULT_CONFIG = ROOT / "sqlite_pipeline.toml"


def load_config(path: Path) -> tuple[dict, dict, Path]:
    resolved = path.resolve()
    if not resolved.is_file():
        raise FileNotFoundError(f"配置文件不存在：{resolved}")
    with resolved.open("rb") as stream:
        document = tomllib.load(stream)
    mosaic = document.get("mosaic")
    shp = document.get("shp")
    if not isinstance(mosaic, dict) or not isinstance(shp, dict):
        raise ValueError("配置文件必须包含 [mosaic] 和 [shp] 段。")
    return mosaic, shp, resolved.parent


def configured_path(values: dict, key: str, base: Path, default: str) -> Path:
    value = Path(str(values.get(key, default)))
    return value if value.is_absolute() else base / value


def configured_cities(values: dict) -> list[str]:
    value = values.get("cities", [])
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ValueError("cities 必须是城市字符串或字符串数组。")
    return list(dict.fromkeys(item.strip() for item in value if item.strip()))


def safe_name(value: str) -> str:
    result = re.sub(r'[<>:"/\\|?*]+', "_", value).strip(" .")
    if not result:
        raise ValueError("图幅号不能用于生成文件名。")
    return result


def index_name(source_id: str, image_suffix: str) -> str:
    suffix_stem = Path(image_suffix).stem
    if suffix_stem and source_id.endswith(suffix_stem):
        return source_id[: -len(suffix_stem)]
    return source_id


def write_mask_worker(
    source_id: str,
    tile_name: str,
    geometry_wkb: bytes,
    crs_wkt: str,
    output_path_text: str,
    overwrite: bool,
) -> tuple[str, str]:
    output_path = Path(output_path_text)
    if shapefile_exists(output_path) and not overwrite:
        return source_id, "已存在，复用"
    if overwrite:
        remove_shapefile(output_path)
    geometry = shapely.from_wkb(geometry_wkb)
    geometry = shapely.make_valid(
        geometry,
        method="structure",
        keep_collapsed=False,
    )
    if geometry is None or geometry.is_empty:
        raise ValueError(f"{source_id} 的分幅几何为空。")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    gpd.GeoDataFrame(
        {
            "PLANE_NAME": [tile_name],
            "source_id": [source_id],
            "geometry": [geometry],
        },
        geometry="geometry",
        crs=crs_wkt,
    ).to_file(output_path, driver="ESRI Shapefile", encoding="UTF-8")
    if not shapefile_exists(output_path):
        raise RuntimeError(f"掩膜 SHP 写出不完整：{output_path}")
    return source_id, "提取完成"


def parse_args() -> argparse.Namespace:
    preliminary = argparse.ArgumentParser(add_help=False)
    preliminary.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    known, _ = preliminary.parse_known_args()
    mosaic, shp, base = load_config(known.config)

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=known.config)
    parser.add_argument("--cities", nargs="*", default=configured_cities(mosaic))
    parser.add_argument(
        "--tile-index",
        type=Path,
        default=configured_path(
            mosaic, "tile_index", base, "市边界和分幅/5w分幅成果结合表.shp"
        ),
    )
    parser.add_argument(
        "--tile-field",
        default=str(mosaic.get("tile_field", "PLANE_NAME")),
    )
    parser.add_argument(
        "--image-suffix",
        default=str(mosaic.get("image_suffix", "_2025.tif")),
    )
    parser.add_argument(
        "--person-shp-dir",
        type=Path,
        default=configured_path(shp, "shp_dir_person", base, "input_person_shp"),
    )
    parser.add_argument(
        "--database-dir",
        type=Path,
        default=configured_path(shp, "database_dir", base, "output/database"),
    )
    parser.add_argument(
        "--mask-dir",
        type=Path,
        default=configured_path(shp, "mask_dir", base, "output/掩膜文件"),
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=32,
        help="逐幅提取的最大 Python 子进程数，默认 32。",
    )
    parser.add_argument("--check-only", action="store_true")
    parser.add_argument("--overwrite", action="store_true")
    args = parser.parse_args()
    args.cities = list(dict.fromkeys(city.strip() for city in args.cities if city.strip()))
    if not args.cities:
        parser.error("至少需要一个城市。")
    if args.workers <= 0:
        parser.error("--workers 必须大于 0。")
    return args


def main() -> None:
    args = parse_args()
    tile_index_path = args.tile_index.resolve()
    person_dir = args.person_shp_dir.resolve()
    database_dir = args.database_dir.resolve()
    mask_dir = args.mask_dir.resolve()
    if not tile_index_path.is_file():
        raise FileNotFoundError(f"5 万分幅索引不存在：{tile_index_path}")
    if not person_dir.is_dir():
        raise FileNotFoundError(f"人工输入 SHP 目录不存在：{person_dir}")

    source_ids: list[str] = []
    for city in args.cities:
        database = city_database(database_dir, city)
        _, rows, _ = load_run(database, None, city)
        source_ids.extend(str(row["source_id"]) for row in rows)
    source_ids = list(dict.fromkeys(source_ids))

    missing_person = [
        source_id
        for source_id in source_ids
        if not shapefile_exists(person_dir / f"{source_id}.shp")
    ]
    if missing_person:
        detail = "、".join(missing_person[:20])
        if len(missing_person) > 20:
            detail += f" 等 {len(missing_person)} 幅"
        raise FileNotFoundError(f"缺少对应人工输入 SHP：{detail}")

    index = gpd.read_file(tile_index_path)
    if index.crs is None:
        raise ValueError(f"5 万分幅索引缺少 CRS：{tile_index_path}")
    if args.tile_field not in index.columns:
        raise ValueError(f"5 万分幅索引缺少字段：{args.tile_field}")
    names = index[args.tile_field].astype(str).str.strip()
    lookup: dict[str, object] = {}
    duplicate_names: set[str] = set()
    for name, geometry in zip(names, index.geometry):
        if not name or geometry is None or geometry.is_empty:
            continue
        if name in lookup:
            duplicate_names.add(name)
        else:
            lookup[name] = geometry
    if duplicate_names:
        requested_duplicates = sorted(
            name
            for name in duplicate_names
            if name in {index_name(source, args.image_suffix) for source in source_ids}
        )
        if requested_duplicates:
            raise ValueError(
                f"分幅索引中的图幅号不唯一：{'、'.join(requested_duplicates)}"
            )

    missing_index = [
        source_id
        for source_id in source_ids
        if index_name(source_id, args.image_suffix) not in lookup
    ]
    if missing_index:
        raise ValueError(
            "5 万分幅索引中找不到图幅：" + "、".join(missing_index)
        )

    worker_count = min(args.workers, len(source_ids))
    print(
        f"掩膜子图幅提取：{len(args.cities)} 个城市，去重后 {len(source_ids)} 幅；"
        f"启动 {worker_count} 个 Python 子进程；输出 {mask_dir}",
        flush=True,
    )
    if args.check_only:
        print("检查完成：人工输入 SHP、城市数据库及 5 万分幅索引均已匹配。")
        return

    mask_dir.mkdir(parents=True, exist_ok=True)
    crs_wkt = index.crs.to_wkt()
    failures: list[tuple[str, Exception]] = []
    with ProcessPoolExecutor(max_workers=worker_count) as executor:
        futures = {}
        for source_id in source_ids:
            tile_name = index_name(source_id, args.image_suffix)
            geometry_wkb = shapely.to_wkb(lookup[tile_name])
            output_path = mask_dir / f"{safe_name(source_id)}.shp"
            future = executor.submit(
                write_mask_worker,
                source_id,
                tile_name,
                geometry_wkb,
                crs_wkt,
                str(output_path),
                args.overwrite,
            )
            futures[future] = source_id

        completed = 0
        for future in as_completed(futures):
            source_id = futures[future]
            completed += 1
            try:
                _, status = future.result()
                print(
                    f"  [图幅 {completed}/{len(source_ids)}] {source_id}：{status}",
                    flush=True,
                )
            except Exception as exc:
                failures.append((source_id, exc))
                print(f"  [{source_id}] 提取失败：{exc}", file=sys.stderr, flush=True)
    if failures:
        detail = "；".join(f"{source}: {error}" for source, error in failures)
        raise RuntimeError(f"{len(failures)} 幅掩膜提取失败：{detail}")
    print(f"全部 {len(source_ids)} 幅原始掩膜 SHP 提取完成。", flush=True)


if __name__ == "__main__":
    main()
