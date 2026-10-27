/* TPM（每分钟词元）module: offline preview, two paid confirmations, bounded observation. */
(() => {
  'use strict';
  const UI = window.UI, $ = id => document.getElementById(id);
  const s = {initialized:false,epoch:0,items:[],loaded:false,loading:false,selected:null,run:null,tab:'report',filter:'all',error:'',timer:null,polling:false,visible:false,drawer:false,draft:null,formMemory:null,preview:null,previewKey:'',busy:false,requestId:'',opener:null,sequence:0,models:[],manualModel:false,modelState:'idle',modelMessage:'',modelSequence:0,modelConnection:'',planningSequence:0,planningTimer:null,expanded:{}};
  const phases = {awaiting_calibration:'待确认校准',calibrating:'校准 / 预热',warming:'预热',awaiting_run:'待确认正式计划',running:'发压',draining:'收尾',finished:'已结束',interrupted:'意外中断',failed:'失败'};
  const reasons = {manual:'人工停止',user_stop:'人工停止',budget:'预算停止',budget_exhausted:'预算停止',completed:'计划结束',duration:'发压时限已到',interrupted:'服务重启中断',calibration_failed:'校准失败',manual_stop:'人工停止',budget_stop:'预算停止',cache_hit_low:'缓存命中率低于保护阈值',client_error:'客户端执行错误',calibration_error:'校准执行错误',cache_hit_rate:'缓存命中率保护',cache_hit_rate_low:'缓存命中率保护'};
  const active = () => typeof activeModule !== 'undefined' && activeModule === 'tpm';
  const signedIn = () => typeof authenticated === 'undefined' || authenticated;
  const live = run => ['awaiting_calibration','calibrating','warming','awaiting_run','running','draining'].includes(run?.phase);
  const n = (value, digits=2) => value == null || !Number.isFinite(Number(value)) ? '—' : Number(value).toLocaleString('zh-CN',{maximumFractionDigits:digits});
  const money = value => value == null ? '待确认' : `$${n(value,5)}`;
  const percent = value => value == null ? '—' : n(Number(value)*100,2)+'%';
  const modeLabel = mode => mode === 'avoid_cache' ? '尽量不命中缓存' : '缓存优先摸底';
  const label = run => run?.phase==='finished'&&run?.status==='error'?'失败':phases[run?.phase] || run?.phase || '待确认';
  const reason = value => reasons[value] || value || '—';
  const date = value => UI.formatTimestamp(value);
  const status = run => run?.phase === 'draining' ? 'stopping' : ['running','calibrating','warming'].includes(run?.phase) ? 'running' : live(run) ? 'waiting' : ['interrupted','failed','error'].includes(run?.status)||['interrupted','failed'].includes(run?.phase) ? 'error' : run?.stop_reason && !['completed','duration','plan_complete'].includes(run.stop_reason) ? 'stopped' : 'finished';
  const path = id => '/tpm/runs/'+encodeURIComponent(id);
  async function request(url, options) {
    const epoch=s.epoch, result=await api(url,options);
    if(epoch!==s.epoch || !signedIn()) throw Object.assign(new Error('登录会话已失效'),{stale:true});
    return result;
  }
  const post=(url,body={})=>request(url,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body),timeout:30000});
  function init() {
    if(s.initialized)return; s.initialized=true;
    try{s.selected=localStorage.getItem('tpm_selected_run')}catch{}
    $('recordPanel').insertAdjacentHTML('beforeend','<section id="tpmSubnav" class="workspace-subnav record-filter" aria-label="TPM（每分钟词元）记录分类" hidden><button class="subnav-item active" type="button" data-click-action="setTpmFilter" data-arg0="all"><span>全部记录</span></button><button class="subnav-item" type="button" data-click-action="setTpmFilter" data-arg0="active"><span>进行中</span></button></section><div class="task-list" id="tpmList" hidden></div>');
    document.body.insertAdjacentHTML('beforeend',`<div class="drawer-backdrop fp-backdrop" id="tpmBackdrop" data-click-action="closeTpmCreate" hidden></div><aside class="config fp-drawer" id="tpmDrawer" role="dialog" aria-modal="true" aria-labelledby="tpmCreateTitle" aria-hidden="true" inert><header class="drawer-head"><div><div class="form-title" id="tpmCreateTitle">新建 TPM（每分钟词元）压测</div><div class="form-sub">先离线预览，再分别确认收费校准和正式发压。</div></div><button class="drawer-close" type="button" data-click-action="closeTpmCreate" aria-label="关闭 TPM 压测表单">${UI.icon('close')}</button></header><div class="drawer-body" id="tpmCreateBody"></div><footer class="drawer-footer"><div class="form-status" id="tpmFormStatus" role="status" aria-live="polite"></div><button class="submit" id="tpmSubmit" type="button" data-click-action="previewTpm">查看方案与费用（不发请求）</button><div class="form-note">校准和预热也会收费，全部计入本轮预算。</div></footer></aside>`);
    document.addEventListener('input',onInput);
    document.addEventListener('change',onInput);
    document.addEventListener('keydown',onKeydown,true);
    document.addEventListener('toggle',onDetailsToggle,true);
    window.addEventListener('resize',drawChart);
    if($('tpmNavIcon'))$('tpmNavIcon').innerHTML=UI.icon('activity');
  }
  function updateChrome(){
    if(!s.initialized)init(); if(!active())return;const resumed=!s.visible;s.visible=true;if(resumed&&s.loaded&&!s.polling)refresh();
    for(const id of ['gatewaySubnav','qualitySubnav','routingSubnav','fingerprintSubnav','connectionsSubnav','gatewayList','qualityList','routingList','fingerprintList','connectionsList'])if($(id))$(id).hidden=true;
    $('tpmSubnav').hidden=false;$('tpmList').hidden=false;
    $('crumb').textContent='TPM（每分钟词元）压测 · 客户端完整用量';
    $('recordTitle').textContent='TPM 压测记录';$('recordSubtitle').textContent='长输入、短输出 · 两阶段确认 · 预算保护';
    $('recordCreateButton').innerHTML=UI.icon('plus')+'<span>新建 TPM 压测</span>';
  }
  function hide(){s.visible=false;if($('tpmSubnav'))$('tpmSubnav').hidden=true;if($('tpmList'))$('tpmList').hidden=true;clearTimeout(s.timer);s.timer=null;if(s.drawer)closeCreate()}
  function schedule(){clearTimeout(s.timer);if(active()&&signedIn())s.timer=setTimeout(()=>refresh(),s.selected&&!s.run?500:s.run&&live(s.run)?1500:10000)}
  async function refresh(){
    if(!active()||!signedIn()||s.polling||document.hidden){schedule();return}
    const epoch=s.epoch;s.polling=true;s.loading=!s.loaded;
    try{
      const result=await request('/tpm/runs');if(epoch!==s.epoch)return;s.items=result.runs||[];s.loaded=true;s.error='';
      const items=filtered();if(!items.some(run=>run.id===s.selected))s.selected=items[0]?.id||null;
      const selected=s.selected;if(selected){const result=await request(path(selected)+'/live');if(s.selected===selected)s.run=result.run||result}else s.run=null;
      if(epoch!==s.epoch)return;s.loading=false;render();
    }catch(error){if(!error.stale&&epoch===s.epoch){s.loading=false;s.error=error.message;render()}}
    finally{if(epoch===s.epoch){s.polling=false;s.loading=false;schedule()}}
  }
  const filtered=()=>s.items.filter(run=>s.filter!=='active'||live(run));
  function detailFocus(host){
    const node=document.activeElement;if(!node||!host.contains(node))return null;
    if(node.id)return {id:node.id};
    if(node.tagName==='SUMMARY'&&node.parentElement?.id)return {summary:node.parentElement.id};
    if(node.dataset?.clickAction)return {action:node.dataset.clickAction,args:Object.entries(node.dataset).filter(([key])=>/^arg\d+$/.test(key))};
    return null;
  }
  function restoreDetailFocus(host,focus){
    if(!focus)return;
    const node=focus.id?$(focus.id):focus.summary?$(focus.summary)?.querySelector('summary'):Array.from(host.querySelectorAll('[data-click-action]')).find(item=>item.dataset.clickAction===focus.action&&focus.args.every(([key,value])=>item.dataset[key]===value));
    if(node&&host.contains(node)&&!node.disabled)node.focus({preventScroll:true});
  }
  function render(){
    if(!active()||!signedIn())return;if(!s.initialized)init();updateChrome();
    for(const id of ['detail','empty','loadingState'])$(id).hidden=true;
    const items=filtered(),host=$('modulePlaceholder'),sameView=host.dataset.tpmId===s.selected&&host.dataset.tpmTab===s.tab,focus=sameView?detailFocus(host):null;
    const scrollers=['.main','.shell'].map(selector=>document.querySelector(selector)).filter(Boolean).map(node=>({node,top:sameView?node.scrollTop||0:0,left:sameView?node.scrollLeft||0:0}));
    const tableScroll=sameView?Array.from(host.querySelectorAll('.table-wrap'),node=>({left:node.scrollLeft,top:node.scrollTop})):[];
    host.querySelectorAll('details[id]').forEach(node=>{s.expanded[`${host.dataset.tpmId}:${host.dataset.tpmTab}:${node.id}`]=node.open});
    host.hidden=false;host.dataset.tpmId=s.selected||'';host.dataset.tpmPhase=s.run?.phase||'';host.dataset.tpmTab=s.tab;
    $('tpmSubnav').querySelectorAll('[data-arg0]').forEach(node=>node.classList.toggle('active',node.dataset.arg0===s.filter));
    $('tpmList').innerHTML=items.length?items.map(run=>UI.recordItem({id:run.id,title:run.config?.name||run.name||run.config?.model||run.model||'TPM 压测',state:status(run),stateLabel:label(run),typeLabel:modeLabel(run.config?.cache_mode??run.cache_mode),meta:[n(run.config?.target_tpm||run.target_tpm,0)+' 词元/分钟',date(run.timing?run.timing.created_at:run.created_at??run.created).split(' ')[0]],progress:null,selected:run.id===s.selected,action:'selectTpm'})).join(''):UI.recordEmpty(s.error?'记录暂时无法更新':s.loading?'正在读取记录':s.filter==='active'?'暂无进行中的任务':'暂无 TPM 压测记录',s.filter==='active'?'已结束的测试可在“全部记录”中查看。':'新建后先校准；刷新页面不会重新发起请求。');
    const waiting=s.selected||s.loading||s.error;
    const empty=UI.pageShell(UI.pageHeader({kicker:'TPM（每分钟词元）压测 · 长输入短输出',title:waiting?(s.error?'暂时无法读取压测详情':s.selected?'正在读取压测详情':'正在读取记录'):s.filter==='active'?'当前没有进行中的任务':'先确认用量，再施加压力',description:waiting?'稍后会自动重试；刷新页面不会重新发起收费请求。':s.filter==='active'?'切换到全部记录查看已结束的测试和结论。':'完整 Token（词元）TPM：最近 60 秒成功完整结束请求的完整输入与输出之和。缓存输入只计一次。'})+(waiting?'':s.filter==='active'?UI.section({title:'查看已有结果',bodyHtml:UI.button('查看全部记录',{action:'setTpmFilter',args:['all']})}):UI.section({title:'安全发起一次测试',bodyHtml:UI.steps(['选择连接和模型，查看不访问上游的配置预览','确认收费校准，核验真实输入、用量和耗时','确认校准后的正式计划，观察吞吐、费用与收尾'])+UI.notice('所有结论仅适用于本次缓存条件和客户端链路，无法证明供应商内部容量。',{tone:'warning'})})));
    const content=s.run&&s.run.id===s.selected?detailHtml(s.run):empty;
    host.innerHTML=s.error?UI.stack(UI.notice(s.error,{tone:'danger'})+content):content;
    host.querySelectorAll('.table-wrap').forEach((node,index)=>{if(tableScroll[index]){node.scrollLeft=tableScroll[index].left;node.scrollTop=tableScroll[index].top}});
    restoreDetailFocus(host,focus);
    for(const {node,top,left} of scrollers){node.scrollTop=top;node.scrollLeft=left}
    if(!sameView)window.scrollTo?.({top:0,left:0,behavior:'instant'});
    drawChart();if(!s.loaded&&!s.polling)refresh();
  }
  async function select(id){s.selected=id;s.run=null;s.tab='report';try{localStorage.setItem('tpm_selected_run',id)}catch{}render();if(s.polling){schedule();return}await refresh()}
  function setFilter(value){const filter=value==='active'?'active':'all';if(s.filter===filter)return;s.filter=filter;const items=filtered();if(!items.some(run=>run.id===s.selected)){s.selected=items[0]?.id||null;s.run=null;s.tab='report'}render();refresh()}
  function setTab(value){s.tab=['report','plan','requests'].includes(value)?value:'report';render()}
  const metric=(label,value,unit='',note='',primary=false)=>({label,value,unit,note,primary});
  function onDetailsToggle(event){
    const node=event.target,host=$('modulePlaceholder');
    if(node?.tagName==='DETAILS'&&node.id&&node.isConnected&&host?.contains(node))s.expanded[`${host.dataset.tpmId}:${host.dataset.tpmTab}:${node.id}`]=node.open;
  }
  const fold=(id,title,body,runId='')=>UI.disclosure({id,title,bodyHtml:body,section:Boolean(runId),open:Boolean(s.expanded[`${runId}:${s.tab}:${id}`])});
  function executionHtml(plan={},config={}){
    const execution=plan.execution_plan||{},auto=(execution.mode||config.execution_mode||'manual')==='auto';
    const source=execution.source==='calibration_p95'?'校准 P95（95 分位）':'校准前耗时假设';
    return '<p class="field-hint">'+UI.esc((auto?'自动规划':'手动覆盖')+'：'+n(plan.target_rps)+' 请求/秒，并发 '+n(execution.concurrency??plan.concurrency??config.concurrency,0)+'，连接池 '+n(execution.pool_connections??plan.pool_connections??config.pool_connections,0)+'；按'+source+' '+n(execution.latency_seconds??config.planning_latency_seconds??30)+' 秒。')+'</p>'+(execution.capped?UI.notice('所需并发超过本机资源上限 '+n(execution.resource_limit,0)+'，实际计划已受限；无法保证施加到目标压力。',{tone:'warning'}):'');
  }
  function warningTexts(plan={}){return (plan.warnings||[]).map(item=>typeof item==='string'?item:item.message||JSON.stringify(item))}
  function planWarnings(plan={}){
    // Routine methodology belongs with the full plan; concrete constraints stay next to confirmation.
    const routine=/^(校准、预热、正式请求和收尾均可能收费|完整 Token TPM|缓存命中是估算条件|正式发压不重试|尚未校准：|执行容量暂按)/;
    const warnings=warningTexts(plan).filter(text=>!routine.test(text));
    return warnings.length?UI.limitations(warnings):'';
  }
  function planSummaryHtml(plan={},config={},calibration=null){
    return UI.metrics([
      metric('本轮预计费用',money(plan.estimated_cost_usd),'美元',calibration?'按校准实测估算':'含校准、预热、正式计划'),
      metric('保守最坏预估',money(plan.worst_case_cost_usd),'美元'),
      metric('预算上限',money(config.budget_usd),'美元','不足时停发收尾')
    ])+executionHtml(plan,config)+'<p class="field-hint">'+UI.esc('目标档 '+n(config.target_duration_seconds)+' 秒；前三档各 '+n(config.ramp_duration_seconds)+' 秒。校准 '+money(plan.calibration_cost_usd)+'，预热 '+money(plan.warmup_cost_usd)+'，均计入预算。')+'</p><p class="field-hint">'+UI.esc('请求体 '+n(plan.request_body_bytes??plan.body_bytes,0)+' 字节；预计上行 '+n(plan.estimated_upload_mbps??plan.upload_mbps??plan.required_upload_mbps)+' Mbps（兆比特/秒）。'+(calibration?'':'这是初始探测体，校准后更新完整长输入体。'))+'</p>'+(config.cache_write_price_mode==='unconfirmed'?UI.notice('缓存写入价格待确认，不能开始收费校准。',{tone:'warning'}):'')+(Number(config.target_duration_seconds)<60?UI.notice('目标档不足 60 秒：可诊断请求压力，但无法形成完整分钟的达标结论。',{tone:'warning'}):'')+(Number(plan.worst_case_cost_usd)>Number(config.budget_usd)?UI.notice('保守最坏预估超过预算，可能在完成阶梯前停发收尾；预算不会自动提高。',{tone:'warning'}):'')+planWarnings(plan);
  }
  function planHtml(plan={},config={}){
    const stages=plan.stages||[];
    return UI.metrics([
      metric('目标档计划开始速率',n(plan.target_rps),'请求/秒','按校准实际输入与输出均值换算'),
      metric('运行前预计费用',money(plan.estimated_cost_usd),'美元','校准前按预期、校准后按实测缓存比例估计'),
      metric('保守最坏预估',money(plan.worst_case_cost_usd),'美元','以完整输入、硬上限和适用写入价计算'),
      metric('单次请求保守预留',money(plan.request_reserve_usd),'美元',plan.reserve_basis||'使用最贵适用输入价与输出硬上限'),
      metric('校准预计费用',money(plan.calibration_cost_usd),'美元'),metric('预热预计费用',money(plan.warmup_cost_usd),'美元'),
      metric('本轮总预算',money(config.budget_usd),'美元','包含校准、预热、发压和收尾'),metric('正式计划完整用量',n(plan.total_planned_tokens,0),'Token（词元）','目标值；不是实测完成量'),
      metric('请求体大小',n(plan.request_body_bytes??plan.body_bytes,0),'字节',plan.request_body_note||'实际初始探测体；校准后才是完整长输入体'),metric('预计上行带宽',n(plan.estimated_upload_mbps??plan.upload_mbps??plan.required_upload_mbps),'Mbps（兆比特/秒）'),
      metric('建议并发 / 执行并发',n(plan.required_concurrency,0)+' / '+n(plan.execution_plan?.concurrency??plan.concurrency??config.concurrency,0),'',((plan.execution_plan?.mode||config.execution_mode||'manual')==='auto'?'自动规划；':'手动覆盖；')+(plan.execution_plan?.source==='calibration_p95'?'按校准 P95（95 分位）':'按校准前耗时假设')+' × 目标速率 × 1.5'),metric('连接池容量',n(plan.execution_plan?.pool_connections??plan.pool_connections??config.pool_connections,0),'连接')
    ])+UI.table({columns:['档位','目标 TPM（每分钟词元）','发压时长','计划开始速率','计划请求数'].map(label=>({label})),rowsHtml:stages.map((stage,index)=>[index+1,n(stage.target_tpm,0),n(stage.duration_seconds)+' 秒',n(stage.rps)+' 请求/秒',n(stage.planned_requests,0)])})+
    UI.methodCards([{title:'冻结判断规则',lines:criteriaLines(plan.criteria||config.criteria||{})},{title:'冻结报价（美元 / 百万词元）',lines:['普通输入：'+money(config.prices?.input),'输出：'+money(config.prices?.output),'缓存读取：'+money(config.prices?.cache_read),'缓存写入：'+money(plan.prices?.cache_write??config.prices?.cache_write)]},{title:'请求结构与容量',lines:['目标完整输入：'+n(config.input_tokens,0)+' Token（词元）','期望输出 / 硬上限：'+n(config.expected_output_tokens,0)+' / '+n(config.max_output_tokens,0)+' Token（词元）','总超时：'+n(config.timeout_seconds)+' 秒；从实际开始计时','模板组数 / 每组预热：'+n(config.template_groups,0)+' / '+n(config.warmup_requests,0),'业务失败重试：关闭；网关内部重试：未知']},{title:'费用与缓存边界',lines:['价格单位：美元 / 百万 Token（词元）。',plan.cost_basis||'预计命中率只用于费用预测；请求发送前按保守预留检查预算。','预算保护依赖用户报价与可获得用量，不保证约束不可见上游收费。','收尾仅结算已发送请求，不按等待时长重复计费。']}])+
    (plan.warnings?.length?UI.limitations(plan.warnings.map(item=>typeof item==='string'?item:item.message||JSON.stringify(item))):'');
  }
  function criteriaLines(criteria){return [
    `严格达到要求：完整 60 秒窗口实测用量 ≥ 目标的 ${percent(criteria.reached_ratio??1)}；接近阈值 ${percent(criteria.near_target_ratio??0.95)}。`,
    `成功率要求 ${percent(criteria.min_success_rate??0.99)}；用量完整率要求 ${percent(criteria.min_usage_complete_rate??1)}。`,
    `允许错过时隙比例 ≤ ${percent(criteria.max_missed_ratio??0.01)}；延迟恶化阈值为前半段 P95（95 分位）的 ${n(criteria.max_latency_degradation_ratio??2)} 倍。`,
    `积压判定：目标档至少 8 个采样点，末四分之一平均在途比首四分之一增加超过 ${percent(criteria.max_backlog_growth_ratio??0.1)}，且超过 5 个请求。`,
    `错误恶化：目标档前后半段各至少 5 条请求，后半段错误率增加超过 ${n((1-(criteria.min_success_rate??0.99))*100)} 个百分点。`,
    '主要观察区间为目标档最后一个完整 60 秒窗口；只纳入目标档发送的请求。',
    '窗口不足、用量不可靠或发压端受限时证据不足；持续积压或明显错误不能稳定通过。'
  ]}
  function feesHtml(f={},config={}){
    return UI.section({title:'费用与预算',description:'按已获得用量和用户报价计算，不是商家最终账单。',bodyHtml:UI.metrics([
      metric('已知费用',money(f.known_cost_usd),'美元','含校准、预热、失败中已获得用量和收尾'),
      metric('未结算保守预留',money(f.reserved_cost_usd),'美元','在途及未知用量不归零')
    ])+'<p class="field-hint">'+UI.esc('本轮预算 '+money(config.budget_usd)+'；保护依赖报价及可获得用量，不能保证覆盖不可见上游收费。')+'</p>'});
  }
  function detailHtml(run){
    const activeStage=(run.stages||[]).findLast(stage=>stage.started_at!=null&&!stage.ended_at);
    const m=run.metrics||{},f=run.fees||{},cal=run.calibration||{},report=run.report||{},config=run.config||{},plan=run.plan||{},current=run.phase||'',done=!live(run),warnings=report.limitations||[],verdict=report.label||report.verdict_label||({reached:'达到目标',near:'接近目标',not_reached:'未达到',insufficient:'证据不足',insufficient_evidence:'证据不足 / 发压端受限',client_limited:'证据不足 / 发压端受限'}[report.verdict])||report.verdict;
    const stoppable=live(run)&&current!=='draining',preformal=['awaiting_calibration','calibrating','warming'].includes(current),calibrationFailed=run.stop_reason==='calibration_failed',formalStarted=run.formal_started_at!=null||(run.stages||[]).some(stage=>stage.started_at!=null)||Number(m.started)>0||Number(report.actual_started)>0;
    const action=done?UI.button('再来一次',{action:'repeatTpm',args:[run.id],iconName:'refresh',disabled:s.busy}):stoppable?UI.button(current==='awaiting_run'||current==='awaiting_calibration'?'放弃本次计划':'停止发新请求并收尾',{action:'stopTpm',args:[run.id],iconName:'stop',disabled:s.busy}):'';
    const resultTone=({reached:'success',near:'warning',not_reached:'danger',insufficient:'warning',insufficient_evidence:'warning',client_limited:'warning'})[report.verdict]||'neutral';
    const heading=UI.pageHeader({kicker:'TPM（每分钟词元）压测',title:config.name||config.model||'长输入短输出压测',description:modeLabel(config.cache_mode)+' · '+(done?'结论仅适用于本次客户端链路与缓存条件。':'长输入、短输出；预算不足时停止发新请求。'),meta:[config.model||'模型待定',config.url||config.base_url||'地址已保存'],statusLabel:!formalStarted&&done?label(run):verdict||label(run),statusTone:status(run)==='error'?'danger':done?resultTone:current==='awaiting_run'||current==='draining'?'warning':'accent',statusNote:run.stop_reason?'停止原因：'+reason(run.stop_reason):current==='awaiting_run'?'等待第二次费用确认':current==='draining'?'已停发，等待在途请求结算':'',actionsHtml:action});
    const tabs=UI.tabs({label:'TPM 压测详情',items:[['report',done?'结果报告':current==='awaiting_run'?'确认计划':'实时概览'],['plan','冻结计划与校准'],['requests','逐请求摘要']],active:s.tab,action:'setTpmTab',ownerId:run.id});
    let body='';
    if(s.tab==='plan')body=UI.section({title:'正式计划与费用',description:'正式开始前冻结；改变缓存模式或预算需新建测试。',bodyHtml:planHtml(plan,config)})+UI.section({title:'校准结果',bodyHtml:calibrationHtml(cal)+UI.methodCards([{title:'合成语料',lines:['语料种子：'+(config.seed||'—'),'语料指纹：'+(run.corpus?.fingerprint||run.corpus_fingerprint||'—'),'计数来源：'+({upstream_usage_calibrated:'上游用量校准',local_tokenizer_content_estimate:'本地分词器内容估计',unmeasured_synthetic_probe:'待实测的合成探测'}[run.corpus?.source]||'未知（尚未获得来源）')]}])});
    else if(s.tab==='requests')body=requestsHtml(run);
    else if(preformal)body=UI.section({title:config.cache_mode==='avoid_cache'?'正在校准':'正在校准与预热',description:'读取真实响应，核验长输入、用量和耗时；不会自动进入正式压测。',bodyHtml:UI.notice('本阶段会收费，全部计入预算。校准结束后需要再次确认正式计划。',{tone:'accent'})+UI.metrics([metric('当前在途',n(m.all_in_flight??m.in_flight,0),'请求'),metric('计划校准',n(config.calibration_requests,0),'请求')])})+feesHtml(f,config);
    else if(current==='awaiting_run')body='<div id="tpmRunConfirmation">'+UI.section({title:'校准完成，请确认正式计划',description:'确认后才开始发压；任务仍独占资源，不继续可在页头放弃。',bodyHtml:
      '<p class="field-hint">'+UI.esc('实测每请求：输入 '+n(cal.mean_input_tokens,0)+' + 输出 '+n(cal.mean_output_tokens,0)+' 词元；P95（95 分位）'+n(cal.p95_seconds)+' 秒；用量完整率 '+percent(cal.usage_complete_rate)+'，缓存命中 '+percent(cal.cache_hit_rate)+'。')+'</p>'+
      planSummaryHtml(plan,config,cal)+(cal.issues?.length?UI.limitations(cal.issues):'')+
      (plan.duration_change_requires_confirmation?UI.notice('建议目标档延长至 '+n(plan.suggested_target_duration_seconds)+' 秒，应用后重估费用；不会自动发压。',{tone:'warning'})+UI.button('应用建议时长并重新估价',{action:'adjustTpmPlan',args:[run.id],disabled:s.busy}):'')+
      '<p class="field-hint">完整阶梯、报价和判断规则见“冻结计划与校准”。校准与预热计费，不计入正式达标统计。</p>'+UI.button(s.busy?'正在提交…':'确认正式计划并发压',{variant:'primary',action:'startTpm',args:[run.id],disabled:s.busy||cal.passed===false})})+'</div>'+feesHtml(f,config);
    else if(!formalStarted&&(done||current==='draining'))body=UI.section({title:current==='draining'?'校准收尾，未发起正式压测':calibrationFailed?'校准失败，未发起正式压测':'未发起正式压测',description:'本次仅有校准或计划准备记录，不作正式吞吐达标判断。',bodyHtml:(current==='draining'?UI.notice('已停止调度新请求，正在等候已发送请求结束、结算和落盘。每条请求仅使用其剩余总超时。',{tone:'warning'})+'<p class="field-hint">'+UI.esc('当前在途 '+n(m.all_in_flight??m.in_flight,0)+' 请求。')+'</p>':'')+(Object.keys(cal).length?calibrationHtml(cal):UI.notice('停止原因：'+reason(run.stop_reason),{tone:status(run)==='error'?'warning':'neutral'}))})+feesHtml(f,config);
    else {
      const primary=report.primary_window||{},mainTokens=done?(primary.valid?(primary.full_tokens??primary.full_token_tpm??primary.tokens):null):m.full_token_tpm;
      const observed=value=>value==null?'证据不足':value?'检出':'样本内未检出';
      body=(current==='draining'?UI.notice('已停止调度新请求，正在等候已发送请求结束、结算和落盘。每条请求仅使用其剩余总超时。',{tone:'warning'}):'')+
        '<div id="tpmKeyMetrics">'+UI.section({title:done&&primary.valid?'目标档最后一个完整 60 秒窗口':'客户端完成用量',description:'完整输入 + 输出，按成功完整结束时间计量；不代表供应商内部容量。',bodyHtml:
          (!done&&!m.window_full?UI.notice(`当前滚动窗口仅 ${n(m.window_seconds)} 秒，尚未满 60 秒；显示已完成用量，不外推为一分钟。`,{tone:'warning'}):'')+UI.metrics([
            metric(!done&&!m.window_full?'当前窗口完成用量':'完整 Token（词元）TPM',n(mainTokens,0),!done&&!m.window_full?'词元':'词元/分钟',done?(primary.valid?'目标档最后完整分钟':'主要窗口证据不足'):m.window_full?'最近 60 秒真实成功完成量':'最近 '+n(m.window_seconds)+' 秒，未外推',true),metric('最终目标 TPM（每分钟词元）',n(config.target_tpm,0),'',current==='running'&&activeStage?'当前第 '+activeStage.index+' 档：'+n(activeStage.target_tpm,0)+' 词元/分钟':''),
            metric('实际开始速率',n(m.actual_start_rps),'请求/秒'),metric('成功率',percent(m.success_rate)),metric('在途请求',n(m.all_in_flight??m.in_flight,0),'条'),metric('用量完整率',percent(m.usage_complete_rate)),metric('缓存读取命中率',percent(m.cache_hit_rate)),metric('429（限流）/ 超时',n(m.http_429,0)+' / '+n(m.timeouts,0))
          ])+(primary.valid?'<p class="field-hint">'+UI.esc('主要窗口达成 '+percent(primary.ratio??report.achievement_ratio)+'；滚动峰值 '+n(report.rolling_peak_tpm,0)+' 词元/分钟，仅作辅助。')+'</p>':done?UI.notice('目标档没有有效的完整 60 秒观察窗口，或可靠用量不足：证据不足。',{tone:'warning'}):'')})+'</div>'+
        UI.hint('已知费用 '+money(f.known_cost_usd)+' · 保守预留 '+money(f.reserved_cost_usd)+' · 本轮预算 '+money(config.budget_usd)+'。不是商家最终账单；在途及未知费用不归零。',{title:'费用与预算（美元）'})+(done?UI.hint('实际施压 '+percent(report.applied_ratio)+'；错误增加：'+observed(report.errors_increased)+'；延迟恶化：'+observed(report.latency_worsened)+'；持续积压：'+observed(report.sustained_backlog)+'。'):'')+(warnings.length?UI.limitations(warnings):'')+
        '<div id="tpmTrend">'+UI.section({title:'全程曲线',description:'紫色为成功完成用量，虚线为目标；包括收尾时间。',bodyHtml:'<div class="chart-wrap"><canvas id="tpmChart" role="img" aria-label="成功完成 TPM（每分钟词元）与目标随时间变化"></canvas>'+((run.series||[]).some(row=>run.formal_started_at&&row.at>=run.formal_started_at)?'':'<div class="chart-empty">'+(done?'本次未记录可绘制的正式阶段采样。':'等待首个正式阶段采样；收到后自动绘制。')+'</div>')+'</div>'})+'</div>'+
        fold('tpmRuntimeDetails','查看用量、费用、延迟与调度明细',feesHtml(f,config)+UI.section({title:'运行明细',bodyHtml:UI.metrics([
          metric('当前滚动完成用量',n(m.full_token_tpm,0),m.window_full?'词元/分钟':'词元',m.window_full?'可能包含收尾，不作为目标档主要结论':'当前窗口 '+n(m.window_seconds)+' 秒，未外推；可能包含收尾'),metric('计划调度 / 实际开始',n(m.planned,0)+' / '+n(m.started,0)),metric('成功 / 失败',n(m.succeeded,0)+' / '+n(m.failed,0)),metric('未发出 / 错过时隙',n(m.not_sent??m.missed,0)),metric('达到输出硬上限',n(m.output_limited??m.truncated??m.output_truncated,0),'条'),
          metric('完整输入 / 输出',n(m.input_tpm,0)+' / '+n(m.output_tpm,0),'Token（词元）'),metric('缓存读取 / 写入',n(m.cache_read_tpm,0)+' / '+n(m.cache_write_tpm,0),'Token（词元）'),metric('首字 P50 / P95（分位耗时）',n(m.ttft_p50)+' / '+n(m.ttft_p95),'秒'),metric('完整 P50 / P95（分位耗时）',n(m.latency_p50)+' / '+n(m.latency_p95),'秒'),metric('调度延迟 P95（95 分位）',n(m.schedule_delay_p95),'秒'),metric('连接池等待 P95（95 分位）',n(m.pool_wait_p95),'秒','客户端准入等待；传输库内部等待未知')
        ])})+(done?reportHtml(report):'')+UI.section({title:'发压与收尾',bodyHtml:UI.methodCards([{title:'时间与停止原因',lines:['发压结束：'+date(run.dispatch_ended_at),'最后请求结束：'+date(run.last_request_ended_at??report.last_request_ended_at),'收尾耗时：'+n(run.drain_seconds??report.drain_seconds)+' 秒','停止原因：'+reason(run.stop_reason)]},{title:'费用与观察边界',lines:['未知账单风险：'+(f.reserved_cost_usd>0?'仍有保守预留，可能继续计费':'以可获得用量和用户报价为限。'),'客户端业务失败重试关闭；网关内部重试未知。','刷新只恢复观察；服务重启标为中断，不恢复收费请求。']}])}),run.id);
    }
    return UI.pageShell(heading+tabs+(run.error?UI.notice(run.error,{tone:'danger'}):'')+body+UI.timeRecords(run,{live:live(run),key:'tpm:'+run.id})+'<p class="field-hint">缓存场景结论不能外推非缓存业务；客户端完成时间口径可能与商家日志、面板或限流口径不同。</p>'+UI.actionBar(UI.button('导出报告与逐请求摘要',{action:'download',args:[path(run.id)+'/export'],iconName:'download'})));
  }
  function reportHtml(report){
    const usage=report.cumulative_usage||{},known=(key)=>n(usage[key],0)+(usage[key+'_complete']===false?'（仅已知部分）':'');
    const yesNo=value=>value==null?'未获得足够观测':value?'是':'未检出（受样本范围限制）';
    return UI.section({title:'计划压力与稳定性复盘',bodyHtml:UI.methodCards([
      {title:'主要观察窗口 · 新加坡时间（UTC+08:00）',lines:['开始：'+date(report.primary_window?.start),'结束：'+date(report.primary_window?.end),report.primary_window?.valid?'目标档最后一个完整 60 秒窗口；按客户端完成时间归属。':'没有有效完整窗口，不能据此宣称达到目标。']},
      {title:'是否实际施加计划压力',lines:['实际开始 / 计划：'+n(report.actual_started,0)+' / '+n(report.planned,0),'实际施加比例：'+percent(report.applied_ratio),'未发出时隙：'+n(report.missed,0),'发压端受限：'+yesNo(report.local_limited)]},
      {title:'错误、延迟与积压',lines:['错误比例增加：'+yesNo(report.errors_increased),'延迟恶化：'+yesNo(report.latency_worsened),'持续积压：'+yesNo(report.sustained_backlog),'目标档成功率：'+percent(report.target_success_rate),'目标档用量完整率：'+percent(report.target_usage_complete_rate)]}
    ])})+UI.section({title:'累计用量与输入输出结构',description:'包含校准、预热、正式请求、失败中已获得的用量和收尾。未知分类标记为仅已知部分；与滚动完成 TPM（每分钟词元）分开。',bodyHtml:UI.metrics([
      metric('普通输入',known('input_tokens'),'Token（词元）'),metric('缓存读取',known('cache_read_tokens'),'Token（词元）'),metric('缓存写入',known('cache_write_tokens'),'Token（词元）'),metric('完整输入',known('full_input_tokens'),'Token（词元）'),metric('输出',known('output_tokens'),'Token（词元）'),metric('完整用量合计',known('total_tokens'),'Token（词元）')
    ])});
  }
  function calibrationHtml(cal){
    if(!Object.keys(cal).length)return UI.notice('尚未获得校准结果。确认收费校准后，才会记录实际输入、输出与耗时。');
    const metrics=cal.mean_input_tokens==null&&cal.mean_output_tokens==null?'':UI.metrics([metric('实际完整输入均值',n(cal.mean_input_tokens,0),'Token（词元）'),metric('实际输出均值',n(cal.mean_output_tokens,0),'Token（词元）'),metric('完整耗时 P95（95 分位）',n(cal.p95_seconds),'秒'),metric('用量完整率 / 缓存命中率',percent(cal.usage_complete_rate)+' / '+percent(cal.cache_hit_rate))]);
    return (cal.passed===false?UI.notice('校准未通过。请查看逐请求摘要中的错误，修正接口或协议后再来一次。',{tone:'warning'}):'')+metrics+(cal.issues?.length?UI.limitations(cal.issues):'');
  }
  function errorLabel(value){return ({http_429:'429（限流）',http_5xx:'服务端错误',http_4xx:'请求被拒绝',timeout:'总超时',network:'网络错误',stream_error:'流内错误',output_limit:'达到输出硬上限',incomplete_stream:'响应不完整',invalid_stream_json:'流式内容解析失败',empty_output:'空输出',client_error:'客户端错误',cancelled:'强制取消',budget_stop:'预算停止',unknown:'未知'})[value]||(value?value+'（请求错误）':'')}
  function errorDetailText(row){const detail=row?.error_detail;if(!detail)return '';if(typeof detail==='string')return detail;return [['HTTP（超文本传输协议）状态码',detail.http_status],['结束原因',detail.finish_reason],['流完整',detail.stream_done==null?null:(detail.stream_done?'是':'否')],['用量完整性',detail.usage_completeness],['结束时间',detail.ended_at]].filter(([,value])=>value!=null&&value!=='').map(([label,value])=>label+'：'+value).join('；')}
  function requestsHtml(run){
    const rows=run.recent||run.recent_requests||[];
    return '<div id="tpmRequestTable">'+UI.section({title:'最近逐请求摘要',description:'先看结果与发送阶段，展开时间可核查每次请求。表格可横向滚动，完整请求标识在最右列；导出包含全部摘要，默认不保存原文。',bodyHtml:UI.table({columns:['结果','发送阶段','时间记录','完整输入 / 输出','用量状态','请求标识'].map(label=>({label})),rowsHtml:rows.slice(-100).map(row=>[
      UI.tag(errorLabel(row.error)||(row.success?'成功完整结束':'失败'),row.success?'success':'danger')+(row.output_limited||row.truncated?' '+UI.tag('达到硬上限','warning'):'')+(errorDetailText(row)?`<small class="role-label">${UI.esc(errorDetailText(row))}</small>`:''),
      UI.esc(row.stage_index!=null?'正式第 '+row.stage_index+' 档':({calibration:'校准',warmup:'预热',formal:'正式'}[row.phase]||'—')),UI.timeRecords(row,{request:true,key:'tpm-request:'+run.id+':'+(row.id??row.request_id)}),
      n(row.usage?.full_input_tokens??row.full_input_tokens,0)+' / '+n(row.usage?.output_tokens??row.output_tokens,0),
      UI.esc(row.usage?.complete?'完整':({partial:'部分已知',missing:'缺失',ambiguous:'语义不明确',complete:'完整'}[row.usage?.completeness]||'未知')),UI.esc(row.id??row.request_id)
    ])})})+'</div>';
  }
  function drawChart(){
    const canvas=$('tpmChart');if(!canvas)return;const rows=(s.run?.series||[]).filter(row=>s.run?.formal_started_at&&row.at>=s.run.formal_started_at);const rect=canvas.parentElement.getBoundingClientRect(),w=Math.max(100,rect.width),h=Math.max(150,rect.height),dpr=window.devicePixelRatio||1;canvas.width=Math.round(w*dpr);canvas.height=Math.round(h*dpr);const ctx=canvas.getContext('2d');ctx.setTransform(dpr,0,0,dpr,0,0);ctx.clearRect(0,0,w,h);if(!rows.length)return;
    const css=getComputedStyle(document.documentElement),token=name=>css.getPropertyValue(name).trim(),points=rows.map(row=>({time:row.elapsed_seconds??row.elapsed??Math.max(0,(row.at||0)-(s.run?.formal_started_at||rows[0]?.at||0)),done:row.full_token_tpm??row.metrics?.full_token_tpm??0,target:row.target_tpm??((s.run?.stages||[]).find(stage=>row.at>=stage.started_at&&row.at<=(stage.ended_at||Infinity))?.target_tpm||0)})),max=Math.max(1,...points.flatMap(row=>[row.done,row.target])),last=Math.max(1,...points.map(row=>row.time));
    ctx.font=token('--text-micro')+' '+token('--font-mono');ctx.fillStyle=token('--color-muted');ctx.strokeStyle=token('--color-chart-grid');ctx.lineWidth=1;
    for(let i=0;i<5;i++){const y=12+(h-42)*i/4;ctx.beginPath();ctx.moveTo(58,y);ctx.lineTo(w-12,y);ctx.stroke();ctx.fillText(n(max*(1-i/4)/1000000,1)+' 百万',4,y+4)}
    for(const [key,color,dash]of [['done','--color-accent',[]],['target','--color-chart-plan',[5,5]]]){ctx.strokeStyle=token(color);ctx.lineWidth=2;ctx.setLineDash(dash);ctx.beginPath();points.forEach((p,index)=>{const x=58+(w-70)*p.time/last,y=h-30-(h-42)*p[key]/max;index?ctx.lineTo(x,y):ctx.moveTo(x,y)});ctx.stroke()}
    ctx.setLineDash([]);ctx.fillStyle=token('--color-muted');ctx.fillText('0 秒',58,h-8);ctx.fillText(n(last,0)+' 秒',Math.max(58,w-65),h-8);
  }
  const field=(key,label,value,options={})=>UI.field({id:'tpm_'+key,label,controlHtml:UI.input({id:'tpm_'+key,value,...options}),hint:options.hint,required:options.required});
  const numberField=(key,label,value,options={})=>field(key,label,value,{type:'number',min:0,step:'any',...options});
  const selectField=(key,label,value,options,hint='')=>UI.field({id:'tpm_'+key,label,controlHtml:UI.select({id:'tpm_'+key,value,options}),hint});
  const targetPresets=['50000000','100000000','150000000','200000000'];
  const manualConnection='__manual__';
  function initialConnection(c,connections){return connections.some(item=>item.id===c.connection_id)?c.connection_id:c.url||c.base_url||!connections.length?manualConnection:''}
  function formHtml(c){
    const prices=c.prices||{},connections=window.ConnectionUI?.list()||[],connection=initialConnection(c,connections);
    return UI.formSectionTitle(1,'连接与模型')+
      UI.inlineFields(selectField('connection_id','常用连接',connection,[['','请选择连接'],...connections.map(item=>[item.id,item.name+' · '+item.url]),[manualConnection,'手动填写地址与密钥']])+UI.field({id:'tpm_model',label:'模型',required:true,controlHtml:
        '<div id="tpmModelList">'+UI.select({id:'tpm_model',value:'',options:[['','请先选择连接']],required:true})+'</div>'+
        '<div id="tpmModelManual" hidden>'+UI.input({id:'tpm_model_override',value:c.model||'',placeholder:'填写准确模型名称',required:true})+'</div>'+
        '<div id="tpmModelMode" class="model-fallback">'+UI.textButton('手动指定模型',{action:'setTpmModelMode',args:['manual']})+'</div>'}))+
      '<div id="tpmManualConnection"'+(connection===manualConnection?'':' hidden')+'>'+
      field('url','Base URL（接口地址）',c.url||c.base_url||'',{type:'url',required:true,placeholder:'https://example.com/v1'})+
      UI.field({id:'tpm_api_key',label:'API Key（接口密钥）',controlHtml:UI.passwordInput({id:'tpm_api_key',value:'',action:'togglePasswordVisibility',args:['tpm_api_key'],visibilityLabel:'API Key（接口密钥）',placeholder:'填写接口密钥'})})+'</div>'+
      '<div id="tpmModelStatus" role="status" aria-live="polite"></div>'+UI.textButton('获取模型列表',{action:'loadTpmModels'})+
      UI.formSectionTitle(2,'压力与预算')+
      UI.inlineFields(selectField('target_preset','目标档位',targetPresets.includes(String(c.target_tpm))?String(c.target_tpm):'custom',[['50000000','5,000 万 TPM（每分钟词元）'],['100000000','1 亿 TPM（每分钟词元）'],['150000000','1.5 亿 TPM（每分钟词元）'],['200000000','2 亿 TPM（每分钟词元）'],['custom','自定义目标']])+selectField('cache_mode','缓存模式',c.cache_mode,[['prefer_cache','缓存优先摸底'],['avoid_cache','尽量不命中缓存']]))+
      '<div id="tpmCustomTarget"'+(targetPresets.includes(String(c.target_tpm))?' hidden':'')+'>'+numberField('target_tpm','自定义 TPM（每分钟词元）',c.target_tpm,{min:1,max:1000000000,step:1})+'</div>'+
      UI.inlineFields(numberField('target_duration_seconds','目标档时长（秒）',c.target_duration_seconds,{min:0.05,max:3600})+numberField('budget_usd','本轮预算（美元）',c.budget_usd,{min:0.001,max:100000,hint:'包含校准、预热、发压和收尾。'}))+
      UI.formSectionTitle(3,'报价（美元 / 百万词元）')+
      UI.inlineFields(numberField('price_input','普通输入价格',prices.input)+numberField('price_output','输出价格',prices.output))+
      UI.inlineFields(numberField('price_cache_read','缓存读取价格',prices.cache_read)+UI.field({id:'tpm_cache_write_price_mode',label:'缓存写入价格',controlHtml:UI.select({id:'tpm_cache_write_price_mode',value:c.cache_write_price_mode,options:[['unconfirmed','待确认（阻止收费校准）'],['input','按普通输入价计算'],['explicit','填写独立写入价格']]})+UI.input({id:'tpm_price_cache_write',type:'number',min:0,step:'any',value:prices.cache_write??'',placeholder:'填写独立写入价格',describedBy:'tpmWritePriceHint'}),hint:'填 0 表示明确确认免费。',hintId:'tpmWritePriceHint'}))+
      '<details class="gateway-single-advanced" id="tpmAdvanced"><summary>高级设置</summary>'+
      UI.formSectionTitle(4,'请求结构与协议')+
      UI.inlineFields(numberField('input_tokens','目标完整输入 Token（词元）',c.input_tokens,{min:64,max:1000000,step:1})+numberField('expected_output_tokens','期望输出 Token（词元）',c.expected_output_tokens,{min:1,max:16384,step:1}))+
      UI.inlineFields(numberField('max_output_tokens','输出硬上限 Token（词元）',c.max_output_tokens,{min:1,max:32768,step:1})+numberField('timeout_seconds','单请求总超时（秒）',c.timeout_seconds,{min:0.05,max:600}))+
      selectField('output_limit_parameter','输出上限参数',c.output_limit_parameter,[['max_tokens','max_tokens（最大词元）'],['max_completion_tokens','max_completion_tokens（最大生成词元）']],'校准验证兼容性；不支持则停止，不静默取消上限或隐式重发。')+
      selectField('usage_semantics','用量协议语义',c.usage_semantics,[['openai','OpenAI Chat Completions（聊天补全）兼容：输入包含缓存'],['independent','独立字段：输入不包含缓存']],'独立语义必须由目标接口文档或已验证样本确认，不能按字段同名推断。')+
      '<div id="tpmIndependentUsage"'+(c.usage_semantics==='independent'?'':' hidden')+'>'+UI.switchRow({id:'tpm_usage_semantics_confirmed',title:'已核实独立用量语义',description:'确认 input_tokens（输入词元）不含独立缓存读取及写入字段。',checked:c.usage_semantics_confirmed})+'</div>'+
      UI.inlineFields(numberField('context_window_tokens','模型上下文上限 Token（词元）',c.context_window_tokens,{min:256,max:2000000,step:1})+numberField('protocol_overhead_tokens','协议开销预留 Token（词元）',c.protocol_overhead_tokens,{max:4096,step:1}))+
      UI.formSectionTitle(5,'校准、缓存与本机容量')+
      UI.inlineFields(numberField('calibration_requests','校准请求数',c.calibration_requests,{min:2,max:100,step:1})+numberField('ramp_duration_seconds','前三档各发压时长（秒）',c.ramp_duration_seconds,{min:0.05,max:600}))+
      '<div id="tpmCacheSettings"'+(c.cache_mode==='avoid_cache'?' hidden':'')+'>'+UI.inlineFields(numberField('template_groups','模板组数',c.template_groups,{min:1,max:128,step:1})+numberField('warmup_requests','每组预热次数',c.warmup_requests,{max:20,step:1}))+
      UI.inlineFields(numberField('expected_cache_hit_rate','预期缓存读取比例（0–1）',c.expected_cache_hit_rate,{max:1})+numberField('cache_hit_stop_threshold','缓存比例保护阈值（0–1）',c.cache_hit_stop_threshold,{max:1}))+'</div>'+
      '<div id="tpmCapacityDetails"><div id="tpmExecutionPlan" aria-live="polite"></div>'+selectField('execution_mode','并发与连接池规划',c.execution_mode||'auto',[['auto','自动规划（校准后更新）'],['manual','手动覆盖']],'并发 = 目标请求速率 × 耗时 × 1.5。校准后使用实测均值与 P95（95 分位），资源上限为 8,192；不会提高预算。')+
      '<div id="tpmPlanningLatency">'+numberField('planning_latency_seconds','校准前耗时假设（秒）',c.planning_latency_seconds??30,{min:0.001,max:600,hint:'仅用于校准前预估并发；正式计划使用校准实测 P95（95 分位）耗时。'})+'</div>'+
      '<div id="tpmManualCapacity">'+UI.inlineFields(numberField('concurrency','手动在途并发上限',c.concurrency,{min:1,max:8192,step:1})+numberField('pool_connections','手动连接池容量',c.pool_connections,{min:1,max:8192,step:1}))+'</div></div>'+
      field('seed','合成语料种子',c.seed,{required:true})+
      UI.hint('业务失败自动重试关闭；高压力任务独占资源。并发不足或调度落后会记录未发出时隙，不补发追赶。')+'</details><div id="tpmPreview" aria-live="polite"></div>';
  }
  async function openCreate(seed){
    if(s.busy)return;if(!s.initialized)init();const epoch=s.epoch,sequence=++s.sequence;
    window.clearTransientFeedback?.();
    try{
      let connectionError='';
      const connectionsReady=window.ConnectionUI?.refresh?.().catch(error=>{if(error.stale)throw error;connectionError=error.message||'读取失败';return []});
      const results=await Promise.all([request('/tpm/defaults'),connectionsReady]);if(epoch!==s.epoch||sequence!==s.sequence)return;
      const defaults=results[0].config||results[0],c={...defaults,...(seed||{}),prices:{...defaults.prices,...seed?.prices},execution_mode:seed?.execution_mode||(seed?'manual':defaults.execution_mode||'auto')};delete c.api_key;
      const connections=window.ConnectionUI?.list()||[],connection=initialConnection(c,connections),saved=connections.find(item=>item.id===connection);
      c.connection_id=saved?.id||'';if(saved)c.url=saved.url;else if(!connection){c.url='';c.model=''}
      s.draft=c;s.formMemory={connectionMode:connection,manualConnection:connection===manualConnection?{url:c.url||'',api_key:''}:null,targetPreset:targetPresets.includes(String(c.target_tpm))?String(c.target_tpm):'custom',customTarget:String(c.target_tpm),executionMode:c.execution_mode,manualCapacity:null};s.preview=null;s.previewKey='';s.requestId=window.crypto?.randomUUID?.()||`tpm-${Date.now()}-${Math.random().toString(36).slice(2)}`;s.drawer=true;s.opener=document.activeElement;
      $('tpmCreateBody').innerHTML=formHtml(c);$('tpmDrawer').classList.add('mobile-open');$('tpmDrawer').inert=false;$('tpmDrawer').setAttribute('aria-hidden','false');$('tpmBackdrop').hidden=false;document.querySelector('.app').inert=true;document.body.classList.add('drawer-open');formStatus(connectionError?'常用连接暂时无法更新，可手动填写地址与密钥：'+connectionError:'',Boolean(connectionError));setSubmit(false);focusDrawerStart($('tpm_connection_id'),sequence);
      resetModels();syncFormControls();queuePlanning();if(c.connection_id)loadModels(c.model||'');
    }catch(error){if(!error.stale)toast(error.message)}
  }
  function focusDrawerStart(node,sequence){
    const attempt=(remaining=20)=>{
      if(!s.drawer||sequence!==s.sequence||!node?.isConnected||$('tpmDrawer').contains(document.activeElement))return;
      if(typeof getComputedStyle!=='function'||getComputedStyle(node).visibility==='visible')node.focus({preventScroll:true});
      if(document.activeElement!==node&&remaining&&typeof requestAnimationFrame==='function')requestAnimationFrame(()=>attempt(remaining-1));
    };
    attempt();
  }
  function closeCreate(){
    if(!s.initialized)return;s.sequence++;s.drawer=false;s.preview=null;s.previewKey='';s.draft=null;s.formMemory=null;s.modelSequence++;s.planningSequence++;clearTimeout(s.planningTimer);s.models=[];s.modelConnection='';
    if($('tpm_api_key'))$('tpm_api_key').value='';$('tpmDrawer').classList.remove('mobile-open');$('tpmDrawer').inert=true;$('tpmDrawer').setAttribute('aria-hidden','true');$('tpmBackdrop').hidden=true;$('tpmCreateBody').innerHTML='';
    document.querySelector('.app').inert=Boolean($('authGate')&&!$('authGate').hidden);document.body.classList.remove('drawer-open');if(s.opener?.isConnected&&signedIn())s.opener.focus({preventScroll:true});s.opener=null;
  }
  function formStatus(text,error=false){if($('tpmFormStatus')){$('tpmFormStatus').textContent=text;$('tpmFormStatus').classList.toggle('error',error)}}
  function showFormError(error){
    formStatus(error.message,true);const node=error.fieldId&&$(error.fieldId);if(!node)return;
    let ancestor=node.closest?.('details');while(ancestor){ancestor.open=true;ancestor=ancestor.parentElement?.closest?.('details')}
    node.setAttribute('aria-invalid','true');node.scrollIntoView?.({block:'center',behavior:'auto'});node.focus({preventScroll:true});
  }
  const invalidField=(key,message)=>Object.assign(new Error(message),{fieldId:'tpm_'+key});
  function setSubmit(confirmed){const button=$('tpmSubmit'),pricePending=confirmed&&$('tpm_cache_write_price_mode')?.value==='unconfirmed';button.disabled=s.busy||pricePending;button.dataset.clickAction=confirmed?'calibrateTpm':'previewTpm';button.textContent=s.busy?'正在处理…':pricePending?'请先确认缓存写入价格':confirmed?'确认费用并开始校准':'查看方案与费用（不发请求）'}
  const stringKeys=['url','model','connection_id','cache_mode','cache_write_price_mode','output_limit_parameter','usage_semantics','seed','execution_mode'];
  const numericKeys=['target_tpm','input_tokens','expected_output_tokens','max_output_tokens','timeout_seconds','calibration_requests','template_groups','warmup_requests','concurrency','pool_connections','target_duration_seconds','ramp_duration_seconds','budget_usd','context_window_tokens','protocol_overhead_tokens','expected_cache_hit_rate','cache_hit_stop_threshold','planning_latency_seconds'];
  const numberLabels={target_tpm:'自定义目标',input_tokens:'目标完整输入',expected_output_tokens:'期望输出',max_output_tokens:'输出硬上限',timeout_seconds:'单请求总超时',calibration_requests:'校准请求数',template_groups:'模板组数',warmup_requests:'每组预热次数',concurrency:'手动在途并发上限',pool_connections:'手动连接池容量',target_duration_seconds:'目标档时长',ramp_duration_seconds:'前三档发压时长',budget_usd:'本轮预算',context_window_tokens:'模型上下文上限',protocol_overhead_tokens:'协议开销预留',expected_cache_hit_rate:'预期缓存读取比例',cache_hit_stop_threshold:'缓存比例保护阈值',planning_latency_seconds:'校准前耗时假设'};
  const cacheKeys=['template_groups','warmup_requests','expected_cache_hit_rate','cache_hit_stop_threshold'];
  function readForm(secret=false){
    const c={...s.draft};for(const key of stringKeys)c[key]=String($('tpm_'+key)?.value??c[key]??'').trim();for(const key of numericKeys){if(c.execution_mode==='auto'&&['concurrency','pool_connections'].includes(key)||c.execution_mode==='manual'&&key==='planning_latency_seconds'||c.cache_mode==='avoid_cache'&&cacheKeys.includes(key)){delete c[key];continue}const node=$('tpm_'+key),value=node?.value??c[key];if(value===''||!Number.isFinite(Number(value)))throw invalidField(key,'请填写有效数字：'+numberLabels[key]);c[key]=Number(value);if(node?.validity?.rangeUnderflow||node?.validity?.rangeOverflow||node?.validity?.stepMismatch)throw invalidField(key,'请检查'+numberLabels[key]+'：'+(node.step==='1'?'须为整数，':'')+'范围 '+node.min+' 至 '+(node.max||'不限'))}
    if(c.connection_id===manualConnection)c.connection_id='';
    if(s.manualModel)c.model=$('tpm_model_override')?.value.trim()||'';
    if(c.expected_output_tokens>c.max_output_tokens)throw invalidField('expected_output_tokens','期望输出不能大于输出硬上限，请调整请求结构。');
    if(c.input_tokens+c.max_output_tokens+c.protocol_overhead_tokens>c.context_window_tokens)throw invalidField('context_window_tokens','模型上下文不足以容纳完整输入、输出硬上限与协议开销，请核对模型容量。');
    if(c.execution_mode==='manual'&&c.pool_connections<c.concurrency)throw invalidField('pool_connections','连接池容量不能小于并发上限，请同时调整。');
    c.prices={};for(const key of ['input','output','cache_read','cache_write']){if(key==='cache_write'&&c.cache_write_price_mode!=='explicit'){c.prices[key]=null;continue}const value=$('tpm_price_'+key)?.value;c.prices[key]=value===''?null:Number(value);if(c.prices[key]==null||!Number.isFinite(c.prices[key])||c.prices[key]<0)throw invalidField('price_'+key,({input:'普通输入',output:'输出',cache_read:'缓存读取',cache_write:'独立缓存写入'})[key]+'价格必须填写非负数字；明确免费请填 0')}
    if(c.name==='2 亿 TPM（完整词元/分钟）摸底'&&c.target_tpm!==200000000)c.name=n(c.target_tpm/1000000)+' 百万 TPM（完整词元/分钟）摸底';
    c.usage_semantics_confirmed=c.usage_semantics==='independent'&&Boolean($('tpm_usage_semantics_confirmed')?.checked);delete c.api_key;delete c.base_url;if(secret)c.api_key=$('tpm_api_key')?.value.trim()||'';return c;
  }
  function syncFormControls(){
    const connection=$('tpm_connection_id')?.value||'';
    if($('tpmManualConnection'))$('tpmManualConnection').hidden=connection!==manualConnection;
    if($('tpmCustomTarget'))$('tpmCustomTarget').hidden=$('tpm_target_preset')?.value!=='custom';
    const cached=$('tpm_cache_mode')?.value!=='avoid_cache';
    if($('tpmCacheSettings'))$('tpmCacheSettings').hidden=!cached;
    for(const key of cacheKeys)if($('tpm_'+key))$('tpm_'+key).disabled=!cached;
    const independent=$('tpm_usage_semantics')?.value==='independent';
    if($('tpmIndependentUsage'))$('tpmIndependentUsage').hidden=!independent;
    if($('tpm_usage_semantics_confirmed'))$('tpm_usage_semantics_confirmed').disabled=!independent;
    const manual=$('tpm_execution_mode')?.value==='manual';
    if($('tpmManualCapacity'))$('tpmManualCapacity').hidden=!manual;
    if($('tpmPlanningLatency'))$('tpmPlanningLatency').hidden=manual;
    for(const key of ['concurrency','pool_connections'])if($('tpm_'+key))$('tpm_'+key).disabled=!manual;
    if($('tpm_planning_latency_seconds'))$('tpm_planning_latency_seconds').disabled=manual;
    if($('tpm_model_override'))$('tpm_model_override').disabled=!s.manualModel;
    if($('tpm_model'))$('tpm_model').disabled=s.manualModel||s.modelState==='loading'||!s.models.length;
    if($('tpmModelList'))$('tpmModelList').hidden=s.manualModel;
    if($('tpmModelManual'))$('tpmModelManual').hidden=!s.manualModel;
    const modeButton=$('tpmModelMode')?.querySelector('button');
    if(modeButton){modeButton.textContent=s.manualModel?'返回模型列表':'手动指定模型';modeButton.dataset.arg0=s.manualModel?'list':'manual';modeButton.setAttribute('aria-controls','tpmModelManual');modeButton.setAttribute('aria-expanded',String(s.manualModel))}
    const modelLabel=$('tpm_model')?.closest?.('.field')?.querySelector('label');
    if(modelLabel){modelLabel.htmlFor=s.manualModel?'tpm_model_override':'tpm_model';modelLabel.firstChild.textContent=s.manualModel?'模型（手动填写）':'模型'}
    const write=$('tpm_price_cache_write'),writeMode=$('tpm_cache_write_price_mode')?.value;
    if(write){write.hidden=writeMode!=='explicit';write.disabled=writeMode!=='explicit';write.setAttribute('aria-label','独立缓存写入价格（美元 / 百万词元）')}
    const modelsButton=$('tpmCreateBody')?.querySelector('[data-click-action="loadTpmModels"]');
    if(modelsButton){modelsButton.hidden=s.manualModel||!connection||s.modelState==='loading';modelsButton.disabled=s.modelState==='loading'||(connection===manualConnection&&(!$('tpm_url')?.value.trim()||!$('tpm_api_key')?.value.trim()))}
  }
  function setModelMode(mode){
    if(!s.drawer)return;
    s.manualModel=mode==='manual';
    if(s.manualModel&&!$('tpm_model_override').value)$('tpm_model_override').value=$('tpm_model').value||'';
    modelFeedback();queuePlanning();s.preview=null;s.previewKey='';$('tpmPreview').innerHTML='';setSubmit(false);
    formStatus(s.manualModel?'已切换为手动模型；请核对准确名称后重新预览。':'已返回模型列表；请选择模型后重新预览。');
    const target=s.manualModel?$('tpm_model_override'):$('tpm_model').disabled?$('tpmModelsButton'):$('tpm_model');
    target?.focus({preventScroll:true});
  }
  function modelConnection(){return JSON.stringify([$('tpm_connection_id')?.value||'',$('tpm_url')?.value.trim()||'',$('tpm_api_key')?.value.trim()||''])}
  function modelFeedback(){
    if($('tpmModelStatus'))$('tpmModelStatus').innerHTML=!s.manualModel&&s.modelMessage?(s.modelState==='error'||s.modelState==='empty'?UI.notice(s.modelMessage,{tone:'warning'}):'<p class="field-hint">'+UI.esc(s.modelMessage)+'</p>'):'';
    const button=$('tpmCreateBody')?.querySelector('[data-click-action="loadTpmModels"]');
    if(button){button.id='tpmModelsButton';button.disabled=s.modelState==='loading';button.textContent=s.modelState==='loading'?'正在获取模型…':s.modelState==='error'||s.modelState==='empty'?'重新获取模型':s.models.length?'刷新模型列表':'获取模型列表';button.setAttribute('aria-busy',String(s.modelState==='loading'))}
    syncFormControls();
  }
  function resetModels(){
    s.modelSequence++;s.models=[];s.modelState='idle';s.modelMessage='';s.modelConnection='';
    if($('tpm_model')){$('tpm_model').innerHTML='<option value="">请先获取可用模型</option>';$('tpm_model').value=''}
    s.manualModel=false;if($('tpm_model_override'))$('tpm_model_override').value='';modelFeedback();
  }
  async function loadModels(preferred=''){
    if(!s.drawer)return;
    const selection=$('tpm_connection_id')?.value||'',connection_id=selection===manualConnection?'':selection,url=$('tpm_url')?.value.trim()||'',api_key=$('tpm_api_key')?.value.trim()||'';
    if(!selection){s.modelState='idle';s.modelMessage='';modelFeedback();return}
    if(!url||(!connection_id&&!api_key)){s.modelState='error';s.modelMessage='请填写接口地址和接口密钥，再获取模型。';modelFeedback();return}
    const sequence=++s.modelSequence,drawerSequence=s.sequence,signature=modelConnection(),current=preferred||$('tpm_model')?.value||'';
    s.models=[];s.modelState='loading';s.modelConnection=signature;s.modelMessage='正在获取模型列表…';
    $('tpm_model').innerHTML='<option value="">正在获取可用模型…</option>';$('tpm_model').value='';modelFeedback();
    // Any refreshed list invalidates a preview: a previously selected model may disappear.
    s.preview=null;s.previewKey='';if($('tpmPreview'))$('tpmPreview').innerHTML='';setSubmit(false);
    const valid=()=>s.drawer&&sequence===s.modelSequence&&drawerSequence===s.sequence&&signature===modelConnection();
    try{
      const result=await request('/models',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({url,api_key,...(connection_id?{connection_id}:{})}),timeout:35000});
      if(!valid())return;if(result.ok===false||!Array.isArray(result.models))throw new Error(result.error||'模型列表格式无效');
      s.models=[...new Set(result.models.filter(model=>typeof model==='string'&&model.trim()).map(model=>model.trim()))].sort();
      s.modelState=s.models.length?'ready':'empty';s.modelMessage=s.models.length?(current&&!s.models.includes(current)?`原模型“${current}”已不在列表中，请重新选择。`:''):'未返回模型。请重试，或手动指定模型。';
      $('tpm_model').innerHTML='<option value="">'+(s.models.length?'请选择模型':'未获取到可用模型')+'</option>'+s.models.map(model=>'<option value="'+UI.esc(model)+'">'+UI.esc(model)+'</option>').join('');
      $('tpm_model').value=s.models.includes(current)?current:'';
    }catch(error){if(!valid()||error.stale)return;s.modelState='error';s.modelMessage='获取模型失败：'+error.message+'。可重试，或点击“手动指定模型”。';$('tpm_model').innerHTML='<option value="">获取失败，请重新获取</option>';$('tpm_model').value=''}
    finally{if(valid()){s.modelConnection='';modelFeedback();queuePlanning()}}
  }
  function queuePlanning(){
    clearTimeout(s.planningTimer);s.planningSequence++;
    if(!s.drawer)return;
    if($('tpmExecutionPlan'))$('tpmExecutionPlan').innerHTML='<p class="field-hint">正在计算执行参数…</p>';
    s.planningTimer=setTimeout(refreshPlanning,400);
  }
  async function refreshPlanning(){
    const sequence=++s.planningSequence,drawerSequence=s.sequence;if(!s.drawer)return;
    try{
      const config=readForm(),result=await post('/tpm/preview',{config});
      if(sequence!==s.planningSequence||drawerSequence!==s.sequence||!s.drawer)return;
      const plan=result.preview||result;if($('tpmExecutionPlan'))$('tpmExecutionPlan').innerHTML=executionHtml(plan,config);
      if(config.execution_mode==='auto')for(const key of ['concurrency','pool_connections'])if($('tpm_'+key)&&plan.execution_plan?.[key]!=null)$('tpm_'+key).value=String(plan.execution_plan[key]);
    }catch(error){if(sequence===s.planningSequence&&drawerSequence===s.sequence&&s.drawer&&!error.stale&&$('tpmExecutionPlan'))$('tpmExecutionPlan').innerHTML=UI.notice('执行参数暂无法规划：'+error.message,{tone:'warning'})}
  }
  function onInput(event){
    if(!s.drawer||!event.target?.id?.startsWith('tpm_'))return;const id=event.target.id;event.target.removeAttribute?.('aria-invalid');
    if(event.type==='input'&&(event.target.tagName==='SELECT'||event.target.type==='checkbox'))return;
    // Text and numeric input already updated the draft; their deferred blur change can follow a preset switch.
    if(event.type==='change'&&event.target.tagName!=='SELECT'&&event.target.type!=='checkbox')return;
    const memory=s.formMemory||(s.formMemory={});
    if(id==='tpm_connection_id'){
      if(memory.connectionMode===manualConnection)memory.manualConnection={url:$('tpm_url').value,api_key:$('tpm_api_key').value};
      const connection=window.ConnectionUI?.list().find(item=>item.id===event.target.value);
      if(connection){$('tpm_url').value=connection.url;$('tpm_api_key').value=''}
      else if(event.target.value===manualConnection&&memory.manualConnection){$('tpm_url').value=memory.manualConnection.url;$('tpm_api_key').value=memory.manualConnection.api_key}
      else if(event.target.value!==manualConnection){$('tpm_url').value='';$('tpm_api_key').value=''}
      memory.connectionMode=event.target.value;resetModels();if(connection)loadModels();
    }
    if(id==='tpm_url'||id==='tpm_api_key'){$('tpm_connection_id').value=manualConnection;resetModels()}
    if(id==='tpm_url'||id==='tpm_api_key')memory.connectionMode=manualConnection;
    if(id==='tpm_model'&&s.modelState==='ready'&&s.models.includes(event.target.value)){s.modelMessage='';modelFeedback()}
    if(id==='tpm_target_preset'){
      if(memory.targetPreset==='custom')memory.customTarget=$('tpm_target_tpm').value;
      if(targetPresets.includes(event.target.value))$('tpm_target_tpm').value=event.target.value;
      else if(event.target.value==='custom'&&memory.customTarget!==undefined)$('tpm_target_tpm').value=memory.customTarget;
      memory.targetPreset=event.target.value;
    }
    if(id==='tpm_target_tpm'&&$('tpm_target_preset')){$('tpm_target_preset').value='custom';memory.targetPreset='custom';memory.customTarget=event.target.value}
    if(id==='tpm_execution_mode'){
      if(memory.executionMode==='manual')memory.manualCapacity={concurrency:$('tpm_concurrency').value,pool_connections:$('tpm_pool_connections').value};
      if(event.target.value==='manual'&&memory.manualCapacity)for(const key of ['concurrency','pool_connections'])$('tpm_'+key).value=memory.manualCapacity[key];
      memory.executionMode=event.target.value;
    }
    syncFormControls();queuePlanning();
    s.preview=null;s.previewKey='';if($('tpmPreview'))$('tpmPreview').innerHTML='';setSubmit(false);formStatus('参数已更改，请重新预览费用；未发送压测请求。');
  }
  async function preview(){
    if(s.busy||!s.drawer)return;const sequence=s.sequence;
    try{
      const config=readForm();s.busy=true;setSubmit(false);formStatus('正在生成合成探测体并离线预检，不访问上游…');
      const result=await post('/tpm/preview',{config});if(sequence!==s.sequence||!s.drawer)return;
      if(JSON.stringify(config)!==JSON.stringify(readForm())){formStatus('参数已变化，已丢弃旧方案；请重新预览费用。');return}
      s.preview=result.preview||result;s.previewKey=JSON.stringify(config);$('tpmPreview').innerHTML=UI.formSectionTitle(6,'确认校准费用')+UI.stack(UI.notice('下一步仅发起收费校准及预热；正式发压需再次确认。',{tone:'warning'})+planSummaryHtml(s.preview,config)+fold('tpmPreviewDetails','查看完整阶梯、判断规则与资源预检',planHtml(s.preview,config)));
      $('tpmPreview').scrollIntoView?.({block:'start',behavior:'auto'});
      formStatus(config.cache_write_price_mode==='unconfirmed'?'缓存写入价格待确认。请选择按普通输入价或填写独立写入价格后重新预览。':'预览完成；本次预览未访问上游。');
    }catch(error){if(sequence===s.sequence&&!error.stale)showFormError(error)}finally{s.busy=false;if(s.drawer)setSubmit(Boolean(s.preview))}
  }
  async function calibrate(){
    if(s.busy||!s.drawer)return;let config;try{config=readForm(true)}catch(error){showFormError(error);return}
    const publicConfig={...config};delete publicConfig.api_key;
    if(!s.preview||JSON.stringify(publicConfig)!==s.previewKey){formStatus('参数已变化，请重新预览费用。',true);return}
    if(config.cache_write_price_mode==='unconfirmed'){formStatus('请先明确缓存写入价格，不能假设免费。',true);return}
    if(!config.url){showFormError(invalidField('connection_id','请选择常用连接，或手动填写接口地址和密钥。'));return}
    if(!config.model){showFormError(invalidField(s.manualModel?'model_override':'model','请选择一个模型，或点击“手动指定模型”填写准确名称。'));return}
    if(config.usage_semantics==='independent'&&!config.usage_semantics_confirmed){showFormError(invalidField('usage_semantics_confirmed','请先核实并确认独立用量语义，再开始收费校准。'));return}
    if(!confirm(`即将向 ${config.url} 发起 ${config.calibration_requests} 次收费校准${config.cache_mode==='prefer_cache'?'及配置的预热请求':'；本场景不预热缓存'}。\n校准预估 ${money(s.preview.calibration_cost_usd)}，预热 ${money(s.preview.warmup_cost_usd)}；本轮总预算 ${money(config.budget_usd)}。\n校准后仍需确认正式计划。确认开始校准吗？`))return;
    s.busy=true;setSubmit(true);formStatus('正在创建并校准；请勿重复提交。');
    try{const result=await post('/tpm/runs',{config,calibration_confirmed:true,request_id:s.requestId});const run=result.run||result;s.selected=run.id||result.run_id;s.run=run;s.tab='report';try{localStorage.setItem('tpm_selected_run',s.selected)}catch{}closeCreate();s.loaded=false;s.filter='all';await refresh()}
    catch(error){if(!error.stale&&s.drawer)formStatus(error.message+'；如网络中断，原请求标识会复用，避免重复任务。',true)}finally{s.busy=false;if(s.drawer)setSubmit(Boolean(s.preview));render()}
  }
  async function adjustPlan(id){
    const run=s.run;if(s.busy||!run||run.id!==id||run.phase!=='awaiting_run'||!run.plan?.duration_change_requires_confirmation)return;
    s.busy=true;render();
    try{const result=await post(path(id)+'/plan',{target_duration_seconds:run.plan.suggested_target_duration_seconds});s.run=result.run||result;toast('已应用建议时长并重新估价；仍需确认正式计划才会发压。');await refresh()}
    catch(error){if(!error.stale)toast(error.message)}finally{s.busy=false;render()}
  }
  async function start(id){
    if(s.busy)return;const run=s.run;if(!run||run.id!==id||run.phase!=='awaiting_run')return;
    const planId=run.plan_id||run.plan?.plan_id;if(!planId){toast('正式计划缺少冻结标识，请刷新后重试。');return}
    if(!confirm(`确认使用校准后的冻结计划正式发压？\n场景：${modeLabel(run.config.cache_mode)}\n目标档 ${n(run.plan?.target_rps)} 请求/秒；预计费用 ${money(run.plan?.estimated_cost_usd)}；本轮预算 ${money(run.config.budget_usd)}。\n高压力任务会独占执行资源；费用不是商家最终账单。`))return;
    s.busy=true;render();try{const result=await post(path(id)+'/start',{plan_id:planId,confirmed:true});s.run=result.run||result;await refresh()}catch(error){if(!error.stale)toast(error.message)}finally{s.busy=false;render()}
  }
  async function stop(id){if(s.busy)return;s.busy=true;render();try{const result=await post(path(id)+'/stop');s.run=result.run||result;await refresh()}catch(error){if(!error.stale)toast(error.message)}finally{s.busy=false;render()}}
  async function repeat(id){if(s.busy)return;try{const result=await request(path(id));const run=result.run||result;await openCreate(run.config)}catch(error){if(!error.stale)toast(error.message)}}
  function onKeydown(event){
    if(!s.drawer)return;const drawer=$('tpmDrawer');if(event.key==='Escape'){event.preventDefault();event.stopImmediatePropagation();closeCreate();return}
    if(event.key!=='Tab')return;const nodes=Array.from(drawer.querySelectorAll('button:not([disabled]),input:not([disabled]),select:not([disabled]),summary,[tabindex="0"]')).filter(node=>node.getClientRects().length);
    const first=nodes[0],last=nodes[nodes.length-1];if(!first)return;
    if(event.shiftKey&&(document.activeElement===first||!drawer.contains(document.activeElement))){event.preventDefault();last.focus()}else if(!event.shiftKey&&(document.activeElement===last||!drawer.contains(document.activeElement))){event.preventDefault();first.focus()}
  }
  function reset(){s.epoch++;s.sequence++;clearTimeout(s.timer);s.timer=null;closeCreate();Object.assign(s,{items:[],run:null,selected:null,loaded:false,polling:false,loading:false,busy:false,error:'',expanded:{}})}
  window.TpmUI={init,updateChrome,hide,render,refresh,select,setFilter,setTab,openCreate,closeCreate,preview,calibrate,loadModels,setModelMode,adjustPlan,start,stop,repeat,reset,drawChart,isOpen:()=>s.drawer};
})();
