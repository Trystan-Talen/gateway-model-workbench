'use strict';
const {spawnSync}=require('node:child_process'),path=require('node:path');
const scripts=['smoke','routing_create','layout','fingerprint','auth_focus','workflow','quality_v2','modal_layout'];
for(const name of scripts){
 console.log(`Browser regression: ${name}`);
 const result=spawnSync(process.execPath,[path.join(__dirname,name+'.cjs')],{stdio:'inherit',env:process.env});
 if(result.status!==0)process.exit(result.status||1);
}
