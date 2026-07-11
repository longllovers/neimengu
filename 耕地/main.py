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

    linux_prefix = "/media/cangling/EAGET"
    windows_prefix = f"//{ip}/datadisk"

    if path.startswith(linux_prefix):
        path = path.replace(linux_prefix, windows_prefix, 1)

    return path.replace("/", "\\")

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

    raw_source_root = run1.get("source_root") or ""
    source_root = escape(raw_source_root)
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
            copyable_item(convert_linux_path_to_network_path(item, raw_source_root))
            for item in run1["delivery_dirs"]
        )
    elif run1["status"] == "运行完成":
        delivery_items = "<li>未解析到 人工修改真值文件夹路径。</li>"
    else:
        delivery_items = "<li>第一步完成后显示。</li>"

    csv_output = run2.get("csv_output") or ""
    if csv_output:
        csv_item = copyable_item(convert_linux_path_to_network_path(csv_output, raw_source_root))
    elif run2["status"] == "运行完成":
        csv_item = "<li>未解析到 args.csv_output 路径。</li>"
    else:
        csv_item = "<li>第二步完成后显示。</li>"

    return f"""<!doctype html>
<html lang="zh-CN">
<head>
    <meta charset="utf-8">
    <title>Python 耕地自检 </title>
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
    <h1>耕地自检</h1>
    {message_html}

    <div class="grid">
        <section class="card">
            <h2>运行-1</h2>
            <form method="post" action="/run1">
                <label for="source_root">source_root <span>路径会自动转换</span></label>
                <input id="source_root" name="source_root" value="{source_root}" placeholder="请输入 Windows 访问路径或 Linux 真实路径" required>
                <label for="mode">mode <span>默认：skip</span></label>
                <input id="mode" name="mode" value="{mode or 'skip'}">
                <button class="primary" type="submit" {run1_disabled}>运行-1</button>
            </form>
            <div class="status-line">状态：<span class="status {status_class(run1['status'])}">{escape(run1['status'])}</span></div>
            <div class="meta">
                转换后 source_root：{escape(run1.get('converted_source_root') or '')}<br>
                退出码：{escape(str(run1.get('returncode')))}
            </div>
        </section>

        <section class="card">
            <h2>运行-2</h2>
            <form method="post" action="/run2">
                <button class="secondary" type="submit" {run2_disabled}>运行-2</button>
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
                <h3>需人工修改文件夹路径</h3>
                <ul>{delivery_items}</ul>
            </div>
            <div class="result-box">
                <h3>args.csv_output</h3>
                <ul>{csv_item}</ul>
            </div>
        </div>
    </section>
</main>
<script>
async function copyText(button) {{
    const text = button ? (button.getAttribute("data-copy") || "") : "";
    console.log("[COPY] 点击复制:", text);

    try {{
        fetch("/copy_log?text=" + encodeURIComponent(text), {{
            method: "GET",
            cache: "no-store"
        }}).catch(() => {{}});
    }} catch (error) {{
        console.log("[COPY] 发送打印提示失败:", error);
    }}

    if (!text) {{
        console.log("[COPY] 没有可复制内容");
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

async function refreshPanel() {{
    const sourceInput = document.getElementById("source_root");
    const modeInput = document.getElementById("mode");

    const oldSourceRoot = sourceInput ? sourceInput.value : "";
    const oldMode = modeInput ? modeInput.value : "";
    const activeId = document.activeElement ? document.activeElement.id : "";

    try {{
        const response = await fetch("/", {{
            method: "GET",
            cache: "no-store"
        }});

        if (!response.ok) {{
            console.log("刷新失败:", response.status);
            return;
        }}

        const html = await response.text();
        const parser = new DOMParser();
        const doc = parser.parseFromString(html, "text/html");

        const newMain = doc.querySelector("main");
        const currentMain = document.querySelector("main");

        if (newMain && currentMain) {{
            currentMain.innerHTML = newMain.innerHTML;

            const newSourceInput = document.getElementById("source_root");
            const newModeInput = document.getElementById("mode");

            if (newSourceInput && oldSourceRoot) {{
                newSourceInput.value = oldSourceRoot;
            }}

            if (newModeInput && oldMode) {{
                newModeInput.value = oldMode;
            }}

            if (activeId) {{
                const newActive = document.getElementById(activeId);
                if (newActive) {{
                    newActive.focus();
                    if (newActive.setSelectionRange) {{
                        const length = newActive.value.length;
                        newActive.setSelectionRange(length, length);
                    }}
                }}
            }}
        }}
    }} catch (error) {{
        console.log("刷新异常:", error);
    }}
}}

setInterval(refreshPanel, 3000);
</script>



</body>
</html>"""


class ScriptPageHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        parsed = urlparse(self.path)
        print(f"[HTTP] GET {self.path}", flush=True)
        if parsed.path == "/copy_log":
            text = parse_qs(parsed.query).get("text", [""])[0]
            print(f"[COPY] 点击复制：{text}", flush=True)
            self.send_response(204)
            self.end_headers()
            return
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
    port = int(os.environ.get("PORT", "8889"))
    server = HTTPServer((host, port), ScriptPageHandler)
    print(f"页面已启动：http://{host}:{port}", flush=True)
    print("Windows 访问时可以使用服务器 IP 加端口，例如：http://<linux-ip>:8889", flush=True)
    print(f"脚本目录：{BASE_DIR}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()





