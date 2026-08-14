import os
import queue
import re
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs


HOST = "0.0.0.0"
PORT = 8894
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
MOUNT_SCRIPT = os.environ.get("COPY_TXT_MOUNT_SCRIPT", os.path.join(SCRIPT_DIR, "mount_new.sh"))
MAX_CONCURRENT_COPIES = 4 


RSYNC_PROGRESS_PATTERN = re.compile(
    r"(?P<percent>\d{1,3})%\s+(?P<speed>[\d.,]+\s*[kKMGTPE]?B/s)"
)


def format_size_gb(size_bytes):
    return f"{size_bytes / (1024 ** 3):.2f}GB"


def format_speed(size_bytes_per_second):
    units = ("B/s", "KB/s", "MB/s", "GB/s", "TB/s")
    speed = max(float(size_bytes_per_second), 0.0)
    for unit in units:
        if speed < 1024 or unit == units[-1]:
            return f"{speed:.1f}{unit}" if unit != "B/s" else f"{speed:.0f}{unit}"
        speed /= 1024


def get_path_size(path):
    if os.path.isfile(path):
        try:
            return os.path.getsize(path)
        except OSError:
            return 0

    total_size = 0
    for root, _, files in os.walk(path):
        for file_name in files:
            try:
                total_size += os.path.getsize(os.path.join(root, file_name))
            except OSError:
                pass
    return total_size


def copy_one_file(index, total, source_path, output_folder, event_queue):
    file_name = os.path.basename(source_path.rstrip("/"))
    try:
        file_size = format_size_gb(os.path.getsize(source_path))
    except OSError:
        file_size = "未知大小"

    prefix = f"[传输中 {index}/{total}] {file_name} | {file_size}"
    event_queue.put(("log", f"{prefix} | 进度 0% | 当前速度 0B/s"))

    recent_output = []
    last_progress = None
    process = None

    try:
        process = subprocess.Popen(
            [
                "rsync",
                "-a",
                "--whole-file",
                "--partial",
                "--info=progress2",
                "--human-readable",
                "--",
                source_path,
                output_folder,
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
        )

        assert process.stdout is not None
        output_buffer = []

        # rsync 使用回车符刷新进度，因此逐字符同时处理 \r 和 \n。
        while True:
            character = process.stdout.read(1)
            if character == "":
                break

            if character in "\r\n":
                output_line = "".join(output_buffer).strip()
                output_buffer.clear()
                if not output_line:
                    continue

                recent_output.append(output_line)
                recent_output = recent_output[-20:]
                progress_match = RSYNC_PROGRESS_PATTERN.search(output_line)
                if progress_match:
                    progress = (
                        progress_match.group("percent"),
                        progress_match.group("speed").replace(" ", ""),
                    )
                    if progress != last_progress:
                        last_progress = progress
                        event_queue.put(
                            (
                                "log",
                                f"{prefix} | 进度 {progress[0]}% | 当前速度 {progress[1]}",
                            )
                        )
            else:
                output_buffer.append(character)

        output_line = "".join(output_buffer).strip()
        if output_line:
            recent_output.append(output_line)
            recent_output = recent_output[-20:]

        return_code = process.wait()
        if return_code == 0:
            event_queue.put(("result", index, True, f"[完成 {index}/{total}] {file_name}"))
        else:
            error_text = recent_output[-1] if recent_output else f"rsync 退出码 {return_code}"
            event_queue.put(
                ("result", index, False, f"[失败 {index}/{total}] {file_name} | {error_text}")
            )
    # 工作线程必须把异常转换为 result 事件，否则主线程会一直等待该任务。
    except Exception as exc:
        event_queue.put(("result", index, False, f"[失败 {index}/{total}] {file_name} | {exc}"))
    finally:
        if process is not None and process.stdout is not None:
            process.stdout.close()
        if process is not None and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()


def copy_one_folder(index, total, source_path, output_folder, event_queue):
    folder_name = os.path.basename(source_path.rstrip("/"))
    destination_path = os.path.join(output_folder, folder_name)
    total_bytes = get_path_size(source_path)
    folder_size = format_size_gb(total_bytes)
    prefix = f"[传输中 {index}/{total}] {folder_name} | {folder_size}"
    event_queue.put(("log", f"{prefix} | 进度 0% | 当前速度 0B/s"))

    process = None
    try:
        process = subprocess.Popen(
            ["cp", "-a", "--", source_path, output_folder],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
        )

        previous_size = 0
        previous_time = time.monotonic()
        while process.poll() is None:
            time.sleep(1)
            current_time = time.monotonic()
            copied_size = get_path_size(destination_path)
            elapsed = max(current_time - previous_time, 0.001)
            current_speed = max(copied_size - previous_size, 0) / elapsed
            progress = (
                min(int(copied_size * 100 / total_bytes), 99)
                if total_bytes > 0
                else 0
            )
            event_queue.put(
                (
                    "log",
                    f"{prefix} | 进度 {progress}% | 当前速度 {format_speed(current_speed)}",
                )
            )
            previous_size = copied_size
            previous_time = current_time

        assert process.stdout is not None
        command_output = process.stdout.read().strip()
        return_code = process.wait()
        if return_code == 0:
            event_queue.put(("log", f"{prefix} | 进度 100% | 当前速度 0B/s"))
            event_queue.put(("result", index, True, f"[完成 {index}/{total}] {folder_name}"))
        else:
            error_text = command_output.splitlines()[-1] if command_output else f"cp 退出码 {return_code}"
            event_queue.put(
                ("result", index, False, f"[失败 {index}/{total}] {folder_name} | {error_text}")
            )
    except Exception as exc:
        event_queue.put(("result", index, False, f"[失败 {index}/{total}] {folder_name} | {exc}"))
    finally:
        if process is not None and process.stdout is not None:
            process.stdout.close()
        if process is not None and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()


def convert_network_path(path):
    if path is None:
        return path

    path = str(path).strip().replace("\\", "/")
    if not path:
        return path

    prefix_mapping = (
        ("//10.10.10.11/data", "/mnt/nas_data"),
        ("//10.10.10.10/4np_share", "/mnt/data/4np/"),
        ("//10.10.10.10/nas_data", "/mnt/nas_data"),
    )
    for windows_prefix, linux_prefix in prefix_mapping:
        if path == windows_prefix:
            return linux_prefix
        if path.startswith(windows_prefix + "/"):
            return linux_prefix.rstrip("/") + path[len(windows_prefix):]
    return path




def iter_copy_logs(txt_path, output_folder, copy_folders=False):
    converted_txt_path = convert_network_path(txt_path)
    converted_output_folder = convert_network_path(output_folder)
    os.makedirs(converted_output_folder, exist_ok=True)

    yield "正在运行"
    yield f"复制模式: {'整个文件夹（cp）' if copy_folders else '单个文件（rsync）'}"
    yield f"TXT路径: {converted_txt_path}"
    yield f"输出文件夹: {converted_output_folder}"

    with open(converted_txt_path, "r", encoding="utf-8") as file:
        file_paths = [line.strip() for line in file if line.strip()]

    total = len(file_paths)
    yield f"共读取到 {total} 个文件路径"

    success_count = 0
    skipped_count = 0
    fail_count = 0

    # 只扫描一次目标文件夹，后续仅按文件名判断是否已存在。
    existing_names = {entry.name for entry in os.scandir(converted_output_folder)}
    copy_tasks = []

    for index, raw_file_path in enumerate(file_paths, start=1):
        source_path = convert_network_path(raw_file_path)
        file_name = os.path.basename(source_path.rstrip("/"))
        destination_path = os.path.join(converted_output_folder, file_name)

        if copy_folders and not os.path.isdir(source_path):
            fail_count += 1
            yield f"[失败 {index}/{total}] 不是有效文件夹: {source_path}"
            continue

        if file_name in existing_names:
            skipped_count += 1
            yield f"[{index}/{total}] 跳过已保存文件: {destination_path}"
            continue

        # 立即占用该文件名，避免 TXT 内多个路径同名时重复复制。
        existing_names.add(file_name)
        copy_tasks.append((index, source_path))

    task_count = len(copy_tasks)
    worker_count = min(MAX_CONCURRENT_COPIES, task_count)
    yield f"预检查完成，待复制 {task_count} 个，跳过 {skipped_count} 个"
    if worker_count:
        yield f"开始复制"

        event_queue = queue.Queue()
        completed_count = 0
        with ThreadPoolExecutor(max_workers=worker_count, thread_name_prefix="rsync-copy") as executor:
            for index, source_path in copy_tasks:
                executor.submit(
                    copy_one_folder if copy_folders else copy_one_file,
                    index,
                    total,
                    source_path,
                    converted_output_folder,
                    event_queue,
                )

            while completed_count < task_count:
                event = event_queue.get()
                if event[0] == "log":
                    yield event[1]
                    continue

                _, _, succeeded, message = event
                completed_count += 1
                if succeeded:
                    success_count += 1
                else:
                    fail_count += 1
                yield message

    yield f"运行完成，成功 {success_count} 个，跳过 {skipped_count} 个，失败 {fail_count} 个"


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
      margin: 0 0 8px;
      font-size: 24px;
    }

    .tab-info {
      margin-bottom: 18px;
      color: #64748b;
      font-size: 14px;
    }

    .tab-info code {
      color: #0f766e;
      font-weight: 700;
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

    .checkbox-label {
      display: flex;
      align-items: center;
      gap: 8px;
      font-weight: 400;
      cursor: pointer;
    }

    .checkbox-label input {
      width: 18px;
      height: 18px;
      margin: 0;
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
    <div class="tab-info">
      <code id="tabId"></code>
    </div>

    <form id="copyForm">
      <input id="tabIdInput" name="tab_id" type="hidden">

      <label for="txtPath">TXT 路径</label>
      <input id="txtPath" name="txt_path" type="text" autocomplete="off" required>

      <label for="outputFolder">输出文件夹路径</label>
      <input id="outputFolder" name="output_folder" type="text" autocomplete="off" required>

      <label class="checkbox-label" for="copyFolders">
        <input id="copyFolders" name="copy_folders" type="checkbox" value="1">
        复制整个文件夹（TXT 中每一行是一个文件夹路径）
      </label>

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
    const tabIdBox = document.getElementById("tabId");
    const tabIdInput = document.getElementById("tabIdInput");
    const runButton = document.getElementById("runButton");
    const mountButton = document.getElementById("mountButton");
    const outputFolder = document.getElementById("outputFolder");
    const statusBox = document.getElementById("status");
    const outputBox = document.getElementById("output");

    // 每次打开或刷新页面都生成新 ID，确保不同标签页不会共用任务身份。
    const tabId = (
      typeof crypto !== "undefined" && typeof crypto.randomUUID === "function"
    )
      ? crypto.randomUUID()
      : `tab-${Date.now()}-${Math.random().toString(16).slice(2)}`;
    tabIdBox.textContent = tabId;
    tabIdInput.value = tabId;

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
      await streamRequest(
        "/mount",
        new URLSearchParams({ tab_id: tabId }),
        "正在挂载",
        "挂载完成",
        (line) => {
        const match = line.match(/^挂载路径：[\s]*(.+?)[\s]*$/);
        if (match) {
          outputFolder.value = match[1];
        }
        }
      );
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

        content_length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(content_length).decode("utf-8")
        form = parse_qs(body)
        tab_id = form.get("tab_id", ["未提供"])[0]
        if not re.fullmatch(r"[A-Za-z0-9-]{1,80}", tab_id):
            tab_id = "无效ID"

        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()

        try:
            self.wfile.write((f"标签页ID: {tab_id}\n").encode("utf-8"))
            self.wfile.flush()

            if self.path == "/run":
                txt_path = form.get("txt_path", [""])[0]
                output_folder = form.get("output_folder", [""])[0]
                copy_folders = form.get("copy_folders", ["0"])[0] == "1"
                log_lines = iter_copy_logs(txt_path, output_folder, copy_folders)
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
