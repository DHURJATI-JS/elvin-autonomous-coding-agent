// Shared picker for AI team (model combination), effort level and per-role models.
import { el, errorToast, get, icon, post, toast } from './core.js';

const MODEL_LABELS = { 'claude-opus-5-5': 'Claude Opus 5.5', 'claude-sonnet-5-5': 'Claude Sonnet 5.5', 'claude-haiku-5-5': 'Claude Haiku 5.5' };

export async function renderAiSettings(container, onChange) {
  let o;
  try { o = await get('/api/settings/options'); } catch (err) { container.replaceChildren(el('p', { class: 'muted', text: err.message })); return; }
  const save = async (body, msg) => {
    try { await post('/api/settings', body); toast(msg, 'ok', 2000); if (onChange) onChange(); await renderAiSettings(container, onChange); }
    catch (err) { errorToast(err); }
  };
  const teams = el('div', { class: 'grid gap-2 sm:grid-cols-2' }, ...o.teams.map((t) => {
    const selected = t.id === o.effective_team;
    return el('button', { class: `rounded-xl border p-3 text-left transition ${selected ? 'border-violet-500 ring-glow' : 'border-[var(--border)] hover:bg-[var(--hover)]'} ${t.available ? '' : 'cursor-not-allowed opacity-50'}`,
      disabled: !t.available, title: t.available ? '' : `Needs: ${t.needs.join(', ')}`, onclick: () => !selected && save({ ai_team: t.id }, `AI team: ${t.label}`) },
      el('div', { class: 'flex items-center gap-2 text-sm font-semibold' }, icon(`fa-solid ${selected ? 'fa-circle-dot text-violet-500' : 'fa-circle text-slate-400'} text-xs`), t.label),
      el('div', { class: 'mt-1 text-xs muted', text: t.available ? t.blurb : `Not set up on this computer (needs ${t.needs.join(' + ')}).` }));
  }));
  const effort = el('div', { class: 'seg flex-wrap' }, ...o.efforts.map((e) => el('button', { 'aria-pressed': String(e.id === o.current_effort), title: e.blurb,
    onclick: () => e.id !== o.current_effort && save({ ai_effort: e.id }, `Effort: ${e.id}`) }, e.id)));
  const blurb = el('p', { class: 'mt-2 text-xs muted', text: (o.efforts.find((e) => e.id === o.current_effort) || {}).blurb || '' });
  const modelRow = (key, label, choices) => {
    if (!choices.length) return '';
    const current = o.model_prefs[key] || o.defaults[key];
    const sel = el('select', { class: 'input !py-1.5 !text-sm' }, ...choices.map((m) => el('option', { value: m, text: MODEL_LABELS[m] || m, selected: m === current })));
    sel.addEventListener('change', () => save({ model_prefs: { [key]: sel.value } }, `${label}: ${MODEL_LABELS[sel.value] || sel.value}`));
    return el('div', {}, el('label', { class: 'label !text-xs', text: label }), sel);
  };
  const models = el('div', { class: 'grid gap-3 sm:grid-cols-2' },
    modelRow('claude_coder', 'Claude for coding', o.models.claude_coder), modelRow('claude_fast', 'Claude for bug hunting & checks', o.models.claude_fast),
    modelRow('openai_coder', 'ChatGPT for coding', o.models.openai_coder), modelRow('openai_fast', 'ChatGPT for checks', o.models.openai_fast));
  container.replaceChildren(
    el('div', { class: 'text-xs font-semibold uppercase tracking-wider faint', text: 'Model combination (team)' }), el('div', { class: 'mt-2' }, teams),
    el('div', { class: 'mt-5 text-xs font-semibold uppercase tracking-wider faint', text: 'Effort' }), el('div', { class: 'mt-2' }, effort), blurb,
    models.children.length ? el('div', { class: 'mt-5 text-xs font-semibold uppercase tracking-wider faint', text: 'Cloud models' }) : '',
    models.children.length ? el('div', { class: 'mt-2' }, models) : el('p', { class: 'mt-4 text-xs muted', text: 'Add an Anthropic or OpenAI API key with the installer to choose cloud models.' }));
}
