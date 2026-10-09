'use strict';
/* Artifact isolation and process lifecycle checks; no browser, user data or upstream. */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const {EventEmitter} = require('node:events');
const {execFile} = require('node:child_process');
const {createRequire} = require('node:module');
const vm = require('node:vm');
const {createBrowserRun,browserArtifacts} = require('./browser/artifacts.cjs');
const {startBrowserServer,stopBrowserServer,browserServers} = require('./browser/server.cjs');
const {serveAsset} = require('./browser/assets.cjs');
const folder = fs.mkdtempSync(path.join(os.tmpdir(),'browser-artifacts-regression-'));
const project = path.join(folder,'project');
let checks = 0;
async function check(name,run) { await run(); checks++; }
function artifacts(suite,env = {}) { return browserArtifacts(suite,{env,root:project,announce:false}); }
class Child extends EventEmitter {
  constructor({exitOnSignal = true} = {}) { super();this.stdout=new EventEmitter();this.stderr=new EventEmitter();this.exitCode=null;this.signalCode=null;this.signals=[];this.exitOnSignal=exitOnSignal; }
  kill(signal) {
    this.signals.push(signal);
    if (this.exitOnSignal || signal === 'SIGKILL') queueMicrotask(()=>this.exit(null,signal));
    return true;
  }
  exit(code = 0,signal = null) { this.exitCode=code;this.signalCode=signal;this.emit('exit',code,signal);this.emit('close',code,signal); }
}
(async()=>{
  await check('standalone suites create unique run directories and retain their own data',()=>{
    const first = artifacts('smoke'), second = artifacts('smoke');
    assert.equal(path.dirname(first.runRoot),path.join(project,'artifacts','browser'));
    assert.notEqual(first.runRoot,second.runRoot);
    assert.equal(first.output,path.join(first.runRoot,'smoke')); assert.equal(first.data,path.join(first.output,'data'));
    fs.writeFileSync(path.join(first.output,'evidence.txt'),'first run');
    fs.writeFileSync(path.join(second.output,'evidence.txt'),'second run');
    assert.equal(fs.readFileSync(path.join(first.output,'evidence.txt'),'utf8'),'first run');
  });
  await check('artifact and project overrides stay independent and inherited environment is preserved',()=>{
    const source = {UI_ARTIFACT_DIR:path.join(folder,'override'),UI_PROJECT_ROOT:path.join(folder,'staging'),PYTHON:'synthetic-python',EXTRA_FLAG:'preserved',ROUTE_TEST_DATA_DIR:'never-read-real-data',ROUTE_TEST_DISABLE_CREDENTIAL_STORE:'0',PYTHONDONTWRITEBYTECODE:'0'};
    const result = artifacts('layout',source);
    assert.equal(result.projectRoot,path.resolve(source.UI_PROJECT_ROOT));
    assert.equal(path.dirname(result.runRoot),source.UI_ARTIFACT_DIR);
    assert.equal(result.env.EXTRA_FLAG,'preserved'); assert.equal(result.env.PYTHON,source.PYTHON);
    assert.equal(result.env.ROUTE_TEST_DATA_DIR,result.data); assert.equal(result.env.ROUTE_TEST_DISABLE_CREDENTIAL_STORE,'1'); assert.equal(result.env.PYTHONDONTWRITEBYTECODE,'1');
    assert.equal(source.ROUTE_TEST_DATA_DIR,'never-read-real-data');
  });
  await check('a full run shares one root while keeping suite files and logs separate',()=>{
    const run = createBrowserRun({env:{},root:project,announce:false});
    const smoke = artifacts('smoke',run.env), workflow = artifacts('workflow',run.env);
    assert.equal(smoke.runRoot,run.runRoot); assert.equal(workflow.runRoot,run.runRoot);
    fs.writeFileSync(path.join(smoke.output,'server.log'),'smoke');fs.writeFileSync(path.join(workflow.output,'server.log'),'workflow');
    assert.equal(fs.readFileSync(path.join(smoke.output,'server.log'),'utf8'),'smoke');
    assert.throws(()=>artifacts('smoke',run.env),{code:'EEXIST'});
    assert.throws(()=>artifacts('../escaped',run.env),/Invalid browser suite name/);
  });
  await check('parallel standalone invocations under one override never overwrite evidence',async()=>{
    const helper = path.resolve(__dirname,'browser/artifacts.cjs'),base = path.join(folder,'parallel');
    const code = 'const fs=require("node:fs"),path=require("node:path");const a=require(process.argv[1]).browserArtifacts("same_suite",{announce:false});fs.writeFileSync(path.join(a.output,"marker"),String(process.pid));console.log(JSON.stringify({root:a.runRoot,output:a.output,pid:process.pid}));';
    const outputs = await Promise.all(Array.from({length:4},()=>new Promise((resolve,reject)=>{
      execFile(process.execPath,['-e',code,helper],{env:{...process.env,UI_ARTIFACT_DIR:base,UI_BROWSER_RUN_ROOT:''}},(error,stdout)=>error?reject(error):resolve(JSON.parse(stdout)));
    })));
    assert.equal(new Set(outputs.map(item=>item.root)).size,4);
    for (const item of outputs) assert.equal(fs.readFileSync(path.join(item.output,'marker'),'utf8'),String(item.pid));
  });
  await check('every browser suite delegates output creation to the shared helper',()=>{
    const scripts = fs.readdirSync(path.join(__dirname,'browser')).filter(name=>name.endsWith('.cjs')&&!['artifacts.cjs','assets.cjs','server.cjs','run-all.cjs'].includes(name));
    assert.ok(scripts.length > 0);
    for (const name of scripts) {
      const source = fs.readFileSync(path.join(__dirname,'browser',name),'utf8');
      assert.ok(source.includes(`browserArtifacts('${path.basename(name,'.cjs')}')`),name);
      assert.ok(!/mkdtempSync|tmpdir\(|artifacts\/review-smoke/.test(source),name);
    }
    const runner = fs.readFileSync(path.join(__dirname,'browser/run-all.cjs'),'utf8');
    assert.match(runner,/createBrowserRun\(\)/);assert.match(runner,/env:run\.env/);
    const listed = [...runner.match(/const scripts=\[([^\n]+)\];/)[1].matchAll(/'([^']+)'/g)].map(match=>match[1]+'.cjs');
    assert.deepEqual(listed.sort(),scripts.sort());
  });
  await check('full runner preserves live output and failure evidence and stops at the first failing suite',async()=>{
    const runnerPath=path.resolve(__dirname,'browser/run-all.cjs'),helper=path.resolve(__dirname,'browser/artifacts.cjs');
    const scriptsRoot=path.join(folder,'synthetic-suites'),artifactBase=path.join(folder,'synthetic-runner');
    fs.mkdirSync(scriptsRoot);
    for (const [suite,exitCode] of [['passed_suite',0],['failed_suite',7],['unreached_suite',0]]) {
      fs.writeFileSync(path.join(scriptsRoot,suite+'.cjs'),`const a=require(${JSON.stringify(helper)}).browserArtifacts(${JSON.stringify(suite)});console.log('synthetic stdout');console.error('synthetic stderr');process.exitCode=${exitCode};`);
    }
    const source=fs.readFileSync(runnerPath,'utf8').replace('const run=createBrowserRun();','const run=createBrowserRun({env:caseEnv,announce:false});').replace(/const scripts=\[[^\n]+\];/,"const scripts=['passed_suite','failed_suite','unreached_suite'];");
    let stdout='',stderr='';
    const childProcess={execPath:process.execPath,stdout:{write:chunk=>{stdout+=chunk;}},stderr:{write:chunk=>{stderr+=chunk;}}};
    await vm.runInNewContext(source,{require:createRequire(runnerPath),__dirname:scriptsRoot,caseEnv:{UI_ARTIFACT_DIR:artifactBase,UI_PROJECT_ROOT:project},process:childProcess,console:{log:message=>{stdout+=message+'\n';},error:message=>{stderr+=message+'\n';}}});
    const runRoot=path.join(artifactBase,fs.readdirSync(artifactBase)[0]);
    assert.equal(childProcess.exitCode,7);assert.match(stdout,/synthetic stdout/);assert.match(stderr,/synthetic stderr/);
    assert.equal(fs.existsSync(path.join(runRoot,'unreached_suite')),false);
    const result=JSON.parse(fs.readFileSync(path.join(runRoot,'run-result.json'),'utf8'));
    assert.deepEqual(result.suites.map(suite=>suite.exitCode),[0,7]);
    assert.match(fs.readFileSync(path.join(runRoot,'failed_suite','stdout.log'),'utf8'),/synthetic stdout/);
    assert.match(fs.readFileSync(path.join(runRoot,'failed_suite','stderr.log'),'utf8'),/synthetic stderr/);
    assert.equal(JSON.parse(fs.readFileSync(path.join(runRoot,'failed_suite','process-result.json'),'utf8')).exitCode,7);
  });
  await check('staging assets honor both the project root and fingerprint-only overrides',async()=>{
    const staticRoot = path.join(project,'static'), fingerprintRoot = path.join(folder,'fingerprint-override');
    fs.mkdirSync(staticRoot,{recursive:true});fs.mkdirSync(fingerprintRoot,{recursive:true});
    fs.writeFileSync(path.join(staticRoot,'app.js'),'staging app');fs.writeFileSync(path.join(staticRoot,'fingerprint.js'),'project fingerprint');fs.writeFileSync(path.join(fingerprintRoot,'fingerprint.js'),'override fingerprint');
    let fulfilled;
    const route = pathname=>({request:()=>({url:()=>`http://synthetic.test${pathname}`}),fulfill:async value=>{fulfilled=value;}});
    assert.equal(await serveAsset(route('/static/app.js'),project,{fingerprintRoot}),true);assert.equal(fulfilled.body.toString(),'staging app');
    assert.equal(await serveAsset(route('/assets/fingerprint.js'),project,{fingerprintRoot}),true);assert.equal(fulfilled.body.toString(),'override fingerprint');
    assert.equal(await serveAsset(route('/assets/fingerprint.js'),project),true);assert.equal(fulfilled.body.toString(),'project fingerprint');
    assert.equal(await serveAsset(route('/not-an-asset'),project),false);
  });
  await check('server startup uses isolated data, selected project and buffered readiness',async()=>{
    const result = artifacts('server_env',{PYTHON:'selected-python',UI_PROJECT_ROOT:path.join(folder,'selected-project')});
    const child = new Child();let launched;
    const ready = startBrowserServer(result,{spawnProcess:(python,args,options)=>{launched={python,args,options};return child;}});
    child.stdout.emit('data',Buffer.from('{"base":"http://127.0.0.1:1"'));
    child.stderr.emit('data',Buffer.from('synthetic diagnostic\n'));
    child.stdout.emit('data',Buffer.from(',"upstream":"http://127.0.0.1:2"}\n'));
    const server = await ready;
    assert.equal(server.child,child);assert.equal(server.base,'http://127.0.0.1:1');
    assert.equal(launched.python,'selected-python');assert.deepEqual(launched.args,['-u',path.join(result.projectRoot,'tests/browser/workflow_server.py'),result.data]);
    assert.equal(launched.options.cwd,result.projectRoot);assert.equal(launched.options.env.ROUTE_TEST_DATA_DIR,result.data);
    assert.equal(fs.readFileSync(path.join(result.output,'server.log'),'utf8'),'synthetic diagnostic\n');
    assert.equal(child.stdout.listenerCount('data'),0);assert.equal(child.listenerCount('error'),0);
    await stopBrowserServer(child);assert.equal(child.signalCode,'SIGTERM');
  });
  await check('shutdown waits for process exit before data can be reused',async()=>{
    const child = new Child({exitOnSignal:false});let stopped = false;
    const stopping = stopBrowserServer(child).then(()=>{stopped=true;});
    await new Promise(setImmediate);assert.equal(stopped,false);assert.deepEqual(child.signals,['SIGTERM']);
    child.exit(null,'SIGTERM');await stopping;assert.equal(stopped,true);
    await stopBrowserServer(child);assert.deepEqual(child.signals,['SIGTERM']);
  });
  await check('stuck shutdown escalates only the owned process and removes lifecycle listeners',async()=>{
    const child = new Child({exitOnSignal:false});await stopBrowserServer(child,{timeout:5});
    assert.deepEqual(child.signals,['SIGTERM','SIGKILL']);assert.equal(child.listenerCount('exit'),0);assert.equal(child.listenerCount('error'),0);
  });
  await check('startup timeout and malformed readiness both stop their isolated process',async()=>{
    const timeoutChild = new Child(),badChild = new Child();
    await assert.rejects(startBrowserServer(artifacts('server_timeout'),{timeout:5,spawnProcess:()=>timeoutChild}),/startup timed out/);
    assert.deepEqual(timeoutChild.signals,['SIGTERM']);assert.equal(timeoutChild.stdout.listenerCount('data'),0);
    const malformed = startBrowserServer(artifacts('server_malformed'),{spawnProcess:()=>badChild});
    badChild.stdout.emit('data',Buffer.from('invalid readiness\n'));
    await assert.rejects(malformed,/startup response/);assert.deepEqual(badChild.signals,['SIGTERM']);
  });
  await check('startup process errors and early exits reject promptly without leaving an active child',async()=>{
    const exited = new Child(),failed = new Child();
    const earlyExit = startBrowserServer(artifacts('server_early_exit'),{spawnProcess:()=>exited});
    exited.exit(7);await assert.rejects(earlyExit,/startup.*7/);assert.deepEqual(exited.signals,[]);
    const startupError = startBrowserServer(artifacts('server_spawn_error'),{spawnProcess:()=>failed});
    failed.emit('error',new Error('synthetic spawn failure'));
    await assert.rejects(startupError,/synthetic spawn failure/);assert.deepEqual(failed.signals,['SIGTERM']);
  });
  await check('server manager waits for restarts and closes every active server',async()=>{
    const children=[];
    const servers = browserServers(artifacts('managed_servers'),{spawnProcess:()=>{
      const child=new Child();children.push(child);queueMicrotask(()=>child.stdout.emit('data',Buffer.from('{"base":"http://127.0.0.1:1"}\n')));return child;
    }});
    const first = await servers.start();await servers.stop(first.child);
    assert.equal(first.child.signalCode,'SIGTERM');await servers.start();await servers.start();await servers.close();
    assert.deepEqual(children.map(child=>child.signals),[['SIGTERM'],['SIGTERM'],['SIGTERM']]);
    await servers.close();assert.equal(children[0].signals.length,1);
  });
  console.log(JSON.stringify({status:'passed',checks}));
})().catch(error=>{console.error(error);process.exitCode=1;}).finally(()=>{fs.rmSync(folder,{recursive:true,force:true});});
