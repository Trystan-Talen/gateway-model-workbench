'use strict';
const fs=require('node:fs'),path=require('node:path'),{spawn}=require('node:child_process');
const {createBrowserRun,browserSuiteOutput}=require('./artifacts.cjs');
const run=createBrowserRun();
const scripts=['smoke','routing_create','routing_live','connections','repeat','layout','fingerprint','auth_focus','workflow','quality_v2','modal_layout'];
const results=[];
function writeResults(){fs.writeFileSync(path.join(run.runRoot,'run-result.json'),JSON.stringify({root:run.runRoot,suites:results},null,2));}
async function runSuite(name){
 const output=browserSuiteOutput(run.runRoot,name);
 console.log(`Browser regression（浏览器回归）: ${name}`);
 const result=await new Promise(resolve=>{
  const child=spawn(process.execPath,[path.join(__dirname,name+'.cjs')],{stdio:['inherit','pipe','pipe'],env:run.env});
  const record=(stream,filename,chunk)=>{fs.mkdirSync(output,{recursive:true});fs.appendFileSync(path.join(output,filename),chunk);stream.write(chunk)};
  child.stdout.on('data',chunk=>record(process.stdout,'stdout.log',chunk));
  child.stderr.on('data',chunk=>record(process.stderr,'stderr.log',chunk));
  child.once('error',error=>resolve({suite:name,exitCode:1,error:error.message}));
  child.once('close',(exitCode,signal)=>resolve({suite:name,exitCode,signal}));
 });
 fs.mkdirSync(output,{recursive:true});
 fs.writeFileSync(path.join(output,'process-result.json'),JSON.stringify(result,null,2));
 results.push(result);writeResults();
 return result.exitCode;
}
(async()=>{
 for(const name of scripts){const code=await runSuite(name);if(code!==0){process.exitCode=code||1;return}}
})().catch(error=>{console.error(error);results.push({error:error.message});writeResults();process.exitCode=1});
