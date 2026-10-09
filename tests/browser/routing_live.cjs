/** Live routing report regression; APIs and request outcomes are synthetic. */
'use strict';
const {chromium}=require('playwright');
const assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path');
const {projectRoot:root,output}=require('./artifacts.cjs').browserArtifacts('routing_live');
const html=fs.readFileSync(path.join(root,'static/index.html'),'utf8');
function rate(successes,total){return {successes,total,pct:total?100*successes/total:null}}
function observation(successes,total){return {requests:total,success:rate(successes,total),cache:rate(0,0),ttft_p95_ms:120,total_p95_ms:450,stability:{max_consecutive_failures:total?total-successes:null,ttft_ewma_max_ms:successes>=5?120:null},diagnostics:{http_2xx:successes,complete_2xx:successes,incomplete_2xx:0,failures:total-successes,failure_types:total>successes?{HTTP_429:total-successes}:{},status_codes:{200:successes,...(total>successes?{429:total-successes}:{})}}}}
function run(successes=2,total=4){const baseline=observation(total,total),candidate=observation(successes,total);return {id:'live-route',name:'合成路由实时观测',status:'running',tier:1000,rpm:600,concurrency:{baseline:20,candidate:20},progress:{done:total*2,pct:total/1000},sides:{baseline:{name:'固定路由',model:'synthetic-model',url:'http://example.test/v1',records:total},candidate:{name:'智能路由',model:'synthetic-model',url:'http://example.test/v1',records:total}},config:{capture_raw:false},cache_isolation:{baseline:'000000000001',candidate:'000000000002'},report:null,live_report:{baseline,candidate,success:{baseline:baseline.success,candidate:candidate.success,delta_pct:candidate.success.pct-100},cache:{baseline:baseline.cache,candidate:candidate.cache,delta_pct:null,baseline_no_usage:total,candidate_no_usage:total},ttft:{delta_pct:0},total_latency:{delta_pct:0},partial:true},limits:[]}}
(async()=>{
 const chrome='/Applications/Google Chrome.app/Contents/MacOS/Google Chrome';
 const browser=await chromium.launch({headless:true,...(process.env.CHROME_PATH||fs.existsSync(chrome)?{executablePath:process.env.CHROME_PATH||chrome}:{})});
 const errors=[];
 try{
  for(const width of [1440,980,390]){
   const context=await browser.newContext({viewport:{width,height:1000},reducedMotion:'reduce'}),page=await context.newPage();
   let current=run(),polls=0;const pageRequests=[];
   page.on('pageerror',error=>errors.push(error.message));
   await context.route('**/*',async route=>{
    if(await require('./assets.cjs').serveAsset(route,root))return;
    const url=new URL(route.request().url());assert.equal(url.hostname,'127.0.0.1','no upstream requests');
    if(url.pathname==='/')return route.fulfill({contentType:'text/html',body:html});
    let data={};
    if(url.pathname==='/auth/status')data={authenticated:true,user:{username:'synthetic',role:'user'}};
    else if(url.pathname==='/tasks'){polls++;data={tasks:[],comparisons:[],quality_tests:[],routing_comparisons:[current]}}
    else if(url.pathname==='/config')data={config:{}};
    else if(url.pathname==='/routing-config')data={sides:{}};
    else if(url.pathname==='/templates')data={templates:[]};
    else if(url.pathname==='/routing-corpus')data={tiers:[{tier:1000,note:'合成题集'}]};
    else if(url.pathname==='/routing-comparison/live-route/requests'){
     const side=url.searchParams.get('side'),offset=Number(url.searchParams.get('offset')),limit=Number(url.searchParams.get('limit'));
     const stat=(current.live_report||current.report)[side],total=stat.requests;pageRequests.push({side,offset,total});
     const items=Array.from({length:Math.max(0,Math.min(limit,total-offset))},(_,i)=>{const index=offset+i,ok=index>=total-stat.success.successes;return {request_index:index,sequence:index+1,ok,status_code:ok?200:429,completed:ok,reason:ok?'完整成功响应':'请求被限流',ttft_ms:ok?120:null,total_ms:450,usage_seen:ok,prompt_tokens:ok?100:null,cached_tokens:ok?20:null,completion_tokens:ok?10:null,total_tokens:ok?110:null,cache_hit:ok}});
     data={run_id:current.id,side,offset,limit,total,recorded_total:total,items,has_more:offset+items.length<total,available:true,reason:''};
    }
    await route.fulfill({contentType:'application/json',body:JSON.stringify(data)});
   });
   await page.goto('http://127.0.0.1:18821/');
   await page.waitForFunction(()=>refreshTimer!==null&&!refreshing);
   await page.locator('[data-module="routing"]').click();
   const metrics=page.locator('.report-section').filter({has:page.getByRole('heading',{name:'两侧实时指标',exact:true})});
   await metrics.waitFor();
   const successRow=metrics.locator('tbody tr').filter({has:page.getByText('成功率',{exact:true})});
   assert.equal(await successRow.locator('td').nth(2).innerText(),'50%');
   const failuresRow=metrics.locator('tbody tr').filter({has:page.getByText('最大连续失败次数',{exact:true})});
   const ewmaRow=metrics.locator('tbody tr').filter({has:page.getByText('TTFT（首字耗时）最大 EWMA（指数加权移动平均）',{exact:true})});
   assert.equal(await failuresRow.locator('td').nth(2).innerText(),'2 次');
   assert.equal(await ewmaRow.locator('td').nth(2).innerText(),'—','fewer than five valid samples');
   assert.match(await page.locator('#modulePlaceholder').innerText(),/429（请求限流）/);
   assert.equal(await page.getByText('可归因于路由',{exact:false}).count(),0,'live statistics have no final attribution');
   const previousPolls=polls;current=run(8,10);
   await page.waitForFunction(()=>document.querySelector('#modulePlaceholder').textContent.includes('80%'),{},{timeout:6000});
   assert.ok(polls>previousPolls,'polling updates metrics without manual refresh');
   assert.equal(await successRow.locator('td').nth(2).innerText(),'80%');
   assert.equal(await ewmaRow.locator('td').nth(2).innerText(),'120 ms（毫秒）');
   await page.evaluate(()=>clearInterval(refreshTimer));
   for(const theme of ['light','dark']){
    if(theme==='dark')await page.locator('#themeToggle').click();
    const overflowing=await page.locator('.main,.report-shell,.report-head,.report-tabs,.report-section').evaluateAll(nodes=>nodes.filter(n=>n.scrollWidth>n.clientWidth+1).map(n=>n.className));
    assert.deepEqual(overflowing,[],`${theme} ${width}: no horizontal overflow`);
    await page.locator('.main').evaluate(el=>el.scrollTop=0);
    await page.screenshot({path:path.join(output,`routing-live-${width}-${theme}.png`)});
    await metrics.screenshot({path:path.join(output,`routing-metrics-${width}-${theme}.png`)});
    const metricTable=metrics.locator('.table-wrap');await metricTable.evaluate(el=>el.scrollTop=el.scrollHeight);
    await metricTable.screenshot({path:path.join(output,`routing-stability-${width}-${theme}.png`)});
   }
   await page.getByRole('tab',{name:'智能路由',exact:true}).click();
   assert.match(await page.locator('#modulePlaceholder').innerText(),/80/);
   assert.match(await page.locator('#modulePlaceholder').innerText(),/429（请求限流）/);
   let requests=page.locator('.report-section').filter({has:page.getByRole('heading',{name:'逐条请求统计',exact:true})});
   await requests.locator('tbody tr').first().waitFor();
   await page.waitForFunction(()=>document.querySelector('#modulePlaceholder').textContent.includes('请求被限流'));
   assert.equal(await requests.locator('tbody tr').count(),10);
   assert.match(await requests.locator('tbody tr').first().innerText(),/失败.*请求被限流/);
   assert.match(await requests.locator('tbody tr').last().innerText(),/成功/);
   current=run(100,125);await page.evaluate(()=>refresh());
   await page.waitForFunction(()=>document.querySelector('#modulePlaceholder').textContent.includes('/ 125 条'));
   assert.equal(await requests.locator('tbody tr').count(),50);
   await requests.getByRole('button',{name:'下一页',exact:true}).click();
   await page.waitForFunction(()=>document.querySelector('#modulePlaceholder').textContent.includes('当前 51–100'));
   assert.equal(await requests.locator('tbody tr').first().locator('td').first().innerText(),'51');
   await requests.getByRole('button',{name:'下一页',exact:true}).click();
   await page.waitForFunction(()=>document.querySelector('#modulePlaceholder').textContent.includes('当前 101–125'));
   assert.equal(await requests.locator('tbody tr').count(),25);
   assert.equal(await requests.getByRole('button',{name:'下一页',exact:true}).isDisabled(),true);
   current=run(101,126);await page.evaluate(()=>refresh());
   await page.waitForFunction(()=>document.querySelector('#modulePlaceholder').textContent.includes('当前 101–126'));
   assert.equal(await requests.locator('tbody tr').count(),26,'live updates preserve selected page');
   await requests.getByRole('button',{name:'上一页',exact:true}).click();
   await page.waitForFunction(()=>document.querySelector('#modulePlaceholder').textContent.includes('当前 51–100'));
   await page.getByRole('tab',{name:'固定路由',exact:true}).click();
   await page.waitForFunction(()=>document.querySelector('#modulePlaceholder').textContent.includes('当前 1–50'));
   assert.equal(await requests.locator('tbody tr').first().locator('td').first().innerText(),'1','each side has independent pagination');
   assert.match(await requests.locator('tbody tr').first().innerText(),/成功/);
   assert.ok(pageRequests.some(request=>request.side==='candidate'&&request.offset===100),'all request pages are reachable');
   for(const theme of ['dark','light']){
    if(theme==='light')await page.locator('#themeToggle').click();
    const overflowing=await page.locator('.main,.report-shell,.report-head,.report-tabs,.report-section').evaluateAll(nodes=>nodes.filter(n=>n.scrollWidth>n.clientWidth+1).map(n=>n.className));
    assert.deepEqual(overflowing,[],`${theme} ${width}: request table overflow is contained`);
    await requests.screenshot({path:path.join(output,`routing-requests-${width}-${theme}.png`)});
   }
   await page.getByRole('tab',{name:'对照结论',exact:true}).click();
   current={...run(0,0),status:'waiting'};await page.evaluate(()=>refresh());
   const waitingRow=page.locator('.report-section').filter({has:page.getByRole('heading',{name:'两侧实时指标',exact:true})}).locator('tbody tr').filter({has:page.getByText('成功率',{exact:true})});
   assert.equal(await waitingRow.locator('td').nth(2).innerText(),'—','no samples do not become zero success');
   current={...run(8,10),status:'finished',report:{...run(8,10).live_report,success:{...run(8,10).live_report.success,attribution:'not_attributable',note:'合成统计不足'},cache:{...run(8,10).live_report.cache,attribution:'insufficient_evidence'},reliability:{paired_total:10,unpaired_count:0,mcnemar:{discordant:2,p_value:0.5}}}};
   await page.evaluate(()=>refresh());
   await page.getByRole('heading',{name:'两侧指标对照',exact:true}).waitFor();
   assert.match(await page.locator('#modulePlaceholder').innerText(),/不足以归因于路由/);
   current={...current,live_report:null};await page.evaluate(()=>refresh());
   assert.match(await page.locator('#modulePlaceholder').innerText(),/429（请求限流）/,'final diagnosis remains available after history restore');
   const historic=JSON.parse(JSON.stringify(current));delete historic.report.baseline.stability;delete historic.report.candidate.stability;
   historic.derived_report={stability:{source:'result_files',baseline:{max_consecutive_failures:0,ttft_ewma_max_ms:150},candidate:{max_consecutive_failures:3,ttft_ewma_max_ms:250}}};current=historic;await page.evaluate(()=>refresh());
   const historicRows=page.locator('.report-section').filter({has:page.getByRole('heading',{name:'两侧指标对照',exact:true})}).locator('tbody tr');
   assert.equal(await historicRows.filter({has:page.getByText('最大连续失败次数',{exact:true})}).locator('td').nth(2).innerText(),'3 次');
   assert.match(await page.locator('#modulePlaceholder').innerText(),/新指标根据原始结果文件补算/);
   await context.close();
  }
  assert.deepEqual(errors,[]);
  console.log(JSON.stringify({passed:true,viewports:[1440,980,390],themes:['light','dark'],checks:['automatic polling','live success denominator','429 diagnosis','sequence metrics','side tabs','full request pagination','live selected page updates','independent side pages','historical derived metrics','empty samples','final report','responsive layout'],output}));
 }finally{await browser.close()}
})().catch(error=>{console.error(error);process.exitCode=1});
