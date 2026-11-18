// Elvin - shared front-end helpers (no inline handlers, CSP-nonce friendly).
const meta = (name) => document.querySelector(`meta[name="${name}"]`)?.content || '';
export const csrf = () => meta('csrf-token');
export const $ = (sel, root = document) => root.querySelector(sel);
export const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));
export const projectId = () => document.body.dataset.project || '';

export class ApiError extends Error {
  constructor(status, detail, data) { super(detail); this.status = status; this.data = data; }
}

export async function api(method, url, body, opts = {}) {
  const headers = { 'X-CSRF-Token': csrf(), Accept: 'application/json' };
  let payload;
  if (body instanceof FormData) payload = body;
  else if (body !== undefined) { headers['Content-Type'] = 'application/json'; payload = JSON.stringify(body); }
  let res;
  try {
    res = await fetch(url, { method, headers, body: payload, credentials: 'same-origin', signal: opts.signal });
  } catch (err) {
    if (err.name === 'AbortError') throw err;
    throw new ApiError(0, 'Network problem - is Elvin still running?', null);
  }
  const ct = res.headers.get('content-type') || '';
  const data = ct.includes('application/json') ? await res.json().catch(() => null) : null;
  if (res.status === 401 && data && data.error === 'signed_out') { signedOut(data.detail); throw new ApiError(401, data.detail, data); }
  if (!res.ok) {
    const detail = data && data.detail;
    const msg = typeof detail === 'string' ? detail : (detail && detail.message) || `Request failed (${res.status})`;
    throw new ApiError(res.status, msg, data);
  }
  return data;
}
export const get = (u, o) => api('GET', u, undefined, o);
export const post = (u, b, o) => api('POST', u, b === undefined ? {} : b, o);
export const put = (u, b) => api('PUT', u, b);
export const patch = (u, b) => api('PATCH', u, b);
export const del = (u) => api('DELETE', u);

// ---------------------------------------------------------------- DOM helpers
export function esc(value) {
  return String(value ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}
export function el(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v === undefined || v === null || v === false) continue;
    if (k === 'class') node.className = v;
    else if (k === 'text') node.textContent = v;
    else if (k === 'html') node.innerHTML = v; // only ever used with esc()/md() output
    else if (k.startsWith('on') && typeof v === 'function') node.addEventListener(k.slice(2), v);
    else if (k === 'dataset') Object.assign(node.dataset, v);
    else node.setAttribute(k, v === true ? '' : v);
  }
  for (const c of children.flat()) {
    if (c === null || c === undefined || c === false) continue;
    node.append(c instanceof Node ? c : document.createTextNode(String(c)));
  }
  return node;
}
export const icon = (cls) => el('i', { class: cls, 'aria-hidden': 'true' });

// Safe markdown: everything is escaped first, then a small set of formatting is re-added.
export function md(src) {
  const blocks = [];
  let text = String(src ?? '').replace(/```([\w+#.-]*)\n?([\s\S]*?)```/g, (_, lang, code) => {
    blocks.push(`<pre><code>${esc(code.replace(/\n$/, ''))}</code></pre>`);
    return `\u0000${blocks.length - 1}\u0000`;
  });
  const inline = (t) => esc(t)
    .replace(/`([^`]+)`/g, '<code>$1</code>')
    .replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>')
    .replace(/(^|[\s(])\*([^*\s][^*]*)\*(?=[\s).,!?:;]|$)/g, '$1<em>$2</em>')
    .replace(/\[([^\]]+)\]\(((?:https?:\/\/|\/(?!\/))[^)\s]*)\)/g, '<a href="$2" target="_blank" rel="noopener noreferrer">$1</a>');
  let html = '';
  let list = null;
  let para = [];
  const flushPara = () => { if (para.length) { html += `<p>${para.map(inline).join('<br>')}</p>`; para = []; } };
  const flushList = () => { if (list) { html += `</${list}>`; list = null; } };
  for (const raw of text.split('\n')) {
    const line = raw.replace(/\s+$/, '');
    let m;
    if (/^\u0000\d+\u0000$/.test(line.trim())) { flushPara(); flushList(); html += line.trim(); continue; }
    if ((m = line.match(/^(#{1,3})\s+(.*)$/))) { flushPara(); flushList(); html += `<h${m[1].length + 2}>${inline(m[2])}</h${m[1].length + 2}>`; continue; }
    if ((m = line.match(/^\s*[-*]\s+(.*)$/))) { flushPara(); if (list !== 'ul') { flushList(); html += '<ul>'; list = 'ul'; } html += `<li>${inline(m[1])}</li>`; continue; }
    if ((m = line.match(/^\s*\d+[.)]\s+(.*)$/))) { flushPara(); if (list !== 'ol') { flushList(); html += '<ol>'; list = 'ol'; } html += `<li>${inline(m[1])}</li>`; continue; }
    if ((m = line.match(/^>\s?(.*)$/))) { flushPara(); flushList(); html += `<blockquote>${inline(m[1])}</blockquote>`; continue; }
    if (!line.trim()) { flushPara(); flushList(); continue; }
    flushList();
    para.push(line);
  }
  flushPara(); flushList();
  return html.replace(/\u0000(\d+)\u0000/g, (_, i) => blocks[Number(i)] || '');
}

export function diffView(diff) {
  const box = el('div', { class: 'code-box !p-0 py-2' });
  const lines = String(diff || '').split('\n');
  for (const ln of lines.slice(0, 4000)) {
    let cls = 'diff-line';
    if (ln.startsWith('+++') || ln.startsWith('---')) cls += ' diff-meta';
    else if (ln.startsWith('+')) cls += ' diff-add';
    else if (ln.startsWith('-')) cls += ' diff-del';
    else if (ln.startsWith('@@')) cls += ' diff-hunk';
    box.append(el('span', { class: cls, text: ln || ' ' }));
  }
  if (lines.length > 4000) box.append(el('span', { class: 'diff-line diff-meta', text: `... ${lines.length - 4000} more lines` }));
  return box;
}

// ---------------------------------------------------------------- formatting
export function timeAgo(iso) {
  if (!iso) return 'never';
  const s = Math.floor((Date.now() - new Date(iso).getTime()) / 1000);
  if (s < 10) return 'just now';
  if (s < 60) return `${s}s ago`;
  if (s < 3600) return `${Math.floor(s / 60)} min ago`;
  if (s < 86400) return `${Math.floor(s / 3600)} h ago`;
  if (s < 86400 * 30) return `${Math.floor(s / 86400)} d ago`;
  return new Date(iso).toLocaleDateString();
}
export const fmtDate = (iso) => (iso ? new Date(iso).toLocaleString() : '');
export function fmtBytes(n) {
  n = Number(n || 0);
  const u = ['B', 'KB', 'MB', 'GB'];
  let i = 0;
  while (n >= 1024 && i < u.length - 1) { n /= 1024; i++; }
  return `${i ? n.toFixed(1) : n} ${u[i]}`;
}
export const fmtNum = (n) => Number(n || 0).toLocaleString();
export const uid = () => (crypto.randomUUID ? crypto.randomUUID() : String(Date.now()) + Math.random()).replace(/[^A-Za-z0-9]/g, '');

// ---------------------------------------------------------------- toasts & modals
export function toast(message, type = 'info', timeout = 4500) {
  const colors = { info: 'fa-circle-info text-violet-500', ok: 'fa-circle-check text-emerald-500', warn: 'fa-triangle-exclamation text-amber-500', error: 'fa-circle-exclamation text-rose-500' };
  const node = el('div', { class: 'toast', role: 'status' }, icon(`fa-solid ${colors[type] || colors.info} mt-0.5`), el('div', { class: 'flex-1', text: message }));
  const close = () => { node.classList.add('leaving'); setTimeout(() => node.remove(), 260); };
  node.append(el('button', { class: 'opacity-60 hover:opacity-100', 'aria-label': 'Close', onclick: close }, icon('fa-solid fa-xmark')));
  $('#toasts')?.append(node);
  if (timeout) setTimeout(close, timeout);
  return close;
}
export function errorToast(err) { toast(err && err.message ? err.message : String(err), 'error', 7000); }

export function modal({ title, body, actions = [{ label: 'Close', value: null, cls: 'btn-ghost' }], wide = false, dismissable = true, onOpen } = {}) {
  return new Promise((resolve) => {
    const root = $('#modal-root');
    const panel = el('div', { class: `modal-panel ${wide ? 'max-w-5xl' : 'max-w-lg'}`, role: 'dialog', 'aria-modal': 'true' });
    const backdrop = el('div', { class: 'modal-backdrop' }, panel);
    const done = (value) => { document.removeEventListener('keydown', onKey); backdrop.remove(); resolve(value); };
    const onKey = (e) => { if (e.key === 'Escape' && dismissable) done(null); };
    panel.append(el('div', { class: 'flex items-center justify-between gap-3 px-5 pt-5' },
      el('h2', { class: 'text-lg font-semibold', text: title || '' }),
      dismissable ? el('button', { class: 'btn-icon', 'aria-label': 'Close', onclick: () => done(null) }, icon('fa-solid fa-xmark')) : ''));
    const content = el('div', { class: 'overflow-y-auto px-5 py-4 text-sm' });
    if (body instanceof Node) content.append(body); else content.append(el('p', { class: 'muted', text: body || '' }));
    panel.append(content);
    const bar = el('div', { class: 'flex flex-wrap justify-end gap-2 border-t px-5 py-4', style: null });
    bar.classList.add('border-[var(--border)]');
    for (const a of actions) {
      bar.append(el('button', { class: `btn ${a.cls || 'btn-ghost'}`, onclick: async () => {
        if (a.handler) { const r = await a.handler(); if (r === false) return; }
        done(a.value);
      } }, a.icon ? icon(a.icon) : '', a.label));
    }
    panel.append(bar);
    backdrop.addEventListener('mousedown', (e) => { if (e.target === backdrop && dismissable) done(null); });
    document.addEventListener('keydown', onKey);
    root.append(backdrop);
    if (onOpen) onOpen(panel, done);
    setTimeout(() => panel.querySelector('input, textarea, select, button.btn-primary')?.focus(), 30);
  });
}
// Every deletion needs the account password (step-up authentication) - Elvin itself can never delete on its own.
export async function askPassword(message, { title = "Confirm it's you", confirm = 'Confirm & delete' } = {}) {
  const input = el('input', { class: 'input mt-3', type: 'password', autocomplete: 'current-password', maxlength: 200,
    placeholder: 'Your Elvin password', 'aria-label': 'Your password' });
  const body = el('div', {}, el('p', { text: message }), input,
    el('p', { class: 'mt-2 text-xs muted' }, icon('fa-solid fa-lock'), ' Deleting always needs your password. Elvin can only propose a deletion - it never deletes anything on its own, and never anything outside this project.'));
  const ok = await modal({ title, body, actions: [{ label: 'Cancel', value: false }, { label: confirm, value: true, cls: 'btn-danger', icon: 'fa-solid fa-lock' }],
    onOpen: (panel, done) => input.addEventListener('keydown', (e) => { if (e.key === 'Enter' && input.value) { e.preventDefault(); done(true); } }) });
  return ok && input.value ? input.value : null;
}
export function needsPassword(err) {
  return err instanceof ApiError && err.status === 403 && !!(err.data && err.data.detail && err.data.detail.reauth);
}
// Runs fn(password). With askFirst the password is asked up front; otherwise only when the server says it is needed.
// Returns undefined when the person cancels.
export async function withPassword(fn, message, { askFirst = true } = {}) {
  let password = askFirst ? await askPassword(message) : null;
  if (askFirst && password === null) return undefined;
  for (let i = 0; i < 5; i += 1) {
    try { return await fn(password); } catch (err) {
      if (!needsPassword(err)) throw err;
      password = await askPassword(err.message || message);
      if (password === null) return undefined;
    }
  }
  throw new Error('Too many attempts - nothing was deleted.');
}
export function confirmDialog(title, message, { confirm = 'Confirm', danger = false, icon: ic } = {}) {
  return modal({ title, body: message, actions: [{ label: 'Cancel', value: false }, { label: confirm, value: true, cls: danger ? 'btn-danger' : 'btn-primary', icon: ic }] });
}

let signedOutShown = false;
export function signedOut(message) {
  if (signedOutShown) return;
  signedOutShown = true;
  modal({ title: 'You were signed out', dismissable: false,
    body: el('div', { class: 'flex gap-3' }, icon('fa-solid fa-user-lock mt-1 text-2xl text-amber-500'),
      el('div', {}, el('p', { text: message || 'Your session ended.' }), el('p', { class: 'mt-2 muted', text: 'Only one active login per account is allowed. Your running AI tasks keep going on your other device.' }))),
    actions: [{ label: 'Sign in again', value: true, cls: 'btn-primary' }] }).then(() => { location.href = '/login'; });
  setTimeout(() => { location.href = '/login'; }, 15000);
}

// ---------------------------------------------------------------- live events (SSE)
const listeners = new Set();
let source = null;
let lastId = 0;
export function onEvent(fn) { listeners.add(fn); ensureStream(); return () => listeners.delete(fn); }
function ensureStream() {
  if (source || !document.body.dataset.user) return;
  source = new EventSource(`/api/stream${lastId ? `?after=${lastId}` : ''}`);
  source.onmessage = (msg) => {
    let ev;
    try { ev = JSON.parse(msg.data); } catch { return; }
    if (ev.id) lastId = Math.max(lastId, ev.id);
    if (ev.type === 'session_ended') { signedOut(ev.data && ev.data.message); source.close(); return; }
    for (const fn of listeners) { try { fn(ev); } catch (e) { console.error(e); } }
  };
  source.onerror = () => { /* EventSource reconnects by itself with Last-Event-ID */ };
}

// ---------------------------------------------------------------- presence
function startPresence() {
  if (!document.body.dataset.user) return;
  const every = Number(document.body.dataset.heartbeat || 15) * 1000;
  const fp = meta('session-fp');
  const beat = async () => {
    try {
      const r = await post('/api/presence/heartbeat');
      if (fp && r.session_fp && r.session_fp !== fp) { toast('Your session changed in another tab - reloading...', 'warn'); setTimeout(() => location.reload(), 1200); }
      const badge = $('#nav-running');
      if (badge) { badge.classList.toggle('hidden', !r.running.length); $('#nav-running-count').textContent = r.running.length; }
    } catch (e) { /* signedOut() handles 401 */ }
  };
  beat();
  setInterval(beat, every);
  document.addEventListener('visibilitychange', () => { if (!document.hidden) beat(); });
  window.addEventListener('pagehide', () => {
    try { navigator.sendBeacon('/api/presence/leave', new Blob([JSON.stringify({ csrf_token: csrf() })], { type: 'text/plain;charset=UTF-8' })); } catch {}
  });
}

// ---------------------------------------------------------------- chrome: theme, dropdowns, nav actions
function applyTheme(t) {
  const dark = t === 'dark' || (t === 'system' && matchMedia('(prefers-color-scheme: dark)').matches);
  document.documentElement.classList.toggle('dark', dark);
  window.dispatchEvent(new CustomEvent('elvin:theme', { detail: { dark } }));
}
function initTheme() {
  for (const b of $$('[data-theme-toggle]')) {
    b.addEventListener('click', async () => {
      const next = document.documentElement.classList.contains('dark') ? 'light' : 'dark';
      try { localStorage.setItem('elvin-theme', next); } catch {}
      applyTheme(next);
      if (document.body.dataset.user) { try { await post('/api/settings', { theme: next }); } catch {} }
    });
  }
}
function initDropdowns() {
  document.addEventListener('click', (e) => {
    const toggle = e.target.closest('[data-dropdown-toggle]');
    for (const dd of $$('[data-dropdown]')) {
      const menu = $('[data-dropdown-menu]', dd);
      if (toggle && dd.contains(toggle)) {
        const open = menu.classList.toggle('hidden') === false;
        toggle.setAttribute('aria-expanded', String(open));
      } else if (!dd.contains(e.target)) {
        menu.classList.add('hidden');
        $('[data-dropdown-toggle]', dd)?.setAttribute('aria-expanded', 'false');
      }
    }
  });
}
function initNavProjects() {
  document.addEventListener('click', async (e) => {
    const btn = e.target.closest('[data-action="archive-project"], [data-action="delete-project"]');
    if (!btn) return;
    e.preventDefault();
    const row = btn.closest('[data-nav-project]');
    const id = row.dataset.navProject;
    const name = row.dataset.name;
    if (btn.dataset.action === 'archive-project') {
      try { await patch(`/api/projects/${id}`, { archived: true }); toast(`Archived "${name}"`, 'ok'); row.remove(); }
      catch (err) { errorToast(err); }
      return;
    }
    await deleteProjectDialog(id, name);
  });
}
export async function deleteProjectDialog(id, name) {
  const input = el('input', { class: 'input mt-2', placeholder: name, autocomplete: 'off' });
  const password = el('input', { class: 'input mt-2', type: 'password', autocomplete: 'current-password', maxlength: 200, placeholder: 'Your Elvin password' });
  const files = el('input', { type: 'checkbox', class: 'check' });
  const body = el('div', {},
    el('p', {}, 'This removes ', el('strong', { text: name }), ' from Elvin, including its chat, memory and version history.'),
    el('label', { class: 'mt-3 flex items-center gap-2' }, files, el('span', { text: 'Also delete the project files from disk' })),
    el('p', { class: 'mt-1 text-xs muted', text: 'If unticked, the files are moved to the "removed-projects" folder instead.' }),
    el('p', { class: 'mt-3 text-xs font-medium', text: 'Type the project name to confirm:' }), input,
    el('p', { class: 'mt-3 text-xs font-medium' }, icon('fa-solid fa-lock'), ' Your password (every deletion needs it):'), password);
  const ok = await modal({ title: 'Delete project?', body, actions: [{ label: 'Cancel', value: false },
    { label: 'Delete project', value: true, cls: 'btn-danger', icon: 'fa-regular fa-trash-can' }] });
  if (!ok) return false;
  try {
    await post(`/api/projects/${id}/delete`, { confirm_name: input.value, delete_files: files.checked, password: password.value || null });
    toast(`Deleted "${name}"`, 'ok');
    if (location.pathname.includes(id)) location.href = '/dashboard'; else location.reload();
    return true;
  } catch (err) { errorToast(err); return false; }
}
function initReveal() {
  const items = $$('.reveal');
  if (!items.length) return;
  const io = new IntersectionObserver((entries) => {
    for (const en of entries) if (en.isIntersecting) { en.target.classList.add('in'); io.unobserve(en.target); }
  }, { threshold: 0.12 });
  items.forEach((i) => io.observe(i));
}
function initRunningBadge() {
  if (!document.body.dataset.user) return;
  onEvent((ev) => {
    if (ev.type === 'done' || ev.type === 'bughunt_done' || ev.type === 'loadtest_done') {
      const st = ev.data && ev.data.status;
      if (st === 'completed') toast('A task finished.', 'ok', 3000);
    }
    if (ev.type === 'alert' && ev.data && !ev.replay) toast(ev.data.text, 'warn', 9000);
  });
}

// System status pill + "effort reduced" banner (checked every 60 s like the status monitor).
let fallbackShown = null;
async function refreshSystemSummary() {
  if (!document.body.dataset.user) return;
  let s;
  try { s = await get('/api/system/summary'); } catch { return; }
  const dot = $('#nav-status-dot');
  if (dot) {
    const allOk = s.models_total && s.models_online === s.models_total;
    dot.className = `dot ${!s.models_online && s.models_total ? 'dot-bad' : s.fallback_active || !allOk ? 'dot-warn' : 'dot-ok'} absolute right-1.5 top-1.5 !h-1.5 !w-1.5`;
    dot.parentElement.title = s.checked_at
      ? `System status: ${s.models_online}/${s.models_total} AI models online${s.pcs_total != null ? `, gateway PCs ${s.pcs_online}/${s.pcs_total}` : ''}`
      : 'System status';
  }
  const banner = $('#fallback-banner');
  if (!banner) return;
  banner.classList.toggle('hidden', !s.fallback_active);
  if (s.fallback_active) {
    const local = s.gemma_source === 'local';
    banner.replaceChildren(el('div', { class: 'glass flex flex-wrap items-center gap-3 border-amber-500/40 px-4 py-3 text-sm' },
      icon('fa-solid fa-triangle-exclamation text-amber-500'),
      el('span', { class: 'flex-1' }, el('strong', { text: local ? 'Using the local Gemma - ' : s.gateway_configured === false ? 'No Gemma gateway set up - ' : 'Gemma gateway offline - ' }),
        local ? `the gateway is switched off for now, so Elvin uses Gemma 3 in Docker on this computer. It is slower, so effort is reduced to ${s.effort_cap}.`
          : `Elvin is using the local Gemma 3 in Docker on this computer. It is slower, so effort is reduced to ${s.effort_cap} while it is in use.`,
        s.local_online ? '' : ' The local model is not answering yet.'),
      el('a', { class: 'btn btn-ghost btn-sm', href: '/status' }, icon('fa-solid fa-heart-pulse'), 'Status')));
    if (fallbackShown === false) {
      toast(local ? `Using the local Gemma in Docker - effort reduced to ${s.effort_cap}.`
        : `Gateway offline: switched to the local Gemma in Docker - effort reduced to ${s.effort_cap}.`, 'warn', 9000);
    }
  }
  fallbackShown = !!s.fallback_active;
}
function initSystemSummary() {
  if (!document.body.dataset.user) return;
  refreshSystemSummary();
  setInterval(refreshSystemSummary, 60000);
}

initTheme();
initDropdowns();
initNavProjects();
initReveal();
startPresence();
initRunningBadge();
initSystemSummary();
