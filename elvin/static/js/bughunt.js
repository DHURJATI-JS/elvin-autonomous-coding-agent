import { $, el, errorToast, fmtBytes, get, icon, onEvent, post, projectId, timeAgo, toast } from './core.js';
import { checkIntegrity } from './integrity.js';

const pid = projectId();
const SEV = ['critical', 'high', 'medium', 'low', 'info'];
let provider = null;
let current = null;
let findings = [];
let sevFilter = 'all';

async function loadFolders() {
  const r = await get(`/api/projects/${pid}/folders`);
  $('#folder').replaceChildren(...r.folders.map((f) => el('option', { value: f, text: f ? `/${f}` : '/ (whole project)' })));
  await loadOptions();
}
async function loadOptions() {
  const folder = $('#folder').value;
  $('#scope-info').textContent = 'Counting files...';
  try {
    const o = await get(`/api/projects/${pid}/bughunt/options?folder=${encodeURIComponent(folder)}`);
    $('#scope-info').replaceChildren(`${o.file_count} code files · ${fmtBytes(o.byte_count)}`,
      o.large ? el('span', { class: 'badge badge-warn ml-2', text: 'large project' }) : '',
      o.skipped ? el('span', { class: 'ml-2 faint', text: `(${o.skipped} skipped: too big)` }) : '');
    const box = $('#providers');
    box.replaceChildren();
    const firstOk = o.providers.find((p) => p.available);
    if (!o.providers.some((p) => p.key === provider && p.available)) provider = firstOk ? firstOk.key : null;
    for (const p of o.providers) {
      const radio = el('input', { type: 'radio', name: 'provider', value: p.key, class: 'check mt-1', checked: p.key === provider, disabled: !p.available });
      radio.addEventListener('change', () => { provider = p.key; });
      box.append(el('label', { class: `flex items-start gap-3 rounded-xl border border-[var(--border)] p-3 ${p.available ? 'cursor-pointer hover:bg-[var(--hover)]' : 'opacity-60'}` },
        radio, el('div', {}, el('div', { class: 'text-sm font-medium', text: p.label }), p.reason ? el('div', { class: 'text-xs muted', text: p.reason }) : '')));
    }
    $('#start').disabled = !provider || !o.file_count;
  } catch (err) { $('#scope-info').textContent = err.message; }
}
$('#folder').addEventListener('change', loadOptions);

$('#start').addEventListener('click', async () => {
  if (!provider) return;
  $('#start').disabled = true;
  try {
    const r = await post(`/api/projects/${pid}/bughunt`, { folder: $('#folder').value, provider });
    current = r.run_id;
    setRunning(true);
    toast('Bug hunt started - findings appear when every chunk is reviewed.', 'info');
    loadRuns();
  } catch (err) { errorToast(err); $('#start').disabled = false; }
});
$('#cancel').addEventListener('click', async () => { if (current) { try { await post(`/api/bughunt/${current}/cancel`); } catch (err) { errorToast(err); } } });

function setRunning(on, done = 0, total = 0) {
  $('#start').classList.toggle('hidden', on);
  $('#cancel').classList.toggle('hidden', !on);
  $('#progress-box').classList.toggle('hidden', !on);
  if (on) {
    const pct = total ? Math.round((done / total) * 100) : 0;
    $('#progress-pct').textContent = `${pct}%`;
    $('#progress-bar').style.width = `${pct}%`;
    $('#progress-label').textContent = total ? `Reviewed ${done} of ${total} chunks...` : 'Collecting files...';
  } else { $('#start').disabled = false; }
}

async function loadRuns() {
  const r = await get(`/api/projects/${pid}/bughunt/runs`);
  const box = $('#runs');
  box.replaceChildren(...r.runs.map((run) => el('button', { class: `menu-item !px-2 ${run.id === current ? 'bg-[var(--hover)]' : ''}`, onclick: () => showRun(run.id) },
    icon(`fa-solid ${run.status === 'running' ? 'fa-spinner fa-spin text-violet-500' : run.status === 'completed' ? 'fa-circle-check text-emerald-500' : 'fa-circle-xmark text-rose-500'}`),
    el('span', { class: 'flex-1 truncate', text: `/${run.folder} · ${run.provider_label}` }), el('span', { class: 'text-xs faint', text: `${run.finding_count} · ${timeAgo(run.created_at)}` }))));
  if (!r.runs.length) box.append(el('p', { class: 'text-xs muted', text: 'No runs yet.' }));
  const running = r.runs.find((x) => x.status === 'running');
  if (running && !current) { current = running.id; setRunning(true, running.progress.done || 0, running.progress.total || 0); }
  if (!current && r.runs.length) showRun(r.runs[0].id);
}

async function showRun(id) {
  current = id;
  const r = await get(`/api/bughunt/${id}`);
  findings = r.findings;
  const run = r.run;
  $('#result-title').textContent = `Findings · /${run.folder} · ${run.provider_label}`;
  $('#result-sub').textContent = `${run.status} · ${run.file_count} files · ${timeAgo(run.created_at)}${run.error ? ` · ${run.error}` : ''}`;
  $('#export').classList.remove('hidden');
  $('#export').href = `/api/bughunt/${id}/export.csv`;
  if (run.status === 'running') setRunning(true, run.progress.done || 0, run.progress.total || 0); else setRunning(false);
  renderFindings();
  loadRunsListOnly();
}
async function loadRunsListOnly() {
  const r = await get(`/api/projects/${pid}/bughunt/runs`);
  const box = $('#runs');
  for (const b of box.children) b.classList.remove('bg-[var(--hover)]');
  void r;
}

function renderFindings() {
  const counts = Object.fromEntries(SEV.map((s) => [s, findings.filter((f) => f.severity === s).length]));
  const seg = $('#sev-filter');
  seg.replaceChildren(...['all', ...SEV].map((s) => el('button', { 'aria-pressed': String(s === sevFilter), onclick: () => { sevFilter = s; renderFindings(); } },
    s === 'all' ? `All ${findings.length}` : `${s} ${counts[s]}`)));
  const list = findings.filter((f) => sevFilter === 'all' || f.severity === sevFilter);
  const box = $('#findings');
  box.replaceChildren();
  if (!list.length) {
    box.append(el('div', { class: 'rounded-2xl border border-dashed border-[var(--border-strong)] p-8 text-center text-sm muted' },
      icon('fa-solid fa-shield-halved text-3xl text-emerald-500'), el('p', { class: 'mt-2', text: findings.length ? 'No findings at this severity.' : 'No findings yet.' })));
    return;
  }
  for (const f of list) {
    box.append(el('div', { class: 'rounded-2xl border border-[var(--border)] p-4' },
      el('div', { class: 'flex flex-wrap items-center gap-2' },
        el('span', { class: `badge sev-${f.severity}`, text: f.severity }), el('span', { class: 'font-semibold', text: f.title }),
        f.cwe ? el('a', { class: 'badge', href: `https://cwe.mitre.org/data/definitions/${(f.cwe.match(/\d+/) || [''])[0]}.html`, target: '_blank', rel: 'noopener noreferrer', text: f.cwe }) : '',
        el('span', { class: 'badge', text: f.source === 'static' ? 'rule' : (f.provider || 'AI') })),
      el('a', { class: 'mt-1.5 inline-block font-mono text-xs text-violet-500 hover:underline', href: `/p/${pid}/editor#path=${encodeURIComponent(f.file)}&line=${f.line || 1}`,
        text: `${f.file}${f.line ? `:${f.line}` : ''}` }),
      f.description ? el('p', { class: 'mt-2 text-sm', text: f.description }) : '',
      f.recommendation ? el('div', { class: 'mt-2 rounded-xl bg-[var(--hover)] p-3 text-sm' }, icon('fa-solid fa-wrench text-emerald-500'), ` ${f.recommendation}`) : ''));
  }
}

onEvent((ev) => {
  if (ev.project_id !== pid) return;
  const d = ev.data || {};
  if (ev.type === 'bughunt_progress' && d.run_id === current) setRunning(true, d.done, d.total);
  if (ev.type === 'bughunt_done' && d.run_id === current) {
    setRunning(false);
    toast(d.status === 'completed' ? `Bug hunt finished: ${d.findings} finding(s)` : `Bug hunt ${d.status}${d.error ? `: ${d.error}` : ''}`, d.status === 'completed' ? 'ok' : 'warn');
    showRun(current);
    loadRuns();
  }
});

checkIntegrity();
loadFolders().catch(errorToast);
loadRuns().catch(errorToast);
