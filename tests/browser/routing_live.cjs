/** Live routing report regression; APIs and request outcomes are synthetic. */
'use strict';
const {chromium}=require('playwright');
const assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path');
const {projectRoot:root,output}=require('./artifacts.cjs').browserArtifacts('routing_live');
const html=fs.readFileSync(path.join(root,'static/index.html'),'utf8');
function rate(successes,total){return {successes,total,pct:total?100*successes/total:null}}
function observation(successes,total){return {requests:total,success:rate(successes,total),cache:rate(0,0),ttft_p95_ms:120,total_p95_ms:450,stability:{max_consecutive_failures:total?total-successes:null,ttft_ewma_max_ms:successes>=5?120:null},diagnostics:{http_2xx:successes,complete_2xx:successes,incomplete_2xx:0,failures:total-successes,failure_types:total>successes?{HTTP_429:total-successes}:{},status_codes:{200:successes,...(total>successes?{429:total-successes}:{})}}}}
function run(successes=2,total=4){const baseline=observation(total,total),candidate=observation(successes,total);return {id:'live-route',name:'合成路由实时观测',status:'running',tier:1000,rpm:600,concurrency:{baseline:20,candidate:20},progress:{done:total*2,pct:total/1000},sides:{baseline:{name:'固定路由',model:'synthetic-model',url:'http://example.test/v1',records:total},candidate:{name:'智能路由',model:'synthetic-model',url:'http://example.test/v1',records:total}},config:{capture_raw:false},cache_isolation:{baseline:'000000000001',candidate:'000000000002'},report:null,live_report:{baseline,candidate,success:{baseline:baseline.success,candidate:candidate.success,delta_pct:candidate.success.pct-100},cache:{baseline:baseline.cache,candidate:candidate.cache,delta_pct:null,baseline_no_usage:total,candidate_no_usage:total},ttft:{delta_pct:0},total_latency:{delta_pct:0},partial:true},limits:[]}}
function finishedRun(){
 const record=run(8,10),report=record.live_report;
 return {...record,name:'合成路由对照结果',status:'finished',tier:10,progress:{done:20,pct:1},corpus:{burst_count:2,burst_size:5,item_id_digest:'synthetic-manifest',id:'synthetic-v1'},report:{...report,partial:false,
  success:{...report.success,baseline:{...report.success.baseline,ci_low_pct:72.25,ci_high_pct:100},candidate:{...report.success.candidate,ci_low_pct:49.02,ci_high_pct:94.33},ci_overlap:true,p_value:0.473684,attribution:'not_attributable',note:'合成统计不足：两侧区间重叠，本次观测无法将成功率差异归因于路由。',mcnemar_supports:null},
  cache:{...report.cache,attribution:'insufficient_evidence',note:'合成缓存证据不足：没有可统计用量，命中率不可比。'},
  reliability:{paired_total:9,unpaired_count:2,unpaired_item_ids:['synthetic-missing-a','synthetic-missing-b'],note:'合成未配对请求仅用于排除验证。',mcnemar:{discordant:2,p_value:0.5}},
  limitations:['结果仅限于本次清单与时段。']}};
}
async function captureTheme(page,theme){
 if(await page.locator('html').getAttribute('data-theme')!==theme)await page.locator('#themeToggle').click();
 await page.waitForFunction(expected=>document.documentElement.dataset.theme===expected,theme);
 await page.evaluate(()=>new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(resolve))));
}
async function captureReadingSection(page,section,file,width){
 if(width!==390)return section.screenshot({path:path.join(output,file)});
 // Tall elements inside the scrolling detail panel cannot be captured as one
 // reliable element screenshot. Capture the actual start/end reading viewports.
 await section.evaluate(el=>{const scroller=document.querySelector('.shell');scroller.scrollTop+=el.getBoundingClientRect().top-scroller.getBoundingClientRect().top});
 const start=await section.boundingBox();
 assert.ok(start.y>=0&&start.y<page.viewportSize().height/2,'mobile reading screenshot starts at the requested section');
 await page.screenshot({path:path.join(output,file)});
 await section.evaluate(el=>{const scroller=document.querySelector('.shell');scroller.scrollTop+=el.getBoundingClientRect().bottom-scroller.getBoundingClientRect().bottom});
 await page.screenshot({path:path.join(output,file.replace('.png','-end.png'))});
}
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
   assert.equal(await page.locator('.report-head [data-click-action="stopRouting"]').count(),1,'stop is accessible in the header');
   assert.equal(await page.locator('[data-click-action="deleteRouting"]').count(),0,'no deletion during running');
   const successRow=metrics.locator('tbody tr').filter({has:page.getByText('成功率',{exact:true})});
   assert.equal(await successRow.locator('td').nth(2).innerText(),'50%');
   const failuresRow=metrics.locator('tbody tr').filter({has:page.getByText('最大连续失败次数',{exact:true})});
   const ewmaRow=metrics.locator('tbody tr').filter({has:page.getByText('最大平滑首字耗时',{exact:true})});
   assert.equal(await failuresRow.locator('td').nth(2).innerText(),'2 次');
   assert.equal(await ewmaRow.locator('td').nth(2).innerText(),'—','fewer than five valid samples');
   assert.match(await page.locator('#modulePlaceholder').innerText(),/429（请求限流）/);
   assert.equal(await page.getByText('可归因于路由',{exact:false}).count(),0,'live statistics have no final attribution');
   const previousPolls=polls;current=run(8,10);
   await page.waitForFunction(()=>document.querySelector('#modulePlaceholder').textContent.includes('80%'),{},{timeout:6000});
   assert.ok(polls>previousPolls,'polling updates metrics without manual refresh');
   assert.equal(await successRow.locator('td').nth(2).innerText(),'80%');
   assert.equal(await ewmaRow.locator('td').nth(2).innerText(),'120 毫秒');
   assert.equal(await metrics.locator('.report-metric').count(),0,'comparison uses one table without duplicate metric cards');
   assert.equal(await metrics.locator('.method-card:visible').count(),0,'calculation details are initially collapsed');
   await page.evaluate(()=>clearInterval(refreshTimer));
   for(const theme of ['light','dark']){
    await captureTheme(page,theme);
    const overflowing=await page.locator('.main,.report-shell,.report-head,.report-tabs,.report-section').evaluateAll(nodes=>nodes.filter(n=>n.scrollWidth>n.clientWidth+1).map(n=>n.className));
    assert.deepEqual(overflowing,[],`${theme} ${width}: no horizontal overflow`);
    await page.locator('.main,.shell').evaluateAll(elements=>elements.forEach(el=>el.scrollTop=0));
    await page.screenshot({path:path.join(output,`routing-live-${width}-${theme}.png`)});
    await captureReadingSection(page,metrics,`routing-metrics-${width}-${theme}.png`,width);
    const metricTable=metrics.locator('.table-wrap');
    if(width===390){await metrics.locator('tbody tr').last().scrollIntoViewIfNeeded();await page.screenshot({path:path.join(output,`routing-stability-${width}-${theme}.png`)})}
    else{await metricTable.evaluate(el=>el.scrollTop=el.scrollHeight);await metricTable.screenshot({path:path.join(output,`routing-stability-${width}-${theme}.png`)})}
   }
   await page.getByRole('tab',{name:'智能路由',exact:true}).click();
   assert.match(await page.locator('#modulePlaceholder').innerText(),/80/);
   assert.match(await page.locator('#modulePlaceholder').innerText(),/429（请求限流）/);
   let requests=page.locator('.report-section').filter({has:page.getByRole('heading',{name:'逐条请求统计',exact:true})});
   await requests.locator('tbody tr').first().waitFor();
   await page.waitForFunction(()=>document.querySelector('#modulePlaceholder').textContent.includes('请求被限流'));
   assert.equal(await requests.locator('tbody tr').count(),10);
   assert.equal(await requests.locator('th').count(),7,'related fields are grouped without losing token categories');
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
    await captureTheme(page,theme);
    const overflowing=await page.locator('.main,.report-shell,.report-head,.report-tabs,.report-section').evaluateAll(nodes=>nodes.filter(n=>n.scrollWidth>n.clientWidth+1).map(n=>n.className));
    assert.deepEqual(overflowing,[],`${theme} ${width}: request table overflow is contained`);
    await requests.screenshot({path:path.join(output,`routing-requests-${width}-${theme}.png`)});
   }
   await page.getByRole('tab',{name:'对照结论',exact:true}).click();
   current={...run(0,0),status:'stopping'};await page.evaluate(()=>refresh());
   assert.equal(await page.getByRole('button',{name:'正在收尾…'}).isDisabled(),true,'stopping cannot send duplicate stop');
   current={...run(0,0),status:'waiting'};await page.evaluate(()=>refresh());
   const waitingRow=page.locator('.report-section').filter({has:page.getByRole('heading',{name:'两侧实时指标',exact:true})}).locator('tbody tr').filter({has:page.getByText('成功率',{exact:true})});
   assert.equal(await waitingRow.locator('td').nth(2).innerText(),'—','no samples do not become zero success');
   assert.match(await page.locator('#modulePlaceholder').innerText(),/尚无返回结果，暂不能判断失败情况/,'empty outcomes never imply zero failures');
   current=finishedRun();
   await page.evaluate(()=>refresh());
   await page.getByRole('heading',{name:'两侧指标对照',exact:true}).waitFor();
   const conclusion=page.locator('.report-section').filter({has:page.getByRole('heading',{name:'归因结论',exact:true})});
   const finalMetrics=page.locator('.report-section').filter({has:page.getByRole('heading',{name:'两侧指标对照',exact:true})});
   const statistics=page.locator('#routing-statistics-live-route'),method=page.locator('#routing-method-live-route');
   const disclosures=['routing-metric-method-live-route','routing-diagnostics-live-route-both','routing-statistics-live-route','routing-method-live-route'];
   const finishedText=await page.locator('#modulePlaceholder').innerText();
   assert.match(finishedText,/成功率：不足以归因于路由/);
   assert.match(finishedText,/缓存命中率：证据不足/);
   assert.match(finishedText,/至少一侧缺少可统计用量，命中率不可比/,'unavailable cache attribution explains the missing evidence concisely');
   assert.match(finishedText,/95% 置信区间重叠.*p 值 0\.473684/,'statistical boundary remains visible');
   assert.match(finishedText,/未配对 2 条已剔除/,'excluded pairing evidence remains visible');
   assert.match(finishedText,/429（请求限流） 2 条/,'actual failures remain visible without opening diagnostics');
   assert.match(finishedText,/缺少用量已剔除：10 \/ 10 条/,'missing usage is visible near the compared data');
   const headings=await page.locator('#modulePlaceholder .report-section-title').allTextContents();
   assert.ok(headings.indexOf('归因结论')<headings.indexOf('两侧指标对照'),'conclusion precedes compared data');
   assert.ok(headings.indexOf('两侧指标对照')<headings.indexOf('失败与响应完整性'),'failure evidence follows the direct comparison');
   assert.ok((await conclusion.innerText()).length<350,'visible conclusion is concise');
   for(const id of disclosures)assert.equal(await page.locator('#'+id).getAttribute('open'),null,`${id}: secondary evidence is initially collapsed`);
   assert.equal(await page.getByText(current.report.success.note,{exact:true}).isVisible(),false,'full statistical explanation is available on demand');
   assert.ok((await statistics.textContent()).includes(current.report.success.note),'full explanation is preserved');
   for(const theme of ['light','dark']){
    await captureTheme(page,theme);
    if(width===390){
     const tableLayout=await finalMetrics.locator('.table-wrap').evaluate(el=>({rows:el.querySelectorAll('tbody tr').length,scrollHeight:el.scrollHeight,clientHeight:el.clientHeight,lastRowBottom:el.querySelector('tbody tr:last-child').getBoundingClientRect().bottom,bottom:el.getBoundingClientRect().bottom}));
     assert.equal(tableLayout.rows,6,'all six comparison metrics are present on mobile');
     assert.ok(tableLayout.scrollHeight<=tableLayout.clientHeight+1,'mobile summary table expands with the page instead of nested vertical scrolling');
     assert.ok(tableLayout.lastRowBottom<=tableLayout.bottom+1,'the sixth metric is not clipped by the table container');
    }
    await page.locator('.main,.shell').evaluateAll(elements=>elements.forEach(el=>el.scrollTop=0));
    await page.screenshot({path:path.join(output,`routing-finished-${width}-${theme}.png`)});
    await captureReadingSection(page,finalMetrics,`routing-finished-metrics-${width}-${theme}.png`,width);
   }
   for(const id of disclosures)await page.locator('#'+id+' summary').click();
   await page.evaluate(()=>refresh());
   for(const id of disclosures)assert.notEqual(await page.locator('#'+id).getAttribute('open'),null,`${id}: refresh preserves reader expansion`);
   assert.equal(await page.getByText(current.report.success.note,{exact:true}).isVisible(),true,'expanded statistics show the original explanation');
   assert.match(await method.innerText(),/跨侧、跨运行从第一个字起不同/,'cache isolation method is retained');
   assert.match(await page.locator('#routing-metric-method-live-route').innerText(),/EWMA（指数加权移动平均）.*p95（第95百分位）/,'technical names are in the metric definition');
   for(const theme of ['dark','light']){
    await captureTheme(page,theme);
    const overflowing=await page.locator('.main,.report-shell,.report-head,.report-tabs,.report-section').evaluateAll(nodes=>nodes.filter(n=>n.scrollWidth>n.clientWidth+1).map(n=>n.className));
    assert.deepEqual(overflowing,[],`${theme} ${width}: expanded evidence does not overflow`);
    await captureReadingSection(page,statistics,`routing-statistics-${width}-${theme}.png`,width);
    await captureReadingSection(page,method,`routing-method-${width}-${theme}.png`,width);
   }
   for(const id of disclosures)await page.locator('#'+id+' summary').click();
   current={...current,cache_isolation:null,report:{...current.report,cache:{...current.report.cache,contamination_suspected:true}}};
   await page.evaluate(()=>refresh());
   assert.match(await conclusion.innerText(),/未做缓存隔离/,'missing isolation is not hidden inside methodology');
   assert.match(await conclusion.innerText(),/缓存疑似被外部预热/,'contamination evidence remains visible');
   assert.equal(await method.getAttribute('open'),null,'warnings are visible while method is collapsed');
   current=finishedRun();
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
  console.log(JSON.stringify({passed:true,viewports:[1440,980,390],themes:['light','dark'],checks:['automatic polling','live success denominator','429 diagnosis','sequence metrics','side tabs','full request pagination','live selected page updates','independent side pages','historical derived metrics','empty samples','concise conclusion before comparison','visible statistical boundaries and failures','collapsed technical detail','expanded detail persists on refresh','cache attribution warnings','final and expanded report screenshots','responsive layout'],output}));
 }finally{await browser.close()}
})().catch(error=>{console.error(error);process.exitCode=1});
