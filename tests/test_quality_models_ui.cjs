'use strict';
/* Quality model entry and connection-race regression; no server or credentials. */
const fs=require('node:fs'),path=require('node:path'),vm=require('node:vm'),assert=require('node:assert/strict');
const source=fs.readFileSync(path.resolve(__dirname,'../static/app.js'),'utf8');
const slice=(start,end)=>source.slice(source.indexOf(start),source.indexOf(end,source.indexOf(start)));
const block=slice('function qualityResourceTemplate(','let qualityCredentialSeq=');
const invalidate=slice('function invalidateQualityModels(','function savedConnectionInputChanged(');
const validate=slice('function validateQualityForm(','function validateGatewayForm(');
const read=source.match(/^function readQuality\([^\n]+/m)[0];
const nodes=new Map(),rows=new Map(),pending=[],calls=[];
let focus='',checks=0;
function node(id=''){
 const n={id,value:'',innerHTML:'',textContent:'',hidden:false,disabled:false,isConnected:true,dataset:{},attrs:{},classes:new Set(),
  setAttribute(key,value){this.attrs[key]=String(value)},removeAttribute(key){delete this.attrs[key]},focus(){focus=this.id}};
 n.classList={add:(...values)=>values.forEach(v=>n.classes.add(v)),remove:(...values)=>values.forEach(v=>n.classes.delete(v))};
 return n;
}
function element(id){if(!nodes.has(id))nodes.set(id,node(id));return nodes.get(id)}
function resource(index){
 const row=node('resource-'+index);row.dataset.qualityIndex=String(index);row.fields={};
 for(const suffix of ['name','url','key','model','model-refresh','model-hint','model-row'])row.fields['.quality-'+suffix]=node('quality-'+suffix+'-'+index);
 row.fields['.quality-name'].value='资源 '+index;row.fields['.quality-url'].value='https://synthetic.invalid/v1';row.fields['.quality-key'].value='synthetic-key';row.fields['.quality-model'].disabled=true;
 row.querySelector=selector=>row.fields[selector]||null;rows.set(String(index),row);
 for(const suffix of ['manual','manual-wrap','toggle']){nodes.set(`quality-model-${suffix}-${index}`,node(`quality-model-${suffix}-${index}`));}
 element(`quality-model-manual-wrap-${index}`).hidden=true;element(`quality-model-manual-${index}`).disabled=true;
 return row;
}
const sandbox={window:{},document:{getElementById:element,querySelector:selector=>rows.get(selector.match(/data-quality-index="(\d+)"/)?.[1])||null,querySelectorAll:selector=>selector==='#qualityResourceFields .quality-resource'?[...rows.values()]:[]},
 URL,console,schedulePreflight(){},focusCreateField(input){input?.focus()},savedConnectionPickerHtml(){return ''},
 boundConnectionId(_context,index){return rows.get(String(index))?.dataset.connectionId||''},
 api(url,options){calls.push({url,body:JSON.parse(options.body)});return new Promise((resolve,reject)=>pending.push({resolve,reject}))}};
vm.runInNewContext(fs.readFileSync(path.resolve(__dirname,'../static/ui-kit.js'),'utf8'),sandbox);
vm.runInNewContext(`const $=id=>document.getElementById(id),UI=window.UI,esc=UI.esc;\n${block}\n${invalidate}\n${validate}\n${read}\nwindow.test={qualityResourceTemplate,currentQualityModel,setQualityModelMode,refreshQualityModels,invalidateQualityModels,validateQualityForm,readQuality};`,sandbox);
const t=sandbox.window.test,check=(name,fn)=>{fn();checks++},field=(row,name)=>row.querySelector('.quality-'+name);
async function load(row,models){const task=t.refreshQualityModels(Number(row.dataset.qualityIndex));pending.shift().resolve({models});await task;}
(async()=>{
 const markup=t.qualityResourceTemplate(0);
 check('model fallback uses shared input and action components',()=>{assert.match(markup,/data-click-action="setQualityModelMode"/);assert.match(markup,/quality-model-manual-0/);assert.match(markup,/aria-controls="quality-model-manual-wrap-0"/)});
 let row=resource(0);await load(row,['first','chosen','chosen',' <quoted> ']);
 check('first load requires an explicit choice',()=>{assert.equal(field(row,'model').value,'');assert.equal(field(row,'model').disabled,false);assert.match(field(row,'model').innerHTML,/&lt;quoted&gt;/);assert.equal((field(row,'model').innerHTML.match(/value="chosen"/g)||[]).length,1)});
 field(row,'model').value='chosen';await load(row,['new-first','chosen']);
 check('refresh preserves a still-available chosen model',()=>assert.equal(t.currentQualityModel(row),'chosen'));
 const transient=t.refreshQualityModels(0);pending.shift().reject(new Error('合成临时失败'));await transient;
 check('failed refresh retains a previously verified selection with an explicit warning',()=>{assert.equal(t.currentQualityModel(row),'chosen');assert.equal(field(row,'model').disabled,false);assert.match(field(row,'model-hint').textContent,/本次刷新未验证/)});
 await load(row,['new-first']);
 check('removed model is cleared with recovery guidance',()=>{assert.equal(t.currentQualityModel(row),'');assert.match(field(row,'model-hint').textContent,/原选模型已不在列表中/);assert.throws(()=>t.validateQualityForm(),/请先获取并选择模型/)});
 field(row,'model').value='new-first';t.setQualityModelMode(0);element('quality-model-manual-0').value=' Manual/Model ';
 check('manual entry is accepted without a successful directory request',()=>{assert.equal(t.currentQualityModel(row),'Manual/Model');assert.doesNotThrow(()=>t.validateQualityForm());assert.equal(t.readQuality().resources[0].model,'Manual/Model');assert.equal(field(row,'model-row').hidden,true)});
 t.setQualityModelMode(0);
 check('returning to the list restores its prior selection',()=>{assert.equal(t.currentQualityModel(row),'new-first');assert.equal(element('quality-model-manual-0').disabled,true);assert.equal(field(row,'model-row').hidden,false)});
 t.setQualityModelMode(0);
 check('returning to manual mode preserves the typed draft',()=>assert.equal(t.currentQualityModel(row),'Manual/Model'));
 element('quality-model-manual-0').value='   ';
 check('empty manual model is rejected at its field',()=>{assert.throws(()=>t.validateQualityForm(),/请填写手动模型标识/);assert.equal(focus,'quality-model-manual-0')});
 element('quality-model-manual-0').value='Manual/Model';t.invalidateQualityModels(row);
 check('connection edits clear both model sources',()=>{assert.equal(t.currentQualityModel(row),'');assert.equal(field(row,'model').value,'');assert.equal(element('quality-model-manual-0').value,'');assert.match(field(row,'model-hint').textContent,/原模型已清空/)});
 t.setQualityModelMode(0);
 const request=t.refreshQualityModels(0),before=calls.length;await t.refreshQualityModels(0);
 check('pending model fetch prevents duplicates and blocks submission',()=>{assert.equal(calls.length,before);assert.throws(()=>t.validateQualityForm(),/正在获取模型列表/);assert.equal(field(row,'model-refresh').attrs['aria-busy'],'true')});
 t.setQualityModelMode(0);element('quality-model-manual-0').value='Manual/Wins';pending.shift().resolve({models:['late-list']});await request;
 check('switching to manual discards pending list responses',()=>{assert.equal(t.currentQualityModel(row),'Manual/Wins');assert.equal(row.dataset.modelPending,undefined);assert.equal(field(row,'model-refresh').disabled,false)});
 t.setQualityModelMode(0);const old=t.refreshQualityModels(0);field(row,'url').value='https://new.invalid/v1';t.invalidateQualityModels(row);
 const next=t.refreshQualityModels(0),older=pending.shift(),newer=pending.shift();newer.resolve({models:['new-model']});await next;field(row,'model').value='new-model';older.resolve({models:['old-model']});await old;
 check('out-of-order old-address responses never overwrite new selections',()=>assert.equal(t.currentQualityModel(row),'new-model'));
 const silent=t.refreshQualityModels(0);field(row,'key').value='changed-without-input';pending.shift().resolve({models:['wrong-key-model']});await silent;
 check('silent credential changes are checked against the captured connection',()=>{assert.equal(t.currentQualityModel(row),'');assert.equal(row.dataset.modelPending,undefined);assert.equal(field(row,'model-refresh').disabled,false)});
 const bound=t.refreshQualityModels(0);row.dataset.connectionId='new-binding';pending.shift().resolve({models:['wrong-binding']});await bound;
 check('saved connection changes discard in-flight lists',()=>assert.equal(t.currentQualityModel(row),''));
 const detached=t.refreshQualityModels(0),previousRow=row;row=resource(0);previousRow.isConnected=false;element('quality-model-manual-0').value='Replacement/Draft';pending.shift().resolve({models:['closed-drawer-model']});await detached;
 check('removed form responses cannot mutate a replacement row or its manual draft',()=>{assert.equal(field(row,'model').innerHTML,'');assert.equal(element('quality-model-manual-0').value,'Replacement/Draft')});
 const failure=t.refreshQualityModels(0);pending.shift().reject(new Error('目录不受支持'));await failure;
 check('failed discovery leaves a manual recovery path',()=>{assert.match(field(row,'model-hint').textContent,/手动指定模型/);assert.equal(field(row,'model-refresh').disabled,false)});
 t.setQualityModelMode(0);element('quality-model-manual-0').value='Fallback/Model';
 check('manual fallback validates after an unsupported directory',()=>assert.doesNotThrow(()=>t.validateQualityForm()));
 const other=resource(1);t.setQualityModelMode(1);element('quality-model-manual-1').value='Reference/Model';
 check('paired resources keep independent model entry methods',()=>{assert.deepEqual(Array.from(t.readQuality().resources,r=>r.model),['Fallback/Model','Reference/Model']);assert.doesNotThrow(()=>t.validateQualityForm())});
 element('qualityReuse').value='history-id';element('quality-model-manual-1').value='';
 check('historical reference reuse skips the hidden reference model',()=>{assert.doesNotThrow(()=>t.validateQualityForm());assert.equal(t.readQuality().resources.length,1)});
 assert.equal(pending.length,0);console.log(JSON.stringify({status:'passed',checks,syntheticRequests:calls.length}));
})().catch(error=>{console.error(error);process.exitCode=1});
