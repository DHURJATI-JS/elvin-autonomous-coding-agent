import { $, diffView, el, errorToast, fmtBytes, get, icon, modal, post, projectId, timeAgo, toast, withPassword } from './core.js';
import { checkIntegrity } from './integrity.js';

const pid = projectId();
const KIND = { step: ['badge-brand', 'Elvin step'], task_end: ['badge-ok', 'task finished'], task_start: ['badge', 'before task'],
  manual: ['badge-info', 'manual'], autosave: ['badge', 'autosave'], restore: ['badge-warn', 'restore'], partial: ['badge-warn', 'partial (stopped)'],
  external: ['badge-warn', 'outside changes'], initial: ['badge-ok', 'initial'] };

function verifyBadge(s) {
  if (s.verify_ok === true) return el('span', { class: 'badge badge-ok' }, icon('fa-solid fa-shield-halved'), ' verified');
  if (s.verify_ok === false) return el('span', { class: 'badge badge-bad' }, icon('fa-solid fa-triangle-exclamation'), ' failed');
  return el('span', { class: 'badge', text: 'not verified yet' });
}

async function load() {
  const r = await get(`/api/projects/${pid}/snapshots?limit=200`);
  if (!r.versioning_enabled) $('#hist-sub').textContent = 'Versioning is turned off in Settings - only restore points made before a restore or a task appear here.';
  const box = $('#snapshots');
  box.replaceChildren();
  if (!r.snapshots.length) box.append(el('p', { class: 'muted', text: 'No versions yet.' }));
  for (const s of r.snapshots) {
    const [cls, label] = KIND[s.kind] || ['badge', s.kind];
    const vb = el('span', {}, verifyBadge(s));
    box.append(el('div', { class: 'glass relative p-4' },
      el('span', { class: 'absolute -left-[22px] top-5 h-3 w-3 rounded-full border-2 border-[var(--bg)] bg-gradient-to-br from-violet-500 to-cyan-500' }),
      el('div', { class: 'flex flex-wrap items-center gap-2' }, el('span', { class: `badge ${cls}`, text: label }), el('span', { class: 'font-medium', text: s.label || '(no label)' }), vb,
        el('span', { class: 'ml-auto text-xs faint', title: s.created_at, text: `${new Date(s.created_at).toLocaleString()} · ${timeAgo(s.created_at)}` })),
      el('div', { class: 'mt-1.5 flex flex-wrap gap-3 text-xs muted' }, el('span', { text: `${s.file_count} files · ${fmtBytes(s.total_bytes)}` }),
        el('span', { text: `by ${s.created_by}` }), el('span', { class: 'font-mono', title: s.chain_hash, text: `chain ${s.chain_hash.slice(0, 12)}…` })),
      el('div', { class: 'mt-3 flex flex-wrap gap-2' },
        el('button', { class: 'btn btn-ghost btn-sm', onclick: async () => {
          try { const v = await post(`/api/snapshots/${s.id}/verify`); vb.replaceChildren(verifyBadge({ verify_ok: v.ok }));
            toast(v.ok ? 'Verified: every stored file matches its hash and the chain is intact.' : `Verification failed: ${[!v.chain_ok && 'chain', !v.local_copy_ok && 'local copy', v.missing.length && `${v.missing.length} missing`, v.corrupt.length && `${v.corrupt.length} corrupt`].filter(Boolean).join(', ')}`, v.ok ? 'ok' : 'error');
          } catch (err) { errorToast(err); }
        } }, icon('fa-solid fa-shield-halved'), 'Verify'),
        el('button', { class: 'btn btn-ghost btn-sm', onclick: () => changes(s) }, icon('fa-solid fa-code-compare'), 'Changes'),
        el('button', { class: 'btn btn-ghost btn-sm', onclick: () => restore(s) }, icon('fa-solid fa-rotate-left'), 'Restore'))));
  }
}

async function changes(s) {
  try {
    const c = await get(`/api/snapshots/${s.id}/changes`);
    const viewer = el('div', { class: 'mt-3' });
    const item = (p, kind, cls) => el('button', { class: 'menu-item !py-1 font-mono text-xs', onclick: async () => {
      try { const d = await get(`/api/snapshots/${s.id}/diff?path=${encodeURIComponent(p)}`); viewer.replaceChildren(diffView(d.diff || '(no text difference)')); } catch (err) { errorToast(err); }
    } }, el('span', { class: `badge ${cls}`, text: kind }), p);
    const all = [...c.added.map((p) => item(p, 'added', 'badge-ok')), ...c.modified.map((p) => item(p, 'changed', 'badge-warn')), ...c.removed.map((p) => item(p, 'removed', 'badge-bad'))];
    modal({ title: `Changes in this version`, wide: true, body: el('div', {}, all.length ? el('div', { class: 'max-h-56 overflow-auto' }, ...all) : el('p', { class: 'muted', text: 'No file changes compared with the previous version.' }), viewer) });
  } catch (err) { errorToast(err); }
}

async function restore(s) {
  const ok = await modal({ title: 'Restore this version?', body: `Your files will be put back exactly as they were at ${new Date(s.created_at).toLocaleString()}. The current state is saved first, so this can be undone.`,
    actions: [{ label: 'Cancel', value: false }, { label: 'Restore', value: true, cls: 'btn-primary', icon: 'fa-solid fa-rotate-left' }] });
  if (!ok) return;
  try {
    const r = await withPassword((password) => post(`/api/snapshots/${s.id}/restore`, { password }), 'Restoring this version deletes files.', { askFirst: false });
    if (r === undefined) return;
    if (r.ok) { toast(`Restored (${r.changed} file change(s))`, 'ok'); load(); checkIntegrity(); } else errorToast(new Error(r.error));
  }
  catch (err) { errorToast(err); }
}

$('#save-now').addEventListener('click', async () => {
  try { const s = await post(`/api/projects/${pid}/snapshots`, { label: 'Manual save' }); toast(s.verify.ok ? 'Version saved and verified' : 'Version saved', 'ok'); load(); } catch (err) { errorToast(err); }
});

checkIntegrity();
load().catch(errorToast);
