from __future__ import annotations

import math
import re
from collections import defaultdict
from pathlib import Path
from typing import Iterable

import fiona

from .crs_check import check_albers_crs
from .models import CheckResult, FieldSpec

TYPE_RE = re.compile(r"^(?P<base>[^:]+)(?::(?P<width>\d+)(?:\.(?P<decimals>\d+))?)?$")
METRIC_RE = re.compile(r"^(MA|LR|CR)(\d{3})$")
SYSTEM_FIELD_RE = re.compile(
    r"^(OBJECTID(?:_\d+)?|FID|Shape(?:_Length|_Area|_Leng|_Le_\d+)?|GLOBALID)$",
    re.IGNORECASE,
)


def _empty(value: object) -> bool:
    if value is None:
        return True
    if isinstance(value, str):
        return not value.strip()
    if isinstance(value, float):
        return math.isnan(value)
    return False


def _parse_type(value: str) -> tuple[str, int | None, int | None]:
    match = TYPE_RE.match(value)
    if not match:
        return value.lower(), None, None
    width = int(match.group("width")) if match.group("width") else None
    decimals = int(match.group("decimals")) if match.group("decimals") else None
    return match.group("base").lower(), width, decimals


def _display_type(value: str) -> str:
    """把 Fiona 类型转换为面向业务人员的“字段设置”描述。"""
    base, width, decimals = _parse_type(value)
    if base == "str":
        length = str(width) if width is not None else "驱动未返回"
        return f"字段设置：Char，长度 {length}"
    if base in {"float", "float32", "float64"}:
        length = str(width) if width is not None else "驱动未返回"
        decimal_text = str(decimals) if decimals is not None else "驱动未返回"
        return f"字段设置：Double，长度 {length}，小数位 {decimal_text}"
    return f"字段设置：{value}"


def _type_ok(expected: FieldSpec, actual: str) -> tuple[bool, str, bool]:
    base, width, decimals = _parse_type(actual)
    expected_base = "str" if expected.kind == "Char" else "float"
    base_ok = base == expected_base or (expected_base == "float" and base in {"float32", "float64"})
    if not base_ok:
        if expected.kind == "Char":
            wanted = f"字段设置：Char，长度 {expected.length}"
        else:
            wanted = f"字段设置：Double，长度 {expected.length}，小数位 {expected.decimals}"
        return False, wanted, False
    if expected.kind == "Char":
        return width == expected.length, f"字段设置：Char，长度 {expected.length}", False
    # OpenFileGDB 通常只返回 float，不返回规范里的显示宽度。
    unavailable = width is None
    if unavailable:
        return (
            True,
            f"字段设置：Double，长度 {expected.length}，小数位 {expected.decimals}",
            True,
        )
    return (
        width == expected.length and decimals == expected.decimals,
        f"字段设置：Double，长度 {expected.length}，小数位 {expected.decimals}",
        False,
    )


def _check_schema(
    result: CheckResult,
    location: str,
    actual_fields: dict[str, str],
    specs: Iterable[FieldSpec],
    *,
    dynamic_metrics: bool,
) -> tuple[list[str], list[str]]:
    expected = {field.code: field for field in specs}
    limited_numeric = False
    for code, field in expected.items():
        actual_type = actual_fields.get(code)
        if actual_type is None:
            result.add(
                "ERROR",
                "FIELD_MISSING",
                location,
                f"缺少字段 {field.name}（{code}）",
                expected=f"{code} {field.kind} 长度{field.length}",
                actual="不存在",
            )
            continue
        ok, wanted, unavailable = _type_ok(field, actual_type)
        limited_numeric |= unavailable
        if not ok:
            result.add(
                "ERROR",
                "FIELD_DEFINITION",
                location,
                f"字段 {field.name}（{code}）的结构设置（类型、长度或小数位）不符合规范",
                expected=wanted,
                actual=_display_type(actual_type),
            )

    metric_fields: list[str] = []
    suffix_groups: dict[str, set[str]] = defaultdict(set)
    for code, actual_type in actual_fields.items():
        if code in expected or SYSTEM_FIELD_RE.match(code):
            continue
        metric = METRIC_RE.match(code) if dynamic_metrics else None
        if metric:
            metric_fields.append(code)
            suffix_groups[metric.group(2)].add(metric.group(1))
            wanted = FieldSpec(code, code, "Double", 10, 2)
            ok, expected_type, unavailable = _type_ok(wanted, actual_type)
            limited_numeric |= unavailable
            if not ok:
                result.add(
                    "ERROR",
                    "FIELD_DEFINITION",
                    location,
                    f"评价指标字段 {code} 的结构设置（类型、长度或小数位）不符合规范",
                    expected=expected_type,
                    actual=_display_type(actual_type),
                )
            continue
        result.add(
            "ERROR",
            "FIELD_EXTRA",
            location,
            f"存在规范表未说明的业务字段：{code}",
            expected="仅保留规范字段和 GIS 系统字段",
            actual=f"{code}；{_display_type(actual_type)}",
        )

    if dynamic_metrics:
        if not metric_fields:
            result.add(
                "ERROR",
                "METRIC_FIELDS_MISSING",
                location,
                "未找到 MA/LR/CR+3位分类代码的评价指标字段",
            )
        for suffix, prefixes in sorted(suffix_groups.items()):
            missing = {"MA", "LR", "CR"} - prefixes
            if missing:
                result.add(
                    "ERROR",
                    "METRIC_GROUP_INCOMPLETE",
                    location,
                    f"分类代码 {suffix} 的评价指标字段不完整",
                    expected=f"MA{suffix}、LR{suffix}、CR{suffix}",
                    actual="、".join(sorted(f"{p}{suffix}" for p in prefixes)),
                )

    if limited_numeric:
        result.add(
            "INFO",
            "GDB_NUMERIC_WIDTH_UNAVAILABLE",
            location,
            "数据驱动未返回 Double 字段显示长度；已检查字段代码和数值类型，小数位仅在驱动可提供时核验。",
        )
    required = [field.code for field in specs if field.required and field.code in actual_fields]
    return required, metric_fields


def check_vector(
    result: CheckResult,
    dataset: Path,
    specs: Iterable[FieldSpec],
    *,
    layer: str | None = None,
    schema_name: str,
    valid_counties: set[str],
    expected_county: str | None = None,
    dynamic_metrics: bool = False,
    sample_limit: int = 10,
) -> int:
    location = str(dataset) + (f" :: {layer}" if layer else "")
    try:
        source = fiona.open(dataset, layer=layer, ignore_geometry=True)
    except Exception as exc:
        result.add("ERROR", "VECTOR_OPEN", location, f"无法打开矢量属性表：{exc}")
        return 0

    with source:
        check_albers_crs(result, location, source.crs_wkt or source.crs)
        actual_fields = dict(source.schema.get("properties", {}))
        required, metrics = _check_schema(
            result, location, actual_fields, specs, dynamic_metrics=dynamic_metrics
        )
        null_counts = {code: 0 for code in required}
        null_samples: dict[str, list[str]] = {code: [] for code in required}
        metric_empty_count = 0
        metric_empty_samples: list[str] = []
        invalid_county_count = 0
        invalid_county_samples: list[str] = []
        mismatch_county_count = 0
        mismatch_county_samples: list[str] = []
        count = 0

        for feature in source:
            count += 1
            fid = str(feature.id)
            props = feature["properties"]
            for code in required:
                if _empty(props.get(code)):
                    null_counts[code] += 1
                    if len(null_samples[code]) < sample_limit:
                        null_samples[code].append(fid)
            if dynamic_metrics and metrics and all(_empty(props.get(code)) for code in metrics):
                metric_empty_count += 1
                if len(metric_empty_samples) < sample_limit:
                    metric_empty_samples.append(fid)
            if "QXDM" in actual_fields and not _empty(props.get("QXDM")):
                raw_county = str(props.get("QXDM")).strip()
                county = raw_county[:6]
                if county not in valid_counties:
                    invalid_county_count += 1
                    if len(invalid_county_samples) < sample_limit:
                        invalid_county_samples.append(f"{fid}:{raw_county}")
                elif expected_county and county != expected_county:
                    mismatch_county_count += 1
                    if len(mismatch_county_samples) < sample_limit:
                        mismatch_county_samples.append(f"{fid}:{raw_county}")

        result.checked_vectors += 1
        result.checked_records += count
        field_names = {field.code: field.name for field in specs}
        for code, empty_count in null_counts.items():
            if empty_count:
                result.add(
                    "ERROR",
                    "REQUIRED_VALUE_EMPTY",
                    location,
                    f"非空字段 {field_names.get(code, code)}（{code}）存在空值：{empty_count}/{count}",
                    expected="每条记录均有值",
                    actual=f"{empty_count} 条为空",
                    details={"sample_feature_ids": null_samples[code]},
                )
        if metric_empty_count:
            result.add(
                "ERROR",
                "METRIC_VALUES_EMPTY",
                location,
                f"面积精度、漏检率、错检率全部为空：{metric_empty_count}/{count}",
                expected="每条记录至少一个 MA/LR/CR 指标有值",
                actual=f"{metric_empty_count} 条全部为空",
                details={"sample_feature_ids": metric_empty_samples},
            )
        if invalid_county_count:
            result.add(
                "ERROR",
                "COUNTY_VALUE_INVALID",
                location,
                f"QXDM 中有 {invalid_county_count} 条记录不是县界中的合法县代码",
                details={"sample_feature_ids_and_values": invalid_county_samples},
            )
        if mismatch_county_count:
            result.add(
                "ERROR",
                "COUNTY_VALUE_MISMATCH",
                location,
                f"QXDM 中有 {mismatch_county_count} 条记录与所在县目录/图层代码 {expected_county} 不一致",
                details={"sample_feature_ids_and_values": mismatch_county_samples},
            )
        if count == 0:
            result.add("WARNING", "VECTOR_EMPTY", location, f"按表 {schema_name} 检查的属性表没有记录")
        return count


def check_vector_crs(
    result: CheckResult,
    dataset: Path,
    *,
    layer: str | None = None,
) -> None:
    location = str(dataset) + (f" :: {layer}" if layer else "")
    try:
        with fiona.open(dataset, layer=layer, ignore_geometry=True) as source:
            check_albers_crs(result, location, source.crs_wkt or source.crs)
    except Exception as exc:
        result.add("ERROR", "VECTOR_OPEN", location, f"无法读取文件坐标系：{exc}")
