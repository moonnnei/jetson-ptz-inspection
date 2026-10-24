// Initialization controls and pending work; no browser rendering or camera access.
const fs=require('fs'),path=require('path'),vm=require('vm'),assert=require('assert');
const root=path.resolve(__dirname,'..');
const source=fs.readFileSync(path.join(root,'src/inspection_console/initialization.js'),'utf8');
const html=fs.readFileSync(path.join(root,'src/inspection_console/index.html'),'utf8');
const ids=new Set([...html.matchAll(/\bid="([^"]+)"/g)].map(m=>m[1]));
for(const m of source.matchAll(/\$\('([^']+)'\)/g))assert(ids.has(m[1]),'Missing HTML target '+m[1]);
class Node{constructor(){this.nodes=[];this.hidden=false;this.disabled=false;this.handlers={};}
  replaceChildren(...n){this.nodes=n;}append(...n){this.nodes.push(...n);}
  addEventListener(k,f){this.handlers[k]=f;}}
const nodes=new Map([...ids].map(id=>[id,new Node()]));let bind;
const calls=[],context={$:id=>nodes.get(id),el:(tag,text)=>({tag,text}),
 document:{addEventListener:(event,fn)=>{assert.strictEqual(event,'DOMContentLoaded');bind=fn;}},
 act:async(...args)=>{calls.push(args);return {started:true};}};
vm.createContext(context);vm.runInContext(source,context);bind();
const state={scene_initialization:{available:true,active:false,job:null},schedule:{enabled:false},
 runtime:{active_job:null},automatic_registration:{active:false,queued:0}};
context.s=state;const update=()=>vm.runInContext('Initialization.update(s)',context);
update();assert(!nodes.get('initialize-start').disabled);assert(nodes.get('initialize-stop').disabled);
state.scene_initialization.active=true;state.scene_initialization.job={status:'detecting',views:2,total_views:19};
update();assert(nodes.get('initialize-start').disabled);assert(!nodes.get('initialize-stop').disabled);
assert(nodes.get('initialization-status').nodes.some(n=>n.text.includes('2 / 19')));
state.scene_initialization.active=false;state.scene_initialization.job={status:'completed_with_uncertain',verified_instruments:3,
 uncertain_candidates:1,duplicate_candidates:2,deferred_candidates:4,ready_for_patrol:false};update();
assert(nodes.get('initialization-status').nodes.some(n=>n.text.includes('未处理')));
assert(!nodes.get('initialization-status').nodes.some(n=>n.text.includes('采集位置已就绪')));
state.scene_initialization.stop_error='STOP';update();assert(nodes.get('initialize-start').disabled);
state.scene_initialization.available=false;update();assert(nodes.get('initialization-card').hidden);
nodes.get('initialize-start').handlers.click().then(()=>{
 assert.strictEqual(calls[0][0],'/api/initialization/start');
 assert(!calls.some(c=>c[0]==='/api/schedule/arm'));
 console.log('Initialization UI contract passed: IDs, progress, uncertain/deferred work, STOP, start endpoint; no rendering claim.');
});
