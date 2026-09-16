/** Isolated 120/240-question browser acceptance; all upstream requests are loopback simulations. */
const {chromium}=require('playwright');
const fs=require('fs'),path=require('path'),os=require('os'),assert=require('node:assert/strict');
const {spawn}=require('child_process');
const output=process.env.UI_ARTIFACT_DIR||fs.mkdtempSync(path.join(os.tmpdir(),'quality-v2-'));
fs.mkdirSync(output,{recursive:true});
const children=[],errors=[];
async function start(){
 const child=spawn('python3',['-u',path.join(__dirname,'workflow_server.py'),output]);children.push(child);
 child.stderr.on('data',x=>fs.appendFileSync(path.join(output,'server.log'),x));
 return new Promise((resolve,reject)=>{let text='';child.stdout.on('data',x=>{text+=x;try{resolve({...JSON.parse(text.split('\n')[0]),child})}catch{}});child.on('error',reject);child.on('exit',c=>reject(Error('server '+c)))});
}
async function until(fn,label,ms=90000){const end=Date.now()+ms;while(Date.now()<end){if(await fn())return;await new Promise(r=>setTimeout(r,80))}throw Error(label)}
(async()=>{
 let server=await start();
 const browser=await chromium.launch({headless:true,executablePath:process.env.CHROME_PATH||undefined});
 try{
  const context=await browser.newContext({viewport:{width:1440,height:1000}});
  await context.route('**/*',r=>new URL(r.request().url()).hostname==='127.0.0.1'?r.continue():r.abort());
  const page=await context.newPage();page.on('pageerror',e=>errors.push(e.message));
  page.on('console',m=>{if(m.type()==='error'&&/\[render\]|\[refresh\]/.test(m.text()))errors.push(m.text())});
  const call=async(p,b)=>{const r=b===undefined?await context.request.get(server.base+p):await context.request.post(server.base+p,{data:b});assert.ok(r.ok(),`${p}: ${r.status()} ${await r.text()}`);return r.json()};
  assert.equal((await context.request.get(server.base+'/quality-suite')).status(),401);
  assert.equal((await context.request.get(server.base+'/quality-report/missing')).status(),401);
  await call('/auth/setup',{username:'regression',password:'local-test-888@'});
  const suite=await call('/quality-suite');assert.equal(suite.cases.length,120);assert.equal(suite.manifest.categories.length,12);
  await page.goto(server.base);await page.waitForFunction(()=>typeof refreshTimer!=='undefined'&&refreshTimer&&!refreshing);
  await page.evaluate(()=>clearInterval(refreshTimer));
  async function createFromForm(compare){
   await page.evaluate(compare=>{setModule('quality');openQualityCreate(compare?'compare':'single')},compare);
   await page.waitForFunction(()=>document.getElementById('qualitySuiteSummary')?.innerText.includes('次请求'));
   assert.match(await page.locator('#qualitySuiteSummary').innerText(),compare?/240 次请求/:/120 次请求/);
   if(!compare){
    await page.getByRole('button',{name:'查看全部题目、答案与规则'}).click();
    assert.equal(await page.locator('#qualitySuiteBody details details').count(),120);
    await page.locator('#qualitySuiteBody .quality-suite-cases > details > summary').first().click();
    await page.locator('#qualitySuiteBody details details > summary').first().click();
    assert.match(await page.locator('#qualitySuiteBody').innerText(),/参考答案/);
    await page.screenshot({path:path.join(output,'suite-audit.png')});
    await page.keyboard.press('Escape');
   }
   await page.locator('#qualityName').fill(compare?'本地模拟 · 双资源退化对照':'本地模拟 · 单资源120题验收');
   await page.locator('#qualityCapture').check();
   const rows=page.locator('.quality-resource');assert.equal(await rows.count(),compare?2:1);
   for(let i=0;i<await rows.count();i++){
    const row=rows.nth(i);await row.locator('.quality-url').fill(server.upstream+'/v1');await row.locator('.quality-key').fill('synthetic-key-only');
    await row.locator('.quality-model-refresh').click();
    await until(async()=>!(await row.locator('.quality-model').isDisabled()),'models not loaded');
    await row.locator('.quality-model').selectOption(i===1?'quality-regressed':'quality-oracle');
   }
   if(compare){await page.screenshot({path:path.join(output,'comparison-create.png')})}
   const responsePromise=page.waitForResponse(r=>r.url().endsWith('/quality-tests')&&r.request().method()==='POST');
   await page.locator('#submit').click();const response=await responsePromise;
   assert.equal(response.status(),201,await response.text());return (await response.json()).quality_test_id;
  }
  const singleId=await createFromForm(false);let single;
  await until(async()=>{single=await call('/quality-test/'+singleId);return single.status==='finished'},'single not finished');
  assert.equal(single.completed_cases,120);assert.equal(single.report.mode,'single');assert.equal(single.report.resources[0].score_pct,100);
  assert.equal(single.resources[0].case_results.length,120);assert.ok(!single.report.pairwise);
  assert.equal(single.report.methodology.version,3);
  assert.equal(single.report.execution.id,'single_sequential_v1');
  const constraintRules=suite.cases.filter(c=>c.evaluator.type==='verifiable_constraints').reduce((n,c)=>n+c.evaluator.rules.length,0);
  assert.equal(single.report.resources[0].constraint_diagnostics.passed_checks,constraintRules);
  assert.equal(single.report.resources[0].constraint_diagnostics.passed_cases,10);
  assert.deepEqual(single.resources[0].case_results.map(c=>c.execution_sequence),Array.from({length:120},(_,i)=>i+1));
  await page.evaluate(async id=>{await refresh();await selectQuality(id)},singleId);
  await page.waitForFunction(id=>window.currentQualityDetail?.id===id,singleId);
  assert.match(await page.locator('#modulePlaceholder').innerText(),/单资源诊断/);
  assert.equal(await page.locator('.quality-score-card').count(),1);
  await page.screenshot({path:path.join(output,'single-report.png')});
  const downloadEvent=page.waitForEvent('download');await page.getByRole('button',{name:'下载报告与证据'}).click();
  const download=await downloadEvent;await download.saveAs(path.join(output,'downloaded-single-report.json'));
  const downloaded=JSON.parse(fs.readFileSync(path.join(output,'downloaded-single-report.json')));
  assert.equal(downloaded.id,singleId);assert.equal(downloaded.resources[0].case_results.length,120);
  assert.ok(!JSON.stringify(downloaded).includes('synthetic-key-only'));
  assert.equal(downloaded.report.methodology.version,3);
  assert.equal(downloaded.report.methodology.sources.length,7);
  assert.equal(downloaded.report.resources[0].constraint_diagnostics.passed_checks,constraintRules);
  assert.equal(downloaded.config.suite_manifest.fingerprint,suite.manifest.fingerprint);
  const readableDownload=page.waitForEvent('download');await page.getByRole('button',{name:'下载结论报告'}).click();
  await (await readableDownload).saveAs(path.join(output,'single-report.md'));
  const readable=fs.readFileSync(path.join(output,'single-report.md'),'utf8');
  assert.match(readable,/未导入官方数据集/);assert.match(readable,/约束/);assert.match(readable,/单资源能力诊断/);assert.match(readable,/适用场景/);assert.match(readable,/题集内容指纹/);
  // Check actual wheel scroll, horizontal scroll, and refresh preservation.
  await page.locator('#modulePlaceholder .report-tab').nth(1).click();
  const table=page.locator('#modulePlaceholder .table-wrap').last();await table.scrollIntoViewIfNeeded();await table.hover();
  await page.mouse.wheel(0,420);await page.waitForTimeout(200);
  const before=await table.evaluate(el=>({top:el.scrollTop,left:el.scrollLeft,h:el.clientHeight,all:el.scrollHeight}));
  if(before.top<=0){console.log('SCROLL_DIAGNOSTIC',await table.evaluate(el=>{const b=el.getBoundingClientRect();return {table:[b.x,b.y,b.width,b.height],point:document.elementFromPoint(b.x+b.width/2,b.y+b.height/2)?.outerHTML.slice(0,600),style:getComputedStyle(el).overflow,main:document.querySelector('.main').scrollTop,body:document.body.className}}));await page.screenshot({path:path.join(output,'scroll-failure.png')})};assert.ok(before.top>0&&before.all>before.h,JSON.stringify(before));
  await page.evaluate(()=>refresh());assert.ok(Math.abs((await table.evaluate(e=>e.scrollTop))-before.top)<3,'refresh resets scroll');
  await page.locator('#modulePlaceholder .raw-link').last().click();
  assert.equal(await page.locator('#qualityEvidenceModal').isVisible(),true);
  assert.match(await page.locator('#qualityEvidenceModal').innerText(),/参考答案|判分依据/);
  assert.match(await page.locator('#qualityEvidenceModal').innerText(),/实际输出/);
  await page.evaluate(()=>refresh());assert.equal(await page.locator('#qualityEvidenceModal').isVisible(),true);
  await page.screenshot({path:path.join(output,'case-evidence.png')});await page.keyboard.press('Escape');
  // Full paired run: B loses one whole dimension and a critical rule.
  const pairId=await createFromForm(true);let pair;
  await until(async()=>{pair=await call('/quality-test/'+pairId);return pair.status==='finished'},'comparison not finished');
  assert.equal(pair.completed_cases,240);
  assert.equal(pair.report.execution.id,'paired_alternating_v1');
  assert.match(pair.report.pairwise.method,/交替/);
  const timeline=pair.resources.flatMap((r,resource)=>r.case_results.map(c=>({...c,resource}))).sort((a,b)=>a.execution_sequence-b.execution_sequence);
  assert.deepEqual(timeline.map(c=>c.execution_sequence),Array.from({length:240},(_,i)=>i+1));
  for(let i=0;i<120;i++){
   assert.equal(timeline[2*i].case_id,suite.cases[i].id);
   assert.equal(timeline[2*i+1].case_id,suite.cases[i].id);
   assert.equal(timeline[2*i].resource,i%2);
   assert.equal(timeline[2*i+1].resource,1-i%2);
   assert.equal(timeline[2*i].prompt,timeline[2*i+1].prompt);
   assert.equal(timeline[2*i].input_chars,timeline[2*i].prompt.length);
  }
  assert.equal(pair.report.mode,'comparison');assert.ok(!pair.report.single);
  assert.equal(pair.report.pairwise.paired_cases,120);assert.equal(pair.report.pairwise.a_wins,11);
  assert.equal(pair.report.pairwise.regressions.length,11);assert.equal(pair.report.pairwise.selection,'优先复核 资源 A');
  assert.equal(pair.report.resources[1].score_pct,90.83);assert.equal(pair.report.resources[1].eligible,false);
  await page.evaluate(async id=>{await refresh();await selectQuality(id)},pairId);
  await page.waitForFunction(id=>window.currentQualityDetail?.id===id,pairId);
  assert.equal(await page.locator('.quality-score-card').count(),2);
  assert.match(await page.locator('#modulePlaceholder').innerText(),/资源对照 · 选择依据/);
  assert.match(await page.locator('#modulePlaceholder').innerText(),/逐题改善、退化与共同缺陷/);
  await page.screenshot({path:path.join(output,'comparison-report.png')});
  // Every report evidence link opens the corresponding stored question.
  const evidence=page.locator('#modulePlaceholder [data-resource="1"][data-case="safety_boundary_01"]').first();
  await evidence.click();assert.match(await page.locator('#qualityEvidenceModal').innerText(),/私密字段遮蔽/);
  await page.keyboard.press('Escape');
  // Running stop flow uses real server state, not a timer pretending progress.
  const {quality_test_id:stoppedId}=await call('/quality-tests',{name:'本地模拟 · 停止与滚动',resources:[{name:'慢速模拟',url:server.upstream+'/v1',api_key:'synthetic-key-only',model:'quality-slow'}]});
  let stopped;await until(async()=>{stopped=await call('/quality-test/'+stoppedId);return stopped.completed_cases>=18},'slow task');
  await page.evaluate(async id=>{await refresh();await selectQuality(id)},stoppedId);
  await page.locator('#modulePlaceholder .report-tab').nth(1).click();
  const activeTable=page.locator('#modulePlaceholder .table-wrap').last();
  await activeTable.evaluate(el=>{el.scrollTop=200});const top=await activeTable.evaluate(el=>el.scrollTop);assert.ok(top>0);
  await page.waitForTimeout(300);await page.evaluate(()=>refresh());assert.ok(Math.abs((await activeTable.evaluate(el=>el.scrollTop))-top)<3);
  page.once('dialog',d=>d.accept());await page.getByRole('button',{name:'停止后续题目'}).click();
  await until(async()=>{stopped=await call('/quality-test/'+stoppedId);return stopped.status==='stopped'},'stop');
  assert.ok(stopped.completed_cases<120);assert.ok(stopped.completed_cases>=18);assert.equal(stopped.report.evidence.complete,false);
  const finalCount=stopped.completed_cases;await page.waitForTimeout(350);assert.equal((await call('/quality-test/'+stoppedId)).completed_cases,finalCount);
  // Restore historical evidence after real process restart.
  server.child.kill('SIGTERM');server=await start();await call('/auth/login',{username:'regression',password:'local-test-888@'});
  const restored=await call('/quality-test/'+pairId);assert.deepEqual(restored.report,pair.report);
  assert.equal(restored.resources[1].case_results.length,120);
  await page.goto(server.base);await page.waitForFunction(()=>qualityTests.length===3&&!refreshing);await page.evaluate(()=>clearInterval(refreshTimer));
  await page.evaluate(async id=>{setModule('quality');setRecordFilter('compare');await selectQuality(id)},pairId);
  await page.locator('#modulePlaceholder .report-tab').first().click();
  await page.setViewportSize({width:390,height:844});await page.screenshot({path:path.join(output,'comparison-mobile.png')});
  assert.ok(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+1),'mobile page overflows horizontally');
  await page.locator('#modulePlaceholder .report-tab').nth(2).click();
  const mobileTable=page.locator('#modulePlaceholder .table-wrap').last();
  const dimensions=await mobileTable.evaluate(e=>{e.scrollLeft=100;return {left:e.scrollLeft,w:e.clientWidth,all:e.scrollWidth}});
  assert.ok(dimensions.left>0,JSON.stringify(dimensions));
  await page.locator('#modulePlaceholder .raw-link').last().click();
  await page.screenshot({path:path.join(output,'evidence-mobile.png')});
  assert.deepEqual(errors,[]);
  console.log(JSON.stringify({passed:true,singleQuestions:120,pairedRequests:240,regressions:11,stopAt:finalCount,download:true,scroll:true,restart:true,pageErrors:errors,output},null,2));
 }finally{await browser.close();children.forEach(c=>c.kill('SIGTERM'))}
})().catch(e=>{console.error(e);children.forEach(c=>c.kill('SIGTERM'));process.exitCode=1});
