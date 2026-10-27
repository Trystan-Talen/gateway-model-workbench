/* Repeat a completed record from its frozen server configuration. Secrets only live in the open form. */
(() => {
  'use strict';
  const UI = window.UI, $ = id => document.getElementById(id);
  const KINDS = ['task','comparison','routing','quality','fingerprint'];
  const ENDED = ['finished','finished_with_errors','stopped','error','expired','interrupted'];
  const s = {initialized:false,epoch:0,operations:new Map(),form:null};
  const signedIn = () => typeof authenticated === 'undefined' || authenticated;
  const keyOf = (kind,id) => `${kind}:${id}`;

  function button(kind,record) {
    if (!KINDS.includes(kind) || !record?.id || !ENDED.includes(record.status)) return '';
    const operation = s.operations.get(keyOf(kind,record.id));
    return UI.button(operation ? operation.phase === 'confirm' ? '等待确认…' : '创建中…' : '再来一次', {
      action:'repeatTest',args:[kind,record.id],iconName:'refresh',disabled:Boolean(operation),
      title:'沿用这条记录的测试参数，创建一条新的测试记录'
    });
  }
  function updateButtons() {
    for (const node of document.querySelectorAll('[data-click-action="repeatTest"]')) {
      const operation = s.operations.get(keyOf(node.dataset.arg0,node.dataset.arg1));
      node.disabled = Boolean(operation);
      const label = node.querySelector('span');
      if (label) label.textContent = operation ? operation.phase === 'confirm' ? '等待确认…' : '创建中…' : '再来一次';
    }
  }
  function init() {
    if (s.initialized) return;
    document.body.insertAdjacentHTML('beforeend', `<div class="modal" id="repeatModal" hidden><div class="modal-backdrop" data-click-action="closeRepeat"></div><section class="modal-panel" role="dialog" aria-modal="true" aria-labelledby="repeatTitle"><header class="modal-head"><div><div class="modal-kicker">沿用原参数 · 创建新记录</div><h2 id="repeatTitle">再来一次</h2></div><button class="modal-close" type="button" data-click-action="closeRepeat" aria-label="关闭再次测试">${UI.icon('close')}</button></header><div class="modal-body" id="repeatBody" tabindex="0"></div><footer class="modal-footer" id="repeatFooter"></footer></section></div>`);
    s.initialized = true;
  }
  function current(operation) { return signedIn() && operation.epoch === s.epoch && s.operations.get(operation.key) === operation; }
  function finish(operation) {
    if (s.operations.get(operation.key) === operation) s.operations.delete(operation.key);
    updateButtons();
  }
  function status(message,error = false) {
    const node = $('repeatStatus');
    if (node) { node.className = `form-status${error ? ' error' : ''}`; node.textContent = message; }
  }
  function formHtml(data) {
    const connections = Array.isArray(data.connections) ? data.connections : [];
    const targets = connections.length ? UI.methodCards(connections.map(connection => ({title:connection.label || '测试目标',lines:[`模型：${connection.model || '未记录'}`,`请求地址：${connection.url || '未记录'}`,connection.saved ? '复用已保存密钥' : connection.optional ? '原记录无需密钥' : '需要填写密钥']}))) : UI.notice('原记录未保存目标信息。',{tone:'warning'});
    const missing = connections.map((connection,index) => ({connection,index})).filter(item => !item.connection.saved && !item.connection.optional);
    const fields = missing.map(({connection,index}) => UI.field({id:`repeatKey${index}`,label:`${connection.label || '测试目标'} · API Key（接口密钥）`,required:true,
      controlHtml:UI.passwordInput({id:`repeatKey${index}`,required:true,visibilityLabel:'API Key（接口密钥）',placeholder:'填写此次测试使用的密钥'}),
      hint:[connection.issue,'密钥仅用于此次创建，关闭窗口后清空。'].filter(Boolean).join(' ')
    })).join('');
    const preflight = data.preflight || {};
    const risk = preflight.requires_confirmation ? UI.switchRow({id:'repeatRiskAck',title:data.source_kind === 'fingerprint' ? '我已确认本次采集的请求数与费用' : '我已确认目标环境及本次请求负载',
      description:'这次测试会重新向上游发送真实请求，可能计入费用；确认只适用于本次创建。',checked:false}) : '';
    const preflightClass = {ok:'preflight-card level-ok',warn:'preflight-card level-warn',risk:'preflight-card level-risk'}[preflight.level] || 'preflight-card';
    const preflightBody = typeof preflightHtml === 'function' ? `<div class="${preflightClass}">${preflightHtml(preflight)}</div>` : '';
    return UI.notice(`将沿用「${data.name || '原测试'}」的参数创建新记录。原记录和结果保留。`)
      + UI.formSectionTitle(1,'核对测试目标') + targets
      + (missing.length ? UI.formSectionTitle(2,'补充接口密钥') + fields : '')
      + UI.formSectionTitle(missing.length ? 3 : 2,'确认本次负载') + preflightBody + risk
      + '<div class="form-status" id="repeatStatus" role="status" aria-live="polite"></div>';
  }
  function showForm(operation,data) {
    init(); const body = formHtml(data); operation.phase = 'confirm'; s.form = {operation,data};
    $('repeatBody').innerHTML = body; $('repeatFooter').innerHTML = UI.button('取消',{action:'closeRepeat'}) + UI.button('创建并开始',{variant:'primary',action:'submitRepeat'}); openModal('repeatModal'); updateButtons();
    const first = data.connections?.findIndex(connection => !connection.saved && !connection.optional);
    if (first >= 0) $(`repeatKey${first}`)?.focus({preventScroll:true});
  }
  async function start(kind,id) {
    if (!signedIn() || !KINDS.includes(kind) || !id) return;
    const key = keyOf(kind,id);
    if (s.operations.has(key)) return;
    if (s.form || [...s.operations.values()].some(item => item.phase === 'prepare')) { toast('请先完成当前的再次测试准备，或关闭已打开的窗口。'); return; }
    const operation = {key,kind,id,epoch:s.epoch,phase:'prepare',context:typeof repeatContext === 'function' ? repeatContext() : null};
    s.operations.set(key,operation); updateButtons();
    try {
      const data = await api('/repeats/prepare',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({source_kind:kind,source_id:id}),timeout:20000});
      if (!current(operation)) return;
      if (data.source_kind !== kind || data.source_id !== id) throw new Error('再次测试参数与来源记录不一致，请刷新后重试。');
      if ((data.connections || []).some(connection => !connection.saved && !connection.optional) || data.preflight?.requires_confirmation) showForm(operation,data);
      else await create(operation,{});
    } catch (error) {
      if (current(operation)) { finish(operation); toast(error.message || '读取原测试参数失败'); }
    }
  }
  async function create(operation,keys,riskAcknowledged = false) {
    if (!current(operation) || operation.phase === 'creating') return;
    operation.phase = 'creating'; updateButtons();
    if (s.form?.operation === operation) {
      status('正在创建新测试；关闭窗口不会取消已提交的创建请求。');
      for (const node of $('repeatFooter').querySelectorAll('[data-click-action="submitRepeat"]')) { node.disabled = true; const label = node.querySelector('span'); if (label) label.textContent = '正在创建…'; }
      for (const node of $('repeatFooter').querySelectorAll('[data-click-action="closeRepeat"]')) { const label = node.querySelector('span'); if (label) label.textContent = '关闭窗口'; }
    }
    try {
      const body = {source_kind:operation.kind,source_id:operation.id};
      if (Object.keys(keys).length) body.keys = keys;
      if (riskAcknowledged) body.risk_acknowledged = true;
      const result = await api('/repeats',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body),timeout:30000});
      if (!current(operation)) return;
      const id = result.task_id || result.comparison_id || result.quality_test_id || result.routing_comparison_id || result.fingerprint_id;
      if (result.ok !== true || !id) throw new Error('服务未返回新测试记录，请刷新记录后检查。');
      operation.completed = true;
      if (s.form?.operation === operation) close();
      finish(operation); toast('已沿用原参数创建新测试，原记录已保留。');
      if (typeof repeatCreated === 'function') await repeatCreated(operation.kind,operation.id,id,operation.dismissed ? null : operation.context);
    } catch (error) {
      if (!current(operation)) return;
      if (operation.dismissed) { finish(operation); return; }
      if (s.form?.operation === operation) {
        operation.phase = 'confirm'; status(error.message || '创建失败，请检查后重试。',true);
        for (const node of $('repeatFooter').querySelectorAll('[data-click-action="submitRepeat"]')) { node.disabled = false; const label = node.querySelector('span'); if (label) label.textContent = '创建并开始'; }
        for (const node of $('repeatFooter').querySelectorAll('[data-click-action="closeRepeat"]')) { const label = node.querySelector('span'); if (label) label.textContent = '取消'; }
        updateButtons();
      } else { finish(operation); toast(error.message || '创建再次测试失败'); }
    } finally { for (const slot of Object.keys(keys)) keys[slot] = ''; }
  }
  async function submit() {
    const form = s.form;
    if (!form || !current(form.operation) || form.operation.phase === 'creating') return;
    const keys = Object.create(null);
    for (const [index,connection] of (form.data.connections || []).entries()) {
      if (connection.saved || connection.optional) continue;
      const input = $(`repeatKey${index}`), value = input?.value.trim();
      if (!value) { status(`请填写${connection.label || '测试目标'}的 API Key（接口密钥）。`,true); input?.focus(); return; }
      keys[connection.slot] = value;
    }
    const ack = Boolean($('repeatRiskAck')?.checked);
    if (form.data.preflight?.requires_confirmation && !ack) { status('请先确认本次请求数、目标环境和费用，再开始测试。',true); $('repeatRiskAck')?.focus(); return; }
    await create(form.operation,keys,ack);
  }
  function onClosed() {
    const form = s.form; s.form = null;
    if ($('repeatBody')) { for (const input of $('repeatBody').querySelectorAll('input')) { input.value = ''; input.type === 'text' && (input.type = 'password'); } $('repeatBody').innerHTML = ''; }
    if ($('repeatFooter')) $('repeatFooter').innerHTML = '';
    if (form) {
      if (form.operation.phase === 'creating' && !form.operation.completed) form.operation.dismissed = true;
      else finish(form.operation);
    }
  }
  function close() { if ($('repeatModal') && !$('repeatModal').hidden) closeModal('repeatModal'); else onClosed(); }
  function reset() { s.epoch++; close(); s.operations.clear(); updateButtons(); }
  window.RepeatUI = Object.freeze({init,button,start,submit,close,onClosed,reset,isOpen:()=>Boolean(s.form)});
})();
