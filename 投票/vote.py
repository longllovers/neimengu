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
from multiprocessing import Pool

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



# -------------------- CONFIG --------------------
MIN_BACKGROUND_THRESHOLD = 0.5
CLASS_FIELD = "class"
NUM_WORKERS = 40
SHP_CACHE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "save.json")

# True:
#   不删除任何原始矢量面，只新增/更新 class 字段。
#   没有有效分类结果的 polygon，class = 0。
#
# False:
#   保持原始逻辑，删除 class = 0 的 polygon，只输出有效分类 polygon。
KEEP_ALL_POLYGONS = False
# ------------------------------------------------


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


def vote_by_bincount(cls_map, polygon_id_map, n_polygons, min_background_threshold=MIN_BACKGROUND_THRESHOLD):
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
    keep[nonempty] = (
        background_count[nonempty] / total_count[nonempty]
        <= min_background_threshold
    )
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


def fallback_vote_for_missing(gdf, raster_poly_id, window_transform, cls_map, min_background_threshold=MIN_BACKGROUND_THRESHOLD):
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

        if (
            np.count_nonzero(pixel_values == 0) / len(pixel_values)
            > min_background_threshold
        ):
            continue

        valid_values = pixel_values[pixel_values > 0]
        if len(valid_values) == 0:
            continue

        result[int(poly_id)] = int(np.bincount(valid_values).argmax())

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
    shp_path, cls_tif, out_shp_path, keep_all_polygons, min_background_threshold = args

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
        )

        poly_id_to_class.update(
            fallback_vote_for_missing(
                gdf_process,
                raster_poly_id,
                window_transform,
                cls_map,
                min_background_threshold,
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
        print(f"[ERROR] process {base_name} failed: {exc}")
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


def _load_shp_cache(cache_path=SHP_CACHE_PATH):
    if not os.path.exists(cache_path) or os.path.getsize(cache_path) == 0:
        return {"version": 1, "directories": {}}

    try:
        with open(cache_path, "r", encoding="utf-8") as cache_file:
            cache = json.load(cache_file)
    except (OSError, json.JSONDecodeError) as exc:
        print(f"[WARNING] Cannot read shp cache, rebuilding it: {exc}")
        return {"version": 1, "directories": {}}

    if not isinstance(cache, dict) or not isinstance(cache.get("directories"), dict):
        print("[WARNING] Invalid shp cache format, rebuilding it.")
        return {"version": 1, "directories": {}}
    return cache


def _save_shp_cache(cache, cache_path=SHP_CACHE_PATH):
    cache_dir = os.path.dirname(os.path.abspath(cache_path))
    os.makedirs(cache_dir, exist_ok=True)
    temp_path = f"{cache_path}.tmp.{os.getpid()}"
    try:
        with open(temp_path, "w", encoding="utf-8") as cache_file:
            json.dump(cache, cache_file, ensure_ascii=False, indent=2)
        os.replace(temp_path, cache_path)
    finally:
        if os.path.exists(temp_path):
            os.remove(temp_path)


def find_cultivated_land_shapefiles(shp_dir, cache_path=SHP_CACHE_PATH):
    """优先从 JSON 缓存读取，否则扫描 shp 并写入缓存。"""
    shp_dir = os.path.abspath(os.path.expanduser(shp_dir))
    if not os.path.isdir(shp_dir):
        raise ValueError(f"Shapefile directory does not exist: {shp_dir}")

    # normcase 使 Windows 下同一路径的大小写差异不会重复建立缓存。
    cache_key = os.path.normcase(os.path.normpath(shp_dir))
    cache = _load_shp_cache(cache_path)
    cached_files = cache["directories"].get(cache_key)
    if isinstance(cached_files, list):
        print(f"[INFO] Loaded {len(cached_files)} shapefile(s) from cache: {cache_path}")
        return cached_files

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
    cache["directories"][cache_key] = shp_files
    _save_shp_cache(cache, cache_path)
    print(f"[INFO] Saved {len(shp_files)} shapefile(s) to cache: {cache_path}")
    return sorted(shp_files)


def build_tasks(
    shp_dir,
    cls_tif,
    out_dir,
    filter_area_path=None,
    keep_all_polygons=True,
    resume=True,
    min_background_threshold=MIN_BACKGROUND_THRESHOLD,
):
    filter_area_geom, filter_area_crs = _load_filter_area(filter_area_path)

    with rasterio.open(cls_tif) as src:
        image_bounds = box(*src.bounds)
        image_crs = src.crs

    tasks = []

    shp_files = find_cultivated_land_shapefiles(shp_dir)
    print(f"[INFO] Found {len(shp_files)} shapefile(s) under: {shp_dir}")
    for shp_file in shp_files:
        base_name = os.path.splitext(os.path.basename(shp_file))[0]
        out_shp_path = os.path.join(out_dir, f"{base_name}.shp")

        if resume and os.path.exists(out_shp_path):
            continue

        with fiona.open(shp_file, "r") as f:
            shp_bounds = box(*f.bounds)
            shp_crs = f.crs

        if not _shp_intersects_area(
            shp_bounds,
            shp_crs,
            filter_area_geom,
            filter_area_crs,
        ):
            #print(f"[SKIP] {base_name}: out of filter area.")
            continue

        if not _shp_intersects_image(
            shp_bounds,
            shp_crs,
            image_bounds,
            image_crs,
        ):
            #print(f"[SKIP] {base_name}: out of image range.")
            continue

        tasks.append((shp_file, cls_tif, out_shp_path, keep_all_polygons, min_background_threshold))

    return tasks


def run_single_tif(
    shp_dir,
    cls_tif,
    out_dir,
    resume=True,
    is_parallel=True,
    filter_area_path=None,
    stats_csv="processing_stats.csv",
    keep_all_polygons=True,
    min_background_threshold=MIN_BACKGROUND_THRESHOLD,
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
    )

    if len(tasks) == 0:
        print("[INFO] No tasks need to be processed.")
        return []

    print(
        f"[INFO] Tasks={len(tasks)}, workers={NUM_WORKERS if is_parallel else 1}, "
        f"background_threshold={min_background_threshold}"
    )

    stats = []
    progress_step = max(1, len(tasks) // 10)

    if is_parallel:
        with Pool(processes=NUM_WORKERS) as pool:
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
    is_parallel=True,
    filter_area_path=None,
    stats_csv="processing_stats.csv",
    keep_all_polygons=True,
    min_background_threshold=MIN_BACKGROUND_THRESHOLD,
):
    if cls_file_type == "file":
        return run_single_tif(
            shp_dir=shp_dir,
            cls_tif=cls_path,
            out_dir=out_dir,
            resume=resume,
            is_parallel=is_parallel,
            filter_area_path=filter_area_path,
            stats_csv=stats_csv,
            keep_all_polygons=keep_all_polygons,
            min_background_threshold=min_background_threshold,
        )

    if cls_file_type == "folder":
        vrt_path = build_vrt(cls_path)
        return run_single_tif(
            shp_dir=shp_dir,
            cls_tif=vrt_path,
            out_dir=out_dir,
            resume=resume,
            is_parallel=is_parallel,
            filter_area_path=filter_area_path,
            stats_csv=stats_csv,
            keep_all_polygons=keep_all_polygons,
            min_background_threshold=min_background_threshold,
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
    args = parser.parse_args()
    shp_dir = args.shp_dir
    cls_tif = args.cls_tif
    out_dir = args.out_dir
    os.makedirs(out_dir, exist_ok=True)
    min_background_threshold = args.MIN_BACKGROUND_THRESHOLD
    if not 0.0 <= min_background_threshold <= 1.0:
        parser.error("--MIN_BACKGROUND_THRESHOLD 必须在 0 到 1 之间")
    
    NUM_WORKERS = 40

    run(
        shp_dir=shp_dir,
        cls_path=cls_tif,
        out_dir=out_dir,
        cls_file_type="file",
        resume=True,
        is_parallel=True,
        filter_area_path=None,
        stats_csv="vote_processing_stats.csv",
        keep_all_polygons=KEEP_ALL_POLYGONS,
        min_background_threshold=min_background_threshold,
    )
