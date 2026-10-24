'use strict';
// Exercise both production adapters and their pointer/list events. This is a
// DOM/coordinate contract test, not a claim that a real browser was rendered.
const fs=require('fs'),vm=require('vm'),path=require('path'),assert=require('assert');
const root=path.resolve(__dirname,'../src/inspection_console');
const html=fs.readFileSync(path.join(root,'index.html'),'utf8');
const ids=new Set([...html.matchAll(/\bid="([^"]+)"/g)].map(m=>m[1]));
function node(tag='div'){
  return {tagName:tag.toUpperCase(),handlers:{},attrs:{},children:[],dataset:{},style:{},hidden:false,
    className:'',textContent:'',captures:new Set(),scrolled:false,
    addEventListener(e,f){this.handlers[e]=f;},setAttribute(k,v){this.attrs[k]=v;},
    append(...nodes){this.children.push(...nodes);},replaceChildren(...nodes){this.children=nodes;},
    getBoundingClientRect(){return {left:20,top:30,width:800,height:440};},
    setPointerCapture(id){this.captures.add(id);},hasPointerCapture(id){return this.captures.has(id);},
    releasePointerCapture(id){this.captures.delete(id);this.handlers.lostpointercapture?.();},
    scrollIntoView(){this.scrolled=true;},closest(){return null;}};
}
const nodes=new Map([...ids].map(id=>[id,node()]));
const el=(tag,text,cls)=>Object.assign(node(tag),{textContent:text||'',className:cls||''});
const list=()=>nodes.get('workbench-candidates');
const rows=[{id:'c1',map_id:'m',frame_id:'f1',state:'pending',bbox:[300,200,600,500],overview_boxes:[[300,200,600,500]]},
  {id:'c2',map_id:'m',frame_id:'f1',state:'pending',bbox:[900,400,950,450],overview_boxes:[[900,400,950,450]],instrument_id:'M2'}];
const scene={review_groups:[],overview:{sha256:'hash',image:'image.png',width:2000,height:1000}};
const handlers={},events={},docEvents={},bindings=[],requests=[],messages=[];
let loaded=[],attempts=0,destroyed=0,nav=true,scale=2,origin=[100,80],fits=[],overlays=[];
let beforeLoad=null,failRequest=false,waitRequest=null;
const item={imageToViewportRectangle:(...x)=>x,
  viewerElementToImageCoordinates:p=>({x:p.x/scale+origin[0],y:p.y/scale+origin[1]}),
  imageToViewerElementCoordinates:p=>({x:(p.x-origin[0])*scale,y:(p.y-origin[1])*scale})};
let freezeCalls=0;
const viewport={goHome(){},fitBounds:b=>fits.push(b),getCenter:current=>{assert.equal(current,true);return {x:.5,y:.25};},
  getZoom:current=>{assert.equal(current,true);return 2;},zoomTo:(zoom,ref,now)=>{assert.equal(zoom,2);assert.equal(ref,null);assert.equal(now,true);freezeCalls++;},
  panTo:(center,now)=>{assert.deepEqual(center,{x:.5,y:.25});assert.equal(now,true);}};
const actualOSD=require('./test_osd_runtime.js')();
for(const method of Object.keys(viewport))assert.equal(typeof actualOSD.Viewport.prototype[method],'function','test replacement invented Viewport.'+method);
assert.equal(typeof viewport.stop,'undefined','do not invent APIs in the test replacement');
const viewer={world:{getItemCount:()=>1,getItemAt:()=>item},viewport,
  setMouseNavEnabled:value=>nav=value,addOverlay:o=>overlays.push(o),removeOverlay:n=>{overlays=overlays.filter(o=>o.element!==n);},
  addHandler:(e,f)=>events[e]=f,open(){overlays=[];events.open();},destroy(){destroyed++;}};
const anno={setDrawingEnabled:x=>assert.equal(x,false,'exclusive pointer layer owns drawing'),cancelSelected(){},
  on:(e,f)=>handlers[e]=f,setAnnotations:x=>loaded=x,setStyle(fn){this.style=fn;},destroy(){}};
const osd=()=>viewer;osd.Point=class Point{constructor(x,y){this.x=x;this.y=y;}};
const ctx={console,window:{addEventListener(){}},OpenSeadragon:osd,
  AnnotoriousOSD:{createOSDAnnotator:()=>{if(++attempts===1)throw Error('simulated renderer failure');return anno;}},
  $:id=>nodes.get(id),map:()=>scene,mapId:'m',candidates:()=>rows,candidateName:c=>'C0'+(rows.indexOf(c)+1),labels:{},el,
  thumbnail:()=>node('canvas'),button:(text,fn,cls)=>{const b=el('button',text,cls);b.handlers.click=fn;return b;},
  state:{revision:7,frames:[],runtime:{},schedule:{enabled:false},capabilities:{automatic_registration:true},
    scene_initialization:{active:false},automatic_registration:{active:false},manual_control:{}},
  renderSchedule(){},setInterval(){},setTimeout,clearTimeout,
  document:{body:{classList:{contains:()=>false}},querySelectorAll:()=>[],
    addEventListener:(e,f)=>{if(e==='DOMContentLoaded')bindings.push(f);else docEvents[e]=f;}},
  request:async(path,body)=>{if(waitRequest)await waitRequest;if(failRequest)throw Error('simulated API failure');requests.push({path,body});},
  load:async()=>{if(beforeLoad)beforeLoad();},toast:(text,error)=>messages.push({text,error})};
vm.createContext(ctx);
for(const file of ['workbench.js','panorama_workbench.js']){
  const js=fs.readFileSync(path.join(root,file),'utf8');
  for(const m of js.matchAll(/\$\('([^']+)'\)/g))assert(ids.has(m[1]),m[1]);
  vm.runInContext(js,ctx);
}
const workbench=vm.runInContext('Workbench',ctx),panorama=ctx.window.PanoramaWorkbench;
bindings[1]();assert.equal(destroyed,1);assert(nodes.get('scene-mode').textContent.includes('重试'));
nodes.get('scene-fit').handlers.click();assert.equal(attempts,2);assert.equal(loaded.length,2);
assert.equal(overlays.length,2);assert.equal(overlays[0].element.children[0].textContent,'C01');
// A thumbnail/name locates a numbered outline without marking it for exclusion.
list().children[1].children[1].handlers.click();assert.equal(workbench.focused(),'c2');assert(!workbench.has('c2'));
assert(overlays.find(o=>o.element.dataset.candidateId==='c2').element.className.includes('focused'));
assert.equal(nodes.get('scene-focus-detail').children[0].textContent,'当前定位：C02 · M2');
assert(fits.length>0);assert(list().children[1].className.includes('focused'));
// Checking a row also focuses it. Its exact image rectangle remains registered.
list().children[0].children[0].handlers.change();assert(workbench.has('c1'));assert.equal(workbench.focused(),'c1');
assert.deepEqual([...overlays.find(o=>o.element.dataset.candidateId==='c1').location],[300,200,300,300]);
handlers.clickAnnotation(loaded[1]);assert.equal(workbench.focused(),'c2');assert(!workbench.has('c2'));
assert(list().children[1].scrolled,'map selection reveals corresponding row');
const layer=nodes.get('scene-draw-layer'),preview=nodes.get('scene-draw-preview');
const event=(x,y,id=1)=>({button:0,isPrimary:true,pointerId:id,clientX:x,clientY:y,preventDefault(){},stopImmediatePropagation(){}});
const draw=()=>nodes.get('scene-add').handlers.click();
const down=(x,y)=>layer.handlers.pointerdown(event(x,y));
const up=(x,y)=>layer.handlers.pointerup(event(x,y));
(async()=>{
  draw();assert.equal(nav,false);assert.equal(layer.hidden,false);assert.equal(freezeCalls,1);
  down(40,50);layer.handlers.pointermove(event(100,110));assert.equal(preview.hidden,false);assert.equal(preview.style.width,'60px');
  await up(100,110);assert.equal(requests.length,1);
  assert.deepEqual([...requests[0].body.bbox],[110,90,140,120],'screen-to-image uses current zoom and pan');
  assert.equal(requests[0].path,'/api/annotations/from-overview');assert.equal(requests[0].body.revision,7);
  assert(nav);assert(layer.hidden);assert(preview.hidden);assert.equal(layer.captures.size,0);
  // Reverse drag, different scale and offset; clip outside image extents.
  scale=.5;origin=[-50,-20];draw();down(100,100);await up(0,0);
  assert.deepEqual([...requests[1].body.bbox],[0,0,110,120]);
  const count=requests.length;
  draw();down(40,50);docEvents.keydown({key:'Escape'});await up(100,110);
  assert.equal(requests.length,count,'Escape never posts');assert(nav);assert.equal(workbench.focused(),null);
  draw();down(40,50);layer.handlers.pointercancel();await up(100,110);assert.equal(requests.length,count);assert(nav);
  draw();down(40,50);await up(41,51);assert.equal(requests.length,count,'tiny gesture is not a meter');
  nodes.get('scene-pan').handlers.click();
  draw();down(40,50);beforeLoad=()=>ctx.mapId='different';await up(100,110);beforeLoad=null;
  assert.equal(requests.length,count,'old-scene gesture cannot post to a new map');ctx.mapId='m';
  failRequest=true;draw();down(40,50);await up(100,110);failRequest=false;
  assert(nav&&layer.hidden&&!nodes.get('scene-add').disabled,'API failure restores browsing and controls');
  // Repeated pointer-up while save is pending cannot create another record.
  let release;waitRequest=new Promise(resolve=>release=resolve);draw();down(40,50);
  const pending=up(100,110);await Promise.resolve();await up(100,110);
  assert(nodes.get('scene-add').disabled);release();await pending;waitRequest=null;
  assert.equal(requests.length,count+1);
  workbench.clearSelection();assert(!overlays.some(o=>o.element.className.includes('focused')));
  assert(requests.every(r=>r.path==='/api/annotations/from-overview'),'review interactions cannot issue PTZ commands');
  console.log('Panorama/list regression: numbered focus, row linkage, pointer preview, zoom/pan mapping, reverse/edge drag, cancel, stale scene, save failure and duplicate release passed. No browser rendering claimed.');
})().catch(e=>{console.error(e);process.exitCode=1;});
