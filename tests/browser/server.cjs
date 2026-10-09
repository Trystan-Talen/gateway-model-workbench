'use strict';
const fs = require('node:fs');
const path = require('node:path');
const {spawn} = require('node:child_process');

function startBrowserServer(artifacts,{python = artifacts.env.PYTHON || 'python3',timeout = 15000,spawnProcess = spawn} = {}) {
  const child = spawnProcess(python,['-u',path.join(artifacts.projectRoot,'tests/browser/workflow_server.py'),artifacts.data],
    {cwd:artifacts.projectRoot,env:artifacts.env});
  child.stderr.on('data',chunk=>fs.appendFileSync(path.join(artifacts.output,'server.log'),chunk));
  const ready = new Promise((resolve,reject)=>{
    let text = '',settled = false;
    const finish = (error,value) => {
      if (settled) return;
      settled = true; clearTimeout(timer);
      child.stdout.removeListener('data',onData); child.removeListener('error',onError); child.removeListener('exit',onExit);
      error ? reject(error) : resolve({...value,child});
    };
    const onData = chunk => {
      text += chunk;
      const newline = text.indexOf('\n');
      if (newline < 0) return;
      try { finish(null,JSON.parse(text.slice(0,newline))); }
      catch { finish(new Error('Invalid browser server startup response（浏览器测试服务启动响应无效）')); }
    };
    const onError = error => finish(error);
    const onExit = code => finish(new Error(`Browser server exited before startup（浏览器测试服务启动前退出）: ${code}`));
    const timer = setTimeout(()=>finish(new Error('Browser server startup timed out（浏览器测试服务启动超时）')),timeout);
    child.stdout.on('data',onData); child.once('error',onError); child.once('exit',onExit);
  });
  return ready.catch(async error=>{await stopBrowserServer(child);throw error;});
}
function stopBrowserServer(child,{signal = 'SIGTERM',timeout = 5000} = {}) {
  if (!child || child.exitCode !== null || child.signalCode !== null) return Promise.resolve();
  return new Promise((resolve,reject)=>{
    let timer;
    const done = () => {clearTimeout(timer);child.removeListener('exit',done);child.removeListener('error',failed);resolve();};
    const failed = error => {clearTimeout(timer);child.removeListener('exit',done);reject(error);};
    child.once('exit',done); child.once('error',failed);
    timer = setTimeout(()=>{if (!child.kill('SIGKILL')) done();},timeout);
    if (!child.kill(signal)) done();
  });
}
function browserServers(artifacts,options) {
  const children = new Set();
  const start = async () => {const server = await startBrowserServer(artifacts,options);children.add(server.child);return server;};
  const stop = async child => {await stopBrowserServer(child);children.delete(child);};
  const close = async () => {await Promise.all([...children].map(stop));};
  return {start,stop,close};
}
module.exports = {startBrowserServer,stopBrowserServer,browserServers};
