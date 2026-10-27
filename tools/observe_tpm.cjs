/** Browser observer for the loopback-only full-workbench performance check. */
'use strict';
const fs=require('node:fs'),path=require('node:path');
const {chromium}=require('playwright');
const [base,output,seconds]=process.argv.slice(2);
if(new URL(base).hostname!=='127.0.0.1')throw Error('Only loopback is allowed');
(async()=>{
 const browser=await chromium.launch({headless:true,...(process.env.CHROME_PATH?{executablePath:process.env.CHROME_PATH}:{})});
 const context=await browser.newContext({viewport:{width:1440,height:1000}}),page=await context.newPage(),errors=[];
 page.on('pageerror',error=>errors.push(error.message));
 await context.route('**/*',route=>new URL(route.request().url()).hostname==='127.0.0.1'?route.continue():route.abort());
 try{
  await page.goto(base);
  await page.locator('#authUsername').fill('tpm-benchmark');
  await page.locator('#authPassword').fill('Synthetic-TPM-benchmark-2026');
  await page.locator('#authSubmit').click();
  await page.locator('#authGate').waitFor({state:'hidden'});
  await page.locator('[data-module="tpm"]').click();
  await page.evaluate(()=>{
   window.tpmBenchmark={longTasks:[],renderFrameDelays:[],renders:0};
   if(PerformanceObserver.supportedEntryTypes.includes('longtask'))new PerformanceObserver(list=>{for(const entry of list.getEntries())window.tpmBenchmark.longTasks.push(entry.duration)}).observe({entryTypes:['longtask']});
   new MutationObserver(()=>{
    const at=performance.now();window.tpmBenchmark.renders++;
    requestAnimationFrame(()=>window.tpmBenchmark.renderFrameDelays.push(performance.now()-at));
   }).observe(document.getElementById('modulePlaceholder'),{childList:true});
  });
  const deadline=Date.now()+Number(seconds)*1000;
  let sawRunning=false,finished=false;
  while(Date.now()<deadline){
   const records=await (await context.request.get(base+'/tpm/runs')).json();
   const run=records.runs[0];
   if(run?.phase==='running')sawRunning=true;
   if(sawRunning&&['finished','interrupted'].includes(run?.phase)){finished=true;break}
   await page.waitForTimeout(1500);
  }
  await page.waitForTimeout(1800);
  const results=await page.evaluate(()=>({...window.tpmBenchmark,overflow:document.documentElement.scrollWidth>innerWidth+1}));
  if(!finished)errors.push('未观察到完整发压和收尾');
  if(results.overflow)errors.push('页面出现横向溢出');
  if(results.renders<2)errors.push('未观察到持续页面刷新');
  await page.screenshot({path:path.join(path.dirname(output),'performance-browser.png'),fullPage:true});
  fs.writeFileSync(output,JSON.stringify({sawRunning,finished,errors,...results},null,2));
 }finally{await browser.close()}
})().catch(error=>{fs.writeFileSync(output,JSON.stringify({errors:[error.message]}));process.exitCode=1});
