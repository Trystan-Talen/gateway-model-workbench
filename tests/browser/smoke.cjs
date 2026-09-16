/** Real served assets and CSP; all user/task data is synthetic and temporary. */
'use strict';
const {chromium}=require('playwright');
const assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path'),os=require('node:os');
const {spawn}=require('node:child_process');
const root=path.resolve(__dirname,'../..'), folder=fs.mkdtempSync(path.join(os.tmpdir(),'workbench-smoke-'));
const output=path.join(root,'artifacts/review-smoke');fs.mkdirSync(output,{recursive:true});
let child,browser;
(async()=>{
 child=spawn(process.env.PYTHON||path.join(root,'.venv/bin/python'),['-u',path.join(__dirname,'workflow_server.py'),folder],{cwd:root,env:{...process.env,PYTHONDONTWRITEBYTECODE:'1'}});
 child.stderr.on('data',d=>fs.appendFileSync(path.join(output,'server.log'),d));
 const endpoints=await new Promise((resolve,reject)=>{let text='';const timer=setTimeout(()=>reject(Error('server startup timed out')),15000);child.stdout.on('data',d=>{text+=d;try{const data=JSON.parse(text.split('\n')[0]);clearTimeout(timer);resolve(data)}catch{}});child.on('exit',code=>{clearTimeout(timer);reject(Error('server exited '+code))});});
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
   await page.getByRole('button',{name:'关闭新建测试',exact:true}).click();
   await page.waitForFunction(()=>!document.getElementById('config').classList.contains('mobile-open'));
   await page.locator('#empty .empty-action').click();
   await page.locator('#config.mobile-open').waitFor();
   await page.getByRole('button',{name:'关闭新建测试',exact:true}).click();
 }
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
 console.log(JSON.stringify({passed:true,modules:4,viewports:[1440,980,640,390],checks:['login','creation buttons','model discovery','select spacing','dark theme','CSP','cross-origin rejection','ETag']},null,2));
})().catch(e=>{console.error(e);process.exitCode=1}).finally(async()=>{if(browser)await browser.close();if(child)child.kill('SIGINT');});
