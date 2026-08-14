from __future__ import annotations

import argparse
from datetime import datetime
from pathlib import Path

from checker.report import write_json, write_pdf
from checker.scanner import check_delivery


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="遥感测量数据成果组织与属性表规范检查器")
    parser.add_argument("root", type=Path, help="待检成果根目录，例如 G:\\EL_150000_2026")
    parser.add_argument(
        "--county-boundary",
        type=Path,
        default=Path("00县边界") / "15_县边界.shp",
        help="用于读取合法县代码的县界 Shapefile",
    )
    parser.add_argument("--province-code", default="150000", help="6 位省代码，默认 150000")
    parser.add_argument("--gdb-schema", choices=["5-1", "6-1"], default="5-1")
    parser.add_argument("--zpj-schema", choices=["5-4", "6-3"], default="5-4")
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("output") / "pdf" / "遥感测量数据成果检查报告.pdf",
        help="PDF 报告路径",
    )
    parser.add_argument(
        "--json-output",
        type=Path,
        default=Path("output") / "遥感测量数据成果检查明细.json",
        help="JSON 明细路径",
    )
    return parser


def main() -> int:
    args = build_parser().parse_args()
    result = check_delivery(
        args.root,
        args.county_boundary,
        province_code=args.province_code,
        gdb_schema=args.gdb_schema,
        zpj_schema=args.zpj_schema,
    )
    write_json(result, args.json_output)
    pdf_output = args.output
    try:
        write_pdf(result, pdf_output)
    except PermissionError:
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        pdf_output = args.output.with_name(f"{args.output.stem}_{timestamp}{args.output.suffix}")
        write_pdf(result, pdf_output)
        print(f"原 PDF 正在被占用，已自动另存为：{pdf_output.resolve()}")
    status = "通过" if result.passed else "不通过"
    print(f"检查完成：{status}")
    print(f"PDF：{pdf_output.resolve()}")
    print(f"JSON：{args.json_output.resolve()}")
    return 0 if result.passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
