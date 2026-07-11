import argparse
from pathlib import Path


FIELD_MAPPING = {
    "YZCHMC": ["养殖场户名称", "养殖场（户）名称", "养殖场名称"],
    "YZCHBM": ["养殖场户编码", "养殖场（户）编码", "养殖场编码"],
    "FCBM": ["分厂编码", "分厂"],
    "QXMC": ["县名称", "县"],
    "XZMC": ["乡镇名称", "镇名称", "镇"],
    "CUNMC": ["村名称", "村"],
    "CUNDM": ["村代码"],
    "WZMS": ["简要文字描述", "简要文字"],
    "SFPS": ["是否棚舍养殖为主", "棚舍"],
    "YZYFMJ": ["养殖用房面积（平方米，整数）", "养殖用房面积", "面积"],
}

TARGET_COLUMNS = [
    "YZCHMC",
    "YZCHBM",
    "FCBM",
    "QXMC",
    "XZMC",
    "CUNMC",
    "CUNDM",
    "LON",
    "LAT",
    "WZMS",
    "SFPS",
    "YZYFMJ",
    "geometry",
]

ATTRIBUTE_COLUMNS = [col for col in TARGET_COLUMNS if col != "geometry"]

OUTPUT_SCHEMA_PROPERTIES = {
    "YZCHMC": "str:100",
    "YZCHBM": "str:17",
    "FCBM": "str:4",
    "QXMC": "str:30",
    "XZMC": "str:30",
    "CUNMC": "str:100",
    "CUNDM": "str:12",
    "LON": "str:30",
    "LAT": "str:30",
    "WZMS": "str:254",
    "SFPS": "str:4",
    "YZYFMJ": "str:20",
}


def parse_args():
    parser = argparse.ArgumentParser(
        description="将畜禽养殖场 Excel 属性表按序号连接到 Shapefile，并输出标准字段 Shapefile。"
    )
    parser.add_argument("--shp", required=True, help="输入 Shapefile 路径。")
    parser.add_argument("--excel", required=True, help="输入 Excel 路径，字段按字符串读取。")
    parser.add_argument("--out-shp", required=True, help="输出 Shapefile 路径。")
    parser.add_argument(
        "--shp-id-field",
        default="序号",
        help="输入 Shapefile 中用于连接 Excel 序号的字段名，默认“序号”。",
    )
    parser.add_argument(
        "--encoding",
        default="utf-8",
        help="输出 Shapefile 属性编码，默认 utf-8。",
    )
    parser.add_argument(
        "--join-type",
        choices=["inner", "left"],
        default="inner",
        help="连接方式：inner只保留匹配图斑；left保留全部shp图斑。默认inner。",
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="输出字段匹配和序号匹配诊断信息。",
    )
    return parser.parse_args()


def require_existing_file(path, label):
    if not path.is_file():
        raise FileNotFoundError(f"{label}不存在或不是文件: {path}")


def normalize_header_text(value):
    import re

    text = "" if value is None else str(value)
    text = text.strip()
    text = text.replace("\u00A0", "")
    text = text.replace("（", "(").replace("）", ")")
    text = re.sub(r"\s+", "", text)
    return text


def get_col_name(df, keywords):
    if isinstance(keywords, str):
        keywords = [keywords]

    normalized_columns = {
        col: normalize_header_text(col)
        for col in df.columns
    }

    normalized_keywords = [normalize_header_text(k) for k in keywords]

    # 先精确匹配
    for keyword in normalized_keywords:
        for col, normalized_col in normalized_columns.items():
            if normalized_col == keyword:
                return col

    # 再包含匹配
    for keyword in normalized_keywords:
        for col, normalized_col in normalized_columns.items():
            if keyword and keyword in normalized_col:
                return col

    return None


def is_blank_cell(value):
    text = "" if value is None else str(value).strip()
    return text == "" or text.lower() in {"nan", "none"}


def make_unique_name(name, existing_names):
    base_name = str(name).strip() if str(name).strip() else "未命名字段"
    if base_name not in existing_names:
        return base_name

    index = 2
    while f"{base_name}_{index}" in existing_names:
        index += 1
    return f"{base_name}_{index}"


def normalize_excel_table_format(df):
    """
    将不同格式的表头归一成普通一行字段表。

    1509 表里同时存在两组“经度/纬度”列，pandas 遇到重复列名时
    df["经度"] 会返回多列 DataFrame。这里按标准字段名合并重复列：
    保留第一个非空值，并把无法识别或空列名改成唯一字段名。
    """
    import pandas as pd

    if df.empty:
        return df

    normalized_df = pd.DataFrame(index=df.index)
    key_to_output_col = {}

    for idx, original_col in enumerate(df.columns):
        col_name = str(original_col).strip()
        col_key = normalize_header_text(col_name)
        series = df.iloc[:, idx]

        is_unnamed = col_key == "" or col_key.startswith("未命名字段") or col_key.startswith("unnamed:")
        if is_unnamed:
            output_col = make_unique_name(col_name or f"未命名字段_{idx}", normalized_df.columns)
            normalized_df[output_col] = series
            continue

        if col_key in key_to_output_col:
            output_col = key_to_output_col[col_key]
            existing = normalized_df[output_col]
            existing_blank = existing.map(is_blank_cell)
            normalized_df.loc[existing_blank, output_col] = series.loc[existing_blank]
            continue

        output_col = make_unique_name(col_name, normalized_df.columns)
        key_to_output_col[col_key] = output_col
        normalized_df[output_col] = series

    normalized_df = normalized_df.dropna(how="all").dropna(axis=1, how="all")
    normalized_df.columns = [str(c).strip() for c in normalized_df.columns]

    return normalized_df


def read_excel_with_auto_header(excel_path):
    """
    兼容两类Excel：
    1. 第一行就是字段名；
    2. 第一行是主表头，第二行补充“经度/维度”等子表头。
    如果第一张 sheet 为空，会自动扫描后续 sheet。
    """
    import pandas as pd

    excel_file = pd.ExcelFile(excel_path)
    raw = None
    selected_sheet = None

    for sheet_name in excel_file.sheet_names:
        candidate = pd.read_excel(
            excel_path,
            sheet_name=sheet_name,
            header=None,
            dtype=str,
        )
        candidate = candidate.dropna(how="all").dropna(axis=1, how="all")
        if not candidate.empty:
            raw = candidate.reset_index(drop=True)
            selected_sheet = sheet_name
            break

    if raw is None or raw.empty:
        raise ValueError(
            f"Excel 文件没有可处理的数据，所有 sheet 都为空: {excel_file.sheet_names}"
        )

    header_row = None
    for i in range(min(10, len(raw))):
        row_values = raw.iloc[i].fillna("").astype(str).tolist()
        row_text = "".join(row_values)
        if "序号" in row_text and ("养殖" in row_text or "场" in row_text):
            header_row = i
            break

    if header_row is None:
        # 兜底：按普通Excel读取同一个非空sheet
        df = pd.read_excel(excel_path, sheet_name=selected_sheet, dtype=str)
        df = df.dropna(how="all").dropna(axis=1, how="all")
        df.columns = [str(c).strip() for c in df.columns]
        return normalize_excel_table_format(df)

    header1 = raw.iloc[header_row].fillna("").astype(str).tolist()

    has_second_header = False
    header2 = [""] * len(header1)
    if header_row + 1 < len(raw):
        possible_header2 = raw.iloc[header_row + 1].fillna("").astype(str).tolist()
        possible_text = "".join(possible_header2)
        if ("经度" in possible_text) or ("纬度" in possible_text) or ("维度" in possible_text):
            has_second_header = True
            header2 = possible_header2

    columns = []
    for idx, (h1, h2) in enumerate(zip(header1, header2)):
        h1 = str(h1).strip()
        h2 = str(h2).strip()

        if h1 and h1.lower() != "nan":
            columns.append(h1)
        elif h2 and h2.lower() != "nan":
            columns.append(h2)
        else:
            columns.append(f"未命名字段_{idx}")

    start_row = header_row + 2 if has_second_header else header_row + 1
    df = raw.iloc[start_row:].copy()
    df.columns = columns
    df = df.reset_index(drop=True)

    # 删除全空行
    df = df.dropna(how="all")

    # 清理列名
    df.columns = [str(c).strip() for c in df.columns]

    return normalize_excel_table_format(df)


def normalize_join_id(series):
    """
    保留你原来的“数字序号”逻辑：
    1、1.0、' 1 ' 都统一成 '1'
    """
    import pandas as pd

    numeric = pd.to_numeric(series, errors="coerce")
    result = series.astype(str).str.strip()
    valid_numeric = numeric.notna()
    result.loc[valid_numeric] = numeric.loc[valid_numeric].astype(int).astype(str)
    result = result.str.replace("\u00A0", "", regex=False)
    result = result.str.replace(r"\s+", "", regex=True)
    return result


def build_extract_data(df, debug=False):
    import pandas as pd

    if df.empty:
        raise ValueError("Excel 文件没有可处理的数据。")

    id_col = get_col_name(df, ["序号"])
    if id_col is None:
        id_col = df.columns[0]

    temp_numeric = pd.to_numeric(df[id_col], errors="coerce")
    valid_rows = temp_numeric.notna()
    df = df[valid_rows].copy()

    if df.empty:
        raise ValueError("Excel 未找到有效序号。")

    df["序号"] = temp_numeric[valid_rows].astype(int).astype(str)

    extract_data = pd.DataFrame()
    extract_data["序号"] = df["序号"]

    if debug:
        print("\n==== Excel 字段识别 ====")
        print(f"序号列 -> {id_col}")

    for target_field, keywords in FIELD_MAPPING.items():
        matched_col = get_col_name(df, keywords)
        extract_data[target_field] = df[matched_col] if matched_col else ""

        if debug:
            print(f"{target_field} {keywords} -> {matched_col}")

    lon_col = get_col_name(df, ["经度", "longitude", "lon", "LON"])
    lat_col = get_col_name(df, ["纬度", "维度", "latitude", "lat", "LAT"])

    extract_data["LON"] = df[lon_col] if lon_col else ""
    extract_data["LAT"] = df[lat_col] if lat_col else ""

    if debug:
        print(f"LON -> {lon_col}")
        print(f"LAT -> {lat_col}")

    # 如果Excel里没有村代码，则从养殖场户编码前12位推导
    if "CUNDM" not in extract_data.columns or extract_data["CUNDM"].replace("", None).isna().all():
        extract_data["CUNDM"] = extract_data["YZCHBM"].astype(str).str[:12]

    for col in extract_data.columns:
        if col != "序号":
            extract_data[col] = extract_data[col].fillna("").astype(str).str.strip()
            extract_data[col] = extract_data[col].replace("nan", "")

    fcbm_mask = extract_data["FCBM"] != ""
    extract_data.loc[fcbm_mask, "FCBM"] = extract_data.loc[fcbm_mask, "FCBM"].str.zfill(4)

    # Shapefile 字段长度限制，提前截断，避免写出时产生警告
    for col, schema_type in OUTPUT_SCHEMA_PROPERTIES.items():
        if col in extract_data.columns and schema_type.startswith("str:"):
            max_len = int(schema_type.split(":")[1])
            extract_data[col] = extract_data[col].astype(str).str[:max_len]

    return extract_data


def drop_empty_attribute_features(gdf):
    existing_attribute_cols = [col for col in ATTRIBUTE_COLUMNS if col in gdf.columns]
    if not existing_attribute_cols:
        return gdf, 0

    normalized_attrs = gdf[existing_attribute_cols].fillna("").astype(str).apply(
        lambda col: col.str.strip().str.lower()
    )
    empty_mask = normalized_attrs.isin(["", "nan", "none"]).all(axis=1)
    dropped_count = int(empty_mask.sum())

    return gdf.loc[~empty_mask].copy(), dropped_count


def remove_existing_shapefile(out_shp_path):
    """
    防止旧shp残留字段影响新结果。
    """
    suffixes = [".shp", ".shx", ".dbf", ".prj", ".cpg", ".qpj"]
    for suffix in suffixes:
        p = out_shp_path.with_suffix(suffix)
        if p.exists():
            p.unlink()


def resolve_shp_field(columns, requested_field):
    if requested_field in columns:
        return requested_field

    requested_lower = str(requested_field).lower()
    for column in columns:
        if str(column).lower() == requested_lower:
            return column

    return None

def update_shapefile(
    shp_path,
    excel_path,
    out_shp_path,
    encoding,
    shp_id_field,
    join_type,
    debug=False,
):
    import geopandas as gpd

    require_existing_file(shp_path, "输入 Shapefile")
    require_existing_file(excel_path, "输入 Excel")

    gdf = gpd.read_file(shp_path)
    if gdf.empty:
        raise ValueError("输入 Shapefile 没有可处理的要素。")

    actual_shp_id_field = resolve_shp_field(gdf.columns, shp_id_field)
    if actual_shp_id_field is None:
        raise KeyError(
            f"输入 Shapefile 缺少“{shp_id_field}”字段，无法连接 Excel 属性。"
            f"现有字段: {list(gdf.columns)}"
        )

    if debug and actual_shp_id_field != shp_id_field:
        print(f"Shapefile连接字段 {shp_id_field} -> {actual_shp_id_field}")

    df = read_excel_with_auto_header(excel_path)
    extract_data = build_extract_data(df, debug=debug)

    gdf["序号"] = normalize_join_id(gdf[actual_shp_id_field])

    if debug:
        shp_keys = set(gdf["序号"])
        excel_keys = set(extract_data["序号"])
        inter = shp_keys & excel_keys

        print("\n==== 序号匹配诊断 ====")
        print(f"SHP要素数: {len(gdf)}")
        print(f"SHP唯一序号数: {len(shp_keys)}")
        print(f"Excel记录数: {len(extract_data)}")
        print(f"Excel唯一序号数: {len(excel_keys)}")
        print(f"交集唯一序号数: {len(inter)}")
        print(f"SHP序号样例: {list(gdf['序号'].head(10))}")
        print(f"Excel序号样例: {list(extract_data['序号'].head(10))}")

    # ======================================================
    # 核心修复：
    # merge前删除shp里已有的标准属性字段，避免生成 _x/_y，
    # 确保最终 TARGET_COLUMNS 取到的是 Excel 导入的新字段。
    # ======================================================
    drop_cols_from_shp = [col for col in ATTRIBUTE_COLUMNS if col in gdf.columns]
    gdf_for_merge = gdf.drop(columns=drop_cols_from_shp)

    merged_gdf = gdf_for_merge.merge(
        extract_data,
        on="序号",
        how=join_type,
    )

    final_gdf = merged_gdf[[col for col in TARGET_COLUMNS if col in merged_gdf.columns]].copy()

    if join_type == "left":
        final_gdf, dropped_count = drop_empty_attribute_features(final_gdf)
    else:
        dropped_count = 0

    if final_gdf.empty:
        raise ValueError("输出结果为空，请检查 shp_id_field 与 Excel 序号是否匹配。")

    geom_types = final_gdf.geometry.geom_type.dropna()
    geom_type = geom_types.iloc[0] if not geom_types.empty else "Polygon"

    output_schema = {
        "geometry": geom_type,
        "properties": OUTPUT_SCHEMA_PROPERTIES,
    }

    out_shp_path.parent.mkdir(parents=True, exist_ok=True)
    remove_existing_shapefile(out_shp_path)

    final_gdf.to_file(
        out_shp_path,
        driver="ESRI Shapefile",
        schema=output_schema,
        encoding=encoding,
        engine="fiona",
    )

    return len(final_gdf), dropped_count


def main():
    args = parse_args()
    shp_path = Path(args.shp)
    excel_path = Path(args.excel)
    out_shp_path = Path(args.out_shp)

    row_count, dropped_count = update_shapefile(
        shp_path,
        excel_path,
        out_shp_path,
        args.encoding,
        args.shp_id_field,
        args.join_type,
        args.debug,
    )

    print(
        f"数据处理完成，删除空信息图斑 {dropped_count} 个，"
        f"输出 {row_count} 个要素: {out_shp_path}"
    )


if __name__ == "__main__":
    main()

