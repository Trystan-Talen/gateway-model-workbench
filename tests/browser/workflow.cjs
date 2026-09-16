/** End-to-end loopback regression. No user accounts, upstream credentials, or external requests.
 * NODE_PATH=<directory containing playwright> node tests/browser/workflow.cjs
 */
const {chromium}=require('playwright');
const fs=require('fs'),path=require('path'),os=require('os'),assert=require('node:assert/strict');
const {spawn}=require('child_process');
const output=process.env.UI_ARTIFACT_DIR||fs.mkdtempSync(path.join(os.tmpdir(),'route-workflow-'));
fs.mkdirSync(output,{recursive:true});
const failures=[],children=[];
async function start(){
 const child=spawn(process.env.PYTHON||'python3',['-u',path.join(__dirname,'workflow_server.py'),output]);children.push(child);
 child.stderr.on('data',chunk=>fs.appendFileSync(path.join(output,'server.log'),chunk));
 const endpoints=await new Promise((resolve,reject)=>{let text='';child.stdout.on('data',b=>{text+=b;try{resolve(JSON.parse(text.split('\n')[0]))}catch{}});child.on('error',reject);child.on('exit',code=>reject(new Error('server exit '+code)));});
 return {...endpoints,child};
}
async function eventually(fn,message){const until=Date.now()+20000;while(Date.now()<until){if(await fn())return;await new Promise(r=>setTimeout(r,40))}throw Error(message)}
(async()=>{
 let server=await start();
 const browser=await chromium.launch({headless:true,executablePath:process.env.CHROME_PATH||undefined});
 try{
 const context=await browser.newContext({viewport:{width:1440,height:1000}});
 // Explicitly block any non-loopback network in browser validation.
 await context.route('**/*',route=>new URL(route.request().url()).hostname==='127.0.0.1'?route.continue():route.abort());
 const page=await context.newPage();
 page.on('pageerror',e=>failures.push(e.message));
 page.on('console',m=>{if(m.type()==='error'&&/\[render\]|\[refresh\]/.test(m.text()))failures.push(m.text())});
 const call=async(endpoint,body)=>{const r=body===undefined?await context.request.get(server.base+endpoint):await context.request.post(server.base+endpoint,{data:body});assert.ok(r.ok(),`${endpoint}: ${r.status()} ${await r.text()}`);return r.json()};
 await call('/auth/setup',{username:'regression',password:'local-test-888@'});
 await page.goto(server.base);
 assert.deepEqual(failures,[], 'Page script must parse before initialization');
 await page.waitForFunction(()=>typeof refreshTimer!=='undefined'&&refreshTimer!==null&&!refreshing);
 await page.evaluate(()=>clearInterval(refreshTimer));
 const resource={name:'资源 A',url:server.upstream+'/v1',api_key:'synthetic-key-only',model:'quality-pass'};
 const {quality_test_id:id}=await call('/quality-tests',{suite_path:server.suite,name:'逐题进度回归 · 本地模拟上游',capture_raw:true,resources:[resource,{...resource,name:'资源 B'}]});
 await page.evaluate(async id=>{await refresh();setModule('quality');setRecordFilter('compare');await selectQuality(id)},id);
 let q=await call('/quality-test/'+id);
 assert.equal(q.planned_cases,10);assert.equal(q.completed_cases,0);assert.equal(q.resources.length,2);
 for(let count=1;count<=10;count++){
  await context.request.post(server.upstream+'/advance');
  await eventually(async()=>{q=await call('/quality-test/'+id);return q.completed_cases===count},'case '+count+' not completed');
  assert.equal(q.progress,count/10);assert.equal(q.planned_cases,10);
  assert.ok(q.resources.flatMap(r=>r.case_results).every(x=>x.score!=null&&typeof x.passed==='boolean'));
  await page.evaluate(()=>refresh());
  assert.equal(await page.locator('#modulePlaceholder [role=progressbar]').getAttribute('aria-valuenow'),String(count));
  if(count<10)assert.equal(await page.locator('#qualityList .task.sel .task-pct').innerText(),count*10+'%');
  if(count===1){
   await page.waitForTimeout(350);const same=await call('/quality-test/'+id);assert.equal(same.completed_cases,1,'progress must not grow with time');
   await page.locator('#modulePlaceholder .report-tab').nth(1).click();
   await page.locator('#modulePlaceholder .raw-link').first().click();
   await page.evaluate(()=>refresh());assert.equal(await page.locator('#qualityEvidenceModal').isVisible(),true);
   assert.match(await page.locator('#qualityEvidenceModal').innerText(),/PONG/);await page.evaluate(()=>closeQualityEvidence());
   await page.locator('#modulePlaceholder .report-tab').first().click();
   await page.screenshot({path:path.join(output,'quality-running.png')});
  }
 }
 await eventually(async()=>{q=await call('/quality-test/'+id);return q.status==='finished'},'quality final state');
 assert.equal(q.completed_resources,2);assert.equal(q.resources[0].summary.passed_cases,5);
 await page.evaluate(()=>refresh());
 assert.equal(q.report.pairwise.paired_cases,5);assert.match(await page.locator('#modulePlaceholder').innerText(),/证据不足/);
 await page.screenshot({path:path.join(output,'quality-finished.png')});
 await page.locator('#modulePlaceholder .report-tab').nth(2).click();
 assert.equal(await page.locator('#modulePlaceholder tbody tr').count(),5);
 await page.locator('#modulePlaceholder .raw-link').first().click();
 assert.match(await page.locator('#qualityEvidenceModal').innerText(),/PONG/);await page.evaluate(()=>closeQualityEvidence());
 const {quality_test_id:bad}=await call('/quality-tests',{suite_path:server.suite,name:'错误请求回归',resources:[{...resource,model:'quality-fail'}]});
 let failed;
 await eventually(async()=>{failed=await call('/quality-test/'+bad);return failed.status==='finished_with_errors'},'error task final state');
 assert.equal(failed.progress,1);assert.equal(failed.failed_cases,5);assert.equal(failed.successful_responses,0);assert.equal(failed.report.level,'risk');
 await page.evaluate(async id=>{await refresh();await selectQuality(id)},bad);
 assert.match(await page.locator('#modulePlaceholder').innerText(),/请求链路错误/);
 await page.locator('#modulePlaceholder .report-tab').nth(1).click();
 assert.match(await page.locator('.group-detail-head .group-badge').innerText(),/错误/);
 const {comparison_id:comparison}=await call('/comparisons',{name:'原始记录入口回归',shared_config:{...resource,name:'本地模拟网关',model:'comparison',mode:'smoke',stream:false,capture_raw:true},groups:[{id:'base',name:'基准组',role:'baseline',concurrency:1,rpm:6000,iterations:2},{id:'other',name:'对比组',role:'comparison',concurrency:2,rpm:6000,iterations:2}]});
 let c;await eventually(async()=>{c=await call('/comparison/'+comparison);return c.status==='finished'},'comparison final state');
 await page.evaluate(async id=>{await refresh();setModule('gateway');setRecordFilter('compare');await selectComparison(id)},comparison);
 for(let i=0;i<2;i++){
  await page.locator('#modulePlaceholder .report-tab').nth(i+1).click();
  assert.equal(await page.locator('#modulePlaceholder .raw-link').count(),2);
  // item id 0 must work; output must be from this group's actual result file.
  await page.locator('#modulePlaceholder .raw-link').last().click();
  await page.waitForFunction(()=>document.getElementById('rawInput').textContent.includes('messages'));
  const raw=await call(`/task/${c.groups[i].task_id}/record/0`);
  assert.equal(JSON.parse(await page.locator('#rawInput').innerText()).model,'comparison');
  assert.equal(await page.locator('#rawOutput').innerText(),raw.raw_output);
  await page.screenshot({path:path.join(output,`comparison-raw-${i}.png`)});
  await page.evaluate(()=>closeRaw());
 }
 const markup=await page.evaluate(()=>({off:comparisonGroupHtml({config:{capture_raw:false}},{summary:{recent:[{id:0}]}}),missing:comparisonGroupHtml({config:{capture_raw:true}},{summary:{recent:[{id:0}]}})}));
 assert.ok(!markup.off.includes('raw-link'));assert.ok(markup.missing.includes('暂不可用'));assert.ok(!markup.missing.includes('data-click-action="showRaw'));
 // Restart the isolated server, then read disk-backed task evidence through HTTP and UI.
 server.child.kill('SIGTERM');server=await start();
 await call('/auth/login',{username:'regression',password:'local-test-888@'});
 await page.goto(server.base);await page.waitForFunction(()=>qualityTests.length===2&&!refreshing);await page.evaluate(()=>clearInterval(refreshTimer));
 assert.equal((await call('/tasks')).tasks.length,0,'comparison children must stay out of standalone list after restart');
 q=await call('/quality-test/'+id);assert.equal(q.progress,1);assert.equal(q.resources[0].case_results[0].response,'PONG');
 await page.evaluate(async id=>{setModule('quality');setRecordFilter('compare');await selectQuality(id)},id);
 await page.locator('#modulePlaceholder .report-tab').nth(1).click();await page.locator('#modulePlaceholder .raw-link').first().click();
 assert.match(await page.locator('#qualityEvidenceModal').innerText(),/PONG/);await page.evaluate(()=>closeQualityEvidence());
 await page.setViewportSize({width:390,height:844});
 await page.screenshot({path:path.join(output,'quality-mobile.png')});
 assert.deepEqual(failures,[]);
 console.log(JSON.stringify({passed:true,progress:[0,10,20,30,40,50,60,70,80,90,100],rawGroups:2,qualityRestart:true,pageErrors:failures,output},null,2));
 }finally{await browser.close();for(const child of children)child.kill('SIGTERM')}
})().catch(e=>{console.error(e);for(const child of children)child.kill('SIGTERM');process.exitCode=1});
