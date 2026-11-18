// Elvin chat: plan / permission / apply / verify timeline with live events.
import { $, ApiError, askPassword, del, diffView, el, errorToast, fmtBytes, get, icon, md, modal, onEvent, post, projectId, timeAgo, toast, uid } from './core.js';
import { checkIntegrity } from './integrity.js';

const pid = projectId();
const timeline = $('#timeline');
const promptEl = $('#prompt');
const sendBtn = $('#send-btn');
const stopBtn = $('#stop-btn');
let state = { tasks: {}, activeTask: null, team: '', teams: [], effort: 'medium', versioning: true };
let attachments = [];
let openApprovals = new Set();
let sending = false;
const taskBoxes = new Map();

const STATUS = {
  idle: ['dot-ok', 'idle'], queued: ['dot-live', 'starting'], running: ['dot-live', 'working'],
  awaiting_approval: ['dot-warn', 'waiting for your permission'],
};

function setBusy(taskId, status = 'running') {
  state.activeTask = taskId;
  const [dot, label] = STATUS[taskId ? status : 'idle'] || STATUS.running;
  $('#agent-status').replaceChildren(el('span', { class: `dot ${dot}` }), ` ${label}`);
  sendBtn.classList.toggle('hidden', !!taskId);
  stopBtn.classList.toggle('hidden', !taskId);
  promptEl.disabled = !!taskId;
  promptEl.placeholder = taskId ? 'Elvin is working - stop it to send something new.' : 'Ask Elvin to build, fix, explain or stress test something... (Enter to send, Shift+Enter for a new line)';
}

function scrollDown(force = false) {
  const near = timeline.scrollHeight - timeline.scrollTop - timeline.clientHeight < 260;
  if (force || near) timeline.scrollTop = timeline.scrollHeight;
}

// ------------------------------------------------------------------ rendering
function userBubble(m) {
  const wrap = el('div', { class: 'flex flex-col items-end gap-1.5' });
  for (const a of (m.meta && m.meta.attachments) || []) {
    wrap.append(el('img', { src: `/api/attachments/${a}`, alt: 'Design image', class: 'max-h-48 rounded-xl border border-[var(--border)]', loading: 'lazy' }));
  }
  wrap.append(el('div', { class: 'bubble-user', text: m.content }));
  wrap.append(el('div', { class: 'text-[11px] faint', text: timeAgo(m.created_at) }));
  return wrap;
}
function elvinBubble(content, meta = {}) {
  const body = el('div', { class: 'bubble-elvin prose-elvin', html: md(content) });
  if (meta.kind === 'loadtest_report' || meta.kind === 'loadtest_started') {
    body.append(el('a', { class: 'btn btn-ghost btn-sm mt-2', href: `/p/${pid}/loadtest` }, icon('fa-solid fa-chart-line'), 'Open Load Test'));
  }
  return el('div', { class: 'flex items-start gap-2.5' }, el('span', { class: 'avatar-elvin' }, icon('fa-solid fa-wand-magic-sparkles text-xs')), body);
}
function taskBox(taskId) {
  if (taskBoxes.has(taskId)) return taskBoxes.get(taskId);
  const box = el('div', { class: 'ml-10 space-y-2', dataset: { task: taskId } });
  taskBoxes.set(taskId, box);
  return box;
}
function line(ic, content, cls = '') {
  return el('div', { class: `event-card flex items-start gap-2.5 ${cls}` }, icon(`${ic} mt-0.5 w-4 text-center`), el('div', { class: 'min-w-0 flex-1' }, content));
}

function renderEvent(taskId, ev, live = false) {
  const box = taskBox(taskId);
  const d = ev.data || {};
  let node = null;
  switch (ev.type) {
    case 'model':
      node = el('div', { class: 'text-xs faint' }, icon('fa-solid fa-microchip'), ` ${d.stage}: ${d.label}`);
      break;
    case 'plan': {
      const steps = el('ol', { class: 'mt-1.5 list-decimal space-y-0.5 pl-5' }, ...(d.steps || []).map((s) => el('li', { text: s.title })));
      node = line('fa-solid fa-map text-violet-500', el('div', {},
        el('div', { class: 'font-medium', text: d.needs_changes ? `Plan · ${d.steps.length} step${d.steps.length === 1 ? '' : 's'}` : 'Answer ready' }),
        d.needs_changes && d.reply ? el('div', { class: 'prose-elvin mt-1 muted', html: md(d.reply) }) : '', d.steps && d.steps.length ? steps : ''));
      break;
    }
    case 'library': {
      const cls = d.verdict === 'go' ? 'badge-ok' : d.verdict === 'go_with_version' ? 'badge-warn' : 'badge-bad';
      node = line('fa-solid fa-shield-virus text-teal-500', el('div', {},
        el('div', { class: 'flex flex-wrap items-center gap-2' }, el('span', { class: 'font-medium', text: `${d.name}` }), el('span', { class: 'badge', text: d.ecosystem }),
          el('span', { class: `badge ${cls}`, text: d.verdict === 'go' ? 'go' : d.verdict === 'go_with_version' ? 'go - safe version' : 'blocked' })),
        el('div', { class: 'mt-0.5 text-xs muted', text: `${d.reason || ''}${d.recommended ? ` → ${d.recommended}` : ''}` })));
      break;
    }
    case 'step': {
      const ic = { coding: 'fa-solid fa-pen-nib text-violet-500', done: 'fa-solid fa-circle-check text-emerald-500', rejected: 'fa-solid fa-ban text-rose-500', nothing_to_do: 'fa-regular fa-circle text-slate-400' }[d.state] || 'fa-solid fa-pen-nib';
      const txt = { coding: 'writing code', done: 'done', rejected: 'skipped (rejected)', nothing_to_do: 'nothing to change' }[d.state] || d.state;
      node = el('div', { class: 'flex items-center gap-2 text-sm' }, icon(ic), el('span', { class: 'font-medium', text: `Step ${d.no}/${d.total}: ${d.title}` }), el('span', { class: 'text-xs muted', text: `- ${txt}` }));
      break;
    }
    case 'approval_required':
      node = approvalCard(taskId, d, live);
      break;
    case 'approval_decided':
      node = el('div', { class: 'text-xs muted' }, icon(d.decision === 'approve' ? 'fa-solid fa-check text-emerald-500' : 'fa-solid fa-xmark text-rose-500'),
        ` You ${d.decision === 'approve' ? `approved ${d.approved.length} change(s)` : d.decision === 'stop' ? 'stopped the task' : 'rejected this step'}`);
      for (const c of box.querySelectorAll(`[data-batch="${d.batch_id}"] [data-review]`)) c.remove();
      break;
    case 'applied':
      node = line('fa-solid fa-floppy-disk text-emerald-500', el('div', {}, el('div', { class: 'font-medium', text: `Applied ${d.files.length} change(s)` }),
        el('div', { class: 'mt-0.5 font-mono text-xs muted', text: d.files.map((f) => `${f.type === 'delete' ? '−' : f.type === 'mkdir' ? '▸' : '+'} ${f.path}`).join('   ') })));
      break;
    case 'autosaved':
      node = el('div', { class: 'flex items-center gap-2 text-xs muted' }, icon(`fa-solid ${d.verified ? 'fa-shield-halved text-emerald-500' : 'fa-triangle-exclamation text-amber-500'}`),
        `Autosaved version ${new Date(d.created_at).toLocaleTimeString()} · ${d.files} files · ${d.verified ? 'verified' : 'verification failed'}`);
      loadVersions();
      break;
    case 'verified':
      node = el('div', { class: 'flex items-center gap-2 text-xs muted' }, icon(`fa-solid ${d.ok ? 'fa-certificate text-emerald-500' : 'fa-triangle-exclamation text-amber-500'}`), d.text);
      break;
    case 'command': {
      const out = `${d.stdout || ''}${d.stderr ? `\n${d.stderr}` : ''}`.trim();
      node = line(`fa-solid fa-terminal ${d.ok ? 'text-emerald-500' : 'text-rose-500'}`, el('div', {},
        el('div', { class: 'flex flex-wrap items-center gap-2' }, el('code', { class: 'text-xs', text: `$ ${d.command}` }),
          el('span', { class: `badge ${d.ok ? 'badge-ok' : 'badge-bad'}`, text: d.timed_out ? 'timed out' : `exit ${d.exit_code}` }),
          d.attempt > 1 ? el('span', { class: 'badge', text: `attempt ${d.attempt}` }) : ''),
        out ? el('details', { class: 'mt-1.5', open: !d.ok }, el('summary', { class: 'cursor-pointer text-xs muted', text: 'Output' }), el('pre', { class: 'terminal mt-1', text: out })) : ''));
      break;
    }
    case 'blocked':
      node = line('fa-solid fa-hand text-rose-500', el('div', { class: 'text-sm', text: `Blocked: ${d.path} - ${d.reason}` }));
      break;
    case 'warning':
      node = line('fa-solid fa-triangle-exclamation text-amber-500', el('div', { class: 'text-sm', text: d.text }));
      break;
    case 'memory':
      node = line('fa-solid fa-brain text-pink-500', el('div', {}, el('div', { class: 'text-sm font-medium', text: 'Elvin will remember (this project only):' }),
        el('ul', { class: 'mt-1 list-disc pl-5 text-sm muted' }, ...(d.facts || []).map((f) => el('li', { text: f })))));
      if (live) loadMemory();
      break;
    case 'design_spec':
      node = line('fa-solid fa-eye text-pink-500', el('div', {}, el('div', { class: 'font-medium', text: 'Design understood' }),
        el('div', { class: 'mt-0.5 text-xs muted', text: (d.spec && (d.spec.layout || d.spec.title)) || '' })));
      break;
    case 'screenshot':
      node = el('div', { class: 'event-card' }, el('div', { class: 'mb-2 text-xs font-medium', text: `Sandbox screenshot · iteration ${d.iteration}` }),
        el('img', { src: `/api/attachments/${d.attachment_id}`, alt: 'Rendered page', class: 'w-full rounded-lg border border-[var(--border)]', loading: 'lazy' }));
      break;
    case 'design_verdict':
      node = line('fa-solid fa-scale-balanced text-cyan-500', el('div', {}, el('div', { class: 'font-medium', text: `Visual match: ${Math.round(d.score)}/10` }),
        (d.differences || []).length ? el('ul', { class: 'mt-1 list-disc pl-5 text-xs muted' }, ...d.differences.map((x) => el('li', { text: x }))) : ''));
      break;
    case 'review':
      node = line('fa-solid fa-magnifying-glass text-indigo-500', el('div', {}, el('div', { class: 'font-medium', text: 'Review pass' }), el('div', { class: 'prose-elvin mt-1 text-sm', html: md(d.text) })));
      break;
    case 'rolled_back':
      node = line('fa-solid fa-rotate-left text-amber-500', el('div', { class: 'text-sm', text: d.text }));
      break;
    case 'alert':
      node = line('fa-solid fa-gauge text-amber-500', el('div', {}, el('div', { class: 'font-medium', text: 'Reduced effort' }),
        el('div', { class: 'mt-0.5 text-sm muted', text: d.text })), 'border-amber-500/40');
      break;
    case 'loadtest_launched':
      node = line('fa-solid fa-gauge-simple-high text-orange-500', el('div', {}, el('span', { text: `Locust started against ${d.host}. ` }),
        el('a', { class: 'text-violet-500 underline', href: `/p/${pid}/loadtest`, text: 'Watch it live' })));
      break;
    case 'message':
      timeline.append(elvinBubble(d.content, d.meta || {}));
      break;
    case 'done': {
      const t = state.tasks[taskId];
      if (t) { t.status = d.status; }
      if (d.status !== 'completed') box.append(retryBar(taskId, d.status, d.error || d.reason));
      if (state.activeTask === taskId) setBusy(null);
      loadVersions();
      break;
    }
    default:
      break;
  }
  if (node) box.append(node);
  if (live) scrollDown();
}

function retryBar(taskId, status, reason) {
  return el('div', { class: 'flex flex-wrap items-center gap-2 text-xs' },
    el('span', { class: `badge ${status === 'failed' ? 'badge-bad' : 'badge-warn'}`, text: status }),
    reason ? el('span', { class: 'muted', text: reason }) : '',
    el('button', { class: 'btn btn-ghost btn-sm', dataset: { retry: taskId }, onclick: () => retry(taskId) }, icon('fa-solid fa-rotate-right'), 'Retry from the start'));
}

function approvalCard(taskId, d, live) {
  const card = el('div', { class: 'event-card border-violet-500/40 ring-glow', dataset: { batch: d.batch_id } },
    el('div', { class: 'flex flex-wrap items-center justify-between gap-2' },
      el('div', { class: 'font-medium' }, icon('fa-solid fa-shield-halved text-violet-500'), ` ${d.title}`),
      el('span', { class: 'badge badge-warn', text: `${d.actions.length} change${d.actions.length === 1 ? '' : 's'} need your permission` })),
    el('div', { class: 'mt-1.5 font-mono text-xs muted', text: d.actions.map((a) => a.type === 'run' ? `$ ${a.command}` : a.type === 'loadtest' ? a.command : `${a.type} ${a.path}`).join('   ') }));
  const pending = state.pending && state.pending.has(d.batch_id);
  if (live || pending) {
    card.append(el('div', { class: 'mt-2 flex justify-end', dataset: { review: '1' } },
      el('button', { class: 'btn btn-primary btn-sm', onclick: () => openApproval(taskId, d) }, icon('fa-solid fa-eye'), 'Review & decide')));
  }
  return card;
}

async function openApproval(taskId, d) {
  if (openApprovals.has(d.batch_id)) return;
  openApprovals.add(d.batch_id);
  const checks = new Map();
  const list = el('div', { class: 'space-y-2' });
  for (const a of d.actions) {
    const cb = el('input', { type: 'checkbox', class: 'check mt-1', checked: true });
    checks.set(a.idx, cb);
    const title = a.type === 'run' ? el('code', { class: 'text-sm', text: `$ ${a.command}` })
      : a.type === 'loadtest' ? el('span', { class: 'font-medium', text: a.command })
      : el('span', { class: 'font-mono text-sm', text: a.path });
    const kind = { write: a.is_new ? 'create' : 'edit', delete: 'delete', mkdir: 'new folder', run: 'run in sandbox', loadtest: 'load test' }[a.type];
    const header = el('label', { class: 'flex cursor-pointer items-start gap-3' }, cb,
      el('div', { class: 'min-w-0 flex-1' }, el('div', { class: 'flex flex-wrap items-center gap-2' },
        el('span', { class: `badge ${a.type === 'delete' ? 'badge-bad' : a.type === 'run' || a.type === 'loadtest' ? 'badge-warn' : 'badge-brand'}`, text: kind }), title,
        a.type === 'write' || a.type === 'delete' ? el('span', { class: 'text-xs' }, el('span', { class: 'text-emerald-500', text: `+${a.added}` }), ' ', el('span', { class: 'text-rose-500', text: `−${a.removed}` })) : '',
        a.bytes ? el('span', { class: 'text-xs faint', text: fmtBytes(a.bytes) }) : ''),
        ...(a.warnings || []).map((w) => el('div', { class: 'mt-1 text-xs text-amber-600 dark:text-amber-400' }, icon('fa-solid fa-triangle-exclamation'), ` ${w}`))));
    const item = el('div', { class: 'rounded-xl border border-[var(--border)] p-3' }, header);
    if (a.diff) {
      item.append(el('details', { class: 'mt-2', open: d.actions.length <= 3 }, el('summary', { class: 'cursor-pointer text-xs muted', text: a.type === 'loadtest' ? 'Endpoints & weights' : 'Show the exact change' }),
        el('div', { class: 'mt-2 max-h-[45vh] overflow-auto' }, diffView(a.diff))));
    }
    list.append(item);
  }
  const body = el('div', {},
    el('p', { class: 'mb-3 muted' }, 'Elvin will only do what you tick. ', d.versioning ? 'A timestamped, verified version is saved after applying.' : 'Versioning is off - Elvin still applies changes all-or-nothing.'),
    list);
  const choice = await modal({ title: `Permission needed · ${d.title}`, body, wide: true, dismissable: true, actions: [
    { label: 'Stop task', value: 'stop', cls: 'btn-ghost text-rose-500', icon: 'fa-solid fa-hand' },
    { label: 'Reject step', value: 'reject', cls: 'btn-ghost', icon: 'fa-solid fa-xmark' },
    { label: 'Approve selected', value: 'approve', cls: 'btn-primary', icon: 'fa-solid fa-check' }] });
  openApprovals.delete(d.batch_id);
  if (!choice) return; // closed: decide later from the card
  const approved = [...checks].filter(([, cb]) => cb.checked).map(([idx]) => idx);
  const decision = choice === 'approve' && !approved.length ? 'reject' : choice;
  let password = null;
  const deletes = decision === 'approve' ? d.actions.filter((a) => a.type === 'delete' && approved.includes(a.idx)).map((a) => a.path) : [];
  if (deletes.length) {
    password = await askPassword(`Elvin wants to delete ${deletes.length} file(s) in this project: ${deletes.slice(0, 5).join(', ')}${deletes.length > 5 ? ' ...' : ''}. Enter your password to allow it.`,
      { title: 'Allow Elvin to delete?' });
    if (!password) { toast('Nothing was deleted - the request is still waiting for your decision.', 'info'); return; }
  }
  try {
    await post(`/api/approvals/${d.batch_id}`, { decision, approved, password });
    state.pending?.delete(d.batch_id);
  } catch (err) { errorToast(err); }
}

// ------------------------------------------------------------------ data
async function loadConversation() {
  const c = await get(`/api/projects/${pid}/conversation`);
  state = { ...state, tasks: c.tasks, team: c.team, teams: c.teams, effort: c.effort, versioning: c.versioning,
    pending: new Set(c.pending_approvals.filter((p) => p.live).map((p) => p.batch_id)) };
  timeline.replaceChildren();
  taskBoxes.clear();
  if (!c.messages.length) {
    timeline.append(el('div', { class: 'mx-auto mt-10 max-w-md text-center' },
      el('span', { class: 'avatar-elvin mx-auto !h-14 !w-14 text-xl' }, icon('fa-solid fa-wand-magic-sparkles')),
      el('h2', { class: 'mt-4 text-xl font-semibold', text: "Hi, I'm Elvin." }),
      el('p', { class: 'mt-2 text-sm muted', text: 'Tell me what to build or fix in this project. I plan first, check every library for vulnerabilities, and ask before I change anything.' })));
  }
  const seen = new Set();
  for (const m of c.messages) {
    if (m.role === 'user') {
      timeline.append(userBubble(m));
      if (m.task_id && c.tasks[m.task_id]) {
        const box = taskBox(m.task_id);
        timeline.append(box);
        seen.add(m.task_id);
        for (const ev of c.tasks[m.task_id].events) if (ev.type !== 'message') renderEvent(m.task_id, ev);
        const t = c.tasks[m.task_id];
        if (['failed', 'cancelled', 'interrupted'].includes(t.status) && !t.events.some((e) => e.type === 'done')) box.append(retryBar(m.task_id, t.status, t.error));
      }
    } else if (m.role === 'system' && m.meta && m.meta.kind === 'run') {
      timeline.append(runCard({ command: m.content.replace(/^\$ /, ''), ...m.meta, created_at: m.created_at }));
    } else {
      timeline.append(elvinBubble(m.content, m.meta || {}));
    }
  }
  // team selector
  const sel = $('#team-select');
  sel.replaceChildren(...c.teams.filter((t) => t.available).map((t) => el('option', { value: t.id, text: t.label, selected: t.id === c.team })));
  $('#team-badge').textContent = (c.teams.find((t) => t.id === c.team) || {}).label || c.team;
  $('#effort-badge').textContent = `effort: ${c.effort}`;
  setBusy(c.active_task, c.active_task ? c.tasks[c.active_task]?.status : null);
  renderMemory(c.memory);
  scrollDown(true);
  for (const p of c.pending_approvals) if (p.live) openApproval(p.task_id, { batch_id: p.batch_id, title: p.title, actions: p.actions, versioning: state.versioning });
}

function renderMemory(items) {
  const box = $('#memory');
  $('#mem-count').textContent = `${items.length} / 100`;
  box.replaceChildren(...items.map((m) => el('div', { class: 'group flex items-start gap-2 rounded-lg px-2 py-1.5 hover:bg-[var(--hover)]' },
    icon(`fa-solid ${m.source === 'user' ? 'fa-thumbtack text-violet-500' : 'fa-brain text-pink-500'} mt-1 text-xs`),
    el('span', { class: 'flex-1', text: m.content }),
    el('button', { class: 'opacity-0 transition group-hover:opacity-100', title: 'Forget', onclick: async () => {
      try { await del(`/api/projects/${pid}/memory/${m.id}`); loadMemory(); } catch (err) { errorToast(err); }
    } }, icon('fa-solid fa-xmark text-xs')))));
  if (!items.length) box.append(el('p', { class: 'text-xs muted', text: 'Nothing yet. Tell Elvin your preferences once - e.g. "always use type hints".' }));
}
async function loadMemory() {
  try { const c = await get(`/api/projects/${pid}/conversation`); renderMemory(c.memory); } catch { /* ignore */ }
}
async function loadVersions() {
  try {
    const r = await get(`/api/projects/${pid}/snapshots?limit=6`);
    const box = $('#versions');
    box.replaceChildren(...r.snapshots.map((s) => el('a', { class: 'flex items-center gap-2 rounded-lg px-2 py-1.5 hover:bg-[var(--hover)]', href: `/p/${pid}/history` },
      icon(`fa-solid ${s.verify_ok === false ? 'fa-triangle-exclamation text-amber-500' : s.verify_ok ? 'fa-shield-halved text-emerald-500' : 'fa-clock text-slate-400'} text-xs`),
      el('span', { class: 'flex-1 truncate', text: s.label || s.kind }), el('span', { class: 'shrink-0 text-xs faint', text: new Date(s.created_at).toLocaleTimeString() }))));
    if (!r.snapshots.length) box.append(el('p', { class: 'text-xs muted', text: r.versioning_enabled ? 'No versions yet.' : 'Versioning is turned off in Settings.' }));
  } catch { /* ignore */ }
}

// ------------------------------------------------------------------ actions
function draftKey() {
  let k = sessionStorage.getItem(`elvin-draft-${pid}`);
  if (!k) { k = uid(); sessionStorage.setItem(`elvin-draft-${pid}`, k); }
  return k;
}
async function send() {
  const content = promptEl.value.trim();
  if (!content || sending || state.activeTask) return;
  sending = true;
  sendBtn.disabled = true;
  try {
    const body = { content, idempotency_key: draftKey(), attachments: attachments.map((a) => a.id),
      mode: attachments.length ? 'design' : 'code', folder: $('#design-folder').value || 'design', team: $('#team-select').value || null };
    const r = await post(`/api/projects/${pid}/messages`, body);
    sessionStorage.removeItem(`elvin-draft-${pid}`);
    promptEl.value = '';
    attachments = [];
    renderAttachments();
    state.tasks[r.task_id] = { id: r.task_id, status: 'queued', events: [] };
    timeline.querySelector('.mx-auto.mt-10')?.remove();
    timeline.append(userBubble({ content, created_at: new Date().toISOString(), meta: { attachments: body.attachments } }));
    timeline.append(taskBox(r.task_id));
    taskBox(r.task_id).append(el('div', { class: 'typing flex gap-1 py-1', dataset: { typing: '1' } }, el('span'), el('span'), el('span')));
    setBusy(r.task_id, 'queued');
    scrollDown(true);
  } catch (err) {
    if (err instanceof ApiError && err.status === 409) toast(err.message, 'warn'); else errorToast(err);
  } finally { sending = false; sendBtn.disabled = false; }
}
async function stop() {
  if (!state.activeTask) return;
  stopBtn.disabled = true;
  try { await post(`/api/tasks/${state.activeTask}/cancel`); toast('Stopping - Elvin is putting your files back to where the task began.', 'info'); }
  catch (err) { errorToast(err); }
  finally { stopBtn.disabled = false; }
}
async function retry(taskId) {
  try {
    const r = await post(`/api/tasks/${taskId}/retry`);
    toast('Retrying from the original starting point', 'info');
    await loadConversation();
    setBusy(r.task_id, 'queued');
  } catch (err) { errorToast(err); }
}

// ------------------------------------------------------------------ run code yourself
function runCard(r) {
  const ok = r.exit_code === 0 && !r.timed_out;
  return el('div', { class: 'ml-10 event-card' },
    el('div', { class: 'flex flex-wrap items-center gap-2' }, icon(`fa-solid fa-terminal ${ok ? 'text-emerald-500' : 'text-rose-500'}`),
      el('span', { class: 'text-xs font-medium', text: 'You ran' }), el('code', { class: 'text-xs', text: `$ ${r.command}` }),
      el('span', { class: `badge ${ok ? 'badge-ok' : 'badge-bad'}`, text: r.timed_out ? 'timed out' : `exit ${r.exit_code}` }),
      el('span', { class: 'text-xs faint', text: `${((r.duration_ms || 0) / 1000).toFixed(1)}s` })),
    el('pre', { class: 'terminal mt-2', text: (r.output || '(no output)').slice(-8000) }),
    el('div', { class: 'mt-2 flex justify-end' }, el('button', { class: 'btn btn-ghost btn-sm', onclick: () => {
      promptEl.value = ok ? `I ran \`${r.command}\`. Check the output above and tell me if anything looks wrong.`
        : `I ran \`${r.command}\` and it failed. Check the output above and fix the problem.`;
      promptEl.focus();
    } }, icon('fa-solid fa-wand-magic-sparkles'), 'Ask Elvin to check this')));
}

async function runCommand(command, force = false) {
  try {
    const r = await post(`/api/projects/${pid}/run`, { command, source: 'elvin', force });
    timeline.querySelector('.mx-auto.mt-10')?.remove();
    timeline.append(runCard(r));
    scrollDown(true);
  } catch (err) {
    if (err instanceof ApiError && err.status === 409 && err.data && err.data.detail && err.data.detail.blocked) {
      const list = err.data.detail.blocked.map((b) => `${b.name}: ${b.reason}`).join('\n');
      const go = await modal({ title: 'Blocked by the vulnerability detector', body: el('div', {}, el('p', { class: 'muted', text: err.message }),
        el('pre', { class: 'terminal mt-3', text: list })), actions: [{ label: 'Cancel', value: false }, { label: 'Run anyway', value: true, cls: 'btn-danger' }] });
      if (go) runCommand(command, true);
    } else errorToast(err);
  }
}

$('#run-btn').addEventListener('click', async () => {
  let remembered = '';
  try { remembered = localStorage.getItem(`elvin-run-${pid}`) || ''; } catch { remembered = ''; }
  const input = el('input', { class: 'input font-mono', placeholder: 'python main.py', maxlength: 500, value: remembered });
  const chips = el('div', { class: 'mt-2 flex flex-wrap gap-1.5' }, ...['python main.py', 'python -m pytest -q', 'npm install && npm test', 'node index.js', 'ls -la']
    .map((c) => el('button', { type: 'button', class: 'badge hover:bg-[var(--hover)]', onclick: () => { input.value = c; input.focus(); } }, c)));
  const body = el('div', {}, el('p', { class: 'mb-2 muted', text: 'Runs on a copy of this project in the isolated sandbox (Linux). Elvin sees the output in your next message.' }), input, chips);
  const ok = await modal({ title: 'Run a command', body, actions: [{ label: 'Cancel', value: false }, { label: 'Run', value: true, cls: 'btn-primary', icon: 'fa-solid fa-play' }],
    onOpen: (panel, done) => input.addEventListener('keydown', (e) => { if (e.key === 'Enter') { e.preventDefault(); done(true); } }) });
  const command = input.value.trim();
  if (!ok || !command) return;
  try { localStorage.setItem(`elvin-run-${pid}`, command); } catch {}
  const close = toast(`Running \`${command}\` in the sandbox...`, 'info', 0);
  await runCommand(command);
  close();
});

function renderAttachments() {
  const box = $('#attachments');
  box.replaceChildren(...attachments.map((a, i) => el('div', { class: 'relative' },
    el('img', { src: a.url, alt: 'Attached design', class: 'h-16 w-24 rounded-lg border border-[var(--border)] object-cover' }),
    el('button', { class: 'absolute -right-1.5 -top-1.5 flex h-5 w-5 items-center justify-center rounded-full bg-rose-500 text-[10px] text-white', title: 'Remove',
      onclick: () => { attachments.splice(i, 1); renderAttachments(); } }, icon('fa-solid fa-xmark')))));
  $('#design-folder').classList.toggle('hidden', !attachments.length);
  if (attachments.length) box.append(el('span', { class: 'self-center text-xs muted', text: 'Design mode: Elvin will build this page and check screenshots of it.' }));
}
$('#image-input').addEventListener('change', async (e) => {
  const file = e.target.files[0];
  e.target.value = '';
  if (!file) return;
  const fd = new FormData();
  fd.append('image', file, file.name);
  const close = toast('Uploading and virus-scanning the image...', 'info', 0);
  try { const r = await post(`/api/projects/${pid}/attachments`, fd); attachments = [{ id: r.id, url: r.url }]; renderAttachments(); }
  catch (err) { errorToast(err); }
  finally { close(); }
});

sendBtn.addEventListener('click', send);
stopBtn.addEventListener('click', stop);
promptEl.addEventListener('keydown', (e) => { if (e.key === 'Enter' && !e.shiftKey && !e.isComposing) { e.preventDefault(); send(); } });
promptEl.addEventListener('input', () => { $('#char-count').textContent = promptEl.value.length > 500 ? `${promptEl.value.length}/8000` : ''; });
for (const b of document.querySelectorAll('[data-example]')) b.addEventListener('click', () => { promptEl.value = b.dataset.example; promptEl.focus(); });
$('#memory-form').addEventListener('submit', async (e) => {
  e.preventDefault();
  const input = $('#memory-input');
  if (!input.value.trim()) return;
  try { await post(`/api/projects/${pid}/memory`, { content: input.value.trim() }); input.value = ''; loadMemory(); } catch (err) { errorToast(err); }
});

onEvent((ev) => {
  if (ev.project_id !== pid && ev.type !== 'chat_message') return;
  if (ev.type === 'chat_message') {
    if (ev.data.project_id === pid) { timeline.append(elvinBubble(ev.data.content, ev.data.meta || {})); scrollDown(); }
    return;
  }
  const taskId = ev.task_id;
  if (!taskId || !(taskId in state.tasks || taskBoxes.has(taskId))) {
    if (ev.type === 'loadtest_sample' || ev.type.startsWith('bughunt') || ev.type.startsWith('loadtest')) return;
  }
  if (!state.tasks[taskId]) state.tasks[taskId] = { id: taskId, status: 'running', events: [] };
  if (!taskBoxes.has(taskId)) { if (ev.type.startsWith('bughunt') || ev.type.startsWith('loadtest')) return; timeline.append(taskBox(taskId)); }
  taskBox(taskId).querySelector('[data-typing]')?.remove();
  if (ev.type === 'status') {
    if (state.activeTask === taskId) $('#agent-status').replaceChildren(el('span', { class: 'dot dot-live' }), ` ${ev.data.text || 'working'}`);
    return;
  }
  if (ev.type === 'approval_required') { setBusy(taskId, 'awaiting_approval'); state.pending = state.pending || new Set(); state.pending.add(ev.data.batch_id); }
  if (ev.type === 'approval_decided' && state.activeTask === taskId) setBusy(taskId, 'running');
  renderEvent(taskId, ev, true);
  if (ev.type === 'approval_required') openApproval(taskId, ev.data);
});

checkIntegrity();
loadConversation().then(() => {
  const ask = new URLSearchParams(location.search).get('ask');
  if (ask && !state.activeTask) { promptEl.value = ask.slice(0, 8000); promptEl.focus(); history.replaceState(null, '', location.pathname); }
}).catch(errorToast);
loadVersions();
