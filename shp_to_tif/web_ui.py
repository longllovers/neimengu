#!/usr/bin/env python3
"""SHP 转 TIF 的 HTTP 多标签页界面。"""

from __future__ import annotations

import argparse
import json
import threading
import traceback
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from shp_to_tif import (
    DEFAULT_SHP_THREADS,
    ProcessingOptions,
    convert_network_path,
    result_names,
    run_processing,
)


@dataclass
class Job:
    id: str
    status: str = "queued"
    lines: list[str] = field(default_factory=list)
    lock: threading.Lock = field(default_factory=threading.Lock)

    def log(self, message: str) -> None:
        timestamp = datetime.now().strftime("%H:%M:%S")
        entries = str(message).splitlines() or [""]
        formatted = [f"[{timestamp}] {line}" if line else "" for line in entries]
        with self.lock:
            self.lines.extend(formatted)
        for line in formatted:
            print(line, flush=True)


JOBS: dict[str, Job] = {}
JOBS_LOCK = threading.Lock()


def options_from_form(data: dict) -> ProcessingOptions:
    source_text = str(data.get("source_root", "")).strip()
    if not source_text:
        raise ValueError("请输入 SHP 所在目录")
    input_dir = Path(convert_network_path(source_text))

    output_text = str(data.get("output_dir", "")).strip()
    output_dir = (
        Path(convert_network_path(output_text))
        if output_text
        else input_dir.parent / "output"
    )
    overwrite = bool(data.get("overwrite", True))
    if overwrite:
        rewritten_dir = input_dir
    else:
        shp_output_text = str(data.get("shp_output_dir", "")).strip()
        if not shp_output_text:
            raise ValueError("不覆盖原始输入 SHP 时，必须填写 SHP 保存目录")
        rewritten_dir = Path(convert_network_path(shp_output_text))
    shp_name, tif_name = result_names(
        str(data.get("merged_shp_name", "")), str(data.get("output_tif_name", ""))
    )
    return ProcessingOptions(
        input_dir=input_dir,
        rewritten_dir=rewritten_dir,
        output_dir=output_dir,
        merged_shp_name=shp_name,
        output_tif_name=tif_name,
        class_value=int(data.get("class_value", 2)),
        overwrite=overwrite,
        tif_only=bool(data.get("tif_only", False)),
        # 页面不提供此设置：统一使用 8×8 超采样。
        supersample=8,
        shp_threads=int(data.get("shp_threads", DEFAULT_SHP_THREADS)),
        source_root_text=source_text,
    )


def worker(job: Job, options: ProcessingOptions) -> None:
    job.status = "running"
    job.log("任务已开始。")
    try:
        run_processing(options, job.log)
    except Exception as exc:
        job.log(f"错误：{exc}")
        job.log(traceback.format_exc())
        job.status = "failed"
    else:
        job.status = "completed"


PAGE = r'''<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>SHP 转 TIF</title><style>
:root{font-family:"Microsoft YaHei",system-ui,sans-serif;color:#172033;background:#eef2f7}*{box-sizing:border-box}
body{margin:0}.run{cursor:pointer;border:0;border-radius:7px}
.brand{padding:17px 22px 11px;font-size:22px;font-weight:700;color:#183153}.panel{display:none;margin:0 18px 18px;background:#fff;border-radius:10px;min-height:calc(100vh - 70px);box-shadow:0 6px 25px #24344b18}.panel.active{display:grid;grid-template-columns:minmax(360px,42%) 1fr}
.form{padding:22px;border-right:1px solid #e4e9f0;overflow:auto}.output{padding:22px;display:flex;flex-direction:column;min-width:0}.section{font-size:14px;color:#2d5f9e;margin:4px 0 13px;font-weight:700}.field{margin-bottom:13px}.conditional.hidden{display:none}label{display:block;font-size:13px;margin-bottom:6px;color:#475569}input[type=text],input[type=number]{width:100%;border:1px solid #cbd5e1;border-radius:7px;padding:9px 10px;font:inherit;background:#fbfdff}.row{display:grid;grid-template-columns:1fr 1fr;gap:12px}.checks{display:flex;gap:18px;margin:17px 0;font-size:13px}.actions{display:flex;align-items:center;gap:13px}.run{background:#15803d;color:#fff;padding:11px 18px;font-weight:700}.run:disabled{background:#94a3b8;cursor:not-allowed}.run-state{font-size:14px;font-weight:700;color:#b45309}.run-state.completed{color:#15803d}.run-state.failed{color:#b91c1c}.hint{font-size:12px;color:#718096;line-height:1.6}.status{font-size:13px;margin-bottom:10px;color:#526275}.status.running{color:#b45309}.status.completed{color:#15803d}.status.failed{color:#b91c1c}
pre{margin:0;flex:1;min-height:420px;max-height:calc(100vh - 185px);overflow:auto;background:#101827;color:#d7e3f4;border-radius:8px;padding:15px;white-space:pre-wrap;word-break:break-all;font:13px/1.55 Consolas,monospace}
@media(max-width:850px){.panel.active{grid-template-columns:1fr}.form{border-right:0;border-bottom:1px solid #e4e9f0}.row{grid-template-columns:1fr}}
</style></head><body><div class="brand">SHP 转 TIF</div><main id="panels"></main>
<template id="panel-template"><section class="panel"><form class="form"><div class="section">路径设置</div>
<div class="field"><label>输入 SHP 目录</label><input name="source_root" type="text" required placeholder="直接填写实际存放 SHP 文件的目录"></div>
<div class="field"><label>输出目录（留空默认：输入 SHP 目录上一级\output）</label><input name="output_dir" type="text" placeholder="可直接填写 Windows 网络路径或 Linux 路径"></div>
<div class="field conditional hidden"><label>SHP 另存目录（不覆盖原始输入时必填）</label><input name="shp_output_dir" type="text" disabled placeholder="另存一份 SHP 到此目录，原始 SHP 保持不变"></div>
<div class="section">输出设置</div><div class="row"><div class="field"><label>合并 SHP 名称（二者至少填一个）</label><input name="merged_shp_name" type="text" value="rice_10m_result_0809.shp"></div><div class="field"><label>输出 TIF 名称（留空按 SHP 同名生成）</label><input name="output_tif_name" type="text" placeholder="rice_10m_result_0809.tif"></div></div>
<div class="field"><label>class 值（整数）</label><input name="class_value" type="number" step="1" value="2"></div>
<div class="field"><label>并行处理数</label><input name="shp_threads" type="number" min="1" value="__SHP_THREADS__"></div><div class="checks"><label><input name="overwrite" type="checkbox" checked> 覆盖原始输入 SHP（默认）</label><label><input name="tif_only" type="checkbox"> 仅重建 TIF</label></div>
<div class="actions"><button class="run" type="submit">开始运行</button><span class="run-state" aria-live="polite"></span></div><p class="hint">合并 SHP 和 TIF 位于 output 根目录。</p></form>
<div class="output"><div class="section">实时输出</div><div class="status">尚未运行</div><pre>等待任务开始……</pre></div></section></template>
<script>
const tasks=new Map();
async function startJob(e,id){e.preventDefault();const form=e.currentTarget,btn=form.querySelector('.run'),runState=form.querySelector('.run-state'),panel=document.getElementById(id),pre=panel.querySelector('pre'),status=panel.querySelector('.status');const fd=new FormData(form),body={};for(const[k,v]of fd)body[k]=v;body.overwrite=fd.has('overwrite');body.tif_only=fd.has('tif_only');btn.disabled=true;runState.textContent='正在运行';runState.className='run-state';pre.textContent='正在提交任务……';status.textContent='正在提交';try{const r=await fetch('/api/jobs',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)}),data=await r.json();if(!r.ok)throw new Error(data.error||'提交失败');const task=tasks.get(id);task.job=data.job_id;task.after=0;pre.textContent='';poll(id)}catch(err){status.textContent='提交失败';status.className='status failed';runState.textContent='运行失败';runState.className='run-state failed';pre.textContent=err.message;btn.disabled=false}}
async function poll(id){const task=tasks.get(id);if(!task?.job)return;const panel=document.getElementById(id);try{const r=await fetch(`/api/jobs/${task.job}?after=${task.after}`),data=await r.json();if(!r.ok)throw new Error(data.error||'读取日志失败');const pre=panel.querySelector('pre');if(data.lines.length){pre.textContent+=(pre.textContent?'\n':'')+data.lines.join('\n');task.after=data.next;pre.scrollTop=pre.scrollHeight}const status=panel.querySelector('.status'),runState=panel.querySelector('.run-state');status.textContent={queued:'排队中',running:'运行中',completed:'已完成',failed:'运行失败'}[data.status]||data.status;status.className='status '+data.status;if(data.status==='queued'||data.status==='running'){runState.textContent='正在运行';runState.className='run-state';task.timer=setTimeout(()=>poll(id),800)}else{runState.textContent=data.status==='completed'?'运行完成':'运行失败';runState.className='run-state '+data.status;panel.querySelector('.run').disabled=false}}catch(err){panel.querySelector('.status').textContent='日志连接异常，正在重试';task.timer=setTimeout(()=>poll(id),1800)}}
const id='main-task',panel=document.querySelector('#panel-template').content.firstElementChild.cloneNode(true);panel.id=id;panel.classList.add('active');const form=panel.querySelector('form'),overwriteBox=form.querySelector('[name="overwrite"]'),shpOutputWrap=form.querySelector('.conditional'),shpOutputInput=form.querySelector('[name="shp_output_dir"]');function syncShpOutput(){const needsPath=!overwriteBox.checked;shpOutputWrap.classList.toggle('hidden',!needsPath);shpOutputInput.disabled=!needsPath;shpOutputInput.required=needsPath}overwriteBox.addEventListener('change',syncShpOutput);syncShpOutput();form.addEventListener('submit',e=>startJob(e,id));document.querySelector('#panels').appendChild(panel);tasks.set(id,{job:null,after:0,timer:null});
</script></body></html>'''


class ScriptPageHandler(BaseHTTPRequestHandler):
    """提供页面、创建任务及增量日志查询接口。"""

    def log_message(self, format: str, *args) -> None:
        """关闭 HTTP 请求日志，避免轮询信息刷屏。"""
        return

    def send_content(
        self, content: bytes, content_type: str, status: int = 200
    ) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(content)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(content)

    def send_json(self, data: dict, status: int = 200) -> None:
        content = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_content(content, "application/json; charset=utf-8", status)

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/":
            page = PAGE.replace("__SHP_THREADS__", str(DEFAULT_SHP_THREADS))
            self.send_content(page.encode("utf-8"), "text/html; charset=utf-8")
            return

        prefix = "/api/jobs/"
        if not parsed.path.startswith(prefix):
            self.send_json({"error": "页面不存在"}, 404)
            return
        job_id = parsed.path[len(prefix):]
        with JOBS_LOCK:
            job = JOBS.get(job_id)
        if job is None:
            self.send_json({"error": "任务不存在"}, 404)
            return
        try:
            after = max(0, int(parse_qs(parsed.query).get("after", ["0"])[0]))
        except ValueError:
            after = 0
        with job.lock:
            lines = job.lines[after:]
            next_index = len(job.lines)
        self.send_json(
            {"status": job.status, "lines": lines, "next": next_index}
        )

    def do_POST(self) -> None:
        if urlparse(self.path).path != "/api/jobs":
            self.send_json({"error": "接口不存在"}, 404)
            return
        try:
            content_length = int(self.headers.get("Content-Length", "0"))
            data = json.loads(self.rfile.read(content_length).decode("utf-8"))
            if not isinstance(data, dict):
                raise ValueError("请求内容必须是 JSON 对象")
            options = options_from_form(data)
        except (json.JSONDecodeError, UnicodeDecodeError, TypeError, ValueError) as exc:
            self.send_json({"error": str(exc)}, 400)
            return
        job = Job(id=uuid.uuid4().hex)
        with JOBS_LOCK:
            JOBS[job.id] = job
        threading.Thread(target=worker, args=(job, options), daemon=True).start()
        self.send_json({"job_id": job.id})


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="0.0.0.0", help="HTTP 监听地址")
    parser.add_argument("--port", type=int, default=9009, help="HTTP 监听端口")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    server = ThreadingHTTPServer((args.host, args.port), ScriptPageHandler)
    print(f"页面已启动：http://127.0.0.1:{args.port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n页面服务已停止。", flush=True)
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
