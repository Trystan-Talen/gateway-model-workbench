/**
 * Report content rhythm, using real renderers and synthetic read-only responses.
 * Unlike overflow checks, this checks the gap between adjacent sibling content blocks.
 * Screenshots tile the real internal scroller, so evidence includes the entire report.
 */
'use strict';
const {chromium}=require('playwright');
const assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path'),crypto=require('node:crypto'),{execFileSync}=require('node:child_process');
const {projectRoot:root,output,env}=require('./artifacts.cjs').browserArtifacts('report_spacing');
execFileSync(process.env.PYTHON||path.join(root,'.venv/bin/python'),[path.join(root,'tests/browser/make_fixture.py'),output],{cwd:root,env});
const fixture=JSON.parse(fs.readFileSync(path.join(output,'fixture.json'),'utf8')),html=fs.readFileSync(path.join(output,'index.html'),'utf8');
// Optional captured pre-fix assets let the identical test demonstrate a real regression.
const baseline=process.env.REPORT_SPACING_BASELINE_ASSETS?path.resolve(process.env.REPORT_SPACING_BASELINE_ASSETS):null;
const assetNames=['app.css','ui-kit.js','app.js','tpm.js','fingerprint.js','connections.js'];
const assets=Object.fromEntries(assetNames.map(name=>{
 const file=baseline&&fs.existsSync(path.join(baseline,name))?path.join(baseline,name):path.join(root,'static',name);
 return [name,{file,sha256:crypto.createHash('sha256').update(fs.readFileSync(file)).digest('hex')}];
}));
const timing={version:1,clock:'client',timezone:'Asia/Singapore',source:'recorded',created_at:'2026-10-15T08:00:00+08:00',started_at:'2026-10-15T08:00:01+08:00',dispatch_ended_at:'2026-10-15T08:03:01+08:00',ended_at:'2026-10-15T08:03:02+08:00',queue_ms:1000,elapsed_ms:181000,drain_ms:1000};
const requestTiming={...timing,scheduled_at:timing.started_at,enqueued_at:timing.started_at,first_token_at:'2026-10-15T08:00:01.120+08:00',ended_at:'2026-10-15T08:00:01.450+08:00',queue_ms:0,schedule_lag_ms:0,ttft_ms:120,total_ms:450};
for(const run of [...fixture.tasks,...fixture.comparisons,...fixture.quality_tests])run.timing=timing;
for(const run of fixture.tasks)for(const row of run.recent)row.timing=requestTiming;
for(const run of fixture.comparisons)for(const group of run.groups){group.timing=timing;group.summary.timing=timing;for(const row of group.summary.recent)row.timing=requestTiming;}
for(const run of fixture.quality_tests)for(const resource of run.resources)for(const row of resource.case_results)row.timing=requestTiming;
const rate=(successes,total)=>({successes,total,pct:total?100*successes/total:null});
function observation(successes,total){
 return {requests:total,success:rate(successes,total),cache:rate(8,total),ttft_p95_ms:120,total_p95_ms:450,
  stability:{max_consecutive_failures:total-successes,ttft_ewma_max_ms:120},
  diagnostics:{http_2xx:successes,complete_2xx:successes,incomplete_2xx:0,failures:total-successes,
   failure_types:total>successes?{HTTP_429:total-successes}:{},status_codes:{200:successes,...(total>successes?{429:total-successes}:{})}}};
}
const routeBase=observation(20,20),routeCandidate=observation(18,20);
const routing={id:'spacing-route',name:'路由对照 · 内容间距核查',status:'finished',timing,tier:1000,rpm:100,concurrency:{baseline:20,candidate:20},progress:{done:40,pct:100},
 sides:{baseline:{name:'固定路由',model:'synthetic-model',url:'http://example.test/v1',records:20},candidate:{name:'智能路由',model:'synthetic-model',url:'http://example.test/v1',records:20}},
 config:{capture_raw:false},cache_isolation:{baseline:'000000000001',candidate:'000000000002'},limits:['合成数据仅用于界面回归。'],
 report:{baseline:routeBase,candidate:routeCandidate,success:{baseline:routeBase.success,candidate:routeCandidate.success,delta_pct:-10,attribution:'not_attributable',note:'合成样本不足以归因。'},
  cache:{baseline:routeBase.cache,candidate:routeCandidate.cache,delta_pct:0,baseline_no_usage:0,candidate_no_usage:0,attribution:'insufficient_evidence'},
  ttft:{delta_pct:0},total_latency:{delta_pct:0},reliability:{paired_total:20,unpaired_count:0,mcnemar:{discordant:2,p_value:0.5}}}};
fixture.routing_comparisons=[routing];
const fingerprint={id:'spacing-fingerprint',name:'模型指纹 · 内容间距核查',mode:'selfcheck',status:'finished',timing,endpoint:{model:'synthetic-model'},
 protocol:{id:'synthetic-protocol',cells:['color','shape'],samples_per_cell:10,concurrency:20,rpm:100,max_tokens:16,seed:'spacing'},
 progress:{done:20,total:20,valid:20,errors:0},artifact:{complete:true,
  cells:{color:{counts:{blue:7,green:3},valid_count:10,total_count:10,entropy_bits:0.8},shape:{counts:{circle:10},valid_count:10,total_count:10,entropy_bits:0}},
  diagnostics:{valid_rate:1,error_rate:0,truncated_rate:0,extra_text_rate:0,model_echo_counts:{'synthetic-model':20},usage:{prompt_tokens:1000,completion_tokens:60,cached_tokens:400},
   cache:{cached_samples:8,uncached_samples:12,unknown_samples:0,cached_median_latency_ms:120,uncached_median_latency_ms:450},
   split_half:{warning:true,alternating:{mean_jsd:0.1,comparable_cells:2},temporal:{mean_jsd:0.2,comparable_cells:2}}}}};
fixture.fingerprint_runs=[fingerprint];
const tpm={id:'spacing-tpm',phase:'finished',status:'finished',timing,formal_started_at:1792022401,
 config:{name:'词元吞吐量 · 内容间距核查',model:'synthetic-model',target_tpm:200000000,input_tokens:49500,expected_output_tokens:500,max_output_tokens:1024,
  cache_mode:'prefer_cache',budget_usd:80,concurrency:2048,timeout_seconds:120,execution_mode:'auto',target_duration_seconds:90,ramp_duration_seconds:20,
  prices:{input:.304,output:1.064,cache_read:.0874,cache_write:null},cache_write_price_mode:'input',usage_semantics:'openai'},
 recent:[{id:1,phase:'formal',stage_index:4,success:true,timing:requestTiming,usage:{complete:true,full_input_tokens:49500,output_tokens:500}}],
 metrics:{started:6000,planned:6000,succeeded:5998,failed:2,full_token_tpm:190000000,window_full:true,window_seconds:60,in_flight:0,success_rate:.9997,cache_hit_rate:.87,usage_complete_rate:1},
 report:{verdict:'near',label:'接近目标',primary_window:{valid:true,full_tokens:190000000,ratio:.95,start:timing.started_at,end:timing.ended_at},rolling_peak_tpm:199000000,
  applied_ratio:1,actual_started:6000,planned:6000,errors_increased:false,latency_worsened:false,sustained_backlog:false,limitations:['仅为本次客户端窗口观测。']},
 fees:{known_cost_usd:45,reserved_cost_usd:0},
 plan:{target_rps:66.6667,estimated_cost_usd:45.1,worst_case_cost_usd:130,calibration_cost_usd:.31,warmup_cost_usd:.02,
  stages:[{target_tpm:200000000,duration_seconds:90,rps:66.6667,planned_requests:6000}],criteria:{near_target_ratio:.95}},
 calibration:{mean_input_tokens:49500,mean_output_tokens:500,p95_seconds:30,usage_complete_rate:1,cache_hit_rate:.87}};
const connection={id:'spacing-connection',name:'业务回归 · 常用连接',url:'https://synthetic.invalid/long-business-gateway-name/v1',api_key_saved:true,updated_at:1792022400};
let connectionReadFailure=false;
const checks=[],violations=[],clippedTables=[],methodViolations=[],scrollViolations=[],errors=[],unexpected=[];
let browser;

// Runs in the page. These are semantic block types, not module-specific exemptions.
// Internal metric cells and text lines are deliberately not independent report blocks.
function collectSpacing(scopeSelector){
 const scope=document.querySelector(scopeSelector);
 const types=[
  ['metrics','.report-metrics,.fp-metrics,.gmetrics'],
  ['table','.table-wrap,.fp-table-wrap'],
  ['methods','.method-grid,.report-method-grid,.fp-protocol'],
  ['notice','.notice,.comparison-hint,.conclusion-banner,.fp-conclusion,.fp-notice'],
  ['disclosure','details'],
  ['section','.report-section,.fp-card,.group-detail'],
  ['chart','.chart-wrap,.charts,.chart-grid'],
  ['list','.signal-list,.report-limitations,.next-step-grid,.quality-score-grid,.quality-scenario-grid,.fp-distributions'],
  ['content','.report-stack,.disclosure-body,.report-shell,.report-readout-grid,.fp-cost'],
  ['header','.report-head,.group-detail-head'],
  ['tabs','.report-tabs'],
  ['progress','.report-progress-card']
 ];
 const kind=node=>types.find(([,selector])=>node.matches(selector))?.[0];
 const visible=node=>{
  if(!node.getClientRects().length||node.getBoundingClientRect().height<=0||getComputedStyle(node).visibility==='hidden')return false;
  // Chromium can retain layout boxes in a closed <details>; those boxes are not painted.
  for(let parent=node.parentElement;parent&&scope.contains(parent);parent=parent.parentElement)
   if(parent.tagName==='DETAILS'&&!parent.open&&!parent.querySelector(':scope > summary')?.contains(node))return false;
  return true;
 };
 const label=node=>(node.id?'#'+node.id:node.tagName.toLowerCase()+'.'+Array.from(node.classList).join('.'))+' '+(node.innerText||'').replace(/\s+/g,' ').trim().slice(0,80);
 const rect=node=>{const r=node.getBoundingClientRect();return {top:r.top,bottom:r.bottom,left:r.left,right:r.right,width:r.width,height:r.height}};
 const pairs=[],parallel=[];
 for(const parent of [scope,...scope.querySelectorAll('*')]){
  if(!visible(parent))continue;
  const children=Array.from(parent.children).filter(visible);
  for(let i=1;i<children.length;i++){
   const before=children[i-1],after=children[i],beforeKind=kind(before),afterKind=kind(after);
   if(!beforeKind||!afterKind)continue;
   const a=rect(before),b=rect(after),horizontalOverlap=Math.min(a.right,b.right)-Math.max(a.left,b.left);
   if(horizontalOverlap<1){parallel.push({parent:label(parent),before:label(before),after:label(after)});continue}
   const css=getComputedStyle(parent),token=parent.classList.contains('report-stack')?'--content-gap':parent.matches('.report-shell,.detail-layout')?'--section-gap':'--space-2';
   const required=parseFloat(css.getPropertyValue(token))||(token==='--content-gap'?16:8),gap=b.top-a.bottom;
   pairs.push({parent:label(parent),before:label(before),after:label(after),beforeKind,afterKind,token,required,gap:Math.round(gap*100)/100,
    beforeRect:a,afterRect:b,pass:gap>=required-.75});
  }
 }
 const cardTables=Array.from(scope.querySelectorAll('.comparison-result-table')).filter(table=>visible(table)&&getComputedStyle(table).display==='block').map(table=>{
  const wrap=table.closest('.table-wrap'),bounds=wrap.getBoundingClientRect(),rows=Array.from(table.querySelectorAll('tbody tr')),last=rows.at(-1)?.getBoundingClientRect();
  return {label:label(table),rows:rows.length,clientHeight:wrap.clientHeight,scrollHeight:wrap.scrollHeight,maxHeight:getComputedStyle(wrap).maxHeight,
   lastRowBottom:last?.bottom,visibleBottom:bounds.bottom,pass:wrap.scrollHeight<=wrap.clientHeight+1&&(!last||last.bottom<=bounds.bottom+1)};
 });
 const methodGrids=Array.from(scope.querySelectorAll('.method-grid,.report-method-grid')).filter(visible).map(grid=>{
  const bounds=grid.getBoundingClientRect(),template=getComputedStyle(grid).gridTemplateColumns;
  const cards=Array.from(grid.querySelectorAll(':scope > .method-card')).filter(visible).map(card=>({...rect(card),label:label(card)}));
  // auto-fit may retain unused 0px tracks in the computed CSS; count occupied columns.
  const columns=new Set(cards.map(card=>Math.round(card.left))).size;
  return {label:label(grid),gridWidth:bounds.width,template,columns,cards,pass:columns<=2&&cards.every(card=>card.left>=bounds.left-1&&card.right<=bounds.right+1)};
 });
 return {pairs,parallel,cardTables,methodGrids,openDetails:scope.querySelectorAll('details[open]').length};
}

(async()=>{
 const chrome='/Applications/Google Chrome.app/Contents/MacOS/Google Chrome';
 browser=await chromium.launch({headless:true,...(process.env.CHROME_PATH||fs.existsSync(chrome)?{executablePath:process.env.CHROME_PATH||chrome}:{})});
 const context=await browser.newContext({viewport:{width:1440,height:1000},reducedMotion:'reduce'}),page=await context.newPage();
 page.on('pageerror',error=>errors.push(error.message));
 await context.route('**/*',async route=>{
  const url=new URL(route.request().url()),p=url.pathname;
  if(url.hostname!=='127.0.0.1'){unexpected.push(route.request().url());return route.abort()}
  const asset=assets[path.basename(p)];
  if(asset&&(p.startsWith('/static/')||p.startsWith('/assets/')))return route.fulfill({contentType:p.endsWith('.css')?'text/css':'text/javascript',body:fs.readFileSync(asset.file)});
  if(await require('./assets.cjs').serveAsset(route,root))return;
  let data={};
  if(p==='/')return route.fulfill({contentType:'text/html',body:html});
  if(p==='/auth/status')data={authenticated:true,user:{username:'合成布局核查',role:'user'}};
  else if(p==='/tasks')data=fixture;
  else if(p==='/config')data={config:{}};
  else if(p==='/connections'){
   if(connectionReadFailure)return route.fulfill({status:503,json:{error:'合成读取失败：本机凭据库暂时不可用，请重新读取；已有连接信息仍保留。'}});
   data={connections:[connection],storage_available:true};
  }
  else if(p==='/templates')data={templates:[]};
  else if(p==='/routing-corpus')data={tiers:[],metrics:[]};
  else if(p==='/quality-reference-records')data={records:[]};
  else if(p==='/quality-suite')data={manifest:{title:'合成题集',case_count:2},cases:[]};
  else if(p==='/fingerprint/catalog')data={presets:[],probes:[]};
  else if(p==='/fingerprint/references')data={references:[]};
  else if(p==='/fingerprints/'+fingerprint.id)data=fingerprint;
  else if(p==='/tpm/runs')data={runs:[tpm]};
  else if(p==='/tpm/runs/'+tpm.id)data={run:tpm};
  else if(p==='/tpm/runs/'+tpm.id+'/live')data={...tpm,recent_requests_view:'display-v1'};
  else if(p.startsWith('/routing-comparison/')&&p.endsWith('/requests'))data={run_id:routing.id,side:url.searchParams.get('side'),items:[{request_index:0,sequence:1,ok:true,completed:true,status_code:200,ttft_ms:120,total_ms:450,timing:requestTiming}],total:1,offset:0,has_more:false,available:true};
  else if(p==='/favicon.ico')data={};
  else {unexpected.push(p);return route.fulfill({status:404,json:{error:'Synthetic route missing'}})}
  return route.fulfill({json:data});
 });
 await page.goto('http://127.0.0.1:18777/');
 await page.waitForFunction(()=>authenticated&&refreshTimer!==null&&!refreshing);
 await page.evaluate(()=>clearInterval(refreshTimer));
 async function openConnections(failed=false){
  connectionReadFailure=false;
  await page.evaluate(async()=>{setModule('connections');await ConnectionUI.refresh(true);ConnectionUI.select('spacing-connection')});
  if(failed){
   connectionReadFailure=true;
   try{await page.evaluate(()=>ConnectionUI.refresh(true).catch(()=>{}))}finally{connectionReadFailure=false}
  }
 }
 const scenarios=[
  {name:'gateway',open:()=>page.evaluate(()=>{setModule('gateway');setWorkspaceView('single');render(tasks.find(x=>x.id==='single-fixture'))}),refresh:()=>page.evaluate(()=>render(window.currentDetail)),scope:'#detail'},
  {name:'comparison',open:()=>page.evaluate(()=>{setModule('gateway');setWorkspaceView('compare');setComparisonTab('compare-fixture','report')}),refresh:()=>page.evaluate(()=>setComparisonTab('compare-fixture','report')),scope:'#modulePlaceholder'},
  {name:'comparison-group',open:()=>page.evaluate(()=>setComparisonTab('compare-fixture','base')),refresh:()=>page.evaluate(()=>setComparisonTab('compare-fixture','base')),scope:'#modulePlaceholder'},
  {name:'routing',open:()=>page.evaluate(()=>{setModule('routing');setRoutingTab('spacing-route','report')}),refresh:()=>page.evaluate(()=>setRoutingTab('spacing-route','report')),scope:'#modulePlaceholder'},
  {name:'routing-side',open:()=>page.evaluate(()=>setRoutingTab('spacing-route','baseline')),refresh:()=>page.evaluate(()=>setRoutingTab('spacing-route','baseline')),scope:'#modulePlaceholder'},
  {name:'quality',open:()=>page.evaluate(()=>{setModule('quality');setWorkspaceView('compare');setQualityTab('quality-fixture','report')}),refresh:()=>page.evaluate(()=>renderQualityDetail(window.currentQualityDetail)),scope:'#modulePlaceholder'},
  {name:'fingerprint',open:async()=>{await page.evaluate(()=>setModule('fingerprint'));await page.evaluate(()=>FingerprintUI.select('spacing-fingerprint'))},refresh:()=>page.evaluate(()=>FingerprintUI.render()),scope:'#modulePlaceholder'},
  {name:'fingerprint-distribution',open:()=>page.evaluate(()=>FingerprintUI.setTab('distribution')),refresh:()=>page.evaluate(()=>FingerprintUI.render()),scope:'#modulePlaceholder'},
  {name:'fingerprint-diagnostics',open:()=>page.evaluate(()=>FingerprintUI.setTab('diagnostics')),refresh:()=>page.evaluate(()=>FingerprintUI.render()),scope:'#modulePlaceholder'},
  {name:'fingerprint-protocol',open:()=>page.evaluate(()=>FingerprintUI.setTab('protocol')),refresh:()=>page.evaluate(()=>FingerprintUI.render()),scope:'#modulePlaceholder'},
  {name:'tpm',open:async()=>{await page.evaluate(()=>setModule('tpm'));await page.evaluate(()=>TpmUI.select('spacing-tpm'))},refresh:()=>page.evaluate(()=>TpmUI.render()),scope:'#modulePlaceholder'},
  {name:'tpm-plan',open:()=>page.evaluate(()=>TpmUI.setTab('plan')),refresh:()=>page.evaluate(()=>TpmUI.render()),scope:'#modulePlaceholder'},
  {name:'connections',open:()=>openConnections(),refresh:()=>page.evaluate(()=>ConnectionUI.render()),scope:'#modulePlaceholder'},
  {name:'connections-error',open:()=>openConnections(true),refresh:()=>page.evaluate(()=>ConnectionUI.render()),scope:'#modulePlaceholder'},
  {name:'components',open:()=>page.evaluate(()=>{
   setModule('gateway');document.querySelector('#detail').hidden=true;const host=document.querySelector('#modulePlaceholder');host.hidden=false;
   const metrics=()=>UI.metrics([{label:'已收到结果',value:20,unit:'条'},{label:'成功率',value:90,unit:'%'}]);
   const table=()=>UI.table({columns:[{label:'指标'},{label:'固定路由'},{label:'智能路由'}],rowsHtml:[['成功率','100%','90%'],['首字耗时','120 毫秒','135 毫秒']]});
   const methods=()=>UI.methodCards([{title:'观测口径',lines:['只统计完整响应。']},{title:'方法边界',lines:['合成证据用于检查组件布局。']},{title:'复核动作',lines:['补充同条件复测，并分别检查成功率、失败类别和完整响应耗时。']}]);
   const nested=UI.disclosure({title:'展开完整方法与证据',id:'spacingNested',bodyHtml:metrics()+table()+methods()+UI.notice('展开后仍需保留内容间距。')});
   host.innerHTML=UI.pageShell(UI.pageHeader({kicker:'组件组合回归',title:'报告内容顺序与间距'})+UI.section({title:'混合内容块',bodyHtml:metrics()+table()+methods()+UI.notice('当前数据不构成容量结论。')+nested})+
    UI.disclosure({title:'跨区块展开详情',id:'spacingSections',section:true,bodyHtml:UI.section({title:'指标与表格',bodyHtml:metrics()+table()})+UI.section({title:'方法与提示',bodyHtml:methods()+UI.hint('只在本次观测窗口内解释结果。')})}));
  }),scope:'#modulePlaceholder'}
 ];
 async function settle(){await page.evaluate(()=>new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(resolve))))}
 async function findScrollport(){
  // Mobile intentionally scrolls .shell, while desktop scrolls .main. Find the
  // actual scrollport rather than assuming the report element is scrollable.
  return page.evaluate(()=>{
   document.querySelectorAll('[data-spacing-scrollport]').forEach(node=>node.removeAttribute('data-spacing-scrollport'));
   for(let node=document.querySelector('.main');node;node=node.parentElement){
    if(/^(auto|scroll)$/.test(getComputedStyle(node).overflowY)){
     node.setAttribute('data-spacing-scrollport','true');return node.id?'#'+node.id:'.'+Array.from(node.classList).join('.');
    }
   }
   throw new Error('No actual report scrollport found');
  });
 }
 async function checkReadingPosition(scenario){
  if(!scenario.refresh)return null; // The mixed-component fixture has no polling renderer.
  const scrollport=await findScrollport(),scroller=page.locator('[data-spacing-scrollport]');
  await scroller.evaluate(node=>{node.scrollTop=Math.round((node.scrollHeight-node.clientHeight)*.65)});await settle();
  const before=await scroller.evaluate(node=>({top:node.scrollTop,left:node.scrollLeft,height:node.clientHeight,total:node.scrollHeight}));
  const openBefore=await page.locator(scenario.scope+' details[open]').count();
  await scenario.refresh();await settle();
  const after=await scroller.evaluate(node=>({top:node.scrollTop,left:node.scrollLeft,height:node.clientHeight,total:node.scrollHeight}));
  const openAfter=await page.locator(scenario.scope+' details[open]').count();
  return {scrollport,before,after,openBefore,openAfter,pass:Math.abs(before.top-after.top)<=1&&Math.abs(before.left-after.left)<=1&&openBefore===openAfter};
 }
 async function captureAll(name){
  const scrollport=await findScrollport();
  const scroller=page.locator('[data-spacing-scrollport]'),tiles=[];
  await scroller.evaluate(node=>{node.scrollTop=0});await settle();
  let next=0;
  while(true){
   await scroller.evaluate((node,top)=>{node.scrollTop=top},next);await settle();
   const geometry=await scroller.evaluate(node=>{const bounds=node.getBoundingClientRect();return {top:node.scrollTop,height:node.clientHeight,total:node.scrollHeight,viewportTop:bounds.top,viewportBottom:bounds.bottom,viewportHeight:innerHeight}});
   assert(geometry.viewportTop>=-1&&geometry.viewportBottom<=geometry.viewportHeight+1,'complete-scroll capture requires the entire scroller viewport to be visible');
   const filename=`${name}-part-${String(tiles.length+1).padStart(2,'0')}.png`;
   await page.screenshot({path:path.join(output,filename)});
   tiles.push({...geometry,scrollport,file:filename});
   if(geometry.top+geometry.height>=geometry.total-1)break;
   assert(tiles.length<100,'report unexpectedly exceeds 100 screenshot tiles');
   next=Math.min(geometry.total-geometry.height,geometry.top+geometry.height-32);
   assert(next>geometry.top,'internal report scroller failed to advance');
  }
  assert.equal(tiles[0].top,0,'full report capture begins at the top');
  for(let i=1;i<tiles.length;i++)assert(tiles[i].top<=tiles[i-1].top+tiles[i-1].height,'screenshot tiles omit report content');
  return tiles;
 }
 // Width/theme are outside each view, so modules are always reached in the same order.
 for(const width of [1440,980,390])for(const theme of ['light','dark']){
  await page.setViewportSize({width,height:1000});await page.evaluate(value=>setTheme(value),theme);
  for(const scenario of scenarios){
   await scenario.open();await settle();await page.locator(scenario.scope).waitFor({state:'visible'});
   if(scenario.name==='routing-side')await page.waitForFunction(()=>document.querySelector('[data-time-key="routing-request:spacing-route:baseline:1"]'));
   await page.locator(scenario.scope+' details').evaluateAll(nodes=>nodes.forEach(node=>{node.open=false}));await settle();
   const collapsed=await page.evaluate(collectSpacing,scenario.scope);
   await page.locator(scenario.scope+' details').evaluateAll(nodes=>nodes.forEach(node=>{node.open=true}));await settle();
   const expanded=await page.evaluate(collectSpacing,scenario.scope);
   assert(expanded.pairs.length>0,`${scenario.name} must exercise adjacent report content`);
   if(scenario.name==='components'){
    const combinations=new Set(expanded.pairs.map(pair=>pair.beforeKind+'>'+pair.afterKind));
    for(const combination of ['metrics>table','table>methods','methods>notice','notice>disclosure','section>section'])assert(combinations.has(combination),'missing mixed-component coverage: '+combination);
   }
   const readingPosition=await checkReadingPosition(scenario);
   if(readingPosition&&!readingPosition.pass)scrollViolations.push({scenario:scenario.name,width,theme,...readingPosition});
   const name=`${scenario.name}-${width}-${theme}`,tiles=await captureAll(name);
   const item={scenario:scenario.name,width,theme,collapsed,expanded,readingPosition,tiles};
   checks.push(item);
   for(const [state,result] of [['collapsed',collapsed],['expanded',expanded]]){
    for(const pair of result.pairs)if(!pair.pass)violations.push({scenario:scenario.name,width,theme,state,...pair});
    for(const table of result.cardTables)if(!table.pass)clippedTables.push({scenario:scenario.name,width,theme,state,...table});
    for(const grid of result.methodGrids)if(!grid.pass)methodViolations.push({scenario:scenario.name,width,theme,state,...grid});
   }
   fs.writeFileSync(path.join(output,'spacing-results.json'),JSON.stringify({synthetic:true,baseline,assets,checks,violations,clippedTables,methodViolations,scrollViolations,errors,unexpected},null,2));
  }
  console.log(`Measured ${width}px ${theme}: report sections and expanded details; complete scroll captures saved.`);
 }
 assert.deepEqual(errors,[],'browser exceptions');assert.deepEqual(unexpected,[],'unexpected network calls');
 assert(checks.some(check=>check.width===390&&check.expanded.cardTables.length>0),'narrow card-layout summary tables must be exercised');
 assert.equal(scrollViolations.length,0,`${scrollViolations.length} report rerenders lost reading position or expanded details:\n`+scrollViolations.map(check=>`${check.scenario}/${check.width}/${check.theme}: ${check.scrollport} ${check.before.top} → ${check.after.top}px; open details ${check.openBefore} → ${check.openAfter}`).join('\n'));
 assert.equal(methodViolations.length,0,`${methodViolations.length} method grids exceed two columns or their container:\n`+methodViolations.slice(0,12).map(grid=>`${grid.scenario}/${grid.width}/${grid.theme}/${grid.state}: ${grid.columns} columns (${grid.template})`).join('\n'));
 assert.equal(clippedTables.length,0,`${clippedTables.length} card-layout summary tables are vertically clipped:\n`+clippedTables.map(table=>`${table.scenario}/${table.width}/${table.theme}/${table.state}: ${table.scrollHeight}px content inside ${table.clientHeight}px viewport`).join('\n'));
 assert.equal(violations.length,0,`${violations.length} adjacent report blocks have insufficient spacing:\n`+violations.slice(0,20).map(v=>`${v.scenario}/${v.width}/${v.theme}/${v.state}: ${v.beforeKind} → ${v.afterKind}: ${v.gap}px < ${v.required}px (${v.parent})`).join('\n'));
 console.log(`PASS: ${checks.length} report/theme/width views; ${checks.reduce((n,c)=>n+c.collapsed.pairs.length+c.expanded.pairs.length,0)} sibling-gap measurements, ${checks.reduce((n,c)=>n+c.tiles.length,0)} complete-scroll screenshots. Artifacts: ${output}`);
})().catch(error=>{console.error(error);process.exitCode=1}).finally(async()=>{await browser?.close()});
