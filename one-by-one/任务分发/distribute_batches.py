#!/usr/bin/env python
"""按县将 Shapefile 图斑空间就近、分层聚合地均分为三个批次。"""

from __future__ import annotations

import argparse
import csv
import logging
import os
import shutil
import sys
import tempfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Iterable

import geopandas as gpd
import numpy as np
import pandas as pd


FIELD_NAME = "批次"
REQUIRED_FIELDS = ("QXMC", "XZMC", "CUNDM", "YZCHBM", "FCBM")
GENERATED_SUFFIXES = {".shp", ".shx", ".dbf", ".prj", ".cpg"}
SHAPEFILE_ENCODING = "UTF-8"
MAX_UNIT_DIFFERENCE_RATIO = 0.05
ONE_BATCH_MAX_UNITS = 10
TWO_BATCH_MAX_UNITS = 20


@dataclass
class CountyResult:
    county: str
    total: int
    batch_counts: tuple[int, int, int]
    split_towns: int
    split_villages: int
    unit_count: int
    split_units: int
    unit_batch_counts: tuple[int, int, int]
    unit_count_difference: int


def active_count_difference(counts: Iterable[int]) -> int:
    active = [int(count) for count in counts if int(count) > 0]
    return max(active) - min(active) if len(active) > 1 else 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="递归处理 SHP：每县分成数量相等、镇村尽量完整且空间相近的三个批次。"
    )
    parser.add_argument("root", nargs="?", default="10个", help="包含 SHP 的根目录")
    parser.add_argument(
        "--execute", action="store_true", help="正式写回原 SHP；不加此参数只试算、不改数据"
    )
    parser.add_argument(
        "--no-backup", action="store_true", help="正式写入时不保留原文件备份（不推荐）"
    )
    parser.add_argument(
        "--field", default=FIELD_NAME, help=f"批次字段名，默认：{FIELD_NAME}"
    )
    parser.add_argument(
        "--log-dir", default="日志", help="日志目录；相对路径将放在 root 目录内"
    )
    return parser.parse_args()


def configure_logging(log_dir: Path, execute: bool) -> tuple[logging.Logger, Path, Path]:
    log_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    mode = "正式" if execute else "试算"
    log_path = log_dir / f"分发日志_{mode}_{stamp}.log"
    csv_path = log_dir / f"分发汇总_{mode}_{stamp}.csv"

    logger = logging.getLogger("batch-distributor")
    logger.setLevel(logging.INFO)
    logger.handlers.clear()
    formatter = logging.Formatter("%(asctime)s | %(levelname)s | %(message)s")
    file_handler = logging.FileHandler(log_path, encoding="utf-8")
    file_handler.setFormatter(formatter)
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(formatter)
    logger.addHandler(file_handler)
    logger.addHandler(console_handler)
    return logger, log_path, csv_path


def normalized_text(series: pd.Series, fallback: str) -> pd.Series:
    values = series.astype("string").str.strip()
    return values.mask(values.isna() | values.eq(""), fallback).astype(str)


def normalize_assignment_units(
    gdf: gpd.GeoDataFrame, logger: logging.Logger
) -> np.ndarray:
    fcbm_text = gdf["FCBM"].astype("string").str.strip()
    zero_mask = fcbm_text.str.fullmatch(r"0(?:\.0+)?", na=False)
    if zero_mask.any():
        gdf["FCBM"] = gdf["FCBM"].astype(object)
        gdf.loc[zero_mask, "FCBM"] = "0000"
        logger.info("已将 %d 条 FCBM=0 修改为 0000", int(zero_mask.sum()))

    yzchbm = normalized_text(gdf["YZCHBM"], "〔YZCHBM缺失〕")
    fcbm = normalized_text(gdf["FCBM"], "〔FCBM缺失〕")
    county = normalized_text(gdf["QXMC"], "〔县名缺失〕")
    # 组合单位只在县内唯一；不同县的相同编码属于不同单位。
    unit_ids = (county + "\x1f" + yzchbm + "\x1f" + fcbm).to_numpy()
    logger.info("县内 YZCHBM+FCBM 组合单位数：%d", len(np.unique(unit_ids)))
    return unit_ids


def build_group_columns(gdf: gpd.GeoDataFrame) -> pd.DataFrame:
    county = normalized_text(gdf["QXMC"], "〔县名缺失〕")
    town = normalized_text(gdf["XZMC"], "〔镇名缺失〕")
    village_code = gdf["CUNDM"].astype("string").str.strip()
    if "CUNMC" in gdf.columns:
        village_name = gdf["CUNMC"].astype("string").str.strip()
        village_code = village_code.mask(
            village_code.isna() | village_code.eq(""), village_name
        )
    missing_village = village_code.isna() | village_code.eq("")
    village_code = village_code.astype(object)
    # 村信息缺失时将单个图斑作为最小单元，避免把所有缺失项误当成一个村。
    village_code.loc[missing_village] = [
        f"〔村名缺失-图斑{i + 1}〕" for i in np.flatnonzero(missing_village.to_numpy())
    ]
    return pd.DataFrame(
        {"county": county, "town": town, "village": village_code.astype(str)},
        index=gdf.index,
    )


def representative_xy(gdf: gpd.GeoDataFrame, logger: logging.Logger) -> np.ndarray:
    invalid = (~gdf.geometry.is_valid & gdf.geometry.notna()).sum()
    empty = (gdf.geometry.isna() | gdf.geometry.is_empty).sum()
    if invalid:
        logger.warning("发现 %d 个无效几何；使用其代表点/质心参与距离计算", invalid)
    if empty:
        logger.warning("发现 %d 个空几何；将使用经纬度字段或组中心作为距离回退", empty)

    projected = gdf
    if gdf.crs is not None and gdf.crs.is_geographic:
        try:
            metric_crs = gdf.estimate_utm_crs()
            if metric_crs is not None:
                projected = gdf.to_crs(metric_crs)
        except Exception as exc:  # 距离仍可用原坐标做相对比较
            logger.warning("投影到米制坐标失败，改用原坐标：%s", exc)
    elif gdf.crs is None:
        logger.warning("SHP 缺少坐标系，距离将按原始坐标计算")

    points = projected.geometry.representative_point()
    xy = np.column_stack((points.x.to_numpy(), points.y.to_numpy())).astype(float)
    bad = ~np.isfinite(xy).all(axis=1)
    if bad.any() and {"LON", "LAT"}.issubset(gdf.columns):
        lon = pd.to_numeric(gdf["LON"], errors="coerce").to_numpy(float)
        lat = pd.to_numeric(gdf["LAT"], errors="coerce").to_numpy(float)
        fallback_xy = np.column_stack((lon, lat))
        usable = bad & np.isfinite(fallback_xy).all(axis=1)
        xy[usable] = fallback_xy[usable]
        bad = ~np.isfinite(xy).all(axis=1)
    if bad.any():
        good_center = np.nanmean(np.where(np.isfinite(xy), xy, np.nan), axis=0)
        if not np.isfinite(good_center).all():
            good_center = np.array([0.0, 0.0])
        xy[bad] = good_center
    return xy


def minimum_spanning_tree(points: np.ndarray) -> list[tuple[int, int, float]]:
    """用 Prim 算法构建邻近最小生成树，不依赖 scipy。"""
    n = len(points)
    in_tree = np.zeros(n, dtype=bool)
    nearest_distance = np.full(n, np.inf)
    parent = np.full(n, -1, dtype=int)
    nearest_distance[0] = 0.0
    edges: list[tuple[int, int, float]] = []

    for _ in range(n):
        candidates = np.where(in_tree, np.inf, nearest_distance)
        node = int(np.argmin(candidates))
        if parent[node] >= 0:
            edges.append((node, int(parent[node]), float(nearest_distance[node])))
        in_tree[node] = True
        delta = points - points[node]
        distances = np.einsum("ij,ij->i", delta, delta)
        better = (~in_tree) & (distances < nearest_distance)
        nearest_distance[better] = distances[better]
        parent[better] = node
    return edges


def components_after_cuts(
    n: int,
    edges: list[tuple[int, int, float]],
    cut_edges: frozenset[int],
) -> list[np.ndarray]:
    adjacency: list[list[int]] = [[] for _ in range(n)]
    for edge_index, (left, right, _distance) in enumerate(edges):
        if edge_index in cut_edges:
            continue
        adjacency[left].append(right)
        adjacency[right].append(left)

    unseen = set(range(n))
    components: list[np.ndarray] = []
    while unseen:
        start = min(unseen)
        stack = [start]
        unseen.remove(start)
        members: list[int] = []
        while stack:
            node = stack.pop()
            members.append(node)
            for neighbor in adjacency[node]:
                if neighbor in unseen:
                    unseen.remove(neighbor)
                    stack.append(neighbor)
        components.append(np.asarray(members, dtype=int))
    return components


def is_ancestor(node: int, descendant: int, tin: np.ndarray, tout: np.ndarray) -> bool:
    return bool(tin[node] <= tin[descendant] < tout[node])


def balanced_projection_components(
    points: np.ndarray, batch_count: int
) -> list[np.ndarray]:
    """沿多种空间方向等量切分，选择内部最紧凑的方案。"""
    n = len(points)
    base, remainder = divmod(n, batch_count)
    sizes = [base + (1 if batch < remainder else 0) for batch in range(batch_count)]
    centered = points - points.mean(axis=0)
    best: tuple[tuple[float, float, int], list[np.ndarray]] | None = None

    # 2.5 度一个方向；相反方向产生相同分区，因此只遍历 0~180 度。
    for angle_index, angle in enumerate(np.linspace(0.0, np.pi, 72, endpoint=False)):
        axis = np.array([np.cos(angle), np.sin(angle)])
        projection = centered @ axis
        order = np.lexsort((np.arange(n), points[:, 1], points[:, 0], projection))
        boundaries = np.cumsum([0, *sizes])
        components = [
            order[boundaries[index] : boundaries[index + 1]]
            for index in range(batch_count)
        ]
        cluster_costs: list[float] = []
        for component in components:
            cluster_points = points[component]
            center = cluster_points.mean(axis=0)
            delta = cluster_points - center
            cluster_costs.append(float(np.einsum("ij,ij->", delta, delta)))
        # 先压低最松散区块，再压低三区总体内部距离。
        score = (max(cluster_costs), sum(cluster_costs), angle_index)
        if best is None or score < best[0]:
            best = (score, components)
    if best is None:
        raise RuntimeError("无法生成均衡空间三区")
    return best[1]


def choose_spatial_components(
    points: np.ndarray, batch_count: int
) -> list[np.ndarray]:
    """组合单位不可拆；限制单位数差后，选择最清晰的空间边界。"""
    n = len(points)
    if batch_count == 1:
        return [np.arange(n, dtype=int)]
    if n <= batch_count:
        return [np.asarray([index], dtype=int) for index in range(n)]

    edges = minimum_spanning_tree(points)
    children: list[list[int]] = [[] for _ in range(n)]
    subtree_units = np.ones(n, dtype=np.int64)
    for child, parent, _distance in edges:
        children[parent].append(child)
    # Prim 生成边的顺序保证父节点先于子节点加入；逆序累计组合单位数。
    for child, parent, _distance in reversed(edges):
        subtree_units[parent] += subtree_units[child]

    tin = np.zeros(n, dtype=int)
    tout = np.zeros(n, dtype=int)
    clock = 0
    stack: list[tuple[int, bool]] = [(0, False)]
    while stack:
        node, exiting = stack.pop()
        if exiting:
            tout[node] = clock
            continue
        tin[node] = clock
        clock += 1
        stack.append((node, True))
        for child in reversed(children[node]):
            stack.append((child, False))

    allowed_unit_difference = max(1, int(np.ceil(n * MAX_UNIT_DIFFERENCE_RATIO)))
    if batch_count == 2:
        best_two: tuple[tuple[float, int, int], int] | None = None
        for edge_index, (child, _parent, distance) in enumerate(edges):
            counts = (int(subtree_units[child]), n - int(subtree_units[child]))
            difference = max(counts) - min(counts)
            if min(counts) <= 0 or difference > allowed_unit_difference:
                continue
            score = (-distance, difference, edge_index)
            if best_two is None or score < best_two[0]:
                best_two = (score, edge_index)
        if best_two is None:
            return balanced_projection_components(points, batch_count)
        return components_after_cuts(n, edges, frozenset((best_two[1],)))

    best: tuple[
        tuple[float, float, int, int, int], tuple[int, int]
    ] | None = None
    for first in range(len(edges)):
        first_child = edges[first][0]
        first_units = int(subtree_units[first_child])
        for second in range(first + 1, len(edges)):
            second_child = edges[second][0]
            second_units = int(subtree_units[second_child])
            if is_ancestor(first_child, second_child, tin, tout):
                unit_counts = (
                    second_units,
                    first_units - second_units,
                    n - first_units,
                )
            elif is_ancestor(second_child, first_child, tin, tout):
                unit_counts = (
                    first_units,
                    second_units - first_units,
                    n - second_units,
                )
            else:
                unit_counts = (
                    first_units,
                    second_units,
                    n - first_units - second_units,
                )
            unit_difference = max(unit_counts) - min(unit_counts)
            if min(unit_counts) <= 0 or unit_difference > allowed_unit_difference:
                continue
            first_distance = edges[first][2]
            second_distance = edges[second][2]
            shorter_boundary = min(first_distance, second_distance)
            total_boundary = first_distance + second_distance
            # 单位数先达到“差不多”；合格后最大化两个空间切口。
            score = (
                -shorter_boundary,
                -total_boundary,
                unit_difference,
                first,
                second,
            )
            pair = (first, second)
            if best is None or score < best[0]:
                best = (score, pair)

    if best is None:
        return balanced_projection_components(points, batch_count)
    best_pair = best[1]
    return components_after_cuts(n, edges, frozenset(best_pair))


def assign_county(
    county_indices: np.ndarray,
    unit_ids: np.ndarray,
    xy: np.ndarray,
) -> np.ndarray:
    local_unit_ids = unit_ids[county_indices]
    unit_members: dict[str, list[int]] = {}
    for local_index, unit_id in enumerate(local_unit_ids):
        unit_members.setdefault(str(unit_id), []).append(local_index)
    units = [np.asarray(members, dtype=int) for members in unit_members.values()]
    local_xy = xy[county_indices]
    unit_xy = np.asarray([local_xy[members].mean(axis=0) for members in units])
    unit_count = len(units)
    if unit_count <= ONE_BATCH_MAX_UNITS:
        batch_count = 1
    elif unit_count <= TWO_BATCH_MAX_UNITS:
        batch_count = 2
    else:
        batch_count = 3
    components = choose_spatial_components(unit_xy, batch_count)
    # 批次号固定按空间中心从西到东、再从南到北排列，保证重复运行一致。
    components.sort(
        key=lambda component: (
            float(unit_xy[component, 0].mean()),
            float(unit_xy[component, 1].mean()),
            int(component.min()),
        )
    )
    assignments = np.zeros(len(county_indices), dtype=np.int8)
    for batch_number, component in enumerate(components, start=1):
        for unit_index in component:
            assignments[units[int(unit_index)]] = batch_number
    return assignments


def log_county_detail(
    logger: logging.Logger,
    source: Path,
    county: str,
    local_groups: pd.DataFrame,
    local_batches: np.ndarray,
) -> CountyResult:
    detail = local_groups.copy()
    detail["batch"] = local_batches
    batch_counts = tuple(
        int((local_batches == batch).sum()) for batch in (1, 2, 3)
    )
    town_table = pd.crosstab(detail["town"], detail["batch"])
    village_table = pd.crosstab(
        [detail["town"], detail["village"]], detail["batch"]
    )
    unit_table = pd.crosstab(detail["unit"], detail["batch"])
    split_towns = int((town_table.gt(0).sum(axis=1) > 1).sum())
    split_villages = int((village_table.gt(0).sum(axis=1) > 1).sum())
    split_units = int((unit_table.gt(0).sum(axis=1) > 1).sum())
    unit_batch_counts = tuple(
        int(unit_table.get(batch, pd.Series(dtype=int)).gt(0).sum())
        for batch in (1, 2, 3)
    )
    unit_count_difference = active_count_difference(unit_batch_counts)
    feature_count_difference = active_count_difference(batch_counts)
    active_batch_count = sum(count > 0 for count in unit_batch_counts)
    logger.info(
        "县=%s | 启用批次数=%d | 总数=%d | 批次1/2/3=%s | 最大差=%d | 组合单位=%d | 组合批次1/2/3=%s | 组合单位数差=%d | 跨批组合=%d | 跨批镇=%d | 跨批村=%d",
        county,
        active_batch_count,
        len(local_batches),
        "/".join(map(str, batch_counts)),
        feature_count_difference,
        len(unit_table),
        "/".join(map(str, unit_batch_counts)),
        unit_count_difference,
        split_units,
        split_towns,
        split_villages,
    )
    for town, row in town_table.iterrows():
        counts = [int(row.get(batch, 0)) for batch in (1, 2, 3)]
        logger.info("  镇=%s | 批次1/2/3=%s", town, "/".join(map(str, counts)))
    for (town, village), row in village_table.iterrows():
        counts = [int(row.get(batch, 0)) for batch in (1, 2, 3)]
        logger.info(
            "    村=%s / %s | 批次1/2/3=%s",
            town,
            village,
            "/".join(map(str, counts)),
        )
    return CountyResult(
        county=county,
        total=len(local_batches),
        batch_counts=batch_counts,
        split_towns=split_towns,
        split_villages=split_villages,
        unit_count=len(unit_table),
        split_units=split_units,
        unit_batch_counts=unit_batch_counts,
        unit_count_difference=unit_count_difference,
    )


def source_sidecars(shp_path: Path) -> list[Path]:
    return sorted(
        path
        for path in shp_path.parent.iterdir()
        if path.is_file() and path.stem.lower() == shp_path.stem.lower()
    )


def write_in_place(
    gdf: gpd.GeoDataFrame,
    shp_path: Path,
    field_name: str,
    backup: bool,
    logger: logging.Logger,
) -> Path | None:
    original_files = source_sidecars(shp_path)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_dir = shp_path.parent / f"{shp_path.stem}_分发前备份_{timestamp}"
    if backup:
        backup_dir.mkdir(parents=True, exist_ok=False)
        for source in original_files:
            shutil.copy2(source, backup_dir / source.name)
        logger.info("已备份 %d 个原始配套文件到：%s", len(original_files), backup_dir)

    temp_dir = Path(tempfile.mkdtemp(prefix=f".{shp_path.stem}_分发临时_", dir=shp_path.parent))
    temp_shp = temp_dir / shp_path.name
    try:
        # DBF 字段名上限为 10 字节。“批次”在 UTF-8 下为 6 字节，
        # 可完整保留字段名，同时不改变原 SHP 使用的 UTF-8 编码。
        gdf.to_file(
            temp_shp,
            driver="ESRI Shapefile",
            encoding=SHAPEFILE_ENCODING,
            index=False,
        )
        check = gpd.read_file(temp_shp)
        if len(check) != len(gdf):
            raise RuntimeError(f"写入后数量不一致：{len(gdf)} -> {len(check)}")
        if field_name not in check.columns:
            raise RuntimeError(
                f"写入后找不到字段“{field_name}”；请将 --field 改为不超过 10 字符的名称"
            )
        values = set(pd.to_numeric(check[field_name], errors="coerce").dropna().astype(int))
        if not values.issubset({1, 2, 3}) or len(check[field_name].dropna()) != len(check):
            raise RuntimeError(f"写入后批次字段校验失败，发现值：{sorted(values)}")
        check_fcbm = check["FCBM"].astype("string").str.strip()
        if check_fcbm.str.fullmatch(r"0(?:\.0+)?", na=False).any():
            raise RuntimeError("写入后仍发现 FCBM=0，规范化校验失败")
        integrity = check.groupby(
            ["QXMC", "YZCHBM", "FCBM"], dropna=False
        )[field_name].nunique()
        if (integrity > 1).any():
            raise RuntimeError("写入后发现同县相同 YZCHBM+FCBM 组合被拆到不同批次")

        generated = {path.suffix.lower(): path for path in source_sidecars(temp_shp)}
        # 清除旧空间索引等未重建的配套文件，防止其与新 DBF/SHP 不一致。
        for old_file in original_files:
            if old_file.suffix.lower() not in generated:
                old_file.unlink()
        for suffix, temp_file in generated.items():
            destination = shp_path.with_suffix(suffix)
            os.replace(temp_file, destination)
        logger.info("正式写入并复读校验成功：%s", shp_path)
        return backup_dir if backup else None
    except Exception:
        logger.exception("写入失败：%s", shp_path)
        if backup and backup_dir.exists():
            for current in source_sidecars(shp_path):
                current.unlink()
            for saved in backup_dir.iterdir():
                shutil.copy2(saved, shp_path.parent / saved.name)
            logger.warning("已从备份恢复原文件：%s", shp_path)
        raise
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


def process_file(
    shp_path: Path,
    field_name: str,
    execute: bool,
    backup: bool,
    logger: logging.Logger,
) -> list[CountyResult]:
    logger.info("=" * 72)
    logger.info("开始处理：%s", shp_path)
    gdf = gpd.read_file(shp_path)
    missing = [field for field in REQUIRED_FIELDS if field not in gdf.columns]
    if missing:
        raise ValueError(f"缺少必要字段：{', '.join(missing)}")
    if len(gdf) == 0:
        logger.warning("空 SHP，跳过：%s", shp_path)
        return []

    working = gdf.reset_index(drop=True)
    unit_ids = normalize_assignment_units(working, logger)
    groups = build_group_columns(working).reset_index(drop=True)
    groups["unit"] = unit_ids
    xy = representative_xy(working, logger)
    batches = np.zeros(len(working), dtype=np.int8)
    results: list[CountyResult] = []

    for county in groups["county"].drop_duplicates():
        county_indices = np.flatnonzero(groups["county"].to_numpy() == county)
        local_batches = assign_county(
            county_indices,
            unit_ids,
            xy,
        )
        batches[county_indices] = local_batches
        results.append(
            log_county_detail(
                logger,
                shp_path,
                county,
                groups.iloc[county_indices],
                local_batches,
            )
        )

    if not np.isin(batches, [1, 2, 3]).all():
        raise RuntimeError("存在未分配或非法批次")
    integrity = pd.DataFrame({"unit": unit_ids, "batch": batches}).groupby("unit")[
        "batch"
    ].nunique()
    if (integrity > 1).any():
        raise RuntimeError("同县相同 YZCHBM+FCBM 组合被拆到不同批次")
    working[field_name] = batches.astype(np.int32)
    if execute:
        write_in_place(working, shp_path, field_name, backup, logger)
    else:
        logger.info("试算完成，未修改原文件：%s", shp_path)
    return results


def write_summary(
    csv_path: Path, rows: Iterable[tuple[Path, CountyResult]], root: Path
) -> None:
    with csv_path.open("w", newline="", encoding="utf-8-sig") as file:
        writer = csv.writer(file)
        writer.writerow(
            [
                "SHP文件",
                "县",
                "总单位",
                "单位批次1",
                "单位批次2",
                "单位批次3",
                "单位数差",
                "跨批单位数",
                "跨批镇数",
                "跨批村数",
                "总图斑数",
                "图斑批次1",
                "图斑批次2",
                "图斑批次3",
                "图斑数差",
            ]
        )
        for shp_path, result in rows:
            counts = result.batch_counts
            writer.writerow(
                [
                    str(shp_path.relative_to(root)),
                    result.county,
                    result.unit_count,
                    *result.unit_batch_counts,
                    result.unit_count_difference,
                    result.split_units,
                    result.split_towns,
                    result.split_villages,
                    result.total,
                    *counts,
                    active_count_difference(counts),
                ]
            )


def main() -> int:
    args = parse_args()
    root = Path(args.root).resolve()
    if not root.is_dir():
        print(f"错误：根目录不存在：{root}", file=sys.stderr)
        return 2
    try:
        encoded_field = args.field.encode(SHAPEFILE_ENCODING)
    except UnicodeEncodeError:
        print(f"错误：字段名无法使用 {SHAPEFILE_ENCODING} 编码：{args.field}", file=sys.stderr)
        return 2
    if len(encoded_field) > 10:
        print(
            f"错误：SHP 字段名最多 10 字节；“{args.field}”使用 {SHAPEFILE_ENCODING} 后为 "
            f"{len(encoded_field)} 字节",
            file=sys.stderr,
        )
        return 2
    log_dir = Path(args.log_dir)
    if not log_dir.is_absolute():
        log_dir = root / log_dir
    logger, log_path, csv_path = configure_logging(log_dir, args.execute)
    shp_files = sorted(
        path for path in root.rglob("*") if path.is_file() and path.suffix.lower() == ".shp"
    )
    # 避免再次扫描默认备份目录或本脚本留下的临时目录。
    shp_files = [
        path
        for path in shp_files
        if not any("分发前备份_" in part or "_分发临时_" in part for part in path.parts)
    ]
    logger.info("运行模式：%s", "正式写入" if args.execute else "试算（不修改数据）")
    logger.info("扫描目录：%s", root)
    logger.info("发现 %d 个 SHP", len(shp_files))
    if not shp_files:
        logger.error("没有找到 SHP 文件")
        return 1

    summary_rows: list[tuple[Path, CountyResult]] = []
    failed: list[tuple[Path, str]] = []
    for shp_path in shp_files:
        try:
            results = process_file(
                shp_path,
                args.field,
                args.execute,
                not args.no_backup,
                logger,
            )
            summary_rows.extend((shp_path, result) for result in results)
        except Exception as exc:
            logger.exception("文件处理失败，继续处理下一个：%s", shp_path)
            failed.append((shp_path, str(exc)))

    write_summary(csv_path, summary_rows, root)
    logger.info("=" * 72)
    logger.info("处理结束：成功文件=%d，失败文件=%d", len(shp_files) - len(failed), len(failed))
    logger.info("详细日志：%s", log_path)
    logger.info("汇总表：%s", csv_path)
    for path, reason in failed:
        logger.error("失败文件：%s | 原因：%s", path, reason)
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
