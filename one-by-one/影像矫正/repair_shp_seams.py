"""修复影像镶嵌后、不同来源 SHP 之间的窄接缝。

本脚本只扩展 ``src_tif`` 不同、且距离小于给定阈值的面，不会对整幅
数据执行全局 buffer。因此，同一原始图幅内部的道路、沟渠和正常地块间隔
会保持不变。默认阈值按最终 TIFF 的像元大小换算到 SHP 坐标系。

直接运行（路径默认就是本项目的数据）：

    .venv\\Scripts\\python.exe repair_shp_seams.py

如仍有较宽接缝，可增大阈值，例如：

    .venv\\Scripts\\python.exe repair_shp_seams.py --max-gap-pixels 20 --overwrite
"""

from __future__ import annotations

import argparse
from collections import defaultdict
from pathlib import Path

import geopandas as gpd
import numpy as np
import rasterio
import shapely
from pyproj import Transformer
from shapely import STRtree


ROOT = Path(__file__).resolve().parent
DEFAULT_INPUT = ROOT / "output" / "aligned_mosaic_all_axis.shp"
DEFAULT_RASTER = ROOT / "output" / "aligned_mosaic_all_axis.tif"
DEFAULT_OUTPUT = ROOT / "output" / "aligned_mosaic_all_axis_seam_merged.shp"
DEFAULT_SHP_DIR = ROOT / "input_shp"
DEFAULT_TIF_DIR = ROOT / "input_tif"


def pixel_size_in_vector_crs(raster_path: Path, vector_crs) -> float:
    """在影像中心处，将一个栅格像元换算为矢量坐标单位。"""
    with rasterio.open(raster_path, sharing=False) as ds:
        if ds.crs is None:
            raise ValueError(f"最终 TIFF 缺少 CRS：{raster_path}")
        cx = (ds.bounds.left + ds.bounds.right) / 2.0
        cy = (ds.bounds.bottom + ds.bounds.top) / 2.0
        transformer = Transformer.from_crs(ds.crs, vector_crs, always_xy=True)
        x0, y0 = transformer.transform(cx, cy)
        x1, y1 = transformer.transform(cx + abs(ds.res[0]), cy)
        x2, y2 = transformer.transform(cx, cy + abs(ds.res[1]))
        sizes = (np.hypot(x1 - x0, y1 - y0), np.hypot(x2 - x0, y2 - y0))
        size = float(max(sizes))
        if not np.isfinite(size) or size <= 0:
            raise ValueError("无法把 TIFF 像元大小换算到 SHP 坐标系。")
        return size


def validate_source_files(
    source_names: set[str], shp_dir: Path, tif_dir: Path
) -> None:
    """确认 src_tif 均有同名原始 SHP 和 TIFF，防止错误来源被混合。"""
    if not shp_dir.is_dir() or not tif_dir.is_dir():
        raise FileNotFoundError("input_shp 或 input_tif 目录不存在。")
    shp_names = {p.stem for p in shp_dir.glob("*.shp")}
    tif_names = {
        p.stem for pattern in ("*.tif", "*.tiff") for p in tif_dir.glob(pattern)
    }
    missing_shp = sorted(source_names - shp_names)
    missing_tif = sorted(source_names - tif_names)
    if missing_shp or missing_tif:
        details = []
        if missing_shp:
            details.append("缺少同名 SHP：" + "、".join(missing_shp))
        if missing_tif:
            details.append("缺少同名 TIFF：" + "、".join(missing_tif))
        raise FileNotFoundError("；".join(details))


def remove_shapefile(output: Path) -> None:
    """仅在用户明确使用 --overwrite 时移除指定输出的旁车文件。"""
    for suffix in (".shp", ".shx", ".dbf", ".prj", ".cpg", ".qix"):
        sidecar = output.with_suffix(suffix)
        if sidecar.exists():
            sidecar.unlink()


def repair_seams(
    frame: gpd.GeoDataFrame,
    source_field: str,
    max_gap: float,
    overlap: float,
) -> tuple[gpd.GeoDataFrame, int, int, list[tuple[int, int, float]]]:
    """查找不同来源的近邻面，并只向它们之间扩展。"""
    source = frame[source_field].astype(str).map(lambda value: Path(value).stem)
    names = sorted(source.unique())
    geometries = frame.geometry.to_numpy(copy=True)
    additions: dict[int, list] = defaultdict(list)
    merge_candidates: list[tuple[int, int, float]] = []
    repaired_pairs = 0

    groups = {
        name: np.flatnonzero(source.to_numpy() == name) for name in names
    }
    bounds = {
        name: shapely.total_bounds(geometries[indexes])
        for name, indexes in groups.items()
    }

    print(f"来源图幅：{len(names)} 个；开始检查不同来源之间的窄缝……")
    for pos, left_name in enumerate(names):
        left_indexes = groups[left_name]
        left_geometries = geometries[left_indexes]
        left_bounds = bounds[left_name]
        for right_name in names[pos + 1 :]:
            right_bounds = bounds[right_name]
            # 总范围都相距很远的图幅不可能形成接缝。
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
                left_geometries, predicate="dwithin", distance=max_gap
            )
            if pairs.shape[1] == 0:
                continue

            left_parts = left_geometries.take(pairs[0])
            right_parts = right_geometries.take(pairs[1])
            distances = shapely.distance(left_parts, right_parts)
            # 已接触或重叠的面无需改动；只处理真正存在的正宽度窄缝。
            keep = np.isfinite(distances) & (distances > 1e-7)
            if not keep.any():
                continue
            left_local = pairs[0, keep]
            right_local = pairs[1, keep]
            left_parts = left_parts[keep]
            right_parts = right_parts[keep]

            # 两侧各扩展半个最大缝宽并略微搭接。用对方面的 max_gap
            # buffer 作掩膜，使扩展只发生在这对跨图幅近邻面之间。
            grow = max_gap / 2.0 + overlap
            left_grown = shapely.buffer(left_parts, grow, quad_segs=2)
            right_grown = shapely.buffer(right_parts, grow, quad_segs=2)
            left_add = shapely.intersection(
                left_grown,
                shapely.buffer(right_parts, max_gap, quad_segs=2),
            )
            right_add = shapely.intersection(
                right_grown,
                shapely.buffer(left_parts, max_gap, quad_segs=2),
            )

            # 面积除以搭接宽度，可近似得到两面的相对接缝长度。真正被图幅
            # 边界切开的同一地块通常有较长的对应边；角点偶遇则非常短。
            bridges = shapely.intersection(left_grown, right_grown)
            bridge_width = np.maximum(
                2.0 * grow - distances[keep], max(overlap, 1e-9)
            )
            contact_lengths = shapely.area(bridges) / bridge_width

            global_left = left_indexes[left_local]
            global_right = right_indexes[right_local]
            merge_candidates.extend(
                (int(i), int(j), float(score))
                for i, j, score in zip(
                    global_left, global_right, contact_lengths, strict=True
                )
                if np.isfinite(score)
            )

            for local_index, addition in zip(left_local, left_add, strict=True):
                if addition is not None and not addition.is_empty:
                    additions[int(left_indexes[local_index])].append(addition)
            for local_index, addition in zip(right_local, right_add, strict=True):
                if addition is not None and not addition.is_empty:
                    additions[int(right_indexes[local_index])].append(addition)

            pair_count = int(keep.sum())
            repaired_pairs += pair_count
            print(f"  {left_name} <-> {right_name}: {pair_count} 对窄缝面")

    for index, parts in additions.items():
        geometries[index] = shapely.union_all([geometries[index], *parts])

    modified = np.fromiter(additions.keys(), dtype=np.int64)
    if modified.size:
        fixed = shapely.make_valid(
            geometries[modified], method="structure", keep_collapsed=False
        )
        geometries[modified] = fixed

    result = frame.copy()
    result.geometry = geometries
    result = result[result.geometry.notna() & ~result.geometry.is_empty].copy()
    return result, repaired_pairs, len(additions), merge_candidates


def straighten_ring_in_zone(ring, zone):
    """删除接缝带内的折点，用带外相邻节点直接相连。"""
    coordinates = np.asarray(ring.coords)
    if len(coordinates) < 5:
        return ring
    # 末点与首点重复，循环处理时先去掉末点。
    coordinates = coordinates[:-1]
    inside = shapely.intersects(shapely.points(coordinates), zone)
    if not inside.any() or inside.all():
        return ring

    # 从一个接缝带外节点开始遍历。连续落在带内的一串折点全部跳过，
    # 于是其前后两个原始节点自动形成直线段；带外坐标完全不变。
    start = int(np.flatnonzero(~inside)[0])
    output = [coordinates[start]]
    offset = 1
    count = len(coordinates)
    while offset <= count:
        index = (start + offset) % count
        if not inside[index]:
            output.append(coordinates[index])
            offset += 1
            continue
        while offset <= count and inside[(start + offset) % count]:
            offset += 1
        if offset <= count:
            output.append(coordinates[(start + offset) % count])
            offset += 1
    if not np.array_equal(output[0], output[-1]):
        output.append(output[0])
    # 接缝带覆盖了几乎整个微小地块时，带外锚点不足以重新组成面；
    # 此类要素保持原外环，避免为了修一个角而破坏整体形状。
    if len(output) < 4 or len(np.unique(np.asarray(output), axis=0)) < 3:
        return ring
    return shapely.LinearRing(np.asarray(output))


def straighten_polygon_in_zone(geometry, zone):
    """只修直接缝带穿过的 Polygon 外环，孔洞和带外边界保持原样。"""
    if geometry.geom_type == "Polygon":
        exterior = straighten_ring_in_zone(geometry.exterior, zone)
        return shapely.Polygon(exterior, [ring.coords for ring in geometry.interiors])
    if geometry.geom_type == "MultiPolygon":
        return shapely.MultiPolygon(
            [straighten_polygon_in_zone(part, zone) for part in geometry.geoms]
        )
    return geometry


def merge_split_features(
    frame: gpd.GeoDataFrame,
    candidates: list[tuple[int, int, float]],
    min_contact: float,
    overlap: float,
    straighten_distance: float,
) -> tuple[gpd.GeoDataFrame, int]:
    """把跨缝且长边相互对应的面融合为一个要素，消除内部边界线。"""
    eligible = [edge for edge in candidates if edge[2] >= min_contact]

    # 每个面只选择接缝长度最大的对象，并要求双方互为最佳匹配。
    # 这可避免把接缝十字角处的几个不同地块误融合到一起。
    best: dict[int, tuple[int, float]] = {}
    for left, right, score in eligible:
        if score > best.get(left, (-1, -1.0))[1]:
            best[left] = (right, score)
        if score > best.get(right, (-1, -1.0))[1]:
            best[right] = (left, score)
    matches = [
        (left, right)
        for left, right, _ in eligible
        if best.get(left, (-1, -1.0))[0] == right
        and best.get(right, (-1, -1.0))[0] == left
    ]

    geometries = frame.geometry.to_numpy(copy=True)
    areas = shapely.area(geometries)
    drop: set[int] = set()
    merged_count = np.ones(len(frame), dtype=np.int16)
    for left, right in matches:
        # 属性保留面积较大的主体面；几何是真正的 union，不会留下内部线。
        keep, remove = (left, right) if areas[left] >= areas[right] else (right, left)
        left_geometry = geometries[left]
        right_geometry = geometries[right]
        merged = shapely.union(left_geometry, right_geometry)
        merged_before_straightening = merged

        # 只在两个待融合面实际搭接的局部接缝附近修直外轮廓。
        # 删除接缝带内的折点，以带外的原始节点直接连接；这不会像凸包
        # 那样向外撑出鼓包，接缝范围之外的坐标也完全不变。
        shared = shapely.intersection(left_geometry, right_geometry)
        if not shared.is_empty and straighten_distance > 0:
            join_zone = shapely.buffer(
                shared, straighten_distance, quad_segs=2
            )
            straightened = straighten_polygon_in_zone(merged, join_zone)
            # 若直线跨过一个强凹形地块自身，会产生自交。此时宁可保持
            # 原融合轮廓，也不把单面破坏成多个部件。
            if shapely.is_valid(straightened) and not straightened.is_empty:
                merged = straightened
            else:
                merged = merged_before_straightening
        if shapely.get_type_id(merged) == 6 and overlap > 0:
            # 极少数复杂拐角仍可能留亚像元断点。只沿两个部件的最短线
            # 增加一个亚像元宽连接，不对整个地块做大范围形态学变形。
            for _ in range(8):
                parts = shapely.get_parts(merged)
                if len(parts) <= 1:
                    break
                nearest = shapely.shortest_line(parts[0], shapely.union_all(parts[1:]))
                bridge = shapely.buffer(nearest, overlap, quad_segs=2)
                merged = shapely.union_all([merged, bridge])
        geometries[keep] = shapely.make_valid(
            merged, method="structure", keep_collapsed=False
        )
        merged_count[keep] = 2
        drop.add(remove)

    result = frame.copy()
    result.geometry = geometries
    result["merged_n"] = merged_count
    if drop:
        result = result.drop(index=sorted(drop)).copy()
    result = result.reset_index(drop=True)
    return result, len(matches)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, default=DEFAULT_INPUT)
    parser.add_argument("--raster", type=Path, default=DEFAULT_RASTER)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--shp-dir", type=Path, default=DEFAULT_SHP_DIR)
    parser.add_argument("--tif-dir", type=Path, default=DEFAULT_TIF_DIR)
    parser.add_argument("--source-field", default="src_tif")
    parser.add_argument(
        "--max-gap-pixels",
        type=float,
        default=15.0,
        help="最大修复缝宽（最终 TIFF 像元数），默认 15",
    )
    parser.add_argument(
        "--overlap-pixels",
        type=float,
        default=0.25,
        help="修复后两侧面的微小搭接宽度（像元数），默认 0.25",
    )
    parser.add_argument(
        "--min-merge-contact-pixels",
        type=float,
        default=20.0,
        help="判定为同一地块所需的最短跨缝对应边（像元数），默认 20",
    )
    parser.add_argument("--overwrite", action="store_true")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    for path, label in ((args.input, "输入 SHP"), (args.raster, "最终 TIFF")):
        if not path.is_file():
            raise FileNotFoundError(f"{label}不存在：{path.resolve()}")
    if (
        args.max_gap_pixels <= 0
        or args.overlap_pixels < 0
        or args.min_merge_contact_pixels <= 0
    ):
        raise ValueError("缝宽和融合边长必须大于 0，搭接像元数不能小于 0。")
    if args.output.resolve() == args.input.resolve():
        raise ValueError("为保护原数据，--output 不能与 --input 相同。")
    if args.output.exists() and not args.overwrite:
        raise FileExistsError(f"输出已存在：{args.output}；覆盖请添加 --overwrite。")

    print(f"读取：{args.input.resolve()}")
    frame = gpd.read_file(args.input)
    if frame.crs is None:
        raise ValueError("输入 SHP 缺少 CRS。")
    if args.source_field not in frame.columns:
        raise ValueError(f"输入 SHP 缺少来源字段：{args.source_field}")
    if frame.geometry.isna().any():
        frame = frame[frame.geometry.notna()].copy()

    source_names = {
        Path(value).stem for value in frame[args.source_field].dropna().astype(str)
    }
    validate_source_files(source_names, args.shp_dir, args.tif_dir)
    pixel_size = pixel_size_in_vector_crs(args.raster, frame.crs)
    max_gap = pixel_size * args.max_gap_pixels
    overlap = pixel_size * args.overlap_pixels
    print(
        f"共 {len(frame)} 个面；1 像元约 {pixel_size:.3f} 米；"
        f"最大修复缝宽 {max_gap:.3f} 米"
    )

    result, pair_count, feature_count, candidates = repair_seams(
        frame, args.source_field, max_gap, overlap
    )
    min_contact = pixel_size * args.min_merge_contact_pixels
    result, merged_pairs = merge_split_features(
        result, candidates, min_contact, overlap, max_gap
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    if args.overwrite:
        remove_shapefile(args.output)
    result.to_file(args.output, driver="ESRI Shapefile", encoding="UTF-8")
    print(
        f"完成：{args.output.resolve()}\n"
        f"修复跨来源近邻 {pair_count} 对，修改 {feature_count} 个面；\n"
        f"真正融合裂开地块 {merged_pairs} 对，输出 {len(result)} 个面。"
    )


if __name__ == "__main__":
    main()
