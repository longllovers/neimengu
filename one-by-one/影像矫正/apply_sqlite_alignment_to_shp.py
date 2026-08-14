#!/usr/bin/env python
"""读取 SQLite 位移模型，矫正/合并 SHP，并按市界生成最终成果。

第一阶段严格重放 ``align_and_mosaic_multiple.py`` 的 SHP 变换和重叠区
所有权分配；第二阶段调用 ``fix_mosaic_shp_boundary_gaps.py`` 修复断缝；
最后使用影像阶段写入 SQLite 的同一份市界裁剪最终 SHP。

示例：
    .venv\\Scripts\\python.exe apply_sqlite_alignment_to_shp.py \
        --database output\\alignment_models.sqlite \
        --shp-dir input_shp --tif-dir input_tif\\内蒙古
"""

from __future__ import annotations

import argparse
from concurrent.futures import ProcessPoolExecutor, as_completed
from dataclasses import dataclass
import importlib.util
import json
import multiprocessing as mp
import os
from pathlib import Path
import re
import sqlite3
import subprocess
import sys
import tomllib

import geopandas as gpd
import numpy as np
import rasterio
from affine import Affine


ROOT = Path(__file__).resolve().parent
TIF_SHP_DIR = ROOT / "tif_shp"


def project_algorithm_path(filename: str) -> Path:
    """兼容算法脚本位于项目根目录或 tif_shp 子目录的两种布局。"""
    candidates = (ROOT / filename, TIF_SHP_DIR / filename)
    return next((path for path in candidates if path.is_file()), candidates[0])


def load_project_module(name: str, source: Path):
    """从指定项目文件加载模块，避免误用其他目录中的同名旧代码。"""
    if not source.is_file():
        raise FileNotFoundError(f"项目算法文件不存在：{source}")
    spec = importlib.util.spec_from_file_location(name, source)
    if spec is None or spec.loader is None:
        raise ImportError(f"无法加载项目算法模块：{source}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    try:
        spec.loader.exec_module(module)
    except Exception:
        sys.modules.pop(name, None)
        raise
    return module


def load_align_base_module():
    """优先加载源码；源码缺失时兼容项目现有的 Python 3.12 缓存。"""
    for source in (
        ROOT / "align_and_mosaic.py",
        TIF_SHP_DIR / "align_and_mosaic.py",
    ):
        if source.is_file():
            return load_project_module("align_and_mosaic", source)

    cache_tag = sys.implementation.cache_tag
    for cache in (
        ROOT / "__pycache__" / f"align_and_mosaic.{cache_tag}.pyc",
        TIF_SHP_DIR / "__pycache__" / f"align_and_mosaic.{cache_tag}.pyc",
    ):
        if cache.is_file():
            return load_project_module("align_and_mosaic", cache)
    raise FileNotFoundError(
        "缺少 align_and_mosaic.py，且没有与当前 Python 版本匹配的缓存。"
    )


load_align_base_module()
load_project_module(
    "align_and_mosaic_multiple",
    project_algorithm_path("align_and_mosaic_multiple.py"),
)
load_project_module(
    "fix_mosaic_shp_boundary_gaps",
    project_algorithm_path("fix_mosaic_shp_boundary_gaps.py"),
)

from align_and_mosaic_multiple import (
    AxisDisplacementModel,
    prepare_moving_vector,
    raster_footprint_axis,
    read_vector_in_raster_crs,
    write_merged_shapefile,
)
from fix_mosaic_shp_boundary_gaps import remove_shapefile


DEFAULT_FIX_SCRIPT = project_algorithm_path("fix_mosaic_shp_boundary_gaps.py")
DEFAULT_CONFIG = ROOT / "sqlite_pipeline.toml"
EXPECTED_MODEL_VERSIONS = {
    "shared-axis-v1",
    "shared-axis-v2-fixed-resolution",
    "shared-axis-v3-fixed-canvas-lzw",
}


def load_config_section(config_path: Path, section: str) -> tuple[dict, Path]:
    """读取 TOML 配置段，并返回配置内容及相对路径基准目录。"""
    path = config_path.resolve()
    if not path.is_file():
        raise FileNotFoundError(f"启动配置文件不存在：{path}")
    with path.open("rb") as stream:
        document = tomllib.load(stream)
    values = document.get(section)
    if not isinstance(values, dict):
        raise ValueError(f"配置文件缺少 [{section}] 段：{path}")
    return values, path.parent


def configured_path(values: dict, key: str, base: Path, default: str) -> Path:
    value = values.get(key, default)
    path = Path(str(value))
    return path if path.is_absolute() else base / path


def configured_string_list(values: dict, key: str) -> list[str]:
    value = values.get(key, [])
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, list) and all(isinstance(item, str) for item in value):
        return value
    raise ValueError(f"配置项 {key} 必须是字符串或字符串数组。")


@dataclass(frozen=True)
class StoredRasterGrid:
    """供原 SHP 变换函数使用的轻量栅格网格。"""

    crs: object
    transform: Affine
    width: int
    height: int


def shapefile_exists(path: Path) -> bool:
    return all(
        path.with_suffix(suffix).is_file()
        for suffix in (".shp", ".shx", ".dbf")
    )


def parse_json_floats(value: str) -> np.ndarray:
    result = np.asarray(json.loads(value), dtype=np.float64)
    if result.ndim != 1 or not np.isfinite(result).all():
        raise ValueError("数据库中的位移数组格式无效。")
    return result


def load_run(
    database: Path, run_id: str | None, city: str | None = None
) -> tuple[sqlite3.Row, list[sqlite3.Row], sqlite3.Row]:
    if not database.is_file():
        raise FileNotFoundError(f"位移数据库不存在：{database}")
    connection = sqlite3.connect(database, timeout=300.0)
    connection.row_factory = sqlite3.Row
    try:
        if run_id is None:
            if city:
                city_table = connection.execute(
                    """
                    SELECT 1 FROM sqlite_master
                    WHERE type='table' AND name='city_processing_runs'
                    """
                ).fetchone()
                if city_table is None:
                    raise ValueError("数据库中没有城市处理记录表。")
                run = connection.execute(
                    """
                    SELECT alignment_runs.*
                    FROM alignment_runs
                    JOIN city_processing_runs USING (run_id)
                    WHERE alignment_runs.status='completed'
                      AND city_processing_runs.status='completed'
                      AND city_processing_runs.city_name=?
                    ORDER BY city_processing_runs.completed_at DESC LIMIT 1
                    """,
                    (city,),
                ).fetchone()
            else:
                run = connection.execute(
                    """
                    SELECT alignment_runs.*
                    FROM alignment_runs
                    JOIN city_processing_runs USING (run_id)
                    WHERE alignment_runs.status='completed'
                      AND city_processing_runs.status='completed'
                    ORDER BY city_processing_runs.completed_at DESC LIMIT 1
                    """
                ).fetchone()
            if run is None:
                detail = f"城市 {city!r} " if city else ""
                raise ValueError(f"数据库中没有{detail}已完成的配准批次。")
        else:
            run = connection.execute(
                "SELECT * FROM alignment_runs WHERE run_id = ?", (run_id,)
            ).fetchone()
            if run is None:
                raise ValueError(f"数据库中没有 run_id：{run_id}")
            if run["status"] != "completed":
                raise ValueError(
                    f"run_id={run_id} 的状态是 {run['status']}，不能用于 SHP。"
                )
            if city:
                matched = connection.execute(
                    """
                    SELECT 1 FROM city_processing_runs
                    WHERE run_id=? AND city_name=? AND status='completed'
                    """,
                    (run_id, city),
                ).fetchone()
                if matched is None:
                    raise ValueError(
                        f"run_id={run_id} 不是城市 {city!r} 的已完成批次。"
                    )
        city_record = connection.execute(
            """
            SELECT * FROM city_processing_runs
            WHERE run_id=? AND status='completed'
              AND (? IS NULL OR city_name=?)
            """,
            (run["run_id"], city, city),
        ).fetchone()
        if city_record is None:
            detail = f"城市 {city!r} " if city else ""
            raise ValueError(
                f"配准批次 {run['run_id']} 没有{detail}已完成的城市边界记录。"
            )
        if run["model_version"] not in EXPECTED_MODEL_VERSIONS:
            raise ValueError(
                f"不支持模型版本 {run['model_version']}；"
                f"当前支持 {sorted(EXPECTED_MODEL_VERSIONS)}。"
            )
        rows = connection.execute(
            """
            SELECT * FROM tile_alignment_models
            WHERE run_id = ? ORDER BY sequence_no
            """,
            (run["run_id"],),
        ).fetchall()
        if len(rows) != run["source_count"]:
            raise ValueError(
                f"批次应有 {run['source_count']} 幅图，但数据库只有 {len(rows)} 条模型。"
            )
        return run, rows, city_record
    finally:
        connection.close()


def grid_from_row(row: sqlite3.Row) -> StoredRasterGrid:
    transform_values = json.loads(row["work_transform_json"])
    if len(transform_values) != 6:
        raise ValueError(f"{row['source_id']} 的仿射变换不是 6 个参数。")
    return StoredRasterGrid(
        crs=rasterio.crs.CRS.from_wkt(row["work_crs_wkt"]),
        transform=Affine(*map(float, transform_values)),
        width=int(row["work_width"]),
        height=int(row["work_height"]),
    )


def displacement_from_row(row: sqlite3.Row) -> AxisDisplacementModel:
    knots = parse_json_floats(row["knots_json"])
    dx = parse_json_floats(row["dx_json"])
    dy = parse_json_floats(row["dy_json"])
    if not (len(knots) == len(dx) == len(dy)) or len(knots) < 2:
        raise ValueError(f"{row['source_id']} 的 knots/dx/dy 长度无效。")
    if row["displacement_axis"] not in ("row", "col"):
        raise ValueError(f"{row['source_id']} 的位移轴无效。")
    return AxisDisplacementModel(
        axis=row["displacement_axis"], knots=knots, dx=dx, dy=dy
    )


def validate_inputs(
    rows: list[sqlite3.Row], shp_dir: Path, tif_dir: Path
) -> dict[str, Path]:
    shp_paths: dict[str, Path] = {}
    errors: list[str] = []
    for row in rows:
        source_id = row["source_id"]
        shp_path = shp_dir / f"{source_id}.shp"
        tif_path = next(
            (
                candidate
                for candidate in (
                    tif_dir / f"{source_id}.tif",
                    tif_dir / f"{source_id}.tiff",
                )
                if candidate.is_file()
            ),
            None,
        )
        if not shapefile_exists(shp_path):
            errors.append(f"缺少完整同名 SHP：{shp_path}")
        else:
            shp_paths[source_id] = shp_path
        if tif_path is None:
            errors.append(f"缺少同名 TIFF：{source_id}.tif/.tiff")
        elif tif_path.stat().st_size != row["original_size"]:
            errors.append(
                f"TIFF 大小与配准记录不一致：{tif_path}；"
                "请确认使用的是当时参与配准的原图。"
            )
    if errors:
        raise FileNotFoundError("\n".join(errors))
    return shp_paths


def safe_city_name(value: str) -> str:
    """生成可安全用于 Windows/Linux 文件名的城市名称。"""
    result = re.sub(r'[<>:"/\\|?*]+', "_", value).strip(" .")
    if not result:
        raise ValueError("数据库中的城市名称不能用于生成文件名。")
    return result


def city_database(database_dir: Path, city: str) -> Path:
    """返回并检查固定命名的“城市名.sqlite”数据库。"""
    directory = database_dir.resolve()
    if not directory.is_dir():
        raise FileNotFoundError(f"城市数据库目录不存在：{directory}")
    city_name = safe_city_name(city)
    path = directory / f"{city_name}.sqlite"
    if not path.is_file():
        raise FileNotFoundError(f"城市数据库不存在：{path}")
    return path


def clip_repaired_shapefile(
    input_path: Path,
    output_path: Path,
    city_record: sqlite3.Row,
) -> tuple[int, int]:
    """使用影像阶段保存的同一份市界裁剪断缝修复后的 SHP。"""
    source_crs = city_record["boundary_source_crs_wkt"]
    boundary_wkt = city_record["boundary_source_wkt"]
    if not source_crs or not boundary_wkt:
        raise ValueError("数据库城市记录缺少市界坐标系或市界几何。")

    frame = gpd.read_file(input_path)
    if frame.crs is None:
        raise ValueError(f"断缝修复 SHP 缺少 CRS：{input_path}")
    boundary = gpd.GeoSeries.from_wkt([boundary_wkt], crs=source_crs).to_crs(
        frame.crs
    )
    boundary_geometry = boundary.make_valid().union_all()
    if boundary_geometry is None or boundary_geometry.is_empty:
        raise ValueError(f"{city_record['city_name']} 的市界几何为空。")

    before = len(frame)
    clipped = frame.clip(boundary_geometry, keep_geom_type=True)
    clipped = clipped[
        clipped.geometry.notna() & ~clipped.geometry.is_empty
    ].copy()
    if clipped.empty:
        raise ValueError(
            f"断缝修复 SHP 与 {city_record['city_name']} 市界没有有效相交面。"
        )
    if not clipped.geometry.is_valid.all():
        clipped.geometry = clipped.geometry.make_valid()
        clipped = clipped[
            clipped.geometry.notna() & ~clipped.geometry.is_empty
        ].copy()

    output_path.parent.mkdir(parents=True, exist_ok=True)
    clipped.to_file(
        output_path,
        driver="ESRI Shapefile",
        encoding="UTF-8",
    )
    return before, len(clipped)


def parse_args(source_kind: str = "model") -> argparse.Namespace:
    if source_kind not in ("model", "person", "mask"):
        raise ValueError(f"不支持的 SHP 来源类型：{source_kind}")
    preliminary = argparse.ArgumentParser(add_help=False)
    preliminary.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    known, _ = preliminary.parse_known_args()
    config, config_base = load_config_section(known.config, "shp")
    mosaic_config, _ = load_config_section(known.config, "mosaic")
    configured_cities = configured_string_list(
        config if "cities" in config else mosaic_config,
        "cities",
    )
    if source_kind == "mask":
        source_key = "mask_dir"
        source_default = configured_path(
            config, source_key, config_base, "output/掩膜文件"
        )
        source_label = "掩膜"
    else:
        source_key = "shp_dir_person" if source_kind == "person" else "shp_dir"
        source_value = str(config.get(source_key, "")).strip()
        source_default = (
            configured_path(config, source_key, config_base, source_value)
            if source_value
            else None
        )
        source_label = "人工" if source_kind == "person" else "模型"

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--config",
        type=Path,
        default=known.config,
        help="启动配置文件，默认 sqlite_pipeline.toml",
    )
    parser.add_argument(
        "--database",
        type=Path,
        default=configured_path(
            config, "database", config_base, "output/alignment_models.sqlite"
        ),
    )
    parser.add_argument(
        "--database-dir",
        type=Path,
        default=configured_path(
            config, "database_dir", config_base, "output/database"
        ),
        help="城市独立 SQLite 所在目录；文件固定命名为 城市名.sqlite",
    )
    parser.add_argument(
        "--run-id",
        default=str(config.get("run_id", "")).strip() or None,
        help="指定配准批次；默认使用数据库中最近完成的批次",
    )
    parser.add_argument(
        "--city",
        default=str(config.get("city", "")).strip() or None,
        help="按城市名称选择并校验已完成批次；留空时使用最近完成的城市批次",
    )
    parser.add_argument(
        "--cities",
        nargs="*",
        default=configured_cities,
        help="并行处理一个或多个城市；默认沿用 [mosaic] 的 cities",
    )
    parser.add_argument(
        "--city-workers",
        type=int,
        default=int(config.get("city_workers", 0)),
        help="同时处理的城市数；0 表示配置中的城市全部同时处理",
    )
    parser.add_argument(
        "--shp-dir",
        type=Path,
        default=source_default,
        help=f"{source_label}同名分幅 SHP 目录（配置项 {source_key}）",
    )
    parser.add_argument(
        "--tif-dir",
        type=Path,
        default=configured_path(config, "tif_dir", config_base, "input_tif"),
        help="该批次原始 TIFF 所在目录，供最终断缝修复建立边界约束",
    )
    parser.add_argument(
        "--city-output-dir",
        type=Path,
        default=configured_path(
            config, "city_output_dir", config_base, "output/按市"
        ),
        help="按城市存放最终市界裁剪 SHP 的根目录",
    )
    parser.add_argument(
        "--intermediate-dir",
        type=Path,
        default=configured_path(
            config,
            "intermediate_dir",
            config_base,
            "output/按市/中间文件",
        ),
        help="集中存放各城市中间 SHP 的目录",
    )
    parser.add_argument(
        "--shp-seam-overlap-pixels",
        type=float,
        default=float(config.get("shp_seam_overlap_pixels", 2.0)),
        help="合并阶段接缝两侧保留的微小搭接宽度",
    )
    parser.add_argument(
        "--max-gap-pixels",
        type=float,
        default=float(config.get("max_gap_pixels", 20.0)),
    )
    parser.add_argument(
        "--fix-overlap-pixels",
        type=float,
        default=float(config.get("fix_overlap_pixels", 0.05)),
    )
    parser.add_argument(
        "--min-merge-contact-pixels",
        type=float,
        default=float(config.get("min_merge_contact_pixels", 20.0)),
    )
    parser.add_argument(
        "--fix-script",
        type=Path,
        default=configured_path(
            config, "fix_script", config_base, str(DEFAULT_FIX_SCRIPT)
        ),
    )
    parser.add_argument(
        "--check-only",
        action=argparse.BooleanOptionalAction,
        default=bool(config.get("check_only", False)),
        help="只检查数据库和同名 TIFF/SHP，不生成文件",
    )
    parser.add_argument(
        "--overwrite",
        action=argparse.BooleanOptionalAction,
        default=bool(config.get("overwrite", False)),
    )
    args = parser.parse_args()
    args.source_kind = source_kind
    args.source_label = source_label

    args.cities = list(
        dict.fromkeys(city.strip() for city in args.cities if city.strip())
    )
    if args.city:
        args.cities = [args.city]
    if args.city_workers < 0:
        parser.error("--city-workers 不能小于 0。")
    if len(args.cities) > 1 and args.run_id:
        parser.error("多城市模式不能共用一个固定 run_id，请将 run_id 留空。")
    if args.shp_dir is None:
        parser.error(f"请先填写 [{source_key}]，或通过 --shp-dir 指定目录。")

    if args.shp_seam_overlap_pixels < 0 or args.fix_overlap_pixels < 0:
        parser.error("搭接像元数不能小于 0。")
    if args.max_gap_pixels <= 0 or args.min_merge_contact_pixels <= 0:
        parser.error("最大缝宽和最短接触边必须大于 0。")
    return args


def process_city(args: argparse.Namespace, requested_city: str | None) -> None:
    args = argparse.Namespace(**vars(args))
    args.city = requested_city
    database = (
        city_database(args.database_dir, requested_city)
        if requested_city
        else args.database.resolve()
    )
    shp_dir = args.shp_dir.resolve()
    tif_dir = args.tif_dir.resolve()
    fix_script = args.fix_script.resolve()

    if not shp_dir.is_dir():
        raise FileNotFoundError(f"SHP 目录不存在：{shp_dir}")
    if not tif_dir.is_dir():
        raise FileNotFoundError(f"TIFF 目录不存在：{tif_dir}")
    if not fix_script.is_file():
        raise FileNotFoundError(f"断缝修复脚本不存在：{fix_script}")
    run, rows, city_record = load_run(database, args.run_id, args.city)
    official_city = str(city_record["city_name"]).strip()
    city_name = safe_city_name(official_city)
    source_label = args.source_label
    merged_output = (
        args.intermediate_dir.resolve() / f"{city_name}_{source_label}_中间.shp"
    )
    repaired_output = (
        args.intermediate_dir.resolve()
        / f".{city_name}_{source_label}_断缝修复临时.shp"
    )
    final_output = (
        args.city_output_dir.resolve()
        / city_name
        / f"{city_name}_{source_label}_市界裁剪.shp"
    )
    if (
        not args.check_only
        and shapefile_exists(merged_output)
        and not args.overwrite
    ):
        raise FileExistsError(
            f"中间输出已存在：{merged_output}；覆盖请添加 --overwrite。"
        )
    if (
        not args.check_only
        and shapefile_exists(final_output)
        and not args.overwrite
    ):
        raise FileExistsError(
            f"最终输出已存在：{final_output}；覆盖请添加 --overwrite。"
        )

    shp_paths = validate_inputs(rows, shp_dir, tif_dir)
    input_shapefiles = {path.resolve() for path in shp_paths.values()}
    if any(
        output in input_shapefiles
        for output in (merged_output, repaired_output, final_output)
    ):
        raise ValueError("为保护原数据，输出路径不能等于任何输入 SHP。")
    print(
        f"使用配准批次：{run['run_id']}（{len(rows)} 幅图）\n"
        f"城市：{official_city}\n"
        f"SHP 来源：{source_label}\n"
        f"中间 SHP：{merged_output}\n"
        f"最终市界裁剪 SHP：{final_output}"
    )
    for row in rows:
        print(
            f"  {row['sequence_no']:02d}. {row['source_id']} "
            f"[{row['model_type']}/{row['displacement_axis']}]"
        )
    if args.check_only:
        print("检查完成：数据库模型、市界以及同名 TIFF/SHP 均已就绪。")
        return

    if args.overwrite:
        remove_shapefile(merged_output)
        remove_shapefile(final_output)
    # 这是内部临时结果；上次异常退出留下的同名文件不能阻止本次运行。
    remove_shapefile(repaired_output)

    first = rows[0]
    first_grid = grid_from_row(first)
    first_vector, output_crs = read_vector_in_raster_crs(
        shp_paths[first["source_id"]], first_grid
    )
    raster_crs = first_grid.crs
    first_footprint = raster_footprint_axis(first_grid)
    first_vector = first_vector.clip(first_footprint, keep_geom_type=True)
    first_vector = first_vector[
        first_vector.geometry.notna() & ~first_vector.geometry.is_empty
    ].copy()
    first_vector["src_tif"] = first["source_id"]
    frames: list[gpd.GeoDataFrame] = [first_vector]
    footprints = [first_footprint]
    ownerships = [first_footprint]
    print(f"首幅 SHP 保留 {len(first_vector)} 个要素：{first['source_id']}")

    for position, row in enumerate(rows[1:], start=2):
        grid = grid_from_row(row)
        if grid.crs != raster_crs:
            raise ValueError(f"{row['source_id']} 的工作 CRS 与首幅不一致。")
        displacement = displacement_from_row(row)
        moving_vector, moving_footprint, moving_ownership = prepare_moving_vector(
            shp_paths[row["source_id"]],
            Path(row["original_tif"]),
            grid,
            displacement,
            frames,
            footprints,
            ownerships,
            args.shp_seam_overlap_pixels,
        )
        frames.append(moving_vector)
        footprints.append(moving_footprint)
        ownerships.append(moving_ownership)
        print(
            f"[{position}/{len(rows)}] {row['source_id']}："
            f"校正并裁切后保留 {len(moving_vector)} 个要素"
        )

    print("\n正在写出矫正合并 SHP……")
    count = write_merged_shapefile(
        frames, raster_crs, output_crs, merged_output
    )
    print(f"中间 SHP：{merged_output}（{count} 个要素）")

    command = [
        sys.executable,
        str(fix_script),
        "--input",
        str(merged_output),
        "--output",
        str(repaired_output),
        "--tif-dir",
        str(tif_dir),
        "--shp-dir",
        str(shp_dir),
        "--max-gap-pixels",
        str(args.max_gap_pixels),
        "--overlap-pixels",
        str(args.fix_overlap_pixels),
        "--min-merge-contact-pixels",
        str(args.min_merge_contact_pixels),
    ]
    if args.overwrite:
        command.append("--overwrite")
    print("\n正在执行跨图幅断缝融合……")
    subprocess.run(command, cwd=ROOT, check=True)
    if not shapefile_exists(repaired_output):
        raise RuntimeError(
            f"断缝修复结束但未生成完整临时 SHP：{repaired_output}"
        )

    print(f"\n正在按 {official_city} 市界裁剪最终 SHP……")
    before_clip, after_clip = clip_repaired_shapefile(
        repaired_output,
        final_output,
        city_record,
    )
    if not shapefile_exists(final_output):
        raise RuntimeError(f"市界裁剪结束但未生成完整最终 SHP：{final_output}")

    temporary_report = repaired_output.with_name(
        f"{repaired_output.stem}_repair_report.json"
    )
    final_report = final_output.with_name(
        f"{final_output.stem}_repair_report.json"
    )
    if temporary_report.is_file():
        report = json.loads(temporary_report.read_text(encoding="utf-8"))
        report.update(
            {
                "city_name": official_city,
                "shp_source_kind": args.source_kind,
                "shp_source_label": source_label,
                "city_boundary_path": city_record["city_boundary_path"],
                "features_before_city_clip": before_clip,
                "features_after_city_clip": after_clip,
                "city_clipped_output": str(final_output),
            }
        )
        final_report.write_text(
            json.dumps(report, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        temporary_report.unlink()
    remove_shapefile(repaired_output)
    print(
        f"\n全部完成：{final_output}\n"
        f"市界裁剪前 {before_clip} 个要素，裁剪后 {after_clip} 个要素。"
    )


def process_city_worker(
    args: argparse.Namespace,
    city: str,
    position: int,
    total: int,
) -> None:
    """在独立 Python 子进程中处理一个城市的一类 SHP。"""
    print(
        f"[城市 {position}/{total}][{city}][{args.source_label}] "
        f"子进程启动，PID={os.getpid()}",
        flush=True,
    )
    process_city(args, city)


def main(source_kind: str = "model") -> None:
    args = parse_args(source_kind)
    if len(args.cities) <= 1:
        process_city(args, args.cities[0] if args.cities else None)
        return

    worker_count = len(args.cities)
    print(
        f"城市 SHP 多进程模式：{len(args.cities)} 个城市，"
        f"启动 {worker_count} 个独立 Python 子进程。"
    )
    failures: list[tuple[str, Exception]] = []
    with ProcessPoolExecutor(
        max_workers=worker_count,
        mp_context=mp.get_context("spawn"),
    ) as executor:
        futures = {
            executor.submit(
                process_city_worker,
                args,
                city,
                position,
                len(args.cities),
            ): (position, city)
            for position, city in enumerate(args.cities, start=1)
        }
        for future in as_completed(futures):
            position, city = futures[future]
            try:
                future.result()
                print(
                    f"\n[城市 {position}/{len(args.cities)}]"
                    f"[{city}][{args.source_label}] 处理完成"
                )
            except Exception as exc:
                failures.append((city, exc))
                print(
                    f"\n[城市 {position}/{len(args.cities)}]"
                    f"[{city}][{args.source_label}] 处理失败：{exc}",
                    file=sys.stderr,
                )

    if failures:
        details = "；".join(f"{city}: {error}" for city, error in failures)
        raise RuntimeError(f"{len(failures)} 个城市 SHP 处理失败：{details}")
    print(f"\n全部城市 SHP 并行处理完成：{len(args.cities)} 个。")


if __name__ == "__main__":
    main()
