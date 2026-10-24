'use strict';
// DOM lifecycle checks without a browser or camera connection.
const assert=require('node:assert/strict');
const fs=require('node:fs'),path=require('node:path'),vm=require('node:vm');
const root=path.resolve(__dirname,'../src/inspection_console');
const script=fs.readFileSync(path.join(root,'live.js'),'utf8');
let groups=0;
async function boot(){
  const nodes=new Map(),timers=new Map(),requests=[],handlers=new Map();let id=0,clock=10000;
  const state={enabled:true,available:true,rotation:180,source_age_ms:40,detection_ms:300};
  function node(name){
    if(!nodes.has(name))nodes.set(name,{hidden:false,open:true,textContent:'',attrs:{},events:new Map(),
      classList:{values:new Set(),toggle(k,on){if(on)this.values.add(k);else this.values.delete(k);}},
      addEventListener(k,f){this.events.set(k,f);},removeAttribute(k){delete this[k];delete this.attrs[k];},
      setAttribute(k,v){this.attrs[k]=v;},requestFullscreen:async()=>{}});
    return nodes.get(name);
  }
  const document={hidden:false,fullscreenElement:null,getElementById:node,
    addEventListener:(k,f)=>handlers.set(k,f),exitFullscreen:async()=>{}};
  const window={addEventListener:(k,f)=>handlers.set(k,f)};
  const context=vm.createContext({document,window,console,AbortController,
    Date:{now:()=>clock},setTimeout:(f,ms)=>{timers.set(++id,{f,ms});return id;},
    clearTimeout:key=>timers.delete(key),setInterval:f=>{handlers.set('interval',f);},
    fetch:async(url,options)=>{requests.push({url,options});return {ok:true,json:async()=>({...state})};}});
  vm.runInContext(script,context);
  const tick=async()=>{for(let i=0;i<6;i++)await Promise.resolve();};
  await tick();
  return {node,state,document,requests,timers,handlers,tick,advance:ms=>{clock+=ms;},
    fire:(name,event)=>node(name).events.get(event)()};
}
async function check(label,fn){await fn();groups++;}
(async()=>{
  await check('default starts a correctly oriented source stream, no control calls',async()=>{
    const b=await boot();assert.ok(b.node('live-image').src.startsWith('/live.mjpg?'));
    assert.ok(b.node('live-image').classList.values.has('rotate180'));
    assert.deepEqual(b.requests.map(r=>r.url),['/api/live/status']);
    assert.ok(b.requests.every(r=>!r.options.method));
  });
  await check('detection polling is serialized after image load and not double rotated',async()=>{
    const b=await boot();b.fire('live-detection','click');
    assert.ok(b.node('live-image').src.startsWith('/live/detection.jpg?'));
    assert.ok(!b.node('live-image').classList.values.has('rotate180'));
    assert.equal([...b.timers.values()].filter(t=>t.ms===300).length,0);
    b.fire('live-image','load');assert.equal([...b.timers.values()].filter(t=>t.ms===300).length,1);
  });
  await check('pause releases the image connection and resume reconnects',async()=>{
    const b=await boot();b.fire('live-pause','click');
    assert.equal(b.node('live-image').src,undefined);assert.equal(b.node('live-image').hidden,true);
    b.fire('live-pause','click');assert.ok(b.node('live-image').src.startsWith('/live.mjpg?'));
  });
  await check('collapsed card and hidden document release the stream',async()=>{
    const b=await boot();b.node('live-card').open=false;b.fire('live-card','toggle');
    assert.equal(b.node('live-image').src,undefined);
    b.node('live-card').open=true;b.fire('live-card','toggle');assert.ok(b.node('live-image').src);
    b.document.hidden=true;b.handlers.get('visibilitychange')();assert.equal(b.node('live-image').src,undefined);
    b.document.hidden=false;b.handlers.get('visibilitychange')();assert.ok(b.node('live-image').src);
  });
  await check('failed image retries after a bounded delay without retaining old frame',async()=>{
    const b=await boot();b.fire('live-image','error');assert.equal(b.node('live-image').hidden,true);
    b.handlers.get('interval')();await b.tick();assert.equal(b.node('live-image').src,undefined);
    b.advance(2000);b.handlers.get('interval')();await b.tick();assert.ok(b.node('live-image').src);
  });
  await check('transient status failures do not break the active video connection',async()=>{
    const b=await boot(),src=b.node('live-image').src;b.state.available=false;b.handlers.get('interval')();await b.tick();
    assert.equal(b.node('live-image').src,src);assert.equal(b.node('live-image').hidden,false);
    b.state.available=true;b.handlers.get('interval')();await b.tick();assert.equal(b.node('live-image').src,src);
  });
  await check('unload releases stream and suppresses reconnect',async()=>{
    const b=await boot();b.handlers.get('pagehide')();b.handlers.get('interval')();await b.tick();
    assert.equal(b.node('live-image').src,undefined);
  });
  await check('all referenced UI elements exist once in the page',async()=>{
    const html=fs.readFileSync(path.join(root,'index.html'),'utf8');
    const ids=[...html.matchAll(/\bid="([^"]+)"/g)].map(m=>m[1]);
    assert.equal(ids.length,new Set(ids).size);
    for(const match of script.matchAll(/node\('([^']+)'\)/g))assert.ok(ids.includes(match[1]),match[1]);
    assert.ok(html.includes('src="/live.js" defer'));
    assert.ok(html.indexOf('id="live-card"')<html.indexOf('id="review-panel"'));
  });
  console.log('Live preview UI lifecycle checks passed: '+groups+' groups; no browser rendering claimed.');
})().catch(error=>{console.error(error);process.exitCode=1;});
