from pathlib import Path
import os
import subprocess
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs
from html import escape
import threading


def convert_network_path(path):
    if path is None:
        return path

    path = str(path).strip()

    if not path:
        return path

    # 把 Windows 的反斜杠 \ 转成 Linux 风格 /
    path = path.replace("\\", "/")
    windows_prefix_list =[]
    for i in range(1,256):
        windows_prefix_list.append(f"//169.254.51.{i}/eaget-1")
        windows_prefix_list.append(f"/169.254.51.{i}/eaget-1")
        windows_prefix_list.append(f"169.254.51.{i}/eaget-1")


    linux_prefix = "/media/cangling/eaget_1_folder"

    for windows_prefix in windows_prefix_list:
        if path.startswith(windows_prefix):
            return path.replace(windows_prefix, linux_prefix, 1)

    return path


BASE_DIR = Path(__file__).resolve().parent

# 默认路径，可以按你的实际情况修改
DEFAULT_DF_PATH = "/media/cangling/eaget_1_folder/专题2_农作物种植用地遥感测量/种植用地-待修正-去除接边"

DEFAULT_INPUT1_PATH = "input1"
DEFAULT_OUTPUT1_PATH = "output1"
DEFAULT_INPUT2_PATH = "input2"
DEFAULT_OUTPUT2_PATH = "output2"

VOTE_SCRIPT = BASE_DIR / "vote.py"
MERGE_SCRIPT = BASE_DIR / "merge_geodata.py"


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
        "input1_path": form_value(form_data, "input1_path", DEFAULT_INPUT1_PATH),
        "output1_path": form_value(form_data, "output1_path", DEFAULT_OUTPUT1_PATH),
        "input2_path": form_value(form_data, "input2_path", DEFAULT_INPUT2_PATH),
        "output2_path": form_value(form_data, "output2_path", DEFAULT_OUTPUT2_PATH),
    }


def normalize_values(values):
   
    return {
        "df_path": convert_network_path(values["df_path"]),
        "input1_path": convert_network_path(values["input1_path"]),
        "output1_path": convert_network_path(values["output1_path"]),
        "input2_path": convert_network_path(values["input2_path"]),
        "output2_path": convert_network_path(values["output2_path"]),
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
    command_text = command_to_string(command)

    print("\n" + "=" * 80, flush=True)
    print(f"[开始运行] {name}", flush=True)
    print(f"[执行命令] {command_text}", flush=True)
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
            "command": command_text,
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
            "command": command_text,
            "returncode": -1,
            "stdout": "",
            "stderr": error_message,
            "output": f"[运行异常] {name}\n[异常信息] {error_message}",
        }


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

        <div class="section-title">执行命令</div>
        <pre>{html_escape(result["command"])}</pre>

        <div class="section-title">运行输出</div>
        <pre>{output}</pre>
    </section>
    """


def build_html(values, result=None):
    defaults = {
        "df_path": DEFAULT_DF_PATH,
        "input1_path": DEFAULT_INPUT1_PATH,
        "output1_path": DEFAULT_OUTPUT1_PATH,
        "input2_path": DEFAULT_INPUT2_PATH,
        "output2_path": DEFAULT_OUTPUT2_PATH,
    }

    result_html = build_result_html(result)

    vote_status_text = ""
    vote_status_class = "run-status"

    merge_status_text = ""
    merge_status_class = "run-status"

    if result:
        if result.get("name") == "vote.py":
            vote_status_text = "运行结束"
            vote_status_class = "run-status done"
        elif result.get("name") == "merge_geodata.py":
            merge_status_text = "运行结束"
            merge_status_class = "run-status done"

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
            width: min(1120px, calc(100% - 32px));
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
            grid-template-columns: repeat(2, minmax(0, 1fr));
            gap: 18px;
            align-items: start;
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

        input {{
            width: 100%;
            height: 42px;
            border: 1px solid #cbd5e1;
            border-radius: 8px;
            padding: 0 12px;
            color: var(--text);
            background: #f8fafc;
            font-size: 14px;
            outline: none;
        }}

        input:focus {{
            border-color: #2563eb;
            background: #fff;
            box-shadow: 0 0 0 4px rgba(37, 99, 235, 0.12);
        }}

        .actions {{
            margin-top: 20px;
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
            margin-top: 18px;
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
            max-height: 320px;
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

        .empty {{
            color: #94a3b8;
            font-style: italic;
        }}

        @media (max-width: 760px) {{
            .page {{
                width: min(100% - 22px, 1120px);
                padding: 22px 0;
            }}

            .layout {{
                grid-template-columns: 1fr;
            }}

            .header h1 {{
                font-size: 24px;
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
        <form class="card run-form" method="post" data-status-id="vote_status">
            <input type="hidden" name="action" value="vote">
            <h2>运行 vote.py</h2>
            <p class="hint">命令：python vote.py --cls_tif input1_path --out_dir output1_path --shp_dir df_path</p>

            <div class="field">
                <input id="df_path" type="text" name="df_path"
                       value="{html_escape(values["df_path"])}"
                       placeholder="{html_escape(defaults["df_path"])}">
            </div>

            <div class="field">
                <label for="input1_path">input1_path <span>默认：{html_escape(defaults["input1_path"])}</span></label>
                <input id="input1_path" type="text" name="input1_path"
                       value="{html_escape(values["input1_path"])}"
                       placeholder="{html_escape(defaults["input1_path"])}">
            </div>

            <div class="field">
                <label for="output1_path">output1_path <span>默认：{html_escape(defaults["output1_path"])}</span></label>
                <input id="output1_path" type="text" name="output1_path"
                       value="{html_escape(values["output1_path"])}"
                       placeholder="{html_escape(defaults["output1_path"])}">
            </div>

            <div class="actions">
                <button class="btn-vote" type="submit">运行 vote.py</button>
            </div>

            <div id="vote_status" class="{vote_status_class}">{vote_status_text}</div>
        </form>

        <form class="card run-form" method="post" data-status-id="merge_status">
            <input type="hidden" name="action" value="merge">
            <h2>运行 merge_geodata.py</h2>
            <p class="hint">命令：python merge_geodata.py --input-dir input2_path --output output2_path</p>

            <div class="field">
                <label for="input2_path">input2_path <span>默认：{html_escape(defaults["input2_path"])}</span></label>
                <input id="input2_path" type="text" name="input2_path"
                       value="{html_escape(values["input2_path"])}"
                       placeholder="{html_escape(defaults["input2_path"])}">
            </div>

            <div class="field">
                <label for="output2_path">output2_path <span>默认：{html_escape(defaults["output2_path"])}</span></label>
                <input id="output2_path" type="text" name="output2_path"
                       value="{html_escape(values["output2_path"])}"
                       placeholder="{html_escape(defaults["output2_path"])}">
            </div>

            <div class="actions">
                <button class="btn-merge" type="submit">运行 merge_geodata.py</button>
            </div>

            <div id="merge_status" class="{merge_status_class}">{merge_status_text}</div>
        </form>
    </section>

    {result_html}
</main>

<script>
    document.querySelectorAll(".run-form").forEach(function(form) {{
        form.addEventListener("submit", function() {{
            var statusId = form.getAttribute("data-status-id");
            var statusEl = document.getElementById(statusId);
            var button = form.querySelector("button");

            if (statusEl) {{
                statusEl.textContent = "正在运行";
                statusEl.className = "run-status running";
            }}

            if (button) {{
                button.disabled = true;
                button.textContent = "正在运行...";
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

    def do_GET(self):
        values = {
            "df_path": DEFAULT_DF_PATH,
            "input1_path": DEFAULT_INPUT1_PATH,
            "output1_path": DEFAULT_OUTPUT1_PATH,
            "input2_path": DEFAULT_INPUT2_PATH,
            "output2_path": DEFAULT_OUTPUT2_PATH,
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

        result = None

        if action == "vote":
            command = [
                sys.executable,
                str(VOTE_SCRIPT),
                "--cls_tif",
                values["input1_path"],
                "--out_dir",
                values["output1_path"],
                "--shp_dir",
                values["df_path"],
            ]
            result = run_command("vote.py", command)

        elif action == "merge":
            command = [
                sys.executable,
                str(MERGE_SCRIPT),
                "--input-dir",
                values["input2_path"],
                "--output",
                values["output2_path"],
            ]
            result = run_command("merge_geodata.py", command)

        else:
            result = {
                "name": "未知操作",
                "command": "",
                "returncode": -1,
                "stdout": "",
                "stderr": f"未知 action: {action}",
                "output": f"未知 action: {action}",
            }

        html_text = build_html(values, result=result)
        self.send_html(html_text)

    def log_message(self, format, *args):
        print(f"[HTTP] {self.address_string()} - {format % args}")


if __name__ == "__main__":
    host = "0.0.0.0"
    port = 8888

    server = HTTPServer((host, port), ScriptRunHandler)
    print(f"网页已启动：http://127.0.0.1:{port}")
    print("按 Ctrl+C 停止服务")

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n服务已停止")
    finally:
        server.server_close()