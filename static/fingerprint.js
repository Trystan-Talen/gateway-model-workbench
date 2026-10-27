/* Model fingerprint workspace. Named connections are resolved by the server; no secrets are loaded into the form. */
(() => {
  'use strict';
  const $ = id => document.getElementById(id);
  const UI = window.UI;
  const html = UI.esc;
  const finite = value => value !== null && value !== undefined && value !== '' && Number.isFinite(Number(value));
  const number = (value, digits = 0) => finite(value) ? Number(value).toLocaleString('zh-CN', {maximumFractionDigits:digits, minimumFractionDigits:digits}) : '未知';
  const percent = value => finite(value) ? `${number(Number(value) * 100, 1)}%` : '未知';
  const metric = (label, value, note = '') => `<div class="fp-metric"><span>${html(label)}</span><strong>${html(value)}</strong>${note ? `<small>${html(note)}</small>` : ''}</div>`;
  const icon = UI.icon;
  const button = (action, label, attrs = '', className = '') => `<button type="button" class="btn fp-button ${className}" data-fp-action="${action}" ${attrs}>${label}</button>`;
  const notice = (text, tone = '') => `<div class="fp-notice ${tone}" role="status">${html(text)}</div>`;
  const MODES = {selfcheck:'通道自检',reference:'建立可信参考',verify:'对照已有参考'};
  const STATUSES = {waiting:'排队中',running:'采集中',stopping:'正在停止',finished:'已完成',error:'采集失败',stopped:'已停止',interrupted:'进程中断'};
  const VERDICTS = {match:'行为分布相近',mismatch:'行为分布存在明显差异',uncertain:'证据存疑，建议复测',insufficient:'有效证据不足',incomparable:'不具备直接可比条件'};
  const CATEGORIES = {valid:'有效',invalid:'无效回答',refusal:'拒答',empty:'空回答',error:'请求失败',cancelled:'已取消'};
  const TABS = [['overview','结论'],['distribution','回答分布'],['diagnostics','通道诊断'],['protocol','协议与来源'],['samples','原始样本']];
  // 记录与页头统一使用 .status 生命周期胶囊；进程中断按失败着色。
  const STATUS_STATE = {waiting:'waiting',running:'running',stopping:'stopping',finished:'finished',error:'error',stopped:'stopped',interrupted:'error'};
  const TONE = {good:'success',warn:'warning',neutral:'neutral'};
  const s = {initialized:false,epoch:0,runs:[],references:[],catalog:null,catalogError:'',referenceError:'',catalogPending:false,referencesPending:false,catalogPromise:null,referencesPromise:null,storageWarnings:[],referencesLoaded:false,referencesAt:0,view:'runs',selected:null,tab:'overview',detail:null,detailError:'',detailPending:false,detailSignature:'',selection:0,referenceArtifacts:new Map(),samplePage:null,sampleError:'',samplePending:false,sampleOffset:0,sampleSequence:0,drawerOpen:false,drawerSession:0,submitting:false,modalOpen:false,modalSession:0,modalBusy:false,opener:null,modalOpener:null,refOpen:null,formProtocol:null,modelInputMode:'list',modelOptions:[],modelDiscoverySequence:0,modelDiscoveryPending:false,modelDiscoveryStatus:'',modelDiscoveryTone:'',adapterManual:false,adapterAuto:null};
  const active = () => typeof activeModule !== 'undefined' && activeModule === 'fingerprint';
  const signedIn = () => typeof authenticated === 'undefined' || authenticated;
  let connectionId = '', connectionRevision = '';
  const savedConnection = () => (window.ConnectionUI?.list() || []).find(profile => profile.id === connectionId);
  const connectionVersion = profile => profile ? JSON.stringify([profile.url,profile.api_key_saved,profile.updated_at]) : '';
  function connectionIssue() {
    if (!connectionId) return '';
    const profile = savedConnection();
    if (!profile) return '所选常用连接已删除，请重新选择连接或手动填写。';
    if (profile.url !== $('fpUrl')?.value.trim()) return '所选常用连接的地址已更新，请重新选择该连接后获取模型。';
    if (!profile.api_key_saved) return '所选常用连接的密钥不可用，请编辑连接补填密钥，或手动填写。';
    return '';
  }
  function connectionPickerHtml() {
    const profiles = window.ConnectionUI?.list() || [];
    const options = [['','手动填写地址和密钥'],...profiles.map(p => [p.id,`${p.name} · ${p.url}${p.api_key_saved?'':' · 密钥不可用'}`])];
    if (connectionId && !profiles.some(p => p.id === connectionId)) options.push([connectionId,'已选择的连接不可用，请重新选择']);
    return UI.field({id:'fpSavedConnection',label:'常用连接',controlHtml:UI.select({id:'fpSavedConnection',options,value:connectionId}).replace('<select ','<select data-change-action="selectFingerprintConnection" ')+`<div class="model-fallback">${UI.textButton(connectionId?'编辑所选连接':'保存当前连接',{action:'saveFingerprintConnection'})} · ${UI.textButton('管理常用连接',{action:'manageSavedConnections'})}</div>`,hint:connectionIssue() || (connectionId?'已复用所选连接的地址与密钥。':'')});
  }
  function renderConnectionPicker() {const host=$('fpConnectionPicker');if(host)host.innerHTML=connectionPickerHtml();if($('fpManualConnection'))$('fpManualConnection').hidden=!!connectionId&&!connectionIssue()}
  function onConnectionsChanged() {
    if (!s.drawerOpen) return;
    const revision = connectionVersion(savedConnection());
    if (connectionId && revision !== connectionRevision) { connectionRevision = revision; invalidateModelConnection(); }
    renderConnectionPicker();
  }
  function selectConnection(node) {
    const id=node?.value||'',profile=(window.ConnectionUI?.list()||[]).find(p=>p.id===id);
    if(id&&!profile){toastMessage('连接不可用，请刷新后重新选择。');return}
    connectionId=id; connectionRevision=connectionVersion(profile);
    if(profile){$('fpUrl').value=profile.url;$('fpKey').value='';$('fpKey').type='password'}
    const toggle = document.querySelector('[data-fp-action="toggle-key"]');
    if (toggle) { toggle.textContent='显示'; toggle.setAttribute('aria-label','显示接口密钥'); toggle.setAttribute('aria-pressed','false'); }
    $('fpKey').placeholder=id?'已选择常用连接，密钥留空复用':'本地免鉴权服务可留空';
    renderConnectionPicker();invalidateModelConnection();$('fpRiskAck').checked=false;updateAdapterHint();
  }
  function saveConnection() {
    if(connectionId)return window.ConnectionUI?.openEdit(connectionId);
    const url=$('fpUrl')?.value.trim();if(!url)return toastMessage('请先填写要保存的请求地址。');
    window.ConnectionUI?.openCreate({name:$('fpName')?.value.trim()||'',url,api_key:$('fpKey')?.value||''});
  }
  const comparedReference = run => run?.comparison?.reference_id || (run?.mode === 'verify' ? run.reference_id : null);
  const running = run => ['waiting','running','stopping'].includes(run?.status);
  const date = value => UI.formatTimestamp(value);
  const timeAge = value => {
    if (!value) return null;
    const t = new Date(typeof value === 'number' ? value * (value < 1e12 ? 1000 : 1) : value).getTime();
    return Number.isFinite(t) ? (Date.now() - t) / 86400000 : null;
  };
  const protocolOf = reference => reference?.protocol || reference?.artifact?.protocol || {};
  const reasoningProtocol = p => p?.version === 'workbench-reasoning-one-word/1' || p?.method_version === 'workbench-reasoning-one-word/1';
  const referenceHealth = reference => {
    const p = protocolOf(reference), age = timeAge(reference?.collected_at || reference?.artifact?.collected_at);
    if (!p.id || p.external_protocol || p.version === 'external' || reference?.external_protocol) return {text:'外部协议／协议未知',tone:'warn',usable:false};
    if (s.catalog?.method_version && !(s.catalog.supported_method_versions || [s.catalog.method_version]).includes(p.version)) return {text:'协议版本与当前版本不同',tone:'warn',usable:false};
    if (reference?.complete === false || reference?.artifact?.complete === false) return {text:'采集未完成',tone:'warn',usable:false};
    if (age === null || age < -5 / 1440) return {text:'采集日期不可验证',tone:'warn',usable:false};
    if (age > (s.catalog?.reference_max_age_days || 30)) return {text:'已过期，建议重新采集',tone:'warn',usable:false};
    if (!reference?.trusted_source) return {text:'来源尚未声明可信',tone:'warn',usable:false};
    const diagnostics = reference?.diagnostics || reference?.artifact?.diagnostics || {};
    if (finite(diagnostics.valid_rate) && diagnostics.valid_rate < .8) return {text:'有效证据不足，建议重新采集',tone:'warn',usable:false};
    if (diagnostics.split_half?.warning) return {text:'自检存在波动，对照需降级',tone:'warn',usable:true};
    if (reasoningProtocol(p)) return {text:'保留思考 · 仅供探索性对照',tone:'warn',usable:true};
    return {text:'当前有效 · 来源由用户声明',tone:'good',usable:true};
  };
  function safeUrl(value) { try { const u = new URL(value); return ['http:','https:'].includes(u.protocol) ? u.href : ''; } catch { return ''; } }
  const link = (value, label) => safeUrl(value) ? `<a href="${html(safeUrl(value))}" target="_blank" rel="noopener noreferrer">${html(label)}</a>` : html(label);
  const toastMessage = value => { if (typeof toast === 'function') toast(String(value)); };
  async function get(url, options) {
    const epoch = s.epoch;
    const result = await api(url, options);
    if (epoch !== s.epoch || !signedIn()) throw Object.assign(new Error('会话已失效'), {stale:true});
    return result;
  }
  const post = (url, data) => get(url, {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(data),timeout:30000});
  function init() {
    if (s.initialized) return;
    s.initialized = true;
    const panel = $('recordPanel');
    panel.insertAdjacentHTML('beforeend', `<section id="fingerprintSubnav" class="workspace-subnav record-filter" aria-label="模型指纹记录分类" hidden><button class="fp-filter active" type="button" data-fp-action="view" data-fp-view="runs">采集记录 <em id="fpRunCount">0</em></button><button class="fp-filter" type="button" data-fp-action="view" data-fp-view="references">参考库 <em id="fpRefCount">0</em></button></section><div class="task-list" id="fingerprintList" hidden></div>`);
    document.body.insertAdjacentHTML('beforeend', `<div class="drawer-backdrop fp-backdrop" id="fingerprintBackdrop" data-fp-action="close-create" hidden></div><aside class="config fp-drawer" id="fingerprintDrawer" role="dialog" aria-modal="true" aria-labelledby="fpCreateTitle" aria-hidden="true" inert><header class="drawer-head"><div><div class="form-title" id="fpCreateTitle">新建模型指纹</div><div class="form-sub" id="fpCreateSubtitle">冻结协议，采集回答分布，保留可复核证据。</div></div><button class="drawer-close" type="button" data-fp-action="close-create" aria-label="关闭新建模型指纹">${icon('close')}</button></header><form id="fpCreateForm" class="fp-form"><div class="drawer-body" id="fpCreateBody"></div><footer class="drawer-footer"><p id="fpFormError" class="fp-form-error" role="alert" hidden></p><button class="submit" id="fpSubmit" type="submit" disabled>开始采集</button><p class="form-note">每次请求独立发出；密钥仅用于本次运行，关闭表单即清空。</p></footer></form></aside><div class="fp-modal" id="fingerprintModal" hidden><div class="fp-modal-backdrop" data-fp-action="close-modal"></div><section class="fp-modal-dialog" role="dialog" aria-modal="true" aria-labelledby="fpModalTitle"><header class="fp-modal-head"><div><div class="fp-kicker">模型指纹</div><h2 id="fpModalTitle"></h2></div><button class="drawer-close" type="button" data-fp-action="close-modal" aria-label="关闭对话框">${icon('close')}</button></header><div class="fp-modal-body" id="fpModalBody"></div><footer id="fpModalFooter" class="fp-modal-footer"></footer></section></div>`);
    document.addEventListener('click', onClick);
    document.addEventListener('change', onChange);
    document.addEventListener('input', onInput);
    document.addEventListener('keydown', onKeydown, true);
    window.addEventListener('connections-changed',onConnectionsChanged);
    $('fpCreateForm').addEventListener('submit', submitCreate);
    hide();
  }
  function setRuns(runs, storageWarnings = []) {
    s.storageWarnings = Array.isArray(storageWarnings) ? storageWarnings.map(String) : [];
    s.runs = Array.isArray(runs) ? runs : [];
    if (!active()) return;
    if (s.selected && !s.runs.some(r => r.id === s.selected)) { s.selected = null; s.detail = null; s.selection++; }
    ensureData();
    if (s.selected) {
      const summary = s.runs.find(r => r.id === s.selected);
      if (summary) {
        const signature = JSON.stringify([summary.id,summary.status,summary.progress,summary.reference_id,summary.comparison,summary.error]);
        if (signature !== s.detailSignature) loadDetail(false, signature);
      }
    }
  }
  function updateChrome() {
    if (!s.initialized) init();
    if (!active()) { hide(); return; }
    for (const id of ['gatewaySubnav','qualitySubnav','routingSubnav','gatewayList','qualityList','routingList']) if ($(id)) $(id).hidden = true;
    $('fingerprintSubnav').hidden = false;
    $('fingerprintList').hidden = false;
    $('crumb').textContent = '模型指纹 · 行为分布与通道自检';
    $('recordTitle').textContent = '模型指纹';
    $('recordSubtitle').textContent = '固定协议下的分布采集与参考对照';
    $('recordCreateButton').innerHTML = icon('plus') + `<span>${s.view === 'references' ? '采集参考指纹' : '新建模型指纹'}</span>`;
    $('fpRunCount').textContent = String(s.runs.length);
    $('fpRefCount').textContent = String(s.references.length);
    document.querySelectorAll('#fingerprintSubnav .fp-filter').forEach(el => { const chosen = el.dataset.fpView === s.view; el.classList.toggle('active',chosen); el.setAttribute('aria-pressed',String(chosen)); });
  }
  function hide() {
    if ($('fingerprintSubnav')) $('fingerprintSubnav').hidden = true;
    if ($('fingerprintList')) $('fingerprintList').hidden = true;
    if (s.drawerOpen && !active()) closeCreate();
    if (s.modalOpen && !active()) closeModal();
  }
  function onAuthLost() {
    s.epoch++; s.selection++; s.sampleSequence++; s.drawerSession++; s.modalSession++;
    s.catalogPending = false; s.referencesPending = false; s.catalogPromise = null; s.referencesPromise = null; s.storageWarnings = []; s.detailPending = false; s.samplePending = false;
    closeCreate(); closeModal();
    s.runs = []; s.references = []; s.referencesLoaded = false; s.referencesAt = 0; s.selected = null; s.detail = null; s.samplePage = null; s.referenceArtifacts.clear();
    if ($('fingerprintList')) $('fingerprintList').innerHTML = '';
    if ($('modulePlaceholder')?.querySelector('.fp-workspace')) $('modulePlaceholder').innerHTML = '';
  }
  async function ensureData(force = false) {
    if (!signedIn()) return;
    const jobs = [];
    if ((!s.catalog && !s.catalogError || force) && !s.catalogPending) jobs.push(loadCatalog());
    if ((!s.referencesLoaded && !s.referenceError || force || (s.referencesLoaded && Date.now() - s.referencesAt > 15000)) && !s.referencesPending) jobs.push(loadReferences());
    if (s.catalogPromise) jobs.push(s.catalogPromise);
    if (s.referencesPromise) jobs.push(s.referencesPromise);
    await Promise.allSettled(jobs);
  }
  function loadCatalog() {
    if (s.catalogPromise) return s.catalogPromise;
    const epoch = s.epoch;
    s.catalogPromise = fetchCatalog().finally(() => { if (epoch === s.epoch) s.catalogPromise = null; });
    return s.catalogPromise;
  }
  async function fetchCatalog() {
    const epoch = s.epoch; s.catalogPending = true; s.catalogError = '';
    try { s.catalog = await get('/fingerprint/catalog'); }
    catch (e) { if (!e.stale && epoch === s.epoch) s.catalogError = e.message; }
    finally { if (epoch === s.epoch) { s.catalogPending = false; if (active()) render(); } }
  }
  function loadReferences() {
    if (s.referencesPromise) return s.referencesPromise;
    const epoch = s.epoch;
    s.referencesPromise = fetchReferences().finally(() => { if (epoch === s.epoch) s.referencesPromise = null; });
    return s.referencesPromise;
  }
  async function fetchReferences() {
    const epoch = s.epoch; s.referencesPending = true; s.referenceError = '';
    try { const data = await get('/fingerprint/references'); s.references = data.references || []; s.referencesLoaded = true; s.referencesAt = Date.now(); }
    catch (e) { if (!e.stale && epoch === s.epoch) s.referenceError = e.message; }
    finally { if (epoch === s.epoch) { s.referencesPending = false; if (active()) render(); } }
  }
  function render() {
    if (!active() || !signedIn()) return;
    if (!s.initialized) init();
    updateChrome();
    for (const id of ['detail','empty','loadingState']) $(id).hidden = true;
    const host = $('modulePlaceholder'); host.hidden = false;
    renderList();
    if (s.view === 'references') { paintHost(referenceLibrary()); return; }
    // 与其他模块一致：有记录时默认打开最新一条，只有列表为空才显示引导页。
    if (!s.selected && s.runs.length) { selectRun(s.runs[0].id); renderList(); loadDetail(true); }
    if (!s.selected) { paintHost(welcome()); return; }
    const run = s.detail?.id === s.selected ? s.detail : s.runs.find(r => r.id === s.selected);
    if (!run) { paintHost(welcome()); return; }
    paintHost(detailHtml(run));
  }
  function paintHost(content) {
    const host = $('modulePlaceholder');
    const key = `${s.view}:${s.selected || ''}:${s.tab}`;
    const same = host.dataset.fpKey === key;
    const scrollers = ['.main','.shell'].map(selector=>document.querySelector(selector)).filter(Boolean).map(node=>({node,top:same?node.scrollTop||0:0,left:same?node.scrollLeft||0:0}));
    const activeEl = document.activeElement;
    const focusKey = host.contains(activeEl) ? {action:activeEl.dataset?.fpAction,id:activeEl.dataset?.fpId,tab:activeEl.dataset?.fpTab,offset:activeEl.dataset?.fpOffset,click:activeEl.dataset?.clickAction,arg:activeEl.dataset?.arg1} : null;
    const tableScrolls = same ? [...host.querySelectorAll('[data-fp-scroll]')].map(el => [el.dataset.fpScroll,el.scrollLeft,el.scrollTop]) : [];
    const expanded = same ? [...host.querySelectorAll('details[open][data-fp-details]')].map(el => el.dataset.fpDetails) : [];
    host.dataset.fpKey = key;
    host.innerHTML = (s.storageWarnings.length ? `<div class="fp-storage-warning">${notice('本地记录恢复提示：' + s.storageWarnings.join('；'),'warn')}</div>` : '') + content;
    if (expanded.length) {
      const details = new Map();
      for (const el of host.querySelectorAll('[data-fp-details]')) if (!details.has(el.dataset.fpDetails)) details.set(el.dataset.fpDetails,el);
      for (const id of expanded) { const el = details.get(id); if (el) el.open = true; }
    }
    if (tableScrolls.length) {
      const regions = new Map();
      for (const el of host.querySelectorAll('[data-fp-scroll]')) if (!regions.has(el.dataset.fpScroll)) regions.set(el.dataset.fpScroll,el);
      for (const [id,left,top] of tableScrolls) { const el = regions.get(id); if (el) { el.scrollLeft = left; el.scrollTop = top; } }
    }
    if (focusKey?.action) [...host.querySelectorAll('[data-fp-action]')].find(el => el.dataset.fpAction === focusKey.action && el.dataset.fpId === focusKey.id && el.dataset.fpTab === focusKey.tab && el.dataset.fpOffset === focusKey.offset)?.focus({preventScroll:true});
    else if (focusKey?.click) [...host.querySelectorAll('[data-click-action]')].find(el => el.dataset.clickAction === focusKey.click && el.dataset.arg1 === focusKey.arg)?.focus({preventScroll:true});
    for (const {node,top,left} of scrollers) { node.scrollTop = top; node.scrollLeft = left; }
  }
  function renderList() {
    const list = $('fingerprintList');
    if (s.view === 'references') {
      list.innerHTML = s.references.length ? s.references.map(ref => { const health = referenceHealth(ref); return `<button class="task fp-record ${s.refOpen === ref.id ? 'sel' : ''}" type="button" data-fp-action="reference-detail" data-fp-id="${html(ref.id)}"><div class="task-top"><div class="task-name">${html(ref.name || '未命名参考')}</div><span class="fp-dot ${health.tone}"></span></div><div class="task-meta">${html(ref.endpoint?.model || '模型未记录')}</div><div class="fp-record-foot"><span>${html(health.text)}</span><span>${html(date(ref.collected_at).split(' ')[0])}</span></div></button>`; }).join('') : `<div class="task-empty"><b>${s.referenceError ? '参考库读取失败' : s.referencesPending ? '正在读取参考库' : '暂无参考指纹'}</b><span>${html(s.referenceError || '从可信来源采集，或导入已保存的指纹文件。')}</span>${s.referenceError ? button('retry-data','重新加载') : ''}</div>`;
      return;
    }
    list.innerHTML = s.runs.length ? s.runs.map(run => UI.recordItem({
      id: run.id, title: run.name, state: STATUS_STATE[run.status] || 'waiting', stateLabel: STATUSES[run.status] || run.status,
      typeLabel: MODES[run.mode] || '指纹采集', compare: Boolean(run.comparison || run.mode === 'verify'), meta: [run.endpoint?.model || '模型未记录'],
      progress: Number(run.progress?.done || 0) / Math.max(1,Number(run.progress?.total || 0)) * 100,
      trailing: `${number(run.progress?.done || 0)} / ${number(run.progress?.total || 0)}`, selected: s.selected === run.id, action: 'selectFingerprint'
    })).join('') : UI.recordEmpty('暂无采集记录','新建模型指纹，从通道自检开始。');
  }
  function welcome() {
    return `<div class="fp-workspace fp-welcome"><section class="fp-hero"><div class="fp-hero-icon">${icon('fingerprint')}</div><div><div class="fp-kicker">模型指纹 · 可重复的黑盒观测</div><h1>让每次对照，都有同一把尺子</h1><p>把固定问题独立、多次发送给同一通道，观察回答分布；先检查自身稳定性，再与可信参考对照。</p></div></section><section class="fp-mode-explainer"><article><span class="fp-step">01</span><h2>通道自检</h2><p>将一次采集分半比较，寻找时间波动或分布不稳定的线索。</p><small>无需参考，适合首次使用</small></article><article><span class="fp-step">02</span><h2>建立可信参考</h2><p>从你认可的来源采集，连同冻结协议、来源和日期存入参考库。</p><small>可信来源需由你明确声明</small>${button('create-reference','采集参考指纹')}</article><article><span class="fp-step">03</span><h2>对照已有参考</h2><p>沿用参考参数采集目标通道，检查协议与证据条件后比较分布。</p><small>也可离线比较已完成记录</small></article></section><section class="fp-card"><div class="fp-section-head"><div><h2>固定协议，保留完整证据</h2><p>独立请求 · 无历史记忆 · 同义改写随机抽取 · 队列随机打乱 · 不自动重试</p></div>${icon('quality')}</div><div class="fp-facts"><span><b>冻结</b> 参数与输出上限</span><span><b>双分半</b> 交替与时间顺序自检</span><span><b>${html(s.catalog?.reference_max_age_days || 30)} 天</b> 本地参考有效期策略</span></div><p class="fp-subtle">分布相似不等于模型身份已被证明；差异也可能来自提示词、采样、版本或路由。阈值为未校准的工程提示。</p>${s.catalogError ? notice('协议目录读取失败：' + s.catalogError,'warn') + button('retry-data','重新加载协议') : ''}<details class="fp-details" data-fp-details="method"><summary>方法来源与判读边界</summary>${methodHtml()}</details></section></div>`;
  }
  function methodHtml() {
    const c = s.catalog || {};
    return `<p>借鉴 ${link(c.source_url || 'https://github.com/ToseaAI/llm-fingerprint-detector','llm-fingerprint-detector（大模型指纹检测器）')}；工作台使用独立协议版本。${c.paper_url ? link(c.paper_url,'查看原论文') : ''}</p><p>距离使用 JSD（詹森–香农散度），越小表示观测分布越接近，并非置信度。协议不兼容、参考过期、有效覆盖不足或诊断异常时，结论降级。</p><p>分半自检异常不能确定存在混合后端，低差异也不能排除混合。缓存诊断只作排查线索，不能单独证明模型身份。</p><p>30 天是本地工程策略；阈值借鉴上游，尚未针对当前供应商和模型版本校准。</p>`;
  }
  function selectRun(id) {
    if (s.selected !== id) { s.selected = id; s.detail = null; s.detailSignature = ''; s.detailError = ''; s.tab = 'overview'; s.samplePage = null; s.sampleOffset = 0; s.sampleError = ''; s.selection++; s.sampleSequence++; s.detailPending = false; s.samplePending = false; }
  }
  async function select(id) {
    selectRun(id); s.view = 'runs'; render(); await loadDetail(true);
  }
  async function loadDetail(force = false, signature = '') {
    if (!s.selected || s.detailPending || !active()) return;
    if (!force && signature && signature === s.detailSignature) return;
    const selection = s.selection, id = s.selected, epoch = s.epoch;
    s.detailPending = true; s.detailError = '';
    try {
      const run = await get(`/fingerprints/${encodeURIComponent(id)}`);
      if (selection !== s.selection || id !== s.selected) return;
      s.detail = run; s.detailSignature = signature || JSON.stringify([run.id,run.status,run.progress,run.reference_id,run.comparison,run.error]);
      if (run.reference_id && !s.references.some(r => r.id === run.reference_id)) loadReferences();
      if (s.tab === 'distribution' && comparedReference(run)) loadReferenceArtifact(comparedReference(run));
    } catch (e) { if (!e.stale && selection === s.selection && epoch === s.epoch) s.detailError = e.message; }
    finally { if (selection === s.selection && epoch === s.epoch) { s.detailPending = false; render(); } }
  }
  function detailHtml(run) {
    const done = Number(run.progress?.done || 0), total = Number(run.progress?.total || 0), pct = Math.min(100,Math.max(0,done / Math.max(1,total) * 100));
    const body = s.tab === 'overview' ? overviewHtml(run) : s.tab === 'distribution' ? distributionHtml(run) : s.tab === 'diagnostics' ? diagnosticsHtml(run) : s.tab === 'protocol' ? protocolHtml(run.protocol || run.artifact?.protocol,run) : samplesHtml(run);
    const verdict = running(run) ? {title:STATUSES[run.status] || '采集中',note:'完成前不作为结论',tone:'accent'} : run.artifact ? (v => ({title:v.title,note:v.note,tone:v.statusTone}))(verdictOf(run)) : {title:STATUSES[run.status] || run.status,note:'当前没有可分析的采样证据',tone:['error','interrupted'].includes(run.status)?'danger':'warning'};
    const tabs = TABS.map(([id,label]) => [id,id === 'overview' && (run.comparison || run.mode === 'verify') ? '对照结论' : label]);
    const showProgress = running(run) || !run.artifact || run.artifact.complete === false;
    const progress = showProgress ? UI.progressCard({kicker:running(run) ? '采集进度' : '本次采集已结束',label:'指纹采集进度',value:pct,
      summary:`${number(done)} / ${number(total)} 次请求已结算 · 有效 ${number(run.progress?.valid || 0)} · 无效 ${number(Math.max(0,done - (run.progress?.valid || 0) - (run.progress?.errors || 0)))} · 请求错误 ${number(run.progress?.errors || 0)}`,
      note:run.status === 'waiting' ? '等待采集槽位；排队时不会向目标发出请求。' : run.status === 'stopping' ? '停止未发请求并取消在途请求，已获得的证据会保留。' : running(run) ? '完成后计算分布与双分半自检；运行中的局部结果不作为对照结论。' : '仅保留已经采集到的证据，不将不完整采集视为完成。'}) : '';
    const actions = [
      run.artifact ? `<a class="btn fp-button" href="/fingerprints/${encodeURIComponent(run.id)}/export" download>${icon('download')}导出指纹</a>` : '',
      run.artifact ? button('compare','离线对照',`data-fp-id="${html(run.id)}"`) : '',
      !running(run) ? button('delete-run','删除本地记录',`data-fp-id="${html(run.id)}"`,'danger') : ''
    ].join('');
    return UI.pageShell([
      UI.pageHeader({kicker:`模型指纹 · ${MODES[run.mode] || '分布采集'}`,title:run.name,
        description:'固定问题独立多次发送，观察回答分布；原始证据只保存在本机，导出前请留意目标地址与回答原文。',
        meta:[STATUSES[run.status] || run.status,run.endpoint?.model || '模型未记录',date(run.timing?run.timing.created_at:run.created),`${number(run.protocol?.cells?.length)} 格问题`,`每格 ${number(run.protocol?.samples_per_cell)} 次`,`并发 ${number(run.protocol?.concurrency)}`],
        statusLabel:verdict.title,statusNote:verdict.note,statusTone:verdict.tone,actionsHtml:running(run) ? button('stop',icon('stop') + (run.status === 'stopping' ? '正在停止…' : '停止采集'),`data-fp-id="${html(run.id)}" ${run.status === 'stopping' ? 'disabled' : ''}`,'danger') : window.RepeatUI?.button('fingerprint',run)||''}),
      UI.tabs({label:'模型指纹详情标签页',items:tabs,active:s.tab,action:'setFingerprintTab',ownerId:run.id}),
      progress,
      s.detailError ? notice('记录详情读取失败：' + s.detailError,'warn') + button('retry-detail','重新加载详情') : '',
      run.error ? notice(run.error,'warn') : '',
      run.storage_warning ? notice(run.storage_warning,'warn') : '',
      `<div class="fp-workspace" id="fpTabPanel" role="tabpanel" aria-label="${html(tabs.find(t => t[0] === s.tab)?.[1] || '')}">${body}</div>`,
      UI.timeRecords(run,{live:running(run),key:'fingerprint:'+run.id}),
      UI.actionBar(actions)
    ].join(''));
  }

  // 页头状态块与结论卡共用同一个判定，避免两处文案漂移。
  function verdictOf(run) {
    const a = run.artifact || {}, d = a.diagnostics || run.diagnostics || {}, split = d.split_half || {}, comparison = run.comparison;
    const complete = a.complete === true, noValid = finite(d.valid_rate) && Number(d.valid_rate) === 0;
    const title = !complete ? '采集未完成，已保留部分证据' : noValid ? '未获得有效指纹' : comparison ? VERDICTS[comparison.verdict] || comparison.label || '对照结果' : split.warning ? '自检发现分布波动，建议复测' : '已形成指纹证据';
    const tone = !complete || noValid ? 'warn' : comparison?.verdict === 'match' ? 'good' : ['mismatch','uncertain','incomparable','insufficient'].includes(comparison?.verdict) || split.warning ? 'warn' : 'neutral';
    return {title,tone,statusTone:TONE[tone],note:comparison ? '描述行为分布，不判定模型身份' : MODES[run.mode] || '分布采集'};
  }
  function overviewHtml(run) {
    const a = run.artifact, d = a?.diagnostics || run.diagnostics || {}, split = d.split_half || {}, comparison = run.comparison;
    if (!a) return `<section class="fp-card fp-quiet"><h2>${s.detailPending ? '正在读取详情' : running(run) ? '结论等待采集完成' : '未形成可分析的指纹证据'}</h2><p>${s.detailPending || running(run) ? '完成后会同时检查有效样本、协议兼容性和通道自检。' : '本次采集已经结束，当前记录没有可用于分析的采样证据。请先检查错误信息、连接与模型配置，再重新采集。'}</p></section>`;
    const complete = a.complete === true, noValid = finite(d.valid_rate) && Number(d.valid_rate) === 0;
    const reasoningTruncated = noValid && d.reasoning_observed && Number(d.truncated_rate) === 1;
    const selfcheckAvailable = complete && Number(split.alternating?.comparable_cells || 0) >= 4 && Number(split.temporal?.comparable_cells || 0) >= 4 && finite(split.alternating?.mean_jsd) && finite(split.temporal?.mean_jsd);
    const {title,tone} = verdictOf(run);
    const reference = s.references.find(r => r.id === comparedReference(run));
    const c = comparison || {};
    const diagnosis = reasoningTruncated ? (reasoningProtocol(run.protocol || a.protocol) ? '保留思考的固定上限仍未产生有效正文。请核对所选模型与接口参数；本次不会自动扩大预算或重试，原记录保留作诊断。' : glmAlwaysThinking(run.endpoint?.model) ? '已观测到空正文与推理阶段输出截断。此 GLM（通用语言模型）名称对应官方强制思考模型，不能关闭思考。请核对接口后明确选择“智谱：保留思考”，确认 4096 词元上限及费用后新建；中转兼容性不能仅凭模型名确认。原记录不可作为有效参考。' : '所有已返回回答都被截断，并且观测到推理用量。检查原始样本：若正文为空，说明输出预算被推理占用。请用关闭思考适配重新采集；无法关闭时，可确认费用后新建保留思考采集。原记录不可作为有效参考。') : noValid ? '请求已结算，但没有可用于分布比较的有效回答。请在通道诊断与原始样本中检查截断、拒答和接口错误，再新建采集。' : '';
    return `<section class="fp-conclusion ${tone}"><div class="fp-kicker">${comparison ? '参考对照结论' : '采集结果'}</div><h2>${html(title)}</h2><p>${!complete ? '本次仅保留已经返回的部分请求；不据此评价稳定性或建立可信参考。' : noValid ? '本次没有有效回答，不能用于指纹对照或评价通道稳定性。' : comparison ? '结论描述本次行为分布，不直接判定模型身份。' : run.mode === 'reference' ? run.reference_id ? '本次指纹已进入参考库；可信来源是用户声明，系统不作身份背书。' : '参考尚未建立，请检查运行状态与证据质量。' : '可将本次记录与参考库离线对照，或在另一时间重新采集。'}</p>${(c.reasons || []).length ? `<ul>${c.reasons.map(x => `<li>${html(x)}</li>`).join('')}</ul>` : ''}${reference ? `<div class="fp-meta"><span>参考：${html(reference.name)}</span><span>${html(referenceHealth(reference).text)}</span></div>` : ''}</section>${diagnosis ? notice(diagnosis,'warn') : ''}<div class="fp-metrics">${metric('有效回答率',percent(d.valid_rate),'有效格式不代表身份已确认')}${metric('交替分半距离',number(split.alternating?.mean_jsd,3),`${number(split.alternating?.comparable_cells || 0)} 格可比`)}${metric('时间分半距离',number(split.temporal?.mean_jsd,3),'按每格发送顺序划分前后半')}${metric(comparison ? '平均对照距离' : '参考对照',comparison ? number(c.mean_jsd,3) : '尚未对照',comparison ? `${number(c.comparable_cells || 0)} 格可比 · 非置信度` : '复用本地证据，不产生请求')}</div>${comparison ? `<section class="fp-card"><div class="fp-section-head"><h2>对照条件</h2><span class="fp-subtle">先检查条件，再解释距离</span></div><div class="fp-facts"><span>协议 <b>${c.protocol_compatible ? '一致' : '不兼容／未知'}</b></span><span>参考年龄 <b>${finite(c.reference_age_days) ? number(c.reference_age_days,1) + ' 天' : '未知'}</b></span><span>可比问题 <b>${number(c.comparable_cells || 0)} 格</b></span></div><details class="fp-details" data-fp-details="thresholds"><summary>阈值与判读限制</summary>${reasoningProtocol(run.protocol || a.protocol) ? '<p>保留思考协议只展示探索性距离；不套用直接回答协议的 0.25／0.35 一致性阈值。</p>' : `<p>工程参考区间：距离 ≤ ${number(c.thresholds?.match ?? 0.25,2)} 提示行为分布相近，> ${number(c.thresholds?.mismatch ?? 0.35,2)} 提示明显差异，中间区域建议复测；协议、有效证据和诊断条件可以使结论降级。</p>`}<p>阈值尚未针对当前部署校准，不是统计显著性或身份置信度。</p></details></section>` : ''}<section class="fp-card"><h2>自检如何解读</h2><p>${!selfcheckAvailable ? '样本或覆盖不足，不能评价稳定性。需完整采集，且交替分半与时间分半各至少有 4 格可比证据；请重新采集后复核。' : split.warning ? '两种分半检查中存在异常波动。请结合逐格样本与通道诊断复测，不能仅据此断言供应商更换或混用后端。' : '本次分半未提示明显不稳定。这个结果只覆盖本次采样窗口，不能排除混合后端。'}</p>${warningsHtml([...(d.warnings || []),...(c.warnings || [])])}</section>`;
  }
  function warningsHtml(warnings) { const items = [...new Set(warnings.filter(Boolean))]; return items.length ? `<ul class="fp-warnings">${items.map(x => `<li>${html(x)}</li>`).join('')}</ul>` : ''; }
  function distributionHtml(run) {
    if (!run.artifact?.cells) return '<section class="fp-card fp-quiet"><h2>暂无可展示的分布</h2><p>采集结算后生成每格回答频数。</p></section>';
    const referenceId = comparedReference(run), ref = s.referenceArtifacts.get(referenceId), reference = ref?.data, compareCells = run.comparison?.cells || [];
    return `<section class="fp-card"><div class="fp-section-head"><div><h2>逐格回答分布</h2><p>条形按各侧有效样本归一化；不把缺失样本当作零概率。</p></div><div class="fp-legend"><span><i></i>本次采集</span>${reference ? '<span><i class="reference"></i>参考指纹</span>' : ''}</div></div>${referenceId && !reference ? notice(ref?.error ? '参考分布读取失败：' + ref.error : '正在读取参考分布…',ref?.error ? 'warn' : '') + (ref?.error ? button('retry-reference-artifact','重试参考分布',`data-fp-id="${html(referenceId)}"`) : '') : ''}<div class="fp-table-wrap" data-fp-scroll="distributions"><table class="fp-table"><thead><tr><th>问题格</th><th>有效／总样本</th><th>无效／拒答／空答／错误</th><th>中位延迟</th>${run.comparison ? '<th>对照距离</th>' : ''}</tr></thead><tbody>${Object.entries(run.artifact.cells).map(([id,cell]) => { const c = compareCells.find(x => x.cell_id === id); return `<tr><th>${html(probeLabel(id))}</th><td>${number(cell.valid_count)} / ${number(cell.total_count)}</td><td>${number(cell.invalid_count)} / ${number(cell.refusal_count)} / ${number(cell.empty_count)} / ${number(cell.error_count)}</td><td>${finite(cell.median_latency_ms) ? number(cell.median_latency_ms) + ' 毫秒' : '未知'}</td>${run.comparison ? `<td>${number(c?.jsd,3)}</td>` : ''}</tr>`; }).join('')}</tbody></table></div><div class="fp-distributions">${Object.entries(run.artifact.cells).map(([id,cell]) => distributionCell(id,cell,reference?.cells?.[id])).join('')}</div></section>`;
  }
  const probeLabel = id => s.catalog?.probes?.find(p => p.cell_id === id)?.label || id;
  function distributionCell(id, cell, reference) {
    const keys = [...new Set([...Object.keys(cell.counts || {}),...Object.keys(reference?.counts || {})])].sort((a,b) => ((cell.counts?.[b] || 0) / Math.max(1,cell.valid_count) + (reference?.counts?.[b] || 0) / Math.max(1,reference?.valid_count || 0)) - ((cell.counts?.[a] || 0) / Math.max(1,cell.valid_count) + (reference?.counts?.[a] || 0) / Math.max(1,reference?.valid_count || 0)));
    return `<details class="fp-distribution" data-fp-details="cell-${html(id)}"><summary><span>${html(probeLabel(id))}</span><small>${keys.length} 种有效回答 · 熵 ${number(cell.entropy_bits,2)} 比特</small></summary><div class="fp-bars">${keys.length ? keys.map(key => { const p = Number(cell.counts?.[key] || 0) / Math.max(1,Number(cell.valid_count || 0)), rp = reference ? Number(reference.counts?.[key] || 0) / Math.max(1,Number(reference.valid_count || 0)) : 0; return `<div class="fp-bar-row"><span class="fp-answer" title="${html(key)}">${html(key)}</span><div><div class="fp-bar-track"><i style="width:${Math.min(100,p * 100)}%"></i><span>${number(cell.counts?.[key] || 0)} · ${percent(p)}</span></div>${reference ? `<div class="fp-bar-track reference"><i style="width:${Math.min(100,rp * 100)}%"></i><span>${number(reference.counts?.[key] || 0)} · ${percent(rp)}</span></div>` : ''}</div></div>`; }).join('') : '<p class="fp-subtle">本格没有有效回答。</p>'}</div></details>`;
  }
  async function loadReferenceArtifact(id, force = false) {
    if (!id || (!force && s.referenceArtifacts.has(id))) return;
    const epoch = s.epoch; s.referenceArtifacts.set(id,{pending:true});
    try { const data = await get(`/fingerprint/references/${encodeURIComponent(id)}/export`); if (epoch === s.epoch) s.referenceArtifacts.set(id,{data}); }
    catch (e) { if (!e.stale && epoch === s.epoch) s.referenceArtifacts.set(id,{error:e.message}); }
    if (epoch === s.epoch && active() && s.tab === 'distribution') render();
  }
  function diagnosticsHtml(run) {
    const d = run.artifact?.diagnostics || run.diagnostics;
    if (!d) return '<section class="fp-card fp-quiet"><h2>等待诊断数据</h2><p>请求结算后显示通道回显、缓存与用量信息。</p></section>';
    const c = d.cache || {}, u = d.usage || {}, models = Object.entries(d.returned_models || {});
    const observed = d.usage_observed_samples || {}, total = run.artifact?.cells ? Object.values(run.artifact.cells).reduce((n,cell) => n + Number(cell.total_count || 0),0) : Number(run.progress?.done || 0);
    const coverage = key => finite(observed[key]) ? `${number(observed[key])} / ${number(total)} 次请求报告` : '回传覆盖未知';
    return `<div class="fp-metrics">${metric('请求错误率',percent(d.error_rate))}${metric('截断率',percent(d.truncated_rate))}${metric('额外文本率',percent(d.extra_text_rate))}${metric('推理令牌',d.reasoning_observed ? '已观测到' : finite(u.reasoning_tokens) ? number(u.reasoning_tokens) : '未知')}</div><section class="fp-card"><div class="fp-section-head"><div><h2>模型回显</h2><p>请求模型：${html(run.endpoint?.model || '未记录')} · 不一致 ${number(d.model_echo_mismatch_count || 0)} 次</p></div></div>${models.length ? `<div class="fp-table-wrap" data-fp-scroll="models"><table class="fp-table"><thead><tr><th>供应商回显的模型名称</th><th>样本数</th></tr></thead><tbody>${models.map(([name,count]) => `<tr><td class="fp-code">${html(name)}</td><td>${number(count)}</td></tr>`).join('')}</tbody></table></div>` : notice('上游未提供模型回显，无法检查名称是否一致。')}<p class="fp-subtle">别名、版本规范化或服务端路由都可能改变回显名称，名称一致也不证明底层模型一致。</p></section><section class="fp-card"><div class="fp-section-head"><div><h2>缓存与延迟</h2><p>仅依据上游用量字段识别缓存；字段缺失记为未知。</p></div></div><div class="fp-table-wrap" data-fp-scroll="cache"><table class="fp-table"><thead><tr><th>分组</th><th>样本数</th><th>中位延迟</th></tr></thead><tbody><tr><th>观测到缓存命中</th><td>${number(c.cached_samples)}</td><td>${finite(c.cached_median_latency_ms) ? number(c.cached_median_latency_ms) + ' 毫秒' : '未知'}</td></tr><tr><th>明确无缓存令牌</th><td>${number(c.uncached_samples)}</td><td>${finite(c.uncached_median_latency_ms) ? number(c.uncached_median_latency_ms) + ' 毫秒' : '未知'}</td></tr><tr><th>缓存状态未知</th><td>${number(c.unknown_samples)}</td><td>不据此推断</td></tr></tbody></table></div><p class="fp-subtle">前缀缓存可能改变延迟表现；缓存与回显只作排查线索，不直接参与身份判断。</p></section><section class="fp-card"><h2>上游报告的用量与费用</h2><div class="fp-metrics fp-metrics-inline">${metric('输入令牌',number(u.prompt_tokens),coverage('prompt_tokens'))}${metric('输出令牌',number(u.completion_tokens),coverage('completion_tokens'))}${metric('推理令牌',number(u.reasoning_tokens),coverage('reasoning_tokens'))}${metric('缓存令牌',number(u.cached_tokens),coverage('cached_tokens'))}</div><div class="fp-cost"><span>上游报告费用</span><strong>${finite(u.reported_cost) ? number(u.reported_cost,6) : '未知'}</strong><span>${finite(u.reported_cost) ? '已报告金额合计；计价单位沿用上游，不自行换算' : '上游未完整报告费用，不能视为免费'}</span></div><p class="fp-subtle">费用回传：${html(coverage('reported_cost'))}。缺失字段显示未知；覆盖不足时只呈现已回传部分，不按令牌数虚构费用。</p>${Number(d.token_budget_exceeded_count) > 0 ? notice(`有 ${number(d.token_budget_exceeded_count)} 次请求报告的输出令牌超过固定上限 ${number(run.protocol?.max_tokens || 16)}，这是参数回报不一致线索，不能单独据此认定隐藏推理。`,'warn') : ''}</section>${warningsHtml(d.warnings || [])}`;
  }
  function protocolHtml(p, run = {}) {
    if (!p) return '<section class="fp-card">协议尚未读取。</section>';
    const keys = [['协议版本',p.version],['协议标识',p.id],['问题集摘要',p.battery_hash],['归一化版本',p.normalizer_version],['问题格数',Array.isArray(p.cells) ? p.cells.length : '未知'],['每格采样数',p.samples_per_cell],['并发数',p.concurrency],['RPM（每分钟请求数）',finite(p.rpm) ? p.rpm : '旧协议：未设速率上限'],['单次超时',finite(p.timeout_seconds) ? `${p.timeout_seconds} 秒` : '未知'],['温度',p.temperature === null ? '不传，沿用接口默认' : p.temperature],['Top-p（核采样）',p.top_p === null ? '不传，沿用接口默认' : p.top_p],[reasoningProtocol(p) ? '推理与正文合计上限' : '输出词元上限',p.max_tokens],['自动重试',p.max_retries],['适配器',adapterLabel(p.adapter)],['令牌参数字段',p.token_field],['采集日期',date(run.artifact?.collected_at || run.collected_at)],['排程种子',run.artifact?.seed],['来源说明',run.source_note || '未提供']];
    return `<section class="fp-card"><div class="fp-section-head"><div><h2>冻结协议快照</h2><p>协议字段变化后，旧参考不直接复用。排程种子用于复现请求队列。</p></div></div><dl class="fp-protocol">${keys.map(([label,value]) => `<div><dt>${html(label)}</dt><dd>${html(value ?? '未知')}</dd></div>`).join('')}</dl><details class="fp-details" data-fp-details="protocol-json"><summary>完整协议 JSON（结构化数据）</summary><pre class="fp-code-block">${html(JSON.stringify(p,null,2))}</pre></details>${p.system_prompts ? `<details class="fp-details" data-fp-details="system-prompts"><summary>查看冻结的系统提示词</summary><pre class="fp-code-block">${html(typeof p.system_prompts === 'string' ? p.system_prompts : JSON.stringify(p.system_prompts,null,2))}</pre></details>` : ''}</section><section class="fp-card"><h2>方法与实现来源</h2>${methodHtml()}<dl class="fp-protocol"><div><dt>方法版本</dt><dd>${html(run.artifact?.source?.method_version || s.catalog?.method_version || '未知')}</dd></div><div><dt>来源提交</dt><dd>${html(run.artifact?.source?.commit || s.catalog?.source_commit || '未知')}</dd></div><div><dt>许可证</dt><dd>${html(s.catalog?.license || '见来源项目')}</dd></div></dl></section>`;
  }
  function samplesHtml(run) {
    const page = s.samplePage;
    const rows = (page?.samples || []).map(sample => `<tr><th><span>${number(Number(sample.index) + 1)}</span><small>${html(probeLabel(sample.cell_id))}</small></th><td>${UI.tag(CATEGORIES[sample.category] || sample.category,sample.category === 'valid' ? 'neutral' : 'warning')}${sample.truncated ? '<small>已截断</small>' : ''}</td><td>${html(sample.normalized ?? '—')}</td><td>${html(UI.formatTimestamp(sample.timing?sample.timing.started_at:sample.started_at))}</td><td>${finite(sample.latency_ms) ? number(sample.latency_ms) + ' 毫秒' : '未知'}</td><td>${UI.textButton(sample.error?'查看错误与证据':'查看证据',{action:'showFingerprintSample',args:[sample.index]})}</td></tr>`).join('');
    return `<section class="fp-card"><div class="fp-section-head"><div><h2>原始请求证据</h2><p>按发送编号分页；查看证据可核查完整时间、原文与用量。原文最多保留 4096 字符，不含密钥。</p></div><div class="fp-head-actions">${button('reload-samples','刷新样本')}${!running(run) && run.artifact ? `<a class="btn fp-button" href="/fingerprints/${encodeURIComponent(run.id)}/samples-export" download>导出全部样本</a>` : ''}</div></div>${s.sampleError ? notice('样本读取失败：' + s.sampleError,'warn') + button('reload-samples','重新加载样本') : ''}${s.samplePending && !page ? '<p class="fp-subtle">正在读取样本…</p>' : rows ? `<div class="fp-table-wrap" data-fp-scroll="samples"><table class="fp-table fp-sample-table"><thead><tr><th>编号／问题</th><th>结果</th><th>规范化回答</th><th>开始时间（新加坡）</th><th>延迟</th><th>证据详情</th></tr></thead><tbody>${rows}</tbody></table></div>` : '<div class="fp-quiet"><p>当前页暂无样本；正在运行时可点击刷新。</p></div>'}<div class="fp-pagination"><span>${page?.total ? `${number(page.offset + 1)}–${number(Math.min(page.total,page.offset + page.samples.length))} / ${number(page.total)} 条` : '0 条样本'}</span><div>${button('sample-page','上一页',`data-fp-offset="${Math.max(0,s.sampleOffset - 50)}" ${s.samplePending || s.sampleOffset <= 0 ? 'disabled' : ''}`)}${button('sample-page','下一页',`data-fp-offset="${s.sampleOffset + 50}" ${s.samplePending || !page || page.offset + page.samples.length >= page.total ? 'disabled' : ''}`)}</div></div></section>`;
  }
  function showSample(index) {
    const sample = s.samplePage?.samples?.find(item=>Number(item.index) === index);
    if (!sample) { toastMessage('该样本不在当前页，请重新读取后查看。'); return; }
    const meta = UI.methodCards([{title:'请求结果',lines:[CATEGORIES[sample.category] || sample.category,`状态码：${sample.status_code ?? '未知'}`,`结束原因：${sample.finish_reason || '未提供'}`,sample.truncated?'达到输出上限，已截断':''].filter(Boolean)},{title:'响应来源',lines:[`模型回显：${sample.returned_model || '未提供'}`,`改写编号：${Number(sample.paraphrase_index) + 1}`]}]);
    const body = (sample.error ? UI.notice(sample.error,{tone:'danger'}) : '') + meta
      + UI.timeRecords(sample,{request:true,key:'fingerprint-sample:'+s.selected+':'+sample.index})
      + UI.section({title:'回答原文',bodyHtml:`<pre class="fp-code-block">${html(sample.raw || '（无回答原文）')}</pre>`})
      + UI.section({title:'用量 JSON（结构化数据）',bodyHtml:`<pre class="fp-code-block">${html(JSON.stringify(sample.usage || {},null,2))}</pre>`});
    openModal(`第 ${Number(sample.index) + 1} 条请求 · ${probeLabel(sample.cell_id)}`,UI.pageShell(body),button('close-modal','关闭证据'));
  }
  async function loadSamples(offset = 0) {
    const seq = ++s.sampleSequence, selection = s.selection, id = s.selected, epoch = s.epoch;
    if (!id) return;
    s.sampleOffset = offset; s.samplePending = true; s.sampleError = ''; render();
    try { const data = await get(`/fingerprints/${encodeURIComponent(id)}/samples?offset=${offset}&limit=50`); if (seq === s.sampleSequence && selection === s.selection) s.samplePage = data; }
    catch (e) { if (!e.stale && seq === s.sampleSequence && selection === s.selection && epoch === s.epoch) s.sampleError = e.message; }
    finally { if (seq === s.sampleSequence && selection === s.selection && epoch === s.epoch) { s.samplePending = false; render(); } }
  }
  function referenceLibrary() {
    return `<div class="fp-workspace">${UI.pageHeader({kicker:'模型指纹 · 可追溯参考',title:'参考库',description:'保存来源、采集日期和协议快照，为下一次对照提供依据。',actionsHtml:button('import','导入指纹') + button('reload-references','刷新参考库')})}${s.referenceError ? notice('参考库读取失败：' + s.referenceError,'warn') + button('retry-data','重新加载') : ''}<section class="fp-card"><div class="fp-section-head"><h2>${s.references.length ? `${s.references.length} 份参考指纹` : '还没有可信参考'}</h2><span class="fp-subtle">有效期策略：${s.catalog?.reference_max_age_days || 30} 天</span></div>${s.references.length ? `<div class="fp-reference-grid">${s.references.map(ref => { const health = referenceHealth(ref), p = protocolOf(ref); return `<article class="fp-reference-card"><div class="fp-reference-title"><div>${icon('fingerprint')}<h3>${html(ref.name || '未命名参考')}</h3></div><span class="fp-status ${health.tone}">${html(health.text)}</span></div><p>${html(ref.endpoint?.model || '模型未记录')}</p><dl><div><dt>采集时间</dt><dd>${html(date(ref.collected_at))}</dd></div><div><dt>协议</dt><dd class="fp-code">${html(p.id ? p.id.slice(0,16) + '…' : '外部协议／未知')}</dd></div><div><dt>来源</dt><dd>${html(ref.source_note || '未声明')}</dd></div></dl><div class="fp-reference-actions">${button('reference-detail','查看详情',`data-fp-id="${html(ref.id)}"`)}${button('create-from-reference','用此参考采集',`data-fp-id="${html(ref.id)}" ${health.usable ? '' : 'disabled'} title="${html(health.usable ? '沿用参考协议创建目标采集' : health.text)}"`)}</div></article>`; }).join('')}</div>` : `<div class="fp-library-empty">${icon('fingerprint')}<h3>先为对照准备一份可追溯参考</h3><p>连接你确认可信的来源，选择一个模型，完整采集后自动保存到参考库。也可导入已有指纹文件。</p><p class="fp-subtle">点击记录栏“采集参考指纹”开始。外部文件会保留原协议状态；导入成功并不意味着可以直接对照。</p></div>`}</section><section class="fp-card fp-library-policy"><h2>参考不是永久基准</h2><p>协议变更、采样参数变化或参考过期时，需要重新采集。来源可信由用户声明，不能由一个模型名称自动证明。</p></section></div>`;
  }
  const adapterLabel = id => s.catalog?.adapters?.find(a=>a.id===id)?.label || ({standard:'标准接口',deepseek:'DeepSeek（深度求索）：关闭思考',openrouter:'OpenRouter（模型聚合平台）',zhipu:'智谱',openai:'OpenAI（模型服务商）',openai_reasoning:'OpenAI（模型服务商）：保留思考',deepseek_reasoning:'DeepSeek（深度求索）：保留思考',zhipu_reasoning:'智谱：保留思考'})[id] || String(id || '未知');
  const selectedAdapter = () => s.formProtocol || s.catalog?.adapters?.find(a=>a.id===$('fpAdapter')?.value) || {max_tokens:16,temperature:1,top_p:1};
  const presetCells = preset => Array.isArray(preset?.cells) ? preset.cells.length : Number(preset?.cells || 0);
  const findPreset = id => s.catalog?.presets?.find(p => p.id === id);
  function matchPreset(p) {
    if (p?.preset && findPreset(p.preset)) return p.preset;
    const cells = Array.isArray(p?.cells) ? p.cells.map(x => typeof x === 'string' ? x : x.cell_id).sort().join('|') : '';
    return s.catalog?.presets?.find(preset => {
      const expected = Array.isArray(preset.cells) ? preset.cells : (s.catalog?.probes || []).slice(0, Number(preset.cells)).map(x => x.cell_id);
      return expected.length > 0 && expected.map(x => typeof x === 'string' ? x : x.cell_id).sort().join('|') === cells;
    })?.id || '';
  }
  const defaultCreateMode = () => s.view === 'references' ? 'reference' : 'selfcheck';
  async function openCreate(mode = defaultCreateMode(), referenceId = '') {
    window.clearTransientFeedback?.();
    if (!Object.hasOwn(MODES,mode)) mode = defaultCreateMode();
    if (!s.initialized) init();
    if (s.drawerOpen) { $('fpName')?.focus(); return; }
    if (typeof toggleConfig === 'function') toggleConfig(false);
    closeModal(); s.drawerOpen = true; s.drawerSession++; s.submitting = false; s.opener = document.activeElement;
    resetModelDiscovery(); updateCreateHeading(mode);
    $('fingerprintDrawer').classList.add('mobile-open'); $('fingerprintDrawer').inert = false; $('fingerprintDrawer').setAttribute('aria-hidden','false'); $('fingerprintBackdrop').hidden = false;
    syncInert();
    $('fpCreateBody').innerHTML = '<div class="fp-quiet"><h2>正在准备采集协议</h2><p>读取问题集、执行限制和参考库…</p></div>';
    $('fpFormError').hidden = true; $('fpSubmit').disabled = true;
    const session = s.drawerSession;
    await ensureData(Boolean(s.catalogError || s.referenceError));
    if (session !== s.drawerSession || !s.drawerOpen) return;
    if (!s.catalog || s.referenceError) { $('fpCreateBody').innerHTML = notice('创建配置读取失败：' + (s.catalogError || s.referenceError || '协议暂不可用'),'warn') + button('retry-create','重新加载'); return; }
    buildCreate(mode,referenceId);
    $('fpName').focus({preventScroll:true});
  }
  function buildCreate(mode, referenceId) {
    connectionId=''; connectionRevision='';
    resetModelDiscovery(); s.adapterManual = false; s.adapterAuto = null;
    s.protocolLocked = false; s.unlockedProtocolValues = null;
    const c = s.catalog, presets = c.presets || [], initial = findPreset('standard') || presets[0];
    const numeric = (id,label,value,min,max) => UI.field({id,label,controlHtml:UI.input({id,type:'number',value,min,max,step:1,required:true})});
    const modePicker = UI.field({id:'fpMode',label:'本次要做什么',controlHtml:UI.select({id:'fpMode',value:mode,options:Object.entries(MODES)}).replace('<select ','<select name="fpMode" ')});
    const identity = UI.inlineFields(modePicker + UI.field({id:'fpName',label:'记录名称',required:true,controlHtml:UI.input({id:'fpName',required:true,placeholder:'例如：主通道 · 稳定性自检'}).replace('<input ','<input name="name" maxlength="120" ')})) +
      `<section id="fpReferenceSection" class="fp-form-section" hidden>${UI.field({id:'fpReference',label:'选择参考指纹',required:true,controlHtml:`<select id="fpReference"><option value="">请选择一份当前有效的参考</option>${s.references.map(ref => { const h = referenceHealth(ref); return `<option value="${html(ref.id)}" ${h.usable ? '' : 'disabled'}>${html(ref.name)} · ${html(h.text)}</option>`; }).join('')}</select>`,hint:'只采集目标通道；参考侧复用本地证据。'})}<div id="fpReferenceNotice"></div></section>`;
    const connection = UI.formSectionTitle(1,'连接与模型') + `<div id="fpConnectionPicker"></div><div id="fpManualConnection">` +
      UI.field({id:'fpUrl',label:'接口地址',required:true,controlHtml:'<input id="fpUrl" name="url" type="url" required placeholder="https://api.example.com/v1" autocomplete="off" spellcheck="false">'}) +
      UI.field({id:'fpKey',label:'接口密钥',controlHtml:'<div class="fp-secret"><input id="fpKey" name="api_key" type="password" placeholder="本地免鉴权服务可留空" autocomplete="new-password" spellcheck="false"><button type="button" class="btn" data-fp-action="toggle-key" aria-label="显示接口密钥" aria-pressed="false">显示</button></div>'}) +
      `</div><div class="field fp-model-field"><label id="fpModelLabel" for="fpModelSelect">请求模型 <span class="field-required">必填</span></label><div id="fpModelListPanel" class="model-row fp-model-row"><select id="fpModelSelect" required disabled aria-describedby="fpModelHelp fpModelStatus"><option value="">请先选择连接，再获取模型</option></select><button id="fpFetchModels" class="icon-btn" type="button" data-fp-action="fetch-models" title="从当前接口获取可用模型列表">获取模型</button></div><div class="model-fallback fp-model-fallback"><button id="fpManualModelToggle" class="text-btn" type="button" data-fp-action="model-input-mode" data-fp-model-mode="manual" aria-expanded="false" aria-controls="fpModelManualPanel">无法获取列表？手动指定模型</button></div><div id="fpModelManualPanel" hidden><input id="fpModel" name="model" maxlength="256" placeholder="填写服务商支持的准确模型名称" autocomplete="off" spellcheck="false" disabled aria-describedby="fpModelManualHelp fpModelStatus"><p id="fpModelManualHelp" class="fp-field-hint">模型名称需由服务商确认。</p></div><p id="fpModelStatus" class="fp-model-status" role="status" hidden></p><p id="fpModelHelp" class="fp-field-hint">获取模型列表不会启动采集。</p></div>`;
    const protocol = UI.formSectionTitle(2,'采集计划') + '<div id="fpProtocolFields">' +
      UI.inlineFields(UI.field({id:'fpPreset',label:'采集范围',controlHtml:UI.select({id:'fpPreset',value:initial?.id,options:presets.map(p=>[p.id,`${p.id === 'quick' ? '快速采集' : p.label} · ${presetCells(p)} 格`])}),hint:'只改变问题范围，保留已填写的每格请求数。'}) + numeric('fpSamples','每格请求数',initial?.samples_per_cell || 20,10,100)) +
      UI.inlineFields(numeric('fpConcurrency','并发上限',c.limits?.default_concurrency || 20,1,c.limits?.max_concurrency || 20) + numeric('fpRpm','RPM（每分钟请求数）',c.limits?.default_rpm || 100,1,c.limits?.max_rpm || 600000)) +
      UI.field({id:'fpAdapter',label:'回答协议与接口适配',controlHtml:UI.select({id:'fpAdapter',value:'standard',options:(c.adapters || [{id:'standard'}]).map(a=>[a.id,adapterLabel(a.id)])}).replace('<select ','<select aria-describedby="fpAdapterHint" ')}) +
      `</div><div id="fpProtocolLock" class="field-hint"></div><div id="fpAdapterHint" class="fp-notice" role="status" hidden></div>`;
    const source = `<section id="fpSourceSection" class="fp-form-section" hidden><h2>参考来源</h2><div class="fp-field"><label for="fpSourceNote">可追溯的来源说明 <em>必填</em></label><textarea id="fpSourceNote" maxlength="1000" rows="3" placeholder="例如：供应商官方账户直连，已核对模型版本和后台配置"></textarea></div><label class="fp-checkbox"><input type="checkbox" id="fpTrusted"><span>我已独立确认来源可信，并接受系统仅记录此声明。</span></label></section>`;
    const budget = `<section class="fp-budget" aria-live="polite"><div class="fp-budget-top"><div><span>计划请求数量</span><strong id="fpRequestCount">—</strong></div><span id="fpBudgetFormula"></span></div><p id="fpBudgetNote"></p><label class="fp-checkbox"><input type="checkbox" id="fpRiskAck" required><span id="fpRiskLabel">我确认本次采集会向目标发出请求，并可能产生上游费用。</span></label></section>`;
    const advanced = `<details id="fpAdvanced" class="gateway-single-advanced"><summary>高级设置与协议说明</summary>` +
      numeric('fpTimeout','单次超时（秒）',30,5,120) +
      `<details class="fp-details" id="fpProbePreview"></details><details class="fp-details"><summary>固定项与适配说明</summary><p>直接回答协议：温度 1.0、核采样 1.0、输出上限 16 词元。保留思考协议：推理与正文合计上限 4096 词元，不传温度和核采样参数，低强度思考。两者都不重试，每次只有系统与用户两条消息，无历史记忆、无工具调用。</p><p>范围、样本数、并发、速率、超时和适配都会写入冻结协议。保留思考有独立协议版本与参考，只展示探索性距离。调整后应重新采集参考；旧协议保持原样。</p><p>接口不支持固定参数时停止并保留错误证据，不自动切换协议或提高预算。</p></details></details>`;
    $('fpCreateBody').innerHTML = identity + connection + protocol + source + budget + advanced;
    if (referenceId && [...$('fpReference').options].some(o => o.value === referenceId && !o.disabled)) $('fpReference').value = referenceId;
    renderConnectionPicker();window.ConnectionUI?.refresh().then(()=>{if(s.drawerOpen)renderConnectionPicker()}).catch(()=>{});paintModelControls(); applyMode();
  }
  const createMode = () => document.querySelector('select[name="fpMode"]')?.value || 'selfcheck';
  function updateCreateHeading(mode) {
    const copy = {
      selfcheck:['新建通道自检','独立采集一个模型，检查本次采样窗口内的分布稳定性。','例如：主通道 · 九月稳定性自检'],
      reference:['采集参考指纹','连接已确认可信的来源，选择一个模型；完整采集后自动保存到参考库。','例如：官方直连 · 九月参考指纹'],
      verify:['新建参考对照','沿用参考协议采集一个目标模型，再比较两侧回答分布。','例如：候选通道 · 对照九月参考'],
    }[mode] || ['新建模型指纹','冻结协议，保留可复核证据。','填写本次采集名称'];
    if ($('fpCreateTitle')) $('fpCreateTitle').textContent = copy[0];
    if ($('fpCreateSubtitle')) $('fpCreateSubtitle').textContent = copy[1];
    if ($('fpName')) $('fpName').placeholder = copy[2];
  }
  function resetModelDiscovery() {
    s.modelDiscoverySequence++;
    s.modelInputMode = 'list'; s.modelOptions = []; s.modelDiscoveryPending = false;
    s.modelDiscoveryStatus = ''; s.modelDiscoveryTone = '';
  }
  function selectedModel() {
    if (s.modelInputMode === 'manual') return $('fpModel')?.value.trim() || '';
    const value = $('fpModelSelect')?.value || '';
    return s.modelOptions.includes(value) ? value : '';
  }
  function paintModelControls() {
    const select = $('fpModelSelect'); if (!select) return;
    const previous = select.value, manual = s.modelInputMode === 'manual';
    const placeholder = s.modelDiscoveryPending ? '正在获取模型列表…' : s.modelOptions.length ? '请选择一个模型，不会自动选择' : '请先填写地址和密钥，再获取模型';
    select.innerHTML = `<option value="">${placeholder}</option>${s.modelOptions.map(model => `<option value="${html(model)}">${html(model)}</option>`).join('')}`;
    select.value = s.modelOptions.includes(previous) ? previous : '';
    select.disabled = manual || s.modelDiscoveryPending || !s.modelOptions.length;
    select.required = !manual;
    $('fpModel').disabled = !manual; $('fpModel').required = manual;
    $('fpModelListPanel').hidden = manual; $('fpModelManualPanel').hidden = !manual;
    $('fpModelLabel').setAttribute('for',manual ? 'fpModel' : 'fpModelSelect');
    const fallback = $('fpManualModelToggle');
    fallback.dataset.fpModelMode = manual ? 'list' : 'manual';
    fallback.setAttribute('aria-expanded',String(manual));
    fallback.textContent = manual ? '返回模型列表选择' : '无法获取列表？手动指定模型';
    const fetchButton = $('fpFetchModels');
    fetchButton.disabled = s.modelDiscoveryPending; fetchButton.setAttribute('aria-busy',String(s.modelDiscoveryPending));
    fetchButton.textContent = s.modelDiscoveryPending ? '获取中…' : s.modelOptions.length ? '刷新模型' : '获取模型';
    $('fpModelListPanel').classList.toggle('is-loading',s.modelDiscoveryPending);
    const status = $('fpModelStatus'); status.textContent = s.modelDiscoveryStatus;
    status.hidden = manual || !s.modelDiscoveryStatus; status.className = `fp-model-status ${s.modelDiscoveryTone}`;
    $('fpModelHelp').hidden = manual;
  }
  function invalidateModelConnection() {
    const hadModels = s.modelOptions.length > 0 || s.modelDiscoveryPending || !!selectedModel() || s.modelDiscoveryStatus.startsWith('连接信息已改变');
    s.modelDiscoverySequence++; s.modelOptions = []; s.modelDiscoveryPending = false;
    s.modelDiscoveryStatus = hadModels ? '连接信息已改变，模型列表与选择已清空；请重新获取或手动填写。' : '';
    s.modelDiscoveryTone = '';
    if ($('fpModelSelect')) $('fpModelSelect').value = '';
    if ($('fpModel')) $('fpModel').value = '';
    if ($('fpRiskAck')) $('fpRiskAck').checked = false;
    paintModelControls();
  }
  function setModelInputMode(mode) {
    if (!['list','manual'].includes(mode) || mode === s.modelInputMode) return;
    s.modelDiscoverySequence++;
    if (s.modelDiscoveryPending) {
      s.modelDiscoveryPending = false;
      s.modelDiscoveryStatus = '已切换填写方式；上一条模型列表响应不会更改当前选择。';
      s.modelDiscoveryTone = '';
    }
    s.modelInputMode = mode;
    if ($('fpRiskAck')) $('fpRiskAck').checked = false;
    paintModelControls();
    (mode === 'manual' ? $('fpModel') : s.modelOptions.length ? $('fpModelSelect') : $('fpFetchModels'))?.focus({preventScroll:true});
  }
  function modelDiscoveryCurrent(request) {
    return s.drawerOpen && signedIn() && request.sequence === s.modelDiscoverySequence && request.session === s.drawerSession && request.epoch === s.epoch && $('fpUrl')?.value.trim() === request.connection.url && $('fpKey')?.value === request.connection.api_key && connectionId === (request.connection.connection_id||'');
  }
  async function discoverModels() {
    if (!s.drawerOpen || s.modelDiscoveryPending) return;
    const url = $('fpUrl')?.value.trim() || '';
    let invalid = connectionIssue();
    if (!invalid && !safeUrl(url)) invalid = '请先填写有效的接口地址，再获取模型列表。';
    else if (!invalid) { const parsed = new URL(url); if (parsed.username || parsed.password || parsed.search || parsed.hash) invalid = '接口地址不能携带账号、密钥、查询参数或片段，请使用专用密钥字段。'; }
    if (invalid) {
      s.modelDiscoveryStatus = invalid; s.modelDiscoveryTone = 'warn'; paintModelControls(); $('fpUrl')?.focus(); return;
    }
    const previousModel = s.modelInputMode === 'list' ? selectedModel() : '', previousOptions = [...s.modelOptions];
    const request = {sequence:++s.modelDiscoverySequence,session:s.drawerSession,epoch:s.epoch,connection:{url,api_key:$('fpKey').value,...(connectionId?{connection_id:connectionId}:{})}};
    s.modelInputMode = 'list'; s.modelDiscoveryPending = true;
    s.modelDiscoveryStatus = previousModel ? '正在刷新模型目录；仍有效的模型选择会保留，不发送采集题目。' : '仅请求一次模型目录；不会发送采集题目。'; s.modelDiscoveryTone = '';
    if (!previousModel) { $('fpModelSelect').value = ''; $('fpRiskAck').checked = false; }
    paintModelControls();
    try {
      const data = await post('/fingerprint/models',request.connection);
      if (!modelDiscoveryCurrent(request)) return;
      if (!Array.isArray(data.models)) throw new Error('接口未返回可用的模型列表，请切换为手动填写模型名称。');
      s.modelOptions = [...new Set(data.models.filter(model => typeof model === 'string' && model.trim()))];
      const retained = previousModel && s.modelOptions.includes(previousModel);
      $('fpModelSelect').value = retained ? previousModel : '';
      if (previousModel && !retained) $('fpRiskAck').checked = false;
      s.modelDiscoveryStatus = s.modelOptions.length ? `已获取 ${number(s.modelOptions.length)} 个模型，${retained ? '已保留当前选择' : previousModel ? '之前选择的模型已不在列表，请重新选择' : '请明确选择一个'}。列表来自上游回传，不证明模型身份。` : '接口返回空模型列表；请手动填写模型名称，或核对地址与密钥后重新获取。';
      s.modelDiscoveryTone = s.modelOptions.length ? '' : 'warn';
    } catch (e) {
      if (!e.stale && modelDiscoveryCurrent(request)) {
        const message = typeof e.message === 'string' && e.message.trim() ? e.message.trim() : '模型目录暂不可用';
        const fallback = /手动填写|手填/.test(message) ? '' : ' 可切换为手动填写模型名称。';
        s.modelOptions = previousOptions; $('fpModelSelect').value = previousModel;
        s.modelDiscoveryStatus = previousOptions.length ? `刷新模型列表失败：${message} 已保留上次列表与选择，可重试刷新或手动填写。` : `获取模型列表失败：${message}${fallback}`; s.modelDiscoveryTone = 'warn';
      }
    } finally {
      const current = modelDiscoveryCurrent(request);
      const sameContext = s.drawerOpen && signedIn() && request.sequence === s.modelDiscoverySequence && request.session === s.drawerSession && request.epoch === s.epoch;
      request.connection.api_key = '';
      if (current) { s.modelDiscoveryPending = false; paintModelControls(); updateAdapterHint(); }
      else if (sameContext) invalidateModelConnection();
    }
  }
  function isDeepseekOfficial(url) {
    try { return new URL(url).hostname.toLowerCase() === 'api.deepseek.com'; } catch { return false; }
  }
  function isZhipuOfficial(url) {
    try { return ['api.z.ai','open.bigmodel.cn'].includes(new URL(url).hostname.toLowerCase()); } catch { return false; }
  }
  const glmAlwaysThinking = model => ['glm-5.3','glm-5.3-flash','glm-5.3-flashx'].includes(String(model || '').toLowerCase());
  function updateAdapterHint() {
    const select = $('fpAdapter'), hint = $('fpAdapterHint');
    if (!select || !hint) return;
    if (createMode() === 'verify' && !s.formProtocol) { hint.hidden = true; return; }
    const official = isDeepseekOfficial($('fpUrl')?.value.trim()), locked = createMode() === 'verify';
    if (!locked && !s.adapterManual) {
      const recommended = official ? 'deepseek' : 'standard';
      if (official || s.adapterAuto) {
        if (select.value !== recommended && [...(select.options || [])].some(x => x.value === recommended)) {
          select.value = recommended;
          if ($('fpRiskAck')) $('fpRiskAck').checked = false;
        }
        s.adapterAuto = official ? recommended : null;
      }
    }
    hint.hidden = false;
    const safe = ['deepseek','deepseek_reasoning'].includes(select.value), thinking = reasoningProtocol(selectedAdapter());
    const glm = glmAlwaysThinking(selectedModel()), zhipu = isZhipuOfficial($('fpUrl')?.value.trim());
    hint.className = 'fp-notice' + (thinking || glm || (official && !safe) ? ' warn' : '');
    hint.textContent = official && !safe
      ? locked ? '参考未选择 DeepSeek（深度求索）专用适配，不能直接复用；请明确选择关闭或保留思考后重新建立参考。' : '此官方接口默认开启思考；请选择 DeepSeek（深度求索）的关闭或保留思考适配。'
      : glm && select.value !== 'zhipu_reasoning' ? `${selectedModel()}（通用语言模型）官方版本不能关闭思考，16 词元预算可能在正文前耗尽。${zhipu ? '请明确选择“智谱：保留思考”并重新确认 4096 词元上限及费用。' : '中转是否兼容智谱参数需向提供方确认；模型名称不能证明协议兼容。确认后可选择“智谱：保留思考”。'}${locked ? '当前参考已冻结，需重新建立思考协议参考。' : ''}不会自动切换或增加预算。`
      : select.value === 'zhipu_reasoning' ? '智谱保留思考：低强度思考，单次输出上限 4096 词元，包含推理；只采集最终正文。GLM-5.3 系列不能关闭思考；中转需确认支持这些参数。比原 16 词元有更高费用风险，仅供探索性对照。'
      : thinking ? '保留思考：只统计最终正文；单次推理与正文合计上限 4096 词元，比直接回答的 16 词元有更高费用风险。仅供探索性对照，不能混用直接回答参考。'
      : select.value === 'deepseek' ? '本次明确关闭 DeepSeek（深度求索）的思考模式，保留 16 词元输出上限。此设置会写入冻结协议，任务运行中不再改参。'
      : '直接回答：单次输出上限 16 词元。默认思考模型应选择可关闭思考的适配；无法关闭时，明确选择对应的保留思考协议。不会自动切换。';
  }
  function applyMode() {
    const mode = createMode(); updateCreateHeading(mode);
    $('fpReferenceSection').hidden = mode !== 'verify'; $('fpSourceSection').hidden = mode !== 'reference';
    $('fpReference').required = mode === 'verify'; $('fpReference').disabled = mode !== 'verify';
    $('fpSourceNote').required = mode === 'reference'; $('fpSourceNote').disabled = mode !== 'reference'; $('fpTrusted').required = mode === 'reference'; $('fpTrusted').disabled = mode !== 'reference';
    $('fpRiskAck').checked = false; $('fpFormError').hidden = true;
    applyReference();
  }
  function applyReference() {
    const mode = createMode(), ref = s.references.find(r => r.id === $('fpReference')?.value), p = protocolOf(ref), preset = matchPreset(p);
    const fields = ['fpPreset','fpSamples','fpConcurrency','fpRpm','fpTimeout','fpAdapter'];
    if (mode === 'verify' && !s.protocolLocked) s.unlockedProtocolValues = Object.fromEntries(fields.map(id=>[id,$(id).value]));
    if (mode !== 'verify' && s.protocolLocked && s.unlockedProtocolValues) for (const id of fields) $(id).value = s.unlockedProtocolValues[id];
    s.protocolLocked = mode === 'verify';
    for (const id of fields) $(id).disabled = mode === 'verify';
    $('fpRpm').placeholder = mode === 'verify' && ref && !finite(p.rpm) ? '原协议未限速' : '';
    let canUse = mode !== 'verify';
    if (mode === 'verify' && ref) {
      const h = referenceHealth(ref); canUse = h.usable && !!preset && [...$('fpAdapter').options].some(o => o.value === p.adapter);
      if (canUse) { $('fpPreset').value = preset; $('fpSamples').value = p.samples_per_cell; $('fpConcurrency').value = p.concurrency; $('fpRpm').value = p.rpm ?? ''; $('fpTimeout').value = p.timeout_seconds; $('fpAdapter').value = p.adapter; }
      $('fpReferenceNotice').innerHTML = notice(canUse ? `已锁定「${ref.name}」的采集参数；参考采于 ${date(ref.collected_at)}。` : '该参考无法按当前协议重新采集，请重新建立参考或使用离线对照。',canUse ? '' : 'warn');
    } else $('fpReferenceNotice').innerHTML = mode === 'verify' ? notice(s.references.some(r => referenceHealth(r).usable) ? '选择参考后，范围、样本数、并发、速率及高级参数将锁定。' : '当前没有可直接复用的参考。请先建立可信参考，或在参考库查看已有文件。') : '';
    s.formProtocol = mode === 'verify' && ref && canUse ? p : null;
    const awaitingReference = mode === 'verify' && !canUse;
    $('fpProtocolFields').hidden = awaitingReference; $('fpAdvanced').hidden = awaitingReference;
    $('fpRiskAck').disabled = awaitingReference;
    $('fpProtocolLock').textContent = awaitingReference ? '先选择有效参考，再显示并锁定采集参数。' : mode === 'verify' ? (ref && !finite(p.rpm) ? '沿用旧参考：仅限制并发，未设置每分钟请求数。' : '沿用参考的全部采集参数。') : '请求按速率间隔开始，并发为同时在途的上限；提交后冻结。';
    $('fpSubmit').disabled = !canUse || s.submitting;
    $('fpSubmit').textContent = mode === 'reference' ? '开始采集并建立参考' : mode === 'verify' ? '开始采集并对照' : '开始通道自检';
    updateBudget(); updateAdapterHint();
  }
  function updateBudget() {
    if (createMode() === 'verify' && !s.formProtocol) {
      $('fpRequestCount').textContent = '请选择参考'; $('fpBudgetFormula').textContent = '';
      $('fpBudgetNote').textContent = '选择有效参考后，显示冻结的请求数量、输出上限和费用说明。';
      $('fpRiskLabel').textContent = '选择参考后，请核对本次请求数量与潜在费用。';
      return;
    }
    const preset = findPreset($('fpPreset')?.value), samples = Number($('fpSamples')?.value), valid = Number.isInteger(samples) && samples >= 10 && samples <= 100;
    const cells = presetCells(preset), total = valid && cells > 0 ? cells * samples : 0;
    $('fpRequestCount').textContent = total ? `${number(total)} 次` : '请完善参数';
    $('fpBudgetFormula').textContent = total ? `${cells} 格 × 每格 ${samples} 次` : '';
    const adapter = selectedAdapter(), cap = adapter.max_tokens || 16, thinking = reasoningProtocol(adapter);
    $('fpBudgetNote').textContent = `上游费用无法预估；${total ? `单次${thinking ? '推理与正文合计' : '输出'}上限 ${number(cap)} 词元，全计划上限 ${number(total * cap)} 词元。` : '词元总预算将在参数有效时显示。'}${thinking ? '保留思考比直接回答的 16 词元预算更高；' : '推理可能占用输出预算；'}输入另计，实际费用以回传为准，上限不代表预计用满。`;
    if ($('fpProbePreview')) {
      const probes = (s.catalog?.probes || []).slice(0,cells);
      $('fpProbePreview').innerHTML = `<summary>查看本次问题与改写池（${cells} 格）</summary><p>每次为本格随机抽取一种改写；系统提示词按语言固定。</p>${probes.map(probe => `<div class="fp-probe-preview"><b>${html(probe.label)}</b><p>系统提示词：${html(probe.system_prompt)}</p><ol>${(probe.paraphrases || []).map(text => `<li>${html(text)}</li>`).join('')}</ol></div>`).join('')}`;
    }
    $('fpRiskLabel').textContent = total ? `我确认 ${number(total)} 次请求、单次上限 ${number(cap)} 词元${thinking ? '（包含推理）' : ''}，并承担可能产生的上游费用。` : '我确认本次采集会向目标发出请求，并可能产生上游费用。';
  }
  function syncInert() {
    const gateOpen = $('authGate') && !$('authGate').hidden;
    const baseDrawerOpen = $('config')?.classList.contains('mobile-open');
    const globalModal = typeof modalStack !== 'undefined' && modalStack.length;
    document.querySelector('.app').inert = !!(s.drawerOpen || s.modalOpen || gateOpen || baseDrawerOpen || globalModal);
    document.body.classList.toggle('fp-overlay-open',s.drawerOpen || s.modalOpen);
    if ($('fingerprintDrawer')) $('fingerprintDrawer').inert = !s.drawerOpen || s.modalOpen;
  }
  function closeCreate() {
    if (!s.initialized) return;
    const wasOpen = s.drawerOpen; resetModelDiscovery(); s.drawerOpen = false; s.drawerSession++; s.submitting = false; s.formProtocol = null;
    const key = $('fpKey'); if (key) key.value = '';
    $('fingerprintDrawer').classList.remove('mobile-open'); $('fingerprintDrawer').inert = true; $('fingerprintDrawer').setAttribute('aria-hidden','true'); $('fingerprintBackdrop').hidden = true;
    $('fpCreateBody').innerHTML = ''; syncInert();
    if (wasOpen && s.opener?.isConnected && signedIn()) s.opener.focus({preventScroll:true});
  }
  function formError(message, field) {
    const host = $('fpFormError'); host.textContent = message; host.hidden = false;
    if (field) { const details = $(field)?.closest('details'); if (details) details.open = true; $(field)?.focus(); $(field)?.scrollIntoView({block:'nearest'}); }
  }
  async function submitCreate(event) {
    event.preventDefault();
    if (s.submitting || !s.drawerOpen) return;
    const form = $('fpCreateForm'); const invalid = form.querySelector(':invalid'); if (invalid) { const details = invalid.closest('details'); if (details) details.open = true; } if (!form.reportValidity()) return;
    const mode = createMode(), url = $('fpUrl').value.trim(), model = selectedModel(), name = $('fpName').value.trim();
    if (!name) return formError('请填写记录名称。','fpName');
    if (connectionIssue()) return formError(connectionIssue(),'fpSavedConnection');
    if (!safeUrl(url)) return formError('请填写有效的 HTTP（超文本传输协议）或 HTTPS（安全超文本传输协议）地址。','fpUrl');
    try { const u = new URL(url); if (u.username || u.password || u.hash || u.search) return formError('接口地址不能包含账号、密钥、查询参数或片段，请将密钥填入专用字段。','fpUrl'); } catch { return formError('接口地址格式无效。','fpUrl'); }
    if (isDeepseekOfficial(url) && !['deepseek','deepseek_reasoning'].includes($('fpAdapter').value)) return formError('请选择 DeepSeek（深度求索）的关闭或保留思考适配；若参考参数已锁定，请按同一适配重新建立参考。','fpAdapter');
    if (isZhipuOfficial(url) && glmAlwaysThinking(model) && $('fpAdapter').value !== 'zhipu_reasoning') return formError('此 GLM（通用语言模型）官方模型不能关闭思考。请明确选择“智谱：保留思考”，确认 4096 词元上限及费用后新建；旧参考需重新建立。','fpAdapter');
    if (!model) return formError(s.modelInputMode === 'manual' ? '请填写准确的请求模型名称。' : '请获取模型列表并明确选择一个模型，也可切换为手动填写。',s.modelInputMode === 'manual' ? 'fpModel' : 'fpFetchModels');
    if (!$('fpRiskAck').checked) return formError('请先确认请求数量与潜在费用。','fpRiskAck');
    if (mode === 'reference' && (!$('fpTrusted').checked || !$('fpSourceNote').value.trim())) return formError('建立参考需要来源说明与可信来源声明。','fpSourceNote');
    const ref = s.references.find(r => r.id === $('fpReference').value);
    if (mode === 'verify' && (!ref || !referenceHealth(ref).usable || !matchPreset(protocolOf(ref)))) return formError('请选择当前协议可用且未过期的参考。','fpReference');
    const config = {name,mode,endpoint:{url,model,api_key:$('fpKey').value,...(connectionId?{connection_id:connectionId}:{})},preset:$('fpPreset').value,samples_per_cell:Number($('fpSamples').value),concurrency:Number($('fpConcurrency').value),...(s.formProtocol && !Object.hasOwn(s.formProtocol,'rpm') ? {} : {rpm:Number($('fpRpm').value)}),timeout_seconds:Number($('fpTimeout').value),adapter:$('fpAdapter').value,risk_acknowledged:true};
    if (mode === 'reference') { config.trusted_source = true; config.source_note = $('fpSourceNote').value.trim(); }
    if (mode === 'verify') config.reference_id = $('fpReference').value;
    const session = s.drawerSession; s.submitting = true; $('fpSubmit').disabled = true; $('fpSubmit').textContent = '正在创建…'; $('fpFormError').hidden = true;
    try {
      const data = await post('/fingerprints',config);
      if (session !== s.drawerSession) return;
      if (!data.run?.id) throw new Error('服务未返回采集记录标识，请刷新记录后确认是否创建成功。');
      const run = data.run; s.runs = [run,...s.runs.filter(r => r.id !== run.id)];
      closeCreate(); s.view = 'runs'; await select(run.id); toastMessage('模型指纹任务已创建，采集参数已冻结。');
      if (typeof refresh === 'function') refresh();
    } catch (e) { if (!e.stale && session === s.drawerSession) formError(e.message); }
    finally { config.endpoint.api_key = ''; if (session === s.drawerSession) { s.submitting = false; applyReference(); } }
  }
  function openModal(title, body, footer = '') {
    s.modalSession++; s.modalOpen = true; s.modalBusy = false; s.modalOpener = document.activeElement;
    $('fpModalTitle').textContent = title; $('fpModalBody').innerHTML = body; $('fpModalFooter').innerHTML = footer; $('fpModalFooter').hidden = !footer;
    $('fingerprintModal').hidden = false; $('fpModalBody').scrollTop = 0; syncInert();
    ($('fpModalBody').querySelector('input:not([type="checkbox"]),select,textarea') || $('fingerprintModal').querySelector('.drawer-close'))?.focus({preventScroll:true});
  }
  function closeModal() {
    if (!s.initialized) return;
    const wasOpen = s.modalOpen; s.modalOpen = false; s.modalBusy = false; s.modalSession++; s.refOpen = null;
    $('fingerprintModal').hidden = true; $('fpModalBody').innerHTML = ''; $('fpModalFooter').innerHTML = ''; syncInert();
    if (wasOpen && s.modalOpener?.isConnected && signedIn()) s.modalOpener.focus({preventScroll:true});
  }
  function modalError(error) {
    let el = $('fpModalError');
    if (!el) { el = document.createElement('p'); el.id = 'fpModalError'; el.className = 'fp-form-error'; el.setAttribute('role','alert'); $('fpModalBody').appendChild(el); }
    el.textContent = error; el.scrollIntoView({block:'nearest'});
  }
  function openImport() {
    openModal('导入参考指纹', `<p class="fp-subtle">支持本工作台及上游项目的 JSON（结构化数据）指纹文件。导入会验证结构，不修改或伪造采集协议。</p><div class="fp-field"><label for="fpImportFile">选择文件 <em>必填</em></label><input id="fpImportFile" type="file" accept=".json,application/json"><p class="fp-field-hint">最大 2 兆字节，仅在本地服务处理。</p></div><div class="fp-field"><label for="fpImportName">参考名称 <em>必填</em></label><input id="fpImportName" maxlength="120" placeholder="例如：官方直连 · 九月参考"></div><div class="fp-field"><label for="fpImportSource">来源说明</label><textarea id="fpImportSource" rows="3" maxlength="1000" placeholder="记录文件来源、采集环境和确认方式"></textarea></div><label class="fp-checkbox"><input id="fpImportTrusted" type="checkbox"><span>我已独立确认该文件来自可信来源（勾选时需填写来源说明）。</span></label><div id="fpImportPreview"></div>`, `${button('close-modal','取消')}${button('submit-import','验证并导入','','primary')}`);
  }
  async function previewImport() {
    const file = $('fpImportFile')?.files?.[0]; if (!file) return;
    if (!$('fpImportName').value.trim()) $('fpImportName').value = file.name.replace(/\.json$/i,'').slice(0,120);
    $('fpImportPreview').innerHTML = notice(`${file.name} · ${number(file.size / 1024,1)} 千字节。导入后由服务验证协议、日期与证据。`);
  }
  async function submitImport() {
    if (s.modalBusy) return;
    const file = $('fpImportFile')?.files?.[0], name = $('fpImportName')?.value.trim(), sourceNote = $('fpImportSource')?.value.trim(), trusted = $('fpImportTrusted')?.checked;
    if (!file) return modalError('请选择指纹文件。');
    if (file.size > 2 * 1024 * 1024) return modalError('文件超过 2 兆字节，请选择单份指纹文件。');
    if (!name) { $('fpImportName').focus(); return modalError('请填写参考名称。'); }
    if (trusted && !sourceNote) { $('fpImportSource').focus(); return modalError('声明可信来源时，需要填写可追溯的来源说明。'); }
    const session = s.modalSession; s.modalBusy = true; modalBusy(true);
    try {
      let data; try { data = JSON.parse(await file.text()); } catch { throw new Error('文件不是有效的 JSON（结构化数据），请检查文件内容。'); }
      if (session !== s.modalSession) return;
      const envelope = {data,name,source_note:sourceNote,trusted_source:!!trusted};
      if (new TextEncoder().encode(JSON.stringify(envelope)).length > 2 * 1024 * 1024) throw new Error('包含名称与来源说明后的导入内容超过 2 兆字节，请选择更小的指纹文件。');
      await post('/fingerprint/references/import',envelope);
      if (session !== s.modalSession) return;
      closeModal(); s.view = 'references'; await loadReferences(); render(); toastMessage('指纹已导入，请检查参考状态与协议兼容性。');
    } catch (e) { if (!e.stale && session === s.modalSession) modalError(e.message); }
    finally { if (session === s.modalSession) { s.modalBusy = false; modalBusy(false); } }
  }
  function modalBusy(busy) { $('fpModalFooter').querySelectorAll('button:not([data-fp-action="close-modal"])').forEach(el => { el.disabled = busy; el.setAttribute('aria-busy',String(busy)); }); }
  function openReference(id) {
    const ref = s.references.find(r => r.id === id); if (!ref) return;
    const health = referenceHealth(ref); s.refOpen = id;
    openModal(ref.name || '参考详情', `${notice(health.text,health.tone)}<p>${html(ref.source_note || '没有来源说明')}</p><div class="fp-facts"><span>${ref.imported ? '导入的参考' : '本机采集的参考'}</span><span>${ref.trusted_source ? '用户已声明可信' : '未声明来源可信'}</span><span>${html(date(ref.collected_at))}</span></div>${protocolHtml(protocolOf(ref),ref)}`, `${button('delete-reference','删除参考',`data-fp-id="${html(id)}"`,'danger')}<a class="btn fp-button" href="/fingerprint/references/${encodeURIComponent(id)}/export" download>导出指纹</a>${button('create-from-reference','用此参考采集',`data-fp-id="${html(id)}" ${health.usable ? '' : 'disabled'}`,'primary')}`);
  }
  async function openCompare(id) {
    if (!s.referencesLoaded) await loadReferences();
    const run = s.detail?.id === id ? s.detail : s.runs.find(r => r.id === id); if (!run) return;
    openModal('离线对照参考', `<p>使用「${html(run.name)}」已经保存的证据。<b>本次不向上游发出请求，不产生新的采集费用。</b></p><div class="fp-field"><label for="fpCompareReference">选择参考指纹</label><select id="fpCompareReference"><option value="">请选择参考</option>${s.references.map(ref => `<option value="${html(ref.id)}">${html(ref.name)} · ${html(referenceHealth(ref).text)}</option>`).join('')}</select></div><div id="fpCompareNotice">${!s.references.length ? notice('参考库为空，请先导入或建立可信参考。') : notice('可选择不同协议或过期参考查看差异，但不满足条件时不会给出严格结论。')}</div>`, `${button('close-modal','取消')}${button('submit-compare','计算对照结果',`data-fp-id="${html(id)}" ${!s.references.length ? 'disabled' : ''}`,'primary')}`);
    if (comparedReference(run) && s.references.some(r => r.id === comparedReference(run))) { $('fpCompareReference').value = comparedReference(run); updateCompareNotice(); }
  }
  function updateCompareNotice() {
    const ref = s.references.find(r => r.id === $('fpCompareReference')?.value); if (!ref) return;
    const p = s.detail?.protocol, rp = protocolOf(ref), health = referenceHealth(ref), same = p?.id && p.id === rp.id;
    $('fpCompareNotice').innerHTML = notice(`${same ? '协议标识一致' : '协议标识不一致／未知'} · ${health.text}。${same && health.usable ? '仍需检查样本覆盖与诊断异常后才能解释距离。' : '这次比较将保留可比性限制，不能据此判断模型身份。'}`,same && health.usable ? '' : 'warn');
  }
  async function submitCompare(id) {
    if (s.modalBusy) return;
    const ref = $('fpCompareReference')?.value; if (!ref) return modalError('请选择一份参考指纹。');
    const session = s.modalSession; s.modalBusy = true; modalBusy(true);
    try {
      await post(`/fingerprints/${encodeURIComponent(id)}/compare`,{reference_id:ref});
      if (session !== s.modalSession) return;
      closeModal(); s.samplePage = null; s.tab = 'overview'; await select(id); await loadReferenceArtifact(ref); toastMessage('离线对照已完成，未发出上游请求。');
    } catch (e) { if (!e.stale && session === s.modalSession) modalError(e.message); }
    finally { if (session === s.modalSession) { s.modalBusy = false; modalBusy(false); } }
  }
  async function stop(id, el) {
    if (el.disabled) return; el.disabled = true;
    try { const data = await post(`/fingerprints/${encodeURIComponent(id)}/stop`,{}); if (s.selected === id && data.run) s.detail = data.run; s.runs = s.runs.map(r => r.id === id ? data.run || r : r); render(); toastMessage('已请求停止采集，已获得证据会保留。'); if (typeof refresh === 'function') refresh(); }
    catch (e) { if (!e.stale) toastMessage(e.message); if (el.isConnected) el.disabled = false; }
  }
  function confirmDelete(kind,id) {
    const record = (kind === 'run' ? s.runs : s.references).find(r => r.id === id); if (!record) return;
    openModal(kind === 'run' ? '删除采集记录' : '删除参考指纹', `<p>将删除「<b>${html(record.name)}</b>」及其本地${kind === 'run' ? '采样证据' : '参考快照'}。此操作无法撤销。</p><p class="fp-subtle">${kind === 'run' ? '已独立保存到参考库的快照会保留。' : '运行中的任务正在使用此参考时，服务将拒绝删除。'}</p>`, `${button('close-modal','取消')}${button('confirm-delete','确认删除',`data-fp-kind="${kind}" data-fp-id="${html(id)}"`,'danger')}`);
  }
  async function deleteRecord(kind,id) {
    if (s.modalBusy) return;
    const session = s.modalSession; s.modalBusy = true; modalBusy(true);
    try {
      await post(kind === 'run' ? `/fingerprints/${encodeURIComponent(id)}/delete` : `/fingerprint/references/${encodeURIComponent(id)}/delete`,{});
      if (session !== s.modalSession) return;
      closeModal();
      if (kind === 'run') { s.runs = s.runs.filter(r => r.id !== id); if (s.selected === id) { s.selected = null; s.detail = null; s.selection++; s.detailPending = false; s.samplePage = null; } }
      else { s.references = s.references.filter(r => r.id !== id); s.referenceArtifacts.delete(id); }
      render(); toastMessage('本地记录已删除。');
    } catch (e) { if (!e.stale && session === s.modalSession) modalError(e.message); }
    finally { if (session === s.modalSession) { s.modalBusy = false; modalBusy(false); } }
  }
  function setTab(tab) {
    if (!TABS.some(t => t[0] === tab)) return;
    s.tab = tab; render();
    if (tab === 'samples' && !s.samplePage && !s.samplePending) loadSamples(s.sampleOffset);
    if (tab === 'distribution' && comparedReference(s.detail)) loadReferenceArtifact(comparedReference(s.detail));
  }
  function onClick(event) {
    const el = event.target.closest?.('[data-fp-action]'); if (!el || el.disabled) return;
    const action = el.dataset.fpAction, id = el.dataset.fpId;
    switch (action) {
      case 'view': s.view = el.dataset.fpView; ensureData(); render(); break;
      case 'create-reference': openCreate('reference'); break;
      case 'fetch-models': discoverModels(); break;
      case 'model-input-mode': setModelInputMode(el.dataset.fpModelMode); break;
      case 'close-create': closeCreate(); break;
      case 'retry-create': { const mode = createMode(); closeCreate(); openCreate(mode); break; }
      case 'retry-data': ensureData(true); break;
      case 'retry-detail': loadDetail(true); break;
      case 'reload-references': loadReferences(); break;
      case 'stop': stop(id,el); break;
      case 'toggle-key': { const key = $('fpKey'), show = key.type === 'password'; key.type = show ? 'text' : 'password'; el.textContent = show ? '隐藏' : '显示'; el.setAttribute('aria-label',show ? '隐藏接口密钥' : '显示接口密钥'); el.setAttribute('aria-pressed',String(show)); break; }
      case 'reload-samples': loadSamples(s.sampleOffset); break;
      case 'sample-page': s.samplePage = null; loadSamples(Math.max(0,Number(el.dataset.fpOffset) || 0)); break;
      case 'retry-reference-artifact': loadReferenceArtifact(id,true); break;
      case 'import': openImport(); break;
      case 'submit-import': submitImport(); break;
      case 'close-modal': closeModal(); break;
      case 'reference-detail': openReference(id); break;
      case 'create-from-reference': closeModal(); openCreate('verify',id); break;
      case 'compare': openCompare(id); break;
      case 'submit-compare': submitCompare(id); break;
      case 'delete-run': confirmDelete('run',id); break;
      case 'delete-reference': confirmDelete('reference',id); break;
      case 'confirm-delete': deleteRecord(el.dataset.fpKind,id); break;
    }
  }
  function onInput(event) {
    if (s.drawerOpen && $('fpCreateForm').contains(event.target)) $('fpFormError').hidden = true;
    if (s.drawerOpen && $('fpCreateForm').contains(event.target) && event.target.id !== 'fpRiskAck') {
      if (['fpSamples','fpConcurrency','fpRpm','fpTimeout','fpUrl','fpModel','fpModelSelect','fpKey'].includes(event.target.id)) $('fpRiskAck').checked = false;
      if (['fpUrl','fpKey'].includes(event.target.id)) {connectionId='';renderConnectionPicker();invalidateModelConnection()}
      if (event.target.id === 'fpSamples') updateBudget();
      if (['fpUrl','fpModel','fpModelSelect'].includes(event.target.id)) updateAdapterHint();
    }
  }
  function onChange(event) {
    const el = event.target;
    if (s.drawerOpen && $('fpCreateForm').contains(el)) $('fpFormError').hidden = true;
    if (el.name === 'fpMode') applyMode();
    if (['fpUrl','fpKey'].includes(el.id) && s.drawerOpen) {connectionId='';renderConnectionPicker();invalidateModelConnection()}
    if (el.id === 'fpModelSelect' || el.id === 'fpModel') $('fpRiskAck').checked = false;
    if (el.id === 'fpReference') { $('fpRiskAck').checked = false; applyReference(); }
    if (el.id === 'fpPreset') { $('fpRiskAck').checked = false; updateBudget(); }
    if (el.id === 'fpAdapter') { s.adapterManual = true; s.adapterAuto = null; $('fpRiskAck').checked = false; updateAdapterHint(); updateBudget(); }
    if (['fpUrl','fpModel','fpModelSelect'].includes(el.id)) updateAdapterHint();
    if (el.id === 'fpImportFile') previewImport();
    if (el.id === 'fpCompareReference') updateCompareNotice();
  }
  function onKeydown(event) {
    if(window.ConnectionUI?.isOpen())return;
    const overlay = s.modalOpen ? $('fingerprintModal') : s.drawerOpen ? $('fingerprintDrawer') : null;
    if (overlay) {
      if (event.key === 'Escape') { event.preventDefault(); event.stopImmediatePropagation(); if (s.modalOpen) closeModal(); else closeCreate(); return; }
      if (event.key === 'Tab') {
        const nodes = [...overlay.querySelectorAll('button:not([disabled]),a[href],input:not([disabled]),select:not([disabled]),textarea:not([disabled]),summary,[tabindex="0"]')].filter(el => el.getClientRects().length && el.tabIndex >= 0);
        const first = nodes[0], last = nodes[nodes.length - 1];
        if (!overlay.contains(document.activeElement)) { event.preventDefault(); first?.focus(); }
        else if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last?.focus(); }
        else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first?.focus(); }
      }
      return;
    }
  }
  window.FingerprintUI = Object.freeze({init,setRuns,updateChrome,render,openCreate,closeCreate,hide,onAuthLost,select,setTab,selectConnection,saveConnection,showSample,getSelectedId:()=>s.selected});
})();
