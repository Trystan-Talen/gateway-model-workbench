/** Fingerprint workspace regression, synthetic HTTP only. Never calls an upstream.
 * NODE_PATH=<playwright modules> PYTHON=<python runtime> node tests/browser/fingerprint.cjs
 * UI_PROJECT_ROOT and UI_ASSET_ROOT can target a staging workspace.
 */
const {chromium} = require('playwright');
const assert = require('node:assert/strict');
const fs = require('node:fs'), path = require('node:path');
const {execFileSync} = require('node:child_process');
const {projectRoot:root,output,env}=require('./artifacts.cjs').browserArtifacts('fingerprint');
const assets = process.env.UI_ASSET_ROOT || path.join(root,'static');
const python = process.env.PYTHON || 'python3';
const html = process.env.UI_HTML ? fs.readFileSync(process.env.UI_HTML,'utf8') : execFileSync(python,['-c','import route_test_ui; print(route_test_ui.INDEX_HTML)'],{cwd:root,encoding:'utf8',env});
const fixture = JSON.parse(execFileSync(python,['-c','import json; from fingerprint_protocol import catalog,make_protocol; print(json.dumps({"catalog":catalog(),"protocol":make_protocol({"preset":"quick","samples_per_cell":10,"concurrency":2}),"reasoning_protocol":make_protocol({"preset":"quick","samples_per_cell":10,"concurrency":2,"adapter":"openai_reasoning"})}))'],{cwd:root,encoding:'utf8',env}));
const origin = 'http://127.0.0.1:18769';
const errors = [], unexpected = [], assertions = [], allSubmissions = [];
const clone = x => JSON.parse(JSON.stringify(x));
const now = () => new Date().toISOString();
function artifact() {
  return {format:'workbench-fingerprint',format_version:1,protocol:fixture.protocol,collected_at:now(),complete:true,seed:17,endpoint:{url:'https://synthetic.invalid/v1',model:'synthetic-model'},cells:Object.fromEntries(fixture.protocol.cells.map(cell=>[cell,{counts:{blue:8,red:2},valid_count:10,total_count:10,invalid_count:0,refusal_count:0,empty_count:0,error_count:0,median_latency_ms:12,entropy_bits:.72}])),diagnostics:{valid_rate:1,error_rate:0,empty_rate:0,truncated_rate:0,extra_text_rate:0,reasoning_observed:false,returned_models:{'synthetic-model':40},model_echo_mismatch_count:0,split_half:{alternating:{mean_jsd:.05,comparable_cells:4},temporal:{mean_jsd:.04,comparable_cells:4},warning:false},cache:{observed_samples:40,cached_samples:10,uncached_samples:20,unknown_samples:10,cached_median_latency_ms:8,uncached_median_latency_ms:13},usage:{prompt_tokens:400,completion_tokens:40,reasoning_tokens:null,cached_tokens:100,reported_cost:null},warnings:[]},source:{url:'https://github.com/ToseaAI/llm-fingerprint-detector',commit:fixture.catalog.source_commit,method_version:fixture.catalog.method_version}};
}
function reference(id,name,extra={}) { const a = artifact(); return {id,name,source_note:'合成测试来源说明',trusted_source:true,imported:false,created:now(),protocol:a.protocol,collected_at:a.collected_at,complete:true,diagnostics:a.diagnostics,endpoint:a.endpoint,...extra}; }
async function geometry(page,width,label) {
  assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth+1),false,label+': page overflow');
  const duplicateIds = await page.locator('[id]').evaluateAll(nodes=>{const map={};for(const el of nodes)map[el.id]=(map[el.id]||0)+1;return Object.entries(map).filter(([,n])=>n>1)});
  assert.deepEqual(duplicateIds,[],label+': duplicate ids');
  const overflow = await page.locator('.fp-workspace,.fp-card,.report-head,.report-tabs,#fingerprintDrawer,.fp-modal-dialog,.fp-form-grid').evaluateAll(nodes=>nodes.filter(n=>n.getClientRects().length&&n.scrollWidth>n.clientWidth+1).map(n=>({id:n.id,class:n.className,width:n.clientWidth,scroll:n.scrollWidth})));
  assert.deepEqual(overflow,[],label+': component overflow');
  if(await page.locator('#fingerprintDrawer').evaluate(n=>n.classList.contains('mobile-open'))) {
    await page.waitForFunction(()=>Math.abs(document.getElementById('fingerprintDrawer').getBoundingClientRect().right-innerWidth)<1);
    const bounds = await page.locator('#fingerprintDrawer').boundingBox();
    assert.ok(bounds.x>=-1&&Math.abs(bounds.x+bounds.width-width)<1,label+': drawer bounds');
    assert.equal(await page.locator('.app').evaluate(n=>n.inert),true,label+': inert background');
  }
}
async function capture(page,width,state) {
  if(['overview','diagnostics','protocol','samples'].includes(state))await page.locator('#fpTabPanel').evaluate(node=>node.scrollIntoView({block:'start'}));
  for (const theme of ['light','dark']) {
    await page.evaluate(theme=>{setTheme(theme);document.getElementById('toast').classList.remove('show')},theme);
    await page.evaluate(()=>new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(resolve))));
    await page.waitForTimeout(250);
    await geometry(page,width,state);
    await page.screenshot({path:path.join(output,`fingerprint-${state}-${width}-${theme}.png`)});
  }
}
async function fillEndpoint(page,name='合成通道自检') {
  await page.locator('#fpName').fill(name);
  await page.locator('#fpUrl').fill('https://synthetic.invalid/v1');
  await page.locator('#fpKey').fill('synthetic-secret-never-send');
  await page.locator('[data-fp-action="model-input-mode"][data-fp-model-mode="manual"]').click();
  await page.locator('#fpModel').fill('synthetic-model');
}
(async()=>{
  const options={headless:true};
  const chrome='/Applications/Google Chrome.app/Contents/MacOS/Google Chrome';
  if(process.env.CHROME_PATH||fs.existsSync(chrome)) options.executablePath=process.env.CHROME_PATH||chrome;
  const browser=await chromium.launch(options);
  try {
    for(const width of [390,980,1440]) {
      const context=await browser.newContext({viewport:{width,height:width===390?844:1000},reducedMotion:'reduce'});
      const page=await context.newPage();
      let runs=[], refs=[reference('ref-good','可信参考 <img src=x onerror=alert(1)>'),reference('ref-old','过期参考',{collected_at:'2020-01-01T00:00:00Z'}),reference('ref-external','外部协议参考',{protocol:{version:'external',external_protocol:true},imported:true})];
      const legacyProtocol=clone(fixture.protocol);delete legacyProtocol.rpm;legacyProtocol.id='synthetic-legacy-no-rate';refs.push(reference('ref-legacy','旧版参考',{protocol:legacyProtocol}));
      refs.push(reference('ref-reasoning','思考协议参考',{protocol:fixture.reasoning_protocol}));
      let samplesFail=false, catalogFail=false, compareCount=0, imported=0;
      const sampleRows=Array.from({length:73},(_,i)=>({index:i,cell_id:fixture.protocol.cells[i%4],paraphrase_index:i%3,started_at:now(),finished_at:now(),latency_ms:12,status_code:200,category:i===1?'invalid':'valid',normalized:'blue',raw:i===1?'<img src=x onerror=alert(1)>':'blue',extra_text:false,finish_reason:'stop',truncated:false,returned_model:'synthetic-model',usage:{prompt_tokens:10,completion_tokens:1,reported_cost:null},error:null}));
      page.on('pageerror',e=>errors.push({width,message:e.message}));
      page.on('dialog',async d=>{errors.push({width,message:'Unexpected dialog: '+d.message()});await d.dismiss()});
      await context.route('**/*',async route=>{
    if(await require('./assets.cjs').serveAsset(route,root,{fingerprintRoot:assets}))return;
        const req=route.request(), url=new URL(req.url()), endpoint=url.pathname, method=req.method();
        if(url.origin!==origin){unexpected.push(req.url());return route.abort()}
        if(endpoint==='/')return route.fulfill({contentType:'text/html',body:html});
        let data;
        if(endpoint==='/auth/status')data={authenticated:true,user:{username:'合成回归',role:'user'}};
        else if(endpoint==='/config')data={config:{url:'https://unrelated.invalid/v1',model:'unrelated'}};
        else if(endpoint==='/connections'&&method==='GET')data={connections:[],storage_available:true};
        else if(endpoint==='/templates')data={templates:[]};
        else if(endpoint==='/routing-corpus')data={tiers:[{tier:1000,bursts:10,note:'合成测试集'}],metrics:[]};
        else if(endpoint==='/tasks')data={tasks:[],comparisons:[],quality_tests:[],routing_comparisons:[],fingerprint_runs:runs.map(r=>{const x={...r};delete x.artifact;return x}),fingerprint_storage_warnings:[]};
        else if(endpoint==='/fingerprint/catalog'){if(catalogFail)return route.fulfill({status:503,json:{error:'合成协议读取失败'}});data=fixture.catalog}
        else if(endpoint==='/fingerprint/references'&&method==='GET')data={references:refs};
        else if(endpoint==='/fingerprints'&&method==='POST'){
          const body=req.postDataJSON();if(body.mode==='selfcheck'){assert.equal(body.concurrency,20);assert.equal(body.rpm,100)}allSubmissions.push({width,body:clone(body)});
          const id='run-'+(runs.length+1), run={id,name:body.name,mode:body.mode,status:'running',created:now(),started_at:now(),finished_at:null,endpoint:{url:body.endpoint.url,model:body.endpoint.model},protocol:fixture.protocol,progress:{done:0,total:40,valid:0,errors:0},error:null,reference_id:body.reference_id||null,comparison:null,diagnostics:null,source_note:body.source_note||'',artifact:null};runs.unshift(run);data={run};
        }
        else if(/^\/fingerprints\/[^/]+\/stop$/.test(endpoint)){
          const run=runs.find(r=>r.id===endpoint.split('/')[2]);run.status='stopped';run.finished_at=now();run.progress={done:40,total:40,valid:40,errors:0};run.artifact=artifact();data={run};
        }
        else if(/^\/fingerprints\/[^/]+\/samples$/.test(endpoint)){
          if(samplesFail){samplesFail=false;return route.fulfill({status:503,json:{error:'合成样本读取失败'}})}
          const offset=Number(url.searchParams.get('offset')||0),limit=Number(url.searchParams.get('limit')||50);data={samples:sampleRows.slice(offset,offset+limit),total:sampleRows.length,offset,limit};
        }
        else if(/^\/fingerprints\/[^/]+\/compare$/.test(endpoint)){
          compareCount++;const run=runs.find(r=>r.id===endpoint.split('/')[2]),reference_id=req.postDataJSON().reference_id;
          run.comparison={reference_id,reference_name:refs.find(r=>r.id===reference_id)?.name,mean_jsd:.03,verdict:'match',label:'行为一致',reasons:[],warnings:[],comparable_cells:4,protocol_compatible:true,reference_age_days:1,thresholds:{match:.25,mismatch:.35},cells:fixture.protocol.cells.map(cell_id=>({cell_id,jsd:.03,valid_candidate:10,valid_reference:10}))};data={comparison:run.comparison};
        }
        else if(/^\/fingerprints\/[^/]+\/delete$/.test(endpoint)){runs=runs.filter(r=>r.id!==endpoint.split('/')[2]);data={ok:true}}
        else if(/^\/fingerprints\/[^/]+\/export$/.test(endpoint))data=artifact();
        else if(/^\/fingerprints\/[^/]+$/.test(endpoint))data=runs.find(r=>r.id===endpoint.split('/')[2]);
        else if(/^\/fingerprint\/references\/[^/]+\/export$/.test(endpoint))data=artifact();
        else if(endpoint==='/fingerprint/references/import'){
          imported++;const body=req.postDataJSON(),ref=reference('ref-import-'+imported,body.name,{source_note:body.source_note,trusted_source:body.trusted_source,imported:true});refs.push(ref);data={reference:ref};
        }
        else if(/^\/fingerprint\/references\/[^/]+\/delete$/.test(endpoint)){refs=refs.filter(r=>r.id!==endpoint.split('/')[3]);data={ok:true}}
        else if(endpoint==='/favicon.ico')data={};
        else{unexpected.push(method+' '+endpoint);return route.fulfill({status:404,json:{error:'Unexpected synthetic route'}})}
        return route.fulfill({contentType:'application/json',body:JSON.stringify(data||{})});
      });
      await page.goto(origin);
      await page.waitForFunction(()=>authenticated&&refreshTimer!==null&&!refreshing);
      await page.evaluate(()=>clearInterval(refreshTimer));
      await page.locator('[data-module="fingerprint"]').click();
      await page.evaluate(()=>refresh());
      await page.getByRole('heading',{name:'让每次对照，都有同一把尺子'}).waitFor();
      await geometry(page,width,'empty '+width);
      await page.locator('#recordCreateButton').click();
      await page.locator('#fpName').waitFor();
      assert.equal(await page.locator('#fpConcurrency').inputValue(),'20','new collection concurrency defaults to 20');
      assert.equal(await page.locator('#fpRpm').inputValue(),'100','new collection RPM defaults to 100');
      assert.equal(await page.locator('#fpAdvanced').evaluate(el=>el.open),false,'advanced protocol fields start folded');
      for(const theme of ['light','dark']) {
        await page.evaluate(value=>document.documentElement.dataset.theme=value,theme);
        await geometry(page,width,'new drawer '+width+' '+theme);
        await page.screenshot({path:path.join(output,`fingerprint-new-${width}-${theme}.png`)});
      }
      await page.evaluate(()=>document.documentElement.dataset.theme='light');
      await fillEndpoint(page);
      await page.locator('#fpSamples').fill('37');
      await page.locator('#fpPreset').selectOption('quick');
      assert.equal(await page.locator('#fpSamples').inputValue(),'37','collection scope never overwrites the chosen sample count');
      assert.match(await page.locator('#fpRequestCount').innerText(),/148/);
      await page.locator('#fpSamples').fill('10');
      assert.match(await page.locator('#fpRequestCount').innerText(),/40/);
      await page.locator('#fpRiskAck').check();
      await page.locator('#fpAdapter').selectOption('openai_reasoning');
      assert.equal(await page.locator('#fpRiskAck').isChecked(),false,'reasoning selection clears fee approval');
      assert.match(await page.locator('#fpBudgetNote').innerText(),/4,096.*163,840/,'reasoning per-request and plan caps are shown');
      assert.match(await page.locator('#fpAdapterHint').innerText(),/仅供探索性对照/);
      assert.equal(allSubmissions.filter(s=>s.width===width).length,0,'changing reasoning adapter sends no requests');
      await capture(page,width,'reasoning-create');
      await page.locator('#fpAdapter').selectOption('standard');
      assert.match(await page.locator('#fpBudgetNote').innerText(),/上限 16.*640/,'returning to direct protocol restores exact cap');
      await page.locator('#fpUrl').fill('https://open.bigmodel.cn/api/paas/v4');
      await page.locator('#fpModel').fill('glm-5.3-flash');
      assert.equal(await page.locator('#fpAdapter').inputValue(),'standard','GLM model does not auto-increase output budget');
      assert.match(await page.locator('#fpAdapterHint').innerText(),/不能关闭思考/);
      await page.locator('#fpRiskAck').check();
      await page.locator('#fpAdapter').selectOption('zhipu_reasoning');
      assert.equal(await page.locator('#fpRiskAck').isChecked(),false,'GLM reasoning requires renewed fee confirmation');
      assert.match(await page.locator('#fpBudgetNote').innerText(),/4,096.*163,840/);
      assert.match(await page.locator('#fpAdapterHint').innerText(),/低强度思考/);
      await capture(page,width,'glm-reasoning-create');
      assert.equal(allSubmissions.filter(s=>s.width===width).length,0,'GLM adapter inspection sends no paid requests');
      await page.locator('#fpUrl').fill('https://synthetic.invalid/v1');
      await page.locator('#fpAdapter').selectOption('standard');
      await page.locator('#fpRiskAck').check();
      await page.locator('#fpModel').fill('changed-model');
      assert.equal(await page.locator('#fpRiskAck').isChecked(),false,'target edit resets cost acknowledgment');
      await page.locator('#fpMode').selectOption('reference');
      assert.equal(await page.locator('#fpUrl').inputValue(),'https://synthetic.invalid/v1','mode switch preserves URL');
      assert.equal(await page.locator('#fpKey').inputValue(),'synthetic-secret-never-send','mode switch preserves draft secret');
      assert.equal(await page.locator('#fpSourceSection').isVisible(),true);
      await page.locator('#fpMode').selectOption('verify');
      assert.equal(await page.locator('#fpReference option[value="ref-reasoning"]').isDisabled(),false,'reasoning references can be reused explicitly');
      await page.locator('#fpReference').selectOption('ref-reasoning');
      assert.equal(await page.locator('#fpAdapter').inputValue(),'openai_reasoning');
      assert.match(await page.locator('#fpBudgetNote').innerText(),/4,096.*163,840/,'reference freezes reasoning budget');
      assert.equal(await page.locator('#fpAdapter').isDisabled(),true,'reasoning reference cannot be mixed with direct protocol');
      await page.locator('#fpReference').selectOption('ref-good');
      for(const id of ['fpPreset','fpSamples','fpConcurrency','fpRpm','fpTimeout','fpAdapter'])assert.equal(await page.locator('#'+id).isDisabled(),true,id+' locked');
      assert.equal(await page.locator('#fpPreset').inputValue(),'quick','numeric catalog cell count locks correct preset');
      assert.equal(await page.locator('#fpConcurrency').inputValue(),'2');
      assert.equal(await page.locator('#fpRpm').inputValue(),'100');
      await page.locator('#fpReference').selectOption('ref-legacy');
      assert.equal(await page.locator('#fpRpm').inputValue(),'','legacy reference does not invent a rate');
      assert.match(await page.locator('#fpProtocolLock').innerText(),/未设置每分钟请求数/);
      await page.locator('#fpMode').selectOption('selfcheck');
      assert.equal(await page.locator('#fpConcurrency').inputValue(),'20','leaving reference restores the new collection draft');
      assert.equal(await page.locator('#fpRpm').inputValue(),'100','leaving legacy reference restores RPM');
      await page.locator('#fpMode').selectOption('verify');
      await page.locator('#fpReference').selectOption('ref-good');
      assert.equal(await page.locator('#fpReference option[value="ref-old"]').isDisabled(),true,'expired reference disabled');
      assert.equal(await page.locator('#fpReference option[value="ref-external"]').isDisabled(),true,'external reference disabled');
      await geometry(page,width,'verify drawer '+width);
      await page.screenshot({path:path.join(output,`fingerprint-create-${width}.png`)});
      await page.keyboard.press('Escape');
      assert.equal(await page.locator('#fpKey').count(),0,'closing removes secret DOM');
      assert.equal(await page.locator('#recordCreateButton').evaluate(n=>n===document.activeElement),true,'focus restored');
      await page.locator('#recordCreateButton').click();await page.locator('#fpName').waitFor();
      assert.equal(await page.locator('#fpKey').inputValue(),'','fresh creation has no key');
      await fillEndpoint(page);await page.locator('#fpPreset').selectOption('quick');await page.locator('#fpSamples').fill('10');await page.locator('#fpRiskAck').check();
      await page.locator('#fpSubmit').click();
      await page.getByRole('heading',{name:'合成通道自检',exact:true}).waitFor();
      assert.equal(await page.locator('#fpKey').count(),0);
      assert.equal(await page.evaluate(()=>Object.values(localStorage).some(x=>x.includes('synthetic-secret-never-send'))),false,'secret not saved');
      assert.equal(await page.locator('.report-head [data-fp-action="stop"]').count(),1,'stop action is visible in the header');
      await capture(page,width,'running');
      await page.locator('[data-fp-action="stop"]').click();
      await page.getByRole('heading',{name:'已形成指纹证据'}).waitFor();
      await geometry(page,width,'overview '+width);await capture(page,width,'overview');
      await page.getByRole('tab',{name:'通道诊断',exact:true}).click();
      assert.match(await page.locator('.fp-cost').innerText(),/未知/,'missing cost stays unknown');
      await capture(page,width,'diagnostics');
      await page.getByRole('tab',{name:'协议与来源',exact:true}).click();
      assert.match(await page.locator('#fpTabPanel').innerText(),/冻结协议快照/);
      await capture(page,width,'protocol');
      samplesFail=true;
      await page.getByRole('tab',{name:'原始样本',exact:true}).click();
      await page.getByText('样本读取失败：合成样本读取失败').waitFor();
      await page.getByRole('button',{name:'重新加载样本'}).click();
      await page.waitForFunction(()=>document.querySelector('.fp-pagination')?.textContent.includes('1–50'));
      await page.getByRole('button',{name:'下一页',exact:true}).click();
      await page.waitForFunction(()=>document.querySelector('.fp-pagination')?.textContent.includes('51–73'));
      await page.getByRole('button',{name:'上一页',exact:true}).click();
      await page.waitForFunction(()=>document.querySelector('.fp-pagination')?.textContent.includes('1–50'));
      await geometry(page,width,'samples '+width);await capture(page,width,'samples');
      await page.locator('[data-click-action="showFingerprintSample"]').nth(1).click();
      assert.equal(await page.locator('#fpModalBody img').count(),0,'sample text escaped');
      await capture(page,width,'evidence');
      await page.locator('#fpModalFooter [data-fp-action="close-modal"]').click();
      await page.locator('[data-fp-action="compare"]').click();
      await page.locator('#fpCompareReference').selectOption('ref-good');await capture(page,width,'compare');
      await page.locator('[data-fp-action="submit-compare"]').click();
      await page.getByRole('heading',{name:'行为分布相近',exact:true}).waitFor();
      assert.equal(compareCount,1);
      assert.equal(allSubmissions.filter(s=>s.width===width).length,1,'offline comparison adds zero upstream runs');
      await page.getByRole('tab',{name:'回答分布',exact:true}).click();
      await page.waitForFunction(()=>document.querySelector('.fp-legend')?.textContent.includes('参考指纹'));
      await page.locator('.fp-distribution summary').first().click();
      assert.equal(await page.locator('.fp-bar-track.reference').count()>0,true,'offline comparison uses comparison.reference_id');
      await page.screenshot({path:path.join(output,`fingerprint-distribution-${width}.png`)});
      await page.locator('[data-fp-view="references"]').click();
      await page.getByRole('heading',{name:'参考库',exact:true}).waitFor();
      assert.equal(await page.locator('#modulePlaceholder img').count(),0,'reference name escaped');
      await geometry(page,width,'library '+width);await capture(page,width,'library');
      assert.equal(await page.locator('#modulePlaceholder [data-fp-action="create-reference"]').count(),0,'record panel is the only new-reference entry');
      assert.equal(await page.locator('.fp-reference-card [data-fp-action="delete-reference"]').count(),0,'destructive reference actions stay inside details');
      await page.locator('[data-fp-action="import"]').click();
      await page.locator('#fpImportFile').setInputFiles({name:'synthetic-reference.json',mimeType:'application/json',buffer:Buffer.from(JSON.stringify(artifact()))});
      await page.locator('#fpImportName').fill('导入合成参考');await capture(page,width,'import');
      await page.locator('[data-fp-action="submit-import"]').click();
      await page.getByRole('heading',{name:'导入合成参考',exact:true}).waitFor();
      assert.equal(imported,1);
      await page.locator('.fp-reference-card [data-fp-action="reference-detail"][data-fp-id="ref-import-1"]').click();await capture(page,width,'reference');
      await page.locator('[data-fp-action="delete-reference"][data-fp-id="ref-import-1"]').click();await capture(page,width,'delete');
      await page.locator('[data-fp-action="confirm-delete"]').click();
      await page.getByRole('heading',{name:'导入合成参考',exact:true}).waitFor({state:'detached'});
      await page.locator('#recordCreateButton').click();await page.locator('#fpName').waitFor();
      await page.locator('#fpMode').selectOption('verify');
      await page.locator('#fpReference').selectOption('ref-legacy');
      await fillEndpoint(page,'合成旧参考对照');
      await page.locator('#fpRiskAck').check();
      await page.locator('#fpSubmit').click();
      await page.getByRole('heading',{name:'合成旧参考对照',exact:true}).waitFor();
      const legacySubmission=allSubmissions.at(-1).body;
      assert.equal(legacySubmission.reference_id,'ref-legacy');
      assert.equal(legacySubmission.concurrency,2);
      assert.equal(Object.hasOwn(legacySubmission,'rpm'),false,'legacy comparison submits no invented RPM');
      await page.locator('#recordCreateButton').click();await page.locator('#fpName').waitFor();await fillEndpoint(page,'会话清理');
      await page.evaluate(()=>{authenticated=false;FingerprintUI.onAuthLost()});
      assert.equal(await page.locator('#fpKey').count(),0,'auth loss clears key');
      assert.equal(await page.locator('#fingerprintModal').isHidden(),true);
      assert.equal(await page.locator('#fingerprintList .task').count(),0,'auth loss clears private records');
      assertions.push({width,status:'passed'});
      await context.close();
    }
    assert.deepEqual(errors,[],'page errors');assert.deepEqual(unexpected,[],'unexpected network requests');
    fs.writeFileSync(path.join(output,'fingerprint-browser-results.json'),JSON.stringify({assertions,submissions:allSubmissions.map(({width,body})=>({width,mode:body.mode,preset:body.preset,risk_acknowledged:body.risk_acknowledged})),errors,unexpected},null,2));
    console.log(JSON.stringify({status:'passed',viewports:assertions,output},null,2));
  } finally { await browser.close(); }
})().catch(e=>{console.error(e);process.exitCode=1});
