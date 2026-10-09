'use strict';
/* Preserve rendering and statistical contracts while exercising the optimized paths.
 * Only synthetic records and a minimal DOM are used; no service or credentials. */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const root = path.resolve(__dirname,'..');
const appSource = fs.readFileSync(path.join(root,'static/app.js'),'utf8');
const uiSource = fs.readFileSync(path.join(root,'static/ui-kit.js'),'utf8');
const fingerprintSource = fs.readFileSync(path.join(root,'static/fingerprint.js'),'utf8');
function between(source,name,next) {
  const start = source.indexOf(`function ${name}(`), end = source.indexOf(`function ${next}(`,start + 1);
  assert.ok(start >= 0 && end > start,`missing ${name} boundary`);
  return source.slice(start,end);
}
const main = {scrollTop:143}, queries = new Map();
let painted = false, activeElement = null, oldRegions = [], newRegions = [], oldDetails = [], newDetails = [], actions = [], clicks = [];
const host = {
  dataset:{fpKey:'runs:run-a:overview'},
  contains:node=>node === activeElement && Boolean(node),
  set innerHTML(value) { this.content = value; painted = true; },
  querySelectorAll(selector) {
    queries.set(selector,(queries.get(selector) || 0) + 1);
    if (selector === '[data-fp-scroll]') return painted ? newRegions : oldRegions;
    if (selector === 'details[open][data-fp-details]') return oldDetails;
    if (selector === '[data-fp-details]') return newDetails;
    if (selector === '[data-fp-action]') return actions;
    if (selector === '[data-click-action]') return clicks;
    throw new Error(`unexpected selector: ${selector}`);
  }
};
const sandbox = {window:{},document:{getElementById:id=>id === 'modulePlaceholder' ? host : null,querySelector:selector=>selector === '.main' ? main : null,get activeElement(){return activeElement;}},URL,console};
vm.runInNewContext(uiSource,sandbox);
vm.runInNewContext(`const UI=window.UI;let tasks=[],comparisons=[],activeView='single';
${between(appSource,'mixedGatewayRecords','filteredQualityRecords')}
${between(appSource,'percentileFromSorted','qualityStats')}
${between(appSource,'qualityStats','qualityPercent')}
${between(appSource,'esc','num')}
window.__appTest={filteredGatewayRecords,qualityStats,esc,setRecords(a,b,view){tasks=a;comparisons=b;activeView=view}};`,sandbox);
vm.runInNewContext(fingerprintSource.replace(/window\.FingerprintUI = Object\.freeze\(\{[^}]+\}\);/,'window.__fpTest={s,paintHost,html};'),sandbox);
const app = sandbox.window.__appTest, fp = sandbox.window.__fpTest, UI = sandbox.window.UI;
const plain = value => JSON.parse(JSON.stringify(value));
let checks = 0;
function check(name,run) { run(); checks++; }

check('all renderers share unchanged HTML escaping for data and attributes',()=>{
  for (const value of [null,undefined,'',0,false,'中文 <script>& "quoted" \'single\'','&lt;already&gt;']) {
    const expected = String(value ?? '').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
    assert.equal(UI.esc(value),expected); assert.equal(app.esc(value),expected); assert.equal(fp.html(value),expected);
  }
});
check('selected record filters retain descending dates, stable ties, tags and frozen inputs',()=>{
  const tasks = Object.freeze([{id:'old',created:1},{id:'new-a',created:3},{id:'new-b',created:3},{id:'undated'}].map(Object.freeze));
  const comparisons = Object.freeze([{id:'comparison',created:100}].map(Object.freeze));
  app.setRecords(tasks,comparisons,'single');
  assert.deepEqual(plain(app.filteredGatewayRecords()),[{id:'new-a',created:3,record_type:'single'},{id:'new-b',created:3,record_type:'single'},{id:'old',created:1,record_type:'single'},{id:'undated',record_type:'single'}]);
  app.setRecords(tasks,comparisons,'compare');
  assert.deepEqual(plain(app.filteredGatewayRecords()),[{id:'comparison',created:100,record_type:'compare'}]);
  app.setRecords(tasks,comparisons,'unknown'); assert.deepEqual(plain(app.filteredGatewayRecords()),[]);
  assert.equal(tasks[0].record_type,undefined); assert.equal(comparisons[0].record_type,undefined);
});
check('legacy non-numeric dates preserve the mixed-list ordering',()=>{
  app.setRecords([{id:'task',created:1}],[{id:'a',created:2},{id:'legacy',created:'invalid'},{id:'b',created:2}],'compare');
  assert.deepEqual(plain(app.filteredGatewayRecords()).map(item=>item.id),['a','b','legacy']);
});
check('the inactive record collection is never copied or sorted for numeric dates',()=>{
  let reads = 0;
  const inactive = Array.from({length:1000},(_,id)=>({id,created:id,get name(){reads++;return 'inactive';}}));
  app.setRecords([{id:'visible',created:1}],inactive,'single');
  assert.equal(app.filteredGatewayRecords()[0].id,'visible'); assert.equal(reads,0);
  app.setRecords(inactive,[{id:'visible',created:1}],'compare');
  assert.equal(app.filteredGatewayRecords()[0].id,'visible'); assert.equal(reads,0);
});
check('quality metrics preserve missing timings, nearest-rank percentiles and partial scoring',()=>{
  const resource = Object.freeze({planned_cases:10,request_errors:['synthetic-error'],case_results:Object.freeze([
    {score:1,passed:true,total_ms:80},{score:0,passed:false,total_ms:20},{score:null,passed:true,total_ms:null},
    {score:1,passed:'legacy-truthy',total_ms:'40'},{score:0,passed:false,total_ms:''},{score:0,passed:false,total_ms:'invalid'}
  ].map(Object.freeze))});
  assert.deepEqual(plain(app.qualityStats(resource)),{total:10,passed:3,passRate:null,score:null,completed:4,errors:1,totalMsP50:20,totalMsP95:80,averageTotalMs:35});
  assert.deepEqual(resource.case_results.map(item=>item.total_ms),[80,20,null,'40','','invalid']);
});
check('quality summaries keep explicit zero overrides and complete fallback values',()=>{
  const results = [{score:1,passed:true,total_ms:12.5}];
  assert.deepEqual(plain(app.qualityStats({case_results:results})),{total:1,passed:1,passRate:1,score:null,completed:1,errors:0,totalMsP50:12.5,totalMsP95:12.5,averageTotalMs:12.5});
  assert.deepEqual(plain(app.qualityStats({case_results:results,summary:{passed_cases:0,pass_rate:0,percentage:0}})),{total:1,passed:0,passRate:0,score:0,completed:1,errors:0,totalMsP50:12.5,totalMsP95:12.5,averageTotalMs:12.5});
  assert.deepEqual(plain(app.qualityStats(null)),{total:0,passed:0,passRate:null,score:null,completed:0,errors:0,totalMsP50:null,totalMsP95:null,averageTotalMs:null});
});
check('quality averages retain request-order floating-point rounding',()=>{
  const resource = {case_results:[1e16,1,-1e16].map(total_ms=>({total_ms}))};
  const stats = app.qualityStats(resource);
  assert.equal(stats.averageTotalMs,0); assert.equal(stats.totalMsP50,1); assert.equal(stats.totalMsP95,1e16);
});
check('quality p50 and p95 share one timing sort',()=>{
  vm.runInNewContext('window.__sortCalls=0;const previousSort=Array.prototype.sort;Array.prototype.sort=function(...args){window.__sortCalls++;return previousSort.apply(this,args)};',sandbox);
  app.qualityStats({case_results:Array.from({length:1000},(_,index)=>({total_ms:1000-index}))});
  assert.equal(sandbox.window.__sortCalls,1);
});
function region(id,left=0,top=0) { return {dataset:{fpScroll:id},scrollLeft:left,scrollTop:top}; }
function detail(id,open=false) { return {dataset:{fpDetails:id},open}; }
function action(dataset) { return {dataset,focus(options){this.focused=options;}}; }
check('fingerprint repaint restores scrolling, expansion and delegated-action focus',()=>{
  fp.s.selected='run-a'; fp.s.storageWarnings=['synthetic-warning'];
  oldRegions=[region('shared',11,12),region('other',21,22),region('shared',31,32)];
  newRegions=[region('shared'),region('shared'),region('other')];
  oldDetails=[detail('proof',true),detail('missing',true)]; newDetails=[detail('proof'),detail('proof')];
  activeElement=action({fpAction:'tab',fpId:'run-a',fpTab:'overview'});
  actions=[action({...activeElement.dataset})];
  fp.paintHost('<section>same content</section>');
  assert.deepEqual(newRegions.map(node=>[node.scrollLeft,node.scrollTop]),[[31,32],[0,0],[21,22]]);
  assert.deepEqual(newDetails.map(node=>node.open),[true,false]);
  assert.equal(actions[0].focused.preventScroll,true); assert.equal(main.scrollTop,143);
  assert.match(host.content,/synthetic-warning/); assert.match(host.content,/same content/);
  assert.equal(queries.get('[data-fp-scroll]'),2); assert.equal(queries.get('[data-fp-details]'),1);
});
check('fingerprint repaint retains shared-action focus and resets scroll after changing records',()=>{
  painted=false; queries.clear(); fp.s.selected='run-a'; activeElement=action({clickAction:'repeatTest',arg1:'run-a'});
  clicks=[action({...activeElement.dataset})];
  fp.paintHost('shared action'); assert.equal(clicks[0].focused.preventScroll,true);
  painted=false; queries.clear(); fp.s.selected='run-b'; main.scrollTop=143;
  fp.paintHost('different record'); assert.equal(main.scrollTop,0);
  assert.equal(queries.get('[data-fp-scroll]'),undefined); assert.equal(queries.get('[data-fp-details]'),undefined);
});
console.log(JSON.stringify({status:'passed',checks}));
