from __future__ import annotations

from pathlib import Path

import fiona


def load_counties(boundary: Path) -> dict[str, str]:
    """从县界 area_code 前六位读取县代码，并保留县名。"""
    counties: dict[str, str] = {}
    with fiona.open(boundary) as source:
        fields = source.schema["properties"]
        if "area_code" not in fields:
            raise ValueError(f"县界缺少 area_code 字段：{boundary}")
        for feature in source:
            raw = str(feature["properties"].get("area_code") or "").strip()
            if len(raw) < 6 or not raw[:6].isdigit():
                continue
            code = raw[:6]
            name = str(feature["properties"].get("area_name") or "").strip()
            counties.setdefault(code, name)
    if not counties:
        raise ValueError(f"未从县界读取到有效县代码：{boundary}")
    return counties
