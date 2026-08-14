const state = {
  files: new Map(),
  results: [],
  activeResult: 0,
  defaultCsvOutputDir: "",
  defaultShpOutputDir: "",
};
const $ = (selector) => document.querySelector(selector);
const elements = {
  dropZone: $("#dropZone"), fileInput: $("#fileInput"), fileSummary: $("#fileSummary"),
  clearButton: $("#clearButton"),
  csvOutputPath: $("#csvOutputPath"), shpOutputPath: $("#shpOutputPath"),
  copyCsvOutputButton: $("#copyCsvOutputButton"), copyShpOutputButton: $("#copyShpOutputButton"),
  runButton: $("#runButton"), message: $("#message"),
  resultPanel: $("#resultPanel"), resultSummary: $("#resultSummary"),
  csvPath: $("#csvPath"), modifiedShpPath: $("#modifiedShpPath"),
  csvPathLine: $("#csvPathLine"), shpPathLine: $("#shpPathLine"), emptyResult: $("#emptyResult"),
  resultTabs: $("#resultTabs"),
  copyCsvPathButton: $("#copyCsvPathButton"), copyModifiedPathButton: $("#copyModifiedPathButton"),
  tableHead: $("#tableHead"), tableBody: $("#tableBody"),
};

function key(file) { return `${file.name.toLowerCase()}::${file.size}::${file.lastModified}`; }
function dirname(path) { const i = Math.max(path.lastIndexOf("/"), path.lastIndexOf("\\")); return i > 0 ? path.slice(0, i) : ""; }
function childDirectory(parent, name) {
  if (!parent) return "";
  const separator = parent.includes("\\") ? "\\" : "/";
  return `${parent.replace(/[\\/]+$/, "")}${separator}${name}`;
}
fetch("/api/config")
  .then((response) => response.json())
  .then((config) => {
    state.defaultCsvOutputDir = config.defaultCsvOutputDir || "";
    state.defaultShpOutputDir = config.defaultShpOutputDir || "";
    if (state.files.size && !elements.csvOutputPath.value && !elements.shpOutputPath.value) {
      elements.csvOutputPath.value = state.defaultCsvOutputDir;
      elements.shpOutputPath.value = state.defaultShpOutputDir;
    }
  })
  .catch(() => {});

function addFiles(files) {
  for (const file of files) state.files.set(key(file), file);
  const shp = [...state.files.values()].find((file) => file.name.toLowerCase().endsWith(".shp"));
  if (shp) {
    const sourceDirectory = dirname(shp.path || "");
    const parentDirectory = dirname(sourceDirectory);
    elements.csvOutputPath.value = parentDirectory
      ? childDirectory(parentDirectory, "csv")
      : state.defaultCsvOutputDir;
    elements.shpOutputPath.value = parentDirectory
      ? childDirectory(parentDirectory, "output")
      : state.defaultShpOutputDir;
  }
  renderFiles();
}
function renderFiles() {
  const groups = new Map();
  for (const file of state.files.values()) {
    const dot = file.name.lastIndexOf(".");
    if (dot < 1) continue;
    const stem = file.name.slice(0, dot).toLowerCase();
    const extension = file.name.slice(dot).toLowerCase();
    if (!groups.has(stem)) groups.set(stem, new Set());
    groups.get(stem).add(extension);
  }
  const shpGroups = [...groups.values()].filter((extensions) => extensions.has(".shp"));
  if (!state.files.size) {
    elements.fileSummary.textContent = "尚未添加文件";
    elements.fileSummary.className = "file-summary empty";
  } else {
    const ready = shpGroups.length > 0 && shpGroups.every((extensions) =>
      [".shp", ".shx", ".dbf", ".prj"].every((ext) => extensions.has(ext))
    );
    elements.fileSummary.textContent = `${shpGroups.length} 个 SHP · ${ready ? "配套文件齐全" : "缺少必要配套文件"}`;
    elements.fileSummary.className = `file-summary file-chip ${ready ? "ready" : "incomplete"}`;
  }
  elements.runButton.disabled = shpGroups.length === 0;
}
function setMessage(text, stateName = "") {
  elements.message.textContent = text;
  elements.message.className = `message ${stateName}`;
}
async function copyText(text, button) {
  if (!text) return;
  try { await navigator.clipboard.writeText(text); const old = button.textContent; button.textContent = "已复制"; setTimeout(() => button.textContent = old, 1200); }
  catch { setMessage("复制失败，请手动选择路径复制。", "error"); }
}
function renderResult(index) {
  state.activeResult = index;
  const result = state.results[index];
  if (!result) return;
  [...elements.resultTabs.children].forEach((tab, tabIndex) => {
    tab.classList.toggle("active", tabIndex === index);
  });
  const s = result.summary;
  elements.resultSummary.textContent = `无效几何 ${s.invalid_count} 个 · 重叠 ${s.overlap_count} 条 · 已调整 ${s.changed_count} 个`;
  elements.csvPath.textContent = s.csv_path;
  elements.modifiedShpPath.textContent = s.shp_path;
  elements.tableHead.replaceChildren(); elements.tableBody.replaceChildren();
  elements.emptyResult.classList.add("hidden");
  elements.csvPathLine.classList.remove("hidden");
  elements.shpPathLine.classList.remove("hidden");
  elements.copyCsvPathButton.classList.remove("hidden");
  const header = document.createElement("tr");
  for (const column of result.columns) { const th = document.createElement("th"); th.textContent = column; header.append(th); }
  elements.tableHead.append(header);
  for (const row of result.rows) {
    const tr = document.createElement("tr");
    for (const column of result.columns) { const td = document.createElement("td"); td.textContent = row[column] ?? ""; tr.append(td); }
    elements.tableBody.append(tr);
  }
  elements.resultPanel.classList.remove("hidden");
  elements.resultPanel.scrollIntoView({ behavior: "smooth", block: "start" });
}
function showResults(results) {
  state.results = results;
  state.activeResult = 0;
  elements.resultTabs.replaceChildren();
  for (const [index, result] of results.entries()) {
    const tab = document.createElement("button");
    tab.type = "button";
    tab.className = "result-tab";
    tab.textContent = result.summary.source_name;
    tab.addEventListener("click", () => renderResult(index));
    elements.resultTabs.append(tab);
  }
  elements.resultTabs.classList.toggle("hidden", results.length < 2);
  renderResult(0);
}
async function run() {
  if (!state.files.size) return;
  const form = new FormData();
  form.append("source_path", "");
  form.append("csv_output_dir", elements.csvOutputPath.value.trim());
  form.append("shp_output_dir", elements.shpOutputPath.value.trim());
  for (const file of state.files.values()) form.append("files", file, file.name);
  elements.runButton.disabled = true; elements.runButton.textContent = "正在运行";
  elements.resultSummary.textContent = "正在运行，请稍候……";
  elements.tableHead.replaceChildren(); elements.tableBody.replaceChildren();
  elements.resultTabs.replaceChildren();
  elements.resultTabs.classList.add("hidden");
  elements.csvPathLine.classList.add("hidden");
  elements.shpPathLine.classList.add("hidden");
  elements.copyCsvPathButton.classList.add("hidden");
  elements.emptyResult.textContent = "正在运行……";
  elements.emptyResult.classList.remove("hidden");
  setMessage("正在运行", "running");
  try {
    const response = await fetch("/api/jobs", { method: "POST", body: form });
    const payload = await response.json();
    if (!response.ok || !payload.ok) throw new Error(payload.error || "任务创建失败");
    await new Promise((resolve, reject) => {
      const events = new EventSource(`/api/jobs/${payload.job_id}/events`);
      events.onmessage = ({ data }) => {
        const event = JSON.parse(data);
        if (event.type === "done") { events.close(); showResults(event.results || [event.result]); setMessage("运行完成", "done"); resolve(); }
        if (event.type === "error") { events.close(); elements.resultSummary.textContent = "运行失败"; elements.emptyResult.textContent = event.message; setMessage("运行失败", "error"); reject(new Error(event.message)); }
      };
      events.onerror = () => { events.close(); reject(new Error("实时日志连接中断")); };
    });
  } catch (error) {
    elements.resultSummary.textContent = "运行失败";
    elements.emptyResult.textContent = error.message || "运行失败";
    elements.emptyResult.classList.remove("hidden");
    setMessage("运行失败", "error");
  } finally { elements.runButton.disabled = false; elements.runButton.textContent = "运行"; }
}

for (const eventName of ["dragenter", "dragover"]) elements.dropZone.addEventListener(eventName, (event) => { event.preventDefault(); elements.dropZone.classList.add("dragging"); });
for (const eventName of ["dragleave", "drop"]) elements.dropZone.addEventListener(eventName, (event) => { event.preventDefault(); elements.dropZone.classList.remove("dragging"); });
elements.dropZone.addEventListener("drop", (event) => addFiles(event.dataTransfer.files));
elements.fileInput.addEventListener("change", () => { addFiles(elements.fileInput.files); elements.fileInput.value = ""; });
elements.clearButton.addEventListener("click", () => { state.files.clear(); state.results = []; state.activeResult = 0; elements.csvOutputPath.value = ""; elements.shpOutputPath.value = ""; elements.resultSummary.textContent = "运行完成后在这里显示 CSV 信息。"; elements.tableHead.replaceChildren(); elements.tableBody.replaceChildren(); elements.resultTabs.replaceChildren(); elements.resultTabs.classList.add("hidden"); elements.csvPathLine.classList.add("hidden"); elements.shpPathLine.classList.add("hidden"); elements.copyCsvPathButton.classList.add("hidden"); elements.emptyResult.textContent = "等待运行"; elements.emptyResult.classList.remove("hidden"); setMessage("等待运行"); renderFiles(); });
elements.runButton.addEventListener("click", run);
elements.copyCsvOutputButton.addEventListener("click", () => copyText(elements.csvOutputPath.value, elements.copyCsvOutputButton));
elements.copyShpOutputButton.addEventListener("click", () => copyText(elements.shpOutputPath.value, elements.copyShpOutputButton));
elements.copyCsvPathButton.addEventListener("click", () => copyText(state.results[state.activeResult]?.summary.csv_path, elements.copyCsvPathButton));
elements.copyModifiedPathButton.addEventListener("click", () => copyText(state.results[state.activeResult]?.summary.shp_path, elements.copyModifiedPathButton));
renderFiles();
