/** Shared time evidence in every module; synthetic responses only, no upstream or user data. */
'use strict';
const {chromium}=require('playwright'),assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path'),{execFileSync}=require('node:child_process');
const {projectRoot:root,output,env}=require('./artifacts.cjs').browserArtifacts('timing');
execFileSync(process.env.PYTHON||path.join(root,'.venv/bin/python'),[path.join(root,'tests/browser/make_fixture.py'),output],{cwd:root,env});
const fixture=JSON.parse(fs.readFileSync(path.join(output,'fixture.json'),'utf8')),html=fs.readFileSync(path.join(output,'index.html'),'utf8');
const taskTiming={version:1,clock:'client',timezone:'Asia/Singapore',source:'recorded',created_at:'2026-10-15T23:59:58.123+08:00',started_at:'2026-10-15T15:59:59.999Z',dispatch_ended_at:'2026-10-15T16:00:00.000Z',ended_at:'2026-10-15T16:00:01.234Z',queue_ms:1876,elapsed_ms:1235,drain_ms:1234};
const requestTiming={version:1,clock:'client',timezone:'Asia/Singapore',source:'recorded',scheduled_at:'2026-10-15T15:59:59.999Z',enqueued_at:'2026-10-15T15:59:59.999Z',started_at:'2026-10-15T15:59:59.999Z',first_token_at:'2026-10-15T16:00:00.000Z',ended_at:'2026-10-15T16:00:00.001Z',queue_ms:0,schedule_lag_ms:0,ttft_ms:1,total_ms:2};
const attempts=[{attempt_index:1,status_code:400,error_type:'unsupported_parameter',timing:{...requestTiming,started_at:'2026-10-15T15:59:58.100Z',first_token_at:null,ended_at:'2026-10-15T15:59:58.200Z',ttft_ms:null,total_ms:100}},{attempt_index:2,status_code:200,error_type:null,timing:requestTiming}];
for(const run of [...fixture.tasks,...fixture.comparisons,...fixture.quality_tests])run.timing=taskTiming;
for(const run of fixture.tasks)for(const row of run.recent){row.timing=requestTiming;row.attempts=attempts;}
for(const run of fixture.comparisons)for(const group of run.groups){group.timing=taskTiming;group.summary.timing=taskTiming;for(const row of group.summary.recent)row.timing=requestTiming;}
for(const run of fixture.quality_tests)for(const resource of run.resources)for(const row of resource.case_results)row.timing=requestTiming;
const routing={id:'routing-timing',name:'路由时间核查',status:'finished',timing:taskTiming,tier:1,rpm:100,concurrency:{baseline:20,candidate:20},sides:{baseline:{name:'固定路由',model:'synthetic',records:1},candidate:{name:'智能路由',model:'synthetic',records:1}},progress:{pct:1,done:2},config:{capture_raw:false},report:null};
fixture.routing_comparisons=[routing];
const fingerprint={id:'fingerprint-timing',name:'指纹时间核查',mode:'selfcheck',status:'finished',timing:taskTiming,endpoint:{model:'synthetic'},protocol:{cells:['color'],samples_per_cell:1,concurrency:20,rpm:100},progress:{done:1,total:1,valid:1,errors:0},artifact:{complete:true,cells:{color:{counts:{blue:1},valid_count:1,total_count:1}},diagnostics:{valid_rate:1,split_half:{warning:true}}}};
fixture.fingerprint_runs=[fingerprint];
const tpm={id:'tpm-timing',phase:'finished',status:'finished',created:'2030-01-01T00:00:00.000Z',timing:{...taskTiming,created_at:null},config:{name:'TPM 时间核查',model:'synthetic',target_tpm:200000000,cache_mode:'prefer_cache',budget_usd:80},recent:[{id:1,phase:'formal',stage_index:4,success:true,timing:requestTiming,usage:{complete:true,full_input_tokens:49500,output_tokens:500}}],metrics:{started:1},report:{primary_window:{valid:false},verdict:'insufficient_evidence'},fees:{known_cost_usd:0,reserved_cost_usd:0}};
const errors=[],unexpected=[],checks=[];let browser;
(async()=>{
 const chrome='/Applications/Google Chrome.app/Contents/MacOS/Google Chrome';
 browser=await chromium.launch({headless:true,...(process.env.CHROME_PATH||fs.existsSync(chrome)?{executablePath:process.env.CHROME_PATH||chrome}:{})});
 const context=await browser.newContext({viewport:{width:1440,height:1000},reducedMotion:'reduce',timezoneId:'Pacific/Honolulu'}),page=await context.newPage();
 page.on('pageerror',e=>errors.push(e.message));
 await context.route('**/*',async route=>{
  if(await require('./assets.cjs').serveAsset(route,root))return;
  const url=new URL(route.request().url()),p=url.pathname;let data={};
  if(url.hostname!=='127.0.0.1'){unexpected.push(route.request().url());return route.abort()}
  if(p==='/')return route.fulfill({contentType:'text/html',body:html});
  if(p==='/auth/status')data={authenticated:true,user:{username:'时间回归',role:'user'}};
  else if(p==='/tasks')data=fixture;
  else if(p==='/config')data={config:{}};
  else if(p==='/connections')data={connections:[]};
  else if(p==='/templates')data={templates:[]};
  else if(p==='/routing-corpus')data={tiers:[],metrics:[]};
  else if(p==='/quality-reference-records')data={records:[]};
  else if(p==='/quality-suite')data={manifest:{title:'合成题集',case_count:2},cases:[]};
  else if(p==='/fingerprint/catalog')data={presets:[],probes:[]};
  else if(p==='/fingerprint/references')data={references:[]};
  else if(p==='/fingerprints/fingerprint-timing')data=fingerprint;
  else if(p==='/fingerprints/fingerprint-timing/samples')data={samples:[{index:0,cell_id:'color',category:'valid',normalized:'blue',raw:'blue',latency_ms:2,timing:requestTiming}],offset:0,total:1,limit:50};
  else if(p==='/tpm/runs')data={runs:[tpm]};
  else if(p==='/tpm/runs/tpm-timing')data={run:tpm};
  else if(p==='/tpm/runs/tpm-timing/live')data={...tpm,recent_requests_view:'display-v1'};
  else if(p.startsWith('/routing-comparison/')&&p.endsWith('/requests'))data={items:[{sequence:1,ok:true,status_code:200,ttft_ms:1,total_ms:2,timing:requestTiming,attempts}],total:1,offset:0,has_more:false};
  else if(p==='/favicon.ico')data={};
  else {unexpected.push(p);return route.fulfill({status:404,json:{error:'Synthetic route missing'}})}
  return route.fulfill({json:data});
 });
 await page.goto('http://127.0.0.1:18775/');await page.waitForFunction(()=>authenticated&&refreshTimer!==null&&!refreshing);await page.evaluate(()=>clearInterval(refreshTimer));
 async function evidence(module,taskKey,requestKey){
  const task=page.locator(`[data-time-key="${taskKey}"]`).first();
  await task.waitFor();await task.locator('summary').click();assert.match(await task.innerText(),/2026-10-15 23:59:59.999 新加坡时间/);assert.match(await task.innerText(),/2026-10-16 00:00:01.234 新加坡时间/);await task.locator('summary').click();
  if(module==='fingerprint')await page.locator('[data-click-action="showFingerprintSample"]').first().click();
  const request=page.locator(`[data-time-key="${requestKey}"]`).first();await request.waitFor();
  await request.locator('summary').click();
  assert.match(await request.innerText(),/实际开始：2026-10-15 23:59:59.999 新加坡时间/);
  assert.match(await request.innerText(),/请求结束：2026-10-16 00:00:00.001 新加坡时间/);
  assert.match(await request.innerText(),/排队等待：0 毫秒/);assert.match(await request.innerText(),/新加坡时间（UTC\+08:00）/);assert.doesNotMatch(await request.innerText(),/协调世界时/);
  if(module==='gateway'||module==='routing'){assert.match(await request.locator('summary').innerText(),/2 次实际发送/);assert.match(await request.innerText(),/第 1 次实际发送/);assert.match(await request.innerText(),/23:59:58.100 新加坡时间/);assert.match(await request.innerText(),/状态码：400/);assert.match(await request.innerText(),/第 2 次实际发送/);assert.match(await request.innerText(),/状态码：200/);}
  for(const width of [1440,980,390]){await page.setViewportSize({width,height:1000});for(const theme of ['light','dark']){
   if(module==='fingerprint')await page.locator('#fpModalFooter [data-fp-action="close-modal"]').click();
   await page.evaluate(value=>setTheme(value),theme);await task.locator('summary').click();await task.scrollIntoViewIfNeeded();
   await page.screenshot({path:path.join(output,`${module}-task-${width}-${theme}.png`)});await task.locator('summary').click();
   if(module==='fingerprint'){await page.locator('[data-click-action="showFingerprintSample"]').first().click();await request.locator('summary').click()}
   await request.scrollIntoViewIfNeeded();
   assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth+1),false,module+' page overflow');
   const overflow=await page.locator('.main,.report-shell,.report-head,.report-section').evaluateAll(nodes=>nodes.filter(node=>node.getClientRects().length&&node.scrollWidth>node.clientWidth+1).map(node=>node.className));
   assert.deepEqual(overflow,[],module+' section overflow');
   await page.screenshot({path:path.join(output,`${module}-${width}-${theme}.png`)});checks.push({module,width,theme});
  }}
  if(module==='fingerprint')await page.locator('#fpModalFooter [data-fp-action="close-modal"]').click();
  await page.setViewportSize({width:1440,height:1000});await page.evaluate(()=>setTheme('light'));
 }
 await evidence('gateway','task:single-fixture','request:single-fixture:1');
 await page.evaluate(()=>render(window.currentDetail));assert.equal(await page.locator('[data-time-key="request:single-fixture:1"]').evaluate(node=>node.open),true,'request expansion survives rerender');
 await page.locator('#gatewaySubnav [data-view="compare"]').click();await page.getByRole('tab',{name:/基准组 ·/}).click();
 await evidence('comparison','comparison:compare-fixture','group-request:base:1');
 await page.locator('[data-module="routing"]').click();await page.getByRole('tab',{name:'固定路由',exact:true}).click();
 await evidence('routing','routing:routing-timing','routing-request:routing-timing:baseline:1');
 await page.locator('[data-module="quality"]').click();await page.locator('#qualitySubnav [data-view="compare"]').click();await page.getByRole('tab',{name:/资源 A · 逐题/}).click();
 await evidence('quality','quality:quality-fixture','quality-request:quality-fixture:0:basic_ping');
 await page.locator('[data-click-action="showQualityCase"]').first().click();assert.match(await page.locator('#qualityEvidenceBody').innerHTML(),/quality-evidence:quality-fixture:0:basic_ping/);await page.locator('#qualityEvidenceModal .modal-close').click();
 await page.locator('[data-module="fingerprint"]').click();await page.evaluate(()=>FingerprintUI.select('fingerprint-timing'));await page.evaluate(()=>FingerprintUI.setTab('samples'));
 await evidence('fingerprint','fingerprint:fingerprint-timing','fingerprint-sample:fingerprint-timing:0');
 await page.locator('[data-module="tpm"]').click();await page.evaluate(()=>TpmUI.select('tpm-timing'));await page.evaluate(()=>TpmUI.setTab('requests'));
 await evidence('tpm','tpm:tpm-timing','tpm-request:tpm-timing:1');
 const tpmRecordMeta=await page.locator('#tpmList .task.sel .task-meta').innerText();
 assert.match(tpmRecordMeta,/(?:^| · )未记录$/,'missing recorded creation remains explicitly unknown in record metadata');
 assert.doesNotMatch(tpmRecordMeta,/2030-01-01|2030/,'missing recorded creation cannot fall back to restore-time compatibility field');
 assert.equal(await page.locator('#tpmList .task-pct').count(),0,'TPM records do not invent a progress track to hold their creation date');
 assert.deepEqual(errors,[]);assert.deepEqual(unexpected,[]);fs.writeFileSync(path.join(output,'timing-results.json'),JSON.stringify({checks,errors,unexpected,timezone:'Pacific/Honolulu',displayTimezone:'Asia/Singapore',fixedOffset:'+08:00',synthetic:true},null,2));
 console.log(`PASS: ${checks.length*2} task/request time-evidence screenshots across six modules; fixed Singapore UTC+08:00 across midnight, Honolulu browser, per-request timing without raw capture, zero wait, polling persistence. Artifacts: ${output}`);
})().catch(error=>{console.error(error);process.exitCode=1}).finally(async()=>{await browser?.close()});
