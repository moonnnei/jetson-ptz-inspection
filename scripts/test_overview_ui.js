'use strict';
// Dependency-free coordinate/markup checks, not a browser rendering test.
const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
const vm=require('node:vm');
const root=path.resolve(__dirname,'../src/inspection_console');
const code=fs.readFileSync(path.join(root,'overview.js'),'utf8');
const math=new Function(code.split('const OverviewReview=')[0]+';return {overviewView,overviewRect};')();
let checks=0;
const check=(label,fn)=>{fn();checks++;};
check('full preview fits without losing top or sides',()=>{
  const v=math.overviewView(4096,600,1000,420,1,2048,300);
  assert.equal(v.x,0);assert.equal(v.width,4096);assert.ok(v.y<=0&&v.y+v.height>=600);
});
check('zoom remains inside the image on both extremes',()=>{
  for(const z of [2,4,12,24])for(const cx of [-999,99999])for(const cy of [-999,99999]){
    const v=math.overviewView(4096,600,1000,420,z,cx,cy);
    if(v.width<4096)assert.ok(v.x>=0&&v.x+v.width<=4096+1e-8);
    if(v.height<600)assert.ok(v.y>=0&&v.y+v.height<=600+1e-8);
  }
});
check('mobile viewport retains coordinate aspect',()=>{
  const v=math.overviewView(4096,600,350,380,4,1800,280);
  assert.ok(Math.abs(v.width/v.height-350/380)<1e-10);
});
check('reverse-direction boxes preserve source coordinates',()=>{
  assert.deepEqual(math.overviewRect([200,400],[50,100],4096,600),[50,100,200,400]);
  assert.deepEqual(math.overviewRect([-20,-50],[5000,999],4096,600),[0,0,4096,600]);
});
check('all literal UI references exist exactly once',()=>{
  const html=fs.readFileSync(path.join(root,'index.html'),'utf8');
  const ids=[...html.matchAll(/\bid="([^"]+)"/g)].map(m=>m[1]);
  assert.equal(new Set(ids).size,ids.length);
  for(const file of ['overview.js','app.js']){
    const text=fs.readFileSync(path.join(root,file),'utf8');
    for(const m of text.matchAll(/\$\('([^']+)'\)/g))assert.ok(ids.includes(m[1]),file+': '+m[1]);
  }
  assert.ok(html.includes('src="/overview.js" defer'));
  assert.ok(html.includes('id="overview-source-dialog"'));
});
check('both scripts boot before asynchronous state arrives',()=>{
  const nodes=new Map();
  const node=()=>({addEventListener(){},open:false,
    classList:{toggle(){},contains(){return false;}},setAttribute(){}});
  const document={getElementById(id){if(!nodes.has(id))nodes.set(id,node());return nodes.get(id);},
    querySelector(){return {content:'test-only-token'};},querySelectorAll(){return [];},addEventListener(){}};
  const context=vm.createContext({document,window:{addEventListener(){}},console,
    fetch:()=>new Promise(()=>{}),AbortController,setInterval(){},setTimeout(){},clearTimeout(){},
    ResizeObserver:class{constructor(fn){this.fn=fn;}observe(){this.fn();}}});
  // Match the actual deferred-script order, including the scene-selection model.
  vm.runInContext(fs.readFileSync(path.join(root,'results_model.js'),'utf8'),context);
  vm.runInContext(fs.readFileSync(path.join(root,'app.js'),'utf8'),context);
  vm.runInContext(code,context);
});
console.log('Overview coordinate/markup checks passed: '+checks+' groups; browser interaction not covered.');
