/* Instrument UI kit — markup builders for the shared components in docs/design-system.md.
 *
 * Use these for every new module, page, drawer section or dialog instead of hand-writing markup,
 * so class names, structure and accessibility attributes stay consistent.
 *
 * Conventions
 *   - Every builder returns an HTML string (the app renders with innerHTML).
 *   - Plain text arguments are escaped. Parameters whose name ends in `Html` accept trusted markup,
 *     normally the output of other UI builders; never pass user or upstream data through them unescaped.
 *   - Interactions use explicit delegation only: `action` becomes data-click-action and must be
 *     registered in static/actions.js (UI_ACTIONS). Inline handlers are rejected by tools/check-ui.cjs.
 *   - Tones are semantic: neutral | accent | success | warning | danger. Never pick colours directly.
 */
'use strict';
(function (root) {
  const TONES = ['neutral', 'accent', 'success', 'warning', 'danger'];
  const ICONS = Object.freeze({
    gateway: '<path d="M4 19V5M4 19h16M8 14l4-5 4 3 4-7"/>',
    routing: '<path d="M4 7h6l4 10h6"/><path d="M4 17h4l3-6"/><circle cx="18" cy="17" r="2"/>',
    quality: '<path d="M12 3l8 3v6c0 5-8 9-8 9s-8-4-8-9V6l8-3Z"/><path d="m8 12 3 3 5-6"/>',
    fingerprint: '<path d="M4 11a8 8 0 0 1 16 0v3M7 20c2-3 2-5 2-9a3 3 0 0 1 6 0c0 5-1 8-3 11M4 16c1-2 1-3 1-5M12 11c0 4 0 5-1 7M17 20c1-3 1-6 1-9a6 6 0 0 0-12 0"/>',
    plus: '<path d="M12 5v14M5 12h14"/>',
    close: '<path d="m6 6 12 12M6 18 18 6"/>',
    check: '<path d="m5 12.5 4.5 4.5L19 7.5"/>',
    alert: '<path d="M12 3.5 2.5 20h19L12 3.5Z"/><path d="M12 10v4.5M12 17.5v.01"/>',
    info: '<circle cx="12" cy="12" r="9"/><path d="M12 11v5.5M12 7.5v.01"/>',
    download: '<path d="M12 4v11M7 10.5l5 5 5-5M5 20h14"/>',
    refresh: '<path d="M20 11a8 8 0 0 0-14.6-4.5L4 8M4 4v4h4M4 13a8 8 0 0 0 14.6 4.5L20 16M20 20v-4h-4"/>',
    stop: '<rect x="6.5" y="6.5" width="11" height="11" rx="2"/>',
    trash: '<path d="M4.5 7h15M10 11v6M14 11v6M6.5 7l1 12.5h9l1-12.5M9.5 7V4.5h5V7"/>',
    activity: '<path d="M3 12h4l3-8 4 16 3-8h4"/>',
    database: '<ellipse cx="12" cy="6" rx="7.5" ry="3"/><path d="M4.5 6v6c0 1.7 3.4 3 7.5 3s7.5-1.3 7.5-3V6M4.5 12v6c0 1.7 3.4 3 7.5 3s7.5-1.3 7.5-3v-6"/>',
    eye: '<path d="M2.5 12s3.5-6 9.5-6 9.5 6 9.5 6-3.5 6-9.5 6-9.5-6-9.5-6Z"/><circle cx="12" cy="12" r="2.5"/>',
    arrowRight: '<path d="M5 12h14M13 6l6 6-6 6"/>'
  });

  function esc(value) {
    return String(value ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  }
  function tone(value, fallback = 'neutral') { return TONES.includes(value) ? value : fallback; }
  function pct(value) { const n = Number(value); return Number.isFinite(n) ? Math.round(Math.max(0, Math.min(100, n))) : 0; }
  function cls(...names) { return names.filter(Boolean).join(' '); }
  function actionAttrs(action, args = []) {
    if (!action) return '';
    if (!/^[A-Za-z][\w]*$/.test(action)) throw new Error(`UI: invalid action name "${action}"`);
    return ` data-click-action="${esc(action)}"` + args.map((value, index) => ` data-arg${index}="${esc(value)}"`).join('');
  }

  /* ---- Foundations ---- */
  function icon(name) {
    const paths = ICONS[name];
    if (!paths) throw new Error(`UI: unknown icon "${name}". Add it to ICONS in static/ui-kit.js.`);
    return `<svg class="ui-icon" viewBox="0 0 24 24" aria-hidden="true">${paths}</svg>`;
  }

  /* ---- Actions ---- */
  // variant: default | primary | danger · size: md (34px) | lg (42px, record panel create button)
  function button(label, { variant = 'default', action = '', args = [], iconName = '', disabled = false, title = '', type = 'button' } = {}) {
    const variantClass = { default: '', primary: 'primary', danger: 'danger' }[variant] ?? '';
    return `<button class="${cls('btn', variantClass)}" type="${esc(type)}"${actionAttrs(action, args)}${title ? ` title="${esc(title)}"` : ''}${disabled ? ' disabled' : ''}>${iconName ? icon(iconName) : ''}<span>${esc(label)}</span></button>`;
  }
  function textButton(label, { action = '', args = [] } = {}) {
    return `<button class="text-btn" type="button"${actionAttrs(action, args)}>${esc(label)}</button>`;
  }
  function actionBar(buttonsHtml) {
    return `<div class="report-section report-footer-actions">${buttonsHtml}</div>`;
  }

  /* ---- Status & feedback ---- */
  // `state` is a run lifecycle key (running, finished, error, waiting, stopped, finished_with_errors…).
  function status(state, label) { return `<span class="status ${esc(state)}">${esc(label)}</span>`; }
  const TAG_CLASS = { neutral: 'muted', accent: 'protocol', success: 'ok', warning: 'timeout', danger: 'error' };
  function tag(label, toneName = 'neutral') { return `<span class="tag ${TAG_CLASS[tone(toneName)]}">${esc(label)}</span>`; }
  const NOTICE_CLASS = { neutral: '', accent: '', success: 'good', warning: 'caution', danger: 'risk' };
  function notice(text, { tone: toneName = 'neutral', title = '' } = {}) {
    return `<div class="${cls('notice', NOTICE_CLASS[tone(toneName)])}"${tone(toneName) === 'danger' ? ' role="alert"' : ''}>${title ? `<b>${esc(title)}</b>` : ''}<span>${esc(text)}</span></div>`;
  }
  const SIGNAL_CLASS = { neutral: '', accent: '', success: 'good', warning: 'caution', danger: 'risk' };
  function signal({ title, detail = '', tone: toneName = 'neutral' }) {
    return `<div class="${cls('signal', SIGNAL_CLASS[tone(toneName)])}"><i class="signal-dot"></i><div><b>${esc(title)}</b>${detail ? `<br>${esc(detail)}` : ''}</div></div>`;
  }
  function signalList(items) { return `<div class="signal-list">${items.map(signal).join('')}</div>`; }
  function badge(label, { hint = false } = {}) { return `<span class="${hint ? 'table-hint' : 'evidence-badge'}">${esc(label)}</span>`; }

  /* ---- Records (middle panel) ---- */
  function recordItem({ id, title, state, stateLabel, typeLabel = '', compare = false, meta = [], progress = 0, trailing = '', selected = false, action }) {
    const value = pct(progress);
    const metaHtml = [typeLabel ? `<span class="${cls('record-type', compare && 'comparison-type')}">${esc(typeLabel)}</span>` : '', ...meta.map(esc)].filter(Boolean).join(' · ');
    return `<button class="${cls('task', compare && 'comparison-record', selected && 'sel')}" type="button"${actionAttrs(action, [id])}><div class="task-top"><div class="task-name">${esc(title)}</div>${status(state, stateLabel)}</div><div class="task-meta">${metaHtml}</div><div class="task-foot"><div class="mini-track"><i style="width:${value}%"></i></div><span class="task-pct">${esc(trailing || `${value}%`)}</span></div></button>`;
  }
  function recordEmpty(title, hint) { return `<div class="task-empty"><b>${esc(title)}</b><span>${esc(hint)}</span></div>`; }

  /* ---- Page scaffolding (detail area) ---- */
  // A detail page is: pageShell(pageHeader + tabs? + sections… + actionBar).
  function pageShell(childrenHtml) { return `<div class="detail-layout report-shell">${childrenHtml}</div>`; }
  const STATUS_CLASS = { neutral: '', accent: '', success: 'good', warning: 'caution', danger: 'risk' };
  function pageHeader({ kicker, title, description = '', meta = [], statusLabel = '', statusNote = '', statusTone = 'neutral', actionsHtml = '' }) {
    const side = statusLabel
      ? `<div class="${cls('report-status', STATUS_CLASS[tone(statusTone)])}">${esc(statusLabel)}${statusNote ? `<br><small>${esc(statusNote)}</small>` : ''}</div>`
      : actionsHtml ? `<div class="hero-actions">${actionsHtml}</div>` : '';
    return `<header class="report-head"><div><div class="report-kicker">${esc(kicker)}</div><h1>${esc(title)}</h1>${description ? `<p>${esc(description)}</p>` : ''}${meta.length ? `<div class="report-meta">${meta.map(x => `<span>${esc(x)}</span>`).join('')}</div>` : ''}</div>${side}</header>`;
  }
  // items: [[id, label]]; the page's action receives (ownerId, tabId). Arrow/Home/End keys are handled globally.
  function tabs({ label, items, active, action, ownerId }) {
    return `<nav class="report-tabs" role="tablist" aria-label="${esc(label)}">${items.map(([id, text]) => `<button class="${cls('report-tab', id === active && 'active')}" role="tab" aria-selected="${id === active}" type="button"${actionAttrs(action, [ownerId, id])}>${esc(text)}</button>`).join('')}</nav>`;
  }
  function section({ title, description = '', badgeHtml = '', bodyHtml = '', className = '' }) {
    return `<section class="${cls('report-section', className)}"><div class="report-section-title-row"><div><h2 class="report-section-title">${esc(title)}</h2>${description ? `<p class="report-section-sub">${esc(description)}</p>` : ''}</div>${badgeHtml}</div>${bodyHtml}</section>`;
  }
  function progressCard({ kicker, value, summary = '', note = '', label = kicker }) {
    const v = pct(value);
    return `<section class="report-progress-card"><div class="report-card-kicker">${esc(kicker)}</div><div class="report-progress-line"><strong>${v}%</strong><span>${esc(summary)}</span></div><div class="report-progress-track" role="progressbar" aria-label="${esc(label)}" aria-valuemin="0" aria-valuemax="100" aria-valuenow="${v}"><i style="width:${v}%"></i></div>${note ? `<p>${esc(note)}</p>` : ''}</section>`;
  }

  /* ---- Data display ---- */
  function metric({ label, value, unit = '', note = '', primary = false }) {
    return `<div class="${cls('report-metric', primary && 'metric-primary')}"><div class="metric-label">${esc(label)}</div><div class="metric-value">${esc(value ?? '—')}${unit ? `<small>${esc(unit)}</small>` : ''}</div>${note ? `<div class="metric-note">${esc(note)}</div>` : ''}</div>`;
  }
  function metrics(items) { return `<div class="report-metrics">${items.map(metric).join('')}</div>`; }
  function methodCards(items) {
    return `<div class="method-grid">${items.map(({ title, lines = [] }) => `<div class="method-card"><b>${esc(title)}</b>${lines.map(line => `<span>${esc(line)}</span>`).join('')}</div>`).join('')}</div>`;
  }
  function steps(items) {
    return `<div class="next-step-grid">${items.map((text, index) => `<div class="next-step"><span>${index + 1}</span><p>${esc(text)}</p></div>`).join('')}</div>`;
  }
  function limitations(items) { return `<ul class="report-limitations">${items.map(x => `<li>${esc(x)}</li>`).join('')}</ul>`; }
  // columns: [{ label }]; rows: arrays of cell HTML (escape text with UI.esc or use UI.tag/status).
  function table({ columns, rowsHtml, empty = '暂无数据' }) {
    const body = rowsHtml.length
      ? rowsHtml.map(cells => `<tr>${cells.map(cell => `<td>${cell}</td>`).join('')}</tr>`).join('')
      : `<tr><td colspan="${columns.length}">${esc(empty)}</td></tr>`;
    return `<div class="table-wrap report-wrap"><table class="report-table"><thead><tr>${columns.map(c => `<th>${esc(c.label)}</th>`).join('')}</tr></thead><tbody>${body}</tbody></table></div>`;
  }

  /* ---- Forms (creation drawer / dialogs) ---- */
  function formSectionTitle(index, text) { return `<h3 class="form-section-title"><i>${esc(index)}</i><span>${esc(text)}</span></h3>`; }
  function field({ id, label, controlHtml, hint = '', required = false, hintId = '' }) {
    return `<div class="field"><label for="${esc(id)}">${esc(label)}${required ? ' <span class="field-required">必填</span>' : ''}</label>${controlHtml}${hint ? `<div class="field-hint"${hintId ? ` id="${esc(hintId)}"` : ''}>${esc(hint)}</div>` : ''}</div>`;
  }
  function input({ id, type = 'text', value = '', placeholder = '', required = false, min, max, step, describedBy = '', autocomplete = 'off' }) {
    const extra = [min != null && ` min="${esc(min)}"`, max != null && ` max="${esc(max)}"`, step != null && ` step="${esc(step)}"`, describedBy && ` aria-describedby="${esc(describedBy)}"`].filter(Boolean).join('');
    return `<input class="input" id="${esc(id)}" type="${esc(type)}" value="${esc(value)}" placeholder="${esc(placeholder)}" autocomplete="${esc(autocomplete)}"${required ? ' required' : ''}${extra}>`;
  }
  function select({ id, options, value = '', required = false }) {
    return `<select class="input" id="${esc(id)}"${required ? ' required' : ''}>${options.map(([v, text]) => `<option value="${esc(v)}"${String(v) === String(value) ? ' selected' : ''}>${esc(text)}</option>`).join('')}</select>`;
  }
  function inlineFields(fieldsHtml) { return `<div class="inline">${fieldsHtml}</div>`; }
  function switchRow({ id, title, description = '', checked = false }) {
    return `<div class="adv-toggle"><div class="switch-copy"><div class="switch-title">${esc(title)}</div>${description ? `<div class="switch-txt">${esc(description)}</div>` : ''}</div><label class="switch"><input type="checkbox" id="${esc(id)}" aria-label="${esc(title)}"${checked ? ' checked' : ''}><span class="slider"></span></label></div>`;
  }
  function hint(text, { title = '' } = {}) { return `<div class="comparison-hint">${title ? `<b>${esc(title)}</b>` : ''}<span>${esc(text)}</span></div>`; }

  root.UI = Object.freeze({
    TONES, ICONS, esc, icon,
    button, textButton, actionBar,
    status, tag, notice, signal, signalList, badge,
    recordItem, recordEmpty,
    pageShell, pageHeader, tabs, section, progressCard,
    metric, metrics, methodCards, steps, limitations, table,
    formSectionTitle, field, input, select, inlineFields, switchRow, hint
  });
})(typeof window !== 'undefined' ? window : globalThis);
