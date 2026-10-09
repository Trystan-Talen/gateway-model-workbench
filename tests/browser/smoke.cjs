/** Real served assets and CSP; all user/task data is synthetic and temporary. */
'use strict';
const {chromium}=require('playwright');
const assert=require('node:assert/strict'),path=require('node:path');
const artifacts=require('./artifacts.cjs').browserArtifacts('smoke');
const {projectRoot:root,output}=artifacts;
const servers=require('./server.cjs').browserServers(artifacts,{python:process.env.PYTHON||path.join(root,'.venv/bin/python')});
let browser;
(async()=>{
 const endpoints=await servers.start();
 browser=await chromium.launch({headless:true,...(process.env.CHROME_PATH?{executablePath:process.env.CHROME_PATH}:{})});
 const context=await browser.newContext({viewport:{width:1440,height:1000}}),page=await context.newPage(),errors=[];
 page.on('pageerror',e=>errors.push(e.message));
 page.on('console',m=>{if(m.type()==='error'&&/Content Security Policy|Refused to execute|MIME type/.test(m.text()))errors.push(m.text());});
 await context.route('**/*',r=>new URL(r.request().url()).hostname==='127.0.0.1'?r.continue():r.abort());
 const response=await page.goto(endpoints.base);
 assert.match(response.headers()['content-security-policy'],/script-src 'self';/);
 await page.locator('#authUsername').fill('smoke-admin');
 await page.locator('#authPassword').fill('Synthetic-password-2026');
 await page.locator('#authConfirm').fill('Synthetic-password-2026');
 await page.locator('#authSubmit').click();
 await page.locator('#authGate').waitFor({state:'hidden'});
 for(const module of ['gateway','routing','quality']){
   await page.locator(`[data-module="${module}"]`).click();
   await page.locator('#recordCreateButton').click();
   await page.locator('#config.mobile-open').waitFor();
   assert.equal(await page.locator('#config').getAttribute('aria-hidden'),'false');
   if(module==='gateway'){
     assert.equal(await page.locator('#rpm').inputValue(),'100');
     assert.equal(await page.locator('#concurrency').inputValue(),'100');
     await page.locator('#templatePickerBody [data-template="soak-short-10m"]').waitFor();
     const templates=(await (await context.request.get(endpoints.base+'/templates')).json()).templates.filter(item=>item.builtin);
     assert.equal(templates.length,6,'every built-in pressure template is covered');
     await page.locator('#url').fill(endpoints.upstream+'/v1');
     await page.locator('#api_key').fill('synthetic-template-key');
     for(const template of templates){
       await page.locator(`#templatePickerBody [data-template="${template.id}"]`).click();
       assert.equal(await page.locator('#concurrency').inputValue(),'100',template.id+': concurrency');
       if(template.config.mode==='ramp'){
         assert.equal(await page.locator('#ramps').inputValue(),template.config.ramps,template.id+': frozen step shape');
         assert.equal(Number((await page.locator('#ramps').inputValue()).split(',')[0]),100,template.id+': initial rate');
         assert.equal(await page.locator('#duration').inputValue(),String(template.config.duration));
       }else{
         assert.equal(await page.locator('#rpm').inputValue(),'100',template.id+': rate');
         const field=template.config.mode==='smoke'?'iterations':'duration';
         assert.equal(await page.locator('#'+field).inputValue(),String(template.config[field]));
       }
       const applied=await page.evaluate(()=>readConfig());
       assert.equal(Number(applied.rpm),100,template.id+': request base rate');
       assert.equal(applied.url,endpoints.upstream+'/v1');
       assert.equal(applied.api_key,'synthetic-template-key','templates retain manually supplied connection');
     }
     for(const width of [1440,980,390]){
       await page.setViewportSize({width,height:1000});
       for(const theme of ['light','dark']){
         await page.evaluate(theme=>setTheme(theme),theme);
         await page.locator('#templatePicker').scrollIntoViewIfNeeded();
         assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth+1),false);
         await page.screenshot({path:path.join(output,`all-pressure-templates-${width}-${theme}.png`)});
       }
     }
     await page.setViewportSize({width:1440,height:1000});
     await page.locator('[data-mode="smoke"]').click();
     for(const width of [1440,980,390]){
       await page.setViewportSize({width,height:1000});
       for(const theme of ['light','dark']){
         await page.evaluate(theme=>setTheme(theme),theme);
         await page.locator('#concurrency').scrollIntoViewIfNeeded();
         await page.screenshot({path:path.join(output,`pressure-defaults-${width}-${theme}.png`)});
       }
     }
     await page.setViewportSize({width:1440,height:1000});
     await page.evaluate(()=>setTheme('light'));
     await page.locator('#rpm').fill('350');
     await page.locator('#concurrency').fill('17');
   }
   await page.getByRole('button',{name:'关闭新建测试',exact:true}).click();
   await page.waitForFunction(()=>!document.getElementById('config').classList.contains('mobile-open'));
   await page.locator('#empty .empty-action').click();
   await page.locator('#config.mobile-open').waitFor();
   if(module==='gateway'){
     assert.equal(await page.locator('#rpm').inputValue(),'100','new pressure resets to default');
     assert.equal(await page.locator('#concurrency').inputValue(),'100','new concurrency resets to default');
     await page.locator('#templatePickerBody [data-template="soak-30m"]').click();
     assert.equal(await page.locator('#rpm').inputValue(),'100','long soak also resets to100 on reopened drawer');
     assert.equal(await page.locator('#concurrency').inputValue(),'100');
   }
   await page.getByRole('button',{name:'关闭新建测试',exact:true}).click();
 }
 await page.locator('[data-module="connections"]').click();
 await page.getByRole('heading',{name:'还没有常用连接',exact:true}).waitFor();
 await page.locator('#recordCreateButton').click();
 await page.locator('#connectionName').waitFor();
 assert.equal(await page.locator('#connectionDrawer').getAttribute('aria-hidden'),'false');
 await page.getByRole('button',{name:'关闭常用连接表单',exact:true}).click();
 await page.waitForFunction(()=>!ConnectionUI.isOpen());
 await page.locator('[data-module="fingerprint"]').click();
 await page.locator('#recordCreateButton').click();
 await page.locator('#fpUrl').fill(endpoints.upstream);
 await page.locator('#fpKey').fill('synthetic-key');
 await page.locator('#fpFetchModels').click();
 await page.locator('#fpModelSelect option[value="quality-oracle"]').waitFor({state:'attached'});
 await page.locator('#fpModelSelect').selectOption('quality-oracle');
 assert.equal(await page.locator('#fpModelSelect').inputValue(),'quality-oracle');
 for(const width of [1440,980,640,390]){
   await page.setViewportSize({width,height:1000});
   await page.waitForTimeout(200);
   const overflow=await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth+1);
   assert.equal(overflow,false,`page overflows at ${width}`);
   const padding=await page.locator('#fpModelSelect').evaluate(el=>parseFloat(getComputedStyle(el).paddingRight));
   assert.ok(padding>=36,`select arrow clearance at ${width}`);
   await page.screenshot({path:path.join(output,`fingerprint-${width}.png`)});
 }
 await page.locator('#fingerprintDrawer [data-fp-action="close-create"]').first().click();
 await page.setViewportSize({width:1440,height:1000});
 await page.locator('#themeToggle').click();
 assert.equal(await page.locator('html').getAttribute('data-theme'),'dark');
 await page.waitForTimeout(400);
 await page.screenshot({path:path.join(output,'dark.png')});
 await page.locator('#themeToggle').click();
 // Browser policy cannot execute an injected attribute handler.
 assert.equal(await page.evaluate(async()=>{const b=document.createElement('button');b.setAttribute('onclick','window.injected=true');document.body.appendChild(b);b.click();b.remove();return Boolean(window.injected)}),false);
 // Ignore only the deliberate blocked injection above.
 assert.deepEqual(errors.filter(e=>!e.includes('inline event handler')),[]);
 const originBlocked=await context.request.post(endpoints.base+'/config',{headers:{Origin:'https://evil.example'},data:{}});
 assert.equal(originBlocked.status(),403);
 const initial=await context.request.get(endpoints.base+'/tasks?summary=1');
 const conditional=await context.request.get(endpoints.base+'/tasks?summary=1',{headers:{'If-None-Match':initial.headers().etag}});
 assert.equal(conditional.status(),304);
 console.log(JSON.stringify({passed:true,modules:5,viewports:[1440,980,640,390],checks:['login','creation buttons','all six pressure templates','template keeps connection','reopened pressure defaults','connections module','model discovery','select spacing','dark theme','CSP','cross-origin rejection','ETag']},null,2));
})().catch(e=>{console.error(e);process.exitCode=1}).finally(async()=>{try{if(browser)await browser.close()}finally{await servers.close()}});
