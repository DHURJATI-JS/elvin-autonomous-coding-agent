import { $, el, errorToast, get, icon, post, toast } from './core.js';
import { renderAiSettings } from './ai-settings.js';
import { connectGithub, disconnectGithub } from './github.js';

const pref = document.documentElement.dataset.themePref || 'system';
let stored = pref;
try { stored = localStorage.getItem('elvin-theme') || pref; } catch {}
for (const b of document.querySelectorAll('#theme-seg button')) {
  b.setAttribute('aria-pressed', String(b.dataset.t === stored));
  b.addEventListener('click', async () => {
    const t = b.dataset.t;
    try { if (t === 'system') localStorage.removeItem('elvin-theme'); else localStorage.setItem('elvin-theme', t); } catch {}
    const dark = t === 'dark' || (t === 'system' && matchMedia('(prefers-color-scheme: dark)').matches);
    document.documentElement.classList.toggle('dark', dark);
    for (const x of document.querySelectorAll('#theme-seg button')) x.setAttribute('aria-pressed', String(x === b));
    try { await post('/api/settings', { theme: t }); } catch (err) { errorToast(err); }
  });
}

const vt = $('#versioning');
const toggleVersioning = async () => {
  const next = vt.getAttribute('aria-checked') !== 'true';
  try { await post('/api/settings', { versioning_enabled: next }); vt.setAttribute('aria-checked', String(next)); toast(next ? 'Timestamped versions on' : 'Timestamped versions off', 'ok'); }
  catch (err) { errorToast(err); }
};
vt.addEventListener('click', toggleVersioning);
vt.addEventListener('keydown', (e) => { if (e.key === ' ' || e.key === 'Enter') { e.preventDefault(); toggleVersioning(); } });

renderAiSettings($('#ai-settings'));

async function gh() {
  try {
    const s = await get('/api/github/status');
    $('#gh-settings').replaceChildren(s.account
      ? el('div', { class: 'flex flex-wrap items-center justify-between gap-3' }, el('div', {}, icon('fa-brands fa-github'), ` Connected as @${s.account.login}`,
          el('div', { class: 'text-xs muted', text: s.account.scopes ? `scopes: ${s.account.scopes}` : 'fine-grained token' })),
        el('button', { class: 'btn btn-ghost btn-sm', onclick: disconnectGithub }, icon('fa-solid fa-link-slash'), 'Disconnect'))
      : el('div', { class: 'flex flex-wrap items-center justify-between gap-3' }, el('span', { class: 'muted', text: 'Not connected.' }),
        el('button', { class: 'btn btn-primary btn-sm', onclick: connectGithub }, icon('fa-brands fa-github'), 'Connect GitHub')));
  } catch (err) { errorToast(err); }
}
gh();

$('#pw-form').addEventListener('submit', async (e) => {
  e.preventDefault();
  try {
    await post('/api/settings/password', { current: $('#pw-cur').value, new: $('#pw-new').value, confirm: $('#pw-conf').value });
    toast('Password updated', 'ok');
    e.target.reset();
  } catch (err) { errorToast(err); }
});
