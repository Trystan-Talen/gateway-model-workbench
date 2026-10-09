/** Real-button creation regression with synthetic API responses only.
 * NODE_PATH=<directory containing playwright> node tests/browser/routing_create.cjs
 * No server, user credentials, saved accounts, or upstream requests are used.
 */
const {chromium}=require('playwright');
const assert=require('node:assert/strict'),fs=require('fs'),path=require('path');
const {execFileSync}=require('child_process');
const {projectRoot:root,output,env}=require('./artifacts.cjs').browserArtifacts('routing_create');
const html=process.env.UI_HTML?fs.readFileSync(process.env.UI_HTML,'utf8'):execFileSync(
 process.env.PYTHON||'python3',['-c','import route_test_ui as ui; print(ui.INDEX_HTML)'],
 {cwd:root,encoding:'utf8',env});
const corpus={tiers:[{tier:1000,bursts:10,note:'合成测试集。'},{tier:3000,bursts:30,note:'合成测试集。'}],metrics:[]};
const suite={manifest:{title:'合成题集',case_count:1,categories:['basic'],input_chars:{min:4,max:4}},cases:[]};
const emptyRecords={tasks:[],comparisons:[],quality_tests:[],routing_comparisons:[]};
const results=[],errors=[],unexpected=[],submissions=[],discoveries=[];
let discoveryMode='normal',releaseDelayed;
async function uniqueIds(page,label){
 const duplicates=await page.locator('[id]').evaluateAll(nodes=>{
  const counts={};for(const node of nodes)counts[node.id]=(counts[node.id]||0)+1;
  return Object.entries(counts).filter(([,count])=>count>1);
 });
 assert.deepEqual(duplicates,[],label+': duplicate DOM ids');
}
async function drawerGeometry(page,width,height,label){
 await page.waitForFunction(()=>Math.abs(document.getElementById('config').getBoundingClientRect().right-innerWidth)<1);
 const box=await page.locator('#config').boundingBox();
 assert.ok(Math.abs(box.x+box.width-width)<1,label+': drawer right edge');
 assert.ok(Math.abs(box.y)<1&&Math.abs(box.height-height)<1,label+': full viewport height');
 assert.ok(box.x>=0&&box.width<=width,label+': drawer fits viewport');
 const overflow=await page.locator('#config,#comparisonFields,#routingFields,.routing-resource,.quality-resource,.group-row').evaluateAll(nodes=>nodes
  .filter(node=>node.getBoundingClientRect().width>0&&node.scrollWidth>node.clientWidth+1)
  .map(node=>({id:node.id,class:node.className,client:node.clientWidth,scroll:node.scrollWidth})));
 assert.deepEqual(overflow,[],label+': horizontal overflow');
 assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth),false,label+': page overflow');
 await uniqueIds(page,label);
}
async function closeDrawer(page){
 await page.getByRole('button',{name:'关闭新建测试',exact:true}).click();
 await page.waitForFunction(()=>!document.getElementById('config').classList.contains('mobile-open'));
}
async function openFromButton(page,module){
 await page.locator(`[data-module="${module}"]`).click();
 await page.locator('#recordCreateButton').click();
 await page.locator('#config.mobile-open').waitFor({timeout:3000});
}
async function side(page,key){
 await page.locator(`#routingSideTabs [data-routing-tab="${key}"]`).click();
 const panel=page.locator(`[data-routing-side="${key}"]`);
 await panel.waitFor({state:'visible'});
 return panel;
}
(async()=>{
 const options={headless:true};const localChrome='/Applications/Google Chrome.app/Contents/MacOS/Google Chrome';
 if(process.env.CHROME_PATH||fs.existsSync(localChrome))options.executablePath=process.env.CHROME_PATH||localChrome;
 const browser=await chromium.launch(options);
 try{
  for(const width of [390,768,980,1440]){
   const height=width===390?844:1000;
   const context=await browser.newContext({viewport:{width,height},reducedMotion:'reduce'});
   const page=await context.newPage();
   const savedRoutes={},savedKeys={},reveals=[],connectionSaves=[];
   let delayRoutingConfig=false,releaseRoutingConfig;
   let delayReveal=false,releaseReveal;
   const credentialRef=(side,url)=>side+':'+url.replace(/\/$/,'');
   page.on('pageerror',error=>errors.push({width,message:error.message}));
   page.on('console',message=>{if(message.type()==='error'&&/\[render\]|\[refresh\]/.test(message.text()))errors.push({width,message:message.text()})});
   await context.route('**/*',async route=>{
    if(await require('./assets.cjs').serveAsset(route,root))return;
    const request=route.request(),url=new URL(request.url()),endpoint=url.pathname;
    if(url.origin!=='http://127.0.0.1:18767'){unexpected.push(url.origin);return route.abort()}
    if(endpoint==='/')return route.fulfill({contentType:'text/html',body:html});
    if(endpoint==='/design-system-gallery')return route.fulfill({contentType:'text/html',body:fs.readFileSync(path.join(root,'docs/design-system/gallery.html'),'utf8')});
    let data;
    if(endpoint==='/auth/status')data={authenticated:true,user:{username:'合成回归',role:'user'}};
    else if(endpoint==='/tasks'&&request.method()==='GET')data=emptyRecords;
    else if(endpoint==='/config')data={config:{url:'http://127.0.0.1:9000/v1',model:'unrelated-gateway-model'}};
    else if(endpoint==='/connections')data={connections:[],storage_available:true};
    else if(endpoint==='/routing-config'){
     if(request.method()==='POST'){
      const {side,name,url,api_key}=request.postDataJSON();connectionSaves.push({side,name,url,api_key});savedRoutes[side]={name,url};
      if(api_key)savedKeys[credentialRef(side,url)]=api_key;
      data={ok:true,saved:Boolean(savedKeys[credentialRef(side,url)]),storage_available:true};
     }else{
      if(delayRoutingConfig)await new Promise(resolve=>{releaseRoutingConfig=resolve});
      data={config:Object.fromEntries(Object.entries(savedRoutes).map(([side,connection])=>[side,{...connection,api_key_saved:Boolean(savedKeys[credentialRef(side,connection.url)])}])),storage_available:true};
     }
    }
    else if(endpoint==='/routing-config/reveal'&&request.method()==='POST'){
     const {side,url}=request.postDataJSON();reveals.push({side,url});
     assert.deepEqual(Object.keys(request.postDataJSON()).sort(),['side','url']);
     assert.equal(savedRoutes[side]?.url,url,'only the saved side and exact address may reveal a key');
     if(delayReveal)await new Promise(resolve=>{releaseReveal=resolve});
     data={api_key:savedKeys[credentialRef(side,url)]};
    }
    else if(endpoint==='/templates')data={templates:[]};
    else if(endpoint==='/routing-corpus')data=corpus;
    else if(endpoint==='/quality-suite')data=suite;
    else if(endpoint==='/quality-reference-records')data={records:[]};
    else if(endpoint==='/routing-models'){
     const connection=request.postDataJSON();discoveries.push(connection);
     assert.deepEqual(Object.keys(connection).sort(),['api_key','side','url']);
     const key=connection.api_key||savedKeys[credentialRef(connection.side,connection.url)];
     assert.ok(key,'discovery uses supplied or saved synthetic credentials');
     const baseline=connection.url.includes(':9001/');
     const mode=discoveryMode;
     if(mode==='delayed'&&baseline)await new Promise(resolve=>{releaseDelayed=resolve});
     if(mode==='failure'&&!baseline)return route.fulfill({status:502,contentType:'application/json',body:JSON.stringify({error:'模型接口返回 401，请检查密钥'})});
     savedKeys[credentialRef(connection.side,connection.url)]=key;
     savedRoutes[connection.side]={name:savedRoutes[connection.side]?.name||'',url:connection.url};
     data={ok:true,saved:true,storage_available:true,models:mode==='disjoint'?[baseline?'fixed-only':'smart-only']:
      mode==='empty'?[]:mode==='delayed'?['obsolete-model']:
      [baseline?'fixed-only':'smart-only','shared-routing-model','<synthetic-model & "quoted">']};
    }
    else if(endpoint==='/models')data={models:['synthetic-quality-model']};
    else if(endpoint==='/credential-status')data={saved:url.searchParams.get('kind')==='routing'&&Boolean(savedKeys[credentialRef(url.searchParams.get('side'),url.searchParams.get('url'))]),storage_available:true};
    else if(endpoint==='/preflight')data={level:'ok',requires_confirmation:false,planned_requests:2000,min_seconds:100,estimated_bytes:1024,target:{host:'127.0.0.1'},findings:[]};
    else if(['/routing-comparisons','/quality-tests','/comparisons','/tasks'].includes(endpoint)&&request.method()==='POST'){
     submissions.push({width,endpoint,body:request.postDataJSON()});
     const key={'/routing-comparisons':'routing_comparison_id','/quality-tests':'quality_test_id','/comparisons':'comparison_id','/tasks':'task_id'}[endpoint];
     data={[key]:'synthetic-'+key};
    }else if(endpoint==='/favicon.ico')data={};
    else{unexpected.push(request.method()+' '+endpoint);return route.fulfill({status:404,contentType:'application/json',body:JSON.stringify({error:'Unexpected mock request'})})}
    return route.fulfill({contentType:'application/json',body:JSON.stringify(data)});
   });
   await page.goto('http://127.0.0.1:18767/');
   await page.waitForFunction(()=>authenticated&&refreshTimer!==null&&!refreshing);
   await page.evaluate(()=>clearInterval(refreshTimer));
   await uniqueIds(page,'initial '+width);

   await openFromButton(page,'gateway');
   await drawerGeometry(page,width,height,'gateway-single '+width);
   assert.equal(await page.locator('#url').isVisible(),true);
   await closeDrawer(page);
   await page.locator('#gatewaySubnav [data-view="compare"]').click();
   await page.locator('#recordCreateButton').click();
   await drawerGeometry(page,width,height,'gateway-compare '+width);
   assert.equal(await page.locator('#groupFields .group-row').count(),2);
   await closeDrawer(page);

   await openFromButton(page,'routing');
   await drawerGeometry(page,width,height,'routing '+width);
   assert.equal(await page.locator('#routingModel').isVisible(),true,'routing owns a visible shared model');
   const beforeInvalid=submissions.length;
   await page.locator('#submit').click();
   await page.waitForFunction(()=>document.getElementById('formStatus').classList.contains('error'));
   assert.equal(submissions.length,beforeInvalid,'incomplete route form must not submit');
   assert.equal(await page.locator('#config').evaluate(node=>node.classList.contains('mobile-open')),true);
   await page.locator('#routingName').fill('按钮交互回归');
   assert.equal(await page.locator('#routingModel').isDisabled(),true);
   await page.locator('#loadRoutingModels').click();
   assert.equal(await page.locator('#routingModelHint').textContent(),'请先填写固定路由的有效网关地址。');
   await page.locator('#routingConcurrency').fill('4');
   await page.locator('#routingRpm').fill('120');
   await page.locator('#routingTier').selectOption('1000');
   await page.locator('.routing-advanced summary').click();
   await page.locator('#routingConnectTimeout').fill('9');
   await page.locator('#routingReadTimeout').fill('45');
   await page.locator('#routingCapture').check();
   const baseline=await side(page,'baseline');
   await baseline.locator('.routing-side-url').fill('http://127.0.0.1:9001/v1');
   await baseline.locator('.routing-side-key').fill('synthetic-baseline-key');
   const candidate=await side(page,'candidate');
   await candidate.locator('.routing-side-url').fill('http://127.0.0.1:9002/v1');
   await candidate.locator('.routing-side-key').fill('synthetic-candidate-key');
   for(const key of ['baseline','candidate','baseline']){
    const panel=await side(page,key);
    assert.equal(await panel.locator('.routing-side-url').inputValue(),`http://127.0.0.1:${key==='baseline'?9001:9002}/v1`);
    assert.equal(await panel.locator('.routing-side-key').inputValue(),`synthetic-${key}-key`,key+': switching must retain input');
   }
   // Current inputs reveal locally and retain their values when concealed again.
   for(const key of ['baseline','candidate']){
    const panel=await side(page,key),input=panel.locator('.routing-side-key'),eye=panel.locator('[data-click-action="revealRoutingKey"]');
    assert.equal(await input.getAttribute('type'),'password');
    assert.equal(await eye.getAttribute('aria-pressed'),'false');
    await eye.click();
    assert.equal(await input.getAttribute('type'),'text');
    assert.equal(await eye.getAttribute('aria-pressed'),'true');
    assert.equal(await input.inputValue(),`synthetic-${key}-key`);
    assert.equal(reveals.length,0,'displaying entered values never retrieves saved keys');
    if(key==='baseline'&&[1440,980,390].includes(width)){
     await page.waitForFunction(()=>routingHasSavedKey('baseline'));
     await page.evaluate(()=>document.getElementById('toast').classList.remove('show'));
     for(const theme of ['light','dark']){
      await page.evaluate(theme=>document.documentElement.dataset.theme=theme,theme);
      await input.scrollIntoViewIfNeeded();
      await drawerGeometry(page,width,height,'routing-key '+width+' '+theme);
      await page.screenshot({path:path.join(output,`routing-key-${width}-${theme}.png`)});
     }
     await page.evaluate(()=>document.documentElement.dataset.theme='light');
    }
    await eye.click();
    assert.equal(await input.getAttribute('type'),'password');
    assert.equal(await eye.getAttribute('aria-pressed'),'false');
    assert.equal(await input.inputValue(),`synthetic-${key}-key`,'concealing entered keys preserves the draft');
   }
   await page.locator('#loadRoutingModels').click();
   await page.waitForFunction(()=>!document.getElementById('loadRoutingModels').disabled);
   assert.deepEqual(await page.locator('#routingModel option').evaluateAll(nodes=>nodes.map(node=>node.value)),['','<synthetic-model & "quoted">','shared-routing-model']);
   assert.match(await page.locator('#routingModelHint').textContent(),/固定路由 3 个模型；智能路由 3 个模型；共同支持 2 个/);
   assert.equal(await page.locator('#routingModel').inputValue(),'','selection is explicit');
   await page.locator('#submit').click();
   assert.equal(submissions.length,beforeInvalid,'discovered models still require a selection');
   await page.locator('#routingModel').selectOption('shared-routing-model');
   assert.equal(await page.locator('#formStatus').textContent(),'','correcting a model clears the outdated validation error');
   // Route labels do not affect discovery, but credentials and addresses do.
   await (await side(page,'baseline')).locator('.routing-side-name').fill('固定入口');
   assert.equal(await page.locator('#routingModel').inputValue(),'shared-routing-model');
   await page.locator('#routing-baseline-key').fill('replacement-key');
   assert.equal(await page.locator('#routingModel').inputValue(),'');
   assert.equal(await page.locator('#routingModel').isDisabled(),true);
   await page.locator('#routing-baseline-key').fill('synthetic-baseline-key');
   await page.locator('#loadRoutingModels').click();
   await page.waitForFunction(()=>!document.getElementById('loadRoutingModels').disabled);
   await page.locator('#routingModel').selectOption('shared-routing-model');
   assert.deepEqual(discoveries.slice(-2).sort((a,b)=>a.url.localeCompare(b.url)),[
    {side:'baseline',url:'http://127.0.0.1:9001/v1',api_key:'synthetic-baseline-key'},
    {side:'candidate',url:'http://127.0.0.1:9002/v1',api_key:'synthetic-candidate-key'}]);
   await page.locator('#routingModel').scrollIntoViewIfNeeded();
   await drawerGeometry(page,width,height,'routing-models '+width);
   await page.screenshot({path:path.join(output,`routing-create-${width}.png`)});
   await page.locator('#submit').click();
   await page.waitForFunction(()=>!document.getElementById('config').classList.contains('mobile-open'));
   const request=submissions.filter(item=>item.width===width&&item.endpoint==='/routing-comparisons').at(-1);
   assert.ok(request,'real submit must reach routing endpoint');
   for(const key of ['baseline','candidate']){
    assert.equal(request.body[key].api_key,`synthetic-${key}-key`);
    assert.equal(request.body[key].url,`http://127.0.0.1:${key==='baseline'?9001:9002}/v1`);
    assert.equal(request.body[key].model,'shared-routing-model','route model is independent from hidden gateway model');
    assert.equal(request.body[key].concurrency,4);
    assert.equal(request.body[key].rpm,120);
    assert.equal(request.body[key].connect_timeout,9);
    assert.equal(request.body[key].read_timeout,45);
    assert.equal(request.body[key].capture_raw,true);
   }
   const shared=object=>Object.fromEntries(Object.entries(object).filter(([key])=>!['name','url','api_key'].includes(key)));
   assert.deepEqual(shared(request.body.baseline),shared(request.body.candidate),'both route sides use identical shared parameters');
   assert.equal(request.body.tier,1000);

   // Failure, empty intersection and stale responses must never keep a usable old choice.
   await page.locator('#recordCreateButton').click();
   for(const key of ['baseline','candidate']){
    const panel=await side(page,key);
    await panel.locator('.routing-side-url').fill(`http://127.0.0.1:${key==='baseline'?9001:9002}/v1`);
    await panel.locator('.routing-side-key').fill(`synthetic-${key}-key`);
   }
   for(const mode of ['failure','disjoint','empty']){
    discoveryMode=mode;
    await page.locator('#loadRoutingModels').click();
    await page.waitForFunction(()=>!document.getElementById('loadRoutingModels').disabled);
    assert.equal(await page.locator('#routingModel').isDisabled(),true);
    assert.equal(await page.locator('#routingModel').inputValue(),'');
    assert.match(await page.locator('#routingModelHint').textContent(),mode==='failure'?/智能路由获取失败.*401/:/没有同名的共同模型/);
   }
   await page.locator('#routingManualModelToggle').click();
   await page.locator('#routingModelManual').fill('manually-specified-model');
   assert.equal(await page.evaluate(()=>readRouting().baseline.model),'manually-specified-model');
   assert.equal(await page.evaluate(()=>readRouting().candidate.model),'manually-specified-model');
   await page.locator('#routingManualModelToggle').click();
   assert.equal(await page.evaluate(()=>readRouting().baseline.model),'','hidden manual draft must not leak');

   discoveryMode='delayed';releaseDelayed=null;
   await page.locator('#loadRoutingModels').click();
   await page.waitForFunction(()=>document.getElementById('loadRoutingModels').disabled);
   // Wait until both mock requests exist, then edit the connection and request fresh data.
   while(!releaseDelayed)await new Promise(resolve=>setTimeout(resolve,10));
   await (await side(page,'baseline')).locator('.routing-side-url').fill('http://127.0.0.1:9001/v1/');
   assert.equal(await page.locator('#loadRoutingModels').isEnabled(),true);
   discoveryMode='normal';
   await page.locator('#loadRoutingModels').click();
   await page.waitForFunction(()=>!document.getElementById('loadRoutingModels').disabled);
   await page.locator('#routingModel').selectOption('shared-routing-model');
   const oldResponse=page.waitForResponse(response=>response.url().endsWith('/routing-models'));
   releaseDelayed();await oldResponse;
   await page.evaluate(()=>new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(resolve))));
   assert.equal(await page.locator('#routingModel').inputValue(),'shared-routing-model','late result cannot replace new list');
   assert.equal(await page.locator('#routingModel option[value="obsolete-model"]').count(),0);
   // Manual fallback remains a real submission path for gateways without discovery.
   await page.locator('#routingManualModelToggle').click();
   await page.locator('#routingModelManual').fill('manually-specified-model');
   await page.locator('#submit').click();
   await page.waitForFunction(()=>!document.getElementById('config').classList.contains('mobile-open'));
   const manual=submissions.filter(item=>item.width===width&&item.endpoint==='/routing-comparisons').at(-1);
   assert.equal(manual.body.baseline.model,'manually-specified-model');
   assert.equal(manual.body.candidate.model,'manually-specified-model');

   await page.locator('#recordCreateButton').click();
   assert.equal(await page.locator('#routingModel').inputValue(),'');
   assert.equal(await page.locator('#routingModelManualWrap').isHidden(),true);
   assert.equal(await page.locator('#routingModelManual').inputValue(),'');
   for(const key of ['baseline','candidate']){
    const panel=await side(page,key);
    assert.equal(await panel.locator('.routing-side-key').inputValue(),'','new run must not retain submitted credentials');
    await page.waitForFunction(side=>document.querySelector(`[data-routing-side="${side}"]`).dataset.apiKeySaved==='true',key);
    assert.match(await panel.locator('.routing-side-url').inputValue(),new RegExp(':'+ (key==='baseline'?9001:9002)+'/v1'));
    assert.equal(await panel.locator('.routing-side-key').evaluate(input=>input.required),false,'saved credentials permit an empty key');
   }
   discoveryMode='normal';await page.locator('#loadRoutingModels').click();
   await page.waitForFunction(()=>!document.getElementById('loadRoutingModels').disabled);
   assert.ok(discoveries.slice(-2).every(item=>item.api_key===''),'new run discovers models using saved credentials');
   // Saved keys are fetched only on an explicit click and are never sent back by autosave.
   assert.equal(reveals.length,0,'opening, restoring and discovering models must not reveal saved keys');
   await page.evaluate(()=>Promise.allSettled(Object.values(routingConnectionQueues)));
   const beforeReveals=connectionSaves.length;
   for(const key of ['baseline','candidate']){
    const panel=await side(page,key),input=panel.locator('.routing-side-key'),eye=panel.locator('[data-click-action="revealRoutingKey"]');
    await eye.click();
    await page.waitForFunction(side=>document.querySelector(`[data-routing-side="${side}"] .routing-side-key`).type==='text',key);
    assert.equal(await input.inputValue(),`synthetic-${key}-key`);
    assert.equal(await page.evaluate(side=>readRoutingSide(side).api_key,key),'','reveal is not a new credential input');
    await eye.click();
    assert.equal(await input.inputValue(),'','concealing a retrieved key clears the temporary value');
    assert.equal(await eye.getAttribute('aria-pressed'),'false');
   }
   await page.waitForTimeout(650);
   assert.equal(connectionSaves.length,beforeReveals,'revealing and concealing saved keys never triggers autosave');
   assert.equal(reveals.length,2,'one saved-key retrieval per explicit side click');

   // Editing a revealed saved key makes it a normal draft; hiding still preserves that edit.
   const keyPanel=await side(page,'baseline'),keyInput=keyPanel.locator('.routing-side-key'),keyEye=keyPanel.locator('[data-click-action="revealRoutingKey"]');
   await keyEye.click();await page.waitForFunction(()=>document.getElementById('routing-baseline-key').type==='text');
   await keyInput.fill('synthetic-edited-key');await keyEye.click();
   assert.equal(await keyInput.inputValue(),'synthetic-edited-key');
   assert.equal(await page.evaluate(()=>readRoutingSide('baseline').api_key),'synthetic-edited-key');
   assert.equal(await keyInput.getAttribute('type'),'password');
   await keyInput.fill('');
   await page.waitForFunction(()=>routingHasSavedKey('baseline'));

   // A changed address clears revealed material before the new address can be saved.
   const savedBaselineUrl=await keyPanel.locator('.routing-side-url').inputValue();
   await keyEye.click();await page.waitForFunction(()=>document.getElementById('routing-baseline-key').type==='text');
   await keyPanel.locator('.routing-side-url').fill('http://127.0.0.1:9003/v1');
   assert.equal(await keyInput.inputValue(),'');assert.equal(await keyInput.getAttribute('type'),'password');
   assert.equal(await keyEye.getAttribute('aria-pressed'),'false');
   await keyPanel.locator('.routing-side-url').fill(savedBaselineUrl);
   await page.waitForFunction(()=>routingHasSavedKey('baseline'));

   // A retrieval that completes after the drawer closes cannot restore a plaintext value.
   delayReveal=true;releaseReveal=null;await keyEye.click();
   while(!releaseReveal)await new Promise(resolve=>setTimeout(resolve,10));
   assert.equal(await keyEye.isDisabled(),true);
   await closeDrawer(page);
   const revealFinished=page.waitForResponse(response=>response.url().endsWith('/routing-config/reveal'));
   delayReveal=false;releaseReveal();await revealFinished;
   await page.evaluate(()=>new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(resolve))));
   assert.equal(await keyInput.inputValue(),'');assert.equal(await keyInput.getAttribute('type'),'password');
   assert.equal(await keyEye.getAttribute('aria-pressed'),'false');assert.equal(await keyEye.isDisabled(),false);
   await page.locator('#recordCreateButton').click();
   for(const key of ['baseline','candidate'])await page.waitForFunction(side=>routingHasSavedKey(side),key);
   const localValues=await page.evaluate(()=>Object.values(localStorage));
   assert.ok(localValues.every(value=>!value.includes('synthetic-baseline-key')&&!value.includes('synthetic-candidate-key')),'no secret is written to browser storage');
   for(const key of ['baseline','candidate']){
    const panel=await side(page,key);
    await panel.locator('.routing-side-key').fill('synthetic-abandoned-key');
   }
   await closeDrawer(page);
   await page.locator('#recordCreateButton').click();
   for(const key of ['baseline','candidate'])assert.equal(await (await side(page,key)).locator('.routing-side-key').inputValue(),'','new run must not retain abandoned credentials');
   await closeDrawer(page);

   // A page reload restores both addresses and saved-key status without returning raw keys.
   await page.reload();await page.waitForFunction(()=>authenticated&&refreshTimer!==null&&!refreshing);
   await page.evaluate(()=>clearInterval(refreshTimer));await openFromButton(page,'routing');
   for(const key of ['baseline','candidate']){
    const panel=await side(page,key);
    await page.waitForFunction(side=>document.querySelector(`[data-routing-side="${side}"]`).dataset.apiKeySaved==='true',key);
    assert.match(await panel.locator('.routing-side-url').inputValue(),new RegExp(':'+ (key==='baseline'?9001:9002)+'/v1'));
    assert.equal(await panel.locator('.routing-side-key').inputValue(),'');
   }
   await closeDrawer(page);

   // Closing during first restore must not overwrite saved addresses with initial empty fields.
   const originalRoutes=structuredClone(savedRoutes);
   await page.reload();await page.waitForFunction(()=>authenticated&&refreshTimer!==null&&!refreshing);
   await page.evaluate(()=>clearInterval(refreshTimer));delayRoutingConfig=true;releaseRoutingConfig=null;
   await openFromButton(page,'routing');
   while(!releaseRoutingConfig)await new Promise(resolve=>setTimeout(resolve,10));
   await closeDrawer(page);
   await page.evaluate(()=>Promise.allSettled(Object.values(routingConnectionQueues)));
   assert.deepEqual(savedRoutes,originalRoutes,'closing unresolved restore preserves saved addresses');
   const restored=page.waitForResponse(response=>response.url().endsWith('/routing-config')&&response.request().method()==='GET');
   delayRoutingConfig=false;releaseRoutingConfig();await restored;

   // Editing only the name during restore still restores the URL and saves the new name.
   await page.reload();await page.waitForFunction(()=>authenticated&&refreshTimer!==null&&!refreshing);
   await page.evaluate(()=>clearInterval(refreshTimer));delayRoutingConfig=true;releaseRoutingConfig=null;
   await openFromButton(page,'routing');
   while(!releaseRoutingConfig)await new Promise(resolve=>setTimeout(resolve,10));
   await page.locator('#routing-baseline-name').fill('恢复期间修改的名称');
   await closeDrawer(page);
   await page.evaluate(()=>Promise.allSettled(Object.values(routingConnectionQueues)));
   assert.deepEqual(savedRoutes,originalRoutes,'name-only draft cannot erase an unresolved address');
   const nameSaved=page.waitForResponse(response=>response.url().endsWith('/routing-config')&&response.request().method()==='POST');
   delayRoutingConfig=false;releaseRoutingConfig();await nameSaved;
   assert.equal(savedRoutes.baseline.name,'恢复期间修改的名称');
   assert.equal(savedRoutes.baseline.url,originalRoutes.baseline.url);
   assert.equal(savedRoutes.candidate.url,originalRoutes.candidate.url);

   // Open quality after routing: hidden routing cards must not become quality rows.
   await openFromButton(page,'quality');
   await drawerGeometry(page,width,height,'quality '+width);
   const resources=page.locator('#qualityResourceFields .quality-resource');
   assert.equal(await resources.count(),2,'quality comparison has two resource cards');
   for(let i=0;i<2;i++){
    await resources.nth(i).locator('.quality-url').fill(`http://127.0.0.1:${9010+i}/v1`);
    await resources.nth(i).locator('.quality-key').fill('synthetic-quality-key-'+i);
    await resources.nth(i).locator('.quality-model-refresh').click();
    await page.waitForFunction(index=>document.querySelectorAll('#qualityResourceFields .quality-model')[index].value==='synthetic-quality-model',i);
   }
   await page.locator('#submit').click();
   await page.waitForFunction(()=>!document.getElementById('config').classList.contains('mobile-open'));
   const quality=submissions.filter(item=>item.width===width&&item.endpoint==='/quality-tests').at(-1);
   assert.ok(quality,'quality submission still works after routing');
   assert.equal(quality.body.resources.length,2);
   assert.ok(quality.body.resources.every(resource=>resource.model==='synthetic-quality-model'));
   await uniqueIds(page,'after submit '+width);
   if(width===1440){
    await page.goto('http://127.0.0.1:18767/design-system-gallery');
    const galleryKey=page.locator('#demoKey'),galleryEye=page.locator('[data-click-action="togglePasswordVisibility"][data-arg0="demoKey"]');
    assert.equal(await galleryKey.getAttribute('type'),'password');
    await galleryEye.click();assert.equal(await galleryKey.getAttribute('type'),'text');
    for(const theme of ['light','dark']){
     await page.evaluate(theme=>document.documentElement.dataset.theme=theme,theme);
     await page.locator('#demoKey').scrollIntoViewIfNeeded();
     assert.equal(await galleryKey.evaluate(input=>input.parentElement.scrollWidth>input.parentElement.clientWidth),false,'gallery password component fits '+theme);
     await page.locator('#demoKey').locator('..').screenshot({path:path.join(output,`gallery-password-${theme}.png`)});
    }
    await galleryEye.click();assert.equal(await galleryKey.getAttribute('type'),'password');
    assert.equal(await galleryKey.inputValue(),'synthetic-demo-key');
   }
   results.push({width,height,realCreationButtons:4,routingSidesRetained:true,sharedModel:true,credentialsReset:true,savedConnectionReuse:true,keyVisibility:true,onDemandKeyReveal:true,revealDoesNotAutosave:true,lateRevealDiscarded:true,reloadRestoresConnections:true,qualityAfterRouting:true});
   await context.close();
  }
  assert.deepEqual(errors,[],'browser runtime errors');
  assert.deepEqual(unexpected,[],'all network must be synthetic and explicitly handled');
  fs.writeFileSync(path.join(output,'result.json'),JSON.stringify({passed:true,results,errors,unexpected,submissions:submissions.length},null,2));
  console.log(JSON.stringify({passed:true,viewports:results.length,submissions:submissions.length,errors,unexpected,output},null,2));
 }finally{await browser.close()}
})().catch(error=>{console.error(error);process.exitCode=1});
