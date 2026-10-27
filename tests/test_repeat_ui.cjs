'use strict';
/* Synthetic records, DOM and API: no live server, credential store or upstream requests. */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const root = path.resolve(__dirname,'..');
const script = fs.readFileSync(path.join(root,'static/repeat.js'),'utf8').replace(/\}\)\(\);\s*$/,'window.__repeatTest = {s,formHtml};\n})();');
let checks = 0;
const check = (name,fn) => { fn(); checks++; };
const defer = () => { let resolve,reject; const promise=new Promise((yes,no)=>{resolve=yes;reject=no;}); return {promise,resolve,reject}; };
const flush = async () => { for(let i=0;i<5;i++)await Promise.resolve(); };
const decode = value => String(value || '').replace(/&quot;/g,'"').replace(/&#39;/g,"'").replace(/&lt;/g,'<').replace(/&gt;/g,'>').replace(/&amp;/g,'&');

function harness() {
  const nodes = new Map(),calls=[],toasts=[],created=[];
  let handler=async()=>({}),context='source-context';
  class Node {
    constructor(id) { this.id=id;this.value='';this.type='text';this.checked=false;this.hidden=false;this.disabled=false;this.dataset={};this.children=[];this.isConnected=true;this._html='';this.textContent=''; }
    set innerHTML(value) {
      this._html=value;
      for(const child of this.children)if(child.id)nodes.delete(child.id);
      this.children=[];
      for(const match of value.matchAll(/<(?:input|div|button)\b[^>]*>/g)) {
        const tag=match[0],id=tag.match(/\bid="([^"]*)"/)?.[1];
        if(!id&&!tag.startsWith('<button'))continue;
        const node=new Node(id || `button-${this.children.length}`);
        node.type=tag.match(/\btype="([^"]*)"/)?.[1]||'text';node.value=decode(tag.match(/\bvalue="([^"]*)"/)?.[1]);node.disabled=/ disabled/.test(tag);
        for(const attr of tag.matchAll(/data-([\w-]+)="([^"]*)"/g))node.dataset[attr[1].replace(/-([a-z])/g,(_,c)=>c.toUpperCase())]=decode(attr[2]);
        if(tag.startsWith('<button'))node.label={textContent:'button'};
        this.children.push(node);if(id)nodes.set(id,node);
      }
    }
    get innerHTML() { return this._html; }
    querySelector(selector) { return selector==='span'?this.label:null; }
    querySelectorAll(selector) { return this.children.filter(node=>selector==='input'?node.id.startsWith('repeatKey')||node.id==='repeatRiskAck':selector.includes('submitRepeat')?node.dataset.clickAction==='submitRepeat':selector.includes('closeRepeat')?node.dataset.clickAction==='closeRepeat':false); }
    focus() { document.activeElement=this; }
  }
  for(const id of ['repeatModal','repeatBody','repeatTitle','repeatFooter'])nodes.set(id,new Node(id));
  nodes.get('repeatModal').hidden=true;
  const document={activeElement:null,getElementById:id=>nodes.get(id)||null,querySelectorAll:()=>[],body:{insertAdjacentHTML(){}}};
  const sandbox={window:{},document,authenticated:true,console,
    api:async(url,options)=>{const body=options?.body?JSON.parse(options.body):undefined;calls.push({url,body});return handler(url,body);},
    toast:value=>toasts.push(value),repeatContext:()=>context,
    repeatCreated:async(kind,sourceId,id,oldContext)=>{created.push({kind,sourceId,id,context:oldContext,select:context===oldContext});},
    openModal:id=>{nodes.get(id).hidden=false;},closeModal:id=>{nodes.get(id).hidden=true;sandbox.window.RepeatUI.onClosed();},
    preflightHtml:data=>`<p>合成预检 ${data.requires_confirmation?'需要确认':'无风险'}</p>`
  };
  vm.runInNewContext(fs.readFileSync(path.join(root,'static/ui-kit.js'),'utf8'),sandbox);
  vm.runInNewContext(script,sandbox);sandbox.window.__repeatTest.s.initialized=true;
  return {UI:sandbox.window.RepeatUI,s:sandbox.window.__repeatTest.s,nodes,calls,toasts,created,sandbox,
    setHandler(fn){handler=fn;},setContext(value){context=value;}};
}
const prepare = (kind='task',id='source-1',extras={}) => ({source_kind:kind,source_id:id,name:'原测试',connections:[{slot:'main',label:'网关目标',url:'https://synthetic.invalid/v1',model:'synthetic-model',saved:true}],preflight:{level:'ok',requires_confirmation:false},...extras});
const result = id => ({ok:true,task_id:id});

(async()=>{
  const a=harness();
  check('finished records use shared repeat action, active records have none',()=>{
    for(const status of ['finished','finished_with_errors','stopped','error','expired'])assert.match(a.UI.button('task',{id:'source-1',status}),/data-click-action="repeatTest"/);
    for(const status of ['draft','waiting','running','stopping'])assert.equal(a.UI.button('task',{id:'source-1',status}),'');
    assert.match(a.UI.button('routing',{id:'<script>',status:'finished'}),/&lt;script&gt;/);
  });
  a.setHandler(async url=>url==='/repeats/prepare'?prepare():result('new-1'));
  await a.UI.start('task','source-1');
  check('saved credentials create immediately using only frozen source identity',()=>{
    assert.deepEqual(a.calls.map(call=>call.url),['/repeats/prepare','/repeats']);
    assert.deepEqual(a.calls[1].body,{source_kind:'task',source_id:'source-1'});
    assert.equal(a.created[0].id,'new-1');assert.equal(a.created[0].select,true);assert.equal(a.UI.isOpen(),false);assert.equal(a.s.operations.size,0);
  });
  const b=harness(),pending=defer();b.setHandler(async url=>url==='/repeats/prepare'?pending.promise:result('new-2'));
  const starting=b.UI.start('task','source-1');
  await b.UI.start('task','source-1');await b.UI.start('routing','different-source');
  check('same-source double click and concurrent different-source preparation are blocked',()=>{assert.equal(b.calls.length,1);assert.match(b.UI.button('task',{id:'source-1',status:'finished'}),/disabled/);assert.equal(b.s.operations.size,1);});
  b.setContext('another-module');pending.resolve(prepare());await starting;
  check('background completion carries original context and cannot select another module',()=>{assert.equal(b.created[0].context,'source-context');assert.equal(b.created[0].select,false);assert.equal(b.created[0].id,'new-2');});
  const c=harness();
  const missing=[{slot:'resource-0',label:'待测资源',url:'https://synthetic.invalid/a',model:'first-model',saved:false,issue:'常用连接已删除'},
    {slot:'resource-1',label:'参考资源',url:'https://synthetic.invalid/b',model:'<unsafe-model>',saved:false}];
  c.setHandler(async url=>url==='/repeats/prepare'?prepare('quality','source-q',{name:'<unsafe-name>',connections:missing,preflight:{level:'risk',requires_confirmation:true}}):({ok:true,quality_test_id:'new-q'}));
  await c.UI.start('quality','source-q');
  check('missing credentials show masked fields, source targets, models and current risk acknowledgement',()=>{
    assert.equal(c.nodes.get('repeatKey0').type,'password');assert.equal(c.nodes.get('repeatKey1').type,'password');assert.equal(c.nodes.get('repeatRiskAck').checked,false);
    assert.match(c.nodes.get('repeatBody').innerHTML,/&lt;unsafe-name&gt;/);assert.match(c.nodes.get('repeatBody').innerHTML,/&lt;unsafe-model&gt;/);assert.match(c.nodes.get('repeatBody').innerHTML,/常用连接已删除/);assert.equal(c.UI.isOpen(),true);assert.match(c.nodes.get('repeatFooter').innerHTML,/创建并开始/);assert.doesNotMatch(c.nodes.get('repeatBody').innerHTML,/<table/);
  });
  await c.UI.submit();
  c.nodes.get('repeatKey0').value='synthetic-first';await c.UI.submit();
  c.nodes.get('repeatKey1').value='synthetic-reference';await c.UI.submit();
  check('each missing key and fresh risk confirmation are required before creating',()=>{assert.equal(c.calls.length,1);assert.match(c.nodes.get('repeatStatus').textContent,/请先确认/);});
  c.nodes.get('repeatRiskAck').checked=true;await c.UI.submit();
  check('slot-specific replacement keys and one-run acknowledgement are submitted, success clears the form',()=>{
    assert.deepEqual(c.calls[1].body,{source_kind:'quality',source_id:'source-q',keys:{'resource-0':'synthetic-first','resource-1':'synthetic-reference'},risk_acknowledged:true});
    assert.equal(c.created[0].id,'new-q');assert.equal(c.nodes.get('repeatKey0'),undefined);assert.equal(c.UI.isOpen(),false);assert.equal(c.s.operations.size,0);
  });
  const optional=harness();optional.setHandler(async url=>url==='/repeats/prepare'?prepare('fingerprint','source-fp',{connections:[{slot:'endpoint',label:'指纹目标',saved:false,optional:true}],preflight:{requires_confirmation:true}}):({ok:true,fingerprint_id:'new-fp'}));
  await optional.UI.start('fingerprint','source-fp');
  check('explicitly unauthenticated fingerprint repeats need no key but still require a fresh cost acknowledgement',()=>{assert.equal(optional.nodes.get('repeatKey0'),undefined);assert.equal(optional.nodes.get('repeatRiskAck').checked,false);assert.match(optional.nodes.get('repeatBody').innerHTML,/原记录无需密钥/);assert.equal(optional.calls.length,1);});
  optional.nodes.get('repeatRiskAck').checked=true;await optional.UI.submit();
  check('unauthenticated fingerprint repetition submits no replacement or unrelated credentials',()=>{assert.deepEqual(optional.calls[1].body,{source_kind:'fingerprint',source_id:'source-fp',risk_acknowledged:true});assert.equal(optional.created[0].id,'new-fp');});
  const d=harness();let attempt=0;
  d.setHandler(async url=>{if(url==='/repeats/prepare')return prepare('task','source-1',{connections:[{slot:'main',label:'网关',saved:false}]});if(++attempt===1)throw new Error('合成创建失败');return result('retry-success');});
  await d.UI.start('task','source-1');d.nodes.get('repeatKey0').value='synthetic-retry';await d.UI.submit();
  check('failed creation preserves entered keys and reports failure without selecting a new record',()=>{assert.equal(d.nodes.get('repeatKey0').value,'synthetic-retry');assert.equal(d.UI.isOpen(),true);assert.match(d.nodes.get('repeatStatus').textContent,/合成创建失败/);assert.equal(d.created.length,0);assert.equal(d.s.operations.size,1);assert.equal(d.nodes.get('repeatFooter').querySelectorAll('[data-click-action="closeRepeat"]')[0].label.textContent,'取消');assert.equal(d.nodes.get('repeatFooter').querySelectorAll('[data-click-action="submitRepeat"]')[0].label.textContent,'创建并开始');});
  await d.UI.submit();
  check('retry after failure uses the retained form and releases the lock on success',()=>{assert.equal(d.calls.filter(call=>call.url==='/repeats').length,2);assert.equal(d.calls.at(-1).body.keys.main,'synthetic-retry');assert.equal(d.created[0].id,'retry-success');assert.equal(d.s.operations.size,0);});
  const e=harness(),posting=defer();
  e.setHandler(async url=>url==='/repeats/prepare'?prepare('task','source-1',{connections:[{slot:'main',label:'网关',saved:false}]}):posting.promise);
  await e.UI.start('task','source-1');e.nodes.get('repeatKey0').value='synthetic-inflight';const submitting=e.UI.submit();await flush();
  await e.UI.submit();e.UI.close();await e.UI.start('task','source-1');
  check('closing an in-flight create clears credentials but holds the same-source lock until settled',()=>{assert.equal(e.calls.filter(call=>call.url==='/repeats').length,1);assert.equal(e.calls.filter(call=>call.url==='/repeats/prepare').length,1);assert.equal(e.nodes.get('repeatKey0'),undefined);assert.equal(e.UI.isOpen(),false);assert.equal(e.s.operations.size,1);});
  posting.resolve(result('late-created'));await submitting;
  check('late closed-form success refreshes records without overwriting selection or recreating inputs',()=>{assert.equal(e.created.length,1);assert.equal(e.created[0].select,false);assert.equal(e.created[0].context,null);assert.equal(e.s.operations.size,0);assert.equal(e.nodes.get('repeatKey0'),undefined);});
  const f=harness();f.setHandler(async()=>prepare('task','source-1',{connections:[{slot:'main',label:'网关',saved:false}]}));
  await f.UI.start('task','source-1');f.nodes.get('repeatKey0').value='synthetic-cancelled';f.UI.close();
  await f.UI.start('task','source-1');
  check('cancelled confirmation clears secrets and permits a fresh source preparation',()=>{assert.equal(f.nodes.get('repeatKey0').value,'');assert.equal(f.calls.length,2);assert.equal(f.created.length,0);});
  const g=harness(),authPending=defer();g.setHandler(async()=>authPending.promise);const oldStart=g.UI.start('task','source-1');g.sandbox.authenticated=false;g.UI.reset();authPending.resolve(prepare());await oldStart;
  check('logout invalidates late prepare responses and protected form state',()=>{assert.equal(g.UI.isOpen(),false);assert.equal(g.s.operations.size,0);assert.equal(g.created.length,0);assert.equal(g.nodes.get('repeatBody').innerHTML,'');});
  const h=harness();h.setHandler(async()=>prepare('task','other-source'));await h.UI.start('task','source-1');
  check('a mismatched source response is rejected before creating',()=>{assert.equal(h.calls.length,1);assert.match(h.toasts[0],/来源记录不一致/);assert.equal(h.s.operations.size,0);});
  const i=harness(),oldFailure=defer();let preparing=0;
  i.setHandler(async url=>url==='/repeats/prepare'?prepare('task',++preparing===1?'source-1':'source-2',{connections:[{slot:'main',label:'网关',saved:false}]}):oldFailure.promise);
  await i.UI.start('task','source-1');i.nodes.get('repeatKey0').value='synthetic-old';const failing=i.UI.submit();await flush();i.UI.close();await i.UI.start('task','source-2');i.nodes.get('repeatKey0').value='synthetic-new';oldFailure.reject(new Error('旧窗口迟到失败'));await failing;
  check('a dismissed in-flight failure never writes into a later confirmation window',()=>{assert.equal(i.nodes.get('repeatKey0').value,'synthetic-new');assert.equal(i.nodes.get('repeatStatus').textContent,'');assert.equal(i.s.form.operation.id,'source-2');assert.equal(i.s.operations.size,1);});

  // Execute the actual application completion callback to verify module-selection behavior.
  const app=fs.readFileSync(path.join(root,'static/app.js'),'utf8');
  const callbacks=app.slice(app.indexOf('function repeatContext(){'),app.indexOf('Object.assign(window,{renderQualityDetail'));
  const selectionCalls=[],refreshCalls=[];
  const appSandbox={activeModule:'gateway',activeView:'single',selected:'source-1',selectedComparison:null,selectedQuality:null,selectedRouting:null,detailTask:{id:'source-1'},detailState:{id:'source-1'},lastRefreshSignature:'old',window:{FingerprintUI:{getSelectedId:()=>null,select:async id=>selectionCalls.push(['fingerprint',id])}},qualityTests:[{id:'source-q',resources:[{},{}]}],localStorage:{setItem:(key,id)=>selectionCalls.push([key,id])},selectDefaultModuleAfterCreate:(kind,id)=>selectionCalls.push([kind,id]),refresh:async()=>refreshCalls.push(true)};
  vm.runInNewContext(callbacks,appSandbox);
  const sourceContext=appSandbox.repeatContext();appSandbox.activeModule='routing';await appSandbox.repeatCreated('task','source-1','new-task',sourceContext);
  check('actual completion callback refreshes in background without switching away from another module',()=>{assert.equal(selectionCalls.length,0);assert.equal(refreshCalls.length,1);assert.equal(appSandbox.activeModule,'routing');});
  appSandbox.activeModule='quality';appSandbox.activeView='compare';appSandbox.selected=null;appSandbox.selectedQuality='source-q';
  await appSandbox.repeatCreated('quality','source-q','new-quality',appSandbox.repeatContext());
  check('actual completion callback opens a repeated dual-resource run in the comparison view',()=>{assert.deepEqual(selectionCalls.at(-1),['quality-compare','new-quality']);assert.equal(appSandbox.detailTask,null);assert.equal(appSandbox.selectedRouting,null);});
  console.log(`repeat UI checks passed: ${checks}`);
})().catch(error=>{console.error(error);process.exitCode=1;});
