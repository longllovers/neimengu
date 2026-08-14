const state = {
  files: new Map(),
  results: [],
  activeResult: 0,
};

const elements = {
  dropZone: document.querySelector("#dropZone"),
  fileInput: document.querySelector("#fileInput"),
  fileSummary: document.querySelector("#fileSummary"),
  clearButton: document.querySelector("#clearButton"),
  outputPath: document.querySelector("#outputPath"),
  copyOutputButton: document.querySelector("#copyOutputButton"),
  mergePathRow: document.querySelector("#mergePathRow"),
  mergeOutputPath: document.querySelector("#mergeOutputPath"),
  copyMergeOutputButton: document.querySelector("#copyMergeOutputButton"),
  mergeSmallCheckbox: document.querySelector("#mergeSmallCheckbox"),
  runButton: document.querySelector("#runButton"),
  message: document.querySelector("#message"),
  resultPanel: document.querySelector("#resultPanel"),
  resultSummary: document.querySelector("#resultSummary"),
  resultTabs: document.querySelector("#resultTabs"),
  csvPath: document.querySelector("#csvPath"),
  copyCsvPathButton: document.querySelector("#copyCsvPathButton"),
  modifiedPathBlock: document.querySelector("#modifiedPathBlock"),
  modifiedShpPath: document.querySelector("#modifiedShpPath"),
  copyModifiedPathButton: document.querySelector("#copyModifiedPathButton"),
  tableHead: document.querySelector("#tableHead"),
  tableBody: document.querySelector("#tableBody"),
  emptyResult: document.querySelector("#emptyResult"),
};

fetch("/api/config")
  .then((response) => response.json())
  .then((config) => {
    elements.outputPath.value = config.defaultOutputDir;
    elements.mergeOutputPath.value = config.defaultMergeOutputDir;
  })
  .catch(() => setMessage("无法读取默认输出路径。", true));

function updateMergePathState() {
  const enabled = elements.mergeSmallCheckbox.checked;
  elements.mergeOutputPath.disabled = !enabled;
  elements.copyMergeOutputButton.disabled = !enabled;
  elements.mergePathRow.classList.toggle("disabled-path", !enabled);
}

function fileKey(file) {
  return `${file.name.toLowerCase()}::${file.size}::${file.lastModified}`;
}

function addFiles(fileList) {
  for (const file of fileList) {
    state.files.set(fileKey(file), file);
  }
  renderFiles();
}

function shapefileGroups() {
  const groups = new Map();
  for (const file of state.files.values()) {
    const dot = file.name.lastIndexOf(".");
    if (dot < 1) continue;
    const stem = file.name.slice(0, dot);
    const extension = file.name.slice(dot).toLowerCase();
    if (!groups.has(stem)) groups.set(stem, new Set());
    groups.get(stem).add(extension);
  }
  return groups;
}

function renderFiles() {
  const groups = shapefileGroups();
  elements.fileSummary.replaceChildren();
  if (!state.files.size) {
    elements.fileSummary.textContent = "尚未添加文件";
    elements.fileSummary.className = "file-summary empty";
    elements.runButton.disabled = true;
    return;
  }

  elements.fileSummary.className = "file-summary";
  for (const [stem, extensions] of groups) {
    const ready = [".shp", ".shx", ".dbf", ".prj"]
      .every((extension) => extensions.has(extension));
    const chip = document.createElement("span");
    chip.className = `file-chip ${ready ? "ready" : "incomplete"}`;
    chip.textContent = ready
      ? `${stem} · 文件齐全`
      : `${stem} · 缺少配套文件`;
    elements.fileSummary.append(chip);
  }
  elements.runButton.disabled = ![...groups.values()]
    .some((extensions) => extensions.has(".shp"));
}

function setMessage(text, error = false) {
  elements.message.textContent = text;
  elements.message.className = `message${error ? " error" : ""}`;
}

async function copyText(text, button) {
  if (!text) return;
  try {
    await navigator.clipboard.writeText(text);
    const oldText = button.textContent;
    button.textContent = "已复制";
    setTimeout(() => { button.textContent = oldText; }, 1200);
  } catch {
    setMessage("复制失败，请手动选择路径复制。", true);
  }
}

function renderResult(index) {
  state.activeResult = index;
  const result = state.results[index];
  if (!result) return;

  [...elements.resultTabs.children].forEach((tab, tabIndex) => {
    tab.classList.toggle("active", tabIndex === index);
  });
  elements.csvPath.textContent = result.csvPath;
  const summaryParts = [
    `重叠 ${result.overlapCount} 对`,
    `小于 0.1 亩 ${result.smallCount} 个`,
  ];
  if (result.mergeEnabled) {
    summaryParts.push(`已合并 ${result.mergedCount} 个`);
    if (result.deletedCount) {
      summaryParts.push(`无相邻面并已删除 ${result.deletedCount} 个`);
    }
    if (result.skippedCount) {
      summaryParts.push(`无相邻大面 ${result.skippedCount} 个`);
    }
  }
  elements.resultSummary.textContent = summaryParts.join(" · ");
  elements.modifiedPathBlock.classList.toggle("hidden", !result.mergeEnabled);
  elements.modifiedShpPath.textContent = result.modifiedShpPath || "";

  elements.tableHead.replaceChildren();
  elements.tableBody.replaceChildren();
  const headRow = document.createElement("tr");
  for (const column of result.columns) {
    const th = document.createElement("th");
    th.textContent = column;
    headRow.append(th);
  }
  elements.tableHead.append(headRow);

  elements.emptyResult.classList.toggle("hidden", result.rows.length > 0);
  for (const row of result.rows) {
    const tr = document.createElement("tr");
    for (const column of result.columns) {
      const td = document.createElement("td");
      td.textContent = row[column] ?? "";
      tr.append(td);
    }
    elements.tableBody.append(tr);
  }
}

function showResults(results) {
  state.results = results;
  state.activeResult = 0;
  elements.resultTabs.replaceChildren();
  results.forEach((result, index) => {
    const tab = document.createElement("button");
    tab.type = "button";
    tab.className = "result-tab";
    tab.textContent = result.sourceName;
    tab.addEventListener("click", () => renderResult(index));
    elements.resultTabs.append(tab);
  });
  elements.resultPanel.classList.remove("hidden");
  renderResult(0);
  elements.resultPanel.scrollIntoView({ behavior: "smooth", block: "start" });
}

async function runCheck() {
  if (!state.files.size) return;
  const data = new FormData();
  for (const file of state.files.values()) {
    data.append("files", file, file.name);
  }
  data.append("output_dir", elements.outputPath.value.trim());
  data.append("merge_output_dir", elements.mergeOutputPath.value.trim());
  data.append(
    "merge_small",
    elements.mergeSmallCheckbox.checked ? "true" : "false",
  );

  elements.runButton.disabled = true;
  elements.runButton.textContent = "正在检查…";
  setMessage(`正在上传并检查 ${shapefileGroups().size} 套数据，请稍候。`);

  try {
    const response = await fetch("/api/check", { method: "POST", body: data });
    const payload = await response.json();
    if (!response.ok || !payload.ok) {
      throw new Error(payload.error || "检查失败。");
    }
    showResults(payload.results);
    setMessage(`检查完成，共生成 ${payload.results.length} 个 CSV 文件。`);
  } catch (error) {
    setMessage(error.message || "检查失败。", true);
  } finally {
    elements.runButton.disabled = !state.files.size;
    elements.runButton.textContent = "开始检查";
  }
}

["dragenter", "dragover"].forEach((eventName) => {
  elements.dropZone.addEventListener(eventName, (event) => {
    event.preventDefault();
    elements.dropZone.classList.add("dragging");
  });
});

["dragleave", "drop"].forEach((eventName) => {
  elements.dropZone.addEventListener(eventName, (event) => {
    event.preventDefault();
    elements.dropZone.classList.remove("dragging");
  });
});

elements.dropZone.addEventListener("drop", (event) => {
  addFiles(event.dataTransfer.files);
});
elements.fileInput.addEventListener("change", () => {
  addFiles(elements.fileInput.files);
  elements.fileInput.value = "";
});
elements.clearButton.addEventListener("click", () => {
  state.files.clear();
  state.results = [];
  elements.mergeSmallCheckbox.checked = false;
  updateMergePathState();
  elements.resultPanel.classList.add("hidden");
  setMessage("");
  renderFiles();
});
elements.runButton.addEventListener("click", runCheck);
elements.copyOutputButton.addEventListener("click", () => {
  copyText(elements.outputPath.value, elements.copyOutputButton);
});
elements.copyMergeOutputButton.addEventListener("click", () => {
  copyText(elements.mergeOutputPath.value, elements.copyMergeOutputButton);
});
elements.mergeSmallCheckbox.addEventListener("change", updateMergePathState);
elements.copyCsvPathButton.addEventListener("click", () => {
  const result = state.results[state.activeResult];
  if (result) copyText(result.csvPath, elements.copyCsvPathButton);
});
elements.copyModifiedPathButton.addEventListener("click", () => {
  const result = state.results[state.activeResult];
  if (result?.modifiedShpPath) {
    copyText(result.modifiedShpPath, elements.copyModifiedPathButton);
  }
});

renderFiles();
updateMergePathState();
