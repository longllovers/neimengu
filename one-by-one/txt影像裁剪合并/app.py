from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys
import tempfile
import threading
import traceback
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal, InvalidOperation
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlparse


APP_DIR = Path(__file__).resolve().parent
COUNTY_BOUNDARY = APP_DIR / "00县边界" / "15_县边界.shp"
MAX_REQUEST_BYTES = 64 * 1024
MAX_LOG_LINES = 1000
CPU_PERCENT_LIMIT = 0.50
CPU_THREAD_BUDGET = max(1, int((os.cpu_count() or 1) * CPU_PERCENT_LIMIT))
GDAL_THREADS_PER_TASK = CPU_THREAD_BUDGET
MAX_TASK_WORKERS = 4
WARP_MEMORY_LIMIT_BYTES = 16 * 1024**3
GDAL_CACHE_MAX_BYTES = 32 * 1024**3


def configure_proj_environment() -> tuple[str, str | None]:
    """让 PROJ 始终使用当前 Python/Conda 环境中的数据库。"""
    active_proj_dir = Path(sys.prefix) / "share" / "proj"
    active_proj_db = active_proj_dir / "proj.db"
    proj_data_path = os.environ.get("PROJ_DATA", "")
    if active_proj_db.is_file():
        proj_data_path = str(active_proj_dir)
        os.environ["PROJ_DATA"] = proj_data_path

    ignored_aux_db = None
    configured_aux_db = os.environ.get("PROJ_AUX_DB")
    if configured_aux_db:
        aux_db_paths = [
            Path(path) for path in configured_aux_db.split(os.pathsep) if path
        ]
        # PROJ_AUX_DB 只能填写 SQLite 数据库文件。误填目录会导致
        # proj_identify() 把目录当数据库打开，并报 "Open of .../share/proj failed"。
        if any(not path.is_file() for path in aux_db_paths):
            ignored_aux_db = configured_aux_db
            os.environ.pop("PROJ_AUX_DB", None)

    return proj_data_path, ignored_aux_db


PROJ_DATA_PATH, IGNORED_PROJ_AUX_DB = configure_proj_environment()


def validate_gdal_environment() -> tuple[str, str]:
    try:
        from osgeo import gdal, ogr, osr
    except ImportError as exc:
        raise RuntimeError(
            "当前 Conda 环境没有安装 GDAL（osgeo），无法启动服务"
        ) from exc

    gdal.UseExceptions()
    ogr.UseExceptions()
    osr.UseExceptions()
    if ogr.GetDriverByName("ESRI Shapefile") is None:
        raise RuntimeError("当前 GDAL 没有 ESRI Shapefile 驱动")
    if ogr.GetDriverByName("GPKG") is None:
        raise RuntimeError("当前 GDAL 没有 GPKG 驱动")

    test_srs = osr.SpatialReference()
    test_srs.ImportFromEPSG(4326)
    proj_version = (
        f"{osr.GetPROJVersionMajor()}."
        f"{osr.GetPROJVersionMinor()}."
        f"{osr.GetPROJVersionMicro()}"
    )
    return gdal.VersionInfo("--version"), proj_version


def configure_process_cpu_limit() -> tuple[int, int, bool, str]:
    """把整个服务进程限制在可用逻辑 CPU 的 50% 上。"""
    try:
        if hasattr(os, "sched_getaffinity") and hasattr(os, "sched_setaffinity"):
            available_cpus = sorted(os.sched_getaffinity(0))
            if not available_cpus:
                raise RuntimeError("没有检测到可用 CPU")
            budget = max(1, int(len(available_cpus) * CPU_PERCENT_LIMIT))
            selected_cpus = set(available_cpus[:budget])
            os.sched_setaffinity(0, selected_cpus)
            return len(available_cpus), budget, True, "Linux CPU affinity"

        if os.name == "nt":
            import ctypes

            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel32.GetCurrentProcess.restype = ctypes.c_void_p
            kernel32.GetProcessAffinityMask.argtypes = [
                ctypes.c_void_p,
                ctypes.POINTER(ctypes.c_size_t),
                ctypes.POINTER(ctypes.c_size_t),
            ]
            kernel32.GetProcessAffinityMask.restype = ctypes.c_int
            kernel32.SetProcessAffinityMask.argtypes = [
                ctypes.c_void_p,
                ctypes.c_size_t,
            ]
            kernel32.SetProcessAffinityMask.restype = ctypes.c_int
            process_handle = kernel32.GetCurrentProcess()
            process_mask = ctypes.c_size_t()
            system_mask = ctypes.c_size_t()
            if not kernel32.GetProcessAffinityMask(
                process_handle,
                ctypes.byref(process_mask),
                ctypes.byref(system_mask),
            ):
                raise OSError(ctypes.get_last_error(), "读取 CPU affinity 失败")

            available_cpus = [
                index
                for index in range(ctypes.sizeof(ctypes.c_size_t) * 8)
                if process_mask.value & (1 << index)
            ]
            if not available_cpus:
                raise RuntimeError("没有检测到可用 CPU")
            budget = max(1, int(len(available_cpus) * CPU_PERCENT_LIMIT))
            selected_mask = sum(1 << index for index in available_cpus[:budget])
            if not kernel32.SetProcessAffinityMask(
                process_handle,
                ctypes.c_size_t(selected_mask),
            ):
                raise OSError(ctypes.get_last_error(), "设置 CPU affinity 失败")
            return len(available_cpus), budget, True, "Windows CPU affinity"
    except Exception as exc:
        total = os.cpu_count() or 1
        budget = max(1, int(total * CPU_PERCENT_LIMIT))
        return total, budget, False, str(exc)

    total = os.cpu_count() or 1
    budget = max(1, int(total * CPU_PERCENT_LIMIT))
    return total, budget, False, "当前系统不支持 CPU affinity"


def convert_network_path(path):
    if path is None:
        return path

    path = str(path).strip()
    if not path:
        return path

    # 把 Windows 的反斜杠 \ 转成 Linux 风格 /
    path = path.replace("\\", "/")

    prefix_mapping = []

    for i in range(1, 256):
        # data -> /media/cangling/nas_folder
        prefix_mapping.append((f"//10.10.10.{i}/data", "/media/cangling/nas_folder"))
        prefix_mapping.append((f"/10.10.10.{i}/data", "/media/cangling/nas_folder"))
        prefix_mapping.append((f"10.10.10.{i}/data", "/media/cangling/nas_folder"))

        # 新建卷 -> /media/cangling/xinjianjuan
        prefix_mapping.append((f"//10.10.10.{i}/新建卷", "/media/cangling/xinjianjuan"))
        prefix_mapping.append((f"/10.10.10.{i}/新建卷", "/media/cangling/xinjianjuan"))
        prefix_mapping.append((f"10.10.10.{i}/新建卷", "/media/cangling/xinjianjuan"))

        # datadisk2 -> /media/cangling/EAGET
        prefix_mapping.append((f"//10.10.10.{i}/datadisk2", "/media/cangling/EAGET"))
        prefix_mapping.append((f"/10.10.10.{i}/datadisk2", "/media/cangling/EAGET"))
        prefix_mapping.append((f"10.10.10.{i}/datadisk2", "/media/cangling/EAGET"))

        # 新加卷 -> /media/cangling/xinjiajuan
        prefix_mapping.append((f"//10.10.10.{i}/新加卷", "/media/cangling/xinjiajuan"))
        prefix_mapping.append((f"/10.10.10.{i}/新加卷", "/media/cangling/xinjiajuan"))
        prefix_mapping.append((f"10.10.10.{i}/新加卷", "/media/cangling/xinjiajuan"))

    for windows_prefix, linux_prefix in prefix_mapping:
        # 必须完整匹配共享目录名，避免 data 错误匹配 datadisk2。
        if path == windows_prefix:
            return linux_prefix
        if path.startswith(windows_prefix + "/"):
            relative_path = path[len(windows_prefix):]
            return linux_prefix + relative_path

    return path


def normalize_resolution(value: Any) -> str:
    text = str(value).strip().lower()
    if text.endswith("m"):
        text = text[:-1].strip()
    try:
        number = Decimal(text)
    except InvalidOperation as exc:
        raise ValueError("影像分辨率必须是数字，例如 0.5 或 2") from exc
    if not number.is_finite() or number <= 0:
        raise ValueError("影像分辨率必须是大于 0 的数字")
    normalized = format(number.normalize(), "f")
    return normalized.rstrip("0").rstrip(".") if "." in normalized else normalized


def validate_date(value: Any, label: str) -> str:
    text = str(value).strip()
    if not re.fullmatch(r"\d{8}", text):
        raise ValueError(f"{label}必须是 8 位日期，格式为 YYYYMMDD")
    try:
        datetime.strptime(text, "%Y%m%d")
    except ValueError as exc:
        raise ValueError(f"{label}不是有效日期") from exc
    return text


def validate_inputs(data: dict[str, Any]) -> dict[str, str]:
    txt_path = convert_network_path(data.get("txt_path"))
    image_root = convert_network_path(data.get("image_root"))
    output_dir = convert_network_path(data.get("output_dir"))
    if not txt_path:
        raise ValueError("请输入 TXT 路径")
    if not image_root:
        raise ValueError("请输入影像根目录")
    if not output_dir:
        raise ValueError("请输入保存文件夹路径")

    resolution = normalize_resolution(data.get("resolution", ""))
    date1 = validate_date(data.get("date1", ""), "日期1")
    date2 = validate_date(data.get("date2", ""), "日期2")
    if date1 > date2:
        raise ValueError("日期1不能晚于日期2")

    county_code = str(data.get("county_code", "")).strip()
    if not re.fullmatch(r"\d{6}", county_code):
        raise ValueError("县代码必须是 6 位数字")

    return {
        "txt_path": txt_path,
        "image_root": image_root,
        "resolution": resolution,
        "date1": date1,
        "date2": date2,
        "county_code": county_code,
        "output_dir": output_dir,
    }


@dataclass
class Task:
    id: str
    params: dict[str, str]
    status: str = "queued"
    logs: list[str] = field(default_factory=list)
    result: str | None = None
    error: str | None = None
    created_at: str = field(default_factory=lambda: datetime.now().isoformat(timespec="seconds"))
    updated_at: str = field(default_factory=lambda: datetime.now().isoformat(timespec="seconds"))
    lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def log(self, message: str) -> None:
        line = f"[{datetime.now():%H:%M:%S}] {message}"
        with self.lock:
            self.logs.append(line)
            if len(self.logs) > MAX_LOG_LINES:
                del self.logs[: len(self.logs) - MAX_LOG_LINES]
            self.updated_at = datetime.now().isoformat(timespec="seconds")
        print(f"[任务 {self.id[:8]}] {line}", flush=True)

    def set_status(self, status: str, *, error: str | None = None) -> None:
        with self.lock:
            self.status = status
            self.error = error
            self.updated_at = datetime.now().isoformat(timespec="seconds")

    def snapshot(self) -> dict[str, Any]:
        with self.lock:
            return {
                "id": self.id,
                "status": self.status,
                "logs": list(self.logs),
                "result": self.result,
                "error": self.error,
                "created_at": self.created_at,
                "updated_at": self.updated_at,
            }


TASKS: dict[str, Task] = {}
TASKS_LOCK = threading.Lock()
EXECUTOR: ThreadPoolExecutor


def read_image_paths(txt_path: str, image_root: str) -> list[str]:
    paths: list[str] = []
    seen: set[str] = set()
    with open(txt_path, "r", encoding="utf-8-sig") as file:
        for line_number, raw_line in enumerate(file, 1):
            entry = raw_line.strip().strip('"').strip("'")
            if not entry or entry.startswith("#"):
                continue
            converted = convert_network_path(entry)
            # 已转换成 Linux 绝对路径的条目直接使用，其余条目视为相对路径。
            if os.path.isabs(converted):
                full_path = os.path.normpath(converted)
            else:
                full_path = os.path.normpath(os.path.join(image_root, converted))
            if full_path not in seen:
                paths.append(full_path)
                seen.add(full_path)
    if not paths:
        raise ValueError("TXT 中没有可用的影像路径")
    return paths


def load_county_geometry(county_code: str, txt_path: str = ""):
    try:
        from osgeo import ogr
    except ImportError as exc:
        raise RuntimeError(
            "当前运行程序的 Python 环境没有安装 GDAL（osgeo），请使用已安装 GDAL 的环境启动"
        ) from exc

    ogr.UseExceptions()

    if not COUNTY_BOUNDARY.is_file():
        raise FileNotFoundError(f"找不到县界文件：{COUNTY_BOUNDARY}")

    source = ogr.Open(str(COUNTY_BOUNDARY), 0)
    if source is None:
        raise RuntimeError(f"OGR 无法打开县界文件：{COUNTY_BOUNDARY}")
    layer = source.GetLayer(0)
    if layer is None:
        source = None
        raise RuntimeError(f"县界文件中没有可读取的图层：{COUNTY_BOUNDARY}")

    spatial_reference = layer.GetSpatialRef()
    boundary_crs = (
        spatial_reference.ExportToWkt() if spatial_reference is not None else None
    )
    if not boundary_crs:
        source = None
        raise ValueError(f"县界文件没有坐标系：{COUNTY_BOUNDARY}")

    records = []
    layer.ResetReading()
    for feature in layer:
        area_code = str(feature.GetField("area_code") or "").strip()
        area_name = str(feature.GetField("area_name") or "").strip()
        feature_geometry = feature.GetGeometryRef()
        if feature_geometry is not None and not feature_geometry.IsEmpty():
            records.append((area_code, area_name, feature_geometry.Clone()))
    source = None

    matched = [record for record in records if record[0][:6] == county_code]
    match_method = "县代码"
    if not matched and txt_path:
        # 部分生产目录使用内部代码（示例 150208），与标准行政区代码并不一致。
        # 此时允许从 TXT 文件名中的唯一县名回退匹配，但不会改变输出所用代码。
        txt_name = os.path.basename(txt_path)
        matched = [record for record in records if record[1] and record[1] in txt_name]
        match_method = "TXT 文件名中的县名"
    if not matched:
        raise ValueError(
            f"县界中找不到县代码 {county_code}，TXT 文件名中也没有可唯一识别的县名"
        )
    if match_method != "县代码" and len(matched) != 1:
        raise ValueError("TXT 文件名匹配到多个县界，无法确定要使用的县界")

    geometry = matched[0][2].Clone()
    for record in matched[1:]:
        merged_geometry = geometry.Union(record[2])
        if merged_geometry is None:
            raise RuntimeError(f"合并县代码 {county_code} 的多个县界几何失败")
        geometry = merged_geometry
    if geometry.IsEmpty():
        raise ValueError(f"县代码 {county_code} 对应的县界为空")
    matched_codes = ",".join(sorted({record[0][:6] for record in matched}))
    matched_names = ",".join(sorted({record[1] for record in matched}))
    return (
        bytes(geometry.ExportToWkb()),
        boundary_crs,
        match_method,
        matched_codes,
        matched_names,
    )


def write_cutline(
    cutline_path: str, county_geometry_wkb: bytes, boundary_crs: str
) -> None:
    from osgeo import ogr, osr

    ogr.UseExceptions()
    osr.UseExceptions()

    geometry = ogr.CreateGeometryFromWkb(county_geometry_wkb)
    if geometry is None or geometry.IsEmpty():
        raise ValueError("无法从县界几何创建临时裁切图形")

    spatial_reference = osr.SpatialReference()
    spatial_reference.ImportFromWkt(boundary_crs)

    driver = ogr.GetDriverByName("GPKG")
    if driver is None:
        raise RuntimeError("当前 GDAL 没有 GPKG 驱动，无法创建临时县界文件")
    destination = driver.CreateDataSource(cutline_path)
    if destination is None:
        raise RuntimeError(f"无法创建临时县界文件：{cutline_path}")

    try:
        layer = destination.CreateLayer(
            "county",
            srs=spatial_reference,
            geom_type=geometry.GetGeometryType(),
        )
        if layer is None:
            raise RuntimeError("无法在临时县界文件中创建 county 图层")
        layer.CreateField(ogr.FieldDefn("id", ogr.OFTInteger))

        feature = ogr.Feature(layer.GetLayerDefn())
        feature.SetField("id", 1)
        feature.SetGeometry(geometry)
        layer.CreateFeature(feature)
        feature = None
        destination.FlushCache()
    finally:
        destination = None


def validate_gdal_sources(image_paths: list[str], gdal, osr) -> None:
    reference_projection = None
    reference_srs = None
    reference_band_count = None
    reference_data_types = None

    for index, image_path in enumerate(image_paths, 1):
        dataset = gdal.OpenEx(image_path, gdal.OF_RASTER | gdal.OF_READONLY)
        if dataset is None:
            raise RuntimeError(f"GDAL 无法打开影像：{image_path}")
        try:
            projection = dataset.GetProjectionRef()
            if not projection:
                raise ValueError(f"影像没有坐标系：{image_path}")
            band_count = dataset.RasterCount
            data_types = tuple(
                dataset.GetRasterBand(band).DataType for band in range(1, band_count + 1)
            )
            current_srs = osr.SpatialReference()
            current_srs.ImportFromWkt(projection)

            if index == 1:
                reference_projection = projection
                reference_srs = current_srs
                reference_band_count = band_count
                reference_data_types = data_types
            else:
                if not reference_srs.IsSame(current_srs):
                    raise ValueError(f"影像坐标系不一致：{image_path}")
                if band_count != reference_band_count:
                    raise ValueError(f"影像波段数不一致：{image_path}")
                if data_types != reference_data_types:
                    raise ValueError(f"影像数据类型不一致：{image_path}")
        finally:
            dataset = None

    if reference_projection is None:
        raise ValueError("没有可用于创建 VRT 的影像")


def build_vrt_and_warp(
    image_paths: list[str],
    county_geometry,
    boundary_crs,
    temporary_dir: str,
    destination_path: str,
    task: Task,
) -> None:
    try:
        from osgeo import gdal, osr
    except ImportError as exc:
        raise RuntimeError(
            "当前运行程序的 Python 环境没有安装 GDAL（osgeo），请使用已安装 GDAL 的环境启动"
        ) from exc

    gdal.UseExceptions()
    gdal.SetCacheMax(GDAL_CACHE_MAX_BYTES)
    validate_gdal_sources(image_paths, gdal, osr)
    cutline_path = os.path.join(temporary_dir, "county_boundary.gpkg")
    vrt_path = os.path.join(temporary_dir, "merged.vrt")
    write_cutline(cutline_path, county_geometry, boundary_crs)

    task.log(f"正在为 {len(image_paths)} 幅影像创建虚拟合并 VRT")
    vrt_options = gdal.BuildVRTOptions(
        resolution="highest",
        strict=True,
        writeAbsolutePath=True,
    )
    # GDAL VRT 的重叠区域以后面的影像优先；反转列表以保持 TXT 第一幅优先。
    vrt_dataset = gdal.BuildVRT(
        vrt_path, list(reversed(image_paths)), options=vrt_options
    )
    if vrt_dataset is None:
        raise RuntimeError("创建 VRT 失败")
    vrt_dataset.FlushCache()
    vrt_dataset = None
    if not os.path.isfile(vrt_path):
        raise RuntimeError("创建 VRT 后未找到 VRT 文件")
    task.log(f"VRT 创建完成：{vrt_path}")

    next_progress = [25]

    def progress_callback(complete, _message, _callback_data):
        percent = int(complete * 100)
        if percent >= next_progress[0]:
            task.log(f"虚拟合并及县界裁切进度：{min(percent, 100)}%")
            while next_progress[0] <= percent:
                next_progress[0] += 25
        return 1

    task.log("开始从 VRT 按县界直接生成最终 GeoTIFF")
    task.log(
        f"GDAL 资源配置：Warp 内存 {WARP_MEMORY_LIMIT_BYTES // 1024**3} GiB，"
        f"全局缓存 {GDAL_CACHE_MAX_BYTES // 1024**3} GiB，"
        f"处理线程 {GDAL_THREADS_PER_TASK}"
    )
    warp_options = gdal.WarpOptions(
        format="GTiff",
        cutlineDSName=cutline_path,
        cutlineLayer="county",
        cropToCutline=True,
        warpMemoryLimit=WARP_MEMORY_LIMIT_BYTES,
        multithread=True,
        warpOptions=[f"NUM_THREADS={GDAL_THREADS_PER_TASK}"],
        creationOptions=[
            "TILED=YES",
            "BLOCKXSIZE=512",
            "BLOCKYSIZE=512",
            "COMPRESS=LZW",
            f"NUM_THREADS={GDAL_THREADS_PER_TASK}",
            "BIGTIFF=IF_SAFER",
        ],
        callback=progress_callback,
    )
    output_dataset = gdal.Warp(destination_path, vrt_path, options=warp_options)
    if output_dataset is None:
        raise RuntimeError("VRT 县界裁切失败")
    output_dataset.FlushCache()
    width = output_dataset.RasterXSize
    height = output_dataset.RasterYSize
    output_dataset = None
    if width <= 0 or height <= 0 or not os.path.isfile(destination_path):
        raise ValueError("VRT 与县界没有有效相交区域，未生成有效 TIFF")


def process_task(task: Task) -> None:
    params = task.params
    task.set_status("running")
    task.log("任务开始")
    temporary_dir: str | None = None
    temporary_output: str | None = None
    processing_succeeded = False
    try:
        txt_path = params["txt_path"]
        if not os.path.isfile(txt_path):
            raise FileNotFoundError(f"TXT 文件不存在：{txt_path}")

        image_root = params["image_root"]
        if not os.path.isdir(image_root):
            raise NotADirectoryError(f"影像根目录不存在：{image_root}")

        image_paths = read_image_paths(txt_path, image_root)
        task.log(f"读取到 {len(image_paths)} 条不重复的影像路径")
        task.log(f"影像根目录：{image_root}")

        missing = [path for path in image_paths if not os.path.isfile(path)]
        if missing:
            preview = "；".join(missing[:5])
            more = f"（另有 {len(missing) - 5} 个）" if len(missing) > 5 else ""
            raise FileNotFoundError(f"有 {len(missing)} 个影像文件不存在：{preview}{more}")

        task.log(f"正在读取县代码 {params['county_code']} 的边界")
        (
            county_geometry,
            boundary_crs,
            match_method,
            matched_codes,
            matched_names,
        ) = load_county_geometry(params["county_code"], txt_path)
        task.log(
            f"县界匹配成功：{matched_names}（标准代码 {matched_codes}，匹配方式：{match_method}）"
        )

        output_dir = params["output_dir"]
        os.makedirs(output_dir, exist_ok=True)
        output_name = (
            f"ELDOM{params['county_code']}_"
            f"{params['date1']}-{params['date2']}_"
            f"{params['resolution']}m.tif"
        )
        final_output = os.path.join(output_dir, output_name)
        temporary_dir = tempfile.mkdtemp(prefix=f"eldom_{task.id[:8]}_")
        task.log(f"临时 VRT 目录：{temporary_dir}（任务结束后自动删除）")
        temporary_output = os.path.join(output_dir, f".{output_name}.{task.id}.tmp.tif")
        build_vrt_and_warp(
            image_paths,
            county_geometry,
            boundary_crs,
            temporary_dir,
            temporary_output,
            task,
        )
        os.replace(temporary_output, final_output)
        temporary_output = None
        with task.lock:
            task.result = final_output
        task.log(f"影像处理完成：{final_output}")
        processing_succeeded = True
    except Exception as exc:
        message = f"{type(exc).__name__}: {exc}"
        task.log(f"任务失败：{message}")
        task.log(traceback.format_exc().rstrip())
        task.set_status("failed", error=message)
    finally:
        cleanup_errors: list[str] = []
        if temporary_output and os.path.exists(temporary_output):
            try:
                os.remove(temporary_output)
                task.log("已删除未完成的临时合并文件")
            except OSError as exc:
                cleanup_errors.append(f"临时合并文件删除失败：{exc}")
        if temporary_dir and os.path.exists(temporary_dir):
            try:
                shutil.rmtree(temporary_dir)
                task.log(f"已删除临时 VRT 目录：{temporary_dir}")
            except OSError as exc:
                cleanup_errors.append(f"临时裁切目录删除失败：{exc}")
        for cleanup_error in cleanup_errors:
            task.log(cleanup_error)
        if processing_succeeded:
            if cleanup_errors:
                task.set_status("failed", error="；".join(cleanup_errors))
            else:
                task.log("任务全部完成")
                task.set_status("completed")


INDEX_HTML = r'''<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>影像裁切合并工具</title>
  <style>
    :root { color-scheme: light; --blue:#1769aa; --line:#d9e0e8; --muted:#667085; }
    * { box-sizing:border-box; }
    body { margin:0; font-family:"Microsoft YaHei",system-ui,sans-serif; background:#f3f6f9; color:#17202a; }
    main { max-width:860px; margin:20px auto; padding:0 16px 26px; }
    h1 { margin:0 0 6px; font-size:26px; }
    .sub { color:var(--muted); margin:0 0 22px; }
    .card { background:white; border:1px solid var(--line); border-radius:12px; padding:18px; box-shadow:0 3px 14px #21354710; }
    .grid { display:grid; grid-template-columns:1fr 1fr; gap:16px; }
    .full { grid-column:1 / -1; }
    label { display:block; font-weight:600; margin-bottom:7px; }
    input { width:100%; height:42px; border:1px solid #b8c2ce; border-radius:7px; padding:0 11px; font-size:15px; }
    input:focus { outline:2px solid #90caf9; border-color:var(--blue); }
    .unit { display:flex; }
    .unit input { border-radius:7px 0 0 7px; }
    .unit span { display:flex; align-items:center; padding:0 14px; background:#eef2f6; border:1px solid #b8c2ce; border-left:0; border-radius:0 7px 7px 0; }
    small { display:block; color:var(--muted); margin-top:5px; }
    .actions { display:flex; align-items:center; gap:14px; margin-top:19px; }
    button { border:0; border-radius:8px; background:var(--blue); color:#fff; padding:11px 22px; font-size:16px; cursor:pointer; }
    button:disabled { opacity:.6; cursor:wait; }
    .run-state { font-size:16px; font-weight:600; color:var(--muted); }
    .run-state.running { color:#b26a00; }
    .run-state.completed { color:#17803d; }
    .run-state.failed { color:#c62828; }
    .status { margin-top:18px; padding:12px; border-radius:8px; background:#eef5fb; display:none; }
    pre { margin:14px 0 0; padding:15px; height:100px; overflow-y:scroll; overflow-x:auto; background:#111820; color:#d6e3ed; border-radius:9px; white-space:pre-wrap; word-break:break-all; font:13px/1.55 Consolas,monospace; scrollbar-gutter:stable; }
    @media (max-width:700px) { .grid { grid-template-columns:1fr; } .full { grid-column:auto; } }
  </style>
</head>
<body>
<main>
  <h1>影像裁切合并工具</h1>
  <p class="sub">按县界逐幅裁切 TXT 中的影像，然后合并为一个 GeoTIFF。</p>
  <section class="card">
    <form id="taskForm">
      <div class="grid">
        <div class="full">
          <label for="txt_path">TXT 路径</label>
          <input id="txt_path" name="txt_path" required placeholder="\\10.10.10.68\data\...\0.5data.txt">
        </div>
        <div class="full">
          <label for="image_root">影像根目录</label>
          <input id="image_root" name="image_root" required placeholder="\\10.10.10.68\data\原始影像">
          <small>TXT 中的短路径将拼接到该目录，输入路径会自动转换为 Linux 路径</small>
        </div>
        <div>
          <label for="resolution">影像分辨率</label>
          <div class="unit"><input id="resolution" name="resolution" required inputmode="decimal" pattern="[0-9]+([.][0-9]+)?" placeholder="0.5"><span>m</span></div>
          <small>只输入数字，例如 0.5 或 2</small>
        </div>
        <div>
          <label for="county_code">县代码</label>
          <input id="county_code" name="county_code" required inputmode="numeric" pattern="[0-9]{6}" minlength="6" maxlength="6" placeholder="150208">
        </div>
        <div>
          <label for="date1">日期1</label>
          <input id="date1" name="date1" required inputmode="numeric" pattern="[0-9]{8}" minlength="8" maxlength="8" placeholder="20250101">
        </div>
        <div>
          <label for="date2">日期2</label>
          <input id="date2" name="date2" required inputmode="numeric" pattern="[0-9]{8}" minlength="8" maxlength="8" placeholder="20251231">
        </div>
        <div class="full">
          <label for="output_dir">保存文件夹路径</label>
          <input id="output_dir" name="output_dir" required placeholder="\\10.10.10.68\data\输出结果">
          <small>文件夹不存在时会自动创建</small>
        </div>
      </div>
      <div class="actions">
        <button id="submitButton" type="submit">开始处理</button>
        <span id="runState" class="run-state" aria-live="polite">等待执行</span>
      </div>
    </form>
    <div id="status" class="status"></div>
    <pre id="log">等待提交任务……</pre>
  </section>
</main>
<script>
  const form = document.getElementById('taskForm');
  const button = document.getElementById('submitButton');
  const runState = document.getElementById('runState');
  const statusBox = document.getElementById('status');
  const logBox = document.getElementById('log');
  let timer = null;

  form.addEventListener('submit', async (event) => {
    event.preventDefault();
    if (timer) clearTimeout(timer);
    button.disabled = true;
    runState.className = 'run-state running';
    runState.textContent = '正在执行……';
    statusBox.style.display = 'block';
    statusBox.textContent = '正在提交任务……';
    logBox.textContent = '';
    const payload = Object.fromEntries(new FormData(form).entries());
    try {
      const response = await fetch('/api/tasks', {
        method: 'POST', headers: {'Content-Type':'application/json'}, body: JSON.stringify(payload)
      });
      const data = await response.json();
      if (!response.ok) throw new Error(data.error || '提交失败');
      statusBox.textContent = `任务 ${data.id} 已提交`;
      poll(data.id);
    } catch (error) {
      statusBox.textContent = `错误：${error.message}`;
      runState.className = 'run-state failed';
      runState.textContent = '执行失败';
      button.disabled = false;
    }
  });

  async function poll(taskId) {
    try {
      const response = await fetch(`/api/tasks/${taskId}`, {cache:'no-store'});
      const data = await response.json();
      if (!response.ok) throw new Error(data.error || '读取状态失败');
      const names = {queued:'排队中', running:'处理中', completed:'已完成', failed:'失败'};
      statusBox.textContent = `任务状态：${names[data.status] || data.status}` + (data.result ? `；结果：${data.result}` : '');
      logBox.textContent = data.logs.join('\n') || '等待后台开始……';
      logBox.scrollTop = logBox.scrollHeight;
      if (data.status === 'queued' || data.status === 'running') {
        runState.className = 'run-state running';
        runState.textContent = '正在执行……';
        timer = setTimeout(() => poll(taskId), 1000);
      } else {
        if (data.status === 'completed') {
          runState.className = 'run-state completed';
          runState.textContent = '执行完成';
        } else {
          runState.className = 'run-state failed';
          runState.textContent = '执行失败';
        }
        button.disabled = false;
      }
    } catch (error) {
      statusBox.textContent = `状态更新失败：${error.message}`;
      runState.className = 'run-state failed';
      runState.textContent = '状态更新失败';
      button.disabled = false;
    }
  }
</script>
</body>
</html>'''


class RequestHandler(BaseHTTPRequestHandler):
    server_version = "ELDOMTool/1.0"

    def send_bytes(self, content: bytes, content_type: str, status: int = 200) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(content)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(content)

    def send_json(self, data: dict[str, Any], status: int = 200) -> None:
        self.send_bytes(
            json.dumps(data, ensure_ascii=False).encode("utf-8"),
            "application/json; charset=utf-8",
            status,
        )

    def do_GET(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        if path == "/":
            self.send_bytes(INDEX_HTML.encode("utf-8"), "text/html; charset=utf-8")
            return
        match = re.fullmatch(r"/api/tasks/([0-9a-f]{32})", path)
        if match:
            with TASKS_LOCK:
                task = TASKS.get(match.group(1))
            if task is None:
                self.send_json({"error": "任务不存在"}, HTTPStatus.NOT_FOUND)
            else:
                self.send_json(task.snapshot())
            return
        self.send_json({"error": "页面不存在"}, HTTPStatus.NOT_FOUND)

    def do_POST(self) -> None:  # noqa: N802
        if urlparse(self.path).path != "/api/tasks":
            self.send_json({"error": "接口不存在"}, HTTPStatus.NOT_FOUND)
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length <= 0 or length > MAX_REQUEST_BYTES:
                raise ValueError("请求内容为空或过大")
            data = json.loads(self.rfile.read(length).decode("utf-8"))
            if not isinstance(data, dict):
                raise ValueError("请求格式不正确")
            params = validate_inputs(data)
        except (ValueError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            self.send_json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
            return

        task = Task(id=uuid.uuid4().hex, params=params)
        task.log("任务已进入队列")
        with TASKS_LOCK:
            TASKS[task.id] = task
        EXECUTOR.submit(process_task, task)
        self.send_json({"id": task.id, "status": task.status}, HTTPStatus.ACCEPTED)

    def log_message(self, format: str, *args: Any) -> None:
        # 不在终端显示 GET/POST、HTTP 状态码等访问日志。
        pass


def main() -> None:
    global EXECUTOR, CPU_THREAD_BUDGET, GDAL_THREADS_PER_TASK
    parser = argparse.ArgumentParser(description="影像按县界裁切并合并的网页工具")
    parser.add_argument("--host", default="0.0.0.0", help="监听地址，默认 0.0.0.0")
    parser.add_argument("--port", type=int, default=8896, help="监听端口，默认 8000")
    parser.add_argument("--workers", type=int, default=1, help="同时处理的影像任务数，默认 1")
    args = parser.parse_args()
    if not 1 <= args.workers <= MAX_TASK_WORKERS:
        parser.error(f"--workers 必须在 1 到 {MAX_TASK_WORKERS} 之间")

    gdal_version, proj_version = validate_gdal_environment()
    cpu_total, CPU_THREAD_BUDGET, affinity_applied, affinity_message = (
        configure_process_cpu_limit()
    )
    # affinity 生效时，每个任务都可请求完整预算，但所有任务只能在同一组
    # 50% CPU 上运行，因此单任务可用满配额，多任务合计仍不会超出配额。
    if affinity_applied:
        GDAL_THREADS_PER_TASK = CPU_THREAD_BUDGET
    else:
        # 极少数不支持 affinity 的系统退回静态均分，优先保证总量不超预算。
        GDAL_THREADS_PER_TASK = max(1, CPU_THREAD_BUDGET // args.workers)
    EXECUTOR = ThreadPoolExecutor(
        max_workers=args.workers,
        thread_name_prefix="raster-task",
    )
    server = ThreadingHTTPServer((args.host, args.port), RequestHandler)
    print(f"服务已启动：http://127.0.0.1:{args.port}")
    print(f"县界文件：{COUNTY_BOUNDARY}")
    print(f"地理环境：{gdal_version}，PROJ {proj_version}")
    print(f"PROJ 数据目录：{PROJ_DATA_PATH or '使用 GDAL/PROJ 默认搜索路径'}")
    if IGNORED_PROJ_AUX_DB:
        print(f"已忽略无效的 PROJ_AUX_DB 配置：{IGNORED_PROJ_AUX_DB}")
    print(
        f"CPU 配额：{CPU_PERCENT_LIMIT:.0%}（检测到 {cpu_total} 个逻辑 CPU，"
        f"使用 {CPU_THREAD_BUDGET} 个；{affinity_message}）"
    )
    print(
        f"GDAL 内存：每任务 Warp {WARP_MEMORY_LIMIT_BYTES // 1024**3} GiB，"
        f"全局缓存 {GDAL_CACHE_MAX_BYTES // 1024**3} GiB"
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n正在停止服务……")
    finally:
        server.shutdown()
        server.server_close()
        EXECUTOR.shutdown(wait=True, cancel_futures=False)


if __name__ == "__main__":
    main()
