'use strict';
const assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path'),vm=require('node:vm');
const root=path.resolve(__dirname,'../src/inspection_console');
const script=fs.readFileSync(path.join(root,'first_capture.js'),'utf8');
function boot(){
  const nodes=new Map(),requests=[],responses={status:{available:true,active:false,status:'idle'}};
  const node=id=>{
    if(!nodes.has(id))nodes.set(id,{disabled:false,checked:false,textContent:'',events:{},children:[],
      addEventListener(k,f){this.events[k]=f;},append(child){this.children.push(child);},
      replaceChildren(){this.children=[];this.textContent='';}});
    return nodes.get(id);
  };
  const plan={revision:7,map_id:'scene-a',fingerprint:'bound-plan',ready_for_trial:true,budget_seconds:370,
    pan_limits:[10,60],tilt_limits:[0,30],zoom_limits:[1,2],blocked:[],stops:[{id:'M0001',name:'A'}]};
  const document={getElementById:node,querySelector:()=>({content:'csrf'}),createElement:()=>({textContent:''})};
  const window={};
  vm.runInNewContext(script,{window,document,fetch:async(url,options)=>{
    requests.push({url,options});
    if(responses.hold&&url.includes('prepare'))return responses.hold;
    return {ok:true,json:async()=>url.endsWith('/status')?{...responses.status}:
      url.endsWith('/start')?{started:true}:url.endsWith('/stop')?{cancel_requested:true}:{plan}};
  }});
  const tick=async()=>{for(let i=0;i<12;i++)await Promise.resolve();};
  return {node,window,requests,responses,plan,tick,
    update:(revision=7,mapId='scene-a')=>window.FirstCapture.update({revision},mapId,{pan:30,tilt:5,zoom:1}),
    fire:async(id,event='click')=>{await node(id).events[event]();await tick();}};
}
let groups=0;
async function check(label, fn){await fn();groups++;}
(async()=>{
  await check('loading a scene only reads status and never starts motion',async()=>{
    const b=boot();b.update();await b.tick();
    assert.equal(b.requests.length,1);assert.equal(b.requests[0].url,'/api/first-capture/status');
    assert.equal(b.node('first-start').disabled,true);
  });
  await check('prepare is read-only and explicit site readiness gates start',async()=>{
    const b=boot();b.update();await b.tick();await b.fire('first-prepare');
    assert.equal(b.node('first-start').disabled,true);
    b.node('first-site-ready').checked=true;await b.fire('first-site-ready','change');
    assert.equal(b.node('first-start').disabled,false);
    await b.fire('first-start');
    const sent=b.requests.find(r=>r.url.endsWith('/start'));
    assert.deepEqual(JSON.parse(sent.options.body),{map_id:'scene-a',revision:7,fingerprint:'bound-plan',site_ready:true});
    assert.equal(b.node('first-start').disabled,true);
  });
  await check('review changes invalidate both prepared plan and prior readiness',async()=>{
    const b=boot();b.update();await b.tick();await b.fire('first-prepare');
    b.node('first-site-ready').checked=true;await b.fire('first-site-ready','change');
    b.update(8);await b.tick();assert.equal(b.node('first-start').disabled,true);
    assert.equal(b.node('first-site-ready').checked,false);
  });
  await check('unconnected hardware offers plan preview without enabling start',async()=>{
    const b=boot();b.responses.status.available=false;b.update();await b.tick();
    await b.fire('first-prepare');
    assert.ok(b.requests.some(r=>r.url==='/api/plan/first-capture'));
    b.node('first-site-ready').checked=true;await b.fire('first-site-ready','change');
    assert.equal(b.node('first-start').disabled,true);
  });
  await check('active status prevents duplicate starts and allows stop request',async()=>{
    const b=boot();b.responses.status.active=true;b.responses.status.status='running';
    b.update();await b.tick();assert.equal(b.node('first-prepare').disabled,true);
    assert.equal(b.node('first-stop').disabled,false);await b.fire('first-stop');
    assert.equal(b.requests.filter(r=>r.url.endsWith('/stop')).length,1);
  });
  await check('markup contains each referenced element exactly once',async()=>{
    const html=fs.readFileSync(path.join(root,'index.html'),'utf8');
    const ids=[...html.matchAll(/\bid="([^"]+)"/g)].map(m=>m[1]);
    assert.equal(ids.length,new Set(ids).size);
    for(const match of script.matchAll(/node\('([^']+)'\)/g))assert.ok(ids.includes(match[1]),match[1]);
  });
  console.log('First-capture UI checks passed: '+groups+' groups; no physical motion or browser rendering.');
})().catch(error=>{console.error(error);process.exitCode=1;});
