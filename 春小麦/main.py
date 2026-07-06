from pathlib import Path
import os
import re
import subprocess
import threading
import time
from html import escape
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlparse


def convert_network_path(path):
    if path is None:
        return path

    path = str(path).strip()
    if not path:
        return path

    # 把 Windows 的反斜杠 \ 转成 Linux 风格 /
    path = path.replace("\\", "/")
    windows_prefix_list = []
    for i in range(1, 256):
        windows_prefix_list.append(f"//169.254.51.{i}/datadisk")
        windows_prefix_list.append(f"/169.254.51.{i}/datadisk")
        windows_prefix_list.append(f"169.254.51.{i}/datadisk")

    linux_prefix = "/media/cangling/EAGET"
    for windows_prefix in windows_prefix_list:
        if path.startswith(windows_prefix):
            return path.replace(windows_prefix, linux_prefix, 1)

    return path


BASE_DIR = Path(__file__).resolve().parent
PYTHON_BIN = "/home/cangling/miniforge3/bin/python"
DEFAULT_CSV_OUTPUT = str(BASE_DIR / "04评价精度结果" / "精度评价汇总.csv")

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
    },
}
JOBS_LOCK = threading.Lock()


def now_text():
    return time.strftime("%Y-%m-%d %H:%M:%S")


def command_for_display(command):
    return " ".join(str(item) for item in command)


def run_command(command):
    print(f"[RUN] 开始执行：{command_for_display(command)}", flush=True)
    process = subprocess.run(
        command,
        cwd=str(BASE_DIR),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    print(f"[RUN] 执行结束：returncode={process.returncode}", flush=True)
    if process.stdout:
        print(process.stdout, flush=True)
    return process.returncode, process.stdout or ""


def extract_delivery_dirs(output):
    dirs = []
    seen = set()
    for line in output.splitlines():
        text = line.strip()
        match = re.search(r"->\s*(.+)$", text)
        if not match:
            continue
        path_text = match.group(1).strip()
        if not path_text:
            continue
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


def run_first_job(source_root, mode):
    converted_source_root = convert_network_path(source_root)
    print(f"[RUN1] 第一步开始：source_root={source_root}", flush=True)
    print(f"[RUN1] 路径转换后：source_root={converted_source_root}", flush=True)
    print(f"[RUN1] mode={mode}", flush=True)

    commands = [
        [
            PYTHON_BIN,
            "01generate_county_samples_by_city.py",
            "--source_root",
            converted_source_root,
            "--mode",
            mode,
        ],
        [PYTHON_BIN, "02fast_clip_samples_and_yangfang.py", "--source_root", converted_source_root],
    ]

    update_job(
        "run1",
        status="正在运行",
        running=True,
        started_at=now_text(),
        finished_at="",
        returncode=None,
        output="",
        delivery_dirs=[],
        source_root=source_root,
        converted_source_root=converted_source_root,
        mode=mode,
    )

    full_output = []
    final_returncode = 0
    for command in commands:
        full_output.append(f"$ {command_for_display(command)}\n")
        returncode, output = run_command(command)
        full_output.append(output)
        if returncode != 0:
            final_returncode = returncode
            break

    output_text = "\n".join(full_output)
    delivery_dirs = extract_delivery_dirs(output_text)
    update_job(
        "run1",
        status="运行完成" if final_returncode == 0 else "运行失败",
        running=False,
        finished_at=now_text(),
        returncode=final_returncode,
        output=output_text,
        delivery_dirs=delivery_dirs,
    )
    print(f"[RUN1] 第一步结束：returncode={final_returncode}", flush=True)
    print(f"[RUN1] 解析到 delivery 文件夹：{delivery_dirs}", flush=True)


def run_second_job(source_root=None):
    print("[RUN2] 第二步开始：03 + 04", flush=True)
    if source_root:
        converted_source_root = convert_network_path(source_root)
    else:
        with JOBS_LOCK:
            converted_source_root = JOBS["run1"].get("converted_source_root") or ""
    print(f"[RUN2] 使用 source_root={converted_source_root}", flush=True)

    command03 = [PYTHON_BIN, "03fast_clip_samples_and_results.py"]
    if converted_source_root:
        command03.extend(["--source_root", converted_source_root])
    commands = [
        command03,
        [PYTHON_BIN, "04_calculate_accuracy_to_boundary.py"],
    ]

    update_job(
        "run2",
        status="正在运行",
        running=True,
        started_at=now_text(),
        finished_at="",
        returncode=None,
        output="",
        csv_output="",
    )

    full_output = []
    final_returncode = 0
    for command in commands:
        full_output.append(f"$ {command_for_display(command)}\n")
        returncode, output = run_command(command)
        full_output.append(output)
        if returncode != 0:
            final_returncode = returncode
            break

    output_text = "\n".join(full_output)
    csv_output = extract_csv_output_path(output_text)
    if final_returncode == 0 and not csv_output:
        csv_output = DEFAULT_CSV_OUTPUT

    update_job(
        "run2",
        status="运行完成" if final_returncode == 0 else "运行失败",
        running=False,
        finished_at=now_text(),
        returncode=final_returncode,
        output=output_text,
        csv_output=csv_output,
    )
    print(f"[RUN2] 第二步结束：returncode={final_returncode}", flush=True)
    print(f"[RUN2] CSV 输出路径：{csv_output}", flush=True)


def start_background(target, *args):
    print(f"[THREAD] 启动后台任务：{target.__name__}", flush=True)
    thread = threading.Thread(target=target, args=args, daemon=True)
    thread.start()


def status_class(status):
    if status == "正在运行":
        return "running"
    if status == "运行完成":
        return "done"
    if status == "运行失败":
        return "failed"
    return "idle"


def page_html(message=""):
    with JOBS_LOCK:
        run1 = dict(JOBS["run1"])
        run2 = dict(JOBS["run2"])

    source_root = escape(run1.get("source_root") or "")
    mode = escape(run1.get("mode") or "skip")
    message_html = f"<div class='message'>{escape(message)}</div>" if message else ""
    run1_disabled = "disabled" if run1["running"] else ""
    run2_disabled = "disabled" if run2["running"] else ""

    if run1["delivery_dirs"]:
        delivery_items = "".join(f"<li>{escape(item)}</li>" for item in run1["delivery_dirs"])
    elif run1["status"] == "运行完成":
        delivery_items = "<li>未解析到 delivery_dir + unit.code 文件夹路径。</li>"
    else:
        delivery_items = "<li>第一步完成后显示。</li>"

    csv_output = run2.get("csv_output") or ""
    if csv_output:
        csv_item = f"<li>{escape(csv_output)}</li>"
    elif run2["status"] == "运行完成":
        csv_item = "<li>未解析到 args.csv_output 路径。</li>"
    else:
        csv_item = "<li>第二步完成后显示。</li>"

    return f"""<!doctype html>
<html lang="zh-CN">
<head>
    <meta charset="utf-8">
    <title>Python 脚本运行面板</title>
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <meta http-equiv="refresh" content="3;url=/">
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
        ul {{ margin: 0; padding-left: 20px; color: #24364b; line-height: 1.8; word-break: break-all; }}
        @media (max-width: 860px) {{
            main {{ padding: 32px 16px; }}
            .grid, .result-grid {{ grid-template-columns: 1fr; }}
        }}
    </style>
</head>
<body>
<main>
    <h1>Python 脚本运行面板</h1>
    {message_html}

    <div class="grid">
        <section class="card">
            <h2>运行 01 + 02</h2>
            <p class="command">命令：{escape(PYTHON_BIN)} 01generate_county_samples_by_city.py --source_root 输入框1 --mode 输入框2；然后运行 02fast_clip_samples_and_yangfang.py --source_root 输入框1</p>
            <form method="post" action="/run1">
                <label for="source_root">source_root <span>路径会自动转换</span></label>
                <input id="source_root" name="source_root" value="{source_root}" placeholder="请输入 Windows 访问路径或 Linux 真实路径" required>
                <label for="mode">mode <span>默认：skip</span></label>
                <input id="mode" name="mode" value="{mode or 'skip'}">
                <button class="primary" type="submit" {run1_disabled}>运行 01 + 02</button>
            </form>
            <div class="status-line">状态：<span class="status {status_class(run1['status'])}">{escape(run1['status'])}</span></div>
            <div class="meta">
                转换后 source_root：{escape(run1.get('converted_source_root') or '')}<br>
                退出码：{escape(str(run1.get('returncode')))}
            </div>
        </section>

        <section class="card">
            <h2>运行 03 + 04</h2>
            <p class="command">命令：{escape(PYTHON_BIN)} 03fast_clip_samples_and_results.py --source_root 输入框1；然后运行 04_calculate_accuracy_to_boundary.py</p>
            <form method="post" action="/run2">
                <button class="secondary" type="submit" {run2_disabled}>运行 03 + 04</button>
            </form>
            <div class="status-line">状态：<span class="status {status_class(run2['status'])}">{escape(run2['status'])}</span></div>
            <div class="meta">
                开始时间：{escape(run2.get('started_at') or '')}<br>
                完成时间：{escape(run2.get('finished_at') or '')}<br>
                退出码：{escape(str(run2.get('returncode')))}
            </div>
        </section>
    </div>

    <section class="result-panel">
        <h2>结果输出</h2>
        <div class="result-grid">
            <div class="result-box">
                <h3>delivery_dir + unit.code</h3>
                <ul>{delivery_items}</ul>
            </div>
            <div class="result-box">
                <h3>args.csv_output</h3>
                <ul>{csv_item}</ul>
            </div>
        </div>
    </section>
</main>
</body>
</html>"""


class ScriptPageHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        parsed = urlparse(self.path)
        print(f"[HTTP] GET {self.path}", flush=True)
        if parsed.path in ("/run1", "/run2"):
            print(f"[HTTP] GET {parsed.path}，重定向到首页", flush=True)
            self.redirect_home()
            return
        if parsed.path in ("", "/", "/index.html", "/main.py"):
            self.respond_html(page_html())
            return
        if parsed.path == "/favicon.ico":
            self.send_response(204)
            self.end_headers()
            return
        print(f"[HTTP] 未识别 GET 路径，返回首页：{parsed.path}", flush=True)
        self.respond_html(page_html(f"未识别的访问地址：{parsed.path}，已返回首页。"))

    def do_POST(self):
        parsed = urlparse(self.path)
        print(f"[HTTP] POST {self.path}", flush=True)
        length = int(self.headers.get("Content-Length", "0"))
        data = self.rfile.read(length).decode("utf-8")
        form = parse_qs(data)

        if parsed.path == "/run1":
            with JOBS_LOCK:
                running = JOBS["run1"]["running"]
            if running:
                self.respond_html(page_html("第一步已经在运行中。"))
                return

            source_root = (form.get("source_root", [""])[0] or "").strip()
            mode = (form.get("mode", ["skip"])[0] or "skip").strip()
            print(f"[RUN1] 页面提交：source_root={source_root}, mode={mode}", flush=True)
            if not source_root:
                print("[RUN1] 未输入 --source_root", flush=True)
                self.respond_html(page_html("请输入 --source_root。"))
                return
            if not mode:
                mode = "skip"
            start_background(run_first_job, source_root, mode)
            self.redirect_home()
            return

        if parsed.path == "/run2":
            with JOBS_LOCK:
                running = JOBS["run2"]["running"]
            print(f"[RUN2] 页面提交：running={running}", flush=True)
            if running:
                self.respond_html(page_html("第二步已经在运行中。"))
                return
            with JOBS_LOCK:
                source_root = JOBS["run1"].get("converted_source_root") or JOBS["run1"].get("source_root") or ""
            if not source_root:
                self.respond_html(page_html("请先在左侧运行一次 01 + 02，让系统记录 source_root。"))
                return
            start_background(run_second_job, source_root)
            self.redirect_home()
            return

        print(f"[HTTP] 未识别 POST 路径，返回首页：{parsed.path}", flush=True)
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

    def log_message(self, format, *args):
        print(f"[HTTP] {self.address_string()} - {format % args}", flush=True)


def main():
    host = os.environ.get("HOST", "0.0.0.0")
    port = int(os.environ.get("PORT", "8000"))
    server = HTTPServer((host, port), ScriptPageHandler)
    print(f"页面已启动：http://{host}:{port}", flush=True)
    print("Windows 访问时可以使用服务器 IP 加端口，例如：http://<linux-ip>:8000", flush=True)
    print(f"脚本目录：{BASE_DIR}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()


