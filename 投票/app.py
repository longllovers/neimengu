from pathlib import Path
import os
import subprocess
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs
from html import escape
import threading
import json


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


BASE_DIR = Path(__file__).resolve().parent

# 默认路径，可以按你的实际情况修改
DEFAULT_DF_PATH = "/media/cangling/nas_folder/专题2_农作物种植用地遥感测量/第三批跟班学习（鄂尔多斯_乌兰察布）\乌兰察布市"

DEFAULT_INPUT1_PATH = "input1"
DEFAULT_OUTPUT1_PATH = "output1"
DEFAULT_INPUT2_PATH = "input2"
DEFAULT_OUTPUT2_PATH = "output2"
DEFAULT_MIN_BACKGROUND_THRESHOLD = "0.5"
DEFAULT_MIN_CLASS_AREA_MU = "1.0"

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
        "min_background_threshold": form_value(form_data, "min_background_threshold", DEFAULT_MIN_BACKGROUND_THRESHOLD),
        "min_class_area_mu": form_value(form_data, "min_class_area_mu", DEFAULT_MIN_CLASS_AREA_MU),
    }


def normalize_values(values):
   
    return {
        "df_path": convert_network_path(values["df_path"]),
        "input1_path": convert_network_path(values["input1_path"]),
        "output1_path": convert_network_path(values["output1_path"]),
        "input2_path": convert_network_path(values["input2_path"]),
        "output2_path": convert_network_path(values["output2_path"]),
        "min_background_threshold": values["min_background_threshold"],
        "min_class_area_mu": values["min_class_area_mu"],
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


def run_command_stream(handler, name, command):
    """运行命令，并用 NDJSON 将命令输出实时发送给浏览器。"""
    command_text = command_to_string(command)
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
    print(f"[执行命令] {command_text}", flush=True)
    print("-" * 80, flush=True)
    send_event("start", name=name, command=command_text)

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

        def forward_stream(stream, prefix):
            try:
                for line in iter(stream.readline, ""):
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
        send_event("done", name=name, returncode=returncode)
    except Exception as exc:
        error_text = f"[运行异常] {name}\n[异常信息] {exc}\n"
        print(error_text, flush=True)
        send_event("output", text=error_text)
        send_event("done", name=name, returncode=-1)

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
        "min_background_threshold": DEFAULT_MIN_BACKGROUND_THRESHOLD,
        "min_class_area_mu": DEFAULT_MIN_CLASS_AREA_MU,
    }

    vote_status_text = ""
    vote_status_class = "run-status"
    merge_status_text = ""
    merge_status_class = "run-status"

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
        <form class="card run-form" method="post" data-status-id="vote_status" onsubmit="return false;">
            <input type="hidden" name="action" value="vote">
            <h2>运行 vote.py</h2>
            <p class="hint">命令：python vote.py --cls_tif input1_path --out_dir output1_path --shp_dir df_path --MIN_BACKGROUND_THRESHOLD 阈值 --MIN_CLASS_AREA_MU 亩数阈值</p>

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
                <div class="field">
                    <label for="min_background_threshold">背景像元比例阈值 <span>默认：{html_escape(defaults["min_background_threshold"])}</span></label>
                    <input id="min_background_threshold" type="number" name="min_background_threshold" min="0" max="1" step="0.01" required value="{html_escape(values["min_background_threshold"])}">
                </div>
                <div class="field">
                    <label for="min_class_area_mu">分类面积保留阈值/亩 <span>默认：{html_escape(defaults["min_class_area_mu"])}</span></label>
                    <input id="min_class_area_mu" type="number" name="min_class_area_mu" min="0" step="0.01" required value="{html_escape(values["min_class_area_mu"])}">
                </div>
                <button class="btn-vote" type="submit">运行 vote.py</button>
            </div>

            <div id="vote_status" class="{vote_status_class}">{vote_status_text}</div>
        </form>

        <form class="card run-form" method="post" data-status-id="merge_status" onsubmit="return false;">
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

    <section id="result_panel" class="card results">
        <div class="result-head">
            <h2 id="result_title">运行输出</h2>
            <span id="result_badge" class="badge">等待运行</span>
        </div>

        <pre id="run_output">尚无运行输出</pre>
    </section>
</main>

<script>
    document.querySelectorAll(".run-form").forEach(function(form) {{
        form.addEventListener("submit", async function(event) {{
            event.preventDefault();

            var statusId = form.getAttribute("data-status-id");
            var statusEl = document.getElementById(statusId);
            var button = form.querySelector("button");
            var originalButtonText = button ? button.textContent : "";
            var titleEl = document.getElementById("result_title");
            var badgeEl = document.getElementById("result_badge");
            var outputEl = document.getElementById("run_output");

            if (statusEl) {{
                statusEl.textContent = "正在运行";
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

            try {{
                var response = await fetch(form.action || window.location.href, {{
                    method: "POST",
                    headers: {{
                        "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
                    }},
                    body: new URLSearchParams(new FormData(form)),
                }});
                if (!response.ok) {{
                    throw new Error("请求失败，HTTP 状态码：" + response.status);
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
                            outputEl.textContent = "[开始运行] " + eventData.name + "\\n";
                            hasOutput = true;
                        }} else if (eventData.type === "output") {{
                            if (!hasOutput) outputEl.textContent = "";
                            outputEl.textContent += eventData.text;
                            hasOutput = true;
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
                        outputEl.scrollTop = outputEl.scrollHeight;
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
            "input1_path": DEFAULT_INPUT1_PATH,
            "output1_path": DEFAULT_OUTPUT1_PATH,
            "input2_path": DEFAULT_INPUT2_PATH,
            "output2_path": DEFAULT_OUTPUT2_PATH,
            "min_background_threshold": DEFAULT_MIN_BACKGROUND_THRESHOLD,
            "min_class_area_mu": DEFAULT_MIN_CLASS_AREA_MU,
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
                "--MIN_BACKGROUND_THRESHOLD",
                values["min_background_threshold"],
                "--MIN_CLASS_AREA_MU",
                values["min_class_area_mu"],
            ]
            result = {"name": "vote.py"}

        elif action == "merge":
            command = [
                sys.executable,
                str(MERGE_SCRIPT),
                "--input-dir",
                values["input2_path"],
                "--output",
                values["output2_path"],
            ]
            result = {"name": "merge_geodata.py"}

        else:
            self.send_json({"error": f"未知 action: {action}"}, status=400)
            return

        self.send_response(200)
        self.send_header("Content-Type", "application/x-ndjson; charset=utf-8")
        self.send_header("Cache-Control", "no-cache, no-transform")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()
        run_command_stream(self, result["name"], command)

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
