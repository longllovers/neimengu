# Shapefile 面要素检查

`check_shp.py` 用于检查：

- 两个面是否存在实际重叠，并输出两个要素 ID 和重叠面积；
- 单个面面积是否小于 0.1 亩，并输出该要素 ID 和面积；
- 每条结果记录对应 Shapefile 的完整文件路径；
- 所有问题统一保存到 UTF-8 BOM 编码的 CSV，Excel 可直接打开中文。

仅共边或共点不属于面重叠。面积计算前会自动转为适合数据位置的米制投影，
再按 `1 亩 = 666.6666667 平方米` 换算。
默认忽略不超过 `1e-5 亩`（约 `0.00666667 平方米`）的极微小交集，以排除
坐标浮点误差；可通过 `--min-overlap-sqm` 修改该容差。

## 运行

当前项目的 `shp` 目录中只有一个 Shapefile，可直接运行：

```powershell
.\.venv\Scripts\python.exe .\check_shp.py
```

默认在项目的 `CSV` 文件夹中输出，CSV 名称与 Shapefile 名称一致。例如：

```text
CSV\K49E012022_2025.csv
```

明确指定输入和输出文件：

```powershell
.\.venv\Scripts\python.exe .\check_shp.py .\shp\K49E012022_2025.shp -o .\检查结果.csv
```

如果属性表中有唯一 ID 字段，例如 `OBJECTID`：

```powershell
.\.venv\Scripts\python.exe .\check_shp.py --id-field OBJECTID
```

未指定 `--id-field` 时使用从 0 开始的 Shapefile FID/行号。本项目数据属性表
只有 `TBLXDM` 和 `TBLXMC`，它们都不是唯一 ID，因此默认使用 FID。

修改小面积阈值：

```powershell
.\.venv\Scripts\python.exe .\check_shp.py --min-mu 0.2
```

如需把任何大于 0 的面交集都列出（包括极小碎片）：

```powershell
.\.venv\Scripts\python.exe .\check_shp.py --min-overlap-sqm 0
```

## Web 页面

页面服务器使用 Python 标准库 `ThreadingHTTPServer`，不使用 Flask，可同时在多个
浏览器标签页中处理请求：

```powershell
.\.venv\Scripts\python.exe .\app.py
```

默认自动打开 `http://127.0.0.1:9001`。页面支持拖入一套或多套 Shapefile、
指定 CSV 输出文件夹、复制保存路径，以及在带横向和纵向滚动条的表格中查看
完整 CSV 内容。

页面提供“CSV 保存文件夹”和“合并后 SHP 保存文件夹”两个路径输入框。
“自动合并小于 0.1 亩的面”默认不勾选；勾选后，SHP 保存路径输入框才会启用。
小面会合并到与它直接接触或相交、且面积最大的非小面积面中。新的 Shapefile
及其 `.shx`、`.dbf`、`.prj` 等配套文件会直接保存在指定文件夹中，不再创建
额外子文件夹，也不添加“_合并后”后缀，而是保持原文件名。请将合并结果保存到
与原始数据不同的文件夹，避免覆盖同名文件。
CSV 的“自动合并到ID”列会记录目标要素 ID。合并时选择直接接触或相交的
非小面积面中面积最大的面；如果找不到这样的目标，小面会从合并结果中删除，
不会再合并到距离最近的面。
勾选自动合并时，程序会先合并、再检查合并后的 Shapefile。CSV 中成功处理的
小面会显示为“已自动合并”，只有合并后仍然存在的小面才会显示“面积小于0.1亩”。
无法找到相邻目标并被删除的小面会显示为“已自动删除”。

Windows 下也可以直接双击 `启动页面.bat`。

受浏览器安全机制限制，拖入文件时网页无法取得源文件在本机的绝对路径，因此
Web 页面生成的 CSV 会在“文件路径”列记录上传文件名，并另外在
“CSV保存路径”列记录结果文件的完整绝对路径。直接运行命令行脚本时，
“文件路径”列仍记录源 Shapefile 的完整路径。

两个保存路径输入框都支持手动填写网络路径。服务端会自动转换以下
`169.254.51.1` 至 `169.254.51.255` 网段的共享路径：

- `\\169.254.51.x\data` → `/media/cangling/nas_folder`
- `\\169.254.51.x\新建卷` → `/media/cangling/xinjianjuan`
- `\\169.254.51.x\datadisk2` → `/media/cangling/EAGET`
- `\\169.254.51.x\新加卷` → `/media/cangling/xinjiajuan`

共享目录之后的相对路径会原样保留。

Shapefile 由多个同名文件组成。上传时至少需要同时拖入 `.shp`、`.shx`、
`.dbf`、`.prj`；也可以一并拖入 `.cpg` 等配套文件。
