import { $, $$, csrf, deleteProjectDialog, el, errorToast, fmtBytes, get, icon, modal, patch, post, timeAgo, toast } from './core.js';

const pick = ($('#pick-target')?.content.textContent || '').trim();
let filter = 'active';
let all = [];

function card(p) {
  const target = `/p/${p.id}/${pick || 'elvin'}`;
  const actions = el('div', { class: 'mt-4 flex flex-wrap gap-2' },
    el('a', { class: 'btn btn-primary btn-sm', href: target }, icon('fa-solid fa-robot'), pick ? 'Open' : 'Elvin'),
    el('a', { class: 'btn btn-ghost btn-sm', href: `/p/${p.id}/editor` }, icon('fa-solid fa-code'), 'Editor'),
    el('button', { class: 'btn btn-ghost btn-sm', onclick: async () => {
      try { await patch(`/api/projects/${p.id}`, { archived: !p.archived }); toast(p.archived ? 'Restored from archive' : 'Archived', 'ok'); load(); }
      catch (err) { errorToast(err); }
    } }, icon(`fa-solid ${p.archived ? 'fa-box-open' : 'fa-box-archive'}`), p.archived ? 'Unarchive' : 'Archive'),
    el('button', { class: 'btn btn-ghost btn-sm text-rose-500', onclick: () => deleteProjectDialog(p.id, p.name).then((ok) => ok && load()) }, icon('fa-regular fa-trash-can')));
  const srcIcon = { new: 'fa-solid fa-wand-magic-sparkles', upload: 'fa-solid fa-upload', clone: 'fa-solid fa-code-branch', import: 'fa-solid fa-file-import' }[p.source] || 'fa-solid fa-folder';
  return el('div', { class: `glass card-hover p-5 ${p.archived ? 'opacity-70' : ''}` },
    el('div', { class: 'flex items-start justify-between gap-3' },
      el('div', { class: 'min-w-0' }, el('a', { class: 'block truncate text-lg font-semibold hover:underline', href: target, text: p.name }),
        el('div', { class: 'mt-0.5 line-clamp-2 text-sm muted', text: p.description || 'No description' })),
      el('span', { class: 'icon-tile !h-10 !w-10 shrink-0 bg-gradient-to-br from-violet-500 to-cyan-500' }, icon(srcIcon))),
    el('div', { class: 'mt-3 flex flex-wrap gap-2 text-xs' },
      el('span', { class: 'badge', text: `${p.file_count} files · ${fmtBytes(p.total_bytes)}` }),
      p.status === 'missing' ? el('span', { class: 'badge badge-bad', text: 'folder missing' }) : p.integrity_ok ? el('span', { class: 'badge badge-ok', text: 'intact' }) : el('span', { class: 'badge badge-warn', text: 'changed outside Elvin' }),
      p.github_repo ? el('span', { class: 'badge' }, icon('fa-brands fa-github'), ` ${p.github_repo}`) : '',
      p.archived ? el('span', { class: 'badge', text: 'archived' }) : '',
      el('span', { class: 'badge', text: `opened ${timeAgo(p.last_opened_at)}` })),
    actions);
}

async function load() {
  try {
    const r = await get('/api/projects');
    all = r.projects;
    render();
  } catch (err) { errorToast(err); }
}
function render() {
  const list = all.filter((p) => filter === 'all' || (filter === 'archived' ? p.archived : !p.archived));
  $('#count').textContent = `${list.length} shown · ${all.length} total`;
  const grid = $('#project-grid');
  grid.replaceChildren(...list.map(card));
  if (!list.length) grid.append(el('div', { class: 'col-span-full glass p-10 text-center' }, icon('fa-solid fa-folder-plus text-4xl text-violet-500'),
    el('p', { class: 'mt-3 muted', text: filter === 'archived' ? 'Nothing archived.' : 'No projects yet - create one, open a folder from your computer, or clone a repository.' })));
}
for (const b of $$('#filter button')) b.addEventListener('click', () => {
  filter = b.dataset.f;
  for (const x of $$('#filter button')) x.setAttribute('aria-pressed', String(x === b));
  render();
});

async function newProject() {
  const name = el('input', { class: 'input', maxlength: 80, placeholder: 'my-awesome-app' });
  const desc = el('textarea', { class: 'input', rows: 3, maxlength: 500, placeholder: 'What is it about? (optional)' });
  const body = el('div', { class: 'space-y-3' }, el('div', {}, el('label', { class: 'label', text: 'Project name' }), name),
    el('div', {}, el('label', { class: 'label', text: 'Description' }), desc));
  await modal({ title: 'New project', body, actions: [{ label: 'Cancel', value: null }, { label: 'Create project', value: 'ok', cls: 'btn-primary', icon: 'fa-solid fa-plus',
    handler: async () => {
      try { const p = await post('/api/projects', { name: name.value, description: desc.value }); location.href = `/p/${p.id}/elvin`; return true; }
      catch (err) { errorToast(err); return false; }
    } }] });
}
$('#new-project').addEventListener('click', newProject);

function upload(files) {
  if (!files.length) return;
  const fd = new FormData();
  let total = 0;
  for (const f of files) { fd.append('files', f, f.webkitRelativePath || f.name); total += f.size; }
  const box = $('#upload-progress');
  box.classList.remove('hidden');
  $('#upload-label').textContent = `Uploading ${files.length} file(s), ${fmtBytes(total)} - then ClamAV scans them...`;
  const xhr = new XMLHttpRequest();
  xhr.open('POST', '/api/projects/upload');
  xhr.setRequestHeader('X-CSRF-Token', csrf());
  xhr.upload.onprogress = (e) => { if (e.lengthComputable) { const pct = Math.round((e.loaded / e.total) * 100); $('#upload-pct').textContent = `${pct}%`; $('#upload-bar').style.width = `${pct}%`; } };
  xhr.onload = () => {
    let data = {};
    try { data = JSON.parse(xhr.responseText); } catch {}
    if (xhr.status >= 200 && xhr.status < 300) { toast(`Imported ${data.name} - ${data.scanned} file(s) scanned clean`, 'ok'); location.href = `/p/${data.id}/elvin`; }
    else { box.classList.add('hidden'); errorToast(new Error(data.detail || `Upload failed (${xhr.status})`)); }
  };
  xhr.onerror = () => { box.classList.add('hidden'); errorToast(new Error('Upload failed - network error.')); };
  xhr.send(fd);
}
$('#upload-folder').addEventListener('change', (e) => upload(Array.from(e.target.files)));
$('#upload-files').addEventListener('change', (e) => upload(Array.from(e.target.files)));

const params = new URLSearchParams(location.search);
if (params.get('new')) newProject();
if (params.get('upload')) toast('Click "Open folder" to choose a folder from your computer.', 'info');
load();
