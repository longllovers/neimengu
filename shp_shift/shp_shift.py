"""Stream and translate every geometry in an ESRI Shapefile.

The module is usable as a command-line program and by ``server.py``.  It keeps
attributes/CRS unchanged and only adds dx/dy to geometry coordinates.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import json
import math
import mmap
import os
import shutil
import struct
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Iterable

try:
    import fiona
except ModuleNotFoundError:  # QGIS environments commonly expose osgeo only.
    fiona = None  # type: ignore[assignment]

try:
    from osgeo import gdal
except ModuleNotFoundError:
    gdal = None  # type: ignore[assignment]

try:
    import numpy as np
except ModuleNotFoundError:
    np = None  # type: ignore[assignment]


ProgressCallback = Callable[[dict[str, Any]], None]


def processing_engine_info() -> str:
    detail = ""
    if gdal is not None:
        detail = gdal.VersionInfo("--version")
    elif fiona is not None:
        detail = f"Fiona {fiona.__version__} / GDAL {fiona.__gdal_version__}"
    return f"二进制快速模式{f'（{detail} 可用于回退）' if detail else ''}"


def parse_control_points(path: str | os.PathLike[str]) -> tuple[float, float, float, float]:
    """Read four numbers: original_x original_y correct_x correct_y.

    Separators may be whitespace, commas, Chinese commas, or semicolons.  The
    numbers may be on one line or split across two lines.
    """
    text = Path(path).read_text(encoding="utf-8-sig")
    for separator in (",", "，", ";", "；"):
        text = text.replace(separator, " ")
    parts = text.split()
    if len(parts) != 4:
        raise ValueError(f"控制点文件必须正好包含 4 个数字，实际读取到 {len(parts)} 个")
    values = tuple(float(value) for value in parts)
    if not all(math.isfinite(value) for value in values):
        raise ValueError("控制点坐标必须是有限数字")
    return values  # type: ignore[return-value]


def _shift_coordinates(value: Any, dx: float, dy: float) -> Any:
    if value is None:
        return None
    if isinstance(value, (list, tuple)):
        # A coordinate is a numeric sequence. Preserve Z/M and container type.
        if len(value) >= 2 and isinstance(value[0], (int, float)) and isinstance(value[1], (int, float)):
            shifted = [value[0] + dx, value[1] + dy, *value[2:]]
            return tuple(shifted) if isinstance(value, tuple) else shifted
        shifted = [_shift_coordinates(item, dx, dy) for item in value]
        return tuple(shifted) if isinstance(value, tuple) else shifted
    return value


def _shift_geometry(geometry: dict[str, Any] | None, dx: float, dy: float) -> dict[str, Any] | None:
    if geometry is None:
        return None
    result = dict(geometry)
    if result.get("type") == "GeometryCollection":
        result["geometries"] = [_shift_geometry(dict(item), dx, dy) for item in result.get("geometries", [])]
    else:
        result["coordinates"] = _shift_coordinates(result.get("coordinates"), dx, dy)
    return result


def _shift_batch(batch: list[dict[str, Any] | None], dx: float, dy: float) -> list[dict[str, Any] | None]:
    return [_shift_geometry(geometry, dx, dy) for geometry in batch]


def _batches(source: Iterable[Any], size: int) -> Iterable[tuple[list[dict[str, Any] | None], list[dict[str, Any]]]]:
    geometries: list[dict[str, Any] | None] = []
    properties: list[dict[str, Any]] = []
    for feature in source:
        geometries.append(dict(feature.geometry) if feature.geometry is not None else None)
        properties.append(dict(feature.properties))
        if len(geometries) >= size:
            yield geometries, properties
            geometries, properties = [], []
    if geometries:
        yield geometries, properties


def _read_cpg(shp_path: Path) -> str | None:
    cpg = shp_path.with_suffix(".cpg")
    if not cpg.exists():
        return None
    value = cpg.read_text(encoding="ascii", errors="ignore").strip()
    return value or None


_SHAPEFILE_SIDECARS = (".shp", ".shx", ".dbf", ".prj", ".cpg", ".qix", ".sbn", ".sbx", ".shp.xml")


def _publish_partial(partial_shp: Path, output_shp: Path, overwrite: bool = False) -> None:
    partial_files = list(partial_shp.parent.glob(f"{partial_shp.stem}.*"))
    if not partial_files:
        raise RuntimeError("未找到临时输出文件")
    targets = {
        item: output_shp.with_name(output_shp.stem + item.name[len(partial_shp.stem):])
        for item in partial_files
    }
    existing = [target for target in targets.values() if target.exists()]
    if existing:
        if not overwrite:
            raise FileExistsError(f"输出文件已存在：{existing[0]}")

    backups: dict[Path, Path] = {}
    published: list[Path] = []
    try:
        if overwrite:
            backup_id = uuid.uuid4().hex
            for suffix in _SHAPEFILE_SIDECARS:
                original = output_shp.with_name(output_shp.stem + suffix)
                if original.exists():
                    backup = original.with_name(f".{original.name}.backup-{backup_id}")
                    original.replace(backup)
                    backups[original] = backup
        for item, target in targets.items():
            item.replace(target)
            published.append(target)
    except BaseException:
        for target in published:
            try:
                target.unlink()
            except OSError:
                pass
        for original, backup in backups.items():
            if backup.exists():
                backup.replace(original)
        raise
    else:
        for backup in backups.values():
            try:
                backup.unlink()
            except OSError:
                pass


def _remove_partial(partial_shp: Path) -> None:
    for item in partial_shp.parent.glob(f"{partial_shp.stem}.*"):
        try:
            item.unlink()
        except OSError:
            pass


_SUPPORTED_SHAPE_TYPES = {0, 1, 3, 5, 8, 11, 13, 15, 18, 21, 23, 25, 28, 31}
_POINT_TYPES = {1, 11, 21}
_MULTIPOINT_TYPES = {8, 18, 28}
_PART_TYPES = {3, 5, 13, 15, 23, 25, 31}
_FAST_COPY_SUFFIXES = (".shp", ".shx", ".dbf", ".prj", ".cpg")
_COPY_BUFFER_SIZE = 16 * 1024 * 1024


def _feature_count(source_path: Path) -> int:
    shx = source_path.with_suffix(".shx")
    if shx.is_file() and shx.stat().st_size >= 100:
        payload = shx.stat().st_size - 100
        if payload % 8 == 0:
            return payload // 8
    count = 0
    with source_path.open("rb") as stream:
        stream.seek(100)
        while header := stream.read(8):
            if len(header) != 8:
                raise ValueError("SHP 记录头不完整")
            content_words = struct.unpack_from(">I", header, 4)[0]
            stream.seek(content_words * 2, os.SEEK_CUR)
            count += 1
    return count


def _partial_component(partial_shp: Path, suffix: str) -> Path:
    return partial_shp.with_name(partial_shp.stem + suffix)


def _copy_component(
    source: Path,
    target: Path,
    on_bytes: Callable[[int], None],
    cancel_event: threading.Event | None,
) -> None:
    buffer = bytearray(_COPY_BUFFER_SIZE)
    view = memoryview(buffer)
    with source.open("rb", buffering=0) as reader, target.open("wb", buffering=0) as writer:
        while size := reader.readinto(buffer):
            if cancel_event and cancel_event.is_set():
                raise InterruptedError("任务已取消")
            writer.write(view[:size])
            on_bytes(size)
        writer.flush()
        os.fsync(writer.fileno())
    try:
        shutil.copystat(source, target)
    except OSError:
        pass


def _shift_bbox(buffer: Any, offset: int, dx: float, dy: float) -> None:
    xmin, ymin, xmax, ymax = struct.unpack_from("<4d", buffer, offset)
    struct.pack_into("<4d", buffer, offset, xmin + dx, ymin + dy, xmax + dx, ymax + dy)


def _shift_xy_points(buffer: Any, offset: int, count: int, dx: float, dy: float) -> None:
    if count <= 0:
        return
    if np is not None and count >= 64:
        points = np.frombuffer(buffer, dtype="<f8", count=count * 2, offset=offset).reshape(count, 2)
        points[:, 0] += dx
        points[:, 1] += dy
        return
    for point_index in range(count):
        point_offset = offset + point_index * 16
        x, y = struct.unpack_from("<2d", buffer, point_offset)
        struct.pack_into("<2d", buffer, point_offset, x + dx, y + dy)


def _shift_shp_records(
    shp_path: Path,
    dx: float,
    dy: float,
    total: int,
    progress: ProgressCallback | None,
    cancel_event: threading.Event | None,
    started: float,
    copy_weight: float,
) -> int:
    processed = 0
    transform_started = time.monotonic()
    with shp_path.open("r+b", buffering=0) as stream, mmap.mmap(stream.fileno(), 0, access=mmap.ACCESS_WRITE) as data:
        size = len(data)
        if size < 100 or struct.unpack_from(">I", data, 0)[0] != 9994:
            raise ValueError("输入文件不是有效的 ESRI Shapefile")
        header_type = struct.unpack_from("<I", data, 32)[0]
        if header_type not in _SUPPORTED_SHAPE_TYPES:
            raise ValueError(f"暂不支持 Shapefile 类型：{header_type}")
        if header_type != 0:
            _shift_bbox(data, 36, dx, dy)

        record_offset = 100
        while record_offset < size:
            if record_offset + 8 > size:
                raise ValueError("SHP 记录头超出文件范围")
            content_size = struct.unpack_from(">I", data, record_offset + 4)[0] * 2
            content_offset = record_offset + 8
            record_end = content_offset + content_size
            if content_size < 4 or record_end > size:
                raise ValueError("SHP 记录长度无效")
            shape_type = struct.unpack_from("<I", data, content_offset)[0]
            if shape_type not in _SUPPORTED_SHAPE_TYPES:
                raise ValueError(f"记录包含不支持的 Shapefile 类型：{shape_type}")

            if shape_type in _POINT_TYPES:
                if content_size < 20:
                    raise ValueError("点记录长度无效")
                _shift_xy_points(data, content_offset + 4, 1, dx, dy)
            elif shape_type in _MULTIPOINT_TYPES:
                if content_size < 40:
                    raise ValueError("多点记录长度无效")
                point_count = struct.unpack_from("<I", data, content_offset + 36)[0]
                points_offset = content_offset + 40
                if points_offset + point_count * 16 > record_end:
                    raise ValueError("多点坐标超出记录范围")
                _shift_bbox(data, content_offset + 4, dx, dy)
                _shift_xy_points(data, points_offset, point_count, dx, dy)
            elif shape_type in _PART_TYPES:
                if content_size < 44:
                    raise ValueError("线或面记录长度无效")
                part_count, point_count = struct.unpack_from("<2I", data, content_offset + 36)
                part_item_size = 8 if shape_type == 31 else 4
                points_offset = content_offset + 44 + part_count * part_item_size
                if points_offset + point_count * 16 > record_end:
                    raise ValueError("线或面坐标超出记录范围")
                _shift_bbox(data, content_offset + 4, dx, dy)
                _shift_xy_points(data, points_offset, point_count, dx, dy)

            processed += 1
            record_offset = record_end
            if cancel_event and processed % 4096 == 0 and cancel_event.is_set():
                raise InterruptedError("任务已取消")
            if progress is not None and (processed % 4096 == 0 or processed == total):
                elapsed = max(time.monotonic() - started, 1e-9)
                transform_elapsed = max(time.monotonic() - transform_started, 1e-9)
                rate = processed / transform_elapsed
                fraction = processed / total if total else 1.0
                progress({
                    "status": "running", "stage": "修改几何坐标",
                    "processed": processed, "total": total,
                    "percent": (copy_weight + (1.0 - copy_weight) * fraction) * 100.0,
                    "elapsed_seconds": elapsed, "rate": rate,
                    "byte_rate": 0.0,
                    "eta_seconds": ((total - processed) / rate) if rate > 0 else None,
                    "dx": dx, "dy": dy,
                })
        data.flush()
    if processed != total:
        total = processed
    return total


def _shift_binary_fast(
    source_path: Path,
    partial_path: Path,
    dx: float,
    dy: float,
    progress: ProgressCallback | None,
    cancel_event: threading.Event | None,
) -> int:
    """Copy unchanged components and patch only XY data in SHP/SHX."""
    started = time.monotonic()
    total = _feature_count(source_path)
    components = [
        (source_path.with_suffix(suffix), _partial_component(partial_path, suffix))
        for suffix in _FAST_COPY_SUFFIXES
        if source_path.with_suffix(suffix).is_file()
    ]
    if not any(source.suffix.lower() == ".shp" for source, _target in components):
        raise FileNotFoundError(f"缺少 SHP 文件：{source_path}")
    byte_total = sum(source.stat().st_size for source, _target in components)
    bytes_done = 0
    copy_weight = 0.85

    for source, target in components:
        stage_names = {
            ".shp": "复制几何文件", ".shx": "复制索引文件",
            ".dbf": "复制属性文件", ".prj": "复制投影文件", ".cpg": "复制编码文件",
        }

        def on_bytes(size: int, stage: str = stage_names.get(source.suffix.lower(), "复制文件")) -> None:
            nonlocal bytes_done
            bytes_done += size
            if progress is not None:
                elapsed = max(time.monotonic() - started, 1e-9)
                byte_rate = bytes_done / elapsed
                progress({
                    "status": "running", "stage": stage,
                    "processed": 0, "total": total,
                    "percent": ((bytes_done / byte_total) * copy_weight * 100.0) if byte_total else 0.0,
                    "elapsed_seconds": elapsed, "rate": 0.0,
                    "bytes_processed": bytes_done, "bytes_total": byte_total,
                    "byte_rate": byte_rate,
                    "eta_seconds": ((byte_total - bytes_done) / byte_rate) if byte_rate > 0 else None,
                    "dx": dx, "dy": dy,
                })

        _copy_component(source, target, on_bytes, cancel_event)

    partial_shx = _partial_component(partial_path, ".shx")
    if partial_shx.is_file() and partial_shx.stat().st_size >= 100:
        with partial_shx.open("r+b", buffering=0) as stream, mmap.mmap(stream.fileno(), 100, access=mmap.ACCESS_WRITE) as header:
            header_type = struct.unpack_from("<I", header, 32)[0]
            if header_type != 0:
                _shift_bbox(header, 36, dx, dy)
            header.flush()

    return _shift_shp_records(
        partial_path, dx, dy, total, progress, cancel_event, started, copy_weight,
    )


def _shift_with_gdal(
    source_path: Path,
    partial_path: Path,
    dx: float,
    dy: float,
    progress: ProgressCallback | None,
    cancel_event: threading.Event | None,
) -> int:
    """Run the whole conversion inside GDAL, avoiding Python feature loops."""
    if gdal is None:
        raise RuntimeError("当前环境没有 GDAL Python 绑定")
    gdal.UseExceptions()
    source_ds = gdal.OpenEx(str(source_path), gdal.OF_VECTOR | gdal.OF_READONLY)
    if source_ds is None:
        raise RuntimeError(f"GDAL 无法打开输入文件：{source_path}")
    layer = source_ds.GetLayer(0)
    if layer is None:
        raise RuntimeError("GDAL 未找到矢量图层")
    total = max(0, int(layer.GetFeatureCount()))
    spatial_ref = layer.GetSpatialRef()
    if spatial_ref is None:
        raise RuntimeError("输入 SHP 没有坐标系，无法使用 GDAL 坐标操作")
    source_wkt = spatial_ref.ExportToWkt()
    started = time.monotonic()
    last_processed = -1

    def callback(complete: float, _message: str, _data: Any) -> int:
        nonlocal last_processed
        processed = min(total, max(0, int(complete * total)))
        if progress is not None and processed != last_processed:
            elapsed = max(time.monotonic() - started, 1e-9)
            rate = processed / elapsed
            progress({
                "status": "running", "processed": processed, "total": total,
                "percent": complete * 100.0, "elapsed_seconds": elapsed,
                "rate": rate,
                "eta_seconds": ((total - processed) / rate) if rate > 0 else None,
                "dx": dx, "dy": dy,
            })
            last_processed = processed
        return 0 if cancel_event and cancel_event.is_set() else 1

    options = gdal.VectorTranslateOptions(
        format="ESRI Shapefile",
        srcSRS=source_wkt,
        dstSRS=source_wkt,
        reproject=True,
        coordinateOperation=f"+proj=affine +xoff={dx:.17g} +yoff={dy:.17g}",
        preserveFID=True,
        setCoordPrecision=False,
        callback=callback,
    )
    try:
        output_ds = gdal.VectorTranslate(str(partial_path), source_ds, options=options)
    except RuntimeError as exc:
        if cancel_event and cancel_event.is_set():
            raise InterruptedError("任务已取消") from exc
        raise
    finally:
        source_ds = None
    if output_ds is None:
        if cancel_event and cancel_event.is_set():
            raise InterruptedError("任务已取消")
        raise RuntimeError("GDAL 转换失败")
    output_ds = None
    return total


def shift_shapefile(
    input_path: str | os.PathLike[str],
    output_path: str | os.PathLike[str],
    original: tuple[float, float],
    correct: tuple[float, float],
    *,
    mode: str = "process",
    workers: int | None = None,
    batch_size: int = 1000,
    progress: ProgressCallback | None = None,
    cancel_event: threading.Event | None = None,
    overwrite: bool = False,
) -> dict[str, Any]:
    """Translate a Shapefile without loading the whole dataset into memory."""
    source_path = Path(input_path).expanduser().resolve()
    destination_path = Path(output_path).expanduser().resolve()
    if source_path.suffix.lower() != ".shp" or not source_path.is_file():
        raise FileNotFoundError(f"输入 SHP 不存在：{source_path}")
    if destination_path.suffix.lower() != ".shp":
        raise ValueError("输出路径必须以 .shp 结尾")
    if source_path == destination_path and not overwrite:
        raise ValueError("输出路径不能与输入路径相同")
    if destination_path.exists() and not overwrite:
        raise FileExistsError(f"输出文件已存在：{destination_path}")
    if mode not in {"process", "thread", "single"}:
        raise ValueError("并行模式必须是 process、thread 或 single")
    if batch_size < 1:
        raise ValueError("批大小必须大于 0")

    values = (*original, *correct)
    if not all(math.isfinite(value) for value in values):
        raise ValueError("坐标必须是有限数字")
    dx, dy = correct[0] - original[0], correct[1] - original[1]
    worker_count = max(1, workers or (os.cpu_count() or 1))
    if mode == "single":
        worker_count = 1

    destination_path.parent.mkdir(parents=True, exist_ok=True)
    partial_path = destination_path.with_name(f".{destination_path.stem}.partial-{uuid.uuid4().hex}.shp")
    started = time.monotonic()
    processed = 0

    def report(total: int, status: str = "running") -> None:
        if progress is None:
            return
        elapsed = max(time.monotonic() - started, 1e-9)
        rate = processed / elapsed
        progress({
            "status": status,
            "stage": "已完成" if status == "completed" else "处理要素",
            "processed": processed,
            "total": total,
            "percent": (processed / total * 100.0) if total else 100.0,
            "elapsed_seconds": elapsed,
            "rate": rate,
            "byte_rate": 0.0,
            "eta_seconds": ((total - processed) / rate) if rate > 0 else None,
            "dx": dx,
            "dy": dy,
        })

    try:
        processed = _shift_binary_fast(
            source_path, partial_path, dx, dy, progress, cancel_event,
        )
        _publish_partial(partial_path, destination_path, overwrite=overwrite)
        report(processed, "completed")
        return {
            "input": str(source_path), "output": str(destination_path),
            "features": processed, "dx": dx, "dy": dy,
            "elapsed_seconds": time.monotonic() - started,
            "engine": "BinaryFast",
        }

        if gdal is not None:
            processed = _shift_with_gdal(
                source_path, partial_path, dx, dy, progress, cancel_event,
            )
            _publish_partial(partial_path, destination_path, overwrite=overwrite)
            report(processed, "completed")
            return {
                "input": str(source_path), "output": str(destination_path),
                "features": processed, "dx": dx, "dy": dy,
                "elapsed_seconds": time.monotonic() - started,
                "engine": "GDAL",
            }

        if fiona is None:
            raise RuntimeError("当前 Python 环境既没有 GDAL，也没有 Fiona")
        encoding = _read_cpg(source_path)
        open_read: dict[str, Any] = {}
        open_write: dict[str, Any] = {"driver": "ESRI Shapefile"}
        if encoding:
            open_read["encoding"] = encoding
            open_write["encoding"] = encoding

        with fiona.open(source_path, "r", **open_read) as source:
            total = len(source)
            open_write.update(schema=source.schema.copy(), crs_wkt=source.crs_wkt)
            report(total)
            with fiona.open(partial_path, "w", **open_write) as target:
                if mode == "single":
                    for geometries, properties in _batches(source, batch_size):
                        if cancel_event and cancel_event.is_set():
                            raise InterruptedError("任务已取消")
                        shifted = _shift_batch(geometries, dx, dy)
                        target.writerecords(
                            {"type": "Feature", "geometry": geometry, "properties": props}
                            for geometry, props in zip(shifted, properties)
                        )
                        processed += len(shifted)
                        report(total)
                else:
                    executor_type = (
                        concurrent.futures.ProcessPoolExecutor
                        if mode == "process"
                        else concurrent.futures.ThreadPoolExecutor
                    )
                    with executor_type(max_workers=worker_count) as executor:
                        pending: list[tuple[concurrent.futures.Future[list[dict[str, Any] | None]], list[dict[str, Any]]]] = []
                        for geometries, properties in _batches(source, batch_size):
                            if cancel_event and cancel_event.is_set():
                                raise InterruptedError("任务已取消")
                            pending.append((executor.submit(_shift_batch, geometries, dx, dy), properties))
                            # Bound RAM use and preserve feature order.
                            if len(pending) >= worker_count * 2:
                                future, props_batch = pending.pop(0)
                                shifted = future.result()
                                target.writerecords(
                                    {"type": "Feature", "geometry": geometry, "properties": props}
                                    for geometry, props in zip(shifted, props_batch)
                                )
                                processed += len(shifted)
                                report(total)
                        for future, props_batch in pending:
                            if cancel_event and cancel_event.is_set():
                                raise InterruptedError("任务已取消")
                            shifted = future.result()
                            target.writerecords(
                                {"type": "Feature", "geometry": geometry, "properties": props}
                                for geometry, props in zip(shifted, props_batch)
                            )
                            processed += len(shifted)
                            report(total)

        _publish_partial(partial_path, destination_path, overwrite=overwrite)
        report(processed, "completed")
        return {
            "input": str(source_path), "output": str(destination_path),
            "features": processed, "dx": dx, "dy": dy,
            "elapsed_seconds": time.monotonic() - started,
            "engine": "Fiona",
        }
    except BaseException:
        _remove_partial(partial_path)
        raise


def main() -> None:
    parser = argparse.ArgumentParser(description="依据两个控制点整体平移 Shapefile")
    parser.add_argument("input", help="输入 .shp")
    parser.add_argument("output", help="输出 .shp（不得与输入相同）")
    points = parser.add_mutually_exclusive_group(required=True)
    points.add_argument("--points-file", help="含原始点和正确点四个坐标值的 TXT")
    points.add_argument("--points", nargs=4, type=float, metavar=("X1", "Y1", "X2", "Y2"))
    parser.add_argument("--mode", choices=("process", "thread", "single"), default="process")
    parser.add_argument("--workers", type=int, default=os.cpu_count() or 1)
    parser.add_argument("--batch-size", type=int, default=1000)
    args = parser.parse_args()
    x1, y1, x2, y2 = parse_control_points(args.points_file) if args.points_file else args.points

    def show(data: dict[str, Any]) -> None:
        print(
            f"\r{data['processed']}/{data['total']}  {data['percent']:.2f}%  "
            f"{data['rate']:.0f} 要素/秒",
            end="", flush=True,
        )

    result = shift_shapefile(
        args.input, args.output, (x1, y1), (x2, y2), mode=args.mode,
        workers=args.workers, batch_size=args.batch_size, progress=show,
    )
    print("\n完成：" + json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
