'use strict';
// Production UI with standard DOM/canvas contracts; this is not a browser-render test.
const fs=require('node:fs'),path=require('node:path'),vm=require('node:vm'),assert=require('node:assert/strict');
const Model=require('../src/inspection_console/results_model.js');
const root=path.resolve(__dirname,'../src/inspection_console'),html=fs.readFileSync(path.join(root,'index.html'),'utf8'),js=fs.readFileSync(path.join(root,'results_ui.js'),'utf8');
const ids=[...html.matchAll(/\bid="([^"]+)"/g)].map(m=>m[1]);assert.equal(new Set(ids).size,ids.length);
for(const m of js.matchAll(/\$\('([^']+)'\)/g))assert(ids.includes(m[1]),m[1]);
const requests=[],draws=[],texts=[],downloads=[],toasts=[],observers=[];let ctx,bind,fullscreens=0,modalCount=0;
const all=(n,fn)=>[...(fn(n)?[n]:[]),...n.children.flatMap(c=>all(c,fn))];
function el(tag,text='',cls=''){
  const node={tagName:tag.toUpperCase(),textContent:text,className:cls,children:[],attrs:{},dataset:{},handlers:{},style:{},value:'',hidden:false,open:false,clientWidth:900,clientHeight:500,
    classList:{remove(){},add(){}},addEventListener(e,f){this.handlers[e]=f;},setAttribute(k,v){this.attrs[k]=v;},
    append(...items){items.forEach(n=>this.insertBefore(n,null));},
    insertBefore(n,ref){n.remove();n.parent=this;const i=ref?this.children.indexOf(ref):-1;this.children.splice(i<0?this.children.length:i,0,n);},
    replaceChildren(...items){this.children.forEach(c=>c.parent=null);this.children=[];this.append(...items);},
    contains(n){return n===this||this.children.some(c=>c.contains(n));},blur(){ctx.document.activeElement=null;},
    focus(){ctx.document.activeElement=this;},select(){},remove(){if(this.parent){this.parent.children.splice(this.parent.children.indexOf(this),1);this.parent=null;}},
    click(){if(this.tagName==='A')downloads.push(this);return this.handlers.click?.();},
    querySelectorAll(selector){return all(this,n=>selector==='.result-picture'&&n.className==='result-picture');},
    getBoundingClientRect(){return {left:0,top:0,width:this.clientWidth,height:this.clientHeight};},setPointerCapture(){},
    showModal(){this.open=true;modalCount++;},close(){this.open=false;this.handlers.close?.();},
    async requestFullscreen(){assert.notEqual(this.tagName,'DIALOG','HTML forbids fullscreen on a dialog');ctx.document.fullscreenElement=this;fullscreens++;},
    toBlob(fn){fn({});},getContext(){return {fillRect(){},drawImage(...args){draws.push(args);},fillText(...args){texts.push(args);}};}};
  Object.defineProperty(node,'firstChild',{get(){return this.children[0];}});return node;
}
const tags=new Map([...html.matchAll(/<([a-z]+)[^>]*\bid="([^"]+)"/g)].map(m=>[m[2],m[1]]));
const nodes=new Map(ids.map(id=>[id,el(tags.get(id)||'div')]));
const result=(id,digit)=>({instrument_id:id,status:'captured',image:digit.repeat(64)+'.jpg',image_sha256:digit.repeat(64),
  captured_at:'2026-10-24T11:0'+digit+':00Z',bbox:[100,200,200,300],quality:{accepted:true}});
const s={revision:1,schedule:{enabled:false},runtime:{active_job:'r2'},manual_control:{},automatic_registration:{},scene_initialization:{},
  maps:[{id:'old',title:'旧现场',created_at:'2026-10-23T10:00:00Z'},{id:'m',title:'新现场',created_at:'2026-10-24T10:00:00Z'}],
  instruments:['A','B'].map(id=>({id,map_id:'m',name:'仪表 '+id,enabled:true})),jobs:[
    {id:'r1',map_id:'m',mode:'software_preset_capture',status:'completed',started_at:'2026-10-24T11:01:00Z',route:['A','B'],results:[result('A','1'),result('B','1')]},
    {id:'r2',map_id:'m',mode:'software_preset_capture',status:'running',started_at:'2026-10-24T11:02:00Z',route:['B','A'],results:[result('A','2')]}]};
const memory=new Map();
ctx={console,window:{ResultGalleryModel:Model,addEventListener(){},localStorage:{getItem:k=>memory.get(k),setItem:(k,v)=>memory.set(k,v)}},
  document:{activeElement:null,body:el('body'),addEventListener:(e,f)=>{if(e==='DOMContentLoaded')bind=f;},async exitFullscreen(){this.fullscreenElement=null;}},
  $:id=>nodes.get(id),el,button:(label,fn,cls)=>{const n=el('button',label,cls);n.handlers.click=fn;return n;},
  option:(select,value,label)=>{const n=el('option',label);n.value=value;select.append(n);},
  Image:class{constructor(){this.complete=true;this.naturalWidth=1000;this.naturalHeight=600;}addEventListener(){}},
  setTimeout:f=>f(),URL:{createObjectURL:()=>'/test-blob',revokeObjectURL(){}},toast:(t,e)=>toasts.push([t,e]),state:s,mapId:'m',
  request:async(url,body)=>{requests.push({path:url,body});if(url==='/api/state')return s;
    if(url==='/api/export')return {job_history:[{id:'old-r1',map_id:'old',mode:'software_preset_capture',status:'completed',
      started_at:'2026-10-23T11:01:00Z',route:['OLD'],results:[result('OLD','1')]}]};
    if(url==='/api/instruments/rename'){s.instruments.find(a=>a.id===body.id).name=body.name;s.revision++;return {};}
    throw Error('unexpected path '+url);},load:async()=>ctx.window.InspectionResults.render(s,'m')};
vm.createContext(ctx);vm.runInContext(js,ctx);bind();
const cards=()=>nodes.get('results-current-grid').children;
const click=id=>nodes.get(id).handlers.click();
const picture=card=>all(card,n=>n.className==='result-picture')[0];
const pencil=card=>all(card,n=>n.className==='result-rename')[0];
assert.equal(cards().length,2);assert(nodes.get('results-current-summary').textContent.includes('1 / 2'));
assert.equal(cards()[0].dataset.instrumentId,'A','optimized route cannot rearrange the wall');
assert(cards()[1].children.some(n=>String(n.textContent).includes('第 1 轮旧图')));
assert.deepEqual(draws[0].slice(1,5),[97,197,106,106],'crop uses this photograph and native source coordinates');
assert(pencil(cards()[0]).disabled,'cannot rename an active route');
assert.equal(ctx.document.body.dataset.workspace,'dashboard');
(async()=>{
  await Promise.resolve();await Promise.resolve();
  const stable=cards()[1];picture(stable).click();assert(nodes.get('result-viewer').open);
  const original=nodes.get('result-viewer-original').href,meta=nodes.get('result-viewer-meta').textContent;
  assert(original.includes('1'.repeat(64)));assert.equal(nodes.get('result-viewer-canvas').width,106,'large viewer uses full crop pixels');
  click('result-viewer-in');assert.equal(nodes.get('result-viewer-zoom').textContent,'125%');
  const stage=nodes.get('result-viewer-stage');let prevented=false;
  stage.handlers.wheel({preventDefault(){prevented=true;},clientX:450,clientY:250,deltaY:-1});assert(prevented);
  stage.handlers.pointerdown({button:0,pointerId:1,clientX:40,clientY:60});stage.handlers.pointermove({pointerId:1,clientX:60,clientY:90});
  assert(nodes.get('result-viewer-canvas').style.transform.includes('translate(20px, 30px)'));stage.handlers.pointerup();
  click('result-viewer-fit');assert(nodes.get('result-viewer-canvas').style.transform.includes('translate(0px, 0px)'));
  s.jobs[1].results[0]=result('A','3');ctx.window.InspectionResults.render(s,'m');
  assert.equal(cards()[1],stable,'an unrelated new capture must not rebuild B');
  assert.equal(nodes.get('result-viewer-original').href,original);assert.equal(nodes.get('result-viewer-meta').textContent,meta,'background updates never jump the opened photo');
  assert(nodes.get('result-viewer-fresh').hidden,'unrelated capture must not show a new-photo prompt');
  s.jobs[1].progress_results=[result('B','2')];ctx.window.InspectionResults.render(s,'m');
  assert(!nodes.get('result-viewer-fresh').hidden);assert.equal(nodes.get('result-viewer-original').href,original);
  click('result-viewer-fresh');assert(nodes.get('result-viewer-original').href.includes('2'.repeat(64)));
  click('result-viewer-download');assert.equal(downloads.length,1);assert(downloads[0].download.includes('仪表 B_B'));
  assert(texts.some(args=>args[0].includes('仪表 B · B')),'export includes the name and stable ID');
  assert(texts.some(args=>args[0].includes('2026/10/24')),'export keeps the actual capture timestamp');
  await click('result-viewer-fullscreen');assert.equal(fullscreens,1);assert.equal(ctx.document.fullscreenElement,nodes.get('result-viewer-content'));await click('result-viewer-fullscreen');
  nodes.get('result-viewer').handlers.keydown({key:'ArrowLeft',preventDefault(){}});assert(nodes.get('result-viewer-title').textContent.includes('仪表 A'));
  click('result-viewer-close');assert(!nodes.get('result-viewer').open);assert.equal(nodes.get('result-viewer-canvas').width,1,'close releases the large canvas');
  click('workspace-setup');assert.equal(ctx.document.body.dataset.workspace,'setup');
  click('workspace-dashboard');assert.equal(memory.get('inspection.workspace.v1'),'dashboard');
  click('results-history-tab');await Promise.resolve();await Promise.resolve();
  assert(nodes.get('results-current').hidden);assert.equal(nodes.get('results-history-grid').children.length,2);
  assert.equal(nodes.get('results-history-round').children.length,1,'running rounds excluded from completed history');
  nodes.get('results-date').value='2026-10-23';nodes.get('results-date').handlers.change();
  assert(nodes.get('results-history-grid').firstChild.textContent.includes('该日期'));
  click('results-current-tab');s.runtime.active_job=null;s.jobs[1].status='completed_with_errors';s.jobs[1].results.push({...result('B','2'),status:'uncertain'});
  ctx.window.InspectionResults.render(s,'m');assert(!pencil(cards()[0]).disabled);
  pencil(cards()[0]).click();assert(nodes.get('result-rename-dialog').open);
  nodes.get('result-rename-input').value='<主电流表>';
  await nodes.get('result-rename-form').handlers.submit({preventDefault(){}});await Promise.resolve();
  const renamed=requests.find(r=>r.path==='/api/instruments/rename');assert(renamed);
  assert.deepEqual(Object.keys(renamed.body).sort(),['id','name','revision']);assert.equal(renamed.body.name,'<主电流表>');
  assert.equal(cards()[0].children[0].children[0].children[0].textContent,'<主电流表>','custom name remains plain text');
  s.jobs=[];ctx.window.InspectionResults.render(s,'m');
  assert(nodes.get('results-current-summary').textContent.includes('第 2 轮'),'recent-window trimming cannot reset numbers');
  assert.equal(nodes.get('results-history-round').children.length,0,'active date filter remains selected');
  nodes.get('results-date').value='';nodes.get('results-date').handlers.change();
  click('results-history-tab');click('results-compare-toggle');
  assert(!nodes.get('results-history-comparison').hidden);assert(nodes.get('results-history-grid').hidden);
  const placeholders=all(nodes.get('results-history-comparison'),n=>n.className==='result-placeholder');assert.equal(placeholders.length,1,'failed history stays empty, never filled from previous round');
  click('results-history-prev');assert.equal(nodes.get('results-history-round').value,'r1');assert(nodes.get('results-history-prev').disabled);
  click('results-history-next');assert.equal(nodes.get('results-history-round').value,'r2');
  const chosen=nodes.get('results-history-round').value;
  s.jobs.push({id:'r3',map_id:'m',mode:'software_preset_capture',status:'completed',started_at:'2026-10-24T11:03:00Z',route:['B','A'],results:[result('A','4'),result('B','4')]});
  ctx.window.InspectionResults.render(s,'m');assert.equal(nodes.get('results-history-round').value,chosen,'new rounds cannot steal historical selection');
  nodes.get('results-history-scene').handlers.change({target:{value:'old'}});click('results-compare-toggle');
  assert.equal(nodes.get('results-date').value,'');assert.equal(nodes.get('results-history-grid').firstChild.dataset.instrumentId,'OLD');
  assert(nodes.get('results-history-summary').textContent.startsWith('场景 1'));
  assert.equal(cards()[0].dataset.instrumentId,'A');assert.equal(ctx.mapId,'m','history scene never changes the camera working scene');
  s.instruments.push({id:'OLD',map_id:'old',name:'原仪表',enabled:true});ctx.window.InspectionResults.render(s,'old');
  assert.equal(cards()[0].dataset.instrumentId,'OLD','switching scenes must replace old cards');
  ctx.window.InspectionResults.showHistory('m');assert.equal(nodes.get('results-history-scene').value,'m');assert(!nodes.get('results-history').hidden);
  s.jobs.push({id:'r4',map_id:'m',mode:'software_preset_capture',status:'completed',started_at:'2026-10-24T11:04:00Z',route:['A'],results:[{...result('A','5'),bbox:[0,0,1000,600]}]});
  ctx.window.InspectionResults.render(s,'m');
  assert.equal(all(cards()[0],n=>n.tagName==='CANVAS')[0].width,640,'thumbnail canvas is bounded even for a large source');
  picture(cards()[0]).click();assert.equal(nodes.get('result-viewer-canvas').width,1000,'viewer retains the full available pixels');click('result-viewer-close');
  for(let n=5;n<=7;n++)s.jobs.push({id:'r'+n,map_id:'m',mode:'software_preset_capture',status:'completed',started_at:'2026-10-24T11:0'+n+':00Z',route:['A'],results:[result('A','6')]});
  ctx.window.InspectionResults.render(s,'m');ctx.window.InspectionResults.showHistory('m');click('results-compare-toggle');
  const heads=all(nodes.get('results-history-comparison'),n=>n.tagName==='THEAD')[0];
  assert.equal(heads.firstChild.children.length,5,'comparison contains at most four round columns plus the instrument column');
  assert(requests.every(r=>['/api/state','/api/export','/api/instruments/rename'].includes(r.path)),'gallery cannot send camera-motion or capture requests');
  // Exercise deferred thumbnails using the browser-standard observer API contract.
  ctx.IntersectionObserver=class{constructor(callback){this.callback=callback;observers.push(this);}observe(n){this.target=n;}unobserve(){}};
  vm.runInContext(js,ctx);bind();assert(observers.length);observers[0].callback([{isIntersecting:true,target:observers[0].target}]);
  console.log('Result UI passed: stable cards, bounded thumbnails, native crop viewer, zoom/drag/navigation, pinned photos, deferred refresh, timestamped export, guarded rename, scene/date filters and truthful cross-round comparison. No browser rendering claimed.');
})().catch(error=>{console.error(error);process.exitCode=1;});
