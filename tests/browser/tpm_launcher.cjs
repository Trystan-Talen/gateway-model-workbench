/** Exercise the unmodified double-click launcher, real backend and real loopback HTTP.
 * Only the OS credential boundary and browser-opening command are isolated.
 */
'use strict';
const fs=require('node:fs'),path=require('node:path'),net=require('node:net');
const {spawn}=require('node:child_process');
const {createHash}=require('node:crypto');
const {stopBrowserServer}=require('./server.cjs');

async function freePort(){
  const server=net.createServer();await new Promise((resolve,reject)=>{server.once('error',reject);server.listen(0,'127.0.0.1',resolve)});
  const port=server.address().port;await new Promise(resolve=>server.close(resolve));return port;
}
function tpmLauncher(artifacts){
  const children=new Set(),{projectRoot:root,output}=artifacts;
  let base,project;
  function launch(command,args,env,log){
    const child=spawn(command,args,{cwd:project||root,env,stdio:['ignore','pipe','pipe']});children.add(child);
    child.stdout.on('data',chunk=>fs.appendFileSync(path.join(output,log),chunk));
    child.stderr.on('data',chunk=>fs.appendFileSync(path.join(output,log),chunk));
    return child;
  }
  async function start(){
    project=path.join(output,'isolated project 中文');fs.mkdirSync(project);
    for(const name of fs.readdirSync(root))if(name.endsWith('.py')||name.endsWith('.command')||['quality_suite.json','quality_calibration.json'].includes(name))fs.copyFileSync(path.join(root,name),path.join(project,name));
    for(const folder of ['static','tools','docs'])fs.cpSync(path.join(root,folder),path.join(project,folder),{recursive:true});
    fs.symlinkSync(path.join(root,'.venv'),path.join(project,'.venv'),'dir');
    const sourceFiles=['启动正式版.command','route_test_ui.py','tpm_config.py','tpm_service.py','tpm_engine.py','static/tpm.js'];
    const sourceManifest=Object.fromEntries(sourceFiles.map(name=>{
      const source=fs.readFileSync(path.join(root,name)),copied=fs.readFileSync(path.join(project,name));
      if(!source.equals(copied))throw new Error('Isolated launcher changed application source: '+name);
      return [name,createHash('sha256').update(source).digest('hex')];
    }));
    fs.writeFileSync(path.join(output,'source-manifest.json'),JSON.stringify(sourceManifest,null,2));
    fs.copyFileSync(path.join(root,'tests/browser/tpm_credentials.py'),path.join(project,'credential_store.py'));
    const bin=path.join(output,'commands');fs.mkdirSync(bin);
    fs.writeFileSync(path.join(bin,'open'),'#!/bin/bash\nprintf \'%s\\n\' "$@" >> "$TPM_OPEN_TRACE"\n',{mode:0o755});
    const env={...artifacts.env,PATH:bin+path.delimiter+process.env.PATH,NO_PROXY:'127.0.0.1,localhost',no_proxy:'127.0.0.1,localhost',TPM_OPEN_TRACE:path.join(output,'launcher-open.txt')};
    const mock=launch(path.join(root,'.venv/bin/python'),['-u',path.join(root,'tests/browser/tpm_upstream.py')],env,'upstream.log');
    const mockReady=await new Promise((resolve,reject)=>{
      let buffer='';const timer=setTimeout(()=>reject(new Error('Synthetic upstream startup timed out')),10000);
      mock.once('error',error=>{clearTimeout(timer);reject(error)});
      mock.once('exit',code=>{clearTimeout(timer);reject(new Error('Synthetic upstream exited: '+code))});
      mock.stdout.on('data',chunk=>{buffer+=chunk;const newline=buffer.indexOf('\n');if(newline>=0){clearTimeout(timer);try{resolve(JSON.parse(buffer.slice(0,newline)))}catch(error){reject(error)}}});
    });
    const port=await freePort();base='http://127.0.0.1:'+port;
    const launcher=launch('/bin/bash',[path.join(project,'启动正式版.command')],{...env,ROUTE_TEST_HOST:'127.0.0.1',ROUTE_TEST_PORT:String(port)},'launcher.log');
    let ready=false;
    for(let attempt=0;attempt<150;attempt++){
      if(launcher.exitCode!==null)throw new Error('Actual launcher exited before ready; inspect launcher.log');
      try{const response=await fetch(base+'/health');if(response.ok&&(await response.json()).ok){ready=true;break}}catch{}
      await new Promise(resolve=>setTimeout(resolve,200));
    }
    if(!ready)throw new Error('Actual launcher never became healthy; inspect launcher.log');
    for(let attempt=0;attempt<20&&!fs.existsSync(env.TPM_OPEN_TRACE);attempt++)await new Promise(resolve=>setTimeout(resolve,100));
    if(!fs.existsSync(env.TPM_OPEN_TRACE))throw new Error('Launcher did not invoke browser opening after health');
    return {base,...mockReady,launcher:true,isolated_project:project,credential_boundary:'in-memory synthetic adapter; OS keychain untouched'};
  }
  async function close(){
    await Promise.all([...children].map(child=>stopBrowserServer(child,{timeout:15000})));
    if(base){let stopped=false;try{await fetch(base+'/health')}catch{stopped=true}
      fs.writeFileSync(path.join(output,'launcher-cleanup.json'),JSON.stringify({service_stopped:stopped,isolated_project:project},null,2));
      if(!stopped)throw new Error('Launcher left its backend running');
    }
  }
  return {start,close};
}
module.exports={tpmLauncher};
