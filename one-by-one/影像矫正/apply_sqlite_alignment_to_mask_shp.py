#!/usr/bin/env python
"""使用 SQLite 配准模型矫正并合成从 5 万分幅索引提取的掩膜 SHP。"""

from apply_sqlite_alignment_to_shp import main


if __name__ == "__main__":
    main(source_kind="mask")
