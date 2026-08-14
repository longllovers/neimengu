# 影像裁切合并网页工具

该工具使用 Python `ThreadingHTTPServer` 提供网页，可由多个页面同时提交任务。每个后台任务会读取 TXT 中的影像路径，按 6 位县代码从 `00县边界/15_县边界.shp` 取得县界，通过 GDAL 创建虚拟合并 VRT，再按县界直接生成最终 GeoTIFF。

县界优先按 `area_code` 的前 6 位匹配。如果生产目录使用的内部县代码在县界中不存在（例如示例目录中的 `150208`，而临河区标准代码为 `150802`），程序会尝试用 TXT 文件名中的县名进行唯一匹配，输出名仍保留用户输入的县代码。

## 安装和启动

```bash
conda activate qgis
python -c 'from osgeo import gdal,ogr,osr; print(gdal.VersionInfo()); print(osr.GetPROJSearchPaths())'
python app.py --host 0.0.0.0 --port 8000 --workers 1
```

浏览器打开 `http://服务器IP:8000`。`--workers` 允许设置 1～4 个同时执行的任务，默认 1、上限 4；更多页面提交的任务会排队。程序通过 CPU affinity 将整个服务限制在可用逻辑 CPU 的 50%：默认单任务可独占完整的 50% 配额；手动启用多个任务时，它们共同分享这 50%，不会各自占用 50%。每个任务的 GDAL Warp 工作内存上限为 16 GiB，整个服务进程的 GDAL 缓存上限为 32 GiB。如果系统不支持 CPU affinity，程序会退回静态均分线程。程序的影像和县界处理统一使用 `osgeo.gdal`、`osgeo.ogr` 与 `osgeo.osr`，服务器应使用已经验证可导入这些模块的 `qgis` Conda 环境启动。程序会自动使用当前 Conda 环境下的 `share/proj/proj.db`，并忽略误设为目录的 `PROJ_AUX_DB`。

## 路径和输出规则

- 网页中输入的 TXT 路径、影像根目录和保存目录都会先通过 `convert_network_path()` 转成 Linux 挂载路径。
- TXT 支持 UTF-8 或带 BOM 的 UTF-8；空行、以 `#` 开头的行会忽略，重复路径会去重。
- TXT 中的绝对路径直接使用，短路径则拼接到网页填写的影像根目录。
- 分辨率不再决定影像所在目录，只用于任务参数和输出文件名。
- 输出文件名示例：`ELDOM150208_20250101-20251231_0.5m.tif`。
- 保存文件夹不存在时自动创建。正在生成的文件使用独立临时名称，完成后才改成正式文件名。
- 每个任务只创建很小的临时 VRT 和县界文件，不再生成逐幅临时裁切 TIFF。
- 输入 TIFF 的 NoData 需提前人工设置正确；程序直接使用影像现有的 NoData 元数据，不再检查或修改。
- GDAL 从 VRT 中只读取县界所需区域，直接生成独立的最终 TIFF；最终文件不依赖 VRT。
- 任务成功或失败后都会自动删除临时 VRT 目录，清理结果会显示在网页日志中。

网页只用 JavaScript 提交任务并轮询更新状态和日志，不会用 JavaScript 修改输入框内容。按钮旁会显示“正在执行”“执行完成”或“执行失败”，输出窗口可通过右侧滚动条查看完整日志。
