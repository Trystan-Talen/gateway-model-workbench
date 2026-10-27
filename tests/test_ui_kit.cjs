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
  formatTimestamp: UI.formatTimestamp('2026-10-15T15:59:59.999Z'),
  formatDurationMs: UI.formatDurationMs(0),
  timeRecords: UI.timeRecords({status:'running',timing:{source:'recorded',started_at:'2026-10-15T15:59:59.999Z',total_ms:0}},{key:evil}),
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
  pageHeader: UI.pageHeader({ kicker: evil, title: evil, description: evil, meta: [evil], statusLabel: evil, statusNote: evil, statusTone: 'success', actionsHtml: UI.button('再来一次', { action:'repeatTest', args:['task',evil], iconName:'refresh' }) }),
  tabs: UI.tabs({ label: evil, items: [['a', evil], ['b', 'B']], active: 'a', action: 'setComparisonTab', ownerId: evil }),
  stack: UI.stack(UI.notice('第一项') + UI.notice('第二项')),
  section: UI.section({ title: evil, description: evil, badgeHtml: UI.badge('x'), bodyHtml: '' }),
  disclosure: UI.disclosure({ title: evil, id: evil, bodyHtml: UI.notice('展开内容'), section: true }),
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
  passwordInput: UI.passwordInput({ id: 'key', value: evil, visibilityLabel: evil, action: 'revealRoutingKey', args: [evil], describedBy: 'key-hint' }),
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
check('static records do not imply completed work and selected records are announced', () => {
  const record = UI.recordItem({id:'connection',title:evil,state:'finished',stateLabel:'已保存',progress:null,selected:true,action:'selectConnection'});
  assert.ok(!record.includes('mini-track') && !record.includes('100%'));
  assert.match(record,/aria-current="true"/);
  assert.match(record,/class="task-name" title="&lt;img/);
});
check('secondary text retains readable contrast on the canvas and card surfaces', () => {
  const source=fs.readFileSync(path.join(root,'static/tokens.css'),'utf8');
  const luminance=hex=>[0,2,4].map(i=>parseInt(hex.slice(i,i+2),16)/255).map(x=>x<=.04045?x/12.92:((x+.055)/1.055)**2.4).reduce((v,x,i)=>v+x*[.2126,.7152,.0722][i],0);
  for(const block of [source.match(/:root\s*\{([^}]+)/)[1],source.match(/:root\[data-theme="dark"\]\s*\{([^}]+)/)[1]]){
    const color=name=>luminance(block.match(new RegExp('--color-'+name+':\\s*#([0-9a-f]{6})'))[1]);
    for(const surface of ['surface','surface-2','canvas']){
      const [light,dark]=[color('muted'),color(surface)].sort((a,b)=>b-a);
      assert.ok((light+.05)/(dark+.05)>=4.5,`secondary text contrast on ${surface}`);
    }
  }
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
check('page headers show one visible repeat action together with the conclusion status', () => {
  assert.match(samples.pageHeader, /class="hero-actions"/);
  assert.equal((samples.pageHeader.match(/data-click-action="repeatTest"/g)||[]).length,1);
  assert.ok(samples.pageHeader.indexOf('data-click-action="repeatTest"')<samples.pageHeader.indexOf('report-status good'));
  assert.match(UI.pageHeader({kicker:'测试',title:'已结束',actionsHtml:UI.button('再来一次')}),/hero-actions/);
  assert.ok(!UI.pageHeader({kicker:'测试',title:'运行中',statusLabel:'运行中'}).includes('hero-actions'));
});
check('unknown icons fail loudly', () => assert.throws(() => UI.icon('nope'), /unknown icon/));
check('named connections use the shared accessible navigation icon', () => {
  assert.match(UI.icon('connections'), /class="ui-icon" viewBox="0 0 24 24" aria-hidden="true"/);
  assert.match(UI.button('管理常用连接', { iconName:'connections', action:'manageSavedConnections' }), /data-click-action="manageSavedConnections"/);
});
check('password inputs are concealed with accessible delegated visibility controls', () => {
  assert.match(samples.passwordInput, /class="password-field"/);
  assert.match(samples.passwordInput, /type="password"/);
  assert.match(samples.passwordInput, /aria-describedby="key-hint"/);
  assert.match(samples.passwordInput, /aria-controls="key" aria-pressed="false"/);
  assert.match(samples.passwordInput, /data-click-action="revealRoutingKey" data-arg0="&lt;img/);
  assert.match(UI.passwordInput({ id: 'pw', type: 'text' }), /type="password"/);
  assert.match(UI.passwordInput({ id: 'pw' }), /data-click-action="togglePasswordVisibility" data-arg0="pw"/);
  assert.match(UI.icon('eyeOff'), /m3 3 18 18/);
});
check('builders emit only documented inline styles', () => {
  for (const [name, html] of Object.entries(samples)) for (const [, style] of html.matchAll(/style="([^"]*)"/g)) assert.match(style, /^width:\d+%$/, `${name} inline style ${style}`);
});


check('timestamps convert actual instants into fixed Singapore time across midnight',()=>{
  const before='2026-10-15 23:59:59.999 新加坡时间（UTC+08:00）',after='2026-10-16 00:00:00.001 新加坡时间（UTC+08:00）';
  assert.equal(UI.formatTimestamp('2026-10-15T15:59:59.999Z'),before);
  assert.equal(UI.formatTimestamp('2026-10-15T16:00:00.001Z'),after);
  assert.equal(UI.formatTimestamp('2026-10-16T00:00:00.001+08:00'),after);
  assert.equal(UI.formatTimestamp('2026-10-15T05:59:59.999-10:00'),before);
  assert.equal(UI.formatTimestamp('2026-10-16T01:00:00.001+09:00'),after);
  const milliseconds=Date.parse('2026-10-15T16:00:00.001Z');
  assert.equal(UI.formatTimestamp(milliseconds),after);
  assert.equal(UI.formatTimestamp(milliseconds/1000),after);
  assert.equal(UI.formatTimestamp(0),'1970-01-01 08:00:00.000 新加坡时间（UTC+08:00）');
  assert.equal(UI.formatTimestamp(null),'未记录');
  assert.equal(UI.formatTimestamp('2026-10-15 12:00:00'),'时区未记录');
  assert.equal(UI.formatTimestamp('invalidZ'),'未记录');
  assert.equal(UI.formatDurationMs(0),'0 毫秒');
  assert.equal(UI.formatDurationMs(null),'未记录');
  assert.equal(UI.formatDurationMs(false),'未记录');
});
check('timelines separate unrecorded, running, interrupted and never fabricate an end',()=>{
  const legacy={started_at:'2026-10-15T15:59:59.999Z',total_ms:1};
  assert.match(UI.timeRecords(legacy,{request:true}),/请求结束：未记录/);
  assert.match(UI.timeRecords({...legacy,status:'running'}),/任务结束：尚未结束/);
  assert.match(UI.timeRecords({...legacy,status:'interrupted'}),/任务结束：未记录/);
  assert.match(UI.timeRecords({timing:{source:'recorded',queue_ms:0}}),/任务等待：0 毫秒/);
  assert.doesNotMatch(UI.timeRecords({started_at:'2026-10-15T15:59:59.999Z',timing:{source:'partial',started_at:null}}),/23:59/);
});

check('multiple physical attempts remain separately traceable and single sends stay compact',()=>{
  const attempts=[
    {attempt_index:1,status_code:400,error_type:'unsupported_parameter',timing:{started_at:'2026-10-15T15:59:58.100Z',first_token_at:null,ended_at:'2026-10-15T15:59:58.200Z',total_ms:100}},
    {attempt_index:2,status_code:200,error_type:null,timing:{started_at:'2026-10-15T15:59:59.999Z',first_token_at:'2026-10-15T16:00:00.000Z',ended_at:'2026-10-15T16:00:00.001Z',ttft_ms:0,total_ms:2}}
  ];
  const multi=UI.timeRecords({timing:attempts[1].timing,attempts},{request:true});
  assert.match(multi,/查看时间记录 · 2 次实际发送/);
  assert.match(multi,/最终尝试时间/);
  assert.match(multi,/第 1 次实际发送/);assert.match(multi,/状态码：400/);
  assert.match(multi,/23:59:58.100 新加坡时间/);assert.match(multi,/23:59:58.200 新加坡时间/);
  assert.match(multi,/错误分类：unsupported_parameter/);assert.match(multi,/收到首字：未记录/);
  assert.match(multi,/第 2 次实际发送/);assert.match(multi,/状态码：200/);
  assert.match(multi,/00:00:00.001 新加坡时间/);assert.match(multi,/首字耗时：0 毫秒/);
  assert.match(multi,/请求总耗时：100 毫秒/);assert.match(multi,/请求总耗时：2 毫秒/);
  assert.doesNotMatch(UI.timeRecords({attempts:[attempts[0]]},{request:true}),/次实际发送|最终尝试时间/);
  const escaped=UI.timeRecords({attempts:[{attempt_index:evil,status_code:evil,error_type:evil},{}]},{request:true});
  assert.doesNotMatch(escaped,/<img/);assert.match(escaped,/&lt;img/);
});

console.log(JSON.stringify({ status: 'passed', checks, builders: Object.keys(samples).length }));
