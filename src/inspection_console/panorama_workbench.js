'use strict';

// OpenSeadragon owns navigation and image coordinates. A separate pointer layer
// owns supplement gestures, so an annotation drag cannot become a viewer pan.
window.PanoramaWorkbench=(()=>{
  let viewer=null,anno=null,scene=null,annotationKey=null,detailKey=null;
  let drawing=false,changed=false,failed=false,drag=null,overlays=[];
  const data=()=>map()?.overview;
  const name=c=>candidateName(c)+(c.instrument_id?' · '+c.instrument_id:'');
  const style=a=>{const id=a.id.split('~')[0],focused=Workbench.focused()===id;
    const color=focused?'#ffad33':Workbench.has(id)?'#66c9ff':'#37c994';
    return {stroke:color,strokeWidth:focused?3:2,fill:color,fillOpacity:focused ? .15 : .06};};
  function rectangle(id,b){
    const [x,y,r,bottom]=b;
    return {id,bodies:[],target:{annotation:id,selector:{type:'RECTANGLE',geometry:{x,y,w:r-x,h:bottom-y,
      bounds:{minX:x,minY:y,maxX:r,maxY:bottom}}}}};
  }
  function ready(){
    if(viewer&&anno)return true;
    if(failed)return false;
    try{
      viewer=OpenSeadragon({element:$('panorama-viewer'),drawer:'canvas',showNavigationControl:false,
        showNavigator:true,navigatorPosition:'BOTTOM_LEFT',animationTime:.15,blendTime:0,
        maxZoomPixelRatio:5,minZoomImageRatio:.5,visibilityRatio:.2,
        gestureSettingsMouse:{clickToZoom:false,dblClickToZoom:true,scrollToZoom:true}});
      anno=AnnotoriousOSD.createOSDAnnotator(viewer,{drawingEnabled:false,userSelectAction:'SELECT',style});
      anno.on('clickAnnotation',a=>{if(!drawing&&!changed)Workbench.focus(a.id.split('~')[0],{scroll:true});});
      viewer.addHandler('open',()=>{fit();renderBoxes(true);});
      viewer.addHandler('open-failed',()=>toast('全景图加载失败，可以点击“显示扫描区域”重试。',true));
    }catch(error){
      if(anno)try{anno.destroy();}catch(unused){}
      if(viewer)try{viewer.destroy();}catch(unused){}
      viewer=null;anno=null;failed=true;
      $('scene-mode').textContent='大图组件暂时不可用，点击“显示扫描区域”重试；实时监看和云台控制可继续使用。';
      toast('大图组件初始化失败：'+error.message,true);return false;
    }
    return true;
  }
  function renderBoxes(force=false){
    if(!anno||!data()||!viewer.world.getItemCount())return;
    const shown=Workbench.shown(),rows=shown.flatMap(c=>(c.overview_boxes||[]).map((b,i)=>rectangle(c.id+'~'+i,b)));
    const key=JSON.stringify(rows)+':'+shown.filter(c=>Workbench.has(c.id)).map(c=>c.id).join(',')+':'+Workbench.focused();
    if(!force&&key===annotationKey)return;
    annotationKey=key;anno.setAnnotations(rows);anno.setStyle(style);
    // DOM outlines/labels remain explicit even if the library's selection
    // changes. OpenSeadragon keeps these registered to the image when zoomed.
    overlays.forEach(node=>viewer.removeOverlay(node));overlays=[];
    const item=viewer.world.getItemAt(0);
    shown.forEach(c=>(c.overview_boxes||[]).forEach(b=>{
      const node=el('div',null,'panorama-target'+(Workbench.focused()===c.id?' focused':'')+(Workbench.has(c.id)?' batch-selected':''));
      node.dataset.candidateId=c.id;node.setAttribute('aria-label',name(c));
      node.append(el('span',name(c),'panorama-target-label'));
      viewer.addOverlay({element:node,location:item.imageToViewportRectangle(b[0],b[1],b[2]-b[0],b[3]-b[1]),checkResize:false});
      overlays.push(node);
    }));
  }
  function renderDetail(){
    const c=Workbench.shown().find(c=>c.id===Workbench.focused());
    const key=mapId+':'+(c?c.id+':'+c.bbox.join(',')+':'+c.state:'');
    if(detailKey===key)return;detailKey=key;
    const root=$('scene-focus-detail');root.replaceChildren();root.hidden=!c;
    if(c){root.append(el('strong','当前定位：'+name(c)),thumbnail(c),
      el('small','橙色框对应此目标。点击缩略图只定位；勾选后可批量排除。'));}
  }
  function fit(){
    if(!viewer?.world.getItemCount()||!data())return;
    const s=data(),p=s.ring_projection;
    const poses=state.frames.filter(f=>f.map_id===mapId&&f.pose).map(f=>f.pose);
    if(!p||!poses.length){viewer.viewport.goHome(true);return;}
    const low=Math.min(...poses.map(p=>p.pan)),high=Math.max(...poses.map(p=>p.pan));
    const fov=2*Math.atan(p.source_width/(2*p.focal_pixels))*180/Math.PI;
    const left=Math.max(0,(low-fov/2)*p.width/360),right=Math.min(p.width,(high+fov/2)*p.width/360);
    const item=viewer.world.getItemAt(0);
    viewer.viewport.fitBounds(item.imageToViewportRectangle(left,0,right-left,s.height),true);
  }
  function render(){
    if(!mapId||!data()||document.body.classList.contains('advanced-mode')){setDrawing(false);return;}
    $('overview-card').hidden=false;if(!ready())return;
    Workbench.syncList();
    const key=mapId+':'+data().sha256;
    if(scene!==key){scene=key;annotationKey=null;setDrawing(false);overlays=[];
      viewer.open({type:'image',url:'/images/'+data().image,buildPyramid:false});}
    renderBoxes();renderDetail();
    $('overview-title').textContent='全景与仪表清单';
    $('overview-status').textContent='后台合并重复观测 · '+Workbench.kept().length+' 个保留目标';
    $('overview-hint').textContent='框上编号与右侧列表一致。点击缩略图、编号或图上框可定位；勾选后点“排除选中”。漏检用“画框补漏”。';
    const selected=Workbench.shown().filter(c=>Workbench.has(c.id)).length;
    $('scene-selection').textContent=selected?'已勾选 '+selected+' 项，可批量排除':'默认保留全部目标；点击编号查看位置。';
    $('scene-exclude').disabled=!selected||changed;
  }
  function clearDrag(){
    const current=drag;drag=null;$('scene-draw-preview').hidden=true;
    if(current&&$('scene-draw-layer').hasPointerCapture(current.pointer))$('scene-draw-layer').releasePointerCapture(current.pointer);
  }
  function setDrawing(value){
    if(changed&&value)return;
    clearDrag();drawing=!!value&&!!viewer?.world.getItemCount();
    // Library drawing stays disabled; only our exclusive pointer layer draws.
    anno?.setDrawingEnabled(false);anno?.cancelSelected();
    if(viewer){
      if(drawing){
        // Viewport has no stop() API. Freeze the displayed center/scale with
        // supported immediate operations before accepting a drawing gesture.
        const center=viewer.viewport.getCenter(true),zoom=viewer.viewport.getZoom(true);
        viewer.viewport.zoomTo(zoom,null,true);viewer.viewport.panTo(center,true);
      }
      viewer.setMouseNavEnabled(!drawing);
    }
    $('scene-draw-layer').hidden=!drawing;
    $('scene-add').setAttribute('aria-pressed',String(drawing));$('scene-pan').setAttribute('aria-pressed',String(!drawing));
    $('scene-add').textContent=changed?'正在保存补框…':drawing?'取消补框（Esc）':'＋ 画框补漏';
    $('scene-add').disabled=changed;$('scene-pan').disabled=changed;
    $('scene-mode').textContent=changed?'正在保存补框…':drawing?'按住左键拖出表盘矩形，松开保存 · Esc 取消':'滚轮缩放 · 拖动平移 · 点击编号框定位';
  }
  function imagePoint(event){
    if(!viewer?.world.getItemCount()||!data())return null;
    const rect=$('panorama-viewer').getBoundingClientRect();
    const p=viewer.world.getItemAt(0).viewerElementToImageCoordinates(new OpenSeadragon.Point(event.clientX-rect.left,event.clientY-rect.top));
    if(!Number.isFinite(p.x)||!Number.isFinite(p.y))return null;
    return [Math.max(0,Math.min(data().width,p.x)),Math.max(0,Math.min(data().height,p.y))];
  }
  function showDrag(point){
    const item=viewer.world.getItemAt(0),a=item.imageToViewerElementCoordinates(new OpenSeadragon.Point(...drag.start)),
      b=item.imageToViewerElementCoordinates(new OpenSeadragon.Point(...point)),preview=$('scene-draw-preview');
    Object.assign(preview.style,{left:Math.min(a.x,b.x)+'px',top:Math.min(a.y,b.y)+'px',width:Math.abs(a.x-b.x)+'px',height:Math.abs(a.y-b.y)+'px'});
    preview.hidden=false;
  }
  function pointerDown(event){
    if(!drawing||changed||drag||event.button!==0||event.isPrimary===false)return;
    const p=imagePoint(event);if(!p)return;
    event.preventDefault();event.stopImmediatePropagation();
    drag={pointer:event.pointerId,start:p,client:[event.clientX,event.clientY],map:mapId,sha:data().sha256};
    $('scene-draw-layer').setPointerCapture(event.pointerId);showDrag(p);
  }
  function pointerMove(event){
    if(!drag||event.pointerId!==drag.pointer)return;
    event.preventDefault();event.stopImmediatePropagation();const p=imagePoint(event);if(p)showDrag(p);
  }
  async function pointerUp(event){
    if(!drag||event.pointerId!==drag.pointer)return;
    event.preventDefault();event.stopImmediatePropagation();
    const current=drag,p=imagePoint(event);clearDrag();
    if(!p)return;
    const box=[Math.min(current.start[0],p[0]),Math.min(current.start[1],p[1]),Math.max(current.start[0],p[0]),Math.max(current.start[1],p[1])];
    if(Math.abs(current.client[0]-event.clientX)<6||Math.abs(current.client[1]-event.clientY)<6||box[2]-box[0]<2||box[3]-box[1]<2){
      toast('框选太小：请按住左键拖出覆盖完整表盘的矩形，松开保存。',true);return;
    }
    changed=true;setDrawing(false);
    try{
      if(state.scene_initialization?.active||state.automatic_registration?.active)await Workbench.stopWork();
      await load();
      if(mapId!==current.map||data()?.sha256!==current.sha)throw Error('场景已切换，请在当前全景重新框选。');
      await request('/api/annotations/from-overview',{revision:state.revision,map_id:current.map,overview_sha256:current.sha,bbox:box});
      await load(true);toast('补框已保存，已加入当前清单。');
    }catch(e){toast('补框未保存：'+e.message,true);}
    finally{changed=false;setDrawing(false);render();}
  }
  function focus(id){
    const c=Workbench.shown().find(c=>c.id===id),b=c?.overview_boxes?.[0];
    if(!b||!viewer?.world.getItemCount())return;
    setDrawing(false);renderBoxes(true);renderDetail();
    const [x,y,r,bt]=b,pad=Math.max(r-x,bt-y)*1.2,item=viewer.world.getItemAt(0);
    viewer.viewport.fitBounds(item.imageToViewportRectangle(x-pad,y-pad,r-x+2*pad,bt-y+2*pad));
  }
  document.addEventListener('DOMContentLoaded',()=>{
    $('scene-pan').addEventListener('click',()=>setDrawing(false));$('scene-add').addEventListener('click',()=>{if(ready())setDrawing(!drawing);});
    $('scene-fit').addEventListener('click',()=>{if(failed){failed=false;scene=null;render();}else{setDrawing(false);fit();}});
    $('scene-exclude').addEventListener('click',()=>Workbench.excludeSelected());$('scene-clear').addEventListener('click',()=>Workbench.clearSelection());
    $('scene-show-excluded').addEventListener('change',event=>{$('overview-rejected').checked=event.target.checked;render();});
    const layer=$('scene-draw-layer');layer.addEventListener('pointerdown',pointerDown);layer.addEventListener('pointermove',pointerMove);
    layer.addEventListener('pointerup',pointerUp);layer.addEventListener('pointercancel',()=>setDrawing(false));
    layer.addEventListener('lostpointercapture',()=>{if(drag)setDrawing(false);});
    layer.addEventListener('wheel',event=>event.preventDefault(),{passive:false});
    window.addEventListener('blur',()=>{if(drag)setDrawing(false);});
    document.addEventListener('keydown',event=>{
      if(event.key==='Escape'){setDrawing(false);Workbench.clearSelection();}
      if(event.key==='Delete'&&!['INPUT','TEXTAREA','SELECT'].includes(document.activeElement?.tagName))Workbench.excludeSelected();
    });
    if(state)render();
  });
  return {render,focus,active:()=>drawing||changed};
})();
