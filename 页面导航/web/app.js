const grid = document.querySelector('#cardGrid');
const emptyState = document.querySelector('#emptyState');
const statusBox = document.querySelector('#status');
const dialog = document.querySelector('#editorDialog');
const form = document.querySelector('#editorForm');
const dialogTitle = document.querySelector('#dialogTitle');
const nameInput = document.querySelector('#nameInput');
const valueInput = document.querySelector('#valueInput');
const helpInput = document.querySelector('#helpInput');
const formError = document.querySelector('#formError');
const saveButton = document.querySelector('#saveButton');
const deleteDialog = document.querySelector('#deleteDialog');
const deleteForm = document.querySelector('#deleteForm');
const deleteSelect = document.querySelector('#deleteSelect');
const deleteError = document.querySelector('#deleteError');
const confirmDeleteButton = document.querySelector('#confirmDeleteButton');
let items = [];
let editingIndex = null;

function targetUrl(value) {
  return /^https?:\/\//i.test(value) ? value : `http://${value}`;
}

function render() {
  grid.replaceChildren();
  emptyState.hidden = items.length !== 0;
  items.forEach((item, index) => {
    const card = document.createElement('article');
    card.className = 'card';
    const link = document.createElement('a');
    link.className = 'card-link';
    link.href = targetUrl(item.value);
    link.target = '_blank';
    link.rel = 'noopener noreferrer';

    const number = document.createElement('span');
    number.className = 'card-index';
    number.textContent = `入口 ${String(index + 1).padStart(2, '0')}`;
    const title = document.createElement('h2');
    title.textContent = item.name;
    const open = document.createElement('span');
    open.className = 'open-label';
    open.textContent = '打开页面 ↗';
    link.append(number, title, open);

    const edit = document.createElement('button');
    edit.className = 'edit-button';
    edit.type = 'button';
    edit.title = `修改${item.name}`;
    edit.setAttribute('aria-label', `修改${item.name}`);
    edit.textContent = '✎';
    edit.addEventListener('click', () => openEditor(index));

    card.append(link);
    const helpText = typeof item.help === 'string' ? item.help.trim() : '';
    if (helpText) {
      const help = document.createElement('button');
      help.className = 'help-button';
      help.type = 'button';
      help.setAttribute('aria-label', `${item.name}的帮助内容`);
      help.textContent = '?';
      const tooltip = document.createElement('span');
      tooltip.className = 'help-tooltip';
      tooltip.setAttribute('role', 'tooltip');
      tooltip.textContent = helpText;
      help.append(tooltip);
      card.append(help);
    } else {
      card.classList.add('without-help');
    }
    card.append(edit);
    grid.append(card);
  });
}

async function deleteItem(index) {
  const item = items[index];
  deleteError.textContent = '';
  confirmDeleteButton.disabled = true;
  confirmDeleteButton.textContent = '删除中…';
  try {
    const response = await fetch(`/api/items/${index}`, { method: 'DELETE' });
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || '删除失败');
    items.splice(index, 1);
    render();
    deleteDialog.close();
  } catch (error) {
    deleteError.textContent = error.message;
  } finally {
    confirmDeleteButton.disabled = false;
    confirmDeleteButton.textContent = '确认删除';
  }
}

function openDeleteDialog() {
  deleteError.textContent = '';
  deleteSelect.replaceChildren();
  items.forEach((item, index) => {
    const option = document.createElement('option');
    option.value = String(index);
    option.textContent = item.name;
    deleteSelect.append(option);
  });
  if (items.length === 0) {
    const option = document.createElement('option');
    option.textContent = '暂无可删除的页面';
    option.disabled = true;
    option.selected = true;
    deleteSelect.append(option);
    confirmDeleteButton.disabled = true;
  } else {
    confirmDeleteButton.disabled = false;
  }
  deleteDialog.showModal();
}

function closeDeleteDialog() {
  if (confirmDeleteButton.textContent !== '删除中…') deleteDialog.close();
}

async function loadItems() {
  statusBox.textContent = '';
  try {
    const response = await fetch('/api/items', { cache: 'no-store' });
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || '读取配置失败');
    items = data;
    render();
  } catch (error) {
    statusBox.textContent = error.message;
  }
}

function openEditor(index = null) {
  editingIndex = index;
  form.reset();
  formError.textContent = '';
  dialogTitle.textContent = index === null ? '添加页面' : '修改页面';
  if (index !== null) {
    nameInput.value = items[index].name;
    valueInput.value = items[index].value;
    helpInput.value = typeof items[index].help === 'string' ? items[index].help : '';
  }
  dialog.showModal();
  nameInput.focus();
}

function closeEditor() {
  if (!saveButton.disabled) dialog.close();
}

form.addEventListener('submit', async (event) => {
  event.preventDefault();
  formError.textContent = '';
  saveButton.disabled = true;
  saveButton.textContent = '保存中…';
  const isNew = editingIndex === null;
  const url = isNew ? '/api/items' : `/api/items/${editingIndex}`;
  try {
    const response = await fetch(url, {
      method: isNew ? 'POST' : 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ name: nameInput.value, value: valueInput.value, help: helpInput.value })
    });
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || '保存失败');
    if (isNew) items.push(data); else items[editingIndex] = data;
    render();
    dialog.close();
  } catch (error) {
    formError.textContent = error.message;
  } finally {
    saveButton.disabled = false;
    saveButton.textContent = '保存';
  }
});

document.querySelector('#addButton').addEventListener('click', () => openEditor());
document.querySelector('#deleteButton').addEventListener('click', openDeleteDialog);
document.querySelector('#closeButton').addEventListener('click', closeEditor);
document.querySelector('#cancelButton').addEventListener('click', closeEditor);
dialog.addEventListener('click', (event) => {
  if (event.target === dialog) closeEditor();
});
deleteForm.addEventListener('submit', (event) => {
  event.preventDefault();
  const index = Number(deleteSelect.value);
  if (Number.isInteger(index) && index >= 0 && index < items.length) deleteItem(index);
});
document.querySelector('#closeDeleteButton').addEventListener('click', closeDeleteDialog);
document.querySelector('#cancelDeleteButton').addEventListener('click', closeDeleteDialog);
deleteDialog.addEventListener('click', (event) => {
  if (event.target === deleteDialog) closeDeleteDialog();
});

loadItems();
