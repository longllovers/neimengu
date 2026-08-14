const tabId = (crypto.randomUUID ? crypto.randomUUID() : `${Date.now()}-${Math.random()}`)
  .replace(/[^A-Za-z0-9_-]/g, "_");

const elements = {
  form: document.querySelector("#checkForm"),
  tabId: document.querySelector("#tabId"),
  sourceRoot: document.querySelector("#sourceRoot"),
  outputRoot: document.querySelector("#outputRoot"),
  gdbSchema: document.querySelector("#gdbSchema"),
  zpjSchema: document.querySelector("#zpjSchema"),
  convertedHint: document.querySelector("#convertedHint"),
  outputHint: document.querySelector("#outputHint"),
  runButton: document.querySelector("#runButton"),
  runButtonText: document.querySelector("#runButtonText"),
  statusIcon: document.querySelector("#statusIcon"),
  statusTitle: document.querySelector("#statusTitle"),
  statusDetail: document.querySelector("#statusDetail"),
  startTime: document.querySelector("#startTime"),
  finishTime: document.querySelector("#finishTime"),
  reportPath: document.querySelector("#reportPath"),
  copyPathButton: document.querySelector("#copyPathButton"),
  openPdfButton: document.querySelector("#openPdfButton"),
  emptyViewer: document.querySelector("#emptyViewer"),
  pdfViewer: document.querySelector("#pdfViewer"),
  toast: document.querySelector("#toast"),
};

elements.tabId.textContent = tabId;
let pollTimer = null;
let currentReportPath = "";
let lastPdfUrl = "";
let outputManuallyEdited = false;
let autoFillingOutput = false;

function defaultOutputPath(sourcePath) {
  const trimmed = sourcePath.trim().replace(/[\\/]+$/, "");
  if (!trimmed) return "";
  const slashIndex = Math.max(trimmed.lastIndexOf("/"), trimmed.lastIndexOf("\\"));
  if (slashIndex < 0) return `${trimmed}_检查结果`;
  const parent = trimmed.slice(0, slashIndex);
  const name = trimmed.slice(slashIndex + 1);
  const separator = trimmed[slashIndex];
  return `${parent}${separator}${name}_检查结果`;
}

elements.sourceRoot.addEventListener("input", () => {
  if (outputManuallyEdited && elements.outputRoot.value.trim()) return;
  autoFillingOutput = true;
  elements.outputRoot.value = defaultOutputPath(elements.sourceRoot.value);
  autoFillingOutput = false;
});

elements.outputRoot.addEventListener("input", () => {
  if (!autoFillingOutput) outputManuallyEdited = true;
});

function setToast(message) {
  elements.toast.textContent = message;
  window.clearTimeout(setToast.timer);
  setToast.timer = window.setTimeout(() => { elements.toast.textContent = ""; }, 2600);
}

function setRunning(running) {
  elements.runButton.disabled = running;
  elements.runButton.classList.toggle("running", running);
  elements.runButtonText.textContent = running ? "正在运行" : "开始运行";
}

function updateStatus(state) {
  const status = state.status || "idle";
  const iconClass = status === "queued" ? "running" : status;
  elements.statusIcon.className = `status-icon ${iconClass}`;
  elements.statusTitle.textContent = state.message || "等待运行";
  elements.startTime.textContent = `开始时间：${state.started_at || "—"}`;
  elements.finishTime.textContent = `结束时间：${state.finished_at || "—"}`;

  if (status === "running" || status === "queued") {
    setRunning(true);
    elements.statusDetail.textContent = "后台线程正在检查文件和属性表，请保持页面打开。";
  } else if (status === "completed") {
    setRunning(false);
    const conclusion = state.passed ? "通过" : "不通过";
    elements.statusDetail.textContent =
      `检查结论：${conclusion}；错误 ${state.errors} 项，警告 ${state.warnings} 项。`;
    showPdf(state);
  } else if (status === "failed") {
    setRunning(false);
    elements.statusDetail.textContent = state.exception || "运行失败，请检查服务器日志。";
  } else {
    setRunning(false);
  }
}

function showPdf(state) {
  if (!state.pdf_url) return;
  const pdfUrl = `${state.pdf_url}&_=${Date.now()}`;
  currentReportPath = state.report_network_path || state.report_path || "";
  elements.reportPath.textContent = currentReportPath;
  elements.copyPathButton.disabled = !currentReportPath;
  elements.openPdfButton.classList.remove("disabled");
  elements.openPdfButton.href = pdfUrl;
  elements.emptyViewer.hidden = true;
  elements.emptyViewer.style.display = "none";
  elements.pdfViewer.hidden = false;
  elements.pdfViewer.style.display = "block";
  if (lastPdfUrl !== state.pdf_url) {
    elements.pdfViewer.src = pdfUrl;
    lastPdfUrl = state.pdf_url;
  }
}

async function fetchStatus() {
  try {
    const response = await fetch(`/api/status?tab_id=${encodeURIComponent(tabId)}`, {
      cache: "no-store",
    });
    const state = await response.json();
    if (!response.ok) throw new Error(state.error || "无法获取运行状态");
    updateStatus(state);
    if (state.status === "completed" || state.status === "failed") {
      window.clearInterval(pollTimer);
      pollTimer = null;
    }
  } catch (error) {
    elements.statusIcon.className = "status-icon failed";
    elements.statusTitle.textContent = "连接失败";
    elements.statusDetail.textContent = error.message;
    setRunning(false);
  }
}

elements.form.addEventListener("submit", async (event) => {
  event.preventDefault();
  const sourceRoot = elements.sourceRoot.value.trim();
  const outputRoot = elements.outputRoot.value.trim();
  if (!sourceRoot) {
    elements.sourceRoot.focus();
    setToast("请先输入一级成果文件夹路径");
    return;
  }
  if (!outputRoot) {
    elements.outputRoot.focus();
    setToast("请填写结果输出文件夹路径");
    return;
  }
  setRunning(true);
  elements.statusIcon.className = "status-icon running";
  elements.statusTitle.textContent = "正在提交";
  elements.statusDetail.textContent = "正在创建当前标签页的独立检查任务。";
  try {
    const response = await fetch("/api/run", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        tab_id: tabId,
        source_root: sourceRoot,
        output_root: outputRoot,
        gdb_schema: elements.gdbSchema.value,
        zpj_schema: elements.zpjSchema.value,
      }),
    });
    const state = await response.json();
    if (!response.ok) throw new Error(state.error || "任务提交失败");
    updateStatus(state);
    if (!pollTimer) {
      pollTimer = window.setInterval(fetchStatus, 1000);
    }
    await fetchStatus();
  } catch (error) {
    elements.statusIcon.className = "status-icon failed";
    elements.statusTitle.textContent = "提交失败";
    elements.statusDetail.textContent = error.message;
    setRunning(false);
  }
});

elements.copyPathButton.addEventListener("click", async () => {
  if (!currentReportPath) return;
  try {
    await navigator.clipboard.writeText(currentReportPath);
    setToast("PDF 路径已复制");
  } catch {
    const helper = document.createElement("textarea");
    helper.value = currentReportPath;
    document.body.appendChild(helper);
    helper.select();
    document.execCommand("copy");
    helper.remove();
    setToast("PDF 路径已复制");
  }
});

fetchStatus();
