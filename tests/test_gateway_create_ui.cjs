'use strict';
/* Exercise the real pressure form handlers using synthetic fields and templates.
 * No server, upstream request, credential store or user configuration is used. */
const assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path'),vm=require('node:vm');
const source=fs.readFileSync(path.resolve(__dirname,'../static/app.js'),'utf8');
const templatesSource=source.slice(source.indexOf('const TEMPLATE_CATEGORIES='),source.indexOf('/* ---------- 运行诊断 ---------- */'));
const modesSource=source.slice(source.indexOf('function paintMode('),source.indexOf('let currentUser=',source.indexOf('function paintMode(')));
const validationSource=source.slice(source.indexOf('function validateGatewayForm('),source.indexOf("\ndocument.addEventListener('input',e=>",source.indexOf('function validateGatewayForm(')));
const rolesSource=source.slice(source.indexOf('function normalizeComparisonRoles('),source.indexOf('function renderQualityFields('));
const readComparisonSource=source.slice(source.indexOf('function readComparison('),source.indexOf('function qualityStatusLabel('));
const modeDeclaration=source.match(/^const modeTemplates=.*;$/m)?.[0];
assert.ok(modeDeclaration&&templatesSource&&modesSource,'pressure form source boundaries exist');
const defer=()=>{let resolve;const promise=new Promise(done=>resolve=done);return {promise,resolve}};
let checks=0;
function check(name,fn){fn();checks++}
function harness(){
 const nodes=new Map(),listeners={},preflights=[],rows=[],messages=[];
 let focused=null;
 let handler=async()=>({templates:[]});
 class Node{
  constructor(id,value=''){this.id=id;this.value=value;this.dataset={};this.attributes={};this.textContent='';this.children=[];this.className='';this.hidden=false;this.checked=false;this._html=''}
  set value(value){this._value=String(value)}
  get value(){return this._value}
  set innerHTML(html){
   this._html=html;
   for(const node of this.children)nodes.delete(node.id);
   this.children=[];
   for(const match of html.matchAll(/<input\b[^>]*>/g)){
    const id=match[0].match(/\bid="([^"]+)"/)?.[1];if(!id)continue;
    const node=new Node(id,match[0].match(/\bvalue="([^"]*)"/)?.[1]||'');node.parent=this;
    nodes.set(id,node);this.children.push(node);
   }
  }
  get innerHTML(){return this._html}
  get classList(){return {contains:key=>this.className.split(/\s+/).includes(key),remove:key=>{this.className=this.className.split(/\s+/).filter(value=>value!==key).join(' ')}}}
  setAttribute(name,value){this.attributes[name]=String(value)}
  removeAttribute(name){delete this.attributes[name]}
  closest(selector){return this.parent?.id==='modeFields'||['concurrency','connect_timeout','read_timeout'].includes(this.id)?this:null}
 }
 for(const [id,value] of Object.entries({modeFields:'',groupFields:'',addGroup:'',comparisonName:'',comparisonCapture:'',templatePickerBody:'',formStatus:'',toast:'',profile:'cache',url:'https://synthetic.invalid/v1',api_key:'synthetic-key',model:'synthetic-model',name:'用户填写的任务',concurrency:20,connect_timeout:10,read_timeout:60,capture_raw:''}))nodes.set(id,new Node(id,value));
 nodes.get('capture_raw').checked=true;
 const tabs=['smoke','ramp','soak'].map(value=>({dataset:{mode:value},classList:{toggle(){}},setAttribute(){}}));
 const sandbox={console,window:{},URL,$:(id)=>nodes.get(id)||null,
  document:{querySelectorAll:selector=>selector==='.tab[data-mode]'?tabs:selector==='#groupFields .group-row'||selector==='.group-row'?rows:[],querySelector:selector=>selector==='#comparisonFields [data-click-action="addComparisonGroup"]'?nodes.get('addGroup'):null,addEventListener:(event,fn)=>(listeners[event]??=[]).push(fn)},
  esc:value=>String(value??'').replace(/[<>&"]/g,char=>({'<':'&lt;','>':'&gt;','&':'&amp;','"':'&quot;'}[char])),
  num:value=>String(value??'—'),duration:value=>`${value} 秒`,
  api:async()=>handler(),schedulePreflight:()=>preflights.push(true),currentModel:()=> 'synthetic-model',focusCreateField:node=>{focused=node},toast:message=>messages.push(message),readConfig:()=>({profile:nodes.get('profile').value})};
 vm.runInNewContext(`let mode='smoke',createKind='gateway-single',modelFetchState='idle';
 function isGatewayCreate(){return createKind.startsWith('gateway-')}
 function isCompareCreate(){return createKind==='gateway-compare'}
 ${modeDeclaration}
 ${templatesSource}
 ${modesSource}
 ${validationSource}
 ${rolesSource}
 ${readComparisonSource}
 window.test={applyTemplate,setMode,readDynamic,applyDynamic,resetPressureDrafts,paintMode,validateGatewayForm,normalizeComparisonRoles,changeComparisonRole,removeComparisonGroup,readComparison,
 setTemplates(items){templateCache=items;renderTemplatePicker(items)},getMode:()=>mode,getActive:()=>activeTemplateId,
 setKind(value){createKind=value}};
 `,sandbox);
 sandbox.window.test.paintMode('smoke');
 return {ui:sandbox.window.test,nodes,preflights,rows,messages,getFocused:()=>focused,setHandler(fn){handler=fn},edit(id,value,event='input'){const node=nodes.get(id);node.value=value;for(const fn of listeners[event]||[])fn({target:node})},
  addRow(role='comparison'){
   const fields=Object.fromEntries(Object.entries({'group-role':role,'group-name':'','group-concurrency':'20','group-rpm':'100','group-iterations':'20','group-remove':''}).map(([key,value])=>[key,new Node(key,value)]));
   const row={querySelector:selector=>fields[selector.slice(1)]||null,remove(){rows.splice(rows.indexOf(row),1)}};
   fields['group-remove'].closest=()=>row;rows.push(row);return fields;
  }};
}
const plans=[
 {id:'smoke-plan',name:'短测',config:{mode:'smoke',rpm:120,iterations:25,concurrency:7,connect_timeout:4,read_timeout:75}},
 {id:'ramp-plan',name:'阶梯',config:{mode:'ramp',ramps:'80,160,320',duration:45,concurrency:9,connect_timeout:5,read_timeout:90}},
 {id:'soak-plan',name:'长跑',config:{mode:'soak',rpm:180,duration:900,concurrency:11,connect_timeout:6,read_timeout:120}}
].map(item=>({...item,config:{...item.config,profile:'short',url:'https://must-not-apply.invalid',api_key:'must-not-apply',model:'must-not-apply',name:'must-not-apply',capture_raw:false}}));

(async()=>{
 for(const profile of ['smoke','short','long','cache']){
  for(const plan of plans){
   const a=harness();a.ui.setTemplates(plans);a.nodes.get('profile').value=profile;
   await a.ui.applyTemplate(plan.id);
   check(`${profile}: ${plan.id} changes only pressure configuration`,()=>{
    assert.equal(a.nodes.get('profile').value,profile);assert.equal(a.nodes.get('url').value,'https://synthetic.invalid/v1');
    assert.equal(a.nodes.get('api_key').value,'synthetic-key');assert.equal(a.nodes.get('model').value,'synthetic-model');
    assert.equal(a.nodes.get('name').value,'用户填写的任务');assert.equal(a.nodes.get('capture_raw').checked,true);
    assert.equal(a.ui.getMode(),plan.config.mode);
    for(const key of ['concurrency','connect_timeout','read_timeout'])assert.equal(a.nodes.get(key).value,String(plan.config[key]));
    assert.equal(a.ui.getActive(),plan.id);assert.match(a.nodes.get('templatePickerBody').innerHTML,/aria-pressed="true"/);
   });
  }
 }
 const order=harness();order.ui.setTemplates(plans);
 for(const plan of plans){
  order.nodes.get('profile').value='cache';await order.ui.applyTemplate(plan.id);
  const before=JSON.stringify(order.ui.readDynamic());
  order.nodes.get('profile').value='smoke';await order.ui.applyTemplate(plan.id);order.nodes.get('profile').value='cache';
  check(`${plan.id}: cache and pressure selection are order independent`,()=>{assert.equal(order.nodes.get('profile').value,'cache');assert.equal(JSON.stringify(order.ui.readDynamic()),before)});
 }
 const draft=harness();draft.ui.setTemplates(plans);
 draft.edit('rpm','321');draft.edit('iterations','77');draft.ui.setMode('ramp');draft.edit('ramps','30,90,180');draft.edit('duration','42');
 draft.ui.setMode('soak');
 check('new long-run mode keeps its own duration default',()=>{assert.equal(draft.nodes.get('duration').value,'1800');assert.equal(draft.nodes.get('rpm').value,'100')});
 draft.edit('rpm','456');draft.edit('duration','');draft.ui.setMode('ramp');
 check('switching back restores ramp fields without long-run duration leakage',()=>{assert.equal(draft.nodes.get('ramps').value,'30,90,180');assert.equal(draft.nodes.get('duration').value,'42')});
 draft.ui.setMode('smoke');
 check('switching back restores request count and rate',()=>{assert.equal(draft.nodes.get('rpm').value,'321');assert.equal(draft.nodes.get('iterations').value,'77')});
 draft.ui.setMode('soak');
 check('in-progress empty edits survive mode navigation',()=>{assert.equal(draft.nodes.get('duration').value,'');assert.equal(draft.nodes.get('rpm').value,'456')});
 const changes=draft.preflights.length;draft.ui.setMode('soak');
 check('reselecting the same mode leaves its draft and preflight untouched',()=>{assert.equal(draft.nodes.get('duration').value,'');assert.equal(draft.preflights.length,changes)});
 await draft.ui.applyTemplate('soak-plan');draft.ui.setMode('ramp');
 check('switching pressure mode clears misleading template selection',()=>{assert.equal(draft.ui.getActive(),null);assert.doesNotMatch(draft.nodes.get('templatePickerBody').innerHTML,/aria-pressed="true"/)});
 await draft.ui.applyTemplate('smoke-plan');draft.edit('concurrency','8','change');
 check('changing shared pressure settings clears template highlight and stale success text',()=>{assert.equal(draft.ui.getActive(),null);assert.match(draft.nodes.get('formStatus').textContent,/自定义配置/)});
 await draft.ui.applyTemplate('ramp-plan');draft.edit('duration','46');
 check('changing dynamic pressure settings clears template highlight',()=>assert.equal(draft.ui.getActive(),null));
 draft.ui.resetPressureDrafts();draft.ui.paintMode('smoke');draft.ui.setMode('ramp');draft.ui.setMode('soak');
 check('a new task cannot inherit another task’s mode drafts',()=>{assert.equal(draft.nodes.get('duration').value,'1800');assert.equal(draft.nodes.get('rpm').value,'100')});
 const late=harness(),pending=defer();late.setHandler(()=>pending.promise);late.nodes.get('profile').value='smoke';
 const loading=late.ui.applyTemplate('smoke-plan');late.nodes.get('profile').value='cache';pending.resolve({templates:plans});await loading;
 check('late template loading preserves the latest request-content selection',()=>{assert.equal(late.nodes.get('profile').value,'cache');assert.equal(late.nodes.get('rpm').value,'120')});
 const edited=harness(),pendingEdit=defer();edited.setHandler(()=>pendingEdit.promise);const loadingEdit=edited.ui.applyTemplate('smoke-plan');edited.edit('rpm','333');pendingEdit.resolve({templates:plans});await loadingEdit;
 check('editing pressure while a template loads cancels stale application',()=>{assert.equal(edited.nodes.get('rpm').value,'333');assert.equal(edited.ui.getActive(),null)});
 const reopened=harness(),pendingReset=defer();reopened.setHandler(()=>pendingReset.promise);const loadingReset=reopened.ui.applyTemplate('soak-plan');reopened.ui.resetPressureDrafts();pendingReset.resolve({templates:plans});await loadingReset;
 check('resetting a form invalidates its pending template application',()=>{assert.equal(reopened.ui.getMode(),'smoke');assert.equal(reopened.ui.getActive(),null)});
 for(const id of ['rpm','iterations','concurrency','connect_timeout','read_timeout']){
  const form=harness();form.nodes.get(id).value='';
  check(`${id}: an empty required pressure value blocks submission and focuses its field`,()=>{assert.throws(()=>form.ui.validateGatewayForm(),/请填写/);assert.equal(form.getFocused(),form.nodes.get(id))});
 }
 for(const [id,value] of [['rpm','0.009'],['rpm','600001'],['rpm','Infinity'],['rpm','0x10'],['iterations','1.5'],['iterations','10000001'],['concurrency','0'],['concurrency','2001'],['concurrency','1.1'],['connect_timeout','0.09'],['read_timeout','3601']]){
  const form=harness();form.nodes.get(id).value=value;
  check(`${id}: invalid boundary ${value} blocks submission`,()=>{assert.throws(()=>form.ui.validateGatewayForm(),/应为/);assert.equal(form.getFocused(),form.nodes.get(id))});
 }
 const limits=harness();limits.nodes.get('rpm').value='0.015';limits.nodes.get('iterations').value='10000000';limits.nodes.get('concurrency').value='2000';limits.nodes.get('connect_timeout').value='0.15';limits.nodes.get('read_timeout').value='3600';
 check('valid backend boundaries and fractional rate/timeout values are accepted',()=>assert.doesNotThrow(()=>limits.ui.validateGatewayForm()));
 limits.ui.setMode('soak');
 for(const value of ['','1.5','86401']){
  limits.nodes.get('duration').value=value;
  check(`long-run duration ${JSON.stringify(value)} cannot silently use the 60-second fallback`,()=>{assert.throws(()=>limits.ui.validateGatewayForm(),/请填写|应为/);assert.equal(limits.getFocused(),limits.nodes.get('duration'))});
 }
 limits.nodes.get('duration').value='86400';
 check('the maximum integer duration is accepted',()=>assert.doesNotThrow(()=>limits.ui.validateGatewayForm()));
 limits.ui.setMode('ramp');limits.nodes.get('ramps').value=Array(50).fill('0.001').join(',');
 check('ramp accepts 50 positive fractional rates as supported by the backend',()=>assert.doesNotThrow(()=>limits.ui.validateGatewayForm()));
 for(const value of ['',',','10,,20','10,','10，20','0,10','600001','Infinity','1,NaN',Array(51).fill('100').join(',')]){
  limits.nodes.get('ramps').value=value;
  check(`invalid ramp list ${value.slice(0,20)} focuses the stage field`,()=>{assert.throws(()=>limits.ui.validateGatewayForm(),/请填写|英文逗号/);assert.equal(limits.getFocused(),limits.nodes.get('ramps'))});
 }
 const comparison=harness();comparison.ui.setKind('gateway-compare');const first=comparison.addRow('baseline'),second=comparison.addRow();comparison.ui.normalizeComparisonRoles();
 check('a two-group comparison cannot delete either required group',()=>{
  assert.equal(first['group-remove'].disabled,true);assert.equal(second['group-remove'].disabled,true);
  comparison.ui.removeComparisonGroup(first['group-remove']);assert.equal(comparison.rows.length,2);assert.match(comparison.messages.at(-1),/至少保留 2/);
 });
 second['group-role'].value='baseline';comparison.ui.changeComparisonRole(second['group-role']);
 check('the latest explicit baseline selection replaces the previous baseline',()=>{assert.equal(first['group-role'].value,'comparison');assert.equal(second['group-role'].value,'baseline')});
 comparison.ui.readComparison();comparison.ui.readComparison();
 check('preflight reads do not reclaim the first baseline or overwrite draft names',()=>{assert.equal(second['group-role'].value,'baseline');assert.equal(first['group-name'].value,'')});
 second['group-role'].value='comparison';comparison.ui.changeComparisonRole(second['group-role']);comparison.ui.readComparison();
 check('an unfinished role edit stays visible and requires an explicit baseline before submission',()=>{assert.equal(first['group-role'].value,'comparison');assert.equal(second['group-role'].value,'comparison');assert.throws(()=>comparison.ui.validateGatewayForm(),/基准组/)});
 second['group-role'].value='baseline';comparison.ui.changeComparisonRole(second['group-role']);comparison.nodes.get('rpm').value='';
 check('comparison validation uses visible group pressure rather than hidden single-test fields',()=>assert.doesNotThrow(()=>comparison.ui.validateGatewayForm()));
 second['group-rpm'].value='';
 check('comparison group fields cannot be empty',()=>{assert.throws(()=>comparison.ui.validateGatewayForm(),/请填写实验组 2/);assert.equal(comparison.getFocused(),second['group-rpm'])});
 second['group-rpm'].value='0.015';comparison.nodes.get('read_timeout').value='';
 check('comparisons also validate shared timeouts',()=>assert.throws(()=>comparison.ui.validateGatewayForm(),/读取超时/));
 comparison.nodes.get('read_timeout').value='60';comparison.addRow();comparison.addRow();comparison.ui.normalizeComparisonRoles();
 check('four groups disable the add action while enabling removal',()=>{assert.equal(comparison.nodes.get('addGroup').disabled,true);assert.equal(first['group-remove'].disabled,false)});
 comparison.ui.removeComparisonGroup(second['group-remove']);
 check('deleting a baseline prompts for a replacement without silently assigning one',()=>{assert.equal(comparison.rows.length,3);assert.equal(comparison.nodes.get('addGroup').disabled,false);assert.match(comparison.messages.at(-1),/选择一个基准组/);assert.throws(()=>comparison.ui.validateGatewayForm(),/基准组/)});
 const named=harness(),generated=named.addRow('baseline'),custom=named.addRow();named.rows[0].dataset={autoName:'基准组'};generated['group-name'].value='基准组';named.rows[1].dataset={autoName:'对比组 1'};custom['group-name'].value='用户命名的高压组';custom['group-role'].value='baseline';named.ui.changeComparisonRole(custom['group-role']);
 check('role changes update untouched generated names while preserving user-written group names',()=>{assert.equal(generated['group-name'].value,'对比组 1');assert.equal(custom['group-name'].value,'用户命名的高压组')});
 const errors=harness();errors.ui.setTemplates(plans);
 const setError=(notice='请填写计划调度时长（秒）。')=>{errors.nodes.get('formStatus').className='form-status error';errors.nodes.get('formStatus').textContent='请填写计划调度时长（秒）。';errors.nodes.get('toast').className='toast show';errors.nodes.get('toast').textContent=notice};
 setError();errors.nodes.get('concurrency').setAttribute('aria-invalid','true');await errors.ui.applyTemplate('soak-plan');
 check('applying a template clears its replaced form error, matching toast and pressure invalid marker',()=>{assert.equal(errors.nodes.get('formStatus').classList.contains('error'),false);assert.equal(errors.nodes.get('toast').classList.contains('show'),false);assert.equal(errors.nodes.get('concurrency').attributes['aria-invalid'],undefined)});
 setError();errors.nodes.get('duration').setAttribute('aria-invalid','true');errors.edit('duration','600');
 check('editing pressure removes stale submission errors and the matching error toast',()=>{assert.equal(errors.nodes.get('formStatus').textContent,'');assert.equal(errors.nodes.get('formStatus').classList.contains('error'),false);assert.equal(errors.nodes.get('toast').classList.contains('show'),false);assert.equal(errors.nodes.get('duration').attributes['aria-invalid'],undefined)});
 setError('另一个任务已完成');await errors.ui.applyTemplate('smoke-plan');
 check('template success does not dismiss an unrelated notification',()=>{assert.equal(errors.nodes.get('toast').classList.contains('show'),true);assert.equal(errors.nodes.get('toast').textContent,'另一个任务已完成')});
 setError('另一个任务已完成');errors.edit('concurrency','12');
 check('editing pressure preserves unrelated notifications',()=>assert.equal(errors.nodes.get('toast').classList.contains('show'),true));
 console.log(`gateway creation UI checks passed: ${checks}`);
})().catch(error=>{console.error(error);process.exitCode=1});
