'use strict';
/* Unit tests for static/ui-kit.js: escaping, delegation attributes and design-system class coverage. */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const root = path.resolve(__dirname, '..');
const sandbox = {};
vm.runInNewContext(fs.readFileSync(path.join(root, 'static/ui-kit.js'), 'utf8'), sandbox);
const UI = sandbox.UI;
const css = ['tokens.css', 'app.css', 'fingerprint.css'].map(f => fs.readFileSync(path.join(root, 'static', f), 'utf8')).join('\n').replace(/\/\*[\s\S]*?\*\//g, '');
const styled = new Set([...css.matchAll(/\.(-?[_a-zA-Z][\w-]*)/g)].map(m => m[1]));
const classesOf = html => [...html.matchAll(/class="([^"]*)"/g)].flatMap(m => m[1].split(/\s+/)).filter(Boolean);
let checks = 0;
const check = (name, fn) => { fn(); checks++; };

const evil = '<img src=x onerror=alert(1)>"\'&';
const samples = {
  icon: UI.icon('plus'),
  button: UI.button(evil, { variant: 'primary', action: 'openCreateForContext', args: [evil], iconName: 'download', title: evil }),
  textButton: UI.textButton(evil, { action: 'loadStorage' }),
  actionBar: UI.actionBar(UI.button('返回')),
  status: UI.status('running', evil),
  tag: UI.tag(evil, 'danger'),
  notice: UI.notice(evil, { tone: 'danger', title: evil }),
  signal: UI.signal({ title: evil, detail: evil, tone: 'warning' }),
  signalList: UI.signalList([{ title: 'a', tone: 'success' }]),
  badge: UI.badge(evil) + UI.badge('x', { hint: true }),
  recordItem: UI.recordItem({ id: evil, title: evil, state: 'finished', stateLabel: '已完成', typeLabel: '对照', compare: true, meta: [evil], progress: 140, selected: true, action: 'selectTask' }),
  recordEmpty: UI.recordEmpty(evil, evil),
  pageShell: UI.pageShell(''),
  pageHeader: UI.pageHeader({ kicker: evil, title: evil, description: evil, meta: [evil], statusLabel: evil, statusNote: evil, statusTone: 'success' }),
  tabs: UI.tabs({ label: evil, items: [['a', evil], ['b', 'B']], active: 'a', action: 'setComparisonTab', ownerId: evil }),
  section: UI.section({ title: evil, description: evil, badgeHtml: UI.badge('x'), bodyHtml: '' }),
  progressCard: UI.progressCard({ kicker: evil, value: -5, summary: evil, note: evil }),
  metric: UI.metric({ label: evil, value: null, unit: evil, note: evil, primary: true }),
  metrics: UI.metrics([{ label: 'a', value: 1 }]),
  methodCards: UI.methodCards([{ title: evil, lines: [evil] }]),
  steps: UI.steps([evil]),
  limitations: UI.limitations([evil]),
  table: UI.table({ columns: [{ label: evil }], rowsHtml: [] , empty: evil }),
  formSectionTitle: UI.formSectionTitle(1, evil),
  field: UI.field({ id: 'x', label: evil, controlHtml: UI.input({ id: 'x', value: evil, placeholder: evil }), hint: evil, required: true }),
  input: UI.input({ id: 'n', type: 'number', min: 1, max: 5, step: 1 }),
  select: UI.select({ id: 's', options: [['1', evil], ['2', 'b']], value: '2' }),
  inlineFields: UI.inlineFields(''),
  switchRow: UI.switchRow({ id: 'sw', title: evil, description: evil, checked: true }),
  hint: UI.hint(evil, { title: evil })
};

check('every exported builder has a sample', () => {
  const builders = Object.keys(UI).filter(k => typeof UI[k] === 'function' && k !== 'esc');
  assert.deepEqual(builders.filter(k => !(k in samples)), []);
});
check('text is escaped everywhere', () => {
  for (const [name, html] of Object.entries(samples)) assert.ok(!/<img|onerror=alert\(1\)>/.test(html.replace(/&lt;img src=x onerror=alert\(1\)&gt;/g, '')), `${name} leaks raw markup`);
  for (const [name, html] of Object.entries(samples)) for (const tag of html.match(/<[a-z][^>]*>/g) || []) assert.ok(!/\son\w+\s*=/.test(tag.replace(/"[^"]*"/g, '""')), `${name} emits an event handler attribute`);
});
check('every emitted class is styled by the design system', () => {
  const hooks = new Set(['report-table', 'comparison-record']); // semantic markers also allowlisted in tools/check-ui.cjs
  for (const [name, html] of Object.entries(samples)) for (const c of classesOf(html)) assert.ok(styled.has(c) || hooks.has(c), `${name} emits unstyled class ${c}`);
});
check('actions use explicit delegation attributes', () => {
  assert.match(samples.button, /data-click-action="openCreateForContext" data-arg0="&lt;img/);
  assert.match(samples.tabs, /data-arg0="&lt;img[^"]*" data-arg1="a"/);
  assert.throws(() => UI.button('x', { action: 'alert(1)' }), /invalid action/);
});
check('progress values are clamped and accessible', () => {
  assert.match(samples.recordItem, /width:100%/);
  assert.match(samples.progressCard, /aria-valuenow="0"/);
  assert.match(samples.progressCard, /role="progressbar"/);
});
check('semantic tones map to component variants and fall back safely', () => {
  assert.match(UI.tag('x', 'success'), /tag ok/);
  assert.match(UI.tag('x', 'unknown'), /tag muted/);
  assert.match(samples.notice, /notice risk" role="alert"/);
  assert.match(samples.pageHeader, /report-status good/);
  assert.match(samples.metric, /metric-primary/);
  assert.match(samples.metric, /—/);
});
check('tabs expose selection state', () => {
  assert.match(samples.tabs, /role="tablist"/);
  assert.match(samples.tabs, /report-tab active" role="tab" aria-selected="true"/);
});
check('unknown icons fail loudly', () => assert.throws(() => UI.icon('nope'), /unknown icon/));
check('builders emit only documented inline styles', () => {
  for (const [name, html] of Object.entries(samples)) for (const [, style] of html.matchAll(/style="([^"]*)"/g)) assert.match(style, /^width:\d+%$/, `${name} inline style ${style}`);
});

console.log(JSON.stringify({ status: 'passed', checks, builders: Object.keys(samples).length }));
