/* Pure rendering regression: no browser, server, credentials, or upstream. */
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const asset = process.env.UI_FINGERPRINT_JS || path.resolve(__dirname,'../static/fingerprint.js');
const source = fs.readFileSync(asset,'utf8').replace('window.FingerprintUI = Object.freeze({init,setRuns,updateChrome,render,openCreate,closeCreate,hide,onAuthLost});','window.__renderTest = {overviewHtml,detailHtml};');
const sandbox = {window:{},document:{getElementById:()=>null},URL,console,TextEncoder};
vm.runInNewContext(source,sandbox,{filename:asset});
const {overviewHtml,detailHtml} = sandbox.window.__renderTest;
function run({complete=true,cells=4,temporalCells=cells,mean=.03,temporalMean=mean,warning=false}={}) {
  return {id:'test',name:'测试采集',mode:'selfcheck',status:complete?'finished':'stopped',progress:{done:56,total:200,valid:48,errors:0},protocol:{cells:['a','b','c','d'],samples_per_cell:50,concurrency:1},endpoint:{model:'synthetic'},artifact:{complete,diagnostics:{valid_rate:.9,split_half:{alternating:{mean_jsd:mean,comparable_cells:cells},temporal:{mean_jsd:temporalMean,comparable_cells:temporalCells},warning}}}};
}
let checks=0;
function check(condition,message){assert.ok(condition,message);checks++;}
const partial = overviewHtml(run({complete:false,cells:1}));
check(partial.includes('采集未完成，已保留部分证据'),'incomplete collection takes precedence');
check(partial.includes('样本或覆盖不足，不能评价稳定性'),'partial evidence cannot establish stability');
check(!partial.includes('本次分半未提示明显不稳定'),'no positive stability claim from partial evidence');
check(detailHtml(run({complete:false,cells:1})).includes('56 / 200 次请求已结算'),'stopped artifacts retain progress counts');
check(overviewHtml(run({cells:1})).includes('样本或覆盖不足，不能评价稳定性'),'one comparable cell is insufficient');
check(overviewHtml(run({cells:4,temporalCells:3})).includes('样本或覆盖不足，不能评价稳定性'),'both split methods need coverage');
check(overviewHtml(run({cells:4,temporalMean:null})).includes('样本或覆盖不足，不能评价稳定性'),'both distances need valid observations');
check(overviewHtml(run()).includes('本次分半未提示明显不稳定'),'complete evidence with both split methods may describe observed stability');
check(overviewHtml(run({warning:true})).includes('两种分半检查中存在异常波动'),'unstable evidence remains qualified');
const compared=run();compared.comparison={verdict:'uncertain',mean_jsd:.35,thresholds:{match:.25,mismatch:.35},comparable_cells:4,protocol_compatible:true};
check(overviewHtml(compared).includes('> 0.35 提示明显差异'),'mismatch interval excludes boundary 0.35');
const emptyRun=run();
Object.assign(emptyRun.artifact.diagnostics,{valid_rate:0,truncated_rate:1,reasoning_observed:true});
emptyRun.artifact.diagnostics.split_half={alternating:{mean_jsd:null,comparable_cells:0},temporal:{mean_jsd:null,comparable_cells:0},warning:false};
const failedHtml=overviewHtml(emptyRun);
check(failedHtml.includes('未获得有效指纹'),'zero-valid historical collection is not presented as a fingerprint');
check(!failedHtml.includes('已形成指纹证据'),'no success heading for empty evidence');
check(failedHtml.includes('关闭思考适配重新采集'),'reasoning truncation shows actionable recovery');
check(!failedHtml.includes('本次分半未提示明显不稳定'),'empty evidence cannot imply stability');
console.log(JSON.stringify({status:'passed',checks}));
