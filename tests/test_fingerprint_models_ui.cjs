/* Model discovery state regression with a minimal DOM and deferred synthetic API.
 * No browser process, HTTP service, real credentials, or upstream is used.
 */
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const asset = process.env.UI_FINGERPRINT_JS || path.resolve(__dirname,'../static/fingerprint.js');
const original = fs.readFileSync(asset,'utf8');
const source = original.replace('window.FingerprintUI = Object.freeze({init,setRuns,updateChrome,render,openCreate,closeCreate,hide,onAuthLost,select,setTab});','window.__modelTest = {s,referenceLibrary,welcome,defaultCreateMode,updateCreateHeading,selectedModel,paintModelControls,invalidateModelConnection,setModelInputMode,discoverModels,resetModelDiscovery,updateAdapterHint,isDeepseekOfficial,onInput,onChange,closeCreate,onAuthLost};');
const nodes = new Map();
let focused = null; let formMode = 'selfcheck';
function element(id) {
  if (!nodes.has(id)) nodes.set(id,{id,value:'',hidden:false,disabled:false,required:false,checked:false,innerHTML:'',textContent:'',placeholder:'',dataset:{},attributes:{},classList:{add(){},remove(){},toggle(){}},setAttribute(name,value){this.attributes[name]=String(value)},focus(){focused=this.id},contains(){return true},querySelectorAll(){return []},scrollIntoView(){}});
  return nodes.get(id);
}
const modeButtons=['list','manual'].map(mode=>{const el=element('mode-'+mode);el.dataset.fpModelMode=mode;return el});
const requests=[];
const pending=[];
const sandbox={window:{},document:{getElementById:element,querySelectorAll:selector=>selector.includes('model-input-mode')?modeButtons:[],querySelector:selector=>selector==='.app'?element('app'):selector==='input[name="fpMode"]:checked'?{value:formMode}:null,body:{classList:{toggle(){}}}},URL,console,TextEncoder,authenticated:true,api:(url,options)=>{requests.push({url,options:JSON.parse(JSON.stringify(options)),body:JSON.parse(options.body)});return new Promise((resolve,reject)=>pending.push({resolve,reject}))}};
const uiKit = path.resolve(__dirname,'../static/ui-kit.js');
vm.runInNewContext(fs.readFileSync(uiKit,'utf8'),sandbox,{filename:uiKit});
vm.runInNewContext(source,sandbox,{filename:asset});
const t=sandbox.window.__modelTest,s=t.s;
let checks=0;
function check(condition,message){assert.ok(condition,message);checks++;}
function equal(actual,expected,message){assert.equal(actual,expected,message);checks++;}
function ready() {
  t.resetModelDiscovery();s.drawerOpen=true;s.drawerSession++;s.epoch++;sandbox.authenticated=true;
  element('fpUrl').value='https://synthetic.invalid/v1';element('fpKey').value='synthetic-credential';
  element('fpModel').value='';element('fpModelSelect').value='';element('fpRiskAck').checked=true;
  t.paintModelControls();
}
(async()=>{
  s.view='references';equal(t.defaultCreateMode(),'reference','reference library chooses reference creation by default');
  s.view='runs';equal(t.defaultCreateMode(),'selfcheck','run view chooses self-check creation by default');
  check((t.referenceLibrary().match(/data-fp-action="create-reference"/g)||[]).length===2,'reference library has header and empty-state creation actions');
  check(t.welcome().includes('data-fp-action="create-reference"'),'welcome exposes reference collection action');
  t.updateCreateHeading('reference');equal(element('fpCreateTitle').textContent,'采集参考指纹','reference-specific drawer title');
  check(element('fpName').placeholder.includes('参考指纹'),'reference-specific name guidance');
  check(element('fpCreateSubtitle').textContent.includes('自动保存到参考库'),'reference completion destination is explicit');
  t.updateCreateHeading('verify');equal(element('fpCreateTitle').textContent,'新建参考对照','verification-specific drawer title');
  t.updateCreateHeading('selfcheck');equal(element('fpCreateTitle').textContent,'新建通道自检','self-check-specific drawer title');
  const formSource=original.slice(original.indexOf('function buildCreate('),original.indexOf('const createMode'));
  check(formSource.indexOf('id="fpUrl"')<formSource.indexOf('id="fpKey"')&&formSource.indexOf('id="fpKey"')<formSource.indexOf('id="fpModelSelect"'),'form follows URL, key, model order');
  check(formSource.includes('不会启动采集'),'model discovery does not imply collection has begun');
  check(formSource.includes('id="fpModel" name="model" maxlength="256"'),'manual model supports backend maximum length');

  element('fpAdapter').options=[{value:'standard'},{value:'deepseek'},{value:'openai'}];
  element('fpAdapter').value='standard'; element('fpUrl').value='https://api.deepseek.com/v1'; element('fpRiskAck').checked=true;
  s.adapterManual=false;s.adapterAuto=null;t.updateAdapterHint();
  equal(element('fpAdapter').value,'deepseek','known official endpoint selects nonthinking before freeze');
  equal(element('fpRiskAck').checked,false,'adapter change resets collection acknowledgment');
  check(element('fpAdapterHint').textContent.includes('关闭'),'visible hint explains the nonthinking protocol');
  element('fpUrl').value='https://proxy.invalid/v1';t.updateAdapterHint();
  equal(element('fpAdapter').value,'standard','moving away from official endpoint clears automatic adapter');
  element('fpUrl').value='https://api.deepseek.com.evil.invalid/v1';t.updateAdapterHint();
  equal(element('fpAdapter').value,'standard','lookalike hosts do not trigger provider assumptions');
  element('fpUrl').value='https://api.deepseek.com';s.adapterManual=true;t.updateAdapterHint();
  equal(element('fpAdapter').value,'standard','manual adapter is never silently overridden');
  check(element('fpAdapterHint').textContent.includes('请选择'),'incompatible explicit adapter gets clear guidance');
  s.adapterManual=false;formMode='verify';t.updateAdapterHint();
  equal(element('fpAdapter').value,'standard','reference protocol remains locked');
  check(element('fpAdapterHint').textContent.includes('重新建立参考'),'incompatible reference explains recovery');
  formMode='selfcheck';
  ready();equal(requests.length,0,'initial state never requests models automatically');
  equal(s.modelInputMode,'list','default is explicit list selection');equal(t.selectedModel(),'','no model is selected by default');
  check(element('fpModelSelect').disabled,'empty model list cannot be selected');
  element('fpUrl').value='';await t.discoverModels();equal(requests.length,0,'invalid connection sends no model request');
  ready();
  const request=t.discoverModels();equal(requests.length,1,'explicit discovery sends exactly one request');
  equal(requests[0].url,'/fingerprint/models','discovery uses the dedicated endpoint');
  equal(JSON.stringify(Object.keys(requests[0].body).sort()),JSON.stringify(['api_key','url']),'request body contains connection fields only');
  equal(element('fpRiskAck').checked,false,'discovery clears collection cost acknowledgment');
  check(element('fpFetchModels').disabled,'duplicate clicks disabled while pending');
  await t.discoverModels();equal(requests.length,1,'repeat pending discovery is ignored');
  pending.shift().resolve({models:['Provider/Model-A','provider/model-a','Model "quoted" <b>','Provider/Model-A']});await request;
  equal(s.modelOptions.length,3,'model IDs are deduplicated without case folding');
  equal(element('fpModelSelect').value,'','successful discovery never chooses the first model');
  check(element('fpModelSelect').innerHTML.includes('&quot;quoted&quot; &lt;b&gt;'),'model labels and option attributes are escaped');
  element('fpModelSelect').value='Provider/Model-A';equal(t.selectedModel(),'Provider/Model-A','single explicitly selected model ID is preserved');
  element('fpRiskAck').checked=true;t.onChange({target:element('fpModelSelect')});equal(element('fpRiskAck').checked,false,'model selection invalidates collection acknowledgment');
  element('fpKey').value='new-synthetic-credential';t.onInput({target:element('fpKey')});
  equal(s.modelOptions.length,0,'credential edit clears discovered models');equal(t.selectedModel(),'','credential edit clears selected model');
  check(element('fpModelStatus').textContent.includes('连接信息已改变'),'connection invalidation is visible');

  ready();const oldRequest=t.discoverModels();
  element('fpUrl').value='https://other-synthetic.invalid/v1';t.onInput({target:element('fpUrl')});
  const newRequest=t.discoverModels();
  const oldDeferred=pending.shift(),newDeferred=pending.shift();newDeferred.resolve({models:['NEW/MODEL']});await newRequest;
  element('fpModelSelect').value='NEW/MODEL';oldDeferred.resolve({models:['OLD/MODEL']});await oldRequest;
  equal(t.selectedModel(),'NEW/MODEL','late old-URL response cannot overwrite current selection');
  equal(s.modelOptions[0],'NEW/MODEL','late old-URL response cannot overwrite current list');

  ready();const oldKeyRequest=t.discoverModels();element('fpKey').value='changed-without-event';
  pending.shift().resolve({models:['WRONG-CREDENTIAL-MODEL']});await oldKeyRequest;
  equal(s.modelOptions.length,0,'live credential comparison rejects stale responses even without an input event');
  equal(s.modelDiscoveryPending,false,'silent credential changes do not leave the model loader stuck');
  ready();const failed=t.discoverModels();pending.shift().reject(new Error('模型目录不受支持'));await failed;
  check(element('fpModelStatus').textContent.includes('模型目录不受支持'),'discovery failure has a readable reason');
  t.setModelInputMode('manual');equal(focused,'fpModel','manual fallback focuses the manual input');
  check(!element('fpModel').disabled&&element('fpModelSelect').disabled,'only the active input method participates');
  element('fpModel').value='Exact/Manually-Entered:Model';equal(t.selectedModel(),'Exact/Manually-Entered:Model','manual fallback remains usable after failure');
  element('fpRiskAck').checked=true;t.onInput({target:element('fpModel')});equal(element('fpRiskAck').checked,false,'manual model change invalidates acknowledgment');
  element('fpUrl').value='https://another.invalid/v1';t.onChange({target:element('fpUrl')});equal(element('fpModel').value,'','change-only connection events also clear manual selection');

  ready();const failureWithFallback=t.discoverModels();pending.shift().reject(new Error('模型列表鉴权失败，请检查密钥；也可手动填写模型名称'));await failureWithFallback;
  equal((element('fpModelStatus').textContent.match(/手动填写模型名称/g)||[]).length,1,'backend manual-fallback guidance is not repeated');
  check(element('fpModelStatus').textContent.includes('模型列表鉴权失败，请检查密钥'),'backend error reason remains intact');

  ready();const switched=t.discoverModels();t.setModelInputMode('manual');element('fpModel').value='Manual-Choice';
  pending.shift().resolve({models:['LATE/LIST']});await switched;
  equal(t.selectedModel(),'Manual-Choice','late discovery never overwrites a manual choice');
  equal(s.modelInputMode,'manual','late discovery never switches input mode');
  ready();const empty=t.discoverModels();pending.shift().resolve({models:[]});await empty;
  check(element('fpModelStatus').textContent.includes('空模型列表'),'empty lists explain the manual path');
  equal(t.selectedModel(),'','empty model list creates no default');

  ready();const previousForm=t.discoverModels();s.drawerOpen=false;t.resetModelDiscovery();s.drawerSession++;s.drawerOpen=true;
  pending.shift().resolve({models:['CLOSED-FORM-MODEL']});await previousForm;
  equal(s.modelOptions.length,0,'closed-form response cannot populate the next form');
  ready();const lostAuth=t.discoverModels();s.epoch++;sandbox.authenticated=false;
  pending.shift().resolve({models:['LOGGED-OUT-MODEL']});await lostAuth;
  equal(s.modelOptions.length,0,'logged-out response is discarded');
  check(!original.includes('localStorage.setItem'),'fingerprint module never persists credentials');
  equal(requests.filter(r=>r.url!=='/fingerprint/models').length,0,'discovery sends no collection requests');
  equal(pending.length,0,'all synthetic requests were settled');
  console.log(JSON.stringify({status:'passed',checks,syntheticRequests:requests.length}));
})().catch(error=>{console.error(error);process.exitCode=1});
