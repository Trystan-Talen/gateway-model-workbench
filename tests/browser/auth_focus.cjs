/** Login-focus regression using an isolated backend; never calls a real upstream. */
const { chromium } = require('playwright');
const assert = require('node:assert/strict');
const fs = require('fs');
const os = require('os');
const path = require('path');
const { spawn } = require('child_process');

const output = fs.mkdtempSync(path.join(os.tmpdir(), 'route-auth-focus-'));
const username = 'focus-regression';
const password = 'local-focus-888@';
let server, browser;

async function start() {
  server = spawn('python3', ['-u', path.join(__dirname, 'workflow_server.py'), output]);
  server.stderr.on('data', data => fs.appendFileSync(path.join(output, 'server.log'), data));
  return new Promise((resolve, reject) => {
    let text = '';
    server.stdout.on('data', data => {
      text += data;
      if (text.includes('\n')) {
        try { resolve(JSON.parse(text.split('\n')[0])); } catch (error) { reject(error); }
      }
    });
    server.on('error', reject);
    server.on('exit', code => reject(new Error('server exited: ' + code)));
  });
}

(async () => {
  const { base } = await start();
  browser = await chromium.launch({
    headless: true,
    executablePath: process.env.BROWSER_PATH || '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome',
  });
  const context = await browser.newContext();
  const page = await context.newPage();
  const errors = [];
  let taskRequests = 0;
  page.on('pageerror', error => errors.push(error.message));
  page.on('request', request => { if (new URL(request.url()).pathname === '/tasks') taskRequests++; });
  await context.route('**/*', route => new URL(route.request().url()).origin === base ? route.continue() : route.abort());
  const setup = await context.request.post(base + '/auth/setup', { data: { username, password } });
  assert.ok(setup.ok());
  await page.goto(base);
  await page.waitForFunction(() => authenticated && refreshTimer !== null && !refreshing);

  // Expiry while a nested dialog is open must also release the dialog focus trap.
  await page.evaluate(async () => {
    currentQualitySuite = await api('/quality-suite');
    showQualitySuite();
    await showQualityMethodology();
  });
  assert.equal(await page.locator('#qualityMethodologyModal').isVisible(), true);

  // Invalidate the server session without changing the page: reproduces a server restart/expired login.
  assert.ok((await context.request.post(base + '/auth/logout')).ok());
  await page.locator('#authGate').waitFor({ state: 'visible' });
  assert.equal(await page.locator('#qualityMethodologyModal').isVisible(), false);
  assert.equal(await page.locator('#qualitySuiteModal').isVisible(), false);
  assert.equal(await page.evaluate(() => modalStack.length), 0);
  await page.locator('#authUsername').fill(username);
  await page.locator('#authPassword').click();
  const requestsAtLogin = taskRequests;
  // Real, deliberately slow keyboard input crosses several 1.5s polling intervals.
  await page.keyboard.type(password, { delay: 260 });
  assert.equal(await page.evaluate(() => document.activeElement.id), 'authPassword', 'password focus was stolen');
  assert.equal(await page.locator('#authPassword').inputValue(), password, 'password characters went to another field');
  assert.equal(await page.locator('#authUsername').inputValue(), username, 'account was modified while typing password');
  assert.equal(taskRequests, requestsAtLogin, 'protected polling continued while logged out');
  assert.equal(await page.evaluate(() => refreshTimer), null);

  // Rendering an already-visible gate must never move an explicit password focus.
  await page.evaluate(() => { authGate(true, false); authGate(true, false); });
  await page.waitForTimeout(100);
  assert.equal(await page.evaluate(() => document.activeElement.id), 'authPassword');
  const eye = page.locator('button[onclick*="authPassword"]');
  await eye.click();
  assert.equal(await page.locator('#authPassword').getAttribute('type'), 'text');
  await eye.click();
  assert.equal(await page.locator('#authPassword').getAttribute('type'), 'password');
  await page.locator('#authUsername').click();
  await page.keyboard.press('Tab');
  assert.equal(await page.evaluate(() => document.activeElement.id), 'authPassword');

  await page.locator('#authPassword').fill('wrong-password');
  await page.locator('#authSubmit').click();
  await page.waitForFunction(() => document.getElementById('authError').textContent.length > 0);
  await page.locator('#authPassword').click();
  await page.waitForTimeout(1800);
  assert.equal(await page.evaluate(() => document.activeElement.id), 'authPassword');
  await page.locator('#authPassword').fill(password);
  await page.locator('#authSubmit').click();
  await page.locator('#authGate').waitFor({ state: 'hidden' });
  await page.waitForFunction(() => authenticated && refreshTimer !== null && !refreshing);
  assert.ok(taskRequests > requestsAtLogin, 'polling did not resume after login');

  // An old in-flight 401 must not invalidate a newly successful login.
  let heldRoute;
  await page.route('**/tasks', async route => {
    if (!heldRoute) { heldRoute = route; return; }
    await route.continue();
  });
  await page.evaluate(() => { clearInterval(refreshTimer); refreshTimer = null; void refresh(); });
  for (let n = 0; !heldRoute && n < 50; n++) await page.waitForTimeout(20);
  assert.ok(heldRoute, 'missing in-flight refresh');
  await page.evaluate(() => logout());
  await page.locator('#authGate').waitFor({ state: 'visible' });
  await page.locator('#authPassword').fill(password);
  await page.locator('#authSubmit').click();
  await page.waitForFunction(() => authenticated && refreshTimer !== null && !refreshing);
  await heldRoute.fulfill({ status: 401, contentType: 'application/json', body: JSON.stringify({ error: '请先登录' }) });
  await page.waitForTimeout(150);
  assert.equal(await page.locator('#authGate').isVisible(), false, 'old unauthorized response replaced new login');
  assert.equal(await page.evaluate(() => authenticated), true);
  await page.unroute('**/tasks');

  // If startup itself encounters an expired session, it must not restart its interval afterward.
  assert.ok((await context.request.post(base + '/auth/logout')).ok());
  await page.evaluate(() => bootApp());
  await page.locator('#authGate').waitFor({ state: 'visible' });
  const requestsAfterFailedBoot = taskRequests;
  await page.waitForTimeout(1800);
  assert.equal(await page.evaluate(() => refreshTimer), null);
  assert.equal(taskRequests, requestsAfterFailedBoot);
  await page.evaluate(() => authGate(false));

  // A queued initial autofocus cannot override a click made before its callback runs.
  await page.evaluate(() => {
    authGate(true, false);
    document.getElementById('authPassword').focus();
  });
  await page.waitForTimeout(100);
  assert.equal(await page.evaluate(() => document.activeElement.id), 'authPassword');
  await page.evaluate(() => { authGate(false); authGate(true, false); authGate(false); });
  await page.waitForTimeout(100);
  assert.equal(await page.locator('#authGate').isVisible(), false);
  assert.notEqual(await page.evaluate(() => document.activeElement.id), 'authUsername');
  assert.deepEqual(errors, []);
  console.log(JSON.stringify({ passed: true, expiredSession: true, slowTyping: true, repeatedGate: true,
    passwordVisibility: true, keyboardNavigation: true, loginRetry: true, resumedPolling: true,
    deferredFocus: true, staleResponseIgnored: true, failedBootStopsPolling: true, pageErrors: errors, output }, null, 2));
})().catch(error => { console.error(error); process.exitCode = 1; })
  .finally(async () => { if (browser) await browser.close(); if (server) server.kill('SIGTERM'); });
