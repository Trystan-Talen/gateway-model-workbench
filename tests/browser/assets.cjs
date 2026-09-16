const fs=require('node:fs'), path=require('node:path');
exports.serveAsset=async function(route,root){
 const pathname=new URL(route.request().url()).pathname;
 const names={'/static/app.js':'app.js','/static/actions.js':'actions.js','/static/app.css':'app.css','/static/tokens.css':'tokens.css','/assets/fingerprint.js':'fingerprint.js','/assets/fingerprint.css':'fingerprint.css'};
 if(!names[pathname])return false;
 const file=names[pathname];
 await route.fulfill({contentType:file.endsWith('.css')?'text/css':'text/javascript',body:fs.readFileSync(path.join(root,'static',file))});
 return true;
};
