const fs=require('fs'),vm=require('vm'),path=require('path'),assert=require('assert');
const root=path.resolve(__dirname,'../src/inspection_console');
const html=fs.readFileSync(path.join(root,'index.html'),'utf8'),js=fs.readFileSync(path.join(root,'workbench.js'),'utf8');
const ids=[...html.matchAll(/\bid="([^"]+)"/g)].map(m=>m[1]);
assert.equal(new Set(ids).size,ids.length,'HTML IDs must be unique');
for(const m of js.matchAll(/\$\('([^']+)'\)/g))assert(ids.includes(m[1]),'missing '+m[1]);
const ctx={};vm.createContext(ctx);vm.runInContext(js.slice(js.indexOf('function liveImagePoint'),js.indexOf('const Workbench')),ctx);
const point=(x,y)=>ctx.liveImagePoint({left:10,top:20,width:100,height:100},160,90,x,y);
assert.equal(point(60,25),null,'letterbox cannot move camera');
assert.deepEqual([...point(60,70)],[.5,.5]);
assert.equal(ctx.liveImagePoint({left:0,top:0,width:0,height:0},0,0,0,0),null);
// Run the real image click handler: a read-only dashboard must never drive PTZ.
const handler=js.match(/\$\('live-image'\).addEventListener\('click',event=>\{([\s\S]*?)\n    \}\);/)[1];
let motions=0;ctx.document={body:{dataset:{workspace:'dashboard'}}};ctx.$=()=>({checked:true});ctx.move=()=>motions++;
ctx.event={currentTarget:{getBoundingClientRect:()=>({left:0,top:0,width:160,height:90}),naturalWidth:160,naturalHeight:90},clientX:80,clientY:45};
vm.runInContext('(function(event){'+handler+'})(event)',ctx);assert.equal(motions,0);
ctx.document.body.dataset.workspace='setup';vm.runInContext('(function(event){'+handler+'})(event)',ctx);assert.equal(motions,1);
console.log('Workbench DOM, click coordinates and letterboxing: passed');
