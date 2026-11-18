// Manual code editor (CodeMirror 5) with Qwen Coder ghost-text autocomplete.
import { $, ApiError, askPassword, csrf, el, errorToast, get, icon, modal, post, projectId, put, toast } from './core.js';
import { checkIntegrity } from './integrity.js';

const pid = projectId();
const CM = window.CodeMirror;
const MODES = {
  python: 'python', javascript: 'javascript', jsx: 'jsx', typescript: { name: 'javascript', typescript: true },
  tsx: { name: 'jsx', base: { name: 'javascript', typescript: true } }, json: { name: 'javascript', json: true },
  html: 'htmlmixed', css: 'css', markdown: 'markdown', shell: 'shell', sql: 'text/x-sql', go: 'go', rust: 'rust',
  java: 'text/x-java', kotlin: 'text/x-kotlin', c: 'text/x-csrc', cpp: 'text/x-c++src', csharp: 'text/x-csharp',
  yaml: 'yaml', toml: 'toml', dockerfile: 'dockerfile', php: 'application/x-httpd-php', ruby: 'ruby', swift: 'swift', xml: 'xml', text: null,
};
const IMAGE_EXT = /\.(png|jpe?g|gif|webp|bmp|ico|avif|svg)$/i;
const tabs = [];
let active = null;
let cm = null;
let ghost = null;
let pending = null;
let timer = null;
let aiOn = true;
try { aiOn = localStorage.getItem('elvin-ai-suggest') !== 'off'; } catch {}
const expanded = new Set(['']);

function setAiToggle() { $('#ai-toggle').setAttribute('aria-checked', String(aiOn)); }
setAiToggle();
$('#ai-toggle').addEventListener('click', () => { aiOn = !aiOn; try { localStorage.setItem('elvin-ai-suggest', aiOn ? 'on' : 'off'); } catch {} setAiToggle(); clearGhost(); });

// ------------------------------------------------------------------ tree
async function loadTree() {
  const tree = $('#tree');
  try {
    const r = await get(`/api/projects/${pid}/tree`);
    tree.replaceChildren(renderNodes(r.children || [], 0));
    if (r.truncated) tree.append(el('p', { class: 'p-2 text-xs muted', text: 'Large project - not every file is listed.' }));
    if (!(r.children || []).length) tree.append(el('p', { class: 'p-3 text-sm muted', text: 'This project is empty. Create a file to start.' }));
  } catch (err) { errorToast(err); }
}
function renderNodes(nodes, depth) {
  const wrap = el('div');
  for (const n of nodes) {
    const pad = `${depth * 12 + 6}px`;
    if (n.type === 'dir') {
      const open = expanded.has(n.path);
      const row = el('button', { class: 'tree-item', title: n.path, onclick: () => { if (n.ignored) return; open ? expanded.delete(n.path) : expanded.add(n.path); loadTree(); } },
        icon(`fa-solid ${n.ignored ? 'fa-folder text-slate-400' : open ? 'fa-folder-open text-amber-500' : 'fa-folder text-amber-500'} w-4`), el('span', { class: 'truncate', text: n.name }),
        n.ignored ? el('span', { class: 'ml-auto text-[10px] faint', text: 'ignored' }) : '');
      row.style.paddingLeft = pad;
      wrap.append(row);
      if (open && !n.ignored) wrap.append(renderNodes(n.children || [], depth + 1));
    } else {
      const fileIcon = IMAGE_EXT.test(n.name) ? 'fa-regular fa-image text-pink-400' : /\.zip$/i.test(n.name) ? 'fa-regular fa-file-zipper text-amber-400' : 'fa-regular fa-file-code text-violet-400';
      const row = el('div', { class: `group tree-item ${active && active.path === n.path ? 'active' : ''}` },
        el('button', { class: 'flex min-w-0 flex-1 items-center gap-2 text-left', title: n.path, onclick: () => openFile(n.path) },
          icon(`${fileIcon} w-4`), el('span', { class: 'truncate', text: n.name })),
        el('button', { class: 'opacity-0 group-hover:opacity-100', title: 'Rename', onclick: () => renameFile(n.path) }, icon('fa-solid fa-pen text-[10px]')),
        el('button', { class: 'opacity-0 group-hover:opacity-100 text-rose-500', title: 'Delete', onclick: () => deleteFile(n.path) }, icon('fa-regular fa-trash-can text-[10px]')));
      row.style.paddingLeft = pad;
      wrap.append(row);
    }
  }
  return wrap;
}

// ------------------------------------------------------------------ files
function ensureEditor() {
  if (cm) return;
  $('#editor-empty').classList.add('hidden');
  $('#editor').classList.remove('hidden');
  cm = CM($('#editor'), {
    lineNumbers: true, theme: 'elvin', indentUnit: 4, tabSize: 4, indentWithTabs: false, autoCloseBrackets: true,
    matchBrackets: true, autoCloseTags: true, styleActiveLine: true, lineWrapping: false,
    extraKeys: {
      Tab: (c) => { if (ghost) { acceptGhost(); return undefined; } if (c.somethingSelected()) return c.indentSelection('add'); c.replaceSelection(' '.repeat(c.getOption('indentUnit'))); return undefined; },
      Esc: () => { if (ghost) { clearGhost(); return undefined; } return CM.Pass; },
      'Cmd-S': () => save(), 'Ctrl-S': () => save(), 'Cmd-/': 'toggleComment', 'Ctrl-/': 'toggleComment',
    },
  });
  cm.on('change', (_c, change) => {
    if (!active) return;
    active.dirty = true;
    updateStatus();
    renderTabs();
    if (change.origin === '+input' || change.origin === '+delete') scheduleSuggest(change.origin === '+input');
  });
  cm.on('cursorActivity', () => {
    updateStatus();
    if (ghost) { const c = cm.getCursor(); if (c.line !== ghost.pos.line || c.ch !== ghost.pos.ch) clearGhost(); }
  });
  cm.on('blur', () => clearGhost());
}
function showImage(tab) {
  clearGhost();
  $('#editor-empty').classList.add('hidden');
  $('#editor').classList.add('hidden');
  const view = $('#image-view');
  view.classList.remove('hidden');
  view.classList.add('flex');
  const img = $('#image-el');
  img.onload = () => { $('#image-info').textContent = `${tab.path} · ${img.naturalWidth} × ${img.naturalHeight}px`; };
  img.onerror = () => { $('#image-info').textContent = `${tab.path} could not be shown (deleted, too large or not an image).`; };
  img.src = `/api/projects/${pid}/raw?path=${encodeURIComponent(tab.path)}&v=${Date.now()}`;
}
function hideImage() {
  const view = $('#image-view');
  view.classList.add('hidden');
  view.classList.remove('flex');
}

async function openFile(path, line = null) {
  let tab = tabs.find((t) => t.path === path);
  if (!tab && IMAGE_EXT.test(path)) {
    tab = { path, image: true, dirty: false, lang: 'image' };
    tabs.push(tab);
  }
  if (tab && tab.image) {
    active = tab;
    showImage(tab);
    setRunDefault(tab);
    renderTabs();
    updateStatus();
    loadTree();
    return;
  }
  hideImage();
  if (!tab) {
    try {
      const r = await get(`/api/projects/${pid}/file?path=${encodeURIComponent(path)}`);
      if (r.binary) { toast(`${path} is a binary file (${r.size} bytes) - it can't be edited here.`, 'warn'); return; }
      ensureEditor();
      tab = { path, sha: r.sha256, dirty: false, lang: r.language, doc: CM.Doc(r.content, MODES[r.language] ?? null) };
      tabs.push(tab);
    } catch (err) { errorToast(err); return; }
  }
  active = tab;
  clearGhost();
  $('#editor-empty').classList.add('hidden');
  $('#editor').classList.remove('hidden');
  cm.swapDoc(tab.doc);
  cm.refresh();
  cm.focus();
  if (line) { cm.setCursor({ line: Math.max(0, line - 1), ch: 0 }); cm.scrollIntoView(null, 200); }
  setRunDefault(tab);
  renderTabs();
  updateStatus();
  loadTree();
}
function renderTabs() {
  $('#tabs').replaceChildren(...tabs.map((t) => el('div', { class: `flex shrink-0 items-center gap-1.5 rounded-lg px-2.5 py-1 text-xs ${t === active ? 'bg-[var(--hover)] font-medium' : 'muted hover:bg-[var(--hover)]'}` },
    el('button', { onclick: () => openFile(t.path), text: t.path.split('/').pop() }),
    t.dirty ? el('span', { class: 'dot', title: 'Unsaved' }) : '',
    el('button', { class: 'opacity-60 hover:opacity-100', title: 'Close', onclick: () => closeTab(t) }, icon('fa-solid fa-xmark')))));
}
function closeTab(t) {
  if (t.dirty && !confirm(`Close ${t.path} without saving?`)) return;
  tabs.splice(tabs.indexOf(t), 1);
  if (active === t) {
    active = tabs[tabs.length - 1] || null;
    if (active) { openFile(active.path); return; }
    hideImage();
    if (cm) cm.swapDoc(CM.Doc(''));
    $('#editor').classList.add('hidden');
    $('#editor-empty').classList.remove('hidden');
  }
  renderTabs();
  updateStatus();
}
function updateStatus() {
  $('#st-file').textContent = active ? active.path : '-';
  $('#st-lang').textContent = active ? active.lang : '';
  if (cm && active) { const c = cm.getCursor(); $('#st-pos').textContent = `Ln ${c.line + 1}, Col ${c.ch + 1}`; }
  $('#st-save').textContent = active ? (active.dirty ? 'unsaved changes' : 'saved') : '';
}
async function save() {
  if (!active || !cm || active.image) return;
  const t = active;
  try {
    const r = await put(`/api/projects/${pid}/file`, { path: t.path, content: t.doc.getValue(), base_sha256: t.sha });
    t.sha = r.sha256;
    t.dirty = false;
    renderTabs(); updateStatus();
    toast(r.snapshot_id ? `Saved ${t.path} · timestamped version created` : `Saved ${t.path}`, 'ok', 2200);
  } catch (err) {
    if (err instanceof ApiError && err.status === 409) {
      const reload = await modal({ title: 'File changed on disk', body: err.message, actions: [{ label: 'Keep editing', value: false }, { label: 'Reload from disk', value: true, cls: 'btn-primary' }] });
      if (reload) { tabs.splice(tabs.indexOf(t), 1); active = null; openFile(t.path); }
    } else errorToast(err);
  }
}
async function promptName(title, value = '') {
  const input = el('input', { class: 'input', value, placeholder: 'folder/file.py' });
  const ok = await modal({ title, body: input, actions: [{ label: 'Cancel', value: false }, { label: 'OK', value: true, cls: 'btn-primary' }] });
  return ok ? input.value.trim() : null;
}
async function fsOp(body, msg) {
  try { await post(`/api/projects/${pid}/fs`, body); toast(msg, 'ok', 2000); await loadTree(); return true; } catch (err) { errorToast(err); return false; }
}
$('#new-file').addEventListener('click', async () => { const p = await promptName('New file'); if (p && await fsOp({ op: 'new_file', path: p }, 'File created')) openFile(p); });
$('#new-folder').addEventListener('click', async () => { const p = await promptName('New folder', ''); if (p) { expanded.add(p); fsOp({ op: 'new_folder', path: p }, 'Folder created'); } });
$('#refresh-tree').addEventListener('click', loadTree);
async function renameFile(path) {
  const p = await promptName('Rename file', path);
  if (p && p !== path && await fsOp({ op: 'rename', path, new_path: p }, 'Renamed')) {
    const t = tabs.find((x) => x.path === path);
    if (t) { t.path = p; renderTabs(); }
  }
}
async function deleteFile(path) {
  const password = await askPassword(`${path} will be deleted. A timestamped version is saved first, so you can restore it from History.`, { title: 'Delete file?' });
  if (password && await fsOp({ op: 'delete', path, password }, 'Deleted')) {
    const t = tabs.find((x) => x.path === path);
    if (t) { t.dirty = false; closeTab(t); }
  }
}
$('#save-btn').addEventListener('click', save);
$('#upload-into').addEventListener('change', async (e) => {
  const files = Array.from(e.target.files);
  e.target.value = '';
  if (!files.length) return;
  const fd = new FormData();
  for (const f of files) fd.append('files', f, f.name);
  const close = toast('Uploading and virus-scanning...', 'info', 0);
  try { const r = await post(`/api/projects/${pid}/upload`, fd); toast(`${r.files} file(s) added`, 'ok'); loadTree(); } catch (err) { errorToast(err); } finally { close(); }
});
window.addEventListener('beforeunload', (e) => { if (tabs.some((t) => t.dirty)) { e.preventDefault(); e.returnValue = ''; } });

// ------------------------------------------------------------------ ghost-text autocomplete
function clearGhost() {
  if (pending) { pending.abort(); pending = null; }
  if (!ghost) return;
  ghost.bm?.clear();
  ghost.lw?.clear();
  ghost = null;
}
function acceptGhost() {
  const g = ghost;
  clearGhost();
  cm.replaceRange(g.text, g.pos);
}
function showGhost(pos, text) {
  clearGhost();
  if (!text || !text.trim()) return;
  const lines = text.split('\n');
  const span = el('span', { class: 'cm-ghost', text: lines[0] });
  const bm = cm.setBookmark(pos, { widget: span, insertLeft: true });
  let lw = null;
  if (lines.length > 1) lw = cm.addLineWidget(pos.line, el('div', { class: 'cm-ghost-block', text: lines.slice(1).join('\n') }));
  ghost = { pos, text, bm, lw };
}
function scheduleSuggest(typed) {
  clearTimeout(timer);
  clearGhost();
  if (!aiOn || !typed || !active) return;
  timer = setTimeout(suggest, 450);
}
async function suggest() {
  if (!cm || !active || cm.somethingSelected()) return;
  const pos = cm.getCursor();
  const lineText = cm.getLine(pos.line);
  if (/\S/.test(lineText.slice(pos.ch).replace(/^[)\]}'"`;,]*$/, ''))) return; // only at the end of a line
  const doc = cm.getValue();
  const idx = cm.indexFromPos(pos);
  const ctrl = new AbortController();
  pending = ctrl;
  try {
    const r = await post(`/api/projects/${pid}/complete`, { path: active.path, prefix: doc.slice(Math.max(0, idx - 6000), idx), suffix: doc.slice(idx, idx + 2000) }, { signal: ctrl.signal });
    if (pending !== ctrl) return;
    pending = null;
    const dot = $('#st-ai .dot');
    if (r.error) { dot.className = 'dot dot-bad'; $('#st-ai').title = r.error; return; }
    dot.className = 'dot dot-ok';
    $('#st-ai').title = r.provider || '';
    const now = cm.getCursor();
    if (now.line === pos.line && now.ch === pos.ch && r.completion) showGhost(pos, r.completion);
  } catch (err) { if (err.name !== 'AbortError') { /* silent: autocomplete is best-effort */ } }
}

// ------------------------------------------------------------------ run code yourself (sandbox)
const customCmd = new Map();
function defaultCommand(path) {
  const name = path.split('/').pop();
  if (/^test_.*\.py$|_test\.py$/.test(name)) return `python -m pytest -q ${path}`;
  if (/\.py$/.test(name)) return `python ${path}`;
  if (/\.(mjs|cjs|js)$/.test(name)) return `node ${path}`;
  if (/\.ts$/.test(name)) return `npx --yes tsx ${path}`;
  if (/\.sh$/.test(name)) return `bash ${path}`;
  if (name === 'package.json') return 'npm install && npm test';
  if (name === 'requirements.txt') return 'pip install -r requirements.txt';
  if (/\.rb$/.test(name)) return `ruby ${path}`;
  if (/\.(html?|png|jpe?g|gif|webp|svg)$/.test(name)) return '';
  return '';
}
function setRunDefault(tab) {
  const input = $('#run-cmd');
  input.value = customCmd.get(tab.path) ?? defaultCommand(tab.path);
  $('#render-go').classList.toggle('hidden', !/\.html?$/i.test(tab.path));
}
$('#run-cmd').addEventListener('input', () => { if (active) customCmd.set(active.path, $('#run-cmd').value); });
function showOutput(on = true) {
  $('#run-output').classList.toggle('hidden', !on);
  $('#run-toggle').querySelector('i').className = `fa-solid ${on ? 'fa-chevron-down' : 'fa-chevron-up'}`;
}
$('#run-toggle').addEventListener('click', () => showOutput($('#run-output').classList.contains('hidden')));
let lastRun = null;
async function runNow(force = false) {
  const command = $('#run-cmd').value.trim();
  if (!command) { toast('Type a command to run (for example python main.py).', 'info'); return; }
  if (active && active.dirty) await save();
  const btn = $('#run-go');
  btn.disabled = true;
  btn.replaceChildren(icon('fa-solid fa-spinner fa-spin'), ' Running');
  $('#render-img').classList.add('hidden');
  showOutput(true);
  $('#run-meta').textContent = `$ ${command} - running in the sandbox...`;
  $('#run-text').textContent = '';
  try {
    const r = await post(`/api/projects/${pid}/run`, { command, source: 'editor', force });
    lastRun = r;
    $('#run-meta').replaceChildren(el('span', { class: `badge mr-2 ${r.ok ? 'badge-ok' : 'badge-bad'}`, text: r.timed_out ? 'timed out' : `exit ${r.exit_code}` }),
      `$ ${r.command} · ${(r.duration_ms / 1000).toFixed(1)}s`);
    $('#run-text').textContent = r.output || '(no output)';
    $('#ask-fix').classList.toggle('hidden', r.ok);
  } catch (err) {
    if (err instanceof ApiError && err.status === 409 && err.data && err.data.detail && err.data.detail.blocked) {
      const list = err.data.detail.blocked.map((b) => `${b.name}: ${b.reason}`).join('\n');
      const go = await modal({ title: 'Blocked by the vulnerability detector', body: el('div', {}, el('p', { class: 'muted', text: err.message }), el('pre', { class: 'terminal mt-3', text: list })),
        actions: [{ label: 'Cancel', value: false }, { label: 'Run anyway', value: true, cls: 'btn-danger' }] });
      if (go) { btn.disabled = false; btn.replaceChildren(icon('fa-solid fa-play'), ' Run'); return runNow(true); }
      $('#run-meta').textContent = 'Not run.';
    } else { $('#run-meta').textContent = err.message; errorToast(err); }
  } finally {
    btn.disabled = false;
    btn.replaceChildren(icon('fa-solid fa-play'), ' Run');
  }
}
$('#run-go').addEventListener('click', () => runNow());
$('#run-cmd').addEventListener('keydown', (e) => { if (e.key === 'Enter') { e.preventDefault(); runNow(); } });
document.addEventListener('keydown', (e) => { if ((e.metaKey || e.ctrlKey) && e.key === 'Enter') { e.preventDefault(); runNow(); } });
$('#ask-fix').addEventListener('click', () => {
  if (!lastRun) return;
  const ask = `I ran \`${lastRun.command}\`${active ? ` (file ${active.path})` : ''} and it failed. Check the output and fix the problem.`;
  location.href = `/p/${pid}/elvin?ask=${encodeURIComponent(ask)}`;
});
$('#render-go').addEventListener('click', async () => {
  if (!active) return;
  if (active.dirty) await save();
  showOutput(true);
  $('#run-meta').textContent = `Rendering ${active.path} in the sandbox's network-blocked browser...`;
  $('#run-text').textContent = '';
  try {
    const r = await post(`/api/projects/${pid}/render`, { path: active.path });
    const img = $('#render-img');
    img.src = r.url;
    img.classList.remove('hidden');
    $('#run-meta').textContent = `Rendered ${r.path} (1280 × 800, no internet access)`;
  } catch (err) { $('#run-meta').textContent = err.message; }
});

// open file from #path=...&line=... (links from Bug Hunt)
const hash = new URLSearchParams(location.hash.slice(1));
loadTree().then(() => { if (hash.get('path')) openFile(hash.get('path'), Number(hash.get('line')) || null); });
// links to another file while the editor is already open only change the #hash (no page load)
window.addEventListener('hashchange', () => {
  const h = new URLSearchParams(location.hash.slice(1));
  if (h.get('path')) openFile(h.get('path'), Number(h.get('line')) || null);
});
checkIntegrity();
void csrf;
