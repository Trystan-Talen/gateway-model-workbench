/** Real local service, complete synthetic streams, real long request bodies; no paid upstream. */
'use strict';
const {chromium}=require('playwright'),assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path');
const artifacts=require('./artifacts.cjs').browserArtifacts('tpm'),{projectRoot:root,output}=artifacts;
artifacts.env.TPM_BROWSER_MEMORY_CREDENTIALS='1';
const servers=process.env.TPM_ACTUAL_LAUNCHER==='1'?require('./tpm_launcher.cjs').tpmLauncher(artifacts):require('./server.cjs').browserServers(artifacts,{python:process.env.PYTHON||path.join(root,'.venv/bin/python')});
let browser;
(async()=>{
 const endpoints=await servers.start(),chrome='/Applications/Google Chrome.app/Contents/MacOS/Google Chrome';
 browser=await chromium.launch({headless:true,...(process.env.CHROME_PATH||fs.existsSync(chrome)?{executablePath:process.env.CHROME_PATH||chrome}:{})});
 const context=await browser.newContext({viewport:{width:1440,height:1000},reducedMotion:'reduce'}),page=await context.newPage(),errors=[],paidPosts=[],modelPosts=[],sentinelLeaks=[];
 page.on('request',request=>{const endpoint=new URL(request.url()).pathname;if(request.method()==='POST'&&(endpoint==='/models'||endpoint.startsWith('/tpm/'))&&request.postData()?.includes('__manual__'))sentinelLeaks.push(endpoint)});
 page.on('request',request=>{if(request.method()==='POST'&&new URL(request.url()).pathname==='/models')modelPosts.push(request.postDataJSON())});
 page.on('pageerror',error=>errors.push(error.message));page.on('dialog',dialog=>dialog.accept());
 page.on('request',request=>{if(request.method()==='POST'&&/\/tpm\/runs(?:$|\/[^/]+\/start$)/.test(new URL(request.url()).pathname))paidPosts.push({path:new URL(request.url()).pathname,body:request.postDataJSON()})});
 await context.route('**/*',route=>new URL(route.request().url()).hostname==='127.0.0.1'?route.continue():route.abort());
 await page.goto(endpoints.base);await page.locator('#authUsername').fill('tpm-admin');await page.locator('#authPassword').fill('Synthetic-tpm-password-2026');await page.locator('#authConfirm').fill('Synthetic-tpm-password-2026');await page.locator('#authSubmit').click();await page.locator('#authGate').waitFor({state:'hidden'});
 const connections={};for(const [name,prefix]of [['main',''],['slow','/slow'],['new','/new'],['flaky','/flaky']]){const saved=await page.evaluate(async body=>api('/connections',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)}),{name:'合成连接 '+name,url:endpoints.upstream+prefix+'/v1',api_key:'synthetic-tpm-key-'+name});connections[name]=saved.connection.id}
 await page.locator('[data-module="tpm"]').click();await page.getByRole('heading',{name:'先确认用量，再施加压力'}).waitFor();
 const creationLayout=[];
 async function checkCreationLayout(width,theme){
  const layout=await page.evaluate(()=>{
   const pairs=[['tpm_connection_id','tpm_model'],['tpm_target_preset','tpm_cache_mode'],['tpm_target_duration_seconds','tpm_budget_usd']].map(ids=>ids.map(id=>({id,top:document.getElementById(id).closest('.field').getBoundingClientRect().top})));
   const body=document.getElementById('tpmCreateBody').getBoundingClientRect(),footer=document.querySelector('#tpmDrawer .drawer-footer').getBoundingClientRect();
   const prices=['tpm_budget_usd','tpm_price_input','tpm_price_output','tpm_price_cache_read','tpm_cache_write_price_mode'].map(id=>{const rect=document.getElementById(id).getBoundingClientRect();return {id,top:rect.top,bottom:rect.bottom}});
   return {pairs,bodyTop:body.top,footerTop:footer.top,prices};
  });
  for(const [left,right] of layout.pairs){if(width>640)assert.ok(Math.abs(left.top-right.top)<=1,`${left.id} and ${right.id} share a desktop row`);else assert.ok(left.top<right.top,`${left.id} and ${right.id} stack on mobile`)}
  if(width===1440)for(const item of layout.prices)assert.ok(item.top>=layout.bodyTop&&item.bottom<=layout.footerTop,`${item.id} is visible in the initial 1440 × 1000 drawer viewport`);
  creationLayout.push({width,theme,...layout});
 }
 async function shots(state,focusId){
  await page.evaluate(()=>{window.scrollTo(0,0);document.querySelector('.main').scrollTop=0;const drawer=document.getElementById('tpmCreateBody');if(drawer)drawer.scrollTop=0});
  for(const width of [1440,980,390]){await page.setViewportSize({width,height:1000});for(const theme of ['light','dark']){await page.evaluate(theme=>{setTheme(theme);window.scrollTo(0,0);document.querySelector('.main').scrollTop=0;const drawer=document.getElementById('tpmCreateBody');if(drawer)drawer.scrollTop=0},theme);if(focusId)await page.locator('#'+focusId).scrollIntoViewIfNeeded();if(state==='request-time'){const detail=page.locator('#tpmRequestTable tbody details').first();if(await detail.evaluate(node=>node.open))await detail.locator('summary').click();await detail.locator('summary').click();await page.waitForFunction(()=>{const detail=document.querySelector('#tpmRequestTable tbody details'),cell=detail.getBoundingClientRect(),viewport=detail.closest('.table-wrap').getBoundingClientRect();return detail.open&&cell.left>=viewport.left&&cell.right<=viewport.right})}assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth+1),false,`${state} page fits ${width}`);const overflow=await page.locator('#tpmDrawer,.report-shell,.report-head,.report-section').evaluateAll(nodes=>nodes.filter(node=>node.getClientRects().length&&node.scrollWidth>node.clientWidth+1).map(node=>node.id||node.className));assert.deepEqual(overflow,[],`${state} components fit ${width}`);if(state==='configuration')await checkCreationLayout(width,theme);if(state==='requests'||state==='request-time'){const timing=await page.locator('#tpmRequestTable tbody details').first().evaluate(node=>({summaryHeight:node.querySelector('summary').getBoundingClientRect().height,width:node.getBoundingClientRect().width,open:node.open}));assert.ok(timing.summaryHeight<=50,'request time control stays horizontal');if(timing.open)assert.ok(timing.width>=260,'expanded request time remains readable inside the horizontally scrollable table')}await page.screenshot({path:path.join(output,`${state}-${width}-${theme}.png`),animations:'disabled'});if(state==='runtime-details'){await page.locator('#tpmRuntimeDetails').evaluate(node=>node.scrollIntoView({block:'end'}));await page.screenshot({path:path.join(output,`${state}-bottom-${width}-${theme}.png`),animations:'disabled'})}}}
  await page.setViewportSize({width:1440,height:1000});await page.evaluate(()=>setTheme('light'));
 }
 // Freeze only an observed local snapshot while producing a responsive screenshot set.
 // The synthetic backend keeps running; no execution state or billing flow is changed.
 async function phaseShots(state,expectedPhase,observedSnapshot){
  const id=await page.locator('#modulePlaceholder').getAttribute('data-tpm-id');
  const snapshot=observedSnapshot||await (await context.request.get(endpoints.base+'/tpm/runs/'+id)).json();
  assert.equal(snapshot.phase,expectedPhase,`${state} captures its actual lifecycle phase`);
  const url=endpoints.base+'/tpm/runs/'+id+'/live';
  const observe=route=>route.request().method()==='GET'?route.fulfill({json:snapshot}):route.continue();
  await context.route(url,observe);
  try{await page.evaluate(()=>TpmUI.refresh());await shots(state)}finally{await context.unroute(url,observe);await page.evaluate(()=>TpmUI.refresh())}
 }
 await shots('empty');
 await page.locator('#recordCreateButton').click();await page.locator('#tpmDrawer.mobile-open').waitFor();
 assert.equal(await page.locator('#tpmAdvanced').evaluate(node=>node.open),false,'new task keeps advanced settings collapsed');
 assert.equal(await page.locator('#tpm_connection_id').inputValue(),'','saved connections begin with an explicit selection placeholder');
 assert.equal(await page.locator('#tpmManualConnection').isVisible(),false,'initial saved-connection flow hides manual credentials');
 await page.waitForTimeout(500);
 assert.equal(modelPosts.length,0,'opening a draft does not discover models until a connection is selected');
 await page.locator('#tpm_connection_id').selectOption('__manual__');
 await page.locator('#tpmManualConnection').waitFor({state:'visible'});
 assert.equal(await page.locator('#tpmManualConnection').isVisible(),true,'manual connection exposes address and key');
 assert.equal(await page.locator('#tpmCustomTarget').isVisible(),false,'preset target does not duplicate its numeric input');
 await page.locator('#tpm_url').fill(endpoints.upstream+'/v1');await page.locator('#tpm_api_key').fill('synthetic-tpm-manual-key');await page.locator('#tpmModelsButton').click();
 await page.waitForFunction(()=>[...document.getElementById('tpm_model').options].some(option=>option.value==='tpm-mock'));
 assert.equal(modelPosts.length,1);assert.ok(!modelPosts[0].connection_id,'manual sentinel never reaches the backend');assert.equal(modelPosts[0].api_key,'synthetic-tpm-manual-key');
 await page.locator('#tpm_model').selectOption('tpm-mock');await page.locator('#tpm_connection_id').selectOption('');
 assert.equal(await page.locator('#tpm_url').inputValue(),'');assert.equal(await page.locator('#tpm_api_key').inputValue(),'');assert.equal(await page.locator('#tpm_model').inputValue(),'');assert.equal(await page.locator('#tpmManualConnection').isVisible(),false);
 await page.waitForTimeout(500);assert.equal(modelPosts.length,1,'returning to placeholder clears fields without a model request');
 // Discovery uses real HTTP and saved synthetic credentials, never a manually typed model.
 await page.locator('#tpm_connection_id').selectOption(connections.slow);
 await page.waitForFunction(()=>document.getElementById('tpm_model').disabled&&document.getElementById('tpmModelStatus').textContent.includes('获取'));
 assert.equal(await page.locator('#tpm_model').inputValue(),'');
 await page.locator('#tpm_connection_id').selectOption(connections.new);
 await page.waitForFunction(()=>[...document.getElementById('tpm_model').options].some(option=>option.value==='tpm-new-model'));
 await page.locator('#tpm_model').selectOption('tpm-new-model');
 const staleResponse=page.waitForResponse(response=>new URL(response.url()).pathname==='/models'&&response.request().postDataJSON()?.connection_id===connections.slow);
 await context.request.post(endpoints.upstream+'/tpm-control',{data:{release_slow_models:true}});
 await staleResponse;await page.waitForTimeout(100);
 assert.equal(await page.locator('#tpm_model').inputValue(),'tpm-new-model','late old connection response cannot replace current selection');
 assert.equal(await page.locator('#tpm_model option[value="tpm-stale-model"]').count(),0);
 await page.locator('#tpm_connection_id').selectOption(connections.flaky);
 await page.waitForFunction(()=>document.getElementById('tpmModelStatus').textContent.includes('失败'));
 assert.equal(await page.locator('#tpm_model').inputValue(),'','failed discovery invalidates previous models');
 await context.request.post(endpoints.upstream+'/tpm-control',{data:{flaky_models_ready:true}});
 await page.locator('#tpmModelsButton').click();
 await page.waitForFunction(()=>[...document.getElementById('tpm_model').options].some(option=>option.value==='tpm-retry-model'));
 await page.locator('#tpm_connection_id').selectOption(connections.main);
 await page.waitForFunction(()=>[...document.getElementById('tpm_model').options].some(option=>option.value==='tpm-mock'));
 await page.locator('#tpm_model').selectOption('tpm-mock');
 assert.equal(await page.locator('#tpmManualConnection').isVisible(),false,'saved connection hides redundant address and credential fields');
 assert.equal(await page.locator('#tpm_url').isVisible(),false);assert.equal(await page.locator('#tpm_api_key').isVisible(),false);
 assert.equal(await page.locator('#tpm_api_key').inputValue(),'','saved connection never reveals its secret');
 const savedModelPosts=modelPosts.filter(body=>body.connection_id);assert.ok(savedModelPosts.length>=5);assert.ok(savedModelPosts.every(body=>body.connection_id!=='__manual__'&&!body.api_key),'saved connection requests reuse server-side credentials');
 assert.equal(await page.locator('#tpmExecutionPlan').isVisible(),false,'automatic capacity stays inside collapsed advanced settings');
 await shots('configuration');
 await page.locator('#tpm_cache_write_price_mode').selectOption('explicit');
 await page.locator('#tpm_price_cache_write').fill('0.45');
 const priceLayout=await page.locator('#tpm_price_cache_write').evaluate(node=>({section:node.closest('details')?.textContent||'',pairedWithRead:Boolean(node.closest('.inline')?.querySelector('#tpm_price_cache_read'))}));
 assert.equal(priceLayout.section,'','write price is a basic price setting');assert.equal(priceLayout.pairedWithRead,true,'cache read and write prices share their price row');
 await shots('prices','tpm_price_cache_write');
 await page.locator('#tpm_cache_write_price_mode').selectOption('input');
 const budget=await page.locator('#tpm_budget_usd').inputValue();await page.locator('#tpm_cache_mode').selectOption('avoid_cache');assert.equal(await page.locator('#tpm_budget_usd').inputValue(),budget,'switching cache mode cannot increase budget');await page.locator('#tpm_cache_mode').selectOption('prefer_cache');
 await page.locator('#tpmAdvanced > summary').click();
 const cacheInputs=await page.locator('#tpmCacheSettings input').evaluateAll(nodes=>nodes.map(node=>node.value));
 await page.locator('#tpm_cache_mode').selectOption('avoid_cache');
 assert.equal(await page.locator('#tpmCacheSettings').isVisible(),false,'non-cache mode hides inapplicable warmup and cache protections');
 assert.equal(await page.locator('#tpm_warmup_requests').isDisabled(),true);
 await page.locator('#tpm_cache_mode').selectOption('prefer_cache');
 assert.equal(await page.locator('#tpmCacheSettings').isVisible(),true);
 assert.deepEqual(await page.locator('#tpmCacheSettings input').evaluateAll(nodes=>nodes.map(node=>node.value)),cacheInputs,'switching cache scenarios preserves the user values');
 assert.equal(await page.locator('#tpmIndependentUsage').isVisible(),false);
 await page.locator('#tpm_usage_semantics').selectOption('independent');await page.locator('#tpm_usage_semantics_confirmed').check();
 await page.locator('#tpm_usage_semantics').selectOption('openai');assert.equal(await page.locator('#tpmIndependentUsage').isVisible(),false);
 await page.locator('#tpm_usage_semantics').selectOption('independent');assert.equal(await page.locator('#tpm_usage_semantics_confirmed').isChecked(),true);
 await page.locator('#tpm_usage_semantics').selectOption('openai');
 const calibrationRequests=await page.locator('#tpm_calibration_requests').inputValue();
 await page.locator('#tpm_calibration_requests').fill('1');await page.locator('#tpmAdvanced > summary').click();await page.locator('#tpmSubmit').click();
 assert.equal(await page.locator('#tpmAdvanced').evaluate(node=>node.open),true,'invalid advanced input is revealed automatically');
 assert.equal(await page.locator('#tpm_calibration_requests').evaluate(node=>node===document.activeElement),true,'validation focuses the offending field');
 assert.match(await page.locator('#tpmFormStatus').textContent(),/校准请求数/);assert.equal(paidPosts.length,0);
 await page.locator('#tpm_calibration_requests').fill(calibrationRequests);
 await page.waitForFunction(()=>!document.getElementById('tpmExecutionPlan').textContent.includes('正在'));
 assert.match(await page.locator('#tpmExecutionPlan').textContent(),/自动/);
 assert.equal(await page.locator('#tpmExecutionPlan .report-metrics').count(),0,'automatic planning is a compact summary, not three large metric cards');
 assert.equal(await page.locator('#tpm_execution_mode').inputValue(),'auto');
 assert.equal(await page.locator('#tpm_concurrency').isDisabled(),true);assert.equal(await page.locator('#tpm_pool_connections').isDisabled(),true);
 const presetPlans=[];
 for(const target of [50000000,100000000,150000000,200000000]){
  await page.locator('#tpm_target_preset').selectOption(String(target));
  const expected=Math.ceil(target*30*1.5/(50000*60));
  await page.waitForFunction(expected=>Number(document.getElementById('tpm_concurrency').value)===expected,expected);
  assert.equal(await page.locator('#tpm_target_tpm').inputValue(),String(target));
  assert.equal(await page.locator('#tpmCustomTarget').isVisible(),false,'preset hides custom numeric target');
  assert.equal(Number(await page.locator('#tpm_pool_connections').inputValue()),expected);
  presetPlans.push({target_tpm:target,concurrency:expected});
 }
 await page.locator('#tpm_execution_mode').selectOption('manual');await page.locator('#tpm_concurrency').fill('7');await page.locator('#tpm_pool_connections').fill('9');
 await page.locator('#tpm_target_preset').selectOption('50000000');
 await page.waitForFunction(()=>document.getElementById('tpmExecutionPlan').textContent.includes('手动'));
 assert.equal(await page.locator('#tpm_concurrency').inputValue(),'7');assert.equal(await page.locator('#tpm_pool_connections').inputValue(),'9');
 await page.locator('#tpm_execution_mode').selectOption('auto');
 await page.locator('#tpm_target_preset').selectOption('custom');
 assert.equal(await page.locator('#tpmCustomTarget').isVisible(),true,'only custom preset exposes numeric target');
 await page.locator('#tpm_target_tpm').fill('');await page.locator('#tpm_target_tpm').pressSequentially('1000000000');
 assert.equal(await page.locator('#tpm_target_preset').inputValue(),'custom','typing a custom value cannot switch to a preset midway');assert.equal(await page.locator('#tpmCustomTarget').isVisible(),true);
 await page.locator('#tpm_target_tpm').fill('12000000');await page.locator('#tpm_target_duration_seconds').fill('45');
 await page.locator('#tpm_calibration_requests').fill('5');await page.locator('#tpm_warmup_requests').fill('0');await page.locator('#tpm_ramp_duration_seconds').fill('1');
 await page.waitForFunction(()=>Number(document.getElementById('tpm_concurrency').value)===180);
 await page.locator('#tpmAdvanced > summary').click();
 await page.locator('#tpmSubmit').click();await page.waitForFunction(()=>document.getElementById('tpmSubmit').dataset.clickAction==='calibrateTpm');
 assert.equal(paidPosts.length,0,'offline preview never starts upstream');assert.equal((await (await context.request.get(endpoints.upstream+'/tpm-stats')).json()).requests,0);
 assert.equal(await page.locator('#tpmAdvanced').evaluate(node=>node.open),false);assert.equal(await page.locator('#tpmPreviewDetails').evaluate(node=>node.open),false,'full preview is available without dominating confirmation');
 await shots('preview','tpmPreview');
 await page.locator('#tpmSubmit').click();await page.waitForFunction(()=>!TpmUI.isOpen()&&document.getElementById('modulePlaceholder').dataset.tpmPhase==='calibrating');await phaseShots('calibrating','calibrating');await page.waitForFunction(()=>document.querySelector('[data-click-action="startTpm"]'),{timeout:30000});assert.equal(paidPosts.length,1,'one confirmed calibration task');
 const list=await (await context.request.get(endpoints.base+'/tpm/runs')).json();assert.equal(list.runs.length,1);const id=list.runs[0].id;
 const calibrated=await (await context.request.get(endpoints.base+'/tpm/runs/'+id)).json();assert.equal(calibrated.phase,'awaiting_run');assert.ok(Math.abs(calibrated.calibration.mean_input_tokens-49500)/49500<.01);assert.equal(calibrated.plan.target_rps,12000000/(calibrated.calibration.mean_input_tokens+500)/60);
 assert.match(await page.locator('#modulePlaceholder').textContent(),/仍独占/);
 const expectedConcurrency=Math.ceil(calibrated.plan.target_rps*calibrated.calibration.p95_seconds*1.5);assert.equal(calibrated.plan.execution_plan.source,'calibration_p95');assert.equal(calibrated.plan.execution_plan.concurrency,expectedConcurrency);assert.equal(calibrated.config.concurrency,expectedConcurrency);assert.equal(calibrated.config.pool_connections,expectedConcurrency);assert.ok(expectedConcurrency<180,'calibration replaces assumed latency with observed latency');
 assert.match(await page.locator('#modulePlaceholder').textContent(),new RegExp(String(expectedConcurrency)));
 assert.equal(await page.locator('#tpmRunConfirmation').isVisible(),true);
 assert.equal(await page.locator('#tpmKeyMetrics,#tpmChart,#tpmRuntimeDetails').count(),0,'awaiting confirmation has no empty runtime statistics');
 async function disclosureSurvivesRefresh(id){
  await page.locator('#'+id+' > summary').click();
  await page.locator('#'+id+' > summary').focus();
  await page.evaluate(id=>{window.tpmPreviousDisclosure=document.getElementById(id)},id);
  await page.evaluate(()=>TpmUI.refresh());
  await page.waitForFunction(id=>window.tpmPreviousDisclosure!==document.getElementById(id),id);
  assert.equal(await page.locator('#'+id).evaluate(node=>node.open),true,id+' stays open through live refresh');
  assert.equal(await page.locator('#'+id+' > summary').evaluate(node=>node===document.activeElement),true,id+' preserves keyboard focus through live refresh');
  await page.locator('#'+id+' > summary').click();
 }
 await page.locator('[data-click-action="setTpmTab"][data-arg1="plan"]').click();await page.getByRole('heading',{name:'校准结果',exact:true}).waitFor();await shots('frozen-plan');
 await page.locator('[data-click-action="setTpmTab"][data-arg1="report"]').click();await page.locator('#tpmRunConfirmation').waitFor();
 await shots('awaiting-run');
 await page.reload();await page.locator('[data-module="tpm"]').click();await page.locator('[data-click-action="startTpm"]').waitFor();assert.equal(paidPosts.length,1,'reload only observes existing record');
 await page.locator('[data-click-action="startTpm"]').click();await page.waitForFunction(()=>!document.querySelector('[data-click-action="startTpm"]'));assert.equal(paidPosts.length,2,'formal execution requires separate confirmation');assert.equal(paidPosts[1].body.plan_id,calibrated.plan_id);
 let applied;for(let attempt=0;attempt<40;attempt++){applied=await (await context.request.get(endpoints.base+'/tpm/runs/'+id)).json();if(applied.metrics.started>=8&&applied.metrics.in_flight>=2)break;await new Promise(resolve=>setTimeout(resolve,500))}assert.ok(applied.metrics.started>=8&&applied.metrics.in_flight>=2,'several formal requests must truly start before stop');await page.evaluate(()=>TpmUI.refresh());
 assert.equal(await page.locator('#tpmKeyMetrics').isVisible(),true);assert.equal(await page.locator('#tpmTrend').isVisible(),true);
 const runtimeOrder=await page.evaluate(()=>{const key=document.getElementById('tpmKeyMetrics'),trend=document.getElementById('tpmTrend'),details=document.getElementById('tpmRuntimeDetails');return Boolean(key.compareDocumentPosition(trend)&Node.DOCUMENT_POSITION_FOLLOWING)&&Boolean(trend.compareDocumentPosition(details)&Node.DOCUMENT_POSITION_FOLLOWING)});
 assert.equal(runtimeOrder,true,'main indicators and trend precede lower frequency statistics');
 assert.equal(await page.locator('#tpmRuntimeDetails').evaluate(node=>node.open),false);
 await disclosureSurvivesRefresh('tpmRuntimeDetails');
 await shots('running');
 const desktopTrendTop=await page.locator('#tpmChart').evaluate(node=>node.getBoundingClientRect().top);
 assert.ok(desktopTrendTop>=0&&desktopTrendTop<1000,'trend canvas begins within the 1440 × 1000 desktop viewport');
 await page.locator('#tpmRuntimeDetails > summary').click();await shots('runtime-details','tpmRuntimeDetails');await page.locator('#tpmRuntimeDetails > summary').click();
 await page.locator('[data-click-action="stopTpm"]').click();await page.getByText('已停止调度新请求，正在等候已发送请求结束、结算和落盘。每条请求仅使用其剩余总超时。',{exact:true}).waitFor();
 const stopped=await (await context.request.get(endpoints.base+'/tpm/runs/'+id)).json(),count=stopped.metrics.started;assert.equal(stopped.phase,'draining');
 await phaseShots('draining','draining',stopped);
 await page.waitForFunction(()=>document.querySelector('[data-click-action="repeatTpm"]'));const finished=await (await context.request.get(endpoints.base+'/tpm/runs/'+id)).json();assert.equal(finished.metrics.started,count,'stop never launches queued requests');assert.equal(finished.metrics.in_flight,0);assert.equal(finished.report.primary_window.valid,false);assert.equal(finished.report.label,'证据不足/发压端受限');assert.ok(finished.fees.known_cost_usd>0,'calibration and drained usage remain billed');
 await shots('finished');
 const exported=await (await context.request.get(endpoints.base+'/tpm/runs/'+id+'/export')).json();assert.ok(exported.requests.length>2);assert.ok(exported.requests.some(row=>row.phase==='formal'));assert.ok(exported.corpus.fingerprint);
 await page.locator('[data-click-action="setTpmTab"][data-arg1="requests"]').click();await page.getByRole('heading',{name:'最近逐请求摘要'}).waitFor();assert.ok(await page.locator('#modulePlaceholder tbody tr').count()>1);
 await shots('requests','tpmRequestTable');
 await page.setViewportSize({width:390,height:1000});
 await page.locator('#modulePlaceholder tbody details').first().locator('summary').click();
 await page.waitForFunction(()=>{const detail=document.querySelector('#tpmRequestTable tbody details'),cell=detail.getBoundingClientRect(),viewport=detail.closest('.table-wrap').getBoundingClientRect();return detail.open&&cell.left>=viewport.left&&cell.right<=viewport.right});
 const auditScroll=await page.locator('#tpmRequestTable .table-wrap').evaluate(node=>({left:node.scrollLeft,top:node.scrollTop}));
 await page.evaluate(()=>TpmUI.refresh());
 assert.deepEqual(await page.locator('#tpmRequestTable .table-wrap').evaluate(node=>({left:node.scrollLeft,top:node.scrollTop})),auditScroll,'polling preserves the request table reading position');
 await page.locator('#tpmRequestTable .table-wrap').evaluate(node=>{node.scrollLeft=0});
 await page.evaluate(()=>TpmUI.refresh());
 await page.waitForTimeout(50);
 assert.equal(await page.locator('#tpmRequestTable .table-wrap').evaluate(node=>node.scrollLeft),0,'restoring an expanded time panel does not act as a user click');
 await shots('request-time','tpmRequestTable');
 await page.locator('[data-click-action="repeatTpm"]').click();await page.locator('#tpmDrawer.mobile-open').waitFor();assert.equal(await page.locator('#tpm_input_tokens').inputValue(),'49500');assert.equal(await page.locator('#tpm_api_key').inputValue(),'');assert.equal(await page.locator('#tpmSubmit').getAttribute('data-click-action'),'previewTpm');assert.equal(paidPosts.length,2,'repeat opens unconfirmed draft');
 // Failed calibration has a separate evidence state and cannot start formal pressure.
 await page.waitForFunction(()=>[...document.getElementById('tpm_model').options].some(option=>option.value==='tpm-fail'));await page.locator('#tpm_model').selectOption('tpm-fail');await page.locator('#tpmSubmit').click();await page.waitForFunction(()=>document.getElementById('tpmSubmit').dataset.clickAction==='calibrateTpm');await page.locator('#tpmSubmit').click();await page.waitForFunction(id=>!TpmUI.isOpen()&&document.getElementById('modulePlaceholder').dataset.tpmId!==id,id);await page.waitForFunction(()=>document.getElementById('modulePlaceholder').dataset.tpmPhase==='finished'&&document.querySelector('[data-click-action="repeatTpm"]'));assert.equal(await page.locator('[data-click-action="startTpm"]').count(),0);assert.equal(await page.locator('#modulePlaceholder').getAttribute('data-tpm-tab'),'report','new calibration resets stale request-detail tab');await page.getByRole('heading',{name:'校准失败，未发起正式压测'}).waitFor();await shots('failed');
 // Stopping during calibration must not invent a formal dashboard or chart.
 await page.locator('[data-click-action="repeatTpm"]').click();await page.locator('#tpmDrawer.mobile-open').waitFor();
 await page.waitForFunction(()=>[...document.getElementById('tpm_model').options].some(option=>option.value==='tpm-mock'));await page.locator('#tpm_model').selectOption('tpm-mock');
 await page.locator('#tpmSubmit').click();await page.waitForFunction(()=>document.getElementById('tpmSubmit').dataset.clickAction==='calibrateTpm');await page.locator('#tpmSubmit').click();await page.waitForFunction(()=>!TpmUI.isOpen());
 await page.locator('[data-click-action="stopTpm"]').click();await page.waitForFunction(()=>document.querySelector('[data-click-action="repeatTpm"]'));
 const stoppedBeforeFormalId=await page.locator('[data-click-action="repeatTpm"]').getAttribute('data-arg0'),stoppedBeforeFormal=await (await context.request.get(endpoints.base+'/tpm/runs/'+stoppedBeforeFormalId)).json();
 assert.equal(Boolean(stoppedBeforeFormal.formal_started_at),false);assert.equal(stoppedBeforeFormal.metrics.started,0);
 assert.equal(await page.locator('#tpmKeyMetrics,#tpmChart,#tpmRuntimeDetails').count(),0,'calibration stop does not show unstarted formal measurements');
 const stats=await (await context.request.get(endpoints.upstream+'/tpm-stats')).json();assert.ok(stats.max_body_bytes>200000,'long input is physically present in real request body');assert.equal(stats.usage_is_synthetic,true);assert.deepEqual(errors,[]);assert.deepEqual(sentinelLeaks,[],'manual selector sentinel is never sent to an API');
 fs.writeFileSync(path.join(output,'tpm-result.json'),JSON.stringify({synthetic_usage:true,not_real_upstream_capacity:true,actual_launcher:Boolean(endpoints.launcher),credential_boundary:endpoints.credential_boundary||'in-memory synthetic fixture',model_discovery_requests:modelPosts.length,preset_execution_plans:presetPlans,calibrated_execution_plan:calibrated.plan.execution_plan,layout_checks:{creation_layout:creationLayout,initial_connection_requires_selection:true,manual_sentinel_not_submitted:true,saved_connection_hides_manual_fields:true,custom_target_only:true,compact_capacity:true,confirmation_without_empty_runtime:true,key_metrics_and_trend_first:true,desktop_trend_canvas_top_px:desktopTrendTop,disclosure_survives_refresh:true,keyboard_focus_survives_refresh:true,preformal_stop_without_runtime:true,screenshot_states:['empty','configuration','prices','preview','calibrating','frozen-plan','awaiting-run','running','runtime-details','runtime-details-bottom','draining','finished','requests','request-time','failed']},browser:'Chromium',viewports:[1440,980,390],themes:['light','dark'],upstream:stats,formal_started:count,drain_seconds:finished.report.drain_seconds,ui_errors:errors},null,2));
 console.log('TPM browser（每分钟词元浏览器）: simplified layout, preserved disclosures, automatic models/pricing/concurrency, long payload, two confirmations, reload, drain, repeat, failed calibration and 90 responsive screenshots passed. Synthetic usage only.');
})().catch(async error=>{console.error(error);process.exitCode=1;const page=browser?.contexts()[0]?.pages()[0];if(page){await page.screenshot({path:path.join(output,'failure.png')}).catch(()=>{});fs.writeFileSync(path.join(output,'failure.txt'),await page.locator('body').innerText().catch(()=>''));fs.writeFileSync(path.join(output,'failure-dom.json'),JSON.stringify(await page.evaluate(()=>['tpmDrawer','tpmCreateBody','tpmManualConnection','tpm_connection_id','tpm_url','tpmCustomTarget'].map(id=>{const node=document.getElementById(id);if(!node)return {id};const css=getComputedStyle(node);return {id,hidden:node.hidden,value:node.value,visibility:css.visibility,display:css.display,bounds:node.getBoundingClientRect().toJSON(),html:node.outerHTML.slice(0,1000)}})),null,2))}}).finally(async()=>{if(browser)await browser.close();await servers.close()});
