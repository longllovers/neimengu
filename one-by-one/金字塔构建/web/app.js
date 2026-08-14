const form = document.querySelector('#task-form');
const runButton = document.querySelector('#run-button');
const statusElement = document.querySelector('#status');
const consoleElement = document.querySelector('#console');
const messageElement = document.querySelector('#form-message');
const clearButton = document.querySelector('#clear-button');
const runButtonLabel = runButton.querySelector('span');

let eventSource = null;

function setStatus(state, label) {
  statusElement.className = `status status-${state}`;
  statusElement.querySelector('b').textContent = label;
  const running = state === 'running';
  runButton.disabled = running;
  runButtonLabel.textContent = running ? '正在构建…' : '开始构建';
}

function appendOutput(text, stream = 'stdout') {
  consoleElement.querySelector('.placeholder')?.remove();
  const chunk = document.createElement('span');
  chunk.className = `line line-${stream}`;
  chunk.textContent = text;
  consoleElement.append(chunk);
  consoleElement.scrollTop = consoleElement.scrollHeight;
}

async function runTask(event) {
  event.preventDefault();
  messageElement.textContent = '';
  const data = new FormData(form);
  const payload = {
    tifDir: data.get('tifDir'),
    maxFactor: Number(data.get('maxFactor')),
    workers: Number(data.get('workers')),
    resampling: data.get('resampling'),
    recursive: data.has('recursive'),
    force: data.has('force'),
    dryRun: data.has('dryRun'),
  };

  setStatus('running', '正在运行');
  consoleElement.replaceChildren();
  eventSource?.close();

  try {
    const response = await fetch('/api/tasks', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(payload),
    });
    const result = await response.json();
    if (!response.ok) throw new Error(result.error || '任务启动失败');
    connectEvents(result.taskId);
  } catch (error) {
    setStatus('failed', '启动失败');
    messageElement.textContent = error.message;
    appendOutput(`${error.message}\n`, 'system');
  }
}

function connectEvents(taskId) {
  const source = new EventSource(`/api/tasks/${taskId}/events`);
  eventSource = source;
  source.onmessage = (event) => {
    const data = JSON.parse(event.data);
    if (data.type === 'output') appendOutput(data.text, data.stream);
    if (data.type === 'status') setStatus(data.state, data.message);
    if (data.type === 'done') {
      source.close();
      eventSource = null;
    }
  };
  source.onerror = () => {
    if (eventSource === source && source.readyState === EventSource.CLOSED) {
      setStatus('failed', '连接已断开');
    }
  };
}

form.addEventListener('submit', runTask);
clearButton.addEventListener('click', () => {
  consoleElement.innerHTML = '<span class="placeholder">准备就绪，等待开始构建。</span>';
});

document.querySelectorAll('[data-step]').forEach((button) => {
  button.addEventListener('click', () => {
    const input = form.elements.workers;
    const next = Number(input.value || 1) + Number(button.dataset.step);
    input.value = Math.max(Number(input.min), Math.min(Number(input.max), next));
  });
});
