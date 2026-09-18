'use strict';
/* Proves tools/check-ui.cjs rejects design-system drift. Each case mutates a temp copy of the front end. */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { spawnSync } = require('node:child_process');

const root = path.resolve(__dirname, '..');
function workspace() {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'ui-policy-'));
  for (const rel of ['static', 'docs/design-system']) fs.cpSync(path.join(root, rel), path.join(dir, rel), { recursive: true });
  fs.copyFileSync(path.join(root, 'docs/design-system.md'), path.join(dir, 'docs/design-system.md'));
  return dir;
}
function run(dir) {
  const result = spawnSync(process.execPath, [path.join(root, 'tools/check-ui.cjs')], { env: { ...process.env, UI_CHECK_ROOT: dir }, encoding: 'utf8' });
  return { ok: result.status === 0, output: result.stdout + result.stderr };
}
const append = (dir, file, text) => fs.appendFileSync(path.join(dir, file), text);
const replace = (dir, file, from, to) => { const p = path.join(dir, file); const s = fs.readFileSync(p, 'utf8'); assert.ok(s.includes(from), `${file} fixture missing ${from}`); fs.writeFileSync(p, s.replace(from, to)); };

const cases = [
  ['baseline passes', () => {}, null],
  ['hex colour', d => append(d, 'static/app.css', '\n.x-new{color:#ff0000}'), /literal hex colour/],
  ['rgb colour', d => append(d, 'static/app.css', '\n.x-new{background:rgba(0,0,0,.4)}'), /rgb\(\)\/hsl\(\)/],
  ['named colour', d => append(d, 'static/fingerprint.css', '\n.x-new{color:white}'), /named colour/],
  ['raw font size', d => append(d, 'static/app.css', '\n.x-new{font-size:17px}'), /must use a --text-\* token/],
  ['raw radius', d => append(d, 'static/app.css', '\n.x-new{border-radius:7px}'), /--radius-\* tokens/],
  ['raw shadow', d => append(d, 'static/app.css', '\n.x-new{box-shadow:0 8px 20px var(--color-overlay)}'), /--shadow-\* tokens/],
  ['raw z-index', d => append(d, 'static/app.css', '\n.x-new{z-index:999}'), /layer token/],
  ['raw font family', d => append(d, 'static/app.css', '\n.x-new{font-family:Arial}'), /var\(--font-/],
  ['token defined in component css', d => append(d, 'static/app.css', '\n:root{--color-brand:var(--color-accent)}'), /tokens belong in/],
  ['unapproved media breakpoint', d => append(d, 'static/app.css', '\n@media (max-width:768px){.task{padding:0}}'), /unapproved viewport breakpoint 768/],
  ['unapproved container breakpoint', d => append(d, 'static/app.css', '\n@container detail (max-width:700px){.task{padding:0}}'), /unapproved container breakpoint 700/],
  ['undefined token reference', d => append(d, 'static/app.css', '\n.task{color:var(--color-brandd)}'), /undefined token --color-brandd/],
  ['colour without dark value', d => replace(d, 'static/tokens.css', '  --color-overlay: #0d0d10;', '  --color-overlay: #0d0d10;\n  --color-new-thing: #123456;'), /--color-new-thing has no dark-theme value/],
  ['undocumented token', d => { replace(d, 'static/tokens.css', '--space-12: 48px;', '--space-12: 48px; --space-16: 64px;'); }, /token --space-16 is not documented/],
  ['inline presentational style', d => replace(d, 'static/app.js', 'class="metric-value metric-value-text"', 'class="metric-value" style="font-size:14px"'), /inline style "font-size:14px"/],
  ['unstyled class', d => replace(d, 'static/app.js', 'class="task-empty"', 'class="task-empty my-special-card"'), /class "my-special-card" has no CSS rule/],
  ['inline event handler', d => append(d, 'static/index.html', '<button onclick="x()">x</button>'), /inline event handler/],
  ['unregistered file', d => fs.writeFileSync(path.join(d, 'static/extra.css'), '.task{}'), /unregistered front-end file/],
  ['kit builder without gallery example', d => replace(d, 'static/ui-kit.js', 'formSectionTitle, field,', 'formSectionTitle, field, extraThing,'), /UI\.extraThing has no gallery example/],
];

for (const [name, mutate, expected] of cases) {
  const dir = workspace();
  try {
    mutate(dir);
    const { ok, output } = run(dir);
    if (!expected) assert.ok(ok, `${name}: ${output}`);
    else { assert.equal(ok, false, `${name}: expected failure`); assert.match(output, expected, `${name}: ${output}`); }
  } finally { fs.rmSync(dir, { recursive: true, force: true }); }
}
console.log(JSON.stringify({ status: 'passed', policyCases: cases.length }));
