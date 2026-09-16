/* Model fingerprint workspace. No credentials are persisted or read from saved connections. */
(() => {
  'use strict';
  const $ = id => document.getElementById(id);
  const html = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const finite = value => value !== null && value !== undefined && value !== '' && Number.isFinite(Number(value));
  const number = (value, digits = 0) => finite(value) ? Number(value).toLocaleString('zh-CN', {maximumFractionDigits:digits, minimumFractionDigits:digits}) : '未知';
  const percent = value => finite(value) ? `${number(Number(value) * 100, 1)}%` : '未知';
  const metric = (label, value, note = '') => `<div class="fp-metric"><span>${html(label)}</span><strong>${html(value)}</strong>${note ? `<small>${html(note)}</small>` : ''}</div>`;
  const icon = name => `<svg class="ui-icon" viewBox="0 0 24 24" aria-hidden="true">${({fingerprint:'<path d="M8 20c2-3 2-5 2-8a2 2 0 0 1 4 0c0 4-1 7-2 10M4 16c2-2 2-4 2-6a6 6 0 0 1 12 0c0 4-.5 7-2 10M3 9a9 9 0 0 1 18 0M6 21l1-2M20 13c0 2-.3 4-1 6"/>', close:'<path d="m6 6 12 12M18 6 6 18"/>', plus:'<path d="M12 5v14M5 12h14"/>', download:'<path d="M12 3v12m-5-5 5 5 5-5M4 16v5h16v-5"/>', upload:'<path d="M12 16V3m-5 5 5-5 5 5M4 16v5h16v-5"/>', shield:'<path d="m12 3 8 3v6c0 5-8 9-8 9s-8-4-8-9V6l8-3Z"/><path d="m8 12 3 3 5-6"/>'})[name] || ''}</svg>`;
  const button = (action, label, attrs = '', className = '') => `<button type="button" class="btn fp-button ${className}" data-fp-action="${action}" ${attrs}>${label}</button>`;
  const notice = (text, tone = '') => `<div class="fp-notice ${tone}" role="status">${html(text)}</div>`;
  const MODES = {selfcheck:'通道自检',reference:'建立可信参考',verify:'对照已有参考'};
  const STATUSES = {waiting:'排队中',running:'采集中',stopping:'正在停止',finished:'已完成',error:'采集失败',stopped:'已停止',interrupted:'进程中断'};
  const VERDICTS = {match:'行为分布相近',mismatch:'行为分布存在明显差异',uncertain:'证据存疑，建议复测',insufficient:'有效证据不足',incomparable:'不具备直接可比条件'};
  const CATEGORIES = {valid:'有效',invalid:'无效回答',refusal:'拒答',empty:'空回答',error:'请求失败',cancelled:'已取消'};
  const TABS = [['overview','结果总览'],['distribution','回答分布'],['diagnostics','通道诊断'],['protocol','协议与来源'],['samples','原始样本']];
  const s = {initialized:false,epoch:0,runs:[],references:[],catalog:null,catalogError:'',referenceError:'',catalogPending:false,referencesPending:false,catalogPromise:null,referencesPromise:null,storageWarnings:[],referencesLoaded:false,referencesAt:0,view:'runs',selected:null,tab:'overview',detail:null,detailError:'',detailPending:false,detailSignature:'',selection:0,referenceArtifacts:new Map(),samplePage:null,sampleError:'',samplePending:false,sampleOffset:0,sampleSequence:0,drawerOpen:false,drawerSession:0,submitting:false,modalOpen:false,modalSession:0,modalBusy:false,opener:null,modalOpener:null,refOpen:null,formProtocol:null,modelInputMode:'list',modelOptions:[],modelDiscoverySequence:0,modelDiscoveryPending:false,modelDiscoveryStatus:'',modelDiscoveryTone:'',adapterManual:false,adapterAuto:null};
  const active = () => typeof activeModule !== 'undefined' && activeModule === 'fingerprint';
  const signedIn = () => typeof authenticated === 'undefined' || authenticated;
  const comparedReference = run => run?.comparison?.reference_id || (run?.mode === 'verify' ? run.reference_id : null);
  const running = run => ['waiting','running','stopping'].includes(run?.status);
  const date = value => {
    if (!value) return '日期未知';
    const d = new Date(typeof value === 'number' ? value * (value < 1e12 ? 1000 : 1) : value);
    return Number.isFinite(d.getTime()) ? d.toLocaleString('zh-CN', {dateStyle:'medium',timeStyle:'short',hour12:false}) : '日期未知';
  };
  const timeAge = value => {
    if (!value) return null;
    const t = new Date(typeof value === 'number' ? value * (value < 1e12 ? 1000 : 1) : value).getTime();
    return Number.isFinite(t) ? (Date.now() - t) / 86400000 : null;
  };
  const protocolOf = reference => reference?.protocol || reference?.artifact?.protocol || {};
  const referenceHealth = reference => {
    const p = protocolOf(reference), age = timeAge(reference?.collected_at || reference?.artifact?.collected_at);
    if (!p.id || p.external_protocol || p.version === 'external' || reference?.external_protocol) return {text:'外部协议／协议未知',tone:'warn',usable:false};
    if (s.catalog?.method_version && p.version !== s.catalog.method_version) return {text:'协议版本与当前版本不同',tone:'warn',usable:false};
    if (reference?.complete === false || reference?.artifact?.complete === false) return {text:'采集未完成',tone:'warn',usable:false};
    if (age === null || age < -5 / 1440) return {text:'采集日期不可验证',tone:'warn',usable:false};
    if (age > (s.catalog?.reference_max_age_days || 30)) return {text:'已过期，建议重新采集',tone:'warn',usable:false};
    if (!reference?.trusted_source) return {text:'来源尚未声明可信',tone:'warn',usable:false};
    const diagnostics = reference?.diagnostics || reference?.artifact?.diagnostics || {};
    if (finite(diagnostics.valid_rate) && diagnostics.valid_rate < .8) return {text:'有效证据不足，建议重新采集',tone:'warn',usable:false};
    if (diagnostics.split_half?.warning) return {text:'自检存在波动，对照需降级',tone:'warn',usable:true};
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
    if (!s.selected) { paintHost(welcome()); return; }
    const run = s.detail?.id === s.selected ? s.detail : s.runs.find(r => r.id === s.selected);
    if (!run) { paintHost(welcome()); return; }
    paintHost(detailHtml(run));
  }
  function paintHost(content) {
    const host = $('modulePlaceholder'), main = document.querySelector('.main');
    const key = `${s.view}:${s.selected || ''}:${s.tab}`;
    const same = host.dataset.fpKey === key;
    const scroll = same ? main.scrollTop : 0;
    const activeEl = document.activeElement;
    const focusKey = host.contains(activeEl) ? {action:activeEl.dataset?.fpAction,id:activeEl.dataset?.fpId,tab:activeEl.dataset?.fpTab,offset:activeEl.dataset?.fpOffset} : null;
    const tableScrolls = same ? [...host.querySelectorAll('[data-fp-scroll]')].map(el => [el.dataset.fpScroll,el.scrollLeft,el.scrollTop]) : [];
    const expanded = same ? [...host.querySelectorAll('details[open][data-fp-details]')].map(el => el.dataset.fpDetails) : [];
    host.dataset.fpKey = key;
    host.innerHTML = (s.storageWarnings.length ? `<div class="fp-storage-warning">${notice('本地记录恢复提示：' + s.storageWarnings.join('；'),'warn')}</div>` : '') + content;
    for (const [id,left,top] of tableScrolls) { const el = [...host.querySelectorAll('[data-fp-scroll]')].find(n => n.dataset.fpScroll === id); if (el) { el.scrollLeft = left; el.scrollTop = top; } }
    for (const id of expanded) { const el = [...host.querySelectorAll('[data-fp-details]')].find(n => n.dataset.fpDetails === id); if (el) el.open = true; }
    if (focusKey?.action) [...host.querySelectorAll('[data-fp-action]')].find(el => el.dataset.fpAction === focusKey.action && el.dataset.fpId === focusKey.id && el.dataset.fpTab === focusKey.tab && el.dataset.fpOffset === focusKey.offset)?.focus({preventScroll:true});
    main.scrollTop = scroll;
  }
  function renderList() {
    const list = $('fingerprintList');
    if (s.view === 'references') {
      list.innerHTML = s.references.length ? s.references.map(ref => { const health = referenceHealth(ref); return `<button class="task fp-record ${s.refOpen === ref.id ? 'sel' : ''}" type="button" data-fp-action="reference-detail" data-fp-id="${html(ref.id)}"><div class="task-top"><div class="task-name">${html(ref.name || '未命名参考')}</div><span class="fp-dot ${health.tone}"></span></div><div class="task-meta">${html(ref.endpoint?.model || '模型未记录')}</div><div class="fp-record-foot"><span>${html(health.text)}</span><span>${html(date(ref.collected_at).split(' ')[0])}</span></div></button>`; }).join('') : `<div class="task-empty"><b>${s.referenceError ? '参考库读取失败' : s.referencesPending ? '正在读取参考库' : '暂无参考指纹'}</b><span>${html(s.referenceError || '从可信来源采集，或导入已保存的指纹文件。')}</span>${s.referenceError ? button('retry-data','重新加载') : ''}</div>`;
      return;
    }
    list.innerHTML = s.runs.length ? s.runs.map(run => {
      const pct = Math.min(100,Math.max(0,Number(run.progress?.done || 0) / Math.max(1,Number(run.progress?.total || 0)) * 100));
      return `<button class="task fp-record ${s.selected === run.id ? 'sel' : ''}" type="button" data-fp-action="select" data-fp-id="${html(run.id)}"><div class="task-top"><div class="task-name">${html(run.name)}</div><span class="fp-status ${running(run) ? 'live' : run.status === 'error' ? 'warn' : ''}">${html(STATUSES[run.status] || run.status)}</span></div><div class="task-meta">${html(MODES[run.mode] || '指纹采集')} · ${html(run.endpoint?.model || '模型未记录')}</div><div class="task-foot"><div class="mini-track"><i style="width:${pct}%"></i></div><span class="fp-count">${number(run.progress?.done || 0)} / ${number(run.progress?.total || 0)}</span></div></button>`;
    }).join('') : '<div class="task-empty"><b>暂无采集记录</b><span>新建模型指纹，从通道自检开始。</span></div>';
  }
  function welcome() {
    return `<div class="fp-workspace fp-welcome"><section class="fp-hero"><div class="fp-hero-icon">${icon('fingerprint')}</div><div><div class="fp-kicker">模型指纹 · 可重复的黑盒观测</div><h1>让每次对照，都有同一把尺子</h1><p>把固定问题独立、多次发送给同一通道，观察回答分布；先检查自身稳定性，再与可信参考对照。</p></div></section><section class="fp-mode-explainer"><article><span class="fp-step">01</span><h2>通道自检</h2><p>将一次采集分半比较，寻找时间波动或分布不稳定的线索。</p><small>无需参考，适合首次使用</small></article><article><span class="fp-step">02</span><h2>建立可信参考</h2><p>从你认可的来源采集，连同冻结协议、来源和日期存入参考库。</p><small>可信来源需由你明确声明</small>${button('create-reference','采集参考指纹')}</article><article><span class="fp-step">03</span><h2>对照已有参考</h2><p>沿用参考参数采集目标通道，检查协议与证据条件后比较分布。</p><small>也可离线比较已完成记录</small></article></section><section class="fp-card"><div class="fp-section-head"><div><h2>固定协议，保留完整证据</h2><p>独立请求 · 无历史记忆 · 同义改写随机抽取 · 队列随机打乱 · 不自动重试</p></div>${icon('shield')}</div><div class="fp-facts"><span><b>1.0</b> 固定采样温度</span><span><b>双分半</b> 交替与时间顺序自检</span><span><b>${html(s.catalog?.reference_max_age_days || 30)} 天</b> 本地参考有效期策略</span></div><p class="fp-subtle">分布相似不等于模型身份已被证明；差异也可能来自提示词、采样、版本或路由。阈值为未校准的工程提示。</p>${s.catalogError ? notice('协议目录读取失败：' + s.catalogError,'warn') + button('retry-data','重新加载协议') : ''}<details class="fp-details" data-fp-details="method"><summary>方法来源与判读边界</summary>${methodHtml()}</details></section></div>`;
  }
  function methodHtml() {
    const c = s.catalog || {};
    return `<p>借鉴 ${link(c.source_url || 'https://github.com/ToseaAI/llm-fingerprint-detector','llm-fingerprint-detector（大模型指纹检测器）')}；工作台使用独立协议版本。${c.paper_url ? link(c.paper_url,'查看原论文') : ''}</p><p>距离使用 JSD（詹森–香农散度），越小表示观测分布越接近，并非置信度。协议不兼容、参考过期、有效覆盖不足或诊断异常时，结论降级。</p><p>分半自检异常不能确定存在混合后端，低差异也不能排除混合。缓存诊断只作排查线索，不能单独证明模型身份。</p><p>30 天是本地工程策略；阈值借鉴上游，尚未针对当前供应商和模型版本校准。</p>`;
  }
  async function select(id) {
    if (s.selected !== id) { s.selected = id; s.detail = null; s.detailSignature = ''; s.detailError = ''; s.tab = 'overview'; s.samplePage = null; s.sampleOffset = 0; s.sampleError = ''; s.selection++; s.sampleSequence++; s.detailPending = false; s.samplePending = false; }
    s.view = 'runs'; render(); await loadDetail(true);
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
    let body = s.tab === 'overview' ? overviewHtml(run) : s.tab === 'distribution' ? distributionHtml(run) : s.tab === 'diagnostics' ? diagnosticsHtml(run) : s.tab === 'protocol' ? protocolHtml(run.protocol || run.artifact?.protocol,run) : samplesHtml(run);
    return `<div class="fp-workspace"><header class="fp-page-head"><div><div class="fp-kicker">模型指纹 · ${html(MODES[run.mode] || '分布采集')}</div><h1>${html(run.name)}</h1><p>${html(run.endpoint?.model || '模型未记录')}<span class="fp-separator">·</span>${html(date(run.created))}</p><div class="fp-meta"><span class="fp-status ${running(run) ? 'live' : run.status === 'error' ? 'warn' : ''}">${html(STATUSES[run.status] || run.status)}</span><span>${number(run.protocol?.cells?.length)} 格问题</span><span>每格 ${number(run.protocol?.samples_per_cell)} 次</span><span>并发 ${number(run.protocol?.concurrency)}</span></div></div><div class="fp-head-actions">${running(run) ? button('stop',run.status === 'stopping' ? '正在停止…' : '停止采集',`data-fp-id="${html(run.id)}" ${run.status === 'stopping' ? 'disabled' : ''}`,'danger') : run.artifact ? `<a class="btn fp-button" href="/fingerprints/${encodeURIComponent(run.id)}/export" download>${icon('download')}导出指纹</a>` : ''}${run.artifact ? button('compare','离线对照',`data-fp-id="${html(run.id)}"`) : ''}</div></header>${s.detailError ? notice('记录详情读取失败：' + s.detailError,'warn') + button('retry-detail','重新加载详情') : ''}${run.error ? notice(run.error,'warn') : ''}${run.storage_warning ? notice(run.storage_warning,'warn') : ''}${running(run) || !run.artifact || run.artifact.complete === false ? `<section class="fp-card fp-progress" aria-live="polite"><div class="fp-section-head"><h2>${running(run) ? '正在收集证据' : '本次采集已结束'}</h2><strong>${number(pct,0)}%</strong></div><div class="fp-track" role="progressbar" aria-label="指纹采集进度" aria-valuemin="0" aria-valuemax="100" aria-valuenow="${Math.round(pct)}"><i style="width:${pct}%"></i></div><div class="fp-progress-meta"><span>${number(done)} / ${number(total)} 次请求已结算</span><span>有效 ${number(run.progress?.valid || 0)} · 无效 ${number(Math.max(0,done - (run.progress?.valid || 0) - (run.progress?.errors || 0)))} · 请求错误 ${number(run.progress?.errors || 0)}</span></div><p class="fp-subtle">${run.status === 'waiting' ? '等待采集槽位；排队时不会向目标发出请求。' : run.status === 'stopping' ? '停止未发请求并取消在途请求，已获得的证据会保留。' : running(run) ? '完成后计算分布与双分半自检；运行中的局部结果不作为对照结论。' : '仅保留已经采集到的证据，不将不完整采集视为完成。'}</p></section>` : ''}<nav class="fp-tabs" role="tablist" aria-label="模型指纹详情标签页">${TABS.map(([id,label]) => `<button class="fp-tab ${s.tab === id ? 'active' : ''}" role="tab" aria-selected="${s.tab === id}" aria-controls="fpTabPanel" tabindex="${s.tab === id ? 0 : -1}" type="button" data-fp-action="tab" data-fp-tab="${id}">${label}</button>`).join('')}</nav><div id="fpTabPanel" role="tabpanel" aria-label="${TABS.find(t => t[0] === s.tab)?.[1] || ''}">${body}</div><div class="fp-footer-actions"><span>原始证据保存在本机；导出前请留意目标地址与回答原文。</span>${!running(run) ? button('delete-run','删除记录',`data-fp-id="${html(run.id)}"`,'danger') : ''}</div></div>`;
  }
  function overviewHtml(run) {
    const a = run.artifact, d = a?.diagnostics || run.diagnostics || {}, split = d.split_half || {}, comparison = run.comparison;
    if (!a) return `<section class="fp-card fp-quiet"><h2>${s.detailPending ? '正在读取详情' : '结论等待采集完成'}</h2><p>完成后会同时检查有效样本、协议兼容性和通道自检。</p></section>`;
    const complete = a.complete === true, noValid = finite(d.valid_rate) && Number(d.valid_rate) === 0;
    const reasoningTruncated = noValid && d.reasoning_observed && Number(d.truncated_rate) === 1;
    const selfcheckAvailable = complete && Number(split.alternating?.comparable_cells || 0) >= 4 && Number(split.temporal?.comparable_cells || 0) >= 4 && finite(split.alternating?.mean_jsd) && finite(split.temporal?.mean_jsd);
    const title = !complete ? '采集未完成，已保留部分证据' : noValid ? '未获得有效指纹' : comparison ? VERDICTS[comparison.verdict] || comparison.label || '对照结果' : split.warning ? '自检发现分布波动，建议复测' : '已形成指纹证据';
    const tone = !complete || noValid ? 'warn' : comparison?.verdict === 'match' ? 'good' : ['mismatch','uncertain','incomparable','insufficient'].includes(comparison?.verdict) || split.warning ? 'warn' : 'neutral';
    const reference = s.references.find(r => r.id === comparedReference(run));
    const c = comparison || {};
    const diagnosis = reasoningTruncated ? '所有已返回回答都被截断，并且观测到推理用量。检查原始样本：若正文为空，说明输出预算被推理占用。请用关闭思考适配重新采集；原记录保留作诊断，不可作为有效参考。' : noValid ? '请求已结算，但没有可用于分布比较的有效回答。请在通道诊断与原始样本中检查截断、拒答和接口错误，再新建采集。' : '';
    return `<section class="fp-conclusion ${tone}"><div class="fp-kicker">${comparison ? '参考对照结论' : '采集结果'}</div><h2>${html(title)}</h2><p>${!complete ? '本次仅保留已经返回的部分请求；不据此评价稳定性或建立可信参考。' : noValid ? '本次没有有效回答，不能用于指纹对照或评价通道稳定性。' : comparison ? '结论描述本次行为分布，不直接判定模型身份。' : run.mode === 'reference' ? run.reference_id ? '本次指纹已进入参考库；可信来源是用户声明，系统不作身份背书。' : '参考尚未建立，请检查运行状态与证据质量。' : '可将本次记录与参考库离线对照，或在另一时间重新采集。'}</p>${(c.reasons || []).length ? `<ul>${c.reasons.map(x => `<li>${html(x)}</li>`).join('')}</ul>` : ''}${reference ? `<div class="fp-meta"><span>参考：${html(reference.name)}</span><span>${html(referenceHealth(reference).text)}</span></div>` : ''}</section>${diagnosis ? notice(diagnosis,'warn') : ''}<div class="fp-metrics">${metric('有效回答率',percent(d.valid_rate),'有效格式不代表身份已确认')}${metric('交替分半距离',number(split.alternating?.mean_jsd,3),`${number(split.alternating?.comparable_cells || 0)} 格可比`)}${metric('时间分半距离',number(split.temporal?.mean_jsd,3),'按每格发送顺序划分前后半')}${metric(comparison ? '平均对照距离' : '参考对照',comparison ? number(c.mean_jsd,3) : '尚未对照',comparison ? `${number(c.comparable_cells || 0)} 格可比 · 非置信度` : '复用本地证据，不产生请求')}</div>${comparison ? `<section class="fp-card"><div class="fp-section-head"><h2>对照条件</h2><span class="fp-subtle">先检查条件，再解释距离</span></div><div class="fp-facts"><span>协议 <b>${c.protocol_compatible ? '一致' : '不兼容／未知'}</b></span><span>参考年龄 <b>${finite(c.reference_age_days) ? number(c.reference_age_days,1) + ' 天' : '未知'}</b></span><span>可比问题 <b>${number(c.comparable_cells || 0)} 格</b></span></div><details class="fp-details" data-fp-details="thresholds"><summary>阈值与判读限制</summary><p>工程参考区间：距离 ≤ ${number(c.thresholds?.match ?? 0.25,2)} 提示行为分布相近，> ${number(c.thresholds?.mismatch ?? 0.35,2)} 提示明显差异，中间区域建议复测；协议、有效证据和诊断条件可以使结论降级。</p><p>阈值尚未针对当前部署校准，不是统计显著性或身份置信度。</p></details></section>` : ''}<section class="fp-card"><h2>自检如何解读</h2><p>${!selfcheckAvailable ? '样本或覆盖不足，不能评价稳定性。需完整采集，且交替分半与时间分半各至少有 4 格可比证据；请重新采集后复核。' : split.warning ? '两种分半检查中存在异常波动。请结合逐格样本与通道诊断复测，不能仅据此断言供应商更换或混用后端。' : '本次分半未提示明显不稳定。这个结果只覆盖本次采样窗口，不能排除混合后端。'}</p>${warningsHtml([...(d.warnings || []),...(c.warnings || [])])}</section>`;
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
    const keys = [['协议版本',p.version],['协议标识',p.id],['问题集摘要',p.battery_hash],['归一化版本',p.normalizer_version],['问题格数',Array.isArray(p.cells) ? p.cells.length : '未知'],['每格采样数',p.samples_per_cell],['并发数',p.concurrency],['单次超时',finite(p.timeout_seconds) ? `${p.timeout_seconds} 秒` : '未知'],['温度',p.temperature],['Top-p（核采样）',p.top_p],['输出令牌上限',p.max_tokens],['自动重试',p.max_retries],['适配器',adapterLabel(p.adapter)],['令牌参数字段',p.token_field],['采集日期',date(run.artifact?.collected_at || run.collected_at)],['排程种子',run.artifact?.seed],['来源说明',run.source_note || '未提供']];
    return `<section class="fp-card"><div class="fp-section-head"><div><h2>冻结协议快照</h2><p>协议字段变化后，旧参考不直接复用。排程种子用于复现请求队列。</p></div></div><dl class="fp-protocol">${keys.map(([label,value]) => `<div><dt>${html(label)}</dt><dd>${html(value ?? '未知')}</dd></div>`).join('')}</dl><details class="fp-details" data-fp-details="protocol-json"><summary>完整协议 JSON（结构化数据）</summary><pre class="fp-code-block">${html(JSON.stringify(p,null,2))}</pre></details>${p.system_prompts ? `<details class="fp-details" data-fp-details="system-prompts"><summary>查看冻结的系统提示词</summary><pre class="fp-code-block">${html(typeof p.system_prompts === 'string' ? p.system_prompts : JSON.stringify(p.system_prompts,null,2))}</pre></details>` : ''}</section><section class="fp-card"><h2>方法与实现来源</h2>${methodHtml()}<dl class="fp-protocol"><div><dt>方法版本</dt><dd>${html(run.artifact?.source?.method_version || s.catalog?.method_version || '未知')}</dd></div><div><dt>来源提交</dt><dd>${html(run.artifact?.source?.commit || s.catalog?.source_commit || '未知')}</dd></div><div><dt>许可证</dt><dd>${html(s.catalog?.license || '见来源项目')}</dd></div></dl></section>`;
  }
  function samplesHtml(run) {
    const page = s.samplePage;
    return `<section class="fp-card"><div class="fp-section-head"><div><h2>原始请求证据</h2><p>按发送编号分页。原文最多保留 4096 字符，不含密钥。</p></div><div class="fp-head-actions">${button('reload-samples','刷新样本')}${!running(run) && run.artifact ? `<a class="btn fp-button" href="/fingerprints/${encodeURIComponent(run.id)}/samples-export" download>导出全部样本</a>` : ''}</div></div>${s.sampleError ? notice('样本读取失败：' + s.sampleError,'warn') + button('reload-samples','重新加载样本') : ''}${s.samplePending && !page ? '<p class="fp-subtle">正在读取样本…</p>' : page?.samples?.length ? `<div class="fp-table-wrap" data-fp-scroll="samples"><table class="fp-table fp-sample-table"><thead><tr><th>编号／问题</th><th>结果</th><th>规范化回答</th><th>延迟</th><th>原文与回显</th></tr></thead><tbody>${page.samples.map(sample => `<tr><th><span>${number(Number(sample.index) + 1)}</span><small>${html(probeLabel(sample.cell_id))}</small></th><td><span class="fp-status ${sample.category === 'valid' ? '' : 'warn'}">${html(CATEGORIES[sample.category] || sample.category)}</span>${sample.truncated ? '<small>已截断</small>' : ''}</td><td>${html(sample.normalized ?? '—')}</td><td>${finite(sample.latency_ms) ? number(sample.latency_ms) + ' 毫秒' : '未知'}</td><td><details class="fp-sample" data-fp-details="sample-${Number(sample.index)}"><summary>${html(sample.error ? '查看错误与详情' : '查看原文与详情')}</summary><pre>${html(sample.raw || '（无回答原文）')}</pre>${sample.error ? notice(sample.error,'warn') : ''}<dl><dt>模型回显</dt><dd>${html(sample.returned_model || '未提供')}</dd><dt>结束原因</dt><dd>${html(sample.finish_reason || '未提供')}</dd><dt>状态码</dt><dd>${html(sample.status_code ?? '未知')}</dd><dt>改写编号</dt><dd>${number(Number(sample.paraphrase_index) + 1)}</dd><dt>开始时间</dt><dd>${html(date(sample.started_at))}</dd><dt>用量 JSON（结构化数据）</dt><dd><pre>${html(JSON.stringify(sample.usage || {},null,2))}</pre></dd></dl></details></td></tr>`).join('')}</tbody></table></div>` : '<div class="fp-quiet"><p>当前页暂无样本；正在运行时可点击刷新。</p></div>'}<div class="fp-pagination"><span>${page?.total ? `${number(page.offset + 1)}–${number(Math.min(page.total,page.offset + page.samples.length))} / ${number(page.total)} 条` : '0 条样本'}</span><div>${button('sample-page','上一页',`data-fp-offset="${Math.max(0,s.sampleOffset - 50)}" ${s.samplePending || s.sampleOffset <= 0 ? 'disabled' : ''}`)}${button('sample-page','下一页',`data-fp-offset="${s.sampleOffset + 50}" ${s.samplePending || !page || page.offset + page.samples.length >= page.total ? 'disabled' : ''}`)}</div></div></section>`;
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
    return `<div class="fp-workspace"><header class="fp-page-head"><div><div class="fp-kicker">模型指纹 · 可追溯参考</div><h1>参考库</h1><p>保存来源、采集日期和协议快照，为下一次对照提供依据。</p></div><div class="fp-head-actions">${button('create-reference',icon('plus') + '采集参考指纹','','primary')}${button('import',icon('upload') + '导入指纹')}${button('reload-references','刷新')}</div></header>${s.referenceError ? notice('参考库读取失败：' + s.referenceError,'warn') + button('retry-data','重新加载') : ''}<section class="fp-card"><div class="fp-section-head"><h2>${s.references.length ? `${s.references.length} 份参考指纹` : '还没有可信参考'}</h2><span class="fp-subtle">有效期策略：${s.catalog?.reference_max_age_days || 30} 天</span></div>${s.references.length ? `<div class="fp-reference-grid">${s.references.map(ref => { const health = referenceHealth(ref), p = protocolOf(ref); return `<article class="fp-reference-card"><div class="fp-reference-title"><div>${icon('fingerprint')}<h3>${html(ref.name || '未命名参考')}</h3></div><span class="fp-status ${health.tone}">${html(health.text)}</span></div><p>${html(ref.endpoint?.model || '模型未记录')}</p><dl><div><dt>采集时间</dt><dd>${html(date(ref.collected_at))}</dd></div><div><dt>协议</dt><dd class="fp-code">${html(p.id ? p.id.slice(0,16) + '…' : '外部协议／未知')}</dd></div><div><dt>来源</dt><dd>${html(ref.source_note || '未声明')}</dd></div></dl><div class="fp-reference-actions">${button('reference-detail','查看详情',`data-fp-id="${html(ref.id)}"`)}${button('create-from-reference','用此参考采集',`data-fp-id="${html(ref.id)}" ${health.usable ? '' : 'disabled'} title="${html(health.usable ? '沿用参考协议创建目标采集' : health.text)}"`)}<a class="btn fp-button" href="/fingerprint/references/${encodeURIComponent(ref.id)}/export" download>导出</a>${button('delete-reference','删除',`data-fp-id="${html(ref.id)}"`,'danger')}</div></article>`; }).join('')}</div>` : `<div class="fp-library-empty">${icon('fingerprint')}<h3>先为对照准备一份可追溯参考</h3><p>连接你确认可信的来源，选择一个模型，完整采集后自动保存到参考库。也可导入已有指纹文件。</p>${button('create-reference',icon('plus') + '采集参考指纹','','primary')}<p class="fp-subtle">外部文件会保留原协议状态；导入成功并不意味着可以直接对照。</p></div>`}</section><section class="fp-card fp-library-policy"><h2>参考不是永久基准</h2><p>协议变更、采样参数变化或参考过期时，需要重新采集。来源可信由用户声明，不能由一个模型名称自动证明。</p></section></div>`;
  }
  const adapterLabel = id => ({standard:'标准接口',deepseek:'DeepSeek（深度求索）：关闭思考',openrouter:'OpenRouter（模型聚合平台）',zhipu:'智谱',openai:'OpenAI（模型服务商）'})[id] || String(id || '未知');
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
    resetModelDiscovery(); s.adapterManual = false; s.adapterAuto = null;
    const c = s.catalog, presets = c.presets || [], initial = findPreset('standard') || presets[0];
    $('fpCreateBody').innerHTML = `<fieldset class="fp-mode-picker"><legend>本次要做什么</legend>${Object.entries(MODES).map(([id,label]) => `<label class="fp-mode-choice"><input type="radio" name="fpMode" value="${id}" ${id === mode ? 'checked' : ''}><span><b>${label}</b><small>${id === 'selfcheck' ? '无需参考，检查自身稳定性' : id === 'reference' ? '从已确认来源建立参考指纹' : '沿用参考协议采集目标通道'}</small></span></label>`).join('')}</fieldset><div class="fp-field"><label for="fpName">记录名称 <em>必填</em></label><input id="fpName" name="name" maxlength="120" required placeholder="例如：主通道 · 9 月稳定性自检" autocomplete="off"></div><section id="fpReferenceSection" class="fp-form-section" hidden><div class="fp-field"><label for="fpReference">选择参考指纹 <em>必填</em></label><select id="fpReference"><option value="">请选择一份当前有效的参考</option>${s.references.map(ref => { const h = referenceHealth(ref); return `<option value="${html(ref.id)}" ${h.usable ? '' : 'disabled'}>${html(ref.name)} · ${html(h.text)}</option>`; }).join('')}</select><p class="fp-field-hint">只采集目标通道；参考侧复用本地证据。</p></div><div id="fpReferenceNotice"></div></section><div class="fp-form-section"><h2>目标连接</h2><div class="fp-field"><label for="fpUrl">接口地址 <em>必填</em></label><input id="fpUrl" name="url" type="url" required placeholder="https://api.example.com/v1" autocomplete="off" spellcheck="false"><p class="fp-field-hint">填写兼容 OpenAI（模型服务商）聊天接口的基础地址或完整端点。</p></div><div class="fp-field"><label for="fpKey">接口密钥 <span>本次运行使用</span></label><div class="fp-secret"><input id="fpKey" name="api_key" type="password" placeholder="本地免鉴权服务可留空" autocomplete="new-password" spellcheck="false"><button type="button" class="btn" data-fp-action="toggle-key" aria-label="显示接口密钥" aria-pressed="false">显示</button></div></div><div class="fp-field fp-model-field"><label id="fpModelLabel" for="fpModelSelect">请求模型 <em>必填</em></label><div id="fpModelListPanel" class="model-row fp-model-row"><select id="fpModelSelect" required disabled aria-describedby="fpModelHelp fpModelStatus"><option value="">请先填写地址和密钥，再获取模型</option></select><button id="fpFetchModels" class="icon-btn" type="button" data-fp-action="fetch-models" title="从当前接口获取可用模型列表">获取模型</button></div><div class="model-fallback fp-model-fallback"><button id="fpManualModelToggle" class="text-btn" type="button" data-fp-action="model-input-mode" data-fp-model-mode="manual" aria-expanded="false" aria-controls="fpModelManualPanel">无法获取列表？手动指定模型</button></div><div id="fpModelManualPanel" hidden><input id="fpModel" name="model" maxlength="256" placeholder="填写服务商支持的准确模型名称" autocomplete="off" spellcheck="false" disabled aria-describedby="fpModelManualHelp fpModelStatus"><p id="fpModelManualHelp" class="fp-field-hint">仅在接口不支持模型列表时使用；请填写准确模型名称，系统不会自动验证名称是否存在。</p></div><p id="fpModelStatus" class="fp-model-status" role="status" hidden></p><p id="fpModelHelp" class="fp-field-hint">填写地址和密钥后获取列表，不会启动采集。本次只采集一个选定模型；接口不支持列表时可手动填写。</p></div></div><div class="fp-form-section"><h2>采集协议</h2><div class="fp-field"><label for="fpPreset">采集范围</label><select id="fpPreset">${presets.map(p => `<option value="${html(p.id)}" ${p.id === initial?.id ? 'selected' : ''}>${html(p.id === 'quick' ? '快速采集' : p.label)} · ${presetCells(p)} 格</option>`).join('')}</select></div><div class="fp-form-grid"><div class="fp-field"><label for="fpSamples">每格请求数</label><input id="fpSamples" type="number" min="10" max="100" step="1" required value="${Number(initial?.samples_per_cell || 20)}"></div><div class="fp-field"><label for="fpConcurrency">并发数</label><input id="fpConcurrency" type="number" min="1" max="8" step="1" required value="1"></div><div class="fp-field"><label for="fpTimeout">单次超时（秒）</label><input id="fpTimeout" type="number" min="5" max="120" step="1" required value="30"></div><div class="fp-field"><label for="fpAdapter">接口适配</label><select id="fpAdapter" aria-describedby="fpAdapterHint">${(c.adapters || [{id:'standard'}]).map(a => `<option value="${html(a.id)}">${html(adapterLabel(a.id))}</option>`).join('')}</select></div></div><div id="fpAdapterHint" class="fp-notice" role="status" hidden></div><div id="fpProtocolLock" class="fp-field-hint"></div><details class="fp-details" id="fpProbePreview"></details><details class="fp-details"><summary>固定项与适配说明</summary><p>温度 1.0、核采样 1.0、输出令牌上限 16、不重试。每次只有系统与用户两条消息，系统提示词固定、无历史记忆、无工具调用。</p><p>接口适配只使用预定义请求字段。更换适配器、样本数、并发或超时都会改变协议，不能与旧参考直接对照。</p><p>若服务不支持这些采样参数，请保留错误证据并选择合适适配，不自动降级参数。</p></details></div><section id="fpSourceSection" class="fp-form-section" hidden><h2>参考来源</h2><div class="fp-field"><label for="fpSourceNote">可追溯的来源说明 <em>必填</em></label><textarea id="fpSourceNote" maxlength="1000" rows="3" placeholder="例如：供应商官方账户直连，已核对模型版本和后台配置"></textarea></div><label class="fp-checkbox"><input type="checkbox" id="fpTrusted"><span>我已独立确认来源可信，并接受系统仅记录此声明。</span></label></section><section class="fp-budget" aria-live="polite"><div class="fp-budget-top"><div><span>本次实际请求</span><strong id="fpRequestCount">—</strong></div><span id="fpBudgetFormula"></span></div><p id="fpBudgetNote"></p><label class="fp-checkbox"><input type="checkbox" id="fpRiskAck" required><span id="fpRiskLabel">我确认本次采集会向目标发出请求，并可能产生上游费用。</span></label></section>`;
    if (referenceId && [...$('fpReference').options].some(o => o.value === referenceId && !o.disabled)) $('fpReference').value = referenceId;
    paintModelControls(); applyMode();
  }
  const createMode = () => document.querySelector('input[name="fpMode"]:checked')?.value || 'selfcheck';
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
    $('fpModelListPanel').hidden = false; $('fpModelManualPanel').hidden = !manual;
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
    status.hidden = !s.modelDiscoveryStatus; status.className = `fp-model-status ${s.modelDiscoveryTone}`;
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
    return s.drawerOpen && signedIn() && request.sequence === s.modelDiscoverySequence && request.session === s.drawerSession && request.epoch === s.epoch && $('fpUrl')?.value.trim() === request.connection.url && $('fpKey')?.value === request.connection.api_key;
  }
  async function discoverModels() {
    if (!s.drawerOpen || s.modelDiscoveryPending) return;
    const url = $('fpUrl')?.value.trim() || '';
    let invalid = '';
    if (!safeUrl(url)) invalid = '请先填写有效的接口地址，再获取模型列表。';
    else { const parsed = new URL(url); if (parsed.username || parsed.password || parsed.search || parsed.hash) invalid = '接口地址不能携带账号、密钥、查询参数或片段，请使用专用密钥字段。'; }
    if (invalid) {
      s.modelDiscoveryStatus = invalid; s.modelDiscoveryTone = 'warn'; paintModelControls(); $('fpUrl')?.focus(); return;
    }
    const request = {sequence:++s.modelDiscoverySequence,session:s.drawerSession,epoch:s.epoch,connection:{url,api_key:$('fpKey').value}};
    s.modelInputMode = 'list'; s.modelOptions = []; s.modelDiscoveryPending = true;
    s.modelDiscoveryStatus = '仅请求一次模型目录；不会发送采集题目。'; s.modelDiscoveryTone = '';
    $('fpModelSelect').value = ''; $('fpRiskAck').checked = false; paintModelControls();
    try {
      const data = await post('/fingerprint/models',request.connection);
      if (!modelDiscoveryCurrent(request)) return;
      if (!Array.isArray(data.models)) throw new Error('接口未返回可用的模型列表，请切换为手动填写模型名称。');
      s.modelOptions = [...new Set(data.models.filter(model => typeof model === 'string' && model.trim()))];
      s.modelDiscoveryStatus = s.modelOptions.length ? `已获取 ${number(s.modelOptions.length)} 个模型，请明确选择一个。列表来自上游回传，不证明模型身份。` : '接口返回空模型列表；请手动填写模型名称，或核对地址与密钥后重新获取。';
      s.modelDiscoveryTone = s.modelOptions.length ? '' : 'warn';
    } catch (e) {
      if (!e.stale && modelDiscoveryCurrent(request)) {
        const message = typeof e.message === 'string' && e.message.trim() ? e.message.trim() : '模型目录暂不可用';
        const fallback = /手动填写|手填/.test(message) ? '' : ' 可切换为手动填写模型名称。';
        s.modelOptions = []; s.modelDiscoveryStatus = `获取模型列表失败：${message}${fallback}`; s.modelDiscoveryTone = 'warn';
      }
    } finally {
      const current = modelDiscoveryCurrent(request);
      const sameContext = s.drawerOpen && signedIn() && request.sequence === s.modelDiscoverySequence && request.session === s.drawerSession && request.epoch === s.epoch;
      request.connection.api_key = '';
      if (current) { s.modelDiscoveryPending = false; paintModelControls(); }
      else if (sameContext) invalidateModelConnection();
    }
  }
  function isDeepseekOfficial(url) {
    try { return new URL(url).hostname.toLowerCase() === 'api.deepseek.com'; } catch { return false; }
  }
  function updateAdapterHint() {
    const select = $('fpAdapter'), hint = $('fpAdapterHint');
    if (!select || !hint) return;
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
    hint.hidden = !official && select.value !== 'deepseek';
    const safe = select.value === 'deepseek';
    hint.className = 'fp-notice' + (official && !safe ? ' warn' : '');
    hint.textContent = official && !safe
      ? locked ? '参考协议未关闭 DeepSeek（深度求索）的思考模式，不能直接复用；请用关闭思考适配重新建立参考。' : '此官方接口默认开启思考；请选择 DeepSeek（深度求索）：关闭思考，避免输出预算被推理占满。'
      : '本次明确关闭 DeepSeek（深度求索）的思考模式，保留 16 令牌输出上限。此设置会写入冻结协议，任务运行中不再改参。';
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
    s.formProtocol = mode === 'verify' && ref ? p : null;
    for (const id of ['fpPreset','fpSamples','fpConcurrency','fpTimeout','fpAdapter']) $(id).disabled = mode === 'verify';
    let canUse = mode !== 'verify';
    if (mode === 'verify' && ref) {
      const h = referenceHealth(ref); canUse = h.usable && !!preset && [...$('fpAdapter').options].some(o => o.value === p.adapter);
      if (canUse) { $('fpPreset').value = preset; $('fpSamples').value = p.samples_per_cell; $('fpConcurrency').value = p.concurrency; $('fpTimeout').value = p.timeout_seconds; $('fpAdapter').value = p.adapter; }
      $('fpReferenceNotice').innerHTML = notice(canUse ? `已锁定「${ref.name}」的采集参数；参考采于 ${date(ref.collected_at)}。` : '该参考无法按当前协议重新采集，请重新建立参考或使用离线对照。',canUse ? '' : 'warn');
    } else $('fpReferenceNotice').innerHTML = mode === 'verify' ? notice(s.references.some(r => referenceHealth(r).usable) ? '选择参考后，采集范围、样本数、并发、超时与适配器将锁定。' : '当前没有可直接复用的参考。请先建立可信参考，或在参考库查看已有文件。') : '';
    $('fpProtocolLock').textContent = mode === 'verify' ? '参数与参考一致，并将在后端再次校验协议。' : '提交后参数冻结；调整参数会创建新的协议标识。';
    $('fpSubmit').disabled = !canUse || s.submitting;
    $('fpSubmit').textContent = mode === 'reference' ? '开始采集并建立参考' : mode === 'verify' ? '开始采集并对照' : '开始通道自检';
    updateBudget(); updateAdapterHint();
  }
  function updateBudget() {
    const preset = findPreset($('fpPreset')?.value), samples = Number($('fpSamples')?.value), valid = Number.isInteger(samples) && samples >= 10 && samples <= 100;
    const cells = presetCells(preset), total = valid && cells > 0 ? cells * samples : 0;
    $('fpRequestCount').textContent = total ? `${number(total)} 次` : '请完善参数';
    $('fpBudgetFormula').textContent = total ? `${cells} 格 × 每格 ${samples} 次` : '';
    $('fpBudgetNote').textContent = `上游费用无法预估；${total ? `请求设置单次输出上限 16 令牌，总输出预算上限 ${number(total * 16)} 令牌。` : '令牌总预算将在参数有效时显示。'}推理可能占用输出预算；输入与最终费用以实际回传为准。`;
    if ($('fpProbePreview')) {
      const probes = (s.catalog?.probes || []).slice(0,cells);
      $('fpProbePreview').innerHTML = `<summary>查看本次问题与改写池（${cells} 格）</summary><p>每次为本格随机抽取一种改写；系统提示词按语言固定。</p>${probes.map(probe => `<div class="fp-probe-preview"><b>${html(probe.label)}</b><p>系统提示词：${html(probe.system_prompt)}</p><ol>${(probe.paraphrases || []).map(text => `<li>${html(text)}</li>`).join('')}</ol></div>`).join('')}`;
    }
    $('fpRiskLabel').textContent = total ? `我确认将向目标发出 ${number(total)} 次请求，并可能产生上游费用。` : '我确认本次采集会向目标发出请求，并可能产生上游费用。';
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
    if (field) { $(field)?.focus(); $(field)?.scrollIntoView({block:'nearest'}); }
  }
  async function submitCreate(event) {
    event.preventDefault();
    if (s.submitting || !s.drawerOpen) return;
    const form = $('fpCreateForm'); if (!form.reportValidity()) return;
    const mode = createMode(), url = $('fpUrl').value.trim(), model = selectedModel(), name = $('fpName').value.trim();
    if (!name) return formError('请填写记录名称。','fpName');
    if (!safeUrl(url)) return formError('请填写有效的 HTTP（超文本传输协议）或 HTTPS（安全超文本传输协议）地址。','fpUrl');
    try { const u = new URL(url); if (u.username || u.password || u.hash || u.search) return formError('接口地址不能包含账号、密钥、查询参数或片段，请将密钥填入专用字段。','fpUrl'); } catch { return formError('接口地址格式无效。','fpUrl'); }
    if (isDeepseekOfficial(url) && $('fpAdapter').value !== 'deepseek') return formError('请选择 DeepSeek（深度求索）：关闭思考；若参考参数已锁定，请先按此适配重新建立参考。','fpAdapter');
    if (!model) return formError(s.modelInputMode === 'manual' ? '请填写准确的请求模型名称。' : '请获取模型列表并明确选择一个模型，也可切换为手动填写。',s.modelInputMode === 'manual' ? 'fpModel' : 'fpFetchModels');
    if (!$('fpRiskAck').checked) return formError('请先确认请求数量与潜在费用。','fpRiskAck');
    if (mode === 'reference' && (!$('fpTrusted').checked || !$('fpSourceNote').value.trim())) return formError('建立参考需要来源说明与可信来源声明。','fpSourceNote');
    const ref = s.references.find(r => r.id === $('fpReference').value);
    if (mode === 'verify' && (!ref || !referenceHealth(ref).usable || !matchPreset(protocolOf(ref)))) return formError('请选择当前协议可用且未过期的参考。','fpReference');
    const config = {name,mode,endpoint:{url,model,api_key:$('fpKey').value},preset:$('fpPreset').value,samples_per_cell:Number($('fpSamples').value),concurrency:Number($('fpConcurrency').value),timeout_seconds:Number($('fpTimeout').value),adapter:$('fpAdapter').value,risk_acknowledged:true};
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
    $('fingerprintModal').querySelector('input:not([type="checkbox"]),select,textarea,.drawer-close')?.focus({preventScroll:true});
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
    openModal(ref.name || '参考详情', `${notice(health.text,health.tone)}<p>${html(ref.source_note || '没有来源说明')}</p><div class="fp-facts"><span>${ref.imported ? '导入的参考' : '本机采集的参考'}</span><span>${ref.trusted_source ? '用户已声明可信' : '未声明来源可信'}</span><span>${html(date(ref.collected_at))}</span></div>${protocolHtml(protocolOf(ref),ref)}`, `<a class="btn fp-button" href="/fingerprint/references/${encodeURIComponent(id)}/export" download>导出指纹</a>${button('create-from-reference','用此参考采集',`data-fp-id="${html(id)}" ${health.usable ? '' : 'disabled'}`,'primary')}`);
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
      case 'select': select(id); break;
      case 'create-reference': openCreate('reference'); break;
      case 'fetch-models': discoverModels(); break;
      case 'model-input-mode': setModelInputMode(el.dataset.fpModelMode); break;
      case 'close-create': closeCreate(); break;
      case 'retry-create': { const mode = createMode(); closeCreate(); openCreate(mode); break; }
      case 'retry-data': ensureData(true); break;
      case 'retry-detail': loadDetail(true); break;
      case 'reload-references': loadReferences(); break;
      case 'tab': setTab(el.dataset.fpTab); break;
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
    if (s.drawerOpen && $('fpCreateForm').contains(event.target) && event.target.id !== 'fpRiskAck') {
      if (['fpSamples','fpConcurrency','fpTimeout','fpUrl','fpModel','fpModelSelect','fpKey'].includes(event.target.id)) $('fpRiskAck').checked = false;
      if (['fpUrl','fpKey'].includes(event.target.id)) invalidateModelConnection();
      if (event.target.id === 'fpSamples') updateBudget();
      if (event.target.id === 'fpUrl') updateAdapterHint();
    }
  }
  function onChange(event) {
    const el = event.target;
    if (el.name === 'fpMode') applyMode();
    if (['fpUrl','fpKey'].includes(el.id) && s.drawerOpen) invalidateModelConnection();
    if (el.id === 'fpModelSelect' || el.id === 'fpModel') $('fpRiskAck').checked = false;
    if (el.id === 'fpReference') { $('fpRiskAck').checked = false; applyReference(); }
    if (el.id === 'fpPreset') { const preset = findPreset(el.value); if (preset) $('fpSamples').value = preset.samples_per_cell; $('fpRiskAck').checked = false; updateBudget(); }
    if (el.id === 'fpAdapter') { s.adapterManual = true; s.adapterAuto = null; $('fpRiskAck').checked = false; updateAdapterHint(); }
    if (el.id === 'fpUrl') updateAdapterHint();
    if (el.id === 'fpImportFile') previewImport();
    if (el.id === 'fpCompareReference') updateCompareNotice();
  }
  function onKeydown(event) {
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
    const tab = event.target.closest?.('.fp-tab');
    if (tab && ['ArrowLeft','ArrowRight','Home','End'].includes(event.key)) {
      event.preventDefault(); const index = TABS.findIndex(t => t[0] === tab.dataset.fpTab), next = event.key === 'Home' ? 0 : event.key === 'End' ? TABS.length - 1 : (index + (event.key === 'ArrowRight' ? 1 : -1) + TABS.length) % TABS.length;
      setTab(TABS[next][0]); document.querySelector(`.fp-tab[data-fp-tab="${TABS[next][0]}"]`)?.focus();
    }
  }
  window.FingerprintUI = Object.freeze({init,setRuns,updateChrome,render,openCreate,closeCreate,hide,onAuthLost});
})();
