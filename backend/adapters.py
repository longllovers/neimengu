"""把统一任务 API 连接到现有计算脚本。

脚本型业务以子并发数运行，确保原有 argparse 行为和环境不变；少数把业务函数
写在旧 Web 文件中的工具，通过动态导入调用，页面代码不会再参与运行。
"""

from __future__ import annotations

import csv
import importlib.util
import json
import os
import re
import signal
import subprocess
import sys
import tempfile
import threading
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from types import ModuleType
from typing import Any, Callable


ROOT = Path(__file__).resolve().parents[1]
WORKER_EVENT_PREFIX = "@@WORKBENCH_EVENT@@"


def _fallback_python() -> str | None:
    """Locate the optional QGIS Python used after a base-environment failure."""
    configured = str(os.environ.get("GEO_WORKBENCH_PYTHON", "")).strip()
    candidates = []
    if configured:
        configured_path = Path(configured)
        candidates.append(configured_path / "python" if configured_path.is_dir() else configured_path)
    candidates.append(Path("/home/cangling/miniforge3/envs/qgis/bin/python"))
    for candidate in candidates:
        if candidate.is_file():
            return str(candidate)
    return None


PYTHON = sys.executable
FALLBACK_PYTHON = _fallback_python()


def business_environment(python_executable: str) -> dict[str, str]:
    """Build a clean GDAL/PROJ environment for business subprocesses."""
    env = os.environ.copy()
    env["PYTHONUNBUFFERED"] = "1"
    executable = Path(python_executable)
    try:
        prefix = executable.resolve().parent.parent
    except OSError:
        return env
    if executable.resolve() == Path(sys.executable).resolve():
        return env

    env["CONDA_PREFIX"] = str(prefix)
    env["CONDA_DEFAULT_ENV"] = prefix.name
    env["PATH"] = str(prefix / "bin") + os.pathsep + env.get("PATH", "")
    env.pop("PYTHONHOME", None)
    env["PROJ_NETWORK"] = "OFF"

    # A server started from the base Conda environment may leave these pointing
    # at base/share.  They override PROJ's own prefix discovery, so replace them.
    env.pop("PROJ_DATA", None)
    env.pop("PROJ_LIB", None)
    env.pop("GDAL_DATA", None)
    configured_proj = str(os.environ.get("GEO_WORKBENCH_PROJ_DATA", "")).strip()
    proj_candidates = ([Path(configured_proj)] if configured_proj else []) + [
        prefix / "share" / "proj", prefix / "Library" / "share" / "proj",
    ]
    for proj_dir in proj_candidates:
        if (proj_dir / "proj.db").is_file():
            env["PROJ_DATA"] = str(proj_dir)
            env["PROJ_LIB"] = str(proj_dir)
            break
    configured_gdal = str(os.environ.get("GEO_WORKBENCH_GDAL_DATA", "")).strip()
    gdal_candidates = ([Path(configured_gdal)] if configured_gdal else []) + [
        prefix / "share" / "gdal", prefix / "Library" / "share" / "gdal",
    ]
    for gdal_dir in gdal_candidates:
        if gdal_dir.is_dir():
            env["GDAL_DATA"] = str(gdal_dir)
            break
    return env


def convert_network_path(value: Any) -> str:
    if value is None:
        return value

    value = str(value).strip().replace("\\", "/")
    if not value:
        return value

    prefix_mapping = (
        ("//10.10.10.11/data", "/mnt/nas_data"),
        ("//10.10.10.10/4np_share", "/mnt/data/4np/"),
        ("//10.10.10.10/nas_data", "/mnt/nas_data"),
    )
    for windows_prefix, linux_prefix in prefix_mapping:
        if value == windows_prefix:
            return linux_prefix
        if value.startswith(windows_prefix + "/"):
            return linux_prefix.rstrip("/") + value[len(windows_prefix):]
    return value



def path_value(payload: dict[str, Any], name: str) -> str:
    converted = convert_network_path(payload.get(name, ""))
    if not converted:
        return converted
    # Windows 上调试时 Path('/mnt/...').is_absolute() 为 False，但它仍是目标
    # Linux 服务器的绝对路径，不能错误拼到当前项目盘符下。
    if converted.startswith("/"):
        return converted
    candidate = Path(converted)
    if not candidate.is_absolute() and not re.match(r"^[A-Za-z]:[\\/]", converted):
        return str((ROOT / candidate).resolve())
    return converted


def required(payload: dict[str, Any], *names: str) -> None:
    missing = [name for name in names if not str(payload.get(name, "")).strip()]
    if missing:
        raise ValueError("缺少必填参数：" + "、".join(missing))


def flag(command: list[str], enabled: Any, name: str) -> None:
    if bool(enabled):
        command.append(name)


def option(command: list[str], name: str, value: Any) -> None:
    if value is not None and str(value).strip() != "":
        command.extend([name, str(value)])


def load_module(relative: str, alias: str) -> ModuleType:
    path = ROOT / relative
    project_dir = str(path.parent)
    if project_dir not in sys.path:
        sys.path.insert(0, project_dir)
    spec = importlib.util.spec_from_file_location(alias, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"无法加载业务模块：{path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[alias] = module
    spec.loader.exec_module(module)
    return module


@dataclass
class TaskRuntime:
    emit: Callable[[str, dict[str, Any]], None]
    process: subprocess.Popen[str] | None = None
    cancel_requested: bool = False
    lock: threading.Lock = field(default_factory=threading.Lock)

    def log(self, message: Any, level: str = "info") -> None:
        for line in str(message).splitlines() or [""]:
            self.emit("log", {"message": line, "level": level})


def self_check_commands(tool: dict[str, Any], payload: dict[str, Any]) -> list[tuple[Path, list[str]]]:
    folder = ROOT / tool["folder"]
    source = path_value(payload, "source_root")
    work = path_value(payload, "work_root")
    mode = str(payload.get("mode", "skip"))
    required(payload, "source_root", "work_root")
    sample = str(Path(work) / "01生成样本")
    truth = str(Path(work) / "02参考真值")
    measure = str(Path(work) / "03测量值")
    result_name = "04评价精度结果" if tool["id"] in {"farmland-check", "greenhouse-check"} else "04精度评价"
    result = str(Path(work) / result_name)
    boundary = str(folder / "00县边界")
    city_boundary = str(folder / "00市边界")
    common_boundary = tool["id"] in {"wheat-check"}

    if payload.get("stage", "prepare") == "prepare":
        first = [PYTHON, str(folder / "01generate_county_samples_by_city.py"), "--source_root", source, "--output_root", sample, "--mode", mode]
        for key in ("sample_count", "min_distance", "square_size", "min_overlap_ratio"):
            option(first, "--" + key.replace("_", "-"), payload.get(key))
        if common_boundary:
            first.extend(["--input-root", work, "--boundary-ref", boundary, "--city-boundary", city_boundary])
        second = [PYTHON, str(folder / "02fast_clip_samples_and_yangfang.py"), source, sample, work, "--source_root", source, "--delivery-dir", truth, "--mode", mode]
        if common_boundary:
            second.extend(["--boundary-ref", boundary, "--city-boundary-ref", city_boundary])
        return [(folder, first), (folder, second)]

    third = [PYTHON, str(folder / "03fast_clip_samples_and_results.py"), source, sample, work, "--source_root", source, "--delivery-dir", measure, "--mode", mode]
    fourth = [
        PYTHON, str(folder / "04_calculate_accuracy_to_boundary.py"),
        "--truth-root", truth, "--measure-root", measure,
        "--boundary-output", str(Path(result) / "精度评价边界.shp"),
        "--csv-output", str(Path(result) / "精度评价汇总.csv"), "--mode", mode,
    ]
    if common_boundary:
        third.extend(["--boundary-ref", boundary, "--city-boundary-ref", city_boundary])
        fourth.extend(["--boundary-shp", boundary])
    return [(folder, third), (folder, fourth)]


def command_for(tool: dict[str, Any], payload: dict[str, Any]) -> list[tuple[Path, list[str]]]:
    tool_id = tool["id"]
    folder = ROOT / tool["folder"]
    if tool_id.endswith("-check") and tool_id in {"farmland-check", "wheat-check", "greenhouse-check", "multicrop-check"}:
        return self_check_commands(tool, payload)
    if tool_id == "county-clip-shp":
        required(payload, "shp_dir", "boundary", "output_dir")
        cmd = [PYTHON, str(folder / "clip_counties.py"), "--shp-dir", path_value(payload, "shp_dir"), "--boundary", path_value(payload, "boundary"), "--output-dir", path_value(payload, "output_dir"), "--emit-progress-events"]
        for name in ("index", "index_mode", "workers", "index_workers", "cpu_percent"):
            option(cmd, "--" + name.replace("_", "-"), path_value(payload, name) if name == "index" else payload.get(name))
        for county in re.split(r"[,，\s]+", str(payload.get("county", "")).strip()):
            option(cmd, "--county", county)
        flag(cmd, payload.get("overwrite"), "--overwrite")
        output_directory = path_value(payload, "output_dir")
        svg_output = (
            output_directory.rstrip("/") + "/县级裁剪成果预览.svg"
            if output_directory.startswith("/")
            else str(Path(output_directory) / "县级裁剪成果预览.svg")
        )
        render_python = FALLBACK_PYTHON or PYTHON
        render_cmd = [
            render_python, str(folder / "clip_counties_web.py"), "--render-svg",
            "--boundary", path_value(payload, "boundary"),
            "--output-dir", output_directory,
            "--svg-output", svg_output,
        ]
        return [(folder, cmd), (folder, render_cmd)]
    if tool_id == "county-clip-tif":
        required(payload, "imagery_dir", "boundary", "output_dir", "date1", "date2", "resolution")
        date1 = str(payload["date1"]).replace("-", "")
        date2 = str(payload["date2"]).replace("-", "")
        cmd = [PYTHON, str(folder / "clip_counties.py"), "--imagery-dir", path_value(payload, "imagery_dir"), "--boundary", path_value(payload, "boundary"), "--output-dir", path_value(payload, "output_dir"), "--date1", date1, "--date2", date2, "--resolution", str(payload["resolution"]), "--emit-progress-events"]
        for name in ("name_template", "index", "index_mode", "index_workers", "workers", "cpu_percent", "gdal_memory_gb", "pixel_size", "resampling", "overview_max_factor"):
            option(cmd, "--" + name.replace("_", "-"), path_value(payload, name) if name == "index" else payload.get(name))
        for county in re.split(r"[,，\s]+", str(payload.get("county", "")).strip()):
            option(cmd, "--county", county)
        flag(cmd, payload.get("overwrite"), "--overwrite")
        return [(folder, cmd)]
    if tool_id == "delivery-check":
        required(payload, "root", "output_dir")
        output_dir = Path(path_value(payload, "output_dir"))
        cmd = [PYTHON, str(folder / "main.py"), path_value(payload, "root"), "--county-boundary", str(folder / "00县边界" / "15_县边界.shp"), "--province-code", str(payload.get("province_code", "150000")), "--gdb-schema", str(payload.get("gdb_schema", "5-1")), "--zpj-schema", str(payload.get("zpj_schema", "5-4")), "--output", str(output_dir / "检查报告.pdf"), "--json-output", str(output_dir / "检查明细.json")]
        return [(folder, cmd)]
    if tool_id in {"livestock", "livestock2"}:
        required(payload, "shp", "excel", "out_shp", "shp_id_field")
        cmd = [PYTHON, str(folder / "neimeng_xumu_shp_v2.py"), "--shp", path_value(payload, "shp"), "--excel", path_value(payload, "excel"), "--out-shp", path_value(payload, "out_shp"), "--shp-id-field", str(payload["shp_id_field"]), "--join-type", "inner"]
        return [(folder, cmd)]
    if tool_id == "vote":
        required(payload, "shp_dir")
        if payload.get("operation", "run") == "refresh_index":
            cmd = [
                PYTHON, str(folder / "vote.py"),
                "--shp_dir", path_value(payload, "shp_dir"),
                "--refresh-shp-cache-only",
                "--index-concurrency-count", str(payload.get("index_concurrency_count", 4)),
            ]
            return [(folder, cmd)]
        required(payload, "cls_tif", "out_dir")
        temp_dir = Path(tempfile.mkdtemp(prefix="geo-vote-"))
        classification_input = path_value(payload, "cls_tif")
        classification_path = Path(classification_input)
        batch_mode = classification_path.is_dir()
        cmd = [
            PYTHON, str(folder / ("batch_tif_pipeline.py" if batch_mode else "regional_pipeline.py")),
            "--shp_dir", path_value(payload, "shp_dir"),
            "--input-dir" if batch_mode else "--cls_tif", classification_input,
            "--temp-dir", str(temp_dir),
            "--output-dir", path_value(payload, "out_dir"),
            "--MIN_BACKGROUND_THRESHOLD", str(payload.get("background_threshold", 0.5)),
            "--MIN_CLASS_AREA_MU", str(payload.get("min_class_area_mu", 999999999)),
            "--index-concurrency-count", str(payload.get("index_concurrency_count", 4)),
            "--precheck-concurrency-count", str(payload.get("precheck_concurrency_count", 8)),
            "--concurrency-count", str(payload.get("vote_concurrency_count", payload.get("concurrency_count", 4))),
        ]
        option(cmd, "--region-name", payload.get("region_name")); flag(cmd, payload.get("multi_class"), "--multi-class")
        if payload.get("multi_class"):
            option(cmd, "--class-mapping", payload.get("class_mapping"))
        return [(folder, cmd)]
    if tool_id == "shp-overlap":
        required(payload, "input", "output_dir")
        source = Path(path_value(payload, "input"))
        sources = sorted(source.rglob("*.shp")) if source.is_dir() else [source]
        if not sources:
            raise ValueError(f"输入目录中没有 Shapefile：{source}")
        commands = []
        for shp_source in sources:
            relative = shp_source.relative_to(source).with_suffix(".csv") if source.is_dir() else Path(f"{shp_source.stem}.csv")
            output = Path(path_value(payload, "output_dir")) / relative
            cmd = [PYTHON, str(folder / "check_shp.py"), str(shp_source), "--output", str(output), "--min-mu", str(payload.get("min_area_mu", 0.1))]
            option(cmd, "--id-field", payload.get("id_field"))
            option(cmd, "--min-overlap-sqm", payload.get("min_overlap_sqm"))
            if payload.get("merge_small"):
                merge_root = Path(path_value(payload, "merge_output_dir"))
                merge_relative = shp_source.relative_to(source) if source.is_dir() else Path(shp_source.name)
                option(cmd, "--merge-small-output", str(merge_root / merge_relative))
            flag(cmd, payload.get("overwrite"), "--overwrite")
            commands.append((folder, cmd))
        return commands
    if tool_id == "pamid":
        required(payload, "input_path")
        cmd = [PYTHON, str(folder / "build_pamid_folder_multithread.py"), "--input", path_value(payload, "input_path"), "--workers", str(payload.get("workers", 4)), "--max-factor", str(payload.get("max_factor", 256)), "--gdal-cache-mb", str(payload.get("gdal_cache_mb", 512))]
        for value, name in (("recursive", "--recursive"), ("force", "--force"), ("dry_run", "--dry-run")): flag(cmd, payload.get(value), name)
        return [(folder, cmd)]
    return []


def run_commands(runtime: TaskRuntime, commands: list[tuple[Path, list[str]]]) -> dict[str, Any]:
    if not commands:
        raise ValueError("该工具没有可执行命令")
    worker_result: dict[str, Any] | None = None
    for index, (cwd, command) in enumerate(commands, 1):
        if runtime.cancel_requested:
            raise InterruptedError("任务已终止")
        active_command = list(command)
        used_fallback = False
        while True:
            runtime.log(f"步骤 {index}/{len(commands)}" + ("（QGIS 环境重试）" if used_fallback else ""))
            runtime.log("$ " + subprocess.list2cmdline(active_command), "command")
            env = business_environment(active_command[0])
            process_options: dict[str, Any] = {"start_new_session": True} if os.name != "nt" else {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
            process = subprocess.Popen(active_command, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace", bufsize=1, env=env, **process_options)
            output_tail: deque[str] = deque(maxlen=200)
            with runtime.lock:
                runtime.process = process
            if runtime.cancel_requested:
                _terminate_new_process(process)
            assert process.stdout is not None
            for line in process.stdout:
                line = line.rstrip("\r\n")
                output_tail.append(line)
                if line.startswith("@@CLIP_EVENT@@"):
                    try:
                        runtime.emit("progress", json.loads(line[len("@@CLIP_EVENT@@"):]))
                    except json.JSONDecodeError:
                        runtime.log(line, "warning")
                elif line.startswith(WORKER_EVENT_PREFIX):
                    try:
                        event = json.loads(line[len(WORKER_EVENT_PREFIX):])
                        event_type = str(event.pop("type"))
                        if event_type == "result":
                            worker_result = event
                        else:
                            runtime.emit(event_type, event)
                    except (json.JSONDecodeError, KeyError, TypeError):
                        runtime.log(line, "warning")
                else:
                    level = "error" if "ERROR" in line or "错误" in line or "失败" in line else "info"
                    runtime.log(line, level)
            return_code = process.wait()
            with runtime.lock:
                runtime.process = None
            if runtime.cancel_requested:
                raise InterruptedError("任务已终止")
            if return_code == 0:
                break
            can_retry = (
                not used_fallback
                and FALLBACK_PYTHON is not None
                and Path(active_command[0]).resolve() != Path(FALLBACK_PYTHON).resolve()
            )
            if not can_retry:
                raise RuntimeError(f"步骤 {index} 运行失败，退出码 {return_code}")
            runtime.log(
                f"基础 Python 运行失败（退出码 {return_code}），自动切换到 QGIS 环境重试当前步骤。",
                "warning",
            )
            active_command = [FALLBACK_PYTHON, *active_command[1:]]
            used_fallback = True
    return worker_result or {"steps": len(commands)}


def _terminate_new_process(process: subprocess.Popen[str]) -> None:
    """Close the cancellation race between process creation and API signalling."""
    try:
        if os.name == "nt":
            process.send_signal(signal.CTRL_BREAK_EVENT)
        else:
            os.killpg(os.getpgid(process.pid), signal.SIGTERM)
    except (OSError, ProcessLookupError, ValueError):
        try:
            process.terminate()
        except OSError:
            pass


class EmitJob:
    def __init__(self, runtime: TaskRuntime): self.runtime = runtime
    def emit(self, event_type: str, **values: Any) -> None:
        if event_type == "log": self.runtime.log(values.get("message", ""))
        else: self.runtime.emit(event_type, values)


class CropTaskState:
    def __init__(self, runtime: TaskRuntime): self.runtime = runtime
    def log(self, message: str) -> None: self.runtime.log(message)


def run_custom(runtime: TaskRuntime, tool_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    if tool_id == "topology":
        required(payload, "source", "output_dir")
        module = load_module("拓扑检查/app.py", "geo_workbench_topology")
        source = Path(path_value(payload, "source"))
        sources = sorted(source.rglob("*.shp")) if source.is_dir() else [source]
        if not sources or any(not item.is_file() or item.suffix.lower() != ".shp" for item in sources):
            raise ValueError(f"找不到有效的 Shapefile：{source}")
        output_dir = Path(path_value(payload, "output_dir"))
        emit_job = EmitJob(runtime)
        concurrency = max(1, min(int(payload.get("concurrency", 4)), len(sources)))
        runtime.log(f"共发现 {len(sources)} 个 Shapefile，并发数：{concurrency}。")
        with ThreadPoolExecutor(max_workers=concurrency, thread_name_prefix="topology") as executor:
            results = list(executor.map(
                lambda item: module.process_shapefile(item, output_dir, output_dir, emit_job, str(payload.get("source", ""))),
                sources,
            ))
        return {"result": results[0], "results": results}
    if tool_id == "shp-compare":
        required(payload, "folder_a", "folder_b", "output_path")
        module = load_module("两shp-统计-亩数-图斑数/app.py", "geo_workbench_compare")
        rows = module.compare_folders(path_value(payload, "folder_a"), path_value(payload, "folder_b"), progress=runtime.log, max_workers=int(payload.get("workers", 4)))
        output = Path(path_value(payload, "output_path")); output.parent.mkdir(parents=True, exist_ok=True)
        with output.open("w", encoding="utf-8-sig", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=module.CSV_COLUMNS); writer.writeheader(); writer.writerows(rows)
        return {"rows": len(rows), "output": str(output)}
    if tool_id == "shp-shift":
        required(payload, "input_path", "original_x", "original_y", "correct_x", "correct_y")
        overwrite = bool(payload.get("overwrite"))
        if not overwrite:
            required(payload, "output_directory")
        module = load_module("shp_shift/shp_shift.py", "geo_workbench_shp_shift")
        source = Path(path_value(payload, "input_path"))
        output = source if overwrite else Path(path_value(payload, "output_directory")) / source.name
        original = (float(payload["original_x"]), float(payload["original_y"]))
        correct = (float(payload["correct_x"]), float(payload["correct_y"]))
        workers = int(payload.get("workers", 16))
        batch_size = int(payload.get("batch_size", 1000))
        if not 1 <= workers <= 128:
            raise ValueError("并发数必须在 1 到 128 之间")
        if not 1 <= batch_size <= 100000:
            raise ValueError("每批要素数必须在 1 到 100000 之间")
        runtime.log(f"输入文件：{source}")
        runtime.log(f"输出文件：{output}")
        runtime.log(f"位移量：dx={correct[0] - original[0]}，dy={correct[1] - original[1]}")
        result = module.shift_shapefile(
            source, output, original, correct,
            mode="process", workers=workers, batch_size=batch_size,
            progress=lambda values: runtime.emit("progress", values),
            overwrite=overwrite,
        )
        runtime.log(f"偏移完成：{result['features']} 个要素，耗时 {result['elapsed_seconds']:.1f} 秒")
        return result
    if tool_id == "county-crop":
        required(payload, "input_path", "output_root")
        module = load_module("县作物抽取/app.py", "geo_workbench_county_crop")
        config = {"source_path": path_value(payload, "input_path"), "output_dir": path_value(payload, "output_root"), "crop_field": str(payload.get("crop_field", "class")), "crop_names": str(payload.get("crop_names", "")), "concurrency": int(payload.get("concurrency", payload.get("workers", 5))), "overwrite": bool(payload.get("overwrite"))}
        module.split_shapefile(config, CropTaskState(runtime))
        return {"output": config["output_dir"]}
    if tool_id == "copy-txt":
        required(payload, "txt_path", "output_folder")
        module = load_module("copy_txt/copy_txt_httpserver.py", "geo_workbench_copy")
        for message in module.iter_copy_logs(path_value(payload, "txt_path"), path_value(payload, "output_folder"), bool(payload.get("copy_folders"))): runtime.log(message)
        return {"output": path_value(payload, "output_folder")}
    if tool_id == "shp-to-tif":
        required(payload, "input_dir", "output_dir")
        module = load_module("shp_to_tif/shp_to_tif.py", "geo_workbench_shp_to_tif")
        shp_name, tif_name = module.result_names(str(payload.get("shp_name", "merged.shp")), str(payload.get("tif_name", "result.tif")))
        options = module.ProcessingOptions(input_dir=Path(path_value(payload, "input_dir")), rewritten_dir=Path(path_value(payload, "input_dir")), output_dir=Path(path_value(payload, "output_dir")), merged_shp_name=shp_name, output_tif_name=tif_name, class_value=int(payload.get("class_value", 2)), overwrite=bool(payload.get("overwrite")), tif_only=False, supersample=8, shp_threads=int(payload.get("shp_threads", 10)), source_root_text=str(payload.get("input_dir", "")))
        module.run_processing(options, runtime.log)
        return {"output": path_value(payload, "output_dir")}
    raise ValueError(f"不支持的工具：{tool_id}")


CUSTOM_TOOLS = {"topology", "shp-compare", "shp-shift", "county-crop", "copy-txt", "shp-to-tif"}


def execute(runtime: TaskRuntime, tool: dict[str, Any], payload: dict[str, Any]) -> dict[str, Any]:
    if tool["id"] in CUSTOM_TOOLS:
        payload_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w", encoding="utf-8", suffix=".json", prefix="geo-workbench-",
                delete=False,
            ) as stream:
                json.dump(payload, stream, ensure_ascii=False)
                payload_path = Path(stream.name)
            command = [PYTHON, "-m", "backend.custom_worker", tool["id"], str(payload_path)]
            return run_commands(runtime, [(ROOT, command)])
        finally:
            if payload_path is not None:
                payload_path.unlink(missing_ok=True)
    return run_commands(runtime, command_for(tool, payload))
