#!/usr/bin/env python
"""使用 SQLite 配准模型处理人工修改的同名分幅 SHP。"""

from apply_sqlite_alignment_to_shp import main


if __name__ == "__main__":
    main(source_kind="person")
