from pathlib import Path, PurePosixPath
import json
import os
import re
import subprocess
import threading
import time
from html import escape
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse


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
        prefix_mapping.append((f"//169.254.51.{i}/data", "/media/cangling/nas_folder"))
        prefix_mapping.append((f"/169.254.51.{i}/data", "/media/cangling/nas_folder"))
        prefix_mapping.append((f"169.254.51.{i}/data", "/media/cangling/nas_folder"))

        # 新建卷 -> /media/cangling/xinjianjuan
        prefix_mapping.append((f"//169.254.51.{i}/新建卷", "/media/cangling/xinjianjuan"))
        prefix_mapping.append((f"/169.254.51.{i}/新建卷", "/media/cangling/xinjianjuan"))
        prefix_mapping.append((f"169.254.51.{i}/新建卷", "/media/cangling/xinjianjuan"))

        # datadisk2 -> /media/cangling/EAGET
        prefix_mapping.append((f"//169.254.51.{i}/datadisk2", "/media/cangling/EAGET"))
        prefix_mapping.append((f"/169.254.51.{i}/datadisk2", "/media/cangling/EAGET"))
        prefix_mapping.append((f"169.254.51.{i}/datadisk2", "/media/cangling/EAGET"))

        # 新加卷 -> /media/cangling/xinjiajuan
        prefix_mapping.append((f"//169.254.51.{i}/新加卷", "/media/cangling/xinjiajuan"))
        prefix_mapping.append((f"/169.254.51.{i}/新加卷", "/media/cangling/xinjiajuan"))        
        prefix_mapping.append((f"169.254.51.{i}/新加卷", "/media/cangling/xinjiajuan"))

    for windows_prefix, linux_prefix in prefix_mapping:
        # 必须完整匹配共享目录名，避免 data 错误匹配 datadisk2。
        if path == windows_prefix:
            return linux_prefix
        if path.startswith(windows_prefix + "/"):
            relative_path = path[len(windows_prefix):]
            return linux_prefix + relative_path

    return path



def get_ip_from_source_root(source_root):
    if source_root is None:
        return ""

    text = str(source_root).strip()
    match = re.search(r"169\.254\.51\.\d+", text)

    if match:
        return match.group(0)

    return ""

def convert_linux_path_to_network_path(path, source_root=""):
    if path is None:
        return path

    path = str(path).strip()
    if not path:
        return path

    ip = get_ip_from_source_root(source_root)

    # 如果第一个输入框 source_root 里面没有 IP，就不强行转换，直接返回原路径
    if not ip:
        return path

    # 统一成 Linux 风格，方便判断
    path = path.replace("\\", "/")

    prefix_mapping = [
        ("/media/cangling/nas_folder", f"//{ip}/data"),
        ("/media/cangling/xinjianjuan", f"//{ip}/新建卷"),
        ("/media/cangling/EAGET", f"//{ip}/datadisk2"),
        ("/media/cangling/xinjiajuan", f"//{ip}/新加卷"),
    ]

    for linux_prefix, windows_prefix in prefix_mapping:
        if path == linux_prefix:
            return windows_prefix.replace("/", "\\")
        if path.startswith(linux_prefix + "/"):
            relative_path = path[len(linux_prefix):]
            return (windows_prefix + relative_path).replace("/", "\\")

    return path.replace("/", "\\")

BASE_DIR = Path(__file__).resolve().parent
PYTHON_BIN = "/home/cangling/miniforge3/bin/python"
JOBS = {
    "run1": {
        "status": "未运行",
        "running": False,
        "started_at": "",
        "finished_at": "",
        "returncode": None,
        "output": "",
        "delivery_dirs": [],
        "source_root": "",
        "converted_source_root": "",
        "output_root": "",
        "converted_output_root": "",
        "sample_dir": "",
        "truth_dir": "",
        "mode": "skip",
    },
    "run2": {
        "status": "未运行",
        "running": False,
        "started_at": "",
        "finished_at": "",
        "returncode": None,
        "output": "",
        "csv_output": "",
        "source_root": "",
        "converted_source_root": "",
        "input_root": "",
        "converted_input_root": "",
        "measure_dir": "",
        "result_dir": "",
    },
}
JOBS_LOCK = threading.Lock()


def now_text():
    return time.strftime("%Y-%m-%d %H:%M:%S")


def join_runtime_path(root, *parts):
    text = str(root)
    if text.startswith("/"):
        return str(PurePosixPath(text).joinpath(*parts))
    return str(Path(text).joinpath(*parts))


def command_for_display(command):
    return " ".join(str(item) for item in command)


def append_job_output(job_name, text):
    if not text:
        return
    with JOBS_LOCK:
        JOBS[job_name]["output"] += text


def reserve_job(job_name, **values):
    """原子地占用任务，避免多个标签页同时启动同一步骤。"""
    with JOBS_LOCK:
        if any(job["running"] for job in JOBS.values()):
            return False
        JOBS[job_name].update(
            status="正在运行",
            running=True,
            started_at=now_text(),
            finished_at="",
            returncode=None,
            **values,
        )
        return True


def run_command(command, job_name):
    command_text = command_for_display(command)
    print(f"[RUN] 开始执行：{command_text}", flush=True)
    append_job_output(job_name, f"$ {command_text}\n")

    process_env = os.environ.copy()
    process_env["PYTHONUNBUFFERED"] = "1"
    process = subprocess.Popen(
        command,
        cwd=str(BASE_DIR),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        bufsize=1,
        env=process_env,
    )
    output_lines = []
    if process.stdout is not None:
        for line in process.stdout:
            output_lines.append(line)
            print(line, end="", flush=True)
            append_job_output(job_name, line)

    returncode = process.wait()
    print(f"[RUN] 执行结束：returncode={returncode}", flush=True)
    append_job_output(job_name, f"[RUN] 执行结束：returncode={returncode}\n")
    return returncode, "".join(output_lines)


def extract_delivery_dirs(output, delivery_root=""):
    dirs = []
    seen = set()
    normalized_root = str(delivery_root).rstrip("/\\")

    for line in output.splitlines():
        text = line.strip()

        if normalized_root and normalized_root in text:
            path_start = text.find(normalized_root)
            path_end = text.lower().find(".shp", path_start)
            if path_end >= 0:
                path_obj = Path(text[path_start:path_end + 4]).parent
                dir_text = str(path_obj)
                if dir_text not in seen:
                    seen.add(dir_text)
                    dirs.append(dir_text)
                continue

        if "02参考真值" not in text:
            continue

        match = re.search(r"(/[^\s]+02参考真值/[^\s]+)", text)
        if not match:
            continue

        path_text = match.group(1).strip()

        path_obj = Path(path_text)
        if path_obj.suffix.lower() == ".shp":
            path_obj = path_obj.parent

        dir_text = str(path_obj)
        if dir_text not in seen:
            seen.add(dir_text)
            dirs.append(dir_text)

    return dirs

def extract_csv_output_path(output):
    for line in output.splitlines():
        text = line.strip()
        match = re.search(r"已导出\s*CSV[：:]\s*(.+)$", text, re.IGNORECASE)
        if match:
            return match.group(1).strip()
    return ""


def update_job(job_name, **values):
    with JOBS_LOCK:
        JOBS[job_name].update(values)


def run_first_job(source_root, output_root, mode):
    converted_source_root = convert_network_path(source_root)
    converted_output_root = convert_network_path(output_root)
    sample_dir = join_runtime_path(converted_output_root, "01生成样本")
    truth_dir = join_runtime_path(converted_output_root, "02参考真值")
    print("[RUN1] 第一步开始", flush=True)
    print(f"[RUN1] 样本目录：{sample_dir}", flush=True)
    print(f"[RUN1] 参考真值目录：{truth_dir}", flush=True)

    commands = [
        [
            PYTHON_BIN,
            "01generate_county_samples_by_city.py",
            "--source_root",
            converted_source_root,
            "--output_root",
            sample_dir,
            "--mode",
            mode,
        ],
        [
            PYTHON_BIN,
            "02fast_clip_samples_and_yangfang.py",
            converted_source_root,
            sample_dir,
            converted_output_root,
            "--source_root",
            converted_source_root,
            "--delivery-dir",
            truth_dir,
            "--mode",
            mode,
        ],
    ]

    update_job(
        "run1",
        converted_source_root=converted_source_root,
        converted_output_root=converted_output_root,
        sample_dir=sample_dir,
        truth_dir=truth_dir,
        delivery_dirs=[sample_dir, truth_dir],
    )

    final_returncode = 0
    for command in commands:
        returncode, _ = run_command(command, "run1")
        if returncode != 0:
            final_returncode = returncode
            break

    append_job_output("run1", "[运行-1] 已完成。\n" if final_returncode == 0 else "[运行-1] 运行失败，请查看上方日志。\n")
    update_job(
        "run1",
        status="运行完成" if final_returncode == 0 else "运行失败",
        running=False,
        finished_at=now_text(),
        returncode=final_returncode,
    )
    print(f"[RUN1] 第一步结束：returncode={final_returncode}", flush=True)
    print(f"[RUN1] 输出文件夹：{delivery_dirs}", flush=True)


def run_second_job(source_root, input_root):
    print("[RUN2] 第二步开始：03 + 04", flush=True)
    converted_source_root = convert_network_path(source_root)
    converted_input_root = convert_network_path(input_root)
    sample_dir = join_runtime_path(converted_input_root, "01生成样本")
    truth_dir = join_runtime_path(converted_input_root, "02参考真值")
    measure_dir = join_runtime_path(converted_input_root, "03测量值")
    result_dir = join_runtime_path(converted_input_root, "04精度评价")
    print(f"[RUN2] 样本目录：{sample_dir}", flush=True)
    print(f"[RUN2] 参考真值目录：{truth_dir}", flush=True)
    print(f"[RUN2] 测量值目录：{measure_dir}", flush=True)
    print(f"[RUN2] 精度评价目录：{result_dir}", flush=True)

    boundary_output = join_runtime_path(result_dir, "精度评价边界.shp")
    csv_output_arg = join_runtime_path(result_dir, "精度评价汇总.csv")

    command03 = [
        PYTHON_BIN,
        "03fast_clip_samples_and_results.py",
        converted_source_root,
        sample_dir,
        converted_input_root,
        "--source_root",
        converted_source_root,
        "--delivery-dir",
        measure_dir,
    ]
    commands = [
        command03,
        [
            PYTHON_BIN,
            "04_calculate_accuracy_to_boundary.py",
            "--truth-root",
            truth_dir,
            "--measure-root",
            measure_dir,
            "--boundary-output",
            boundary_output,
            "--csv-output",
            csv_output_arg,
        ],
    ]

    update_job(
        "run2",
        converted_input_root=converted_input_root,
        measure_dir=measure_dir,
        result_dir=result_dir,
    )

    final_returncode = 0
    for command in commands:
        returncode, _ = run_command(command, "run2")
        if returncode != 0:
            final_returncode = returncode
            break

    with JOBS_LOCK:
        output_text = JOBS["run2"]["output"]
    csv_output = extract_csv_output_path(output_text)
    if final_returncode == 0 and not csv_output:
        csv_output = csv_output_arg

    update_job(
        "run2",
        status="运行完成" if final_returncode == 0 else "运行失败",
        running=False,
        finished_at=now_text(),
        returncode=final_returncode,
        csv_output=csv_output,
        result_dir=result_dir,
    )
    append_job_output("run2", "[运行-2] 已完成。\n" if final_returncode == 0 else "[运行-2] 运行失败，请查看上方日志。\n")
    print(f"[RUN2] 第二步结束：returncode={final_returncode}", flush=True)
    print(f"[RUN2] CSV 输出路径：{csv_output}", flush=True)
    print(f"[RUN2] 评价结果目录：{result_dir}", flush=True)


def start_background(job_name, target, *args):
    def guarded_target():
        try:
            target(*args)
        except Exception as exc:
            message = f"[{job_name}] 启动或运行失败：{exc}\n"
            print(message, end="", flush=True)
            append_job_output(job_name, message)
            update_job(
                job_name,
                status="运行失败",
                running=False,
                finished_at=now_text(),
                returncode=-1,
            )

    print(f"[THREAD] 启动后台任务：{target.__name__}", flush=True)
    thread = threading.Thread(target=guarded_target, daemon=True)
    thread.start()


def status_class(status):
    if status == "正在运行":
        return "running"
    if status == "运行完成":
        return "done"
    if status == "运行失败":
        return "failed"
    return "idle"


def status_payload():
    with JOBS_LOCK:
        run1 = dict(JOBS["run1"])
        run2 = dict(JOBS["run2"])

    run1_path_root = run1.get("output_root") or run1.get("source_root") or ""
    run2_path_root = run2.get("input_root") or run1_path_root
    run1_results = []
    for path in run1.get("delivery_dirs") or []:
        label = "01生成样本：" if Path(path).name == "01生成样本" else "02参考真值："
        run1_results.append(
            {"label": label, "path": convert_linux_path_to_network_path(path, run1_path_root)}
        )

    run2_results = []
    for label, path in (
        ("03测量值：", run2.get("measure_dir")),
        ("04精度评价：", run2.get("result_dir")),
        ("args.csv_output：", run2.get("csv_output")),
    ):
        if path:
            run2_results.append(
                {"label": label, "path": convert_linux_path_to_network_path(path, run2_path_root)}
            )

    def public_job(job):
        return {
            "status": job.get("status") or "未运行",
            "status_class": status_class(job.get("status")),
            "running": bool(job.get("running")),
            "started_at": job.get("started_at") or "",
            "finished_at": job.get("finished_at") or "",
        }

    output_parts = [part for part in (run1.get("output"), run2.get("output")) if part]
    return {
        "run1": public_job(run1),
        "run2": public_job(run2),
        "run1_results": run1_results,
        "run2_results": run2_results,
        "output": "\n".join(output_parts),
    }


def page_html(message=""):
    with JOBS_LOCK:
        run1 = dict(JOBS["run1"])
        run2 = dict(JOBS["run2"])

    raw_source_root = run1.get("source_root") or ""
    raw_output_root = run1.get("output_root") or ""
    raw_run2_source_root = run2.get("source_root") or raw_source_root
    raw_input_root = run2.get("input_root") or raw_output_root
    source_root = escape(raw_source_root)
    run2_source_root = escape(raw_run2_source_root)
    output_root = escape(raw_output_root)
    input_root = escape(raw_input_root)
    mode = escape(run1.get("mode") or "skip")
    message_html = f"<div class='message'>{escape(message)}</div>" if message else ""
    def copyable_item(value):
        safe_value = escape(str(value), quote=True)
        return (
            f'<li class="copy-row">'
            f'<span class="copy-text">{safe_value}</span>'
            f'<button class="copy-btn" type="button" onclick="copyText(this)" data-copy="{safe_value}">复制</button>'
            f'</li>'
        )
    run1_disabled = "disabled" if run1["running"] else ""
    run2_disabled = "disabled" if run2["running"] else ""

    if run1["delivery_dirs"]:
        delivery_items = "".join(
            copyable_item(convert_linux_path_to_network_path(item, raw_output_root or raw_source_root))
            for item in run1["delivery_dirs"]
        )
    elif run1["status"] == "运行完成":
        delivery_items = "<li>未解析到 人工修改真值文件夹路径。</li>"
    else:
        delivery_items = "<li>第一步完成后显示。</li>"

    csv_output = run2.get("csv_output") or ""
    measure_dir = run2.get("measure_dir") or ""
    result_dir = run2.get("result_dir") or ""
    result_items = []
    if measure_dir:
        windows_measure_dir = convert_linux_path_to_network_path(measure_dir, raw_input_root or raw_output_root or raw_source_root)
        result_items.append(
            "<li><strong>03测量值：</strong></li>" + copyable_item(windows_measure_dir)
        )
    if result_dir:
        windows_result_dir = convert_linux_path_to_network_path(result_dir, raw_input_root or raw_output_root or raw_source_root)
        result_items.append(
            "<li><strong>04精度评价：</strong></li>" + copyable_item(windows_result_dir)
        )
    if csv_output:
        windows_csv_output = convert_linux_path_to_network_path(csv_output, raw_input_root or raw_output_root or raw_source_root)
        result_items.append(
            "<li><strong>args.csv_output：</strong></li>" + copyable_item(windows_csv_output)
        )
    if result_items:
        csv_item = "".join(result_items)
    elif run2["status"] == "运行完成":
        csv_item = "<li>未解析到评价结果目录或 args.csv_output 路径。</li>"
    else:
        csv_item = "<li>第二步完成后显示。</li>"

    log_text = escape((run1.get("output") or "") + ("\n" if run1.get("output") and run2.get("output") else "") + (run2.get("output") or ""))

    return f"""<!doctype html>
<html lang="zh-CN">
<head>
    <meta charset="utf-8">
    <title>Python 多作物自检 </title>
    <meta name="viewport" content="width=device-width, initial-scale=1">

    <style>
        body {{
            margin: 0;
            font-family: Arial, "Microsoft YaHei", sans-serif;
            background: #eef3f9;
            color: #172033;
        }}
        main {{
            max-width: 1280px;
            margin: 0 auto;
            padding: 56px 32px 64px;
        }}
        h1 {{
            font-size: 34px;
            margin: 0 0 30px;
            font-weight: 800;
        }}
        .grid {{
            display: grid;
            grid-template-columns: repeat(2, minmax(0, 1fr));
            gap: 24px;
            align-items: start;
        }}
        .card, .result-panel {{
            background: #fff;
            border: 1px solid #d9e2ec;
            border-radius: 8px;
            box-shadow: 0 18px 44px rgba(31, 54, 79, 0.08);
        }}
        .card {{ padding: 28px; min-height: 360px; }}
        .result-panel {{ margin-top: 24px; padding: 26px 28px; }}
        h2 {{ font-size: 24px; margin: 0 0 10px; }}
        .command {{ color: #64748b; margin: 0 0 24px; line-height: 1.6; }}
        label {{ display: flex; justify-content: space-between; gap: 16px; font-weight: 700; margin: 18px 0 8px; }}
        label span {{ color: #718096; font-weight: 500; }}
        input {{
            box-sizing: border-box;
            width: 100%;
            height: 48px;
            padding: 0 14px;
            border: 1px solid #cbd5e1;
            border-radius: 8px;
            font-size: 16px;
            background: #f8fafc;
        }}
        button {{
            width: 100%;
            height: 54px;
            margin-top: 28px;
            border: 0;
            border-radius: 8px;
            color: #fff;
            font-size: 18px;
            font-weight: 800;
            cursor: pointer;
        }}
        .primary {{ background: #2563eb; }}
        .secondary {{ background: #0f766e; }}
        button:disabled {{ background: #94a3b8; cursor: not-allowed; }}
        .status-line {{ margin-top: 18px; color: #52616f; }}
        .status {{ display: inline-block; padding: 4px 10px; border-radius: 999px; font-weight: 800; }}
        .idle {{ background: #eef2f6; color: #52616f; }}
        .running {{ background: #fef3c7; color: #92400e; }}
        .done {{ background: #dcfce7; color: #166534; }}
        .failed {{ background: #fee2e2; color: #991b1b; }}
        .meta {{ color: #64748b; line-height: 1.8; margin-top: 12px; }}
        .message {{ background: #e8f3ff; border: 1px solid #b9dcff; border-radius: 8px; padding: 12px 14px; margin-bottom: 22px; }}
        .result-grid {{ display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 18px; }}
        .result-box {{ background: #f8fafc; border: 1px solid #dbe4ef; border-radius: 8px; padding: 16px; min-height: 96px; }}
        .result-box h3 {{ margin: 0 0 12px; font-size: 17px; }}
        .log-window {{
            box-sizing: border-box;
            width: 100%;
            min-height: 280px;
            max-height: 520px;
            overflow: auto;
            margin: 0;
            padding: 18px;
            border-radius: 8px;
            background: #0f172a;
            color: #d1fae5;
            font: 14px/1.6 Consolas, "Courier New", monospace;
            white-space: pre-wrap;
            word-break: break-all;
        }}
        ul {{ margin: 0; padding-left: 20px; color: #24364b; line-height: 1.8; word-break: break-all; }}
        .copy-row {{ padding-right: 92px; position: relative; }}
        .copy-text {{ display: inline; }}
        .copy-btn {{
            position: absolute;
            right: 0;
            top: 0;
            width: auto;
            height: 30px;
            margin: 0;
            padding: 0 12px;
            border-radius: 6px;
            background: #2563eb;
            color: #fff;
            font-size: 13px;
            font-weight: 700;
        }}
        .copy-btn.copied {{ background: #16a34a; }}
        @media (max-width: 860px) {{
            main {{ padding: 32px 16px; }}
            .grid, .result-grid {{ grid-template-columns: 1fr; }}
        }}
    </style>
</head>
<body>
<main>
    <h1>多作物自检</h1>
    {message_html}

    <div class="grid">
        <section class="card">
            <h2>运行-1</h2>
            <form method="post" action="/run1">
                <label for="source_root">source_root <span>路径会自动转换</span></label>
                <input id="source_root" name="source_root" value="{source_root}" placeholder="请输入 Windows 访问路径或 Linux 真实路径" required>
                <label for="output_root">输出文件夹根路径 <span>生成 01生成样本、02参考真值</span></label>
                <input id="output_root" name="output_root" value="{output_root}" placeholder="请输入输出文件夹的 Windows 或 Linux 根路径" required>
                <label for="mode">mode <span>默认：skip</span></label>
                <input id="mode" name="mode" value="{mode or 'skip'}">
                <button id="run1-button" class="primary" type="submit" {run1_disabled}>运行-1</button>
            </form>
            <div class="status-line">状态：<span id="run1-status" class="status {status_class(run1['status'])}">{escape(run1['status'])}</span></div>
            <div class="meta">开始时间：<span id="run1-started">{escape(run1.get('started_at') or '')}</span><br>
                完成时间：<span id="run1-finished">{escape(run1.get('finished_at') or '')}</span>
            </div>
        </section>

        <section class="card">
            <h2>运行-2</h2>
            <form id="run2-form" method="post" action="/run2">
                <label for="run2_source_root">source_root <span>与运行-1相同</span></label>
                <input id="run2_source_root" name="source_root" value="{run2_source_root}" placeholder="请输入 Windows 访问路径或 Linux 真实路径" required>
                <label for="input_root">输入文件夹根路径 <span>读取 01、02，生成 03、04</span></label>
                <input id="input_root" name="input_root" value="{input_root}" placeholder="请先运行-1，或手动输入包含 01、02 文件夹的根路径" required>
                <button id="run2-button" class="secondary" type="submit" {run2_disabled}>运行-2</button>
            </form>
            <div class="status-line">状态：<span id="run2-status" class="status {status_class(run2['status'])}">{escape(run2['status'])}</span></div>
            <div class="meta">
                开始时间：<span id="run2-started">{escape(run2.get('started_at') or '')}</span><br>
                完成时间：<span id="run2-finished">{escape(run2.get('finished_at') or '')}</span>
            </div>
        </section>
    </div>

    <section class="result-panel">
        <h2>结果输出</h2>
        <div class="result-grid">
            <div class="result-box">
                <h3>运行-1输出文件夹</h3>
                <ul id="run1-results">{delivery_items}</ul>
            </div>
            <div class="result-box">
                <h3>运行-2输出文件夹</h3>
                <ul id="run2-results">{csv_item}</ul>
            </div>
        </div>
    </section>

    <section class="result-panel">
        <h2>运行输出</h2>
        <pre id="log-output" class="log-window">{log_text}</pre>
    </section>
</main>
<script>
async function copyText(button) {{
    const text = button ? (button.getAttribute("data-copy") || "") : "";

    if (!text) {{
        return;
    }}

    let copied = false;
    try {{
        if (navigator.clipboard && window.isSecureContext) {{
            await navigator.clipboard.writeText(text);
            copied = true;
        }}
    }} catch (error) {{
        console.log("[COPY] navigator.clipboard 复制失败:", error);
    }}

    if (!copied) {{
        try {{
            const textarea = document.createElement("textarea");
            textarea.value = text;
            textarea.setAttribute("readonly", "");
            textarea.style.position = "fixed";
            textarea.style.left = "-9999px";
            textarea.style.top = "0";
            document.body.appendChild(textarea);
            textarea.focus();
            textarea.select();
            copied = document.execCommand("copy");
            document.body.removeChild(textarea);
        }} catch (error) {{
            console.log("[COPY] execCommand 复制失败:", error);
        }}
    }}

    const oldText = button.textContent;
    if (copied) {{
        console.log("[COPY] 复制成功:", text);
        button.textContent = "已复制";
        button.classList.add("copied");
    }} else {{
        console.log("[COPY] 复制失败，请手动复制:", text);
        button.textContent = "复制失败";
    }}

    setTimeout(() => {{
        button.textContent = oldText;
        button.classList.remove("copied");
    }}, 1200);
}}

function updateStatus(prefix, job) {{
    const status = document.getElementById(prefix + "-status");
    const started = document.getElementById(prefix + "-started");
    const finished = document.getElementById(prefix + "-finished");
    const button = document.getElementById(prefix + "-button");
    if (status) {{
        status.textContent = job.status;
        status.className = "status " + job.status_class;
    }}
    if (started) started.textContent = job.started_at || "";
    if (finished) finished.textContent = job.finished_at || "";
    if (button) button.disabled = Boolean(job.running);
}}

function renderPaths(containerId, entries, placeholder) {{
    const container = document.getElementById(containerId);
    if (!container) return;
    container.replaceChildren();
    if (!entries || entries.length === 0) {{
        const empty = document.createElement("li");
        empty.textContent = placeholder;
        container.appendChild(empty);
        return;
    }}
    for (const entry of entries) {{
        if (entry.label) {{
            const label = document.createElement("li");
            const strong = document.createElement("strong");
            strong.textContent = entry.label;
            label.appendChild(strong);
            container.appendChild(label);
        }}
        const row = document.createElement("li");
        row.className = "copy-row";
        const value = document.createElement("span");
        value.className = "copy-text";
        value.textContent = entry.path;
        const button = document.createElement("button");
        button.className = "copy-btn";
        button.type = "button";
        button.textContent = "复制";
        button.setAttribute("data-copy", entry.path);
        button.addEventListener("click", () => copyText(button));
        row.append(value, button);
        container.appendChild(row);
    }}
}}

async function refreshStatus() {{
    try {{
        const response = await fetch("/status", {{cache: "no-store"}});
        if (!response.ok) return;
        const data = await response.json();
        updateStatus("run1", data.run1);
        updateStatus("run2", data.run2);
        renderPaths("run1-results", data.run1_results, "第一步完成后显示。");
        renderPaths("run2-results", data.run2_results, "第二步完成后显示。");
        const logWindow = document.getElementById("log-output");
        if (logWindow) {{
            const nearBottom = logWindow.scrollHeight - logWindow.scrollTop - logWindow.clientHeight < 80;
            logWindow.textContent = data.output || "等待运行任务……";
            if (nearBottom) logWindow.scrollTop = logWindow.scrollHeight;
        }}
    }} catch (error) {{
        console.log("状态刷新异常:", error);
    }}
}}

setInterval(refreshStatus, 1500);
const initialLogWindow = document.getElementById("log-output");
if (initialLogWindow) initialLogWindow.scrollTop = initialLogWindow.scrollHeight;
refreshStatus();
</script>



</body>
</html>"""


class ScriptPageHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path == "/status":
            self.respond_json(status_payload())
            return
        if parsed.path in ("/run1", "/run2"):
            self.redirect_home()
            return
        if parsed.path in ("", "/", "/index.html", "/main.py"):
            self.respond_html(page_html())
            return
        if parsed.path == "/favicon.ico":
            self.send_response(204)
            self.end_headers()
            return
        self.respond_html(page_html(f"未识别的访问地址：{parsed.path}，已返回首页。"))

    def do_POST(self):
        parsed = urlparse(self.path)
        length = int(self.headers.get("Content-Length", "0"))
        data = self.rfile.read(length).decode("utf-8")
        form = parse_qs(data)

        if parsed.path == "/run1":
            source_root = (form.get("source_root", [""])[0] or "").strip()
            output_root = (form.get("output_root", [""])[0] or "").strip()
            mode = (form.get("mode", ["skip"])[0] or "skip").strip()
            if not source_root:
                self.respond_html(page_html("请输入 --source_root。"))
                return
            if not output_root:
                self.respond_html(page_html("请输入输出文件夹根路径。"))
                return
            if not mode:
                mode = "skip"
            converted_output_root = convert_network_path(output_root)
            sample_dir = join_runtime_path(converted_output_root, "01生成样本")
            truth_dir = join_runtime_path(converted_output_root, "02参考真值")
            with JOBS_LOCK:
                busy = JOBS["run1"]["running"] or JOBS["run2"]["running"]
                if not busy:
                    JOBS["run1"].update(
                        status="正在运行", running=True, started_at=now_text(), finished_at="",
                        returncode=None, output="[运行-1] 开始生成样本和参考真值……\n",
                        delivery_dirs=[sample_dir, truth_dir], source_root=source_root,
                        output_root=output_root, converted_output_root=converted_output_root,
                        sample_dir=sample_dir, truth_dir=truth_dir, mode=mode,
                    )
                    # 新一轮运行-1清空整个日志窗口及旧的运行-2结果。
                    JOBS["run2"].update(
                        status="未运行", running=False, started_at="", finished_at="",
                        returncode=None, output="", csv_output="", source_root=source_root,
                        converted_source_root="", input_root=output_root,
                        converted_input_root="", measure_dir="", result_dir="",
                    )
            if busy:
                self.respond_html(page_html("当前已有任务在运行，请等待完成后再启动运行-1。"))
                return
            start_background("run1", run_first_job, source_root, output_root, mode)
            self.redirect_home()
            return

        if parsed.path == "/run2":
            with JOBS_LOCK:
                saved_source_root = JOBS["run2"].get("source_root") or JOBS["run1"].get("source_root") or ""
                saved_input_root = JOBS["run2"].get("input_root") or JOBS["run1"].get("output_root") or ""
                run1_running = JOBS["run1"]["running"]
            source_root = (form.get("source_root", [saved_source_root])[0] or saved_source_root).strip()
            input_root = (form.get("input_root", [saved_input_root])[0] or saved_input_root).strip()
            if run1_running:
                self.respond_html(page_html("运行-1尚未完成，请等待完成后再启动运行-2。"))
                return
            if not source_root:
                self.respond_html(page_html("请输入运行-2的 source_root。"))
                return
            if not input_root:
                self.respond_html(page_html("请输入运行-2的输入文件夹根路径。"))
                return
            if not reserve_job(
                "run2",
                output="\n[运行-2] 开始生成测量值和精度评价……\n",
                csv_output="",
                source_root=source_root,
                converted_source_root=convert_network_path(source_root),
                input_root=input_root,
                converted_input_root="",
                measure_dir="",
                result_dir="",
            ):
                self.respond_html(page_html("第二步已经在运行中。"))
                return
            start_background("run2", run_second_job, source_root, input_root)
            self.redirect_home()
            return

        self.respond_html(page_html(f"未识别的提交地址：{parsed.path}"))

    def redirect_home(self):
        self.send_response(303)
        self.send_header("Location", "/")
        self.end_headers()

    def respond_html(self, html):
        body = html.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def respond_json(self, payload):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):
        # 页面会定时轮询状态，默认访问日志会淹没真正的脚本日志。
        return
    

def main():
    host = os.environ.get("HOST", "0.0.0.0")
    port = int(os.environ.get("PORT", "8891"))
    server = ThreadingHTTPServer((host, port), ScriptPageHandler)
    print(f"页面已启动：http://{host}:{port}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()





