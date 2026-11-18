import { $, diffView, el, errorToast, get, icon, modal, post, projectId, timeAgo, toast } from './core.js';
import { checkIntegrity } from './integrity.js';

const pid = projectId();
const SEV = [['CRITICAL', 'sev-critical'], ['HIGH', 'sev-high'], ['MEDIUM', 'sev-medium'], ['LOW', 'sev-low'], ['UNKNOWN', 'sev-unknown']];

function render(r) {
  const tiles = $('#sev-tiles');
  const list = $('#vuln-list');
  if (!r.scan) {
    tiles.replaceChildren();
    list.replaceChildren(el('div', { class: 'card text-center' }, icon('fa-solid fa-shield-virus text-4xl text-teal-500'),
      el('p', { class: 'mt-3 muted', text: 'No scan yet. Press "Scan now" to check every dependency.' })));
    return;
  }
  const s = r.scan;
  const counts = (s.summary && s.summary.counts) || {};
  $('#scan-sub').textContent = s.status === 'failed' ? `Scan failed: ${s.error}` :
    `Last scan ${timeAgo(s.created_at)} · ${s.summary.dependencies || 0} dependencies in ${(s.summary.manifests || []).length} manifest(s) · ${s.summary.vulnerable || 0} vulnerable`;
  tiles.replaceChildren(...SEV.map(([k, cls]) => el('div', { class: 'stat-tile' }, el('span', { class: `badge ${cls}`, text: k.toLowerCase() }), el('div', { class: 'value', text: counts[k] || 0 }))));
  list.replaceChildren();
  if (!r.findings.length) {
    list.append(el('div', { class: 'card flex items-center gap-3' }, icon('fa-solid fa-circle-check text-2xl text-emerald-500'),
      el('div', {}, el('div', { class: 'font-semibold', text: 'No known vulnerabilities' }), el('div', { class: 'text-sm muted', text: 'Every dependency passed the OSV.dev check.' }))));
    return;
  }
  for (const f of r.findings) {
    const sevCls = (SEV.find(([k]) => k === f.severity) || [null, 'sev-unknown'])[1];
    const actions = el('div', { class: 'mt-3 flex flex-wrap gap-2' });
    if (f.fixable) actions.append(el('button', { class: 'btn btn-primary btn-sm', onclick: () => fix(f) }, icon('fa-solid fa-wrench'), `Upgrade to ${f.fixed_version}`));
    actions.append(el('button', { class: 'btn btn-ghost btn-sm', onclick: (e) => alternatives(f, e.currentTarget) }, icon('fa-solid fa-shuffle'), 'Suggest alternatives'));
    list.append(el('div', { class: 'card' },
      el('div', { class: 'flex flex-wrap items-center gap-2' },
        el('span', { class: `badge ${sevCls}`, text: `${f.severity.toLowerCase()}${f.score ? ` ${f.score}` : ''}` }),
        el('span', { class: 'font-semibold', text: f.package }), el('span', { class: 'font-mono text-sm muted', text: f.version || '(unpinned)' }),
        el('span', { class: 'badge', text: f.ecosystem }), f.details && f.details.malicious ? el('span', { class: 'badge sev-critical', text: 'MALICIOUS' }) : '',
        el('span', { class: 'ml-auto font-mono text-xs faint', text: f.manifest })),
      f.summary ? el('p', { class: 'mt-2 text-sm', text: f.summary }) : '',
      el('div', { class: 'mt-2 rounded-xl bg-[var(--hover)] p-3 text-sm' }, icon('fa-solid fa-lightbulb text-amber-500'), ` ${f.recommendation}`),
      f.vuln_ids.length ? el('div', { class: 'mt-2 flex flex-wrap gap-1.5' }, ...f.vuln_ids.slice(0, 12).map((id) =>
        el('a', { class: 'badge hover:underline', href: `https://osv.dev/vulnerability/${encodeURIComponent(id)}`, target: '_blank', rel: 'noopener noreferrer', text: id }))) : '',
      actions));
  }
}

async function load() { try { render(await get(`/api/projects/${pid}/vulns`)); } catch (err) { errorToast(err); } }

$('#scan').addEventListener('click', async (e) => {
  const btn = e.currentTarget;
  btn.disabled = true;
  btn.replaceChildren(icon('fa-solid fa-spinner fa-spin'), ' Scanning...');
  try { render(await post(`/api/projects/${pid}/vulns/scan`)); toast('Scan complete', 'ok', 2000); }
  catch (err) { errorToast(err); }
  finally { btn.disabled = false; btn.replaceChildren(icon('fa-solid fa-satellite-dish'), ' Scan now'); }
});

async function fix(f) {
  try {
    const p = await post(`/api/vulns/findings/${f.id}/fix-preview`);
    if (!p.automatic) {
      modal({ title: `Upgrade ${f.package}`, body: el('div', {}, el('p', { class: 'muted', text: p.message }), el('pre', { class: 'terminal mt-3', text: p.command })) });
      return;
    }
    const ok = await modal({ title: `Upgrade ${f.package} to ${f.fixed_version}?`, wide: true,
      body: el('div', {}, el('p', { class: 'mb-3 muted', text: `This edits ${p.manifest}. A timestamped version is saved, so you can undo it from History.` }), diffView(p.diff)),
      actions: [{ label: 'Cancel', value: false }, { label: 'Apply upgrade', value: true, cls: 'btn-primary', icon: 'fa-solid fa-check' }] });
    if (!ok) return;
    await post(`/api/vulns/findings/${f.id}/fix-apply`, { sha256: p.sha256 });
    toast(`${f.package} upgraded in ${p.manifest}. Re-scan to confirm.`, 'ok');
  } catch (err) { errorToast(err); }
}

async function alternatives(f, btn) {
  btn.disabled = true;
  try {
    const r = await post(`/api/vulns/findings/${f.id}/alternatives`);
    const body = r.alternatives.length ? el('div', { class: 'space-y-2' }, ...r.alternatives.map((a) => el('div', { class: 'rounded-xl border border-[var(--border)] p-3' },
      el('div', { class: 'flex items-center gap-2' }, el('span', { class: 'font-semibold', text: a.name }), el('span', { class: 'font-mono text-xs muted', text: a.version || '' }),
        el('span', { class: `badge ${a.clean ? 'badge-ok' : 'badge-warn'}`, text: a.clean ? 'no known vulnerabilities' : 'has advisories' })),
      el('p', { class: 'mt-1 text-sm muted', text: a.reason }))))
      : el('p', { class: 'muted', text: 'No safe alternative was found.' });
    modal({ title: `Alternatives to ${f.package}`, body });
  } catch (err) { errorToast(err); } finally { btn.disabled = false; }
}

$('#check-form').addEventListener('submit', async (e) => {
  e.preventDefault();
  const box = $('#check-result');
  box.replaceChildren(el('div', { class: 'skeleton h-16' }));
  try {
    const v = await post(`/api/projects/${pid}/vulns/check`, { ecosystem: $('#ck-eco').value, name: $('#ck-name').value.trim(), version: $('#ck-ver').value.trim() });
    const cls = v.verdict === 'go' ? 'badge-ok' : v.verdict === 'go_with_version' ? 'badge-warn' : 'badge-bad';
    box.replaceChildren(el('div', { class: 'rounded-xl border border-[var(--border)] p-3 text-sm' },
      el('div', { class: 'flex items-center gap-2' }, el('span', { class: `badge ${cls}`, text: v.verdict.replace(/_/g, ' ') }), el('span', { class: 'font-medium', text: `${v.name} ${v.version || ''}` })),
      el('p', { class: 'mt-1.5 muted', text: v.reason }),
      v.recommended ? el('p', { class: 'mt-1' }, 'Use: ', el('code', { text: v.recommended })) : ''));
  } catch (err) { box.replaceChildren(); errorToast(err); }
});

checkIntegrity();
load();
