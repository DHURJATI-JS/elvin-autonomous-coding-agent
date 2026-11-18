import { $, el, errorToast, fmtBytes, get, icon, md, onEvent, post, timeAgo, toast } from './core.js';
import { connectGithub } from './github.js';

let cloneId = null;
let poll = null;
let plain = false;
const KIND = { permission: 'badge-ok', condition: 'badge-warn', limitation: 'badge-info', restriction: 'badge-bad', notice: 'badge' };

async function gate() {
  try {
    const s = await get('/api/github/status');
    const ok = (v) => icon(`fa-solid ${v ? 'fa-circle-check text-emerald-500' : 'fa-circle-xmark text-rose-500'}`);
    $('#gate').replaceChildren(el('span', { class: 'mr-4' }, ok(s.tools.git), ` ${s.tools.git || 'git not installed'}`),
      el('span', { class: 'mr-4' }, ok(s.tools.gh), ` ${s.tools.gh ? s.tools.gh.split('(')[0] : 'GitHub CLI missing'}`),
      s.account ? el('span', {}, ok(true), ` GitHub: @${s.account.login}`)
        : el('span', {}, icon('fa-solid fa-circle-info text-amber-500'), ' GitHub not connected (needed only for private repositories) ',
          el('button', { class: 'text-violet-500 underline', onclick: connectGithub, text: 'Connect' })));
  } catch { /* ignore */ }
}

async function recent() {
  const r = await get('/api/clone');
  $('#recent').replaceChildren(...r.clones.map((c) => el('button', { class: 'menu-item !px-2', onclick: () => open(c.id) },
    icon(`fa-solid ${c.status === 'completed' ? 'fa-circle-check text-emerald-500' : c.status === 'awaiting_agreement' ? 'fa-file-signature text-amber-500' : ['failed', 'infected', 'cancelled'].includes(c.status) ? 'fa-circle-xmark text-rose-500' : 'fa-spinner fa-spin text-violet-500'}`),
    el('span', { class: 'flex-1 truncate', text: c.owner ? `${c.owner}/${c.repo}` : c.url }), el('span', { class: 'text-xs faint', text: `${c.status.replace('_', ' ')} · ${timeAgo(c.created_at)}` }))));
  if (!r.clones.length) $('#recent').append(el('p', { class: 'text-xs muted', text: 'Nothing cloned yet.' }));
}

function statusLine(icn, text) { return el('div', { class: 'glass flex items-center gap-3 p-4 text-sm' }, icon(icn), el('span', { text })); }

function render(c) {
  const view = $('#clone-view');
  if (['inspecting', 'scanning'].includes(c.status)) {
    view.replaceChildren(statusLine('fa-solid fa-spinner fa-spin text-violet-500', c.status === 'scanning' ? 'ClamAV is scanning every file before import...' : 'Downloading into quarantine and reading the README and license...'));
    return;
  }
  if (['failed', 'infected', 'cancelled'].includes(c.status)) {
    view.replaceChildren(statusLine(`fa-solid ${c.status === 'infected' ? 'fa-virus text-rose-500' : 'fa-circle-xmark text-rose-500'}`, c.error || c.status));
    stopPoll();
    return;
  }
  if (c.status === 'completed') {
    view.replaceChildren(el('div', { class: 'glass p-5' }, el('div', { class: 'flex items-center gap-2 font-semibold' }, icon('fa-solid fa-circle-check text-emerald-500'), `${c.owner}/${c.repo} imported`),
      el('p', { class: 'mt-1 text-sm muted', text: `${c.file_count} files · ${fmtBytes(c.size_bytes)} · ClamAV scanned ${c.scan.scanned || 0} files: clean` }),
      el('a', { class: 'btn btn-primary mt-4', href: `/p/${c.project_id}/elvin` }, icon('fa-solid fa-robot'), 'Open the project')));
    stopPoll();
    return;
  }
  // awaiting agreement
  stopPoll();
  const checks = [];
  const statements = el('div', { class: 'mt-4 space-y-2' });
  const simplified = Object.fromEntries((c.simplified || []).map((s) => [s.id, s.plain]));
  const agreeBtn = el('button', { class: 'btn btn-primary sheen', disabled: true, onclick: () => agree(c, checks) }, icon('fa-solid fa-file-signature'), 'I agree - scan & import');
  const update = () => { agreeBtn.disabled = !checks.every((x) => x.cb.checked); };
  for (const s of c.terms) {
    const cb = el('input', { type: 'checkbox', class: 'check mt-1' });
    cb.addEventListener('change', update);
    checks.push({ id: s.id, cb });
    statements.append(el('label', { class: 'flex cursor-pointer items-start gap-3 rounded-xl border border-[var(--border)] p-3 hover:bg-[var(--hover)]' }, cb,
      el('div', { class: 'min-w-0 flex-1' }, el('div', { class: 'flex flex-wrap items-center gap-2' }, el('span', { class: `badge ${KIND[s.kind] || 'badge'}`, text: s.kind }), el('span', { class: 'text-xs faint', text: s.source })),
        el('p', { class: 'mt-1 text-sm', text: plain && simplified[s.id] ? simplified[s.id] : s.text }))));
  }
  const simplifyBtn = el('button', { class: 'btn btn-ghost btn-sm', onclick: async () => {
    if (!c.simplified || !c.simplified.length) {
      simplifyBtn.disabled = true;
      simplifyBtn.replaceChildren(icon('fa-solid fa-spinner fa-spin'), ' Simplifying...');
      try { const r = await post(`/api/clone/${c.id}/simplify`); c.simplified = r.simplified; } catch (err) { errorToast(err); simplifyBtn.disabled = false; return; }
    }
    plain = !plain;
    render(c);
  } }, icon('fa-solid fa-language'), plain ? 'Show original wording' : 'Explain in plain language');
  view.replaceChildren(el('div', { class: 'glass-strong p-5 sm:p-6' },
    el('div', { class: 'flex flex-wrap items-start justify-between gap-3' },
      el('div', {}, el('div', { class: 'text-xl font-semibold', text: `${c.owner}/${c.repo}` }),
        el('div', { class: 'mt-1 text-sm muted', text: `commit ${c.commit} · ${c.file_count} files · ${fmtBytes(c.size_bytes)}` })),
      el('span', { class: `badge ${c.license_spdx ? 'badge-brand' : 'badge-bad'} text-sm`, text: c.license_name || 'No license' })),
    c.summary ? el('div', { class: 'prose-elvin mt-4 text-sm', html: md(c.summary) }) : '',
    c.error ? el('div', { class: 'mt-3 rounded-xl border border-amber-500/30 bg-amber-500/10 p-3 text-sm', text: c.error }) : '',
    el('div', { class: 'mt-5 flex flex-wrap items-center justify-between gap-2' }, el('h3', { class: 'font-semibold', text: 'Terms & conditions - tick each one to agree' }), simplifyBtn),
    statements,
    el('div', { class: 'mt-5 flex flex-wrap justify-end gap-2' },
      el('button', { class: 'btn btn-ghost', onclick: async () => { try { await post(`/api/clone/${c.id}/cancel`); toast('Clone cancelled and deleted', 'ok'); $('#clone-view').replaceChildren(); recent(); } catch (err) { errorToast(err); } } }, 'Cancel'),
      agreeBtn),
    c.readme ? el('details', { class: 'mt-6' }, el('summary', { class: 'cursor-pointer text-sm font-medium', text: 'Read the README' }),
      el('div', { class: 'prose-elvin mt-3 max-h-[60vh] overflow-auto rounded-xl border border-[var(--border)] p-4 text-sm', html: md(c.readme) })) : ''));
}

async function agree(c, checks) {
  try {
    await post(`/api/clone/${c.id}/agree`, { agreed: checks.map((x) => x.id) });
    render({ ...c, status: 'scanning' });
    startPoll();
  } catch (err) { errorToast(err); }
}

async function open(id) {
  cloneId = id;
  plain = false;
  try { render(await get(`/api/clone/${id}`)); } catch (err) { errorToast(err); }
}
function startPoll() {
  stopPoll();
  poll = setInterval(async () => { if (!cloneId) return; try { const c = await get(`/api/clone/${cloneId}`); render(c); if (!['inspecting', 'scanning'].includes(c.status)) { stopPoll(); recent(); } } catch { stopPoll(); } }, 2500);
}
function stopPoll() { if (poll) clearInterval(poll); poll = null; }

$('#clone-form').addEventListener('submit', async (e) => {
  e.preventDefault();
  const url = $('#clone-url').value.trim();
  if (!url) return;
  $('#clone-btn').disabled = true;
  try {
    const r = await post('/api/clone', { url });
    cloneId = r.clone_id;
    render({ status: 'inspecting' });
    startPoll();
    recent();
  } catch (err) { errorToast(err); } finally { $('#clone-btn').disabled = false; }
});
onEvent((ev) => { if (ev.type === 'clone_status' && ev.data.clone_id === cloneId) open(cloneId); });

gate();
recent().catch(errorToast);
const params = new URLSearchParams(location.search);
if (params.get('id')) open(params.get('id'));
else if (params.get('url')) {  // "Clone into Elvin" from the GitHub repositories page
  $('#clone-url').value = params.get('url').slice(0, 500);
  history.replaceState(null, '', location.pathname);
  $('#clone-form').requestSubmit();
}
