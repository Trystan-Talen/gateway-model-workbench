/** Actual fingerprint form interactions. All API responses are synthetic; collection is blocked. */
'use strict';
const {chromium}=require('playwright'),assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path'),{execFileSync}=require('node:child_process');
const {projectRoot:root,output,env}=require('./artifacts.cjs').browserArtifacts('fingerprint_create');
const python=process.env.PYTHON||path.join(root,'.venv/bin/python');
const html=execFileSync(python,['-c','import route_test_ui; print(route_test_ui.INDEX_HTML)'],{cwd:root,env,encoding:'utf8'});
const fixture=JSON.parse(execFileSync(python,['-c','import json; from fingerprint_protocol import catalog,make_protocol; print(json.dumps({"catalog":catalog(),"protocol":make_protocol({"preset":"quick","samples_per_cell":10,"concurrency":2}),"reasoning":make_protocol({"preset":"quick","samples_per_cell":12,"concurrency":3,"rpm":75,"adapter":"openai_reasoning"})}))'],{cwd:root,env,encoding:'utf8'}));
const reference=(id,name,protocol)=>({id,name,protocol,source_note:'合成可追溯来源',trusted_source:true,complete:true,collected_at:new Date().toISOString(),endpoint:{model:'Synthetic/Model-B'},diagnostics:{valid_rate:1}});
const references=[reference('reference-direct','直接回答参考',fixture.protocol),reference('reference-reasoning','保留思考参考',fixture.reasoning)];
const errors=[],unexpected=[],checks=[],captures=[];
let browser;
function record(name,actual,expected,context){checks.push({name,actual,expected,...context,pass:JSON.stringify(actual)===JSON.stringify(expected)})}
(async()=>{
 const chrome='/Applications/Google Chrome.app/Contents/MacOS/Google Chrome';
 browser=await chromium.launch({headless:true,...(process.env.CHROME_PATH||fs.existsSync(chrome)?{executablePath:process.env.CHROME_PATH||chrome}:{})});
 for(const width of [1440,980,390]){
  const context=await browser.newContext({viewport:{width,height:width===390?844:1000},reducedMotion:'reduce'}),page=await context.newPage();
  let discoveries=0;
  page.on('pageerror',error=>errors.push({width,message:error.message}));
  await context.route('**/*',async route=>{
   if(await require('./assets.cjs').serveAsset(route,root))return;
   const request=route.request(),url=new URL(request.url()),p=url.pathname;let data={};
   if(url.hostname!=='127.0.0.1'){unexpected.push(request.url());return route.abort()}
   if(p==='/')return route.fulfill({contentType:'text/html',body:html});
   if(p==='/auth/status')data={authenticated:true,user:{username:'创建流程核查',role:'user'}};
   else if(p==='/tasks')data={tasks:[],comparisons:[],quality_tests:[],routing_comparisons:[],fingerprint_runs:[]};
   else if(p==='/config')data={config:{}};
   else if(p==='/connections')data={connections:[],storage_available:true};
   else if(p==='/templates')data={templates:[]};
   else if(p==='/routing-corpus')data={tiers:[],metrics:[]};
   else if(p==='/quality-reference-records')data={records:[]};
   else if(p==='/quality-suite')data={manifest:{title:'合成题集',case_count:0},cases:[]};
   else if(p==='/fingerprint/catalog')data=fixture.catalog;
   else if(p==='/fingerprint/references')data={references};
   else if(p==='/fingerprint/models'){discoveries++;data={models:['Synthetic/Model-A','Synthetic/Model-B']}}
   else if(p==='/favicon.ico')data={};
   else {unexpected.push(request.method()+' '+p);return route.fulfill({status:400,json:{error:'This synthetic form review never starts collection'}})}
   return route.fulfill({json:data});
  });
  await page.goto('http://127.0.0.1:18779/');
  await page.waitForFunction(()=>authenticated&&refreshTimer!==null&&!refreshing);await page.evaluate(()=>clearInterval(refreshTimer));
  await page.locator('[data-module="fingerprint"]').click();await page.locator('#recordCreateButton').click();await page.locator('#fpName').waitFor();
  const settle=()=>page.evaluate(()=>new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(resolve))));
  async function capture(state,{all=false}={}){
   for(const theme of ['light','dark']){
    await page.evaluate(value=>setTheme(value),theme);await page.locator('#fpCreateBody').evaluate(node=>{node.scrollTop=0});await settle();
    const parts=[];let top=0;
    do{
     await page.locator('#fpCreateBody').evaluate((node,value)=>{node.scrollTop=value},top);await settle();
     const geometry=await page.evaluate(()=>{
      const body=document.querySelector('#fpCreateBody'),footer=document.querySelector('#fingerprintDrawer .drawer-footer'),button=document.querySelector('#fpSubmit'),a=body.getBoundingClientRect(),b=footer.getBoundingClientRect(),c=button.getBoundingClientRect();
      return {top:body.scrollTop,height:body.clientHeight,total:body.scrollHeight,bodyTop:a.top,bodyBottom:a.bottom,footerTop:b.top,footerBottom:b.bottom,buttonTop:c.top,buttonBottom:c.bottom,viewportHeight:innerHeight,overflow:body.scrollWidth>body.clientWidth+1};
     });
     assert(!geometry.overflow,'drawer body has horizontal overflow');
     assert(geometry.bodyBottom<=geometry.footerTop+1,'footer covers form content');
     assert(geometry.buttonTop>=geometry.footerTop&&geometry.buttonBottom<=geometry.viewportHeight+1,'submit button is not fully visible');
     const file=`${state}-${width}-${theme}-part-${String(parts.length+1).padStart(2,'0')}.png`;await page.screenshot({path:path.join(output,file)});
     parts.push({...geometry,file});
     if(!all||geometry.top+geometry.height>=geometry.total-1)break;
     top=Math.min(geometry.total-geometry.height,geometry.top+geometry.height-32);
     assert(parts.length<80&&top>geometry.top,'form capture did not advance');
    }while(true);
    captures.push({state,width,theme,complete:all,parts});
   }
  }
  const values=()=>page.evaluate(()=>Object.fromEntries(['fpName','fpUrl','fpModel','fpPreset','fpSamples','fpConcurrency','fpRpm','fpTimeout','fpAdapter','fpSourceNote'].map(id=>[id,document.getElementById(id).value])));
  await page.locator('#fpName').fill('非默认参数 · 参考往返检查');
  await page.locator('#fpUrl').fill('https://synthetic.invalid/v1');
  await page.locator('#fpKey').fill('synthetic-only-never-sent-upstream');
  await page.locator('#fpFetchModels').click();await page.waitForFunction(()=>document.querySelector('#fpModelSelect option[value="Synthetic/Model-B"]'));
  await page.locator('#fpModelSelect').selectOption('Synthetic/Model-B');await page.locator('#fpFetchModels').click();
  await page.waitForFunction(()=>!document.querySelector('#fpFetchModels').disabled);
  record('same-connection model refresh preserves selection',await page.locator('#fpModelSelect').inputValue(),'Synthetic/Model-B',{width});
  await capture('refreshed-models');
  await page.locator('#fpManualModelToggle').click();await page.locator('#fpModel').fill('Manual/Chosen-Model');
  record('manual entry hides inactive list controls',await page.locator('#fpModelListPanel').isVisible(),false,{width});
  await page.locator('#fpSamples').fill('37');await page.locator('#fpConcurrency').fill('7');await page.locator('#fpRpm').fill('143');
  await page.locator('#fpAdvanced > summary').click();await page.locator('#fpTimeout').fill('78');await page.locator('#fpAdapter').selectOption('openai_reasoning');
  const independentDraft=await values();
  await page.locator('#fpPreset').selectOption('quick');record('range selection retains sample count',await page.locator('#fpSamples').inputValue(),'37',{width});
  await page.locator('#fpPreset').selectOption(independentDraft.fpPreset);
  await page.locator('#fpMode').selectOption('reference');
  await page.locator('#fpSourceNote').fill('已核对账号与模型版本，合成参考仅用于本地界面流程验证。');await page.locator('#fpTrusted').check();
  const referenceDraft=await values();
  await capture('reference-manual-model');
  await page.locator('#fpMode').selectOption('verify');
  record('unselected reference has no invented request count',await page.locator('#fpRequestCount').innerText(),'请选择参考',{width});
  await capture('reference-required',{all:true});
  await page.locator('#fpReference').selectOption('reference-direct');
  record('reference locks frozen controls',await page.locator('#fpSamples').isDisabled(),true,{width});
  record('reference applies its sample count',await page.locator('#fpSamples').inputValue(),'10',{width});
  await page.locator('#fpReference').selectOption('reference-reasoning');
  record('changing reference applies its frozen rate',await page.locator('#fpRpm').inputValue(),'75',{width});
  await capture('reference-locked',{all:true});
  await page.locator('#fpMode').selectOption('reference');
  record('returning from reference restores nondefault draft',await values(),referenceDraft,{width});
  record('source declaration survives mode roundtrip',await page.locator('#fpTrusted').isChecked(),true,{width});
  await page.locator('#fpMode').selectOption('selfcheck');
  record('selfcheck shares the explicitly chosen plan',await values(),referenceDraft,{width});
  await page.locator('#fpManualModelToggle').click();
  record('returning to list restores explicitly selected model',await page.locator('#fpModelSelect').inputValue(),'Synthetic/Model-B',{width});
  await page.locator('#fpManualModelToggle').click();
  record('returning to manual preserves its own model draft',await page.locator('#fpModel').inputValue(),'Manual/Chosen-Model',{width});
  await page.locator('#fpProbePreview > summary').click();
  await page.locator('#fpAdvanced .fp-details').last().locator('summary').click();
  await capture('advanced-complete',{all:true});
  for(const id of ['fpMode','fpName','fpUrl','fpModel','fpSamples','fpConcurrency','fpRpm','fpAdapter','fpTimeout','fpRiskAck']){
   const field=page.locator('#'+id);await field.scrollIntoViewIfNeeded();await field.focus();await settle();
   const bounds=await field.evaluate(node=>{const r=node.getBoundingClientRect(),body=document.querySelector('#fpCreateBody').getBoundingClientRect();return {top:r.top,bottom:r.bottom,bodyTop:body.top,bodyBottom:body.bottom}});
   assert(bounds.top>=bounds.bodyTop-1&&bounds.bottom<=bounds.bodyBottom+1,id+' cannot be focused fully above the footer');
  }
  await page.locator('#fpUrl').fill('https://synthetic.invalid/v1?invalid=query');await page.locator('#fpModel').fill('Manual/Chosen-Model');await page.locator('#fpRiskAck').check();await page.locator('#fpSubmit').click();
  await page.locator('#fpFormError').waitFor({state:'visible'});
  await page.locator('#fpUrl').fill('https://synthetic.invalid/v1');
  record('correcting a form input clears the stale error',await page.locator('#fpFormError').isVisible(),false,{width});
  await page.keyboard.press('Escape');record('closing removes credential input',await page.locator('#fpKey').count(),0,{width});
  record('model discovery is explicit and bounded',discoveries,2,{width});
  await context.close();
  fs.writeFileSync(path.join(output,'creation-results.json'),JSON.stringify({synthetic:true,checks,captures,errors,unexpected},null,2));
 }
 assert.deepEqual(errors,[]);assert.deepEqual(unexpected,[]);
 const failures=checks.filter(check=>!check.pass);assert.equal(failures.length,0,JSON.stringify(failures,null,2));
 console.log(`PASS: ${checks.length} actual form-state checks; ${captures.length} visual states, complete advanced-form captures, no collection calls. Artifacts: ${output}`);
})().catch(error=>{console.error(error);process.exitCode=1}).finally(async()=>{await browser?.close()});
