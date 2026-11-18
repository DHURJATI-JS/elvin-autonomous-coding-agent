// System status: AI latency every 60 s, gateway PCs, services, fallback policy.
import { $, el, errorToast, get, icon, post, timeAgo, toast } from './core.js';

const Chart = window.Chart;
let snap = null;
const charts = new Map();

function fmtMs(v) { return v == null ? '-' : v >= 1000 ? `${(v / 1000).toFixed(1)} s` : `${v} ms`; }
function grid() { return document.documentElement.classList.contains('dark') ? 'rgba(255,255,255,.06)' : 'rgba(16,22,48,.07)'; }

function spark(canvas, history, key) {
  const old = charts.get(key);
  if (old) old.destroy();
  const pts = history.map((h) => (h.gen_ms ?? h.api_ms));
  charts.set(key, new Chart(canvas, { type: 'line', data: { labels: history.map((h) => new Date(h.t).toLocaleTimeString()), datasets: [{
    data: pts, borderColor: '#8b5cf6', backgroundColor: 'rgba(139,92,246,.15)', fill: true, tension: 0.35, pointRadius: 0, borderWidth: 2, spanGaps: false }] },
    options: { responsive: true, maintainAspectRatio: false, animation: false, plugins: { legend: { display: false }, tooltip: { callbacks: { label: (c) => fmtMs(c.parsed.y) } } },
      scales: { x: { display: false }, y: { beginAtZero: true, grid: { color: grid() }, ticks: { maxTicksLimit: 3, callback: (v) => fmtMs(v) } } } } }));
}

function tile(label, value, sub, cls = '') {
  return el('div', { class: 'stat-tile' }, el('div', { class: 'text-xs font-medium uppercase tracking-wider faint', text: label }),
    el('div', { class: `value ${cls}`, text: value }), el('div', { class: 'text-xs muted', text: sub }));
}

function render() {
  const s = snap;
  $('#interval').textContent = s.interval_s;
  $('#checked').textContent = s.checked_at ? timeAgo(s.checked_at) : 'never';
  $('#cap').textContent = s.effort_cap;
  $('#local-model').textContent = [s.local_gemma_model, s.local_qwen_model].filter(Boolean).join(' + ') || 'not installed';
  const online = s.providers.filter((p) => p.online);
  const gens = online.map((p) => p.gen_ms).filter((v) => v != null);
  const gw = s.gateway || {};
  $('#overall').replaceChildren(
    tile('AI models online', `${online.length}/${s.providers.length}`, 'answering right now', online.length === s.providers.length ? 'text-emerald-500' : 'text-amber-500'),
    gw.configured === false ? tile('Gateway PCs', 'not set up', 'add its address below (optional)', 'text-amber-500')
      : tile('Gateway PCs', gw.pcs_total != null ? `${gw.pcs_online}/${gw.pcs_total}` : (gw.reachable ? 'online' : 'offline'),
        gw.pcs_total != null ? `${gw.slots_busy ?? 0}/${gw.slots_total ?? 0} slots busy` : (gw.error || ''), gw.reachable ? '' : 'text-rose-500'),
    tile('Fastest response', gens.length ? fmtMs(Math.min(...gens)) : '-', '1-token answer'),
    tile('Gemma', s.fallback_active ? 'Local Docker' : 'Gateway', s.fallback_active
      ? `${s.gemma_source === 'local' ? 'chosen on this page' : 'gateway is down'} · effort ≤ ${s.effort_cap}` : 'gateway is serving', s.fallback_active ? 'text-amber-500' : 'text-emerald-500'));
  const note = $('#fallback-note');
  note.classList.toggle('hidden', !s.fallback_active);
  if (s.fallback_active) {
    const local = s.gemma_source === 'local';
    note.replaceChildren(el('div', { class: 'glass flex items-start gap-3 border-amber-500/40 p-4 text-sm' }, icon('fa-solid fa-triangle-exclamation mt-0.5 text-lg text-amber-500'),
      el('div', {}, el('div', { class: 'font-semibold', text: local
        ? 'Elvin is using the local Gemma 3 in Docker for now - the gateway is switched off below.'
        : s.gateway_configured === false ? 'No Gemma gateway is set up - Elvin is using the local Gemma 3 in Docker.'
          : 'The Gemma gateway is not answering - Elvin is using the local Gemma 3 in Docker.' }),
      el('div', { class: 'muted', text: `It runs on this computer's CPU, so answers are slower and effort is reduced to ${s.effort_cap} while it is in use.${
        s.local_online ? '' : ' The local model is not answering yet - if it was just installed it may still be downloading.'}${
        local ? ' Switch back to "Gateway first" once the gateway works again.' : ''}` }))));
  }
  renderSource(s);

  const box = $('#providers');
  box.replaceChildren();
  for (const p of s.providers) {
    const canvas = el('canvas');
    box.append(el('div', { class: `rounded-2xl border p-4 ${p.online ? 'border-[var(--border)]' : 'border-rose-500/40'}` },
      el('div', { class: 'flex items-start justify-between gap-2' },
        el('div', { class: 'min-w-0' }, el('div', { class: 'flex items-center gap-2 font-semibold' }, el('span', { class: `dot ${p.online ? 'dot-ok' : 'dot-bad'}` }), p.label),
          el('div', { class: 'truncate font-mono text-xs muted', text: p.model })),
        el('div', { class: 'flex flex-wrap justify-end gap-1' },
          p.key === 'gemma' && s.gemma_source === 'local' ? el('span', { class: 'badge', text: 'not used' }) : '',
          p.fallback ? el('span', { class: 'badge badge-warn', text: p.online && (p.key === 'gemma_local' ? s.fallback_active
            : !s.providers.some((x) => x.key === 'qwen' && x.online)) ? 'in use · fallback' : 'fallback' }) : '',
          p.local ? el('span', { class: 'badge', text: 'this computer' }) : '', p.circuit_open ? el('span', { class: 'badge badge-bad', text: 'paused' }) : '')),
      el('div', { class: 'mt-3 grid grid-cols-2 gap-2 text-sm' },
        el('div', {}, el('div', { class: 'text-[11px] uppercase tracking-wider faint', text: 'Response' }), el('div', { class: 'text-lg font-semibold', text: p.gen_ms != null ? fmtMs(p.gen_ms) : (p.local || p.key === 'gemma' ? '-' : 'n/a') })),
        el('div', {}, el('div', { class: 'text-[11px] uppercase tracking-wider faint', text: 'API round-trip' }), el('div', { class: 'text-lg font-semibold', text: fmtMs(p.api_ms) }))),
      el('div', { class: 'mt-1 truncate text-xs muted', text: p.detail }),
      el('div', { class: 'mt-3 h-16' }, canvas)));
    spark(canvas, p.history || [], p.key);
  }
  // gateway
  $('#gw-badge').replaceChildren(gw.configured === false ? el('span', { class: 'badge', text: 'not set up' })
    : el('span', { class: `badge ${gw.reachable ? 'badge-ok' : 'badge-bad'}`, text: gw.reachable ? `online · ${fmtMs(gw.latency_ms)}` : 'unreachable' }));
  const pcs = gw.pcs || [];
  $('#gateway').replaceChildren(
    el('div', { class: 'grid grid-cols-2 gap-2 text-sm sm:grid-cols-4' },
      ...[['Address', snap.gateway_url || gw.url || '-'], ['PCs online', gw.pcs_total != null ? `${gw.pcs_online} of ${gw.pcs_total}` : '-'],
        ['Busy slots', gw.slots_total ? `${gw.slots_busy}/${gw.slots_total}` : '-'], ['Host CPU', gw.host_cpu_percent != null ? `${gw.host_cpu_percent}%` : '-']]
        .map(([k, v]) => el('div', { class: 'min-w-0 rounded-xl border border-[var(--border)] p-3' }, el('div', { class: 'text-[11px] uppercase tracking-wider faint', text: k }), el('div', { class: 'truncate font-medium', title: v, text: v })))),
    pcs.length ? el('div', { class: 'mt-3 overflow-x-auto' }, el('table', { class: 'table' }, el('thead', {}, el('tr', {}, ...['PC', 'Address', 'Status', 'Slots', 'Load'].map((h) => el('th', { text: h })))),
      el('tbody', {}, ...pcs.map((pc) => el('tr', {}, el('td', { class: 'font-medium' }, icon('fa-solid fa-desktop text-cyan-500'), ` ${pc.name}`), el('td', { class: 'font-mono text-xs', text: pc.address }),
        el('td', {}, el('span', { class: `badge ${pc.online ? 'badge-ok' : 'badge-bad'}`, text: pc.online ? 'online' : 'offline' })),
        el('td', { text: `${pc.busy_slots}/${pc.total_slots}` }), el('td', {}, el('span', { class: `badge ${pc.overloaded ? 'badge-warn' : ''}`, text: pc.overloaded ? 'overloaded' : 'normal' }))))))) :
      el('p', { class: 'mt-3 text-sm muted', text: gw.configured === false ? 'No Gemma gateway is set up - Elvin uses the local Gemma in Docker. If you have one, the administrator can add its address below.'
        : gw.error ? `The gateway can't be reached (${gw.error}). Check that the gateway PC is on and its address is right.` : (gw.note || 'No per-PC details.') }));
  const hist = gw.history || [];
  const old = charts.get('gw');
  if (old) old.destroy();
  charts.set('gw', new Chart($('#gw-chart'), { type: 'bar', data: { labels: hist.map((h) => new Date(h.t).toLocaleTimeString()),
    datasets: [{ label: 'Gateway round-trip', data: hist.map((h) => h.api_ms), backgroundColor: hist.map((h) => h.ok ? 'rgba(34,211,238,.7)' : 'rgba(244,63,94,.7)'), borderRadius: 4 }] },
    options: { responsive: true, maintainAspectRatio: false, animation: false, plugins: { legend: { display: false } },
      scales: { x: { display: false }, y: { beginAtZero: true, grid: { color: grid() }, ticks: { callback: (v) => fmtMs(v), maxTicksLimit: 4 } } } } }));
  // services
  const names = { postgres: 'PostgreSQL', redis: 'Redis', clamav: 'ClamAV virus scanner', runner: 'Sandbox runner' };
  $('#services').replaceChildren(...Object.entries(s.services || {}).map(([k, v]) => el('div', { class: 'flex items-center gap-3 rounded-xl px-2 py-1.5 hover:bg-[var(--hover)]' },
    el('span', { class: `dot ${v.online ? 'dot-ok' : 'dot-bad'}` }), el('span', { class: 'flex-1', text: names[k] || k }),
    el('span', { class: 'truncate text-xs muted', text: v.detail }), el('span', { class: 'w-16 text-right font-mono text-xs', text: fmtMs(v.latency_ms) }))));
  const form = $('#gw-form');
  form.classList.toggle('hidden', !s.is_admin);
  if (s.is_admin && !$('#gw-url').value) $('#gw-url').value = s.gateway_url || '';
}

function renderSource(s) {
  for (const b of $('#gemma-source').querySelectorAll('button')) {
    b.setAttribute('aria-pressed', String(b.dataset.src === s.gemma_source));
    b.disabled = !s.is_admin || (b.dataset.src === 'local' && !s.local_gemma_model);
  }
  $('#gemma-source-admin').classList.toggle('hidden', !s.is_admin);
  $('#gemma-source-help').textContent = (s.gemma_source === 'local'
    ? 'Local Docker only: the gateway is not used, even when it is online. Good while the gateway has trouble.'
    : 'Gateway first: the gateway answers; the local Gemma in Docker steps in automatically whenever it is down.')
    + (s.local_gemma_model ? '' : ' The local Docker Gemma is not installed.')
    + (s.is_admin ? '' : ' Only the administrator can change this.');
}

$('#gemma-source').addEventListener('click', async (e) => {
  const b = e.target.closest('button[data-src]');
  if (!b || b.disabled || !snap || b.dataset.src === snap.gemma_source) return;
  try {
    snap = await post('/api/system/gemma-source', { source: b.dataset.src });
    render();
    toast(snap.gemma_source === 'local' ? `Elvin now uses the local Gemma in Docker - effort capped at ${snap.effort_cap}.` : 'Elvin uses the gateway first again.', 'ok', 4000);
  } catch (err) { errorToast(err); }
});

async function load(force = false) {
  try {
    snap = force ? await post('/api/system/monitor/refresh') : await get('/api/system/monitor');
    render();
  } catch (err) { errorToast(err); }
}
async function platform() {
  try {
    const s = await get('/api/system/status');
    const p = s.platform;
    const row = (k, v) => el('div', { class: 'flex justify-between gap-3' }, el('span', { class: 'muted', text: k }), el('span', { class: 'font-medium', text: v }));
    $('#platform').replaceChildren(row('System', `${p.os} · ${p.arch}`), row('GPU', `${p.gpu_name || p.gpu}`), row('Memory', `${p.ram_gb} GB`),
      row('Local Qwen', p.local_backend === 'none' ? 'not available - cloud models' : `${p.qwen_model} via ${p.local_backend}`),
      row('Can run local models', p.compatible ? 'yes' : 'no - Claude/ChatGPT key required'));
  } catch { /* ignore */ }
}

$('#refresh').addEventListener('click', async (e) => {
  const b = e.currentTarget;
  b.disabled = true;
  b.querySelector('i').classList.add('fa-spin');
  await load(true);
  b.disabled = false;
  b.querySelector('i').classList.remove('fa-spin');
  toast('Checked every model just now', 'ok', 2000);
});
$('#gw-form').addEventListener('submit', async (e) => {
  e.preventDefault();
  try {
    snap = await post('/api/system/gateway', { url: $('#gw-url').value.trim(), api_key: $('#gw-key').value.trim() || null });
    $('#gw-key').value = '';
    render();
    toast(snap.gateway.reachable ? 'Gateway saved and reachable' : 'Saved, but the gateway is still not answering', snap.gateway.reachable ? 'ok' : 'warn');
  } catch (err) { errorToast(err); }
});
setInterval(() => {
  if (!snap || !snap.next_check_at) return;
  const left = Math.max(0, Math.round(snap.next_check_at - Date.now() / 1000));
  $('#countdown').textContent = `${left}s`;
  $('#checked').textContent = snap.checked_at ? timeAgo(snap.checked_at) : 'never';
  if (left === 0 && !document.hidden) { snap.next_check_at = Date.now() / 1000 + 5; setTimeout(() => load(), 3000); }
}, 1000);
window.addEventListener('elvin:theme', () => snap && render());
load();
platform();
