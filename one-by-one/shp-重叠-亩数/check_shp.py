"""检查 Shapefile 中的面重叠和小面积要素，并输出 CSV。

默认规则：
1. 两个面存在实际重叠面积时，记录两个要素 ID（仅共边/共点不算重叠）。
2. 要素面积小于 0.1 亩时，记录该要素 ID。
3. 属性表没有指定 ID 字段时，使用从 0 开始的 Shapefile FID/行号。
"""

from __future__ import annotations

import argparse
import shutil
import sys
import tempfile
from pathlib import Path

import geopandas as gpd
import pandas as pd
import shapely


SQM_PER_MU = 666.6666666666666
DEFAULT_MIN_OVERLAP_MU = 1e-5
DEFAULT_MIN_OVERLAP_SQM = DEFAULT_MIN_OVERLAP_MU * SQM_PER_MU


def polygonal_only(geometry):
    """从可能的 GeometryCollection 中只保留 Polygon/MultiPolygon 部分。"""
    if geometry is None or geometry.is_empty:
        return geometry
    if geometry.geom_type in {"Polygon", "MultiPolygon"}:
        return geometry

    polygons = []

    def collect(item) -> None:
        if item.geom_type == "Polygon":
            polygons.append(item)
        elif item.geom_type in {"MultiPolygon", "GeometryCollection"}:
            for part in item.geoms:
                collect(part)

    collect(geometry)
    if not polygons:
        raise ValueError("几何修复后不再包含面，无法保存为面 Shapefile。")
    return shapely.union_all(polygons)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="检查面 Shapefile 的重叠要素和面积小于指定亩数的要素。"
    )
    parser.add_argument(
        "input",
        nargs="?",
        type=Path,
        help="输入 .shp 文件；省略时自动使用 ./shp 目录中的唯一 .shp 文件。",
    )
    parser.add_argument(
        "-o",
        "--output",
        type=Path,
        help="输出 CSV 路径；省略时输出到 ./CSV/<Shapefile文件名>.csv。",
    )
    parser.add_argument(
        "--id-field",
        help="用作要素 ID 的属性字段；省略时使用从 0 开始的 FID/行号。",
    )
    parser.add_argument(
        "--min-mu",
        type=float,
        default=0.1,
        help="小面积阈值，单位为亩（默认：0.1）。",
    )
    parser.add_argument(
        "--min-overlap-sqm",
        type=float,
        default=DEFAULT_MIN_OVERLAP_SQM,
        help=(
            "最小重叠面积，单位为平方米，用于排除浮点误差"
            f"（默认：{DEFAULT_MIN_OVERLAP_SQM:g}，即 1e-5 亩）。"
        ),
    )
    return parser.parse_args()


def find_input_shp(input_path: Path | None) -> Path:
    if input_path is not None:
        path = input_path
    else:
        candidates = sorted(Path("shp").glob("*.shp"))
        if len(candidates) != 1:
            raise ValueError(
                "未指定输入文件，且 ./shp 中的 .shp 文件数量不是 1；"
                "请在命令中明确指定输入文件。"
            )
        path = candidates[0]

    if not path.is_file() or path.suffix.lower() != ".shp":
        raise FileNotFoundError(f"找不到有效的 Shapefile：{path}")
    return path


def choose_metric_crs(gdf: gpd.GeoDataFrame):
    """选择适合当前数据范围、单位为米的投影坐标系。"""
    if gdf.crs is None:
        raise ValueError(
            "Shapefile 缺少坐标系（.prj），无法可靠计算面积。"
            "请先为数据定义正确的坐标系。"
        )

    if gdf.crs.is_projected:
        axis_info = gdf.crs.axis_info
        if axis_info and all(axis.unit_name.lower() in {"metre", "meter"} for axis in axis_info):
            return gdf.crs

    metric_crs = gdf.estimate_utm_crs()
    if metric_crs is None:
        raise ValueError("无法根据数据范围自动确定米制投影坐标系。")
    return metric_crs


def build_ids(gdf: gpd.GeoDataFrame, id_field: str | None) -> pd.Series:
    if id_field is None:
        return pd.Series(gdf.index, index=gdf.index, name="要素ID")

    if id_field not in gdf.columns:
        available = ", ".join(column for column in gdf.columns if column != "geometry")
        raise ValueError(f"找不到 ID 字段“{id_field}”。可用字段：{available}")
    if gdf[id_field].isna().any():
        raise ValueError(f"ID 字段“{id_field}”包含空值。")
    if gdf[id_field].duplicated().any():
        raise ValueError(f"ID 字段“{id_field}”包含重复值，不能唯一标识要素。")
    return gdf[id_field]


def merge_small_features(
    input_path: Path,
    output_path: Path,
    id_field: str | None = None,
    min_mu: float = 0.1,
) -> dict[str, object]:
    """把小面积面合并到直接相邻且面积最大的非小面积面。

    原文件不会被修改。没有符合条件的相邻大面时，从结果中删除该小面。
    """
    if min_mu < 0:
        raise ValueError("min_mu 不能小于 0。")

    gdf = gpd.read_file(input_path)
    if gdf.empty:
        raise ValueError("Shapefile 中没有要素。")

    allowed_types = {"Polygon", "MultiPolygon"}
    unexpected_types = sorted(set(gdf.geom_type.dropna()) - allowed_types)
    if unexpected_types:
        raise ValueError(
            "输入数据必须是面数据，发现其他几何类型：" + ", ".join(unexpected_types)
        )

    ids = build_ids(gdf, id_field).reset_index(drop=True)
    metric_crs = choose_metric_crs(gdf)
    source = gdf.reset_index(drop=True)
    geometries = source.geometry.array

    empty_mask = shapely.is_empty(geometries) | shapely.is_missing(geometries)
    if empty_mask.any():
        raise ValueError("存在空几何要素，无法执行自动合并。")
    invalid_mask = ~shapely.is_valid(geometries)
    if invalid_mask.any():
        source.geometry = shapely.make_valid(geometries)
    source.geometry = source.geometry.map(polygonal_only)
    geometries = source.geometry.array

    projected = source.to_crs(metric_crs)
    areas_sqm = shapely.area(projected.geometry.array)
    small_indices = set(
        pd.Series(areas_sqm).index[
            areas_sqm < min_mu * SQM_PER_MU
        ].tolist()
    )

    target_indices = set(range(len(source))) - small_indices

    assignments: dict[int, int] = {}
    for small_index in sorted(small_indices):
        candidates = source.sindex.query(
            source.geometry.iloc[small_index], predicate="intersects"
        )
        eligible = [
            int(index)
            for index in candidates
            if int(index) in target_indices
        ]
        if eligible:
            target_index = max(
                eligible, key=lambda index: float(areas_sqm[index])
            )
        else:
            continue
        assignments[small_index] = target_index

    sources_by_target: dict[int, list[int]] = {}
    for small_index, target_index in assignments.items():
        sources_by_target.setdefault(target_index, []).append(small_index)

    for target_index, source_indices in sources_by_target.items():
        merge_indices = [target_index, *source_indices]
        source.at[target_index, "geometry"] = polygonal_only(
            shapely.make_valid(
                shapely.union_all(geometries.take(merge_indices))
            )
        )

    deleted_indices = sorted(small_indices - set(assignments))
    # 成功合并的小面及无法找到相邻目标的小面都从结果中移除。
    removed_indices = sorted(small_indices)
    merged_gdf = source.drop(index=removed_indices).reset_index(drop=True)
    merged_gdf.geometry = merged_gdf.geometry.map(polygonal_only)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    # GDAL 直接逐条写入 NAS/网络挂载目录时可能中途失败。先在本机临时目录
    # 完整生成全部 Shapefile 组件，成功后再复制到用户指定目录。
    with tempfile.TemporaryDirectory(prefix="merged-shp-") as temp_name:
        local_output = Path(temp_name) / output_path.name
        try:
            merged_gdf.to_file(
                local_output,
                driver="ESRI Shapefile",
                encoding="UTF-8",
                index=False,
            )
        except Exception as exc:
            raise RuntimeError(f"生成合并后 Shapefile 失败：{exc}") from exc

        component_files = list(
            local_output.parent.glob(f"{local_output.stem}.*")
        )
        if not component_files:
            raise RuntimeError("未生成 Shapefile 配套文件。")
        try:
            for component in component_files:
                destination = output_path.with_suffix(component.suffix)
                shutil.copy2(component, destination)
        except Exception as exc:
            raise RuntimeError(
                f"复制合并结果到“{output_path.parent}”失败：{exc}"
            ) from exc

    assignment_rows = [
        {
            "source_id": str(ids.iloc[small_index]),
            "target_id": str(ids.iloc[target_index]),
            "method": "相邻最大面积面",
        }
        for small_index, target_index in sorted(assignments.items())
    ]
    deleted_ids = [str(ids.iloc[index]) for index in deleted_indices]
    return {
        "merged_count": len(assignments),
        "deleted_count": len(deleted_ids),
        "nearest_count": 0,
        "skipped_count": 0,
        "assignments": assignment_rows,
        "deleted_ids": deleted_ids,
        "skipped_ids": [],
        "output_path": str(output_path.resolve()),
    }


def check_shp(
    input_path: Path,
    output_path: Path | None,
    id_field: str | None,
    min_mu: float,
    min_overlap_sqm: float,
) -> tuple[int, int]:
    if min_mu < 0:
        raise ValueError("--min-mu 不能小于 0。")
    if min_overlap_sqm < 0:
        raise ValueError("--min-overlap-sqm 不能小于 0。")

    if output_path is None:
        output_path = Path("CSV") / f"{input_path.stem}.csv"
    source_file_path = str(input_path.resolve())

    gdf = gpd.read_file(input_path)
    if gdf.empty:
        raise ValueError("Shapefile 中没有要素。")

    allowed_types = {"Polygon", "MultiPolygon"}
    unexpected_types = sorted(set(gdf.geom_type.dropna()) - allowed_types)
    if unexpected_types:
        raise ValueError(
            "输入数据必须是面数据，发现其他几何类型：" + ", ".join(unexpected_types)
        )

    ids = build_ids(gdf, id_field).reset_index(drop=True)
    metric_crs = choose_metric_crs(gdf)
    source = gdf.reset_index(drop=True)
    source_geometries = source.geometry.array
    empty_mask = shapely.is_empty(source_geometries) | shapely.is_missing(
        source_geometries
    )
    if empty_mask.any():
        bad_ids = ", ".join(map(str, ids[empty_mask].tolist()[:10]))
        suffix = "……" if empty_mask.sum() > 10 else ""
        raise ValueError(f"存在空几何要素，ID：{bad_ids}{suffix}")

    invalid_mask = ~shapely.is_valid(source_geometries)
    if invalid_mask.any():
        source.geometry = shapely.make_valid(source_geometries)
        source_geometries = source.geometry.array

    projected = source.to_crs(metric_crs)
    areas_sqm = shapely.area(projected.geometry.array)
    areas_mu = areas_sqm / SQM_PER_MU
    small_indices = pd.Series(areas_mu).index[areas_mu < min_mu].to_numpy()

    # 先在原始坐标系中做拓扑判断，避免相邻边界分别投影后产生极小的假重叠；
    # 再把真实的交集几何整体投影到米制坐标系计算面积。
    left, right = source.sindex.query(source.geometry, predicate="intersects")
    unique_pair_mask = left < right
    left = left[unique_pair_mask]
    right = right[unique_pair_mask]

    if len(left):
        intersections = shapely.intersection(
            source_geometries.take(left), source_geometries.take(right)
        )
        # 交集必须包含面（维度为 2）；线或点表示仅共边/共点。
        overlap_mask = (~shapely.is_empty(intersections)) & (
            shapely.get_dimensions(intersections) == 2
        )
        left = left[overlap_mask]
        right = right[overlap_mask]
        overlap_geometries = gpd.GeoSeries(
            intersections[overlap_mask], crs=source.crs
        ).to_crs(metric_crs)
        overlap_sqm = shapely.area(overlap_geometries.array)
        area_mask = overlap_sqm > min_overlap_sqm
        left = left[area_mask]
        right = right[area_mask]
        overlap_mu = overlap_sqm[area_mask] / SQM_PER_MU
    else:
        overlap_mu = []

    rows: list[dict[str, object]] = []
    for index_1, index_2, area_mu in zip(left, right, overlap_mu, strict=True):
        rows.append(
            {
                "文件路径": source_file_path,
                "问题类型": "面重叠",
                "ID_1": str(ids.iloc[index_1]),
                "ID_2": str(ids.iloc[index_2]),
                "要素面积_亩": None,
                "重叠面积_亩": round(float(area_mu), 8),
                "说明": "两个面存在实际重叠；仅共边或共点不计入",
            }
        )

    for index in small_indices:
        rows.append(
            {
                "文件路径": source_file_path,
                "问题类型": f"面积小于{min_mu:g}亩",
                "ID_1": str(ids.iloc[index]),
                "ID_2": None,
                "要素面积_亩": round(float(areas_mu[index]), 8),
                "重叠面积_亩": None,
                "说明": f"要素面积小于{min_mu:g}亩",
            }
        )

    columns = [
        "文件路径",
        "问题类型",
        "ID_1",
        "ID_2",
        "要素面积_亩",
        "重叠面积_亩",
        "说明",
    ]
    result = pd.DataFrame(rows, columns=columns)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    result.to_csv(output_path, index=False, encoding="utf-8-sig")

    print(f"输入文件：{input_path.resolve()}")
    print(f"要素数量：{len(gdf)}")
    print(f"面积计算坐标系：{metric_crs}")
    print(f"实际重叠要素对：{len(left)}")
    print(f"面积小于 {min_mu:g} 亩的要素：{len(small_indices)}")
    print(f"结果文件：{output_path.resolve()}")
    return len(left), len(small_indices)


def main() -> int:
    args = parse_args()
    try:
        input_path = find_input_shp(args.input)
        check_shp(
            input_path,
            args.output,
            args.id_field,
            args.min_mu,
            args.min_overlap_sqm,
        )
    except Exception as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
