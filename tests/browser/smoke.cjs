/** Real served assets and CSP; all user/task data is synthetic and temporary. */
'use strict';
const {chromium}=require('playwright');
const assert=require('node:assert/strict'),path=require('node:path');
const artifacts=require('./artifacts.cjs').browserArtifacts('smoke');
const {projectRoot:root,output}=artifacts;
const servers=require('./server.cjs').browserServers(artifacts,{python:process.env.PYTHON||path.join(root,'.venv/bin/python')});
let browser;
async function captureCreate(page,module,drawer='#config'){
 for(const width of [1440,980,390]){
  await page.setViewportSize({width,height:1000});
  for(const theme of ['light','dark']){
   await page.evaluate(theme=>setTheme(theme),theme);
   await page.waitForFunction(selector=>Math.abs(document.querySelector(selector).getBoundingClientRect().right-innerWidth)<1,drawer);
   await page.locator(drawer+' .drawer-body').evaluate(el=>{el.scrollTop=0});
   await page.evaluate(()=>new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(resolve))));
   assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth+1),false,module+' page fits');
   const overflow=await page.locator(drawer+', '+drawer+' .drawer-body').evaluateAll(nodes=>nodes.filter(el=>el.scrollWidth>el.clientWidth+1).map(el=>el.id||el.className));
   assert.deepEqual(overflow,[],module+' drawer fits');
   await page.screenshot({path:path.join(output,`create-${module}-${width}-${theme}.png`)});
  }
 }
 await page.setViewportSize({width:1440,height:1000});
 await page.evaluate(()=>setTheme('light'));
}
(async()=>{
 const endpoints=await servers.start();
 browser=await chromium.launch({headless:true,...(process.env.CHROME_PATH?{executablePath:process.env.CHROME_PATH}:{})});
 const context=await browser.newContext({viewport:{width:1440,height:1000},reducedMotion:'reduce'}),page=await context.newPage(),errors=[];
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
   const single=page.locator(`#${module}Subnav [data-view="single"]`);
   if(await single.count())await single.click();
   await page.locator('#recordCreateButton').click();
   await page.locator('#config.mobile-open').waitFor();
   assert.equal(await page.locator('#config').getAttribute('aria-hidden'),'false');
   await captureCreate(page,module);
   if(module==='gateway'){
     assert.equal(await page.locator('#rpm').inputValue(),'100');
     assert.equal(await page.locator('#concurrency').inputValue(),'20');
     assert.equal(await page.locator('#templatePickerBody').isVisible(),false,'optional templates start collapsed');
     await page.locator('#templatePickerToggle').click();
     await page.locator('#templatePickerBody [data-template="soak-short-10m"]').waitFor();
     const templates=(await (await context.request.get(endpoints.base+'/templates')).json()).templates.filter(item=>item.builtin);
     assert.equal(templates.length,6,'every built-in pressure template is covered');
     await page.locator('#url').fill('');
     await page.locator('#submit').click();
     assert.match(await page.locator('#formStatus').innerText(),/地址/,'empty address blocks creation before requests');
     await page.locator('#url').fill(endpoints.upstream+'/v1');
     assert.equal(await page.locator('#formStatus').innerText(),'','correcting connection input clears its old error');
     await page.locator('#api_key').fill('synthetic-template-key');
     await page.locator('#loadModels').click();
     await page.waitForFunction(()=>document.querySelector('#modelSelect option[value="quality-regressed"]'));
     await page.locator('#modelSelect').selectOption('quality-regressed');
     await page.locator('#manualModelToggle').click();
     await page.locator('#model').fill('custom/cache-draft');
     assert.equal(await page.locator('#loadModels').isVisible(),false,'manual model has no misleading directory refresh action');
     await page.locator('#manualModelToggle').click();
     assert.equal(await page.locator('#modelSelect').inputValue(),'quality-regressed','manual entry leaves the list draft intact');
     await page.locator('#loadModels').click();
     await page.waitForFunction(()=>document.querySelector('#loadModels').disabled===false);
     await page.locator('#manualModelToggle').click();
     assert.equal(await page.locator('#model').inputValue(),'custom/cache-draft','directory refresh leaves the manual draft intact');
     await page.locator('#model').fill('');
     assert.equal(await page.evaluate(()=>currentModel()),'','empty manual input cannot silently submit the hidden list selection');
     await page.locator('#submit').click();
     assert.match(await page.locator('#formStatus').innerText(),/模型/,'empty manual model blocks creation');
     await page.locator('#model').fill('custom/cache-draft');
     assert.equal(await page.locator('#formStatus').innerText(),'','correcting the manual model clears its old error');
     await page.locator('#api_key').fill('synthetic-template-key-edited');
     assert.equal(await page.locator('#modelManualWrap').isVisible(),true,'credential editing does not change input mode');
     assert.equal(await page.locator('#model').inputValue(),'custom/cache-draft','credential editing preserves explicit manual input');
     await page.locator('#manualModelToggle').click();
     await page.route('**/models',route=>route.fulfill({json:{models:['replacement-model']}}));
     await page.locator('#loadModels').click();
     await page.waitForFunction(()=>document.querySelector('#modelListHint').textContent.includes('本次列表未包含'));
     assert.equal(await page.locator('#modelSelect').inputValue(),'quality-regressed','a missing directory entry remains visible as unverified');
     await page.unroute('**/models');
     await page.locator('#manualModelToggle').click();
     await page.locator('#model').fill('synthetic-pressure-model');
     await page.locator('#api_key').fill('synthetic-template-key');
     await page.locator('#profile').selectOption('cache');
     for(const template of templates){
       await page.locator(`#templatePickerBody [data-template="${template.id}"]`).click();
       assert.equal(await page.locator('#profile').inputValue(),'cache',template.id+': template preserves cache workload');
       assert.equal(await page.locator(`#templatePickerBody [data-template="${template.id}"]`).getAttribute('aria-pressed'),'true');
       assert.equal(await page.locator('#concurrency').inputValue(),'20',template.id+': concurrency');
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
       assert.equal(applied.profile,'cache','submitted configuration retains the chosen request content');
     }
     await page.locator('[data-mode="smoke"]').click();
     await page.locator('#rpm').fill('321');await page.locator('#iterations').fill('77');
     await page.locator('[data-mode="ramp"]').click();
     await page.locator('#ramps').fill('30,90,180');await page.locator('#duration').fill('42');
     await page.locator('[data-mode="soak"]').click();
     await page.locator('#rpm').fill('456');await page.locator('#duration').fill('900');
     await page.locator('[data-mode="ramp"]').click();
     assert.equal(await page.locator('#ramps').inputValue(),'30,90,180','ramp draft survives mode navigation');
     assert.equal(await page.locator('#duration').inputValue(),'42','ramp duration does not inherit the soak duration');
     await page.locator('[data-mode="smoke"]').click();
     assert.equal(await page.locator('#rpm').inputValue(),'321','smoke rate draft survives mode navigation');
     assert.equal(await page.locator('#iterations').inputValue(),'77','smoke count draft survives mode navigation');
     assert.equal(await page.locator('#templatePickerBody .template-chip.active').count(),0,'manual edits clear the template highlight');
     assert.equal(await page.locator('#profile').inputValue(),'cache','changing pressure modes retains request content');
     await page.locator('#templatePickerBody [data-template="soak-short-10m"]').click();
     await page.locator('#profile').selectOption('long');
     assert.equal(await page.locator('#templatePickerBody [data-template="soak-short-10m"]').getAttribute('aria-pressed'),'true','request content is independent of the selected pressure template');
     const submittedTasks=[];
     const captureSubmit=request=>{if(request.method()==='POST'&&new URL(request.url()).pathname==='/tasks')submittedTasks.push(request.url())};
     page.on('request',captureSubmit);
     await page.locator('#duration').fill('');
     await page.locator('#submit').click();
     await page.waitForFunction(()=>document.getElementById('formStatus').textContent.includes('请填写计划调度时长'));
     assert.equal(await page.locator('#duration').getAttribute('aria-invalid'),'true','empty duration is marked instead of silently defaulted');
     assert.deepEqual(submittedTasks,[],'invalid pressure does not create a charged task');
     page.off('request',captureSubmit);
     await page.locator('#templatePickerBody [data-template="soak-short-10m"]').click();
     assert.equal(await page.locator('#toast').evaluate(node=>node.classList.contains('show')),false,'successful template application dismisses only its obsolete form-error toast');
     assert.equal(await page.locator('#formStatus').evaluate(node=>node.classList.contains('error')),false,'template replacement clears obsolete form error styling');
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
   }else if(module==='routing'){
     assert.equal(await page.locator('#routingConcurrency').inputValue(),'20');
     assert.equal(await page.locator('#routingRpm').inputValue(),'100');
     await page.locator('#routingConcurrency').fill('7');
     await page.locator('#routingRpm').fill('350');
   }
   await page.getByRole('button',{name:'关闭新建测试',exact:true}).click();
   await page.waitForFunction(()=>!document.getElementById('config').classList.contains('mobile-open'));
   await page.locator('#empty .empty-action').click();
   await page.locator('#config.mobile-open').waitFor();
   if(module==='gateway'){
     assert.equal(await page.locator('#rpm').inputValue(),'100','new pressure resets to default');
     assert.equal(await page.locator('#concurrency').inputValue(),'20','new concurrency resets to default');
     await page.locator('[data-mode="ramp"]').click();
     assert.equal(await page.locator('#duration').inputValue(),'60','new tasks discard previous ramp drafts');
     await page.locator('[data-mode="smoke"]').click();
     await page.locator('#templatePickerToggle').click();
     await page.locator('#templatePickerBody [data-template="soak-30m"]').click();
     assert.equal(await page.locator('#rpm').inputValue(),'100','long soak also resets to100 on reopened drawer');
     assert.equal(await page.locator('#concurrency').inputValue(),'20');
   }else if(module==='routing'){
     assert.equal(await page.locator('#routingConcurrency').inputValue(),'20','new routing concurrency resets');
     assert.equal(await page.locator('#routingRpm').inputValue(),'100','new routing rate resets');
   }
   await page.getByRole('button',{name:'关闭新建测试',exact:true}).click();
 }
 await page.locator('[data-module="gateway"]').click();
 await page.locator('#gatewaySubnav [data-view="compare"]').click();
 await page.locator('#recordCreateButton').click();
 assert.equal(await page.locator('#profile').isVisible(),true,'comparison requests expose their shared content selection');
 await page.locator('#profile').selectOption('cache');
 assert.equal(await page.evaluate(()=>readComparison().shared_config.profile),'cache','comparison uses its visible request content');
 assert.deepEqual(await page.locator('#groupFields .group-remove').evaluateAll(nodes=>nodes.map(node=>node.disabled)),[true,true],'comparison retains at least two groups');
 await page.locator('#groupFields .group-role').nth(1).selectOption('baseline');
 await page.waitForFunction(()=>document.querySelectorAll('#groupFields .group-role')[0].value==='comparison');
 assert.deepEqual(await page.evaluate(()=>readComparison().groups.map(group=>group.role)),['comparison','baseline'],'reading a comparison honors the latest baseline choice');
 assert.deepEqual(await page.evaluate(()=>readComparison().groups.map(group=>group.role)),['comparison','baseline'],'a second preflight read does not restore the first baseline');
 await page.locator('#comparisonFields [data-click-action="addComparisonGroup"]').click();
 await page.locator('#groupFields .group-role').nth(2).selectOption('baseline');
 await page.locator('#groupFields .group-remove').nth(2).click();
 assert.equal(await page.locator('#groupFields .group-row').count(),2);
 assert.deepEqual(await page.evaluate(()=>readComparison().groups.map(group=>group.role)),['comparison','comparison'],'removing a baseline does not silently replace it');
 await page.locator('#groupFields .group-role').nth(1).selectOption('baseline');
 assert.deepEqual(await page.locator('#groupFields .group-concurrency').evaluateAll(nodes=>nodes.map(el=>el.value)),['20','20']);
 assert.deepEqual(await page.locator('#groupFields .group-rpm').evaluateAll(nodes=>nodes.map(el=>el.value)),['100','100']);
 await page.locator('#url').fill(endpoints.upstream+'/v1');
 await page.locator('#api_key').fill('synthetic-comparison-key');
 await page.locator('#manualModelToggle').click();
 await page.locator('#model').fill('quality-oracle');
 await page.waitForFunction(()=>document.querySelector('#preflightCard').textContent.includes('40 次请求'));
 assert.doesNotMatch(await page.locator('#preflightCard').innerText(),/请填写网关 URL/,'comparison estimates use the shared connection and sum both groups');
 assert.equal(await page.locator('#groupFields label').evaluateAll(labels=>labels.every(label=>Boolean(document.getElementById(label.htmlFor)))),true,'every group field has a working label');
 await page.locator('#groupFields .group-rpm').nth(1).fill('');
 await page.locator('#submit').click();
 assert.match(await page.locator('#formStatus').innerText(),/实验组 2/,'empty group pressure blocks creation');
 await page.locator('#groupFields .group-rpm').nth(1).fill('100');
 assert.equal(await page.locator('#formStatus').innerText(),'','correcting group pressure clears its old error');
 await captureCreate(page,'gateway-compare');
 await page.getByRole('button',{name:'关闭新建测试',exact:true}).click();
 await page.locator('[data-module="quality"]').click();
 await page.locator('#qualitySubnav [data-view="compare"]').click();
 await page.locator('#recordCreateButton').click();
 await captureCreate(page,'quality-compare');
 await page.getByRole('button',{name:'关闭新建测试',exact:true}).click();
 await page.locator('[data-module="connections"]').click();
 await page.getByRole('heading',{name:'还没有常用连接',exact:true}).waitFor();
 await page.locator('#recordCreateButton').click();
 await page.locator('#connectionName').waitFor();
 assert.equal(await page.locator('#connectionDrawer').getAttribute('aria-hidden'),'false');
 await page.getByRole('button',{name:'关闭常用连接表单',exact:true}).click();
 await page.waitForFunction(()=>!ConnectionUI.isOpen());
 await page.locator('[data-module="fingerprint"]').click();
 await page.locator('#recordCreateButton').click();
 await page.locator('#fpConcurrency').waitFor({state:'attached'});
 assert.equal(await page.locator('#fpConcurrency').inputValue(),'20');
 assert.equal(await page.locator('#fpRpm').inputValue(),'100');
 await captureCreate(page,'fingerprint','#fingerprintDrawer');
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
 // Compact navigation keeps every module and all utilities reachable without horizontal scrolling.
 for(const width of [980,390]){
   await page.setViewportSize({width,height:844});
   await page.waitForFunction(()=>document.querySelector('.top .rail-tools'));
   const outside=await page.locator('.module-nav-item,.top .rail-btn,.top-action:visible').evaluateAll(nodes=>nodes.filter(el=>{const r=el.getBoundingClientRect();return r.left<0||r.right>innerWidth||r.width<1}).map(el=>el.textContent.trim()));
   assert.deepEqual(outside,[],`navigation controls are fully visible at ${width}`);
 }
 await page.locator('#recordToggle').click();
 assert.equal(await page.locator('#recordToggle').getAttribute('aria-expanded'),'false');
 assert.equal(await page.locator('#recordPanel .task-list:visible').count(),0,'record lists collapse without losing selected detail');
 await page.locator('#recordToggle').focus();await page.keyboard.press('Enter');
 assert.equal(await page.locator('#recordToggle').getAttribute('aria-expanded'),'true');
 for(const theme of ['light','dark']){
   await page.evaluate(theme=>setTheme(theme),theme);
   await page.evaluate(()=>new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(resolve))));
   await page.screenshot({path:path.join(output,`compact-navigation-390-${theme}.png`)});
 }
 await page.evaluate(()=>setTheme('light'));
 await page.setViewportSize({width:1440,height:1000});
 await page.waitForFunction(()=>document.querySelector('.sidebar .rail-tools'));
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
 console.log(JSON.stringify({passed:true,modules:5,viewports:[1440,980,640,390],checks:['login','creation buttons','all six pressure templates','template keeps connection and cache workload','independent pressure mode drafts','custom pressure clears template selection','empty pressure prevents charged submission','comparison request content is visible','comparison baseline selection and minimum groups','reopened pressure defaults','connections module','model discovery','select spacing','dark theme','CSP','cross-origin rejection','ETag']},null,2));
})().catch(e=>{console.error(e);process.exitCode=1}).finally(async()=>{try{if(browser)await browser.close()}finally{await servers.close()}});
