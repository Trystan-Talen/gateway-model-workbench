/** Fingerprint workspace regression, synthetic HTTP only. Never calls an upstream.
 * NODE_PATH=<playwright modules> PYTHON=<python runtime> node tests/browser/fingerprint.cjs
 * UI_PROJECT_ROOT and UI_ASSET_ROOT can target a staging workspace.
 */
const {chromium} = require('playwright');
const assert = require('node:assert/strict');
const fs = require('node:fs'), path = require('node:path'), os = require('node:os');
const {execFileSync} = require('node:child_process');
const root = process.env.UI_PROJECT_ROOT || path.resolve(__dirname,'../..');
const assets = process.env.UI_ASSET_ROOT || path.join(root,'static');
const output = process.env.UI_ARTIFACT_DIR || fs.mkdtempSync(path.join(os.tmpdir(),'fingerprint-ui-'));
fs.mkdirSync(output,{recursive:true});
const python = process.env.PYTHON || 'python3';
const html = process.env.UI_HTML ? fs.readFileSync(process.env.UI_HTML,'utf8') : execFileSync(python,['-c','import route_test_ui; print(route_test_ui.INDEX_HTML)'],{cwd:root,encoding:'utf8',env:{...process.env,PYTHONDONTWRITEBYTECODE:'1'}});
const fixture = JSON.parse(execFileSync(python,['-c','import json; from fingerprint_protocol import catalog,make_protocol; print(json.dumps({"catalog":catalog(),"protocol":make_protocol({"preset":"quick","samples_per_cell":10,"concurrency":2})}))'],{cwd:root,encoding:'utf8',env:{...process.env,PYTHONDONTWRITEBYTECODE:'1'}}));
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
  const overflow = await page.locator('.fp-workspace,.fp-card,.fp-page-head,#fingerprintDrawer,.fp-modal-dialog,.fp-form-grid').evaluateAll(nodes=>nodes.filter(n=>n.getClientRects().length&&n.scrollWidth>n.clientWidth+1).map(n=>({id:n.id,class:n.className,width:n.clientWidth,scroll:n.scrollWidth})));
  assert.deepEqual(overflow,[],label+': component overflow');
  if(await page.locator('#fingerprintDrawer').evaluate(n=>n.classList.contains('mobile-open'))) {
    await page.waitForFunction(()=>Math.abs(document.getElementById('fingerprintDrawer').getBoundingClientRect().right-innerWidth)<1);
    const bounds = await page.locator('#fingerprintDrawer').boundingBox();
    assert.ok(bounds.x>=-1&&Math.abs(bounds.x+bounds.width-width)<1,label+': drawer bounds');
    assert.equal(await page.locator('.app').evaluate(n=>n.inert),true,label+': inert background');
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
    for(const width of [390,768,1440]) {
      const context=await browser.newContext({viewport:{width,height:width===390?844:1000},reducedMotion:'reduce'});
      const page=await context.newPage();
      let runs=[], refs=[reference('ref-good','可信参考 <img src=x onerror=alert(1)>'),reference('ref-old','过期参考',{collected_at:'2020-01-01T00:00:00Z'}),reference('ref-external','外部协议参考',{protocol:{version:'external',external_protocol:true},imported:true})];
      let samplesFail=false, catalogFail=false, compareCount=0, imported=0;
      const sampleRows=Array.from({length:73},(_,i)=>({index:i,cell_id:fixture.protocol.cells[i%4],paraphrase_index:i%3,started_at:now(),finished_at:now(),latency_ms:12,status_code:200,category:i===1?'invalid':'valid',normalized:'blue',raw:i===1?'<img src=x onerror=alert(1)>':'blue',extra_text:false,finish_reason:'stop',truncated:false,returned_model:'synthetic-model',usage:{prompt_tokens:10,completion_tokens:1,reported_cost:null},error:null}));
      page.on('pageerror',e=>errors.push({width,message:e.message}));
      page.on('dialog',async d=>{errors.push({width,message:'Unexpected dialog: '+d.message()});await d.dismiss()});
      await context.route('**/*',async route=>{
    if(await require('./assets.cjs').serveAsset(route,root))return;
        const req=route.request(), url=new URL(req.url()), endpoint=url.pathname, method=req.method();
        if(url.origin!==origin){unexpected.push(req.url());return route.abort()}
        if(endpoint==='/')return route.fulfill({contentType:'text/html',body:html});
        if(endpoint==='/assets/fingerprint.js'||endpoint==='/assets/fingerprint.css')return route.fulfill({contentType:endpoint.endsWith('.js')?'text/javascript':'text/css',body:fs.readFileSync(path.join(assets,path.basename(endpoint)),'utf8')});
        let data;
        if(endpoint==='/auth/status')data={authenticated:true,user:{username:'合成回归',role:'user'}};
        else if(endpoint==='/config')data={config:{url:'https://unrelated.invalid/v1',model:'unrelated'}};
        else if(endpoint==='/templates')data={templates:[]};
        else if(endpoint==='/routing-corpus')data={tiers:[{tier:1000,bursts:10,note:'合成测试集'}],metrics:[]};
        else if(endpoint==='/tasks')data={tasks:[],comparisons:[],quality_tests:[],routing_comparisons:[],fingerprint_runs:runs.map(r=>{const x={...r};delete x.artifact;return x}),fingerprint_storage_warnings:[]};
        else if(endpoint==='/fingerprint/catalog'){if(catalogFail)return route.fulfill({status:503,json:{error:'合成协议读取失败'}});data=fixture.catalog}
        else if(endpoint==='/fingerprint/references'&&method==='GET')data={references:refs};
        else if(endpoint==='/fingerprints'&&method==='POST'){
          const body=req.postDataJSON();allSubmissions.push({width,body:clone(body)});
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
      await fillEndpoint(page);
      await page.locator('#fpPreset').selectOption('quick');
      assert.match(await page.locator('#fpRequestCount').innerText(),/60/);
      await page.locator('#fpSamples').fill('10');
      assert.match(await page.locator('#fpRequestCount').innerText(),/40/);
      await page.locator('#fpRiskAck').check();
      await page.locator('#fpModel').fill('changed-model');
      assert.equal(await page.locator('#fpRiskAck').isChecked(),false,'target edit resets cost acknowledgment');
      await page.locator('input[name="fpMode"][value="reference"]').check();
      assert.equal(await page.locator('#fpUrl').inputValue(),'https://synthetic.invalid/v1','mode switch preserves URL');
      assert.equal(await page.locator('#fpKey').inputValue(),'synthetic-secret-never-send','mode switch preserves draft secret');
      assert.equal(await page.locator('#fpSourceSection').isVisible(),true);
      await page.locator('input[name="fpMode"][value="verify"]').check();
      await page.locator('#fpReference').selectOption('ref-good');
      for(const id of ['fpPreset','fpSamples','fpConcurrency','fpTimeout','fpAdapter'])assert.equal(await page.locator('#'+id).isDisabled(),true,id+' locked');
      assert.equal(await page.locator('#fpPreset').inputValue(),'quick','numeric catalog cell count locks correct preset');
      assert.equal(await page.locator('#fpConcurrency').inputValue(),'2');
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
      await page.locator('[data-fp-action="stop"]').click();
      await page.getByRole('heading',{name:'已形成指纹证据'}).waitFor();
      await geometry(page,width,'overview '+width);
      await page.locator('[data-fp-tab="diagnostics"]').click();
      assert.match(await page.locator('.fp-cost').innerText(),/未知/,'missing cost stays unknown');
      await page.locator('[data-fp-tab="protocol"]').click();
      assert.match(await page.locator('#fpTabPanel').innerText(),/冻结协议快照/);
      samplesFail=true;
      await page.locator('[data-fp-tab="samples"]').click();
      await page.getByText('样本读取失败：合成样本读取失败').waitFor();
      await page.getByRole('button',{name:'重新加载样本'}).click();
      await page.waitForFunction(()=>document.querySelector('.fp-pagination')?.textContent.includes('1–50'));
      await page.getByRole('button',{name:'下一页',exact:true}).click();
      await page.waitForFunction(()=>document.querySelector('.fp-pagination')?.textContent.includes('51–73'));
      await page.getByRole('button',{name:'上一页',exact:true}).click();
      await page.waitForFunction(()=>document.querySelector('.fp-pagination')?.textContent.includes('1–50'));
      await page.locator('.fp-sample').nth(1).locator('summary').click();
      assert.equal(await page.locator('#modulePlaceholder img').count(),0,'sample text escaped');
      await geometry(page,width,'samples '+width);
      await page.locator('[data-fp-action="compare"]').click();
      await page.locator('#fpCompareReference').selectOption('ref-good');
      await page.locator('[data-fp-action="submit-compare"]').click();
      await page.getByRole('heading',{name:'行为分布相近',exact:true}).waitFor();
      assert.equal(compareCount,1);
      assert.equal(allSubmissions.filter(s=>s.width===width).length,1,'offline comparison adds zero upstream runs');
      await page.locator('[data-fp-tab="distribution"]').click();
      await page.waitForFunction(()=>document.querySelector('.fp-legend')?.textContent.includes('参考指纹'));
      await page.locator('.fp-distribution summary').first().click();
      assert.equal(await page.locator('.fp-bar-track.reference').count()>0,true,'offline comparison uses comparison.reference_id');
      await page.screenshot({path:path.join(output,`fingerprint-distribution-${width}.png`)});
      await page.locator('[data-fp-view="references"]').click();
      await page.getByRole('heading',{name:'参考库',exact:true}).waitFor();
      assert.equal(await page.locator('#modulePlaceholder img').count(),0,'reference name escaped');
      await geometry(page,width,'library '+width);
      await page.locator('[data-fp-action="import"]').click();
      await page.locator('#fpImportFile').setInputFiles({name:'synthetic-reference.json',mimeType:'application/json',buffer:Buffer.from(JSON.stringify(artifact()))});
      await page.locator('#fpImportName').fill('导入合成参考');
      await page.locator('[data-fp-action="submit-import"]').click();
      await page.getByRole('heading',{name:'导入合成参考',exact:true}).waitFor();
      assert.equal(imported,1);
      await page.locator('[data-fp-action="delete-reference"][data-fp-id="ref-import-1"]').click();
      await page.locator('[data-fp-action="confirm-delete"]').click();
      await page.getByRole('heading',{name:'导入合成参考',exact:true}).waitFor({state:'detached'});
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
