/** Browser geometry regression; all APIs are synthetic, no user account/key is used.
 * NODE_PATH=<directory containing playwright> node tests/browser/layout.cjs
 * Optional: CHROME_PATH=<browser binary>, UI_ARTIFACT_DIR=<output directory>.
 */
const {chromium}=require('playwright');
const fs=require('fs'),path=require('path'),assert=require('node:assert/strict');
const {execFileSync}=require('child_process');
const {projectRoot:root,output,env}=require('./artifacts.cjs').browserArtifacts('layout');
execFileSync(process.env.PYTHON||'python3',[path.join(root,'tests/browser/make_fixture.py'),output],{cwd:root,env});
const fixture=JSON.parse(fs.readFileSync(path.join(output,'fixture.json'),'utf8'));
const html=fs.readFileSync(process.env.UI_HTML||path.join(output,'index.html'),'utf8');
const results=[],errors=[],unexpected=[];
const near=(a,b,label)=>assert.ok(Math.abs(a-b)<1,`${label}: ${a} != ${b}`);
async function bounds(page,selector){return page.locator(selector).first().evaluate(el=>{const r=el.getBoundingClientRect(),s=getComputedStyle(el);return {left:r.left,right:r.right,width:r.width,top:r.top,bottom:r.bottom,height:r.height,paddingLeft:s.paddingLeft,paddingRight:s.paddingRight,shadow:s.boxShadow,radius:s.borderRadius}})}
async function sameEdges(page,selector,reference){const rects=await page.locator(selector).evaluateAll(els=>els.map(el=>el.getBoundingClientRect()).filter(r=>r.width>0).map(r=>({left:r.left,right:r.right})));assert.ok(rects.length,selector+' has visible content');for(const r of rects){near(r.left,reference.left,`${selector} left`);near(r.right,reference.right,`${selector} right`)}}
async function checkOverflow(page,selector){const issues=await page.locator(selector).evaluateAll(els=>els.filter(el=>el.getBoundingClientRect().width>0&&el.scrollWidth>el.clientWidth+1).map(el=>({id:el.id,class:el.className,scrollWidth:el.scrollWidth,clientWidth:el.clientWidth})));assert.deepEqual(issues,[],`horizontal overflow: ${JSON.stringify(issues)}`)}
async function screenshot(page,name){await page.screenshot({path:path.join(output,name+'.png')})}
(async()=>{
 const options={headless:true};const localChrome='/Applications/Google Chrome.app/Contents/MacOS/Google Chrome';
 if(process.env.CHROME_PATH||fs.existsSync(localChrome))options.executablePath=process.env.CHROME_PATH||localChrome;
 const browser=await chromium.launch(options);
 try{
 for(const width of [2560,1920,1440,1280,1024,980,768,640,390]){
  console.log('Checking viewport',width);
  const page=await browser.newPage({viewport:{width,height:1080},reducedMotion:'reduce'});
  page.on('pageerror',e=>errors.push(e.message));
  await page.route('**/*',async route=>{
    if(await require('./assets.cjs').serveAsset(route,root))return;
   const url=new URL(route.request().url()),pathname=url.pathname;let data={};
   if(url.hostname!=='127.0.0.1'){unexpected.push(url.origin);return route.abort()}
   if(pathname==='/')return route.fulfill({contentType:'text/html',body:html});
   if(pathname==='/auth/status')data={authenticated:true,user:{username:'视觉验收',role:'user'}};
   else if(pathname==='/tasks')data=fixture;
   else if(pathname==='/config')data={config:{}};
   else if(pathname==='/connections'&&route.request().method()==='GET')data={connections:[],storage_available:true};
   else if(pathname==='/templates')data={templates:[]};
   else if(pathname==='/routing-corpus')data={tiers:[{tier:1000,bursts:10,note:'合成布局题集'}],metrics:[]};
   else if(pathname==='/quality-suite')data={manifest:{title:'合成布局题集',case_count:2,categories:['basic'],input_chars:{min:4,max:20}},cases:[]};
   else if(pathname==='/quality-reference-records')data={records:[]};
   else if(pathname==='/preflight')data={level:'ok',requires_confirmation:false,planned_requests:2,min_seconds:1,estimated_bytes:1024,target:{host:'example.test'},findings:[]};
   else if(pathname.startsWith('/task/'))data=fixture.tasks.find(t=>t.id===pathname.split('/')[2]);
   else if(pathname.startsWith('/comparison/'))data=fixture.comparisons[0];
   else if(pathname.startsWith('/quality-test/'))data=fixture.quality_tests[0];
   else if(pathname==='/models')data={models:['model-fixture','model-alternate']};
   else if(pathname.startsWith('/credential-status'))data={saved:false};
   else if(pathname!=='/favicon.ico')unexpected.push(pathname);
   await route.fulfill({contentType:'application/json',body:JSON.stringify(data)});
  });
  await page.goto('http://127.0.0.1:18766/');
  await page.locator('#gatewayList .task').first().waitFor({state:'attached'});
  const geometry=await page.locator('.app,.layout,.sidebar,.record-panel,.main').evaluateAll(els=>els.map(el=>({class:el.className,rect:el.getBoundingClientRect().toJSON(),display:getComputedStyle(el).display,rows:getComputedStyle(el).gridTemplateRows})));
  fs.writeFileSync(path.join(output,'layout-'+width+'.json'),JSON.stringify(geometry,null,2));
  await page.locator('#gatewayList .task').first().waitFor({timeout:3000});
  // Polling stays covered by an explicit refresh below; prevent mid-measurement replacement.
  await page.waitForFunction(()=>refreshTimer!==null&&!refreshing);await page.evaluate(()=>clearInterval(refreshTimer));
  await page.locator('#gatewayList .task').first().click();await page.waitForFunction(()=>!refreshing);
  await page.locator('#detail .overview-shell').waitFor();
  const single=await bounds(page,'.overview-shell');
  await sameEdges(page,'#detail>.report-section:not(.report-footer-actions),#detail>.report-head',single);
  const headingSingle=await bounds(page,'#recordTitle'),createSingle=await bounds(page,'#recordCreateButton');
  await checkOverflow(page,'.layout,.main,#detail');
  if([1920,768,390].includes(width))await screenshot(page,`single-${width}`);
  if([1440,980,390].includes(width))for(const theme of ['light','dark']){
   await page.evaluate(theme=>document.documentElement.dataset.theme=theme,theme);await screenshot(page,`single-${width}-${theme}`);
  }
  await page.evaluate(()=>document.documentElement.dataset.theme='light');
  assert.equal(await page.locator('#detail .overview-shell .report-metric').count(),4,'the summary has four balanced key metrics');
  if(width===980){
   const tiles=await page.locator('#detail .overview-shell .report-metric').evaluateAll(nodes=>nodes.map(node=>{const r=node.getBoundingClientRect();return {top:r.top,left:r.left}}));
   near(tiles[0].top,tiles[1].top,'first row of balanced metrics');near(tiles[2].top,tiles[3].top,'second row of balanced metrics');
   assert.ok(tiles[2].top>tiles[0].top,'four metrics use two rows at medium width');
  }
  assert.equal(await page.locator('#detail > .report-progress-card:visible').count(),0,'completed progress does not displace the result');
  if([1440,980,390].includes(width)){
   const base=fixture.tasks[0];
   for(const state of ['waiting','running','finished_with_errors','stopped','error','expired']){
    const active=['waiting','running'].includes(state),empty=state==='waiting'||state==='error',done=empty?0:['stopped','running'].includes(state)?150:300;
    const record={...base,status:state,name:'缓存压测 · 状态与异常核查',config:{...base.config,profile:'cache'},total:300,done,progress:done/300,phase:state==='running'?'按计划发出请求':state==='waiting'?'等待执行':'已结束',scheduled:done,started:done,inflight:0,pending:0,elapsed:183,planned_duration:180,rpm:{avg:98,live:98},ttft:{p95:7946},success_pct:empty?null:100*(done-2)/done,counters:empty?{}:{ok:done-2,r5xx:2},cache:{rate_pct:empty?null:100*Math.floor(done*.87)/done,hits:Math.floor(done*.87),request_total:done,no_usage:empty?0:2},recent:empty?[]:[{id:1,cls:'ok',label:'成功',status:200,total:8000,ttft:7000},{id:2,cls:'r5xx',label:'服务端错误',status:502,total:100,error_type:'HTTP_5XX',reason:'合成失败示例'}],error:state==='error'?'本地合成连接失败':'',timeline:empty?[]:base.timeline};
    if(state==='expired'){record.artifact={state:'expired'};record.out_path='/synthetic-expired.jsonl';record.config.capture_raw=true}
    await page.evaluate(data=>{render(data);document.querySelector('.main').scrollTop=0;document.querySelector('.shell').scrollTop=0},record);
    assert.equal(await page.locator('#detail > .report-progress-card:visible').count(),active?1:0,'progress follows lifecycle');
    if(state==='expired'){
     assert.equal(await page.locator('#detail [data-click-action="download"],#detail [data-click-action="showRaw"]').count(),0,'expired files do not offer dead download or raw-view actions');
     assert.match(await page.locator('#gatewayRequests').innerText(),/仅保留历史摘要/);
    }
    if(state==='finished_with_errors'){
     await page.locator('#detail .overview-shell [data-click-action="setGatewayRequestFilter"]').click();
     assert.equal(await page.locator('#gatewayRequests tbody tr').count(),1,'one failing request after filter');
     assert.match(await page.locator('#gatewayRequests tbody').innerText(),/502/);
     await page.evaluate(()=>render(window.currentDetail));
     assert.equal(await page.locator('#gatewayRequests tbody tr').count(),1,'filter survives refresh');
     await page.locator('#gatewayRequests [data-arg1="all"]').click();
     await page.locator('#gatewayRequests [data-arg1="all"]').focus();
     await page.keyboard.press('ArrowRight');
     await page.evaluate(()=>new Promise(requestAnimationFrame));
     assert.equal(await page.locator('#gatewayRequests [data-arg1="failed"]').evaluate(el=>el===document.activeElement&&el.getAttribute('aria-selected')==='true'),true,'keyboard focus matches selected request filter after rendering');
     await page.keyboard.press('ArrowRight');
     await page.evaluate(()=>new Promise(requestAnimationFrame));
     assert.equal(await page.locator('#gatewayRequests [data-arg1="all"]').evaluate(el=>el===document.activeElement&&el.getAttribute('aria-selected')==='true'),true,'request filters can cycle without focus reverting');
    }
    for(const theme of ['light','dark']){
     await page.evaluate(async theme=>{setTheme(theme);await new Promise(requestAnimationFrame);document.querySelector('.main').scrollTop=0;document.querySelector('.shell').scrollTop=0},theme);
     await checkOverflow(page,'.layout,.main,#detail,#detail>.report-section');
     await page.screenshot({path:path.join(output,`single-${state}-${width}-${theme}.png`),animations:'disabled'});
    }
   }
   await page.evaluate(data=>{render(data);setTheme('light')},base);
  }
  const finishedSingle=fixture.tasks[0];fixture.tasks[0]={...finishedSingle,status:'running'};await page.evaluate(()=>refresh());
  assert.equal(await page.locator('#detail .report-head [data-click-action="stopTask"]').count(),1,'single stop is in the header');
  assert.equal(await page.locator('#detail [data-click-action="stopTask"]').count(),1,'single stop is not duplicated');
  fixture.tasks[0]=finishedSingle;await page.evaluate(()=>refresh());
  await page.locator('#gatewaySubnav [data-view="compare"]').click();
  const headingCompare=await bounds(page,'#recordTitle'),createCompare=await bounds(page,'#recordCreateButton');
  near(headingSingle.left,headingCompare.left,'record heading left');near(headingSingle.width,headingCompare.width,'record heading width');
  near(createSingle.left,createCompare.left,'create button left');near(createSingle.width,createCompare.width,'create button width');
  await page.locator('#gatewayList .task').first().click();await page.waitForFunction(()=>!refreshing);
  await page.locator('#modulePlaceholder .report-head').waitFor();
  const comparison=await bounds(page,'#modulePlaceholder .report-head');
  near(single.left,comparison.left,'single vs comparison left');near(single.right,comparison.right,'single vs comparison right');
  assert.equal(single.paddingLeft,comparison.paddingLeft,'shared card padding');assert.equal(single.shadow,comparison.shadow,'shared shadow');
  await sameEdges(page,'.report-shell>.report-section,.report-shell>.report-overview,.report-shell>.report-tabs',single);
  const progress=await bounds(page,'.report-progress-card'),focus=await bounds(page,'.report-focus-card');
  if(Math.abs(progress.top-focus.top)<1)near(progress.height,focus.height,'overview sibling heights');
  const topShadows=await page.locator('.report-shell>.report-section:not(.report-footer-actions),.report-overview>section').evaluateAll(els=>els.map(el=>getComputedStyle(el).boxShadow));
  assert.ok(topShadows.every(shadow=>shadow===single.shadow),'one diffuse shadow for sibling cards');
  await checkOverflow(page,'.layout,.main,.report-shell,.report-head,.report-tabs,.report-section');
  if([1920,768,390].includes(width))await screenshot(page,`comparison-${width}`);
  for(const tab of ['基准组','对比组']){
   await page.getByRole('tab',{name:new RegExp('^'+tab)}).click();
   await page.locator('.group-detail-head').waitFor();
   await sameEdges(page,'.group-detail,.group-detail-head,.group-detail>.report-section',single);
   assert.equal(await page.locator('.group-detail>.report-progress-card').count(),0,'finished group puts execution below its result');
   assert.equal(await page.locator('.group-detail>details[id^="group-process-"]').evaluate(el=>el.open),false,'finished group execution starts collapsed');
   assert.equal((await bounds(page,'.group-detail')).shadow,'none','no nested group shadow');
   assert.equal((await bounds(page,'.group-detail-head')).shadow,single.shadow,'group card shadow');
   assert.equal(await page.locator('.report-overview').count(),0,'group data must not repeat overall report');
   await checkOverflow(page,'.main,.report-shell,.group-detail,.group-detail-head');
   if(tab==='基准组'&&[1920,390].includes(width))await screenshot(page,`group-${width}`);
  }
  await page.getByRole('tab',{name:/^对比组/}).focus();await page.keyboard.press('Home');
  assert.equal(await page.locator('#modulePlaceholder .report-tab.active').innerText(),'对照结论','keyboard report navigation');
  await page.evaluate(()=>refresh());
  const refreshed=await bounds(page,'#modulePlaceholder .report-head');near(refreshed.width,single.width,'width after refresh');
  const completedComparison=fixture.comparisons[0];
  fixture.comparisons[0]={...completedComparison,status:'running',completed_requests:5,planned_requests:20,current_group_name:'40 并发基准'};
  await page.evaluate(()=>refresh());
  assert.equal((await page.locator('#modulePlaceholder .report-progress-line strong').innerText()).trim(),'25%','live request progress');
  await sameEdges(page,'#modulePlaceholder .report-head,.report-overview',single);
  await checkOverflow(page,'.main,.report-head,.report-overview');
  if(width===1920)await screenshot(page,'running-1920');
  fixture.comparisons[0]=completedComparison;await page.evaluate(()=>refresh());
  // Independent quality module shares surfaces but not record flow.
  await page.locator('[data-module="quality"]').click();
  await page.locator('#qualitySubnav [data-view="compare"]').click();
  await page.locator('#qualityList .task').first().click();
  const quality=await bounds(page,'#modulePlaceholder .report-head');near(quality.left,single.left,'quality left');near(quality.right,single.right,'quality right');
  await checkOverflow(page,'.main,.report-shell,.report-head,.report-section');
  if(width===1920)await screenshot(page,'quality-1920');
  // All creation flows remain right-aligned, full height, no horizontal overflow.
  for(const kind of ['quality','comparison','single']){
   await page.evaluate(k=>k==='quality'?openQualityCreate('compare'):openGatewayCreate(k==='comparison'?'compare':'single'),kind);
   await page.locator('#config.mobile-open').waitFor();
   await page.waitForFunction(()=>Math.abs(document.querySelector('#config').getBoundingClientRect().right-innerWidth)<1);
   const drawer=await bounds(page,'#config');near(drawer.right,width,'right drawer edge');near(drawer.top,0,'drawer top');near(drawer.height,1080,'full-height drawer');
   await checkOverflow(page,'#config,#comparisonFields,.group-row,.quality-resource');
   if(kind==='comparison'&&[1920,390].includes(width))await screenshot(page,`drawer-${width}`);
   await page.getByRole('button',{name:'关闭新建测试',exact:true}).click();
  }
  const create=page.locator('#recordCreateButton');
  await page.keyboard.press('Tab');await create.focus();
  assert.equal(await create.evaluate(el=>getComputedStyle(el).outlineStyle),'solid','keyboard focus outline');
  if(width===1920){
   await page.mouse.move(0,0);const normal=await create.evaluate(el=>getComputedStyle(el).backgroundColor);
   await create.hover();await page.waitForTimeout(200);const hover=await create.evaluate(el=>getComputedStyle(el).backgroundColor);assert.notEqual(normal,hover,'hover feedback');
   await create.evaluate(el=>el.disabled=true);assert.equal(await create.evaluate(el=>getComputedStyle(el).cursor),'not-allowed','disabled feedback');await create.evaluate(el=>el.disabled=false);
   assert.equal(await page.locator('.record-create-button:visible,.mobile-config:visible').count(),1,'single primary creation action');
  }
  const visibleIcons=await page.locator('.module-icon .ui-icon').evaluateAll(els=>els.map(el=>({stroke:getComputedStyle(el).strokeWidth,fill:getComputedStyle(el).fill})));assert.ok(visibleIcons.every(x=>x.stroke==='1.8px'&&x.fill==='none'));
  assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth),false,'page horizontal overflow');
  results.push({width,single,comparison,overview:{progress:progress.height,focus:focus.height}});
  await page.close();
 }
 assert.deepEqual(errors,[],'page errors');assert.deepEqual(unexpected,[],'unexpected requests');
 fs.writeFileSync(path.join(output,'measurements.json'),JSON.stringify({results,errors,unexpected},null,2));
 console.log(`PASS: ${results.length} viewport widths; single/comparison/groups/quality, shadows, spacing, record switch, refresh, drawers, icons.\nArtifacts: ${output}`);
 }finally{await browser.close()}
})().catch(e=>{console.error(e);process.exitCode=1});
