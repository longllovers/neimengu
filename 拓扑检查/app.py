"""Shapefile 拓扑检查与相交消重 Web 工具。

HTTP 层仅使用 Python 标准库的 ThreadingHTTPServer；空间处理使用
GeoPandas/Shapely。每次运行拥有独立任务和 SSE 日志流，可由多个页面并行使用。
"""

from __future__ import annotations

import argparse
import csv
import json
import mimetypes
import re
import shutil
import tempfile
import threading
import time
import traceback
import uuid
import webbrowser
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from email.parser import BytesParser
from email.policy import default
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path, PurePath
from urllib.parse import unquote, urlparse

import geopandas as gpd
from shapely import area as geometry_area
from shapely import intersection as geometry_intersection
from shapely import make_valid
from shapely.geometry import GeometryCollection, MultiPolygon, Polygon
from shapely.ops import unary_union


ROOT = Path(__file__).resolve().parent
WEB_ROOT = ROOT / "web"
MAX_UPLOAD_BYTES = 1024 * 1024 * 1024
SIDE_CAR_EXTENSIONS = {".shp", ".shx", ".dbf", ".prj", ".cpg", ".qix", ".sbn", ".sbx"}


def convert_network_path(path: str | Path | None) -> str | None:
    """把 Windows NAS 路径转换为服务器上对应的挂载路径。"""
    if path is None:
        return None

    text = str(path).strip()
    if not text:
        return text

    text = text.replace("\\", "/")
    match = re.match(
        r"^(?://|/)?10\.10\.10\.(\d{1,3})/"
        r"(data|新建卷|datadisk2|新加卷)(?=/|$)",
        text,
    )
    if not match or not 1 <= int(match.group(1)) <= 255:
        return text

    mount_mapping = {
        "data": "/media/cangling/nas_folder",
        "新建卷": "/media/cangling/xinjianjuan",
        "datadisk2": "/media/cangling/EAGET",
        "新加卷": "/media/cangling/xinjiajuan",
    }
    return mount_mapping[match.group(2)] + text[match.end():]


def get_ip_from_source_root(source_root: str | Path | None) -> str:
    if source_root is None:
        return ""
    match = re.search(r"10\.10\.10\.(\d{1,3})", str(source_root).strip())
    if match and 1 <= int(match.group(1)) <= 255:
        return match.group(0)
    return ""


def convert_linux_path_to_network_path(
    path: str | Path | None,
    source_root: str | Path | None = "",
) -> str | None:
    """使用用户输入中的 IP，把服务器挂载路径转回 Windows 网络路径。"""
    if path is None:
        return None

    text = str(path).strip()
    if not text:
        return text

    ip = get_ip_from_source_root(source_root)
    if not ip:
        return text

    text = text.replace("\\", "/")
    prefix_mapping = (
        ("/media/cangling/nas_folder", f"//{ip}/data"),
        ("/media/cangling/xinjianjuan", f"//{ip}/新建卷"),
        ("/media/cangling/EAGET", f"//{ip}/datadisk2"),
        ("/media/cangling/xinjiajuan", f"//{ip}/新加卷"),
    )
    for linux_prefix, windows_prefix in prefix_mapping:
        if text == linux_prefix or text.startswith(linux_prefix + "/"):
            relative_path = text[len(linux_prefix):]
            return (windows_prefix + relative_path).replace("/", "\\")
    return text.replace("/", "\\")


HTML = r'''<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Shapefile 拓扑处理</title>
<style>
:root{font-family:Inter,"Microsoft YaHei",sans-serif;color:#172033;background:#f5f7fb}*{box-sizing:border-box}
body{margin:0}.page{max-width:1180px;margin:auto;padding:34px 24px 50px}.head{margin-bottom:22px}.head h1{font-size:27px;margin:0 0 8px}.head p{margin:0;color:#687386}
.card{background:#fff;border:1px solid #e4e8f0;border-radius:14px;box-shadow:0 7px 24px #2538580d;padding:22px;margin-top:16px}
.drop{border:2px dashed #c9d2e3;border-radius:12px;padding:30px;text-align:center;cursor:pointer;transition:.2s}.drop.over{border-color:#3478f6;background:#f5f9ff}.drop b{display:block;margin-bottom:7px}.muted{font-size:13px;color:#7a8495}
.grid{display:grid;grid-template-columns:1fr 1fr;gap:16px;margin-top:18px}.field label{font-size:13px;font-weight:650;display:block;margin-bottom:7px}.field input{width:100%;height:42px;border:1px solid #d9deea;border-radius:8px;padding:0 11px;font-size:14px;outline:none}.field input:focus{border-color:#3478f6;box-shadow:0 0 0 3px #3478f61c}
.actions{display:flex;align-items:center;gap:13px;margin-top:19px}button{border:0;border-radius:8px;background:#246bfd;color:#fff;height:42px;padding:0 25px;font-weight:650;cursor:pointer}button:disabled{opacity:.58;cursor:not-allowed}.status{font-size:14px;color:#6c7687}.status.running{color:#e48816}.status.done{color:#16834a}.status.error{color:#d64242}
.terminal{height:230px;overflow:auto;background:#101722;color:#d9e3f1;border-radius:10px;padding:14px;font:13px/1.7 Consolas,monospace;white-space:pre-wrap}.terminal .err{color:#ff8d8d}
.summary{display:flex;gap:12px;flex-wrap:wrap;margin-bottom:13px}.pill{background:#f1f5fb;border-radius:20px;padding:7px 12px;font-size:13px}.table-wrap{overflow:auto;max-height:430px;border:1px solid #e3e7ef;border-radius:9px}table{width:100%;border-collapse:collapse;font-size:13px;white-space:nowrap}th,td{text-align:left;padding:10px 12px;border-bottom:1px solid #e8ebf1}th{position:sticky;top:0;background:#f7f9fc;color:#4d596d}tr:last-child td{border-bottom:0}.empty{padding:28px;text-align:center;color:#7b8494}
@media(max-width:720px){.grid{grid-template-columns:1fr}.page{padding:20px 12px}.card{padding:16px}}
</style></head><body><main class="page">
<div class="head"><h1>Shapefile 拓扑处理</h1><p>检查无效几何与面重叠，并将相交区域保留在面积最大的要素中。</p></div>
<section class="card">
 <div id="drop" class="drop"><b>拖入 Shapefile 文件</b><span class="muted">请同时拖入 .shp、.shx、.dbf、.prj（也可点击选择）</span><input id="files" type="file" multiple accept=".shp,.shx,.dbf,.prj,.cpg,.qix,.sbn,.sbx" hidden></div>
 <div class="grid">
  <div class="field"><label>Shapefile 路径</label><input id="source" placeholder="拖入后自动填写，也可输入服务器本机绝对路径"></div>
  <div class="field"><label>输出目录</label><input id="output" placeholder="留空时默认保存到 shp 所在文件夹"></div>
 </div>
 <div class="actions"><button id="run">运行</button><span id="status" class="status">等待运行</span></div>
</section>
<section class="card"><h3>终端日志</h3><div id="terminal" class="terminal">等待任务开始……</div></section>
<section class="card"><h3>检查结果</h3><div id="summary" class="summary"></div><div id="result" class="empty">运行完成后在此显示 CSV 内容</div></section>
</main><script>
const $=id=>document.getElementById(id), drop=$('drop'), picker=$('files'); let selected=[];
drop.onclick=()=>picker.click(); picker.onchange=()=>acceptFiles([...picker.files]);
for(const ev of ['dragenter','dragover']) drop.addEventListener(ev,e=>{e.preventDefault();drop.classList.add('over')});
for(const ev of ['dragleave','drop']) drop.addEventListener(ev,e=>{e.preventDefault();drop.classList.remove('over')});
drop.addEventListener('drop',e=>acceptFiles([...e.dataTransfer.files]));
function dirname(p){const i=Math.max(p.lastIndexOf('/'),p.lastIndexOf('\\'));return i>0?p.slice(0,i):''}
function acceptFiles(fs){selected=fs;const shp=fs.find(f=>f.name.toLowerCase().endsWith('.shp'));if(!shp)return;
 const full=shp.path||shp.webkitRelativePath||shp.name;$('source').value=full;$('output').value=dirname(full);
 drop.querySelector('b').textContent=`已选择 ${shp.name}`;drop.querySelector('.muted').textContent=`共 ${fs.length} 个配套文件，可继续修改路径或输出目录`;}
function log(text,error=false){const t=$('terminal');if(t.textContent==='等待任务开始……')t.textContent='';const s=document.createElement('span');if(error)s.className='err';s.textContent=text+'\n';t.appendChild(s);t.scrollTop=t.scrollHeight}
function setStatus(text,cls=''){$('status').textContent=text;$('status').className='status '+cls}
function render(data){const s=data.summary;$('summary').innerHTML=`<span class="pill">文件：${esc(s.source_name)}</span><span class="pill">无效几何：${s.invalid_count}</span><span class="pill">重叠：${s.overlap_count}</span><span class="pill">已调整：${s.changed_count}</span><span class="pill">已删除空记录：${s.deleted_count}</span><span class="pill">CSV：${esc(s.csv_path)}</span><span class="pill">结果 SHP：${esc(s.shp_path)}</span>`;
 const cols=data.columns,rows=data.rows;if(!rows.length){$('result').className='empty';$('result').textContent='没有检查记录';return}let h='<div class="table-wrap"><table><thead><tr>'+cols.map(c=>`<th>${esc(c)}</th>`).join('')+'</tr></thead><tbody>';h+=rows.map(r=>'<tr>'+cols.map(c=>`<td>${esc(r[c]??'')}</td>`).join('')+'</tr>').join('')+'</tbody></table></div>';$('result').className='';$('result').innerHTML=h}
function esc(v){return String(v).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]))}
$('run').onclick=async()=>{if(!$('source').value.trim()&&!selected.length){setStatus('请选择 shp 文件','error');return}$('run').disabled=true;$('terminal').textContent='';$('summary').innerHTML='';$('result').className='empty';$('result').textContent='正在处理……';setStatus('正在运行','running');
 try{const fd=new FormData();fd.append('source_path',$('source').value.trim());fd.append('output_dir',$('output').value.trim());selected.forEach(f=>fd.append('files',f,f.name));const res=await fetch('/api/jobs',{method:'POST',body:fd});const start=await res.json();if(!res.ok||!start.ok)throw new Error(start.error||'任务创建失败');
  await new Promise((resolve,reject)=>{const es=new EventSource('/api/jobs/'+start.job_id+'/events');es.onmessage=e=>{const event=JSON.parse(e.data);if(event.type==='log')log(event.message);else if(event.type==='done'){es.close();render(event.result);setStatus('运行完成','done');resolve()}else if(event.type==='error'){es.close();log(event.message,true);setStatus('运行失败','error');reject(new Error(event.message))}};es.onerror=()=>{es.close();reject(new Error('日志连接中断'))}})
 }catch(e){log(e.message,true);if($('status').textContent!=='运行失败')setStatus('运行失败','error')}finally{$('run').disabled=false}}
</script></body></html>'''


@dataclass
class Job:
    id: str
    events: list[dict[str, object]] = field(default_factory=list)
    finished: bool = False
    condition: threading.Condition = field(default_factory=threading.Condition)

    def emit(self, event_type: str, **values: object) -> None:
        event = {"type": event_type, **values}
        with self.condition:
            self.events.append(event)
            if event_type in {"done", "error"}:
                self.finished = True
            self.condition.notify_all()
        if event_type == "log":
            print(f"[任务 {self.id[:8]}] {values.get('message', '')}", flush=True)


JOBS: dict[str, Job] = {}
JOBS_LOCK = threading.Lock()
OUTPUT_LOCKS: dict[str, threading.Lock] = {}
OUTPUT_LOCKS_GUARD = threading.Lock()


def path_lock(path: Path) -> threading.Lock:
    key = str(path.resolve()).casefold()
    with OUTPUT_LOCKS_GUARD:
        return OUTPUT_LOCKS.setdefault(key, threading.Lock())


def safe_name(name: str) -> str:
    result = PurePath(name.replace("\\", "/")).name
    if not result or result in {".", ".."}:
        raise ValueError("上传文件名无效")
    return result


def polygonal(geometry):
    """从 make_valid 结果中只保留面几何。"""
    if geometry is None or geometry.is_empty:
        return Polygon()
    if isinstance(geometry, (Polygon, MultiPolygon)):
        return geometry
    if isinstance(geometry, GeometryCollection):
        parts = [g for g in geometry.geoms if isinstance(g, (Polygon, MultiPolygon)) and not g.is_empty]
        return unary_union(parts) if parts else Polygon()
    return geometry


def unique_output_path(directory: Path, stem: str, suffix: str) -> Path:
    candidate = directory / f"{stem}{suffix}"
    if not candidate.exists():
        return candidate
    stamp = time.strftime("%Y%m%d_%H%M%S")
    candidate = directory / f"{stem}_{stamp}{suffix}"
    number = 2
    while candidate.exists():
        candidate = directory / f"{stem}_{stamp}_{number}{suffix}"
        number += 1
    return candidate


def process_shapefile(
    source: Path,
    csv_output_dir: Path | None,
    shp_output_dir: Path | None,
    job: Job,
    display_source_root: str = "",
) -> dict[str, object]:
    source = source.resolve()
    if not source.is_file() or source.suffix.lower() != ".shp":
        raise ValueError(f"找不到 Shapefile：{source}")
    for suffix in (".shx", ".dbf", ".prj"):
        if not source.with_suffix(suffix).exists():
            raise ValueError(f"{source.name} 缺少配套文件 {suffix}")

    csv_dir = (csv_output_dir or source.parent).resolve()
    shp_dir = (shp_output_dir or source.parent).resolve()
    csv_dir.mkdir(parents=True, exist_ok=True)
    shp_dir.mkdir(parents=True, exist_ok=True)
    job.emit("log", message=f"读取文件：{source}")
    gdf = gpd.read_file(source)
    if gdf.empty:
        raise ValueError("Shapefile 中没有要素")
    if not all(str(t) in {"Polygon", "MultiPolygon"} for t in gdf.geometry.geom_type):
        raise ValueError("当前相交消重功能仅支持 Polygon/MultiPolygon 面图层")

    original_invalid = [not bool(g.is_valid) for g in gdf.geometry]
    invalid_count = sum(original_invalid)
    rows: list[dict[str, object]] = []
    if invalid_count:
        job.emit("log", message=f"发现 {invalid_count} 个无效几何，处理副本将使用 make_valid 修复")
    else:
        job.emit("log", message="几何有效性检查通过")
    for pos, bad in enumerate(original_invalid):
        if bad:
            rows.append({"shp名称": source.name, "问题类型": "无效几何", "要素索引": str(gdf.index[pos]), "关联要素索引": "", "重叠面积_平方米": "", "处理说明": "已在输出副本中修复"})
    working = gdf.copy()
    working.geometry = [polygonal(make_valid(g)) if not g.is_valid else g for g in working.geometry]

    # 投影副本只用于平方米面积排序。拓扑运算保留源坐标，避免投影浮点误差
    # 把原本仅共边的面变成大量极细小的“伪重叠”。
    metric_crs = gdf.crs
    if gdf.crs is None:
        job.emit("log", message="警告：图层没有 CRS，面积将按原坐标单位计算")
        metric = working.copy()
    elif gdf.crs.is_geographic:
        metric_crs = gdf.estimate_utm_crs()
        if metric_crs is None:
            raise ValueError("无法为经纬度图层估算面积投影")
        job.emit("log", message=f"面积计算临时投影：{metric_crs}")
        metric = working.to_crs(metric_crs)
    else:
        metric = working.copy()

    areas = metric.geometry.area.to_numpy()
    order = sorted(range(len(metric)), key=lambda i: (-areas[i], i))
    rank_by_position = {position: rank for rank, position in enumerate(order)}
    overlap_count = 0
    job.emit("log", message=f"开始检查 {len(metric)} 个面要素的相交重叠")
    spatial_index = working.sindex
    # 先用包围盒批量取得候选对，再让 Shapely/GEOS 一次性计算交集。
    # 相比逐要素调用 overlaps/intersection，这对复杂共享边界快得多。
    bbox_pairs = spatial_index.query(working.geometry)
    smaller_positions: list[int] = []
    larger_positions: list[int] = []
    for current, candidate in zip(bbox_pairs[0], bbox_pairs[1]):
        current_pos, candidate_pos = int(current), int(candidate)
        if current_pos != candidate_pos and rank_by_position[candidate_pos] < rank_by_position[current_pos]:
            smaller_positions.append(current_pos)
            larger_positions.append(candidate_pos)

    # 裁切时必须直接减去较大要素，不能减去预先算出的交集面。
    # 后者在某些相交形态下会把位于大面内部的小面顶点保留为零面积尖刺。
    cutters_by_smaller: dict[int, list[object]] = {}
    if smaller_positions:
        intersections = geometry_intersection(
            working.geometry.array.take(smaller_positions),
            working.geometry.array.take(larger_positions),
        )
        source_areas = geometry_area(intersections)
        positive = [index for index, value in enumerate(source_areas) if float(value) > 0]
        positive_geometries = [intersections[index] for index in positive]
        if gdf.crs is not None and positive_geometries:
            measured = gpd.GeoSeries(positive_geometries, crs=gdf.crs)
            if metric_crs != gdf.crs:
                measured = measured.to_crs(metric_crs)
            measured_areas = list(measured.area)
        else:
            measured_areas = [float(source_areas[index]) for index in positive]
        for intersection_index, inter_area in zip(positive, measured_areas):
            smaller_pos = smaller_positions[intersection_index]
            larger_pos = larger_positions[intersection_index]
            inter = intersections[intersection_index]
            if inter.is_empty or float(inter_area) <= 0:
                continue
            cutters_by_smaller.setdefault(smaller_pos, []).append(
                working.geometry.iloc[larger_pos]
            )
            overlap_count += 1
            rows.append({"shp名称": source.name, "问题类型": "面重叠", "要素索引": str(gdf.index[smaller_pos]), "关联要素索引": str(gdf.index[larger_pos]), "重叠面积_平方米": f"{float(inter_area):.8f}", "处理说明": "相交部分保留在面积较大的要素中"})

    changed: set[int] = set()
    # overlap_count 已在批量交集阶段累计。
    new_geometries = list(working.geometry)
    for pos in order:
        geom = working.geometry.iloc[pos]
        cutters = cutters_by_smaller.get(pos, [])
        if cutters:
            new_geometries[pos] = polygonal(
                geom.difference(unary_union(cutters))
            )
            changed.add(pos)

    working.geometry = new_geometries
    # 一个较小面被较大面完全覆盖时，difference 的结果是空几何。如果仍直接
    # 写出整个 GeoDataFrame，DBF 中可能保留一条没有图形的属性记录。
    # 写出前删除这些行，使 SHP 几何与属性表记录始终一一对应。
    deleted_positions = [
        pos
        for pos, geometry in enumerate(working.geometry)
        if geometry is None or geometry.is_empty
    ]
    for pos in deleted_positions:
        reason = (
            "去除重叠后被完全覆盖，已从输出SHP及属性表删除"
            if pos in changed
            else "几何为空，已从输出SHP及属性表删除"
        )
        rows.append({"shp名称": source.name, "问题类型": "空几何记录", "要素索引": str(gdf.index[pos]), "关联要素索引": "", "重叠面积_平方米": "", "处理说明": reason})

    deleted_count = len(deleted_positions)
    if deleted_count:
        job.emit("log", message=f"删除 {deleted_count} 条空几何记录（属性记录同步删除）")

    # 即便没有问题也始终保存一份结果。
    result_gdf = working.drop(index=working.index[deleted_positions]).copy()
    columns = ["shp名称", "问题类型", "要素索引", "关联要素索引", "重叠面积_平方米", "处理说明"]
    if not rows:
        rows.append({"shp名称": source.name, "问题类型": "检查通过", "要素索引": "", "关联要素索引": "", "重叠面积_平方米": "", "处理说明": "未发现无效几何或面重叠"})

    # 同名源文件共用一把输出锁，多个页面并发时不会同时写同一结果。
    lock_key = Path(
        min(
            str(csv_dir / source.stem).casefold(),
            str(shp_dir / source.stem).casefold(),
        )
        + ".topology-output"
    )
    with path_lock(lock_key):
        output_stem = source.stem
        if (shp_dir / source.name).exists() or (csv_dir / f"{source.stem}.csv").exists():
            stamp = time.strftime("%Y%m%d_%H%M%S")
            output_stem = f"{source.stem}_{stamp}"
            number = 2
            while (shp_dir / f"{output_stem}.shp").exists() or (csv_dir / f"{output_stem}.csv").exists():
                output_stem = f"{source.stem}_{stamp}_{number}"
                number += 1
        output_shp = shp_dir / f"{output_stem}.shp"
        output_csv = csv_dir / f"{output_stem}.csv"
        job.emit("log", message=f"保存处理后的 Shapefile：{output_shp}")
        result_gdf.to_file(output_shp, driver="ESRI Shapefile", encoding="utf-8")
        job.emit("log", message=f"输出 CSV：{output_csv}")
        with output_csv.open("w", encoding="utf-8-sig", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=columns)
            writer.writeheader()
            writer.writerows(rows)

    job.emit("log", message=f"处理完成：无效几何 {invalid_count} 个，重叠记录 {overlap_count} 条，调整要素 {len(changed)} 个，删除空记录 {deleted_count} 条")
    return {"columns": columns, "rows": rows, "summary": {"source_name": source.name, "invalid_count": invalid_count, "overlap_count": overlap_count, "changed_count": len(changed), "deleted_count": deleted_count, "csv_path": convert_linux_path_to_network_path(output_csv, display_source_root), "shp_path": convert_linux_path_to_network_path(output_shp, display_source_root)}}


def run_job(
    job: Job,
    source_text: str,
    csv_output_text: str,
    shp_output_text: str,
    uploads: list[tuple[str, bytes]],
) -> None:
    temp_dir: Path | None = None
    try:
        source = Path(source_text).expanduser() if source_text else None
        sources: list[Path]
        if source is None or not source.is_file():
            if not uploads:
                raise ValueError("请输入有效的 shp 路径，或拖入完整的 Shapefile 配套文件")
            temp_dir = Path(tempfile.mkdtemp(prefix="shp-job-", dir=ROOT))
            for name, payload in uploads:
                suffix = Path(name).suffix.lower()
                if suffix in SIDE_CAR_EXTENSIONS:
                    (temp_dir / safe_name(name)).write_bytes(payload)
            sources = sorted(temp_dir.glob("*.shp"))
            if not sources:
                raise ValueError("没有找到 .shp 文件")
            # 浏览器通常不提供原文件夹绝对路径，上传模式使用页面显示的默认目录。
            default_csv_dir = ROOT / "csv"
            default_shp_dir = ROOT / "output"
        else:
            sources = [source]
            parent_dir = source.parent.parent
            default_csv_dir = parent_dir / "csv"
            default_shp_dir = parent_dir / "output"
        csv_output_dir = (
            Path(convert_network_path(csv_output_text)).expanduser()
            if csv_output_text
            else default_csv_dir
        )
        shp_output_dir = (
            Path(convert_network_path(shp_output_text)).expanduser()
            if shp_output_text
            else default_shp_dir
        )
        # IP 可能来自源路径，也可能只出现在两个输出框之一；合并后用于结果路径反显。
        display_source_root = " ".join(
            value for value in (source_text, csv_output_text, shp_output_text) if value
        )
        job.emit("log", message=f"共收到 {len(sources)} 个 Shapefile，开始并行处理")
        worker_count = min(4, len(sources))
        with ThreadPoolExecutor(
            max_workers=worker_count,
            thread_name_prefix=f"shp-{job.id[:6]}",
        ) as executor:
            results = list(
                executor.map(
                    lambda shp_source: process_shapefile(
                        shp_source,
                        csv_output_dir,
                        shp_output_dir,
                        job,
                        display_source_root,
                    ),
                    sources,
                )
            )
        job.emit("done", result=results[0], results=results)
    except Exception as exc:
        job.emit("log", message=traceback.format_exc())
        job.emit("error", message=str(exc))
    finally:
        if temp_dir is not None:
            shutil.rmtree(temp_dir, ignore_errors=True)


class Handler(BaseHTTPRequestHandler):
    server_version = "ShpTopologyServer/2.0"

    def log_message(self, _format: str, *_args: object) -> None:
        # HTTP 请求日志不进入脚本终端；页面只显示任务自身输出。
        return

    def json_response(self, value: object, status: HTTPStatus = HTTPStatus.OK) -> None:
        body = json.dumps(value, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        path = unquote(urlparse(self.path).path)
        if path == "/":
            self.static_response("index.html")
            return
        if path == "/api/config":
            self.json_response(
                {
                    "defaultCsvOutputDir": str(ROOT / "csv"),
                    "defaultShpOutputDir": str(ROOT / "output"),
                }
            )
            return
        if path.startswith("/static/"):
            self.static_response(path.removeprefix("/static/"))
            return
        if path.startswith("/api/jobs/") and path.endswith("/events"):
            job_id = path.split("/")[3]
            with JOBS_LOCK:
                job = JOBS.get(job_id)
            if not job:
                self.send_error(HTTPStatus.NOT_FOUND)
                return
            self.stream_events(job)
            return
        self.send_error(HTTPStatus.NOT_FOUND)

    def static_response(self, relative: str) -> None:
        requested = (WEB_ROOT / relative).resolve()
        try:
            requested.relative_to(WEB_ROOT.resolve())
        except ValueError:
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        if not requested.is_file():
            # 保留内嵌页面作为 web 目录缺失时的安全回退。
            if relative == "index.html":
                body = HTML.encode("utf-8")
                content_type = "text/html; charset=utf-8"
            else:
                self.send_error(HTTPStatus.NOT_FOUND)
                return
        else:
            body = requested.read_bytes()
            content_type = mimetypes.guess_type(requested.name)[0] or "application/octet-stream"
            if content_type.startswith("text/") or content_type in {"application/javascript", "application/json"}:
                content_type += "; charset=utf-8"
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(body)

    def stream_events(self, job: Job) -> None:
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.end_headers()
        cursor = 0
        try:
            while True:
                with job.condition:
                    if cursor >= len(job.events) and not job.finished:
                        job.condition.wait(timeout=15)
                    events = job.events[cursor:]
                for event in events:
                    data = json.dumps(event, ensure_ascii=False)
                    self.wfile.write(f"data: {data}\n\n".encode("utf-8"))
                    self.wfile.flush()
                    cursor += 1
                if job.finished and cursor >= len(job.events):
                    break
                if not events:
                    self.wfile.write(b": keepalive\n\n")
                    self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass

    def parse_form(self) -> tuple[dict[str, str], list[tuple[str, bytes]]]:
        content_type = self.headers.get("Content-Type", "")
        length = int(self.headers.get("Content-Length", "0"))
        if "multipart/form-data" not in content_type or length <= 0:
            raise ValueError("请求格式无效")
        if length > MAX_UPLOAD_BYTES:
            raise ValueError("上传内容超过 1 GB 限制")
        raw = self.rfile.read(length)
        message = BytesParser(policy=default).parsebytes(f"Content-Type: {content_type}\r\nMIME-Version: 1.0\r\n\r\n".encode() + raw)
        fields: dict[str, str] = {}
        files: list[tuple[str, bytes]] = []
        for part in message.iter_parts():
            field = part.get_param("name", header="content-disposition")
            filename = part.get_filename()
            payload = part.get_payload(decode=True) or b""
            if filename is not None:
                files.append((safe_name(filename), payload))
            elif field:
                fields[field] = payload.decode(part.get_content_charset() or "utf-8", errors="replace")
        return fields, files

    def do_POST(self) -> None:
        if urlparse(self.path).path != "/api/jobs":
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        try:
            fields, files = self.parse_form()
            job = Job(uuid.uuid4().hex)
            with JOBS_LOCK:
                JOBS[job.id] = job
            csv_output_text = fields.get(
                "csv_output_dir",
                fields.get("output_dir", ""),
            ).strip()
            shp_output_text = fields.get(
                "shp_output_dir",
                fields.get("output_dir", ""),
            ).strip()
            thread = threading.Thread(
                target=run_job,
                args=(
                    job,
                    fields.get("source_path", "").strip(),
                    csv_output_text,
                    shp_output_text,
                    files,
                ),
                name=f"shp-job-{job.id[:8]}",
                daemon=True,
            )
            thread.start()
            self.json_response({"ok": True, "job_id": job.id}, HTTPStatus.ACCEPTED)
        except Exception as exc:
            self.json_response({"ok": False, "error": str(exc)}, HTTPStatus.BAD_REQUEST)


class Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="启动 Shapefile 拓扑处理页面")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=9004)
    parser.add_argument(
        "--open-browser",
        action="store_true",
        help="启动服务后自动打开浏览器（默认不打开）",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    server = Server((args.host, args.port), Handler)
    url = f"http://127.0.0.1:{args.port}"
    print(f"Shapefile 拓扑处理页面已启动：{url}")
    print("按 Ctrl+C 停止服务。")
    if args.open_browser:
        threading.Timer(0.7, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n正在停止服务……")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
