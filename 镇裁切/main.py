import argparse
import html
import json
import os
import subprocess
import threading
import traceback
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import geopandas as gpd
import pandas as pd


REQUIREMENTS_TEXT = """
Help：
1. 需要输入：县矢量路径、镇边界路径、已完成镇名单 txt 路径、镇影像路径、保存文件路径。
2. 保存路径会产生三个文件夹：镇边界、镇矢量、镇影像。
3. 镇边界输出：读取镇边界 shp，排除已完成镇名单 txt 中的镇，按镇裁剪/导出边界。
4. 镇矢量输出：用镇边界 shp 和县矢量 shp，排除已完成镇名单 txt 中的镇，按镇裁剪县矢量。
5. 镇影像输出：从镇影像路径中查找对应 0.5m 影像文件夹，排除已完成镇，复制到保存路径/镇影像。
"""

EXPECTED_SHP_EXTENSIONS = [".shp", ".shx", ".xml", ".cpg", ".dbf", ".prj", ".sbn", ".sbx"]


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

    for windows_prefix, linux_prefix in prefix_mapping:
        if path.startswith(windows_prefix):
            return path.replace(windows_prefix, linux_prefix, 1)

    return path


@dataclass
class RunArgs:
    county_vector_path: str
    town_boundary_path: str
    completed_town_txt_path: str
    town_image_path: str
    save_path: str


class JobState:
    def __init__(self):
        self.lock = threading.Lock()
        self.running = False
        self.status = "未运行"
        self.log = []
        self.error = ""

    def reset(self):
        with self.lock:
            self.running = True
            self.status = "正在运行"
            self.log = []
            self.error = ""

    def add_log(self, message):
        with self.lock:
            self.log.append(message)
            self.log = self.log[-300:]

    def finish(self, message="运行完成"):
        with self.lock:
            self.running = False
            self.status = message

    def fail(self, message):
        with self.lock:
            self.running = False
            self.status = "运行失败"
            self.error = message

    def snapshot(self):
        with self.lock:
            return {
                "running": self.running,
                "status": self.status,
                "log": self.log,
                "error": self.error,
            }


job_state = JobState()


def safe_name(value):
    value = str(value).strip()
    for char in '<>:"/\\|?*':
        value = value.replace(char, "_")
    return value or "未命名"


def find_shp_files(path):
    source = Path(convert_network_path(path))
    if source.is_file() and source.suffix.lower() == ".shp":
        return [source]
    if source.is_dir():
        shp_files = sorted(
            (item for item in source.rglob("*") if item.is_file() and item.suffix.lower() == ".shp"),
            key=lambda item: str(item),
        )
        if shp_files:
            return shp_files
    raise FileNotFoundError(f"没有找到 shp 文件：{source}")


def find_first_shp(path):
    return find_shp_files(path)[0]


def read_completed_towns(txt_path):
    txt_file = Path(convert_network_path(txt_path))
    if not txt_file.exists():
        return set()

    names = set()
    with txt_file.open("r", encoding="utf-8-sig") as file:
        for line in file:
            line = line.strip()
            if not line:
                continue
            for item in line.replace(",", "\n").replace("，", "\n").splitlines():
                item = item.strip()
                if item:
                    names.add(item)
    return names


def ensure_output_dirs(save_path):
    save_dir = Path(convert_network_path(save_path))
    boundary_dir = save_dir / "镇边界"
    vector_dir = save_dir / "镇矢量"
    image_dir = save_dir / "镇影像"

    for folder in (boundary_dir, vector_dir, image_dir):
        folder.mkdir(parents=True, exist_ok=True)

    return boundary_dir, vector_dir, image_dir


def remove_old_shapefile_files(shp_path):
    shp_path = Path(shp_path)
    stem = shp_path.with_suffix("")
    for item in shp_path.parent.glob(stem.name + ".*"):
        item.unlink()


def ensure_required_shapefile_sidecars(shp_path):
    shp_path = Path(shp_path)
    cpg_path = shp_path.with_suffix(".cpg")
    if not cpg_path.exists():
        cpg_path.write_text("UTF-8", encoding="ascii")

    # GeoPandas/Fiona 通常不会自动生成 xml、sbn、sbx；这里保留同名占位文件，方便下游按固定后缀检查。
    for suffix in (".xml", ".sbn", ".sbx"):
        sidecar = shp_path.with_suffix(suffix)
        if not sidecar.exists():
            sidecar.touch()


def write_shapefile(gdf, shp_path):
    shp_path = Path(shp_path)
    shp_path.parent.mkdir(parents=True, exist_ok=True)
    remove_old_shapefile_files(shp_path)
    gdf.to_file(shp_path, driver="ESRI Shapefile", encoding="utf-8")
    ensure_required_shapefile_sidecars(shp_path)


def load_town_boundaries(town_boundary_path):
    town_shp = find_first_shp(town_boundary_path)
    town_gdf = gpd.read_file(town_shp)

    required_fields = {"XZQDM", "XZQMC"}
    missing_fields = required_fields - set(town_gdf.columns)
    if missing_fields:
        raise ValueError(f"镇边界 shp 缺少字段：{', '.join(sorted(missing_fields))}")

    town_gdf = town_gdf[~town_gdf.geometry.is_empty & town_gdf.geometry.notna()].copy()
    if town_gdf.empty:
        raise ValueError("镇边界 shp 没有有效几何。")

    return town_gdf


def iter_towns(town_gdf, completed_towns):
    filtered = town_gdf[~town_gdf["XZQMC"].astype(str).str.strip().isin(completed_towns)].copy()
    filtered["XZQDM"] = filtered["XZQDM"].astype(str).str.strip()
    filtered["XZQMC"] = filtered["XZQMC"].astype(str).str.strip()

    for (town_code, town_name), group in filtered.groupby(["XZQDM", "XZQMC"], dropna=False):
        dissolved = group.dissolve(by="XZQMC", as_index=False)
        dissolved["XZQDM"] = town_code
        dissolved["XZQMC"] = town_name
        yield str(town_code).strip(), str(town_name).strip(), dissolved


def export_town_boundaries(town_gdf, completed_towns, boundary_dir, log):
    exported_towns = []
    for town_code, town_name, town_boundary in iter_towns(town_gdf, completed_towns):
        output_name = safe_name(f"{town_name}边界.shp")
        output_path = boundary_dir / output_name
        exported_towns.append((town_code, town_name, town_boundary))

        if output_path.exists():
            log(f"镇边界已存在，跳过导出：{town_name}")
            continue

        write_shapefile(town_boundary, output_path)
        log(f"已导出镇边界：{town_name}")
    return exported_towns


def export_town_vectors(county_vector_path, exported_towns, vector_dir, log):
    pending_towns = []
    for town_code, town_name, town_boundary in exported_towns:
        output_path = vector_dir / safe_name(f"{town_name}矢量.shp")
        if output_path.exists():
            log(f"镇矢量已存在，跳过裁剪：{town_name}")
            continue
        pending_towns.append((town_code, town_name, town_boundary))

    if not pending_towns:
        log("所有镇矢量均已存在，无需裁剪。")
        return

    county_shp_files = find_shp_files(county_vector_path)
    log(f"县矢量路径共找到 {len(county_shp_files)} 个 shp，开始读取...")

    county_layers = []
    for county_shp in county_shp_files:
        try:
            county_gdf = gpd.read_file(county_shp)
            county_gdf = county_gdf[
                ~county_gdf.geometry.is_empty & county_gdf.geometry.notna()
            ].copy()
        except Exception as exc:
            log(f"县矢量读取失败，已跳过：{county_shp}；原因：{exc}")
            continue

        if county_gdf.empty:
            log(f"县矢量没有有效几何，已跳过：{county_shp}")
            continue

        county_layers.append((county_shp, county_gdf))

    if not county_layers:
        raise ValueError("县矢量路径下没有可用的 shp 数据。")

    log(f"成功读取 {len(county_layers)} 个县矢量 shp。")

    for _, town_name, town_boundary in pending_towns:
        output_name = safe_name(f"{town_name}矢量.shp")
        output_path = vector_dir / output_name
        clipped_parts = []
        output_crs = None

        for county_shp, county_gdf in county_layers:
            clip_boundary = town_boundary
            if county_gdf.crs and town_boundary.crs and county_gdf.crs != town_boundary.crs:
                clip_boundary = town_boundary.to_crs(county_gdf.crs)

            try:
                clipped = gpd.clip(county_gdf, clip_boundary)
            except Exception as exc:
                log(f"裁剪失败，已跳过：{town_name} <- {county_shp}；原因：{exc}")
                continue

            if clipped.empty:
                continue

            if output_crs is None:
                output_crs = clipped.crs
            elif clipped.crs and output_crs and clipped.crs != output_crs:
                clipped = clipped.to_crs(output_crs)
            clipped_parts.append(clipped)

        if not clipped_parts:
            remove_old_shapefile_files(output_path)
            log(f"镇矢量裁剪结果为空，未导出：{town_name}")
            continue

        merged = gpd.GeoDataFrame(
            pd.concat(clipped_parts, ignore_index=True),
            geometry=clipped_parts[0].geometry.name,
            crs=output_crs,
        )
        write_shapefile(merged, output_path)
        log(
            f"已导出镇矢量：{town_name}，共 {len(merged)} 个要素，"
        )


def find_image_folder(image_root, town_code, town_name):
    image_root = Path(convert_network_path(image_root))
    if not image_root.exists():
        raise FileNotFoundError(f"镇影像路径不存在：{image_root}")

    town_code = str(town_code).strip()
    town_name = str(town_name).strip()

    code_matches = []
    name_matches = []
    for current_root, dirs, _ in os.walk(image_root):
        for dirname in dirs:
            normalized = dirname.lower()
            full_path = Path(current_root) / dirname
            if "0.5m" not in normalized:
                continue
            if town_code and town_code in dirname:
                code_matches.append(full_path)
            elif town_name and town_name in dirname:
                name_matches.append(full_path)

    matches = code_matches or name_matches
    if not matches:
        return None
    return sorted(matches, key=lambda item: len(str(item)))[0]


def copy_image_folder(source_folder, destination_folder, log=None, town_name=""):
    source_folder = Path(source_folder)
    destination_folder = Path(destination_folder)
    destination_folder.mkdir(parents=True, exist_ok=True)

    if log:
        log(f"正在使用 Linux cp 命令复制{town_name}影像...")

    command = ["cp", "-a", f"{source_folder}/.", str(destination_folder)]
    result = subprocess.run(command, capture_output=True, text=True)
    if result.returncode != 0:
        error_message = result.stderr.strip() or result.stdout.strip() or "cp 命令执行失败"
        raise RuntimeError(f"复制{town_name}影像失败：{error_message}")

    if log:
        log(f"{town_name}影像 cp 复制完成。")


def export_town_images(image_root, exported_towns, image_dir, log):
    for town_code, town_name, _ in exported_towns:
        destination = image_dir / safe_name(f"{town_name}影像")
        if destination.exists():
            log(f"镇影像已存在，跳过查找和复制：{town_name}")
            continue

        log(f"正在查找{town_name}影像...")
        source_folder = find_image_folder(image_root, town_code, town_name)
        if source_folder is None:
            log(f"没有找到 0.5m 镇影像：{town_name}（{town_code}）")
            continue

        log(f"找到{town_name}影像，开始复制：{source_folder}")
        copy_image_folder(source_folder, destination, log=log, town_name=town_name)
        log(f"已复制镇影像：{town_name} <- {source_folder}")


def run_task(args, log):
    converted_args = RunArgs(
        county_vector_path=convert_network_path(args.county_vector_path),
        town_boundary_path=convert_network_path(args.town_boundary_path),
        completed_town_txt_path=convert_network_path(args.completed_town_txt_path),
        town_image_path=convert_network_path(args.town_image_path),
        save_path=convert_network_path(args.save_path),
    )

    log("需求如下：")
    for line in REQUIREMENTS_TEXT.strip().splitlines():
        log(line)

    log("开始创建保存目录...")
    boundary_dir, vector_dir, image_dir = ensure_output_dirs(converted_args.save_path)

    log("开始读取已完成镇名单...")
    completed_towns = read_completed_towns(converted_args.completed_town_txt_path)
    log(f"已完成镇数量：{len(completed_towns)}")

    log("开始读取镇边界...")
    town_gdf = load_town_boundaries(converted_args.town_boundary_path)

    log("开始导出镇边界...")
    exported_towns = export_town_boundaries(town_gdf, completed_towns, boundary_dir, log)
    log(f"需要处理的镇数量：{len(exported_towns)}")

    log("开始裁剪并导出镇矢量...")
    export_town_vectors(converted_args.county_vector_path, exported_towns, vector_dir, log)

    log("开始查找并复制镇影像...")
    export_town_images(converted_args.town_image_path, exported_towns, image_dir, log)

    log("全部任务完成。")


HTML_PAGE = """<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>镇数据裁剪工具</title>
  <style>
    body {
      margin: 0;
      font-family: Arial, "Microsoft YaHei", sans-serif;
      color: #1f2937;
      background: #f5f7fb;
    }
    main {
      max-width: 980px;
      margin: 32px auto;
      padding: 0 20px;
    }
    h1 {
      margin: 0 0 18px;
      font-size: 26px;
      font-weight: 700;
    }
    form {
      background: #fff;
      border: 1px solid #dde3ee;
      border-radius: 8px;
      padding: 20px;
      box-shadow: 0 8px 24px rgba(15, 23, 42, 0.06);
    }
    label {
      display: block;
      margin: 14px 0 6px;
      font-weight: 700;
    }
    input {
      box-sizing: border-box;
      width: 100%;
      height: 40px;
      border: 1px solid #c9d3e1;
      border-radius: 6px;
      padding: 8px 10px;
      font-size: 14px;
    }
    button {
      margin-top: 18px;
      height: 40px;
      min-width: 120px;
      border: 0;
      border-radius: 6px;
      background: #2563eb;
      color: #fff;
      font-size: 15px;
      font-weight: 700;
      cursor: pointer;
    }
    button:disabled {
      background: #94a3b8;
      cursor: wait;
    }
    .panel {
      margin-top: 18px;
      background: #fff;
      border: 1px solid #dde3ee;
      border-radius: 8px;
      padding: 16px;
    }
    .status {
      font-weight: 700;
      color: #0f766e;
    }
    .error {
      color: #b91c1c;
      white-space: pre-wrap;
    }
    pre {
      max-height: 360px;
      overflow: auto;
      margin: 12px 0 0;
      padding: 12px;
      background: #0f172a;
      color: #e5e7eb;
      border-radius: 6px;
      font-size: 13px;
      line-height: 1.5;
      white-space: pre-wrap;
    }
  </style>
</head>
<body>
  <main>
    <h1>镇边界、镇矢量、镇影像处理工具</h1>
    <form id="run-form">
      <label for="county_vector_path">县矢量路径</label>
      <input id="county_vector_path" name="county_vector_path" required>

      <label for="town_boundary_path">镇边界路径</label>
      <input id="town_boundary_path" name="town_boundary_path" required>

      <label for="completed_town_txt_path">已完成镇名单路径（txt）</label>
      <input id="completed_town_txt_path" name="completed_town_txt_path" required>

      <label for="town_image_path">镇影像路径</label>
      <input id="town_image_path" name="town_image_path" required>

      <label for="save_path">保存文件路径</label>
      <input id="save_path" name="save_path" required>

      <button id="run-button" type="submit">运行</button>
    </form>

    <section class="panel">
      <div>状态：<span id="status" class="status">未运行</span></div>
      <div id="error" class="error"></div>
      <pre id="log"></pre>
    </section>
  </main>

  <script>
    const form = document.getElementById("run-form");
    const button = document.getElementById("run-button");
    const statusBox = document.getElementById("status");
    const logBox = document.getElementById("log");
    const errorBox = document.getElementById("error");
    let timer = null;

    async function refreshStatus() {
      const response = await fetch("/status");
      const data = await response.json();
      statusBox.textContent = data.status || "未知";
      logBox.textContent = (data.log || []).join("\\n");
      errorBox.textContent = data.error || "";
      button.disabled = Boolean(data.running);
      if (!data.running && timer) {
        clearInterval(timer);
        timer = null;
      }
    }

    form.addEventListener("submit", async (event) => {
      event.preventDefault();
      button.disabled = true;
      statusBox.textContent = "正在运行";
      errorBox.textContent = "";
      logBox.textContent = "";

      const formData = new FormData(form);
      await fetch("/run", {
        method: "POST",
        body: new URLSearchParams(formData)
      });

      if (!timer) {
        timer = setInterval(refreshStatus, 1000);
      }
      refreshStatus();
    });

    refreshStatus();
  </script>
</body>
</html>
"""


class AppHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path == "/":
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(HTML_PAGE.encode("utf-8"))
            return

        if parsed.path == "/status":
            self.send_json(job_state.snapshot())
            return

        self.send_response(404)
        self.end_headers()

    def do_POST(self):
        parsed = urlparse(self.path)
        if parsed.path != "/run":
            self.send_response(404)
            self.end_headers()
            return

        if job_state.snapshot()["running"]:
            self.send_json({"ok": False, "message": "任务正在运行"})
            return

        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length).decode("utf-8")
        form = parse_qs(body)

        try:
            args = RunArgs(
                county_vector_path=self.get_form_value(form, "county_vector_path"),
                town_boundary_path=self.get_form_value(form, "town_boundary_path"),
                completed_town_txt_path=self.get_form_value(form, "completed_town_txt_path"),
                town_image_path=self.get_form_value(form, "town_image_path"),
                save_path=self.get_form_value(form, "save_path"),
            )
        except ValueError as exc:
            self.send_json({"ok": False, "message": str(exc)})
            return

        job_state.reset()
        thread = threading.Thread(target=self.worker, args=(args,), daemon=True)
        thread.start()
        self.send_json({"ok": True, "message": "正在运行"})

    def get_form_value(self, form, key):
        value = form.get(key, [""])[0].strip()
        if not value:
            raise ValueError(f"缺少参数：{key}")
        return value

    def worker(self, args):
        try:
            run_task(args, job_state.add_log)
        except Exception as exc:
            job_state.add_log(traceback.format_exc())
            job_state.fail(str(exc))
        else:
            job_state.finish("运行完成")

    def send_json(self, data):
        payload = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, format, *args):
        return


def print_requirements():
    print(REQUIREMENTS_TEXT.strip())


def parse_args():
    parser = argparse.ArgumentParser(description="镇边界、镇矢量、镇影像处理工具")
    parser.add_argument("--host", default="0.0.0.0", help="服务监听地址")
    parser.add_argument("--port", type=int, default=8891, help="服务端口")
    parser.add_argument("--print-requirements", action="store_true", help="只输出需求整理")
    return parser.parse_args()


def main():
    args = parse_args()
    print_requirements()

    if args.print_requirements:
        return

    server = ThreadingHTTPServer((args.host, args.port), AppHandler)
    url = f"http://{args.host}:{args.port}"
    print(f"\n服务已启动：{url}")
    print("按 Ctrl+C 停止服务。")
    server.serve_forever()


if __name__ == "__main__":
    main()
