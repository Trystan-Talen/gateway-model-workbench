const fs=require('node:fs'), path=require('node:path');
exports.serveAsset=async function(route,root,{fingerprintRoot}={}){
 const pathname=new URL(route.request().url()).pathname;
 const names={'/static/tpm.js':'tpm.js','/static/repeat.js':'repeat.js','/static/connections.js':'connections.js','/static/app.js':'app.js','/static/actions.js':'actions.js','/static/ui-kit.js':'ui-kit.js','/static/app.css':'app.css','/static/tokens.css':'tokens.css','/assets/fingerprint.js':'fingerprint.js','/assets/fingerprint.css':'fingerprint.css'};
 if(!names[pathname])return false;
 const file=names[pathname];
 const folder=fingerprintRoot&&pathname.startsWith('/assets/fingerprint.')?fingerprintRoot:path.join(root,'static');
 await route.fulfill({contentType:file.endsWith('.css')?'text/css':'text/javascript',body:fs.readFileSync(path.join(folder,file))});
 return true;
};
