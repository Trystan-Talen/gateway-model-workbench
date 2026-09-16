'use strict';
const fs = require('node:fs');
const assert = require('node:assert/strict');
for (const file of ['static/index.html','static/app.js','static/fingerprint.js']) {
  const source = fs.readFileSync(file,'utf8');
  assert(!/\bon(?:click|change|input|submit|keydown)\s*=\s*["']/.test(source), `${file}: inline event handler`);
  assert(!/\beval\s*\(|new\s+Function\s*\(/.test(source), `${file}: executable handler strings`);
}
for (const file of ['static/app.css','static/fingerprint.css']) {
  const css=fs.readFileSync(file,'utf8').replace(/url\([^)]*\)/g,'');
  assert(!/#[0-9a-f]{3,8}\b/i.test(css), `${file}: use a shared color token`);
  for(const [,value] of css.matchAll(/@media[^{}]*max-width:\s*(\d+)px/g)) assert(['640','980','1100'].includes(value), `${file}: unapproved breakpoint ${value}`);
}
console.log('UI policy: external scripts, explicit actions, shared colors, 3 breakpoints passed');
