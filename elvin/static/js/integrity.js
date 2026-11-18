// Integrity banner shown on every project page: detects files changed or deleted while Elvin was closed.
import { $, el, errorToast, get, icon, modal, post, projectId, toast } from './core.js';

export async function checkIntegrity() {
  const pid = projectId();
  const box = $('#integrity-banner');
  if (!pid || !box) return null;
  let r;
  try { r = await get(`/api/projects/${pid}/integrity`); } catch (err) { return null; }
  box.replaceChildren();
  if (r.ok) { box.classList.add('hidden'); return r; }
  box.classList.remove('hidden');
  if (r.missing_root) {
    box.append(el('div', { class: 'glass flex flex-wrap items-center gap-3 border-rose-500/40 p-4 text-sm' },
      icon('fa-solid fa-folder-minus text-xl text-rose-500'),
      el('div', { class: 'flex-1' }, el('div', { class: 'font-semibold', text: 'The project folder is missing.' }),
        el('div', { class: 'muted', text: 'It may have been moved or deleted while Elvin was closed. Elvin can rebuild it from the latest saved version.' })),
      el('button', { class: 'btn btn-primary btn-sm', onclick: async () => {
        try { await post(`/api/projects/${pid}/integrity/restore`, { paths: [] }); toast('Project folder restored', 'ok'); location.reload(); } catch (err) { errorToast(err); }
      } }, icon('fa-solid fa-rotate-left'), 'Restore from latest version')));
    return r;
  }
  const c = r.counts || {};
  const parts = [];
  if (c.deleted) parts.push(`${c.deleted} deleted`);
  if (c.modified) parts.push(`${c.modified} changed`);
  if (c.added) parts.push(`${c.added} added`);
  const details = () => {
    const list = (title, items, cls) => items.length ? el('div', { class: 'mb-3' }, el('div', { class: `text-xs font-semibold uppercase tracking-wider ${cls}`, text: title }),
      el('div', { class: 'mt-1 max-h-48 overflow-auto font-mono text-xs' }, ...items.map((p) => el('div', { text: p })))) : '';
    modal({ title: 'Changes made outside Elvin', body: el('div', {}, list('Deleted', r.deleted, 'text-rose-500'), list('Changed', r.modified, 'text-amber-500'), list('Added', r.added, 'text-emerald-500')) });
  };
  box.append(el('div', { class: 'glass flex flex-wrap items-center gap-3 p-4 text-sm' },
    icon('fa-solid fa-triangle-exclamation text-xl text-amber-500'),
    el('div', { class: 'min-w-0 flex-1' }, el('div', { class: 'font-semibold', text: `Files changed while Elvin was closed: ${parts.join(', ')}` }),
      el('div', { class: 'muted', text: 'Nothing is broken. Restore them from the last saved version, or accept the changes as the new baseline.' })),
    el('button', { class: 'btn btn-ghost btn-sm', onclick: details }, icon('fa-solid fa-list'), 'Details'),
    (r.deleted.length || r.modified.length) ? el('button', { class: 'btn btn-ghost btn-sm', onclick: async () => {
      try { const res = await post(`/api/projects/${pid}/integrity/restore`, { paths: [...r.deleted, ...r.modified] }); toast(`Restored ${res.restored.length} file(s)`, 'ok'); checkIntegrity(); }
      catch (err) { errorToast(err); }
    } }, icon('fa-solid fa-rotate-left'), 'Restore files') : '',
    el('button', { class: 'btn btn-primary btn-sm', onclick: async () => {
      try { await post(`/api/projects/${pid}/integrity/accept`); toast('Changes accepted and saved as a new version', 'ok'); checkIntegrity(); }
      catch (err) { errorToast(err); }
    } }, icon('fa-solid fa-check'), 'Accept changes')));
  return r;
}
