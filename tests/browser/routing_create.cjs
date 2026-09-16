/** Real-button creation regression with synthetic API responses only.
 * NODE_PATH=<directory containing playwright> node tests/browser/routing_create.cjs
 * No server, user credentials, saved accounts, or upstream requests are used.
 */
const {chromium}=require('playwright');
const assert=require('node:assert/strict'),fs=require('fs'),path=require('path'),os=require('os');
const {execFileSync}=require('child_process');
const root=process.env.UI_PROJECT_ROOT||path.resolve(__dirname,'../..');
const output=process.env.UI_ARTIFACT_DIR||fs.mkdtempSync(path.join(os.tmpdir(),'route-create-'));
fs.mkdirSync(output,{recursive:true});
const html=process.env.UI_HTML?fs.readFileSync(process.env.UI_HTML,'utf8'):execFileSync(
 process.env.PYTHON||'python3',['-c','import route_test_ui as ui; print(ui.INDEX_HTML)'],
 {cwd:root,encoding:'utf8',env:{...process.env,PYTHONDONTWRITEBYTECODE:'1'}});
const corpus={tiers:[{tier:1000,bursts:10,note:'合成测试集。'},{tier:3000,bursts:30,note:'合成测试集。'}],metrics:[]};
const suite={manifest:{title:'合成题集',case_count:1,categories:['basic'],input_chars:{min:4,max:4}},cases:[]};
const emptyRecords={tasks:[],comparisons:[],quality_tests:[],routing_comparisons:[]};
const results=[],errors=[],unexpected=[],submissions=[];
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
  for(const width of [390,768,1440]){
   const height=width===390?844:1000;
   const context=await browser.newContext({viewport:{width,height},reducedMotion:'reduce'});
   const page=await context.newPage();
   page.on('pageerror',error=>errors.push({width,message:error.message}));
   page.on('console',message=>{if(message.type()==='error'&&/\[render\]|\[refresh\]/.test(message.text()))errors.push({width,message:message.text()})});
   await context.route('**/*',async route=>{
    const request=route.request(),url=new URL(request.url()),endpoint=url.pathname;
    if(url.origin!=='http://127.0.0.1:18767'){unexpected.push(url.origin);return route.abort()}
    if(endpoint==='/')return route.fulfill({contentType:'text/html',body:html});
    let data;
    if(endpoint==='/auth/status')data={authenticated:true,user:{username:'合成回归',role:'user'}};
    else if(endpoint==='/tasks'&&request.method()==='GET')data=emptyRecords;
    else if(endpoint==='/config')data={config:{url:'http://127.0.0.1:9000/v1',model:'unrelated-gateway-model'}};
    else if(endpoint==='/templates')data={templates:[]};
    else if(endpoint==='/routing-corpus')data=corpus;
    else if(endpoint==='/quality-suite')data=suite;
    else if(endpoint==='/quality-reference-records')data={records:[]};
    else if(endpoint==='/models')data={models:['synthetic-quality-model']};
    else if(endpoint==='/credential-status')data={saved:false};
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
   await page.locator('#routingModel').fill('shared-routing-model');
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

   await page.locator('#recordCreateButton').click();
   for(const key of ['baseline','candidate']){
    const panel=await side(page,key);
    assert.equal(await panel.locator('.routing-side-key').inputValue(),'','new run must not retain submitted credentials');
    await panel.locator('.routing-side-key').fill('synthetic-abandoned-key');
   }
   await closeDrawer(page);
   await page.locator('#recordCreateButton').click();
   for(const key of ['baseline','candidate'])assert.equal(await (await side(page,key)).locator('.routing-side-key').inputValue(),'','new run must not retain abandoned credentials');
   await closeDrawer(page);

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
   results.push({width,height,realCreationButtons:4,routingSidesRetained:true,sharedModel:true,credentialsReset:true,qualityAfterRouting:true});
   await context.close();
  }
  assert.deepEqual(errors,[],'browser runtime errors');
  assert.deepEqual(unexpected,[],'all network must be synthetic and explicitly handled');
  fs.writeFileSync(path.join(output,'result.json'),JSON.stringify({passed:true,results,errors,unexpected,submissions:submissions.length},null,2));
  console.log(JSON.stringify({passed:true,viewports:results.length,submissions:submissions.length,errors,unexpected,output},null,2));
 }finally{await browser.close()}
})().catch(error=>{console.error(error);process.exitCode=1});
