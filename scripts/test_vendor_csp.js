'use strict';
// Run the actual vendored bundle with dynamic JS code generation disabled.
// This is a runtime regression for Pixi, not a browser rendering claim.
const fs=require('node:fs'),path=require('node:path'),vm=require('node:vm'),assert=require('node:assert/strict');
const file=path.resolve(__dirname,'../src/inspection_console/vendor/annotorious-openseadragon.js');
const source=fs.readFileSync(file,'utf8');
const start=source.indexOf('/* INSPECTION_CSP_PATCH_BEGIN'),end=source.indexOf('/* INSPECTION_CSP_PATCH_END */');
assert(start>0&&end>start,'static compatibility patch must be present');
const original=source.slice(0,start)+source.slice(end+'/* INSPECTION_CSP_PATCH_END */'.length);
function load(script){
  // Expose only the pinned embedded class to this isolated test copy.
  script=script.replace('Kt.ShapeType=yt,','Kt.__testShaderSystem=lm;Kt.ShapeType=yt,');
  const context=vm.createContext({console,crypto:require('node:crypto').webcrypto,performance:require('node:perf_hooks').performance,
    setTimeout,clearTimeout,OpenSeadragon:{},navigator:{userAgent:'CSP regression'}},
    {codeGeneration:{strings:false,wasm:false}});
  assert.throws(()=>vm.runInContext('new Function("return 1")',context),/Code generation from strings disallowed/);
  try{vm.runInContext(script,context,{timeout:3000});}
  catch(error){throw new Error(error.name+': '+error.message);}
  return context.AnnotoriousOSD;
}
const old=load(original);
assert.throws(()=>new old.__testShaderSystem({}),/Current environment does not allow unsafe-eval/);
const fixed=load(source);assert.equal(fixed.inspectionCspSafe,true);
const calls=[];
const renderer={gl:{uniform1f:(location,value)=>calls.push([location,value])}};
const shader=new fixed.__testShaderSystem(renderer);
shader.shader={program:{uniformData:{opacity:{type:'float',size:1,isArray:false}}}};
const group={uniforms:{opacity:.75}};
const program={uniformData:{opacity:{value:0,location:'opacity-location'}}};
shader.syncUniforms(group,program);assert.deepEqual(calls,[['opacity-location',.75]]);
shader.syncUniforms(group,program);assert.equal(calls.length,1,'unchanged values do not need a GPU upload');
console.log('Vendored CSP runtime: old failure reproduced, static patch initializes and synchronizes uniforms with code generation disabled.');
