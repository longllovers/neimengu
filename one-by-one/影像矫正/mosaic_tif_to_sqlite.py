#!/usr/bin/env python
"""配准并镶嵌多个 TIFF，同时把可重放的位移模型保存到 SQLite。

本脚本只处理 TIFF，不要求同名 SHP 已经存在。影像配准和单轴位移模型
复用 ``align_and_mosaic_multiple.py``；镶嵌采用 LZW 固定画布局部更新。以后可使用
``apply_sqlite_alignment_to_shp.py`` 读取数据库，按完全相同的模型校正 SHP。

示例：
    .venv\\Scripts\\python.exe mosaic_tif_to_sqlite.py input_tif\\内蒙古 \
        --database output\\alignment_models.sqlite
"""

from __future__ import annotations

import argparse
import copy
from concurrent.futures import ProcessPoolExecutor, as_completed
from contextlib import closing, contextmanager
from datetime import datetime
import importlib.util
import json
import math
import multiprocessing as mp
import os
from pathlib import Path
import re
import socket
import sqlite3
import sys
import tempfile
import tomllib
import traceback
import uuid

import numpy as np
import geopandas as gpd
import rasterio
from affine import Affine
from rasterio.enums import Resampling
from rasterio.features import geometry_mask
from rasterio.shutil import copy as copy_raster
from rasterio.vrt import WarpedVRT
from rasterio.warp import reproject
from rasterio.windows import Window, from_bounds
from shapely.geometry import mapping

from find_city_tif_tiles import find_intersecting_tiles, select_city


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


_align_base = load_align_base_module()
load_project_module(
    "align_and_mosaic_multiple",
    project_algorithm_path("align_and_mosaic_multiple.py"),
)

from align_and_mosaic import same_grid
from align_and_mosaic import write_gcp_vrt
from align_and_mosaic_multiple import (
    albers_grid,
    aligned_warp_grid,
    best_neighbor_overlap,
    build_axis_displacement_model,
    build_overviews,
    discover_files,
    estimate_local_shifts,
    is_albers_crs,
    make_axis_gcps,
    print_order,
    read_infos,
    spatial_order,
)


MODEL_VERSION = "shared-axis-v3-fixed-canvas-lzw"
DEFAULT_CONFIG = ROOT / "sqlite_pipeline.toml"


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
    """TOML 中城市既可写成单个字符串，也可写成字符串数组。"""
    value = values.get(key, [])
    if value is None:
        return []
    if isinstance(value, str):
        return [value.strip()] if value.strip() else []
    if isinstance(value, list) and all(isinstance(item, str) for item in value):
        return [item.strip() for item in value if item.strip()]
    raise ValueError(f"配置字段 {key!r} 必须是城市字符串或字符串数组。")


def remove_sqlite_database(path: Path) -> None:
    """显式覆盖时删除指定城市数据库及 SQLite 事务旁车文件。"""
    for candidate in (
        path,
        Path(f"{path}-wal"),
        Path(f"{path}-shm"),
        Path(f"{path}-journal"),
    ):
        if candidate.exists():
            candidate.unlink()


def process_is_alive(pid: int) -> bool:
    """检查本机 PID 是否仍然存在。"""
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        # Windows 对超出有效范围的 PID 返回 WinError 87。
        return False
    return True


@contextmanager
def city_process_lock(
    database_dir: Path,
    city: str,
    city_sequence: int | None = None,
    city_total: int | None = None,
):
    """用原子锁文件防止两个进程同时写同一城市成果。"""
    directory = database_dir.resolve()
    directory.mkdir(parents=True, exist_ok=True)
    safe_name = city_safe_name(city)
    lock_path = directory / f".{safe_name}.process.lock"
    host = socket.gethostname()
    token = uuid.uuid4().hex
    metadata = {
        "city": city,
        "hostname": host,
        "pid": os.getpid(),
        "token": token,
        "created_at": datetime.now().astimezone().isoformat(),
    }
    progress = (
        f"[城市 {city_sequence}/{city_total}][{city}]"
        if city_sequence is not None and city_total is not None
        else f"[{city}]"
    )

    descriptor: int | None = None
    for attempt in range(2):
        try:
            descriptor = os.open(
                lock_path,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                0o644,
            )
            break
        except FileExistsError:
            try:
                before_stat = lock_path.stat()
                before_signature = (
                    before_stat.st_ino,
                    before_stat.st_size,
                    before_stat.st_mtime_ns,
                )
            except OSError:
                before_signature = None
            try:
                existing = json.loads(lock_path.read_text(encoding="utf-8"))
            except (OSError, ValueError, TypeError):
                existing = {}
            existing_host = str(existing.get("hostname", ""))
            try:
                existing_pid = int(existing.get("pid", 0))
            except (TypeError, ValueError):
                existing_pid = 0
            stale = (
                existing_host == host
                and existing_pid > 0
                and not process_is_alive(existing_pid)
            )
            if stale and attempt == 0:
                try:
                    current_stat = lock_path.stat()
                    current_signature = (
                        current_stat.st_ino,
                        current_stat.st_size,
                        current_stat.st_mtime_ns,
                    )
                except OSError:
                    current_signature = None
                if before_signature is not None and current_signature == before_signature:
                    lock_path.unlink(missing_ok=True)
                    continue
            owner = (
                f"主机={existing_host or '未知'}，PID={existing_pid or '未知'}"
            )
            raise RuntimeError(
                f"{progress} 正被另一个进程处理（{owner}）：{lock_path}"
            )
    if descriptor is None:
        raise RuntimeError(f"{progress} 无法取得进程锁：{lock_path}")

    metadata_written = False
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(metadata, stream, ensure_ascii=False, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        metadata_written = True
        print(f"{progress} 已取得独占写锁：{lock_path}", flush=True)
        yield lock_path
    finally:
        try:
            current = json.loads(lock_path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            current = {}
        if not metadata_written or current.get("token") == token:
            lock_path.unlink(missing_ok=True)
            print(f"{progress} 已释放独占写锁。", flush=True)


def normalize_inputs_to_resolution(
    files: list[Path],
    pixel_size_x: float,
    pixel_size_y: float,
) -> tuple[list[Path], tempfile.TemporaryDirectory[str] | None]:
    """把全部输入接入统一 Albers、固定像元大小和公共网格。"""
    target_crs, _, _, anchor_x, anchor_y = albers_grid(files)
    normalized: list[Path] = []
    temporary: tempfile.TemporaryDirectory[str] | None = None
    converted_count = 0

    print(
        "统一工作坐标系：CGCS2000 Albers；"
        f"像元大小 {pixel_size_x:.6f} x {pixel_size_y:.6f} 米"
    )
    for path in files:
        with rasterio.open(path, sharing=False) as dataset:
            if dataset.crs is None:
                raise ValueError(f"影像缺少 CRS：{path}")
            same_crs = is_albers_crs(dataset.crs) and dataset.crs == target_crs
            same_resolution = (
                abs(abs(float(dataset.transform.a)) - pixel_size_x) <= 1e-12
                and abs(abs(float(dataset.transform.e)) - pixel_size_y) <= 1e-12
            )
            no_rotation = all(
                abs(value) <= 1e-12
                for value in (dataset.transform.b, dataset.transform.d)
            )
            col_offset = (float(dataset.transform.c) - anchor_x) / pixel_size_x
            row_offset = (float(dataset.transform.f) - anchor_y) / pixel_size_y
            same_anchor = (
                abs(col_offset - round(col_offset)) <= 0.10
                and abs(row_offset - round(row_offset)) <= 0.10
            )
            if same_crs and same_resolution and no_rotation and same_anchor:
                normalized.append(path)
                print(
                    f"  已是 {pixel_size_x:g} x {pixel_size_y:g} 米公共网格，"
                    f"原样使用：{path.name}"
                )
                continue

            if temporary is None:
                temporary = tempfile.TemporaryDirectory(
                    prefix="fixed_resolution_inputs_"
                )
            vrt_path = Path(temporary.name) / f"{path.stem}.vrt"
            transform, width, height = aligned_warp_grid(
                dataset,
                target_crs,
                pixel_size_x,
                pixel_size_y,
                anchor_x,
                anchor_y,
            )
            options = {
                "crs": target_crs,
                "transform": transform,
                "width": width,
                "height": height,
                "resampling": Resampling.bilinear,
            }
            if dataset.nodata is not None:
                options["src_nodata"] = dataset.nodata
                options["nodata"] = dataset.nodata
            with WarpedVRT(dataset, **options) as warped:
                copy_raster(warped, vrt_path, driver="VRT")
            normalized.append(vrt_path)
            converted_count += 1
            print(
                f"  转换到 {pixel_size_x:g} x {pixel_size_y:g} 米网格："
                f"{path.name}"
            )

    if converted_count:
        print(
            f"已为 {converted_count} 幅影像创建固定分辨率临时 VRT；"
            "原始 TIFF 不变。"
        )
    return normalized, temporary


def manifest_json(infos, original_by_stem: dict[str, Path]) -> str:
    """记录有序输入清单，续跑时防止混入不同文件或不同顺序。"""
    records = []
    for sequence_no, info in enumerate(infos, start=1):
        original = original_by_stem[info.path.stem]
        stat = original.stat()
        records.append(
            {
                "sequence_no": sequence_no,
                "source_id": original.stem,
                "path": str(original.resolve()),
                "size": stat.st_size,
                "mtime_ns": stat.st_mtime_ns,
            }
        )
    return json.dumps(records, ensure_ascii=False, separators=(",", ":"))


def json_array(values) -> str:
    """用稳定、无 NaN 的 JSON 保存数值数组。"""
    return json.dumps(
        [float(value) for value in values],
        ensure_ascii=False,
        allow_nan=False,
    )


def initialize_database(connection: sqlite3.Connection) -> None:
    connection.execute("PRAGMA busy_timeout = 300000")
    connection.execute("PRAGMA foreign_keys = ON")
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS alignment_runs (
            run_id TEXT PRIMARY KEY,
            created_at TEXT NOT NULL,
            completed_at TEXT,
            status TEXT NOT NULL CHECK(status IN ('running', 'completed', 'failed')),
            output_tif TEXT NOT NULL,
            order_mode TEXT NOT NULL,
            model_type TEXT NOT NULL,
            model_version TEXT NOT NULL,
            max_shift REAL NOT NULL,
            min_response REAL NOT NULL,
            source_count INTEGER NOT NULL,
            error TEXT
        );

        CREATE TABLE IF NOT EXISTS tile_alignment_models (
            run_id TEXT NOT NULL,
            sequence_no INTEGER NOT NULL,
            source_id TEXT NOT NULL,
            original_tif TEXT NOT NULL,
            original_size INTEGER NOT NULL,
            original_mtime_ns INTEGER NOT NULL,
            work_crs_wkt TEXT NOT NULL,
            work_transform_json TEXT NOT NULL,
            work_width INTEGER NOT NULL,
            work_height INTEGER NOT NULL,
            model_type TEXT NOT NULL,
            displacement_axis TEXT NOT NULL,
            knots_json TEXT NOT NULL,
            dx_json TEXT NOT NULL,
            dy_json TEXT NOT NULL,
            global_dx REAL NOT NULL,
            global_dy REAL NOT NULL,
            match_count INTEGER NOT NULL,
            response_median REAL,
            residual_p90 REAL,
            overlap_bounds_json TEXT,
            PRIMARY KEY (run_id, sequence_no),
            UNIQUE (run_id, source_id),
            FOREIGN KEY (run_id) REFERENCES alignment_runs(run_id)
                ON DELETE CASCADE
        );

        CREATE INDEX IF NOT EXISTS idx_tile_models_source
            ON tile_alignment_models(source_id);
        CREATE INDEX IF NOT EXISTS idx_runs_status_completed
            ON alignment_runs(status, completed_at);

        CREATE TABLE IF NOT EXISTS city_processing_runs (
            run_id TEXT PRIMARY KEY,
            city_name TEXT NOT NULL,
            city_code TEXT,
            city_boundary_path TEXT NOT NULL,
            boundary_source_crs_wkt TEXT NOT NULL,
            boundary_source_wkt TEXT NOT NULL,
            boundary_work_crs_wkt TEXT,
            boundary_work_wkt TEXT,
            tile_index_path TEXT NOT NULL,
            tile_ids_json TEXT NOT NULL,
            source_manifest_json TEXT NOT NULL,
            image_suffix TEXT NOT NULL,
            mosaic_tif TEXT NOT NULL,
            clipped_tif TEXT NOT NULL,
            status TEXT NOT NULL CHECK(
                status IN ('mosaic_completed', 'completed', 'failed')
            ),
            created_at TEXT NOT NULL,
            completed_at TEXT,
            updated_at TEXT NOT NULL,
            error TEXT,
            FOREIGN KEY (run_id) REFERENCES alignment_runs(run_id)
                ON DELETE CASCADE
        );

        CREATE INDEX IF NOT EXISTS idx_city_runs_lookup
            ON city_processing_runs(city_name, mosaic_tif, status);
        """
    )
    existing_columns = {
        row[1] for row in connection.execute("PRAGMA table_info(alignment_runs)")
    }
    migrations = {
        "input_dir": "TEXT",
        "input_manifest_json": "TEXT",
        "completed_count": "INTEGER NOT NULL DEFAULT 0",
        "checkpoint_tif": "TEXT",
        "checkpoint_dir": "TEXT",
        "updated_at": "TEXT",
    }
    for column, declaration in migrations.items():
        if column not in existing_columns:
            connection.execute(
                f"ALTER TABLE alignment_runs ADD COLUMN {column} {declaration}"
            )
    connection.commit()


def working_grid_record(path: Path) -> dict:
    with rasterio.open(path, sharing=False) as dataset:
        if dataset.crs is None:
            raise ValueError(f"工作影像缺少 CRS：{path}")
        return {
            "work_crs_wkt": dataset.crs.to_wkt(),
            "work_transform_json": json_array(tuple(dataset.transform)[:6]),
            "work_width": dataset.width,
            "work_height": dataset.height,
        }


def insert_tile_model(
    connection: sqlite3.Connection,
    *,
    run_id: str,
    sequence_no: int,
    original_tif: Path,
    working_tif: Path,
    model_type: str,
    axis: str,
    knots,
    dx_values,
    dy_values,
    global_dx: float,
    global_dy: float,
    matches: list[tuple[float, float, float, float, float]],
    residual_p90: float | None,
    overlap_bounds: tuple[float, float, float, float] | None,
) -> None:
    stat = original_tif.stat()
    responses = [match[4] for match in matches]
    grid = working_grid_record(working_tif)
    connection.execute(
        """
        INSERT INTO tile_alignment_models (
            run_id, sequence_no, source_id, original_tif,
            original_size, original_mtime_ns,
            work_crs_wkt, work_transform_json, work_width, work_height,
            model_type, displacement_axis, knots_json, dx_json, dy_json,
            global_dx, global_dy, match_count, response_median,
            residual_p90, overlap_bounds_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            run_id,
            sequence_no,
            original_tif.stem,
            str(original_tif.resolve()),
            stat.st_size,
            stat.st_mtime_ns,
            grid["work_crs_wkt"],
            grid["work_transform_json"],
            grid["work_width"],
            grid["work_height"],
            model_type,
            axis,
            json_array(knots),
            json_array(dx_values),
            json_array(dy_values),
            float(global_dx),
            float(global_dy),
            len(matches),
            float(np.median(responses)) if responses else None,
            residual_p90,
            (
                json_array(overlap_bounds)
                if overlap_bounds is not None
                else None
            ),
        ),
    )


def partial_output_path(target: Path) -> Path:
    """生成与正式输出同目录的临时文件名，便于完成后原子替换。"""
    return target.with_name(f".{target.stem}.partial{target.suffix}")


def fixed_canvas_spec(
    input_paths: list[Path],
    pixel_size_x: float,
    pixel_size_y: float,
    max_shift: float,
) -> tuple[dict, Affine, int, int]:
    """计算一次性覆盖全部输入及允许位移范围的固定输出画布。"""
    with rasterio.open(input_paths[0], sharing=False) as first:
        profile = first.profile.copy()
        target_crs = first.crs
        anchor_x = float(first.transform.c)
        anchor_y = float(first.transform.f)
        left, bottom, right, top = first.bounds
        first_count = first.count
        first_dtypes = first.dtypes

    for path in input_paths[1:]:
        with rasterio.open(path, sharing=False) as dataset:
            if dataset.crs != target_crs:
                raise ValueError(f"统一网格后 CRS 仍不一致：{path}")
            if dataset.count != first_count or dataset.dtypes != first_dtypes:
                raise ValueError(f"输入影像的波段数或数据类型不一致：{path}")
            left = min(left, dataset.bounds.left)
            bottom = min(bottom, dataset.bounds.bottom)
            right = max(right, dataset.bounds.right)
            top = max(top, dataset.bounds.top)

    # 局部位移模型的观测值受 max_shift 限制；额外留出 8 个像元边界。
    padding_pixels = int(math.ceil(max_shift)) + 8
    left -= padding_pixels * pixel_size_x
    right += padding_pixels * pixel_size_x
    bottom -= padding_pixels * pixel_size_y
    top += padding_pixels * pixel_size_y
    aligned_left = anchor_x + math.floor(
        (left - anchor_x) / pixel_size_x
    ) * pixel_size_x
    aligned_right = anchor_x + math.ceil(
        (right - anchor_x) / pixel_size_x
    ) * pixel_size_x
    aligned_bottom = anchor_y + math.floor(
        (bottom - anchor_y) / pixel_size_y
    ) * pixel_size_y
    aligned_top = anchor_y + math.ceil(
        (top - anchor_y) / pixel_size_y
    ) * pixel_size_y
    width = int(round((aligned_right - aligned_left) / pixel_size_x))
    height = int(round((aligned_top - aligned_bottom) / pixel_size_y))
    transform = Affine(
        pixel_size_x, 0.0, aligned_left,
        0.0, -pixel_size_y, aligned_top,
    )
    nodata = profile.get("nodata")
    if nodata is None:
        nodata = 0
    profile.update(
        driver="GTiff",
        width=width,
        height=height,
        transform=transform,
        crs=target_crs,
        nodata=nodata,
        tiled=True,
        blockxsize=512,
        blockysize=512,
        compress="LZW",
        BIGTIFF="YES",
        interleave="pixel",
        SPARSE_OK="TRUE",
    )
    # 明确移除输入影像可能携带的 Predictor 设置。
    profile.pop("predictor", None)
    return profile, transform, width, height


def create_fixed_canvas(
    output: Path,
    input_paths: list[Path],
    args: argparse.Namespace,
) -> None:
    profile, _, width, height = fixed_canvas_spec(
        input_paths,
        args.pixel_size_x,
        args.pixel_size_y,
        args.max_shift,
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    with rasterio.open(output, "w", **profile) as canvas:
        canvas.update_tags(
            MOSAIC_STEP="-1",
            ADDED_SOURCE="",
            ALIGNMENT_MODEL_VERSION=MODEL_VERSION,
            MOSAIC_WRITE_MODE="fixed-canvas-local-update",
        )
    print(
        f"固定画布已创建：{width} x {height}；"
        "0.5 x 0.5 米；LZW；无 Predictor"
    )


def validate_fixed_canvas(
    output: Path,
    input_paths: list[Path],
    args: argparse.Namespace,
    completed_count: int,
) -> None:
    if not output.is_file():
        if completed_count:
            raise FileNotFoundError(f"数据库已有进度，但固定画布不存在：{output}")
        return
    _, transform, width, height = fixed_canvas_spec(
        input_paths,
        args.pixel_size_x,
        args.pixel_size_y,
        args.max_shift,
    )
    with rasterio.open(output, sharing=False) as canvas:
        if (
            canvas.width != width
            or canvas.height != height
            or canvas.transform != transform
            or canvas.tags().get("ALIGNMENT_MODEL_VERSION") != MODEL_VERSION
        ):
            raise ValueError(f"现有固定画布与本次参数不一致：{output}")
        expected_step = str(completed_count - 1)
        if canvas.tags().get("MOSAIC_STEP") != expected_step:
            raise ValueError(
                f"固定画布步骤为 {canvas.tags().get('MOSAIC_STEP')!r}，"
                f"数据库要求 {expected_step!r}：{output}"
            )


def affected_window(
    canvas: rasterio.DatasetReader,
    moving: rasterio.DatasetReader,
    max_shift: float,
) -> Window:
    """返回新增影像（含允许位移边界）在固定画布中的局部窗口。"""
    pad_x = (math.ceil(max_shift) + 8) * abs(canvas.transform.a)
    pad_y = (math.ceil(max_shift) + 8) * abs(canvas.transform.e)
    raw = from_bounds(
        moving.bounds.left - pad_x,
        moving.bounds.bottom - pad_y,
        moving.bounds.right + pad_x,
        moving.bounds.top + pad_y,
        transform=canvas.transform,
    )
    col0 = max(0, int(math.floor(raw.col_off)))
    row0 = max(0, int(math.floor(raw.row_off)))
    col1 = min(canvas.width, int(math.ceil(raw.col_off + raw.width)))
    row1 = min(canvas.height, int(math.ceil(raw.row_off + raw.height)))
    if col1 <= col0 or row1 <= row0:
        raise ValueError(f"影像位于固定画布之外：{moving.name}")
    return Window(col0, row0, col1 - col0, row1 - row0)


def write_window_backup(
    canvas: rasterio.DatasetReader,
    window: Window,
    target: Path,
    pending_sequence: int,
    stripe_rows: int,
) -> None:
    """原子生成一个局部回滚文件，供服务器异常中断后恢复。"""
    partial = partial_output_path(target)
    partial.unlink(missing_ok=True)
    profile = canvas.profile.copy()
    profile.update(
        driver="GTiff",
        width=int(window.width),
        height=int(window.height),
        transform=canvas.window_transform(window),
        tiled=True,
        blockxsize=512,
        blockysize=512,
        compress="LZW",
        BIGTIFF="IF_SAFER",
    )
    profile.pop("predictor", None)
    with rasterio.open(partial, "w", **profile) as backup:
        for row in range(0, int(window.height), stripe_rows):
            rows = min(stripe_rows, int(window.height) - row)
            source_window = Window(
                int(window.col_off), int(window.row_off) + row,
                int(window.width), rows,
            )
            destination_window = Window(0, row, int(window.width), rows)
            backup.write(canvas.read(window=source_window), window=destination_window)
        backup.update_tags(
            PENDING_SEQUENCE=str(pending_sequence),
            PREVIOUS_MOSAIC_STEP=canvas.tags().get("MOSAIC_STEP", "-1"),
            PREVIOUS_ADDED_SOURCE=canvas.tags().get("ADDED_SOURCE", ""),
        )
    partial.replace(target)


def restore_window_backup(output: Path, backup_path: Path, stripe_rows: int) -> None:
    """把未提交步骤写入前的局部像元恢复到固定画布。"""
    with rasterio.open(output, "r+", sharing=False) as canvas, rasterio.open(
        backup_path, sharing=False
    ) as backup:
        target = from_bounds(*backup.bounds, transform=canvas.transform)
        target = Window(
            int(round(target.col_off)),
            int(round(target.row_off)),
            backup.width,
            backup.height,
        )
        for row in range(0, backup.height, stripe_rows):
            rows = min(stripe_rows, backup.height - row)
            source_window = Window(0, row, backup.width, rows)
            destination_window = Window(
                int(target.col_off), int(target.row_off) + row,
                backup.width, rows,
            )
            canvas.write(backup.read(window=source_window), window=destination_window)
        canvas.update_tags(
            MOSAIC_STEP=backup.tags().get("PREVIOUS_MOSAIC_STEP", "-1"),
            ADDED_SOURCE=backup.tags().get("PREVIOUS_ADDED_SOURCE", ""),
        )


def recover_pending_window(
    output: Path,
    backup_path: Path,
    completed_count: int,
    stripe_rows: int,
) -> None:
    partial_output_path(backup_path).unlink(missing_ok=True)
    if not backup_path.is_file():
        return
    with rasterio.open(backup_path, sharing=False) as backup:
        pending_sequence = int(backup.tags().get("PENDING_SEQUENCE", "0"))
    if pending_sequence <= completed_count:
        backup_path.unlink()
        return
    print(f"检测到未提交的第 {pending_sequence} 幅局部写入，正在回滚……")
    restore_window_backup(output, backup_path, stripe_rows)
    backup_path.unlink()
    print("局部窗口回滚完成。")


def update_fixed_canvas(
    canvas: rasterio.DatasetWriter,
    moving: rasterio.DatasetReader,
    moving_transform: Affine,
    moving_gcps,
    window: Window,
    args: argparse.Namespace,
) -> None:
    """只重投影并更新新增影像覆盖的窗口，已有有效像元保持优先。"""
    nodata = canvas.nodata if canvas.nodata is not None else 0
    moving_source = moving
    moving_vrt = None
    vrt_temporary = tempfile.TemporaryDirectory(
        prefix="sqlite_gcp_", dir=Path(canvas.name).parent
    )
    try:
        if moving_gcps:
            vrt_path = Path(vrt_temporary.name) / "moving_gcps.vrt"
            write_gcp_vrt(moving, moving_gcps, vrt_path)
            moving_vrt = rasterio.open(vrt_path, sharing=False)
            moving_source = moving_vrt

        stripe_count = math.ceil(int(window.height) / args.stripe_rows)
        for stripe_index, row in enumerate(
            range(0, int(window.height), args.stripe_rows), start=1
        ):
            rows = min(args.stripe_rows, int(window.height) - row)
            stripe = Window(
                int(window.col_off), int(window.row_off) + row,
                int(window.width), rows,
            )
            stripe_transform = canvas.window_transform(stripe)
            for band in range(1, canvas.count + 1):
                data = np.full(
                    (rows, int(window.width)),
                    nodata,
                    dtype=canvas.dtypes[band - 1],
                )
                moving_args = dict(
                    source=rasterio.band(moving_source, band),
                    destination=data,
                    src_nodata=moving.nodata,
                    dst_transform=stripe_transform,
                    dst_crs=canvas.crs,
                    dst_nodata=nodata,
                    resampling=Resampling.bilinear,
                    init_dest_nodata=True,
                    num_threads=args.threads,
                    warp_mem_limit=args.warp_mem_limit_mb,
                )
                if moving_gcps:
                    moving_args.update(MAX_GCP_ORDER=-1)
                else:
                    moving_args.update(
                        src_transform=moving_transform,
                        src_crs=moving.crs,
                    )
                reproject(**moving_args)
                existing = canvas.read(band, window=stripe)
                existing_valid = canvas.read_masks(band, window=stripe) > 0
                data[existing_valid] = existing[existing_valid]
                canvas.write(data, band, window=stripe)
            if stripe_index == 1 or stripe_index == stripe_count:
                print(
                    f"    局部写入：{stripe_index}/{stripe_count} 条带",
                    flush=True,
                )
    finally:
        if moving_vrt is not None:
            moving_vrt.close()
        vrt_temporary.cleanup()


def parse_args() -> argparse.Namespace:
    preliminary = argparse.ArgumentParser(add_help=False)
    preliminary.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    known, _ = preliminary.parse_known_args()
    config, config_base = load_config_section(known.config, "mosaic")

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--config",
        type=Path,
        default=known.config,
        help="启动配置文件，默认 sqlite_pipeline.toml",
    )
    parser.add_argument(
        "input_dir",
        nargs="?",
        type=Path,
        default=configured_path(config, "input_dir", config_base, "input_tif"),
        help="包含所有 TIFF 影像的目录",
    )
    parser.add_argument(
        "-o",
        "--output",
        type=Path,
        default=configured_path(
            config, "output", config_base, "output/内蒙古完整影像.tif"
        ),
        help="最终镶嵌 TIFF",
    )
    parser.add_argument(
        "--database",
        type=Path,
        default=configured_path(
            config, "database", config_base, "output/alignment_models.sqlite"
        ),
        help="保存位移模型的 SQLite 数据库",
    )
    parser.add_argument(
        "--database-dir",
        type=Path,
        default=configured_path(
            config, "database_dir", config_base, "output/database"
        ),
        help="城市模式下每个城市独立 SQLite 的保存目录",
    )
    parser.add_argument(
        "--run-id",
        default=str(config.get("run_id", "")).strip() or None,
        help="可选的运行批次 ID；默认自动生成 UUID",
    )
    parser.add_argument(
        "--cities",
        nargs="*",
        default=configured_string_list(config, "cities"),
        help="按城市处理一个或多个市/盟；配置文件中也可填写 cities",
    )
    parser.add_argument(
        "--city-workers",
        type=int,
        default=int(config.get("city_workers", 0)),
        help="兼容旧配置保留；当前固定为每个城市一个独立子进程",
    )
    parser.add_argument(
        "--city-boundary",
        type=Path,
        default=configured_path(
            config,
            "city_boundary",
            config_base,
            "市边界和分幅/15_市边界.shp",
        ),
    )
    parser.add_argument(
        "--tile-index",
        type=Path,
        default=configured_path(
            config,
            "tile_index",
            config_base,
            "市边界和分幅/5w分幅成果结合表.shp",
        ),
    )
    parser.add_argument(
        "--image-suffix",
        default=str(config.get("image_suffix", "_2025.tif")),
        help="图幅号对应的影像后缀，默认 _2025.tif",
    )
    parser.add_argument(
        "--city-output-dir",
        type=Path,
        default=configured_path(
            config, "city_output_dir", config_base, "output/按市"
        ),
        help="每个城市成果文件夹的上级目录",
    )
    parser.add_argument(
        "--city-field",
        default=str(config.get("city_field", "市名称")),
    )
    parser.add_argument(
        "--city-code-field",
        default=str(config.get("city_code_field", "市代码")),
    )
    parser.add_argument(
        "--tile-field",
        default=str(config.get("tile_field", "PLANE_NAME")),
    )
    parser.add_argument(
        "--order",
        choices=("name", "spatial"),
        default=config.get("order", "spatial"),
    )
    parser.add_argument(
        "--model",
        choices=("rubber", "translation"),
        default=config.get("model", "rubber"),
    )
    parser.add_argument(
        "--pixel-size-x",
        type=float,
        default=float(config.get("pixel_size_x", 0.5)),
        help="统一工作网格横向像元大小（米），默认 0.5",
    )
    parser.add_argument(
        "--pixel-size-y",
        type=float,
        default=float(config.get("pixel_size_y", 0.5)),
        help="统一工作网格纵向像元大小（米），默认 0.5",
    )
    parser.add_argument(
        "--max-shift", type=float, default=float(config.get("max_shift", 30.0))
    )
    parser.add_argument(
        "--min-response",
        type=float,
        default=float(config.get("min_response", 0.40)),
    )
    parser.add_argument("--threads", type=int, default=int(config.get("threads", 1)))
    parser.add_argument(
        "--gdal-cache-mb",
        type=int,
        default=int(config.get("gdal_cache_mb", 8192)),
        help="GDAL 块缓存（MB），默认 8192",
    )
    parser.add_argument(
        "--warp-mem-limit-mb",
        type=int,
        default=int(config.get("warp_mem_limit_mb", 4096)),
        help="单次 GDAL 重投影内存上限（MB），默认 4096",
    )
    parser.add_argument(
        "--stripe-rows", type=int, default=int(config.get("stripe_rows", 512))
    )
    parser.add_argument(
        "--resume",
        action=argparse.BooleanOptionalAction,
        default=bool(config.get("resume", True)),
        help="自动从同一输出的最近未完成批次继续（默认开启）",
    )
    parser.add_argument(
        "--checkpoint-dir",
        type=Path,
        default=(
            configured_path(config, "checkpoint_dir", config_base, "")
            if str(config.get("checkpoint_dir", "")).strip()
            else None
        ),
        help="局部回滚窗口目录；默认在最终输出旁建立 *_checkpoint",
    )
    parser.add_argument(
        "--keep-intermediate",
        action=argparse.BooleanOptionalAction,
        default=bool(config.get("keep_intermediate", False)),
    )
    parser.add_argument(
        "--build-overviews",
        action=argparse.BooleanOptionalAction,
        default=bool(config.get("build_overviews", True)),
    )
    parser.add_argument(
        "--check-only",
        action=argparse.BooleanOptionalAction,
        default=bool(config.get("check_only", False)),
        help="只检查影像和处理顺序，不生成 TIFF 或数据库批次",
    )
    parser.add_argument(
        "--overwrite",
        action=argparse.BooleanOptionalAction,
        default=bool(config.get("overwrite", False)),
    )
    args = parser.parse_args()

    args.cities = list(dict.fromkeys(city.strip() for city in args.cities if city.strip()))
    if args.city_workers < 0:
        parser.error("--city-workers 不能小于 0。")
    if args.cities and not args.image_suffix.strip():
        parser.error("--image-suffix 不能为空。")
    if len(args.cities) > 1 and args.run_id:
        parser.error("多城市模式不能共用一个固定 run_id，请将 run_id 留空。")
    if args.max_shift <= 0:
        parser.error("--max-shift 必须大于 0。")
    if args.pixel_size_x <= 0 or args.pixel_size_y <= 0:
        parser.error("像元大小必须大于 0。")
    if not 0 < args.min_response <= 1:
        parser.error("--min-response 必须在 (0, 1] 范围内。")
    if args.threads < 1:
        parser.error("--threads 必须至少为 1。")
    if args.gdal_cache_mb < 64:
        parser.error("--gdal-cache-mb 必须至少为 64。")
    if args.warp_mem_limit_mb < 64:
        parser.error("--warp-mem-limit-mb 必须至少为 64。")
    if args.stripe_rows < 128:
        parser.error("--stripe-rows 必须至少为 128。")
    return args


def resumable_run(
    connection: sqlite3.Connection,
    args: argparse.Namespace,
    output: Path,
    source_count: int,
    input_dir: Path,
    input_manifest: str,
) -> sqlite3.Row | None:
    """查找参数完全一致的批次，支持断点和城市裁剪阶段复用。"""
    connection.row_factory = sqlite3.Row
    if not args.resume:
        return None
    if args.run_id:
        rows = connection.execute(
            "SELECT * FROM alignment_runs WHERE run_id = ?", (args.run_id,)
        ).fetchall()
    else:
        rows = connection.execute(
            """
            SELECT * FROM alignment_runs
            WHERE output_tif = ? AND status IN ('running', 'failed', 'completed')
            ORDER BY created_at DESC
            """,
            (str(output),),
        ).fetchall()
    for row in rows:
        compatible = (
            row["status"] in ("running", "failed", "completed")
            and row["output_tif"] == str(output)
            and row["order_mode"] == args.order
            and row["model_type"] == args.model
            and row["model_version"] == MODEL_VERSION
            and int(row["source_count"]) == source_count
            and abs(float(row["max_shift"]) - args.max_shift) <= 1e-12
            and abs(float(row["min_response"]) - args.min_response) <= 1e-12
            and (
                row["input_dir"] is None
                or row["input_dir"] == str(input_dir)
            )
            and (
                row["input_manifest_json"] is None
                or row["input_manifest_json"] == input_manifest
            )
        )
        if compatible:
            return row
    if args.run_id and rows:
        raise ValueError(
            f"run_id={args.run_id} 已存在，但状态或参数与本次续跑不一致。"
        )
    return None


def model_rows(connection: sqlite3.Connection, run_id: str) -> list[sqlite3.Row]:
    return connection.execute(
        """
        SELECT * FROM tile_alignment_models
        WHERE run_id = ? ORDER BY sequence_no
        """,
        (run_id,),
    ).fetchall()


def validate_resume_rows(
    rows: list[sqlite3.Row],
    input_paths: list[Path],
    original_by_stem: dict[str, Path],
) -> None:
    """确认数据库断点与本次输入顺序、原文件完全对应。"""
    if len(rows) > len(input_paths):
        raise ValueError("数据库断点记录数超过当前输入 TIFF 数量。")
    for expected_sequence, row in enumerate(rows, start=1):
        if int(row["sequence_no"]) != expected_sequence:
            raise ValueError("数据库断点序号不连续，不能安全续跑。")
        expected_id = input_paths[expected_sequence - 1].stem
        if row["source_id"] != expected_id:
            raise ValueError(
                "数据库断点与当前空间排序不一致："
                f"第 {expected_sequence} 幅应为 {expected_id}，"
                f"记录中为 {row['source_id']}。"
            )
        original = original_by_stem[expected_id]
        stat = original.stat()
        if (
            int(row["original_size"]) != stat.st_size
            or int(row["original_mtime_ns"]) != stat.st_mtime_ns
        ):
            raise ValueError(f"断点对应的原始 TIFF 已变化：{original}")


def run_pipeline(
    args: argparse.Namespace,
    source_files: list[Path] | None = None,
) -> str | None:
    output = args.output.resolve()
    database = args.database.resolve()
    run_id = args.run_id or ""
    albers_temporary: tempfile.TemporaryDirectory[str] | None = None
    connection: sqlite3.Connection | None = None
    run_active = False
    try:
        if not args.input_dir.is_dir():
            raise NotADirectoryError(f"TIFF 输入目录不存在：{args.input_dir}")
        original_files = (
            discover_files([args.input_dir])
            if source_files is None
            else discover_files([path.resolve() for path in source_files])
        )
        if output in set(original_files):
            raise ValueError("为保护原数据，输出 TIFF 不能等于任何输入 TIFF。")
        original_by_stem = {path.stem: path for path in original_files}
        if len(original_by_stem) != len(original_files):
            raise ValueError("输入 TIFF 存在重复图幅名，无法建立唯一数据库记录。")

        working_files, albers_temporary = normalize_inputs_to_resolution(
            original_files,
            args.pixel_size_x,
            args.pixel_size_y,
        )
        infos = read_infos(working_files)
        if args.order == "spatial":
            infos = spatial_order(infos)
        print_order(infos)

        if args.check_only:
            print("检查完成：不会生成 TIFF，也不会写入数据库批次。")
            return

        output.parent.mkdir(parents=True, exist_ok=True)
        database.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(database, timeout=300.0)
        initialize_database(connection)
        input_paths = [info.path for info in infos]
        input_dir = args.input_dir.resolve()
        input_manifest = manifest_json(infos, original_by_stem)
        checkpoint_dir = (
            args.checkpoint_dir.resolve()
            if args.checkpoint_dir is not None
            else output.parent / f"{output.stem}_checkpoint"
        )
        checkpoint_dir.mkdir(parents=True, exist_ok=True)
        backup_path = checkpoint_dir / "pending_window_backup.tif"

        previous_run = resumable_run(
            connection,
            args,
            output,
            len(infos),
            input_dir,
            input_manifest,
        )
        if previous_run is not None:
            run_id = str(previous_run["run_id"])
            rows = model_rows(connection, run_id)
            validate_resume_rows(rows, input_paths, original_by_stem)
            completed_count = len(rows)
            recorded_count = int(previous_run["completed_count"] or 0)
            if recorded_count != completed_count:
                raise ValueError(
                    "数据库批次进度与位移模型记录数不一致，不能安全续跑："
                    f"completed_count={recorded_count}，models={completed_count}。"
                )
            if previous_run["status"] == "completed":
                if completed_count != len(input_paths):
                    raise ValueError("已完成批次的模型数量不完整，不能复用。")
                if not output.is_file():
                    raise FileNotFoundError(
                        f"数据库批次已完成，但合成影像不存在：{output}"
                    )
                validate_fixed_canvas(output, input_paths, args, completed_count)
                print(f"\n配准合成批次已经完成，直接复用：{run_id}")
                print(f"固定画布：{output}")
                return run_id
            if not output.is_file():
                if completed_count:
                    raise FileNotFoundError(
                        f"数据库已有 {completed_count} 幅进度，但固定画布不存在："
                        f"{output}"
                    )
                backup_path.unlink(missing_ok=True)
                partial_output_path(backup_path).unlink(missing_ok=True)
                create_fixed_canvas(output, input_paths, args)
            recover_pending_window(
                output, backup_path, completed_count, args.stripe_rows
            )
            validate_fixed_canvas(output, input_paths, args, completed_count)
            connection.execute(
                """
                UPDATE alignment_runs
                SET status = 'running', completed_at = NULL, error = NULL,
                    input_dir = ?, input_manifest_json = ?,
                    completed_count = ?, checkpoint_tif = ?,
                    checkpoint_dir = ?, updated_at = ?
                WHERE run_id = ?
                """,
                (
                    str(input_dir),
                    input_manifest,
                    completed_count,
                    str(output),
                    str(checkpoint_dir),
                    datetime.now().astimezone().isoformat(),
                    run_id,
                ),
            )
            connection.commit()
            run_active = True
            print(
                f"\n检测到未完成批次 {run_id}：已完成 "
                f"{completed_count}/{len(input_paths)} 幅，从断点继续。"
            )
            print(f"固定画布：{output}")
        else:
            if output.exists() and not args.overwrite:
                raise FileExistsError(
                    f"输出已存在且没有匹配的可续跑批次：{output}；"
                    "如需开始新批次，请设置 overwrite=true。"
                )
            if args.run_id and connection.execute(
                "SELECT 1 FROM alignment_runs WHERE run_id = ?", (args.run_id,)
            ).fetchone():
                raise ValueError(f"数据库中已经存在 run_id：{args.run_id}")
            run_id = args.run_id or str(uuid.uuid4())
            connection.execute(
                """
                INSERT INTO alignment_runs (
                    run_id, created_at, status, output_tif, order_mode,
                    model_type, model_version, max_shift, min_response,
                    source_count, input_dir, input_manifest_json,
                    completed_count, checkpoint_tif, checkpoint_dir, updated_at
                ) VALUES (?, ?, 'running', ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, NULL, ?, ?)
                """,
                (
                    run_id,
                    datetime.now().astimezone().isoformat(),
                    str(output),
                    args.order,
                    args.model,
                    MODEL_VERSION,
                    args.max_shift,
                    args.min_response,
                    len(infos),
                    str(input_dir),
                    input_manifest,
                    str(checkpoint_dir),
                    datetime.now().astimezone().isoformat(),
                ),
            )
            connection.commit()
            run_active = True
            backup_path.unlink(missing_ok=True)
            partial_output_path(backup_path).unlink(missing_ok=True)
            create_fixed_canvas(output, input_paths, args)
            completed_count = 0
            print(f"\n创建新配准批次：{run_id}")

        for index, moving_path in enumerate(
            input_paths[completed_count:], start=completed_count + 1
        ):
            print(f"\n[{index}/{len(input_paths)}] 固定画布 + {moving_path.name}")
            with rasterio.open(output, sharing=False) as reference, \
                    rasterio.open(moving_path, sharing=False) as moving:
                same_grid(reference, moving)
                window = affected_window(reference, moving, args.max_shift)
                if index == 1:
                    global_dx = global_dy = 0.0
                    matches = []
                    residual_p90 = None
                    overlap_bounds = None
                    displacement_axis = "identity"
                    knots = dx_values = dy_values = []
                    gcps = None
                    corrected_transform = moving.transform
                else:
                    overlap_bounds = best_neighbor_overlap(
                        infos[:index - 1], infos[index - 1]
                    )
                    global_dx, global_dy, matches = estimate_local_shifts(
                        reference,
                        moving,
                        max_shift=args.max_shift,
                        min_response=args.min_response,
                        limit_bounds=overlap_bounds,
                    )
                    local = np.asarray(matches, dtype=np.float64)[:, 2:4]
                    residual = np.linalg.norm(
                        local - np.asarray([global_dx, global_dy]), axis=1
                    )
                    residual_p90 = float(np.percentile(residual, 90))
                    displacement = build_axis_displacement_model(
                        reference,
                        moving,
                        matches,
                        overlap_bounds,
                        args.model,
                        global_dx,
                        global_dy,
                    )
                    displacement_axis = displacement.axis
                    knots = displacement.knots
                    dx_values = displacement.dx
                    dy_values = displacement.dy
                    gcps = (
                        make_axis_gcps(moving, displacement)
                        if args.model == "rubber"
                        else None
                    )
                    corrected_transform = moving.transform * Affine.translation(
                        -global_dx, -global_dy
                    )
                    print(
                        f"  可靠匹配块: {len(matches)}；dx={global_dx:.3f}, "
                        f"dy={global_dy:.3f} 像元；"
                        f"残差 P90={residual_p90:.3f} 像元"
                    )

                write_window_backup(
                    reference,
                    window,
                    backup_path,
                    index,
                    args.stripe_rows,
                )

            with rasterio.open(output, "r+", sharing=False) as canvas, \
                    rasterio.open(moving_path, sharing=False) as moving:
                update_fixed_canvas(
                    canvas,
                    moving,
                    corrected_transform,
                    gcps,
                    window,
                    args,
                )
                canvas.update_tags(
                    REGISTRATION=(
                        "identity"
                        if index == 1
                        else "sequential overlap phase correlation"
                    ),
                    MOSAIC_STEP=str(index - 1),
                    ADDED_SOURCE=moving_path.name,
                    ALIGNMENT_MODEL_VERSION=MODEL_VERSION,
                    DISPLACEMENT_AXIS=displacement_axis,
                )

            insert_tile_model(
                connection,
                run_id=run_id,
                sequence_no=index,
                original_tif=original_by_stem[moving_path.stem],
                working_tif=moving_path,
                model_type="identity" if index == 1 else args.model,
                axis=displacement_axis,
                knots=knots,
                dx_values=dx_values,
                dy_values=dy_values,
                global_dx=global_dx,
                global_dy=global_dy,
                matches=matches,
                residual_p90=residual_p90,
                overlap_bounds=overlap_bounds,
            )
            connection.execute(
                """
                UPDATE alignment_runs
                SET completed_count = ?, checkpoint_tif = ?, updated_at = ?
                WHERE run_id = ?
                """,
                (
                    index,
                    str(output),
                    datetime.now().astimezone().isoformat(),
                    run_id,
                ),
            )
            connection.commit()
            backup_path.unlink(missing_ok=True)
            print(f"  局部更新完成：{moving_path.name}")

        if args.build_overviews:
            print("\n正在为最终影像创建金字塔……")
            build_overviews(output)
        with rasterio.open(output, "r+") as final:
            final.update_tags(
                ALIGNMENT_DATABASE=str(database),
                ALIGNMENT_RUN_ID=run_id,
                ALIGNMENT_MODEL_VERSION=MODEL_VERSION,
            )
        connection.execute(
            """
            UPDATE alignment_runs
            SET status = 'completed', completed_at = ?, updated_at = ?,
                completed_count = ?, checkpoint_tif = ?
            WHERE run_id = ?
            """,
            (
                datetime.now().astimezone().isoformat(),
                datetime.now().astimezone().isoformat(),
                len(input_paths),
                str(output),
                run_id,
            ),
        )
        connection.commit()
        run_active = False
        print(f"\n全部完成：{output}")
        print(f"位移数据库：{database}")
        print(f"运行批次 ID：{run_id}")
        return run_id
    except Exception as exc:
        if connection is not None and run_active and run_id:
            connection.rollback()
            connection.execute(
                """
                UPDATE alignment_runs
                SET status = 'failed', completed_at = ?, updated_at = ?, error = ?
                WHERE run_id = ?
                """,
                (
                    datetime.now().astimezone().isoformat(),
                    datetime.now().astimezone().isoformat(),
                    "".join(traceback.format_exception_only(type(exc), exc)).strip(),
                    run_id,
                ),
            )
            connection.commit()
        raise
    finally:
        if connection is not None:
            connection.close()
        if albers_temporary is not None:
            albers_temporary.cleanup()


def city_safe_name(value: str) -> str:
    return re.sub(r'[<>:"/\\|?*]+', "_", value).strip(" .")


def source_manifest_json(paths: list[Path]) -> str:
    records = []
    for path in paths:
        stat = path.stat()
        records.append(
            {
                "path": str(path.resolve()),
                "size": stat.st_size,
                "mtime_ns": stat.st_mtime_ns,
            }
        )
    return json.dumps(records, ensure_ascii=False, separators=(",", ":"))


def resolve_city_source_files(
    all_files: list[Path],
    tile_ids: list[str],
    image_suffix: str,
    city_name: str,
) -> list[Path]:
    by_name: dict[str, Path] = {}
    for path in all_files:
        key = path.name.casefold()
        if key in by_name:
            raise ValueError(f"输入目录存在同名影像：{path.name}")
        by_name[key] = path
    expected_names = [f"{tile_id}{image_suffix}" for tile_id in tile_ids]
    missing = [name for name in expected_names if name.casefold() not in by_name]
    if missing:
        preview = "；".join(missing[:30])
        extra = f"；另有 {len(missing) - 30} 幅" if len(missing) > 30 else ""
        raise FileNotFoundError(
            f"{city_name} 应有 {len(tile_ids)} 幅影像，但缺少 {len(missing)} 幅："
            f"{preview}{extra}"
        )
    return [by_name[name.casefold()] for name in expected_names]


def find_reusable_city_record(
    database: Path,
    city_name: str,
    mosaic_tif: Path,
    tile_ids_json: str,
    sources_json: str,
) -> sqlite3.Row | None:
    if not database.is_file():
        return None
    with closing(sqlite3.connect(database, timeout=300.0)) as connection:
        initialize_database(connection)
        connection.row_factory = sqlite3.Row
        return connection.execute(
            """
            SELECT city_processing_runs.*, alignment_runs.status AS alignment_status
            FROM city_processing_runs
            JOIN alignment_runs USING (run_id)
            WHERE city_name = ? AND mosaic_tif = ?
              AND tile_ids_json = ? AND source_manifest_json = ?
            ORDER BY city_processing_runs.updated_at DESC
            LIMIT 1
            """,
            (
                city_name,
                str(mosaic_tif.resolve()),
                tile_ids_json,
                sources_json,
            ),
        ).fetchone()


def write_city_record(
    database: Path,
    *,
    run_id: str,
    city_name: str,
    city_code: str,
    boundary_path: Path,
    boundary_source_crs_wkt: str,
    boundary_source_wkt: str,
    boundary_work_crs_wkt: str,
    boundary_work_wkt: str,
    tile_index_path: Path,
    tile_ids_json: str,
    sources_json: str,
    image_suffix: str,
    mosaic_tif: Path,
    clipped_tif: Path,
    status: str,
    error: str | None = None,
) -> None:
    now = datetime.now().astimezone().isoformat()
    with closing(sqlite3.connect(database, timeout=300.0)) as connection:
        initialize_database(connection)
        connection.execute(
            """
            INSERT INTO city_processing_runs (
                run_id, city_name, city_code, city_boundary_path,
                boundary_source_crs_wkt, boundary_source_wkt,
                boundary_work_crs_wkt, boundary_work_wkt,
                tile_index_path, tile_ids_json, source_manifest_json,
                image_suffix, mosaic_tif, clipped_tif, status,
                created_at, completed_at, updated_at, error
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(run_id) DO UPDATE SET
                city_name = excluded.city_name,
                city_code = excluded.city_code,
                city_boundary_path = excluded.city_boundary_path,
                boundary_source_crs_wkt = excluded.boundary_source_crs_wkt,
                boundary_source_wkt = excluded.boundary_source_wkt,
                boundary_work_crs_wkt = excluded.boundary_work_crs_wkt,
                boundary_work_wkt = excluded.boundary_work_wkt,
                tile_index_path = excluded.tile_index_path,
                tile_ids_json = excluded.tile_ids_json,
                source_manifest_json = excluded.source_manifest_json,
                image_suffix = excluded.image_suffix,
                mosaic_tif = excluded.mosaic_tif,
                clipped_tif = excluded.clipped_tif,
                status = excluded.status,
                completed_at = excluded.completed_at,
                updated_at = excluded.updated_at,
                error = excluded.error
            """,
            (
                run_id,
                city_name,
                city_code,
                str(boundary_path.resolve()),
                boundary_source_crs_wkt,
                boundary_source_wkt,
                boundary_work_crs_wkt,
                boundary_work_wkt,
                str(tile_index_path.resolve()),
                tile_ids_json,
                sources_json,
                image_suffix,
                str(mosaic_tif.resolve()),
                str(clipped_tif.resolve()),
                status,
                now,
                now if status == "completed" else None,
                now,
                error,
            ),
        )
        connection.commit()


def clip_mosaic_to_city(
    source_path: Path,
    output_path: Path,
    boundary: gpd.GeoDataFrame,
    city_name: str,
    city_code: str,
    run_id: str,
    stripe_rows: int,
    make_overviews: bool,
) -> tuple[str, str]:
    """按市界包围窗口分条带裁剪，避免一次把全市影像读入内存。"""
    partial = partial_output_path(output_path)
    partial.unlink(missing_ok=True)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with rasterio.open(source_path, sharing=False) as source:
            projected = boundary.to_crs(source.crs)
            city_geometry = projected.geometry.union_all()
            if city_geometry is None or city_geometry.is_empty:
                raise ValueError(f"{city_name} 的投影后边界为空。")
            left = max(source.bounds.left, city_geometry.bounds[0])
            bottom = max(source.bounds.bottom, city_geometry.bounds[1])
            right = min(source.bounds.right, city_geometry.bounds[2])
            top = min(source.bounds.top, city_geometry.bounds[3])
            if left >= right or bottom >= top:
                raise ValueError(f"{city_name} 边界与完整镶嵌影像不相交。")
            raw = from_bounds(left, bottom, right, top, source.transform)
            col0 = max(0, int(math.floor(raw.col_off)))
            row0 = max(0, int(math.floor(raw.row_off)))
            col1 = min(source.width, int(math.ceil(raw.col_off + raw.width)))
            row1 = min(source.height, int(math.ceil(raw.row_off + raw.height)))
            crop_window = Window(col0, row0, col1 - col0, row1 - row0)
            profile = source.profile.copy()
            profile.update(
                driver="GTiff",
                width=int(crop_window.width),
                height=int(crop_window.height),
                transform=source.window_transform(crop_window),
                tiled=True,
                blockxsize=512,
                blockysize=512,
                compress="LZW",
                BIGTIFF="YES",
                SPARSE_OK="TRUE",
            )
            profile.pop("predictor", None)
            nodata = source.nodata if source.nodata is not None else 0
            profile["nodata"] = nodata
            with rasterio.open(partial, "w", **profile) as destination:
                stripe_count = math.ceil(int(crop_window.height) / stripe_rows)
                for stripe_index, row in enumerate(
                    range(0, int(crop_window.height), stripe_rows), start=1
                ):
                    rows = min(stripe_rows, int(crop_window.height) - row)
                    source_window = Window(
                        int(crop_window.col_off),
                        int(crop_window.row_off) + row,
                        int(crop_window.width),
                        rows,
                    )
                    destination_window = Window(
                        0, row, int(crop_window.width), rows
                    )
                    stripe_transform = source.window_transform(source_window)
                    inside = geometry_mask(
                        [mapping(city_geometry)],
                        out_shape=(rows, int(crop_window.width)),
                        transform=stripe_transform,
                        invert=True,
                    )
                    data = source.read(window=source_window)
                    data[:, ~inside] = nodata
                    destination.write(data, window=destination_window)
                    if stripe_index == 1 or stripe_index == stripe_count:
                        print(
                            f"    市界裁剪：{stripe_index}/{stripe_count} 条带",
                            flush=True,
                        )
                destination.update_tags(
                    CITY_NAME=city_name,
                    CITY_CODE=city_code,
                    ALIGNMENT_RUN_ID=run_id,
                    SOURCE_MOSAIC=str(source_path.resolve()),
                    CLIPPED_BY_CITY_BOUNDARY="true",
                )
            work_crs_wkt = source.crs.to_wkt()
            work_boundary_wkt = city_geometry.wkt
        partial.replace(output_path)
        if make_overviews:
            print("正在为市界裁剪成果创建金字塔……")
            build_overviews(output_path)
        return work_crs_wkt, work_boundary_wkt
    except Exception:
        partial.unlink(missing_ok=True)
        raise


def run_city_pipeline_serial(args: argparse.Namespace) -> None:
    boundary_path = args.city_boundary.resolve()
    tile_index_path = args.tile_index.resolve()
    if not boundary_path.is_file():
        raise FileNotFoundError(f"市边界文件不存在：{boundary_path}")
    if not tile_index_path.is_file():
        raise FileNotFoundError(f"分幅文件不存在：{tile_index_path}")
    if not args.input_dir.is_dir():
        raise NotADirectoryError(f"TIFF 输入目录不存在：{args.input_dir}")

    all_files = discover_files([args.input_dir])
    boundaries = gpd.read_file(boundary_path)
    if boundaries.crs is None:
        raise ValueError(f"市边界缺少坐标系：{boundary_path}")
    completed_outputs: list[Path] = []

    for local_position, requested_city in enumerate(args.cities, start=1):
        city_position = int(getattr(args, "city_sequence", local_position))
        city_total = int(getattr(args, "city_total", len(args.cities)))
        print(
            f"\n[城市 {city_position}/{city_total}] 开始准备：{requested_city}",
            flush=True,
        )
        official_name, tile_ids = find_intersecting_tiles(
            boundary_path,
            tile_index_path,
            requested_city,
            args.city_field,
            args.tile_field,
        )
        selected_name, selected_boundary = select_city(
            boundaries, args.city_field, requested_city
        )
        if selected_name != official_name:
            raise ValueError("市边界查询结果前后不一致。")
        city_code = ""
        if args.city_code_field in selected_boundary.columns:
            city_code = str(selected_boundary.iloc[0][args.city_code_field]).strip()
        city_sources = resolve_city_source_files(
            all_files,
            tile_ids,
            args.image_suffix,
            official_name,
        )
        safe_name = city_safe_name(official_name)
        city_dir = args.city_output_dir.resolve() / safe_name
        mosaic_path = city_dir / f"{safe_name}_完整镶嵌.tif"
        clipped_path = city_dir / f"{safe_name}_市界裁剪.tif"
        tile_ids_json = json.dumps(
            tile_ids, ensure_ascii=False, separators=(",", ":")
        )
        sources_json = source_manifest_json(city_sources)
        source_geometry = selected_boundary.geometry.union_all()
        source_crs_wkt = selected_boundary.crs.to_wkt()
        source_boundary_wkt = source_geometry.wkt
        database = (
            args.database_dir.resolve()
            / f"{safe_name}.sqlite"
        )
        if not args.check_only:
            database.parent.mkdir(parents=True, exist_ok=True)
            if args.overwrite:
                print(
                    f"[城市 {city_position}/{city_total}][{official_name}] "
                    f"收到 --overwrite，删除旧数据库并从头重建：{database}",
                    flush=True,
                )
                remove_sqlite_database(database)

        print(
            f"\n{'=' * 72}\n"
            f"[城市 {city_position}/{city_total}] {official_name}"
            f"（{city_code or '无代码'}）\n"
            f"该市待处理影像总数：{len(tile_ids)} 幅\n"
            f"成果目录：{city_dir}\n"
            f"独立数据库：{database}\n{'=' * 72}"
        )
        print("该市图幅号：" + "、".join(tile_ids), flush=True)

        reusable = find_reusable_city_record(
            database,
            official_name,
            mosaic_path,
            tile_ids_json,
            sources_json,
        )
        if (
            reusable is not None
            and reusable["status"] == "completed"
            and reusable["alignment_status"] == "completed"
            and mosaic_path.is_file()
            and clipped_path.is_file()
        ):
            print(
                f"[城市 {city_position}/{city_total}] {official_name} "
                f"成果已经完成，跳过：{clipped_path}",
                flush=True,
            )
            completed_outputs.append(clipped_path)
            continue

        city_args = copy.copy(args)
        city_args.output = mosaic_path
        city_args.database = database
        city_args.run_id = args.run_id if len(args.cities) == 1 else None
        city_args.checkpoint_dir = city_dir / f"{safe_name}_checkpoint"
        city_args.build_overviews = False

        if args.check_only:
            run_pipeline(city_args, city_sources)
            print(
                f"[城市 {city_position}/{city_total}] 检查完成："
                f"{official_name} 共 {len(city_sources)} 幅源影像。",
                flush=True,
            )
            continue

        run_id: str
        if (
            reusable is not None
            and reusable["alignment_status"] == "completed"
            and mosaic_path.is_file()
        ):
            run_id = str(reusable["run_id"])
            print(f"复用已完成的完整镶嵌：{mosaic_path}")
        else:
            print(
                f"[城市 {city_position}/{city_total}] 开始配准镶嵌："
                f"{official_name}，共 {len(city_sources)} 幅影像。",
                flush=True,
            )
            result = run_pipeline(city_args, city_sources)
            if result is None:
                raise RuntimeError(f"{official_name} 镶嵌没有返回 run_id。")
            run_id = result

        with rasterio.open(mosaic_path, sharing=False) as mosaic:
            work_boundary = selected_boundary.to_crs(mosaic.crs).geometry.union_all()
            work_crs_wkt = mosaic.crs.to_wkt()
            work_boundary_wkt = work_boundary.wkt
        write_city_record(
            database,
            run_id=run_id,
            city_name=official_name,
            city_code=city_code,
            boundary_path=boundary_path,
            boundary_source_crs_wkt=source_crs_wkt,
            boundary_source_wkt=source_boundary_wkt,
            boundary_work_crs_wkt=work_crs_wkt,
            boundary_work_wkt=work_boundary_wkt,
            tile_index_path=tile_index_path,
            tile_ids_json=tile_ids_json,
            sources_json=sources_json,
            image_suffix=args.image_suffix,
            mosaic_tif=mosaic_path,
            clipped_tif=clipped_path,
            status="mosaic_completed",
        )
        try:
            work_crs_wkt, work_boundary_wkt = clip_mosaic_to_city(
                mosaic_path,
                clipped_path,
                selected_boundary,
                official_name,
                city_code,
                run_id,
                args.stripe_rows,
                args.build_overviews,
            )
            write_city_record(
                database,
                run_id=run_id,
                city_name=official_name,
                city_code=city_code,
                boundary_path=boundary_path,
                boundary_source_crs_wkt=source_crs_wkt,
                boundary_source_wkt=source_boundary_wkt,
                boundary_work_crs_wkt=work_crs_wkt,
                boundary_work_wkt=work_boundary_wkt,
                tile_index_path=tile_index_path,
                tile_ids_json=tile_ids_json,
                sources_json=sources_json,
                image_suffix=args.image_suffix,
                mosaic_tif=mosaic_path,
                clipped_tif=clipped_path,
                status="completed",
            )
        except Exception as exc:
            write_city_record(
                database,
                run_id=run_id,
                city_name=official_name,
                city_code=city_code,
                boundary_path=boundary_path,
                boundary_source_crs_wkt=source_crs_wkt,
                boundary_source_wkt=source_boundary_wkt,
                boundary_work_crs_wkt=work_crs_wkt,
                boundary_work_wkt=work_boundary_wkt,
                tile_index_path=tile_index_path,
                tile_ids_json=tile_ids_json,
                sources_json=sources_json,
                image_suffix=args.image_suffix,
                mosaic_tif=mosaic_path,
                clipped_tif=clipped_path,
                status="failed",
                error=str(exc),
            )
            raise
        completed_outputs.append(clipped_path)
        print(
            f"[城市 {city_position}/{city_total}] {official_name} "
            f"全部完成（{len(city_sources)} 幅）：{clipped_path}",
            flush=True,
        )

    if args.check_only:
        print("\n所有城市检查完成；没有写入 TIFF 或 SQLite 批次。")
    else:
        print(f"\n城市处理全部完成：{len(completed_outputs)} 个成果。")
        for path in completed_outputs:
            print(path)


def run_city_process(city_args: argparse.Namespace) -> None:
    """在独立 Python 子进程中执行一个城市的完整原有流水线。"""
    city_sequence = int(city_args.city_sequence)
    city_total = int(city_args.city_total)
    city = city_args.cities[0]
    print(
        f"[城市 {city_sequence}/{city_total}][{city}] "
        f"Python 子进程启动，PID={os.getpid()}",
        flush=True,
    )
    with city_process_lock(
        city_args.database_dir,
        city,
        city_sequence,
        city_total,
    ):
        with rasterio.Env(
            GDAL_CACHEMAX=city_args.gdal_cache_mb * 1024 * 1024,
        ):
            run_city_pipeline_serial(city_args)


def run_city_pipeline(args: argparse.Namespace) -> None:
    """每个城市启动一个互相独立的 Python 子进程。"""

    worker_count = len(args.cities)
    print(
        f"\n城市多进程模式：{len(args.cities)} 个城市，"
        f"启动 {worker_count} 个独立 Python 子进程。"
    )
    print("本次城市清单：")
    for position, city in enumerate(args.cities, start=1):
        print(f"  {position:02d}/{len(args.cities):02d}  {city}")
    failures: list[tuple[str, Exception]] = []

    # 使用 spawn，避免从已经初始化 GDAL/rasterio 的父进程直接 fork。
    with ProcessPoolExecutor(
        max_workers=worker_count,
        mp_context=mp.get_context("spawn"),
    ) as executor:
        futures = {}
        for position, city in enumerate(args.cities, start=1):
            city_args = copy.copy(args)
            city_args.cities = [city]
            city_args.run_id = None
            city_args.city_sequence = position
            city_args.city_total = len(args.cities)
            future = executor.submit(run_city_process, city_args)
            futures[future] = (position, city)
        for future in as_completed(futures):
            position, city = futures[future]
            try:
                future.result()
                print(
                    f"\n[城市 {position}/{len(args.cities)}][{city}] 处理完成",
                    flush=True,
                )
            except Exception as exc:
                failures.append((city, exc))
                print(
                    f"\n[城市 {position}/{len(args.cities)}][{city}] 处理失败：{exc}",
                    file=sys.stderr,
                    flush=True,
                )

    if failures:
        details = "；".join(f"{city}: {error}" for city, error in failures)
        raise RuntimeError(f"{len(failures)} 个城市处理失败：{details}")
    print(f"\n全部城市并行处理完成：{len(args.cities)} 个。")


def main() -> None:
    args = parse_args()
    print(
        f"运行资源：线程={args.threads}；GDAL 缓存={args.gdal_cache_mb} MB；"
        f"warp 内存上限={args.warp_mem_limit_mb} MB"
    )
    if args.cities:
        run_city_pipeline(args)
    else:
        with rasterio.Env(
            GDAL_CACHEMAX=args.gdal_cache_mb * 1024 * 1024,
        ):
            run_pipeline(args)


if __name__ == "__main__":
    main()
