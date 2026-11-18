import { $, el, errorToast, get, icon, modal, post, projectId, timeAgo, toast } from './core.js';
import { connectGithub, disconnectGithub } from './github.js';
import { checkIntegrity } from './integrity.js';

const pid = projectId();
let visibility = 'private';
let info = null;

for (const b of document.querySelectorAll('#visibility button')) b.addEventListener('click', () => {
  visibility = b.dataset.v;
  for (const x of document.querySelectorAll('#visibility button')) x.setAttribute('aria-pressed', String(x === b));
});

async function load() {
  info = await get(`/api/projects/${pid}/git`);
  const acct = $('#account-card');
  if (info.account) {
    acct.replaceChildren(el('div', { class: 'flex flex-wrap items-center justify-between gap-3' },
      el('div', { class: 'flex items-center gap-3' }, el('span', { class: 'icon-tile bg-gradient-to-br from-emerald-500 to-teal-500' }, icon('fa-brands fa-github')),
        el('div', {}, el('div', { class: 'font-semibold', text: `Connected as @${info.account.login}` }),
          el('div', { class: 'text-xs muted', text: `${info.tools.git || 'git missing'} · ${info.tools.gh || 'gh missing'}` }))),
      el('button', { class: 'btn btn-ghost btn-sm', onclick: disconnectGithub }, icon('fa-solid fa-link-slash'), 'Disconnect')));
  } else {
    acct.replaceChildren(el('div', { class: 'flex flex-wrap items-center justify-between gap-3' },
      el('div', { class: 'flex items-center gap-3' }, el('span', { class: 'icon-tile bg-gradient-to-br from-slate-500 to-zinc-700' }, icon('fa-brands fa-github')),
        el('div', {}, el('div', { class: 'font-semibold', text: 'GitHub is not connected' }), el('div', { class: 'text-xs muted', text: 'Connect once - Elvin initialises the GitHub CLI in the background.' }))),
      el('button', { class: 'btn btn-primary', onclick: connectGithub }, icon('fa-brands fa-github'), 'Connect GitHub')));
  }
  if (!$('#repo-name').value) $('#repo-name').value = (info.github_repo ? info.github_repo.split('/')[1] : info.default_name) || '';
  const st = info.status;
  const box = $('#repo-status');
  const row = (label, value) => el('div', { class: 'flex justify-between gap-3' }, el('span', { class: 'muted', text: label }), el('span', { class: 'truncate text-right font-medium', text: value }));
  if (!st.is_repo) box.replaceChildren(el('p', { class: 'muted', text: 'Not a git repository yet - Elvin will initialise it on the first push.' }));
  else box.replaceChildren(row('Branch', st.branch || '-'), row('Remote', st.remote || 'none'), row('Uncommitted changes', st.changes ?? '-'),
    st.last_commit ? row('Last commit', `${st.last_commit.subject} · ${timeAgo(st.last_commit.date)}`) : '');
  if (info.repo_url) box.append(el('a', { class: 'btn btn-ghost btn-sm mt-2 w-full', href: info.repo_url, target: '_blank', rel: 'noopener noreferrer' }, icon('fa-brands fa-github'), 'Open repository'));
}

async function doPush(secretsAction = '') {
  const body = { repo_name: $('#repo-name').value.trim(), private: visibility === 'private', description: $('#repo-desc').value.trim(),
    message: $('#commit-msg').value.trim() || 'Update from Elvin', secrets_action: secretsAction };
  $('#push-btn').disabled = true;
  $('#push-result').replaceChildren(el('div', { class: 'flex items-center gap-2 text-sm muted' }, icon('fa-solid fa-spinner fa-spin'), 'Scanning, committing and pushing...'));
  try {
    const r = await post(`/api/projects/${pid}/git/push`, body);
    if (r.needs_confirmation) {
      $('#push-result').replaceChildren();
      const fixable = r.secrets.every((s) => s.fixable === 'yes');
      const choice = await modal({ title: 'Possible secrets found', wide: true,
        body: el('div', {}, el('p', { class: 'muted', text: r.message || 'These files may contain passwords or keys. Pushing them would publish them.' }),
          el('div', { class: 'mt-3 space-y-1.5' }, ...r.secrets.map((s) => el('div', { class: 'flex items-center gap-2 text-sm' },
            el('span', { class: `badge ${s.fixable === 'yes' ? 'badge-warn' : 'badge-bad'}`, text: s.kind }), el('span', { class: 'font-mono text-xs', text: s.path }))))),
        actions: [{ label: 'Cancel', value: null },
          { label: 'Push anyway', value: 'push_anyway', cls: 'btn-ghost text-rose-500' },
          ...(fixable ? [{ label: 'Keep them out (.gitignore) and push', value: 'ignore_files', cls: 'btn-primary' }] : [])] });
      if (choice) return doPush(choice);
      return;
    }
    $('#push-result').replaceChildren(el('div', { class: 'rounded-2xl border border-emerald-500/30 bg-emerald-500/10 p-4' },
      el('div', { class: 'flex items-center gap-2 font-semibold' }, icon('fa-solid fa-circle-check text-emerald-500'), 'Pushed to GitHub'),
      el('ul', { class: 'mt-2 list-disc pl-5 text-sm muted' }, ...r.steps.map((s) => el('li', { text: s }))),
      el('div', { class: 'mt-3 flex flex-wrap items-center gap-2' },
        el('a', { class: 'btn btn-primary btn-sm', href: r.url, target: '_blank', rel: 'noopener noreferrer' }, icon('fa-brands fa-github'), r.repo.nameWithOwner || r.url),
        r.repo.visibility ? el('span', { class: 'badge', text: r.repo.visibility.toLowerCase() }) : '',
        r.repo.defaultBranchRef ? el('span', { class: 'badge', text: `branch ${r.repo.defaultBranchRef.name}` }) : '')));
    toast('Pushed to GitHub', 'ok');
    load();
  } catch (err) { $('#push-result').replaceChildren(); errorToast(err); }
  finally { $('#push-btn').disabled = false; }
}

$('#push-form').addEventListener('submit', async (e) => {
  e.preventDefault();
  if (!info || !info.account) { connectGithub(); return; }
  const ok = await modal({ title: 'Upload to GitHub?', body: `Elvin will commit this project and push it to ${visibility} repository "${$('#repo-name').value.trim()}" on @${info.account.login}.`,
    actions: [{ label: 'Cancel', value: false }, { label: 'Push', value: true, cls: 'btn-primary', icon: 'fa-solid fa-cloud-arrow-up' }] });
  if (ok) doPush('');
});

checkIntegrity();
load().catch(errorToast);
