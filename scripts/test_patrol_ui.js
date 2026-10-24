// Pure DOM-contract tests; these do not claim browser rendering or camera access.
const fs=require('fs'),path=require('path'),vm=require('vm'),assert=require('assert');
const root=path.resolve(__dirname,'..');
const source=fs.readFileSync(path.join(root,'src/inspection_console/app.js'),'utf8');
const overview=fs.readFileSync(path.join(root,'src/inspection_console/overview.js'),'utf8');
const html=fs.readFileSync(path.join(root,'src/inspection_console/index.html'),'utf8');
const ids=new Set([...html.matchAll(/\bid="([^"]+)"/g)].map(m=>m[1]));
const dynamic=new Set([...source.matchAll(/\.id='([^']+)'/g)].map(m=>m[1]));
for(const m of (source+'\n'+overview).matchAll(/\$\('([^']+)'\)/g))
  assert(ids.has(m[1])||dynamic.has(m[1]),'Missing HTML target '+m[1]);
class Element {
  constructor(){this.children=[];this.listeners={};this.disabled=false;this.hidden=false;}
  replaceChildren(...nodes){this.children=nodes;}
  append(...nodes){this.children.push(...nodes);}
  closest(){return this;}
  addEventListener(type,fn){this.listeners[type]=fn;}
}
const nodes=new Map([...ids].map(id=>[id,new Element()]));
const state={schedule:{enabled:false,map_id:null,interval_seconds:300},
  instruments:[1,2,3,4].map(i=>({id:'M'+i,map_id:'scene',enabled:true,pose_status:'verified',visit_pose:{pan:i,tilt:0,zoom:2}})),
  runtime:{mode:'software_preset_capture',hardware_ready:true,active_job:null},
  capabilities:{automatic_registration:true},
  automatic_registration:{available:true,active:false,queued:0,paused:false,uncertain:0,duplicates:0,automatic_model_recheck:true}};
const calls=[];
const context={state,mapId:'scene',$:id=>nodes.get(id),map:()=>({id:'scene',status:'review'}),
  el:(tag,text)=>({tag,text}),document:{activeElement:null,querySelector:()=>new Element(),
    querySelectorAll:()=>[new Element(),new Element(),new Element(),new Element()]},
  act:(...args)=>calls.push(args)};
vm.createContext(context);
vm.runInContext(source.slice(source.indexOf('function renderSchedule()'),source.indexOf('function renderHistory()')),context);
const render=()=>vm.runInContext('renderSchedule()',context);
render();assert.strictEqual(nodes.get('run-once').disabled,false);
assert.strictEqual(nodes.get('arm').disabled,true,'Unconfigured period must stay unavailable');
state.automatic_registration.active=true;render();assert(nodes.get('run-once').disabled);
state.automatic_registration.active=false;state.automatic_registration.queued=1;render();assert(nodes.get('run-once').disabled);
state.automatic_registration.paused=true;render();assert.strictEqual(nodes.get('annotation-resume').disabled,false);
state.automatic_registration.queued=0;state.schedule.enabled=true;render();assert(nodes.get('run-once').disabled);
assert(nodes.get('arm').disabled,'An enabled batch cannot be rearmed');
state.schedule.round_limit=5;state.schedule.completed_rounds=2;state.schedule.batch_id='batch-test';render();
assert(nodes.get('schedule-status').children.some(n=>n.text?.includes('2 / 5')));
state.schedule.enabled=false;state.schedule.state='batch_completed';render();
assert(nodes.get('schedule-status').children.some(n=>n.text?.includes('已自动停止')));
state.schedule.enabled=false;state.instruments[0].pose_status='unverified';render();assert(nodes.get('run-once').disabled);
const clickLine=source.split('\n').find(s=>s.includes("$('run-once').addEventListener('click'"));
vm.runInContext(clickLine,context);nodes.get('run-once').listeners.click();
assert.strictEqual(calls[0][0],'/api/patrol/run-once');
assert.strictEqual(calls[0][1].map_id,'scene');
assert(!calls.some(c=>c[0]==='/api/schedule/arm'),'One-shot must not arm periodic capture');
const formLine=source.split('\n').find(s=>s.includes("$('schedule-form').addEventListener('submit'"));
vm.runInContext(formLine,context);nodes.get('interval').value='60';nodes.get('round-limit').value='5';
nodes.get('schedule-form').listeners.submit({preventDefault(){}});
assert.strictEqual(calls.at(-1)[1].round_limit,5);
nodes.get('round-limit').value='';nodes.get('schedule-form').listeners.submit({preventDefault(){}});
assert.strictEqual(calls.at(-1)[1].round_limit,null);
console.log('Patrol UI contracts passed: readiness, STOP, bounded progress, rearm prevention, round-limit configuration, one-shot endpoint; no browser rendering claimed.');
