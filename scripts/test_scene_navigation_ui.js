'use strict';
// Execute the production load/change handlers against persistent multi-scene data.
// DOM stubs describe APIs only; this is not a browser rendering test.
const fs=require('node:fs'),path=require('node:path'),vm=require('node:vm'),assert=require('node:assert/strict');
const Model=require('../src/inspection_console/results_model.js');
const root=path.resolve(__dirname,'../src/inspection_console'),js=fs.readFileSync(path.join(root,'app.js'),'utf8');
const html=fs.readFileSync(path.join(root,'index.html'),'utf8'),ids=[...html.matchAll(/\bid="([^"]+)"/g)].map(m=>m[1]);
assert.equal(ids.length,new Set(ids).size);
const nodes=new Map(ids.map(id=>[id,{value:'',dataset:{},children:[],handlers:{},
  replaceChildren(){this.children=[];},addEventListener(e,f){this.handlers[e]=f;},scrollIntoView(){this.scrolled=true;}}]));
const data={revision:4,maps:[{id:'old',title:'原场景',created_at:'2026-10-23T10:00:00Z'},
  {id:'new',title:'新场景',created_at:'2026-10-24T10:00:00Z'}],frames:['old','new'].map(id=>({id:'f-'+id,map_id:id})),
  candidates:[],instruments:[],runtime:{mode:'software_preset_capture'}};
const saved=new Map([['inspection.selected-scene.v1','old']]),renders=[],requests=[];
let preferred=null,unfollowed=0,opened=null;
const context={state:null,mapId:null,frameId:null,selectedId:null,document:{body:{dataset:{tab:'review'}}},
  sceneSelection:Model.sceneSelection({getItem:k=>saved.get(k),setItem:(k,v)=>saved.set(k,v)}),
  window:{ResultGalleryModel:Model,InspectionResults:{render:(s,id)=>renders.push(id),showHistory:id=>{opened=id;}}},
  Initialization:{preferredMap:()=>preferred,update(){},unfollow(){preferred=null;unfollowed++;}},
  $:id=>nodes.get(id),request:async url=>{requests.push(url);assert.equal(url,'/api/state');return structuredClone(data);},
  option:(select,value,label)=>select.children.push({value,label}),reviewCandidates:()=>[],
  map:()=>context.state?.maps.find(m=>m.id===context.mapId),frame:()=>context.state?.frames.find(f=>f.id===context.frameId),
  stamp:v=>v,setTab(){},renderReview(){},renderAssets(){},renderSchedule(){},renderHistory(){},setDrawMode(){},toast(){}};
vm.createContext(context);
vm.runInContext(js.slice(js.indexOf('async function load('),js.indexOf('function renderReview(')),context);
vm.runInContext(js.slice(js.indexOf("$('map-select').addEventListener"),js.indexOf("$('frame-select').addEventListener")),context);
(async()=>{
  await context.load();assert.equal(context.mapId,'old');assert.equal(nodes.get('map-select').value,'old');
  assert(nodes.get('scene-archive-note').textContent.includes('2 个独立场景'));
  assert.equal(nodes.get('map-select').children[0].value,'new');
  preferred='new';await context.load();
  assert.equal(context.mapId,'new','explicitly followed completed scan switches despite an unchanged registry revision');
  assert.equal(renders.at(-1),'new');
  nodes.get('map-select').value='old';nodes.get('map-select').handlers.change();await Promise.resolve();await Promise.resolve();
  assert.equal(context.mapId,'old');assert.equal(unfollowed,1);
  assert.equal(saved.get('inspection.selected-scene.v1'),'old');
  data.maps.push({id:'newest',title:'另一个新场景'});data.revision++;
  await context.load();assert.equal(context.mapId,'old','subsequent poll preserves a manually selected old scene');
  nodes.get('scene-open-history').handlers.click();assert.equal(opened,'old');assert(nodes.get('results-panel').scrolled);
  assert(requests.every(p=>p==='/api/state'),'scene navigation cannot start, stop or move any hardware');
  console.log('Scene navigation: reload persistence, explicit scan follow, old-scene selection, history entry and GET-only navigation passed.');
})().catch(error=>{console.error(error);process.exitCode=1;});
