'use strict';
/* Real gateway model handlers with controlled responses; no browser or upstream calls. */
const fs=require('node:fs'),path=require('node:path'),vm=require('node:vm'),assert=require('node:assert/strict');
const source=fs.readFileSync(path.resolve(__dirname,'../static/app.js'),'utf8');
const between=(start,end)=>source.slice(source.indexOf(start),source.indexOf(end,source.indexOf(start)));
const single=name=>source.match(new RegExp('^function '+name+'\\([^\\n]+','m'))[0];
let checks=0;
const check=(name,run)=>{try{run();checks++}catch(error){error.message=name+': '+error.message;throw error}};
function harness(){
 const nodes=new Map(),pending=[],requests=[],timers=[],calls=[];
 function node(id=''){
  const classes=new Set();
  return {id,value:'',innerHTML:'',textContent:'',hidden:false,disabled:false,inert:false,isConnected:true,dataset:{},attrs:{},
   classList:{add(...values){values.forEach(value=>classes.add(value))},remove(...values){values.forEach(value=>classes.delete(value))},contains:value=>classes.has(value),toggle(value,on){on?classes.add(value):classes.delete(value)}},
   setAttribute(key,value){this.attrs[key]=String(value)},removeAttribute(key){delete this.attrs[key]},
   querySelector(){return null},querySelectorAll(){return []},focus(){calls.push(['focus',id])}};
 }
 const element=id=>{if(!nodes.has(id))nodes.set(id,node(id));return nodes.get(id)};
 const row=node('model-row'),body=node('drawer-body'),app=node('app'),opener=node('opener');
 element('config').querySelector=selector=>selector==='.drawer-body'?body:null;
 element('loadModels').closest=()=>row;element('modelSelect').closest=()=>row;
 element('modelManualWrap').hidden=true;element('authGate').hidden=true;
 element('url').value='https://synthetic.invalid/v1';element('api_key').value='synthetic-only';
 const sandbox={$:element,console,window:{},document:{activeElement:opener,body:node('body'),querySelector:selector=>selector==='.app'?app:null},
  setTimeout(fn){timers.push(fn);return timers.length},clearTimeout(){},
  esc:value=>String(value??'').replace(/[&<>"]/g,ch=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[ch])),
  api(url,options){requests.push({url,body:JSON.parse(options.body)});return new Promise((resolve,reject)=>pending.push({resolve,reject}))},
  updateKeyInputHint(){calls.push(['credential-hint'])},checkSavedCredential(){},
  schedulePreflight(){calls.push(['preflight'])},isRoutingCreate:()=>false,isQualityCreate:()=>false,isCompareCreate:()=>false};
 vm.createContext(sandbox);
 vm.runInContext(`
 let modelFetchState='idle',modelSource='none',modelList=[],modelRequestSeq=0;
 let manualConnectionDrafts=new WeakMap(),preflightTimer=null,preflightSeq=0,drawerOpener=null;
 ${between('function currentModel(','async function loadConfig(')}
 ${between('function invalidateModelFetch(', "$('modelSelect').onchange=")}
 ${between('function cancelGatewayModelRequest(', "$('submit').onclick=")}
 ${single('clearTransientFeedback')}
 ${between('function toggleConfig(', "document.addEventListener('keydown',e=>{if(e.key==='Escape'&&$('config')")}
 ${source.split('\n').find(line=>line.startsWith("$('manualModelToggle').onclick="))}
 function readConfig(){return {url:$('url').value,api_key:$('api_key').value,model:currentModel()}}
 window.test={currentModel,setManualModel,setSavedModelOption,fillModelOptions,invalidateModelFetch,toggleConfig,
  seedManualDraft(node){manualConnectionDrafts.set(node,{url:'https://draft.invalid',key:'synthetic-private-draft'})},
  manualDraft(node){return manualConnectionDrafts.get(node)},state(){return {modelFetchState,modelRequestSeq}}};
 `,sandbox);
 return {t:sandbox.window.test,element,row,pending,requests,timers,calls,window:sandbox.window};
}

(async()=>{
 const h=harness(),{t,element:el,row,pending,requests,timers}=h;
 t.setSavedModelOption('Chosen/Model');
 el('manualModelToggle').onclick();el('model').value=' Manual/Draft ';
 check('manual entry has one active control and uses its own value',()=>{assert.equal(t.currentModel(),'Manual/Draft');assert.equal(row.hidden,true);assert.equal(el('modelSelect').disabled,true)});
 el('model').value=' ';
 check('empty manual input never submits the hidden list selection',()=>assert.equal(t.currentModel(),''));
 el('model').value='Manual/Draft';
 const beforeManual=requests.length;await el('loadModels').onclick();
 check('hidden list refresh cannot start requests during manual entry',()=>assert.equal(requests.length,beforeManual));
 el('manualModelToggle').onclick();
 check('list and manual choices remain separate drafts',()=>{assert.equal(t.currentModel(),'Chosen/Model');assert.equal(el('model').value,'Manual/Draft');assert.equal(el('model').disabled,true);assert.equal(row.hidden,false)});
 const refresh=el('loadModels').onclick();pending.shift().resolve({models:['Other/Model','Chosen/Model']});await refresh;
 check('same-connection refresh retains the selected model and manual draft',()=>{assert.equal(t.currentModel(),'Chosen/Model');assert.equal(el('model').value,'Manual/Draft')});
 el('manualModelToggle').onclick();el('api_key').value='synthetic-edited';t.invalidateModelFetch();
 check('credential edits retain explicit manual mode and input',()=>{assert.equal(t.currentModel(),'Manual/Draft');assert.equal(el('modelManualWrap').hidden,false);assert.equal(el('modelListHint').hidden,true)});
 el('manualModelToggle').onclick();
 const missing=el('loadModels').onclick();pending.shift().resolve({models:['Replacement/Model']});await missing;
 check('an absent prior model remains visible with unverified guidance',()=>{assert.equal(t.currentModel(),'Chosen/Model');assert.match(el('modelSelect').innerHTML,/本次列表未包含，未验证/);assert.match(el('modelListHint').textContent,/请核对模型标识/)});
 const failed=el('loadModels').onclick();pending.shift().reject(new Error('合成目录失败'));await failed;
 check('failed refresh retains the current visible choice with an error',()=>{assert.equal(t.currentModel(),'Chosen/Model');assert.match(el('modelListHint').textContent,/合成目录失败/);assert.equal(el('loadModels').disabled,false)});

 // A settles while B is still pending: A must not release B's busy controls.
 for(const outcome of ['resolve','reject']){
  const older=el('loadModels').onclick(),old=pending.shift();
  el('url').value=`https://next-${outcome}.invalid/v1`;t.invalidateModelFetch();
  const newer=el('loadModels').onclick(),next=pending.shift(),count=requests.length;
  old[outcome](outcome==='resolve'?{models:['Old/Response'],saved:true}:new Error('过期错误'));
  await older;
  check('old '+outcome+' leaves the current request busy',()=>{assert.equal(el('loadModels').disabled,true);assert.equal(el('loadModels').attrs['aria-busy'],'true');assert.equal(row.classList.contains('is-loading'),true);assert.doesNotMatch(el('modelSelect').innerHTML,/Old\/Response/);assert.doesNotMatch(el('modelListHint').textContent,/过期错误/)});
  await el('loadModels').onclick();
  check('old '+outcome+' cannot enable duplicate requests',()=>assert.equal(requests.length,count));
  next.resolve({models:['Current/Response']});await newer;
  check('current '+outcome+' replacement owns completion',()=>{assert.match(el('modelSelect').innerHTML,/Current\/Response/);assert.equal(el('loadModels').disabled,false);assert.equal(el('loadModels').attrs['aria-busy'],undefined)});
 }
 const older=el('loadModels').onclick(),old=pending.shift();t.invalidateModelFetch();
 const newer=el('loadModels').onclick();pending.shift().resolve({models:['Newer/Choice']});await newer;
 el('modelSelect').value='Newer/Choice';old.resolve({models:['Too/Late']});await older;
 check('old response after new completion cannot replace the new choice',()=>assert.equal(t.currentModel(),'Newer/Choice'));

 const timerFromPrevious=timers.at(-1),latest=el('loadModels').onclick();
 pending.shift().resolve({models:['Newer/Choice']});await latest;timerFromPrevious();
 check('old feedback timer cannot clear a newer completed state',()=>assert.equal(row.classList.contains('is-done'),true));
 const interrupted=el('loadModels').onclick();el('manualModelToggle').onclick();el('model').value='Manual/Wins';
 pending.shift().resolve({models:['Late/Directory'],saved:true,hint:'wrong hint'});await interrupted;
 check('switching to manual rejects the pending directory result and credential hint',()=>{assert.equal(t.currentModel(),'Manual/Wins');assert.equal(el('modelManualWrap').hidden,false);assert.doesNotMatch(el('modelSelect').innerHTML,/Late\/Directory/);assert.equal(h.window.savedApiKeyHint,undefined);assert.equal(el('loadModels').disabled,false)});
 el('manualModelToggle').onclick();t.toggleConfig(true);t.seedManualDraft(el('config'));
 const closed=el('loadModels').onclick(),beforeClose=el('modelSelect').innerHTML;t.toggleConfig(false);
 pending.shift().resolve({models:['Closed/Drawer'],saved:true,hint:'closed hint'});await closed;
 check('closing the drawer cancels pending model work and clears temporary connection drafts',()=>{assert.equal(el('config').inert,true);assert.equal(el('modelSelect').innerHTML,beforeClose);assert.equal(el('loadModels').disabled,false);assert.equal(t.manualDraft(el('config')),undefined);assert.equal(h.window.savedApiKeyHint,undefined)});
 t.toggleConfig(true);
 check('reopening cannot restore a discarded connection draft',()=>assert.equal(t.manualDraft(el('config')),undefined));
 assert.equal(pending.length,0);
 console.log(JSON.stringify({status:'passed',checks,syntheticRequests:requests.length}));
})().catch(error=>{console.error(error);process.exitCode=1});
