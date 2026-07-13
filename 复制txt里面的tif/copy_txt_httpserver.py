import os
import subprocess
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs


HOST = "0.0.0.0"
PORT = 8894
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
MOUNT_SCRIPT = "/media/cangling/nas_folder/code/copy_txt/mount_new.sh"


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


def iter_copy_logs(txt_path, output_folder):
    converted_txt_path = convert_network_path(txt_path)
    converted_output_folder = convert_network_path(output_folder)
    os.makedirs(converted_output_folder, exist_ok=True)

    yield "正在运行"
    yield f"TXT路径: {converted_txt_path}"
    yield f"输出文件夹: {converted_output_folder}"

    with open(converted_txt_path, "r", encoding="utf-8") as file:
        file_paths = [line.strip() for line in file if line.strip()]

    total = len(file_paths)
    yield f"共读取到 {total} 个文件路径"

    success_count = 0
    fail_count = 0

    for index, raw_file_path in enumerate(file_paths, start=1):
        source_path = convert_network_path(raw_file_path)
        yield f"[{index}/{total}] 复制: {source_path}"

        result = subprocess.run(
            ["cp", source_path, converted_output_folder],
            capture_output=True,
            text=True,
        )

        if result.returncode == 0:
            success_count += 1
            yield "  成功"
        else:
            fail_count += 1
            error_text = result.stderr.strip() or result.stdout.strip() or "未知错误"
            yield f"  失败: {error_text}"

    yield f"运行完成，成功 {success_count} 个，失败 {fail_count} 个"


def iter_mount_logs():
    if not os.path.isfile(MOUNT_SCRIPT):
        yield f"挂载失败: 未找到脚本 {MOUNT_SCRIPT}"
        return

    yield "正在启动挂载脚本，请暂时不要插入新硬盘……"

    try:
        process = subprocess.Popen(
            ["sudo", "-n", "/usr/bin/bash", MOUNT_SCRIPT],
            cwd=SCRIPT_DIR,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
    except OSError as exc:
        yield f"挂载失败: 无法启动脚本: {exc}"
        return

    assert process.stdout is not None
    for line in process.stdout:
        yield line.rstrip("\r\n")

    return_code = process.wait()
    if return_code == 0:
        yield "挂载脚本执行完成"
    else:
        yield f"挂载失败: 脚本退出码 {return_code}"


HTML_PAGE = r"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>TXT 文件路径复制工具</title>
  <style>
    * {
      box-sizing: border-box;
    }

    body {
      margin: 0;
      font-family: Arial, "Microsoft YaHei", sans-serif;
      color: #1f2937;
      background: #f3f4f6;
    }

    main {
      width: min(1080px, calc(100% - 32px));
      margin: 28px auto;
      background: #ffffff;
      border: 1px solid #d1d5db;
      border-radius: 8px;
      padding: 24px;
    }

    h1 {
      margin: 0 0 18px;
      font-size: 24px;
    }

    label {
      display: block;
      margin: 16px 0 6px;
      font-weight: 700;
    }

    input {
      width: 100%;
      height: 40px;
      padding: 8px 10px;
      border: 1px solid #9ca3af;
      border-radius: 6px;
      font-size: 15px;
    }

    button {
      margin-top: 18px;
      height: 40px;
      padding: 0 18px;
      border: 0;
      border-radius: 6px;
      color: #ffffff;
      background: #2563eb;
      font-size: 15px;
      cursor: pointer;
    }

    button:disabled {
      background: #9ca3af;
      cursor: not-allowed;
    }

    .mount-button {
      margin-left: 8px;
      background: #0f766e;
    }

    .output-panel {
      margin-top: 24px;
      padding: 24px;
      border: 1px solid #d8dee8;
      border-radius: 8px;
      background: #ffffff;
      box-shadow: 0 16px 44px rgba(15, 23, 42, 0.10);
    }

    .output-header {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 16px;
      margin-bottom: 16px;
    }

    .output-header h2 {
      margin: 0;
      font-size: 22px;
    }

    .status {
      flex: 0 0 auto;
      font-weight: 700;
    }

    pre {
      height: 128px;
      overflow-x: auto;
      overflow-y: scroll;
      margin: 0;
      padding: 16px;
      border: 1px solid #d1d5db;
      border-radius: 8px;
      background: #111827;
      color: #e5e7eb;
      white-space: pre-wrap;
      word-break: break-word;
      scrollbar-color: #64748b #111827;
      scrollbar-width: auto;
    }

    pre::-webkit-scrollbar {
      width: 12px;
    }

    pre::-webkit-scrollbar-thumb {
      border: 3px solid #111827;
      border-radius: 8px;
      background: #64748b;
    }
  </style>
</head>
<body>
  <main>
    <h1>TXT 文件路径复制工具</h1>

    <form id="copyForm">
      <label for="txtPath">TXT 路径</label>
      <input id="txtPath" name="txt_path" type="text" autocomplete="off" required>

      <label for="outputFolder">输出文件夹路径</label>
      <input id="outputFolder" name="output_folder" type="text" autocomplete="off" required>

      <button id="runButton" type="submit">运行</button>
      <button id="mountButton" class="mount-button" type="button">挂载新硬盘</button>
    </form>

    <section class="output-panel">
      <div class="output-header">
        <h2>运行输出</h2>
        <div id="status" class="status">等待运行</div>
      </div>
      <pre id="output">尚无运行输出</pre>
    </section>
  </main>

  <script>
    const form = document.getElementById("copyForm");
    const runButton = document.getElementById("runButton");
    const mountButton = document.getElementById("mountButton");
    const outputFolder = document.getElementById("outputFolder");
    const statusBox = document.getElementById("status");
    const outputBox = document.getElementById("output");

    function setButtonsDisabled(disabled) {
      runButton.disabled = disabled;
      mountButton.disabled = disabled;
    }

    async function streamRequest(url, body, startStatus, finishStatus, onLine) {
      setButtonsDisabled(true);
      statusBox.textContent = startStatus;
      outputBox.textContent = "";

      let allOutput = "";
      let pendingText = "";

      try {
        const response = await fetch(url, {
          method: "POST",
          headers: {
            "Content-Type": "application/x-www-form-urlencoded;charset=UTF-8"
          },
          body
        });

        if (!response.ok || !response.body) {
          throw new Error("请求失败");
        }

        const reader = response.body.getReader();
        const decoder = new TextDecoder("utf-8");

        while (true) {
          const { value, done } = await reader.read();
          if (done) {
            break;
          }

          pendingText += decoder.decode(value, { stream: true });
          const lines = pendingText.split(/\r?\n/);
          pendingText = lines.pop();
          for (const line of lines) {
            allOutput += line + "\n";
            outputBox.textContent += line + "\n";
            if (onLine) {
              onLine(line);
            }
          }
          outputBox.scrollTop = outputBox.scrollHeight;
        }

        pendingText += decoder.decode();
        if (pendingText) {
          allOutput += pendingText + "\n";
          outputBox.textContent += pendingText + "\n";
          if (onLine) {
            onLine(pendingText);
          }
        }
        outputBox.scrollTop = outputBox.scrollHeight;
        statusBox.textContent = allOutput.includes("失败") ? `${startStatus.replace("正在", "")}失败` : finishStatus;
      } catch (error) {
        statusBox.textContent = `${startStatus.replace("正在", "")}失败`;
        outputBox.textContent = String(error);
      } finally {
        setButtonsDisabled(false);
      }
    }

    form.addEventListener("submit", async (event) => {
      event.preventDefault();
      await streamRequest(
        "/run",
        new URLSearchParams(new FormData(form)),
        "正在运行",
        "运行完成"
      );
    });

    mountButton.addEventListener("click", async () => {
      await streamRequest("/mount", "", "正在挂载", "挂载完成", (line) => {
        const match = line.match(/^挂载路径：[\s]*(.+?)[\s]*$/);
        if (match) {
          outputFolder.value = match[1];
        }
      });
    });
  </script>
</body>
</html>
"""


class CopyRequestHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path != "/":
            self.send_error(404)
            return

        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(HTML_PAGE.encode("utf-8"))

    def do_POST(self):
        if self.path not in {"/run", "/mount"}:
            self.send_error(404)
            return

        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()

        try:
            if self.path == "/run":
                content_length = int(self.headers.get("Content-Length", "0"))
                body = self.rfile.read(content_length).decode("utf-8")
                form = parse_qs(body)
                txt_path = form.get("txt_path", [""])[0]
                output_folder = form.get("output_folder", [""])[0]
                log_lines = iter_copy_logs(txt_path, output_folder)
            else:
                log_lines = iter_mount_logs()

            for line in log_lines:
                self.wfile.write((line + "\n").encode("utf-8"))
                self.wfile.flush()
        except Exception as exc:
            action = "运行" if self.path == "/run" else "挂载"
            self.wfile.write((f"{action}失败: {exc}\n").encode("utf-8"))
            self.wfile.flush()

    def log_message(self, format, *args):
        print("%s - %s" % (self.address_string(), format % args))


def main():
    server = ThreadingHTTPServer((HOST, PORT), CopyRequestHandler)
    print(f"服务已启动: http://127.0.0.1:{PORT}")
    server.serve_forever()


if __name__ == "__main__":
    main()
