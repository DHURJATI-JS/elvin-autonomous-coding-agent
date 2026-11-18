// Connect a GitHub account: device code (via the GitHub CLI) or a personal access token.
import { el, errorToast, get, icon, modal, post, toast } from './core.js';

export async function connectGithub() {
  const status = el('div', { class: 'mt-3 text-sm' });
  const codeBox = el('div', { class: 'hidden mt-3 rounded-2xl border border-[var(--border)] p-4 text-center' });
  const tokenInput = el('input', { class: 'input mt-2', type: 'password', placeholder: 'ghp_... or github_pat_...', autocomplete: 'off' });
  let polling = null;
  const startDevice = async () => {
    status.textContent = 'Starting GitHub sign-in...';
    try {
      const flow = await post('/api/github/device');
      const show = (f) => {
        codeBox.classList.remove('hidden');
        codeBox.replaceChildren(
          el('div', { class: 'text-xs uppercase tracking-wider muted', text: 'Your one-time code' }),
          el('div', { class: 'mt-1 font-mono text-3xl font-semibold tracking-[.2em]', text: f.code || '....-....' }),
          el('a', { class: 'btn btn-primary mt-3', href: f.url, target: '_blank', rel: 'noopener noreferrer' }, icon('fa-brands fa-github'), 'Open github.com/login/device'),
          el('p', { class: 'mt-2 text-xs muted', text: 'Paste the code there and approve Elvin. This window updates by itself.' }));
      };
      show(flow);
      status.textContent = 'Waiting for you to approve on GitHub...';
      polling = setInterval(async () => {
        try {
          const f = await get(`/api/github/device/${flow.id}`);
          if (f.code) show(f);
          if (f.status === 'connected') { clearInterval(polling); status.textContent = ''; toast(`Connected to GitHub as ${f.login}`, 'ok'); setTimeout(() => location.reload(), 900); }
          if (f.status === 'failed' || f.status === 'expired') { clearInterval(polling); status.textContent = f.error || 'Sign-in failed.'; }
        } catch (err) { clearInterval(polling); status.textContent = err.message; }
      }, 3000);
    } catch (err) { status.textContent = err.message; }
  };
  const body = el('div', {},
    el('p', { class: 'muted', text: 'Elvin uses the GitHub CLI for pushes, private clones and your stats. The token is stored encrypted and only for your account.' }),
    el('button', { class: 'btn btn-primary mt-4 w-full', onclick: startDevice }, icon('fa-brands fa-github'), 'Sign in with a one-time code'),
    codeBox,
    el('div', { class: 'my-4 flex items-center gap-3 text-xs muted' }, el('div', { class: 'divider flex-1' }), 'or paste a token', el('div', { class: 'divider flex-1' })),
    el('label', { class: 'label', text: 'Personal access token (classic: repo, read:org, workflow)' }), tokenInput,
    status);
  const res = await modal({ title: 'Connect GitHub', body, actions: [{ label: 'Close', value: null },
    { label: 'Save token', value: 'token', cls: 'btn-ghost', icon: 'fa-solid fa-key', handler: async () => {
      if (!tokenInput.value.trim()) { status.textContent = 'Paste a token first, or use the one-time code.'; return false; }
      try { const r = await post('/api/github/token', { token: tokenInput.value.trim() }); toast(`Connected as ${r.login}`, 'ok'); return true; }
      catch (err) { status.textContent = err.message; return false; }
    } }] });
  if (polling) clearInterval(polling);
  if (res === 'token') location.reload();
}

export async function disconnectGithub() {
  try { await post('/api/github/disconnect'); toast('GitHub disconnected', 'ok'); location.reload(); } catch (err) { errorToast(err); }
}
