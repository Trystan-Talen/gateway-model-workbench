/* Named local connections: public metadata is cached; secrets are fetched only on explicit reveal. */
(() => {
  'use strict';
  const $ = id => document.getElementById(id);
  const UI = window.UI;
  const s = {initialized:false,epoch:0,loadSequence:0,promise:null,loaded:false,pending:false,error:'',items:[],storageAvailable:true,selected:null,drawerOpen:false,drawerMode:'form',drawerOwner:null,drawerSession:0,editing:null,sourceSide:null,submitting:false,revealing:false,revealSequence:0,retrieved:false,opener:null,overlayState:null};
  const active = () => typeof activeModule !== 'undefined' && activeModule === 'connections';
  const signedIn = () => typeof authenticated === 'undefined' || authenticated;
  const notify = text => { if (typeof toast === 'function') toast(text); };
  const date = value => UI.formatTimestamp(value);
  function publicConnection(item) {
    return {id:String(item?.id || ''),name:String(item?.name || ''),url:String(item?.url || ''),api_key_saved:item?.api_key_saved === true,updated_at:item?.updated_at || null};
  }
  function list() { return s.items.map(publicConnection); }
  async function request(path, options) {
    const epoch = s.epoch;
    const result = await api(path, options);
    if (epoch !== s.epoch || !signedIn()) throw Object.assign(new Error('登录会话已失效'),{stale:true});
    return result;
  }
  const post = (path, body = {}) => request(path,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
  function init() {
    if (s.initialized) return;
    s.initialized = true;
    $('recordPanel').insertAdjacentHTML('beforeend', '<section id="connectionsSubnav" class="record-summary" aria-label="常用连接列表" hidden><span>全部常用连接</span><span class="record-count" id="connectionsCount">0</span></section><div class="task-list" id="connectionsList" hidden></div>');
    document.body.insertAdjacentHTML('beforeend', `<div class="drawer-backdrop fp-backdrop" id="connectionBackdrop" data-click-action="closeConnectionCreate" hidden></div><aside class="config fp-drawer" id="connectionDrawer" role="dialog" aria-modal="true" aria-labelledby="connectionCreateTitle" aria-hidden="true" inert><header class="drawer-head"><div><div class="form-title" id="connectionCreateTitle">保存常用连接</div><div class="form-sub">给地址与密钥取一个名称，测试时直接选择。</div></div><button class="drawer-close" type="button" data-click-action="closeConnectionCreate" aria-label="关闭常用连接表单">${UI.icon('close')}</button></header><div class="drawer-body" id="connectionCreateBody"></div><footer class="drawer-footer"><div class="form-status" id="connectionFormStatus" role="status" aria-live="polite"></div><button class="submit" id="connectionSubmit" type="button" data-click-action="saveConnection">保存常用连接</button><div class="form-note">保存连接不会向目标地址发送测试请求。</div></footer></aside>`);
    document.addEventListener('keydown',onKeydown,true);
    document.addEventListener('input',onInput);
  }
  function updateChrome() {
    if (!s.initialized) init();
    if (!active()) { hide(); return; }
    for (const id of ['gatewaySubnav','qualitySubnav','routingSubnav','fingerprintSubnav','gatewayList','qualityList','routingList','fingerprintList']) if ($(id)) $(id).hidden = true;
    $('connectionsSubnav').hidden = false;
    $('connectionsList').hidden = false;
    $('connectionsCount').textContent = String(s.items.length);
    $('crumb').textContent = '常用连接 · 本地地址与密钥';
    $('recordTitle').textContent = '常用连接';
    $('recordSubtitle').textContent = '为常用地址与密钥命名，测试时直接选择';
    $('recordCreateButton').innerHTML = UI.icon('plus') + '<span>保存常用连接</span>';
  }
  function hide() {
    if ($('connectionsSubnav')) $('connectionsSubnav').hidden = true;
    if ($('connectionsList')) $('connectionsList').hidden = true;
    // A shortcut can stack this drawer over a test form; ordinary refreshes keep it open.
    if (s.drawerOpen && typeof activeModule !== 'undefined' && s.drawerOwner !== activeModule) closeCreate();
  }
  function refresh(force = false) {
    if (!signedIn()) return Promise.resolve([]);
    if (s.promise && !force) return s.promise;
    const epoch = s.epoch, sequence = ++s.loadSequence;
    s.pending = true; s.error = '';
    const promise = (async () => {
      try {
        const data = await request('/connections');
        if (epoch !== s.epoch || sequence !== s.loadSequence) return list();
        s.items = (Array.isArray(data.connections) ? data.connections : []).map(publicConnection).filter(item=>item.id);
        s.storageAvailable = data.storage_available !== false;
        if (!s.items.some(item=>item.id === s.selected)) s.selected = s.items[0]?.id || null;
        s.loaded = true;
        window.dispatchEvent(new CustomEvent('connections-changed'));
        return list();
      } catch (error) {
        if (!error.stale && epoch === s.epoch && sequence === s.loadSequence) s.error = error.message || '无法读取常用连接';
        throw error;
      } finally {
        if (epoch === s.epoch && sequence === s.loadSequence) { s.pending = false; s.promise = null; if (active()) render(); if (s.drawerOpen && s.drawerMode === 'manage') paintManager(); }
      }
    })();
    s.promise = promise;
    if (active()) render();
    return promise;
  }
  function detailHtml(item) {
    return UI.pageShell([
      UI.pageHeader({kicker:'常用连接 · 本地保存',title:item.name,description:'在测试表单中选择这个名称，即可复用地址与密钥。',meta:[`更新于 ${date(item.updated_at)}`],statusLabel:item.api_key_saved?'密钥已保存':'密钥不可用',statusTone:item.api_key_saved?'success':'warning',statusNote:item.api_key_saved?'可在测试时直接选择':'编辑后重新保存密钥',actionsHtml:UI.button('编辑常用连接',{variant:'primary',action:'openConnectionEdit',args:[item.id]})}),
      UI.section({title:'连接信息',bodyHtml:UI.methodCards([{title:'URL（请求地址）',lines:[item.url]},{title:'API Key（接口密钥）',lines:[item.api_key_saved?'已保存在本机凭据库；编辑时可主动点击眼睛核对。':'未找到已保存密钥；请编辑连接重新输入。']}])}),
      !s.storageAvailable ? UI.notice('本机凭据库当前不可用，保存密钥可能失败。',{tone:'warning'}) : '',
      UI.actionBar(UI.button('删除常用连接',{variant:'danger',action:'deleteConnection',args:[item.id]}))
    ].join(''));
  }
  function emptyHtml() {
    return UI.pageShell(UI.pageHeader({kicker:'常用连接 · 本地保存',title:'还没有常用连接',description:'保存常用的请求地址与密钥，之后在测试表单中按名称选择。'}) + UI.section({title:'保存第一个连接',bodyHtml:UI.steps(['给连接取一个便于辨认的名称','填写请求地址与接口密钥并保存','新建测试时选择常用连接，仍可手动输入'])}));
  }
  function render() {
    if (!active() || !signedIn()) return;
    if (!s.initialized) init();
    updateChrome();
    for (const id of ['detail','empty','loadingState']) $(id).hidden = true;
    const host = $('modulePlaceholder'), main = document.querySelector('.main');
    const previous = host.dataset.connectionId, scroll = main?.scrollTop || 0;
    host.hidden = false;
    const item = s.items.find(connection=>connection.id === s.selected) || s.items[0];
    if (item) s.selected = item.id;
    $('connectionsList').innerHTML = s.items.length ? s.items.map(connection=>UI.recordItem({id:connection.id,title:connection.name,state:connection.api_key_saved?'finished':'error',stateLabel:connection.api_key_saved?'密钥已保存':'密钥不可用',typeLabel:'常用连接',meta:[connection.url],progress:null,trailing:date(connection.updated_at).split(' ')[0],selected:connection.id === s.selected,action:'selectConnection'})).join('') : UI.recordEmpty(s.pending?'正在读取常用连接':'暂无常用连接','保存后可在测试表单中按名称选择。');
    const loading = s.pending && !s.loaded && !s.items.length ? UI.section({title:'正在读取常用连接',bodyHtml:UI.notice('正在读取本地保存的名称与地址，请稍候。')}) : '';
    const error = s.error ? UI.section({title:'常用连接读取失败',bodyHtml:UI.notice(s.error,{tone:'danger'}) + UI.button('重新读取',{action:'refreshConnections'})}) : '';
    host.dataset.connectionId = item?.id || '';
    const content = loading || (item ? detailHtml(item) : !s.error ? emptyHtml() : '');
    host.innerHTML = error ? UI.stack(error + content) : content;
    if (main) main.scrollTop = previous === host.dataset.connectionId ? scroll : 0;
    if (!s.loaded && !s.pending && !s.error) refresh().catch(()=>{});
  }
  function select(id) { if (!s.items.some(item=>item.id === id)) return; s.selected = id; render(); }
  function formStatus(text, error = false) {
    const node = $('connectionFormStatus');
    node.textContent = text; node.classList.toggle('error',error);
  }
  function keyVisibility(visible) {
    const input = $('connectionKey'), button = input?.parentElement.querySelector('.password-toggle');
    if (!input || !button) return;
    input.type = visible ? 'text' : 'password';
    const label = `${visible?'隐藏':'显示'}API Key（接口密钥）`;
    button.setAttribute('aria-label',label); button.title = label;
    button.setAttribute('aria-pressed',String(visible)); button.innerHTML = UI.icon(visible?'eyeOff':'eye');
  }
  function clearRetrieved() {
    s.revealSequence++; s.revealing = false;
    const input = $('connectionKey');
    if (s.retrieved && input) input.value = '';
    s.retrieved = false; keyVisibility(false);
    const button = input?.parentElement.querySelector('.password-toggle'); if (button) { button.disabled = false; button.removeAttribute('aria-busy'); }
  }
  function syncOverlay() {
    const base = $('config'), baseOpen = base?.classList.contains('mobile-open');
    const fingerprint = $('fingerprintDrawer'), fingerprintOpen = fingerprint?.classList.contains('mobile-open');
    const gate = $('authGate') && !$('authGate').hidden;
    const modal = typeof modalStack !== 'undefined' && modalStack.length > 0;
    const app = document.querySelector('.app'); if (app) app.inert = Boolean(s.drawerOpen || s.overlayState?.appInert || baseOpen || fingerprintOpen || gate || modal);
    if (base) base.inert = Boolean(s.drawerOpen || gate || s.overlayState?.baseInert || !baseOpen);
    if (fingerprint) fingerprint.inert = Boolean(s.drawerOpen || gate || s.overlayState?.fingerprintInert || !fingerprintOpen);
    document.body.classList.toggle('drawer-open',Boolean(s.drawerOpen || baseOpen));
  }
  function beginDrawer(mode) {
    if (!s.initialized) init();
    if (s.drawerOpen) closeCreate();
    window.clearTransientFeedback?.();
    s.overlayState = {appInert:document.querySelector('.app')?.inert,baseInert:$('config')?.inert,fingerprintInert:$('fingerprintDrawer')?.inert};
    s.drawerOpen = true; s.drawerMode = mode; s.drawerOwner = typeof activeModule !== 'undefined' ? activeModule : null; s.drawerSession++; s.opener = document.activeElement;
    $('connectionDrawer').classList.add('mobile-open'); $('connectionDrawer').inert = false; $('connectionDrawer').setAttribute('aria-hidden','false'); $('connectionBackdrop').hidden = false;
    syncOverlay();
  }
  function focusDrawerStart(node) {
    const session=s.drawerSession;
    // Descendants can remain hidden for the first frame of the visibility transition.
    const attempt=(remaining=20)=>{
      if(!s.drawerOpen||session!==s.drawerSession||!node?.isConnected||$('connectionDrawer').contains(document.activeElement))return;
      if(typeof getComputedStyle!=='function'||getComputedStyle(node).visibility==='visible')node.focus({preventScroll:true});
      if(document.activeElement!==node&&remaining&&typeof requestAnimationFrame==='function')requestAnimationFrame(()=>attempt(remaining-1));
    };
    attempt();
  }
  function paintManager() {
    if (!s.drawerOpen || s.drawerMode !== 'manage') return;
    const body = $('connectionCreateBody'), scroll = body.scrollTop;
    body.innerHTML = UI.pageShell((s.error?UI.notice(s.error,{tone:'danger'}) + UI.button('重新读取',{action:'refreshConnections'}):'') +
      UI.hint('编辑和删除仅影响常用连接；当前测试输入会保留。') + (s.pending && !s.loaded ? UI.notice('正在读取本地连接，请稍候。') : s.items.length ? s.items.map(item=>UI.section({title:item.name,description:item.url,badgeHtml:UI.tag(item.api_key_saved?'密钥已保存':'密钥不可用',item.api_key_saved?'success':'warning'),bodyHtml:UI.actionBar(UI.button('编辑连接',{action:'openConnectionEdit',args:[item.id]}) + UI.button('删除连接',{variant:'danger',action:'deleteConnection',args:[item.id]}))})).join('') : UI.recordEmpty('暂无常用连接','点击下方“保存新连接”添加。')));
    body.scrollTop = scroll;
  }
  function openManage() {
    if (!signedIn()) return;
    beginDrawer('manage'); s.editing = null; s.sourceSide = null;
    $('connectionCreateTitle').textContent = '管理常用连接';
    $('connectionSubmit').textContent = '保存新连接'; $('connectionSubmit').disabled = false; $('connectionSubmit').dataset.clickAction = 'openConnectionCreate';
    formStatus(''); paintManager(); focusDrawerStart($('connectionDrawer').querySelector('.drawer-close'));
    refresh().catch(()=>{});
  }
  function openCreate(seed = {}) { openForm(null,seed); }
  function openEdit(id) {
    const item = s.items.find(connection=>connection.id === id);
    if (!item) { notify('常用连接已失效，请重新读取后再编辑。'); return; }
    openForm(item,{});
  }
  function openForm(item, seed) {
    if (!signedIn()) return;
    beginDrawer('form'); s.editing = item ? publicConnection(item) : null;
    s.sourceSide = Object.hasOwn(seed,'source_side') ? seed.source_side : null;
    s.submitting = false; s.retrieved = false;
    $('connectionCreateTitle').textContent = item?'编辑常用连接':'保存常用连接';
    const keyRequired = item ? !item.api_key_saved : s.sourceSide === null;
    $('connectionCreateBody').innerHTML = UI.formSectionTitle(1,'连接名称与地址') +
      UI.field({id:'connectionName',label:'连接名称',required:true,controlHtml:UI.input({id:'connectionName',value:item?.name || seed.name || '',placeholder:'例如：日常智能路由',required:true})}) +
      UI.field({id:'connectionUrl',label:'URL（请求地址）',required:true,controlHtml:UI.input({id:'connectionUrl',type:'url',value:item?.url || seed.url || '',placeholder:'https://example.com/v1',required:true}),hint:'填写测试时使用的完整请求地址；编辑后更换地址需要重新输入密钥。'}) +
      UI.formSectionTitle(2,'接口密钥') + UI.field({id:'connectionKey',label:'API Key（接口密钥）',required:keyRequired,controlHtml:UI.passwordInput({id:'connectionKey',value:seed.api_key || '',required:keyRequired,action:'revealConnectionKey',args:[],visibilityLabel:'API Key（接口密钥）',placeholder:item?.api_key_saved?'留空保留已保存密钥':'填写接口密钥',describedBy:'connectionKeyHint'}),hint:item?(item.api_key_saved?'留空保留原密钥。点击眼睛可主动取回核对，隐藏或关闭即清空取回值。':'该连接的密钥已不可用，请重新填写后保存。'):s.sourceSide !== null?'留空时复用当前地址已保存的密钥。':'密钥存入本机凭据库，列表只展示名称与地址。',hintId:'connectionKeyHint'});
    formStatus(''); $('connectionSubmit').disabled = false; $('connectionSubmit').textContent = '保存常用连接'; $('connectionSubmit').dataset.clickAction = 'saveConnection';
    $('connectionCreateBody').scrollTop = 0; focusDrawerStart($('connectionName'));
  }
  function closeCreate() {
    if (!s.initialized) return;
    const wasOpen = s.drawerOpen; clearRetrieved();
    const key = $('connectionKey'); if (key) key.value = '';
    s.drawerOpen = false; s.drawerSession++; s.submitting = false; s.editing = null; s.sourceSide = null;
    $('connectionDrawer').classList.remove('mobile-open'); $('connectionDrawer').inert = true; $('connectionDrawer').setAttribute('aria-hidden','true'); $('connectionBackdrop').hidden = true; $('connectionCreateBody').innerHTML = '';
    syncOverlay(); s.overlayState = null;
    if (wasOpen && s.opener?.isConnected && signedIn()) s.opener.focus({preventScroll:true});
    s.opener = null;
  }
  async function save() {
    if (!s.drawerOpen || s.drawerMode !== 'form' || s.submitting) return;
    const fields = ['connectionName','connectionUrl','connectionKey'].map($);
    for (const field of fields) if (!field.reportValidity()) { field.focus(); return; }
    const [name,url,key] = fields;
    if (!name.value.trim()) { formStatus('请填写连接名称。',true); name.focus(); return; }
    const body = {name:name.value.trim(),url:url.value.trim()};
    if (s.editing) body.id = s.editing.id;
    if (s.sourceSide !== null) body.source_side = s.sourceSide;
    if (key.value && !s.retrieved) body.api_key = key.value;
    if (s.editing && body.url !== s.editing.url && !body.api_key) { formStatus('更换请求地址后，请重新输入对应密钥。',true); key.focus(); return; }
    const session = s.drawerSession;
    s.submitting = true; $('connectionSubmit').disabled = true; $('connectionSubmit').textContent = '正在保存…'; formStatus('');
    try {
      const result = await post('/connections',body);
      const saved = publicConnection(result.connection);
      if (saved.id) {
        s.items = [saved,...s.items.filter(item=>item.id !== saved.id)];
        s.selected = saved.id;
      } else s.selected = String(result.id || s.editing?.id || '');
      window.dispatchEvent(new CustomEvent('connections-changed'));
      if (session === s.drawerSession) closeCreate();
      notify('常用连接已保存');
      try { await refresh(true); }
      catch (error) { if (!error.stale) notify('常用连接已保存，但列表刷新失败：' + (error.message || '请重新读取。')); }
    } catch (error) { if (!error.stale && session === s.drawerSession) formStatus(error.message || '常用连接保存失败。',true); }
    finally { body.api_key = ''; if (session === s.drawerSession) { s.submitting = false; $('connectionSubmit').disabled = false; $('connectionSubmit').textContent = '保存常用连接'; } }
  }
  async function remove(id) {
    const item = s.items.find(connection=>connection.id === id);
    if (!item || !confirm(`确认删除常用连接“${item.name}”？仅删除这条常用连接及其保存的密钥，不删除测试记录。`)) return;
    try {
      await post(`/connections/${encodeURIComponent(id)}/delete`);
      s.items = s.items.filter(connection=>connection.id !== id);
      if (s.selected === id) s.selected = s.items[0]?.id || null;
      window.dispatchEvent(new CustomEvent('connections-changed')); notify('常用连接已删除');
      try { await refresh(true); }
      catch (error) { if (!error.stale) notify('常用连接已删除，但列表刷新失败：' + (error.message || '请重新读取。')); }
    }
    catch (error) { if (!error.stale) notify(error.message || '常用连接删除失败。'); }
  }
  async function revealKey() {
    if (!s.drawerOpen || s.revealing || s.submitting) return;
    const input = $('connectionKey'), button = input.parentElement.querySelector('.password-toggle');
    if (input.type === 'text') { clearRetrieved(); return; }
    if (input.value) { keyVisibility(true); return; }
    if (!s.editing?.api_key_saved) { formStatus('请先输入接口密钥。',true); input.focus(); return; }
    if ($('connectionUrl').value.trim() !== s.editing.url) { formStatus('请求地址已修改，请输入新地址对应的密钥。',true); input.focus(); return; }
    const session = s.drawerSession, sequence = ++s.revealSequence, id = s.editing.id;
    s.revealing = true; button.disabled = true; button.setAttribute('aria-busy','true'); formStatus('正在取回已保存密钥…');
    try {
      const data = await post(`/connections/${encodeURIComponent(id)}/reveal`);
      if (!s.drawerOpen || session !== s.drawerSession || sequence !== s.revealSequence || input.value || $('connectionUrl').value.trim() !== s.editing?.url) return;
      if (!data.api_key) throw new Error('未找到已保存密钥，请重新输入。');
      input.value = String(data.api_key); s.retrieved = true; data.api_key = ''; keyVisibility(true); formStatus('已取回密钥，仅供本次核对；隐藏或关闭后清空。');
    } catch (error) { if (!error.stale && session === s.drawerSession && sequence === s.revealSequence) formStatus(error.message || '无法取回密钥。',true); }
    finally { if (session === s.drawerSession && sequence === s.revealSequence) { s.revealing = false; button.disabled = false; button.removeAttribute('aria-busy'); } }
  }
  function onInput(event) {
    if (!s.drawerOpen) return;
    if (event.target.id === 'connectionUrl') clearRetrieved();
    if (event.target.id === 'connectionKey') { s.revealSequence++; s.revealing = false; s.retrieved = false; const button = event.target.parentElement.querySelector('.password-toggle'); button.disabled = false; button.removeAttribute('aria-busy'); }
  }
  function onKeydown(event) {
    if (!s.drawerOpen) return;
    const drawer = $('connectionDrawer');
    if (event.key === 'Escape') { event.preventDefault(); event.stopImmediatePropagation(); closeCreate(); return; }
    if (event.key !== 'Tab') return;
    const nodes = [...drawer.querySelectorAll('button:not([disabled]),input:not([disabled]),a[href],select:not([disabled]),textarea:not([disabled]),[tabindex="0"]')].filter(node=>node.getClientRects().length && node.tabIndex >= 0);
    const first = nodes[0], last = nodes[nodes.length - 1];
    if (!drawer.contains(document.activeElement)) { event.preventDefault(); first?.focus(); }
    else if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last?.focus(); }
    else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first?.focus(); }
  }
  function reset() {
    s.epoch++; s.loadSequence++; s.promise = null; s.pending = false; s.loaded = false; s.items = []; s.selected = null; s.error = ''; s.storageAvailable = true;
    closeCreate(); hide();
    if ($('connectionsList')) $('connectionsList').innerHTML = '';
    if ($('modulePlaceholder')?.dataset.connectionId !== undefined) { $('modulePlaceholder').innerHTML = ''; delete $('modulePlaceholder').dataset.connectionId; }
  }
  const isOpen = () => s.drawerOpen;
  window.ConnectionUI = Object.freeze({init,updateChrome,hide,render,openCreate,openEdit,openManage,closeCreate,save,remove,revealKey,select,list,refresh,reset,isOpen});
})();
