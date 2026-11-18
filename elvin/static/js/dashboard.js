import { $, el, errorToast, fmtBytes, fmtNum, get, icon, onEvent, post, timeAgo, toast } from './core.js';
import { connectGithub } from './github.js';

const hour = new Date().getHours();
$('#greeting-time').textContent = hour < 5 ? 'Late night' : hour < 12 ? 'Good morning' : hour < 18 ? 'Good afternoon' : 'Good evening';

const levelBadge = { required: 'badge-bad', recommended: 'badge-warn', info: 'badge-info' };

function renderSuggestions(list) {
  const box = $('#suggestions');
  box.replaceChildren();
  if (!list.length) { box.append(el('p', { class: 'text-sm muted' }, icon('fa-solid fa-circle-check text-emerald-500'), ' All good - nothing needs your attention.')); return; }
  for (const s of list) {
    const row = el('div', { class: 'flex items-start gap-3 rounded-xl border border-[var(--border)] p-3' },
      el('span', { class: `badge ${levelBadge[s.level] || 'badge-info'} mt-0.5`, text: s.level }),
      el('div', { class: 'min-w-0 flex-1' }, el('div', { class: 'text-sm font-medium', text: s.title }), s.detail ? el('div', { class: 'mt-0.5 text-xs muted', text: s.detail }) : ''));
    if (s.action === 'connect') row.append(el('button', { class: 'btn btn-primary btn-sm', onclick: connectGithub }, 'Connect'));
    else if (s.action && s.action.startsWith('/')) row.append(el('a', { class: 'btn btn-ghost btn-sm', href: s.action }, 'Open'));
    else if (s.action && s.action.startsWith('https://')) row.append(el('a', { class: 'btn btn-ghost btn-sm', href: s.action, target: '_blank', rel: 'noopener noreferrer' }, 'View'));
    box.append(row);
  }
}

function renderGithub(stats) {
  const body = $('#gh-body');
  const d = stats.data || {};
  $('#gh-fetched').textContent = stats.fetched_at ? `Collected with the GitHub CLI · updated ${timeAgo(stats.fetched_at)}` : 'Collected with the GitHub CLI';
  renderSuggestions(stats.suggestions || []);
  if (!d.profile) {
    body.replaceChildren(el('div', { class: 'flex flex-col items-center gap-3 rounded-2xl border border-dashed border-[var(--border-strong)] p-6 text-center' },
      icon('fa-brands fa-github text-4xl opacity-70'),
      el('p', { class: 'text-sm muted', text: stats.error && stats.error !== 'auth' ? `GitHub stats unavailable: ${stats.error}` : 'Connect GitHub to see your repositories, contributions, pull requests and notifications here.' }),
      el('button', { class: 'btn btn-primary', onclick: connectGithub }, icon('fa-brands fa-github'), 'Connect GitHub')));
    return;
  }
  const p = d.profile;
  const chip = (ic, label, value) => el('div', { class: 'min-w-0 rounded-xl border border-[var(--border)] px-3 py-2' },
    el('div', { class: 'truncate text-[11px] uppercase tracking-wider faint' }, icon(ic), ' ', label), el('div', { class: 'mt-0.5 text-lg font-semibold', text: value ?? '-' }));
  const langs = el('div', { class: 'flex min-w-0 flex-wrap gap-1.5' }, ...(d.languages || []).map(([l, n]) => el('span', { class: 'badge', text: `${l} · ${n}` })));
  const repos = el('div', { class: 'min-w-0 divide-y divide-[var(--border)]' }, ...(d.recent || []).slice(0, 6).map((r) => el('a', {
    class: 'flex min-w-0 items-center gap-3 py-2.5 hover:opacity-90', href: r.url, target: '_blank', rel: 'noopener noreferrer' },
    icon(`fa-solid ${r.private ? 'fa-lock' : 'fa-book-bookmark'} text-violet-500`),
    el('div', { class: 'min-w-0 flex-1' }, el('div', { class: 'truncate text-sm font-medium', text: r.full_name }),
      el('div', { class: 'truncate text-xs muted', text: r.description || 'No description' })),
    el('div', { class: 'hidden shrink-0 items-center gap-3 text-xs muted sm:flex' }, r.language ? el('span', { text: r.language }) : '',
      el('span', {}, icon('fa-regular fa-star'), ` ${r.stars}`), el('span', { text: timeAgo(r.pushed_at) })))));
  body.replaceChildren(
    el('div', { class: 'flex min-w-0 flex-wrap items-center justify-between gap-3' },
      el('a', { class: 'flex min-w-0 items-center gap-3', href: p.url, target: '_blank', rel: 'noopener noreferrer' },
        el('span', { class: 'flex h-11 w-11 items-center justify-center rounded-2xl bg-gradient-to-br from-violet-500 to-cyan-500 text-lg font-semibold text-white', text: (p.login || '?')[0].toUpperCase() }),
        el('div', {}, el('div', { class: 'font-semibold', text: p.name || p.login }), el('div', { class: 'text-xs muted', text: `@${p.login} · ${fmtNum(p.followers)} followers` }))),
      langs),
    el('div', { class: 'mt-4 grid grid-cols-2 gap-2 sm:grid-cols-4 xl:grid-cols-2 2xl:grid-cols-4' },
      chip('fa-solid fa-book', 'Repos', fmtNum((p.public_repos || 0) + (p.private_repos || 0))), chip('fa-regular fa-star', 'Stars', fmtNum(d.totals?.stars)),
      chip('fa-solid fa-fire', 'Contributions', d.contributions != null ? fmtNum(d.contributions) : '-'), chip('fa-solid fa-code-pull-request', 'Open PRs', d.open_prs ?? '-'),
      chip('fa-solid fa-code-fork', 'Forks', fmtNum(d.totals?.forks)), chip('fa-solid fa-circle-dot', 'Open issues', fmtNum(d.totals?.open_issues)),
      chip('fa-solid fa-eye', 'Review requests', d.review_requests ?? '-'), chip('fa-regular fa-bell', 'Notifications', d.notifications ?? '-')),
    el('div', { class: 'mt-4 text-xs font-semibold uppercase tracking-wider faint', text: 'Recently pushed' }), repos,
    el('a', { class: 'btn btn-ghost btn-sm mt-3 w-full', href: '/github' }, icon('fa-brands fa-github'),
      `View all ${fmtNum((d.repos || []).length || d.totals?.all_repos || 0)} repositories`));
}

async function loadGithub() {
  try { renderGithub(await get('/api/github/stats')); }
  catch (err) { $('#gh-body').replaceChildren(el('p', { class: 'text-sm muted', text: err.message })); }
}
$('#gh-refresh').addEventListener('click', async (e) => {
  const btn = e.currentTarget;
  btn.disabled = true;
  btn.querySelector('i').classList.add('fa-spin');
  try { renderGithub(await post('/api/github/stats/refresh')); toast('GitHub stats refreshed', 'ok', 2500); }
  catch (err) { errorToast(err); }
  finally { btn.disabled = false; btn.querySelector('i').classList.remove('fa-spin'); }
});

async function loadProjects() {
  try {
    const { projects, limit } = await get('/api/projects');
    const active = projects.filter((p) => !p.archived);
    $('#st-projects').textContent = active.length;
    $('#st-projects-sub').textContent = `${projects.length - active.length} archived · limit ${limit}`;
    const box = $('#recent-projects');
    box.replaceChildren();
    if (!active.length) {
      box.append(el('div', { class: 'col-span-full rounded-2xl border border-dashed border-[var(--border-strong)] p-6 text-center text-sm muted' },
        'No projects yet. ', el('a', { class: 'text-violet-500 underline', href: '/projects?new=1', text: 'Create your first one' }), '.'));
    }
    for (const p of active.slice(0, 6)) {
      box.append(el('a', { class: 'glass card-hover block p-4', href: `/p/${p.id}/elvin` },
        el('div', { class: 'flex items-center justify-between gap-2' }, el('div', { class: 'truncate font-semibold', text: p.name }),
          p.integrity_ok ? el('span', { class: 'badge badge-ok', text: 'intact' }) : el('span', { class: 'badge badge-warn', text: 'changed outside' })),
        el('div', { class: 'mt-1 truncate text-xs muted', text: p.description || p.source }),
        el('div', { class: 'mt-3 flex gap-3 text-xs faint' }, el('span', { text: `${p.file_count} files` }), el('span', { text: fmtBytes(p.total_bytes) }), el('span', { text: timeAgo(p.last_opened_at) }))));
    }
  } catch (err) { errorToast(err); }
}

async function loadUsageAndTeam() {
  try {
    const [u, o] = await Promise.all([get('/api/usage?days=30'), get('/api/settings/options')]);
    $('#st-calls').textContent = fmtNum(u.totals.calls);
    $('#st-cost').textContent = u.totals.est_cost_usd ? `≈ $${u.totals.est_cost_usd.toFixed(2)} cloud cost` : 'local models: free';
    const team = o.teams.find((t) => t.id === o.effective_team);
    $('#st-team').textContent = team ? team.label : o.effective_team;
    $('#st-effort').textContent = `Effort: ${o.current_effort}`;
  } catch (err) { /* non-critical */ }
}

async function loadTasks() {
  try {
    const r = await post('/api/presence/heartbeat');
    $('#st-running').textContent = r.running.length;
    const box = $('#task-list');
    box.replaceChildren();
    if (!r.running.length) { box.append(el('p', { class: 'muted', text: 'Nothing running.' })); return; }
    const page = { code: 'elvin', design: 'elvin', bughunt: 'bughunt', loadtest: 'loadtest', vulnscan: 'vulns' };
    for (const t of r.running) {
      box.append(el('a', { class: 'flex items-center gap-3 rounded-xl border border-[var(--border)] p-2.5 hover:bg-[var(--hover)]',
        href: t.project_id && page[t.kind] ? `/p/${t.project_id}/${page[t.kind]}` : '/clone' },
        el('span', { class: 'dot dot-live' }), el('span', { class: 'flex-1 capitalize', text: t.kind }), icon('fa-solid fa-arrow-right text-xs opacity-60')));
    }
  } catch { /* ignore */ }
}

async function loadSystem() {
  const box = $('#system');
  try {
    const s = await get('/api/system/status');
    const row = (ok, label, detail, dot = '') => el('div', { class: 'flex items-center gap-3 rounded-xl px-2 py-1.5 hover:bg-[var(--hover)]' },
      el('span', { class: `dot ${dot || (ok ? 'dot-ok' : 'dot-bad')}` }), el('span', { class: 'flex-1', text: label }), el('span', { class: 'truncate text-xs muted', text: detail }));
    const summ = s.summary || {};
    const localMode = summ.gemma_source === 'local';
    const plat = s.platform;
    const lat = s.latency || {};
    const ms = (v) => (v == null ? '' : v >= 1000 ? `${(v / 1000).toFixed(1)} s` : `${v} ms`);
    const gw = s.gateway || {};
    box.replaceChildren(
      el('div', { class: 'mb-2 rounded-xl border border-[var(--border)] p-3 text-xs' },
        el('div', { class: 'font-semibold', text: `${plat.os} · ${plat.arch} · ${plat.gpu_name || plat.gpu}` }),
        el('div', { class: 'muted', text: plat.local_backend === 'none' ? 'No local model - using cloud models' : `Local Qwen via ${plat.local_backend} (${plat.qwen_model || ''})` }),
        gw.configured ? el('div', { class: 'muted', text: gw.pcs_total != null ? `Gemma gateway: ${gw.pcs_online}/${gw.pcs_total} PCs online, ${gw.slots_busy ?? 0}/${gw.slots_total ?? 0} slots busy`
          : `Gemma gateway: ${gw.reachable ? 'online' : 'unreachable'}` }) : ''),
      ...s.providers.map((p) => {
        if (p.key === 'gemma' && localMode) return row(false, p.label, `not used - local Gemma chosen${p.online ? '' : ' (gateway unreachable)'}`, 'dot-warn');
        let detail = p.circuit_open ? 'paused after errors' : lat[p.key] && lat[p.key].gen_ms != null ? `answers in ${ms(lat[p.key].gen_ms)}` : lat[p.key] ? `${p.detail} · ${ms(lat[p.key].api_ms)}` : p.detail;
        if (p.key === 'gemma_local' && summ.fallback_active && p.online) detail = `in use · ${detail} · effort ≤ ${summ.effort_cap}`;
        if (p.key === 'qwen' && !p.online && plat.local_backend === 'ollama') detail = 'not running on this computer - see README → Troubleshooting';
        return row(p.online && !p.circuit_open, p.label, detail);
      }),
      row(s.clamav.available, 'ClamAV virus scanner', s.clamav.available ? s.clamav.version.split('/')[0] : 'starting / unavailable'),
      row(s.runner.online, 'Sandbox runner', s.runner.detail),
      row(s.redis, 'Redis', s.redis ? 'online' : 'offline (falling back to PostgreSQL)'),
      row(s.database, 'PostgreSQL', s.database ? 'online' : 'offline'));
  } catch (err) { box.replaceChildren(el('p', { class: 'muted', text: err.message })); }
}

onEvent((ev) => { if (['done', 'bughunt_done', 'loadtest_done', 'clone_status'].includes(ev.type)) loadTasks(); });
loadGithub(); loadProjects(); loadUsageAndTeam(); loadTasks(); loadSystem();
setInterval(loadTasks, 20000);
