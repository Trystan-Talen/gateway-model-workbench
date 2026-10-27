/* Named connection binding with synthetic metadata and a minimal DOM.
 * No real credentials, browser, service or upstream requests are used. */
const fs=require('node:fs'),path=require('node:path'),vm=require('node:vm'),assert=require('node:assert/strict');
const source=fs.readFileSync(path.resolve(__dirname,'../static/app.js'),'utf8');
const block=source.slice(source.indexOf('/* ---------- Named local connections:'),source.indexOf('/* ---------- End named local connections ---------- */'));
const nodes=new Map(),rows=new Map(),calls=[],timers=[],listeners={};
function node(id=''){return {id,value:'',type:'password',innerHTML:'',textContent:'',disabled:false,dataset:{},classList:{add(){},remove(){},toggle(){}},setAttribute(){},removeAttribute(){},checkValidity(){return true},querySelector(selector){return this.children?.[selector]||null},matches(selector){return selector.split(',').some(value=>this.selector===value)},closest(selector){return this.parent?.selector===selector?this.parent:null}}}
function element(id){if(!nodes.has(id))nodes.set(id,node(id));return nodes.get(id)}
for(const side of ['baseline','candidate']){
 const box=node();box.selector='[data-routing-side]';box.dataset.routingSide=side;box.children={};
 for(const suffix of ['url','key','name']){const input=element(`routing-${side}-${suffix}`);input.selector='.routing-side-'+suffix;input.parent=box;box.children[input.selector]=input}
 rows.set('routing:'+side,box);
}
for(const index of ['0','1']){
 const box=node();box.selector='.quality-resource';box.dataset.qualityIndex=index;box.children={};
 for(const suffix of ['url','key','name','model','model-refresh','model-hint','model-row','credential-hint']){const input=element(`quality-${suffix}-${index}`);input.selector='.quality-'+suffix;input.parent=box;box.children[input.selector]=input}
 rows.set('quality:'+index,box);
}
const sandbox={window:{ConnectionUI:{list(){return []},openCreate(seed){calls.push({kind:'create',seed})},openEdit(id){calls.push({kind:'edit',id})}},addEventListener(name,callback){listeners[name]=callback}},document:{getElementById:element,querySelector(selector){const side=selector.match(/^\[data-routing-side="(baseline|candidate)"\]$/);if(side)return rows.get('routing:'+side[1]);const quality=selector.match(/^\.quality-resource\[data-quality-index="(\d+)"\]$/);if(quality)return rows.get('quality:'+quality[1]);return null},querySelectorAll(selector){if(selector==='.quality-resource[data-quality-index]'||selector==='#qualityResourceFields .quality-resource')return ['0','1'].map(index=>rows.get('quality:'+index));if(selector==='[data-routing-side]')return ['baseline','candidate'].map(side=>rows.get('routing:'+side));return []},addEventListener(){}},setTimeout(callback){timers.push(callback);return timers.length},clearTimeout(){},console,
 toast(message){calls.push({kind:'toast',message})},schedulePreflight(){calls.push({kind:'preflight'})},resetKeyButton(){},setSavedModelOption(value){element('modelSelect').value=value;calls.push({kind:'saved-model',value})},setModelHint(message){calls.push({kind:'model-hint',message})},invalidateModelFetch(){calls.push({kind:'model-invalidate'})},resetRoutingModels(message){calls.push({kind:'routing-model-invalidate',message})},checkSavedCredential(){calls.push({kind:'legacy-gateway'})},checkQualityCredential(){calls.push({kind:'legacy-quality'})},checkRoutingCredential(){calls.push({kind:'legacy-routing'})},saveRoutingSide(side){calls.push({kind:'routing-save',side})},updateKeyInputHint(){},setRoutingCredentialState(side,url,saved){calls.push({kind:'routing-hint',side,url,saved})},hideRoutingKey(side){const key=element(`routing-${side}-key`);if(key.dataset.routingRevealed==='true')key.value='';delete key.dataset.routingRevealed;key.type='password'},currentModel(){return 'synthetic-model'}};
vm.runInNewContext(fs.readFileSync(path.resolve(__dirname,'../static/ui-kit.js'),'utf8'),sandbox);
const singleLine=name=>source.match(new RegExp('^function '+name+'\\([^\\n]+','m'))[0];
vm.runInNewContext(`const $=id=>document.getElementById(id),UI=window.UI,mode='smoke',esc=UI.esc;let credentialStatusSeq=0,qualityCredentialSeq=0,qualityModelSeq=0;const routingConnectionTimers={};\n${block}\n${singleLine('readConfig')}\n${singleLine('currentQualityModel')}\n${singleLine('readQuality')}\n${singleLine('readRoutingSide')}\nwindow.test={selectSavedConnection,boundConnectionId,boundConnectionProfile,savedConnectionPickerHtml,savedConnectionInputChanged,renderSavedConnectionPickers,readConfig,readQuality,readRoutingSide,saveCurrentConnection,refreshSavedConnectionPickers,loaded(){return savedConnectionsLoaded},setProfiles(profiles,loaded=true){savedConnectionProfiles=profiles;savedConnectionsLoaded=loaded}};`,sandbox);
const t=sandbox.window.test,URL='https://synthetic.invalid/v1';
const profiles=[{id:'profile-a',name:'资源 A <unsafe>',url:URL,api_key_saved:true},{id:'profile-b',name:'资源 B',url:URL,api_key_saved:true}];
t.setProfiles(profiles);
const markup=t.savedConnectionPickerHtml('gateway');
assert.ok(markup.includes('data-change-action="selectSavedConnection"'));
assert.ok(markup.includes('资源 A &lt;unsafe&gt;'));
assert.ok(markup.includes('value="profile-a"')&&markup.includes('value="profile-b"'));
assert.equal(t.readConfig().concurrency,20,'empty new gateway config defaults to 20 concurrent requests');
assert.equal(t.readConfig().rpm,100,'empty new gateway config defaults to 100 planned requests per minute');
element('concurrency').value='37';element('rpm').value='123';
assert.equal(t.readConfig().concurrency,'37','explicit concurrency is preserved');
assert.equal(t.readConfig().rpm,'123','explicit rate is preserved');
element('concurrency').value='';element('rpm').value='';
for(const [context,target] of [['gateway',''],['quality','0'],['routing','baseline']]){
 const url=context==='gateway'?element('url'):element(context==='quality'?'quality-url-0':'routing-baseline-url');
 const key=context==='gateway'?element('api_key'):element(context==='quality'?'quality-key-0':'routing-baseline-key');
 url.value='https://manual.synthetic.invalid/v1';key.value='synthetic-manual-draft';
 t.selectSavedConnection(context,target,{value:'profile-a'});
 t.selectSavedConnection(context,target,{value:'profile-b'});
 t.selectSavedConnection(context,target,{value:''});
 assert.equal(url.value,'https://manual.synthetic.invalid/v1','returning from saved connections restores the original manual address');
 assert.equal(key.value,'synthetic-manual-draft','manual credential draft is kept only in the current form');
 assert.equal(key.type,'password','restored credential remains masked');
 assert.equal(t.boundConnectionId(context,target),'','restored manual input has no saved binding');
}
for(const [context,target] of [['gateway',''],['quality','0'],['routing','baseline']]){
 const details=element(`connection-details-${context}-${target||'main'}`);
 t.selectSavedConnection(context,target,{value:'profile-a'});
 assert.equal(details.open,false,'ready named connection collapses duplicate credentials');
 details.open=true;t.renderSavedConnectionPickers();
 assert.equal(details.open,true,'metadata refresh preserves a manually expanded disclosure');
 t.selectSavedConnection(context,target,{value:'profile-b'});
 assert.equal(details.open,false,'choosing another saved connection resets the disclosure');
 t.selectSavedConnection(context,target,{value:''});
 assert.equal(details.open,true,'manual connection keeps address and credentials visible');
}

const manualProbeCount=calls.filter(call=>call.kind==='legacy-gateway'||call.kind==='legacy-quality'||call.kind==='legacy-routing').length;
for(const [context,target] of [['gateway',''],['quality','0'],['routing','baseline']]){
 t.selectSavedConnection(context,target,{value:'profile-a'});
 const key=context==='gateway'?element('api_key'):element(context==='quality'?'quality-key-0':'routing-baseline-key');
 assert.equal(key.value,'','selection does not retrieve secret plaintext');
 assert.equal(t.boundConnectionId(context,target),'profile-a');
 const config=context==='gateway'?t.readConfig():context==='quality'?t.readQuality().resources[0]:t.readRoutingSide(target);
 assert.equal(config.connection_id,'profile-a');assert.equal(config.url,URL);assert.equal(config.api_key,'');
 t.selectSavedConnection(context,target,{value:'profile-b'});
 const configB=context==='gateway'?t.readConfig():context==='quality'?t.readQuality().resources[0]:t.readRoutingSide(target);
 assert.equal(configB.connection_id,'profile-b','same URL never collapses distinct profile credentials');
 const input=context==='gateway'?element('api_key'):key;
 input.value='synthetic-edited-key';t.savedConnectionInputChanged({target:input});
 assert.equal(t.boundConnectionId(context,target),'','editing key explicitly clears binding');
}
t.selectSavedConnection('routing','baseline',{value:'profile-a'});
const routeName=element('routing-baseline-name');routeName.value='自定义固定路由';t.savedConnectionInputChanged({target:routeName});
assert.equal(t.readRoutingSide('baseline').connection_id,'profile-a','renaming route preserves connection selection');
t.selectSavedConnection('quality','0',{value:'profile-a'});
const qualityName=element('quality-name-0');qualityName.value='自定义资源';t.savedConnectionInputChanged({target:qualityName});
assert.equal(t.readQuality().resources[0].connection_id,'profile-a','renaming resource preserves connection selection');
t.selectSavedConnection('gateway','',{value:'profile-a'});
t.setProfiles([profiles[1]]);t.renderSavedConnectionPickers();
assert.equal(t.readConfig().connection_id,'profile-a','deleted profile remains explicitly bound; no URL-based secret fallback');
assert.equal(element('gatewayConnectionPicker').dataset.connectionInvalid,'true');
assert.equal(element('connection-details-gateway-main').open,true,'invalid saved connection exposes fields for correction');
assert.equal(sandbox.window.savedApiKey,false);
assert.ok(t.savedConnectionPickerHtml('gateway').includes('已删除'));
assert.equal(element('url').value,URL,'metadata refresh leaves edited test fields intact');
t.setProfiles([{...profiles[0],url:'https://updated.invalid/v1'},profiles[1]]);t.renderSavedConnectionPickers();
assert.equal(t.readConfig().connection_id,'profile-a');assert.equal(t.readConfig().url,URL);
assert.equal(t.boundConnectionProfile('gateway'),null,'updated address cannot silently change selected connection');
assert.equal(element('gatewayConnectionPicker').dataset.connectionInvalid,'true');
t.selectSavedConnection('gateway','',{value:'profile-a'});
assert.equal(t.readConfig().url,'https://updated.invalid/v1','explicit reselect accepts updated address');
assert.equal(element('gatewayConnectionPicker').dataset.connectionInvalid,undefined);
t.saveCurrentConnection('gateway');assert.deepEqual(calls.at(-1),{kind:'edit',id:'profile-a'},'saving selected profile edits existing entry without reading key');
element('url').value='https://manual.invalid/v1';t.savedConnectionInputChanged({target:element('url')});
assert.equal(t.readConfig().connection_id,undefined);
element('api_key').value='synthetic-only';t.saveCurrentConnection('gateway');
assert.equal(calls.at(-1).kind,'create');assert.equal(calls.at(-1).seed.api_key,'synthetic-only');
assert.equal(calls.filter(call=>call.kind==='legacy-gateway'||call.kind==='legacy-quality'||call.kind==='legacy-routing').length,manualProbeCount,'profile selection and refresh never probe URL-based credentials');
const credentialFunction=source.slice(source.indexOf('async function checkSavedCredential('),source.indexOf('function invalidateModelFetch('));
vm.runInNewContext(credentialFunction+';window.test.checkSavedCredential=checkSavedCredential;',sandbox);
(async()=>{
 let resolveCredential;let statusRequests=0;
 sandbox.api=()=>{statusRequests++;return new Promise(resolve=>{resolveCredential=resolve})};
 element('api_key').value='';
 const pending=t.checkSavedCredential();
 assert.equal(statusRequests,1,'manual connection can check its legacy stored key');
 t.setProfiles(profiles);t.selectSavedConnection('gateway','',{value:'profile-a'});
 resolveCredential({saved:false,hint:'wrong-legacy-secret',storage_available:true});await pending;
 assert.equal(sandbox.window.savedApiKey,true,'late URL credential response never overwrites selected named profile');
 await t.checkSavedCredential();assert.equal(statusRequests,1,'selected profile never checks URL fallback');
 t.setProfiles([{...profiles[0],api_key_saved:false},profiles[1]]);t.renderSavedConnectionPickers();
 assert.equal(sandbox.window.savedApiKey,false,'profile credential availability is authoritative');
 assert.equal(element('connection-details-gateway-main').open,true,'unavailable saved credential exposes the key input');
 assert.equal(statusRequests,1);
 const originalProfiles=profiles.map(profile=>({...profile,updated_at:1}));t.setProfiles(originalProfiles);
 for(const [context,target] of [['gateway',''],['quality','0'],['routing','baseline']])t.selectSavedConnection(context,target,{value:'profile-a'});
 element('modelSelect').value='old-permission-model';element('model').value='old-manual-model';
 element('quality-model-0').value='old-permission-model';element('quality-model-0').disabled=false;
 element('routing-baseline-key').dataset.routingRevealed='true';element('routing-baseline-key').value='synthetic-old-profile-key';
 const beforeModelChanges=calls.filter(call=>call.kind==='model-invalidate'||call.kind==='routing-model-invalidate').length;
 const beforeQualitySequence=rows.get('quality:0').dataset.modelSeq;
 const rotatedProfiles=originalProfiles.map(profile=>profile.id==='profile-a'?{...profile,updated_at:2}:profile);
 sandbox.window.ConnectionUI.list=()=>rotatedProfiles;listeners['connections-changed']();
 assert.equal(t.readConfig().connection_id,'profile-a');assert.equal(t.readQuality().resources[0].connection_id,'profile-a');assert.equal(t.readRoutingSide('baseline').connection_id,'profile-a');
 assert.equal(element('modelSelect').value,'','same-URL key rotation clears gateway selected model');
 assert.equal(element('model').value,'','same-URL key rotation clears gateway manual model');
 assert.equal(element('quality-model-0').disabled,true,'same-URL key rotation invalidates quality model permissions');
 assert.notEqual(rows.get('quality:0').dataset.modelSeq,beforeQualitySequence,'quality model fetch in flight is invalidated');
 assert.equal(element('routing-baseline-key').value,'','key rotation clears previously revealed stale secret');
 assert.equal(calls.filter(call=>call.kind==='model-invalidate'||call.kind==='routing-model-invalidate').length,beforeModelChanges+2,'gateway and routing invalidate once on metadata revision');
 const settledChanges=calls.filter(call=>call.kind==='model-invalidate'||call.kind==='routing-model-invalidate').length,settledQualitySequence=rows.get('quality:0').dataset.modelSeq;
 listeners['connections-changed']();assert.equal(calls.filter(call=>call.kind==='model-invalidate'||call.kind==='routing-model-invalidate').length,settledChanges,'unchanged successful refresh retains model verification');assert.equal(rows.get('quality:0').dataset.modelSeq,settledQualitySequence);
 sandbox.window.ConnectionUI.list=()=>rotatedProfiles.map(profile=>profile.id==='profile-b'?{...profile,updated_at:3}:profile);listeners['connections-changed']();
 assert.equal(calls.filter(call=>call.kind==='model-invalidate'||call.kind==='routing-model-invalidate').length,settledChanges,'editing an unbound profile does not invalidate selected models');
 sandbox.window.ConnectionUI.refresh=async()=>{throw new Error('synthetic read failure')};await t.refreshSavedConnectionPickers();
 assert.equal(t.readConfig().connection_id,'profile-a');assert.equal(element('gatewayConnectionPicker').dataset.connectionInvalid,undefined,'failed refresh preserves previously loaded metadata and binding');
 assert.equal(sandbox.window.savedApiKey,true);
 t.setProfiles([],false);element('gatewayConnectionPicker').dataset.connectionRevision='';await t.refreshSavedConnectionPickers();
 assert.equal(t.loaded(),false,'failed first refresh is never treated as an empty successful list');
 assert.equal(t.readConfig().connection_id,'profile-a');assert.equal(element('gatewayConnectionPicker').dataset.connectionInvalid,undefined);
 console.log('Saved connection picker: binding, key isolation, manual edits, deletion/address updates, metadata rotation, stale-response and failed-refresh isolation passed');
})().catch(error=>{console.error(error);process.exitCode=1});
