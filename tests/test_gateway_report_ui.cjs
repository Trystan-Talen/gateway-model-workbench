'use strict';
/* Exercise report behavior with real markup builders and a recording canvas; no server or upstream calls. */
const assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path'),vm=require('node:vm');
const root=path.resolve(__dirname,'..'),source=fs.readFileSync(path.join(root,'static/app.js'),'utf8');
function between(start,end){
 const a=source.indexOf(start),b=source.indexOf(end,a+start.length);
 assert.ok(a>=0&&b>a,`source boundaries: ${start}`);
 return source.slice(a,b);
}
const helpers=between('function gatewayOutcome(','async function showRaw(');
const drawing=between('function drawChart(','async function stopTask(');
let checks=0;
function check(name,fn){try{fn();checks++}catch(error){error.message=`${name}: ${error.message}`;throw error}}
function harness(){
 const frames=[],strokes=[],labels=[];
 let pen=[];
 const context={
  setTransform(){},clearRect(){strokes.length=0;labels.length=0},setLineDash(){},
  beginPath(){pen=[]},moveTo(x,y){pen.push([x,y])},lineTo(x,y){pen.push([x,y])},
  stroke(){strokes.push({color:this.strokeStyle,points:pen.slice()})},
  fillText(text,x,y){labels.push({text,x,y})},arc(){},fill(){}
 };
 const nodes=new Map(['loadingState','empty','modulePlaceholder','detail'].map(id=>[id,{hidden:false,innerHTML:'',querySelector(){return null}}]));
 nodes.set('chart',{style:{},parentElement:{getBoundingClientRect:()=>({width:500,height:200})},getContext:()=>context});
 let scrolls=0;
 nodes.set('gatewayRequests',{scrollIntoView(){scrolls++}});
 const sandbox={console,window:{devicePixelRatio:1},document:{documentElement:{}},
  $:id=>nodes.get(id)||null,requestAnimationFrame:fn=>frames.push(fn),
  getComputedStyle:()=>({getPropertyValue:name=>name}),detailState:{id:null},
  qualityScrollSnapshot:()=>({}),restoreQualityScroll(){},showLoading(){},refresh(){},
 };
 vm.createContext(sandbox);
 vm.runInContext(fs.readFileSync(path.join(root,'static/ui-kit.js'),'utf8'),sandbox);
 sandbox.UI=sandbox.window.UI;
 const dependencies=[
  between('function esc(v)','function toast(t)'),
  source.match(/^const statusNames=.*;$/m)[0],
  between('function gatewayErrorText(','function taskItem('),
  between('function artifactNotice(','function openModal('),
  between('function runHeaderAction(','function routingStatusLabel(')
 ].join('\n');
 vm.runInContext(`${dependencies}\n${helpers}\n${drawing}
 window.test={gatewayOutcome,gatewayCacheDetails,gatewayExecutionHtml,gatewayRequestsHtml,gatewayArtifactAvailable,setGatewayRequestFilter,render,drawChart};`,sandbox);
 return {ui:sandbox.window.test,window:sandbox.window,nodes,strokes,labels,
  flush(){while(frames.length)frames.shift()()},get scrolls(){return scrolls}};
}
const base=overrides=>({
 id:'report-a',name:'合成报告',mode:'冒烟验证',status:'finished',total:10,done:10,progress:1,
 counters:{ok:10},success_pct:100,scheduled:10,started:10,inflight:0,pending:0,dropped:0,
 config:{profile:'cache',capture_raw:false},cache:{hits:0,no_usage:0,request_total:10,rate_pct:0},
 recent:[],timeline:[],rpm:{avg:60,live:120},target_rpm:60,ttft:{p95:80},
 ...overrides
});
const rows=[
 {id:1,request_id:'success-request',step:1,cls:'ok',label:'成功',status:200,total:100,ttft:70},
 {id:2,request_id:'failed-request',step:1,cls:'r5xx',label:'服务端错误',status:502,total:20,error_type:'HTTP_5XX',reason:'合成异常'}
];
const a=harness();
for(const status of ['waiting','running','stopping']){
 check(`${status}: no results are provisional, never success`,()=>{
  const result=a.ui.gatewayOutcome(base({status,done:0,counters:{}}));
  assert.equal(result.tone,'neutral');assert.match(result.title,/等待/);assert.equal(result.failed,0);
  assert.doesNotMatch(result.title,/成功/);
 });
}
for(const status of ['error','stopped','expired']){
 check(`${status}: no results carry a recovery action and no success claim`,()=>{
  const result=a.ui.gatewayOutcome(base({status,done:0,counters:{}}));
  assert.equal(result.tone,'warning');assert.match(result.title,/没有.*结果/);assert.match(result.detail,/检查.*连接/);
 });
}
check('full success is explicitly limited to this observation window',()=>{
 const result=a.ui.gatewayOutcome(base());assert.equal(result.tone,'success');
 assert.match(result.title,/完整成功/);assert.match(result.detail,/不代表.*容量/);assert.equal(result.failed,0);
});
check('stopped successful requests cannot imply the whole plan completed',()=>{
 const result=a.ui.gatewayOutcome(base({status:'stopped',done:4,counters:{ok:4}}));
 assert.equal(result.tone,'warning');assert.match(result.title,/未完整/);assert.match(result.detail,/6 条未完成/);
});
check('completed errors retain both the actual success and failure counts',()=>{
 const result=a.ui.gatewayOutcome(base({status:'finished_with_errors',counters:{ok:8,r5xx:2}}));
 assert.equal(result.failed,2);assert.equal(result.tone,'warning');
 assert.match(result.detail,/成功 8 条.*失败 2 条/);assert.doesNotMatch(result.title,/均.*成功/);
});
check('partial failure reports missing coverage as well as failed requests',()=>{
 const result=a.ui.gatewayOutcome(base({status:'stopped',done:4,counters:{ok:3,r5xx:1}}));
 assert.equal(result.failed,1);assert.match(result.detail,/失败 1 条/);assert.match(result.detail,/6 条未完成/);
});
check('live successful results are not a final pass verdict',()=>{
 const result=a.ui.gatewayOutcome(base({status:'running'}));
 assert.equal(result.tone,'neutral');assert.match(result.detail,/过程结果.*完成后/);
});
check('known zero cache hits remain distinct from missing cache evidence',()=>{
 const known=a.ui.gatewayCacheDetails(base());
 const missing=a.ui.gatewayCacheDetails(base({cache:{hits:0,no_usage:10,request_total:10,rate_pct:0}}));
 assert.match(known,/已返回缓存字段的 10 条.*均未报告缓存命中/);
 assert.doesNotMatch(missing,/均未报告缓存命中/);assert.match(missing,/10 条缺少缓存字段/);
 assert.match(missing,/不能据此证明上游没有缓存/);
});
check('mixed cache evidence uses all requests as denominator',()=>{
 const text=a.ui.gatewayCacheDetails(base({done:12,cache:{hits:0,no_usage:3,request_total:10,rate_pct:0}}));
 assert.match(text,/命中 0 \/ 已返回 10 条/);assert.match(text,/缓存字段的 7 条/);assert.match(text,/3 条缺少缓存字段/);
});
check('zero cached tokens are valid measured usage, not unavailable usage',()=>{
 const zero=a.ui.gatewayCacheDetails(base({cache:{hits:0,no_usage:0,request_total:10,token_eligible:10,prompt_tokens:100,cached_tokens:0,token_rate_pct:0}}));
 assert.match(zero,/缓存 0 \/ 输入 100 词元，比例 0%/);assert.doesNotMatch(zero,/无法计算/);
 const missing=a.ui.gatewayCacheDetails(base({cache:{token_eligible:0}}));assert.match(missing,/无法计算/);
});
check('live execution distinguishes queue and in-flight requests',()=>{
 const html=a.ui.gatewayExecutionHtml(base({status:'running',done:2,progress:.2,scheduled:7,started:5,inflight:3,pending:2}));
 assert.match(html,/已返回 2 \/ 10/);assert.match(html,/在途 3 · 排队 2/);assert.match(html,/aria-valuenow="20"/);
});
check('finished execution keeps uncompleted work rather than showing a full plan',()=>{
 const html=a.ui.gatewayExecutionHtml(base({status:'stopped',done:4,progress:.4,dropped:6}));
 assert.match(html,/执行记录/);assert.match(html,/未完成 6/);assert.match(html,/aria-valuenow="40"/);
});
check('no results display unknown success/cache rates rather than measured zeros',()=>{
 a.ui.render(base({status:'error',done:0,progress:0,counters:{},success_pct:null,cache:{},error:'连接失败'}));
 const html=a.nodes.get('detail').innerHTML;
 assert.match(html,/请求成功率<\/div><div class="metric-value">—<\/div>/);
 assert.match(html,/缓存命中率（请求）<\/div><div class="metric-value">—<\/div>/);
 assert.match(html,/连接失败/);assert.match(html,/本次没有可用请求结果/);
});
check('finished execution moves behind the result while live execution remains above it',()=>{
 a.ui.render(base());let html=a.nodes.get('detail').innerHTML;
 assert.ok(html.indexOf('overview-shell')<html.indexOf('report-progress-card'));
 assert.match(html,/<details[^>]*id="gateway-execution-report-a"/);assert.doesNotMatch(html,/<details[^>]*id="gateway-execution-report-a"[^>]* open/);
 a.ui.render(base({status:'running'}));html=a.nodes.get('detail').innerHTML;
 assert.ok(html.indexOf('report-progress-card')<html.indexOf('overview-shell'));
});
check('filter selection survives polling and only contains failed recent requests',()=>{
 a.ui.render(base({recent:rows}));a.ui.setGatewayRequestFilter('report-a','failed');a.flush();
 let html=a.nodes.get('detail').innerHTML;
 assert.match(html,/failed-request/);assert.doesNotMatch(html,/success-request/);
 assert.match(html,/aria-selected="true"[^>]*data-arg1="failed"/);
 a.ui.render(base({recent:[...rows,{...rows[0],id:3,request_id:'new-success'}]}));
 html=a.nodes.get('detail').innerHTML;
 assert.match(html,/failed-request/);assert.doesNotMatch(html,/new-success/);assert.equal(a.scrolls,1);
});
check('an empty failed subset does not claim all historical requests succeeded',()=>{
 a.ui.render(base({recent:[rows[0]]}));
 assert.match(a.nodes.get('detail').innerHTML,/不代表全部历史请求均成功/);
 assert.match(a.nodes.get('detail').innerHTML,/aria-selected="true"[^>]*data-arg1="failed"/);
});
check('selecting a different task resets the filter and ignores stale task actions',()=>{
 a.ui.render(base({id:'report-b',recent:rows}));
 let html=a.nodes.get('detail').innerHTML;assert.match(html,/success-request/);assert.match(html,/failed-request/);
 assert.match(html,/aria-selected="true"[^>]*data-arg1="all"/);
 a.ui.setGatewayRequestFilter('report-a','failed');assert.equal(a.nodes.get('detail').innerHTML,html);
});
check('unrecognized filters safely return to all recent results',()=>{
 a.ui.setGatewayRequestFilter('report-b','failed');a.ui.setGatewayRequestFilter('report-b','unexpected');
 assert.match(a.nodes.get('detail').innerHTML,/success-request/);
 assert.match(a.nodes.get('detail').innerHTML,/aria-selected="true"[^>]*data-arg1="all"/);
});
for(const artifact of [{status:'expired'},...['expired','missing','deleted'].map(state=>({artifact:{state}})),{artifact_state:'missing'}]){
 check(`unavailable evidence ${JSON.stringify(artifact)} suppresses broken retrieval actions`,()=>{
  a.ui.render(base({recent:rows,config:{capture_raw:true},out_path:'/synthetic/result.jsonl',...artifact}));
  const html=a.nodes.get('detail').innerHTML;
  assert.doesNotMatch(html,/data-click-action="(?:showRaw|download)"/);
  assert.match(html,/仅保留历史摘要/);assert.doesNotMatch(html,/完整结果可在页底下载/);
  assert.match(html,/success-request/);assert.match(html,/failed-request/);
 });
}
check('available raw evidence offers retrieval, absent full files do not promise downloads',()=>{
 a.ui.render(base({recent:rows,config:{capture_raw:true},out_path:'/synthetic/result.jsonl',artifact:{state:'present'}}));
 let html=a.nodes.get('detail').innerHTML;
 assert.match(html,/data-click-action="showRaw"/);assert.match(html,/data-click-action="download"/);
 a.ui.render(base({recent:rows,out_path:null}));
 html=a.nodes.get('detail').innerHTML;
 assert.doesNotMatch(html,/data-click-action="(?:showRaw|download)"/);assert.doesNotMatch(html,/完整结果可在页底下载/);
});
check('upstream request text remains escaped in the report',()=>{
 a.ui.render(base({recent:[{...rows[1],reason:'<img src=x onerror=alert(1)>',request_id:'<script>bad</script>'}]}));
 const html=a.nodes.get('detail').innerHTML;
 assert.doesNotMatch(html,/<img|<script/);assert.match(html,/&lt;img/);assert.match(html,/&lt;script/);
});
check('one failed request in a second is plotted as 60 per minute on the shared axis',()=>{
 a.ui.drawChart([{rpm:60,target_rpm:60,r5xx:1}]);
 const failed=a.strokes.find(line=>line.color==='--color-danger'),completed=a.strokes.find(line=>line.color==='--color-accent');
 assert.deepEqual(failed.points,completed.points);assert.equal(failed.points[0][1],12);
 assert.equal(a.labels[0].text,60);
});
check('all failure classes use the same rate scale, excluding successful counts',()=>{
 a.ui.drawChart([{rpm:420,target_rpm:420,ok:1,r429:1,r5xx:1,r4xx:1,timeout:1,network:1,protocol:1}]);
 const failed=a.strokes.find(line=>line.color==='--color-danger');
 assert.equal(a.labels[0].text,420);
 const observed=(172-failed.points[0][1])/160*420;
 assert.ok(Math.abs(observed-360)<1e-9,`six failed requests must plot at 360/min, got ${observed}`);
});
check('empty trends and absent failure categories do not generate invalid coordinates',()=>{
 a.ui.drawChart([]);assert.equal(a.strokes.length,0);
 a.ui.drawChart([{rpm:60,target_rpm:60}]);
 const failed=a.strokes.find(line=>line.color==='--color-danger');
 assert.equal(failed.points[0][1],172);assert.ok(a.strokes.every(line=>line.points.flat().every(Number.isFinite)));
});
check('irregular samples retain their real elapsed spacing and window duration',()=>{
 a.ui.drawChart([{t:100000,rpm:60},{t:101000,rpm:60},{t:110000,rpm:60}]);
 const points=a.strokes.find(line=>line.color==='--color-accent').points;
 assert.equal(points[0][0],45);assert.equal(points[2][0],486);
 assert.ok(Math.abs((points[1][0]-points[0][0])/(points[2][0]-points[0][0])-.1)<1e-9,'the middle sample occurred at one tenth of the time span');
 assert.equal(a.labels.at(-2).text,'采样窗口起点');assert.equal(a.labels.at(-1).text,'10 秒');
});
check('missing or reversed timestamps use sample positions without inventing elapsed seconds',()=>{
 for(const data of [[{rpm:60},{rpm:60},{rpm:60}],[{t:1000,rpm:60},{t:900,rpm:60},{t:1200,rpm:60}]]){
  a.ui.drawChart(data);
  const points=a.strokes.find(line=>line.color==='--color-accent').points;
  assert.equal(points[1][0],(points[0][0]+points[2][0])/2);
  assert.equal(a.labels.at(-2).text,'第 1 个采样');assert.equal(a.labels.at(-1).text,'第 3 个采样');
  assert.ok(a.labels.every(label=>!String(label.text).includes('秒')));
 }
});
console.log(`gateway report UI checks passed: ${checks}`);
