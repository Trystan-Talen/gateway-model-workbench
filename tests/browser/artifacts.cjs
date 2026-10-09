'use strict';
const fs = require('node:fs');
const path = require('node:path');

function projectRoot(env, explicit) {
  return path.resolve(env.UI_PROJECT_ROOT || explicit || path.resolve(__dirname,'../..'));
}
function createBrowserRun({env = process.env, root, announce = true} = {}) {
  const resolvedRoot = projectRoot(env,root);
  // Overrides select the artifact base; every invocation still gets an isolated run directory.
  const base = env.UI_ARTIFACT_DIR ? path.resolve(env.UI_ARTIFACT_DIR) : path.join(resolvedRoot,'artifacts','browser');
  fs.mkdirSync(base,{recursive:true});
  const stamp = new Date().toISOString().replace(/[:.]/g,'-');
  const runRoot = fs.mkdtempSync(path.join(base,`${stamp}-${process.pid}-`));
  if (announce) console.log(`Browser artifacts root（浏览器回归输出根目录）: ${runRoot}`);
  return {projectRoot:resolvedRoot,runRoot,env:{...env,UI_PROJECT_ROOT:resolvedRoot,UI_BROWSER_RUN_ROOT:runRoot}};
}
function browserSuiteOutput(runRoot,suite) {
  if (!/^[a-z][a-z0-9_-]*$/i.test(suite)) throw new Error('Invalid browser suite name（浏览器测试名称无效）');
  return path.join(runRoot,suite);
}
function browserArtifacts(suite,{env = process.env, root, announce = true} = {}) {
  if (!/^[a-z][a-z0-9_-]*$/i.test(suite)) throw new Error('Invalid browser suite name（浏览器测试名称无效）');
  const run = env.UI_BROWSER_RUN_ROOT
    ? {projectRoot:projectRoot(env,root),runRoot:path.resolve(env.UI_BROWSER_RUN_ROOT)}
    : createBrowserRun({env,root,announce});
  const output = browserSuiteOutput(run.runRoot,suite), data = path.join(output,'data');
  fs.mkdirSync(run.runRoot,{recursive:true});
  // Refuse suite-directory reuse so concurrent or repeated children cannot overwrite prior evidence.
  fs.mkdirSync(output);
  fs.mkdirSync(data);
  if (announce) console.log(`Browser artifacts（浏览器回归输出）: ${output}`);
  return {...run,output,data,env:{...env,UI_PROJECT_ROOT:run.projectRoot,UI_BROWSER_RUN_ROOT:run.runRoot,
    ROUTE_TEST_DATA_DIR:data,ROUTE_TEST_DISABLE_CREDENTIAL_STORE:'1',PYTHONDONTWRITEBYTECODE:'1'}};
}
module.exports = {createBrowserRun,browserArtifacts,browserSuiteOutput};
