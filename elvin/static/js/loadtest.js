// Locust load testing: per-page weights, live Chart.js graphs, history, CSV export.
import { $, el, errorToast, fmtNum, get, icon, onEvent, post, projectId, del, timeAgo, toast } from './core.js';
import { checkIntegrity } from './integrity.js';

const pid = projectId();
const Chart = window.Chart;
let configs = [];
let currentRun = null;
let liveRun = null;
const charts = {};

function palette() {
  const dark = document.documentElement.classList.contains('dark');
  return { grid: dark ? 'rgba(255,255,255,.06)' : 'rgba(16,22,48,.07)', text: dark ? '#8e97b0' : '#5a6378',
    violet: '#8b5cf6', cyan: '#22d3ee', pink: '#f472b6', amber: '#f59e0b', rose: '#f43f5e', emerald: '#10b981' };
}
function makeCharts() {
  const p = palette();
  Chart.defaults.font.family = getComputedStyle(document.body).fontFamily;
  Chart.defaults.color = p.text;
  const common = { responsive: true, maintainAspectRatio: false, animation: { duration: 300 }, interaction: { mode: 'index', intersect: false },
    plugins: { legend: { labels: { boxWidth: 10, usePointStyle: true } } },
    scales: { x: { grid: { color: p.grid }, ticks: { maxTicksLimit: 8 } }, y: { grid: { color: p.grid }, beginAtZero: true } } };
  for (const c of Object.values(charts)) c.destroy();
  charts.rps = new Chart($('#chart-rps'), { type: 'line', data: { labels: [], datasets: [
    { label: 'Requests/s', data: [], borderColor: p.violet, backgroundColor: 'rgba(139,92,246,.15)', fill: true, tension: 0.35, pointRadius: 0, borderWidth: 2 },
    { label: 'Failures/s', data: [], borderColor: p.rose, backgroundColor: 'rgba(244,63,94,.1)', fill: true, tension: 0.35, pointRadius: 0, borderWidth: 2 },
    { label: 'Users', data: [], borderColor: p.cyan, borderDash: [5, 4], tension: 0.2, pointRadius: 0, borderWidth: 1.5, yAxisID: 'y2' }] },
    options: { ...common, scales: { ...common.scales, y2: { position: 'right', grid: { drawOnChartArea: false }, beginAtZero: true } } } });
  charts.lat = new Chart($('#chart-lat'), { type: 'line', data: { labels: [], datasets: [
    { label: 'p50', data: [], borderColor: p.emerald, tension: 0.35, pointRadius: 0, borderWidth: 2 },
    { label: 'p95', data: [], borderColor: p.amber, tension: 0.35, pointRadius: 0, borderWidth: 2 },
    { label: 'avg', data: [], borderColor: p.pink, tension: 0.35, pointRadius: 0, borderWidth: 1.5, borderDash: [4, 3] }] }, options: common });
  charts.ep = new Chart($('#chart-ep'), { type: 'bar', data: { labels: [], datasets: [
    { label: 'avg ms', data: [], backgroundColor: 'rgba(139,92,246,.75)', borderRadius: 6, maxBarThickness: 56 },
    { label: 'p95 ms', data: [], backgroundColor: 'rgba(34,211,238,.75)', borderRadius: 6, maxBarThickness: 56 }] },
    options: { ...common, animation: false, interaction: { mode: 'index', intersect: false },
      datasets: { bar: { categoryPercentage: 0.6, barPercentage: 0.9 } } } });
}
function pushSample(s) {
  const label = `${Math.round(s.t)}s`;
  charts.rps.data.labels.push(label); charts.lat.data.labels.push(label);
  charts.rps.data.datasets[0].data.push(s.rps); charts.rps.data.datasets[1].data.push(s.fail_rps); charts.rps.data.datasets[2].data.push(s.users);
  charts.lat.data.datasets[0].data.push(s.p50); charts.lat.data.datasets[1].data.push(s.p95); charts.lat.data.datasets[2].data.push(s.avg);
}
function resetSeries() {
  for (const c of [charts.rps, charts.lat]) { c.data.labels = []; c.data.datasets.forEach((d) => { d.data = []; }); }
}

// ------------------------------------------------------------------ form
function endpointRow(ep = { method: 'GET', path: '/', weight: 1 }) {
  const method = el('select', { class: 'input !w-24 !px-2 !py-1.5 !text-xs' }, ...['GET', 'POST', 'PUT', 'PATCH', 'DELETE', 'HEAD'].map((m) => el('option', { value: m, text: m, selected: m === ep.method })));
  const path = el('input', { class: 'input !py-1.5 !text-xs', value: ep.path, placeholder: '/path', maxlength: 500 });
  const weight = el('input', { class: 'input !w-16 !px-2 !py-1.5 !text-xs', type: 'number', min: 1, max: 100, value: ep.weight, title: 'Weight' });
  const body = el('textarea', { class: 'input mt-1.5 hidden !py-1.5 font-mono !text-xs', rows: 2, placeholder: '{"json": "body"}' });
  if (ep.json !== undefined) { body.value = typeof ep.json === 'string' ? ep.json : JSON.stringify(ep.json); body.classList.remove('hidden'); }
  const row = el('div', { class: 'rounded-xl border border-[var(--border)] p-2', dataset: { ep: '1' } },
    el('div', { class: 'flex items-center gap-1.5' }, method, path, weight,
      el('button', { type: 'button', class: 'btn-icon !h-8 !w-8 text-xs', title: 'JSON body', onclick: () => body.classList.toggle('hidden') }, icon('fa-solid fa-code')),
      el('button', { type: 'button', class: 'btn-icon !h-8 !w-8 text-xs text-rose-500', title: 'Remove', onclick: () => row.remove() }, icon('fa-solid fa-xmark'))), body);
  row.read = () => ({ method: method.value, path: path.value.trim(), weight: Number(weight.value) || 1, ...(body.value.trim() ? { json: body.value.trim() } : {}) });
  return row;
}
$('#add-ep').addEventListener('click', () => $('#endpoints').append(endpointRow({ method: 'GET', path: '/', weight: 1 })));

function fill(c) {
  $('#lt-name').value = c.name; $('#lt-host').value = c.host; $('#lt-users').value = c.users; $('#lt-rate').value = c.spawn_rate; $('#lt-dur').value = c.duration_s;
  $('#endpoints').replaceChildren(...(c.endpoints.length ? c.endpoints : [{ method: 'GET', path: '/', weight: 1 }]).map(endpointRow));
}
async function loadConfigs() {
  const r = await get(`/api/projects/${pid}/loadtest/configs`);
  configs = r.configs;
  const sel = $('#saved');
  sel.replaceChildren(el('option', { value: '', text: configs.length ? 'Saved configs...' : 'No saved configs' }), ...configs.map((c) => el('option', { value: c.id, text: c.name })));
  if (configs.length && !$('#lt-host').value) fill(configs[0]);
  if (!$('#endpoints').children.length) $('#endpoints').append(endpointRow());
}
$('#saved').addEventListener('change', (e) => { const c = configs.find((x) => x.id === e.target.value); if (c) fill(c); });

$('#lt-form').addEventListener('submit', async (e) => {
  e.preventDefault();
  const endpoints = [...document.querySelectorAll('[data-ep]')].map((r) => r.read());
  const body = { name: $('#lt-name').value.trim() || 'Default', host: $('#lt-host').value.trim(), users: Number($('#lt-users').value),
    spawn_rate: Number($('#lt-rate').value), duration_s: Number($('#lt-dur').value), endpoints, save: true };
  $('#run').disabled = true;
  try {
    const r = await post(`/api/projects/${pid}/loadtest/run`, body);
    if (r.note) toast(r.note, 'info', 6000);
    toast('Locust is running...', 'ok', 2500);
    startLive(r.run_id);
    loadConfigs(); loadRuns();
  } catch (err) { errorToast(err); $('#run').disabled = false; }
});
$('#stop').addEventListener('click', async () => { if (liveRun) { try { await post(`/api/loadtest/${liveRun}/stop`); toast('Stopping - Locust is writing its final results.', 'info'); } catch (err) { errorToast(err); } } });

function startLive(runId) {
  liveRun = runId; currentRun = runId;
  resetSeries();
  charts.rps.update(); charts.lat.update();
  $('#live-badge').classList.remove('hidden');
  $('#stop').classList.remove('hidden');
  $('#run').classList.add('hidden');
  $('#run-title').textContent = 'Live results';
  $('#run-sub').textContent = 'Locust is ramping up...';
  $('#export').classList.add('hidden');
}
function stopLive() {
  liveRun = null;
  $('#live-badge').classList.add('hidden');
  $('#stop').classList.add('hidden');
  $('#run').classList.remove('hidden');
  $('#run').disabled = false;
}

// ------------------------------------------------------------------ results
function tile(label, value, cls = '') { return el('div', { class: 'rounded-xl border border-[var(--border)] p-3' }, el('div', { class: 'text-[11px] uppercase tracking-wider faint', text: label }), el('div', { class: `mt-0.5 text-lg font-semibold ${cls}`, text: value })); }
async function showRun(id) {
  currentRun = id;
  const r = await get(`/api/loadtest/${id}`);
  const run = r.run;
  $('#run-title').textContent = `${run.config.host} · ${run.config.users} users · ${run.config.duration_s}s`;
  $('#run-sub').textContent = `${run.status} · ${new Date(run.started_at).toLocaleString()}${run.error ? ` · ${run.error.split('\n')[0]}` : ''}`;
  const failPct = run.total_requests ? (100 * run.total_failures) / run.total_requests : 0;
  $('#tiles').replaceChildren(tile('Requests', fmtNum(run.total_requests)), tile('Failures', `${fmtNum(run.total_failures)} (${failPct.toFixed(1)}%)`, failPct > 1 ? 'text-rose-500' : ''),
    tile('Req/s', run.rps.toFixed(1)), tile('Avg', `${Math.round(run.avg_ms)} ms`), tile('p95', `${Math.round(run.p95_ms)} ms`), tile('p99', `${Math.round(run.p99_ms)} ms`));
  if (run.status !== 'running') {
    resetSeries();
    for (const s of r.samples) pushSample(s);
    charts.rps.update(); charts.lat.update();
    $('#export').classList.remove('hidden');
    $('#export').href = `/api/loadtest/${id}/export.csv`;
  }
  charts.ep.data.labels = r.endpoints.map((e) => e.name);
  charts.ep.data.datasets[0].data = r.endpoints.map((e) => e.avg);
  charts.ep.data.datasets[1].data = r.endpoints.map((e) => e.p95);
  charts.ep.update();
  $('#ep-table tbody').replaceChildren(...r.endpoints.map((e) => el('tr', {},
    el('td', {}, el('span', { class: 'badge mr-1.5', text: e.method }), el('span', { class: 'font-mono text-xs', text: e.name.startsWith(`${e.method} `) ? e.name.slice(e.method.length + 1) : e.name })),
    el('td', { text: fmtNum(e.requests) }), el('td', { class: e.failures ? 'text-rose-500' : '', text: fmtNum(e.failures) }),
    el('td', { text: Math.round(e.median) }), el('td', { text: Math.round(e.avg) }), el('td', { text: Math.round(e.p95) }), el('td', { text: Math.round(e.p99) }),
    el('td', { text: Math.round(e.max) }), el('td', { text: e.rps.toFixed(1) }))));
  const fails = (run.summary && run.summary.failures) || [];
  $('#failures').replaceChildren(...(fails.length ? [el('div', { class: 'text-xs font-semibold uppercase tracking-wider faint', text: 'Failures' }),
    ...fails.map((f) => el('div', { class: 'mt-1.5 text-sm' }, el('span', { class: 'badge badge-bad mr-2', text: `${f.occurrences}×` }), el('span', { class: 'font-mono text-xs', text: `${f.method} ${f.name}: ${f.error}` })))] : []));
}
async function loadRuns() {
  const r = await get(`/api/projects/${pid}/loadtest/runs`);
  $('#runs').replaceChildren(...r.runs.map((run) => el('button', { class: 'menu-item !px-2', onclick: () => showRun(run.id) },
    icon(`fa-solid ${run.status === 'running' ? 'fa-spinner fa-spin text-violet-500' : run.status === 'completed' ? 'fa-circle-check text-emerald-500' : run.status === 'stopped' ? 'fa-circle-stop text-amber-500' : 'fa-circle-xmark text-rose-500'}`),
    el('span', { class: 'flex-1 truncate', text: `${run.config.users} users · ${run.config.host}` }),
    el('span', { class: 'text-xs faint', text: `${run.rps ? run.rps.toFixed(0) + ' rps · ' : ''}${timeAgo(run.started_at)}` }))));
  if (!r.runs.length) $('#runs').append(el('p', { class: 'text-xs muted', text: 'No runs yet.' }));
  const running = r.runs.find((x) => x.status === 'running');
  if (running && !liveRun) { startLive(running.id); }
  else if (!currentRun && r.runs.length) showRun(r.runs[0].id);
}

onEvent((ev) => {
  if (ev.project_id !== pid) return;
  const d = ev.data || {};
  if (ev.type === 'loadtest_started' && !liveRun) startLive(d.run_id);
  if (ev.type === 'loadtest_sample' && d.run_id === liveRun) {
    pushSample(d);
    charts.rps.update('none'); charts.lat.update('none');
    $('#run-sub').textContent = `Running · ${d.users} users · ${d.rps} req/s · ${fmtNum(d.total_requests)} requests · ${fmtNum(d.total_failures)} failures`;
  }
  if (ev.type === 'loadtest_done' && d.run_id === liveRun) {
    stopLive();
    toast(`Load test ${d.status}`, d.status === 'completed' ? 'ok' : 'warn');
    showRun(d.run_id); loadRuns();
  }
});
window.addEventListener('elvin:theme', () => { makeCharts(); if (currentRun) showRun(currentRun); });
void del;

makeCharts();
checkIntegrity();
loadConfigs().catch(errorToast);
loadRuns().catch(errorToast);
