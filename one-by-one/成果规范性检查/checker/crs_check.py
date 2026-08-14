from __future__ import annotations

from typing import Any

from pyproj import CRS

from .models import CheckResult


def _as_crs(value: Any) -> CRS | None:
    if value is None:
        return None
    try:
        if not value:
            return None
    except (TypeError, ValueError):
        pass
    try:
        return CRS.from_user_input(value)
    except Exception:
        return None


def is_albers(value: Any) -> bool:
    crs = _as_crs(value)
    if crs is None:
        return False
    operation = crs.coordinate_operation
    method = operation.method_name if operation else ""
    text = " ".join((crs.name or "", method or "", crs.to_wkt() or "")).upper()
    return "ALBERS" in text


def describe_crs(value: Any) -> str:
    crs = _as_crs(value)
    if crs is None:
        return "未定义或无法识别"
    parts = [crs.name or "未命名坐标系"]
    operation = crs.coordinate_operation
    if operation and operation.method_name:
        parts.append(f"投影方法：{operation.method_name}")
    authority = crs.to_authority()
    if authority:
        parts.append(f"{authority[0]}:{authority[1]}")
    return "；".join(parts)


def check_albers_crs(result: CheckResult, location: object, value: Any) -> None:
    crs = _as_crs(value)
    if crs is None:
        result.add(
            "ERROR",
            "CRS_MISSING",
            location,
            "文件缺少坐标系，或坐标系无法识别",
            expected="Albers 等积圆锥投影",
            actual="未定义或无法识别",
        )
        return
    if not is_albers(crs):
        result.add(
            "ERROR",
            "CRS_NOT_ALBERS",
            location,
            "文件坐标系不是 Albers 投影",
            expected="Albers 等积圆锥投影",
            actual=describe_crs(crs),
        )
