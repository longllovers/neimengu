from pathlib import Path
import os
import subprocess
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs
from html import escape
import threading
import json
import shutil
import tempfile
import time
import stat
from datetime import datetime


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


BASE_DIR = Path(__file__).resolve().parent

# 默认路径，可以按你的实际情况修改
DEFAULT_DF_PATH = r"\\10.10.10.11\data\北京预测结果传递\地块结果\所有地块结果最新-去除接边"

DEFAULT_REGION_NAME = ""
DEFAULT_INPUT1_PATH = ""
DEFAULT_OUTPUT2_PATH = ""
DEFAULT_MIN_BACKGROUND_THRESHOLD = "0.5"
DEFAULT_MIN_CLASS_AREA_MU = "999999999"
DEFAULT_CONCURRENCY_COUNT = "4"
DEFAULT_CLASS_MAPPING = (
    "1=春玉米\n"
    "2=中稻\n"
    "3=大豆\n"
    "4=春小麦\n"
    "5=马铃薯\n"
    "6=油菜\n"
    "7=向日葵籽\n"
    "0=背景或无有效分类"
)

VOTE_SCRIPT = BASE_DIR / "vote.py"
MERGE_SCRIPT = BASE_DIR / "merge_geodata.py"
REGIONAL_PIPELINE_SCRIPT = BASE_DIR / "regional_pipeline.py"
BATCH_TIF_PIPELINE_SCRIPT = BASE_DIR / "batch_tif_pipeline.py"
TEMP_OUTPUT_ROOT = BASE_DIR / "temp_vote_outputs"

MAX_CONCURRENT_TASKS = 3
TASK_SEMAPHORE = threading.BoundedSemaphore(MAX_CONCURRENT_TASKS)
SHP_INDEX_REFRESH_LOCK = threading.Lock()
TEMP_DIR_LOCK = threading.Lock()
ACTIVE_TEMP_DIRS = set()
TEMP_RETENTION_DAYS = 5
TEMP_RETENTION_SECONDS = TEMP_RETENTION_DAYS * 24 * 60 * 60
TIF_SUFFIXES = {".tif", ".tiff"}


def hidden_subprocess_options():
    """在 Windows 上启动 Python 子进程时不创建控制台窗口。"""
    if os.name != "nt":
        return {}
    startupinfo = subprocess.STARTUPINFO()
    startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    startupinfo.wShowWindow = subprocess.SW_HIDE
    return {
        "startupinfo": startupinfo,
        "creationflags": subprocess.CREATE_NO_WINDOW,
    }


def collect_tif_files(input_value):
    """接受单个 TIF 或文件夹；文件夹模式递归查找所有 TIF。"""
    input_text = str(input_value or "").strip()
    if not input_text:
        raise ValueError("请输入单个 TIF 路径或包含 TIF 的文件夹路径。")
    input_path = Path(input_text).expanduser()
    if input_path.is_file():
        if input_path.suffix.lower() not in TIF_SUFFIXES:
            raise ValueError(f"输入文件不是 TIF: {input_path}")
        return input_path, [input_path]
    if input_path.is_dir():
        tif_files = sorted(
            (path for path in input_path.rglob("*") if path.is_file() and path.suffix.lower() in TIF_SUFFIXES),
            key=lambda path: str(path).lower(),
        )
        if not tif_files:
            raise ValueError(f"输入文件夹及其子文件夹中没有找到 TIF: {input_path}")
        return input_path, tif_files
    raise ValueError(f"输入 TIF 或文件夹不存在: {input_path}")


def create_temp_output_dir():
    """清理超过保留期的旧目录，再创建不会与并发请求冲突的新目录。"""
    with TEMP_DIR_LOCK:
        TEMP_OUTPUT_ROOT.mkdir(parents=True, exist_ok=True)
        now = time.time()
        for old_dir in TEMP_OUTPUT_ROOT.iterdir():
            if not old_dir.is_dir() or old_dir.resolve() in ACTIVE_TEMP_DIRS:
                continue
            try:
                age_seconds = now - old_dir.stat().st_mtime
            except FileNotFoundError:
                continue
            if age_seconds < TEMP_RETENTION_SECONDS:
                continue
            try:
                cleanup_temp_output_dir(old_dir)
                print(f"[超过 {TEMP_RETENTION_DAYS} 天的临时目录已清理] {old_dir}", flush=True)
            except Exception as exc:
                print(f"[过期临时目录清理失败] {old_dir}: {exc}", flush=True)

        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        temp_dir = Path(
            tempfile.mkdtemp(
                prefix=f"vote_output_{timestamp}_",
                dir=TEMP_OUTPUT_ROOT,
            )
        )
        ACTIVE_TEMP_DIRS.add(temp_dir.resolve())
        return temp_dir


def release_temp_output_dir(temp_dir):
    """任务结束后解除占用标记；目录保留五天后再清理。"""
    with TEMP_DIR_LOCK:
        ACTIVE_TEMP_DIRS.discard(Path(temp_dir).resolve())


def cleanup_temp_output_dir(temp_dir):
    """只允许删除临时根目录下的单次运行目录，并重试短暂的文件占用。"""
    temp_path = Path(temp_dir).resolve()
    temp_root = TEMP_OUTPUT_ROOT.resolve()
    if temp_path.parent != temp_root:
        raise ValueError(f"拒绝删除临时根目录之外的路径: {temp_path}")

    def remove_readonly(func, path, _exc_info):
        os.chmod(path, stat.S_IWRITE)
        func(path)

    last_error = None
    for attempt in range(1, 6):
        if not temp_path.exists():
            return
        try:
            shutil.rmtree(temp_path, onerror=remove_readonly)
            return
        except OSError as exc:
            last_error = exc
            if attempt < 5:
                time.sleep(0.2 * attempt)

    raise OSError(f"重试后仍无法删除临时目录: {temp_path}") from last_error


def html_escape(value):
    if value is None:
        return ""
    return escape(str(value), quote=True)


def form_value(form_data, name, default):
    values = form_data.get(name, [""])
    value = values[0].strip() if values else ""
    return value or default


def current_values(form_data):
    return {
        "df_path": form_value(form_data, "df_path", DEFAULT_DF_PATH),
        "region_name": form_value(form_data, "region_name", DEFAULT_REGION_NAME),
        "input1_path": form_value(form_data, "input1_path", DEFAULT_INPUT1_PATH),
        "output2_path": form_value(form_data, "output2_path", DEFAULT_OUTPUT2_PATH),
        "min_background_threshold": form_value(form_data, "min_background_threshold", DEFAULT_MIN_BACKGROUND_THRESHOLD),
        "min_class_area_mu": form_value(form_data, "min_class_area_mu", DEFAULT_MIN_CLASS_AREA_MU),
        "concurrency_count": form_value(form_data, "concurrency_count", DEFAULT_CONCURRENCY_COUNT),
        "multi_class": form_data.get("multi_class", [""])[0] == "1",
        "class_mapping": form_value(form_data, "class_mapping", DEFAULT_CLASS_MAPPING),
    }


def normalize_values(values):
   
    return {
        "df_path": convert_network_path(values["df_path"]),
        "region_name": values["region_name"].strip(),
        "input1_path": convert_network_path(values["input1_path"]),
        "output2_path": convert_network_path(values["output2_path"]),
        "min_background_threshold": values["min_background_threshold"],
        "min_class_area_mu": values["min_class_area_mu"],
        "concurrency_count": values["concurrency_count"],
        "multi_class": bool(values.get("multi_class", False)),
        "class_mapping": values.get("class_mapping", "").strip(),
    }


def command_to_string(command):
    result = []
    for part in command:
        part = str(part)
        if " " in part:
            result.append(f'"{part}"')
        else:
            result.append(part)
    return " ".join(result)


def run_command(name, command):
    print("\n" + "=" * 80, flush=True)
    print(f"[开始运行] {name}", flush=True)
    print("-" * 80, flush=True)

    stdout_lines = []
    stderr_lines = []
    output_lines = []

    def read_stream(stream, storage, prefix):
        try:
            for line in iter(stream.readline, ""):
                storage.append(line)
                output_lines.append(f"{prefix}{line}")
                print(f"{prefix}{line}", end="", flush=True)
        finally:
            stream.close()

    try:
        process = subprocess.Popen(
            command,
            cwd=BASE_DIR,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            env={
                **os.environ,
                "PYTHONIOENCODING": "utf-8",
                "PYTHONUNBUFFERED": "1",
            },
            **hidden_subprocess_options(),
        )

        stdout_thread = threading.Thread(
            target=read_stream,
            args=(process.stdout, stdout_lines, "[stdout] "),
            daemon=True,
        )

        stderr_thread = threading.Thread(
            target=read_stream,
            args=(process.stderr, stderr_lines, "[stderr] "),
            daemon=True,
        )

        stdout_thread.start()
        stderr_thread.start()

        returncode = process.wait()

        stdout_thread.join()
        stderr_thread.join()

        print("-" * 80, flush=True)

        if returncode == 0:
            end_message = f"[运行结束] {name} 执行成功，返回码：{returncode}"
        else:
            end_message = f"[运行结束] {name} 执行失败，返回码：{returncode}"

        print(end_message, flush=True)
        print("=" * 80 + "\n", flush=True)

        return {
            "name": name,
            "command": "",
            "returncode": returncode,
            "stdout": "".join(stdout_lines),
            "stderr": "".join(stderr_lines),
            "output": "".join(output_lines),
        }
    except Exception as exc:
        error_message = str(exc)
        print("-" * 80, flush=True)
        print(f"[运行异常] {name}", flush=True)
        print(f"[异常信息] {error_message}", flush=True)
        print("=" * 80 + "\n", flush=True)
        return {
            "name": name,
            "command": "",
            "returncode": -1,
            "stdout": "",
            "stderr": error_message,
            "output": f"[运行异常] {name}\n[异常信息] {error_message}",
        }


def run_command_stream(handler, name, command, send_done=True):
    """运行命令，并用 NDJSON 将命令输出实时发送给浏览器。"""
    write_lock = threading.Lock()
    client_connected = True

    def send_event(event_type, **data):
        nonlocal client_connected
        if not client_connected:
            return
        payload = {"type": event_type, **data}
        line = (json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8")
        try:
            with write_lock:
                handler.wfile.write(line)
                handler.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            client_connected = False

    print("\n" + "=" * 80, flush=True)
    print(f"[开始运行] {name}", flush=True)
    print("-" * 80, flush=True)
    send_event("start", name=name)

    try:
        process = subprocess.Popen(
            command,
            cwd=BASE_DIR,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            env={
                **os.environ,
                "PYTHONIOENCODING": "utf-8",
                "PYTHONUNBUFFERED": "1",
            },
            **hidden_subprocess_options(),
        )

        def forward_stream(stream, prefix):
            try:
                for line in iter(stream.readline, ""):
                    if prefix == "[stdout] " and line.startswith("__AREA_SUMMARY__"):
                        try:
                            summary = json.loads(line[len("__AREA_SUMMARY__"):])
                            send_event("area_summary", **summary)
                        except json.JSONDecodeError as exc:
                            text = f"[面积统计解析失败] {exc}\n"
                            print(text, end="", flush=True)
                            send_event("output", text=text)
                        continue
                    text = f"{prefix}{line}"
                    print(text, end="", flush=True)
                    send_event("output", text=text)
            finally:
                stream.close()

        stdout_thread = threading.Thread(
            target=forward_stream, args=(process.stdout, "[stdout] "), daemon=True
        )
        stderr_thread = threading.Thread(
            target=forward_stream, args=(process.stderr, "[stderr] "), daemon=True
        )
        stdout_thread.start()
        stderr_thread.start()
        returncode = process.wait()
        stdout_thread.join()
        stderr_thread.join()

        print("-" * 80, flush=True)
        print(f"[运行结束] {name}，返回码：{returncode}", flush=True)
        print("=" * 80 + "\n", flush=True)
        if send_done:
            send_event("done", name=name, returncode=returncode)
        return returncode
    except Exception as exc:
        error_text = f"[运行异常] {name}\n[异常信息] {exc}\n"
        print(error_text, flush=True)
        send_event("output", text=error_text)
        if send_done:
            send_event("done", name=name, returncode=-1)
        return -1

def build_result_html(result):
    """
    根据运行结果生成网页中的结果区域。
    stdout 和 stderr 不再分开显示，统一显示为一个运行输出。
    """
    if not result:
        return ""

    if result["returncode"] == 0:
        badge_class = "success"
        badge_text = f"成功，返回码 {result['returncode']}"
    else:
        badge_class = "error"
        badge_text = f"失败，返回码 {result['returncode']}"

    output = result.get("output", "")

    if not output:
        output = '<span class="empty">没有运行输出</span>'
    else:
        output = html_escape(output)

    return f"""
    <section class="card results">
        <div class="result-head">
            <h2>运行结果：{html_escape(result["name"])}</h2>
            <span class="badge {badge_class}">{html_escape(badge_text)}</span>
        </div>

        <div class="section-title">运行输出</div>
        <pre>{output}</pre>
    </section>
    """


def build_html(values, result=None):
    defaults = {
        "df_path": DEFAULT_DF_PATH,
        "region_name": DEFAULT_REGION_NAME,
        "input1_path": DEFAULT_INPUT1_PATH,
        "output2_path": DEFAULT_OUTPUT2_PATH,
        "min_background_threshold": DEFAULT_MIN_BACKGROUND_THRESHOLD,
        "min_class_area_mu": DEFAULT_MIN_CLASS_AREA_MU,
        "concurrency_count": DEFAULT_CONCURRENCY_COUNT,
        "class_mapping": DEFAULT_CLASS_MAPPING,
    }

    pipeline_status_text = ""
    pipeline_status_class = "run-status"

    return f"""
<!DOCTYPE html>
<html lang="zh-CN">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Python 脚本运行面板</title>
    <style>
        :root {{
            color-scheme: light;
            --bg: #eef2f7;
            --panel: #ffffff;
            --text: #172033;
            --muted: #64748b;
            --line: #d8dee9;
            --vote: #2563eb;
            --vote-dark: #1d4ed8;
            --merge: #0f766e;
            --merge-dark: #115e59;
            --danger: #dc2626;
            --success: #15803d;
            --console: #0b1220;
            --console-text: #dbeafe;
        }}

        * {{
            box-sizing: border-box;
        }}

        body {{
            margin: 0;
            min-height: 100vh;
            background:
                radial-gradient(circle at top left, rgba(37, 99, 235, 0.14), transparent 34rem),
                linear-gradient(135deg, #f8fafc 0%, var(--bg) 100%);
            color: var(--text);
            font-family: Arial, "Microsoft YaHei", sans-serif;
        }}

        .page {{
            width: min(1480px, calc(100% - 48px));
            margin: 0 auto;
            padding: 38px 0;
        }}

        .header {{
            margin-bottom: 22px;
        }}

        .header h1 {{
            margin: 0 0 8px;
            font-size: 30px;
            line-height: 1.25;
        }}

        .layout {{
            display: grid;
            grid-template-columns: minmax(520px, 0.95fr) minmax(0, 1.05fr);
            gap: 24px;
            align-items: start;
        }}

        .run-form {{
            min-width: 0;
        }}

        .card {{
            background: rgba(255, 255, 255, 0.96);
            border: 1px solid var(--line);
            border-radius: 8px;
            box-shadow: 0 16px 40px rgba(15, 23, 42, 0.08);
            padding: 22px;
        }}

        .card h2 {{
            margin: 0 0 4px;
            font-size: 20px;
        }}

        .card .hint {{
            margin: 0 0 18px;
            color: var(--muted);
            font-size: 13px;
        }}

        .field {{
            margin-top: 14px;
        }}

        label {{
            display: flex;
            justify-content: space-between;
            gap: 10px;
            margin-bottom: 7px;
            font-weight: 700;
            font-size: 14px;
        }}

        label span {{
            color: var(--muted);
            font-weight: 400;
            white-space: nowrap;
        }}

        input, textarea {{
            width: 100%;
            border: 1px solid #cbd5e1;
            border-radius: 8px;
            padding: 0 12px;
            color: var(--text);
            background: #f8fafc;
            font-size: 14px;
            outline: none;
        }}

        input {{ height: 42px; }}
        textarea {{ min-height: 120px; padding: 10px 12px; resize: vertical; font-family: Consolas, "Microsoft YaHei", monospace; }}

        input:focus, textarea:focus {{
            border-color: #2563eb;
            background: #fff;
            box-shadow: 0 0 0 4px rgba(37, 99, 235, 0.12);
        }}

        .checkbox-row {{
            display: flex;
            align-items: center;
            justify-content: flex-start;
            gap: 9px;
            margin: 10px 0 0;
            color: var(--text);
            font-weight: 400;
            cursor: pointer;
        }}

        .checkbox-row input[type="checkbox"] {{
            width: 18px;
            height: 18px;
            margin: 0;
            padding: 0;
            flex: 0 0 auto;
            accent-color: var(--vote);
        }}

        .checkbox-row span {{
            color: var(--muted);
            font-size: 13px;
            white-space: normal;
        }}

        .actions {{
            margin-top: 20px;
            display: grid;
            grid-template-columns: repeat(2, minmax(0, 1fr));
            gap: 12px;
        }}

        .parameter-grid {{
            display: grid;
            grid-template-columns: repeat(2, minmax(0, 1fr));
            gap: 16px;
            margin-top: 2px;
        }}

        button {{
            width: 100%;
            min-height: 44px;
            border: 0;
            border-radius: 8px;
            color: white;
            font-size: 15px;
            font-weight: 700;
            cursor: pointer;
        }}

        button:hover {{
            transform: translateY(-1px);
            box-shadow: 0 10px 20px rgba(15, 23, 42, 0.14);
        }}

        button:disabled {{
            opacity: 0.75;
            cursor: not-allowed;
            transform: none;
        }}

        .btn-vote {{
            background: var(--vote);
        }}

        .btn-vote:hover {{
            background: var(--vote-dark);
        }}

        .btn-merge {{
            background: var(--merge);
        }}

        .btn-merge:hover {{
            background: var(--merge-dark);
        }}

        .run-status {{
            margin-top: 12px;
            min-height: 24px;
            font-size: 14px;
            font-weight: 700;
        }}

        .run-status.running {{
            color: #2563eb;
        }}

        .run-status.done {{
            color: var(--success);
        }}

        .results {{
            min-width: 0;
            margin-top: 0;
            position: sticky;
            top: 24px;
        }}

        .result-head {{
            display: flex;
            align-items: center;
            justify-content: space-between;
            gap: 12px;
            margin-bottom: 12px;
        }}

        .result-head h2 {{
            margin: 0;
            font-size: 20px;
        }}

        .badge {{
            display: inline-flex;
            align-items: center;
            min-height: 26px;
            border-radius: 999px;
            padding: 0 10px;
            font-size: 13px;
            font-weight: 700;
        }}

        .badge.success {{
            background: #dcfce7;
            color: var(--success);
        }}

        .badge.error {{
            background: #fee2e2;
            color: var(--danger);
        }}

        .section-title {{
            margin: 16px 0 7px;
            color: #334155;
            font-size: 14px;
            font-weight: 700;
        }}

        pre {{
            margin: 0;
            height: 380px;
            max-height: 48vh;
            min-height: 260px;
            overflow: auto;
            border-radius: 8px;
            padding: 14px;
            background: var(--console);
            color: var(--console-text);
            font-family: Consolas, "Courier New", monospace;
            font-size: 13px;
            line-height: 1.55;
            white-space: pre-wrap;
            word-break: break-word;
        }}

        .area-summary {{
            display: none;
            width: 100%;
            min-width: 0;
            margin-top: 18px;
        }}

        .area-summary .result-head {{
            align-items: flex-start;
        }}

        .area-csv-path {{
            min-width: 0;
            max-width: 65%;
            overflow: hidden;
            text-align: right;
            text-overflow: ellipsis;
            white-space: nowrap;
        }}

        .area-summary-scroll {{
            width: 100%;
            max-height: 230px;
            overflow: auto;
            border: 1px solid #d9e0e7;
            border-radius: 8px;
            background: #fff;
            scrollbar-gutter: stable;
        }}

        .area-summary-table {{
            width: 100%;
            min-width: 360px;
            border-collapse: collapse;
        }}

        .area-summary-table thead th {{
            position: sticky;
            top: 0;
            z-index: 1;
            padding: 10px;
            border-bottom: 1px solid #d9e0e7;
            background: #f8fafc;
        }}

        .empty {{
            color: #94a3b8;
            font-style: italic;
        }}

        @media (max-width: 1000px) {{
            .page {{
                width: min(100% - 32px, 760px);
                padding: 22px 0;
            }}

            .layout {{
                grid-template-columns: 1fr;
            }}

            .results {{
                position: static;
            }}

            pre {{
                height: 320px;
                max-height: 48vh;
                min-height: 220px;
            }}

            .header h1 {{
                font-size: 24px;
            }}
        }}

        @media (max-width: 560px) {{
            .page {{
                width: min(100% - 22px, 760px);
            }}

            .parameter-grid {{
                grid-template-columns: 1fr;
                gap: 0;
            }}
        }}
    </style>
</head>
<body>
<main class="page">
    <header class="header">
        <h1>Python 脚本运行面板</h1>
    </header>

    <section class="layout">
        <form class="card run-form" method="post" data-status-id="pipeline_status" onsubmit="return false;">
            <input type="hidden" name="action" value="run_all">
            <h2>投票并合并</h2>
            <p class="hint">输入可为单个 TIF，也可为文件夹；文件夹模式会递归处理全部 TIF，并为每个 TIF 使用独立的 Python 子进程。</p>

            <div class="field">
                <label for="df_path">输入 SHP 根目录 <span>首次运行会扫描并缓存</span></label>
                <input id="df_path" type="text" name="df_path"
                       value="{html_escape(values["df_path"])}"
                       placeholder="{html_escape(defaults["df_path"])}">
            </div>

            <div class="field">
                <label for="region_name">市/县名称 <span>可不填；多个名称用；分隔，例如：呼和浩特市；包头市</span></label>
                <input id="region_name" type="text" name="region_name"
                       value="{html_escape(values["region_name"])}"
                       placeholder="不填则按 TIF 范围内的全部县输出">
            </div>

            <div class="field">
                <label for="input1_path">输入 TIF 或文件夹</label>
                <input id="input1_path" type="text" name="input1_path"
                       value="{html_escape(values["input1_path"])}"
                       placeholder="请输入单个 TIF 路径或包含 TIF 的文件夹路径">
                <p class="hint">文件夹模式递归查找 .tif/.tiff；每个 TIF 独立投票，完成后按市/县统一合并，最终目录不创建 TIF 子目录。</p>
            </div>

            <div class="field">
                <label class="checkbox-row" for="multi_class">
                    <input id="multi_class" type="checkbox" name="multi_class" value="1" {"checked" if values.get("multi_class") else ""}>
                    启用多分类
                </label>
                <div id="class_mapping_panel">
                    <label for="class_mapping">多分类映射 <span>每行：栅格值=类别名称</span></label>
                    <textarea id="class_mapping" name="class_mapping" rows="8" placeholder="1=春玉米&#10;2=中稻">{html_escape(values.get("class_mapping", defaults["class_mapping"]))}</textarea>
                    <p class="hint">读取输入 TIF 的 class 类别值并按此映射对应，例如 TIF class=1 对应春玉米。取消勾选时按 0=背景、1=目标类别的二分类运行。</p>
                </div>
            </div>

            <div class="parameter-grid">
                <div class="field">
                    <label for="min_background_threshold">背景像元比例阈值 <span>默认：{html_escape(defaults["min_background_threshold"])}</span></label>
                    <input id="min_background_threshold" type="number" name="min_background_threshold" min="0" max="1" step="0.01" required value="{html_escape(values["min_background_threshold"])}">
                </div>
                <div class="field">
                    <label for="min_class_area_mu">分类面积保留阈值/亩 <span>默认：{html_escape(defaults["min_class_area_mu"])}</span></label>
                    <input id="min_class_area_mu" type="number" name="min_class_area_mu" min="0" step="0.01" required value="{html_escape(values["min_class_area_mu"])}">
                </div>
                <div class="field">
                    <label for="concurrency_count">并发数 <span>默认：{html_escape(defaults["concurrency_count"])}</span></label>
                    <input id="concurrency_count" type="number" name="concurrency_count" min="1" max="96" step="1" required value="{html_escape(values["concurrency_count"])}">
                </div>
            </div>

            <div class="field">
                <label for="output2_path">最终输出文件夹</label>
                <input id="output2_path" type="text" name="output2_path"
                       value="{html_escape(values["output2_path"])}"
                       placeholder="请输入保存各市/县 SHP 的文件夹">
            </div>

            <div class="actions">
                <button class="btn-vote" type="submit" data-action="run_all">运行投票并合并</button>
                <button class="btn-merge" type="submit" data-action="refresh_shp_index">刷新 SHP 索引</button>
            </div>

            <div id="pipeline_status" class="{pipeline_status_class}">{pipeline_status_text}</div>
        </form>

        <section id="result_panel" class="card results">
            <div class="result-head">
                <h2 id="result_title">运行输出</h2>
                <span id="result_badge" class="badge">等待运行</span>
            </div>

            <pre id="run_output">尚无运行输出</pre>
            <div id="area_summary" class="area-summary">
                <div class="result-head">
                    <h2>市/县面积统计</h2>
                    <span id="area_csv_path" class="hint area-csv-path" title=""></span>
                </div>
                <div class="area-summary-scroll">
                    <table class="area-summary-table">
                        <thead><tr><th style="text-align:left;">市/县名字</th><th style="text-align:right;">亩数</th></tr></thead>
                        <tbody id="area_summary_body"></tbody>
                    </table>
                </div>
            </div>
        </section>
    </section>
</main>

<script>
    var runOutputEl = document.getElementById("run_output");
    var followRunOutput = true;
    var multiClassEl = document.getElementById("multi_class");
    var classMappingPanelEl = document.getElementById("class_mapping_panel");

    function updateClassMappingVisibility() {{
        classMappingPanelEl.style.display = multiClassEl.checked ? "block" : "none";
    }}
    multiClassEl.addEventListener("change", updateClassMappingVisibility);
    updateClassMappingVisibility();

    function isOutputNearBottom(element) {{
        return element.scrollHeight - element.scrollTop - element.clientHeight <= 48;
    }}

    runOutputEl.addEventListener("scroll", function() {{
        followRunOutput = isOutputNearBottom(runOutputEl);
    }});

    function scrollOutputToLatest(element) {{
        if (followRunOutput) {{
            element.scrollTop = element.scrollHeight;
        }}
    }}

    document.querySelectorAll(".run-form").forEach(function(form) {{
        form.addEventListener("submit", async function(event) {{
            event.preventDefault();

            var statusId = form.getAttribute("data-status-id");
            var statusEl = document.getElementById(statusId);
            var button = event.submitter || form.querySelector("button");
            var requestedAction = button ? button.getAttribute("data-action") : "run_all";
            var originalButtonText = button ? button.textContent : "";
            var titleEl = document.getElementById("result_title");
            var badgeEl = document.getElementById("result_badge");
            var outputEl = document.getElementById("run_output");
            var concurrencyCount = form.querySelector('[name="concurrency_count"]');
            var concurrencyText = concurrencyCount ? concurrencyCount.value : "1";
            var areaSummaryEl = document.getElementById("area_summary");
            var areaSummaryBodyEl = document.getElementById("area_summary_body");
            var areaCsvPathEl = document.getElementById("area_csv_path");

            if (statusEl) {{
                statusEl.textContent = "正在运行，并发数：" + concurrencyText;
                statusEl.className = "run-status running";
            }}

            if (button) {{
                button.disabled = true;
                button.textContent = "正在运行...";
            }}

            titleEl.textContent = "运行输出";
            badgeEl.textContent = "正在运行";
            badgeEl.className = "badge";
            outputEl.textContent = "程序正在运行，请稍候...";
            areaSummaryEl.style.display = "none";
            areaSummaryBodyEl.textContent = "";
            areaCsvPathEl.textContent = "";
            followRunOutput = true;
            scrollOutputToLatest(outputEl);

            try {{
                var formParams = new URLSearchParams(new FormData(form));
                formParams.set("action", requestedAction || "run_all");
                var response = await fetch(form.action || window.location.href, {{
                    method: "POST",
                    headers: {{
                        "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
                    }},
                    body: formParams,
                }});
                if (!response.ok) {{
                    var errorData = await response.json().catch(function() {{ return {{}}; }});
                    throw new Error(errorData.error || ("请求失败，HTTP 状态码：" + response.status));
                }}

                var reader = response.body.getReader();
                var decoder = new TextDecoder("utf-8");
                var buffer = "";
                var hasOutput = false;

                while (true) {{
                    var chunk = await reader.read();
                    if (chunk.done) break;
                    buffer += decoder.decode(chunk.value, {{stream: true}});
                    var lines = buffer.split("\\n");
                    buffer = lines.pop();

                    lines.forEach(function(line) {{
                        if (!line.trim()) return;
                        var eventData = JSON.parse(line);
                        if (eventData.type === "start") {{
                            titleEl.textContent = "运行输出：" + eventData.name;
                            badgeEl.textContent = "正在运行";
                            badgeEl.className = "badge";
                            if (statusEl) {{
                                statusEl.textContent = "正在运行：" + eventData.name + "（并发数：" + concurrencyText + "）";
                                statusEl.className = "run-status running";
                            }}
                            if (hasOutput) {{
                                outputEl.textContent += "\\n[开始运行] " + eventData.name + "\\n";
                            }} else {{
                                outputEl.textContent = "[开始运行] " + eventData.name + "\\n";
                            }}
                            hasOutput = true;
                        }} else if (eventData.type === "output") {{
                            if (!hasOutput) outputEl.textContent = "";
                            outputEl.textContent += eventData.text;
                            hasOutput = true;
                        }} else if (eventData.type === "area_summary") {{
                            areaSummaryBodyEl.textContent = "";
                            (eventData.rows || []).forEach(function(row) {{
                                var tr = document.createElement("tr");
                                var nameCell = document.createElement("td");
                                var areaCell = document.createElement("td");
                                nameCell.textContent = row.name;
                                areaCell.textContent = Number(row.area_mu).toFixed(4);
                                nameCell.style.cssText = "padding:10px;border-bottom:1px solid #edf0f3;";
                                areaCell.style.cssText = "padding:10px;text-align:right;border-bottom:1px solid #edf0f3;font-variant-numeric:tabular-nums;";
                                tr.appendChild(nameCell);
                                tr.appendChild(areaCell);
                                areaSummaryBodyEl.appendChild(tr);
                            }});
                            areaCsvPathEl.textContent = "CSV：" + eventData.csv_path;
                            areaCsvPathEl.title = eventData.csv_path || "";
                            areaSummaryEl.style.display = "block";
                        }} else if (eventData.type === "done") {{
                            outputEl.textContent += eventData.returncode === 0
                                ? "[运行完成]\\n"
                                : "[运行失败] 返回码：" + eventData.returncode + "\\n";
                            badgeEl.textContent = (eventData.returncode === 0 ? "成功" : "失败") + "，返回码 " + eventData.returncode;
                            badgeEl.className = eventData.returncode === 0 ? "badge success" : "badge error";
                            if (statusEl) {{
                                statusEl.textContent = eventData.returncode === 0 ? "运行完成" : "运行失败";
                                statusEl.className = eventData.returncode === 0 ? "run-status done" : "run-status";
                            }}
                        }}
                        scrollOutputToLatest(outputEl);
                    }});
                }}
            }} catch (error) {{
                badgeEl.textContent = "请求失败";
                badgeEl.className = "badge error";
                outputEl.textContent = error.message;
                if (statusEl) {{
                    statusEl.textContent = "运行失败";
                    statusEl.className = "run-status";
                }}
            }} finally {{
                if (button) {{
                    button.disabled = false;
                    button.textContent = originalButtonText;
                }}
            }}
        }});
    }});
</script>
</body>
</html>
"""


class ScriptRunHandler(BaseHTTPRequestHandler):
    def send_html(self, html_text):
        html_bytes = html_text.encode("utf-8")

        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(html_bytes)))
        self.end_headers()
        self.wfile.write(html_bytes)

    def send_json(self, data, status=200):
        json_bytes = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(json_bytes)))
        self.end_headers()
        self.wfile.write(json_bytes)

    def do_GET(self):
        values = {
            "df_path": DEFAULT_DF_PATH,
            "region_name": DEFAULT_REGION_NAME,
            "input1_path": DEFAULT_INPUT1_PATH,
            "output2_path": DEFAULT_OUTPUT2_PATH,
            "min_background_threshold": DEFAULT_MIN_BACKGROUND_THRESHOLD,
            "min_class_area_mu": DEFAULT_MIN_CLASS_AREA_MU,
            "concurrency_count": DEFAULT_CONCURRENCY_COUNT,
            "multi_class": False,
            "class_mapping": DEFAULT_CLASS_MAPPING,
        }

        # 默认值也统一处理一下
        values = normalize_values(values)

        html_text = build_html(values, result=None)
        self.send_html(html_text)

    def do_POST(self):
        content_length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(content_length).decode("utf-8", errors="replace")
        form_data = parse_qs(body)

        # 第一步：读取网页输入
        values = current_values(form_data)

        # 第二步：把用户输入的 Windows 网络路径转换成 Linux 路径
        values = normalize_values(values)

        action = form_data.get("action", [""])[0]

        if action not in {"run_all", "refresh_shp_index"}:
            self.send_json({"error": f"未知 action: {action}"}, status=400)
            return

        if action == "refresh_shp_index":
            refresh_command = [
                sys.executable,
                str(VOTE_SCRIPT),
                "--shp_dir",
                values["df_path"],
                "--refresh-shp-cache-only",
            ]
            self.send_response(200)
            self.send_header("Content-Type", "application/x-ndjson; charset=utf-8")
            self.send_header("Cache-Control", "no-cache, no-transform")
            self.send_header("X-Accel-Buffering", "no")
            self.end_headers()
            with SHP_INDEX_REFRESH_LOCK:
                with TASK_SEMAPHORE:
                    run_command_stream(self, "刷新 SHP 索引", refresh_command)
            return

        try:
            input_root, tif_files = collect_tif_files(values["input1_path"])
            batch_mode = input_root.is_dir()
            if not str(values["output2_path"]).strip():
                raise ValueError("最终输出文件夹不能为空。")
            if values["multi_class"] and not values["class_mapping"]:
                raise ValueError("启用多分类后必须填写类别映射")
        except ValueError as exc:
            self.send_json({"error": str(exc)}, status=400)
            return

        temp_output_dir = None
        try:
            temp_output_dir = create_temp_output_dir()

            self.send_response(200)
            self.send_header("Content-Type", "application/x-ndjson; charset=utf-8")
            self.send_header("Cache-Control", "no-cache, no-transform")
            self.send_header("X-Accel-Buffering", "no")
            self.end_headers()
            with TASK_SEMAPHORE:
                if batch_mode:
                    pipeline_command = [
                        sys.executable, str(BATCH_TIF_PIPELINE_SCRIPT),
                        "--input-dir", str(input_root),
                        "--temp-dir", str(temp_output_dir),
                        "--shp_dir", values["df_path"],
                        "--region-name", values["region_name"],
                        "--output-dir", values["output2_path"],
                        "--MIN_BACKGROUND_THRESHOLD", values["min_background_threshold"],
                        "--MIN_CLASS_AREA_MU", values["min_class_area_mu"],
                        "--concurrency-count", values["concurrency_count"],
                    ]
                    label = f"批量处理并统一合并 {len(tif_files)} 个 TIF"
                else:
                    pipeline_command = [
                        sys.executable, str(REGIONAL_PIPELINE_SCRIPT),
                        "--cls_tif", str(tif_files[0]),
                        "--temp-dir", str(temp_output_dir),
                        "--shp_dir", values["df_path"],
                        "--region-name", values["region_name"],
                        "--output-dir", values["output2_path"],
                        "--MIN_BACKGROUND_THRESHOLD", values["min_background_threshold"],
                        "--MIN_CLASS_AREA_MU", values["min_class_area_mu"],
                        "--concurrency-count", values["concurrency_count"],
                    ]
                    label = f"处理 TIF：{tif_files[0].name}"
                if values["multi_class"]:
                    pipeline_command.extend(["--multi-class", "--class-mapping", values["class_mapping"]])
                run_command_stream(self, label, pipeline_command)
        finally:
            if temp_output_dir is not None:
                release_temp_output_dir(temp_output_dir)
                print(
                    f"[临时目录保留 {TEMP_RETENTION_DAYS} 天] {temp_output_dir}",
                    flush=True,
                )

    def log_message(self, format, *args):
        print(f"[HTTP] {self.address_string()} - {format % args}")


if __name__ == "__main__":
    host = "0.0.0.0"
    port = 8888

    server = ThreadingHTTPServer((host, port), ScriptRunHandler)
    print(f"网页已启动：http://127.0.0.1:{port}")
    print("按 Ctrl+C 停止服务")

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n服务已停止")
    finally:
        server.server_close()
