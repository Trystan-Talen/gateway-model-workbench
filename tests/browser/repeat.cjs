/** Repeat every task kind with synthetic APIs; never sends an upstream request. */
'use strict';
const {chromium}=require('playwright');
const assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path');
const {execFileSync}=require('node:child_process');
const {projectRoot:root,output,env}=require('./artifacts.cjs').browserArtifacts('repeat');
const origin='http://127.0.0.1:18764',python=process.env.PYTHON||'python3';
execFileSync(python,[path.join(root,'tests/browser/make_fixture.py'),output],{cwd:root,env});
const base=JSON.parse(fs.readFileSync(path.join(output,'fixture.json'),'utf8'));
const fp=JSON.parse(execFileSync(python,['-c','import json; from fingerprint_protocol import catalog,make_protocol; print(json.dumps({"catalog":catalog(),"protocol":make_protocol({"preset":"quick","samples_per_cell":10,"concurrency":2})}))'],{cwd:root,env,encoding:'utf8'}));
const html=fs.readFileSync(path.join(root,'static/index.html'),'utf8'),clone=x=>JSON.parse(JSON.stringify(x));
const fields={task:'tasks',comparison:'comparisons',routing:'routing_comparisons',quality:'quality_tests',fingerprint:'fingerprint_runs'};
const resultKeys={task:'task_id',comparison:'comparison_id',routing:'routing_comparison_id',quality:'quality_test_id',fingerprint:'fingerprint_id'};
const ids={task:'single-fixture',comparison:'compare-fixture',routing:'routing-fixture',quality:'quality-fixture',fingerprint:'fingerprint-fixture'};
const errors=[],unexpected=[];
async function fits(page,width){
 assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth+1),false,`page fits ${width}`);
 const overflow=await page.locator('#repeatModal .modal-panel,#repeatBody,.report-shell,.report-head').evaluateAll(nodes=>nodes.filter(n=>n.getClientRects().length&&n.scrollWidth>n.clientWidth+1).map(n=>n.id||n.className));
 assert.deepEqual(overflow,[],`components fit ${width}`);
}
(async()=>{
 const chrome='/Applications/Google Chrome.app/Contents/MacOS/Google Chrome';
 const browser=await chromium.launch({headless:true,...(process.env.CHROME_PATH||fs.existsSync(chrome)?{executablePath:process.env.CHROME_PATH||chrome}:{})});
 try{
  for(const width of [1440,980,390]){
   const context=await browser.newContext({viewport:{width,height:1000},reducedMotion:'reduce'}),page=await context.newPage();
   const records={...clone(base),tasks:[clone(base.tasks[0])],routing_comparisons:[{id:ids.routing,name:'路由对照复测 · 合成验证',status:'error',tier:1000,rpm:100,config:{capture_raw:false},progress:{done:0,pct:0},sides:{baseline:{name:'固定路由',url:'https://example.test/fixed/v1',model:'model-fixture'},candidate:{name:'智能路由',url:'https://example.test/smart/v1',model:'model-fixture'}},report:null,error:'合成结束状态'}],fingerprint_runs:[{id:ids.fingerprint,name:'指纹复测 · 合成验证',status:'finished',mode:'candidate',created:new Date().toISOString(),endpoint:{url:'https://example.test/v1',model:'model-fixture'},protocol:fp.protocol,progress:{done:40,total:40,valid:40,errors:0},artifact:null,diagnostics:null,comparison:null}]};
   const source=clone(records),prepares=[],creates=[];let serial=0,failRouting=true,delayed=false,releaseCreate,forceTaskModal=false;
   page.on('pageerror',error=>errors.push({width,error:error.message}));
   page.on('console',message=>{if(message.type()==='error'&&/\[render\]|\[refresh\]/.test(message.text()))errors.push({width,error:message.text()})});
   await context.route('**/*',async route=>{
    if(await require('./assets.cjs').serveAsset(route,root))return;
    const req=route.request(),url=new URL(req.url()),endpoint=url.pathname,method=req.method();
    if(url.origin!==origin){unexpected.push(req.url());return route.abort()}
    if(endpoint==='/')return route.fulfill({contentType:'text/html',body:html});
    let data={};
    if(endpoint==='/auth/status')data={authenticated:true,user:{username:'合成复测',role:'user'}};
    else if(endpoint==='/tasks'&&method==='GET')data=records;
    else if(endpoint==='/config')data={config:{url:'https://unrelated.invalid/v1',rpm:100,concurrency:100}};
    else if(endpoint==='/connections')data={connections:[],storage_available:true};
    else if(endpoint==='/templates')data={templates:[]};
    else if(endpoint==='/routing-corpus')data={tiers:[{tier:1000,note:'合成题集'}]};
    else if(endpoint==='/routing-config')data={config:{}};
    else if(endpoint==='/fingerprint/catalog')data=fp.catalog;
    else if(endpoint==='/fingerprint/references')data={references:[]};
    else if(endpoint==='/fingerprints')data={runs:records.fingerprint_runs};
    else if(endpoint==='/repeats/prepare'){
     const body=req.postDataJSON();prepares.push(body);const kind=body.source_kind,record=records[fields[kind]].find(r=>r.id===body.source_id);
     const slots=kind==='routing'?['baseline','candidate']:kind==='quality'?['resource-0','resource-1']:kind==='fingerprint'?['endpoint']:['main'];
     data={...body,name:record.name,connections:slots.map((slot,index)=>({slot,label:kind==='routing'?(index?'智能路由':'固定路由'):`测试目标 ${index+1}`,url:kind==='routing'?record.sides[slot].url:'https://example.test/v1',model:'model-fixture',saved:!['routing','fingerprint'].includes(kind)&&!(kind==='task'&&forceTaskModal)})),preflight:{level:'info',requires_confirmation:['comparison','fingerprint'].includes(kind),planned_requests:40,findings:[],target:{host:'example.test'}}};
    }else if(endpoint==='/repeats'){
     const body=req.postDataJSON();creates.push(body);
     if(body.source_kind==='routing'&&failRouting){failRouting=false;return route.fulfill({status:503,json:{error:'合成创建失败，请重试'}})}
     if(delayed)await new Promise(resolve=>{releaseCreate=resolve});
     const kind=body.source_kind,id=`repeat-${kind}-${++serial}`,record=clone(records[fields[kind]].find(r=>r.id===body.source_id));record.id=id;
     records[fields[kind]].unshift(record);data={ok:true,...body,[resultKeys[kind]]:id};delete data.keys;
    }else if(/^\/(task|comparison|quality-test|routing-comparison|fingerprints)\/[^/]+$/.test(endpoint)){
     const [type,id]=endpoint.slice(1).split('/'),kind={'task':'task','comparison':'comparison','quality-test':'quality','routing-comparison':'routing','fingerprints':'fingerprint'}[type];data=records[fields[kind]].find(r=>r.id===id);
    }
    await route.fulfill({contentType:'application/json',body:JSON.stringify(data)});
   });
   await page.goto(origin+'/');await page.waitForFunction(()=>refreshTimer!==null&&!refreshing);await page.evaluate(()=>clearInterval(refreshTimer));
   async function select(kind){
    await page.evaluate(async({kind,id})=>{
     if(kind==='task'){setModule('gateway');setRecordFilter('single');selectTask(id)}
     if(kind==='comparison'){setModule('gateway');setRecordFilter('compare');await selectComparison(id)}
     if(kind==='routing'){setModule('routing');await selectRouting(id)}
     if(kind==='quality'){setModule('quality');setRecordFilter('compare');await selectQuality(id)}
     if(kind==='fingerprint'){setModule('fingerprint');await FingerprintUI.select(id)}
    },{kind,id:ids[kind]});
    await page.locator(`[data-click-action="repeatTest"][data-arg0="${kind}"][data-arg1="${ids[kind]}"]`).waitFor();
    const entry=page.locator(`[data-click-action="repeatTest"][data-arg0="${kind}"][data-arg1="${ids[kind]}"]`);
    assert.equal(await entry.count(),1,kind+': one repeat entry');
    assert.equal(await entry.evaluate(node=>Boolean(node.closest('.report-head'))),true,kind+': repeat entry is in header');
    await page.evaluate(()=>{document.querySelector('.main').scrollTop=0;window.scrollTo(0,0)});
    await page.waitForTimeout(150);
    const bounds=await entry.boundingBox();
    assert.ok(bounds&&bounds.y>=0&&bounds.y+bounds.height<=1000,kind+': repeat entry visible before scrolling');
    await fits(page,width);
   }
   const repeatButton=kind=>page.locator(`[data-click-action="repeatTest"][data-arg0="${kind}"][data-arg1="${ids[kind]}"]`);
   async function created(kind,count){await page.waitForFunction(({kind,count})=>({task:tasks,comparison:comparisons,routing:routingComparisons,quality:qualityTests,fingerprint:[]}[kind]||[]).filter(r=>r.id.startsWith('repeat-')).length>=count,{kind,count})}
   await select('task');
   for(const theme of ['light','dark']){await page.evaluate(theme=>setTheme(theme),theme);await page.waitForTimeout(150);await fits(page,width);await page.screenshot({path:path.join(output,`repeat-entry-${width}-${theme}.png`)})}
   await repeatButton('task').click();await page.evaluate(()=>RepeatUI.start('task','single-fixture'));await created('task',1);
   assert.equal(prepares.filter(x=>x.source_kind==='task').length,1);assert.equal(creates.filter(x=>x.source_kind==='task').length,1);assert.equal(creates[0].keys,undefined);
   assert.equal(records.tasks[0].config.concurrency,source.tasks[0].config.concurrency,'original pressure is retained');
   await select('comparison');await repeatButton('comparison').click();await page.locator('#repeatRiskAck').waitFor();
   await page.locator('#repeatBody [data-click-action="submitRepeat"]').click();assert.match(await page.locator('#repeatStatus').innerText(),/请先确认/);
   await page.locator('#repeatRiskAck').check();await page.locator('#repeatBody [data-click-action="submitRepeat"]').click();await created('comparison',1);
   assert.equal(creates.find(x=>x.source_kind==='comparison').risk_acknowledged,true);
   await select('quality');await repeatButton('quality').click();await created('quality',1);assert.equal(creates.find(x=>x.source_kind==='quality').keys,undefined);
   await select('routing');await repeatButton('routing').click();await page.locator('#repeatKey0').waitFor();
   assert.equal(await page.locator('#repeatBody input[type="password"]').count(),2);await page.locator('#repeatKey0').fill('synthetic-fixed-key');await page.locator('#repeatKey1').fill('synthetic-smart-key');
   await page.locator('#repeatBody [data-click-action="togglePasswordVisibility"]').first().click();assert.equal(await page.locator('#repeatKey0').getAttribute('type'),'text');
   for(const theme of ['light','dark']){await page.evaluate(theme=>setTheme(theme),theme);await page.waitForTimeout(200);await fits(page,width);await page.screenshot({path:path.join(output,`repeat-${width}-${theme}.png`)})}
   await page.locator('#repeatBody [data-click-action="submitRepeat"]').click();await page.waitForFunction(()=>document.getElementById('repeatStatus').textContent.includes('合成创建失败'));
   assert.equal(await page.locator('#repeatKey0').inputValue(),'synthetic-fixed-key','failure keeps input for retry');
   await page.locator('#repeatBody [data-click-action="submitRepeat"]').click();await created('routing',1);assert.equal(await page.locator('#repeatKey0').count(),0,'success clears keys');
   assert.deepEqual(creates.filter(x=>x.source_kind==='routing').at(-1).keys,{baseline:'synthetic-fixed-key',candidate:'synthetic-smart-key'});
   await select('fingerprint');await repeatButton('fingerprint').click();await page.locator('#repeatKey0').fill('synthetic-fingerprint-key');await page.locator('#repeatRiskAck').check();await page.locator('#repeatBody [data-click-action="submitRepeat"]').click();
   await page.waitForFunction(()=>!RepeatUI.isOpen()&&FingerprintUI.getSelectedId().startsWith('repeat-fingerprint-'));assert.equal(creates.find(x=>x.source_kind==='fingerprint').risk_acknowledged,true);
   // Closing a submitted form keeps the duplicate guard and leaves later navigation alone.
   forceTaskModal=true;delayed=true;await select('task');await repeatButton('task').click();await page.locator('#repeatKey0').fill('synthetic-background-key');await page.locator('#repeatBody [data-click-action="submitRepeat"]').click();
   for(let i=0;i<1000&&!releaseCreate;i++)await new Promise(resolve=>setTimeout(resolve,10));assert.ok(releaseCreate);
   await page.keyboard.press('Escape');assert.equal(await page.locator('#repeatKey0').count(),0);const prepareCount=prepares.length;
   await page.evaluate(()=>RepeatUI.start('task','single-fixture'));assert.equal(prepares.length,prepareCount,'submitted source stays locked');
   await select('quality');releaseCreate();delayed=false;await created('task',2);assert.equal(await page.evaluate(()=>activeModule),'quality','late completion preserves navigation');
   for(const [kind,list]of Object.entries(fields))assert.deepEqual(records[list].find(r=>r.id===ids[kind]),source[list].find(r=>r.id===ids[kind]),`${kind} source unchanged`);
   await context.close();
  }
  assert.deepEqual(errors,[]);assert.deepEqual(unexpected,[]);
  console.log(JSON.stringify({passed:true,kinds:Object.keys(fields),viewports:[1440,980,390],themes:['light','dark'],checks:['unique header entry visible before scroll','saved credentials','missing credentials and eye','risk confirmation','failure retry','duplicate guard','late completion','original records'],output}));
 }finally{await browser.close()}
})().catch(error=>{console.error(error);process.exitCode=1});
