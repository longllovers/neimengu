# 县级影像批量裁剪

脚本先建立可复用的 SQLite RTree 影像空间索引。每个县只选择相交 TIFF，再通过一次
`gdalwarp` 完成 Albers 坐标统一、镶嵌和按投影后县界裁剪。

相关输入中已有 Albers 等积圆锥投影时，直接沿用该 Albers；没有 Albers 输入时使用中国
通用 Albers（中央经线 105°、标准纬线 25°/47°、GRS80）。非 Albers 影像由
`gdalwarp -t_srs` 在裁剪时重投影。临时县界使用 GeoPackage 保存，并向 `gdalwarp`
显式传递 `-cutline_srs`。

详细约定见 [需求说明.md](需求说明.md)。

## 环境

推荐直接使用已有的 Linux Conda `qgis` 环境。该环境中的 GDAL 3.13 已提供脚本所需的
Python `osgeo` 模块以及以下命令：

- `gdalwarp`
- `gdaladdo`

脚本不再要求单独安装 Rasterio、GeoPandas 或 Shapely。

## 第一次运行：建立索引并裁剪

```bash
conda activate qgis
python clip_counties.py \
  --imagery-dir "/影像在Linux中的挂载路径/0.5m影像_转投影影像" \
  --boundary "./00县边界" \
  --output-dir "./输出_0.5m" \
  --date1 20250101 \
  --date2 20251231 \
  --resolution 0.5m \
  --workers 4 \
  --cpu-percent 75 \
  --gdal-memory-gb 8 \
  --overview-max-factor 256 \
  --buffer-distance-m 50 \
  --index "./0.5m影像索引.sqlite" \
  --index-mode auto
```

注意：`\\10.10.10.11\...` 是 Windows UNC 写法。Linux 服务器上应先把 SMB 共享挂载到
某个目录，再把该 Linux 路径传给 `--imagery-dir`。

默认文件名示例：

```text
ELDOM150102_20250101_20251231_0.5m.tif
```

## 后续运行

影像目录中可能新增文件时，继续使用 `auto`。脚本只重新读取新增/变化影像的元数据：

```bash
python clip_counties.py ... --index-mode auto
```

确定影像目录没有变化、希望完全跳过扫描时：

```bash
python clip_counties.py ... --index-mode skip
```

强制完整重建：

```bash
python clip_counties.py ... --index-mode rebuild
```

## 常用参数

- `--county 150102`：只处理一个县；可重复写多个。
- `--buffer-distance-m 50`：县界向外缓冲的米数，默认 50；影像覆盖不足时仅保留实际覆盖部分。
- `--overwrite`：覆盖已有输出；默认断点续跑并跳过已有结果。
- `--pixel-size 0.5`：真正重采样为 0.5 × 0.5；不写时保持原始/VRT 像元大小。
- `--resampling bilinear`：设置重采样算法；分类影像建议保持默认 `near`。
- `--gdal-memory-gb 8`：所有并发县任务合计使用 8 GB GDAL 缓存/warp 预算。
- `--cpu-percent 75`：整个任务及其索引、县级 Python、GDAL 子进程最高只能调用约
  75% 的逻辑 CPU 核心；这是上限，不要求程序持续用满。
- `--overview-max-factor 256`：按 2、4、8……256 构建外部 `.ovr`，完成后再构建内部金字塔。
- `--name-template "ELDOM{code}{date1}{date2}_{resolution}.tif"`：修改命名格式。
- `--creation-option PREDICTOR=2`：追加 GeoTIFF 创建选项。
- `--temp-dir /data/gdal_temp`：把临时 VRT 和裁剪线放到指定的高速磁盘。

建议先通过 `--county` 选择一个县做小范围验证，再批量处理全部 103 个县。

## Web 控制界面

在脚本所在目录中激活 QGIS 环境并启动 HTTP 服务：

```bash
conda activate qgis
python clip_counties_web.py
```

默认监听局域网所有网卡的 `9007` 端口。同一局域网内的电脑访问：

```text
http://运行脚本的电脑IP:9007/
```

启动终端会自动打印检测到的局域网访问地址。本机也可以访问
`http://127.0.0.1:9007/`。如需修改，仍可使用
`--host 127.0.0.1 --port 8765`。

页面左侧可以设置影像、县界、输出、日期、分辨率、索引模式、指定县代码、县界外扩米数、并发县数、
CPU 比例、GDAL 总内存 GB、重采样、金字塔最高倍数和 GDAL 创建选项。右侧显示：

- 当前是第几个县 / 总县数；
- 已完成百分比和总体进度条；
- 每个县一行的原位状态更新；
- 裁剪脚本标准输出。

页面使用 SSE 长连接接收脚本输出和进度事件，不使用 HTTP 定时轮询。输入框只在点击
“开始处理”提交表单时生效，不进行 JavaScript 实时更新。

每个新打开的网页标签页都会在服务端获得一个随机隐藏 ID，并分别拥有自己的：

- 裁剪子进程和开始/停止控制；
- 页面参数；
- SSE 事件流；
- 逐县进度和终端历史。

因此，一个页面启动的任务和输出不会同步到另一个新页面。隐藏 ID 不显示在页面或地址栏。
同一标签页直接刷新时会恢复该标签页原来的会话；复制或新开页面时会创建独立会话。已经
结束且超过 24 小时没有访问的页面会话会自动清理，运行中的任务不会被清理。

页面会话保存在 HTTP 服务进程的内存中。重启 HTTP 服务后，旧页面的会话会失效；旧
SSE 连接收到空 `204` 后停止重连，刷新页面即可创建新的独立会话。

“停止任务”使用异步请求，不刷新页面或断开 SSE。HTTP 服务会立即确认停止请求，先向
该页面对应的 Python、索引进程、县级进程和 GDAL 进程组发送温和停止信号，让程序清理
临时 TIFF、`.ovr` 和临时目录；8 秒内仍未退出时自动强制结束整个进程树。

页面填写的是全部并发任务合计的 GDAL 内存 GB，程序平均分配到各县进程。GDAL Warp
内存和缓存参数始终携带明确的 `MB` 单位，避免 GDAL 将大于等于 10000 的无单位数值
按字节解释。

索引元数据由独立 Python 进程并行读取，SQLite RTree 只由主进程串行写入。每个县也由
独立 Python 子进程处理，县内的 `gdalwarp` 重采样与 `gdaladdo` 是独立 GDAL 进程并可
使用多线程。县级 TIFF 先写入唯一临时文件，并在稳定的跨进程锁保护下依次构建外部和
内部金字塔，全部成功后才替换正式 TIFF 与 `.ovr`，防止同一输出被本程序同时写入。

页面路径输入支持直接粘贴 Windows UNC 地址。任务启动前会在 Python 服务端转换为
Linux 实际挂载地址：

| Windows 网络共享 | Linux 挂载目录 |
|---|---|
| `\\10.10.10.11\data` | `/media/cangling/nas_folder` |
| `\\10.10.10.10\4np_share` | `/mnt/data/4np/` |

除以上两条及其子目录外，其他路径保持原样，不进行映射。

例如，页面输入
`\\10.10.10.11\data\原始影像\0.5m影像_转投影影像`，实际传给裁剪脚本的是
`/media/cangling/nas_folder/原始影像/0.5m影像_转投影影像`。页面会保留原始输入，
脚本输出窗口开头显示转换后的实际启动命令。

如果不允许直接开放局域网端口，也可以使用 SSH 端口转发：

```bash
ssh -L 9007:127.0.0.1:9007 cangling@Linux主机地址
```

然后在本机浏览器访问 `http://127.0.0.1:9007/`。默认的
`--host 0.0.0.0` 应只在可信局域网中使用。
