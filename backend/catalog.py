"""前端表单目录。

目录只描述界面需要的字段；真正的命令拼装与业务调用位于 adapters.py。
"""

from __future__ import annotations

from typing import Any


def field(name: str, label: str, kind: str = "text", **values: Any) -> dict[str, Any]:
    return {"name": name, "label": label, "type": kind, **values}


PATH = {"placeholder": "可输入 Linux 路径或 Windows 网络路径", "wide": True}


TOOLS: list[dict[str, Any]] = [
    {
        "id": "county-clip-shp", "name": "县级 SHP 裁剪", "folder": "县裁剪-shp",
        "category": "裁剪与转换", "accent": "emerald", "featured": True,
        "description": "按县界批量裁剪矢量成果，维护空间索引并实时查看处理进度。",
        "fields": [
            field(
                "shp_dir", "输入 SHP 根目录",
                default=r"\\10.10.10.11\data\专题2_农作物种植用地遥感测量\加密0711_乌兰察布-道_已全部完成并解密\完成成果解密",
                required=True, **PATH,
            ),
            field("boundary", "县界文件或目录", default="县裁剪-shp/00县边界", required=True, **PATH),
            field("output_dir", "输出目录", default="县裁剪-shp/县级SHP裁剪结果", required=True, **PATH),
            field("index", "空间索引", default="县裁剪-shp/shapefile_index.sqlite", **PATH),
            field("index_mode", "索引模式", "select", default="auto", options=[["auto", "自动增量"], ["rebuild", "完整重建"], ["skip", "跳过扫描"]]),
            field("workers", "县级并发", "number", default=4, min=1, max=64),
            field("index_workers", "索引并发", "number", default=4, min=1, max=64),
            field("cpu_percent", "CPU 使用上限 (%)", "number", default=15, min=1, max=100),
            field("county", "指定县代码", placeholder="可留空；多个代码用逗号分隔"),
            field("overwrite", "覆盖已有结果", "checkbox", default=False),
        ],
    },
    {
        "id": "county-clip-tif", "name": "县级影像裁剪", "folder": "县裁剪-tif",
        "category": "裁剪与转换", "accent": "cyan", "featured": True,
        "description": "按县界拼接、裁剪影像，控制 GDAL 内存、并发和金字塔。",
        "fields": [
            field("imagery_dir", "影像根目录", required=True, **PATH),
            field("boundary", "县界文件或目录", default="县裁剪-tif/00县边界", required=True, **PATH),
            field("output_dir", "输出目录", default="县裁剪-tif/输出_0.5m", required=True, **PATH),
            field("date1", "开始日期", "date", default="2025-01-01", required=True),
            field("date2", "结束日期", "date", default="2025-12-31", required=True),
            field("resolution", "分辨率标记", default="0.5m", required=True),
            field("name_template", "文件名模板", default="ELDOM{code}_{date1}_{date2}_{resolution}.tif", wide=True),
            field("index", "影像索引", default="县裁剪-tif/0.5m影像索引.sqlite", **PATH),
            field("index_mode", "索引模式", "select", default="auto", options=[["auto", "自动增量"], ["rebuild", "完整重建"], ["skip", "跳过扫描"]]),
            field("workers", "县级并发", "number", default=4, min=1, max=32),
            field("index_workers", "索引并发", "number", default=4, min=1, max=32, help="并行读取新增影像元数据。"),
            field("cpu_percent", "CPU 使用上限 (%)", "number", default=75, min=1, max=100),
            field("gdal_memory_gb", "GDAL 内存 (GB)", "number", default=8, min=1, max=256),
            field("pixel_size", "输出像元大小", "number", min=0.000001, help="留空保持 VRT 原始分辨率。"),
            field("resampling", "重采样", "select", default="near", options=[["near", "最近邻"], ["bilinear", "双线性"], ["cubic", "三次卷积"], ["cubicspline", "三次样条"], ["lanczos", "Lanczos"], ["average", "平均值"], ["mode", "众数"]]),
            field("overview_max_factor", "金字塔最高倍率", "select", default="256", options=[["32", "32"], ["64", "64"], ["128", "128"], ["256", "256"]]),
            field("county", "指定县代码", placeholder="可留空；多个代码用逗号分隔"),
            field("overwrite", "覆盖已有结果", "checkbox", default=False),
        ],
    },
]


def self_check(tool_id: str, name: str, folder: str, description: str) -> dict[str, Any]:
    sample_count = 3 if tool_id == "greenhouse-check" else 100
    square_size = 2000 if tool_id == "greenhouse-check" else 50
    return {
        "id": tool_id, "name": name, "folder": folder, "category": "质量自检", "accent": "lime",
        "description": description,
        "fields": [
            field("stage", "运行阶段", "select", default="prepare", options=[["prepare", "01–02 生成样本与参考真值"], ["evaluate", "03–04 生成测量值与精度评价"]], wide=True),
            field("source_root", "分类成果根目录", required=True, **PATH),
            field("work_root", "自检工作目录", required=True, **PATH),
            field("mode", "已有结果处理", "select", default="skip", options=[["skip", "跳过已有"], ["overwrite", "覆盖重算"]]),
            field("sample_count", "每县样方数", "number", default=sample_count, min=1, visibleWhen=["stage", "prepare"]),
            field("min_distance", "样方最小间距（米）", "number", default=100, min=0, visibleWhen=["stage", "prepare"]),
            field("square_size", "样方边长（米）", "number", default=square_size, min=0.01, visibleWhen=["stage", "prepare"]),
            field("min_overlap_ratio", "最小重叠比例", "number", default=0.2, min=0, max=1, step=0.01, visibleWhen=["stage", "prepare"]),
        ],
    }


TOOLS += [
    self_check("farmland-check", "耕地自检", "耕地自检", "生成抽样网格、参考真值与测量值，并计算县级精度指标。"),
    self_check("wheat-check", "春小麦自检", "春小麦自检", "面向春小麦成果的两阶段自动抽样与精度评价。"),
    self_check("greenhouse-check", "大棚自检", "大棚自检", "生成大棚检验网格，完成真值裁剪与精度汇总。"),
    self_check("multicrop-check", "多作物自检", "多作物自检", "按作物代码生成样本并汇总多作物县级精度。"),
    {
        "id": "delivery-check", "name": "成果规范性检查", "folder": "成果规范性检查", "category": "质量自检", "accent": "amber",
        "description": "检查交付目录、命名、坐标系与矢量规范，输出 PDF 与 JSON 报告。",
        "fields": [
            field("root", "待检成果根目录", required=True, **PATH),
            field("output_dir", "报告输出目录", required=True, **PATH),
            field("province_code", "省级代码", default="150000", required=True),
            field("gdb_schema", "GDB 方案", "select", default="5-1", options=[["5-1", "表 5-1"], ["6-1", "表 6-1"]]),
            field("zpj_schema", "ELJDZPJ 方案", "select", default="5-4", options=[["5-4", "表 5-4"], ["6-3", "表 6-3"]]),
        ],
    },
    {
        "id": "topology", "name": "拓扑检查", "folder": "拓扑检查", "category": "矢量处理", "accent": "rose",
        "description": "检查无效几何和面重叠，并将相交区域保留给面积最大的要素。",
        "fields": [
            field("source", "输入 Shapefile 或目录", required=True, **PATH),
            field("output_dir", "输出目录", default="拓扑检查/output", required=True, **PATH),
            field("concurrency", "并发数", "number", default=4, min=1, max=32),
        ],
    },
    {
        "id": "shp-overlap", "name": "SHP 重叠与亩数检查", "folder": "shp-重叠-亩数", "category": "矢量处理", "accent": "rose",
        "description": "检查图斑重叠与碎小要素，输出修正结果和 CSV 明细。",
        "fields": [
            field("input", "输入 SHP 或目录", required=True, **PATH), field("output_dir", "输出目录", default="shp-重叠-亩数/CSV", required=True, **PATH),
            field("min_area_mu", "最小图斑面积（亩）", "number", default=0.1, min=0, step=0.01),
            field("min_overlap_sqm", "最小重叠面积（平方米）", "number", default=0.00666667, min=0, step=0.000001, help="用于排除浮点误差，默认约 1e-5 亩。"),
            field("id_field", "标识字段", placeholder="留空自动生成"),
            field("merge_small", "自动合并碎小图斑", "checkbox", default=False, help="先把小于阈值的图斑合并到相邻的大图斑，再生成检查 CSV。"),
            field("merge_output_dir", "合并后 SHP 输出目录", default="shp-重叠-亩数/合并结果", visibleWhen=["merge_small", True], requiredWhen=["merge_small", True], **PATH),
            field("overwrite", "覆盖已有结果", "checkbox", default=False),
        ],
    },
    {
        "id": "shp-compare", "name": "两期 SHP 统计对比", "folder": "两shp-统计-亩数-图斑数", "category": "统计分析", "accent": "blue",
        "description": "按文件名匹配两期成果，对比面积、亩数与图斑数量并导出 CSV。",
        "fields": [field("folder_a", "模型/基准文件夹", default=r"\\10.10.10.11\data\北京预测结果传递\地块结果\所有地块结果最新-去除接边", required=True, **PATH), field("folder_b", "待统计文件夹", required=True, **PATH), field("output_path", "CSV 输出路径", required=True, **PATH), field("workers", "并发数", "number", default=4, min=1, max=32)],
    },
    {
        "id": "county-crop", "name": "县级作物抽取", "folder": "县作物抽取", "category": "裁剪与转换", "accent": "emerald",
        "description": "把作物成果按县界拆分并按作物类型组织输出。",
        "fields": [
            field("input_path", "输入 Shapefile", required=True, **PATH), field("output_root", "输出根目录", required=True, **PATH),
            field("crop_field", "作物代码字段", default="class", required=True), field("crop_names", "作物代码映射", "textarea", default="1:玉米\n2:小麦\n3:水稻", wide=True),
            field("concurrency", "空间相交并发数", "number", default=5, min=1, max=64), field("overwrite", "覆盖已有结果", "checkbox", default=False),
        ],
    },
    {
        "id": "livestock", "name": "畜牧属性关联", "folder": "畜牧", "category": "数据整理", "accent": "orange",
        "description": "读取 Excel 表格，按序号关联并更新畜牧 Shapefile 属性。",
        "fields": [field("shp", "输入 Shapefile", required=True, **PATH), field("excel", "输入 Excel", required=True, **PATH), field("out_shp", "输出 Shapefile", required=True, **PATH), field("shp_id_field", "SHP 序号字段", default="序号", required=True)],
    },
    {
        "id": "livestock2", "name": "畜牧属性关联 2", "folder": "畜牧2", "category": "数据整理", "accent": "orange",
        "description": "支持合并单元格和 FCBM 标准化的增强畜牧属性关联。",
        "fields": [field("shp", "输入 Shapefile", required=True, **PATH), field("excel", "输入 Excel", required=True, **PATH), field("out_shp", "输出 Shapefile", required=True, **PATH), field("shp_id_field", "SHP 序号字段", default="序号", required=True)],
    },
    {
        "id": "vote", "name": "分类投票", "folder": "投票", "category": "栅格分析", "accent": "violet",
        "description": "按市或县拆分分类投票，自动合并、行政区裁边并输出独立 Shapefile。",
        "fields": [
            field("operation", "运行操作", "select", default="run", options=[["run", "按市/县投票并合并"], ["refresh_index", "仅刷新 SHP 索引"]], wide=True),
            field("shp_dir", "输入 SHP 根目录", default=r"\\10.10.10.11\data\北京预测结果传递\地块结果\所有地块结果最新-去除接边", required=True, help="首次运行会扫描并缓存；需要时可选择“仅刷新 SHP 索引”。", **PATH),
            field("cls_tif", "输入分类 TIF 或目录", requiredWhen=["operation", "run"], visibleWhen=["operation", "run"], help="单个 TIF 直接处理；目录会递归处理其中全部 TIF，并按行政区统一合并。", **PATH),
            field("out_dir", "最终输出文件夹", requiredWhen=["operation", "run"], visibleWhen=["operation", "run"], help="每个行政区生成一个“代码_名称.shp”。", **PATH),
            field("region_name", "市/县名称", placeholder="留空则处理 TIF 范围内全部县；多个名称用；分隔", visibleWhen=["operation", "run"], wide=True),
            field("background_threshold", "最大背景像元比例", "number", default=0.5, min=0, max=1, step=0.01, visibleWhen=["operation", "run"]),
            field("min_class_area_mu", "分类面积保留阈值（亩）", "number", default=999999999, min=0, step=0.01, visibleWhen=["operation", "run"]),
            field("index_concurrency_count", "索引并发数", "number", default=4, min=1, max=96, help="同时建立索引的 SHP 数。"),
            field("precheck_concurrency_count", "相交检查并发数", "number", default=8, min=1, max=96, visibleWhen=["operation", "run"], help="同时检查范围相交的 SHP 数。"),
            field("vote_concurrency_count", "投票并发数", "number", default=4, min=1, max=96, visibleWhen=["operation", "run"], help="区域任务和单区域投票任务共用的总并发预算。"),
            field("multi_class", "启用多分类", "checkbox", default=False, visibleWhen=["operation", "run"]),
            field("class_mapping", "多分类映射", "textarea", default="1=春玉米\n2=中稻\n3=大豆\n4=春小麦\n5=马铃薯\n6=油菜\n7=向日葵籽\n0=背景或无有效分类", visibleWhen=[["operation", "run"], ["multi_class", True]], requiredWhen=["multi_class", True], help="每行填写：栅格值=类别名称。", wide=True),
        ],
    },
    {
        "id": "shp-to-tif", "name": "SHP 转 TIF", "folder": "shp_to_tif", "category": "裁剪与转换", "accent": "cyan",
        "description": "合并、清洗矢量并栅格化为 TIF，同时生成处理报告。",
        "fields": [
            field("input_dir", "输入 SHP 目录", required=True, **PATH), field("output_dir", "输出目录", required=True, **PATH),
            field("shp_name", "合并 SHP 文件名", default="rice_10m_result_0809.shp"), field("tif_name", "输出 TIF 文件名", default=""),
            field("class_value", "分类值", "number", default=2), field("shp_threads", "SHP 处理线程", "number", default=10, min=1, max=64),
            field("overwrite", "覆盖已有结果", "checkbox", default=False),
        ],
    },
    {
        "id": "pamid", "name": "影像金字塔构建", "folder": "pamid", "category": "栅格分析", "accent": "violet",
        "description": "并行构建 TIF 内外部金字塔，提升大影像浏览速度。",
        "fields": [field("input_path", "TIF 文件或目录", required=True, **PATH), field("workers", "并发数", "number", default=4, min=1, max=32), field("max_factor", "最大层级因子", "select", default="256", options=[["32", "32"], ["64", "64"], ["128", "128"], ["256", "256"]]), field("gdal_cache_mb", "GDAL 缓存 (MB)", "number", default=512, min=64), field("recursive", "递归子目录", "checkbox", default=False), field("force", "重新构建", "checkbox", default=False), field("dry_run", "仅预检", "checkbox", default=False)],
    },
    {
        "id": "copy-txt", "name": "清单批量复制", "folder": "copy_txt", "category": "数据整理", "accent": "blue",
        "description": "按照 TXT 清单并发复制文件或文件夹，并持续反馈速度与进度。",
        "fields": [field("txt_path", "TXT 清单路径", required=True, **PATH), field("output_folder", "输出目录", required=True, **PATH), field("copy_folders", "按文件夹复制", "checkbox", default=False)],
    },
]


TOOL_MAP = {tool["id"]: tool for tool in TOOLS}


def public_catalog() -> list[dict[str, Any]]:
    return TOOLS
