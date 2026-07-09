import json
import contextlib
import io
import os
import traceback
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

from neimeng_xumu_shp_v2 import update_shapefile




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
        windows_prefix_list.append(f"//169.254.51.{i}/eaget-1")
        windows_prefix_list.append(f"/169.254.51.{i}/eaget-1")
        windows_prefix_list.append(f"169.254.51.{i}/eaget-1")

    linux_prefix = "/media/cangling/eaget_1_folder"
    for windows_prefix in windows_prefix_list:
        if path.startswith(windows_prefix):
            return path.replace(windows_prefix, linux_prefix, 1)

    return path


HOST = "0.0.0.0"
PORT = 8000
LOG_PATH = Path(__file__).with_name('web_server.log')




def log_debug(message):
    text = f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {message}"
    print(text, flush=True)
    with LOG_PATH.open("a", encoding="utf-8") as log_file:
        log_file.write(text + "\n")


def describe_path(label, value):
    path = Path(value)
    parent = path.parent
    log_debug(f"{label}: {value}")
    log_debug(f"{label} exists: {path.exists()}")
    log_debug(f"{label} is_file: {path.is_file()}")
    log_debug(f"{label} is_dir: {path.is_dir()}")
    log_debug(f"{label} parent: {parent}")
    log_debug(f"{label} parent exists: {parent.exists()}")


def log_captured_output(label, text):
    text = text.strip()
    if not text:
        return

    log_debug(f"----- {label} begin -----")
    for line in text.splitlines():
        log_debug(line)
    log_debug(f"----- {label} end -----")


def inspect_shapefile_file(shp_path):
    try:
        import geopandas as gpd

        log_debug("========== shapefile inspect start ==========")
        gdf = gpd.read_file(shp_path, rows=5)
        log_debug(f"shapefile preview rows: {len(gdf)}")
        log_debug(f"shapefile columns: {[str(col) for col in gdf.columns]}")
        if not gdf.empty:
            for index, row in gdf.drop(columns="geometry", errors="ignore").iterrows():
                log_debug(f"shapefile row {index}: {row.astype(str).to_dict()}")
        log_debug("========== shapefile inspect end ==========")
    except Exception:
        log_debug("========== shapefile inspect failed ==========")
        log_debug(traceback.format_exc())

def inspect_excel_file(excel_path):
    try:
        import pandas as pd

        path = Path(excel_path)
        log_debug("========== excel inspect start ==========")
        log_debug(f"excel size bytes: {path.stat().st_size if path.exists() else 'missing'}")

        excel_file = pd.ExcelFile(path)
        log_debug(f"excel sheet_names: {excel_file.sheet_names}")

        for sheet_name in excel_file.sheet_names:
            preview = pd.read_excel(
                path,
                sheet_name=sheet_name,
                header=None,
                dtype=str,
                nrows=12,
            )
            non_empty_cells = int(preview.notna().sum().sum())
            non_empty_rows = int(preview.dropna(how="all").shape[0])
            log_debug(
                f"sheet {sheet_name!r}: preview_shape={preview.shape}, "
                f"non_empty_rows={non_empty_rows}, non_empty_cells={non_empty_cells}"
            )

            if non_empty_cells:
                preview = preview.fillna("")
                for index, row in preview.iterrows():
                    values = [str(value) for value in row.tolist()]
                    log_debug(f"sheet {sheet_name!r} row {index + 1}: {values}")

        default_df = pd.read_excel(path, dtype=str)
        log_debug(f"default read_excel shape: {default_df.shape}")
        log_debug(f"default read_excel columns: {[str(col) for col in default_df.columns]}")
        log_debug("========== excel inspect end ==========")
    except Exception:
        log_debug("========== excel inspect failed ==========")
        log_debug(traceback.format_exc())
HTML = """<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>内蒙古畜牧 Shapefile 处理</title>
  <style>
    * {
      box-sizing: border-box;
    }

    body {
      margin: 0;
      min-height: 100vh;
      font-family: "Microsoft YaHei", "Segoe UI", Arial, sans-serif;
      color: #1f2937;
      background: #f5f7fb;
    }

    main {
      width: min(880px, calc(100% - 32px));
      margin: 48px auto;
    }

    h1 {
      margin: 0 0 24px;
      font-size: 28px;
      font-weight: 700;
    }

    form {
      display: grid;
      gap: 18px;
      padding: 28px;
      background: #ffffff;
      border: 1px solid #dce3ee;
      border-radius: 8px;
      box-shadow: 0 12px 32px rgba(15, 23, 42, 0.08);
    }

    label {
      display: grid;
      gap: 8px;
      font-size: 15px;
      font-weight: 600;
    }

    input {
      width: 100%;
      height: 42px;
      padding: 0 12px;
      border: 1px solid #cbd5e1;
      border-radius: 6px;
      font: inherit;
      font-weight: 400;
      color: #111827;
      background: #ffffff;
    }

    input:focus {
      outline: 2px solid #2563eb;
      outline-offset: 1px;
      border-color: #2563eb;
    }

    .actions {
      display: flex;
      align-items: center;
      gap: 16px;
      margin-top: 4px;
      flex-wrap: wrap;
    }

    button {
      height: 42px;
      padding: 0 18px;
      border: 0;
      border-radius: 6px;
      font: inherit;
      font-weight: 700;
      color: #ffffff;
      background: #2563eb;
      cursor: pointer;
    }

    button:disabled {
      background: #94a3b8;
      cursor: wait;
    }

    #status {
      min-height: 24px;
      font-size: 15px;
      color: #475569;
      white-space: pre-wrap;
    }
  </style>
</head>
<body>
  <main>
    <h1>内蒙古畜牧 Shapefile 处理</h1>

    <form id="run-form">
      <label>
        输入 Shapefile 路径
        <input name="shp" required placeholder="例如：D:\\data\\input.shp">
      </label>

      <label>
        输入 Excel 路径
        <input name="excel" required placeholder="例如：D:\\data\\table.xlsx">
      </label>

      <label>
        输出 Shapefile 路径
        <input name="out_shp" required placeholder="例如：D:\\data\\output.shp">
      </label>

      <label>
        Shapefile 序号字段名
        <input name="shp_id_field" value="序号" required>
      </label>

      <div class="actions">
        <button id="run-button" type="submit">执行</button>
        <div id="status">等待执行</div>
      </div>
    </form>
  </main>

  <script>
    const form = document.getElementById("run-form");
    const button = document.getElementById("run-button");
    const statusText = document.getElementById("status");

    form.addEventListener("submit", async (event) => {
      event.preventDefault();

      const formData = new FormData(form);
      const payload = {
        shp: formData.get("shp"),
        excel: formData.get("excel"),
        out_shp: formData.get("out_shp"),
        shp_id_field: formData.get("shp_id_field")
      };

      statusText.textContent = "正在运行";
      button.disabled = true;

      try {
        const response = await fetch("/run", {
          method: "POST",
          headers: {
            "Content-Type": "application/json"
          },
          body: JSON.stringify(payload)
        });

        const result = await response.json();
        if (!response.ok || !result.ok) {
          throw new Error(result.error || "运行失败");
        }

        statusText.textContent =
          "运行完成\\n输出要素：" + result.row_count +
          "\\n删除空信息图斑：" + result.dropped_count +
          "\\n输出路径：" + result.out_shp;
      } catch (error) {
        statusText.textContent = "运行失败\\n" + error.message;
      } finally {
        button.disabled = false;
      }
    });
  </script>
</body>
</html>
"""


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path in ("", "/"):
            self._send_bytes(HTML.encode("utf-8"), "text/html; charset=utf-8")
            return

        self.send_error(404, "Not Found")

    def do_POST(self):
        parsed = urlparse(self.path)
        if parsed.path != "/run":
            self.send_error(404, "Not Found")
            return

        try:
            log_debug("========== /run start ==========")
            log_debug(f"cwd: {os.getcwd()}")
            log_debug(f"client: {self.client_address}")
            log_debug(f"request path: {self.path}")

            content_length = int(self.headers.get("Content-Length", "0"))
            log_debug(f"content_length: {content_length}")

            raw_body = self.rfile.read(content_length)
            decoded_body = raw_body.decode("utf-8")
            log_debug(f"raw body: {decoded_body}")

            payload = json.loads(decoded_body)
            raw_shp = self._required_text(payload, "shp")
            raw_excel = self._required_text(payload, "excel")
            raw_out_shp = self._required_text(payload, "out_shp")
            shp_id_field = self._required_text(payload, "shp_id_field")

            shp = convert_network_path(raw_shp)
            excel = convert_network_path(raw_excel)
            out_shp = convert_network_path(raw_out_shp)

            log_debug(f"raw shp: {raw_shp}")
            log_debug(f"converted shp: {shp}")
            log_debug(f"raw excel: {raw_excel}")
            log_debug(f"converted excel: {excel}")
            log_debug(f"raw out_shp: {raw_out_shp}")
            log_debug(f"converted out_shp: {out_shp}")
            log_debug(f"shp_id_field: {shp_id_field}")

            describe_path("shp", shp)
            describe_path("excel", excel)
            describe_path("out_shp", out_shp)
            inspect_shapefile_file(shp)
            inspect_excel_file(excel)

            stdout_buffer = io.StringIO()
            stderr_buffer = io.StringIO()
            log_debug("calling update_shapefile")
            with contextlib.redirect_stdout(stdout_buffer), contextlib.redirect_stderr(stderr_buffer):
                row_count, dropped_count = update_shapefile(
                    Path(shp),
                    Path(excel),
                    Path(out_shp),
                    encoding="utf-8",
                    shp_id_field=shp_id_field,
                    join_type="inner",
                    debug=True,
                )
            log_captured_output("update_shapefile stdout", stdout_buffer.getvalue())
            log_captured_output("update_shapefile stderr", stderr_buffer.getvalue())
            log_debug(
                f"update_shapefile done: row_count={row_count}, dropped_count={dropped_count}"
            )

            self._send_json(
                {
                    "ok": True,
                    "row_count": row_count,
                    "dropped_count": dropped_count,
                    "out_shp": out_shp,
                }
            )
            log_debug("========== /run success ==========")
        except Exception as exc:
            if "stdout_buffer" in locals():
                log_captured_output("update_shapefile stdout", stdout_buffer.getvalue())
            if "stderr_buffer" in locals():
                log_captured_output("update_shapefile stderr", stderr_buffer.getvalue())
            log_debug("========== /run failed ==========")
            log_debug(f"error: {exc}")
            log_debug(traceback.format_exc())
            self._send_json({"ok": False, "error": str(exc)}, status=500)

    def log_message(self, format, *args):
        log_debug("%s - %s" % (self.address_string(), format % args))

    def _required_text(self, payload, key):
        value = str(payload.get(key, "")).strip()
        if not value:
            raise ValueError(f"缺少参数: {key}")
        return value

    def _send_json(self, data, status=200):
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self._send_bytes(body, "application/json; charset=utf-8", status=status)

    def _send_bytes(self, body, content_type, status=200):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def main():
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    log_debug(f"页面已启动: http://{HOST}:{PORT}")
    server.serve_forever()


if __name__ == "__main__":
    main()













