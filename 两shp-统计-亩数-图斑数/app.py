from __future__ import annotations

import csv
import io
import json
import os
import re
import struct
import threading
import traceback
import webbrowser
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse


HOST = "0.0.0.0"
PORT = 9002
SQUARE_METRES_PER_SQUARE_KILOMETRE = 1_000_000
DEFAULT_FOLDER_A = r"\\10.10.10.11\data\北京预测结果传递\地块结果\所有地块结果最新-去除接边"


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




def get_ip_from_source_root(source_root):
    if source_root is None:
        return ""
    match = re.search(r"10\.10\.10\.\d+", str(source_root).strip())
    return match.group(0) if match else ""


def convert_linux_path_to_network_path(path, source_root=""):
    if path is None:
        return path

    path = str(path).strip().replace("\\", "/")
    if not path:
        return path

    source_text = str(source_root or "").strip().replace("\\", "/")
    nas_windows_root = (
        "//10.10.10.10/nas_data"
        if source_text == "//10.10.10.10/nas_data"
        or source_text.startswith("//10.10.10.10/nas_data/")
        else "//10.10.10.11/data"
    )
    prefix_mapping = (
        ("/mnt/data/4np", "//10.10.10.10/4np_share"),
        ("/mnt/nas_data", nas_windows_root),
    )
    for linux_prefix, windows_prefix in prefix_mapping:
        if path == linux_prefix or path.startswith(linux_prefix + "/"):
            return (windows_prefix + path[len(linux_prefix):]).replace("/", "\\")
    return path.replace("/", "\\")



def local_path(value):
    """Make a user-entered path usable on the current operating system."""
    value = str(value or "").strip().strip('"')
    if os.name == "nt":
        return value.replace("/", "\\")
    return convert_network_path(value)


def ring_area(points):
    if len(points) < 3:
        return 0.0
    total = 0.0
    previous = points[-1]
    for current in points:
        total += previous[0] * current[1] - current[0] * previous[1]
        previous = current
    return total / 2.0


def read_prj_text(shp_filename):
    prj_path = Path(shp_filename).with_suffix(".prj")
    if not prj_path.is_file():
        # Windows is case-insensitive, but Linux may contain an upper-case suffix.
        for candidate in prj_path.parent.iterdir():
            if candidate.stem.casefold() == prj_path.stem.casefold() and candidate.suffix.casefold() == ".prj":
                prj_path = candidate
                break
        else:
            return ""
    data = prj_path.read_bytes()
    for encoding in ("utf-8-sig", "gb18030", "latin-1"):
        try:
            return data.decode(encoding).strip()
        except UnicodeDecodeError:
            continue
    return ""


def looks_like_longitude_latitude(bounds):
    xmin, ymin, xmax, ymax = bounds
    return (
        -180 <= xmin <= 180
        and -180 <= xmax <= 180
        and -90 <= ymin <= 90
        and -90 <= ymax <= 90
        and xmin <= xmax
        and ymin <= ymax
    )


def coordinate_transform_for_area(shp_filename, bounds):
    """
    Return (transformer, note). The transformer is used only in memory.

    Metric projected data needs no conversion. Geographic and non-metric data
    is transformed to a local Lambert azimuthal equal-area CRS in metres.
    """
    prj_text = read_prj_text(shp_filename)
    if not prj_text and not looks_like_longitude_latitude(bounds):
        return None, "缺少PRJ，按原坐标为米计算"

    try:
        from pyproj import CRS, Transformer
    except ImportError as exc:
        raise RuntimeError(
            "检测到经纬度或非米制坐标，但未安装 pyproj；请运行 pip install pyproj"
        ) from exc

    if prj_text:
        try:
            source_crs = CRS.from_wkt(prj_text)
        except Exception as exc:
            raise ValueError(f"PRJ坐标系无法识别：{exc}") from exc
        axes = source_crs.axis_info[:2]
        is_metric = (
            source_crs.is_projected
            and len(axes) == 2
            and all(
                axis.unit_conversion_factor is not None
                and abs(axis.unit_conversion_factor - 1.0) < 1e-9
                for axis in axes
            )
        )
        if is_metric:
            return None, ""
        assumption_note = "已在内存中自动转换为等积米制投影"
    else:
        source_crs = CRS.from_epsg(4326)
        assumption_note = "缺少PRJ，已按WGS84经纬度在内存中转换"

    xmin, ymin, xmax, ymax = bounds
    centre_x = (xmin + xmax) / 2.0
    centre_y = (ymin + ymax) / 2.0
    try:
        to_lonlat = Transformer.from_crs(source_crs, CRS.from_epsg(4326), always_xy=True)
        centre_lon, centre_lat = to_lonlat.transform(centre_x, centre_y)
        if not (-180 <= centre_lon <= 180 and -90 <= centre_lat <= 90):
            raise ValueError("转换后的中心点不在有效经纬度范围内")
        area_crs = CRS.from_proj4(
            f"+proj=laea +lat_0={centre_lat:.12f} +lon_0={centre_lon:.12f} "
            "+datum=WGS84 +units=m +no_defs"
        )
        return Transformer.from_crs(source_crs, area_crs, always_xy=True), assumption_note
    except Exception as exc:
        raise ValueError(f"无法自动转换为米制等积投影：{exc}") from exc


def read_shapefile_statistics(filename):
    """
    Return (area_square_metres, feature_count, coordinate_note).

    Supports the ESRI Polygon, PolygonZ and PolygonM record layouts. Shapefile
    polygon rings use opposite winding for shells and holes, so the absolute
    value of the sum of signed ring areas gives the feature area.
    """
    total_area = 0.0
    feature_count = 0
    with open(filename, "rb") as shp:
        header = shp.read(100)
        if len(header) != 100 or struct.unpack(">i", header[:4])[0] != 9994:
            raise ValueError("不是有效的 ESRI Shapefile")
        declared_type = struct.unpack("<i", header[32:36])[0]
        if declared_type not in (5, 15, 25, 0):
            raise ValueError(f"不支持的 SHP 类型 {declared_type}（仅支持面要素）")
        bounds = struct.unpack("<4d", header[36:68])
        coordinate_transformer, coordinate_note = coordinate_transform_for_area(filename, bounds)

        while True:
            record_header = shp.read(8)
            if not record_header:
                break
            if len(record_header) != 8:
                raise ValueError("记录头不完整")
            _, length_words = struct.unpack(">ii", record_header)
            content_length = length_words * 2
            content = shp.read(content_length)
            if len(content) != content_length:
                raise ValueError("记录内容不完整")
            if content_length < 4:
                continue
            shape_type = struct.unpack_from("<i", content, 0)[0]
            if shape_type == 0:
                continue
            if shape_type not in (5, 15, 25):
                raise ValueError(f"记录中包含非面类型 {shape_type}")
            if content_length < 44:
                raise ValueError("面记录长度异常")

            num_parts, num_points = struct.unpack_from("<ii", content, 36)
            if num_parts < 0 or num_points < 0:
                raise ValueError("面记录的部件数或点数无效")
            points_offset = 44 + num_parts * 4
            required = points_offset + num_points * 16
            if required > content_length:
                raise ValueError("面记录坐标数据不完整")
            parts = list(struct.unpack_from(f"<{num_parts}i", content, 44)) if num_parts else []
            points = [
                struct.unpack_from("<dd", content, points_offset + index * 16)
                for index in range(num_points)
            ]
            if coordinate_transformer is not None and points:
                transformed_x, transformed_y = coordinate_transformer.transform(
                    [point[0] for point in points],
                    [point[1] for point in points],
                )
                points = list(zip(transformed_x, transformed_y))
            signed_area = 0.0
            for part_index, start in enumerate(parts):
                end = parts[part_index + 1] if part_index + 1 < len(parts) else num_points
                if not (0 <= start <= end <= num_points):
                    raise ValueError("面记录的部件索引无效")
                signed_area += ring_area(points[start:end])
            total_area += abs(signed_area)
            feature_count += 1
    return total_area, feature_count, coordinate_note


def find_shapefiles(folder):
    files = {}
    # 递归扫描全部子文件夹。网络共享上不逐个调用 is_file()，避免产生大量
    # 额外请求；真正读取时仍会正常报告不存在或无法读取的文件。
    for current_folder, _, filenames in os.walk(folder):
        for filename in filenames:
            if filename.casefold().endswith(".shp"):
                files[filename.casefold()] = Path(current_folder) / filename
    return files


CSV_COLUMNS = [
    "shp文件名",
    "模型平方千米",
    "模型图斑数量",
    "输入平方千米",
    "输入图斑数量",
    "输入-模型平方千米",
    "输入-模型图斑数量",
    "状态",
]


def format_square_kilometres(value):
    return f"{value / SQUARE_METRES_PER_SQUARE_KILOMETRE:.6f}"


def compare_one_shapefile(key, b_file, a_files, position, total, progress=None):
    if progress:
        progress(
            f"{threading.current_thread().name} 开始处理 "
            f"[{position}/{total}]：{b_file.name}"
        )
    row = {column: "" for column in CSV_COLUMNS}
    row["shp文件名"] = b_file.name
    a_file = a_files.get(key)
    if a_file is None:
        row["状态"] = "模型文件夹中缺少同名SHP"
        try:
            b_area, b_count, b_note = read_shapefile_statistics(b_file)
            row["输入平方千米"] = format_square_kilometres(b_area)
            row["输入图斑数量"] = b_count
            if b_note:
                row["状态"] += f"；输入：{b_note}"
        except Exception as exc:
            row["状态"] += f"；输入解析失败：{exc}"
        return row

    try:
        a_area, a_count, a_note = read_shapefile_statistics(a_file)
        b_area, b_count, b_note = read_shapefile_statistics(b_file)
        notes = []
        if a_note:
            notes.append(f"模型：{a_note}")
        if b_note:
            notes.append(f"输入：{b_note}")
        row.update({
            "模型平方千米": format_square_kilometres(a_area),
            "模型图斑数量": a_count,
            "输入平方千米": format_square_kilometres(b_area),
            "输入图斑数量": b_count,
            "输入-模型平方千米": format_square_kilometres(b_area - a_area),
            "输入-模型图斑数量": b_count - a_count,
            "状态": "正常" + ("；" + "；".join(notes) if notes else ""),
        })
    except Exception as exc:
        row["状态"] = f"解析失败：{exc}"
    return row


def compare_folders(folder_a, folder_b, progress=None, max_workers=4):
    folder_a = Path(local_path(folder_a))
    folder_b = Path(local_path(folder_b))
    if not folder_a.is_dir():
        raise FileNotFoundError(f"模型文件夹不存在或无法访问：{folder_a}")
    if not folder_b.is_dir():
        raise FileNotFoundError(f"输入文件夹不存在或无法访问：{folder_b}")
    if progress:
        progress("正在扫描模型文件夹中的 SHP……")
    a_files = find_shapefiles(folder_a)
    if progress:
        progress(f"模型文件夹扫描完成：发现 {len(a_files)} 个 SHP")
        progress("正在扫描输入文件夹中的 SHP……")
    b_files = find_shapefiles(folder_b)
    if progress:
        progress(f"输入文件夹扫描完成：发现 {len(b_files)} 个 SHP")
    if not b_files:
        raise ValueError(f"输入文件夹中没有找到 .shp 文件：{folder_b}")
    if progress:
        progress(f"发现模型 SHP {len(a_files)} 个，输入 SHP {len(b_files)} 个")
        progress(f"使用 {max_workers} 个工作线程，每个 SHP 作为一个独立任务")

    sorted_b_files = sorted(b_files.items(), key=lambda item: item[1].name.casefold())
    rows = [None] * len(sorted_b_files)
    worker_count = min(max_workers, len(sorted_b_files))
    with ThreadPoolExecutor(max_workers=worker_count, thread_name_prefix="SHP线程") as executor:
        future_positions = {
            executor.submit(
                compare_one_shapefile,
                key,
                b_file,
                a_files,
                index + 1,
                len(sorted_b_files),
                progress,
            ): index
            for index, (key, b_file) in enumerate(sorted_b_files)
        }
        completed = 0
        for future in as_completed(future_positions):
            index = future_positions[future]
            row = future.result()
            rows[index] = row
            completed += 1
            if progress:
                progress(
                    f"完成进度 [{completed}/{len(sorted_b_files)}] "
                    f"{row['shp文件名']} → {row['状态']}"
                )
    return rows


def csv_text(rows):
    output = io.StringIO(newline="")
    writer = csv.DictWriter(output, fieldnames=CSV_COLUMNS)
    writer.writeheader()
    writer.writerows(rows)
    return output.getvalue()


def default_output(folder_b):
    folder = Path(local_path(folder_b))
    if not str(folder_b).strip():
        return ""
    return str(folder.parent / "csv" / f"{folder.name}.csv")


@dataclass
class JobState:
    status: str = "idle"
    message: str = "等待运行"
    output_path: str = ""
    rows: list = field(default_factory=list)
    started_at: str = ""
    finished_at: str = ""
    lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def snapshot(self):
        with self.lock:
            return {
                "status": self.status,
                "message": self.message,
                "output_path": self.output_path,
                "rows": self.rows,
                "started_at": self.started_at,
                "finished_at": self.finished_at,
            }


JOBS = {}
JOBS_LOCK = threading.Lock()
JOB_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{8,128}$")


def get_job(job_id):
    if not isinstance(job_id, str) or not JOB_ID_PATTERN.fullmatch(job_id):
        raise ValueError("无效的标签页任务 ID")
    with JOBS_LOCK:
        if job_id not in JOBS:
            JOBS[job_id] = JobState()
        return JOBS[job_id]


def task_log(message):
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{timestamp}] {message}", flush=True)


def run_job(job_id, job, folder_a, folder_b, output_path):
    job_log = lambda message: task_log(f"任务 {job_id[:8]} | {message}")
    with job.lock:
        job.status = "running"
        job.message = "正在运行"
        job.output_path = output_path
        job.rows = []
        job.started_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        job.finished_at = ""
    try:
        job_log("开始执行 SHP 对比")
        job_log(f"模型文件夹：{local_path(folder_a)}")
        job_log(f"输入文件夹：{local_path(folder_b)}")
        job_log(f"输出 CSV：{local_path(output_path)}")
        rows = compare_folders(folder_a, folder_b, progress=job_log)
        output = Path(local_path(output_path))
        output.parent.mkdir(parents=True, exist_ok=True)
        with output.open("w", encoding="utf-8-sig", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=CSV_COLUMNS)
            writer.writeheader()
            writer.writerows(rows)
        with job.lock:
            job.status = "completed"
            job.message = f"运行完成，共处理 {len(rows)} 个 SHP"
            job.output_path = str(output)
            job.rows = rows
            job.finished_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        normal_count = sum(1 for row in rows if str(row["状态"]).startswith("正常"))
        job_log(f"运行完成：共处理 {len(rows)} 个 SHP，正常 {normal_count} 个")
        job_log(f"CSV 已保存：{output}")
    except Exception as exc:
        job_log(f"运行失败：{exc}")
        traceback.print_exc()
        with job.lock:
            job.status = "error"
            job.message = f"运行失败：{exc}"
            job.finished_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")


HTML = r"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>SHP 平方千米与图斑数量对比</title>
<style>
*{box-sizing:border-box}body{margin:0;background:#f5f6f8;color:#202124;font:14px/1.5 Arial,"Microsoft YaHei",sans-serif}
.shell{max-width:1280px;margin:28px auto;padding:0 24px}.card{background:white;border:1px solid #e2e5e9;border-radius:10px;box-shadow:0 2px 10px #0000000a}
header{padding:24px 28px 16px}h1{font-size:24px;margin:0 0 5px}.hint{color:#6b7280}
.panel{padding:25px 28px 30px}.results-section{border-top:1px solid #e5e7eb;padding-top:24px;margin-top:26px}
.field{margin-bottom:18px}label{display:block;font-weight:600;margin-bottom:7px}.input-row{display:flex;gap:8px}input{width:100%;border:1px solid #cbd5e1;border-radius:6px;padding:10px 12px;font-size:14px}
input:focus{outline:2px solid #bfdbfe;border-color:#3b82f6}.button{border:1px solid #cbd5e1;background:white;border-radius:6px;padding:9px 16px;cursor:pointer;white-space:nowrap}
.primary{background:#2563eb;border-color:#2563eb;color:white;font-weight:600}.primary:disabled{background:#93c5fd;border-color:#93c5fd;cursor:not-allowed}
.status{margin-top:18px;padding:12px 14px;border-radius:6px;background:#f1f5f9}.status.running{background:#eff6ff;color:#1d4ed8}.status.completed{background:#f0fdf4;color:#15803d}.status.error{background:#fef2f2;color:#b91c1c}
.toolbar{display:flex;justify-content:space-between;align-items:center;gap:10px;margin-bottom:12px}.result-path{max-width:55%;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}.table-wrap{height:520px;overflow:auto;border:1px solid #dfe3e8;border-radius:6px}
table{border-collapse:separate;border-spacing:0;min-width:1100px;width:100%;font-size:13px}th,td{padding:9px 10px;border-right:1px solid #e5e7eb;border-bottom:1px solid #e5e7eb;text-align:right;white-space:nowrap}
th{position:sticky;top:0;background:#f8fafc;z-index:1;font-weight:600}th:first-child,td:first-child,th:last-child,td:last-child{text-align:left}tr:hover td{background:#fafcff}.empty{text-align:center;padding:70px;color:#94a3b8}
@media(max-width:700px){.shell{padding:0 10px;margin:10px auto}.panel,header{padding-left:16px;padding-right:16px}.input-row{flex-direction:column}.toolbar{align-items:flex-start;flex-direction:column}}
</style></head>
<body><main class="shell"><section class="card">
<header><h1>SHP 平方千米与图斑数量对比</h1><div class="hint">shp统计平方千米与图斑数量</div></header>
<section id="settings" class="panel">
 <div class="field"><label for="folderA">模型shp文件夹</label><input id="folderA" value="__DEFAULT_A__"></div>
 <div class="field"><label for="folderB">输入shp文件夹</label><input id="folderB" placeholder="请输入包含 SHP 的文件夹路径"></div>
 <div class="field"><label for="output">输出 CSV 路径</label><div class="input-row"><input id="output" placeholder="填写待对比shp文件夹后自动生成"><button class="button" id="copy">复制路径</button></div></div>
 <button class="button primary" id="run">运行对比</button>
 <div id="status" class="status">等待运行</div>
 <section id="results" class="results-section">
  <div class="toolbar"><strong>CSV 结果 <span id="count"></span></strong><span id="resultPath" class="hint result-path"></span></div>
  <div class="table-wrap"><div class="empty" id="empty">运行完成后在这里显示结果</div><table id="table" hidden><thead></thead><tbody></tbody></table></div>
 </section>
</section>
</section></main>
<script>
const columns=__COLUMNS__;
const $=id=>document.getElementById(id);
const jobId=(globalThis.crypto&&crypto.randomUUID)
 ? crypto.randomUUID().replaceAll("-","")
 : Date.now().toString(36)+Math.random().toString(36).slice(2);
function suggestedOutput(){
 const b=$("folderB").value.trim().replace(/[\\/]+$/,""); if(!b)return "";
 const slash=Math.max(b.lastIndexOf("/"),b.lastIndexOf("\\")); if(slash<0)return "csv/"+b+".csv";
 const parent=b.slice(0,slash), name=b.slice(slash+1), sep=b.includes("\\")?"\\":"/";
 return parent+sep+"csv"+sep+name+".csv";
}
let autoOutput=true;
$("folderB").addEventListener("input",()=>{if(autoOutput)$("output").value=suggestedOutput()});
$("output").addEventListener("input",()=>autoOutput=false);
$("copy").onclick=async()=>{const value=$("output").value;try{await navigator.clipboard.writeText(value);$("copy").textContent="已复制"}catch(e){$("output").select();document.execCommand("copy");$("copy").textContent="已复制"}setTimeout(()=>$("copy").textContent="复制路径",1200)};
function setStatus(data){
 const box=$("status");box.textContent=data.message;box.className="status "+data.status;
 $("run").disabled=data.status==="running";$("run").textContent=data.status==="running"?"正在运行…":"运行对比";
 const pathBox=$("resultPath"), fullPath=data.output_path||"";
 pathBox.textContent=fullPath?shortPath(fullPath):"";
 pathBox.title=fullPath;
 render(data.rows||[]);
}
function shortPath(value){
 const parts=value.replace(/\\/g,"/").split("/").filter(Boolean);
 return parts.length?parts[parts.length-1]:value;
}
function render(rows){
 $("count").textContent=rows.length?"("+rows.length+")":"";$("empty").hidden=rows.length>0;$("table").hidden=!rows.length;
 if(!rows.length)return;
 $("table").querySelector("thead").innerHTML="<tr>"+columns.map(c=>`<th>${escapeHtml(c)}</th>`).join("")+"</tr>";
 $("table").querySelector("tbody").innerHTML=rows.map(r=>"<tr>"+columns.map(c=>`<td>${escapeHtml(r[c]??"")}</td>`).join("")+"</tr>").join("");
}
function escapeHtml(v){const d=document.createElement("div");d.textContent=String(v);return d.innerHTML}
$("run").onclick=async()=>{
 const payload={job_id:jobId,folder_a:$("folderA").value.trim(),folder_b:$("folderB").value.trim(),output_path:$("output").value.trim()};
 if(!payload.folder_b){setStatus({status:"error",message:"请先填写输入 SHP 文件夹",rows:[]});return}
 if(!payload.output_path){payload.output_path=suggestedOutput();$("output").value=payload.output_path}
 try{const res=await fetch("/api/run",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(payload)});setStatus(await res.json());poll()}catch(e){setStatus({status:"error",message:"请求失败："+e,rows:[]})}
};
let timer;function poll(){clearTimeout(timer);fetch("/api/status?job_id="+encodeURIComponent(jobId)).then(r=>r.json()).then(data=>{setStatus(data);if(data.status==="running")timer=setTimeout(poll,700)}).catch(()=>timer=setTimeout(poll,1500))}
poll();
</script></body></html>"""
HTML = HTML.replace("__DEFAULT_A__", DEFAULT_FOLDER_A.replace("&", "&amp;").replace('"', "&quot;"))
HTML = HTML.replace("__COLUMNS__", json.dumps(CSV_COLUMNS, ensure_ascii=False))


class RequestHandler(BaseHTTPRequestHandler):
    def send_bytes(self, status, data, content_type):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def send_json(self, status, value):
        self.send_bytes(status, json.dumps(value, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        if path == "/":
            self.send_bytes(200, HTML.encode("utf-8"), "text/html; charset=utf-8")
        elif path == "/api/status":
            try:
                job_id = parse_qs(parsed.query).get("job_id", [""])[0]
                self.send_json(200, get_job(job_id).snapshot())
            except ValueError as exc:
                self.send_json(400, {"status": "error", "message": str(exc), "rows": []})
        elif path == "/favicon.ico":
            self.send_bytes(204, b"", "image/x-icon")
        else:
            self.send_json(404, {"message": "Not found"})

    def do_POST(self):
        if urlparse(self.path).path != "/api/run":
            self.send_json(404, {"message": "Not found"})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length > 1024 * 1024:
                raise ValueError("请求内容过大")
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
            job_id = str(payload.get("job_id", "")).strip()
            job = get_job(job_id)
            folder_a = str(payload.get("folder_a", "")).strip()
            folder_b = str(payload.get("folder_b", "")).strip()
            output_path = str(payload.get("output_path", "")).strip()
            if not folder_a or not folder_b:
                raise ValueError("模型文件夹和输入文件夹均不能为空")
            if not output_path:
                output_path = default_output(folder_b)
            with job.lock:
                if job.status == "running":
                    busy = {
                        "status": job.status,
                        "message": job.message,
                        "output_path": job.output_path,
                        "rows": job.rows,
                        "started_at": job.started_at,
                        "finished_at": job.finished_at,
                    }
                else:
                    busy = None
                    job.status = "running"
                    job.message = "正在运行"
                    job.output_path = output_path
                    job.rows = []
                    job.started_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                    job.finished_at = ""
            if busy is not None:
                self.send_json(409, busy)
                return
            threading.Thread(
                target=run_job,
                args=(job_id, job, folder_a, folder_b, output_path),
                daemon=True,
            ).start()
            self.send_json(202, {"status": "running", "message": "正在运行", "rows": [], "output_path": output_path})
        except (ValueError, json.JSONDecodeError) as exc:
            self.send_json(400, {"status": "error", "message": str(exc), "rows": []})

    def log_message(self, fmt, *args):
        # 页面会定时轮询 /api/status；不输出 HTTP 访问日志，避免刷屏。
        return


def main():
    server = ThreadingHTTPServer((HOST, PORT), RequestHandler)
    url = f"http://127.0.0.1:{PORT}"
    task_log(f"SHP 对比页面已启动：{url}")
    task_log("按 Ctrl+C 停止服务")
    threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        task_log("服务已停止")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
