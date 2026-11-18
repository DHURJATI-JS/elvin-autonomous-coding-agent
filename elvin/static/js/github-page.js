// Every GitHub repository the account can see, with search / filter / sort and one-click clone.
import { $, $$, el, errorToast, fmtNum, get, icon, md, modal, post, timeAgo, toast } from './core.js';
import { connectGithub } from './github.js';

let repos = [];
let login = '';
let filter = 'all';

function card(r) {
  return el('div', { class: 'glass card-hover flex min-w-0 flex-col p-4' },
    el('div', { class: 'flex min-w-0 items-start gap-3' },
      icon(`fa-solid ${r.private ? 'fa-lock' : 'fa-book-bookmark'} mt-1 text-violet-500`),
      el('div', { class: 'min-w-0 flex-1' },
        el('a', { class: 'block truncate font-semibold hover:underline', href: r.url, target: '_blank', rel: 'noopener noreferrer', title: r.full_name, text: r.full_name }),
        el('div', { class: 'mt-1 line-clamp-2 text-sm muted', text: r.description || 'No description' }))),
    el('div', { class: 'mt-3 flex flex-wrap gap-1.5 text-xs' },
      el('span', { class: `badge ${r.private ? 'badge-warn' : 'badge-ok'}`, text: r.visibility || (r.private ? 'private' : 'public') }),
      r.fork ? el('span', { class: 'badge', text: 'fork' }) : '', r.archived ? el('span', { class: 'badge', text: 'archived' }) : '',
      r.owner && r.owner !== login ? el('span', { class: 'badge badge-info', text: r.owner }) : '',
      r.language ? el('span', { class: 'badge', text: r.language }) : '', r.license ? el('span', { class: 'badge', text: r.license }) : ''),
    el('div', { class: 'mt-auto flex flex-wrap items-center justify-between gap-2 pt-3 text-xs muted' },
      el('span', {}, icon('fa-regular fa-star'), ` ${fmtNum(r.stars)}  `, icon('fa-solid fa-code-fork'), ` ${fmtNum(r.forks)}  `,
        icon('fa-solid fa-circle-dot'), ` ${fmtNum(r.open_issues)}`),
      el('span', { text: `pushed ${timeAgo(r.pushed_at)}` })),
    el('div', { class: 'mt-3 flex gap-2' },
      el('button', { class: 'btn btn-ghost btn-sm flex-1', onclick: () => viewRepo(r) }, icon('fa-solid fa-eye'), 'View'),
      el('a', { class: 'btn btn-ghost btn-sm', href: r.url, target: '_blank', rel: 'noopener noreferrer', title: 'Open on GitHub' }, icon('fa-solid fa-arrow-up-right-from-square')),
      el('a', { class: 'btn btn-primary btn-sm flex-1', href: `/clone?url=${encodeURIComponent(r.full_name)}` }, icon('fa-solid fa-code-branch'), 'Clone into Elvin')));
}

// Read-only repository viewer: README, files, commits. Nothing here can change the repository.
async function viewRepo(r) {
  const [owner, name] = r.full_name.split('/');
  const body = el('div', {}, el('div', { class: 'skeleton h-40' }));
  modal({ title: r.full_name, wide: true, body, actions: [
    { label: 'Close', value: null },
    { label: 'Clone into Elvin', value: 'clone', cls: 'btn-primary', icon: 'fa-solid fa-code-branch', handler: () => { location.href = `/clone?url=${encodeURIComponent(r.full_name)}`; } }] });
  let v;
  try { v = await get(`/api/github/repos/${encodeURIComponent(owner)}/${encodeURIComponent(name)}`); }
  catch (err) { body.replaceChildren(el('p', { class: 'muted', text: err.message })); return; }
  const pane = el('div', { class: 'mt-4' });
  const tabs = el('div', { class: 'seg' });
  const show = (key) => {
    for (const b of tabs.children) b.setAttribute('aria-pressed', String(b.dataset.k === key));
    if (key === 'readme') pane.replaceChildren(v.readme ? el('div', { class: 'prose-elvin max-h-[60vh] overflow-auto rounded-xl border border-[var(--border)] p-4', html: md(v.readme) })
      : el('p', { class: 'muted', text: 'This repository has no README.' }));
    if (key === 'files') browse('');
    if (key === 'commits') pane.replaceChildren(el('div', { class: 'divide-y divide-[var(--border)]' }, ...v.commits.map((c) => el('a', {
      class: 'flex items-center gap-3 py-2 text-sm hover:opacity-80', href: c.url, target: '_blank', rel: 'noopener noreferrer' },
      el('code', { class: 'text-xs text-violet-500', text: c.sha }), el('span', { class: 'min-w-0 flex-1 truncate', text: c.message }),
      el('span', { class: 'shrink-0 text-xs muted', text: `${c.author || ''} · ${timeAgo(c.date)}` })))));
  };
  async function browse(path) {
    pane.replaceChildren(el('div', { class: 'skeleton h-32' }));
    try {
      const c = await get(`/api/github/repos/${encodeURIComponent(owner)}/${encodeURIComponent(name)}/contents?path=${encodeURIComponent(path)}`);
      const crumbs = el('div', { class: 'mb-2 flex flex-wrap items-center gap-1 text-xs' },
        el('button', { class: 'text-violet-500 hover:underline', onclick: () => browse('') }, name),
        ...path.split('/').filter(Boolean).flatMap((part, i, all) => [el('span', { class: 'faint', text: '/' }),
          el('button', { class: 'text-violet-500 hover:underline', onclick: () => browse(all.slice(0, i + 1).join('/')) }, part)]));
      if (c.type === 'dir') {
        pane.replaceChildren(crumbs, el('div', { class: 'max-h-[55vh] overflow-auto rounded-xl border border-[var(--border)]' }, ...c.files.map((f) => el('button', {
          class: 'flex w-full items-center gap-2 border-b border-[var(--border)] px-3 py-2 text-left text-sm last:border-0 hover:bg-[var(--hover)]', onclick: () => browse(f.path) },
          icon(`fa-solid ${f.type === 'dir' ? 'fa-folder text-amber-500' : 'fa-file-lines text-violet-400'} w-4`), el('span', { class: 'flex-1 truncate', text: f.name }),
          f.type === 'file' ? el('span', { class: 'text-xs faint', text: `${(f.size / 1024).toFixed(1)} KB` }) : ''))));
      } else {
        pane.replaceChildren(crumbs, c.too_large || c.binary
          ? el('p', { class: 'muted' }, c.binary ? 'Binary file - ' : 'Large file - ', el('a', { class: 'text-violet-500 underline', href: c.url, target: '_blank', rel: 'noopener noreferrer', text: 'open it on GitHub' }), '.')
          : el('pre', { class: 'code-box max-h-[55vh] whitespace-pre', text: c.content }));
      }
    } catch (err) { pane.replaceChildren(el('p', { class: 'muted', text: err.message })); }
  }
  for (const [k, label] of [['readme', 'README'], ['files', 'Files'], ['commits', `Commits (${v.commits.length})`]]) {
    tabs.append(el('button', { dataset: { k }, 'aria-pressed': 'false', onclick: () => show(k) }, label));
  }
  body.replaceChildren(
    el('div', { class: 'flex flex-wrap items-center gap-2 text-xs' },
      el('span', { class: 'badge badge-info' }, icon('fa-solid fa-lock'), ' read-only - Elvin never modifies or deletes pushed code'),
      el('span', { class: `badge ${v.private ? 'badge-warn' : 'badge-ok'}`, text: v.private ? 'private' : 'public' }),
      v.language ? el('span', { class: 'badge', text: v.language }) : '', v.license ? el('span', { class: 'badge', text: v.license }) : '',
      el('span', { class: 'badge', text: `branch ${v.default_branch}` }), el('span', { class: 'badge', text: `★ ${fmtNum(v.stars)}` })),
    v.description ? el('p', { class: 'mt-3 muted', text: v.description }) : '',
    el('div', { class: 'mt-4' }, tabs), pane);
  show('readme');
}

function render() {
  const q = $('#q').value.trim().toLowerCase();
  const keep = { all: () => true, owned: (r) => r.owner === login, forks: (r) => r.fork, private: (r) => r.private, public: (r) => !r.private, archived: (r) => r.archived }[filter];
  let list = repos.filter(keep);
  if (q) list = list.filter((r) => `${r.full_name} ${r.description || ''} ${r.language || ''}`.toLowerCase().includes(q));
  const sort = $('#sort').value;
  list = [...list].sort((a, b) => (sort === 'stars' ? b.stars - a.stars
    : sort === 'name' ? a.full_name.localeCompare(b.full_name)
      : (b.pushed_at || '').localeCompare(a.pushed_at || '')));
  $('#count').textContent = `Showing ${list.length} of ${repos.length} repositories`;
  const box = $('#repos');
  box.replaceChildren(...list.map(card));
  if (!list.length) box.append(el('p', { class: 'col-span-full py-10 text-center muted', text: repos.length ? 'No repository matches.' : 'No repositories yet.' }));
}

function show(stats) {
  const d = stats.data || {};
  if (!d.profile) {
    $('#gh-sub').textContent = 'GitHub is not connected.';
    $('#repos').replaceChildren(el('div', { class: 'col-span-full glass flex flex-col items-center gap-3 p-10 text-center' }, icon('fa-brands fa-github text-4xl'),
      el('p', { class: 'muted', text: stats.error && stats.error !== 'auth' ? stats.error : 'Connect GitHub to list every repository you can access.' }),
      el('button', { class: 'btn btn-primary', onclick: connectGithub }, icon('fa-brands fa-github'), 'Connect GitHub')));
    return;
  }
  login = d.profile.login;
  repos = d.repos || d.recent || [];
  $('#gh-sub').textContent = `@${login} · ${fmtNum(repos.length)} repositories you can access (${fmtNum(d.totals?.repos)} your own) · updated ${timeAgo(stats.fetched_at)}`;
  render();
}

async function load() {
  try { show(await get('/api/github/stats')); } catch (err) { errorToast(err); }
}
$('#refresh').addEventListener('click', async (e) => {
  const b = e.currentTarget;
  b.disabled = true;
  try { show(await post('/api/github/stats/refresh')); toast('Repositories refreshed', 'ok', 2000); } catch (err) { errorToast(err); } finally { b.disabled = false; }
});
$('#q').addEventListener('input', render);
$('#sort').addEventListener('change', render);
for (const b of $$('#filter button')) {
  b.addEventListener('click', () => {
    filter = b.dataset.f;
    for (const x of $$('#filter button')) x.setAttribute('aria-pressed', String(x === b));
    render();
  });
}
load();
