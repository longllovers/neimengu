# ----------------------------------------
# Authored by DF416
# Modified for:
# 1. multi-core parallel processing
# 2. per-file timing statistics
# 3. polygons-per-second statistics
# 4. optional keeping all polygons without deleting class=0 polygons
# 5. stable backfill to original polygons by explicit position column
# ----------------------------------------

import os
import glob
import time
import csv
import traceback
import numpy as np
from tqdm import tqdm
import multiprocessing as mp
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor, as_completed

import fiona
import rasterio
from osgeo import gdal
import geopandas as gpd
from pyproj import Transformer
from rasterio.features import rasterize
from rasterio.windows import from_bounds
from shapely.geometry import Point, box
from shapely.ops import transform
import argparse
import json
import re
import sqlite3



# -------------------- CONFIG --------------------
MIN_BACKGROUND_THRESHOLD = 0.5
MIN_CLASS_AREA_MU = 999999999
CLASS_FIELD = "class"
NUM_WORKERS = 40
SHP_CACHE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "save.sqlite3")
LEGACY_SHP_CACHE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "save.json")
MU_SQUARE_METERS = 666.6666666667
DEFAULT_INDEX_CONCURRENCY = 4
SHP_TASK_PRECHECK_WORKERS = 4
SHP_INDEX_WORKERS = DEFAULT_INDEX_CONCURRENCY
SPAWN_CONTEXT = mp.get_context("spawn")

# True:
#   不删除任何原始矢量面，只新增/更新 class 字段。
#   没有有效分类结果的 polygon，class = 0。
#
# False:
#   保持原始逻辑，删除 class = 0 的 polygon，只输出有效分类 polygon。
KEEP_ALL_POLYGONS = False
# ------------------------------------------------


def parse_class_mapping(text):
    """解析“栅格值=类别名称”，支持换行及中英文分号分隔。"""
    mapping = {}
    entries = re.split(r"[;；\r\n]+", str(text or ""))
    for entry_number, entry in enumerate(entries, start=1):
        entry = entry.strip()
        if not entry:
            continue
        parts = [part.strip() for part in re.split(r"[=：:]", entry, maxsplit=1)]
        if len(parts) != 2 or not all(parts):
            raise ValueError(
                f"class 映射第 {entry_number} 项格式错误：{entry!r}；"
                "正确格式为 栅格值=类别名称"
            )
        class_text, crop_name = parts
        try:
            class_value = int(class_text)
        except ValueError as exc:
            raise ValueError(f"class 值必须是非负整数：{class_text!r}") from exc
        if class_value < 0:
            raise ValueError(f"class 值必须是非负整数：{class_value}")
        if class_value in mapping:
            raise ValueError(f"class 值 {class_value} 重复配置")
        mapping[class_value] = crop_name
    if not mapping:
        raise ValueError("至少需要填写一项 class 类别映射")
    if 0 not in mapping:
        mapping[0] = "背景或无有效分类"
    return mapping


def normalize_classification_map(cls_map, class_mapping=None):
    """按运行模式规范分类值：二分类归一为 1，多分类保留原类别值。"""
    cls_map = np.asarray(cls_map)
    if class_mapping is None:
        return np.where(cls_map > 0, 1, 0).astype(np.int32)

    valid_values = np.fromiter(class_mapping.keys(), dtype=np.int32)
    return np.where(np.isin(cls_map, valid_values), cls_map, 0).astype(np.int32)


def filter_polygons_by_image_box(gdf, image_box, keep_all_polygons=False):
    """
    keep_all_polygons=False:
        保持原始行为，只保留完全位于影像范围内的地块。

    keep_all_polygons=True:
        不在这里删除地块。
        后续只对影像范围内的地块赋值，不在影像范围内的地块 class 保持 0。
    """
    if keep_all_polygons:
        return gdf.copy()

    return gdf[gdf.geometry.within(image_box)].copy()


def rasterize_polygons(gdf, transform, width, height, attribute):
    shapes = ((geom, val) for geom, val in zip(gdf.geometry, gdf[attribute]))

    return rasterize(
        shapes=shapes,
        out_shape=(height, width),
        transform=transform,
        fill=0,
        dtype=np.int32,
        all_touched=True,
    )


def get_pixel_area_mu(transform):
    pixel_area_square_meters = abs(transform.a * transform.e - transform.b * transform.d)
    return pixel_area_square_meters / MU_SQUARE_METERS


def vote_by_bincount(
    cls_map,
    polygon_id_map,
    n_polygons,
    min_background_threshold=MIN_BACKGROUND_THRESHOLD,
    min_class_area_mu=MIN_CLASS_AREA_MU,
    pixel_area_mu=0.0,
):
    flat_poly = polygon_id_map.ravel()
    flat_cls = cls_map.ravel()

    in_polygon = flat_poly > 0
    if not np.any(in_polygon):
        return {}

    poly_all = flat_poly[in_polygon]
    cls_all = flat_cls[in_polygon]

    total_count = np.bincount(poly_all, minlength=n_polygons + 1)
    background_count = np.bincount(
        poly_all[cls_all == 0],
        minlength=n_polygons + 1,
    )

    valid = cls_all > 0
    if not np.any(valid):
        return {}

    poly_valid = poly_all[valid]
    cls_valid = cls_all[valid]

    best_class = np.zeros(n_polygons + 1, dtype=np.int32)
    best_count = np.zeros(n_polygons + 1, dtype=np.int64)

    max_class = int(cls_valid.max())

    if max_class <= 4096:
        pair_index = poly_valid * (max_class + 1) + cls_valid
        pair_count = np.bincount(pair_index)

        nonzero_pair = np.flatnonzero(pair_count)
        poly_ids = nonzero_pair // (max_class + 1)
        cls_ids = nonzero_pair % (max_class + 1)
        counts = pair_count[nonzero_pair]
    else:
        pairs = np.column_stack((poly_valid, cls_valid))
        unique_pairs, counts = np.unique(pairs, axis=0, return_counts=True)
        poly_ids = unique_pairs[:, 0]
        cls_ids = unique_pairs[:, 1]

    for pid, cid, count in zip(poly_ids, cls_ids, counts):
        if pid > n_polygons:
            continue
        if count > best_count[pid]:
            best_count[pid] = count
            best_class[pid] = cid

    keep = np.zeros(n_polygons + 1, dtype=bool)
    nonempty = total_count > 0
    background_ok = np.zeros(n_polygons + 1, dtype=bool)
    background_ok[nonempty] = (
        background_count[nonempty] / total_count[nonempty]
        <= min_background_threshold
    )

    class_area_ok = np.zeros(n_polygons + 1, dtype=bool)
    if min_class_area_mu > 0 and pixel_area_mu > 0:
        class_area_ok = best_count * pixel_area_mu > min_class_area_mu

    keep = background_ok | class_area_ok
    keep &= best_class > 0

    return {
        int(pid): int(best_class[pid])
        for pid in np.flatnonzero(keep)
        if pid != 0
    }


def get_pixels_within_polygon(polygon, transform, cls_map):
    buffer_radius = abs(transform.a) * 0.3
    inverse_transform = ~transform

    minx, miny, maxx, maxy = polygon.bounds

    px_min, py_min = map(int, inverse_transform * (minx, maxy))
    px_max, py_max = map(int, inverse_transform * (maxx, miny))

    px_min = max(0, px_min)
    py_min = max(0, py_min)
    px_max = min(cls_map.shape[1] - 1, px_max)
    py_max = min(cls_map.shape[0] - 1, py_max)

    values = []

    for y in range(py_min, py_max + 1):
        for x in range(px_min, px_max + 1):
            lon, lat = transform * (x + 0.5, y + 0.5)
            point = Point(lon, lat).buffer(buffer_radius)

            if polygon.intersects(point):
                values.append(cls_map[y, x])

    return values


def fallback_vote_for_missing(
    gdf,
    raster_poly_id,
    window_transform,
    cls_map,
    min_background_threshold=MIN_BACKGROUND_THRESHOLD,
    min_class_area_mu=MIN_CLASS_AREA_MU,
    pixel_area_mu=0.0,
):
    raster_poly_ids = set(np.unique(raster_poly_id))
    shp_poly_ids = set(gdf["poly_id"].to_numpy())

    missing_poly_ids = shp_poly_ids.difference(raster_poly_ids)
    if not missing_poly_ids:
        return {}

    result = {}
    gdf_indexed = gdf.set_index("poly_id")

    for poly_id in missing_poly_ids:
        polygon = gdf_indexed.loc[poly_id].geometry
        pixel_values = get_pixels_within_polygon(
            polygon,
            window_transform,
            cls_map,
        )

        if len(pixel_values) == 0:
            continue

        pixel_values = np.asarray(pixel_values, dtype=np.int32)

        valid_values = pixel_values[pixel_values > 0]
        if len(valid_values) == 0:
            continue

        value_counts = np.bincount(valid_values)
        best_class = int(value_counts.argmax())
        best_count = int(value_counts[best_class])

        background_ratio = np.count_nonzero(pixel_values == 0) / len(pixel_values)
        background_ok = background_ratio <= min_background_threshold
        class_area_ok = (
            min_class_area_mu > 0
            and pixel_area_mu > 0
            and best_count * pixel_area_mu > min_class_area_mu
        )

        if not (background_ok or class_area_ok):
            continue

        result[int(poly_id)] = best_class

    return result


def _finish_stat(stat, start_time):
    elapsed = time.perf_counter() - start_time
    stat["elapsed_sec"] = elapsed

    processed = stat.get("covered_polygons", 0)
    if elapsed > 0 and processed > 0:
        stat["polygons_per_sec"] = processed / elapsed
    else:
        stat["polygons_per_sec"] = 0.0

    return stat


def save_stats_csv(stats, csv_path):
    if len(stats) == 0:
        return

    fieldnames = [
        "shp",
        "status",
        "input_polygons",
        "covered_polygons",
        "output_polygons",
        "elapsed_sec",
        "polygons_per_sec",
        "out_path",
        "error",
    ]

    with open(csv_path, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()

        for s in stats:
            row = s.copy()
            row["elapsed_sec"] = round(row["elapsed_sec"], 6)
            row["polygons_per_sec"] = round(row["polygons_per_sec"], 3)
            writer.writerow(row)


def majority_vote_by_polygon(args):
    (
        shp_path,
        cls_tif,
        out_shp_path,
        keep_all_polygons,
        min_background_threshold,
        min_class_area_mu,
        class_mapping,
    ) = args

    start_time = time.perf_counter()
    base_name = os.path.basename(shp_path)

    stat = {
        "shp": base_name,
        "status": "UNKNOWN",
        "input_polygons": 0,
        "covered_polygons": 0,
        "output_polygons": 0,
        "elapsed_sec": 0.0,
        "polygons_per_sec": 0.0,
        "out_path": out_shp_path,
        "error": "",
    }

    try:
        with rasterio.open(cls_tif) as src:
            tif_crs = src.crs
            image_box = box(*src.bounds)

            gdf_original = gpd.read_file(shp_path).copy()
            original_crs = gdf_original.crs

            stat["input_polygons"] = len(gdf_original)

            if gdf_original.empty:
                stat["status"] = "SKIP_EMPTY_SHP"
                return _finish_stat(stat, start_time)

            if original_crs is None:
                raise ValueError(
                    f"{base_name} has no CRS. Please define its original CRS first."
                )

            # 用显式列记录原始位置，完全不依赖 DataFrame index
            gdf_original = gdf_original.reset_index(drop=True)
            gdf_original["_orig_pos"] = np.arange(len(gdf_original), dtype=np.int64)
            gdf_original[CLASS_FIELD] = 0

            if gdf_original.crs != tif_crs:
                gdf_work = gdf_original.to_crs(tif_crs)
            else:
                gdf_work = gdf_original.copy()

            if keep_all_polygons:
                process_mask = gdf_work.geometry.within(image_box)
                gdf_process = gdf_work[process_mask].copy()
            else:
                gdf_process = filter_polygons_by_image_box(
                    gdf_work,
                    image_box,
                    keep_all_polygons=False,
                )

            stat["covered_polygons"] = len(gdf_process)

            if gdf_process.empty:
                if keep_all_polygons:
                    gdf_original = gdf_original.drop(columns=["_orig_pos"])
                    gdf_original.to_file(out_shp_path)

                    stat["output_polygons"] = len(gdf_original)
                    stat["status"] = "DONE_KEEP_ALL_NO_POLYGONS_IN_IMAGE"
                    return _finish_stat(stat, start_time)

                stat["status"] = "SKIP_NO_POLYGONS_IN_IMAGE"
                print(f"[SKIP] {base_name}: no polygons in image range.")
                return _finish_stat(stat, start_time)

            bounds = gdf_process.total_bounds
            window = from_bounds(*bounds, transform=src.transform)
            window = window.round_offsets(op="floor", pixel_precision=3)

            window_transform = src.window_transform(window)
            cls_map = src.read(1, window=window).astype(np.int32)

        # 只有二分类模式才统一赋值为 1；多分类模式保留每个像元原本的类别值。
        # 多分类中未配置的栅格值按背景 0 处理。
        cls_map = normalize_classification_map(cls_map, class_mapping)

        if np.all(cls_map == 0):
            if keep_all_polygons:
                gdf_original = gdf_original.drop(columns=["_orig_pos"])
                gdf_original.to_file(out_shp_path)

                stat["output_polygons"] = len(gdf_original)
                stat["status"] = "DONE_KEEP_ALL_ALL_ZERO"
                return _finish_stat(stat, start_time)

            stat["status"] = "SKIP_ALL_ZERO"
            print(f"[SKIP] {base_name}: clip range is all 0.")
            return _finish_stat(stat, start_time)

        height, width = cls_map.shape
        pixel_area_mu = get_pixel_area_mu(window_transform)

        # 这里 reset_index 只为了让 gdf_process 自己行号干净，不再用于回填
        gdf_process = gdf_process.reset_index(drop=True)
        gdf_process["poly_id"] = np.arange(1, len(gdf_process) + 1, dtype=np.int32)

        raster_poly_id = rasterize_polygons(
            gdf_process,
            window_transform,
            width,
            height,
            attribute="poly_id",
        )

        poly_id_to_class = vote_by_bincount(
            cls_map,
            raster_poly_id,
            len(gdf_process),
            min_background_threshold,
            min_class_area_mu,
            pixel_area_mu,
        )

        poly_id_to_class.update(
            fallback_vote_for_missing(
                gdf_process,
                raster_poly_id,
                window_transform,
                cls_map,
                min_background_threshold,
                min_class_area_mu,
                pixel_area_mu,
            )
        )

        gdf_process[CLASS_FIELD] = (
            gdf_process["poly_id"].map(poly_id_to_class).fillna(0).astype(int)
        )

        if keep_all_polygons:
            update_df = gdf_process[["_orig_pos", CLASS_FIELD]].copy()
            row_pos = update_df["_orig_pos"].to_numpy(dtype=np.int64)
            col_pos = gdf_original.columns.get_loc(CLASS_FIELD)

            gdf_original.iloc[row_pos, col_pos] = update_df[CLASS_FIELD].to_numpy()
            gdf_original = gdf_original.drop(columns=["_orig_pos"])
            gdf_original = gdf_original.set_crs(original_crs, allow_override=True)

            stat["output_polygons"] = len(gdf_original)

            gdf_original.to_file(out_shp_path)
            stat["status"] = "DONE_KEEP_ALL"

            return _finish_stat(stat, start_time)

        else:
            valid_process = gdf_process[gdf_process[CLASS_FIELD] != 0].copy()

            if len(valid_process) == 0:
                stat["status"] = "SKIP_NO_VALID_CLASS"
                print(f"[SKIP] {base_name}: the parcel shapefile has no valid class.")
                return _finish_stat(stat, start_time)

            valid_pos = valid_process["_orig_pos"].to_numpy(dtype=np.int64)
            valid_class = valid_process[CLASS_FIELD].to_numpy(dtype=np.int32)

            gdf_out = gdf_original.iloc[valid_pos].copy()
            gdf_out[CLASS_FIELD] = valid_class
            gdf_out = gdf_out.drop(columns=["_orig_pos"])
            gdf_out = gdf_out.set_crs(original_crs, allow_override=True)
            stat["output_polygons"] = len(gdf_out)

            gdf_out.to_file(out_shp_path)
            stat["status"] = "DONE"

            return _finish_stat(stat, start_time)

    except Exception as exc:
        stat["status"] = "ERROR"
        stat["error"] = f"{repr(exc)}\n{traceback.format_exc()}"
        print(f"[ERROR] task {base_name} failed: {exc}")
        return _finish_stat(stat, start_time)


def _load_filter_area(filter_area_path):
    if not filter_area_path or not os.path.exists(filter_area_path):
        return None, None

    area_gdf = gpd.read_file(filter_area_path)
    return area_gdf.unary_union, area_gdf.crs


def _shp_intersects_area(shp_bounds, shp_crs, target_geom, target_crs):
    if target_geom is None:
        return True

    if shp_crs != target_crs:
        transformer = Transformer.from_crs(
            shp_crs,
            target_crs,
            always_xy=True,
        ).transform
        shp_bounds = transform(transformer, shp_bounds)

    return shp_bounds.intersects(target_geom)


def _shp_intersects_image(shp_bounds, shp_crs, image_bounds, image_crs):
    if shp_crs != image_crs:
        transformer = Transformer.from_crs(
            shp_crs,
            image_crs,
            always_xy=True,
        ).transform
        shp_bounds = transform(transformer, shp_bounds)

    return shp_bounds.intersects(image_bounds)


def _prepare_vote_task(
    shp_number,
    shp_file,
    number_width,
    cls_tif,
    out_dir,
    keep_all_polygons,
    min_background_threshold,
    min_class_area_mu,
    class_mapping,
    resume,
    filter_area_geom,
    filter_area_crs,
    image_bounds,
    image_crs,
):
    """并发读取单个 SHP 的范围，返回任务或明确的跳过原因。"""
    base_name = os.path.splitext(os.path.basename(shp_file))[0]
    numbered_base_name = f"{base_name}_{shp_number:0{number_width}d}"
    out_shp_path = os.path.join(out_dir, f"{numbered_base_name}.shp")
    if resume and os.path.exists(out_shp_path):
        return shp_number, base_name, None, "resume", None

    try:
        with fiona.open(shp_file, "r") as source:
            shp_bounds = box(*source.bounds)
            shp_crs = source.crs_wkt or source.crs
        if not _shp_intersects_area(
            shp_bounds, shp_crs, filter_area_geom, filter_area_crs
        ):
            return shp_number, base_name, None, "outside_filter", None
        if not _shp_intersects_image(
            shp_bounds, shp_crs, image_bounds, image_crs
        ):
            return shp_number, base_name, None, "outside_image", None
    except Exception as exc:
        return shp_number, base_name, None, "error", repr(exc)

    task = (
        shp_file,
        cls_tif,
        out_shp_path,
        keep_all_polygons,
        min_background_threshold,
        min_class_area_mu,
        class_mapping,
    )
    return shp_number, base_name, task, "ready", None


def _empty_shp_cache():
    return {
        "version": 4,
        "built_roots": [],
        "directories": {},
        "cities": {},
        "counties": {},
        "unmatched_boundary_files": {},
    }


def _load_legacy_shp_cache(legacy_path=LEGACY_SHP_CACHE_PATH):
    """读取旧 JSON 索引，仅供首次迁移到 SQLite。"""
    if not os.path.exists(legacy_path) or os.path.getsize(legacy_path) == 0:
        return _empty_shp_cache()
    try:
        with open(legacy_path, "r", encoding="utf-8") as cache_file:
            cache = json.load(cache_file)
    except (OSError, json.JSONDecodeError) as exc:
        print(f"[WARNING] Cannot migrate legacy SHP cache: {exc}", flush=True)
        return _empty_shp_cache()
    if not isinstance(cache, dict) or not isinstance(cache.get("directories"), dict):
        print("[WARNING] Invalid legacy SHP cache; it will be rebuilt.", flush=True)
        return _empty_shp_cache()
    cache.setdefault("built_roots", [])
    cache.setdefault("cities", {})
    cache.setdefault("counties", {})
    cache.setdefault("unmatched_boundary_files", {})
    # version 4 使用市、县两张反向索引，不再保存逐文件 file_regions 表。
    cache.pop("regions", None)
    cache.pop("file_regions", None)
    cache["version"] = 4
    return cache


def _connect_shp_cache(cache_path=SHP_CACHE_PATH):
    """创建支持并发读取、事务写入的 SQLite 索引连接。"""
    cache_dir = os.path.dirname(os.path.abspath(cache_path))
    os.makedirs(cache_dir, exist_ok=True)
    deadline = time.monotonic() + 60
    while True:
        connection = sqlite3.connect(cache_path, timeout=60)
        try:
            connection.execute("PRAGMA busy_timeout = 60000")
            current_mode = connection.execute("PRAGMA journal_mode").fetchone()[0]
            if str(current_mode).lower() != "wal":
                connection.execute("PRAGMA journal_mode = WAL")
            connection.execute("PRAGMA synchronous = NORMAL")
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS shp_cache_roots (
                    root_key TEXT PRIMARY KEY,
                    files_json TEXT NOT NULL,
                    cities_json TEXT NOT NULL,
                    counties_json TEXT NOT NULL,
                    unmatched_json TEXT NOT NULL,
                    updated_at REAL NOT NULL
                )
                """
            )
            connection.commit()
            return connection
        except sqlite3.OperationalError as exc:
            connection.close()
            if "locked" not in str(exc).lower() or time.monotonic() >= deadline:
                raise
            time.sleep(0.05)


def _save_shp_cache(cache, cache_path=SHP_CACHE_PATH, root_keys=None):
    """按 SHP 根目录事务写入；SQLite 负责并发写入排队和故障回滚。"""
    if root_keys is None:
        roots = sorted(
            set(cache.get("built_roots", []))
            | set(cache.get("directories", {}))
        )
    else:
        roots = sorted(set(root_keys))
    connection = _connect_shp_cache(cache_path)
    try:
        with connection:
            connection.execute("BEGIN IMMEDIATE")
            for root_key in roots:
                connection.execute(
                """
                INSERT INTO shp_cache_roots (
                    root_key, files_json, cities_json, counties_json,
                    unmatched_json, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(root_key) DO UPDATE SET
                    files_json=excluded.files_json,
                    cities_json=excluded.cities_json,
                    counties_json=excluded.counties_json,
                    unmatched_json=excluded.unmatched_json,
                    updated_at=excluded.updated_at
                """,
                    (
                        root_key,
                        json.dumps(cache.get("directories", {}).get(root_key, []), ensure_ascii=False),
                        json.dumps(cache.get("cities", {}).get(root_key, {}), ensure_ascii=False),
                        json.dumps(cache.get("counties", {}).get(root_key, {}), ensure_ascii=False),
                        json.dumps(
                            cache.get("unmatched_boundary_files", {}).get(root_key, []),
                            ensure_ascii=False,
                        ),
                        time.time(),
                    ),
                )
    finally:
        connection.close()


def _load_shp_cache(cache_path=SHP_CACHE_PATH):
    cache = _empty_shp_cache()
    try:
        connection = _connect_shp_cache(cache_path)
        try:
            rows = connection.execute(
                """
                SELECT root_key, files_json, cities_json, counties_json, unmatched_json
                FROM shp_cache_roots
                ORDER BY root_key
                """
            ).fetchall()
        finally:
            connection.close()
    except (sqlite3.Error, OSError) as exc:
        print(f"[WARNING] Cannot read SQLite SHP cache, rebuilding it: {exc}", flush=True)
        return cache

    if not rows and os.path.abspath(cache_path) == os.path.abspath(SHP_CACHE_PATH):
        legacy_cache = _load_legacy_shp_cache()
        if legacy_cache["built_roots"]:
            try:
                _save_shp_cache(legacy_cache, cache_path)
                print(
                    f"[INFO] Migrated legacy SHP cache to SQLite: {cache_path}",
                    flush=True,
                )
                return legacy_cache
            except (sqlite3.Error, OSError) as exc:
                print(f"[WARNING] Legacy SHP cache migration failed: {exc}", flush=True)

    for root_key, files_text, cities_text, counties_text, unmatched_text in rows:
        try:
            cache["directories"][root_key] = json.loads(files_text)
            cache["cities"][root_key] = json.loads(cities_text)
            cache["counties"][root_key] = json.loads(counties_text)
            cache["unmatched_boundary_files"][root_key] = json.loads(unmatched_text)
            cache["built_roots"].append(root_key)
        except (TypeError, json.JSONDecodeError) as exc:
            print(f"[WARNING] Ignoring invalid SQLite cache row {root_key!r}: {exc}", flush=True)
    cache["built_roots"].sort()
    return cache


def _normalize_region_name(value):
    return re.sub(r"\s+", "", str(value or "")).strip()


def _split_region_names(value):
    """按中英文分号拆分多个行政区名称，并保持输入顺序去重。"""
    names = []
    seen = set()
    for item in re.split(r"[;；]", str(value or "")):
        name = _normalize_region_name(item)
        if name and name not in seen:
            names.append(name)
            seen.add(name)
    return names


def _region_name_aliases(value):
    """同时支持“乌兰察布”和“乌兰察布市”这样的输入。"""
    name = _normalize_region_name(value)
    if not name:
        return set()

    aliases = {name}
    for suffix in ("自治旗", "市辖区", "市", "盟", "县", "旗", "区"):
        if name.endswith(suffix) and len(name) > len(suffix):
            aliases.add(name[:-len(suffix)])
            break
    return aliases


def _names_from_shp_path(shp_path, shp_dir):
    """从行政区目录名中提取名称，例如 150921卓资县 -> 卓资县。"""
    names = set()
    relative_path = os.path.relpath(shp_path, shp_dir)
    path_parts = [os.path.basename(os.path.normpath(shp_dir))]
    path_parts.extend(os.path.normpath(relative_path).split(os.sep)[:-1])
    for part in path_parts:
        part = _normalize_region_name(part)
        part = re.sub(r"^\d{4,12}", "", part)
        if part and part not in {"耕地矢量", "原始", "00市边界", "00县边界"}:
            if part.endswith(("市", "县", "旗", "区", "自治旗")):
                names.add(part)
    return names


def _find_boundary_shapefiles(shp_dir):
    """查找约定的 00市边界、00县边界，优先使用输入目录附近的数据。"""
    candidates = []
    current = os.path.abspath(shp_dir)
    for _ in range(5):
        for dirname in ("00市边界", "00县边界"):
            candidates.extend(glob.glob(os.path.join(current, dirname, "*.shp")))
        parent = os.path.dirname(current)
        if parent == current:
            break
        current = parent

    # 项目内部也可能集中存放边界文件。
    for dirname in ("00市边界", "00县边界"):
        candidates.extend(
            glob.glob(os.path.join(os.path.dirname(SHP_CACHE_PATH), "**", dirname, "*.shp"), recursive=True)
        )
    return sorted(set(os.path.abspath(path) for path in candidates))


def _load_boundary_features(shp_dir):
    """读取市县名称和边界，供首次建立名称索引时做空间匹配。"""
    boundary_features = []
    name_fields = ("市名称", "市名", "area_name", "县名称", "县名", "QXMC", "NAME", "name")
    boundary_paths = _find_boundary_shapefiles(shp_dir)
    print(f"[INFO] Found {len(boundary_paths)} boundary shapefile(s).", flush=True)
    for boundary_number, boundary_path in enumerate(boundary_paths, start=1):
        before_count = len(boundary_features)
        print(
            f"[BOUNDARY] {boundary_number}/{len(boundary_paths)} loading: {boundary_path}",
            flush=True,
        )
        try:
            with fiona.open(boundary_path, "r") as source:
                boundary_crs = source.crs_wkt or source.crs
                for feature in source:
                    properties = feature.get("properties") or {}
                    region_name = next(
                        (properties.get(field) for field in name_fields if properties.get(field)),
                        None,
                    )
                    geometry = feature.get("geometry")
                    if region_name and geometry:
                        from shapely.geometry import shape
                        boundary_level = (
                            "city"
                            if os.path.basename(os.path.dirname(boundary_path)) == "00市边界"
                            else "county"
                        )
                        boundary_features.append(
                            (_normalize_region_name(region_name), shape(geometry), boundary_crs, boundary_level)
                        )
            loaded_count = len(boundary_features) - before_count
            print(
                f"[BOUNDARY] {boundary_number}/{len(boundary_paths)} completed: "
                f"{loaded_count} boundary feature(s)",
                flush=True,
            )
        except Exception as exc:
            print(f"[WARNING] Cannot read boundary file {boundary_path}: {exc}", flush=True)
    if boundary_features:
        print(
            f"[INFO] Loaded {len(boundary_features)} city/county boundaries for cache indexing.",
            flush=True,
        )
    return boundary_features


def _match_shp_to_boundaries(shp_path, boundary_features):
    """只读取和计算单个 SHP；不修改共享 SQLite 索引。"""
    matched_names = set()
    matched_cities = set()
    matched_counties = set()
    error = None
    if boundary_features:
        try:
            with fiona.open(shp_path, "r") as source:
                shp_bounds = box(*source.bounds)
                shp_crs = source.crs_wkt or source.crs
            transformed_bounds = {}
            for region_name, boundary_geometry, boundary_crs, boundary_level in boundary_features:
                cache_key = str(boundary_crs)
                if cache_key not in transformed_bounds:
                    transformed_bounds[cache_key] = shp_bounds
                    if shp_crs and boundary_crs and shp_crs != boundary_crs:
                        transformer = Transformer.from_crs(
                            shp_crs, boundary_crs, always_xy=True
                        ).transform
                        transformed_bounds[cache_key] = transform(transformer, shp_bounds)
                if transformed_bounds[cache_key].intersects(boundary_geometry):
                    matched_names.add(region_name)
                    if boundary_level == "city":
                        matched_cities.add(region_name)
                    else:
                        matched_counties.add(region_name)
        except Exception as exc:
            error = str(exc)
    return shp_path, matched_names, matched_cities, matched_counties, error


def _build_region_index(shp_files, shp_dir, concurrency_count=DEFAULT_INDEX_CONCURRENCY):
    city_index = {}
    county_index = {}
    unmatched_boundary_files = []
    boundary_features = _load_boundary_features(shp_dir)
    total_files = len(shp_files)
    if total_files == 0:
        print("[INFO] Region cache index completed: no shapefiles to index.", flush=True)
        return {}, {}, []
    active_concurrency = min(max(1, int(concurrency_count)), total_files)
    print(
        f"[INFO] Starting region cache index for {total_files} input shapefile(s) "
        f"with concurrency={active_concurrency}.",
        flush=True,
    )

    # 并发任务仅返回匹配结果；以下汇总逻辑统一执行，避免同时修改索引。
    with ProcessPoolExecutor(
        max_workers=active_concurrency,
        mp_context=SPAWN_CONTEXT,
    ) as executor:
        futures = {
            executor.submit(_match_shp_to_boundaries, shp_path, boundary_features): shp_path
            for shp_path in shp_files
        }
        for processed, future in enumerate(as_completed(futures), start=1):
            shp_path, matched_names, matched_cities, matched_counties, error = future.result()
            if error:
                print(f"[WARNING] Cannot index {shp_path} by boundary: {error}", flush=True)

            for region_name in matched_cities:
                city_index.setdefault(region_name, []).append(shp_path)
            for region_name in matched_counties:
                county_index.setdefault(region_name, []).append(shp_path)
            if not matched_names:
                unmatched_boundary_files.append(shp_path)

            matched_text = "、".join(sorted(matched_names)) if matched_names else "未匹配到市/县"
            print(
                f"[CACHE PROGRESS] {processed}/{total_files} completed: "
                f"{os.path.basename(shp_path)} -> {matched_text}",
                flush=True,
            )

    city_result = {
        name: sorted(set(paths))
        for name, paths in sorted(city_index.items())
    }
    county_result = {
        name: sorted(set(paths))
        for name, paths in sorted(county_index.items())
    }
    print(
        f"[INFO] Region cache index completed: {total_files} shapefile(s), "
        f"{len(city_result)} searchable city name(s), "
        f"{len(county_result)} searchable county name(s).",
        flush=True,
    )
    return city_result, county_result, sorted(unmatched_boundary_files)


def find_cultivated_land_shapefiles(
    shp_dir,
    cache_path=SHP_CACHE_PATH,
    refresh_cache=False,
    region_name=None,
    index_concurrency_count=DEFAULT_INDEX_CONCURRENCY,
):
    """优先从 JSON 缓存按市县名称读取，否则扫描并更新缓存。"""
    shp_dir = os.path.abspath(os.path.expanduser(shp_dir))
    if not os.path.isdir(shp_dir):
        raise ValueError(f"Shapefile directory does not exist: {shp_dir}")

    # normcase 使 Windows 下同一路径的大小写差异不会重复建立缓存。
    cache_key = os.path.normcase(os.path.normpath(shp_dir))
    cache = _load_shp_cache(cache_path)
    cached_files = cache["directories"].get(cache_key)
    cached_cities = cache["cities"].get(cache_key)
    cached_counties = cache["counties"].get(cache_key)
    query_names = _split_region_names(region_name)
    root_is_built = cache_key in cache["built_roots"]
    has_region_indexes = isinstance(cached_cities, dict) and isinstance(cached_counties, dict)
    if not refresh_cache and root_is_built and isinstance(cached_files, list) and has_region_indexes:
        print(f"[INFO] Loaded {len(cached_files)} shapefile(s) from cache: {cache_path}")
        shp_files = cached_files
    else:
        if refresh_cache:
            print(f"[INFO] Refreshing shapefile cache for: {shp_dir}")

        direct_files = sorted(glob.glob(os.path.join(shp_dir, "*.shp")))
        if direct_files:
            shp_files = direct_files
        else:
            shp_files = []
            for root, _, files in os.walk(shp_dir):
                parts = os.path.normpath(root).split(os.sep)
                if "耕地矢量" in parts:
                    shp_files.extend(
                        os.path.join(root, name)
                        for name in files
                        if name.lower().endswith(".shp")
                    )

        shp_files = sorted(shp_files)
        print(
            f"[INFO] Input shapefile scan completed: {len(shp_files)} file(s) found under {shp_dir}.",
            flush=True,
        )
        city_index, county_index, unmatched_boundary_files = _build_region_index(
            shp_files,
            shp_dir,
            concurrency_count=index_concurrency_count,
        )
        cache["directories"][cache_key] = shp_files
        cache["cities"][cache_key] = city_index
        cache["counties"][cache_key] = county_index
        cache["unmatched_boundary_files"][cache_key] = unmatched_boundary_files
        cache["built_roots"] = sorted(set(cache["built_roots"]) | {cache_key})
        # 只更新本次建立的根目录，避免并发任务用旧快照覆盖其他根目录的新索引。
        _save_shp_cache(cache, cache_path, root_keys=[cache_key])
        cached_cities = cache["cities"][cache_key]
        cached_counties = cache["counties"][cache_key]
        print(
            f"[INFO] Saved {len(shp_files)} shapefile(s) and "
            f"{len(cached_cities)} city name(s), {len(cached_counties)} county name(s) "
            f"to cache: {cache_path}"
        )
        print(
            f"[INFO] Boundary index saved: "
            f"{len(unmatched_boundary_files)} file(s) with no city/county boundary intersection."
        )

    if query_names:
        cached_cities = cache["cities"].get(cache_key, {})
        cached_counties = cache["counties"].get(cache_key, {})
        selected_files = set()
        for query_name in query_names:
            matched_files = sorted(set(cached_cities.get(query_name, [])) | set(cached_counties.get(query_name, [])))
            if not matched_files:
                available_names = sorted(
                    name for name in set(cached_cities) | set(cached_counties) if len(name) >= 2
                )
                preview = "、".join(available_names[:20]) or "无"
                raise ValueError(
                    f"在输入 SHP 缓存中找不到市/县名称“{query_name}”。"
                    f"可用名称示例：{preview}。如目录内容已变化，请点击页面上的“刷新 SHP 索引”。"
                )
            selected_files.update(matched_files)
            print(f"[INFO] Region '{query_name}' matched {len(matched_files)} shapefile(s) from cache.")
        shp_files = sorted(selected_files)
        print(f"[INFO] Selected {len(shp_files)} unique shapefile(s) for {len(query_names)} region(s).")

    return sorted(shp_files)


def build_tasks(
    shp_dir,
    cls_tif,
    out_dir,
    filter_area_path=None,
    keep_all_polygons=True,
    resume=True,
    min_background_threshold=MIN_BACKGROUND_THRESHOLD,
    min_class_area_mu=MIN_CLASS_AREA_MU,
    refresh_shp_cache=False,
    region_name=None,
    class_mapping=None,
):
    filter_area_geom, filter_area_crs = _load_filter_area(filter_area_path)

    with rasterio.open(cls_tif) as src:
        image_bounds = box(*src.bounds)
        image_crs = src.crs

    tasks = []

    shp_files = find_cultivated_land_shapefiles(
        shp_dir,
        refresh_cache=refresh_shp_cache,
        region_name=region_name,
        index_concurrency_count=SHP_INDEX_WORKERS,
    )
    print(f"[INFO] Found {len(shp_files)} shapefile(s) under: {shp_dir}")
    number_width = max(3, len(str(len(shp_files))))
    active_precheck_concurrency = min(
        SHP_TASK_PRECHECK_WORKERS,
        max(1, len(shp_files)),
    )
    print(
        f"[INFO] Checking {len(shp_files)} shapefile(s) with "
        f"concurrency={active_precheck_concurrency} before voting.",
        flush=True,
    )
    task_records = []
    errors = []
    # 此阶段只读取各 SHP 的元数据并做范围相交判断，I/O 占主导。
    # 线程无需重复启动解释器或序列化边界对象；真正的投票仍使用独立进程。
    with ThreadPoolExecutor(max_workers=active_precheck_concurrency) as executor:
        futures = [
            executor.submit(
                _prepare_vote_task,
                shp_number,
                shp_file,
                number_width,
                cls_tif,
                out_dir,
                keep_all_polygons,
                min_background_threshold,
                min_class_area_mu,
                class_mapping,
                resume,
                filter_area_geom,
                filter_area_crs,
                image_bounds,
                image_crs,
            )
            for shp_number, shp_file in enumerate(shp_files, start=1)
        ]
        for processed, future in enumerate(as_completed(futures), start=1):
            shp_number, base_name, task, status, error = future.result()
            if task is not None:
                task_records.append((shp_number, task))
            if error:
                errors.append((base_name, error))
            print(
                f"[TASK CHECK] {processed}/{len(shp_files)} "
                f"{status.upper()}: {base_name}",
                flush=True,
            )

    if errors:
        preview = "；".join(f"{name}: {error}" for name, error in errors[:10])
        raise RuntimeError(
            f"{len(errors)} 个 SHP 在投票预检查时读取失败：{preview}"
        )

    tasks = [task for _, task in sorted(task_records, key=lambda item: item[0])]
    print(
        f"[INFO] Task check completed: {len(tasks)}/{len(shp_files)} shapefile(s) "
        "intersect the classification image and are ready for voting.",
        flush=True,
    )
    return tasks


def run_single_tif(
    shp_dir,
    cls_tif,
    out_dir,
    resume=True,
    concurrency_enabled=True,
    filter_area_path=None,
    stats_csv="processing_stats.csv",
    keep_all_polygons=True,
    min_background_threshold=MIN_BACKGROUND_THRESHOLD,
    min_class_area_mu=MIN_CLASS_AREA_MU,
    refresh_shp_cache=False,
    region_name=None,
    class_mapping=None,
):
    os.makedirs(out_dir, exist_ok=True)

    total_start = time.perf_counter()

    tasks = build_tasks(
        shp_dir=shp_dir,
        cls_tif=cls_tif,
        out_dir=out_dir,
        filter_area_path=filter_area_path,
        keep_all_polygons=keep_all_polygons,
        resume=resume,
        min_background_threshold=min_background_threshold,
        min_class_area_mu=min_class_area_mu,
        refresh_shp_cache=refresh_shp_cache,
        region_name=region_name,
        class_mapping=class_mapping,
    )

    if len(tasks) == 0:
        print("[INFO] No tasks need to be processed.")
        return []

    print(
        f"[INFO] Tasks={len(tasks)}, concurrency={NUM_WORKERS if concurrency_enabled else 1}, "
        f"background_threshold={min_background_threshold}, "
        f"min_class_area_mu={min_class_area_mu}"
    )

    stats = []
    progress_step = max(1, len(tasks) // 10)

    if concurrency_enabled:
        with SPAWN_CONTEXT.Pool(processes=NUM_WORKERS) as pool:
            for processed, stat in enumerate(
                pool.imap_unordered(majority_vote_by_polygon, tasks), start=1
            ):
                stats.append(stat)
                if processed == 1 or processed % progress_step == 0 or processed == len(tasks):
                    print(f"[PROGRESS] {processed}/{len(tasks)} shapefiles")
    else:
        for processed, task in enumerate(tasks, start=1):
            stat = majority_vote_by_polygon(task)
            stats.append(stat)
            if processed == 1 or processed % progress_step == 0 or processed == len(tasks):
                print(f"[PROGRESS] {processed}/{len(tasks)} shapefiles")

    total_elapsed = time.perf_counter() - total_start

    stats_path = os.path.join(out_dir, stats_csv)
    save_stats_csv(stats, stats_path)

    done_stats = [s for s in stats if str(s["status"]).startswith("DONE")]
    valid_stats = [
        s for s in stats
        if s["covered_polygons"] > 0 and s["elapsed_sec"] > 0
    ]

    total_input_polygons = sum(s["input_polygons"] for s in stats)
    total_covered_polygons = sum(s["covered_polygons"] for s in stats)
    total_output_polygons = sum(s["output_polygons"] for s in stats)

    wall_speed = (
        total_covered_polygons / total_elapsed
        if total_elapsed > 0
        else 0
    )

    avg_file_speed = (
        sum(s["polygons_per_sec"] for s in valid_stats) / len(valid_stats)
        if len(valid_stats) > 0
        else 0
    )

    avg_file_time = (
        sum(s["elapsed_sec"] for s in valid_stats) / len(valid_stats)
        if len(valid_stats) > 0
        else 0
    )

    error_count = sum(1 for s in stats if s["status"] == "ERROR")
    print("[SUMMARY] Processing completed")
    print(f"[SUMMARY] Tasks={len(stats)}, done={len(done_stats)}, errors={error_count}")
    print(
        f"[SUMMARY] Polygons: input={total_input_polygons}, "
        f"covered={total_covered_polygons}, output={total_output_polygons}"
    )
    print(f"[SUMMARY] Time={total_elapsed:.3f}s, stats={stats_path}")

    return stats


def build_vrt(cls_path):
    tif_list = sorted(
        glob.glob(os.path.join(cls_path, "*.TIF"))
        + glob.glob(os.path.join(cls_path, "*.tif"))
    )

    vrt_path = os.path.join(cls_path, "mosaic.vrt")

    if len(tif_list) == 0:
        raise ValueError("No tif files found. please check the input cls path.")

    print(f"Building VRT from {len(tif_list)} tif files in {cls_path}.")

    vrt_options = gdal.BuildVRTOptions(
        resampleAlg="nearest",
        addAlpha=False,
    )

    gdal.BuildVRT(vrt_path, tif_list, options=vrt_options)

    print(f"VRT saved to: {vrt_path}.")

    return vrt_path


def run(
    shp_dir,
    cls_path,
    out_dir,
    cls_file_type="file",
    resume=True,
    concurrency_enabled=True,
    filter_area_path=None,
    stats_csv="processing_stats.csv",
    keep_all_polygons=True,
    min_background_threshold=MIN_BACKGROUND_THRESHOLD,
    min_class_area_mu=MIN_CLASS_AREA_MU,
    refresh_shp_cache=False,
    region_name=None,
    class_mapping=None,
):
    if cls_file_type == "file":
        return run_single_tif(
            shp_dir=shp_dir,
            cls_tif=cls_path,
            out_dir=out_dir,
            resume=resume,
            concurrency_enabled=concurrency_enabled,
            filter_area_path=filter_area_path,
            stats_csv=stats_csv,
            keep_all_polygons=keep_all_polygons,
            min_background_threshold=min_background_threshold,
            min_class_area_mu=min_class_area_mu,
            refresh_shp_cache=refresh_shp_cache,
            region_name=region_name,
            class_mapping=class_mapping,
        )

    if cls_file_type == "folder":
        vrt_path = build_vrt(cls_path)
        return run_single_tif(
            shp_dir=shp_dir,
            cls_tif=vrt_path,
            out_dir=out_dir,
            resume=resume,
            concurrency_enabled=concurrency_enabled,
            filter_area_path=filter_area_path,
            stats_csv=stats_csv,
            keep_all_polygons=keep_all_polygons,
            min_background_threshold=min_background_threshold,
            min_class_area_mu=min_class_area_mu,
            refresh_shp_cache=refresh_shp_cache,
            region_name=region_name,
            class_mapping=class_mapping,
        )

    raise ValueError("The data input format is not currently supported.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="投票统计脚本",
    )
    parser.add_argument(
    "--shp_dir",
    type=str,
    default="/media/cangling/EAGET/专题2_农作物种植用地遥感测量/种植用地-待修正-去除接边",
    help="Shapefile 文件夹路径，默认不需要修改"
    )

    parser.add_argument(
        "--cls_tif",
        type=str,
        help="分类结果 tif 文件路径"
    )

    parser.add_argument(
        "--out_dir",
        type=str,
        help="输出结果文件夹路径"
    )
    parser.add_argument(
        "--MIN_BACKGROUND_THRESHOLD", type=float, default=0.5,
        help="地块允许的最大背景像元比例，取值范围 0 到 1，默认 0.5",
    )
    parser.add_argument(
        "--MIN_CLASS_AREA_MU", type=float, default=MIN_CLASS_AREA_MU,
        help="主分类像元面积大于该亩数时直接保留地块，默认 1.0；设为 0 可关闭该规则",
    )
    parser.add_argument(
        "--region-name",
        type=str,
        default="",
        help="可选。输入一个或多个市/县名称，多个名称使用中文或英文分号分隔。",
    )
    parser.add_argument(
        "--multi-class",
        action="store_true",
        help="启用多分类，并使用 --class-mapping 限定有效类别。",
    )
    parser.add_argument(
        "--class-mapping",
        default="",
        help="多分类映射，格式：栅格值=类别名称；多项用换行或中英文分号分隔。",
    )
    parser.add_argument(
        "--refresh-shp-cache",
        action="store_true",
        help="忽略当前 shp_dir 的旧缓存，重新扫描并事务写入 SQLite 索引",
    )
    parser.add_argument(
        "--refresh-shp-cache-only",
        action="store_true",
        help="只刷新 SHP 文件及市县边界相交索引，不执行投票。",
    )
    parser.add_argument(
        "--ensure-shp-cache-only",
        action="store_true",
        help="只确保 SHP 索引存在；已有有效索引时直接读取，不强制刷新。",
    )
    parser.add_argument(
        "--concurrency-count",
        dest="concurrency_count",
        type=int,
        default=40,
        help="实际投票并发数。",
    )
    parser.add_argument(
        "--index-concurrency-count",
        type=int,
        default=DEFAULT_INDEX_CONCURRENCY,
        help="建立索引时同时处理的 SHP 文件数。",
    )
    parser.add_argument(
        "--precheck-concurrency-count",
        type=int,
        default=4,
        help="投票前范围相交检查同时处理的 SHP 文件数。",
    )
    args = parser.parse_args()
    shp_dir = args.shp_dir
    if not 1 <= args.concurrency_count <= 96:
        parser.error("--concurrency-count 必须在 1 到 96 之间")
    if not 1 <= args.index_concurrency_count <= 96:
        parser.error("--index-concurrency-count 必须在 1 到 96 之间")
    if not 1 <= args.precheck_concurrency_count <= 96:
        parser.error("--precheck-concurrency-count 必须在 1 到 96 之间")
    NUM_WORKERS = args.concurrency_count
    SHP_INDEX_WORKERS = args.index_concurrency_count
    SHP_TASK_PRECHECK_WORKERS = args.precheck_concurrency_count
    if args.refresh_shp_cache_only or args.ensure_shp_cache_only:
        shp_files = find_cultivated_land_shapefiles(
            shp_dir,
            refresh_cache=args.refresh_shp_cache_only,
            region_name=None,
            index_concurrency_count=args.index_concurrency_count,
        )
        action_text = "refresh completed" if args.refresh_shp_cache_only else "is ready"
        print(f"[SUMMARY] SHP index {action_text}: {len(shp_files)} file(s).", flush=True)
        raise SystemExit(0)
    cls_tif = args.cls_tif
    out_dir = args.out_dir
    os.makedirs(out_dir, exist_ok=True)
    min_background_threshold = args.MIN_BACKGROUND_THRESHOLD
    if not 0.0 <= min_background_threshold <= 1.0:
        parser.error("--MIN_BACKGROUND_THRESHOLD 必须在 0 到 1 之间")
    min_class_area_mu = args.MIN_CLASS_AREA_MU
    if min_class_area_mu < 0:
        parser.error("--MIN_CLASS_AREA_MU 必须大于等于 0")
    try:
        class_mapping = parse_class_mapping(args.class_mapping) if args.multi_class else None
    except ValueError as exc:
        parser.error(str(exc))
    if class_mapping:
        mapping_text = "；".join(
            f"{value}={name}" for value, name in sorted(class_mapping.items())
        )
        print(f"[INFO] SHP class mapping: {mapping_text}", flush=True)
    
    run(
        shp_dir=shp_dir,
        cls_path=cls_tif,
        out_dir=out_dir,
        cls_file_type="file",
        resume=True,
        concurrency_enabled=NUM_WORKERS > 1,
        filter_area_path=None,
        stats_csv="vote_processing_stats.csv",
        keep_all_polygons=KEEP_ALL_POLYGONS,
        min_background_threshold=min_background_threshold,
        min_class_area_mu=min_class_area_mu,
        refresh_shp_cache=args.refresh_shp_cache,
        region_name=args.region_name,
        class_mapping=class_mapping,
    )
