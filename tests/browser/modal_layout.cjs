/** Isolated modal geometry, scrolling, focus and methodology regression. No paid upstream. */
const {chromium}=require('playwright');
const assert=require('node:assert/strict'),fs=require('fs'),path=require('path'),os=require('os');
const {spawn}=require('child_process');
const output=process.env.UI_ARTIFACT_DIR||fs.mkdtempSync(path.join(os.tmpdir(),'route-modal-layout-'));
fs.mkdirSync(output,{recursive:true});
let server,browser;
async function start(){server=spawn(process.env.PYTHON||'python3',['-u',path.join(__dirname,'workflow_server.py'),output]);server.stderr.on('data',d=>fs.appendFileSync(path.join(output,'server.log'),d));return new Promise((resolve,reject)=>{let text='';server.stdout.on('data',d=>{text+=d;if(text.includes('\n'))try{resolve(JSON.parse(text.split('\n')[0]))}catch(e){reject(e)}});server.on('error',reject);server.on('exit',c=>reject(Error('server '+c)))})}
(async()=>{
 const {base}=await start();browser=await chromium.launch({headless:true,executablePath:process.env.BROWSER_PATH||process.env.CHROME_PATH||undefined});
 const context=await browser.newContext(),page=await context.newPage(),errors=[],results=[];
 page.on('pageerror',e=>errors.push(e.message));
 await context.route('**/*',r=>new URL(r.request().url()).origin===base?r.continue():r.abort());
 assert.equal((await context.request.get(base+'/quality-methodology')).status(),401);
 await context.request.post(base+'/auth/setup',{data:{username:'modal-regression',password:'local-test-888@'}});
 await page.goto(base);await page.waitForFunction(()=>authenticated&&refreshTimer!==null&&!refreshing);
 await page.evaluate(async()=>{clearInterval(refreshTimer);currentQualitySuite=await api('/quality-suite')});
 for(const viewport of [{width:1440,height:900},{width:390,height:844},{width:844,height:390}]){
  await page.setViewportSize(viewport);
  for(const id of ['qualitySuiteModal','qualityEvidenceModal','rawModal','accountModal','qualityMethodologyModal']){
   await page.evaluate(async id=>{
    if(id==='qualitySuiteModal'){showQualitySuite();document.querySelectorAll('#qualitySuiteBody details').forEach(e=>e.open=true)}
    if(id==='qualityMethodologyModal')await showQualityMethodology();
    if(id==='qualityEvidenceModal'){
     const c=currentQualitySuite.cases.find(c=>c.category==='long_context');
     window.currentQualityDetail={config:{capture_raw:true},resources:[{name:'本地布局样例',case_results:[{...c,case_id:c.id,response:'验证用长输出\n'.repeat(500),raw_input:{messages:[{content:c.prompt}]},raw_output:'本地模拟输出'.repeat(1000),checks:['布局测试'],failures:[],score:1,passed:true,completed:true}]}]};
     showQualityCase(0,0);
    }
    if(id==='rawModal'){openModal(id);document.getElementById('rawTitle').textContent='布局测试：非常长的响应标识符'+('abcdefgh'.repeat(15));document.getElementById('rawInput').textContent='本地输入 '+('字'.repeat(500))+'\n'+'input line\n'.repeat(200);document.getElementById('rawOutput').textContent='output line\n'.repeat(250)}
    if(id==='accountModal'){await openAccountModal();document.getElementById('accountList').innerHTML=Array.from({length:35},(_,i)=>`<div class="account-row"><b>布局账号${i}</b><span>使用者</span></div>`).join('')}
   },id);
   const modal=page.locator('#'+id),panel=modal.locator('.modal-panel'),body=modal.locator('.modal-body'),header=modal.locator('.modal-head,.account-modal-head');
   assert.equal(await modal.isVisible(),true);
   const geometry=await panel.evaluate(e=>{const b=e.querySelector('.modal-body'),h=e.querySelector('.modal-head,.account-modal-head'),r=x=>{const q=x.getBoundingClientRect();return {left:q.left,right:q.right,top:q.top,bottom:q.bottom,width:q.width,height:q.height}};return {panel:r(e),body:r(b),header:r(h),overflow:getComputedStyle(e).overflow,bodyOverflow:getComputedStyle(b).overflow,bodyWidth:b.clientWidth,bodyAllWidth:b.scrollWidth}});
   assert.equal(geometry.overflow,'hidden',id+' shell must clip rather than scroll');
   assert.equal(geometry.bodyOverflow,'auto');
   assert.ok(geometry.panel.left>=8&&geometry.panel.right<=viewport.width-8,JSON.stringify(geometry));
   assert.ok(geometry.panel.top>=8&&geometry.panel.bottom<=viewport.height-8,JSON.stringify(geometry));
   assert.ok(geometry.body.bottom<=geometry.panel.bottom&&geometry.body.top>=geometry.header.bottom-1,JSON.stringify(geometry));
   assert.ok(geometry.bodyAllWidth<=geometry.bodyWidth+1, id+' unexpected horizontal body overflow');
   const headerBefore=await header.boundingBox(),backgroundBefore=await page.evaluate(()=>document.querySelector('.main').scrollTop);
   await body.evaluate(e=>e.scrollTop=300);await page.waitForTimeout(30);
   const headerAfter=await header.boundingBox();assert.equal(headerAfter.y,headerBefore.y,'header moved');
   // Wheel at the internal scrollbar gutter must stay within the dialog, even at the bottom.
   const b=await body.boundingBox();await page.mouse.move(b.x+b.width-5,b.y+b.height/2);await page.mouse.wheel(0,300);await page.waitForTimeout(80);
   const canScroll=await body.evaluate(e=>e.scrollHeight>e.clientHeight);
   if(canScroll)assert.ok(await body.evaluate(e=>e.scrollTop)>0,id+' body does not scroll');
   await body.evaluate(e=>e.scrollTop=e.scrollHeight);await page.mouse.wheel(0,800);await page.waitForTimeout(60);
   assert.equal(await page.evaluate(()=>document.querySelector('.main').scrollTop),backgroundBefore,'background scrolled');
   const focusables=modal.locator('button:visible,a:visible,input:visible,summary:visible,[tabindex="0"]:visible');
   await focusables.last().focus();await page.keyboard.press('Tab');
   assert.equal(await modal.evaluate(e=>e.contains(document.activeElement)),true,'focus escaped dialog');
   await body.evaluate(e=>e.scrollTop=0);
   await page.screenshot({path:path.join(output,`${id}-${viewport.width}x${viewport.height}.png`)});
   await page.keyboard.press('Escape');assert.equal(await modal.isVisible(),false);
   results.push(`${id}@${viewport.width}x${viewport.height}`);
  }
 }
 // Nested method dialog: close only the top layer and preserve the parent scroll/focus.
 await page.setViewportSize({width:1440,height:900});
 await page.evaluate(()=>{setModule('quality');openQualityCreate('single')});
 await page.waitForFunction(()=>document.getElementById('qualitySuiteSummary').innerText.includes('次请求'));
 await page.getByRole('button',{name:'查看全部题目、答案与规则'}).click();
 await page.locator('#qualitySuiteModal').getByRole('button',{name:'评测方法与官方参考'}).click();
 await page.waitForFunction(()=>document.getElementById('qualityMethodologyBody').innerText.includes('IFEval'));
 assert.equal(await page.locator('#qualityMethodologyBody a[target="_blank"]').count()>7,true);
 await page.keyboard.press('Escape');assert.equal(await page.locator('#qualitySuiteModal').isVisible(),true);
 assert.equal(await page.locator('#qualityMethodologyModal').isVisible(),false);
 assert.equal(await page.evaluate(()=>document.getElementById('qualitySuiteModal').contains(document.activeElement)),true);
 await page.keyboard.press('Escape');assert.equal(await page.locator('#config').evaluate(e=>e.classList.contains('mobile-open')),true);
 await page.keyboard.press('Escape');assert.equal(await page.locator('#config').evaluate(e=>e.classList.contains('mobile-open')),false);
 assert.deepEqual(errors,[]);
 console.log(JSON.stringify({passed:true,modalViewportChecks:results.length,nestedEscape:true,focusTrap:true,bodyScroll:true,headerFixed:true,methodology:true,pageErrors:errors,output},null,2));
})().catch(e=>{console.error(e);process.exitCode=1}).finally(async()=>{if(browser)await browser.close();if(server)server.kill('SIGTERM')});
