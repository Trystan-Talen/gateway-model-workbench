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
    connections: '<rect x="3" y="4" width="18" height="16" rx="2"/><path d="M3 9h18M7 14h4M7 17h7"/><circle cx="17" cy="14" r="1.5"/>',
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
    eyeOff: '<path d="m3 3 18 18M10.6 6.2A10.7 10.7 0 0 1 12 6c6 0 9.5 6 9.5 6a18 18 0 0 1-3.1 3.7M6.2 6.8C3.8 8.5 2.5 12 2.5 12s3.5 6 9.5 6c1.2 0 2.3-.2 3.3-.6"/>',
    arrowRight: '<path d="M5 12h14M13 6l6 6-6 6"/>'
  });

  const HTML_ENTITIES = Object.freeze({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' });
  function esc(value) {
    return String(value ?? '').replace(/[&<>"']/g, c => HTML_ENTITIES[c]);
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
  // variant: default | primary | danger · controls expand to touch targets on narrow screens.
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
    const foot = progress == null ? '' : `<div class="task-foot"><div class="mini-track"><i style="width:${value}%"></i></div><span class="task-pct">${esc(trailing || `${value}%`)}</span></div>`;
    return `<button class="${cls('task', compare && 'comparison-record', selected && 'sel')}" type="button"${actionAttrs(action, [id])}${selected ? ' aria-current="true"' : ''}><div class="task-top"><div class="task-name" title="${esc(title)}">${esc(title)}</div>${status(state, stateLabel)}</div><div class="task-meta">${metaHtml}</div>${foot}</button>`;
  }
  function recordEmpty(title, hint) { return `<div class="task-empty"><b>${esc(title)}</b><span>${esc(hint)}</span></div>`; }

  /* ---- Page scaffolding (detail area) ---- */
  // A detail page is: pageShell(pageHeader + tabs? + sections… + actionBar).
  function pageShell(childrenHtml) { return `<div class="detail-layout report-shell">${childrenHtml}</div>`; }
  const STATUS_CLASS = { neutral: '', accent: '', success: 'good', warning: 'caution', danger: 'risk' };
  function pageHeader({ kicker, title, description = '', meta = [], statusLabel = '', statusNote = '', statusTone = 'neutral', actionsHtml = '' }) {
    meta = meta.filter(item => item !== statusLabel);
    const statusHtml = statusLabel
      ? `<div class="${cls('report-status', STATUS_CLASS[tone(statusTone)])}">${esc(statusLabel)}${statusNote ? `<br><small>${esc(statusNote)}</small>` : ''}</div>`
      : '';
    const side = actionsHtml ? `<div class="hero-actions">${actionsHtml}${statusHtml}</div>` : statusHtml;
    return `<header class="report-head"><div><div class="report-kicker">${esc(kicker)}</div><h1>${esc(title)}</h1>${description ? `<p>${esc(description)}</p>` : ''}${meta.length ? `<div class="report-meta">${meta.map(x => `<span>${esc(x)}</span>`).join('')}</div>` : ''}</div>${side}</header>`;
  }
  // items: [[id, label]]; the page's action receives (ownerId, tabId). Arrow/Home/End keys are handled globally.
  function tabs({ label, items, active, action, ownerId }) {
    return `<nav class="report-tabs" role="tablist" aria-label="${esc(label)}">${items.map(([id, text]) => `<button class="${cls('report-tab', id === active && 'active')}" role="tab" aria-selected="${id === active}" tabindex="${id === active ? 0 : -1}" type="button"${actionAttrs(action, [ownerId, id])}>${esc(text)}</button>`).join('')}</nav>`;
  }
  function stack(childrenHtml) { return `<div class="report-stack">${childrenHtml}</div>`; }
  function section({ title, description = '', badgeHtml = '', bodyHtml = '', className = '' }) {
    return `<section class="${cls('report-section', className)}"><div class="report-section-title-row"><div><h2 class="report-section-title">${esc(title)}</h2>${description ? `<p class="report-section-sub">${esc(description)}</p>` : ''}</div>${badgeHtml}</div>${bodyHtml ? stack(bodyHtml) : ''}</section>`;
  }
  function disclosure({ title, bodyHtml = '', id = '', open = false, section: standalone = false }) {
    return `<details class="${standalone ? 'report-section report-readout-section' : 'suite-details'}"${id ? ` id="${esc(id)}"` : ''}${open ? ' open' : ''}><summary>${esc(title)}</summary><div class="disclosure-body report-stack">${bodyHtml}</div></details>`;
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

  // Display instants at fixed Singapore UTC+08:00, independent of browser or OS timezone.
  // Naive legacy dates remain unknown; an offset cannot be inferred safely.
  function formatTimestamp(value, missing = '未记录') {
    if (value == null || value === '' || typeof value === 'boolean') return missing;
    if (typeof value === 'string' && !/(?:Z|[+-]\d{2}:?\d{2})$/i.test(value)) return '时区未记录';
    const date = new Date(typeof value === 'number' && Math.abs(value) < 1e12 ? value * 1000 : value);
    if (!Number.isFinite(date.getTime())) return missing;
    const singapore = new Date(date.getTime() + 8 * 60 * 60 * 1000);
    if (!Number.isFinite(singapore.getTime())) return missing;
    return singapore.toISOString().replace('T', ' ').replace('Z', ' 新加坡时间（UTC+08:00）');
  }
  function formatDurationMs(value) {
    return value == null || value === '' || typeof value === 'boolean' || !Number.isFinite(Number(value)) || Number(value) < 0
      ? '未记录' : Number(value).toLocaleString('zh-CN', {maximumFractionDigits:3}) + ' 毫秒';
  }
  const timeExpanded = new Set();
  if (root.document?.addEventListener) root.document.addEventListener('toggle', event => {
    const node = event.target, key = node?.dataset?.timeKey;
    if (key && node.isConnected) node.open ? timeExpanded.add(key) : timeExpanded.delete(key);
  }, true);
  if (root.document?.addEventListener) root.document.addEventListener('click', event => {
    const summary=event.target.closest?.('.request-time-records > summary');
    if(!summary)return;
    root.requestAnimationFrame(()=>{
      const details=summary.parentElement,table=details.closest('.table-wrap');
      if(!details.isConnected||!details.open||!table)return;
      const cell=details.getBoundingClientRect(),viewport=table.getBoundingClientRect();
      // Reveal the evidence inside its table only; preserve the page's reading position.
      if(cell.right>viewport.right)table.scrollLeft+=cell.right-viewport.right+12;
      else if(cell.left<viewport.left)table.scrollLeft-=viewport.left-cell.left+12;
    });
  });
  // Only recorded instants are used. Never reconstruct an end from a start plus latency.
  function timeRecords(record = {}, {request = false, live, key = '', title = '时间记录'} = {}) {
    const timing = record.timing || {}, hasTiming = !!record.timing;
    const active = live ?? ['waiting','running','stopping','queued','collecting','calibrating','warming','awaiting_calibration','awaiting_run','draining'].includes(record.phase || record.status);
    const read = name => hasTiming ? timing[name] : record[name] ?? (name === 'created_at' ? record.created : name === 'ended_at' ? record.finished_at : null);
    const started = formatTimestamp(read('started_at'), active ? '尚未开始' : '未记录');
    const ended = formatTimestamp(read('ended_at'), active ? '尚未结束' : '未记录');
    const names = request ? [['scheduled_at','计划发送'],['enqueued_at','进入队列'],['started_at','实际开始'],['first_token_at','收到首字'],['ended_at','请求结束']]
      : [['created_at','任务创建'],['started_at','执行开始'],['stop_requested_at','请求停止'],['dispatch_ended_at','停止发新请求'],['ended_at','任务结束'],['interruption_detected_at','发现意外中断']];
    const moments = names.map(([name,label]) => label + '：' + (name === 'started_at' ? started : name === 'ended_at' ? ended : formatTimestamp(read(name))));
    const durations = (request ? [['queue_ms','排队等待'],['schedule_lag_ms','调度延迟'],['ttft_ms','首字耗时'],['total_ms','请求总耗时']] : [['queue_ms','任务等待'],['elapsed_ms','执行耗时'],['drain_ms','收尾耗时']])
      .map(([name,label]) => label + '：' + formatDurationMs(hasTiming ? timing[name] : record[name]));
    const source = timing.source === 'recorded' ? '已记录' : timing.source === 'partial' ? '部分已记录' : '历史记录；缺失时间不推算';
    const attempts = request && Array.isArray(record.attempts) && record.attempts.length > 1 ? record.attempts : [];
    const summary = request ? '查看时间记录' + (attempts.length ? ` · ${attempts.length} 次实际发送` : '') : title + ' · 新加坡时间（UTC+08:00）';
    const content = methodCards([{title:(attempts.length ? '最终尝试时间' : '客户端时间') + ' · 新加坡时间（UTC+08:00）',lines:moments},{title:'耗时与来源',lines:[...durations,source,'时间点来自运行服务的系统时钟；耗时来自单调时钟。历史字段以原始记录为限。']}]) + (attempts.length ?
      hint(`该逻辑请求包含 ${attempts.length} 次实际 HTTP（网络请求）尝试；下方逐次列出，不能视为只发送一次。网关内部重试仍不可见。`) +
      methodCards(attempts.map((attempt,index) => {
        const item = attempt || {}, at = item.timing || {};
        return {title:`第 ${item.attempt_index ?? index + 1} 次实际发送`,lines:[
          '状态码：' + (item.status_code ?? '未记录'), '错误分类：' + (item.error_type || '未记录'),
          '实际开始：' + formatTimestamp(at.started_at), '收到首字：' + formatTimestamp(at.first_token_at),
          '请求结束：' + formatTimestamp(at.ended_at), '首字耗时：' + formatDurationMs(at.ttft_ms),
          '请求总耗时：' + formatDurationMs(at.total_ms)
        ]};
      })) : '');
    return `<details class="gateway-single-advanced time-records${request ? ' request-time-records' : ''}"${key ? ` id="time-${esc(key)}" data-time-key="${esc(key)}"` : ''}${key && timeExpanded.has(key) ? ' open' : ''}><summary>${esc(summary)}</summary>${content}</details>`;
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
  function passwordInput({ id, action = 'togglePasswordVisibility', args = [id], visibilityLabel = '密码', ...options }) {
    const label = `显示${visibilityLabel}`;
    return `<div class="password-field">${input({ ...options, id, type: 'password' })}<button class="password-toggle" type="button"${actionAttrs(action, args)} aria-controls="${esc(id)}" aria-pressed="false" aria-label="${esc(label)}" title="${esc(label)}">${icon('eye')}</button></div>`;
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
    pageShell, pageHeader, tabs, stack, section, disclosure, progressCard,
    metric, metrics, methodCards, steps, limitations, table, formatTimestamp, formatDurationMs, timeRecords,
    formSectionTitle, field, input, passwordInput, select, inlineFields, switchRow, hint
  });
})(typeof window !== 'undefined' ? window : globalThis);
