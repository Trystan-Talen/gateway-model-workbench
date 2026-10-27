'use strict';
/* Front-end policy gate (run by `npm run check`, `make test` and CI).
 * Encodes the design system in docs/design-system.md so every module stays consistent.
 * When a rule blocks you, fix the component to use the system — do not widen the allowlists
 * unless the design-system spec is updated in the same change. */
const fs = require('node:fs');
const path = require('node:path');

const ROOT = process.env.UI_CHECK_ROOT ? path.resolve(process.env.UI_CHECK_ROOT) : path.resolve(__dirname, '..');
const read = file => fs.readFileSync(path.join(ROOT, file), 'utf8');
const failures = [];
const fail = (file, message) => failures.push(`${file}: ${message}`);
const stripComments = css => css.replace(/\/\*[\s\S]*?\*\//g, '');

/* ---- Registries: adding a stylesheet or script is a deliberate, reviewed decision. ---- */
const TOKENS = 'static/tokens.css';
const COMPONENT_CSS = ['static/app.css', 'static/fingerprint.css'];
const SCRIPTS = ['static/ui-kit.js', 'static/app.js', 'static/actions.js', 'static/fingerprint.js', 'static/connections.js', 'static/repeat.js', 'static/tpm.js'];
const MARKUP = ['static/index.html', ...SCRIPTS];
const DOC = 'docs/design-system.md';
const GALLERY = 'docs/design-system/gallery.html';

const onDisk = fs.readdirSync(path.join(ROOT, 'static')).map(name => `static/${name}`);
for (const file of onDisk) {
  if (!['static/index.html', TOKENS, ...COMPONENT_CSS, ...SCRIPTS].includes(file)) fail(file, 'unregistered front-end file; register it in tools/check-ui.cjs, route_test_ui.py assets and tests/browser/assets.cjs');
}

/* ---- 1. Security: explicit actions only ---- */
for (const file of MARKUP) {
  const source = read(file);
  if (/\bon(?:click|change|input|submit|keydown)\s*=\s*["']/.test(source)) fail(file, 'inline event handler; use data-click-action + static/actions.js');
  if (/\beval\s*\(|new\s+Function\s*\(/.test(source)) fail(file, 'executable handler strings');
}

/* ---- 2. Tokens: single source of truth ---- */
const tokenSource = stripComments(read(TOKENS));
const defined = new Set([...tokenSource.matchAll(/(--[\w-]+)\s*:/g)].map(m => m[1]));
const lightBlock = tokenSource.match(/:root\s*\{([\s\S]*?)\}/)?.[1] || '';
const darkBlock = tokenSource.match(/:root\[data-theme="dark"\]\s*\{([\s\S]*?)\}/)?.[1] || '';
const colorNames = block => new Set([...block.matchAll(/(--color-[\w-]+)\s*:/g)].map(m => m[1]));
const light = colorNames(lightBlock), dark = colorNames(darkBlock);
for (const name of light) if (!dark.has(name)) fail(TOKENS, `${name} has no dark-theme value`);
for (const name of dark) if (!light.has(name)) fail(TOKENS, `${name} has no light-theme value`);

const doc = fs.existsSync(path.join(ROOT, DOC)) ? read(DOC) : '';
if (!doc) fail(DOC, 'design-system spec is missing');
const DOCUMENTED_PREFIXES = /^--(color|text|radius|shadow|z|space|font|halo|ease|duration)-?/;
const LEGACY_ALIASES = new Set(['--card-shadow', '--float-shadow', '--control-shadow', '--ring', '--line', '--line-strong', '--text', '--sub', '--muted', '--blue', '--green', '--red', '--amber', '--canvas']);
for (const name of defined) {
  if (LEGACY_ALIASES.has(name) || !DOCUMENTED_PREFIXES.test(name)) continue;
  if (!doc.includes(`\`${name}\``)) fail(DOC, `token ${name} is not documented`);
}

/* ---- 3. Component CSS consumes tokens by role ---- */
const NAMED_COLOR = /(?:^|[\s:,(])(white|black|red|green|blue|gray|grey|silver|orange|yellow|purple|pink|navy|teal|crimson|slateblue|lightsteelblue|transparentize)(?=[\s;,)!]|$)/i;
const COLOR_PROPS = /^(color|background|background-color|background-image|border|border-(?:top|right|bottom|left|color)|outline|outline-color|fill|stroke|box-shadow|text-shadow|caret-color|accent-color|text-decoration-color|column-rule)$/;
const LAYOUT_OVERRIDES = new Set(['--records-w', '--top-h', '--rail-w']);
const TOKEN_PREFIX = /^--(color|text|radius|shadow|z|space|font)-/;
const MEDIA_BREAKPOINTS = new Set(['640', '980', '1100']);
const CONTAINER_BREAKPOINTS = new Set(['560', '820']);
const allCss = [TOKENS, ...COMPONENT_CSS].map(read).join('\n');
const localProps = new Set([...stripComments(allCss).matchAll(/(--[\w-]+)\s*:/g)].map(m => m[1]));

for (const file of COMPONENT_CSS) {
  const css = stripComments(read(file));
  const lineOf = index => css.slice(0, index).split('\n').length;
  if (/#[0-9a-f]{3,8}\b/i.test(css.replace(/url\([^)]*\)/g, ''))) fail(file, 'literal hex colour; use a --color-* token');
  if (/\b(?:rgba?|hsla?)\(/i.test(css)) fail(file, 'literal rgb()/hsl() colour; use a --color-* token with color-mix()');
  for (const [, value] of css.matchAll(/@media[^{]*?max-width:\s*(\d+)px/g)) if (!MEDIA_BREAKPOINTS.has(value)) fail(file, `unapproved viewport breakpoint ${value}px (allowed: 1100, 980, 640)`);
  for (const [, value] of css.matchAll(/@media[^{]*?min-width:\s*(\d+)px/g)) fail(file, `min-width media query ${value}px; the system is desktop-first with max-width breakpoints`);
  for (const [, value] of css.matchAll(/@container[^{]*?max-width:\s*(\d+)px/g)) if (!CONTAINER_BREAKPOINTS.has(value)) fail(file, `unapproved container breakpoint ${value}px (allowed: 820, 560)`);

  for (const match of css.matchAll(/([\w-]+)\s*:\s*((?:[^;{}()]|\((?:[^()]|\([^()]*\))*\))+)(?=[;}])/g)) {
    const [, prop, rawValue] = match;
    const value = rawValue.replace(/!important$/, '').trim();
    const at = `line ${lineOf(match.index)}`;
    if (prop.startsWith('--')) {
      if (TOKEN_PREFIX.test(prop)) fail(file, `${at}: defines token ${prop}; tokens belong in ${TOKENS}`);
      else if (!LAYOUT_OVERRIDES.has(prop) && /\d(px|rem|em|%)/.test(value) && !/var\(/.test(value)) fail(file, `${at}: custom property ${prop} uses a raw length; reference tokens`);
      continue;
    }
    if (prop === 'font-size' && !/^(var\(--text-[\w-]+\)|inherit|0)$/.test(value)) fail(file, `${at}: font-size "${value}" must use a --text-* token`);
    if (prop === 'font-family' && !/^(var\(--font-(sans|mono)\)|inherit)$/.test(value)) fail(file, `${at}: font-family must be var(--font-sans|mono)`);
    if (prop === 'z-index' && !/^var\(--z-[\w-]+\)$/.test(value)) fail(file, `${at}: z-index "${value}" must use a --z-* layer token`);
    if (/^border(-(top|bottom)-(left|right))?-radius$/.test(prop)) {
      for (const part of value.split(/\s+(?![^(]*\))/)) if (!/^(0|50%|inherit|var\(--radius-[\w-]+\)|calc\(var\(--radius-[\w-]+\)\s*[+-]\s*\d+px\))$/.test(part)) fail(file, `${at}: border-radius "${value}" must use --radius-* tokens`);
    }
    if (prop === 'box-shadow' && value !== 'none') {
      const withoutVars = value.replace(/var\(--[\w-]+\)/g, 'V').replace(/color-mix\((?:[^()]|\([^()]*\))*\)/g, 'C');
      const lengths = withoutVars.match(/-?\d+(\.\d+)?px/g) || [];
      if (lengths.some(len => !['0px', '1px', '2px', '3px', '-1px', '-2px', '-3px'].includes(len))) fail(file, `${at}: box-shadow "${value}" must use --shadow-* tokens (only 1–3px hairline strokes may be literal)`);
    }
    if (COLOR_PROPS.test(prop) && NAMED_COLOR.test(value)) fail(file, `${at}: named colour in ${prop}; use a --color-* token`);
  }
  for (const [, name] of css.matchAll(/var\((--[\w-]+)/g)) if (!localProps.has(name) && name !== '--pct') fail(file, `references undefined token ${name}`);
}

/* ---- 4. Markup: no presentational inline styles or colours in scripts ---- */
for (const file of MARKUP) {
  const source = read(file);
  for (const [, style] of source.matchAll(/style="([^"]*)"/g)) {
    for (const decl of style.split(';').map(x => x.trim()).filter(Boolean)) {
      if (!/^(width:\s*\$\{[^}]+\}%|--[\w-]+:\s*.+)$/.test(decl)) fail(file, `inline style "${decl}"; only dynamic width percentages and custom properties are allowed — add a class instead`);
    }
  }
  if (/["'`]#[0-9a-f]{3,8}["'`;]/i.test(source) || /\brgba?\(\s*\d/.test(source)) fail(file, 'literal colour in script; read tokens via getComputedStyle or use classes');
}

/* ---- 5. Every class in markup is styled (or is a declared behaviour hook) ---- */
const HOOK_CLASSES = new Set([
  // form/state hooks read by JS; styling comes from their component class (.field, .input, …)
  'theme-toggle', 'record-create-button', 'record-filter', 'routing-only', 'model-select', 'single-only',
  'gateway-single-name', 'gateway-connection-field', 'gateway-single-pressure', 'comparison-form-scope',
  'group-name', 'group-role', 'group-concurrency', 'group-rpm', 'group-iterations',
  'quality-name', 'quality-url', 'quality-key', 'quality-credential-hint', 'quality-model', 'quality-model-hint',
  'routing-side-name', 'routing-side-url', 'routing-side-key', 'comparison-record',
  // semantic markers kept for tests / future targeting
  'report-table', 'report-metrics-emphasis', 'report-signal-list', 'next-step-section', 'report-supplement',
  'advice', 'quality-progress-secondary', 'quality-report-table', 'single', 'fp-welcome'
]);
const styled = new Set([...stripComments(allCss).matchAll(/\.(-?[_a-zA-Z][\w-]*)/g)].map(m => m[1]));
function markupClasses(source) {
  const found = new Set();
  const clean = source.replace(/\$\{(?:[^{}]|\{[^{}]*\})*\}/g, ' ');
  for (const m of clean.matchAll(/class(?:Name)?\s*=\s*\\?["']([^"'\\]*)/g)) for (const token of m[1].split(/\s+/)) if (/^[a-z][\w-]*$/i.test(token)) found.add(token);
  for (const m of clean.matchAll(/classList\.(?:add|remove|toggle)\(([^)]*)\)/g)) for (const q of m[1].matchAll(/["']([\w-]+)["']/g)) found.add(q[1]);
  return found;
}
for (const file of MARKUP) {
  for (const name of markupClasses(read(file))) if (!styled.has(name) && !HOOK_CLASSES.has(name)) fail(file, `class "${name}" has no CSS rule; reuse a documented component or add its styles`);
}

/* ---- 6. Gallery stays in sync with the UI kit ---- */
if (!fs.existsSync(path.join(ROOT, GALLERY))) fail(GALLERY, 'component gallery is missing');
else {
  const gallery = read(GALLERY);
  const kitExports = (read('static/ui-kit.js').match(/root\.UI = Object\.freeze\(\{([\s\S]*?)\}\);/)?.[1] || '').split(/[\s,]+/).filter(x => /^[a-z]\w*$/.test(x));
  for (const name of kitExports) if (!['esc'].includes(name) && !gallery.includes(`UI.${name}(`)) fail(GALLERY, `UI.${name} has no gallery example`);
  for (const name of markupClasses(gallery)) if (!styled.has(name) && !name.startsWith('gallery-') && !HOOK_CLASSES.has(name)) fail(GALLERY, `class "${name}" is not defined by the design system`);
  for (const name of kitExports) if (!doc.includes(`UI.${name}`) && name !== 'esc' && name !== 'TONES' && name !== 'ICONS') fail(DOC, `UI.${name} is not documented`);
}

if (failures.length) {
  console.error(`UI policy failed (${failures.length}):\n - ` + failures.join('\n - '));
  console.error('\nSee docs/design-system.md for the rules and components.');
  process.exit(1);
}
console.log('UI policy: explicit actions, token-only styling (colour/type/radius/shadow/layers), approved breakpoints, styled classes, gallery + docs in sync passed');
