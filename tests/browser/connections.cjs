/** Named connections use synthetic APIs and credentials only. */
'use strict';
const {chromium}=require('playwright');
const assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path');
const {execFileSync}=require('node:child_process');
const {projectRoot:root,output,env}=require('./artifacts.cjs').browserArtifacts('connections');
const origin='http://127.0.0.1:18763';
const html=fs.readFileSync(path.join(root,'static/index.html'),'utf8');
const catalog=JSON.parse(execFileSync(process.env.PYTHON||'python3',['-c','import json; from fingerprint_protocol import catalog; print(json.dumps(catalog()))'],{cwd:root,encoding:'utf8',env}));
const errors=[],unexpected=[];
async function fits(page,width){
 assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth+1),false,`page fits ${width}`);
 const overflow=await page.locator('#connectionDrawer,.report-shell,.report-head,.report-section,#config,#fingerprintDrawer').evaluateAll(nodes=>nodes.filter(n=>n.getClientRects().length&&n.scrollWidth>n.clientWidth+1).map(n=>n.id||n.className));
 assert.deepEqual(overflow,[],`components fit ${width}`);
}
async function closeTest(page){await page.locator('#config [data-click-action="toggleConfig"]').click();await page.waitForFunction(()=>!document.getElementById('config').classList.contains('mobile-open'))}
(async()=>{
 const chrome='/Applications/Google Chrome.app/Contents/MacOS/Google Chrome';
 const browser=await chromium.launch({headless:true,...(process.env.CHROME_PATH||fs.existsSync(chrome)?{executablePath:process.env.CHROME_PATH||chrome}:{})});
 try{
  for(const width of [1440,980,390]){
   const context=await browser.newContext({viewport:{width,height:1000},reducedMotion:'reduce'}),page=await context.newPage();
   let profiles=[],serial=0,keys={},savedConfig={},routingConfig={},failSave=false,delayReveal=false,releaseReveal;
   const reveals=[],saves=[],discoveries=[],submissions=[];
   page.on('pageerror',error=>errors.push({width,error:error.message}));
   page.on('dialog',dialog=>dialog.accept());
   await context.route('**/*',async route=>{
    if(await require('./assets.cjs').serveAsset(route,root))return;
    const req=route.request(),url=new URL(req.url()),endpoint=url.pathname,method=req.method();
    if(url.origin!==origin){unexpected.push(req.url());return route.abort()}
    if(endpoint==='/')return route.fulfill({contentType:'text/html',body:html});
    let data={};
    if(endpoint==='/auth/status')data={authenticated:true,user:{username:'合成测试',role:'user'}};
    else if(endpoint==='/tasks'&&method==='GET')data={tasks:[],comparisons:[],quality_tests:[],routing_comparisons:[],fingerprint_runs:[]};
    else if(endpoint==='/connections'&&method==='GET')data={connections:profiles.map(p=>({...p,api_key_saved:Boolean(keys[p.id])})),storage_available:true};
    else if(endpoint==='/connections'&&method==='POST'){
     const body=req.postDataJSON();saves.push(body);
     if(failSave)return route.fulfill({status:503,json:{error:'合成测试：本机凭据库保存失败'}});
     const id=body.id||(++serial).toString(16).padStart(24,'0'),old=profiles.find(p=>p.id===id);
     if(body.api_key)keys[id]=body.api_key;
     if(!keys[id])return route.fulfill({status:400,json:{error:'请填写接口密钥'}});
     const record={id,name:body.name,url:body.url,updated_at:serial+Date.now()/1000};
     profiles=[record,...profiles.filter(p=>p.id!==id)];data={ok:true,connection:{...record,api_key_saved:true}};
     assert.ok(!old||old.id===id);
    }else if(/^\/connections\/[^/]+\/reveal$/.test(endpoint)){
     const id=endpoint.split('/')[2];reveals.push(id);if(delayReveal)await new Promise(resolve=>{releaseReveal=resolve});
     data={api_key:keys[id]};
    }else if(/^\/connections\/[^/]+\/delete$/.test(endpoint)){
     const id=endpoint.split('/')[2];profiles=profiles.filter(p=>p.id!==id);delete keys[id];data={ok:true};
    }else if(endpoint==='/config'){
     if(method==='POST'){savedConfig=req.postDataJSON();data={ok:true}}else data={config:savedConfig};
    }else if(endpoint==='/routing-config'){
     if(method==='POST'){const body=req.postDataJSON();routingConfig[body.side]={...body,api_key_saved:Boolean(body.connection_id&&keys[body.connection_id])};delete routingConfig[body.side].api_key;data={ok:true,saved:true,storage_available:true}}else data={config:routingConfig,storage_available:true};
    }else if(endpoint==='/templates')data={templates:[]};
    else if(endpoint==='/routing-corpus')data={tiers:[{tier:1000,note:'合成题集'}]};
    else if(endpoint==='/quality-suite')data={manifest:{title:'合成题集',case_count:1,categories:['basic'],input_chars:{min:4,max:4}},cases:[]};
    else if(endpoint==='/quality-reference-records')data={records:[]};
    else if(endpoint==='/credential-status')data={saved:false,storage_available:true};
    else if(['/models','/routing-models','/fingerprint/models'].includes(endpoint)){
     const body=req.postDataJSON();discoveries.push({endpoint,body});
     assert.equal(body.api_key,'','selected connections keep plaintext out of model requests');assert.ok(keys[body.connection_id]);
     data={ok:true,models:['shared-model'],saved:true,storage_available:true};
    }else if(endpoint==='/preflight')data={level:'ok',requires_confirmation:false,planned_requests:10,findings:[],target:{host:'127.0.0.1'}};
    else if(endpoint==='/tasks'&&method==='POST'){const body=req.postDataJSON();submissions.push({endpoint,body});savedConfig={...body,api_key_saved:true};data={task_id:'synthetic-task'}}
    else if(endpoint==='/routing-comparisons'&&method==='POST'){submissions.push({endpoint,body:req.postDataJSON()});data={routing_comparison_id:'synthetic-routing'}}
    else if(endpoint==='/fingerprint/catalog')data=catalog;
    else if(endpoint==='/fingerprint/references')data={references:[]};
    else if(endpoint==='/fingerprints'&&method==='POST'){submissions.push({endpoint,body:req.postDataJSON()});return route.fulfill({status:400,json:{error:'合成验证完成；不创建实际采集'}})};
    await route.fulfill({contentType:'application/json',body:JSON.stringify(data)});
   });
   await page.goto(origin+'/');await page.waitForFunction(()=>refreshTimer!==null&&!refreshing);
   await page.locator('[data-module="connections"]').click();
   await page.getByRole('heading',{name:'还没有常用连接',exact:true}).waitFor();
   async function saveNew(name,key){
    await page.locator('#recordCreateButton').click();await page.locator('#connectionName').fill(name);await page.locator('#connectionUrl').fill('http://127.0.0.1:9100/v1');await page.locator('#connectionKey').fill(key);
    await page.locator('#connectionSubmit').click();await page.waitForFunction(name=>!ConnectionUI.isOpen()&&ConnectionUI.list().some(item=>item.name===name),name);
   }
   await saveNew('常用固定路由','synthetic-fixed-secret');const fixed=profiles[0].id;
   await saveNew('常用智能路由','synthetic-smart-secret');const smart=profiles[0].id;
   assert.notEqual(fixed,smart);assert.equal(reveals.length,0,'no automatic key reveal');
   for(const theme of ['light','dark']){
    await page.evaluate(theme=>setTheme(theme),theme);assert.equal(await page.locator('html').getAttribute('data-theme'),theme);await page.waitForTimeout(300);await fits(page,width);
    await page.screenshot({path:path.join(output,`connections-${width}-${theme}.png`)});
   }
   await page.getByRole('button',{name:'编辑常用连接',exact:true}).click();
   assert.equal(await page.locator('#connectionKey').inputValue(),'');
   await page.locator('#connectionDrawer [data-click-action="revealConnectionKey"]').click();
   await page.waitForFunction(()=>document.getElementById('connectionKey').type==='text');
   assert.equal(await page.locator('#connectionKey').inputValue(),'synthetic-smart-secret');
   await page.locator('#connectionName').fill('常用智能路由已改名');await page.locator('#connectionSubmit').click();
   await page.waitForFunction(()=>!ConnectionUI.isOpen());assert.equal(Object.hasOwn(saves.at(-1),'api_key'),false,'revealed key is not resubmitted');
   await page.reload();await page.waitForFunction(()=>refreshTimer!==null&&!refreshing);
   await page.locator('[data-module="routing"]').click();await page.locator('#recordCreateButton').click();
   await page.locator('#saved-connection-routing-baseline').selectOption(fixed);
   await page.locator('#routingSideTabs [data-routing-tab="candidate"]').click();
   await page.locator('#saved-connection-routing-candidate').selectOption(smart);
   assert.equal(await page.locator('#routing-candidate-key').inputValue(),'');
   await page.locator('#loadRoutingModels').click();await page.waitForFunction(()=>!document.getElementById('loadRoutingModels').disabled);
   const routeDiscoveries=discoveries.filter(d=>d.endpoint==='/routing-models');
   assert.deepEqual(routeDiscoveries.slice(-2).map(d=>d.body.connection_id).sort(),[fixed,smart].sort());
   await page.locator('#routingModel').selectOption('shared-model');
   const chosen=await page.evaluate(()=>readRouting());assert.equal(chosen.baseline.connection_id,fixed);assert.equal(chosen.candidate.connection_id,smart);
   const draft=await page.locator('#routing-candidate-url').inputValue();
   await page.locator('#routing-candidate-panel [data-click-action="manageSavedConnections"]').click();
   await page.locator('#connectionCreateTitle').filter({hasText:'管理常用连接'}).waitFor();
   assert.equal(await page.locator('#config').evaluate(el=>el.inert),true);
   await page.keyboard.press('Escape');assert.equal(await page.locator('#config').evaluate(el=>el.inert),false);
   await page.locator('#routing-candidate-panel [data-click-action="saveCurrentConnection"]').click();
   await page.locator('#connectionName').waitFor();assert.equal(await page.locator('#config').evaluate(el=>el.inert),true);
   await page.keyboard.press('Escape');assert.equal(await page.locator('#config').evaluate(el=>el.inert),false);assert.equal(await page.locator('#routing-candidate-url').inputValue(),draft);
   await page.locator('#routing-candidate-panel [data-click-action="saveCurrentConnection"]').click();
   await page.locator('#connectionKey').fill('synthetic-smart-rotated');await page.locator('#connectionSubmit').click();await page.waitForFunction(()=>!ConnectionUI.isOpen());
   await page.waitForFunction(()=>document.getElementById('routingModel').value==='');
   assert.equal((await page.evaluate(()=>readRouting())).candidate.connection_id,smart,'key update retains named binding');
   assert.equal(keys[fixed],'synthetic-fixed-secret','same address update preserves other profile key');
   await page.locator('#routing-candidate-key').fill('synthetic-manual-key');assert.equal((await page.evaluate(()=>readRouting())).candidate.connection_id,undefined,'manual key exits binding');
   await page.locator('#saved-connection-routing-candidate').selectOption(smart);
   await closeTest(page);
   await page.locator('[data-module="gateway"]').click();await page.locator('#recordCreateButton').click();
   await page.locator('#saved-connection-gateway-main').selectOption(fixed);
   await page.locator('#loadModels').click();await page.waitForFunction(()=>!document.getElementById('loadModels').disabled);
   await page.locator('#modelSelect').selectOption('shared-model');await page.locator('#submit').click();
   await page.waitForFunction(()=>!document.getElementById('config').classList.contains('mobile-open'));
   assert.equal(submissions.find(x=>x.endpoint==='/tasks').body.connection_id,fixed);
   await page.locator('[data-module="quality"]').click();await page.locator('#recordCreateButton').click();
   await page.locator('#saved-connection-quality-0').selectOption(smart);
   await page.locator('[data-quality-index="0"] .quality-model-refresh').click();
   await page.waitForFunction(()=>!document.querySelector('[data-quality-index="0"] .quality-model').disabled);
   assert.equal((await page.evaluate(()=>readQuality())).resources[0].connection_id,smart);
   await closeTest(page);
   await page.locator('[data-module="fingerprint"]').click();await page.locator('#recordCreateButton').click();
   await page.locator('#fpSavedConnection').waitFor();await page.locator('#fpSavedConnection').selectOption(fixed);
   await page.locator('#fpName').fill('合成指纹');await page.locator('#fpFetchModels').click();
   await page.waitForFunction(()=>!document.getElementById('fpFetchModels').disabled);await page.locator('#fpModelSelect').selectOption('shared-model');
   await page.locator('#fpRiskAck').check();await page.locator('#fpSubmit').click();
   await page.waitForFunction(()=>document.getElementById('fpFormError').textContent.includes('合成验证完成'));
   assert.equal(submissions.find(x=>x.endpoint==='/fingerprints').body.endpoint.connection_id,fixed);
   await page.locator('[data-click-action="saveFingerprintConnection"]').click();await page.locator('#connectionName').waitFor();
   await page.keyboard.press('Escape');assert.equal(await page.locator('#fingerprintDrawer').evaluate(el=>el.inert),false,'fingerprint drawer remains open after closing connection overlay');
   await page.locator('#fingerprintDrawer [data-fp-action="close-create"]').click();
   await page.locator('[data-module="connections"]').click();
   await page.getByRole('button',{name:'编辑常用连接',exact:true}).click();delayReveal=true;
   await page.locator('#connectionDrawer [data-click-action="revealConnectionKey"]').click();await page.waitForFunction(()=>document.getElementById('connectionDrawer').querySelector('[aria-busy="true"]')!==null);
   await page.locator('#connectionDrawer [data-click-action="closeConnectionCreate"]').click();
   for(let i=0;i<1000&&!releaseReveal;i++)await new Promise(resolve=>setTimeout(resolve,10));assert.ok(releaseReveal);releaseReveal();delayReveal=false;
   await page.waitForFunction(()=>!ConnectionUI.isOpen());assert.equal(await page.locator('#connectionKey').count(),0,'late plaintext never repopulates closed drawer');
   await page.locator('#recordCreateButton').click();await page.locator('#connectionName').fill('保存失败测试');await page.locator('#connectionUrl').fill('http://127.0.0.1:9200/v1');await page.locator('#connectionKey').fill('synthetic-rejected-key');failSave=true;
   await page.locator('#connectionSubmit').click();await page.waitForFunction(()=>document.getElementById('connectionFormStatus').textContent.includes('保存失败'));
   assert.equal(await page.evaluate(()=>ConnectionUI.isOpen()),true);failSave=false;
   for(const theme of ['dark','light']){await page.evaluate(theme=>setTheme(theme),theme);await page.waitForTimeout(300);await fits(page,width);await page.screenshot({path:path.join(output,`connections-form-${width}-${theme}.png`)})}
   await page.locator('#connectionDrawer [data-click-action="closeConnectionCreate"]').click();
   await page.getByRole('button',{name:'删除常用连接',exact:true}).click();await page.waitForFunction(()=>ConnectionUI.list().length===1);
   assert.equal(profiles.length,1);await context.close();
  }
  assert.deepEqual(errors,[]);assert.deepEqual(unexpected,[]);
  console.log(JSON.stringify({passed:true,viewports:[1440,980,390],themes:['light','dark'],checks:['named CRUD','same URL independent keys','reload','gateway routing quality fingerprint selectors','manual input','model discovery binding','key rotation invalidates models','no implicit reveal','transient reveal','stacked drawer focus','storage error','responsive layout'],output}));
 }finally{await browser.close()}
})().catch(error=>{console.error(error);process.exitCode=1});
