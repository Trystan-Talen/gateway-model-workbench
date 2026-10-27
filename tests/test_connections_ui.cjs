'use strict';
/* Synthetic DOM and API regression: never contacts a server or credential store. */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const root = path.resolve(__dirname,'..');
const source = fs.readFileSync(path.join(root,'static/connections.js'),'utf8').replace(/\}\)\(\);\s*$/,'window.__connectionTest = {s,detailHtml,emptyHtml,onInput,onKeydown};\n})();');
const nodes = new Map(), events = {}, calls = [], notifications = [], dispatched = [];
const decode = value => String(value || '').replace(/&quot;/g,'"').replace(/&#39;/g,"'").replace(/&lt;/g,'<').replace(/&gt;/g,'>').replace(/&amp;/g,'&');
class Node {
  constructor(id) { this.id=id; this.value=''; this.type='text'; this.hidden=false; this.inert=false; this.isConnected=true; this.disabled=false; this.required=false; this.dataset={}; this.attrs={}; this.scrollTop=0; this.tabIndex=0; this._html=''; this.classes=new Set(); this.classList={contains:c=>this.classes.has(c),add:c=>this.classes.add(c),remove:c=>this.classes.delete(c),toggle:(c,yes)=>{if(yes === undefined)yes=!this.classes.has(c);yes?this.classes.add(c):this.classes.delete(c);return yes;}}; }
  set innerHTML(value) {
    this._html = value;
    if (this.id === 'connectionCreateBody') {
      for (const id of ['connectionName','connectionUrl','connectionKey']) {
        const tag = value.match(new RegExp(`<input[^>]*id="${id}"[^>]*>`))?.[0];
        if (!tag) { if(nodes.has(id))nodes.get(id).isConnected=false; nodes.delete(id); continue; }
        const node = new Node(id); node.value=decode(tag.match(/value="([^"]*)"/)?.[1]); node.type=tag.match(/type="([^"]*)"/)?.[1]||'text'; node.required=/\srequired(?:\s|>)/.test(tag); nodes.set(id,node);
        if(id==='connectionKey') { node.parentElement={querySelector:()=>nodes.get('keyToggle')}; }
      }
    }
  }
  get innerHTML() { return this._html; }
  setAttribute(name,value) { this.attrs[name]=String(value); }
  removeAttribute(name) { delete this.attrs[name]; }
  focus() { document.activeElement=this; }
  reportValidity() { return !this.required || Boolean(this.value); }
  getClientRects() { return [1]; }
  contains(node) { return ['connectionName','connectionUrl','connectionKey','keyToggle','connectionSubmit','connectionClose'].includes(node?.id); }
  querySelector(selector) { return selector === '.drawer-close' ? nodes.get('connectionClose') : null; }
  querySelectorAll() { return ['connectionClose','connectionName','connectionUrl','connectionKey','keyToggle','connectionSubmit'].map(id=>nodes.get(id)).filter(node=>node && !node.disabled); }
}
for (const id of ['recordPanel','connectionsSubnav','connectionsList','connectionsCount','connectionBackdrop','connectionDrawer','connectionCreateTitle','connectionCreateBody','connectionFormStatus','connectionSubmit','connectionClose','keyToggle','config','fingerprintDrawer','authGate','app','main','opener','detail','empty','loadingState','modulePlaceholder','crumb','recordTitle','recordSubtitle','recordCreateButton']) nodes.set(id,new Node(id));
nodes.get('authGate').hidden=true;
nodes.get('config').classList.add('mobile-open'); nodes.get('config').inert=false;
nodes.get('fingerprintDrawer').inert=true; nodes.get('app').inert=true;
const document = {activeElement:nodes.get('opener'),getElementById:id=>nodes.get(id)||null,querySelector:selector=>selector==='.app'?nodes.get('app'):selector==='.main'?nodes.get('main'):null,addEventListener:(name,fn)=>{events[name]=fn;},body:{classList:new Node('body').classList}};
let apiHandler = async(path,options)=>path==='/connections'?{connections:[],storage_available:true}:{};
const sandbox = {window:{dispatchEvent:event=>dispatched.push(event.type)},document,activeModule:'gateway',authenticated:true,modalStack:[],api:async(path,options)=>{calls.push({path,body:options?.body?JSON.parse(options.body):null}); return apiHandler(path,options);},toast:text=>notifications.push(text),confirm:()=>true,CustomEvent:class{constructor(type){this.type=type;}},Date,URL,console};
vm.runInNewContext(fs.readFileSync(path.join(root,'static/ui-kit.js'),'utf8'),sandbox);
vm.runInNewContext(source,sandbox);
const UI = sandbox.window.ConnectionUI, {s,detailHtml,emptyHtml,onInput,onKeydown} = sandbox.window.__connectionTest;
s.initialized=true;
let checks=0;
const check = (name, fn) => { fn(); checks++; };
const item = {id:'synthetic-1',name:'日常连接',url:'https://synthetic.invalid/v1',api_key_saved:true,updated_at:1};
const defer = () => { let resolve; const promise=new Promise(done=>{resolve=done;}); return {promise,resolve}; };
(async()=>{
  apiHandler=async()=>({connections:[{...item,api_key:'never-public'}],storage_available:true});
  await UI.refresh();
  check('public metadata excludes credentials and returned arrays cannot mutate the cache',()=>{const list=UI.list();assert.equal(list.length,1);assert.equal(list[0].api_key,undefined);list[0].name='modified';assert.equal(UI.list()[0].name,item.name);});
  const escaped=detailHtml({...item,name:'<script>bad</script>',url:'https://synthetic.invalid/?x=<img>'});
  check('detail uses shared components and escapes names and addresses',()=>{assert.match(escaped,/report-head/);assert.match(escaped,/report-section/);assert.match(escaped,/report-footer-actions/);assert.ok(!escaped.includes('<script>'));assert.match(escaped,/&lt;img&gt;/);assert.ok(!escaped.includes('never-public'));assert.match(emptyHtml(),/保存第一个连接/);});
  const count=calls.length;
  UI.openEdit(item.id);
  check('opening edit does not reveal saved credentials',()=>{assert.equal(calls.length,count);assert.equal(nodes.get('connectionKey').value,'');assert.equal(nodes.get('connectionKey').type,'password');assert.ok(UI.isOpen());assert.equal(nodes.get('config').inert,true);});
  UI.closeCreate();s.items=[{...item,api_key_saved:false}];UI.openEdit(item.id);
  check('editing a connection without its saved key requires replacement credentials',()=>{assert.equal(nodes.get('connectionKey').required,true);assert.match(nodes.get('connectionCreateBody').innerHTML,/密钥已不可用，请重新填写/);assert.ok(!nodes.get('connectionCreateBody').innerHTML.includes('留空保留原密钥'));});
  const missingKeyCalls=calls.length;await UI.save();
  check('missing replacement credentials are rejected before posting',()=>{assert.equal(calls.length,missingKeyCalls);assert.equal(document.activeElement.id,'connectionKey');assert.equal(UI.isOpen(),true);});
  UI.closeCreate();s.items=[item];document.activeElement=nodes.get('opener');UI.openEdit(item.id);
  apiHandler=async(path)=>path.endsWith('/reveal')?{api_key:'synthetic-saved-key'}:path==='/connections'?{connections:[item]}:{connection:item};
  await UI.revealKey();
  check('explicit eye action reveals saved key',()=>{assert.equal(nodes.get('connectionKey').value,'synthetic-saved-key');assert.equal(nodes.get('connectionKey').type,'text');assert.equal(s.retrieved,true);assert.equal(calls.at(-1).path,'/connections/synthetic-1/reveal');});
  await UI.save();
  check('retrieved key is omitted from ordinary edit submission',()=>{const saved=calls.findLast(call=>call.path==='/connections'&&call.body);assert.deepEqual(saved.body,{name:item.name,url:item.url,id:item.id});assert.equal(UI.isOpen(),false);assert.equal(nodes.get('config').inert,false);assert.equal(nodes.get('app').inert,true);assert.equal(document.activeElement.id,'opener');assert.ok(dispatched.includes('connections-changed'));});
  UI.openEdit(item.id); await UI.revealKey(); await UI.revealKey();
  check('hiding retrieved saved key clears it',()=>{assert.equal(nodes.get('connectionKey').value,'');assert.equal(nodes.get('connectionKey').type,'password');assert.equal(s.retrieved,false);});
  nodes.get('connectionKey').value='synthetic-new-key'; onInput({target:nodes.get('connectionKey')}); await UI.revealKey(); await UI.revealKey();
  check('new typed keys may be shown then masked without clearing',()=>{assert.equal(nodes.get('connectionKey').value,'synthetic-new-key');assert.equal(nodes.get('connectionKey').type,'password');});
  nodes.get('connectionUrl').value='https://other-synthetic.invalid/v1'; nodes.get('connectionKey').value=''; onInput({target:nodes.get('connectionUrl')});
  const beforeChangeSave=calls.length; await UI.save();
  check('changing address requires a newly entered key',()=>{assert.equal(calls.length,beforeChangeSave);assert.match(nodes.get('connectionFormStatus').textContent,/重新输入/);assert.equal(UI.isOpen(),true);});
  UI.closeCreate();
  UI.openCreate({name:'路由快捷保存',url:item.url,source_side:'candidate'});
  await UI.save();
  check('shortcut preserves source side and can reuse a saved key without copying plaintext',()=>{const body=calls.findLast(call=>call.path==='/connections'&&call.body).body;assert.deepEqual(body,{name:'路由快捷保存',url:item.url,source_side:'candidate'});assert.equal(body.api_key,undefined);});
  UI.openEdit(item.id);
  const pending=defer(); apiHandler=async path=>path.endsWith('/reveal')?pending.promise:{connections:[item]};
  const revealing=UI.revealKey(); UI.closeCreate(); pending.resolve({api_key:'late-secret'}); await revealing;
  check('closed drawers discard late credential reveal',()=>{assert.equal(s.retrieved,false);assert.equal(nodes.get('connectionKey'),undefined);assert.equal(UI.isOpen(),false);});
  UI.openEdit(item.id);
  const racing=defer(); apiHandler=async()=>racing.promise;
  const racingReveal=UI.revealKey(); nodes.get('connectionUrl').value='https://changed.invalid'; onInput({target:nodes.get('connectionUrl')}); racing.resolve({api_key:'stale-url-key'}); await racingReveal;
  check('address changes discard in-flight reveals',()=>{assert.equal(nodes.get('connectionKey').value,'');assert.equal(nodes.get('keyToggle').disabled,false);});
  UI.closeCreate(); UI.openCreate({name:'新连接',url:item.url,api_key:'synthetic-input'});
  let prevented=false, stopped=false; onKeydown({key:'Escape',preventDefault(){prevented=true;},stopImmediatePropagation(){stopped=true;}});
  check('Escape closes only the connection overlay and restores the test form',()=>{assert.ok(prevented&&stopped);assert.equal(UI.isOpen(),false);assert.ok(nodes.get('config').classList.contains('mobile-open'));assert.equal(nodes.get('config').inert,false);});
  apiHandler=async()=>({connections:[item]}); UI.openManage(); await UI.refresh();
  check('management shortcut stacks over the test drawer',()=>{assert.ok(UI.isOpen());assert.equal(nodes.get('connectionSubmit').dataset.clickAction,'openConnectionCreate');assert.match(nodes.get('connectionCreateBody').innerHTML,/selectConnection|openConnectionEdit/);assert.equal(nodes.get('config').inert,true);UI.hide();assert.ok(UI.isOpen());});
  UI.closeCreate();
  sandbox.activeModule='connections';
  s.items=[item]; s.selected=item.id; s.loaded=true; UI.render();
  const second={...item,id:'synthetic-2',name:'第二个连接',updated_at:2}, savedList=defer();
  apiHandler=async(path,options)=>path==='/connections'&&options?.method==='POST'?{ok:true,connection:second}:savedList.promise;
  UI.openCreate({name:second.name,url:second.url,api_key:'synthetic-second-key'});
  const savingSecond=UI.save(); await new Promise(setImmediate);
  check('newly saved connection stays selected while refreshing the old list',()=>{assert.equal(s.pending,true);assert.equal(s.selected,second.id);assert.ok(nodes.get('modulePlaceholder').innerHTML.includes(second.name));assert.ok(UI.list().some(connection=>connection.id===second.id));});
  savedList.resolve({connections:[second,item]}); await savingSecond;
  check('newly saved connection remains selected after refresh completes',()=>{assert.equal(s.selected,second.id);assert.ok(nodes.get('modulePlaceholder').innerHTML.includes(second.name));});
  sandbox.activeModule='gateway';
  const eventsBeforeRefresh=dispatched.length;
  apiHandler=async()=>({connections:[second,item],storage_available:true}); await UI.refresh();
  check('every valid refresh publishes updated public metadata',()=>{assert.equal(dispatched.length,eventsBeforeRefresh+1);assert.equal(dispatched.at(-1),'connections-changed');});
  const cacheBeforeFailure=JSON.stringify(UI.list()), eventsBeforeFailure=dispatched.length;
  apiHandler=async()=>{throw new Error('合成读取失败');}; await assert.rejects(UI.refresh(),/合成读取失败/);
  check('failed refresh preserves the last cache and emits no deletion event',()=>{assert.equal(JSON.stringify(UI.list()),cacheBeforeFailure);assert.equal(dispatched.length,eventsBeforeFailure);assert.equal(s.loaded,true);assert.equal(s.pending,false);assert.equal(s.error,'合成读取失败');});
  UI.reset(); sandbox.activeModule='connections'; const eventsBeforeInitialFailure=dispatched.length, callsBeforeInitialFailure=calls.length;
  await assert.rejects(UI.refresh(),/合成读取失败/);
  check('initial refresh failure does not pretend an empty library was loaded',()=>{assert.equal(s.loaded,false);assert.equal(s.pending,false);assert.equal(dispatched.length,eventsBeforeInitialFailure);});
  check('active connection module stops automatic retries after an initial read failure',()=>{assert.equal(calls.length,callsBeforeInitialFailure+1);assert.match(nodes.get('modulePlaceholder').innerHTML,/常用连接读取失败/);UI.render();assert.equal(calls.length,callsBeforeInitialFailure+1);});
  sandbox.activeModule='gateway';
  apiHandler=async()=>({connections:[second,item]}); await UI.refresh();
  const third={...item,id:'synthetic-3',name:'延迟保存连接',updated_at:3}, delayedPost=defer();
  apiHandler=async(path,options)=>path==='/connections'&&options?.method==='POST'?delayedPost.promise:{connections:[third,second,item]};
  UI.openCreate({name:third.name,url:third.url,api_key:'synthetic-third-key'});
  const lateSave=UI.save(); UI.closeCreate();
  UI.openCreate({name:'保留的新表单',url:'https://new-form.invalid/v1',api_key:'new-form-synthetic-key'});
  const replacementSession=s.drawerSession, eventsBeforeLateSave=dispatched.length;
  delayedPost.resolve({ok:true,connection:third}); await lateSave;
  check('successful delayed save updates the cache after its original drawer was closed',()=>{assert.ok(UI.list().some(connection=>connection.id===third.id));assert.ok(dispatched.length>eventsBeforeLateSave);});
  check('successful delayed save does not close or reset the replacement drawer',()=>{assert.equal(UI.isOpen(),true);assert.equal(s.drawerSession,replacementSession);assert.equal(nodes.get('connectionName').value,'保留的新表单');assert.equal(nodes.get('connectionKey').value,'new-form-synthetic-key');assert.equal(nodes.get('connectionSubmit').disabled,false);});
  UI.closeCreate();
  const oldRead=defer(), newRead=defer(); let reads=0;
  apiHandler=async()=>++reads===1?oldRead.promise:newRead.promise;
  const oldRefresh=UI.refresh(), newRefresh=UI.refresh(true), eventsBeforeFreshRead=dispatched.length;
  newRead.resolve({connections:[third]}); await newRefresh;
  oldRead.resolve({connections:[item]}); await oldRefresh;
  check('superseded refreshes never replace new metadata or emit another change event',()=>{assert.equal(UI.list()[0].id,third.id);assert.equal(dispatched.length,eventsBeforeFreshRead+1);});
  s.items=[third,item]; s.selected=third.id; const eventsBeforeDelete=dispatched.length, notificationsBeforeDelete=notifications.length;
  apiHandler=async(path)=>{if(path.endsWith('/delete'))return{ok:true};throw new Error('合成列表刷新失败');};
  await UI.remove(third.id);
  check('confirmed deletion updates metadata even when the following refresh fails',()=>{assert.ok(!UI.list().some(connection=>connection.id===third.id));assert.equal(UI.list()[0].id,item.id);assert.equal(s.selected,item.id);assert.equal(dispatched.length,eventsBeforeDelete+1);});
  check('failed post-delete refresh reports the completed deletion accurately',()=>{const messages=notifications.slice(notificationsBeforeDelete);assert.ok(messages.includes('常用连接已删除'));assert.ok(messages.some(message=>message.startsWith('常用连接已删除，但列表刷新失败：')));assert.ok(!messages.includes('常用连接删除失败。'));});
  const stale=defer();apiHandler=async()=>stale.promise;const loading=UI.refresh();UI.reset();stale.resolve({connections:[item],storage_available:true});await assert.rejects(loading,/会话已失效/);checks++;
  check('logout reset clears public metadata and invalidates pending requests',()=>{assert.equal(UI.list().length,0);assert.equal(UI.isOpen(),false);assert.equal(s.promise,null);});
  check('connection update time uses fixed Singapore time instead of browser locale',()=>{assert.match(detailHtml({...item,updated_at:'2026-10-15T16:00:00.001Z'}),/2026-10-16 00:00:00.001 新加坡时间（UTC\+08:00）/);});
  console.log(JSON.stringify({status:'passed',checks}));
})().catch(error=>{console.error(error);process.exitCode=1;});
