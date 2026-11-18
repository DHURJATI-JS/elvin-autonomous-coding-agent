import { $, el, errorToast, fmtNum, get, icon, post, toast } from './core.js';
import { renderAiSettings } from './ai-settings.js';

const Chart = window.Chart;
let days = 30;
const charts = {};
const FAMILY = { gemma: 'Gemma', qwen: 'Qwen', claude_opus: 'Claude', claude_haiku: 'Claude', chatgpt: 'ChatGPT', chatgpt_mini: 'ChatGPT' };
const COLORS = { Gemma: '#8b5cf6', Qwen: '#22d3ee', Claude: '#f59e0b', ChatGPT: '#10b981', Other: '#94a3b8' };

function theme() {
  const dark = document.documentElement.classList.contains('dark');
  Chart.defaults.color = dark ? '#8e97b0' : '#5a6378';
  return dark ? 'rgba(255,255,255,.06)' : 'rgba(16,22,48,.07)';
}

async function load() {
  let u;
  try { u = await get(`/api/usage?days=${days}`); } catch (err) { errorToast(err); return; }
  const ok = u.models.reduce((a, m) => a + m.ok_calls, 0);
  const tile = (label, value, sub) => el('div', { class: 'stat-tile' }, el('div', { class: 'text-xs font-medium uppercase tracking-wider faint', text: label }),
    el('div', { class: 'value', text: value }), el('div', { class: 'text-xs muted', text: sub }));
  $('#u-tiles').replaceChildren(tile('AI calls', fmtNum(u.totals.calls), `last ${u.days} days`), tile('Tokens', fmtNum(u.totals.tokens), 'input + output'),
    tile('Estimated cost', `$${u.totals.est_cost_usd.toFixed(2)}`, 'cloud models only'), tile('Success rate', u.totals.calls ? `${((100 * ok) / u.totals.calls).toFixed(1)}%` : '-', 'calls that returned usable output'));
  $('#price-note').textContent = u.prices_note;
  $('#m-table tbody').replaceChildren(...u.models.map((m) => el('tr', {},
    el('td', {}, el('div', { class: 'font-medium', text: m.model || m.provider }), el('div', { class: 'text-xs muted', text: `${FAMILY[m.provider] || m.provider}${m.local ? ' · local' : ''}` })),
    el('td', { text: fmtNum(m.calls) }), el('td', { class: m.success_rate < 80 ? 'text-rose-500' : '', text: `${m.success_rate}%` }),
    el('td', { text: `${m.avg_seconds}s` }), el('td', { class: 'text-xs', text: `${fmtNum(m.input_tokens)} / ${fmtNum(m.output_tokens)}` }),
    el('td', { text: m.local ? 'free' : `$${m.est_cost_usd.toFixed(4)}` }))));
  if (!u.models.length) $('#m-table tbody').append(el('tr', {}, el('td', { colspan: 6, class: 'muted', text: 'No AI calls in this period.' })));
  $('#e-table tbody').replaceChildren(...u.efforts.map((e) => el('tr', {}, el('td', { class: 'font-medium', text: e.effort }), el('td', { text: fmtNum(e.calls) }),
    el('td', { text: `${e.success_rate}%` }), el('td', { text: `${e.avg_seconds}s` }))));

  const grid = theme();
  const dayLabels = [...new Set(u.daily.map((d) => d.day))];
  const fams = [...new Set(u.daily.map((d) => FAMILY[d.provider] || 'Other'))];
  charts.daily?.destroy();
  charts.daily = new Chart($('#c-daily'), { type: 'bar', data: { labels: dayLabels, datasets: fams.map((f) => ({ label: f, backgroundColor: COLORS[f] || COLORS.Other, borderRadius: 4,
    data: dayLabels.map((day) => u.daily.filter((d) => d.day === day && (FAMILY[d.provider] || 'Other') === f).reduce((a, d) => a + d.calls, 0)) })) },
    options: { responsive: true, maintainAspectRatio: false, plugins: { legend: { labels: { usePointStyle: true, boxWidth: 8 } } },
      scales: { x: { stacked: true, grid: { color: grid } }, y: { stacked: true, beginAtZero: true, grid: { color: grid } } } } });
  charts.feature?.destroy();
  charts.feature = new Chart($('#c-feature'), { type: 'doughnut', data: { labels: u.features.map((f) => f.feature), datasets: [{ data: u.features.map((f) => f.calls),
    backgroundColor: ['#8b5cf6', '#22d3ee', '#f472b6', '#f59e0b', '#10b981', '#6366f1', '#ef4444', '#94a3b8'], borderWidth: 0 }] },
    options: { responsive: true, maintainAspectRatio: false, cutout: '62%', plugins: { legend: { position: 'right', labels: { usePointStyle: true, boxWidth: 8 } } } } });

  const recs = $('#recs');
  recs.replaceChildren(...u.recommendations.map((r) => el('div', { class: 'rounded-xl border border-[var(--border)] p-3' },
    el('div', { class: 'flex items-start gap-2' }, icon(`fa-solid ${r.level === 'warn' ? 'fa-triangle-exclamation text-amber-500' : 'fa-circle-info text-violet-500'} mt-0.5`),
      el('div', { class: 'flex-1' }, el('div', { class: 'text-sm font-medium', text: r.title }), el('div', { class: 'text-xs muted', text: r.detail }))),
    (r.action_effort || r.action_team) ? el('div', { class: 'mt-2 flex justify-end' }, el('button', { class: 'btn btn-ghost btn-sm', onclick: async () => {
      try { await post('/api/settings', r.action_effort ? { ai_effort: r.action_effort } : { ai_team: r.action_team }); toast('Applied', 'ok'); load(); renderAiSettings($('#ai-settings'), load); }
      catch (err) { errorToast(err); }
    } }, icon('fa-solid fa-check'), r.action_effort ? `Use ${r.action_effort} effort` : `Switch to ${r.action_team}`)) : '')));
}

for (const b of document.querySelectorAll('#days button')) b.addEventListener('click', () => {
  days = Number(b.dataset.d);
  for (const x of document.querySelectorAll('#days button')) x.setAttribute('aria-pressed', String(x === b));
  load();
});
window.addEventListener('elvin:theme', load);
renderAiSettings($('#ai-settings'), load);
load();
