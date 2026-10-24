'use strict';
// Load the actual pinned bundle. Minimal DOM shims only support library loading;
// no Viewer or browser is constructed. Viewport/Spring are the real classes.
const fs=require('node:fs'),path=require('node:path'),vm=require('node:vm'),assert=require('node:assert/strict');
function loadOSD(){
  const element=()=>({style:{},getContext:()=>({}),addEventListener(){},removeEventListener(){}});
  const document={createElement:element,documentElement:element(),addEventListener(){},removeEventListener(){}};
  const ctx={console,document,navigator:{userAgent:'Node viewport regression',appVersion:'1'},location:{search:''},
    setTimeout,clearTimeout,setInterval,clearInterval,performance:require('node:perf_hooks').performance};
  ctx.window=ctx;ctx.self=ctx;
  vm.runInNewContext(fs.readFileSync(path.resolve(__dirname,'../src/inspection_console/vendor/openseadragon.min.js'),'utf8'),ctx,{timeout:3000});
  return ctx.OpenSeadragon;
}
module.exports=loadOSD;
if(require.main===module){
  const O=loadOSD();assert.equal(O.version.versionStr,'6.1.1');
  const v=new O.Viewport({containerSize:new O.Point(800,440),contentSize:new O.Point(2000,1000)});
  assert.equal(typeof v.stop,'undefined','real pinned viewport does not offer stop()');
  const zoom=v.getZoom(true),center=v.getCenter(true);
  // Put the real springs in a moving state, including a non-central zoom pivot.
  v.zoomTo(zoom*2,new O.Point(.1,.2),false);v.panTo(new O.Point(.25,.4),false);
  assert.notEqual(v.getZoom(false),zoom);
  v.zoomTo(zoom,null,true);v.panTo(center,true);v.update();
  for(const current of [true,false]){
    assert(Math.abs(v.getZoom(current)-zoom)<1e-12);
    assert(v.getCenter(current).distanceTo(center)<1e-12);
  }
  console.log('Pinned OpenSeadragon 6.1.1: actual Viewport API and immediate freeze of animated center/zoom passed. No browser rendering claimed.');
}
