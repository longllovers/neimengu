# 遥感测量数据成果规范检查器

## 运行

在项目目录执行：

```powershell
.\.venv\Scripts\python.exe main.py "G:\EL_150000_2026" `
  --gdb-schema 5-1 `
  --zpj-schema 5-4
```

可选方案：

- GDB：`--gdb-schema 5-1` 或 `--gdb-schema 6-1`
- ELJDZPJ：`--zpj-schema 5-4` 或 `--zpj-schema 6-3`

自定义输出路径：

```powershell
.\.venv\Scripts\python.exe main.py "G:\EL_150000_2026" `
  --output "output\pdf\EL_150000_2026_检查报告.pdf" `
  --json-output "output\EL_150000_2026_检查明细.json"
```

程序返回码：

- `0`：通过
- `1`：存在不合规项

完整规则见 [REQUIREMENTS.md](REQUIREMENTS.md)。

## Web 页面

页面仅使用 Python 标准库 `ThreadingHTTPServer`，每个浏览器标签页具有独立任务 ID。

```powershell
.\.venv\Scripts\python.exe web_server.py --host 0.0.0.0 --port 8000
```

浏览器访问：

```text
http://服务器IP:8000
```

服务器部署说明：

- 把项目根目录中的 `SimHei.ttf` 一起复制到服务器，PDF 会优先使用该字体。
- 也可通过环境变量 `SIMHEI_FONT` 指定其他黑体字体文件的绝对路径。
- 网络路径如 `\\169.254.51.10\data\...` 会自动转换为 `/media/cangling/nas_folder/...`。
- 输入一级成果文件夹后，页面会自动把结果目录设置为其上一级的
  `<一级文件夹名>_检查结果`，也可以手动修改。
- PDF 和 JSON 使用带运行 ID 的唯一文件名，不同标签页和不同运行互不覆盖。
- 检查 GDB、Shapefile 和 TIFF 的坐标系；缺失坐标系或不是 Albers 时写入 PDF。
- 终端不记录页面状态轮询请求，只输出任务开始、输入/输出路径、核验方案和完成结果。
