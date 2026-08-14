"""修复合成 SHP 在输入图幅边界处产生的窄缝。

设计原则
--------
1. 只检查 ``src_tif`` 不同的面，图幅内部的道路、沟渠和正常空白不动。
2. 用 ``input_tif`` 的边界作为补面的安全范围；用同名 ``input_shp``
   与合成结果估计图幅校正后的边界位置。
3. 只有长边互为最佳匹配的两个面才向断口各扩展一半并融合；其他空隙
   （包括道路、过道和沟渠）完全不填。
4. 不覆盖输入文件，默认生成 ``aligned_mosaic_all_axis_fixed.shp``，
   同时生成一个 JSON 检查报告。

直接运行：

    .venv\\Scripts\\python.exe fix_mosaic_shp_boundary_gaps.py

如果仍有更宽的缝，可提高最大缝宽：

    .venv\\Scripts\\python.exe fix_mosaic_shp_boundary_gaps.py \
        --max-gap-pixels 30 --overwrite
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import geopandas as gpd
import numpy as np
import rasterio
import shapely
from pyproj import Transformer
from shapely import STRtree, Polygon


ROOT = Path(__file__).resolve().parent
DEFAULT_INPUT = ROOT / "output" / "aligned_mosaic_all_axis.shp"
DEFAULT_OUTPUT = ROOT / "output" / "aligned_mosaic_all_axis_fixed.shp"
DEFAULT_TIF_DIR = ROOT / "input_tif"
DEFAULT_SHP_DIR = ROOT / "input_shp"


def normalized_source(value: object) -> str:
    """把字段中的文件名或路径统一为不含扩展名的图幅名。"""
    return Path(str(value)).stem


def polygonal_only(geometry):
    """移除布尔运算偶尔产生的零面积线/点残片。"""
    type_id = shapely.get_type_id(geometry)
    if type_id in (3, 6):
        return geometry
    parts = shapely.get_parts(geometry)
    polygon_parts = [
        part
        for part in parts
        if shapely.get_type_id(part) in (3, 6)
    ]
    if not polygon_parts:
        return Polygon()
    return shapely.union_all(polygon_parts)


def remove_shapefile(path: Path) -> None:
    """删除一个明确指定的 Shapefile 及其旁车文件。"""
    for suffix in (".shp", ".shx", ".dbf", ".prj", ".cpg", ".qix"):
        sidecar = path.with_suffix(suffix)
        if sidecar.exists():
            sidecar.unlink()


def verify_and_repair_written_shapefile(path: Path) -> int:
    """复读输出；修复 Shapefile 环规则在写出时产生的拓扑问题。"""
    repaired_count = 0
    for _ in range(2):
        written = gpd.read_file(path)
        invalid = ~written.geometry.is_valid
        count = int(invalid.sum())
        if count == 0:
            return repaired_count
        repaired_count += count
        repaired = []
        for geometry in written.loc[invalid, "geometry"]:
            fixed = shapely.make_valid(
                geometry,
                method="structure",
                keep_collapsed=False,
            )
            fixed = polygonal_only(fixed)
            fixed = shapely.buffer(
                fixed, 0.0, quad_segs=1, join_style="mitre"
            )
            repaired.append(
                shapely.orient_polygons(fixed, exterior_cw=True)
            )
        written.loc[invalid, "geometry"] = repaired
        remove_shapefile(path)
        written.to_file(
            path,
            driver="ESRI Shapefile",
            encoding="UTF-8",
        )
    final = gpd.read_file(path)
    final_invalid = int((~final.geometry.is_valid).sum())
    if final_invalid:
        raise RuntimeError(
            f"输出 SHP 复读后仍有 {final_invalid} 个无效几何。"
        )
    return repaired_count


def densified_raster_footprint(
    tif_path: Path,
    output_crs,
    points_per_edge: int = 65,
):
    """将栅格四边加密后投影到合成 SHP 的坐标系。"""
    with rasterio.open(tif_path, sharing=False) as ds:
        if ds.crs is None:
            raise ValueError(f"TIFF 缺少 CRS：{tif_path}")
        cols = np.linspace(0.0, float(ds.width), points_per_edge)
        rows = np.linspace(0.0, float(ds.height), points_per_edge)
        pixel_ring = (
            [(float(col), 0.0) for col in cols]
            + [(float(ds.width), float(row)) for row in rows[1:]]
            + [
                (float(col), float(ds.height))
                for col in cols[-2::-1]
            ]
            + [(0.0, float(row)) for row in rows[-2:0:-1]]
        )
        transformer = Transformer.from_crs(
            ds.crs, output_crs, always_xy=True
        )
        world_ring = [ds.transform * point for point in pixel_ring]
        projected = [transformer.transform(x, y) for x, y in world_ring]
        return Polygon(projected)


def projected_pixel_size(tif_path: Path, output_crs) -> float:
    """在 TIFF 中心把一个像元换算为输出矢量坐标单位。"""
    with rasterio.open(tif_path, sharing=False) as ds:
        if ds.crs is None:
            raise ValueError(f"TIFF 缺少 CRS：{tif_path}")
        col = ds.width / 2.0
        row = ds.height / 2.0
        x0, y0 = ds.transform * (col, row)
        x1, y1 = ds.transform * (col + 1.0, row)
        x2, y2 = ds.transform * (col, row + 1.0)
        transformer = Transformer.from_crs(
            ds.crs, output_crs, always_xy=True
        )
        p0 = transformer.transform(x0, y0)
        p1 = transformer.transform(x1, y1)
        p2 = transformer.transform(x2, y2)
        return float(
            max(
                np.hypot(p1[0] - p0[0], p1[1] - p0[1]),
                np.hypot(p2[0] - p0[0], p2[1] - p0[1]),
            )
        )


def axis_map_geometry(geometry, old_bounds, new_bounds):
    """用原始/校正后要素范围估计图幅边界的轴向校正。"""
    old_left, old_bottom, old_right, old_top = old_bounds
    new_left, new_bottom, new_right, new_top = new_bounds
    old_width = old_right - old_left
    old_height = old_top - old_bottom
    if old_width <= 0 or old_height <= 0:
        raise ValueError("输入 SHP 的范围无效，无法估计图幅边界。")
    scale_x = (new_right - new_left) / old_width
    scale_y = (new_top - new_bottom) / old_height

    def transform_coordinates(coordinates: np.ndarray) -> np.ndarray:
        result = coordinates.copy()
        result[:, 0] = new_left + (coordinates[:, 0] - old_left) * scale_x
        result[:, 1] = new_bottom + (
            coordinates[:, 1] - old_bottom
        ) * scale_y
        return result

    return shapely.transform(geometry, transform_coordinates)


def build_source_footprints(
    frame: gpd.GeoDataFrame,
    source: np.ndarray,
    tif_dir: Path,
    shp_dir: Path,
) -> tuple[dict[str, object], float]:
    """读取同名输入数据，建立校正后的图幅安全边界并计算像元大小。"""
    footprints: dict[str, object] = {}
    pixel_sizes: list[float] = []
    for name in sorted(set(source)):
        tif_candidates = [
            tif_dir / f"{name}.tif",
            tif_dir / f"{name}.tiff",
        ]
        tif_path = next((path for path in tif_candidates if path.is_file()), None)
        shp_path = shp_dir / f"{name}.shp"
        if tif_path is None:
            raise FileNotFoundError(f"缺少同名 TIFF：{name}.tif/.tiff")
        if not shp_path.is_file():
            raise FileNotFoundError(f"缺少同名 SHP：{shp_path}")

        original = gpd.read_file(shp_path)
        if original.crs is None:
            raise ValueError(f"输入 SHP 缺少 CRS：{shp_path}")
        original = original.to_crs(frame.crs)
        original = original[
            original.geometry.notna() & ~original.geometry.is_empty
        ]
        if original.empty:
            raise ValueError(f"输入 SHP 没有有效面：{shp_path}")

        indexes = np.flatnonzero(source == name)
        corrected_bounds = shapely.total_bounds(
            frame.geometry.to_numpy()[indexes]
        )
        raster_boundary = densified_raster_footprint(tif_path, frame.crs)
        footprint = axis_map_geometry(
            raster_boundary,
            original.total_bounds,
            corrected_bounds,
        )
        footprints[name] = shapely.make_valid(footprint)
        pixel_sizes.append(projected_pixel_size(tif_path, frame.crs))
        print(f"  已匹配边界：{name}")

    pixel_size = float(np.median(pixel_sizes))
    if not np.isfinite(pixel_size) or pixel_size <= 0:
        raise ValueError("无法计算有效的像元大小。")
    return footprints, pixel_size


def pair_contact_score(
    left_parts: np.ndarray,
    right_parts: np.ndarray,
    distances: np.ndarray,
    max_gap: float,
) -> np.ndarray:
    """估算两面沿接缝相对的长度，过滤十字角上的偶遇。"""
    left_buffer = shapely.buffer(
        left_parts, max_gap / 2.0, quad_segs=1, join_style="mitre"
    )
    right_buffer = shapely.buffer(
        right_parts, max_gap / 2.0, quad_segs=1, join_style="mitre"
    )
    common = shapely.intersection(left_buffer, right_buffer)
    width = np.maximum(max_gap - distances, max_gap * 1e-6)
    return shapely.area(common) / width


def find_boundary_candidates(
    frame: gpd.GeoDataFrame,
    source: np.ndarray,
    max_gap: float,
) -> tuple[list[tuple[int, int, float]], dict]:
    """查找不同图幅的近邻面，此阶段只检查、不修改任何几何。"""
    geometries = frame.geometry.to_numpy()
    candidates: list[tuple[int, int, float]] = []
    pair_stats: dict[str, int] = {}
    names = sorted(set(source))
    groups = {
        name: np.flatnonzero(source == name)
        for name in names
    }
    bounds = {
        name: shapely.total_bounds(geometries[indexes])
        for name, indexes in groups.items()
    }

    for position, left_name in enumerate(names):
        left_indexes = groups[left_name]
        left_geometries = geometries[left_indexes]
        left_bounds = bounds[left_name]
        for right_name in names[position + 1 :]:
            right_bounds = bounds[right_name]
            if (
                left_bounds[2] + max_gap < right_bounds[0]
                or right_bounds[2] + max_gap < left_bounds[0]
                or left_bounds[3] + max_gap < right_bounds[1]
                or right_bounds[3] + max_gap < left_bounds[1]
            ):
                continue

            right_indexes = groups[right_name]
            right_geometries = geometries[right_indexes]
            pairs = STRtree(right_geometries).query(
                left_geometries,
                predicate="dwithin",
                distance=max_gap,
            )
            if pairs.shape[1] == 0:
                continue

            left_parts = left_geometries.take(pairs[0])
            right_parts = right_geometries.take(pairs[1])
            distances = shapely.distance(left_parts, right_parts)
            keep = np.isfinite(distances) & (distances > 1e-7)
            if not keep.any():
                continue
            left_local = pairs[0, keep]
            right_local = pairs[1, keep]
            left_parts = left_parts[keep]
            right_parts = right_parts[keep]
            distances = distances[keep]

            scores = pair_contact_score(
                left_parts, right_parts, distances, max_gap
            )
            global_left = left_indexes[left_local]
            global_right = right_indexes[right_local]
            candidates.extend(
                (int(left), int(right), float(score))
                for left, right, score in zip(
                    global_left, global_right, scores, strict=True
                )
                if np.isfinite(score)
            )
            key = f"{left_name} <-> {right_name}"
            pair_stats[key] = int(keep.sum())
            print(f"  {key}: {int(keep.sum())} 对")

    stats = {
        "candidate_pairs": len(candidates),
        "pairs_by_source": pair_stats,
    }
    return candidates, stats


def select_mutual_best(
    candidates: list[tuple[int, int, float]],
    min_contact: float,
) -> list[tuple[int, int, float]]:
    """选择互为最佳匹配且对应边足够长的跨缝地块。"""
    eligible = [
        edge for edge in candidates
        if edge[2] >= min_contact
    ]
    best: dict[int, tuple[int, float]] = {}
    for left, right, score in eligible:
        if score > best.get(left, (-1, -1.0))[1]:
            best[left] = (right, score)
        if score > best.get(right, (-1, -1.0))[1]:
            best[right] = (left, score)
    matches = {
        (min(left, right), max(left, right)): score
        for left, right, score in eligible
        if best.get(left, (-1, -1.0))[0] == right
        and best.get(right, (-1, -1.0))[0] == left
    }
    return [
        (left, right, score)
        for (left, right), score in sorted(matches.items())
    ]


def merge_matched_pairs(
    geometries: np.ndarray,
    source: np.ndarray,
    footprints: dict[str, object],
    matches: list[tuple[int, int, float]],
    max_gap: float,
    overlap: float,
) -> tuple[np.ndarray, set[int], float]:
    """只给确认属于同一地块的成对面补断口，然后直接融合。"""
    original_areas = shapely.area(geometries)
    areas = shapely.area(geometries)
    dropped: set[int] = set()
    total_added_area = 0.0
    for left, right, _ in matches:
        if left in dropped or right in dropped:
            continue
        left_geometry = geometries[left]
        right_geometry = geometries[right]
        distance = float(shapely.distance(left_geometry, right_geometry))
        if not np.isfinite(distance) or distance <= 1e-7 or distance > max_gap:
            continue

        # 只提取两面在断口处彼此相对的边界，再用这些局部断边的凸包
        # 生成连接四边形。上下断边端点不齐时，左右边会直接斜接，
        # 不会出现只填重叠宽度所产生的横向台阶。
        pair_union = shapely.union(left_geometry, right_geometry)
        left_facing_edge = shapely.intersection(
            shapely.boundary(left_geometry),
            shapely.buffer(
                right_geometry,
                max_gap,
                quad_segs=1,
                join_style="mitre",
            ),
        )
        right_facing_edge = shapely.intersection(
            shapely.boundary(right_geometry),
            shapely.buffer(
                left_geometry,
                max_gap,
                quad_segs=1,
                join_style="mitre",
            ),
        )
        local_connector = shapely.convex_hull(
            shapely.union(left_facing_edge, right_facing_edge)
        )
        facing_corridor = shapely.intersection(
            shapely.buffer(
                left_geometry,
                max_gap,
                quad_segs=1,
                join_style="mitre",
            ),
            shapely.buffer(
                right_geometry,
                max_gap,
                quad_segs=1,
                join_style="mitre",
            ),
        )
        allowed_footprint = shapely.union(
            footprints[source[left]],
            footprints[source[right]],
        )
        bridge = shapely.intersection(
            shapely.difference(local_connector, pair_union),
            facing_corridor,
        )
        bridge = shapely.intersection(bridge, allowed_footprint)
        joined = shapely.union(pair_union, bridge)
        if shapely.distance(
            shapely.union(left_geometry, bridge),
            right_geometry,
        ) > max(overlap * 0.1, 1e-7):
            continue
        joined = shapely.make_valid(
            joined,
            method="structure",
            keep_collapsed=False,
        )
        joined = polygonal_only(joined)
        if joined.is_empty:
            continue

        keep, drop = (
            (left, right)
            if areas[left] >= areas[right]
            else (right, left)
        )
        geometries[keep] = joined
        areas[keep] = shapely.area(geometries[keep])
        total_added_area += float(
            areas[keep] - original_areas[left] - original_areas[right]
        )
        dropped.add(drop)
    return geometries, dropped, total_added_area


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, default=DEFAULT_INPUT)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--tif-dir", type=Path, default=DEFAULT_TIF_DIR)
    parser.add_argument("--shp-dir", type=Path, default=DEFAULT_SHP_DIR)
    parser.add_argument("--source-field", default="src_tif")
    parser.add_argument(
        "--max-gap-pixels",
        type=float,
        default=20.0,
        help="最大待修缝宽，单位为输入 TIFF 像元，默认 20",
    )
    parser.add_argument(
        "--overlap-pixels",
        type=float,
        default=0.05,
        help="两侧补面间防渲染白线的微小搭接，默认 0.05 像元",
    )
    parser.add_argument(
        "--min-merge-contact-pixels",
        type=float,
        default=20.0,
        help="融合为同一面所需的最短对应边，默认 20 像元",
    )
    parser.add_argument("--overwrite", action="store_true")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    if not args.input.is_file():
        raise FileNotFoundError(f"输入 SHP 不存在：{args.input.resolve()}")
    if not args.tif_dir.is_dir() or not args.shp_dir.is_dir():
        raise FileNotFoundError("input_tif 或 input_shp 目录不存在。")
    if args.output.resolve() == args.input.resolve():
        raise ValueError("为保护原数据，输出路径不能等于输入路径。")
    if args.output.exists() and not args.overwrite:
        raise FileExistsError(
            f"输出已存在：{args.output.resolve()}；覆盖请添加 --overwrite。"
        )
    if (
        args.max_gap_pixels <= 0
        or args.overlap_pixels < 0
        or args.min_merge_contact_pixels <= 0
    ):
        raise ValueError("缝宽和融合边长须大于 0，搭接值不能小于 0。")

    print(f"读取合成 SHP：{args.input.resolve()}")
    frame = gpd.read_file(args.input)
    if frame.crs is None:
        raise ValueError("合成 SHP 缺少 CRS。")
    if args.source_field not in frame.columns:
        raise ValueError(
            f"合成 SHP 缺少来源字段：{args.source_field}"
        )
    frame = frame[
        frame.geometry.notna() & ~frame.geometry.is_empty
    ].copy().reset_index(drop=True)
    invalid_before = int((~frame.geometry.is_valid).sum())
    if invalid_before:
        frame.geometry = shapely.make_valid(
            frame.geometry.array,
            method="structure",
            keep_collapsed=False,
        )

    source = (
        frame[args.source_field]
        .map(normalized_source)
        .to_numpy(dtype=str)
    )
    if np.any(source == ""):
        raise ValueError(f"{args.source_field} 中存在空来源。")

    print("读取同名输入 TIFF/SHP 并建立边界约束：")
    footprints, pixel_size = build_source_footprints(
        frame, source, args.tif_dir, args.shp_dir
    )
    max_gap = pixel_size * args.max_gap_pixels
    overlap = pixel_size * args.overlap_pixels
    min_contact = pixel_size * args.min_merge_contact_pixels
    print(
        f"共 {len(frame)} 个面；中位像元 {pixel_size:.4f} 米；"
        f"最大修复缝宽 {max_gap:.3f} 米。"
    )
    print("检查跨图幅边界近邻（此阶段不填缝）：")
    candidates, stats = find_boundary_candidates(
        frame, source, max_gap
    )
    matches = select_mutual_best(candidates, min_contact)
    geometries = frame.geometry.to_numpy(copy=True)
    geometries, dropped, added_area = merge_matched_pairs(
        geometries,
        source,
        footprints,
        matches,
        max_gap,
        overlap,
    )
    remaining = len(matches) - len(dropped)

    keep = np.ones(len(frame), dtype=bool)
    if dropped:
        keep[np.fromiter(dropped, dtype=np.int64)] = False
    result = frame.loc[keep].copy().reset_index(drop=True)
    result.geometry = geometries[keep]
    result = result[
        result.geometry.notna() & ~result.geometry.is_empty
    ].copy()
    # Shapefile 对外环/内环方向有严格约定，统一方向可避免写出后被
    # GDAL 判定为无效环。
    result.geometry = shapely.orient_polygons(
        result.geometry.array,
        exterior_cw=True,
    )
    invalid_after = int((~result.geometry.is_valid).sum())
    if invalid_after:
        raise RuntimeError(
            f"修复后仍有 {invalid_after} 个无效几何，已停止写出。"
        )

    args.output.parent.mkdir(parents=True, exist_ok=True)
    if args.overwrite:
        remove_shapefile(args.output)
    result.to_file(
        args.output,
        driver="ESRI Shapefile",
        encoding="UTF-8",
    )
    roundtrip_repaired = verify_and_repair_written_shapefile(args.output)

    report = {
        "input": str(args.input.resolve()),
        "output": str(args.output.resolve()),
        "source_tiles": len(set(source)),
        "features_before": len(frame),
        "features_after": len(result),
        "invalid_before": invalid_before,
        "invalid_after": invalid_after,
        "roundtrip_geometries_repaired": roundtrip_repaired,
        "pixel_size": pixel_size,
        "max_gap_pixels": args.max_gap_pixels,
        "max_gap_map_units": max_gap,
        "overlap_pixels": args.overlap_pixels,
        "min_merge_contact_pixels": args.min_merge_contact_pixels,
        "matched_pairs": len(matches),
        "merged_pairs": len(dropped),
        "modified_features_before_merge": len(dropped) * 2,
        "bridge_area_added": added_area,
        "remaining_matched_pair_gaps": remaining,
        "unmatched_candidate_pairs_kept_as_gaps": (
            len(candidates) - len(matches)
        ),
        **stats,
    }
    report_path = args.output.with_name(
        f"{args.output.stem}_repair_report.json"
    )
    report_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(
        "\n修复完成：\n"
        f"  输出：{args.output.resolve()}\n"
        f"  报告：{report_path.resolve()}\n"
        f"  近邻候选 {len(candidates)} 对，仅确认同一地块 "
        f"{len(matches)} 对；\n"
        f"  实际融合 {len(dropped)} 对，"
        f"匹配对中仍断开 {remaining} 对；"
        f"其余空隙保持不变。"
    )


if __name__ == "__main__":
    main()
