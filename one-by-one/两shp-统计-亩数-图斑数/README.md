# SHP 平方千米与图斑数量对比

使用 Python 标准库 `ThreadingHTTPServer` 提供本地网页服务，以输入文件夹中的 `.shp`
文件名为索引，对比模型文件夹中的同名文件。坐标系自动转换使用 `pyproj`。
支持打开多个浏览器标签页同时运行；每个标签页拥有独立任务状态，每个任务内部默认使用
4 个线程并行处理 SHP。

## 运行

```powershell
python app.py
```

浏览器会自动打开 <http://127.0.0.1:8000>。如果没有自动打开，请手动访问该地址。

面积使用 `1 平方千米 = 1,000,000 平方米` 换算，支持 Polygon、PolygonZ 和
PolygonM。米制投影坐标直接计算；经纬度或其他非米制坐标会根据 `.prj` 在内存中自动
转换到局部等积米制投影后计算，不会生成、修改或覆盖任何原始 SHP 文件。缺少 `.prj`
但坐标范围明显是经纬度时，程序会按 WGS84 处理。

自动坐标转换使用 `pyproj`：

```powershell
pip install pyproj
```

输出 CSV 使用 UTF-8 with BOM 编码，可直接用 Excel 打开。
