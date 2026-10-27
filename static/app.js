const responseCache=new Map();
let lastRefreshSignature="",lastPollAt=0,hasActiveRuns=false;
const uiIconPaths={"gateway": "<path d=\"M4 19V5M4 19h16M8 14l4-5 4 3 4-7\"/>", "quality": "<path d=\"M12 3l8 3v6c0 5-8 9-8 9s-8-4-8-9V6l8-3Z\"/><path d=\"m8 12 3 3 5-6\"/>", "routing": "<path d=\"M4 7h6l4 10h6\"/><path d=\"M4 17h4l3-6\"/><circle cx=\"18\" cy=\"17\" r=\"2\"/>", "plus": "<path d=\"M12 5v14M5 12h14\"/>", "close": "<path d=\"m6 6 12 12M6 18 18 6\"/>"};
function uiIcon(name){return `<svg class="ui-icon" viewBox="0 0 24 24" aria-hidden="true">${uiIconPaths[name]||uiIconPaths.plus}</svg>`}

const $=id=>document.getElementById(id);let selected=null,detailTask=null,mode='smoke',tasks=[],comparisons=[],refreshing=false,refreshSeq=0;let modelFetchState='idle',modelSource='none',modelList=[],modelRequestSeq=0;const detailState={id:null,scrollTop:0};
const statusNames={draft:'草稿',running:'运行中',stopping:'正在停止',waiting:'排队中',finished:'已完成',finished_with_errors:'完成但有错误',stopped:'已停止',error:'失败',expired:'结果已过期',archived:'已归档'};
const modeTemplates={smoke:`<div class="inline"><div class="field"><label for="rpm">计划调度速率（RPM）</label><input class="input" id="rpm" type="number" min="0.01" max="600000" step="any" required value="100"><div class="muted-help">调度目标；实际开始与完成另计。</div></div><div class="field"><label for="iterations">计划请求条数</label><input class="input" id="iterations" type="number" min="1" max="10000000" step="1" required value="50"></div></div>`,ramp:`<div class="field"><label for="ramps">各档计划调度速率（RPM）</label><input class="input" id="ramps" required value="100,200,400"><div class="muted-help">逗号分隔；每档按配置速率尝试调度，受并发与队列背压影响。</div></div><div class="field"><label for="duration">每档计划调度时长（秒）</label><input class="input" id="duration" type="number" min="1" max="86400" step="1" required value="60"></div>`,soak:`<div class="inline"><div class="field"><label for="rpm">计划调度速率（RPM）</label><input class="input" id="rpm" type="number" min="0.01" max="600000" step="any" required value="100"></div><div class="field"><label for="duration">计划调度时长（秒）</label><input class="input" id="duration" type="number" min="1" max="86400" step="1" required value="1800"></div></div>`};
let createKind='gateway-single', activeModule='gateway', activeView='single', selectedComparison=null, selectedQuality=null, selectedRouting=null, qualityTests=[];
const gatewayFormIds=['name','url','api_key','modelSelect','modelManualWrap','profile','concurrency','capture_raw'];
/* ---------- Named local connections: metadata only, secrets stay in the credential store. ---------- */
let savedConnectionProfiles=[],savedConnectionsLoaded=false,savedConnectionRefreshSeq=0;
// 仅在当前创建抽屉存活，避免切到常用连接时丢失尚未保存的手填连接。
let manualConnectionDrafts=new WeakMap();
function connectionBindingNode(context,target=''){
 if(context==='gateway')return $('gatewayConnectionPicker');
 if(context==='quality')return document.querySelector(`.quality-resource[data-quality-index="${Number(target)}"]`);
 if(context==='routing'&&['baseline','candidate'].includes(target))return document.querySelector(`[data-routing-side="${target}"]`);
 return null;
}
function connectionFieldNodes(context,target=''){
 const box=connectionBindingNode(context,target);
 return context==='gateway'?{box,url:$('url'),key:$('api_key'),name:$('name')}:
  context==='quality'?{box,url:box?.querySelector('.quality-url'),key:box?.querySelector('.quality-key'),name:box?.querySelector('.quality-name')}:
  {box,url:box?.querySelector('.routing-side-url'),key:box?.querySelector('.routing-side-key'),name:box?.querySelector('.routing-side-name')};
}
function boundConnectionId(context,target=''){
 return connectionBindingNode(context,target)?.dataset.connectionId||'';
}
function boundConnectionProfile(context,target=''){
 const {box,url}=connectionFieldNodes(context,target),id=boundConnectionId(context,target);
 return savedConnectionProfiles.find(profile=>profile.id===id&&profile.url===String(url?.value||'').trim()&&profile.url===box?.dataset.connectionUrl)||null;
}
function savedConnectionPickerHtml(context,target='',selectedId=''){
 const id=`saved-connection-${context}-${target||'main'}`,selected=selectedId||boundConnectionId(context,target);
 const options=[['','手动填写地址和密钥'],...savedConnectionProfiles.map(profile=>[profile.id,`${profile.name} · ${profile.url}${profile.api_key_saved?'':' · 密钥不可用'}${profile.id===selected&&connectionBindingNode(context,target)?.dataset.connectionInvalid==='true'?' · 连接已更新，请重新选择':''}`])];
 if(selected&&!options.some(option=>option[0]===selected))options.push([selected,savedConnectionsLoaded?'常用连接已删除，请重新选择':'已选择的常用连接（正在读取）']);
 const select=UI.select({id,options,value:selected}).replace('<select ',`<select data-change-action="selectSavedConnection" data-arg0="${UI.esc(context)}" data-arg1="${UI.esc(target)}" `);
 return UI.field({id,label:'常用连接',controlHtml:select+'<div class="suite-actions">'+UI.textButton('保存当前连接',{action:'saveCurrentConnection',args:[context,target]})+UI.textButton('管理常用连接',{action:'manageSavedConnections'})+'</div>'});
}
function connectionProfileRevision(profile){return profile?JSON.stringify([profile.updated_at??null,profile.url,Boolean(profile.api_key_saved),profile.name]):''}
function invalidateBoundConnectionModels(context,target,message='常用连接已更新，请重新获取模型列表。'){
 if(context==='gateway'){invalidateModelFetch();setSavedModelOption('');if($('model'))$('model').value='';setModelHint(message,'error')}
 else if(context==='quality')invalidateQualityModels(connectionBindingNode(context,target));
 else if(context==='routing'){hideRoutingKey(target);resetRoutingModels(message)}
}
function renderSavedConnectionPickers(){
 const entries=[['gateway',''],...Array.from(document.querySelectorAll('.quality-resource[data-quality-index]'),box=>['quality',box.dataset.qualityIndex]),...Array.from(document.querySelectorAll('[data-routing-side]'),box=>['routing',box.dataset.routingSide])];
 for(const [context,target] of entries){
  const {box,url}=connectionFieldNodes(context,target);if(!box)continue;
  const id=boundConnectionId(context,target),profile=savedConnectionProfiles.find(item=>item.id===id),revision=connectionProfileRevision(profile),updated=Boolean(box.dataset.connectionRevision)&&box.dataset.connectionRevision!==revision;
  if(id&&savedConnectionsLoaded&&(!profile||profile.url!==String(url?.value||'').trim())){
   const first=box.dataset.connectionInvalid!=='true';box.dataset.connectionInvalid='true';
   if(first||updated)invalidateBoundConnectionModels(context,target,'常用连接已删除或地址已更新，请重新选择连接并获取模型。');
  }else{delete box.dataset.connectionInvalid;if(id&&updated)invalidateBoundConnectionModels(context,target)}
  if(id&&savedConnectionsLoaded)box.dataset.connectionRevision=revision;
  const host=context==='gateway'?box:$(`connection-picker-${context}-${target}`);if(host)host.innerHTML=savedConnectionPickerHtml(context,target);
  if(boundConnectionId(context,target))updateBoundConnectionHint(context,target);
  syncConnectionDetails(context,target);
 }
}
function syncConnectionDetails(context,target=''){
 const details=$(`connection-details-${context}-${target||'main'}`),box=connectionBindingNode(context,target);if(!details||!box)return;
 const id=boundConnectionId(context,target),ready=Boolean(id)&&box.dataset.connectionInvalid!=='true'&&box.dataset.connectionApiKeySaved==='true',state=`${id}:${ready}`;
 if(details.dataset.connectionState!==state){details.open=!ready;details.dataset.connectionState=state}
 const summary=details.querySelector('summary');if(summary)summary.textContent=ready?'地址与密钥 · 已保存，可展开核对':'地址与密钥';
}
function updateBoundConnectionHint(context,target=''){
 const {box,url,key}=connectionFieldNodes(context,target),profile=boundConnectionProfile(context,target);if(!box||!boundConnectionId(context,target))return;
 const invalid=box.dataset.connectionInvalid==='true',saved=!invalid&&(profile?Boolean(profile.api_key_saved):box.dataset.connectionApiKeySaved==='true');box.dataset.connectionApiKeySaved=String(saved);
 if(context==='gateway'){window.savedApiKey=saved;window.savedApiKeyHint='';updateKeyInputHint()}
 else if(context==='quality'){const hint=box.querySelector('.quality-credential-hint');if(hint){hint.textContent=invalid?'常用连接已删除或地址已更新，请重新选择；当前绑定不会改用其他密钥。':saved?'已选择常用连接；密钥留空即可复用，修改地址或密钥将切换为手动填写。':'该常用连接的密钥不可用；请填写密钥后另存或更新。';hint.className=`field-hint quality-credential-hint ${saved?'ok':'error'}`}}
 else if(context==='routing')setRoutingCredentialState(target,url.value.trim(),saved,true,saved?'已选择常用连接；密钥留空即可复用，点击眼睛可核对。修改地址或密钥将切换为手动填写。':'该常用连接的密钥不可用；请填写密钥后另存或更新。');
 if(context==='routing'&&key)key.required=!saved;
 syncConnectionDetails(context,target);
}
function clearSavedConnectionBinding(context,target=''){
 const {box}=connectionFieldNodes(context,target);if(!box)return;
 delete box.dataset.connectionId;delete box.dataset.connectionUrl;delete box.dataset.connectionApiKeySaved;delete box.dataset.connectionInvalid;delete box.dataset.connectionRevision;
 const select=$(`saved-connection-${context}-${target||'main'}`);if(select)select.value='';
 syncConnectionDetails(context,target);
 if(context==='gateway'){credentialStatusSeq++;window.savedApiKey=false;window.savedApiKeyHint='';updateKeyInputHint()}
 else if(context==='quality')box.dataset.credentialSeq=String(++qualityCredentialSeq);
 else if(context==='routing'){box.dataset.credentialSeq=String(Number(box.dataset.credentialSeq||0)+1);setRoutingCredentialState(target,readRoutingSide(target).url,false)}
}
function selectSavedConnection(context,target='',selectNode){
 const {box,url,key}=connectionFieldNodes(context,target);if(!box||!url||!key)return;
 const id=selectNode?.value||'',profile=savedConnectionProfiles.find(item=>item.id===id);
 box.dataset.connectionUserEdited='true';
 if(!id){
  if(context==='routing')hideRoutingKey(target);
  const draft=manualConnectionDrafts.get(box);clearSavedConnectionBinding(context,target);
  if(draft){url.value=draft.url;key.value=draft.key;key.type='password';invalidateBoundConnectionModels(context,target,'已恢复手填连接，请重新获取该连接的模型列表。')}
  if(context==='gateway'){resetKeyButton(true);checkSavedCredential()}else if(context==='quality')checkQualityCredential(box);else if(context==='routing'){checkRoutingCredential(target);box.dataset.connectionEdited='true';clearTimeout(routingConnectionTimers[target]);routingConnectionTimers[target]=setTimeout(()=>saveRoutingSide(target),500)}
  schedulePreflight();return
 }
 if(!profile){toast('该常用连接已不存在，请刷新后重选。');renderSavedConnectionPickers();return}
 if(!boundConnectionId(context,target))manualConnectionDrafts.set(box,{url:url.value,key:key.dataset.routingRevealed==='true'?'':key.value});
 if(context==='routing')hideRoutingKey(target,true);
 box.dataset.connectionId=profile.id;box.dataset.connectionUrl=profile.url;box.dataset.connectionApiKeySaved=String(Boolean(profile.api_key_saved));box.dataset.connectionRevision=connectionProfileRevision(profile);delete box.dataset.connectionInvalid;
 url.value=profile.url;key.value='';key.type='password';
 if(context==='gateway'){credentialStatusSeq++;resetKeyButton(true);invalidateBoundConnectionModels(context,target,'连接已变化，请重新获取模型列表。')}
 else if(context==='quality'){box.dataset.credentialSeq=String(++qualityCredentialSeq);invalidateQualityModels(box)}
 else{box.dataset.credentialSeq=String(Number(box.dataset.credentialSeq||0)+1);box.dataset.connectionEdited='true';url.dataset.routingEdited='true';key.dataset.routingEdited='true';resetRoutingModels('连接信息已更新，请重新获取两侧模型列表。');clearTimeout(routingConnectionTimers[target]);routingConnectionTimers[target]=setTimeout(()=>saveRoutingSide(target),500)}
 updateBoundConnectionHint(context,target);schedulePreflight();
}
function invalidateQualityModels(box){
 if(!box)return;box.dataset.modelSeq=String(++qualityModelSeq);delete box.dataset.modelPending;delete box.dataset.modelLoaded;
 const select=box.querySelector('.quality-model'),button=box.querySelector('.quality-model-refresh'),hint=box.querySelector('.quality-model-hint'),wrap=box.querySelector('.quality-model-row');
 if(select){select.innerHTML='<option value="">连接已变化，请重新获取模型</option>';select.value='';select.disabled=true}
 const manual=$(`quality-model-manual-${box.dataset.qualityIndex}`);if(manual)manual.value='';
 if(button){button.disabled=false;button.textContent='获取模型';button.removeAttribute('aria-busy')}if(wrap)wrap.classList.remove('is-loading','is-done','is-error');if(hint){hint.textContent='连接已变化，原模型已清空；请重新获取模型列表或手动指定。';hint.className='field-hint quality-model-hint'}
}
function savedConnectionInputChanged(event){
 const input=event.target;let context='',target='';
 if(input.id==='url'||input.id==='api_key')context='gateway';
 else if(input.matches?.('.quality-url,.quality-key')){context='quality';target=input.closest('.quality-resource')?.dataset.qualityIndex}
 else if(input.matches?.('.routing-side-url,.routing-side-key')){context='routing';target=input.closest('[data-routing-side]')?.dataset.routingSide}
 if(!context)return;
 const {box}=connectionFieldNodes(context,target);if(box)box.dataset.connectionUserEdited='true';
 if(boundConnectionId(context,target)||box?.dataset.connectionId)clearSavedConnectionBinding(context,target);
 if(context==='quality')invalidateQualityModels(box);
}
function saveCurrentConnection(context,target=''){
 const {url,key,name}=connectionFieldNodes(context,target);if(!url?.value.trim()){toast('请先填写要保存的请求地址。');return}
 const bound=boundConnectionId(context,target);if(bound){window.ConnectionUI?.openEdit?.(bound);return}
 window.ConnectionUI?.openCreate({name:name?.value.trim()||'',url:url.value.trim(),api_key:key?.dataset.routingRevealed==='true'?'':key?.value.trim()||'',source_side:context==='routing'?target:''});
}
function manageSavedConnections(){window.ConnectionUI?.openManage?.()}
async function refreshSavedConnectionPickers(){
 const seq=++savedConnectionRefreshSeq;
 try{const result=await window.ConnectionUI?.refresh?.();if(seq!==savedConnectionRefreshSeq)return;savedConnectionProfiles=Array.isArray(result)?result:window.ConnectionUI?.list?.()||[];savedConnectionsLoaded=true;renderSavedConnectionPickers()}catch{if(seq===savedConnectionRefreshSeq)renderSavedConnectionPickers()}
}
document.addEventListener('input',savedConnectionInputChanged,true);
window.addEventListener('connections-changed',()=>{savedConnectionProfiles=window.ConnectionUI?.list?.()||[];savedConnectionsLoaded=true;renderSavedConnectionPickers()});
/* ---------- End named local connections ---------- */
function gatewayFormNodes(){const nodes=[];for(const id of gatewayFormIds){const el=$(id);if(!el)continue;const root=el.closest('.field')||el.closest('details')||el.closest('.tabs')||el.closest('#modeFields')||el;if(!nodes.includes(root))nodes.push(root)}const mode=$('modeFields');if(mode&&!nodes.includes(mode))nodes.push(mode);return nodes}
function isGatewayCreate(){return createKind.startsWith('gateway-')}function isQualityCreate(){return createKind.startsWith('quality-')}function isRoutingCreate(){return createKind==='routing-compare'}function isCompareCreate(){return createKind.endsWith('-compare')&&!isRoutingCreate()}
function setCreateKind(next,forceRender=false){
 const allowed=['gateway-single','gateway-compare','quality-single','quality-compare','routing-compare'];
 const normalized=allowed.includes(next)?next:'gateway-single',changed=createKind!==normalized;createKind=normalized;
 const quality=isQualityCreate(),routing=isRoutingCreate(),comparison=isCompareCreate(),single=isGatewayCreate()&&!comparison;
 document.querySelectorAll('.gateway-single-only,.gateway-single-name,.gateway-single-pressure,.single-only').forEach(node=>node.hidden=!single);
 document.querySelectorAll('.gateway-connection-field').forEach(node=>node.hidden=!isGatewayCreate());
 document.querySelectorAll('.gateway-single-advanced').forEach(node=>node.hidden=quality||routing);
 document.querySelectorAll('.routing-only').forEach(node=>node.hidden=!routing);
 $('comparisonFields').hidden=!(quality||(isGatewayCreate()&&comparison));
 $('comparisonNameField').hidden=!(isGatewayCreate()&&comparison);
 if(changed||forceRender){if(routing)renderRoutingFields(true);else if(quality)renderQualityFields(comparison);else if(comparison)renderComparisonFields()}
 const title=routing?'新建路由对照':quality?(comparison?'新建资源对照':'新建单资源评测'):(comparison?'新建压测对照':'新建压测');
 $('templatePicker').hidden=quality||routing||comparison;
 $('createTitle').textContent=title;
 $('createSubtitle').textContent=routing?'比较固定路由与智能路由的成功率、延迟和缓存表现。':quality?'使用同一题集评估模型能力，逐题查看得分与证据。':comparison?'共用一个网关，比较不同压力参数下的表现。':'配置网关连接和压力参数，开始后查看实时结果。';
 $('submit').textContent=routing?'开始路由对照':quality?(comparison?'开始资源对照':'开始质量评测'):(comparison?'开始压测对照':'创建并开始压测');
}
function renderComparisonFields(){if(!$('comparisonFields'))return;$('comparisonFields').innerHTML='<div class="comparison-form-scope">'+UI.formSectionTitle(3,'设置实验组')+'<div class="field"><div class="field-label-row"><label>实验组参数</label><button class="text-btn" type="button" data-click-action="addComparisonGroup">＋ 添加对比组</button></div><div class="comparison-fields" id="groupFields"></div><div class="field-hint">各组按顺序执行；建议每次只改变一个压力参数。</div></div><div class="adv-toggle comparison-capture"><div class="switch-copy"><div class="switch-title">记录全部实验组原始数据</div><div class="switch-txt">开启后，各组每条请求的输入与输出都会写入 JSONL（逐行 JSON）文件。</div></div><label class="switch"><input type="checkbox" id="comparisonCapture"><span class="slider"></span></label></div></div>';$('groupFields').innerHTML='';[['基准组','baseline',20],['对比组 1','comparison',20]].forEach((x,i)=>addComparisonGroup(x[0],x[1],x[2],i+1))}
function addComparisonGroup(name='对比组',role='comparison',concurrency=20,index=null){
 const list=$('groupFields');if(!list||list.children.length>=4)return;
 const n=index||Math.max(0,...Array.from(list.children,row=>Number(row.dataset.groupIndex)||0))+1;
 const row=document.createElement('div');row.className='group-row';row.dataset.groupIndex=n;row.dataset.autoName=name;
 const classes={name:'input group-name',role:'input group-role',concurrency:'input group-concurrency',rpm:'input group-rpm',iterations:'input group-iterations'};
 const field=(key,label,controlHtml)=>UI.field({id:`group-${n}-${key}`,label,controlHtml:controlHtml.replace('class="input"',`class="${classes[key]}"`)}).replace('class="field"',key==='name'?'class="field group-cell group-name-cell"':'class="field group-cell"');
 row.innerHTML=field('name','组名',UI.input({id:`group-${n}-name`,value:name}))+
  field('role','角色',UI.select({id:`group-${n}-role`,value:role,options:[['baseline','基准组'],['comparison','对比组']]}).replace('<select ','<select data-change-action="changeComparisonRole" '))+
  field('concurrency','并发上限',UI.input({id:`group-${n}-concurrency`,type:'number',min:1,max:2000,step:1,required:true,value:concurrency}))+
  field('rpm','RPM（每分钟请求数）',UI.input({id:`group-${n}-rpm`,type:'number',min:0.01,max:600000,step:'any',required:true,value:100}))+
  field('iterations','请求数',UI.input({id:`group-${n}-iterations`,type:'number',min:1,max:10000000,step:1,required:true,value:20}))+
  UI.button('',{action:'removeGroup',iconName:'close',title:'移除实验组'}).replace('class="btn"','class="icon-btn group-remove" aria-label="移除实验组"');
 list.appendChild(row);normalizeComparisonRoles();schedulePreflight()
}
function normalizeComparisonRoles(preferred){
 const rows=[...document.querySelectorAll('#groupFields .group-row')];
 if(preferred?.value==='baseline')rows.forEach(row=>{const role=row.querySelector('.group-role');if(role!==preferred)role.value='comparison'});
 let comparisonIndex=0;
 rows.forEach(row=>{const role=row.querySelector('.group-role'),input=row.querySelector('.group-name'),name=role.value==='baseline'?'基准组':`对比组 ${++comparisonIndex}`;if(row.dataset?.autoName!=null){if(input.value===row.dataset.autoName){input.value=name;row.dataset.autoName=name}else delete row.dataset.autoName}});
 rows.forEach(row=>{const button=row.querySelector('.group-remove');if(button){button.disabled=rows.length<=2;button.title=rows.length<=2?'对照实验至少保留 2 个组':'移除实验组'}});
 const add=document.querySelector('#comparisonFields [data-click-action="addComparisonGroup"]');if(add){add.disabled=rows.length>=4;add.title=rows.length>=4?'最多支持 4 个实验组':'添加一个对比组'}
}
function changeComparisonRole(select){clearGatewayFormError();normalizeComparisonRoles(select);schedulePreflight()}
function removeComparisonGroup(button){
 const rows=[...document.querySelectorAll('#groupFields .group-row')],row=button.closest('.group-row');
 if(!row)return;
 if(rows.length<=2){toast('对照实验至少保留 2 个组。');return}
 const baseline=row.querySelector('.group-role')?.value==='baseline';row.remove();normalizeComparisonRoles();schedulePreflight();
 if(baseline)toast('已移除基准组，请为剩余实验组选择一个基准组。')
}
function renderQualityFields(compare=false){
 const host=$('comparisonFields');
 host.innerHTML=UI.field({id:'qualityName',label:'评测名称',controlHtml:UI.input({id:'qualityName',placeholder:'例如：模型能力基线'})})+
  UI.formSectionTitle(1,compare?'选择两个资源':'选择评测资源')+'<div id="qualityResourceFields"></div>'+UI.formSectionTitle(2,'确认题集')+
  '<div class="quality-suite-note" id="qualitySuiteSummary" data-ready="false">正在读取题集…</div><details class="routing-advanced"><summary>原始记录</summary>'+UI.switchRow({id:'qualityCapture',title:'保存每道题的原始输入与输出',description:'便于逐题核对，会增加本地存储占用。'})+'</details>';
 $('qualityResourceFields').innerHTML=qualityResourceTemplate(0)+(compare?'<div class="field" id="qualityReuseField"><label for="qualityReuse">参考资源来源</label><select class="input" id="qualityReuse"><option value="">填写参考资源，本次一起评测</option></select><div class="field-hint">复用历史结果时，只请求资源 A。</div><div id="qualityReusePreview"></div></div>'+qualityResourceTemplate(1):'');
 renderSavedConnectionPickers();loadQualitySuiteSummary(compare);if(compare)loadQualityReferenceRecords();
}

let currentQualitySuite=null,qualitySuiteSeq=0;
function renderQualitySuiteSummary(compare=isCompareCreate()){
 const box=$('qualitySuiteSummary'),d=currentQualitySuite;if(!box||!d)return;
 const m=d.manifest,reused=Boolean($('qualityReuse')?.value),count=reused?1:compare?2:1,expanded=Boolean(box.querySelector('#quality-suite-method')?.open);
 box.innerHTML=UI.stack(`<div><b>${esc(m.title)}</b><p>单题串行 · ${m.case_count} 题 · 本次请求 ${count} 个资源 · 合计 ${m.case_count*count} 次请求${reused?'；参考侧复用历史结果':''}</p></div>`+
  '<div class="suite-actions">'+UI.button('查看全部题目、答案与规则',{action:'showQualitySuite'})+UI.button('评测方法与官方参考',{action:'showQualityMethodology'})+'</div>'+
  UI.disclosure({title:'执行方式与评分说明',id:'quality-suite-method',open:expanded,bodyHtml:`<p>${m.categories.length} 个维度；输入 ${m.input_chars?.min??'—'}～${m.input_chars?.max??'—'} 字符／题。每题请求一次，不自动重试；可停止后续题目。</p><p>${reused?'参考资源不重新请求，使用其已保存的同题集证据。':compare?'同题配对，逐题交替两侧的请求顺序。':'单资源按题序执行，侧重能力诊断。'}</p><p>请求可能计入上游费用；字符数不等同于词元数。</p>`}));
}
async function loadQualitySuiteSummary(compare=isCompareCreate()){
 const box=$('qualitySuiteSummary');if(!box)return;
 const seq=++qualitySuiteSeq;currentQualitySuite=null;box.dataset.ready='false';box.innerHTML='正在读取题集…';
 const current=()=>seq===qualitySuiteSeq&&box.isConnected&&$('qualitySuiteSummary')===box;
 try{
  const d=await api('/quality-suite');if(!current())return;
  if(!d.manifest||!Number.isFinite(d.manifest.case_count)||!Array.isArray(d.manifest.categories))throw new Error('题集信息不完整');
  currentQualitySuite=d;box.dataset.ready='true';renderQualitySuiteSummary(compare);
  if($('formStatus')?.textContent.startsWith('题集尚未加载'))clearQualityFormError();
 }catch(e){if(current())box.innerHTML=UI.stack(UI.notice('题集读取失败：'+e.message,{tone:'warning'})+UI.button('重新加载题集',{action:'loadQualitySuiteSummary'}))}
}

const modalStack=[];
function bytesText(n){const v=Number(n);if(!Number.isFinite(v)||v<0)return '—';const units=['B','KB','MB','GB','TB'];let i=0,x=v;while(x>=1024&&i<units.length-1){x/=1024;i++}return `${x.toFixed(i===0?0:1)} ${units[i]}`}
function openStorageModal(){openModal('storageModal');loadStorage()}
function closeStorageModal(){closeModal('storageModal')}
async function loadStorage(){const host=$('storageBody');if(!host)return;host.innerHTML='<div class="field-hint">正在读取本机结果文件占用…</div>';try{const d=await api('/storage',{timeout:20000});const largest=d.largest?`${esc(d.largest.name)}（${bytesText(d.largest.size)}）`:'—';host.innerHTML=`${d.warn?`<div class="notice">结果文件已占用 ${bytesText(d.total_bytes)}，建议清理不再需要的记录或孤立文件。</div>`:''}<div class="summary report-metrics"><div class="report-metric"><div class="metric-label">结果文件合计</div><div class="metric-value">${bytesText(d.total_bytes)}<small> ${num(d.artifact_count,0)} 个</small></div><div class="metric-note">仅统计 run_*.jsonl 与 quality_run_*.json</div></div><div class="report-metric"><div class="metric-label">已被记录引用</div><div class="metric-value">${bytesText(d.referenced_bytes)}</div><div class="metric-note">删除对应任务记录才会一并删除</div></div><div class="report-metric"><div class="metric-label">孤立文件</div><div class="metric-value">${bytesText(d.orphan_bytes)}<small> ${num(d.orphan_count,0)} 个</small></div><div class="metric-note">没有任何记录引用，可安全清理</div></div><div class="report-metric"><div class="metric-label">最大单个文件</div><div class="metric-value metric-value-text">${largest}</div><div class="metric-note">用于判断单次任务的结果规模</div></div></div><p class="report-section-sub">清理只删除“没有任何记录引用”的孤立文件；被引用的结果文件必须通过删除对应任务记录来删除，这样“删文件”和“删记录”始终是同一个可审计动作。</p><div class="report-footer-actions"><button class="btn" type="button" data-click-action="loadStorage">刷新</button><button class="btn danger" type="button" data-click-action="cleanupStorage" ${d.orphan_count?'':'disabled'}>清理孤立文件</button></div>${d.orphans?.length?`<div class="table-wrap report-wrap"><table class="report-table"><thead><tr><th>孤立文件</th><th>大小</th><th>最后修改</th></tr></thead><tbody>${d.orphans.map(x=>`<tr><td>${esc(x.name)}</td><td>${bytesText(x.size)}</td><td>${esc(UI.formatTimestamp(x.mtime))}</td></tr>`).join('')}</tbody></table></div>`:''}`}catch(e){host.innerHTML=`<div class="auth-error">${esc(e.message)}</div>`}}
async function cleanupStorage(){if(!confirm('确认清理孤立结果文件？这些文件没有任何任务记录引用，删除后无法恢复。'))return;try{const d=await api('/storage/cleanup',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({}) ,timeout:60000});toast(`已清理 ${num((d.removed||[]).length,0)} 个文件，释放 ${bytesText(d.freed_bytes)}`);loadStorage()}catch(e){toast(e.message)}}
/* ---------- 测试方案模板 ---------- */
const TEMPLATE_CATEGORIES={smoke:'冒烟',ramp:'阶梯',soak:'长跑'};
let templateCache=null,activeTemplateId=null,templateApplySeq=0;
let pressureModeDrafts=Object.create(null);
const COMPARE_TEMPLATE_MODES=['smoke','ramp','soak'];
// 模板只描述“怎么压”。请求内容类型（尤其是缓存压测）属于当前任务语义，
// 无论模板来自内置还是用户自建，都不能被模板中的旧字段悄悄改写。
const TEMPLATE_PRESSURE_FIELDS=Object.freeze(['rpm','iterations','ramps','duration','concurrency','connect_timeout','read_timeout']);
async function loadTemplates(){const host=$('templatePickerBody');if(!host)return;if(templateCache){renderTemplatePicker(templateCache);return}host.innerHTML='<div class="field-hint">正在读取模板…</div>';try{const d=await api('/templates');templateCache=d.templates||[];renderTemplatePicker(templateCache)}catch(e){templateCache=null;host.innerHTML=`<div class="field-hint">暂时无法读取模板：${esc(e.message)}</div>`}}
function renderTemplatePicker(items){const host=$('templatePickerBody');if(!host)return;const usable=(items||[]).filter(x=>COMPARE_TEMPLATE_MODES.includes(x.config?.mode));if(!usable.length){host.innerHTML='<div class="field-hint">暂无可用模板</div>';return}const guide='<div class="template-picker-note">模板替换模式、速率、时长/条数、并发和超时；连接与请求内容保留。</div>';host.innerHTML=guide+usable.map(t=>{const c=t.config||{};const shape=c.mode==='ramp'?`每分钟 ${esc(c.ramps||'')} 次 · 每档 ${duration(c.duration)}`:`每分钟 ${num(c.rpm)} 次 · ${c.mode==='soak'?duration(c.duration):`${num(c.iterations)} 条`}`;return `<button class="template-chip${activeTemplateId===t.id?' active':''}" type="button" aria-pressed="${activeTemplateId===t.id}" data-template="${esc(t.id)}" data-click-action="applyTemplate" data-arg0="${esc(t.id)}"><b>${esc(t.name)}${t.builtin?'':' · 自建'}</b><small>${esc(t.summary||'')}</small><em>并发 ${num(c.concurrency)} · ${shape}</em></button>`}).join('')}
function pickTemplate(id){const items=(templateCache||[]).filter(x=>COMPARE_TEMPLATE_MODES.includes(x.config?.mode));return items.find(x=>x.id===id)||null}
function clearGatewayFormError(){
 const status=$('formStatus'),notice=$('toast');if(!status?.classList.contains('error'))return;
 if(notice?.textContent===status.textContent)notice.classList.remove('show');
 status.className='form-status';status.textContent='';delete status.dataset.templateMessage;
}
async function applyTemplate(id){
 const seq=++templateApplySeq,kind=createKind;
 if(!templateCache)await loadTemplates();
 if(seq!==templateApplySeq||kind!==createKind||!isGatewayCreate())return;
 const t=pickTemplate(id);if(!t)return;
 activeTemplateId=t.id;const c=t.config||{};
 if(isCompareCreate()){
  const first=document.querySelector('#groupFields .group-row');
  if(first){const input=first.querySelector('.group-concurrency');if(input&&c.concurrency!=null)input.value=c.concurrency}
 }else{
  // 只应用压力字段白名单。模板里的 profile、URL、密钥、模型等运行语义永远不进入表单。
  pressureModeDrafts[mode]=readDynamic();
  if(COMPARE_TEMPLATE_MODES.includes(c.mode))paintMode(c.mode);
  const pressure={};for(const key of TEMPLATE_PRESSURE_FIELDS)if(c[key]!=null)pressure[key]=c[key];
  applyDynamic(pressure);
  for(const k of ['concurrency','connect_timeout','read_timeout'])if($(k)&&pressure[k]!=null){$(k).value=pressure[k];$(k).removeAttribute('aria-invalid')}
  pressureModeDrafts[mode]=readDynamic();
 }
 clearGatewayFormError();
 if($('formStatus')){$('formStatus').className='form-status';$('formStatus').dataset.templateMessage='true';$('formStatus').textContent=`已套用「${t.name}」的压力参数。`}
 renderTemplatePicker(templateCache);schedulePreflight()
}
function toggleTemplatePicker(){const body=$('templatePickerBody'),btn=$('templatePickerToggle');if(!body||!btn)return;const next=body.hidden;body.hidden=!next;btn.setAttribute('aria-expanded',String(next));btn.textContent=next?'收起':'展开'}
/* ---------- 运行诊断 ---------- */
function openDiagnostics(){openModal('diagnosticsModal');loadDiagnostics()}
function closeDiagnostics(){closeModal('diagnosticsModal')}
function diagLimit(){const v=Number($('diagLimit')?.value||50);return Number.isFinite(v)?Math.max(10,Math.min(500,Math.round(v))):50}
function diagTime(ts){return UI.formatTimestamp(ts)}
function diagList(items,render,emptyText){if(!items||!items.length)return `<div class="diag-empty">${esc(emptyText)}</div>`;return `<ul class="diag-list">${items.map(render).join('')}</ul>`}
function renderSchedulerBlock(s){if(!s)return '<div class="diag-empty">无法读取调度状态。</div>';const running=(s.running_tasks||[]).map(t=>t.name||t.id);const waiting=(s.waiting_tasks||[]).map(t=>t.name||t.id);return `<div class="diag-block"><h3>调度器</h3><p class="diag-sub">并发上限决定同时能跑几个任务；排队中的任务按创建顺序进入。</p><div class="diag-kv"><span>并发上限 ${num(s.limit,0)}</span><span>运行中 ${num(s.active,0)}</span><span>排队 ${num(waiting.length,0)}</span></div><div class="diag-list diag-list-spaced">${running.length?running.map(n=>`<li><b>运行中</b><span>${esc(n)}</span></li>`).join(''):'<div class="diag-empty">当前没有运行中的任务。</div>'}${waiting.length?waiting.map(n=>`<li><b>排队中</b><span>${esc(n)}</span></li>`).join(''):''}</div></div>`}
function renderEventBlock(events){return `<div class="diag-block"><h3>最近事件</h3><p class="diag-sub">只记录「发生了什么」，请求正文与密钥不会写入事件日志。最新在上。</p>${diagList(events,e=>`<li><b>${esc(e.event||'—')}</b><span>${esc(e.rid||'-')}${e.status?` · HTTP ${esc(e.status)}`:''}${e.elapsed_ms!=null?` · ${num(e.elapsed_ms,0)} ms`:''}${e.path?` · ${esc(e.path)}`:''}${e.task?` · ${esc(e.task)}`:''}</span><time>${diagTime(e.ts)}</time></li>`,'暂无事件记录')}</div>`}
function renderAuditBlock(records){return `<div class="diag-block"><h3>操作审计</h3><p class="diag-sub">谁在什么时候做了什么。失败的操作也会保留，便于回溯。</p>${diagList((records||[]).slice().reverse(),r=>`<li><b>${esc(r.action||'—')}</b><span>${esc(r.actor||'anonymous')}${r.target?` · ${esc(r.target)}`:''}${r.ok===false?' · 失败':''}${r.detail?` · ${esc(r.detail)}`:''}</span><time>${diagTime(r.ts)}</time></li>`,'暂无审计记录')}</div>`}
function renderEventStatsBlock(stats){if(!stats)return '';const by=stats.by_event||{};const keys=Object.keys(by);return `<div class="diag-block"><h3>事件统计</h3><p class="diag-sub">按事件名累计，用于快速判断服务是否在正常工作。</p><div class="diag-kv">${keys.length?keys.map(k=>`<span>${esc(k)} ${num(by[k],0)}</span>`).join(''):'<span>暂无事件</span>'}</div>${stats.degraded?'<div class="diag-empty diag-list-spaced">事件日志写入曾失败，已降级为不影响业务；请检查磁盘权限与剩余空间。</div>':''}</div>`}
async function loadDiagnostics(){const host=$('diagnosticsBody');if(!host)return;host.innerHTML='<div class="field-hint">正在读取调度状态、事件与审计记录…</div>';const limit=diagLimit();const results=await Promise.all([api('/scheduler').catch(e=>({__error:e.message})),api(`/events?limit=${limit}`).catch(e=>({__error:e.message})),api(`/audit?limit=${limit}`).catch(e=>({__error:e.message}))]);const [scheduler,events,audit]=results;const errors=[scheduler,events,audit].filter(x=>x&&x.__error).map(x=>x.__error);host.innerHTML=`${errors.length?`<div class="diag-block"><h3>部分读取失败</h3><p class="diag-sub">${esc([...new Set(errors)].join('；'))}</p></div>`:''}${renderSchedulerBlock(scheduler.__error?null:scheduler)}${renderEventBlock(events.events||[])}${renderEventStatsBlock(events.stats)}${renderAuditBlock(audit.records||[])}`}
function artifactNotice(record){const a=record?.artifact||{},state=a.state||record?.artifact_state;if(state==='expired'||state==='missing'){return `<div class="notice risk">${esc(a.expired_reason||record?.artifact_expired_reason||'结果文件已失效，可能已被删除或改写，报告里的证据无法再逐条核对。')}</div>`}if(state==='deleted'){return '<div class="notice">结果文件已删除；页面仍保留这次运行的历史摘要，但不能再下载原始结果。</div>'}return ''}
function openModal(id){const modal=$(id);if(!modal)return;if(!modalStack.some(x=>x.id===id))modalStack.push({id,opener:document.activeElement});modal.hidden=false;modal.style.zIndex=String(120+modalStack.findIndex(x=>x.id===id)*2);document.body.classList.add('modal-open');const body=modal.querySelector('.modal-body');if(body)body.scrollTop=0;modal.querySelector('.modal-close,.drawer-close')?.focus({preventScroll:true})}
function closeModal(id){const modal=$(id);if(!modal||modal.hidden)return;const index=modalStack.findIndex(x=>x.id===id),entry=index>=0?modalStack.splice(index,1)[0]:null;modal.hidden=true;document.body.classList.toggle('modal-open',modalStack.length>0);if(entry?.opener?.isConnected)entry.opener.focus({preventScroll:true});if(id==='repeatModal')window.RepeatUI?.onClosed()}
document.addEventListener('keydown',e=>{const top=modalStack[modalStack.length-1];if(!top)return;const modal=$(top.id);if(e.key==='Escape'){e.preventDefault();e.stopImmediatePropagation();closeModal(top.id);return}if(e.key!=='Tab')return;const focusables=[...modal.querySelectorAll('button:not([disabled]),a[href],input:not([disabled]),select:not([disabled]),summary,[tabindex="0"]')].filter(el=>el.getClientRects().length);if(!focusables.length)return;const first=focusables[0],last=focusables[focusables.length-1];if(!modal.contains(document.activeElement)){e.preventDefault();first.focus()}else if(e.shiftKey&&document.activeElement===first){e.preventDefault();last.focus()}else if(!e.shiftKey&&document.activeElement===last){e.preventDefault();first.focus()}},true);
function sourceLink(source){let url='';try{const parsed=new URL(source.url);if(parsed.protocol==='https:')url=parsed.href}catch{}return url?`<a href="${esc(url)}" target="_blank" rel="noopener noreferrer">${esc(source.name)} ↗</a>`:esc(source.name)}
async function showQualityMethodology(context='suite'){
 const frozen=context==='report'?window.currentQualityDetail?.config?.suite_manifest?.methodology:currentQualitySuite?.manifest?.methodology;
 openModal('qualityMethodologyModal');$('qualityMethodologyBody').textContent='正在读取方法与来源…';
 try{const m=frozen||await api('/quality-methodology');if($('qualityMethodologyModal').hidden)return;const sources=m.sources||[];
 $('qualityMethodologyBody').innerHTML=`<div class="quality-suite-note"><strong>${esc(m.provenance)}</strong><p>方法版本 ${esc(m.version)} · 官方资料核对 ${esc(m.reviewed_at)}。${context==='report'&&!frozen?'这条历史任务未保存方法快照；以下仅是当前方法参考，不改变历史结论。':frozen?'展示与题集一起保存的方法快照。':'展示当前方法。'}</p></div><h3>怎样从测试走到结论</h3><ol class="methodology-steps">${(m.workflow||[]).map(x=>`<li>${esc(x)}</li>`).join('')}</ol><h3>分数怎么计算、怎样用于选择</h3><p>${esc(m.score_policy)}</p><p>${esc(m.decision_policy)}</p><h3>12个维度分别测什么</h3><div class="methodology-grid">${(m.dimensions||[]).map(d=>`<section class="methodology-card"><h3>${esc(d.label)}</h3><p>${esc(d.scope)}</p><p>方法参考：${(d.references||[]).map(id=>sources.find(x=>x.id===id)).filter(Boolean).map(sourceLink).join(' · ')}</p></section>`).join('')}</div><h3>官方评测框架与题集参考</h3><p>“借鉴方法”不等于“集成官方基准”。以下逐项列出已采用的部分和没有覆盖的部分。</p><div class="methodology-grid">${sources.map(x=>`<section class="methodology-card"><h3>${sourceLink(x)}</h3><span class="methodology-status">方法参考 · 非官方成绩</span><p><b>原项目重点：</b>${esc(x.focus)}</p><p><b>本项目采用：</b>${esc(x.adopted)}</p><p><b>未覆盖：</b>${esc(x.not_covered)}</p></section>`).join('')}</div><h3>引入官方数据前的要求</h3><p>${esc(m.import_policy)}</p>`
 }catch(e){$('qualityMethodologyBody').textContent='方法读取失败：'+e.message}
}
function showQualitySuite(){const d=currentQualitySuite;if(!d)return;const modal=$('qualitySuiteModal');window.qualitySuiteOpener=document.activeElement;openModal('qualitySuiteModal');$('qualitySuiteTitle').textContent=d.manifest.title;const groups=[...new Set(d.cases.map(c=>c.category))];$('qualitySuiteBody').innerHTML=`<p>${esc(d.manifest.description)}</p><p>${esc(d.manifest.scoring)}</p><p>原创建设题，不等同于公开标准榜单；查看答案便于审计，不向被测资源发送参考答案。</p><button class="btn" data-click-action="showQualityMethodology">评测方法与官方参考</button><div class="quality-suite-cases">${groups.map(cat=>`<details><summary>${esc(categoryLabel(cat))} · ${d.cases.filter(c=>c.category===cat).length} 题</summary>${d.cases.filter(c=>c.category===cat).map(c=>`<details><summary>${esc(c.title)} · ${esc(c.difficulty)}${c.critical?' · 关键规则':''}</summary><p>输入长度 ${c.prompt.length} 字符。评分：${c.evaluator?.type==='verifiable_constraints'?'逐项可验证约束（任一未满足则整题不通过）':c.evaluator?.type==='json_exact'?'完整结构与值验证':'答案与指定格式验证'}。</p><pre class="evidence-pre">${esc(c.prompt)}</pre>${c.evaluator?.rules?`<ul>${c.evaluator.rules.map(rule=>`<li>${esc(rule.label)}</li>`).join('')}</ul>`:''}<b>参考答案${c.evaluator?.rules?'（一种合规示例）':''}</b><pre class="evidence-pre">${esc(c.reference_answer)}</pre><p>${esc(c.rationale)}</p></details>`).join('')}</details>`).join('')}</div>`;modal.querySelector('.modal-close').focus()}
function closeQualitySuite(){closeModal('qualitySuiteModal')}

function qualityResourceTemplate(index){
 const title=index===0?'资源 A':'资源 B（参考）';
 const classes={name:'class="input quality-name"',url:'class="input quality-url"',key:'class="input quality-key"'};
 const input=(field,options)=>UI.input({id:`quality-${field}-${index}`,...options}).replace('class="input"',classes[field]);
 const connection=UI.field({id:`quality-name-${index}`,label:'资源名称',controlHtml:input('name',{value:index===0?'资源 A':'资源 B'})})+
  `<div id="connection-picker-quality-${index}">${savedConnectionPickerHtml('quality',String(index))}</div>`+
  `<details class="routing-advanced" id="connection-details-quality-${index}" open><summary>地址与密钥</summary>`+
  UI.field({id:`quality-url-${index}`,label:'接口地址',required:true,controlHtml:input('url',{type:'url',placeholder:'https://provider.example.com/v1'})})+
  UI.field({id:`quality-key-${index}`,label:'API Key（接口密钥）',controlHtml:input('key',{type:'password',placeholder:'已保存密钥可留空复用'})+'<div class="field-hint quality-credential-hint">密钥留空可复用，输入新值会覆盖。</div>'})+'</details>';
 const model=UI.field({id:`quality-model-${index}`,label:'模型',required:true,controlHtml:`<div class="quality-model-row"><select id="quality-model-${index}" class="input quality-model" disabled><option value="">先选择连接并获取模型</option></select><button class="icon-btn quality-model-refresh" type="button" data-click-action="refreshQualityModels" data-arg0="${index}">获取模型</button></div><div id="quality-model-manual-wrap-${index}" hidden>`+
  UI.field({id:`quality-model-manual-${index}`,label:'手动模型标识',controlHtml:UI.input({id:`quality-model-manual-${index}`,placeholder:'填写服务商提供的准确模型名称'}).replace('<input ','<input disabled '),hint:'仅用于模型列表不可用时；名称不会自动校验。'})+`</div>`+
  UI.textButton('无法获取列表？手动指定模型',{action:'setQualityModelMode',args:[index]}).replace('<button ',`<button id="quality-model-toggle-${index}" aria-expanded="false" aria-controls="quality-model-manual-wrap-${index}" `)+`<div class="field-hint quality-model-hint" aria-live="polite"></div>`});
 return `<section class="quality-resource" data-quality-index="${index}"><div class="card-title">${title}</div>`+UI.stack(connection+model)+'</section>';
}

function currentQualityModel(row){return (row?.dataset.modelMode==='manual'?$(`quality-model-manual-${row.dataset.qualityIndex}`)?.value:row?.querySelector('.quality-model')?.value)?.trim()||''}
function setQualityModelMode(index){
 const row=document.querySelector(`.quality-resource[data-quality-index="${index}"]`);if(!row)return;
 const manual=row.dataset.modelMode!=='manual',input=$(`quality-model-manual-${index}`),panel=$(`quality-model-manual-wrap-${index}`),toggle=$(`quality-model-toggle-${index}`),select=row.querySelector('.quality-model'),wrap=row.querySelector('.quality-model-row'),button=row.querySelector('.quality-model-refresh'),hint=row.querySelector('.quality-model-hint');
 row.dataset.modelMode=manual?'manual':'list';
 if(row.dataset.modelPending==='true'){row.dataset.modelSeq=String(++qualityModelSeq);delete row.dataset.modelPending;button.disabled=false;button.removeAttribute('aria-busy');button.textContent='获取模型';wrap.classList.remove('is-loading');hint.textContent='已切换填写方式；上一条列表响应不会更改当前输入。'}
 panel.hidden=!manual;input.disabled=!manual;wrap.hidden=manual;hint.hidden=manual;select.disabled=manual||row.dataset.modelLoaded!=='true';
 const label=row.querySelector(`label[for="quality-model-${index}"]`);if(label)label.hidden=manual;
 toggle.textContent=manual?'返回模型列表选择':'无法获取列表？手动指定模型';toggle.setAttribute('aria-expanded',String(manual));
 (manual?input:select.disabled?button:select).focus({preventScroll:true});schedulePreflight();
}
let qualityModelSeq=0;
async function refreshQualityModels(index){
 const row=document.querySelector(`.quality-resource[data-quality-index="${index}"]`),url=row?.querySelector('.quality-url')?.value.trim(),key=row?.querySelector('.quality-key')?.value.trim(),select=row?.querySelector('.quality-model'),button=row?.querySelector('.quality-model-refresh'),hint=row?.querySelector('.quality-model-hint'),wrap=row?.querySelector('.quality-model-row');
 if(!row||!select||!button||!hint||row.dataset.modelPending==='true'||row.dataset.modelMode==='manual')return;
 if(!url){focusCreateField(row.querySelector('.quality-url'));hint.textContent='请先填写接口地址，再获取模型。';hint.className='field-hint quality-model-hint error';return}
 const connection=boundConnectionId('quality',index),previous=select.value,seq=++qualityModelSeq;
 const current=()=>row.isConnected!==false&&document.querySelector(`.quality-resource[data-quality-index="${index}"]`)===row&&row.dataset.modelSeq===String(seq)&&row.dataset.modelMode!=='manual'&&row.querySelector('.quality-url').value.trim()===url&&row.querySelector('.quality-key').value.trim()===key&&boundConnectionId('quality',index)===connection;
 row.dataset.modelSeq=String(seq);row.dataset.modelPending='true';button.disabled=true;button.setAttribute('aria-busy','true');select.disabled=true;wrap.classList.remove('is-done','is-error');wrap.classList.add('is-loading');button.textContent='获取中…';hint.textContent='正在请求模型列表，请稍候…';hint.className='field-hint quality-model-hint';
 try{
  const d=await api('/models',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({url,api_key:key,...(connection?{connection_id:connection}:{})}),timeout:15000});
  if(!current())return;
  const models=[...new Set((Array.isArray(d.models)?d.models:[]).filter(model=>typeof model==='string'&&model.trim()).map(model=>model.trim()))],kept=previous&&models.includes(previous);
  row.dataset.modelLoaded=String(models.length>0);
  select.innerHTML=`<option value="">${models.length?'请选择模型':'接口没有返回可用模型'}</option>`+models.map(model=>`<option value="${esc(model)}">${esc(model)}</option>`).join('');
  select.value=kept?previous:'';select.disabled=!models.length;wrap.classList.remove('is-loading');wrap.classList.add(models.length?'is-done':'is-error');button.textContent=models.length?'刷新模型':'重试';
  hint.textContent=models.length?`已获取 ${models.length} 个模型；${kept?'保留原选模型。':previous?'原选模型已不在列表中，请重新选择。':'请明确选择一个模型。'}`:`${d.error||'模型列表为空'}；可手动指定模型，或检查地址与密钥后重试。`;
  hint.className=`field-hint quality-model-hint ${models.length?'ok':'error'}`;
 }catch(e){
  if(!current())return;
  const retained=row.dataset.modelLoaded==='true';select.disabled=!retained;
  if(!retained){select.innerHTML='<option value="">获取失败，请重试或手动指定</option>';select.value=''}
  wrap.classList.remove('is-loading');wrap.classList.add('is-error');button.textContent='重试';hint.textContent=`${e.message||'获取模型失败'}；${retained?'保留上次模型列表与选择，本次刷新未验证。':''}可重试或手动指定模型。`;hint.className='field-hint quality-model-hint error';
 }finally{
  if(row.dataset.modelSeq===String(seq)&&row.isConnected!==false&&document.querySelector(`.quality-resource[data-quality-index="${index}"]`)===row){if(!current())invalidateQualityModels(row);else{delete row.dataset.modelPending;button.disabled=false;button.removeAttribute('aria-busy')}}
 }
}
let qualityCredentialSeq=0;
function clearQualityFormError(){
 const status=$('formStatus');if(!status?.classList.contains('error'))return;
 if($('toast')?.textContent===status.textContent)$('toast').classList.remove('show');
 status.className='form-status';status.textContent='';
}
function qualityCreationChanged(event){
 if(!isQualityCreate()||!event.target.closest('#comparisonFields'))return;
 clearQualityFormError();
 if(event.target.matches('.quality-url,.quality-key'))checkQualityCredential(event.target.closest('.quality-resource'));
}
async function checkQualityCredential(row){
 const url=String(row?.querySelector('.quality-url')?.value||'').trim(),key=String(row?.querySelector('.quality-key')?.value||'').trim(),hint=row?.querySelector('.quality-credential-hint');if(!hint)return;
 const seq=++qualityCredentialSeq;row.dataset.credentialSeq=String(seq);
 if(boundConnectionId('quality',row.dataset.qualityIndex)){updateBoundConnectionHint('quality',row.dataset.qualityIndex);return}
 if(key){hint.textContent='已填写本次接口密钥；开始后使用此值。';hint.className='field-hint quality-credential-hint';return}
 if(!url){hint.textContent='填写接口地址后检查已保存密钥；留空可复用，输入新值会覆盖。';hint.className='field-hint quality-credential-hint';return}
 hint.textContent='正在检查该地址的已保存密钥…';hint.className='field-hint quality-credential-hint';
 const current=()=>row.isConnected&&String(seq)===row.dataset.credentialSeq&&row.querySelector('.quality-url')?.value.trim()===url&&!row.querySelector('.quality-key')?.value.trim()&&!boundConnectionId('quality',row.dataset.qualityIndex);
 try{const d=await api('/credential-status?url='+encodeURIComponent(url));if(!current())return;hint.textContent=d.saved?`已保存密钥 ${d.hint||''}；接口密钥留空即可复用，输入新值会覆盖。`:'该地址尚未保存密钥，请填写接口密钥。';hint.className=`field-hint quality-credential-hint ${d.saved?'ok':'error'}`}
 catch(e){if(current()){hint.textContent='暂时无法检查已保存密钥；提交时仍会再次校验。';hint.className='field-hint quality-credential-hint error'}}
}

function addQualityResource(){const wrap=$('qualityResourceFields'),button=$('addQualityResource');if(!wrap||wrap.querySelectorAll('#qualityResourceFields .quality-resource').length>=2)return toast('最多支持两个资源实例');wrap.insertAdjacentHTML('beforeend',qualityResourceTemplate(1));if(button)button.disabled=true}
let qualityReferenceRecords=[];async function loadQualityReferenceRecords(){const select=$('qualityReuse');if(!select)return;try{const d=await api('/quality-reference-records');if(!select.isConnected||$('qualityReuse')!==select)return;qualityReferenceRecords=d.records||[];select.innerHTML='<option value="">实时填写参考资源</option>'+qualityReferenceRecords.map(x=>`<option value="${esc(x.id)}">复用：${esc(x.name)} · ${esc(x.model||'未知模型')} · ${comparisonMetric(x.score_1000)}/1000 · ${x.case_count||0}题</option>`).join('');select.onchange=()=>{const reference=document.querySelector('[data-quality-index="1"]');if(reference)reference.hidden=Boolean(select.value);renderQualitySuiteSummary(true);schedulePreflight();const x=qualityReferenceRecords.find(r=>r.id===select.value);$('qualityReusePreview').innerHTML=x?`<div class="reference-reuse-card"><b>已选择历史参考结果</b><span>${esc(x.name)} · ${esc(x.model||'未知模型')} · ${comparisonMetric(x.score_1000)}/1000</span><button type="button" class="text-btn" data-click-action="showQualityHistoryReference" data-arg0="${esc(x.id)}">查看上次评测详情</button></div>`:''}}catch(e){if(select.isConnected&&$('qualityReuse')===select)select.innerHTML='<option value="">填写参考资源，本次一起评测（历史记录暂不可用）</option>'}}async function showQualityHistoryReference(id){let q=qualityTests.find(x=>x.id===id);if(!q)return;if(q._summary){try{q=await api('/quality-report/'+encodeURIComponent(id))}catch(e){toast(e.message);return}}const report=q.report||{},r=(report.resources||[])[0]||{},m=q.config?.suite_manifest||{};$('qualityEvidenceTitle').textContent='历史参考评测详情';$('qualityEvidenceBody').innerHTML=`<div class="reference-detail"><h3>${esc(q.name||'历史质量评测')}</h3><p>模型：${esc(q.resources?.[0]?.model||'—')} · 题数：${q.case_count||0} · 题集指纹：${esc(m.fingerprint||'未记录')}</p><div class="report-metrics"><div class="report-metric"><div class="metric-label">质量分</div><div class="metric-value">${comparisonMetric(r.score_1000)}/1000</div></div><div class="report-metric"><div class="metric-label">通过题数</div><div class="metric-value">${r.passed_cases||0}/${r.total_cases||0}</div></div><div class="report-metric"><div class="metric-label">请求错误</div><div class="metric-value">${r.request_errors||0}</div></div></div><h4>维度分</h4><div class="table-wrap report-wrap"><table class="report-table"><thead><tr><th>维度</th><th>得分</th><th>通过/计划</th></tr></thead><tbody>${(r.categories||[]).map(c=>`<tr><td>${esc(c.label)}</td><td>${comparisonMetric(c.score_1000)}/1000</td><td>${c.passed||0}/${c.planned||0}</td></tr>`).join('')}</tbody></table></div><h4>失败题</h4><ul>${(r.failure_evidence||[]).slice(0,12).map(f=>`<li>${esc(f.case_id)} · ${esc(f.title)}：${esc(f.reason)}</li>`).join('')||'<li>无失败题</li>'}</ul></div>`;openModal('qualityEvidenceModal')}
function focusCreateField(input){
 if(!input)return;
 for(let node=input.parentElement;node&&node!==$('config');node=node.parentElement)if(node.tagName==='DETAILS')node.open=true;
 input.setAttribute('aria-invalid','true');input.focus();input.scrollIntoView({block:'nearest'});
}
function validateQualityForm(){
 if($('qualitySuiteSummary')?.dataset.ready==='false'){focusCreateField($('qualitySuiteSummary'));throw new Error('题集尚未加载，请先重新加载并确认题目数量。')}
 const reuse=$('qualityReuse')?.value,rows=[...document.querySelectorAll('#qualityResourceFields .quality-resource')].filter((row,index)=>!reuse||index===0);
 for(const row of rows){
  const name=row.querySelector('.quality-name')?.value.trim()||'资源',url=row.querySelector('.quality-url'),manual=row.dataset.modelMode==='manual',model=manual?$(`quality-model-manual-${row.dataset.qualityIndex}`):row.querySelector('.quality-model'),refresh=row.querySelector('.quality-model-refresh');
  let valid=false;try{valid=['http:','https:'].includes(new URL(url.value.trim()).protocol)}catch{}
  if(!valid){focusCreateField(url);throw new Error(`${name}：请填写完整的接口地址，例如 https://example.com/v1。`)}
  if(!manual&&refresh.disabled){focusCreateField(refresh);throw new Error(`${name}：正在获取模型列表，请等待完成，或切换为手动指定。`)}
  if(!model||model.disabled||!currentQualityModel(row)){focusCreateField(manual?model:refresh);throw new Error(`${name}：${manual?'请填写手动模型标识。':'请先获取并选择模型；接口不支持列表时，可手动指定。'}`)}
 }
}
function validateGatewayForm(){
 const url=$('url');let valid=false;try{valid=['http:','https:'].includes(new URL(url.value.trim()).protocol)}catch{}
 if(!valid){focusCreateField(url);throw new Error('请填写完整的接口地址，例如 https://example.com/v1。')}
 if(modelFetchState==='loading')throw new Error('正在获取模型列表，请等待完成后选择模型。');
 if(!currentModel()){focusCreateField($('modelManualWrap')?.hidden?$('loadModels'):$('model'));throw new Error('请先选择模型；接口不提供模型列表时，可手动指定。')}
 const reject=(input,message)=>{focusCreateField(input);throw new Error(message)};
 const number=(input,label,min,max,integer=false)=>{
  if(!input)return;
  const raw=input.value.trim(),value=Number(raw);
  if(!raw)reject(input,`请填写${label}。`);
  if(!/^[+-]?(?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?$/.test(raw)||!Number.isFinite(value)||value<min||value>max||(integer&&!Number.isInteger(value)))reject(input,`${label}应为 ${min.toLocaleString('zh-CN')}～${max.toLocaleString('zh-CN')} 的${integer?'整数':'数值'}。`);
 };
 if(isCompareCreate()){
  const rows=[...document.querySelectorAll('#groupFields .group-row')];
  if(rows.length<2||rows.length>4)reject($('groupFields'), '对照实验需要 2～4 个实验组。');
  if(rows.filter(row=>row.querySelector('.group-role')?.value==='baseline').length!==1)reject(rows[0]?.querySelector('.group-role'),'请为本次对照明确选择一个基准组。');
  rows.forEach((row,index)=>{
   const label=`实验组 ${index+1} 的`;
   number(row.querySelector('.group-concurrency'),label+'并发上限',1,2000,true);
   number(row.querySelector('.group-rpm'),label+'计划调度速率',0.01,600000);
   number(row.querySelector('.group-iterations'),label+'请求条数',1,10000000,true);
  });
 }else{
  number($('concurrency'),'并发上限',1,2000,true);
  number($('rpm'),'计划调度速率',0.01,600000);
  number($('iterations'),'计划请求条数',1,10000000,true);
  number($('duration'),mode==='ramp'?'每档调度时长（秒）':'计划调度时长（秒）',1,86400,true);
  if(mode==='ramp'){
   const input=$('ramps'),parts=input.value.trim().split(',');
   if(!input.value.trim())reject(input,'请填写各档计划调度速率。');
   if(parts.length>50||parts.some(part=>!part.trim()||!/^[+]?(?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?$/.test(part.trim())||!Number.isFinite(Number(part))||Number(part)<=0||Number(part)>600000))reject(input,'请用英文逗号分隔 1～50 档速率；每档必须大于 0，且不超过 600,000 次/分钟。');
  }
 }
 number($('connect_timeout'),'连接超时（秒）',0.1,3600);
 number($('read_timeout'),'读取超时（秒）',0.1,3600);
}
document.addEventListener('input',e=>{if(e.target.closest?.('#config'))e.target.removeAttribute('aria-invalid')});
function readQuality(){const reuse=$('qualityReuse')?.value||'';const rows=[...document.querySelectorAll('#qualityResourceFields .quality-resource')];const selectedRows=reuse?rows.slice(0,1):rows;return {name:$('qualityName')?.value||'质量评测',reference_reuse_id:reuse,capture_raw:Boolean($('qualityCapture')?.checked),resources:selectedRows.map(row=>({name:row.querySelector('.quality-name').value.trim(),url:row.querySelector('.quality-url').value.trim(),api_key:row.querySelector('.quality-key').value.trim(),...(boundConnectionId('quality',row.dataset.qualityIndex)?{connection_id:boundConnectionId('quality',row.dataset.qualityIndex)}:{}),model:currentQualityModel(row),is_reference:false}))}}
function readComparison(){const shared=readConfig();const name=$('comparisonName')?.value||'对照实验';shared.name=name;shared.mode='smoke';shared.rpm=100;shared.iterations=1;shared.concurrency=20;shared.capture_raw=Boolean($('comparisonCapture')?.checked);return {name,shared_config:shared,execution_policy:'serial',groups:[...document.querySelectorAll('.group-row')].map((row,i)=>({id:`group-${i+1}`,name:row.querySelector('.group-name').value.trim()||`实验组 ${i+1}`,role:row.querySelector('.group-role')?.value|| (i===0?'baseline':'comparison'),concurrency:row.querySelector('.group-concurrency').value,rpm:row.querySelector('.group-rpm').value,iterations:row.querySelector('.group-iterations').value}))}}
function qualityStatusLabel(status){return {draft:'草稿',stopped:'已停止',stopping:'正在停止',running:'评测中',finished:'已完成',finished_with_errors:'完成但有错误',error:'失败',expired:'结果已过期',waiting:'排队中'}[status]||status}
let preflightData=null,preflightSeq=0,preflightTimer=null,preflightAckRequested=false;
function preflightLevelLabel(level){return {risk:'高风险',warn:'需注意',info:'提示',ok:'未发现风险'}[level]||'预检'}function preflightSeconds(s){const v=Number(s);if(!Number.isFinite(v)||v<=0)return '—';if(v<60)return `${num(v,0)} 秒`;if(v<3600)return `${num(v/60,1)} 分钟`;return `${num(v/3600,1)} 小时`}
function preflightHtml(d){if(d.unavailable)return `<div class="preflight-head"><span class="preflight-badge">无法预检</span><span class="preflight-title">按当前配置无法完成预检</span></div><p class="preflight-note">${esc(d.unavailable)} 补全配置后会自动更新；开始前仍需通过表单校验。</p>`;const findings=d.findings||[],resources=Number(d.resource_count)||1;const planned=`${num(d.planned_requests,0)} 次请求`;const cases=d.case_count?` · ${num(d.case_count,0)} 题 × ${resources} 资源`:'';return `<div class="preflight-head"><span class="preflight-badge">预计 ${esc(preflightLevelLabel(d.level))}</span><span class="preflight-title">这次运行大约会发生什么</span></div><div class="preflight-summary"><span>计划请求 <b>${planned}</b>${cases}</span><span>预计耗时 <b>${esc(preflightSeconds(d.min_seconds))}</b></span><span>结果文件 <b>${esc(bytesText(d.estimated_bytes))}</b></span><span>目标 <b>${esc((d.target||{}).host||'未知')}</b></span></div>${findings.length?`<ul class="preflight-findings">${findings.map(f=>`<li class="lv-${esc(f.level||'info')}"><b>${esc(f.title||'风险项')}</b>${esc(f.detail||'')}</li>`).join('')}</ul>`:''}<p class="preflight-note">${esc(d.note||'估算基于配置与经验单行长度，不是对上游真实性能的预测。')}</p>`}
function renderPreflight(d){const host=$('preflightCard');if(!host)return;preflightData=d;if(!d.requires_confirmation)preflightAckRequested=false;host.className=`preflight-card level-${d.unavailable?'info':d.level||'ok'}`;host.innerHTML=preflightHtml(d);host.hidden=false;if(d.requires_confirmation){const ack=document.createElement('label');ack.className='preflight-ack';ack.innerHTML=`<input type="checkbox" id="riskAck"><span>我已确认目标是生产/公网环境：这次运行会产生真实负载、可能计入上游费用并影响线上用户。</span>`;host.appendChild(ack);const box=$('riskAck');if(box){box.checked=Boolean(preflightAckRequested);box.onchange=()=>{preflightAckRequested=box.checked}}}}
async function refreshPreflight(){const host=$('preflightCard');if(!host)return;let draft;try{draft=isRoutingCreate()?readRouting():isQualityCreate()?readQuality():isCompareCreate()?readComparison():readConfig()}catch(e){host.hidden=true;return}const seq=++preflightSeq;try{const d=await api('/preflight',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({kind:isRoutingCreate()?'routing':isQualityCreate()?'quality':isCompareCreate()?'comparison':'run',config:draft}),timeout:20000});if(seq!==preflightSeq||!$('config').classList.contains('mobile-open'))return;renderPreflight(d)}catch(e){if(seq===preflightSeq&&$('config').classList.contains('mobile-open')){host.className='preflight-card';host.innerHTML=`<p class="preflight-note">暂时无法预检：${esc(e.message)}</p>`;host.hidden=false}}}
function schedulePreflight(){if(!$('config')?.classList.contains('mobile-open'))return;clearTimeout(preflightTimer);preflightTimer=setTimeout(refreshPreflight,450)}
async function ensurePreflightAck(){const button=$('submit');if(!preflightData||!preflightData.requires_confirmation)return true;if($('riskAck')?.checked)return true;const ok=confirm(`${preflightData.target?.host||'该目标'} 疑似生产环境：${preflightData.target?.reason||'无法确认是否为测试环境'}\n\n这次运行会产生真实负载并可能计费。确认要在该目标上执行吗？`);if(!ok)return false;const box=$('riskAck');if(box)box.checked=true;preflightAckRequested=true;return true}
function comparisonMetric(v,suffix=''){return v==null?'—':num(v)+suffix}
function pctDelta(v,suffix='',lowerIsBetter=false){if(v==null)return '—';const n=Number(v),better=lowerIsBetter?n<0:n>0,worse=lowerIsBetter?n>0:n<0;return `<span class="${worse?'delta-risk':better?'delta-good':'delta-neutral'}">${n>0?'+':''}${num(n)}${suffix}</span>`}
function pctl(values,p){const a=values.filter(v=>Number.isFinite(Number(v))).map(Number).sort((a,b)=>a-b);return a.length?a[Math.max(0,Math.min(a.length-1,Math.ceil(a.length*p)-1))]:null}
function setRecordFilter(view){autoPickView=false;activeView=view==='compare'?'compare':'single';selected=null;detailTask=null;selectedComparison=null;selectedQuality=null;localStorage.removeItem('route_selected_task');updateWorkspaceChrome();renderWorkspace()}
function updateWorkspaceChrome(){
 document.querySelectorAll('.module-nav-item').forEach(x=>{const active=x.dataset.module===activeModule;x.classList.toggle('active',active);x.setAttribute('aria-current',active?'page':'false')});
 if(activeModule==='tpm'){window.ConnectionUI?.hide();window.FingerprintUI?.hide();window.TpmUI?.updateChrome();return}window.TpmUI?.hide();
 if(activeModule==='fingerprint'){window.ConnectionUI?.hide();window.FingerprintUI?.updateChrome();return}window.FingerprintUI?.hide();
 if(activeModule==='connections'){window.ConnectionUI?.updateChrome();return}window.ConnectionUI?.hide();
 document.querySelectorAll('.subnav-item').forEach(x=>x.classList.toggle('active',x.closest('.workspace-subnav')?.id===`${activeModule}Subnav`&&x.dataset.view===activeView));
 $('gatewaySubnav').hidden=activeModule!=='gateway';$('qualitySubnav').hidden=activeModule!=='quality';$('routingSubnav').hidden=activeModule!=='routing';
 $('gatewaySingleCount').textContent=tasks.length;$('gatewayCompareCount').textContent=comparisons.length;
 $('qualitySingleCount').textContent=qualityTests.filter(x=>(x.resources||[]).length<=1).length;$('qualityCompareCount').textContent=qualityTests.filter(x=>(x.resources||[]).length>1).length;
 $('routingCount').textContent=routingComparisons.length;
 const quality=activeModule==='quality',routing=activeModule==='routing';
 $('crumb').textContent=routing?'固定路由与智能路由对照':quality?'质量评测':'网关压测';
 $('recordTitle').textContent=routing?'路由对照记录':quality?'评测记录':'压测记录';
 $('recordSubtitle').textContent=routing?'同一份请求清单交错打在两条路由上的黑盒对照':quality?'固定题集下的能力、格式遵循与一致性黑盒观测':'吞吐、成功率、延迟与压力窗口黑盒观测';
 $('recordCreateButton').innerHTML=uiIcon('plus')+'<span>'+(routing?'新建路由对照':quality?'新建评测':'新建压测')+'</span>';
 const emptyAction=$('empty')?.querySelector('.empty-action');
 if(emptyAction)emptyAction.innerHTML=uiIcon('plus')+'<span>'+(routing?'新建路由对照':quality?(activeView==='compare'?'新建资源对照':'新建单资源测试'):(activeView==='compare'?'新建压测对照':'新建压测'))+'</span>';document.querySelector('.empty-visual').innerHTML=uiIcon(routing?'routing':quality?'quality':'gateway');
 document.querySelector('.task-section-title').textContent=routing?'路由对照记录':quality?'评测记录':'压测记录';
}
function setModule(next){moduleMemory[activeModule]={view:activeView,selected,selectedComparison,selectedQuality,selectedRouting};activeModule=['quality','routing','fingerprint','connections','tpm'].includes(next)?next:'gateway';const memory=moduleMemory[activeModule];activeView=memory?memory.view:activeModule==='gateway'||activeModule==='fingerprint'||activeModule==='connections'?'single':'compare';autoPickView=!memory;selected=memory?.selected||null;detailTask=null;selectedComparison=memory?.selectedComparison||null;selectedQuality=memory?.selectedQuality||null;selectedRouting=memory?.selectedRouting||null;localStorage.removeItem('route_selected_task');updateWorkspaceChrome();renderWorkspace()}
function setWorkspaceView(view){setRecordFilter(view)}
function openGatewayCreate(view=activeView){activeModule='gateway';activeView=view==='compare'?'compare':'single';updateWorkspaceChrome();startNewTest(activeModule,activeView)}
function openQualityCreate(view=activeView){activeModule='quality';activeView=view==='single'?'single':'compare';updateWorkspaceChrome();startNewTest(activeModule,activeView)}
function openCreateForContext(){if(activeModule==='tpm')return window.TpmUI?.openCreate();if(activeModule==='connections')return window.ConnectionUI?.openCreate();if(activeModule==='fingerprint')return window.FingerprintUI?.openCreate();return activeModule==='routing'?openRoutingCreate():activeModule==='quality'?openQualityCreate(activeView==='compare'?'compare':'single'):openGatewayCreate(activeView==='compare'?'compare':'single')}
let routingModelRequestSeq=0;
let routingSavedConnections={},routingConnectionLoadSeq=0;
const routingConnectionTimers={},routingConnectionQueues={};
function routingTierOptions(){return routingCorpus?.length?routingCorpus.map(x=>`<option value="${esc(x.tier)}">${num(x.tier,0)} 条 / 侧 · 合计 ${num(x.tier*2,0)} 条</option>`).join(''):'<option value="">测试集尚未加载</option>'}
function renderRoutingFields(reset=false){
 const tier=$('routingTier'),previous=tier.value;
 tier.innerHTML=routingTierOptions();tier.disabled=!routingCorpus?.length;
 if([...tier.options].some(x=>x.value===previous))tier.value=previous;
 const picked=routingCorpus?.find(x=>String(x.tier)===String(tier.value));
 $('routingTierHint').textContent=picked?`${picked.note||'同一清单用于两侧配对测试。'} 两侧合计 ${num(picked.tier*2,0)} 次请求。`:'测试集未加载，暂时无法开始。请重新加载后重试。';
 $('routingCorpusRetry').hidden=Boolean(picked);
 const panels=$('routingSidePanels');
 if(reset||!panels.children.length){panels.innerHTML=routingSideTemplate('baseline')+routingSideTemplate('candidate');setRoutingSide('baseline');resetRoutingModels();renderSavedConnectionPickers();loadRoutingConnections()}
 document.querySelectorAll('#routingSideTabs .tab').forEach(x=>x.onclick=()=>setRoutingSide(x.dataset.routingTab));
 updateRoutingSideStatus();
}
function routingSideTemplate(side){
 const baseline=side==='baseline',label=baseline?'固定路由':'智能路由',prefix=`routing-${side}`,saved=routingSavedConnections[side]||{};
 const classes={name:'class="input routing-side-name"',url:'class="input routing-side-url"',key:'class="input routing-side-key"'};
 const input=(field,options)=>UI.input({id:`${prefix}-${field}`,...options}).replace('class="input"',classes[field]);
 return `<section class="quality-resource routing-resource" id="${prefix}-panel" data-routing-side="${side}" data-connection-id="${esc(saved.connection_id||'')}" data-connection-url="${esc(saved.url||'')}" data-connection-api-key-saved="${Boolean(saved.api_key_saved)}" data-api-key-saved="${Boolean(saved.api_key_saved)}" data-saved-url="${esc(saved.url||'')}" role="tabpanel" aria-labelledby="${baseline?'routingBaselineTab':'routingCandidateTab'}" ${baseline?'':'hidden'}>`+
  `<div id="connection-picker-routing-${side}">${savedConnectionPickerHtml('routing',side,saved.connection_id||'')}</div>`+
  UI.field({id:`${prefix}-name`,label:'路由名称',controlHtml:input('name',{value:saved.name||label})})+
  `<details class="routing-advanced" id="connection-details-routing-${side}" open><summary>地址与密钥</summary>`+
  UI.field({id:`${prefix}-url`,label:'网关地址',required:true,controlHtml:input('url',{type:'url',value:saved.url||'',required:true,placeholder:'https://gateway.example.com/v1'})})+
  UI.field({id:`${prefix}-key`,label:'API Key（接口密钥）',controlHtml:UI.passwordInput({id:`${prefix}-key`,action:'revealRoutingKey',args:[side],visibilityLabel:'API Key（接口密钥）',required:!saved.api_key_saved,placeholder:saved.api_key_saved?'已保存，留空即可复用':'填写该路由的密钥',describedBy:`${prefix}-credential-hint`}).replace('class="input"',classes.key),hint:saved.api_key_saved?'已自动保存密钥；留空即可复用，点击眼睛核对，输入新密钥会覆盖。':'填写后自动保存到系统凭据库；点击眼睛可核对密钥。',hintId:`${prefix}-credential-hint`})+'</details></section>';
}
function setRoutingSide(side){
 if(!['baseline','candidate'].includes(side))return;
 document.querySelectorAll('#routingSideTabs .tab').forEach(x=>{const active=x.dataset.routingTab===side;x.classList.toggle('active',active);x.setAttribute('aria-selected',String(active));x.tabIndex=active?0:-1});
 document.querySelectorAll('#routingSidePanels [data-routing-side]').forEach(x=>x.hidden=x.dataset.routingSide!==side);
}
function routingHasSavedKey(side){if(boundConnectionId('routing',side)){const profile=boundConnectionProfile('routing',side),bound=connectionBindingNode('routing',side);return bound?.dataset.connectionInvalid!=='true'&&(profile?Boolean(profile.api_key_saved):bound?.dataset.connectionApiKeySaved==='true')}const box=document.querySelector(`[data-routing-side="${side}"]`);return box?.dataset.apiKeySaved==='true'&&box.dataset.savedUrl===readRoutingSide(side).url}
function updateRoutingSideStatus(){document.querySelectorAll('#routingSideTabs .tab').forEach(tab=>{const side=readRoutingSide(tab.dataset.routingTab),ready=Boolean(side.url&&(side.api_key||routingHasSavedKey(tab.dataset.routingTab)));tab.querySelector('.routing-side-status').textContent=ready?'已填写':'待填写';tab.classList.toggle('is-complete',ready)})}
function readRoutingSide(mode){const box=document.querySelector(`[data-routing-side="${mode}"]`),key=box?.querySelector('.routing-side-key');return{name:(box?.querySelector('.routing-side-name')?.value||'').trim()||(mode==='baseline'?'固定路由':'智能路由'),url:(box?.querySelector('.routing-side-url')?.value||'').trim(),api_key:key?.dataset?.routingRevealed==='true'?'':(key?.value||'').trim(),...(boundConnectionId('routing',mode)?{connection_id:boundConnectionId('routing',mode)}:{})}}
function setRoutingKeyVisibility(side,visible){
 const box=document.querySelector(`[data-routing-side="${side}"]`),input=box?.querySelector('.routing-side-key'),button=box?.querySelector('[data-click-action="revealRoutingKey"]');if(!input||!button)return;
 input.type=visible?'text':'password';
 const label=`${visible?'隐藏':'显示'}API Key（接口密钥）`;
 button.setAttribute('aria-pressed',String(Boolean(visible)));button.setAttribute('aria-label',label);button.title=label;button.innerHTML=UI.icon(visible?'eyeOff':'eye');
}
function hideRoutingKey(side,clear=false){
 const box=document.querySelector(`[data-routing-side="${side}"]`),input=box?.querySelector('.routing-side-key'),button=box?.querySelector('[data-click-action="revealRoutingKey"]');if(!input)return;
 box.dataset.keyRevealSeq=String(Number(box.dataset.keyRevealSeq||0)+1);
 if(clear||input.dataset.routingRevealed==='true')input.value='';
 delete input.dataset.routingRevealed;setRoutingKeyVisibility(side,false);
 if(button){button.disabled=false;button.removeAttribute('aria-busy')}
}
async function revealRoutingKey(side,button){
 if(!['baseline','candidate'].includes(side))return;
 const box=document.querySelector(`[data-routing-side="${side}"]`),input=box?.querySelector('.routing-side-key');if(!input||!button)return;
 if(input.type==='text'){hideRoutingKey(side);return}
 if(input.value.trim()){setRoutingKeyVisibility(side,true);return}
 const {url,connection_id}=readRoutingSide(side);if(!url){toast('请先填写该路由的地址和密钥，再点击眼睛核对。');return}
 const seq=String(Number(box.dataset.keyRevealSeq||0)+1);box.dataset.keyRevealSeq=seq;
 button.disabled=true;button.setAttribute('aria-busy','true');
 try{
  if(!routingHasSavedKey(side)){
   await checkRoutingCredential(side);
   if(!input.isConnected||box.dataset.keyRevealSeq!==seq||readRoutingSide(side).url!==url||readRoutingSide(side).connection_id!==connection_id||input.value.trim())return;
   if(!routingHasSavedKey(side))throw new Error('请先填写该路由的密钥，再点击眼睛核对。');
  }
  const data=await api(connection_id?'/connections/'+encodeURIComponent(connection_id)+'/reveal':'/routing-config/reveal',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(connection_id?{}:{side,url})});
  if(!input.isConnected||box.dataset.keyRevealSeq!==seq||readRoutingSide(side).url!==url||readRoutingSide(side).connection_id!==connection_id||input.value.trim())return;
  if(typeof data.api_key!=='string'||!data.api_key.trim())throw new Error('该路由没有可核对的已保存密钥，请重新填写。');
  // Revealing is a transient view, not an edit: neither autosave nor submission resends the secret.
  input.value=data.api_key;input.dataset.routingRevealed='true';setRoutingKeyVisibility(side,true);
 }finally{if(box.dataset.keyRevealSeq===seq){button.disabled=false;button.removeAttribute('aria-busy')}}
}
function setRoutingCredentialState(side,url,saved,available=true,message=''){
 const box=document.querySelector(`[data-routing-side="${side}"]`);if(!box||readRoutingSide(side).url!==url)return;
 if(boundConnectionId('routing',side)){const profile=boundConnectionProfile('routing',side);saved=box.dataset.connectionInvalid!=='true'&&(profile?Boolean(profile.api_key_saved):box.dataset.connectionApiKeySaved==='true');available=true;message=box.dataset.connectionInvalid==='true'?'常用连接已删除或地址已更新，请重新选择；当前绑定不会改用其他密钥。':saved?'已选择常用连接；密钥留空即可复用，点击眼睛可核对。修改地址或密钥将切换为手动填写。':'该常用连接的密钥不可用；请填写密钥后另存或更新。'}
 box.dataset.apiKeySaved=String(Boolean(saved));box.dataset.savedUrl=url;
 const key=box.querySelector('.routing-side-key'),hint=$(`routing-${side}-credential-hint`);
 key.required=!saved;key.placeholder=saved?'已保存，留空即可复用':'填写该路由的密钥';
 if(hint)hint.textContent=message||(!available?'地址已自动保存；系统凭据库不可用，密钥仅供本次使用，下次需重新填写。':saved?'已自动保存地址和密钥；留空即可复用，点击眼睛核对，输入新值会覆盖。':'地址已自动保存；请输入密钥，填写后自动保存到系统凭据库。');
 updateRoutingSideStatus();
}
async function loadRoutingConnections(){
 const seq=++routingConnectionLoadSeq;
 try{await Promise.allSettled(Object.values(routingConnectionQueues));const data=await api('/routing-config');if(seq!==routingConnectionLoadSeq)return;
  for(const side of ['baseline','candidate']){
   const box=document.querySelector(`[data-routing-side="${side}"]`);if(!box)continue;
   const saved=data.config?.[side]||{};routingSavedConnections[side]={name:saved.name||'',url:saved.url||'',api_key_saved:Boolean(saved.api_key_saved),connection_id:saved.connection_id||''};
   const url=box.querySelector('.routing-side-url'),name=box.querySelector('.routing-side-name'),key=box.querySelector('.routing-side-key');
   if(url.dataset.routingEdited!=='true')url.value=saved.url||'';
   if(saved.name&&name.dataset.routingEdited!=='true')name.value=saved.name;
   if(url.dataset.routingEdited!=='true'&&key.dataset.routingEdited!=='true'&&box.dataset.connectionUserEdited!=='true'){
    box.dataset.connectionId=saved.connection_id||'';box.dataset.connectionUrl=saved.url||'';box.dataset.connectionApiKeySaved=String(Boolean(saved.api_key_saved));
   }
   box.dataset.connectionLoaded='true';
   setRoutingCredentialState(side,saved.url||'',Boolean(saved.api_key_saved),data.storage_available!==false);
   if(box.dataset.connectionEdited==='true'){clearTimeout(routingConnectionTimers[side]);routingConnectionTimers[side]=setTimeout(()=>saveRoutingSide(side),0)}
  }
  renderSavedConnectionPickers();
 }catch{if(seq===routingConnectionLoadSeq)for(const side of ['baseline','candidate']){const hint=$(`routing-${side}-credential-hint`);if(hint)hint.textContent='暂时无法读取已保存连接；可填写后继续测试。'}}
}
async function saveRoutingSide(side,connection=readRoutingSide(side)){
 const box=document.querySelector(`[data-routing-side="${side}"]`),input=box?.querySelector('.routing-side-url');
 if(!input||(!connection.url&&connection.api_key)||(connection.url&&!input.checkValidity()))return;
 // An untouched empty address is an unresolved restore, never a request to erase a saved connection.
 if(!connection.url&&box.dataset.connectionLoaded!=='true'&&input.dataset.routingEdited!=='true')return;
 const previous=routingConnectionQueues[side]||Promise.resolve();
 const request=previous.catch(()=>{}).then(()=>api('/routing-config',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({side,...connection})}));
 routingConnectionQueues[side]=request;
 try{const data=await request;
  routingSavedConnections[side]={name:connection.name,url:connection.url,api_key_saved:Boolean(data.saved),connection_id:connection.connection_id||''};
  const current=readRoutingSide(side);if(current.url!==connection.url||current.api_key!==connection.api_key||current.connection_id!==connection.connection_id)return;
  setRoutingCredentialState(side,connection.url,Boolean(data.saved),data.storage_available!==false,connection.api_key&&!data.saved&&data.storage_available!==false?'地址已自动保存；密钥保存失败，本次仍可测试，下次需重新填写或重试。':'');
 }catch{const current=readRoutingSide(side);if(current.url===connection.url&&current.api_key===connection.api_key&&current.connection_id===connection.connection_id){const hint=$(`routing-${side}-credential-hint`);if(hint)hint.textContent='连接自动保存失败；本次仍可测试，下次可能需重新填写。'}}
}
function saveRoutingConnections(){for(const side of ['baseline','candidate']){clearTimeout(routingConnectionTimers[side]);if(document.querySelector(`[data-routing-side="${side}"]`)?.dataset.connectionEdited==='true')saveRoutingSide(side)}}
async function checkRoutingCredential(side){
 const box=document.querySelector(`[data-routing-side="${side}"]`);if(!box)return;
 if(boundConnectionId('routing',side)){updateBoundConnectionHint('routing',side);return}
 const url=readRoutingSide(side).url,seq=String(Number(box.dataset.credentialSeq||0)+1);box.dataset.credentialSeq=seq;
 setRoutingCredentialState(side,url,false);
 if(!url)return;
 try{const data=await api('/credential-status?kind=routing&side='+side+'&url='+encodeURIComponent(url));if(box.dataset.credentialSeq!==seq||readRoutingSide(side).api_key||boundConnectionId('routing',side))return;setRoutingCredentialState(side,url,Boolean(data.saved),data.storage_available!==false)}catch{}
}
function currentRoutingModel(){return String($($('routingModelManualWrap').hidden?'routingModel':'routingModelManual').value||'').trim()}
function setRoutingModelMode(manual,focus=false){
 $('routingModelRow').hidden=manual;
 $('routingModelHint').hidden=manual;
 const label=document.querySelector('label[for="routingModel"]');if(label)label.hidden=manual;
 const fallback=$('routingManualModelToggle').closest('.model-fallback');
 if(fallback)(manual?$('routingModelManualWrap'):$('routingModelHint')).after(fallback);
 $('routingModelManualWrap').hidden=!manual;
 $('routingModelManual').disabled=!manual;$('routingModelManual').required=manual;
 $('routingModel').disabled=manual||![...$('routingModel').options].some(option=>option.value);
 $('routingModel').required=!manual;
 const toggle=$('routingManualModelToggle');
 toggle.setAttribute('aria-expanded',String(manual));
 toggle.textContent=manual?'返回模型列表选择':'无法获取列表？手动指定模型';
 if(focus)$(manual?'routingModelManual':$('routingModel').disabled?'loadRoutingModels':'routingModel').focus();
 schedulePreflight();
}
function resetRoutingModels(message='确认两侧已保存的连接或填写新连接后，点击“获取模型”，选择两侧共同支持的模型。'){
 routingModelRequestSeq++;
 $('routingModel').innerHTML='<option value="">请先获取两侧模型列表</option>';
 $('routingModel').value='';$('routingModelManual').value='';
 $('routingModelRow').className='model-row';$('routingModelRow').setAttribute('aria-busy','false');
 $('loadRoutingModels').disabled=false;$('loadRoutingModels').textContent='获取模型';
 $('routingManualModelToggle').disabled=false;
 $('routingModelHint').textContent=message;
 setRoutingModelMode(false);
}
async function refreshRoutingModels(){
 const button=$('loadRoutingModels');if(button.disabled)return;
 for(const side of ['baseline','candidate']){
  for(const field of ['url','key']){
   const input=$(`routing-${side}-${field}`);
   if(field==='key'&&routingHasSavedKey(side))continue;
   if(input.value.trim()&&input.checkValidity())continue;
   setRoutingSide(side);input.focus();input.reportValidity();
   const message=`请先填写${side==='baseline'?'固定路由':'智能路由'}的${field==='url'?'有效网关地址':'接口密钥'}。`;
   $('routingModelHint').textContent=message;toast(message);return;
  }
 }
 const seq=++routingModelRequestSeq,previous=currentRoutingModel(),row=$('routingModelRow'),previousOptions=$('routingModel').innerHTML,hadOptions=[...$('routingModel').options].some(option=>option.value);
 setRoutingModelMode(false);$('routingModel').value='';$('routingModel').disabled=true;
 $('routingManualModelToggle').disabled=true;
 button.disabled=true;button.textContent='获取中…';row.className='model-row is-loading';row.setAttribute('aria-busy','true');
 $('routingModelHint').textContent='正在获取固定路由和智能路由的模型列表…';
 const sides=['baseline','candidate'],labels=['固定路由','智能路由'];
 const results=await Promise.allSettled(sides.map(async side=>{
  const {url,api_key,connection_id}=readRoutingSide(side);
  const data=await api('/routing-models',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({side,url,api_key,...(connection_id?{connection_id}:{})}),timeout:35000});
  if(data.ok===false||!Array.isArray(data.models))throw new Error(data.error||'模型列表格式无效');
  if(seq===routingModelRequestSeq)setRoutingCredentialState(side,url,Boolean(data.saved),data.storage_available!==false);
  return [...new Set(data.models.filter(model=>typeof model==='string'&&model.trim()))].sort();
 }));
 // A connection edit, closed drawer or new draft invalidates both responses.
 if(seq!==routingModelRequestSeq)return;
 const failed=results.some(result=>result.status==='rejected');
 const candidateModels=new Set(results[1].status==='fulfilled'?results[1].value:[]);
 const common=failed?[]:results[0].value.filter(model=>candidateModels.has(model));
 $('routingModel').innerHTML=`<option value="">${failed?'获取失败，请重试':common.length?'请选择共同模型':'没有共同模型'}</option>`+common.map(model=>`<option value="${esc(model)}">${esc(model)}</option>`).join('');
 if(failed&&hadOptions){$('routingModel').innerHTML=previousOptions;$('routingModel').value=previous}else if(common.includes(previous))$('routingModel').value=previous;
 const summary=results.map((result,index)=>result.status==='fulfilled'?`${labels[index]} ${result.value.length} 个模型`:`${labels[index]}获取失败：${result.reason?.message||'请重试'}`).join('；');
 $('routingModelHint').textContent=summary+(failed?(hadOptions?'。保留上次共同模型列表与选择，本次刷新未验证；可重试或手动指定。':'。请检查失败一侧的连接后重试；网关不提供列表时可手动指定。'):common.length?`；共同支持 ${common.length} 个。两侧将使用同一个模型名称。`:'；没有同名的共同模型，请检查连接或模型权限。');
 row.className=`model-row ${failed||!common.length?'is-error':'is-done'}`;row.setAttribute('aria-busy','false');
 button.disabled=false;button.textContent='刷新模型';$('routingManualModelToggle').disabled=false;
 setRoutingModelMode(false);
}
$('loadRoutingModels').onclick=refreshRoutingModels;
$('routingManualModelToggle').onclick=()=>setRoutingModelMode($('routingModelManualWrap').hidden,true);
function readRouting(){
 const v=id=>$(id)?.value,shared={model:currentRoutingModel(),concurrency:Number(v('routingConcurrency')),rpm:Number(v('routingRpm')),stream:true,profile:'smoke',connect_timeout:Number(v('routingConnectTimeout')),read_timeout:Number(v('routingReadTimeout')),write_timeout:30,include_usage:true,capture_raw:Boolean($('routingCapture').checked)};
 return{name:String(v('routingName')||'').trim()||'路由对照',tier:Number(v('routingTier')),rpm:shared.rpm,baseline:{...shared,...readRoutingSide('baseline')},candidate:{...shared,...readRoutingSide('candidate')}};
}
function validateRoutingForm(){
 if(!routingCorpus?.length)throw new Error('测试集尚未加载，请点击“重新加载测试集”。');
 for(const input of $('routingFields').querySelectorAll('input[required],select[required]')){
  if(input.disabled)continue;
  if(input.checkValidity()&&(!['text','password','url'].includes(input.type)||input.value.trim()))continue;
  const side=input.closest('[data-routing-side]')?.dataset.routingSide;if(side)setRoutingSide(side);
  const details=input.closest('details');if(details)details.open=true;
  input.focus();input.reportValidity();
  const label=$('routingFields').querySelector(`label[for="${input.id}"]`)?.textContent.replace('必填','').trim()||'配置';
  throw new Error(`${side==='baseline'?'固定路由：':side==='candidate'?'智能路由：':''}请检查${label}。`);
 }
 if($('loadRoutingModels').disabled)throw new Error('正在获取模型列表，请等待完成后选择共同模型。');
 if(!currentRoutingModel()){$('loadRoutingModels').focus();throw new Error('请先获取模型列表并选择共同模型；网关不提供列表时可手动指定。')}
}
function openRoutingCreate(){activeModule='routing';activeView='compare';updateWorkspaceChrome();startNewTest('routing','compare')}
function mixedGatewayRecords(){return [...tasks.map(x=>({...x,record_type:'single'})),...comparisons.map(x=>({...x,record_type:'compare'}))].sort((a,b)=>(b.created||0)-(a.created||0))}
function filteredGatewayRecords(){
 if(activeView!=='single'&&activeView!=='compare')return [];
 // Non-numeric dates make the legacy comparator inconsistent; retain its mixed-list order for those records.
 if(!tasks.every(x=>Number.isFinite(Number(x.created||0)))||!comparisons.every(x=>Number.isFinite(Number(x.created||0)))){
  return mixedGatewayRecords().filter(x=>x.record_type===activeView);
 }
 const records=activeView==='single'?tasks:comparisons;
 return records.map(x=>({...x,record_type:activeView})).sort((a,b)=>(b.created||0)-(a.created||0));
}
function filteredQualityRecords(){return qualityTests.filter(x=>activeView==='compare'?(x.resources||[]).length>1:(x.resources||[]).length<=1)}
function pickRecordView(single,compare){if(!autoPickView||!recordsLoaded)return;autoPickView=false;if(activeView==='single'&&!single&&compare)activeView='compare';else if(activeView==='compare'&&!compare&&single)activeView='single';updateWorkspaceChrome()}
function renderWorkspace(){
 if(activeModule==='tpm'){updateWorkspaceChrome();window.TpmUI?.render();return}
 if(activeModule==='connections'){updateWorkspaceChrome();window.ConnectionUI?.render();return}
 if(activeModule==='fingerprint'){updateWorkspaceChrome();window.FingerprintUI?.render();return}
 updateWorkspaceChrome();$('loadingState').hidden=true;$('empty').hidden=true;$('detail').hidden=true;$('modulePlaceholder').hidden=true;
 $('gatewayList').hidden=activeModule!=='gateway';$('qualityList').hidden=activeModule!=='quality';$('routingList').hidden=activeModule!=='routing';
 if(activeModule==='routing'){
   let run=selectedRouting&&routingComparisons.find(x=>x.id===selectedRouting);
   if(!run&&routingComparisons.length){run=routingComparisons[0];selectedRouting=run.id;routingTab='report'}
   updateRoutingList();
   if(run){$('modulePlaceholder').hidden=false;renderRoutingDetail(run);return}
   renderWorkspaceEmpty('当前暂无路由对照记录','创建一次路由对照后，智能路由相对固定路由的提升与下降都会出现在这里。','openRoutingCreate()');
   return;
 }
 if(activeModule==='gateway'){
   let current=detailTask||(selected&&tasks.find(x=>x.id===selected)),comparison=selectedComparison&&comparisons.find(x=>x.id===selectedComparison);
   if(!current&&!comparison){pickRecordView(tasks.length,comparisons.length);const first=filteredGatewayRecords()[0];if(first?.record_type==='compare'){comparison=comparisons.find(x=>x.id===first.id);selectedComparison=first.id;comparisonTab='report'}else if(first){current=tasks.find(x=>x.id===first.id);selected=first.id}}
   updateTaskList();
   if(current){render(current);return} if(comparison){$('modulePlaceholder').hidden=false;renderComparisonDetail(comparison);return}
   showEmptyForWorkspace();
   return;
 }
 let quality=selectedQuality&&qualityTests.find(x=>x.id===selectedQuality);
 if(!quality){pickRecordView(qualityTests.filter(x=>(x.resources||[]).length<=1).length,qualityTests.filter(x=>(x.resources||[]).length>1).length);quality=filteredQualityRecords()[0];if(quality){selectedQuality=quality.id;qualityTab='report'}}
 updateQualityList();
 if(quality){$('modulePlaceholder').hidden=false;renderQualityDetail(quality);return}
 showEmptyForWorkspace();
}
function renderModule(){renderWorkspace()}
async function selectComparison(id){comparisonTab='report';selectedComparison=id;selected=null;detailTask=null;selectedQuality=null;activeModule='gateway';updateWorkspaceChrome();updateTaskList();const c=comparisons.find(x=>x.id===id);if(c){if(c._summary){showLoading();return refresh()}$('modulePlaceholder').hidden=false;renderComparisonDetail(c)}}
async function selectQuality(id){if(selectedQuality!==id)qualityTab='report';selectedQuality=id;selected=null;detailTask=null;selectedComparison=null;activeModule='quality';updateWorkspaceChrome();updateQualityList();const q=qualityTests.find(x=>x.id===id);if(q){if(q._summary){showLoading();return refresh()}$('modulePlaceholder').hidden=false;renderQualityDetail(q)}}
let comparisonTab='report', qualityTab='report', routingComparisons=[], routingCorpus=null, routingMetrics=[], routingTab='report';
// 每个模块记住上次查看的筛选与记录；首次进入时按“有记录的第一个筛选 → 最新一条”默认选中，不停在空状态。
let recordsLoaded=false, autoPickView=true; const moduleMemory={};
function setComparisonTab(id,tab){comparisonTab=tab;const c=comparisons.find(x=>x.id===id);if(c)renderComparisonDetail(c)}
function setQualityTab(id,tab){qualityTab=tab;const q=qualityTests.find(x=>x.id===id);if(q)renderQualityDetail(q)}
function runHeaderAction(kind,record,stopAction,stopLabel){
 if(record.status==='stopping')return UI.button('正在收尾…',{disabled:true,title:'已停止安排后续请求，正在等待已发送请求结束并保存。'});
 if(['waiting','running'].includes(record.status))return UI.button(stopLabel,{variant:'danger',action:stopAction,args:[record.id]});
 return window.RepeatUI?.button(kind,record)||'';
}
function requestLatencyHtml(record,key,{total=record.total_ms,first=record.ttft_ms}={}){
 return `<b>${UI.esc(UI.formatDurationMs(total))}</b><small class="role-label">首字 ${UI.esc(UI.formatDurationMs(first))}</small>${UI.timeRecords(record,{request:true,key})}`;
}
function routingStatusLabel(status){return {draft:'草稿',stopped:'已停止',stopping:'正在停止',running:'运行中',finished:'已完成',error:'失败',waiting:'排队中'}[status]||status||'准备中'}
async function selectRouting(id){if(selectedRouting!==id)routingTab='report';selectedRouting=id;selected=null;detailTask=null;selectedComparison=null;selectedQuality=null;activeModule='routing';activeView='compare';updateWorkspaceChrome();updateRoutingList();const run=routingComparisons.find(x=>x.id===id);if(run){if(run._summary){showLoading();return refresh()}$('modulePlaceholder').hidden=false;renderRoutingDetail(run)}}
function setRoutingTab(id,tab){routingTab=tab;const run=routingComparisons.find(x=>x.id===id);if(run)renderRoutingDetail(run)}
async function stopRouting(id){if(!confirm('停止后不再发出后续请求；已经开始的请求会完成并写入结果。部分结果不会生成完整结论。继续？'))return;try{await api('/routing-comparisons?stop='+encodeURIComponent(id),{method:'POST'});toast('已请求停止路由对照');refresh()}catch(e){toast(e.message)}}
async function deleteRouting(id){if(!confirm('确认删除这条路由对照记录？记录和两侧的结果文件会一起删除，删除后无法恢复。'))return;try{await api('/routing-comparisons?delete='+encodeURIComponent(id),{method:'POST'});selectedRouting=null;toast('路由对照记录已删除');refresh()}catch(e){toast(e.message)}}
function routingItem(run){const pct=run.status==='finished'?100:Math.round(Math.max(0,Math.min(1,Number(run.progress?.pct)||0))*100);const report=run.report||{},tier=Number(run.tier||0),successDelta=report.success?report.success.delta_pct:null;return `<button class="task comparison-record ${run.id===selectedRouting?'sel':''}" data-click-action="selectRouting" data-arg0="${esc(run.id)}"><div class="task-top"><div class="task-name">${esc(run.name)}</div><span class="status ${esc(run.status)}">${routingStatusLabel(run.status)}</span></div><div class="task-meta"><span class="record-type comparison-type">路由对照</span> · ${num(tier,0)} 条 × 2 侧 · ${num(run.progress?.done,0)}/${num(Number(tier)*2,0)} 已完成</div><div class="task-foot"><div class="mini-track"><i style="width:${pct}%"></i></div><span class="task-pct">${run.status==='finished'?(successDelta==null?'完成':`成功率差 ${signedMetric(successDelta)} 个百分点`):`${pct}%`}</span></div></button>`}
function updateRoutingList(){const list=$('routingList');if(!list)return;list.innerHTML=routingComparisons.length?routingComparisons.map(routingItem).join(''):listEmpty('暂无路由对照记录','创建一次路由对照后，同一份清单交错打在两条路由上的差别会出现在这里。','openRoutingCreate()')}
function routingRateCell(side,unit='命中/成功'){if(!side)return '<span class="metric-value">—</span>';const ci=side.ci_low_pct==null?'':`<small>95% 置信区间 ${num(side.ci_low_pct)}% ~ ${num(side.ci_high_pct)}%</small>`;return `<span class="metric-value">${comparisonMetric(side.pct)}%<small>${unit} ${num(side.successes,0)} / ${num(side.total,0)}</small></span>${ci}`}
function routingAttribution(block,label){
 if(!block)return '';
 const attribution={attributable:'可归因于路由',not_attributable:'不足以归因于路由',insufficient_evidence:'证据不足'}[block.attribution]||'无法判定';
 const delta=block.delta_pct==null?'差值未确定':`差值 ${signedMetric(block.delta_pct,' 个百分点')}`;
 const boundary=block.ci_overlap===true?'95% 置信区间重叠':block.ci_overlap===false?'95% 置信区间不重叠':'置信区间证据未完整记录';
 const probability=block.p_value==null?'精确检验未给出结果':`精确检验 p 值 ${String(block.p_value)}`;
 const noSamples=block.baseline?.total===0||block.candidate?.total===0;
 const detail=noSamples?(label==='缓存命中率'?'至少一侧缺少可统计用量，命中率不可比。':'至少一侧没有可用样本，成功率不可比。'):`${delta}；${boundary}；${probability}。`;
 return UI.signal({title:`${label}：${attribution}`,detail,tone:block.attribution==='attributable'?(Number(block.delta_pct)<0?'danger':'success'):'neutral'});
}
function routingObservedReport(run){return run.report||run.live_report||{}}
function routingStability(run,key){const report=routingObservedReport(run);return report[key]?.stability||report.stability?.[key]||run.derived_report?.stability?.[key]||report[key]||{}}
function routingStabilityNotice(run,side){const keys=side?[side]:['baseline','candidate'],notes=[];if(run.derived_report?.stability?.source==='result_files')notes.push('这条旧记录的新指标根据原始结果文件补算。');for(const key of keys){const stat=routingStability(run,key),name=run.sides?.[key]?.name||(key==='baseline'?'固定路由':'智能路由');if(stat.reason)notes.push(`${name}：${stat.reason}`);if(stat.unordered_requests||stat.excluded_duplicate_requests)notes.push(`${name}：仅统计可还原顺序且编号唯一的请求；无法排序 ${num(stat.unordered_requests,0)} 条，重复编号 ${num(stat.excluded_duplicate_requests,0)} 条。`)}return notes.length?UI.notice(notes.join(' '),{tone:'warning'}):''}
function routingMetricTable(run){
 const report=routingObservedReport(run),success=report.success||{},cache=report.cache||{},base=report.baseline||{},cand=report.candidate||{},partial=run.status!=='finished';
 const names={baseline:run.sides?.baseline?.name||'固定路由',candidate:run.sides?.candidate?.name||'智能路由'};
 const value=(v,unit)=>v==null?'—':`${comparisonMetric(v)}${unit}`;
 const bs=routingStability(run,'baseline'),cs=routingStability(run,'candidate'),difference=field=>bs[field]==null||cs[field]==null?null:cs[field]-bs[field];
 const rows=[['成功率',value(base.success?.pct,'%'),value(cand.success?.pct,'%'),success.delta_pct,false,' 个百分点'],['缓存命中率',value(base.cache?.pct,'%'),value(cand.cache?.pct,'%'),cache.delta_pct,false,' 个百分点'],['首字耗时 · 第95百分位',value(base.ttft_p95_ms,' 毫秒'),value(cand.ttft_p95_ms,' 毫秒'),report.ttft?.delta_pct,true,'%'],['总耗时 · 第95百分位',value(base.total_p95_ms,' 毫秒'),value(cand.total_p95_ms,' 毫秒'),report.total_latency?.delta_pct,true,'%'],['最大连续失败次数',value(bs.max_consecutive_failures,' 次'),value(cs.max_consecutive_failures,' 次'),difference('max_consecutive_failures'),true,' 次'],['最大平滑首字耗时',value(bs.ttft_ewma_max_ms,' 毫秒'),value(cs.ttft_ewma_max_ms,' 毫秒'),difference('ttft_ewma_max_ms'),true,' 毫秒']];
 const sideLabel=(key,label)=>names[key]===label?label:`${names[key]}（${label}）`;
 const table=UI.table({columns:[{label:'指标'},{label:sideLabel('baseline','固定路由')},{label:sideLabel('candidate','智能路由')},{label:'差值'}],rowsHtml:rows.map(([label,a,b,delta,lower,unit])=>[UI.esc(label),a,b,pctDelta(delta,unit,lower)])}).replace('class="report-table"','class="report-table comparison-result-table"');
 const counts=`${names.baseline}已返回 ${num(base.requests,0)} 条（成功 ${num(base.success?.successes,0)}）；${names.candidate}已返回 ${num(cand.requests,0)} 条（成功 ${num(cand.success?.successes,0)}）。`;
 const usage=`缓存可统计样本：${names.baseline} ${num(base.cache?.total,0)} 条，${names.candidate} ${num(cand.cache?.total,0)} 条。`+((cache.baseline_no_usage||cache.candidate_no_usage)?` 缺少用量已剔除：${num(cache.baseline_no_usage,0)} / ${num(cache.candidate_no_usage,0)} 条。`:'');
 const definitions=UI.disclosure({id:'routing-metric-method-'+run.id,title:'指标口径：差值、连续失败与平滑首字耗时',bodyHtml:UI.methodCards([{title:'差值与统计分母',lines:['差值为智能路由减固定路由；成功率和命中率使用百分点，延迟使用相对百分比，连续失败和平滑耗时使用次数、毫秒。','成功率以本侧已返回请求为分母；缓存命中率只纳入可读取用量的请求。']},{title:'连续失败口径',lines:['按请求编号排序；成功、未返回请求或缺失编号均打断连续失败。过程数据随请求返回更新。']},{title:'平滑首字耗时口径',lines:['TTFT（首字耗时）采用 EWMA（指数加权移动平均）平滑；p95（第95百分位）表示 95% 的有效样本不超过该值。','每连续 5 次完整成功且有有效首字耗时的请求组成滑动窗口；首值初始化，之后按 0.5 × 当前值 + 0.5 × 前值递推，取各窗口最终值的最大值。','失败、缺失耗时或缺失编号打断窗口；不足 5 次显示“—”。']}])});
 return UI.section({title:partial?'两侧实时指标':'两侧指标对照',description:counts+(partial?' 当前是过程数据，尚不作路由归因。':' 差值为智能路由减固定路由。'),badgeHtml:UI.badge(partial?'约每 1.5 秒更新':'完整执行窗口'),bodyHtml:table+`<p class="report-note">${UI.esc(usage)}</p>`+routingStabilityNotice(run)+definitions});
}
function routingDiagnosticsHtml(run,sideKey){
 const observed=routingObservedReport(run),keys=sideKey?[sideKey]:['baseline','candidate'],names=keys.map(key=>run.sides?.[key]?.name||(key==='baseline'?'固定路由':'智能路由'));
 const hasData=keys.some(key=>observed[key]?.diagnostics);
 const explanation=UI.notice('本工具只把完整结束的 2xx（成功状态码）响应计为成功。429（请求限流）可能在网关入口就被拒绝；上游后台如果只统计到达上游的请求，成功率会更高。请核对相同时段、模型与请求范围。');
 if(!hasData)return UI.section({title:'失败与响应完整性',bodyHtml:UI.notice(keys.some(key=>Number(observed[key]?.requests)>0)?'未记录失败分类，请结合单侧逐请求结果核查。':'尚无返回结果，暂不能判断失败情况。')+UI.disclosure({title:'成功率判定说明',id:'routing-diagnostics-'+run.id+'-'+(sideKey||'both'),bodyHtml:explanation})});
 const labels={HTTP_429:'429（请求限流）',TIMEOUT:'请求超时',PROTOCOL:'响应协议或完整性错误',HTTP_4XX:'其他客户端状态码失败',HTTP_5XX:'服务器状态码失败',HTTP_OTHER:'其他状态码失败',NETWORK:'连接或传输错误',UNKNOWN:'其他失败'};
 const categories=[...new Set(keys.flatMap(key=>Object.keys(observed[key]?.diagnostics?.failure_types||{})))];
 const rows=[['收到 2xx（成功状态码）',...keys.map(key=>num(observed[key]?.diagnostics?.http_2xx,0))],['完整 2xx（成功状态码）',...keys.map(key=>num(observed[key]?.diagnostics?.complete_2xx,0))],['未完整结束的 2xx（成功状态码）',...keys.map(key=>num(observed[key]?.diagnostics?.incomplete_2xx,0))],['失败合计',...keys.map(key=>num(observed[key]?.diagnostics?.failures,0))],...categories.map(type=>[labels[type]||'其他失败',...keys.map(key=>num(observed[key]?.diagnostics?.failure_types?.[type]||0,0))])];
 const codes=keys.map((key,i)=>({title:`${names[i]} · 状态码分布`,lines:[Object.entries(observed[key]?.diagnostics?.status_codes||{}).map(([code,count])=>`${code==='no_status'?'未收到状态码':code}：${num(count,0)} 条`).join('；')||'尚无返回结果']}));
 const findings=keys.flatMap((key,index)=>{const diag=observed[key]?.diagnostics||{},failures=Number(diag.failures||0),incomplete=Number(diag.incomplete_2xx||0);if(!failures&&!incomplete)return [];const types=Object.entries(diag.failure_types||{}).filter(([,count])=>Number(count)>0).map(([type,count])=>`${labels[type]||'其他失败'} ${num(count,0)} 条`);if(incomplete)types.push(`未完整结束的 2xx（成功状态码）${num(incomplete,0)} 条`);return [{title:`${names[index]}：${num(failures,0)} 条失败`,detail:types.join('；')||'失败分类未记录，请查看逐请求结果。',tone:'warning'}]});
 const known=keys.every(key=>observed[key]?.diagnostics?.failures!=null),returned=keys.reduce((sum,key)=>sum+Number(observed[key]?.requests||0),0);
 const summary=findings.length?UI.signalList(findings):UI.notice(!returned?'尚无返回结果，暂不能判断失败情况。':known?'当前已返回结果中未记录失败。':'失败计数未完整记录，请结合逐请求结果核查。');
 return UI.section({title:'失败与响应完整性',description:'失败原因保持可见；进入固定路由或智能路由标签可逐条核查。',bodyHtml:summary+UI.disclosure({title:'查看状态码明细与成功判定口径',id:'routing-diagnostics-'+run.id+'-'+(sideKey||'both'),bodyHtml:UI.table({columns:[{label:'观测项'},...names.map(label=>({label}))],rowsHtml:rows.map(row=>row.map(UI.esc))})+UI.methodCards(codes)+explanation})});
}
function routingReliabilityHtml(run){
 const report=run.report||{},rel=report.reliability||{},mcnemar=rel.mcnemar||{},support=report.success?.mcnemar_supports;
 const supportText=support===true?'配对检验支持两侧成功/失败存在差异。':support===false?'配对检验不支持差异，不能排除抽样波动。':'配对样本不足 20 条或无成功/失败不一致的请求，不做配对显著性判断。';
 const ci=side=>side?.ci_low_pct==null?'未记录':`${num(side.ci_low_pct)}% ~ ${num(side.ci_high_pct)}%`;
 const evidence=UI.table({columns:['检验指标','固定路由 95% 置信区间','智能路由 95% 置信区间','Fisher（费舍尔）检验 p 值'].map(label=>({label})),rowsHtml:[['成功率',report.success],['缓存命中率',report.cache]].map(([title,item])=>[UI.esc(title),UI.esc(ci(item?.baseline)),UI.esc(ci(item?.candidate)),UI.esc(item?.p_value==null?'未记录':String(item.p_value))])});
 const paired=UI.methodCards([{title:'成功率判定依据',lines:[report.success?.note||'没有记录完整归因说明。']},{title:'缓存命中率判定依据',lines:[report.cache?.note||'没有记录完整归因说明。']},{title:'配对方式',lines:['按 item_id（请求标识）一一配对；只有一侧有结果的请求整条剔除，绝不当作另一侧失败。',`已配对 ${num(rel.paired_total,0)} 对；剔除 ${num(rel.unpaired_count,0)} 条。`,rel.note||'']},{title:'McNemar（配对一致性）精确检验',lines:[`不一致对数 ${num(mcnemar.discordant,0)}；p 值 ${mcnemar.p_value==null?'未记录':String(mcnemar.p_value)}`,supportText]},{title:'执行方式',lines:['同一请求的两侧同时到期，奇偶交替决定谁先入队。','置信区间使用 Wilson（威尔逊）95% 区间；结合独立比例检验与配对检验查看证据。']}]);
 const unmatched=(rel.unpaired_item_ids||[]).length?`<p class="report-note">未配对示例：${(rel.unpaired_item_ids||[]).slice(0,5).map(esc).join('、')}${rel.unpaired_count>5?' 等':''}</p>`:'';
 return UI.disclosure({title:'统计证据：置信区间、精确检验与配对明细',section:true,id:'routing-statistics-'+run.id,bodyHtml:evidence+paired+unmatched});
}
function routingMethodHtml(run){
 const report=run.report||{},manifest=run.corpus||{};
 return UI.disclosure({title:'测试方法：请求清单、缓存隔离与适用范围',section:true,id:'routing-method-'+run.id,bodyHtml:UI.methodCards([{title:'请求清单',lines:[`${num(run.tier,0)} 条请求 · ${num(manifest.burst_count,0)} 段 · 每 ${num(manifest.burst_size,0)} 条一段共享长前缀`,`清单指纹：${manifest.item_id_digest||'未记录'}（版本 ${manifest.id||'—'}）`]},{title:'缓存判定口径',lines:['usage.cached_tokens（缓存词元用量）> 0 计为命中；命中率 = 命中数 / 能取到用量的请求数。','缺少用量的请求不计入分母，也不当作未命中。']},{title:'配置对齐与实际节奏',lines:['两侧的模型、并发上限、计划节奏、超时与请求内容相同；地址和密钥区分路由。','两侧独立排队，响应速度会改变实际发送时间；相同计划值不保证实际负载窗口一致，显著性检验不能单独排除这一影响。',`模型：${run.sides?.baseline?.model||'—'}`]}])+routingIsolationHtml(run)+UI.limitations(report.limitations||run.limits||[])});
}
function routingIsolationHtml(run){
 const iso=run.cache_isolation,cold=run.report?.cache?.cold_start||{},side=key=>{const c=cold[key]||{};return c.eligible?`${num(c.hits,0)} / ${num(c.eligible,0)}${c.raced?`（${num(c.raced,0)} 次可由同段抢跑解释）`:''}`:'—'};
 return UI.methodCards([{title:'缓存隔离',lines:iso?[`每侧请求以本侧专属标记开头（固定路由 ${iso.baseline} · 智能路由 ${iso.candidate}）。同段前缀照常复用；跨侧、跨运行从第一个字起不同，互不预热。`,`段首命中（应接近 0）：固定路由 ${side('baseline')} · 智能路由 ${side('candidate')}。`,'同段请求在段首返回首字前已发出时，命中可能由本侧抢跑造成；其余命中不少于 2 段且达到 10% 时，判定为实验之外的预热，缓存差值降级为不可归因。']:['这条旧记录没有缓存隔离，两侧请求逐字相同，先到的一侧可能替另一侧预热；缓存命中率仅供参考。']}]);
}
function routingReportHtml(run){
 const report=run.report,cache=report?.cache||{},rel=report?.reliability||{},support=report?.success?.mcnemar_supports,warnings=[];
 if(!run.cache_isolation&&report)warnings.push('未做缓存隔离，两侧可能互相预热；缓存命中率差值不能用于路由归因。');
 if(cache.contamination_suspected)warnings.push('缓存疑似被外部预热：段首命中无法用同段抢跑解释，缓存差值已降级为不可归因。');
 const boundary=warnings.length?UI.notice(warnings.join(' '),{tone:'warning',title:'归因限制'}):'';
 const paired=`已配对 ${num(rel.paired_total,0)} 对；未配对 ${num(rel.unpaired_count,0)} 条已剔除。`+(support===true?'配对检验支持成功/失败差异。':support===false?'配对检验不支持成功/失败差异。':'配对显著性证据不足。');
 const conclusion=report&&run.status==='finished'?UI.section({title:'归因结论',description:'结论仅适用于本次请求清单与观测时段。',bodyHtml:routingAttribution(report.success,'成功率')+routingAttribution(cache,'缓存命中率')+`<p class="report-note">${UI.esc(paired)}</p>`+boundary}):boundary;
 return conclusion+routingMetricTable(run)+routingDiagnosticsHtml(run)+(report?routingReliabilityHtml(run):'')+routingMethodHtml(run);
}
function routingSideFactsHtml(run,side){
 const key=side?.key||side,cfg=run.sides?.[key]||{},report=routingObservedReport(run),stat=report[key]||{},cache=stat.cache||{},noUsage=report.cache?.[`${key}_no_usage`];
 const stability=routingStability(run,key);
 return UI.section({title:`${cfg.name||'路由'} · 原始观测`,description:'成功率以本侧已返回请求为分母；延迟只统计完整成功请求。',bodyHtml:UI.metrics([{label:'已收到结果',value:stat.requests??0,unit:'条'},{label:'成功率',value:stat.success?.pct==null?'—':comparisonMetric(stat.success.pct),unit:'%',note:`成功 ${num(stat.success?.successes,0)} / ${num(stat.requests,0)}`},{label:'缓存命中率',value:cache.pct==null?'—':comparisonMetric(cache.pct),unit:'%',note:`命中 ${num(cache.successes,0)} / 可统计 ${num(cache.total,0)}；无用量 ${num(noUsage,0)} 条`},{label:'首字耗时 · 第95百分位',value:comparisonMetric(stat.ttft_p95_ms),unit:'毫秒'},{label:'总耗时 · 第95百分位',value:comparisonMetric(stat.total_p95_ms),unit:'毫秒'},{label:'最大连续失败次数',value:stability.max_consecutive_failures??'—',unit:'次'},{label:'最大平滑首字耗时',value:comparisonMetric(stability.ttft_ewma_max_ms),unit:'毫秒',note:'连续 5 次窗口；平滑系数 0.5'}])+UI.methodCards([{title:'本次连接信息',lines:[cfg.url||'—',`模型 ${cfg.model||'—'} · 记录 ${num(cfg.records,0)} 条`]},{title:'并发与节奏',lines:[`两侧并发 ${num(run.concurrency?.baseline,0)} / ${num(run.concurrency?.candidate,0)}`,`每侧计划调度 ${num(run.rpm,0)} RPM（每分钟请求数）`]}])})+routingDiagnosticsHtml(run,key)+routingRequestsHtml(run,key);
}
const routingRequestPages=new Map(),ROUTING_REQUEST_PAGE_SIZE=50;
function routingRequestPage(run,key){const id=`${run.id}:${key}`;if(!routingRequestPages.has(id))routingRequestPages.set(id,{offset:0,data:null,stamp:null,loading:false,error:'',seq:0});return routingRequestPages.get(id)}
function routingRequestStamp(run,key){return `${run.sides?.[key]?.records??routingObservedReport(run)[key]?.requests??0}:${run.status}`}
function routingRequestsHtml(run,key){
 const page=routingRequestPage(run,key),data=page.data,items=data?.items||[];
 const rows=items.map(item=>[
  UI.esc(item.sequence??'—'),
  UI.tag(item.ok?'成功':'失败',item.ok?'success':'danger')+(item.ok?'':` ${UI.esc(item.reason||'请求未完整结束')}`)+`<small class="role-label">状态码 ${UI.esc(item.status_code??'未记录')}</small>`,
  requestLatencyHtml(item,'routing-request:'+run.id+':'+key+':'+item.sequence),
  UI.esc(item.prompt_tokens??'—'),
  UI.esc(item.cached_tokens??'—')+`<small class="role-label">${UI.esc(item.usage_seen&&typeof item.cache_hit==='boolean'?(item.cache_hit?'命中':'未命中'):'命中未知')}</small>`,
  UI.esc(item.completion_tokens??'—'),UI.esc(item.total_tokens??'—')
 ]);
 const note=page.error?UI.notice(page.error,{tone:'warning'}):data?.reason?UI.notice(data.reason,{tone:'warning'}):'';
 const unavailable=data?.available===false;
 const summary=data?`已返回 ${num(data.recorded_total??data.total,0)} 条 · 当前 ${items.length?num(page.offset+1,0):0}–${num(page.offset+items.length,0)} / ${num(data.total,0)} 条`:page.loading?'正在读取逐条统计…':'等待逐条统计';
 const buttons=UI.button('刷新当前页',{action:'setRoutingRequestsPage',args:[run.id,key,page.offset],disabled:page.loading})+UI.button('上一页',{action:'setRoutingRequestsPage',args:[run.id,key,Math.max(0,page.offset-ROUTING_REQUEST_PAGE_SIZE)],disabled:page.loading||page.offset===0})+UI.button('下一页',{action:'setRoutingRequestsPage',args:[run.id,key,page.offset+ROUTING_REQUEST_PAGE_SIZE],disabled:page.loading||!data?.has_more});
 return UI.section({title:'逐条请求统计',description:'按请求编号排序，每页 50 条。耗时单位为 ms（毫秒）；输入、缓存、输出与总量均为 token（词元）。运行中自动更新当前页。',badgeHtml:UI.badge(summary,{hint:true}),bodyHtml:note+UI.table({columns:['请求编号','结果与状态码','耗时与时间','输入词元','缓存词元','输出词元','总词元'].map(label=>({label})),rowsHtml:rows,empty:unavailable?'结果文件不可用':page.error?'读取失败':page.loading?'正在加载…':'暂无已返回请求'})+UI.actionBar(buttons)});
}
async function loadRoutingRequests(run,key){
 const page=routingRequestPage(run,key),stamp=routingRequestStamp(run,key);if(page.loading||page.stamp===stamp)return;
 const seq=++page.seq,offset=page.offset;page.loading=true;page.error='';
 try{const data=await api(`/routing-comparison/${encodeURIComponent(run.id)}/requests?side=${encodeURIComponent(key)}&offset=${offset}&limit=${ROUTING_REQUEST_PAGE_SIZE}`);if(seq!==page.seq)return;page.data=data;page.stamp=stamp}
 catch(error){if(seq!==page.seq)return;page.error=error.message||'逐条统计读取失败';page.stamp=stamp}
 finally{if(seq===page.seq){page.loading=false;const selected=routingComparisons.find(item=>item.id===run.id);if(selectedRouting===run.id&&routingTab===key&&selected)renderRoutingDetail(selected)}}
}
function setRoutingRequestsPage(id,key,offset){const run=routingComparisons.find(item=>item.id===id);if(!run||selectedRouting!==id||routingTab!==key)return;const page=routingRequestPage(run,key);page.seq++;page.offset=Math.max(0,Number(offset)||0);page.data=null;page.stamp=null;page.loading=false;page.error='';renderRoutingDetail(run)}
function renderRoutingDetail(run){
 if(run._summary){showLoading();return refresh()}
 window.currentRoutingDetail=run;
 const host=$('modulePlaceholder'),same=host.dataset.routingId===run.id&&host.dataset.routingTab===routingTab,scroll=same?qualityScrollSnapshot(host):null;
 $('loadingState').hidden=true;$('empty').hidden=true;$('detail').hidden=true;host.hidden=false;
 const tabs=[['report','对照结论'],['baseline',run.sides?.baseline?.name||'固定路由'],['candidate',run.sides?.candidate?.name||'智能路由']];
 if(!tabs.some(x=>x[0]===routingTab))routingTab='report';host.dataset.routingId=run.id;host.dataset.routingTab=routingTab;
 const body=routingTab==='report'?routingReportHtml(run):routingSideFactsHtml(run,routingTab),active=['waiting','running','stopping'].includes(run.status);
 const pct=run.status==='finished'?100:Math.round(Math.max(0,Math.min(1,Number(run.progress?.pct)||0))*100);
 const progress=UI.progressCard({kicker:'执行进度',value:pct,summary:`${num(run.progress?.done,0)} / ${num(Number(run.tier||0)*2,0)} 条请求已返回 · 两侧各 ${num(run.tier||0)} 条`,note:active?'指标按已返回结果实时更新；两侧完成速度可能不同，完成前不作最终归因。':run.status==='finished'?'两侧请求已全部返回，结论基于本次完整执行窗口。':'本次未完整执行；以下数据只描述已返回请求，不作最终归因。'});
 const actions=active?'':UI.button('删除本地记录',{variant:'danger',action:'deleteRouting',args:[run.id]});
 host.innerHTML=UI.pageShell(UI.pageHeader({kicker:'路由对照 · 固定与智能路由',title:run.name,description:'同一份请求清单、同一计划节奏、同一并发上限，交错发往两条路由。',meta:[routingStatusLabel(run.status),`${num(run.tier,0)} 条 × 2 侧`,`每侧计划调度 ${num(run.rpm,0)} RPM（每分钟请求数）`,run.config?.capture_raw?'已开启原始记录':'未开启原始记录'],statusLabel:routingStatusLabel(run.status),statusNote:run.error||'按请求编号配对',statusTone:run.status==='error'?'danger':active?'accent':'neutral',actionsHtml:runHeaderAction('routing',run,'stopRouting','停止后续请求')})+UI.tabs({label:'路由对照详情标签页',items:tabs,active:routingTab,action:'setRoutingTab',ownerId:run.id})+artifactNotice(run)+(active?progress:'')+body+(active?'':UI.disclosure({title:'查看执行完成情况',section:true,id:'routing-execution-'+run.id,bodyHtml:progress}))+UI.timeRecords(run,{live:active,key:'routing:'+run.id})+(actions?UI.actionBar(actions):''));
 requestAnimationFrame(()=>{if(host.dataset.routingId===run.id&&host.dataset.routingTab===routingTab)restoreQualityScroll(host,scroll)});
 if(routingTab!=='report')loadRoutingRequests(run,routingTab);
}
function reportStatus(status){return statusNames[status]||status||'准备中'}
function reportTag(level){return level==='risk'?'风险':level==='caution'?'关注':level==='good'?'正常':'待完成'}
function signedMetric(v,suffix=''){return v==null?'—':`${Number(v)>0?'+':''}${num(v)}${suffix}`}
function comparisonOverviewHtml(c){const rows=c.report?.groups||c.groups||[],report=c.report||{},baselineId=report.baseline_group||rows.find(x=>x.role==='baseline')?.id,baseline=rows.find(x=>x.id===baselineId)||{},complete=rows.filter(x=>['finished','finished_with_errors'].includes(x.status)).length,totalGroups=Number(c.total_groups||rows.length||0),planned=Number(c.planned_requests||rows.reduce((sum,x)=>sum+Number(x.iterations||0),0)),completed=Number(c.completed_requests!=null?c.completed_requests:rows.reduce((sum,x)=>sum+Number(x.completed_requests||x.summary?.done||0),0)),finished=c.status==='finished'||c.status==='finished_with_errors',pct=finished?100:planned?Math.round(Math.max(0,Math.min(1,completed/planned))*100):(totalGroups?Math.round(complete/totalGroups*100):0),current=c.current_group_name?`当前执行：${esc(c.current_group_name)}`:finished?'全部实验组已完成':c.status==='running'?'正在执行实验组':'等待执行';const hint=finished?'全部实验组已完成，以下结论基于本次完整执行窗口。':c.status==='running'?`当前已完成 ${num(completed,0)} / ${num(planned,0)} 条请求，等待剩余实验组完成。`:'实验尚未完成；当前只展示过程数据，不把局部结果包装成最终结论。';return `<section class="report-overview"><section class="report-progress-card"><div class="report-card-kicker">实验整体进度</div><div class="report-progress-line"><strong>${pct}%</strong><span>${num(completed,0)} / ${num(planned,0)} 条请求 · ${complete}/${totalGroups} 组完成</span></div><div class="report-progress-track"><i style="width:${pct}%"></i></div><p>${hint} · ${current}</p></section><section class="report-focus-card"><div class="report-card-kicker">本次参照</div><strong>${esc(baseline.name||'尚未生成基准值')}</strong><span>基准组 · ${num(baseline.concurrency,0)} 并发</span><small>完成吞吐 ${comparisonMetric(baseline.completed_rpm)} RPM · 成功率 ${comparisonMetric(baseline.success_pct)}%</small></section></section><div class="report-baseline-strip"><span>基准值是本次实验内部参照，不是绝对标准</span><b>完成吞吐 ${comparisonMetric(baseline.completed_rpm)} RPM</b><b>成功率 ${comparisonMetric(baseline.success_pct)}%</b><b>总耗时 p95 ${comparisonMetric(baseline.total_p95_ms)} ms</b></div>`}
function comparisonReportHtml(c, options={}){
 const report=c.report||{}, conclusion=report.conclusion||{}, decision=report.decision||{}, evidence=report.evidence||{}, comparison=report.comparability||{}, capacity=report.capacity||{}, rows=report.groups||c.groups||[], baseline=report.baseline_group||rows.find(x=>x.role==='baseline')?.id, comparisons=rows.filter(x=>x.id!==baseline);
 const complete=rows.filter(x=>['finished','finished_with_errors'].includes(x.status)).length;
 const validThroughput=rows.filter(x=>x.completed_rpm!=null).sort((a,b)=>Number(b.completed_rpm)-Number(a.completed_rpm));
 const highest=validThroughput[0]||{};
 const worstSuccess=comparisons.reduce((worst,x)=>x.delta_success_pct!=null&&(worst==null||Number(x.delta_success_pct)<Number(worst.delta_success_pct))?x:worst,null);
 const worstP95=comparisons.reduce((worst,x)=>x.delta_total_p95_ms!=null&&(worst==null||Number(x.delta_total_p95_ms)>Number(worst.delta_total_p95_ms))?x:worst,null);
 const signals=conclusion.signals||[], nextSteps=decision.next_steps||[];
 const decisionTitle=decision.label||'等待实验结果';
 const statusLabel=decision.status==='hold'?'hold':decision.status==='review'?'review':decision.status==='observe'?'observe':'pending';
 const compClass=comparison.level==='comparable'?'good':comparison.level==='partial'?'caution':'risk';
 const compLabel=comparison.label||'待判断';
 const pressureSignal=report.pressure_window_signal||report.capacity_inflection;
 const overview=options.withoutOverview?'':comparisonOverviewHtml(c);
 return `${overview}<section class="report-section report-decision-hero ${statusLabel}"><div class="decision-main"><div class="decision-eyebrow">结论先行 · 只回答当前证据能支持什么</div><h2>${esc(decisionTitle)}</h2><p class="decision-summary">${esc(conclusion.summary||'实验结果尚未完整，暂不能形成结论。')}</p><div class="decision-scope"><b>适用范围</b><span>${esc(decision.scope||'结论适用范围将在实验完成后给出。')}</span></div></div><div class="decision-badge ${conclusion.level||'neutral'}"><span>${reportTag(conclusion.level)}</span><small>${esc(conclusion.title||'等待实验结果')}</small></div><div class="decision-action-row"><div><span class="decision-action-label">建议动作</span><strong>${esc(decision.recommendation||'等待实验完成')}</strong></div><div class="decision-guardrail"><span>正式放量</span><b>不自动批准</b></div></div></section>${UI.disclosure({title:'阅读指引与证据边界',section:true,id:'comparison-readout-'+c.id,bodyHtml:`<div class="report-section-title-row"><div><h2 class="report-section-title">快速读报告</h2><p class="report-section-sub">先看证据边界，再看指标差异；客户端观测值不等于网关内部真实容量。</p></div><span class="evidence-badge">${esc(evidence.grade||'低')}证据</span></div><div class="report-readout-grid"><div class="readout-card"><span class="readout-label">实验可比性</span><strong class="readout-value ${compClass}">${esc(compLabel)}</strong><p>${esc(comparison.note||'未生成可比性说明。')}</p></div><div class="readout-card"><span class="readout-label">证据量</span><strong class="readout-value">${num(evidence.completed_requests,0)} <small>条完成请求</small></strong><p>最小实验组样本 ${num(evidence.min_group_sample,0)} 条；证据量级不是统计置信区间。</p></div><div class="readout-card"><span class="readout-label">执行顺序</span><strong class="readout-value">基准组优先</strong><p>随后按顺序串行执行，可能受到连接预热、缓存和服务状态时间差影响。</p></div><div class="readout-card"><span class="readout-label">实验进度</span><strong class="readout-value">${complete}/${rows.length||0} <small>组完成</small></strong><p>${complete===rows.length&&rows.length?'所有组均已完成，可查看最终窗口判断。':'所有组完成前，不生成最终对照结论。'}</p></div></div>`})}<section class="report-section"><div class="report-section-title-row"><div><h2 class="report-section-title">关键指标</h2><p class="report-section-sub">用实际完成结果描述本次窗口；“计划调度 RPM（每分钟请求数）”只代表投放意图。</p></div></div><div class="report-metrics report-metrics-emphasis"><div class="report-metric metric-primary"><div class="metric-label">客户端观测的稳定完成吞吐</div><div class="metric-value">${comparisonMetric(capacity.observed_max_stable_completed_rpm)}<small>RPM</small></div><div class="metric-note">${esc(capacity.observed_max_stable_group||'没有足够稳定样本')} · 不是网关容量承诺</div></div><div class="report-metric"><div class="metric-label">最高客户端观测完成吞吐</div><div class="metric-value">${comparisonMetric(highest.completed_rpm)}<small>RPM</small></div><div class="metric-note">${esc(highest.name||'没有已完成结果')}</div></div><div class="report-metric"><div class="metric-label">最差成功率变化</div><div class="metric-value">${signedMetric(worstSuccess?.delta_success_pct,'%')}</div><div class="metric-note">相对基准组；负值表示退化</div></div><div class="report-metric"><div class="metric-label">最大总耗时 p95 变化</div><div class="metric-value">${signedMetric(worstP95?.delta_total_p95_ms,' ms')}</div><div class="metric-note">正值表示长尾变慢</div></div></div></section><section class="report-section"><div class="report-section-title-row"><div><h2 class="report-section-title">关键发现</h2><p class="report-section-sub">异常信号是复测线索，不是单凭一次黑盒实验做出的根因结论。</p></div></div><div class="signal-list report-signal-list">${signals.length?signals.map(x=>`<div class="signal ${x.level||''}"><i class="signal-dot"></i><div><b>${esc(x.title||'')}</b><br>${esc(x.detail||'')}</div></div>`).join(''):`<div class="signal good"><i class="signal-dot"></i><div><b>当前窗口没有额外异常信号</b><br>这不代表长期稳定，也不代表可以直接扩大流量。</div></div>`}</div></section><section class="report-section"><div class="report-section-title-row"><div><h2 class="report-section-title">实验组结果</h2><p class="report-section-sub">基准组是同一条实验记录内的参照；每个对比组的差值均相对它计算。</p></div><span class="table-hint">${complete}/${rows.length||0} 组完成 · ${pressureSignal?'有压力窗口信号':'暂未发现压力窗口信号'}</span></div><div class="table-wrap report-wrap"><table class="report-table comparison-result-table"><thead><tr><th>实验组</th><th>并发</th><th>计划调度 RPM</th><th>客户端观测完成 RPM</th><th>计划兑现率</th><th>成功率</th><th>总耗时 p50 / p95</th><th>调度延迟 p95</th><th>失败/异常</th><th>相对基准</th></tr></thead><tbody>${rows.length?rows.map(x=>`<tr class="${x.id===baseline?'baseline-row':''}"><td><b>${esc(x.name||'实验组')}</b><small class="role-label">${x.id===baseline?'基准组':'对比组'} · ${x.status==='finished'?'已完成':x.status==='finished_with_errors'?'完成但有错误':x.status==='running'?'执行中':'待执行'}</small></td><td>${num(x.concurrency)}</td><td>${num(x.planned_rpm)}</td><td><b>${num(x.completed_rpm)}</b></td><td>${num(x.dispatch_fulfilment_pct)}%</td><td>${num(x.success_pct)}%</td><td>${num(x.total_p50_ms)} / ${num(x.total_p95_ms)} ms</td><td>${num(x.schedule_lag_p95_ms)} ms</td><td>${num(x.failure_count,0)}</td><td>${x.id===baseline?'—':`${pctDelta(x.delta_success_pct,'%')} · ${pctDelta(x.delta_completed_rpm,' RPM')} · ${pctDelta(x.delta_total_p95_ms,' ms',true)}`}</td></tr>`).join(''):`<tr><td colspan="10">暂无实验结果</td></tr>`}</tbody></table></div></section><section class="report-section next-step-section"><div class="report-section-title-row"><div><h2 class="report-section-title">下一步动作</h2><p class="report-section-sub">报告的作用是把观察结果转成可执行的复测和准入动作。</p></div></div><div class="next-step-grid">${(nextSteps.length?nextSteps:['等待产生足够数据后再决定']).map((x,i)=>`<div class="next-step"><span>${i+1}</span><p>${esc(x)}</p></div>`).join('')}</div></section>`;
}
function comparisonSupplementHtml(c){
 const r=c.report||{},e=r.evidence||{},m=r.method||{},signal=r.pressure_window_signal||r.capacity_inflection,rows=r.groups||[],comp=r.comparability||{};
 return UI.disclosure({title:'证据、可比性与方法',section:true,id:'comparison-method-'+c.id,bodyHtml:
  UI.section({title:'证据概况',description:'证据量级不是统计置信区间。',badgeHtml:UI.badge('证据量级：'+(e.grade||'低')),bodyHtml:UI.metrics([
   {label:'已完成请求',value:num(e.completed_requests,0),unit:'条',note:'仅统计已经收到最终结果的请求'},
   {label:'最小组样本',value:num(e.min_group_sample,0),unit:'条',note:'组间证据量越不均衡，横向比较越弱'},
   {label:'压力窗口信号',value:signal?(signal.group_name||'已发现'):'暂未发现',note:signal?(signal.reasons||[]).join('；'):'当前样本未满足启发式阈值'},
   {label:'基准组',value:(rows.find(x=>x.id===r.baseline_group)||{}).name||'—',note:'所有对比组均与该组比较'}
  ])})+
  UI.section({title:'计算方法与适用边界',bodyHtml:UI.methodCards([
   {title:'实验可比性 · '+(comp.label||'待判断'),lines:[comp.note||'未生成可比性说明。']},
   {title:'计划调度速率',lines:['客户端尝试投放的节奏，以 RPM（每分钟请求数）计，不等于请求已开始或网关实际完成能力。']},
   {title:'客户端观测完成吞吐',lines:['按已完成请求数 ÷ 实际运行时间计算，只描述本次客户端观测窗口。']},
   {title:'串行顺序效应',lines:[m.order_effect||'基准组优先、对比组随后串行执行；结果可能受到时间顺序影响。']}
  ])+UI.limitations([
   e.note||'证据量级不等于统计置信区间。',
   '当前执行是串行实验，不能代表多个压力组同时打入网关的混压场景。',
   '压力窗口信号是启发式提示，不是容量上限、稳定承载量或正式放量依据。',
   '黑盒结果只能描述客户端可观测差异，不能证明上游来源、官方身份或资源真伪。'
  ])})
 });
}
const qualityCategoryLabels={"numerical_reasoning": "数值与应用计算", "logical_reasoning": "逻辑与约束推理", "structured_extraction": "结构化抽取", "instruction_following": "指令与格式遵循", "reading_comprehension": "阅读与流程理解", "grounded_answering": "依据与不确定性", "code_reasoning": "代码与查询理解", "data_analysis": "数据分析", "multilingual": "跨语言理解", "long_context": "长上下文检索与整合", "robustness": "噪声与边界输入", "safety_boundary": "给定安全规则执行", "basic_response": "基础响应", "math": "数学计算", "structured_json": "结构化输出", "long_text_consistency": "长文本一致性"};

function qualityProgress(q){const resources=q.resources||[],total=Number(q.planned_cases??(Number(q.case_count||0)*Number(q.total_resources||q.config?.resources?.length||resources.length))),done=Number(q.completed_cases??resources.reduce((n,r)=>n+(r.case_results||[]).filter(x=>x.score!=null&&typeof x.passed==='boolean').length,0));return {total,done,pct:total?Math.min(100,Math.floor(done*100/total)):0,resources:Number(q.total_resources||q.config?.resources?.length||resources.length)}}
function qualityProgressHtml(q){const p=qualityProgress(q),active=['running','waiting','stopping'].includes(q.status);let state=q.status==='stopping'?'已停止安排后续题目，当前请求结束并保存后完成收尾。':q.current_resource?`当前：${esc(q.current_resource)}${q.current_case?` · 第 ${num(q.current_case,0)} / ${num(q.case_count,0)} 题`:''} · ${q.phase==='scoring'?'正在评分':'等待响应'}`:active?'等待执行':p.done<p.total?'执行已结束，仍有题目未完成；不能生成完整质量判断。':'全部题目已评测；请求成功与评分通过请分别查看。';return `<section class="report-progress-card"><div class="report-card-kicker">题集执行进度</div><div class="report-progress-line"><strong>${p.pct}%</strong><span>${num(p.done,0)} / ${num(p.total,0)} 题已评测 · ${num(q.completed_resources,0)} / ${p.resources} 个资源完成</span></div><div class="report-progress-track" role="progressbar" aria-label="题集执行进度" aria-valuemin="0" aria-valuemax="${p.total}" aria-valuenow="${p.done}"><i style="width:${p.pct}%"></i></div><p>${state}</p><p>每题收到最终结果、完成评分并保存后才更新进度；请求错误也计为已评测，不代表通过。</p></section>`}
function percentileFromSorted(values, ratio){if(!values.length)return null;const index=Math.min(values.length-1,Math.max(0,Math.ceil(values.length*ratio)-1));return values[index]}
function qualityStats(resource){
 const s=resource?.summary||{},cases=resource?.case_results||[],errors=resource?.request_errors||[],timings=[];
 let observedPassed=0,completed=0;
 for(const result of cases){
  if(result.passed)observedPassed++;
  if(result.score!=null&&typeof result.passed==='boolean')completed++;
  if(result.total_ms!=null){const timing=Number(result.total_ms);if(Number.isFinite(timing))timings.push(timing)}
 }
 const total=Number(resource?.planned_cases||s.total_cases||cases.length||0),passed=Number(s.passed_cases??observedPassed);
 // Average in request order to preserve floating-point results, then sort once for both percentiles.
 const average=timings.length?timings.reduce((a,b)=>a+b,0)/timings.length:null;
 timings.sort((a,b)=>a-b);
 return {total,passed,passRate:s.pass_rate==null?(total&&completed===total?passed/total:null):Number(s.pass_rate),score:s.percentage==null?(s.score==null?null:Number(s.score)*100):Number(s.percentage),completed,errors:errors.length,totalMsP50:percentileFromSorted(timings,.5),totalMsP95:percentileFromSorted(timings,.95),averageTotalMs:average};
}
function qualityPercent(value){return value==null?null:Number(value)*100}

function qualityDelta(a,b){if(a==null||b==null||!Number.isFinite(Number(a))||!Number.isFinite(Number(b)))return null;return Number(b)-Number(a)}
function qualityConclusion(q){
 const backend=q.report||{};
 // 结论只从后端报告读取，避免前端重复实现阈值后与报告出现矛盾。
 if(backend.title)return {level:backend.level||'neutral',title:backend.title,summary:backend.summary||'暂无结论摘要。',scope:backend.decision?.scope||'结论范围待确认',action:backend.decision?.recommendation||'查看逐题证据并复测。'};
 const resources=q.resources||[],a=qualityStats(resources[0]);
 if(q.status!=='finished'&&q.status!=='finished_with_errors')return {level:'neutral',title:'测试尚未完成',summary:'题集尚未全部执行完，当前数据只能用于查看进度，不能形成最终质量判断。',scope:'进行中',action:'等待题集完成后再判断。'};
 return {level:a.errors?'risk':a.passRate>=.95?'good':'caution',title:a.errors?'资源存在请求链路错误':a.passRate>=.95?'固定题集下表现稳定':'固定题集下存在未通过题',summary:`通过 ${a.passed}/${a.total} 道题；这是能力、格式和完整性的黑盒观测，不是官方来源或长期稳定性证明。`,scope:a.errors?'可定位本次题集的问题':'可做初步能力判断',action:a.errors?'先查看请求错误，再用相同题集复测。':a.passRate>=.95?'可作为当前题集下的初步基线；如需承诺仍需扩大题集。':'进入逐题证据定位未通过原因，并复测确认。'};
}

function categoryLabel(category){return qualityCategoryLabels[category]||'未分类'}
function qualityFactsHtml(q){
 const facts=q.report?.facts||[];
 const value=(fact,key)=>{const item=(fact.items||[]).find(x=>x.key===key);return item?num(item.value):'未记录'};
 const rows=facts.map(f=>[esc(f.resource||'资源'),`${value(f,'processed_cases')} / ${value(f,'planned_cases')}`,value(f,'passed_cases'),value(f,'failed_cases'),value(f,'request_errors')]);
 const keys=[...new Set(facts.flatMap(f=>(f.items||[]).map(x=>x.key)))];
 const detailed=keys.map(key=>{const item=facts.flatMap(f=>f.items||[]).find(x=>x.key===key)||{};return [esc(item.label||key),...facts.map(f=>value(f,key)),esc(item.note||'')]});
 return UI.section({title:'本次观测计数',description:'已评测包含请求错误；通过与请求成功分别统计。完整计数可与逐题证据核对。',bodyHtml:UI.table({columns:['资源','已评测 / 计划','通过','未通过','请求错误'].map(label=>({label})),rowsHtml:rows})+UI.disclosure({id:'quality-facts-'+q.id,title:'查看完整计数与统计口径',bodyHtml:UI.table({columns:['观测项',...facts.map(f=>f.resource||'资源'),'口径'].map(label=>({label})),rowsHtml:detailed})})});
}
function qualityLayersHtml(q){
 const report=q.report||{},layers=report.layers||{},conclusion=qualityConclusion(q),stats=layers.statistics||{},advice=layers.advice||{},tone={risk:'danger',caution:'warning',good:'success'}[conclusion.level]||'neutral';
 const basis=stats.basis||report.evidence?.scope||'';
 return UI.section({title:'结论与建议',badgeHtml:UI.badge(report.evidence?.grade||'证据不足'),bodyHtml:UI.notice(conclusion.summary,{tone,title:conclusion.title})+UI.methodCards([{title:'建议动作',lines:[advice.summary||report.decision?.recommendation||conclusion.action]},{title:'适用范围',lines:[conclusion.scope,advice.boundary||'建议面向业务复核，不构成自动准入。']}])+UI.disclosure({id:'quality-basis-'+q.id,title:'判断依据与后续步骤',bodyHtml:UI.limitations(stats.notes||['等待完整证据后再给出判断。'])+(basis?`<p class="layer-basis">${esc(basis)}</p>`:'')+UI.steps(advice.steps||[report.decision?.recommendation||'等待证据'])})});
}
function qualityScoreCard(r,index,q){const final=q.report?.evidence?.complete;return `<article class="quality-score-card ${r.health_level}"><div class="report-card-kicker">${esc(r.name||`资源 ${index+1}`)} · ${final?'内部评分':'阶段评分'}</div><div class="quality-score-number">${comparisonMetric(r.score_1000 ?? (r.score_pct!=null?r.score_pct*10:null))}<small> / 1000</small></div><p>${esc(r.health_title||'等待结果')}</p><div class="quality-score-facts"><span>通过率 <b>${comparisonMetric(r.pass_rate_pct)}%</b></span><span>请求成功 <b>${comparisonMetric(r.request_success_pct)}%</b></span><span>关键失败 <b>${r.critical_failures?.length||0} 题</b></span></div>${r.constraint_diagnostics?.checks?`<p class="quality-constraint-diagnostic">指令约束通过 ${r.constraint_diagnostics.passed_checks} / ${r.constraint_diagnostics.checks} · 辅助诊断，不计入主分</p>`:''}<small>项目内部 1000 分尺度，各维度等权、链路失败计零分；不是行业基准分。</small></article>`}
function qualityScoreSectionHtml(q){const resources=q.report?.resources||[];return `<section class="report-section"><div class="report-section-title-row"><div><h2 class="report-section-title">内部评分（参考项，不是主结论）</h2><p class="report-section-sub">按固定评分规则汇总；分数需结合已评测题数、未通过原因与结论范围一起阅读。</p></div></div><div class="quality-score-grid">${resources.map((r,i)=>qualityScoreCard(r,i,q)).join('')}</div></section>`}
function qualityReportHtml(q){
 const report=q.report||{},paired=report.mode==='comparison',manifest=q.config?.suite_manifest||{},policy=report.policy||{},active=['waiting','running','stopping'].includes(q.status);
 const methodology=UI.button('评测方法与官方参考',{action:'showQualityMethodology1',args:['report']})+
  `<p class="field-hint">${esc(report.execution?.label||'历史执行策略未记录')} · ${esc(report.execution?.description||'')}</p>`+
  UI.methodCards([
   {title:manifest.title||'历史冒烟题集',lines:[`${q.case_count||0} 题 · ${(manifest.categories||[]).length||'未记录'} 个维度 · 版本 ${manifest.version||q.config?.suite_version||1}`,`题集指纹：${manifest.fingerprint||'历史记录未保存'}`]},
   {title:'评分与内部参考线',lines:[manifest.scoring||'按历史题目规则评分',`展示总分 /1000；内部阈值仍按百分比：总分 ≥ ${policy.total_score_min??85}；各维度 ≥ ${policy.category_score_min??70}；请求错误与关键失败均为零；足量覆盖后才进入业务复核。`,policy.note||'内部参考阈值']},
   {title:report.evidence?.grade||'证据不足',lines:[report.evidence?.repeatability||'未验证重复性','成功响应得分与整体质量分分开，超时不从总体分母移除。']}
  ])+((report.limitations||[]).length?UI.limitations(report.limitations):'');
 return [
  active?qualityProgressHtml(q):'',qualityLayersHtml(q),qualityFactsHtml(q),qualityScoreSectionHtml(q),
  paired?qualityPairedHtml(q):qualityDiagnosisHtml(q),qualityDimensionHtml(q),qualityFailureHtml(q),
  active?'':UI.disclosure({section:true,id:'quality-execution-'+q.id,title:'查看执行完成情况',bodyHtml:qualityProgressHtml(q)}),
  UI.disclosure({section:true,id:'quality-method-'+q.id,title:'题集、评分口径与结论可信范围',bodyHtml:methodology})
 ].join('');
}
function evidenceButton(resourceIndex,caseId,label='查看证据'){return `<button class="raw-link" type="button" data-resource="${resourceIndex}" data-case="${esc(caseId)}" data-click-action="showQualityCaseById">${esc(label)}</button>`}
function qualityDiagnosisHtml(q){const report=q.report||{},r=report.resources?.[0]||{},single=report.single||{};return `<section class="report-section"><h2 class="report-section-title">单资源诊断 · 是否值得进入业务复核</h2><div class="method-grid"><div class="method-card"><b>本轮优势维度</b><span>${esc((single.strengths||[]).join('、')||'暂无充分证据')}</span></div><div class="method-card"><b>薄弱维度</b><span>${esc((single.weaknesses||[]).join('、')||(report.evidence?.complete?'未发现低于内部维度阈值的项目':'等待完整结果'))}</span></div><div class="method-card"><b>筛选阻断项</b><span>${esc((r.gate_reasons||[]).join('；')||'本题集内未触发阻断项；仍需人工复核')}</span></div></div></section><section class="report-section"><h2 class="report-section-title">适用场景与使用边界</h2><p class="report-section-sub">仅由本轮相关维度推导；“可进入业务复核”不是生产可用承诺。</p><div class="quality-scenario-grid">${(r.scenarios||[]).map(x=>`<article class="method-card"><b>${esc(x.name)} · ${esc(x.status)}</b><span>${esc(x.reason)}</span><small>${esc(x.dimensions.join('、'))}</small></article>`).join('')}</div></section>`}
function qualityPairedHtml(q){const p=q.report?.pairwise||{};const rows=[...(p.regressions||[]).map(x=>({...x,label:'B 退化'})),...(p.gains||[]).map(x=>({...x,label:'B 改善'})),...(p.shared_failures||[]).filter(x=>x.delta===0).map(x=>({...x,label:'共同失败'}))];return `<section class="report-section"><h2 class="report-section-title">资源对照 · 选择依据</h2><div class="conclusion-banner ${p.comparable?'caution':'risk'}"><strong>${esc(p.selection||'等待配对证据')}</strong><p>${esc(p.method||'')}</p><p>${esc(p.repeatability||'')}</p>${p.same_endpoint_model?'<p>两侧地址与模型名相同：可能只是重复观测，不能确认是独立资源。</p>':''}</div><div class="report-metrics"><div class="report-metric"><div class="metric-label">配对质量分差 · B − A</div><div class="metric-value">${signedMetric(p.score_delta_pct,' 分')}</div><div class="metric-note">仅按 ${p.paired_cases||0} 道有效同题配对；非稳定性证明</div></div><div class="report-metric"><div class="metric-label">A 独胜 / B 独胜</div><div class="metric-value">${p.a_wins||0} / ${p.b_wins||0}<small> 题</small></div><div class="metric-note">平分 ${p.ties||0} 题，不等于两侧都通过</div></div><div class="report-metric"><div class="metric-label">共同未通过</div><div class="metric-value">${p.both_failed||0}<small> 题</small></div><div class="metric-note">共同缺陷不能被相对分差掩盖</div></div><div class="report-metric"><div class="metric-label">成功响应长尾耗时差</div><div class="metric-value">${signedMetric(p.p95_delta_ms,' 毫秒')}</div><div class="metric-note">95分位耗时，B − A；速度不计入质量分</div></div></div></section><section class="report-section"><h2 class="report-section-title">逐题改善、退化与共同缺陷</h2><p class="report-section-sub">按同一题和相同规则对齐；先看退化。两侧证据分别打开，不能只看净分差。</p><div class="table-wrap report-wrap"><table class="report-table"><thead><tr><th>题目</th><th>变化</th><th>分差</th><th>A 证据</th><th>B 证据</th></tr></thead><tbody>${rows.length?rows.map(x=>`<tr><td>${esc(x.title)}<small class="role-label">${esc(x.case_id)}</small></td><td>${x.label}</td><td>${signedMetric(x.delta,' 分')}</td><td>${evidenceButton(0,x.case_id)}</td><td>${evidenceButton(1,x.case_id)}</td></tr>`).join(''):'<tr><td colspan="5">暂无差异或共同失败；未测完时不能推断等效。</td></tr>'}</tbody></table></div></section>`}
function qualityDimensionHtml(q){const resources=q.report?.resources||[],ids=[...new Set(resources.flatMap(r=>(r.categories||[]).map(x=>x.category)))];return `<section class="report-section"><h2 class="report-section-title">能力维度 · 定位分数背后的问题</h2><p class="report-section-sub">维度等权；内部参考线为 ${q.report?.policy?.category_score_min??70} 分，不是行业认证。阶段分仅使用已测题。</p><div class="table-wrap report-wrap"><table class="report-table"><thead><tr><th>能力维度</th>${resources.map(r=>`<th>${esc(r.name)} · 分值</th><th>通过 / 已测 / 计划</th>`).join('')}${resources.length===2?'<th>B − A</th>':''}</tr></thead><tbody>${ids.map(id=>{const rows=resources.map(r=>r.categories.find(x=>x.category===id)||{});return `<tr><td>${esc(categoryLabel(id))}</td>${rows.map(r=>`<td class="${r.score!=null&&r.score<70?'delta-risk':''}">${comparisonMetric(r.score)} / 100</td><td>${r.passed||0} / ${r.completed||0} / ${r.planned||0}</td>`).join('')}${rows.length===2?`<td>${signedMetric(qualityDelta(rows[0].score,rows[1].score),' 分')}</td>`:''}</tr>`}).join('')}</tbody></table></div></section>`}
function qualityFailureHtml(q){const rows=(q.report?.resources||[]).flatMap((r,i)=>(r.failure_evidence||[]).map(x=>({...x,resourceIndex:i})));return `<section class="report-section"><h2 class="report-section-title">优先复核清单</h2><p class="report-section-sub">关键规则、链路错误优先；点击证据查看参考答案、评分依据与本次实际输出。</p><div class="table-wrap report-wrap"><table class="report-table"><thead><tr><th>资源</th><th>题目</th><th>问题分类</th><th>证据</th></tr></thead><tbody>${rows.length?rows.map(x=>`<tr><td>${esc(x.resource_name)}</td><td>${esc(x.title)}<small class="role-label">${esc(x.case_id)}</small></td><td>${esc(x.reason)}</td><td>${evidenceButton(x.resourceIndex,x.case_id)}</td></tr>`).join(''):'<tr><td colspan="4">当前没有已记录失败；未完成时不构成通过结论。</td></tr>'}</tbody></table></div></section>`}

function qualityCaseTable(q,resource,resourceIndex){
 const cases=resource?.case_results||[],capture=Boolean(q.config?.capture_raw);
 const rows=cases.map((x,idx)=>[
  `<b>${esc(x.title||x.case_id||'—')}</b><small class="role-label">${esc(categoryLabel(x.category))} · ${esc(x.case_id||'')}</small>`,
  UI.tag(x.passed?'通过':'未通过',x.passed?'success':'danger')+`<small class="role-label">得分 ${x.score==null?'未记录':comparisonMetric(Number(x.score)*100)+'%'}</small>`,
  requestLatencyHtml(x,'quality-request:'+q.id+':'+resourceIndex+':'+x.case_id),
  esc((x.failures||[]).join('、')||x.error||'—'),
  UI.textButton('查看详情',{action:'showQualityCase',args:[Number(resourceIndex),idx]})
 ]);
 return UI.section({title:'逐题证据',description:`点击“查看详情”核对题目、判分依据和实际输出。${capture?'已保存完整原始输入输出。':'本次仅保留输出预览，无法补看未保存的全文。'}`,badgeHtml:UI.badge(`${cases.length} 道题`,{hint:true}),bodyHtml:UI.table({columns:['题目与类别','结果与得分','耗时与时间','未通过原因','证据'].map(label=>({label})),rowsHtml:rows,empty:'暂无逐题结果'})});
}
function qualityCaseDeltaHtml(a,b){const left=new Map((a?.case_results||[]).map(x=>[x.case_id,x])),right=new Map((b?.case_results||[]).map(x=>[x.case_id,x])),ids=[...new Set([...left.keys(),...right.keys()])];if(!ids.length)return '';return `<div class="report-section"><div class="report-section-title-row"><div><h2 class="report-section-title">逐题对照差异</h2><p class="report-section-sub">逐题对齐资源 A 与资源 B；差异用于定位复核对象，不直接等同于资源真伪结论。</p></div><span class="table-hint">${ids.length} 道题</span></div><div class="table-wrap report-wrap"><table class="report-table quality-report-table"><thead><tr><th>题目</th><th>类别</th><th>资源 A</th><th>资源 B</th><th>得分差</th><th>耗时差</th><th>差异说明</th></tr></thead><tbody>${ids.map(id=>{const x=left.get(id)||{},y=right.get(id)||{},scoreA=Number(x.score),scoreB=Number(y.score),timeA=x.total_ms==null?NaN:Number(x.total_ms),timeB=y.total_ms==null?NaN:Number(y.total_ms),scoreDiff=Number.isFinite(scoreA)&&Number.isFinite(scoreB)?(scoreB-scoreA)*100:null,timeDiff=Number.isFinite(timeA)&&Number.isFinite(timeB)?timeB-timeA:null;const changed=x.case_id&&y.case_id&&x.passed!==y.passed,slow=timeDiff!=null&&Math.abs(timeDiff)>=100,leftLabel=x.case_id?(x.passed?'通过':'未通过'):'未返回',rightLabel=y.case_id?(y.passed?'通过':'未通过'):'未返回';return `<tr><td><b>${esc(id)}</b></td><td><b>${esc(categoryLabel(x.category||y.category))}</b><small class="role-label">${esc(x.category||y.category||'—')}</small></td><td><span class="tag ${x.case_id?(x.passed?'ok':'error'):'muted'}">${leftLabel}</span></td><td><span class="tag ${y.case_id?(y.passed?'ok':'error'):'muted'}">${rightLabel}</span></td><td class="${scoreDiff!=null&&scoreDiff<0?'delta-risk':scoreDiff!=null&&scoreDiff>0?'delta-good':'delta-neutral'}">${signedMetric(scoreDiff,'%')}</td><td class="${timeDiff!=null&&timeDiff>0?'delta-risk':'delta-neutral'}">${signedMetric(timeDiff,' ms')}</td><td>${!x.case_id||!y.case_id?'等待两侧结果':changed?'通过结果不同':slow?(timeDiff>0?'资源 B 更慢':'资源 B 更快'):'结果基本一致'}</td></tr>`}).join('')}</tbody></table></div></div>`}
function qualityResourceHtml(q,r){
 const i=(q.resources||[]).indexOf(r),s=q.report?.resources?.[i]||{},active=['running','waiting','stopping'].includes(q.status);
 return `<div class="group-detail"><div class="group-detail-head"><div><div class="report-kicker">资源实例 · 逐题明细</div><h2>${esc(r.name||'资源')}</h2><p>模型 ${esc(r.model||'—')} · 通过 ${s.passed_cases??0} / ${s.total_cases??0} 题</p></div><span class="group-badge">${qualityStatusLabel(r.status||q.status)}</span></div>`+
 UI.metrics([{label:'内部质量分',value:comparisonMetric(s.score_1000??(s.score_pct!=null?s.score_pct*10:null)),unit:'/ 1000',note:'固定题集内部尺度，完整结论见结论标签页'},{label:'请求错误',value:s.request_errors??'—',unit:'题',note:'链路失败计零分，耗时不计入质量分'},{label:'成功响应中位耗时',value:comparisonMetric(s.p50_ms),unit:'毫秒'},{label:'成功响应95分位耗时',value:comparisonMetric(s.p95_ms),unit:'毫秒'}])+
 (r.error?UI.notice(r.error,{tone:'danger'}):'')+(active?qualityProgressHtml(q):'')+qualityCaseTable(q,r,i)+'</div>';
}
function qualityScrollSnapshot(host){
 const focused=document.activeElement;
 return {main:document.querySelector('.main')?.scrollTop||0,shell:document.querySelector('.shell')?.scrollTop||0,tables:[...host.querySelectorAll('.table-wrap')].map(el=>[el.scrollTop,el.scrollLeft]),
  disclosures:[...host.querySelectorAll('details')].map(el=>({id:el.id,summary:el.querySelector(':scope > summary')?.textContent,open:el.open})),
  focus:host.contains(focused)&&focused?.dataset?.clickAction?{...focused.dataset}:null,
  summaryFocus:host.contains(focused)&&focused?.tagName==='SUMMARY'?[...host.querySelectorAll('details > summary')].indexOf(focused):-1};
}
function restoreQualityScroll(host,snapshot){
 const main=document.querySelector('.main'),shell=document.querySelector('.shell');if(!snapshot){if(main)main.scrollTop=0;if(shell)shell.scrollTop=0;return}
 host.querySelectorAll('details').forEach((el,i)=>{const old=snapshot.disclosures?.[i];if(old&&(old.id?old.id===el.id:old.summary===el.querySelector(':scope > summary')?.textContent))el.open=old.open});
 host.querySelectorAll('.table-wrap').forEach((el,i)=>{el.scrollTop=snapshot.tables[i]?.[0]||0;el.scrollLeft=snapshot.tables[i]?.[1]||0});
 if(snapshot.focus){const button=[...host.querySelectorAll('[data-click-action]')].find(el=>Object.entries(snapshot.focus).every(([key,value])=>el.dataset[key]===value));if(button&&!button.disabled)button.focus({preventScroll:true})}
 else if(snapshot.summaryFocus>=0)host.querySelectorAll('details > summary')[snapshot.summaryFocus]?.focus({preventScroll:true});
 if(main)main.scrollTop=snapshot.main;
 if(shell)shell.scrollTop=snapshot.shell||0;
}
function showQualityCaseById(resourceIndex,caseId){const r=window.currentQualityDetail?.resources?.[resourceIndex];showQualityCase(resourceIndex,(r?.case_results||[]).findIndex(c=>c.case_id===caseId))}
function showQualityCase(resourceIndex,caseIndex){const q=window.currentQualityDetail,r=q?.resources?.[resourceIndex],x=r?.case_results?.[caseIndex];if(!x)return;const modal=$('qualityEvidenceModal');window.qualityEvidenceOpener=document.activeElement;openModal('qualityEvidenceModal');$('qualityEvidenceTitle').textContent=`${r.name} · ${x.title||x.case_id}`;const capture=q.config?.capture_raw;const pre=value=>esc(typeof value==='string'?value:JSON.stringify(value??null,null,2));$('qualityEvidenceBody').innerHTML=`<div class="report-meta"><span>${esc(x.case_id)}</span><span>${esc(categoryLabel(x.category))} · ${esc(x.difficulty||'未标注')}</span><span>得分 ${num(Number(x.score||0)*100)} / 100</span><span>${x.passed?'通过':'未通过'}${x.critical?' · 关键规则':''}</span><span>状态码 ${x.status_code??'未记录'}</span>${x.execution_sequence?`<span>本任务第 ${x.execution_sequence} 次请求</span>`:''}${x.input_chars!=null?`<span>输入 ${x.input_chars} 字符</span>`:''}</div>${UI.timeRecords(x,{request:true,key:'quality-evidence:'+q.id+':'+resourceIndex+':'+x.case_id})}<h3>题目与输入</h3><pre class="evidence-pre">${pre(x.prompt||'历史记录未保存题目')}</pre><div class="quality-scenario-grid"><div><h3>参考答案${x.evaluator?.type==='verifiable_constraints'?'（一种合规示例）':''}</h3><pre class="evidence-pre">${pre(x.reference_answer??'历史记录未保存参考答案')}</pre></div><div><h3>判分依据</h3><p>${esc(x.rationale||'历史记录未保存解释')}</p><p>检查项：${esc((x.checks||[]).join('；')||'未记录')}</p><p>未通过：${esc((x.failures||[]).join('；')||'无')}</p></div></div><h3>实际输出${capture?'':' · 仅保存的预览'}</h3><pre class="evidence-pre">${pre(x.response??x.response_preview??'没有输出')}</pre><p>${capture?'已开启原始记录。':'本次未开启原始记录，只有预览；不能补造完整正文。'}</p>${x.error?`<p role="alert">请求错误：${esc(x.error)}</p>`:''}<p>完整响应：${x.completed?'是':'否'} · 总耗时 ${num(x.total_ms)} 毫秒</p>${capture?`<details><summary>查看实际请求与原始响应</summary><h3>请求正文</h3><pre class="evidence-pre">${pre(x.raw_input??'旧记录没有保存')}</pre><h3>原始响应</h3><pre class="evidence-pre">${pre(x.raw_output??'没有保存原始响应正文')}</pre></details>`:''}`;modal.querySelector('.modal-body').scrollTop=0;modal.querySelector('.modal-close').focus()}
function closeQualityEvidence(){closeModal('qualityEvidenceModal')}
function renderQualityDetail(q){if(q._summary){showLoading();return refresh()}window.currentQualityDetail=q;const host=$('modulePlaceholder'),same=host.dataset.qualityId===q.id&&host.dataset.qualityTab===qualityTab,scroll=same?qualityScrollSnapshot(host):null;host.dataset.qualityId=q.id;host.dataset.qualityTab=qualityTab;$('loadingState').hidden=true;$('empty').hidden=true;$('detail').hidden=true;host.hidden=false;const resources=q.resources||[],tabs=[['report',resources.length===2?'对照结论':'结论'],...resources.map((r,i)=>[String(i),`${r.name||('资源 '+(i+1))} · 逐题`])];if(!tabs.some(x=>x[0]===qualityTab))qualityTab='report';const current=qualityTab==='report'?null:resources[Number(qualityTab)];const body=current?qualityResourceHtml(q,current):qualityReportHtml(q);const c=qualityConclusion(q),manifest=q.config?.suite_manifest||{};host.innerHTML=`<div class="detail-layout report-shell">${UI.pageHeader({kicker:`质量评测 · ${resources.length===2?'双资源配对对照':'单资源能力诊断'}`,title:q.name,description:resources.length===2?'先分别检查两侧是否达标，再比较同题胜负、维度差异和退化；不把分数接近当作等效。':'判断当前资源的优势、短板、阻断项与适用场景；不把完成度当作质量。',meta:[qualityStatusLabel(q.status),`${q.case_count||0} 题 / 资源`,manifest.title||'历史小题集 · 证据不足',q.config?.capture_raw?'已开启原始记录':'未开启原始记录'],statusLabel:qualityStatusLabel(q.status),statusNote:['waiting','running','stopping'].includes(q.status)?`${qualityProgress(q).done} / ${qualityProgress(q).total} 题已评测`:q.report?.evidence?.grade||'查看结论与证据',statusTone:['waiting','running','stopping'].includes(q.status)?'accent':{risk:'danger',caution:'warning',good:'success'}[c.level]||'neutral',actionsHtml:runHeaderAction('quality',q,'stopQuality','停止后续题目')})}<nav class="report-tabs" role="tablist" aria-label="质量测试详情标签页">${tabs.map(([id,label])=>`<button class="report-tab ${qualityTab===id?'active':''}" role="tab" aria-selected="${qualityTab===id}" type="button" data-click-action="setQualityTab" data-arg0="${q.id}" data-arg1="${id}">${esc(label)}</button>`).join('')}</nav>${artifactNotice(q)}${body}${UI.timeRecords(q,{key:'quality:'+q.id})}${UI.actionBar(UI.button('下载结论报告',{action:'download',args:[`/quality-report/${q.id}?format=markdown`]})+UI.button('下载报告与证据',{action:'download',args:[`/quality-report/${q.id}`]})+(['waiting','running','stopping'].includes(q.status)?'':UI.button('删除本地记录',{variant:'danger',action:'deleteQuality',args:[q.id]})))}</div>`;restoreQualityScroll(host,scroll)}
async function stopQuality(id){if(!confirm('停止后不再发出后续请求；正在执行的一题会完成并保存。部分结果不会生成完整结论。继续？'))return;try{await api('/quality-test/'+id+'?stop',{method:'POST'});toast('已请求停止后续题目');refresh()}catch(e){toast(e.message)}}

function comparisonGroupHtml(c,g){
 const s=g.summary||{},cnt=s.counters||{},done=Number(s.done||0),total=Number(g.iterations||s.total||0),success=s.success_pct==null?null:Number(s.success_pct),recent=s.recent||[],capture=Boolean(c.config?.capture_raw||s.config?.capture_raw)&&gatewayArtifactAvailable(s),taskId=g.task_id||s.id;
 const status=statusNames[g.status]||g.status||'待执行',active=['running','stopping'].includes(g.status);
 const groupPct=g.status==='finished'||g.status==='finished_with_errors'?100:(g.progress!=null?Math.round(Math.max(0,Math.min(1,Number(g.progress)||0))*100):(total?Math.round(Math.max(0,Math.min(1,done/total))*100):0));
 const failures=Object.entries({'限流 429':cnt.r429,'服务端 5xx（服务器错误）':cnt.r5xx,'客户端 4xx（请求错误）':cnt.r4xx,超时:cnt.timeout,网络错误:cnt.network,响应不完整:cnt.protocol}).filter(([,v])=>Number(v)>0);
 const progress=UI.progressCard({value:groupPct,kicker:'本组执行进度',summary:`已收到 ${num(done,0)} / ${num(total,0)} 条最终结果`,note:'完成度不代表速度；运行时长、完成吞吐与长尾耗时分别记录。'});
 const failureSummary=failures.length?UI.signalList(failures.map(([title,value])=>({title:`${title}：${num(value,0)} 条`,tone:title==='超时'||title.startsWith('服务端')?'danger':'warning'}))):done===0?UI.notice(active?'等待本组首条请求结果，暂无法判断成功率和失败分布。':'本组尚无已完成的请求结果，无法判断成功率和失败分布。'):cnt.ok!=null&&Number(cnt.ok)===done?UI.notice(`已收到的 ${num(done,0)} 条请求均成功，当前结果中未记录失败。`,{tone:'success'}):UI.notice('本组已收到请求结果，但失败分类记录不完整；请结合下方逐请求结果核查。',{tone:'warning'});
 const metrics='<div class="report-metrics">'+UI.metric({label:'成功率',value:success==null?'—':num(success),unit:success==null?'':'%',note:'仅完整 2xx（成功）响应计为成功'})+UI.metric({label:'完成吞吐',value:num(s.rpm?.avg),unit:'RPM（每分钟请求数）',note:'按实际完成结果和运行窗口计算'})+UI.metric({label:'总耗时 p50 / p95（分位耗时）',value:num(s.total_ms?.p50)+' / '+num(s.total_ms?.p95),unit:'毫秒',note:'长尾耗时需结合超时和失败分布判断'})+gatewayCacheMetric(s)+'</div>';
 const process=UI.disclosure({section:true,id:`group-process-${c.id||''}-${g.id||''}`,title:`执行过程 · 已收到 ${num(done,0)} / ${num(total,0)} 条结果`,bodyHtml:UI.metrics([{label:'实际运行时长',value:duration(Number(g.elapsed||s.elapsed||0)),note:'从本组开始到最后一个结果完成'},{label:'已完成请求',value:num(done,0),unit:`/ ${num(total,0)} 条`,note:'已收到最终结果的请求；不等同于成功数'}])+(active?'':progress)});
 return `<div class="group-detail"><div class="group-detail-head"><div><div class="report-kicker">实验组 · ${g.role==='baseline'?'基准组':'对比组'}</div><h2>${esc(g.name||'实验组')}</h2><p>并发上限 ${num(g.concurrency)} · 计划调度 ${num(g.rpm)} RPM（每分钟请求数）· 计划 ${num(total,0)} 条请求</p></div><span class="group-badge ${g.status==='finished'?'good':['finished_with_errors','stopped','error'].includes(g.status)?'caution':g.status==='running'?'running':''}">${esc(status)}</span></div>${artifactNotice(s)}${active?progress:''}${metrics}${failureSummary}${gatewayCacheDetails({...s,id:taskId||g.id})}<section class="report-section"><div class="report-section-title-row"><div><h2 class="report-section-title">请求结果</h2><p class="report-section-sub">页面展示最近 ${num(recent.length,0)} 条；${gatewayArtifactAvailable(s)?'完整结果保存在本次实验组的逐行结构化数据文件中。':'结果文件已不可用，当前仅保留历史摘要。'}${capture?'已开启原始输入输出记录，点击下方“查看”。':!gatewayArtifactAvailable(s)?'本次原始记录已不可读取。':'本次未开启原始记录，无法补录历史正文。'}</p></div></div><div class="table-wrap report-wrap"><table class="report-table"><thead><tr><th>请求编号</th><th>结果与状态码</th><th>耗时与时间</th><th>原因</th>${capture?'<th>原始记录</th>':''}</tr></thead><tbody>${recent.length?recent.map(r=>`<tr><td>${num(r.id,0)}<small class="role-label">${esc(r.request_id||'请求标识未记录')}</small></td><td><span class="tag ${esc(r.cls||'muted')}">${esc(r.label||'—')}</span><small class="role-label">状态码 ${esc(r.status??'未记录')}</small></td><td>${requestLatencyHtml(r,'group-request:'+taskId+':'+r.id,{total:r.total,first:r.ttft})}</td><td title="${esc(gatewayErrorText(r))}">${esc(gatewayErrorText(r))}</td>${capture?`<td>${taskId&&Number.isInteger(r.id)?`<button type="button" class="raw-link" data-click-action="showRaw" data-arg0="${esc(taskId)}" data-arg1="${Number(r.id)}">查看</button>`:'暂不可用'}</td>`:''}</tr>`).join(''):`<tr><td colspan="${capture?5:4}">当前组尚无可展示的请求结果。</td></tr>`}</tbody></table></div></section>${process}${UI.timeRecords(g.timing?g:{...g,timing:s.timing},{key:'group:'+c.id+':'+g.id})}</div>`
}
function renderComparisonDetail(c){if(c._summary){showLoading();return refresh()}const host=$('modulePlaceholder'),same=host.dataset.comparisonId===c.id&&host.dataset.comparisonTab===comparisonTab,scroll=same?qualityScrollSnapshot(host):null;host.dataset.comparisonId=c.id;host.dataset.comparisonTab=comparisonTab;$('loadingState').hidden=true;$('empty').hidden=true;$('detail').hidden=true;$('modulePlaceholder').hidden=false;const tabs=[['report','对照结论'],...(c.groups||[]).map(g=>[g.id,(g.role==='baseline'?'基准组 · ':'对比组 · ')+(g.name||'实验组')])];if(!tabs.some(x=>x[0]===comparisonTab))comparisonTab='report';const currentGroup=(c.groups||[]).find(x=>x.id===comparisonTab);const report=c.report?.conclusion||{};const live=['waiting','running','stopping'].includes(c.status);const body=comparisonTab==='report'?(live?comparisonOverviewHtml(c):'')+comparisonReportHtml(c,{withoutOverview:true})+(typeof comparisonSupplementHtml==='function'?comparisonSupplementHtml(c):'')+(live?'':comparisonOverviewHtml(c)):currentGroup?comparisonGroupHtml(c,currentGroup):'';$('modulePlaceholder').innerHTML=`<div class="detail-layout report-shell">${UI.pageHeader({kicker:'网关压测 · 对照实验记录',title:c.name,description:'一条实验记录承载公共配置和全部压力组；在标签页中分别查看结论、基准组和对比组。',meta:[reportStatus(c.status),`${num((c.groups||[]).length,0)} 个实验组`,c.execution_policy==='serial'?'串行执行':'并行执行',`基准组：${(c.groups||[]).find(g=>g.role==='baseline')?.name||'—'}`],statusLabel:reportTag(report.level),statusNote:report.title||'等待结果',statusTone:{risk:'danger',caution:'warning',good:'success'}[report.level]||'neutral',actionsHtml:window.RepeatUI?.button('comparison',c)||''})}<nav class="report-tabs" role="tablist" aria-label="对照实验详情标签页">${tabs.map(([id,label])=>`<button class="report-tab ${comparisonTab===id?'active':''}" role="tab" aria-selected="${comparisonTab===id}" type="button" data-click-action="setComparisonTab" data-arg0="${c.id}" data-arg1="${esc(id)}">${esc(label)}${id===report?.baseline_group?' · 当前基准':''}</button>`).join('')}</nav>${body}${UI.timeRecords(c,{key:'comparison:'+c.id})}${['waiting','running','stopping'].includes(c.status)?'':UI.actionBar(UI.button('删除本地记录',{variant:'danger',action:'deleteComparison',args:[c.id]}))}</div>`;restoreQualityScroll(host,scroll)}
function resetCreateForm(){manualConnectionDrafts=new WeakMap();document.querySelectorAll('#config details:not([id^="connection-details-"])').forEach(node=>node.open=false);resetPressureDrafts();const picker=$('templatePicker');if(picker){picker.hidden=isQualityCreate()||isRoutingCreate();const body=$('templatePickerBody'),btn=$('templatePickerToggle');if(body)body.hidden=true;if(btn){btn.setAttribute('aria-expanded','false');btn.textContent='展开'}}if(!isQualityCreate()&&!isRoutingCreate()&&templateCache)renderTemplatePicker(templateCache);setCreateKind(activeModule==='routing'?'routing-compare':activeModule==='quality'?(activeView==='compare'?'quality-compare':'quality-single'):(activeView==='compare'?'gateway-compare':'gateway-single'),true);clearTimeout(preflightTimer);preflightSeq++;preflightData=null;preflightAckRequested=false;const card=$('preflightCard');if(card){card.hidden=true;card.innerHTML=''}if($('name'))$('name').value='';if($('comparisonName'))$('comparisonName').value='';if($('api_key')){$('api_key').value='';$('api_key').type='password'}if($('toggleKey'))resetKeyButton(true);updateKeyInputHint();if($('copyKey'))$('copyKey').textContent='复制';if($('capture_raw'))$('capture_raw').checked=false;if($('comparisonCapture'))$('comparisonCapture').checked=false;setSavedModelOption('');if($('profile')){ $('profile').value='smoke'; syncProfileHint(); }if(isGatewayCreate()){paintMode('smoke');applyDynamic({rpm:100,iterations:50,duration:60});if(createKind==='gateway-single')$('concurrency').value=20}renderSavedConnectionPickers()}
function selectDefaultModuleAfterCreate(kind,id){activeView=kind.includes('compare')?'compare':'single';if(kind==='routing-compare'){activeModule='routing';routingTab='report';selectedRouting=id;selected=null;selectedComparison=null;selectedQuality=null}else if(kind==='quality-single'||kind==='quality-compare'){activeModule='quality';qualityTab='report';selectedQuality=id;selected=null;selectedComparison=null}else if(kind==='gateway-compare'){activeModule='gateway';selectedComparison=id;selected=null;selectedQuality=null}else{activeModule='gateway';selected=id;selectedComparison=null;selectedQuality=null}updateWorkspaceChrome()}
document.querySelectorAll('.module-nav-item').forEach(x=>x.onclick=()=>setModule(x.dataset.module));document.querySelectorAll('.subnav-item').forEach(x=>x.onclick=()=>setWorkspaceView(x.dataset.view));
function togglePasswordVisibility(id,button){const input=$(id);if(!input||!button)return;const visible=input.type==='text';input.type=visible?'password':'text';button.setAttribute('aria-pressed',String(!visible));button.setAttribute('aria-label',visible?'显示密码':'隐藏密码');button.innerHTML=UI.icon(visible?'eye':'eyeOff');}
function esc(v){return UI.esc(v)}function num(v,d='—'){return v==null||Number.isNaN(v)?d:Number(v).toLocaleString('zh-CN',{maximumFractionDigits:1})}function duration(sec){sec=Math.max(0,Math.floor(sec||0));if(sec<60)return sec+' 秒';if(sec<3600)return Math.floor(sec/60)+'分 '+sec%60+'秒';return Math.floor(sec/3600)+'时 '+Math.floor(sec%3600/60)+'分 '+sec%60+'秒'}function durationPrecise(sec){sec=Math.max(0,Number(sec||0));if(sec<1)return Math.max(.1,sec).toLocaleString('zh-CN',{maximumFractionDigits:1})+' 秒';return duration(sec)}function toast(t){
 const el=$('toast');if(!el)return;
 clearTimeout(window.__toastTimer);el.classList.remove('show');el.textContent=t;
 const drawer=Array.from(document.querySelectorAll('.config[aria-hidden="false"]')).find(node=>!node.inert),footer=drawer?.querySelector('.drawer-footer');
 let feedback=$('drawerToast');if(feedback)feedback.hidden=true;
 if(footer){
  // 抽屉内反馈进入文档流，留出主按钮的位置；已在底栏展示的错误不重复堆叠。
  const duplicate=Array.from(footer.querySelectorAll('[role="status"],[role="alert"]')).some(node=>node!==feedback&&!node.hidden&&node.textContent===t);
  if(!duplicate){if(!feedback){feedback=document.createElement('div');feedback.id='drawerToast';feedback.setAttribute('role','status');feedback.setAttribute('aria-live','polite')}feedback.innerHTML=UI.hint(t);footer.prepend(feedback);feedback.hidden=false}
 }else el.classList.add('show');
 window.__toastTimer=setTimeout(()=>{el.classList.remove('show');if(feedback)feedback.hidden=true},2400);
}
function clearTransientFeedback(){clearTimeout(window.__toastTimer);$('toast')?.classList.remove('show');const feedback=$('drawerToast');if(feedback)feedback.hidden=true}
function syncProfileHint(){const select=$('profile'),hint=$('profileHint');if(!select||!hint)return;const hints={smoke:'常规短问答会轮换独立样本，适合快速检查接口可用性。',short:'最短响应只验证低输出开销，不用于评估缓存复用。',long:'长输入使用稳定材料轮换问题，适合观察长上下文性能。',cache:'缓存压测固定长前缀，只改变末尾问题；需要上游返回 usage（用量）中的 cached_tokens（缓存词元）才能统计命中。命中率 = cached_tokens（缓存词元）> 0 的请求数 ÷ 总请求数。'};hint.textContent=hints[select.value]||hints.smoke}
if($('profile')){$('profile').addEventListener('change',syncProfileHint);syncProfileHint()}
function paintMode(next){if(!Object.hasOwn(modeTemplates,next))return;mode=next;document.querySelectorAll('.tab[data-mode]').forEach(x=>{x.classList.toggle('active',x.dataset.mode===mode);x.setAttribute('aria-selected',String(x.dataset.mode===mode))});const host=$('modeFields');host.innerHTML=modeTemplates[mode];host.dataset.mode=mode}
function setMode(next){
 if(!Object.hasOwn(modeTemplates,next)||next===mode&&$('modeFields')?.dataset.mode===next)return;
 // 每档时长和长跑总时长虽然同名，含义不同；每种压力模式独立保留本次草稿。
 pressureModeDrafts[mode]=readDynamic();paintMode(next);applyDynamic(pressureModeDrafts[next]||{});
 clearActiveTemplate();schedulePreflight()
}
document.querySelectorAll('.tab[data-mode]').forEach(x=>x.onclick=()=>setMode(x.dataset.mode));
function readDynamic(){const draft={};for(const key of ['rpm','iterations','ramps','duration'])if($(key))draft[key]=$(key).value;return draft}
function applyDynamic(cfg={}){for(const key of ['rpm','iterations','ramps','duration'])if($(key)&&cfg[key]!=null)$(key).value=cfg[key]}
function resetPressureDrafts(){pressureModeDrafts=Object.create(null);templateApplySeq++;activeTemplateId=null;const status=$('formStatus');if(status)delete status.dataset.templateMessage}
function clearActiveTemplate(){
 clearGatewayFormError();
 templateApplySeq++;
 if(!activeTemplateId)return;
 activeTemplateId=null;if(templateCache)renderTemplatePicker(templateCache);
 const status=$('formStatus');if(status?.dataset.templateMessage==='true'){status.textContent='压力参数已调整，当前使用自定义配置。';delete status.dataset.templateMessage}
}
function pressureInputChanged(event){if(isGatewayCreate()&&event.target.closest?.('#config'))clearGatewayFormError();if(event.target.closest?.('#modeFields,#concurrency,#connect_timeout,#read_timeout')){event.target.removeAttribute('aria-invalid');clearActiveTemplate()}}
document.addEventListener('input',pressureInputChanged);
document.addEventListener('change',pressureInputChanged);
let currentUser=null,authenticated=false,authSetupRequired=false,refreshTimer=null,authFocusTimer=null;
function stopRefresh(){responseCache.clear();lastRefreshSignature="";if(refreshTimer!==null)clearInterval(refreshTimer);refreshTimer=null;refreshSeq++;refreshing=false;}
function authGate(show,setup=false){
 const gate=$('authGate');if(!gate)return;
 const opening=show&&gate.hidden;
 if(authFocusTimer!==null){clearTimeout(authFocusTimer);authFocusTimer=null}
 gate.hidden=!show;
 $('authTitle').textContent=setup?'初始化管理员账号':'登录网关压测工作台';
 $('authHint').textContent=setup?'首次使用请创建系统管理员账号，之后由管理员创建并分发使用者账号。':'请输入系统管理员分发的账号。';
 $('authConfirmField').hidden=!setup;$('authConfirm').required=setup;
 $('authSubmit').textContent=setup?'创建管理员并进入':'登录';
 // 登录界面出现后停止受保护接口轮询，并作废旧请求，避免重复抢焦点或过期响应覆盖新会话。
 document.querySelector('.app').inert=show||$('config').classList.contains('mobile-open');
 if(show){window.TpmUI?.reset();window.RepeatUI?.reset();window.ConnectionUI?.reset();window.FingerprintUI?.onAuthLost();toggleConfig(false);stopRefresh();for(const {id} of modalStack.splice(0))$(id).hidden=true;document.body.classList.remove('modal-open')}
 // 只在首次打开时自动聚焦；用户已选中表单控件时，延迟回调不得覆盖其选择。
 if(opening)authFocusTimer=setTimeout(()=>{authFocusTimer=null;if(!gate.hidden&&!gate.contains(document.activeElement))$('authUsername')?.focus({preventScroll:true})},0);
}
function updateAccountChrome(){const name=$('accountUser');if(name)name.textContent=currentUser?`${currentUser.username} · ${currentUser.role==='admin'?'管理员':'使用者'}`:'';const manage=$('accountManage');if(manage)manage.hidden=currentUser?.role!=='admin'}
async function initAuth(){try{const d=await fetch('/auth/status',{cache:'no-store'}).then(async r=>{const j=await r.json();if(!r.ok)throw new Error(j.error||'认证状态读取失败');return j});authSetupRequired=Boolean(d.setup_required);authenticated=Boolean(d.authenticated);currentUser=d.user||null;updateAccountChrome();if(!authenticated)authGate(true,authSetupRequired);else authGate(false);return authenticated}catch(e){authGate(true,false);$('authError').textContent=e.message||'无法读取登录状态';return false}}
async function logout(){try{await api('/auth/logout',{method:'POST'});authenticated=false;currentUser=null;updateAccountChrome();authGate(true,false)}catch(e){toast(e.message)}}
$('authForm').addEventListener('submit',async e=>{e.preventDefault();const button=$('authSubmit'),error=$('authError'),setup=authSetupRequired,username=$('authUsername').value.trim(),password=$('authPassword').value,confirm=$('authConfirm').value;error.textContent='';if(setup&&password!==confirm){error.textContent='两次输入的密码不一致';return}button.disabled=true;button.textContent=setup?'创建中…':'登录中…';try{const d=await fetch(setup?'/auth/setup':'/auth/login',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({username,password})}).then(async r=>{const j=await r.json();if(!r.ok)throw new Error(j.error||'登录失败');return j});authenticated=true;currentUser=d.user||null;updateAccountChrome();authGate(false);$('authPassword').value='';$('authConfirm').value='';await bootApp()}catch(err){error.textContent=err.message||'操作失败'}finally{button.disabled=false;button.textContent=setup?'创建管理员并进入':'登录'}});
async function api(url,options={}){const controller=new AbortController();const timer=setTimeout(()=>controller.abort(),Number(options.timeout||8000));const cached=url.startsWith('/tasks?')?responseCache.get(url):null;const requestOptions={...options,headers:{...options.headers,...(cached?{'If-None-Match':cached.etag}:{})},cache:'no-store',signal:controller.signal};delete requestOptions.timeout;try{const r=await fetch(url,requestOptions);if(r.status===304&&cached)return cached.data;let d={};try{d=await r.json()}catch{}if(!r.ok)throw new Error(d.error||`请求失败（${r.status}）`);if(r.headers.get('ETag')){if(responseCache.size>20)responseCache.clear();responseCache.set(url,{etag:r.headers.get('ETag'),data:d})}return d}catch(e){if(e?.name==='AbortError')throw new Error('请求超时，请检查本地服务是否仍在运行');throw e}finally{clearTimeout(timer)}}function savedConfig(){try{const value=JSON.parse(localStorage.getItem('route_ui_config')||'{}');return value&&typeof value==='object'&&!Array.isArray(value)?value:{}}catch{return{}}}function parseBool(value,fallback=false){if(typeof value==='boolean')return value;if(typeof value==='number')return value===1;if(typeof value==='string'){const v=value.trim().toLowerCase();if(['true','1','yes','on'].includes(v))return true;if(['false','0','no','off',''].includes(v))return false}return fallback}function saveConfig(cfg){const safe={...cfg};delete safe.api_key;delete safe.resources;delete safe.shared_config;localStorage.setItem('route_ui_config',JSON.stringify(safe))}
function currentModel(){return $('modelManualWrap')?.hidden?String($('modelSelect')?.value||''):($('model')?.value||'').trim()}
function setManualModel(enabled, value, focus=false){
 const wrap=$('modelManualWrap'),input=$('model'),select=$('modelSelect'),toggle=$('manualModelToggle');if(!wrap||!input)return;
 wrap.hidden=!enabled;input.disabled=!enabled;
 if(select){select.disabled=enabled||modelFetchState==='loading';const row=select.closest('.model-row');if(row)row.hidden=enabled}
 if($('modelListHint'))$('modelListHint').hidden=enabled;
 if(toggle){toggle.setAttribute('aria-expanded',String(enabled));toggle.textContent=enabled?'返回模型列表':'无法获取列表？手动指定模型'}
 if(enabled&&value!=null)input.value=value;
 if(focus)(enabled?input:select)?.focus();
}
function setModelHint(text, tone=''){const hint=$('modelListHint');if(!hint)return;hint.textContent=text;hint.classList.toggle('error',tone==='error');hint.classList.toggle('ok',tone==='ok')}
function setSavedModelOption(model){const select=$('modelSelect');const value=String(model||'').trim();if(!select)return;modelRequestSeq++;select.innerHTML=value?`<option value="${esc(value)}">已保存模型：${esc(value)}（未验证）</option>`:'<option value="">请先填写 URL 和 API Key，再获取模型</option>';select.value=value;modelFetchState=value?'stale':'idle';modelSource=value?'saved':'none';modelList=[];if($('model'))$('model').value='';setManualModel(false);setModelHint(value?'已恢复上次使用的模型；点击“刷新模型”验证并获取最新列表。':'尚未获取模型列表。')}
function fillModelOptions(models, current=''){
 const select=$('modelSelect');if(!select)return;
 const clean=[...new Set((models||[]).map(x=>String(x||'').trim()).filter(Boolean))],value=String(current||''),missing=value&&!clean.includes(value);
 modelList=clean;modelFetchState=clean.length?'success':'empty';modelSource=missing?'stale':'fetched';
 select.innerHTML=(clean.length?'<option value="">请选择模型</option>':'<option value="">未获取到可用模型</option>')+(missing?`<option value="${esc(value)}">${esc(value)}（本次列表未包含，未验证）</option>`:'')+clean.map(x=>`<option value="${esc(x)}">${esc(x)}</option>`).join('');
 select.value=value;setManualModel(false);
 setModelHint(missing?'本次列表未包含原选模型，已保留输入但尚未验证；请核对模型标识，或选择列表中的模型。':clean.length?`已成功获取 ${clean.length} 个模型，请从下拉菜单选择。`:'请求已完成，但网关没有返回可用模型；如该网关不支持 /models，可手动指定。',missing||!clean.length?'error':'ok');
}
function clearModelOptions(message,current=''){const select=$('modelSelect');if(!select)return;const value=String(current||'').trim();modelList=[];modelFetchState='error';modelSource=value?'stale':'none';select.innerHTML=value?`<option value="${esc(value)}">当前模型：${esc(value)}（刷新失败，未验证）</option>`:'<option value="">刷新失败，暂无可选模型</option>';select.value=value;setManualModel(false);setModelHint(`${message||'刷新模型失败'}；请修正 URL 或 API Key 后重试。${value?'当前保留的是上次模型，未代表本次获取成功。':''}`,'error')}
async function loadConfig(){
 let cfg=savedConfig();try{const serverCfg=(await api('/config')).config||{};cfg={...cfg,...serverCfg}}catch{};
 if(cfg.mode)setMode(cfg.mode);else setMode('smoke');
 const box=$('gatewayConnectionPicker'),edited=box?.dataset.connectionUserEdited==='true';
 for(const k of ['name','url','concurrency','connect_timeout','read_timeout','profile'])if($(k)&&cfg[k]!=null&&!(k==='url'&&edited))$(k).value=cfg[k];
 syncProfileHint();
 if(box&&!edited&&!$('api_key').value.trim()){box.dataset.connectionId=cfg.connection_id||'';box.dataset.connectionUrl=String(cfg.url||'').trim();box.dataset.connectionApiKeySaved=String(Boolean(cfg.api_key_saved))}
 applyDynamic(cfg);$('capture_raw').checked=parseBool(cfg.capture_raw,false);if(!edited)setSavedModelOption(String(cfg.model||''));
 if(!edited){window.savedApiKey=Boolean(cfg.api_key_saved);window.savedApiKeyHint=String(cfg.api_key_hint||'')}
 updateKeyInputHint();renderSavedConnectionPickers();loadTemplates();loadRoutingCorpus();
}
async function loadRoutingCorpus(){
 $('routingCorpusRetry').disabled=true;
 try{const d=await api('/routing-corpus');routingCorpus=d.tiers||[];routingMetrics=d.metrics||[]}catch(e){routingCorpus=null}
 finally{$('routingCorpusRetry').disabled=false}
 if(isRoutingCreate()){renderRoutingFields();schedulePreflight()}
}
function readConfig(){const v=id=>$(id)?.value;return{name:v('name'),url:v('url'),api_key:v('api_key'),...(boundConnectionId('gateway')?{connection_id:boundConnectionId('gateway')} :{}),model:currentModel(),mode,rpm:v('rpm')||100,iterations:v('iterations')||50,ramps:v('ramps')||'',duration:v('duration')||60,concurrency:v('concurrency')||20,connect_timeout:v('connect_timeout')||10,read_timeout:v('read_timeout')||60,write_timeout:30,profile:v('profile')||'smoke',stream:true,capture_raw:$('capture_raw').checked}}
const eyeOpen='<svg class="ui-icon" viewBox="0 0 24 24" aria-hidden="true"><path d="M2.2 12s3.4-5.3 9.8-5.3 9.8 5.3 9.8 5.3-3.4 5.3-9.8 5.3S2.2 12 2.2 12Z"></path><circle cx="12" cy="12" r="2.7"></circle></svg>',eyeClosed='<svg class="ui-icon" viewBox="0 0 24 24" aria-hidden="true"><path d="M3 3l18 18"></path><path d="M10.6 6.9A10.9 10.9 0 0 1 12 6.7c6.4 0 9.8 5.3 9.8 5.3a18.6 18.6 0 0 1-3.1 3.4M6.2 6.9C3.7 8.5 2.2 12 2.2 12s3.4 5.3 9.8 5.3c.8 0 1.5-.1 2.2-.2"></path></svg>';function resetKeyButton(hidden=true){$('toggleKey').innerHTML=hidden?eyeOpen:eyeClosed;$('toggleKey').setAttribute('aria-pressed',String(!hidden));$('toggleKey').setAttribute('aria-label',hidden?'显示 API Key（应用程序编程接口密钥）':'隐藏 API Key（应用程序编程接口密钥）');$('toggleKey').title=$('toggleKey').getAttribute('aria-label')}
function scrollKeyToEnd(){const input=$('api_key');if(input)requestAnimationFrame(()=>{input.scrollLeft=input.scrollWidth})}
function updateKeyInputHint(){const input=$('api_key'),hint=$('keyInputHint'),n=input?.value?.length||0;if(!hint)return;const profile=boundConnectionProfile('gateway');if(boundConnectionId('gateway')){hint.textContent=$('gatewayConnectionPicker')?.dataset.connectionInvalid==='true'?'常用连接已删除或地址已更新，请重新选择；当前绑定不会改用其他密钥。':profile?.api_key_saved||$('gatewayConnectionPicker')?.dataset.connectionApiKeySaved==='true'?'已选择常用连接；密钥留空即可复用，修改地址或密钥将切换为手动填写。':'该常用连接的密钥不可用，请填写密钥后另存或更新。';return}if(window.secureStoreAvailable===false){hint.textContent='系统安全存储不可用；密钥仅供本次运行，重测时需重新填写。';return}if(n)hint.textContent=`已输入 ${n} 个字符；启动成功后会加密保存到 系统钥匙串。`;else if(window.savedApiKey)hint.textContent=`已保存密钥 ${window.savedApiKeyHint||''}；留空将直接复用，输入新值会覆盖。`;else hint.textContent='尚未保存密钥；输入后会加密保存到 系统钥匙串。'}
$('toggleKey').onclick=()=>{const input=$('api_key');const visible=input.type==='text';input.type=visible?'password':'text';resetKeyButton(visible);scrollKeyToEnd()};
$('api_key').addEventListener('focus',scrollKeyToEnd);$('api_key').addEventListener('input',()=>{scrollKeyToEnd();updateKeyInputHint()});$('api_key').addEventListener('paste',()=>setTimeout(updateKeyInputHint,0));
$('copyKey').onclick=async()=>{const value=$('api_key').value;if(!value)return toast('没有可复制的 API Key（应用程序编程接口密钥）');try{await navigator.clipboard.writeText(value);$('copyKey').textContent='已复制';setTimeout(()=>{$('copyKey').textContent='复制'},1200);toast('API Key 已复制到剪贴板')}catch{toast('浏览器未允许访问剪贴板，请手动复制')}};
$('manualModelToggle').onclick=()=>{if(modelFetchState==='loading')cancelGatewayModelRequest();setManualModel($('modelManualWrap').hidden,undefined,true);schedulePreflight()};
let credentialStatusSeq=0;async function checkSavedCredential(){if(boundConnectionId('gateway')){updateBoundConnectionHint('gateway');return}const url=String($('url')?.value||'').trim();if(!url){window.savedApiKey=false;window.savedApiKeyHint='';updateKeyInputHint();return}const seq=++credentialStatusSeq;try{const d=await api('/credential-status?url='+encodeURIComponent(url));if(seq!==credentialStatusSeq||boundConnectionId('gateway'))return;window.secureStoreAvailable=d.storage_available!==false;window.savedApiKey=Boolean(d.saved);window.savedApiKeyHint=String(d.hint||'');updateKeyInputHint()}catch{if(seq===credentialStatusSeq){window.savedApiKey=false;window.savedApiKeyHint='';updateKeyInputHint()}}}function invalidateModelFetch(){
 const manual=!$('modelManualWrap').hidden,draft=$('model').value,current=$('modelSelect').value;
 setSavedModelOption(current);$('model').value=draft;setManualModel(manual);
 if(current||draft)setModelHint('地址或密钥已变化，当前模型尚未针对新配置验证；请重新获取。','error');
 const button=$('loadModels');button.disabled=false;button.removeAttribute('aria-busy');button.textContent='刷新模型';button.classList.remove('done');button.closest('.model-row')?.classList.remove('is-loading','is-done','is-error');
 if($('api_key')?.value==='')checkSavedCredential();
}
$('modelSelect').onchange=()=>{setManualModel(false);if(modelList.includes($('modelSelect').value))setModelHint(`已选择 ${$('modelSelect').value}。`,'ok');schedulePreflight()};
$('url').addEventListener('input',invalidateModelFetch);$('api_key').addEventListener('input',invalidateModelFetch);document.addEventListener('input',qualityCreationChanged);document.addEventListener('change',qualityCreationChanged);document.addEventListener('focusout',e=>{if(e.target.matches('.quality-url'))checkQualityCredential(e.target.closest('.quality-resource'))});
function cancelGatewayModelRequest(){
 modelRequestSeq++;if(modelFetchState==='loading')modelFetchState='stale';
 const button=$('loadModels');button.disabled=false;button.removeAttribute('aria-busy');button.textContent='刷新模型';button.closest('.model-row')?.classList.remove('is-loading','is-done','is-error');
 if($('modelSelect'))$('modelSelect').disabled=!$('modelManualWrap').hidden;
}
$('loadModels').onclick=async()=>{
 const button=$('loadModels');if(button.disabled||!$('modelManualWrap').hidden)return;
 const cfg=readConfig(),row=button.closest('.model-row');
 if(!cfg.url||(!cfg.api_key&&!window.savedApiKey)){setModelHint('请先填写地址；接口密钥可输入新值或复用已保存密钥。','error');return}
 const requestId=++modelRequestSeq;
 modelFetchState='loading';button.disabled=true;$('modelSelect').disabled=true;row.classList.add('is-loading');row.classList.remove('is-error','is-done');button.textContent='获取中…';button.setAttribute('aria-busy','true');setModelHint('正在请求网关的模型列表，请稍候…');
 try{
  const d=await api('/models',{method:'POST',timeout:35000,headers:{'Content-Type':'application/json'},body:JSON.stringify({url:cfg.url,api_key:cfg.api_key,...(cfg.connection_id?{connection_id:cfg.connection_id}:{})})});
  if(requestId!==modelRequestSeq)return;
  fillModelOptions(d.models,cfg.model);if(d.saved){window.savedApiKey=true;window.savedApiKeyHint=String(d.hint||'');updateKeyInputHint()}
  row.classList.add(d.models?.length?'is-done':'is-error');button.textContent=d.models?.length?`已获取 ${d.models.length} 个`:'重新获取';schedulePreflight();
 }catch(e){if(requestId===modelRequestSeq){clearModelOptions(e.message,cfg.model);row.classList.add('is-error');button.textContent='重新获取'}}
 finally{if(requestId===modelRequestSeq){button.disabled=false;button.removeAttribute('aria-busy');$('modelSelect').disabled=!$('modelManualWrap').hidden;row.classList.remove('is-loading');setTimeout(()=>{if(requestId!==modelRequestSeq)return;row.classList.remove('is-done','is-error');if(modelFetchState==='success')button.textContent='刷新模型'},1800)}}
};
$('submit').onclick=async()=>{const button=$('submit'),status=$('formStatus');if(button.disabled)return;button.disabled=true;status.className='form-status';status.textContent='正在校验配置并启动测试…';try{if(isRoutingCreate())validateRoutingForm();else if(isQualityCreate())validateQualityForm();else validateGatewayForm();const body=isRoutingCreate()?readRouting():isQualityCreate()?readQuality():isCompareCreate()?readComparison():readConfig();const endpoint=isRoutingCreate()?'/routing-comparisons':isQualityCreate()?'/quality-tests':isCompareCreate()?'/comparisons':'/tasks';if(endpoint!=='/comparisons'){await refreshPreflight();if(!(await ensurePreflightAck())){status.className='form-status';status.textContent='已取消：未确认在疑似生产环境中执行。';button.disabled=false;return}if(preflightData&&preflightData.requires_confirmation)body.risk_acknowledged=true}const d=await api(endpoint,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});if(createKind==='gateway-single')saveConfig(body.shared_config||body);$('api_key').value='';$('api_key').type='password';resetKeyButton(true);updateKeyInputHint();document.querySelectorAll('.quality-key,.routing-side-key').forEach(input=>{const side=input.closest('[data-routing-side]')?.dataset.routingSide;if(side)hideRoutingKey(side,true);input.value='';input.type='password'});const id=d.task_id||d.comparison_id||d.quality_test_id||d.routing_comparison_id;status.className='form-status ok';status.textContent=isRoutingCreate()?'路由对照已创建：两侧按同一清单交错发出，成功率、缓存和延迟指标会实时更新。':isQualityCreate()?(isCompareCreate()?'资源对照已创建，正在执行同一题集。':'评测已创建，正在执行题集。'):(isCompareCreate()?'压测对照实验已创建，实验组将按顺序执行。':'压测任务已创建，已加入执行队列。');const successMessage=status.textContent;toggleConfig(false);toast(successMessage);selectDefaultModuleAfterCreate(createKind,id);await refresh()}catch(e){status.className='form-status error';status.textContent=e.message;toast(e.message)}finally{button.disabled=false;setCreateKind(createKind,false)}};
function startNewTest(module=activeModule,view=activeView){
 activeModule=['gateway','quality','routing'].includes(module)?module:'gateway';activeView=view==='compare'?'compare':'single';
 const modelValue=currentModel(),keep={url:$('url').value,connect_timeout:$('connect_timeout').value,read_timeout:$('read_timeout').value};
 resetCreateForm();
 for(const [key,value] of Object.entries(keep))if($(key)&&value)$(key).value=value;
 setSavedModelOption(modelValue);
 if(isRoutingCreate()){$('routingName').value='';$('routingModel').value='';$('routingConcurrency').value='20';$('routingRpm').value='100';$('routingConnectTimeout').value='10';$('routingReadTimeout').value='60';$('routingCapture').checked=false}
 $('formStatus').className='form-status';$('formStatus').textContent='';
 toggleConfig(true);schedulePreflight();
}
function focusConfig(){openCreateForContext()}
let drawerOpener=null;
function toggleConfig(force){
 const panel=$('config'),open=force===undefined?!panel.classList.contains('mobile-open'):Boolean(force);
 clearTransientFeedback();
 if(open&&!panel.classList.contains('mobile-open'))drawerOpener=document.activeElement;
 panel.classList.toggle('mobile-open',open);panel.inert=!open;panel.setAttribute('aria-hidden',String(!open));
 $('drawerBackdrop').hidden=!open;document.body.classList.toggle('drawer-open',open);
 document.querySelector('.app').inert=open||!$('authGate').hidden;
 if(open){panel.querySelector('.drawer-body').scrollTop=0;$(isRoutingCreate()?'routingName':isQualityCreate()?'qualityName':isCompareCreate()?'comparisonName':'name')?.focus({preventScroll:true})}
 else{cancelGatewayModelRequest();manualConnectionDrafts=new WeakMap();if(isRoutingCreate()){saveRoutingConnections();for(const side of ['baseline','candidate'])hideRoutingKey(side,true);resetRoutingModels()}clearTimeout(preflightTimer);preflightSeq++;if(drawerOpener?.isConnected&&$('authGate').hidden)drawerOpener.focus({preventScroll:true})}
}
document.addEventListener('keydown',e=>{if(e.key==='Escape'&&$('config')?.classList.contains('mobile-open')&&['qualitySuiteModal','qualityEvidenceModal','qualityMethodologyModal','rawModal','accountModal','storageModal','diagnosticsModal'].every(id=>$(id)?.hidden!==false))toggleConfig(false)})
function routingConnectionChanged(e){
 if(e.target.closest('#routingFields')&&$('formStatus').classList.contains('error')){
  if($('toast').textContent===$('formStatus').textContent)$('toast').classList.remove('show');
  $('formStatus').className='form-status';$('formStatus').textContent='';
 }
 if(!e.target.closest('#routingSidePanels'))return;
 const side=e.target.closest('[data-routing-side]')?.dataset.routingSide,box=e.target.closest('[data-routing-side]');
 if(side){if(e.target.matches('.routing-side-key'))delete e.target.dataset.routingRevealed;if(e.target.matches('.routing-side-url'))hideRoutingKey(side);box.dataset.connectionEdited='true';e.target.dataset.routingEdited='true';clearTimeout(routingConnectionTimers[side]);routingConnectionTimers[side]=setTimeout(()=>saveRoutingSide(side),500)}
 if(e.target.matches('.routing-side-url')||(e.target.matches('.routing-side-key')&&!e.target.value.trim()))checkRoutingCredential(side);
 updateRoutingSideStatus();
 if(e.target.matches('.routing-side-url,.routing-side-key'))resetRoutingModels('连接信息已更新，请重新获取两侧模型列表。');
}
document.addEventListener('input',routingConnectionChanged);
document.addEventListener('change',routingConnectionChanged);
document.addEventListener('keydown',e=>{
 const routeTab=e.target.closest?.('#routingSideTabs [role="tab"]');
 if(routeTab&&['ArrowLeft','ArrowRight','Home','End'].includes(e.key)){e.preventDefault();const next=e.key==='Home'?'baseline':e.key==='End'?'candidate':routeTab.dataset.routingTab==='baseline'?'candidate':'baseline';setRoutingSide(next);document.querySelector(`#routingSideTabs [data-routing-tab="${next}"]`).focus();return}
 if(e.key!=='Tab'||modalStack.length||!$('authGate').hidden||!$('config').classList.contains('mobile-open'))return;
 const nodes=[...$('config').querySelectorAll('button:not([disabled]),input:not([disabled]),select:not([disabled]),summary,a[href],[tabindex="0"]')].filter(x=>x.getClientRects().length&&x.tabIndex>=0),first=nodes[0],last=nodes[nodes.length-1];
 if(e.shiftKey&&document.activeElement===first){e.preventDefault();last?.focus()}else if(!e.shiftKey&&document.activeElement===last){e.preventDefault();first?.focus()}
});
// 预检卡片跟随创建抽屉里的草稿：任何输入/选择变化后延迟重算，用户改了目标地址或题量立刻看到风险变化。
document.addEventListener('input',e=>{if($('config')?.contains(e.target)&&!e.target.closest('.quality-suite-picker'))schedulePreflight()});
document.addEventListener('change',e=>{if($('config')?.contains(e.target))schedulePreflight()});
function errorItems(d){const c=d.counters||{};return[['r429','限流 429'],['r5xx','服务端 5xx'],['r4xx','客户端 4xx'],['timeout','请求超时'],['network','网络错误'],['protocol','响应不完整']].map(([k,label])=>`<div class=\"error-item\"><b>${num(c[k],0)}</b><span>${label}</span></div>`).join('')}
function gatewayCacheMetric(d){const c=d.cache||{};return UI.metric({label:'缓存命中率（请求）',value:num(c.rate_pct),unit:c.rate_pct==null?'':'%',note:`命中 ${num(c.hits,0)} / 总请求 ${num(c.request_total??d.done,0)} 条；缺少缓存字段 ${num(c.no_usage,0)} 条按未命中计入。`})}
function gatewayErrorText(row){const detail=row?.error_detail;const detailText=detail&&typeof detail==='object'?Object.values(detail).filter(value=>value!=null&&value!=='').join(' · '):detail;return [row?.error_type,detailText||row?.reason].filter(Boolean).join(' · ')||'—'}
function capacityNote(d){const target=Number(d.target_rpm)||0,estimate=Number(d.rpm?.capacity_estimate)||0;if(!target||!estimate)return '';return UI.notice(`按本次已完成请求的平均耗时与并发上限估算，理论处理速率约 ${num(estimate)} 次/分钟；计划调度 ${num(target)} 次/分钟。估算不含未完成请求与调度开销，不作为可达吞吐或容量结论。`,{tone:estimate<target?'warning':'neutral'})}
function taskItem(t){return UI.recordItem({id:t.id,title:t.name,state:t.status,stateLabel:statusNames[t.status]||t.status,typeLabel:'单次压测',meta:[t.mode||'',`${num(t.concurrency)} 并发`,`${num(t.target_rpm)} 次/分钟`],progress:(Number(t.progress)||0)*100,selected:t.id===selected,action:'selectTask'})}
function comparisonItem(c){const groupList=c.groups||[],groupTotal=groupList.length,groupDone=groupList.filter(g=>['finished','finished_with_errors'].includes(g.status)).length,groupProgress=groupList.reduce((sum,g)=>sum+Math.max(0,Math.min(1,Number(g.progress)||0)),0);const pct=c.status==='finished'||c.status==='finished_with_errors'?100:groupTotal?Math.round(groupProgress/groupTotal*100):c.status==='running'?1:0;const report=c.report||{},conclusion=report.conclusion||{};const done=(c.groups||[]).filter(g=>['finished','finished_with_errors'].includes(g.status)).length;const total=(c.groups||[]).length;const label=c.status==='finished'?'已完成':c.status==='finished_with_errors'?'有错误':c.status==='running'?'执行中':'准备中';return `<button class="task comparison-record ${c.id===selectedComparison?'sel':''}" data-click-action="selectComparison" data-arg0="${c.id}"><div class="task-top"><div class="task-name">${esc(c.name)}</div><span class="status ${c.status}">${label}</span></div><div class="task-meta"><span class="record-type comparison-type">对照实验</span> · ${done}/${total||'—'} 组完成 · ${esc(report.evidence?.grade||'等待证据')}证据</div><div class="task-foot"><div class="mini-track"><i style="width:${pct}%"></i></div><span class="task-pct">${conclusion.level?reportTag(conclusion.level):`${done}/${total||'—'}`}</span></div></button>`}
function qualityItem(q){const {pct}=qualityProgress(q);const compared=(q.resources||[]).length>1;return `<button class="task ${q.id===selectedQuality?'sel':''}" data-click-action="selectQuality" data-arg0="${q.id}"><div class="task-top"><div class="task-name">${esc(q.name)}</div><span class="status ${q.status}">${qualityStatusLabel(q.status)}</span></div><div class="task-meta"><span class="record-type ${compared?'comparison-type':''}">${compared?'资源对照':'单资源测试'}</span> · ${q.case_count||0} 道题 · ${q.resources?.length||0} 个资源</div><div class="task-foot"><div class="mini-track"><i style="width:${pct}%"></i></div><span class="task-pct">${q.status==='finished'?'完成':q.status==='finished_with_errors'?'有错误':pct+'%'}</span></div></button>`}
function listEmpty(title,hint,action){const label=activeModule==='quality'?(activeView==='compare'?'新建资源对照':'新建单资源测试'):(activeView==='compare'?'新建压测对照':'新建压测');return `<div class="task-empty"><b>${title}</b><span>${hint}</span></div>`}
function updateTaskList(){const list=$('gatewayList'),records=filteredGatewayRecords();list.innerHTML=records.length?records.map(x=>x.record_type==='compare'?comparisonItem(x):taskItem(x)).join(''):listEmpty(activeView==='compare'?'暂无对照实验记录':'暂无单次压测记录',activeView==='compare'?'对照实验是一条记录，进入后用横向标签切换基准组、对比组和结论报告。':'创建单次网关压测后，结果会保留在这里。',activeView==='compare'?"openGatewayCreate('compare')":"openGatewayCreate('single')")}
function updateComparisonList(){updateTaskList()}
function updateQualityList(){const list=$('qualityList'),filtered=filteredQualityRecords();list.innerHTML=filtered.length?filtered.map(qualityItem).join(''):listEmpty(activeView==='compare'?'暂无资源对照记录':'暂无单资源测试记录',activeView==='compare'?'创建包含资源 A 与资源 B 的质量测试；它仍然是一条实验记录。':'创建单资源质量测试后，结果会保留在这里。',activeView==='compare'?"openQualityCreate('compare')":"openQualityCreate('single')")}
function renderWorkspaceEmpty(title,hint,action){$('loadingState').hidden=true;$('detail').hidden=true;$('modulePlaceholder').hidden=true;$('empty').hidden=false;$('emptyTitle').textContent=title;$('emptyHint').textContent=hint;const button=$('empty').querySelector('.empty-action');if(button){const match=/^(\w+)\((?:'([^']*)')?\)$/.exec(action);if(match){button.setAttribute('data-click-action',match[1]);button.dataset.arg0=match[2]||''}}}
function showEmptyForWorkspace(){const quality=activeModule==='quality',compare=activeView==='compare';renderWorkspaceEmpty(quality?(compare?'当前暂无资源对照记录':'当前暂无单资源测试记录'):(compare?'当前暂无对照实验记录':'当前暂无单次压测记录'),quality?(compare?'创建资源 A 与资源 B 的对照评测后，结果会出现在评测记录中。':'创建单资源评测后，结果会出现在评测记录中。'):(compare?'创建一个对照实验后，结果会出现在压测记录中。':'创建一次网关压测后，结果会出现在压测记录中。'),quality?(compare?"openQualityCreate('compare')":"openQualityCreate('single')"):(compare?"openGatewayCreate('compare')":"openGatewayCreate('single')"))}
function formatAccountDate(value){return UI.formatTimestamp(value)}
async function openAccountModal(){if(currentUser?.role!=='admin')return;const modal=$('accountModal');openModal('accountModal');$('accountStatus').textContent='';try{const d=await api('/admin/accounts');$('accountList').innerHTML=(d.accounts||[]).map(a=>`<div class="account-row"><b>${esc(a.username)}</b><span class="role-badge ${a.role==='admin'?'admin':''}">${a.role==='admin'?'管理员':'使用者'}</span><span class="created-at">${formatAccountDate(a.created_at)}</span><button class="account-delete" type="button" data-click-action="deleteAccount" data-arg0="${esc(a.username)}" ${a.username===currentUser.username?'disabled':''}>删除</button></div>`).join('')||'<div class="field-hint">暂无账号。</div>'}catch(e){$('accountList').innerHTML=`<div class="auth-error">${esc(e.message)}</div>`}}
function closeAccountModal(){closeModal('accountModal')}
async function deleteAccount(username){if(!confirm(`确认删除账号“${username}”？该账号将立即无法登录。`))return;try{await api('/admin/accounts?delete='+encodeURIComponent(username),{method:'POST'});toast('账号已删除');openAccountModal()}catch(e){toast(e.message)}}
$('accountCreateForm').addEventListener('submit',async e=>{e.preventDefault();const status=$('accountStatus'),button=e.target.querySelector('button[type="submit"]');button.disabled=true;status.className='form-status';status.textContent='正在创建账号…';try{const d=await api('/admin/accounts',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({username:$('newUsername').value.trim(),password:$('newPassword').value,role:$('newRole').value})});status.className='form-status ok';status.textContent=`已创建账号 ${d.account?.username||''}，请安全分发初始密码。`;$('newUsername').value='';$('newPassword').value='';openAccountModal()}catch(err){status.className='form-status error';status.textContent=err.message}finally{button.disabled=false}});
function showLoading(){$('loadingState').hidden=false;$('empty').hidden=true;$('detail').hidden=true;$('modulePlaceholder').hidden=true}
function showEmpty(){showEmptyForWorkspace()}
async function refresh(){if(!authenticated||refreshing)return;refreshing=true;const seq=++refreshSeq;try{const kind=selectedQuality?'quality_tests':selectedComparison?'comparisons':selectedRouting?'routing_comparisons':'tasks';const id=selectedQuality||selectedComparison||selectedRouting||selected||'';const d=await api('/tasks?summary=1&kind='+kind+'&selected='+encodeURIComponent(id));lastPollAt=Date.now();if(seq!==refreshSeq)return;const currentId=selectedQuality||selectedComparison||selectedRouting||selected||'';if(id!==currentId){refreshing=false;return refresh()}tasks=d.tasks||[];comparisons=d.comparisons||[];qualityTests=d.quality_tests||[];routingComparisons=d.routing_comparisons||[];recordsLoaded=true;window.FingerprintUI?.setRuns(d.fingerprint_runs||[],d.fingerprint_storage_warnings||[]);hasActiveRuns=[...tasks,...comparisons,...qualityTests,...routingComparisons,...(d.fingerprint_runs||[])].some(x=>['running','waiting','stopping','collecting','queued'].includes(x.status));$('health').classList.remove('conn-bad');$('healthText').textContent='服务正常';const signature=JSON.stringify([d,activeModule,activeView,selected,selectedQuality,selectedComparison,selectedRouting]);if(signature===lastRefreshSignature)return;lastRefreshSignature=signature;try{renderWorkspace();const picked=selectedQuality||selectedComparison||selectedRouting||selected||'';if(picked!==id)setTimeout(refresh,0)}catch(renderError){console.error('[render] failed',renderError);$('loadingState').hidden=true;$('detail').hidden=true;$('modulePlaceholder').hidden=true;$('empty').hidden=false;$('emptyTitle').textContent='详情显示异常';$('emptyHint').textContent='服务数据已读取，但页面渲染失败：'+renderError.message}}catch(e){if(seq!==refreshSeq)return;if(/请先登录|401|登录/.test(e.message||'')){authenticated=false;currentUser=null;updateAccountChrome();authGate(true,false);return}console.error('[refresh] failed',e);if(seq===refreshSeq){$('health').classList.add('conn-bad');$('healthText').textContent='连接失败';$('loadingState').hidden=true;$('detail').hidden=true;$('modulePlaceholder').hidden=true;$('empty').hidden=false;$('emptyTitle').textContent='测试记录加载失败';$('emptyHint').textContent=e.message||'无法读取测试记录，请确认本地服务已启动后重试。'}}finally{if(seq===refreshSeq)refreshing=false}}
function selectTask(id){selected=id;detailTask=null;selectedComparison=null;selectedQuality=null;activeModule='gateway';localStorage.setItem('route_selected_task',id);detailState.id=null;toggleConfig(false);updateWorkspaceChrome();renderWorkspace();refresh()}
async function openGroupTask(id){try{detailTask=await api('/task/'+id);selected=null;selectedComparison=null;selectedQuality=null;activeModule='gateway';detailState.id=null;toggleConfig(false);updateWorkspaceChrome();renderWorkspace()}catch(e){toast(e.message||'无法读取实验组明细')}}
function gatewayOutcome(d){
 const done=Number(d.done)||0,ok=Number(d.counters?.ok)||0,failed=Math.max(0,done-ok),live=['running','waiting','stopping'].includes(d.status),remaining=Math.max(0,Number(d.total||0)-done);
 if(!done)return {title:live?'等待请求结果':'本次没有可用请求结果',detail:live?'首条请求返回后会更新成功率、吞吐与耗时。':'检查任务错误和连接配置后再复测。',tone:live?'neutral':'warning',failed};
 const summary=`已返回 ${num(done,0)} 条，成功 ${num(ok,0)} 条${failed?`，失败 ${num(failed,0)} 条`:''}。`;
 return {title:live?'本轮实时观测':failed?'本次存在失败请求':remaining?'本次未完整执行':'本次请求均完整成功',detail:summary+(live?'当前是过程结果，完成后再判断。':remaining?`另有 ${num(remaining,0)} 条未完成；结果仅覆盖已返回请求。`:failed?'先核对失败分布与异常请求，再决定是否复测。':'结果仅覆盖本次窗口，不代表持续稳定或容量上限。'),tone:failed||remaining?'warning':live?'neutral':'success',failed};
}
function gatewayCacheDetails(d){
 const c=d.cache||{},total=c.request_total??d.done??0,known=Math.max(0,Number(total)-Number(c.no_usage||0));
 return UI.disclosure({id:'gateway-cache-'+d.id,title:'缓存统计口径与用量证据',bodyHtml:UI.methodCards([
  {title:'按请求统计',lines:[`命中 ${num(c.hits,0)} / 已返回 ${num(total,0)} 条。`,...(known>0&&Number(c.hits)===0?[`上游已返回缓存字段的 ${num(known,0)} 条请求均未报告缓存命中。`]:[]),`其中 ${num(c.no_usage,0)} 条缺少缓存字段；按未命中计入总请求分母，不能据此证明上游没有缓存。`,'cached_tokens（缓存词元）大于 0 计为命中。']},
  {title:'按词元辅助统计',lines:[c.token_eligible?`缓存 ${num(c.cached_tokens,0)} / 输入 ${num(c.prompt_tokens,0)} 词元，比例 ${num(c.token_rate_pct)}%。`:'缺少可同时统计的输入词元和缓存词元，无法计算。',d.config?.profile==='cache'?'请求采用固定长前缀，轮换末尾问题。':'不同请求内容的缓存复用条件不同；验证稳定前缀时请选择缓存压测。']}
 ])});
}
function gatewayExecutionHtml(d){
 const live=['running','waiting','stopping'].includes(d.status),uncompleted=Math.max(Number(d.dropped)||0,Number(d.total||0)-Number(d.done||0));
 return UI.progressCard({kicker:live?'执行进度':'执行记录',value:(Number(d.progress)||0)*100,summary:`已返回 ${num(d.done,0)} / ${num(d.total,0)} 条`,note:`已调度 ${num(d.scheduled,0)} · 已开始 ${num(d.started,0)} · 在途 ${num(d.inflight,0)} · ${live?`排队 ${num(d.pending,0)}`:`未完成 ${num(uncompleted,0)}`}`})+(live?'':capacityNote(d));
}
const gatewayRequestView={id:null,filter:'all'};
function setGatewayRequestFilter(id,filter){
 const d=window.currentDetail;if(!d||d.id!==id)return;
 gatewayRequestView.id=id;gatewayRequestView.filter=filter==='failed'?'failed':'all';render(d);
 requestAnimationFrame(()=>$('gatewayRequests')?.scrollIntoView({block:'start'}));
}
function gatewayArtifactAvailable(d){return d.status!=='expired'&&!(['expired','missing','deleted'].includes(d.artifact?.state||d.artifact_state))}
function gatewayRequestsHtml(d){
 if(gatewayRequestView.id!==d.id){gatewayRequestView.id=d.id;gatewayRequestView.filter='all'}
 const recent=d.recent||[],failures=recent.filter(r=>r.cls!=='ok'),filtered=gatewayRequestView.filter==='failed',rows=filtered?failures:recent,available=gatewayArtifactAvailable(d),capture=d.config?.capture_raw&&available;
 const tabs=UI.tabs({label:'最近请求筛选',items:[['all',`全部 ${num(recent.length,0)}`],['failed',`仅异常 ${num(failures.length,0)}`]],active:gatewayRequestView.filter,action:'setGatewayRequestFilter',ownerId:d.id});
 return UI.section({title:'最近请求',description:`展示最近 ${num(recent.length,0)} 条结果；筛选只作用于这批记录。${!available?'结果文件已不可用，当前仅保留历史摘要。':d.out_path?'完整结果可在页底下载。':'当前未提供可下载的完整文件。'}${capture?'已保存原始输入与输出。':''}`,bodyHtml:tabs+UI.table({columns:['请求','结果与状态码','耗时与时间','原因',...(capture?['原始记录']:[])].map(label=>({label})),rowsHtml:rows.map(r=>[
  `${num(r.id,0)}<small class="role-label">档位 ${num(r.step,0)}</small><small class="role-label">${esc(r.request_id||'请求标识未记录')}</small>`,
  UI.tag(r.label||'未记录',r.cls==='ok'?'success':'danger')+`<small class="role-label">状态码 ${esc(r.status??'未记录')}</small>`,
  requestLatencyHtml(r,'request:'+d.id+':'+r.id,{total:r.total,first:r.ttft}),esc(gatewayErrorText(r)),
  ...(capture?[UI.textButton('查看原始记录',{action:'showRaw',args:[d.id,Number(r.id)]})]:[])
 ]),empty:filtered?'最近这批结果中没有异常；不代表全部历史请求均成功。':['running','waiting','stopping'].includes(d.status)?'等待首条请求结果。':'本次没有已返回的请求。'})}).replace('<section ','<section id="gatewayRequests" ');
}
function render(d){
 if(d._summary){showLoading();return refresh()}
 window.detailReceivedAt=Date.now();window.currentDetail=d;$('loadingState').hidden=true;$('empty').hidden=true;$('modulePlaceholder').hidden=true;$('detail').hidden=false;
 const detail=$('detail'),same=detailState.id===d.id,scroll=same?qualityScrollSnapshot(detail):null,live=['running','waiting','stopping'].includes(d.status),ended=['finished','finished_with_errors','stopped','error','expired'].includes(d.status),outcome=gatewayOutcome(d);
 const actions=[d.out_path&&gatewayArtifactAvailable(d)?UI.button('下载全部请求记录',{action:'download',args:['/download/'+d.id],iconName:'download',title:'JSONL（逐行结构化数据）文件'}):'',ended?UI.button('删除本地记录',{variant:'danger',action:'deleteTask',args:[d.id]}):''].join('');
 const statusTone={running:'accent',waiting:'accent',stopping:'warning',finished:'success',finished_with_errors:'warning',stopped:'warning',error:'danger'}[d.status]||'neutral';
 const cache=d.cache||{},requestType={smoke:'常规短问答',short:'最短响应',long:'长输入',cache:'缓存压测'}[d.config?.profile];
 const metrics=UI.metrics([
  {label:'请求成功率',value:num(d.success_pct),unit:d.success_pct==null?'':'%',note:`成功 ${num(d.counters?.ok,0)} / 已返回 ${num(d.done,0)} 条`,primary:true},
  {label:d.status==='running'?'实时完成吞吐':'平均完成吞吐',value:num(d.status==='running'?d.rpm?.live:d.rpm?.avg),unit:'次/分钟',note:`计划调度 ${num(d.target_rpm)} 次/分钟`},
  {label:'首字耗时 · 第95百分位',value:num(d.ttft?.p95),unit:'毫秒',note:'仅统计可观测到正文首字的响应'},
  {label:'缓存命中率（请求）',value:num(cache.rate_pct),unit:cache.rate_pct==null?'':'%',note:`命中 ${num(cache.hits,0)} / 已返回 ${num(cache.request_total??d.done,0)} 条`}
 ]);
 const failureBody=UI.metrics([['r429','限流 429'],['r5xx','服务端 5xx（服务错误）'],['r4xx','客户端 4xx（请求错误）'],['timeout','请求超时'],['network','网络错误'],['protocol','响应不完整']].filter(([key])=>Number(d.counters?.[key])>0).map(([key,label])=>({label,value:num(d.counters[key]),unit:'条'})));
 detail.innerHTML=[
  UI.pageHeader({kicker:`网关压测 · 单次压测 · ${d.mode||'—'}`,title:d.name,description:'观察本次窗口的成功率、完成吞吐与延迟。',meta:[d.model||'—',d.target_url||'—',...(requestType?[requestType]:[]),`实际运行 ${duration(d.elapsed)}`,...(d.planned_duration?[`计划调度 ${durationPrecise(d.planned_duration)}`]:[])],statusLabel:statusNames[d.status]||d.status,statusNote:live?(d.phase||'等待执行'):`已返回 ${num(d.done,0)} / ${num(d.total,0)} 条请求`,statusTone,actionsHtml:runHeaderAction('task',d,'stopTask','停止后续请求')}),
  artifactNotice(d),d.error?UI.notice(d.error,{tone:'danger',title:'任务错误'}):'',live?gatewayExecutionHtml(d):'',
  UI.section({title:outcome.title,description:outcome.detail,className:'overview-shell',badgeHtml:outcome.failed?UI.button('查看异常请求',{action:'setGatewayRequestFilter',args:[d.id,'failed']}):'',bodyHtml:metrics+gatewayCacheDetails(d)}),
  outcome.failed?UI.section({title:'失败分布',description:'完整的 2xx（成功状态码）响应才计为成功；以下为客户端观测分类。',bodyHtml:failureBody}):'',
  UI.section({title:'完成吞吐趋势',description:'纵轴为次/分钟；完成包含成功与失败，失败另列。仅显示最近采样窗口，不代表网关容量。',badgeHtml:'<div class="chart-legend"><span><i class="lg-done"></i>完成</span><span><i class="lg-plan"></i>计划</span><span><i class="lg-fail"></i>失败</span></div>',bodyHtml:`<div class="chart-wrap"><canvas id="chart"></canvas>${d.timeline?.length?'':`<div class="chart-empty">${live?'等待请求返回后绘制趋势':'本次没有保存趋势采样'}</div>`}</div>`}),
  gatewayRequestsHtml(d),
  UI.disclosure({title:'延迟明细与调度记录',id:'gateway-execution-'+d.id,section:true,bodyHtml:UI.metrics([{label:'首字耗时 · 中位数',value:num(d.ttft?.p50),unit:'毫秒'},{label:'完整响应 · 中位数',value:num(d.total_ms?.p50),unit:'毫秒'},{label:'完整响应 · 第95百分位',value:num(d.total_ms?.p95),unit:'毫秒'},{label:'调度延迟 · 第95百分位',value:num(d.schedule_lag?.p95),unit:'毫秒'}])+(live?'':gatewayExecutionHtml(d))}),
  UI.timeRecords(d,{key:'task:'+d.id}),actions?UI.actionBar(actions):''
 ].join('');
 detailState.id=d.id;requestAnimationFrame(()=>{restoreQualityScroll(detail,scroll);drawChart(d.timeline||[]);const wrap=detail.querySelector('.chart-wrap');if(wrap&&window.chartResizeObserver){window.chartResizeObserver.disconnect();window.chartResizeObserver.observe(wrap)}});
}
async function showRaw(taskId,itemId){const modal=$('rawModal');openModal('rawModal');$('rawTitle').textContent=`请求 #${itemId} 原始输入与输出`;$('rawInput').textContent='加载中…';$('rawOutput').textContent='加载中…';try{const d=await api(`/task/${taskId}/record/${itemId}`);$('rawInput').textContent=JSON.stringify(d.raw_input??null,null,2);$('rawOutput').textContent=d.raw_output||'（没有响应正文，例如连接失败或超时）'}catch(e){$('rawInput').textContent='读取失败';$('rawOutput').textContent=e.message}}
function closeRaw(){closeModal('rawModal')}
document.addEventListener('keydown',e=>{
 const tab=e.target.closest?.('.report-tab,.tab[data-mode]');if(!tab||!['ArrowLeft','ArrowRight','Home','End'].includes(e.key))return;
 const owner=tab.closest('#detail,#modulePlaceholder,#config')||document,nav=tab.parentElement,label=nav.getAttribute('aria-label'),selector=tab.matches('.report-tab')?'.report-tab':'.tab[data-mode]',tabs=[...nav.querySelectorAll(selector)],index=tabs.indexOf(tab),next=e.key==='Home'?0:e.key==='End'?tabs.length-1:(index+(e.key==='ArrowRight'?1:-1)+tabs.length)%tabs.length;
 e.preventDefault();tabs[next].focus({preventScroll:true});tabs[next].click();queueMicrotask(()=>requestAnimationFrame(()=>{const current=[...owner.querySelectorAll('[role="tablist"]')].find(node=>node.getAttribute('aria-label')===label);current?.querySelectorAll(selector)[next]?.focus({preventScroll:true})}));
});
function drawChart(data){
 const c=$('chart');if(!c)return;const wrap=c.parentElement;if(!wrap)return;
 const rect=wrap.getBoundingClientRect(),dpr=window.devicePixelRatio||1,w=Math.max(80,Math.floor(rect.width)),h=Math.max(150,Math.floor(rect.height));
 c.width=Math.floor(w*dpr);c.height=Math.floor(h*dpr);c.style.width='100%';c.style.height='100%';
 const x=c.getContext('2d');x.setTransform(dpr,0,0,dpr,0,0);x.clearRect(0,0,w,h);if(!data.length)return;
 const css=getComputedStyle(document.documentElement),token=name=>css.getPropertyValue(name).trim(),colors={grid:token('--color-chart-grid'),label:token('--color-muted'),done:token('--color-accent'),plan:token('--color-chart-plan'),fail:token('--color-danger')};
 const points=data.map(v=>({...v,rpm:Number(v.rpm)||0,target_rpm:Number(v.target_rpm)||0,failed:60*((Number(v.r429)||0)+(Number(v.r5xx)||0)+(Number(v.r4xx)||0)+(Number(v.timeout)||0)+(Number(v.network)||0)+(Number(v.protocol)||0))}));
 const max=Math.max(60,...points.flatMap(v=>[v.rpm,v.target_rpm,v.failed]));
 const timed=points.every((v,i)=>typeof v.t==='number'&&Number.isFinite(v.t)&&(i===0||v.t>=points[i-1].t)),span=timed?points.at(-1).t-points[0].t:0;
 x.strokeStyle=colors.grid;x.fillStyle=colors.label;x.lineWidth=1;x.font=token('--text-small')+' '+token('--font-mono');
 for(let i=0;i<=4;i++){const y=12+(h-40)*i/4;x.beginPath();x.moveTo(45,y);x.lineTo(w-14,y);x.stroke();x.fillText(Math.round(max*(1-i/4)),4,y+3)}
 const px=i=>45+(w-59)*(points.length===1?.5:span>0?(points[i].t-points[0].t)/span:i/(points.length-1)),py=v=>h-28-(h-40)*(v/max);
 function line(key,color,width=2,dash=[]){x.strokeStyle=color;x.lineWidth=width;x.setLineDash(dash);x.beginPath();points.forEach((v,i)=>i?x.lineTo(px(i),py(v[key])):x.moveTo(px(i),py(v[key])));x.stroke();x.setLineDash([]);if(points.length===1){x.fillStyle=color;x.beginPath();x.arc(px(0),py(points[0][key]),Math.max(3,width+1),0,Math.PI*2);x.fill()}}
 line('target_rpm',colors.plan,1.5,[5,5]);line('rpm',colors.done,2.4);line('failed',colors.fail,1.5);
 x.fillStyle=colors.label;x.fillText(timed?'采样窗口起点':'第 1 个采样',45,h-8);x.textAlign='right';x.fillText(span>0?`${num(span/1000,1)} 秒`:`第 ${points.length} 个采样`,w-14,h-8);x.textAlign='left';
}
async function stopTask(id){if(!confirm('停止后将取消尚未开始的排队请求；已经开始的请求会继续完成并写入结果。是否继续？'))return;try{await api('/task/'+id+'?stop',{method:'POST'});toast('正在停止任务');refresh()}catch(e){toast(e.message)}}async function deleteTask(id){if(!confirm('确认删除任务？任务记录和它的结果文件会一起删除，删除后无法恢复，也无法再下载这次的结果。'))return;try{await api('/task/'+id+'?delete',{method:'POST'});if(selected===id){selected=null;localStorage.removeItem('route_selected_task')}refresh()}catch(e){toast(e.message)}}async function deleteComparison(id){if(!confirm('确认删除这条对照实验记录？实验记录和它各实验组的结果文件会一起删除，删除后无法恢复。'))return;try{await api('/comparisons?delete='+encodeURIComponent(id),{method:'POST'});selectedComparison=null;toast('对照实验记录已删除');refresh()}catch(e){toast(e.message)}}async function deleteQuality(id){if(!confirm('确认删除这条质量评测记录？评测记录和它的证据文件会一起删除，删除后无法恢复，报告也将无法复现。'))return;try{await api('/quality-tests?delete='+encodeURIComponent(id),{method:'POST'});selectedQuality=null;toast('质量评测记录已删除');refresh()}catch(e){toast(e.message)}}function retryTask(id){return window.RepeatUI?.start('task',id)}
// Keep the user's current module and record when a repeat finishes in the background.
function repeatContext(){return JSON.stringify([activeModule,activeView,selected,selectedComparison,selectedQuality,selectedRouting,window.FingerprintUI?.getSelectedId?.()||null])}
async function repeatCreated(kind,sourceId,id,context){
 const stillViewing=repeatContext()===context;
 if(stillViewing){
  detailTask=null;detailState.id=null;lastRefreshSignature='';
  if(kind==='fingerprint'){await window.FingerprintUI?.select(id)}
  else{const quality=qualityTests.find(item=>item.id===sourceId),createKind=kind==='task'?'gateway-single':kind==='comparison'?'gateway-compare':kind==='routing'?'routing-compare':(Number(quality?.total_resources||quality?.config?.resources?.length||quality?.resources?.length||1)>1?'quality-compare':'quality-single');selectDefaultModuleAfterCreate(createKind,id);selectedRouting=kind==='routing'?id:null;if(kind==='task')localStorage.setItem('route_selected_task',id)}
 }
 await refresh();
}
Object.assign(window,{renderQualityDetail,renderComparisonDetail,render,selectQuality,selectComparison,refresh,deleteComparison,deleteQuality,openAccountModal,closeAccountModal,logout});
if(window.ResizeObserver){window.chartResizeObserver=new ResizeObserver(()=>{const d=tasks.find(x=>x.id===selected);if(d)drawChart(d.timeline||[])})}document.querySelector('.app')?.scrollTo(0,0);window.scrollTo(0,0);showLoading();async function bootApp(){stopRefresh();if(!authenticated)return;try{await loadConfig();await refreshSavedConnectionPickers()}catch(e){console.error('[init] config failed',e)}if(!authenticated)return;await refresh();if(authenticated&&$('authGate').hidden)refreshTimer=setInterval(()=>{const active=hasActiveRuns;if(!document.hidden&&Date.now()-lastPollAt>=(active?1400:30000))refresh()},1500)};window.FingerprintUI?.init();window.ConnectionUI?.init();window.TpmUI?.init();window.RepeatUI?.init();if($('connectionsNavIcon'))$('connectionsNavIcon').innerHTML=UI.icon('connections');(async function boot(){const ok=await initAuth();if(ok)await bootApp()})();setInterval(()=>{const d=window.currentDetail;if(d&&(['running','waiting'].includes(d.status))){const el=d.elapsed+(Date.now()-window.detailReceivedAt)/1000;document.querySelectorAll('[data-elapsed]').forEach(n=>n.textContent=duration(el))}document.querySelectorAll('[data-live-elapsed]').forEach(n=>{const task=tasks.find(t=>t.id===selected);if(task&&(['running','waiting'].includes(task.status)))n.textContent=duration(Number(n.dataset.base||0)+(Date.now()-Number(n.dataset.stamp||Date.now()))/1000)})},250);window.addEventListener('resize',()=>{const d=tasks.find(x=>x.id===selected);if(d)drawChart(d.timeline||[])})

document.addEventListener('visibilitychange',()=>{if(!document.hidden)refresh()});

const themeIcons={dark:'<path d="M20 14.5A8 8 0 1 1 9.5 4a6.5 6.5 0 0 0 10.5 10.5Z"/>',light:'<circle cx="12" cy="12" r="4"/><path d="M12 2.5v2M12 19.5v2M4.6 4.6l1.4 1.4M18 18l1.4 1.4M2.5 12h2M19.5 12h2M4.6 19.4 6 18M18 6l1.4-1.4"/>'};
function setTheme(theme){document.documentElement.dataset.theme=theme;const button=$('themeToggle');if(button){const next=theme==='dark'?'light':'dark',label=next==='dark'?'切换深色模式':'切换浅色模式';button.innerHTML=`<svg class="ui-icon" viewBox="0 0 24 24" aria-hidden="true">${themeIcons[next]}</svg><span>${next==='dark'?'深色':'浅色'}</span>`;button.setAttribute('aria-label',label);button.title=label;button.setAttribute('aria-pressed',String(theme==='dark'));}if(window.currentDetail)drawChart(window.currentDetail.timeline||[]);window.TpmUI?.drawChart();}
setTheme(localStorage.getItem('workbench_theme')||(matchMedia('(prefers-color-scheme: dark)').matches?'dark':'light'));
$('themeToggle')?.addEventListener('click',()=>{const theme=document.documentElement.dataset.theme==='dark'?'light':'dark';localStorage.setItem('workbench_theme',theme);setTheme(theme)});

// Keep actual table headers for accessibility and attach labels for narrow-screen cards.
function labelComparisonCells(){for(const table of document.querySelectorAll('.comparison-result-table')){const labels=[...table.querySelectorAll('thead th')].map(th=>th.textContent.trim());for(const row of table.querySelectorAll('tbody tr'))[...row.cells].forEach((cell,index)=>{if(!cell.hasAttribute('data-label'))cell.dataset.label=labels[index]||''});}}
new MutationObserver(labelComparisonCells).observe(document.querySelector('.main'),{childList:true,subtree:true});
