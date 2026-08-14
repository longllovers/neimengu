#!/usr/bin/env python
"""用人工图幅覆盖掩膜提取模型 SHP，补充人工 SHP 的未覆盖区域。

人工数据始终是主体。逐幅掩膜由 ``extract_mask_shp_from_tile_index.py``
从 5 万分幅索引提取，再由 ``apply_sqlite_alignment_to_mask_shp.py`` 按与
人工 SHP 相同的流程矫正、合成和市界裁剪。本脚本只读取该正式掩膜，并
补入“模型图斑减去掩膜”后的部分。
"""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
import json
from pathlib import Path
import re
import sys
import tomllib

import geopandas as gpd
import numpy as np
import pandas as pd
import shapely
from shapely.geometry import Polygon


ROOT = Path(__file__).resolve().parent
DEFAULT_CONFIG = ROOT / "sqlite_pipeline.toml"


def load_sections(path: Path) -> tuple[dict, dict, Path]:
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


def safe_city_name(value: str) -> str:
    result = re.sub(r'[<>:"/\\|?*]+', "_", value).strip(" .")
    if not result:
        raise ValueError("城市名称不能用于生成文件名。")
    return result


def shapefile_exists(path: Path) -> bool:
    return all(path.with_suffix(suffix).is_file() for suffix in (".shp", ".shx", ".dbf"))


def remove_shapefile(path: Path) -> None:
    for suffix in (".shp", ".shx", ".dbf", ".prj", ".cpg", ".qix"):
        sidecar = path.with_suffix(suffix)
        if sidecar.exists():
            sidecar.unlink()


def polygonal_only(geometry):
    if geometry is None or geometry.is_empty:
        return Polygon()
    geometry = shapely.make_valid(
        geometry,
        method="structure",
        keep_collapsed=False,
    )
    if shapely.get_type_id(geometry) in (3, 6):
        return geometry
    parts = [
        part
        for part in shapely.get_parts(geometry)
        if shapely.get_type_id(part) in (3, 6) and not part.is_empty
    ]
    return shapely.union_all(np.asarray(parts, dtype=object)) if parts else Polygon()


def supplement_city(
    city: str,
    city_output_dir: Path,
    search_distance: float,
    min_area: float,
    source_field: str,
    overwrite: bool,
    check_only: bool,
) -> Path:
    safe_name = safe_city_name(city)
    city_dir = city_output_dir.resolve() / safe_name
    model_path = city_dir / f"{safe_name}_模型_市界裁剪.shp"
    person_path = city_dir / f"{safe_name}_人工_市界裁剪.shp"
    mask_path = city_dir / f"{safe_name}_掩膜_市界裁剪.shp"
    output_path = city_dir / f"{safe_name}_人工_模型补缝_市界裁剪.shp"

    for path, label in (
        (model_path, "模型 SHP"),
        (person_path, "人工 SHP"),
        (mask_path, "人工覆盖掩膜 SHP"),
    ):
        if not shapefile_exists(path):
            raise FileNotFoundError(f"{label} 不完整或不存在：{path}")
    if shapefile_exists(output_path) and not overwrite and not check_only:
        raise FileExistsError(f"补缝输出已存在：{output_path}；覆盖请设置 overwrite=true。")

    print(
        f"\n[{city}] 模型补人工接缝检查\n"
        f"  模型：{model_path}\n"
        f"  人工：{person_path}\n"
        f"  掩膜：{mask_path}\n"
        f"  输出：{output_path}\n"
        f"  方式：模型图斑减人工图幅覆盖掩膜；最小补充面积：{min_area}",
        flush=True,
    )
    if check_only:
        return output_path

    person = gpd.read_file(person_path)
    model = gpd.read_file(model_path)
    mask = gpd.read_file(mask_path)
    if person.crs is None or model.crs is None or mask.crs is None:
        raise ValueError(f"{city} 的模型、人工或掩膜 SHP 缺少 CRS。")
    if model.crs != person.crs:
        model = model.to_crs(person.crs)
    if mask.crs != person.crs:
        mask = mask.to_crs(person.crs)

    person = person[person.geometry.notna() & ~person.geometry.is_empty].copy()
    model = model[model.geometry.notna() & ~model.geometry.is_empty].copy()
    mask = mask[mask.geometry.notna() & ~mask.geometry.is_empty].copy()
    if person.empty or model.empty or mask.empty:
        raise ValueError(f"{city} 的模型、人工或掩膜 SHP 没有有效几何。")
    if not person.geometry.is_valid.all():
        person.geometry = person.geometry.make_valid()
    if not model.geometry.is_valid.all():
        model.geometry = model.geometry.make_valid()
    if not mask.geometry.is_valid.all():
        mask.geometry = mask.geometry.make_valid()
    coverage_mask = polygonal_only(shapely.union_all(mask.geometry.to_numpy()))
    if coverage_mask.is_empty:
        raise ValueError(f"{city} 的正式人工覆盖掩膜为空。")
    supplement_records: list[dict] = []
    supplement_area = 0.0
    residual_parts = 0
    model_columns = [column for column in model.columns if column != model.geometry.name]
    person_columns = [column for column in person.columns if column != person.geometry.name]

    for position, (_, model_row) in enumerate(model.iterrows(), start=1):
        model_geometry = polygonal_only(model_row.geometry)
        if model_geometry.is_empty:
            continue
        residual = polygonal_only(shapely.difference(model_geometry, coverage_mask))
        if residual.is_empty:
            continue

        for part in shapely.get_parts(residual):
            part = polygonal_only(part)
            if part.is_empty or float(shapely.area(part)) < min_area:
                continue
            residual_parts += 1
            candidate = part

            record = {column: None for column in person_columns}
            for column in model_columns:
                if column in record:
                    record[column] = model_row[column]
            record["geometry"] = candidate
            record["fill_src"] = "model_gap"
            supplement_records.append(record)
            supplement_area += float(shapely.area(candidate))

        if position % 5000 == 0 or position == len(model):
            print(
                f"  [{city}] 已检查模型图斑 {position}/{len(model)}；"
                f"确认补充 {len(supplement_records)} 块",
                flush=True,
            )

    person["fill_src"] = "person"
    if supplement_records:
        supplement = gpd.GeoDataFrame(
            supplement_records,
            geometry="geometry",
            crs=person.crs,
        )
        for column in person.columns:
            if column not in supplement.columns:
                supplement[column] = None
        supplement = supplement[person.columns]
        result = gpd.GeoDataFrame(
            pd.concat([person, supplement], ignore_index=True, sort=False),
            geometry=person.geometry.name,
            crs=person.crs,
        )
    else:
        result = person

    result = result[result.geometry.notna() & ~result.geometry.is_empty].copy()
    if not result.geometry.is_valid.all():
        result.geometry = result.geometry.make_valid()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    if overwrite:
        remove_shapefile(output_path)
    result.to_file(output_path, driver="ESRI Shapefile", encoding="UTF-8")

    report = {
        "city": city,
        "model": str(model_path),
        "person": str(person_path),
        "mask": str(mask_path),
        "output": str(output_path),
        "person_features": len(person),
        "model_features_checked": len(model),
        "supplement_features": len(supplement_records),
        "output_features": len(result),
        "supplement_area": supplement_area,
        "residual_parts_checked": residual_parts,
        "mask_features": len(mask),
        "mask_area": float(shapely.area(coverage_mask)),
        "mask_method": "generated_from_each_input_shp_then_aligned_mosaicked_city_clipped",
        "search_distance": search_distance,
        "min_area": min_area,
        "source_field": source_field,
    }
    report_path = output_path.with_name(f"{output_path.stem}_report.json")
    report_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(
        f"[{city}] 补缝完成：新增 {len(supplement_records)} 块，"
        f"面积 {supplement_area:.3f}；输出 {output_path}",
        flush=True,
    )
    return output_path


def parse_args() -> argparse.Namespace:
    preliminary = argparse.ArgumentParser(add_help=False)
    preliminary.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    known, _ = preliminary.parse_known_args()
    mosaic, shp, base = load_sections(known.config)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=known.config)
    parser.add_argument("--cities", nargs="*", default=configured_cities(mosaic))
    parser.add_argument(
        "--city-output-dir",
        type=Path,
        default=configured_path(shp, "city_output_dir", base, "output/按市"),
    )
    parser.add_argument(
        "--search-distance",
        type=float,
        default=float(shp.get("person_gap_search_distance", 20.0)),
    )
    parser.add_argument(
        "--min-area",
        type=float,
        default=float(shp.get("person_gap_min_area", 1.0)),
    )
    parser.add_argument(
        "--source-field",
        default=str(shp.get("source_field", "src_tif")),
    )
    parser.add_argument(
        "--city-workers",
        type=int,
        default=int(shp.get("city_workers", 0)),
    )
    parser.add_argument(
        "--check-only",
        action=argparse.BooleanOptionalAction,
        default=bool(shp.get("check_only", False)),
    )
    parser.add_argument(
        "--overwrite",
        action=argparse.BooleanOptionalAction,
        default=bool(shp.get("overwrite", False)),
    )
    args = parser.parse_args()
    args.cities = list(dict.fromkeys(city.strip() for city in args.cities if city.strip()))
    if not args.cities:
        parser.error("至少需要一个城市。")
    if args.search_distance <= 0 or args.min_area < 0:
        parser.error("搜索距离必须大于 0，最小面积不能小于 0。")
    if args.city_workers < 0:
        parser.error("城市并发数不能小于 0。")
    return args


def main() -> None:
    args = parse_args()
    worker_count = min(args.city_workers or len(args.cities), len(args.cities))
    failures: list[tuple[str, Exception]] = []
    with ThreadPoolExecutor(max_workers=worker_count, thread_name_prefix="person-gap") as executor:
        futures = {
            executor.submit(
                supplement_city,
                city,
                args.city_output_dir,
                args.search_distance,
                args.min_area,
                args.source_field,
                args.overwrite,
                args.check_only,
            ): city
            for city in args.cities
        }
        for future in as_completed(futures):
            city = futures[future]
            try:
                future.result()
            except Exception as exc:
                failures.append((city, exc))
                print(f"[{city}] 补缝失败：{exc}", file=sys.stderr, flush=True)
    if failures:
        detail = "；".join(f"{city}: {error}" for city, error in failures)
        raise RuntimeError(f"{len(failures)} 个城市补缝失败：{detail}")


if __name__ == "__main__":
    main()
