/**
 * Interactive creation audit against an isolated local server and memory credentials.
 * Uses real fills/clicks/selection changes; never starts calibration or paid requests.
 */
'use strict';
const {chromium}=require('playwright'),assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path');
const artifacts=require('./artifacts.cjs').browserArtifacts('tpm_connections_create'),{projectRoot:root,output}=artifacts;
artifacts.env.TPM_BROWSER_MEMORY_CREDENTIALS='1';
const servers=require('./server.cjs').browserServers(artifacts,{python:process.env.PYTHON||path.join(root,'.venv/bin/python')});
const observations=[],geometry=[],errors=[],blocked=[],requests=[];
let browser,page,saveFailure=false,connectionReadFailure=false,modelList=null;
function observe(name,actual,expected){observations.push({name,actual,expected,passed:JSON.stringify(actual)===JSON.stringify(expected)})}
async function capture(name,drawer,field){
 if(field){await page.locator(field).scrollIntoViewIfNeeded();await page.locator(field).focus()}
 const bounds=await page.locator(drawer).evaluate(node=>{
  const rect=el=>{const r=el.getBoundingClientRect();return {top:r.top,bottom:r.bottom,left:r.left,right:r.right}};
  const body=node.querySelector('.drawer-body'),footer=node.querySelector('.drawer-footer'),active=document.activeElement,r=active?.getBoundingClientRect();
  const hit=r?.width?document.elementFromPoint(r.left+r.width/2,r.top+r.height/2):null;
  return {body:rect(body),footer:rect(footer),active:active?.id,field:r?rect(active):null,hit:hit===active||active?.contains(hit),overflow:node.scrollWidth>node.clientWidth+1};
 });
 geometry.push({name,...bounds});
 assert.ok(bounds.body.bottom<=bounds.footer.top+1,name+' body remains above footer');
 assert.equal(bounds.overflow,false,name+' drawer has no horizontal overflow');
 if(field){assert.ok(bounds.field.top>=bounds.body.top-1&&bounds.field.bottom<=bounds.body.bottom+1,name+' focused field is fully visible');assert.equal(bounds.hit,true,name+' focused field is unobscured')}
 await page.screenshot({path:path.join(output,name+'.png'),animations:'disabled'});
}
async function closeTpm(){await page.locator('#tpmDrawer .drawer-close').click();await page.locator('#tpmDrawer').waitFor({state:'hidden'})}
async function ensureAdvanced(){if(!await page.locator('#tpmAdvanced').evaluate(node=>node.open))await page.locator('#tpmAdvanced > summary').click()}
async function awaitModels(model){await page.waitForFunction(model=>!document.getElementById('tpm_model').disabled&&[...document.getElementById('tpm_model').options].some(option=>option.value===model),model)}
(async()=>{
 const endpoints=await servers.start();
 browser=await chromium.launch({headless:true,executablePath:process.env.CHROME_PATH||'/Applications/Google Chrome.app/Contents/MacOS/Google Chrome'});
 const gallery=await browser.newPage({viewport:{width:1440,height:1000},reducedMotion:'reduce'});
 await gallery.goto('file://'+path.join(root,'docs/design-system/gallery.html'));
 await gallery.locator('#forms').scrollIntoViewIfNeeded();
 await gallery.screenshot({path:path.join(output,'gallery-forms-light.png')});
 await gallery.close();
 const context=await browser.newContext({viewport:{width:1440,height:1000},reducedMotion:'reduce'});
 page=await context.newPage();page.on('pageerror',error=>errors.push(error.message));page.on('dialog',dialog=>dialog.dismiss());
 await context.route('**/*',async route=>{
  const req=route.request(),url=new URL(req.url());
  if(url.hostname!=='127.0.0.1'){blocked.push(req.url());return route.abort()}
  if(req.method()==='POST')requests.push({path:url.pathname,body:req.postDataJSON()});
  if(req.method()==='POST'&&/^\/tpm\/runs(?:$|\/.*\/start$)/.test(url.pathname)){blocked.push(url.pathname);return route.fulfill({status:403,json:{error:'Creation audit never starts calibration'}})}
  if(url.pathname==='/connections'&&req.method()==='POST'&&saveFailure)return route.fulfill({status:503,json:{error:'合成保存失败：请保留输入并重新提交。'}});
  if(url.pathname==='/connections'&&req.method()==='GET'&&connectionReadFailure)return route.fulfill({status:503,json:{error:'合成连接目录暂时不可用'}});
  if(url.pathname==='/models'&&modelList)return route.fulfill({json:{ok:true,models:modelList}});
  return route.continue();
 });
 await page.goto(endpoints.base);
 await page.locator('#authUsername').fill('creation-audit');
 await page.locator('#authPassword').fill('Synthetic-creation-audit-2026');
 await page.locator('#authConfirm').fill('Synthetic-creation-audit-2026');
 await page.locator('#authSubmit').click();await page.locator('#authGate').waitFor({state:'hidden'});
 await page.waitForFunction(()=>refreshTimer!==null&&!refreshing);
 await page.locator('[data-module="connections"]').click();await page.locator('#recordCreateButton').click();
 await page.waitForFunction(()=>document.activeElement.id==='connectionName');
 await page.locator('#connectionSubmit').click();await page.waitForFunction(()=>document.activeElement.id==='connectionName');
 observe('empty connection focuses name',await page.evaluate(()=>document.activeElement.id),'connectionName');
 await page.locator('#connectionName').fill('实际操作 · 合成连接');
 await page.locator('#connectionUrl').fill('not-a-url');await page.locator('#connectionSubmit').click();
 observe('invalid connection address receives focus',await page.evaluate(()=>document.activeElement.id),'connectionUrl');
 await page.locator('#connectionUrl').fill(endpoints.upstream+'/v1');await page.locator('#connectionKey').fill('synthetic-creation-key');
 const eye=page.locator('#connectionDrawer .password-toggle');
 await eye.click();observe('typed key is revealed',await page.locator('#connectionKey').getAttribute('type'),'text');
 await eye.click();observe('masking typed key retains draft',await page.locator('#connectionKey').inputValue(),'synthetic-creation-key');
 saveFailure=true;await page.locator('#connectionSubmit').click();await page.getByText('合成保存失败：请保留输入并重新提交。',{exact:true}).waitFor();
 observe('failed save retains complete input',await page.locator('#connectionName,#connectionUrl,#connectionKey').evaluateAll(nodes=>nodes.map(node=>node.value)),['实际操作 · 合成连接',endpoints.upstream+'/v1','synthetic-creation-key']);
 for(const width of [1440,980,390])for(const theme of ['light','dark']){
  await page.setViewportSize({width,height:width===390?700:1000});await page.evaluate(theme=>setTheme(theme),theme);
  await capture(`connections-save-error-${width}-${theme}`,'#connectionDrawer','#connectionKey');
 }
 saveFailure=false;await page.locator('#connectionSubmit').click();await page.locator('#connectionDrawer').waitFor({state:'hidden'});
 const saved=(await page.evaluate(()=>ConnectionUI.list()))[0];assert.ok(saved.id);
 await page.locator('[data-click-action="openConnectionEdit"]').first().click();
 observe('editing never exposes a saved key automatically',await page.locator('#connectionKey').inputValue(),'');
 await eye.click();await page.waitForFunction(()=>document.getElementById('connectionKey').value==='synthetic-creation-key');
 await eye.click();observe('hiding retrieved key clears plaintext',await page.locator('#connectionKey').inputValue(),'');
 await page.locator('#connectionUrl').fill(endpoints.upstream+'/new/v1');await page.locator('#connectionSubmit').click();
 observe('changing saved address requires replacement credentials',await page.evaluate(()=>document.activeElement.id),'connectionKey');
 await page.locator('#connectionUrl').fill(endpoints.upstream+'/v1');await page.locator('#connectionDrawer .drawer-close').click();
 await page.setViewportSize({width:1440,height:1000});await page.evaluate(()=>setTheme('light'));
 await page.locator('[data-module="tpm"]').click();await page.locator('#recordCreateButton').click();await page.locator('#tpmDrawer').waitFor({state:'visible'});
 observe('new TPM draft requires an explicit connection',await page.locator('#tpm_connection_id').inputValue(),'');
 await page.locator('#tpm_connection_id').selectOption(saved.id);await awaitModels('tpm-mock');
 observe('connection discovery requires an explicit model',await page.locator('#tpm_model').inputValue(),'');
 await page.locator('#tpm_model').selectOption('tpm-mock');await page.locator('#tpmModelsButton').click();await awaitModels('tpm-mock');
 observe('model refresh preserves the chosen available model',await page.locator('#tpm_model').inputValue(),'tpm-mock');
 await page.locator('#tpmModelMode [data-arg0="manual"]').click();await page.locator('#tpm_model_override').fill('synthetic-override');
 await page.locator('#tpmModelMode [data-arg0="list"]').click();observe('manual to list retains list model',await page.locator('#tpm_model').inputValue(),'tpm-mock');
 await page.locator('#tpmModelMode [data-arg0="manual"]').click();observe('list to manual retains manual draft',await page.locator('#tpm_model_override').inputValue(),'synthetic-override');
 await page.locator('#tpmModelMode [data-arg0="list"]').click();
 await page.locator('#tpm_target_preset').selectOption('custom');await page.locator('#tpm_target_tpm').fill('123456789');
 await page.locator('#tpm_target_preset').selectOption('50000000');await page.locator('#tpm_target_duration_seconds').focus();await page.locator('#tpm_target_preset').selectOption('custom');
 observe('custom target survives a preset round trip',await page.locator('#tpm_target_tpm').inputValue(),'123456789');
 await ensureAdvanced();await page.locator('#tpm_execution_mode').selectOption('manual');await page.locator('#tpm_concurrency').fill('7');await page.locator('#tpm_pool_connections').fill('9');
 await page.locator('#tpm_execution_mode').selectOption('auto');
 await page.waitForFunction(()=>Number(document.getElementById('tpm_concurrency').value)!==7&&!document.getElementById('tpmExecutionPlan').textContent.includes('正在'));
 await page.locator('#tpm_execution_mode').selectOption('manual');
 observe('manual capacity survives automatic planning',await page.locator('#tpm_concurrency,#tpm_pool_connections').evaluateAll(nodes=>nodes.map(node=>node.value)),['7','9']);
 await page.locator('#tpm_execution_mode').selectOption('auto');
 await page.locator('#tpm_warmup_requests').fill('4');await page.locator('#tpm_expected_cache_hit_rate').fill('0.75');
 const budget=await page.locator('#tpm_budget_usd').inputValue();
 await page.locator('#tpm_cache_mode').selectOption('avoid_cache');observe('non-cache mode conceals cache-only settings',await page.locator('#tpmCacheSettings').isVisible(),false);
 await page.locator('#tpm_cache_mode').selectOption('prefer_cache');
 observe('cache round trip retains cache settings and budget',await page.locator('#tpm_warmup_requests,#tpm_expected_cache_hit_rate,#tpm_budget_usd').evaluateAll(nodes=>nodes.map(node=>node.value)),[budget,'4','0.75']);
 await page.locator('#tpm_cache_write_price_mode').selectOption('explicit');await page.locator('#tpm_price_cache_write').fill('0.123');
 await page.locator('#tpm_cache_write_price_mode').selectOption('input');await page.locator('#tpm_cache_write_price_mode').selectOption('explicit');
 observe('independent write price survives mode round trip',await page.locator('#tpm_price_cache_write').inputValue(),'0.123');
 await page.locator('#tpm_usage_semantics').selectOption('independent');await page.locator('#tpm_usage_semantics_confirmed').check();
 await page.locator('#tpm_usage_semantics').selectOption('openai');await page.locator('#tpm_usage_semantics').selectOption('independent');
 observe('usage acknowledgement survives reversible mode change',await page.locator('#tpm_usage_semantics_confirmed').isChecked(),true);
 await page.locator('#tpm_usage_semantics').selectOption('openai');
 await page.locator('#tpm_connection_id').selectOption('__manual__');await page.locator('#tpm_url').fill(endpoints.upstream+'/new/v1');await page.locator('#tpm_api_key').fill('synthetic-manual-draft');
 await page.locator('#tpmModelsButton').click();await awaitModels('tpm-new-model');await page.locator('#tpm_model').selectOption('tpm-new-model');
 await page.locator('#tpm_connection_id').selectOption(saved.id);await awaitModels('tpm-mock');await page.locator('#tpm_connection_id').selectOption('__manual__');
 observe('manual connection survives saved connection round trip',await page.locator('#tpm_url,#tpm_api_key').evaluateAll(nodes=>nodes.map(node=>node.value)),[endpoints.upstream+'/new/v1','synthetic-manual-draft']);
 await page.locator('#tpm_url').fill(endpoints.upstream+'/v1');await page.locator('#tpm_api_key').fill('synthetic-manual-final');await page.locator('#tpmModelsButton').click();await awaitModels('tpm-mock');await page.locator('#tpm_model').selectOption('tpm-mock');
 modelList=['synthetic-replacement-model'];await page.locator('#tpmModelsButton').click();await awaitModels('synthetic-replacement-model');
 observe('missing previous model is explicitly explained',/已不在|不再|已移除|重新选择/.test(await page.locator('#tpmModelStatus').innerText()),true);
 await page.locator('#tpmModelMode [data-arg0="manual"]').click();observe('manual model hides list-only warning',await page.locator('#tpmModelStatus').textContent(),'');
 await page.locator('#tpmModelMode [data-arg0="list"]').click();assert.match(await page.locator('#tpmModelStatus').textContent(),/已不在/);
 await page.locator('#tpm_model').selectOption('synthetic-replacement-model');modelList=null;
 observe('choosing the replacement clears the old model warning',await page.locator('#tpmModelStatus').textContent(),'');
 for(const width of [1440,980,390])for(const theme of ['light','dark']){
  await page.setViewportSize({width,height:width===390?700:1000});await page.evaluate(theme=>setTheme(theme),theme);
  await ensureAdvanced();await page.locator('#tpmAdvanced > summary').click();
  await capture(`tpm-model-and-mode-${width}-${theme}`,'#tpmDrawer','#tpm_model');
  await page.locator('#tpmModelMode [data-arg0="manual"]').click();
  await capture(`tpm-manual-model-${width}-${theme}`,'#tpmDrawer','#tpm_model_override');
  await page.locator('#tpmModelMode [data-arg0="list"]').click();
  await ensureAdvanced();await capture(`tpm-advanced-last-field-${width}-${theme}`,'#tpmDrawer','#tpm_seed');
  await page.locator('#tpmSubmit').click();await page.waitForFunction(()=>document.getElementById('tpmPreview').textContent.includes('确认校准费用'));
  const previewSpacing=await page.locator('#tpmPreview > .report-stack').evaluate(stack=>{const [notice,metrics]=stack.children;return metrics.getBoundingClientRect().top-notice.getBoundingClientRect().bottom});
  assert.ok(previewSpacing>=12,`TPM preview notice and cost metrics have visible separation at ${width} ${theme}`);
  await capture(`tpm-preview-${width}-${theme}`,'#tpmDrawer');
 }
 await closeTpm();
 connectionReadFailure=true;
 const failedDirectory=page.waitForResponse(response=>new URL(response.url()).pathname==='/connections'&&response.status()===503);
 await page.locator('#recordCreateButton').click();await failedDirectory;await page.locator('#tpmDrawer').waitFor({state:'visible'});
 await page.waitForFunction(()=>document.getElementById('tpmFormStatus').textContent.includes('可手动填写'));
 observe('connection directory failure still permits manual creation',await page.locator('#tpmFormStatus').textContent().then(text=>text.includes('可手动填写')),true);
 await capture('tpm-connection-directory-error-390-dark','#tpmDrawer');
 await page.locator('#tpm_connection_id').selectOption('__manual__');
 observe('closed TPM draft never restores the previous secret',await page.locator('#tpm_api_key').inputValue(),'');
 await capture('tpm-connection-directory-unavailable-390-dark','#tpmDrawer','#tpm_url');
 await closeTpm();connectionReadFailure=false;
 assert.deepEqual(blocked,[]);assert.deepEqual(errors,[]);
 const report={observations,geometry,pageErrors:errors,unexpectedRequests:blocked,paidRequests:requests.filter(req=>/^\/tpm\/runs/.test(req.path)).length};
 fs.writeFileSync(path.join(output,'creation-audit.json'),JSON.stringify(report,null,2));
 const failed=observations.filter(item=>!item.passed);
 console.log(JSON.stringify({output,observations:observations.length,failures:failed},null,2));
 assert.deepEqual(failed,[],'interactive creation audit findings');
})().catch(async error=>{console.error(error);process.exitCode=1;if(page){await page.screenshot({path:path.join(output,'failure.png')}).catch(()=>{});fs.writeFileSync(path.join(output,'failure-observations.json'),JSON.stringify(observations,null,2))}}).finally(async()=>{if(browser)await browser.close();await servers.close()});
