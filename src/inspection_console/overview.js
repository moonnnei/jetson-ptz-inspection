'use strict';

// Image coordinates are independent of CSS pixels, zoom and letterboxing.
function overviewView(width,height,screenWidth,screenHeight,zoom,cx,cy) {
  const scale=Math.min(Math.max(1,screenWidth)/width,Math.max(1,screenHeight)/height)*zoom;
  const w=Math.max(1,screenWidth)/scale,h=Math.max(1,screenHeight)/scale;
  const center=(value,total,size)=>size>=total?total/2:Math.max(size/2,Math.min(total-size/2,value));
  cx=center(cx,width,w);cy=center(cy,height,h);
  return {x:cx-w/2,y:cy-h/2,width:w,height:h,cx,cy};
}
function overviewRect(a,b,width,height) {
  const bound=(v,max)=>Math.max(0,Math.min(max,v));
  return [bound(Math.min(a[0],b[0]),width),bound(Math.min(a[1],b[1]),height),
          bound(Math.max(a[0],b[0]),width),bound(Math.max(a[1],b[1]),height)];
}

const OverviewReview=(()=>{
  let ids=new Set(),sceneId=null,zoom=1,cx=0,cy=0,mode='pan';
  let gesture=null,suppressClick=false,proposal=null,source=null,sourceBox=null,sourceStart=null,sourceReady=false;
  const svg=$('overview'),dialog=$('overview-source-dialog');
  Object.assign(actionLabels,{candidate_add_overview:'大图补框',review_many_confirm:'多选确认',review_many_reject:'多选排除',review_many_restore:'多选恢复'});
  const items=()=>candidates().filter(c=>(typeof Workbench==='undefined'||Workbench.visibleIds().has(c.id))&&(!$('overview-pending').checked||c.state==='pending')&&
    ($('overview-rejected').checked||!['rejected','merged','auto_uncertain','auto_duplicate'].includes(c.state)));
  const point=(element,event)=>{
    const matrix=element.getScreenCTM();if(!matrix)return null;
    const p=element.createSVGPoint();p.x=event.clientX;p.y=event.clientY;
    const v=p.matrixTransform(matrix.inverse());return [v.x,v.y];
  };
  function applyView() {
    const summary=map()?.overview;if(!summary)return;
    const rect=svg.getBoundingClientRect();
    const v=overviewView(summary.width,summary.height,rect.width,rect.height,zoom,cx,cy);
    cx=v.cx;cy=v.cy;svg.setAttribute('viewBox',[v.x,v.y,v.width,v.height].join(' '));
    $('overview-scale').textContent=zoom===1?'适应画幅':zoom.toFixed(1)+'×';
    svg.querySelectorAll('.overview-label').forEach(n=>n.setAttribute('font-size',Math.max(6,v.width/100)));
  }
  function setMode(value) {
    mode=value;gesture=null;
    ['pan','select','add'].forEach(key=>$('overview-'+key).setAttribute('aria-pressed',String(key===mode)));
    svg.classList.toggle('box-mode',mode!=='pan');
    $('overview-mode-hint').textContent=mode==='add'?(state?.capabilities?.automatic_registration?'拖框圈出漏检表盘；松开后自动建档':'拖框圈出漏检表盘；松开后核对高清来源'):mode==='select'?'拖框选择多个候选；Ctrl / Shift 可追加':'滚轮放大 · 拖动平移 · 点击候选查看高清局部';
  }
  function changeZoom(factor,event) {
    const before=event?point(svg,event):null;
    zoom=Math.max(1,Math.min(24,zoom*factor));applyView();
    if(before){const after=point(svg,event);cx+=before[0]-after[0];cy+=before[1]-after[1];applyView();}
  }
  function fitScannedArea(){
    const summary=map()?.overview,p=summary?.ring_projection;
    zoom=1;cx=summary.width/2;cy=summary.height/2;
    const poses=state.frames.filter(f=>f.map_id===mapId&&f.pose).map(f=>f.pose);
    if(p&&poses.length){
      const low=Math.min(...poses.map(p=>p.pan)),high=Math.max(...poses.map(p=>p.pan));
      if(high-low<240){
        const fov=2*Math.atan(p.source_width/(2*p.focal_pixels))*180/Math.PI;
        const width=(high-low+fov)*p.width/360;
        const r=svg.getBoundingClientRect();
        if(r.width>0&&r.height>0){
          const base=Math.min(r.width/p.width,r.height/p.height);
          zoom=Math.max(1,Math.min(24,Math.min(r.width/width,r.height/p.height)/base));
          cx=(high+low)/2/360*p.width;
        }
      }
    }
    applyView();
  }
  function showSource() {
    $('source-details').open=true;
    $('source-details').scrollIntoView({behavior:'smooth',block:'start'});
  }
  function inspector() {
    const root=$('overview-inspector');root.replaceChildren();
    const list=candidates().filter(c=>ids.has(c.id));
    if(!list.length){root.append(el('h3','查看环视大图'),el('p','点击一个框查看高清原图中的表盘局部。滚轮可放大，拖动可浏览整个场景。'),el('p',state.capabilities?.automatic_registration?'检测候选由系统复检和去重；遗漏表盘用“补充漏检”圈出。':'多个背景误检可用“框选多个”一起排除；遗漏表盘用“补充漏检”圈出。'));return;}
    root.append(el('h3',list.length>1?'已选择 '+list.length+' 个候选':candidateName(list[0])+(list[0].instrument_id?' · '+list[0].instrument_id:'')));
    if(list.length>1){
      root.append(el('p','下方操作只作用于选中的候选；可按 Ctrl / Shift 点击增减。'));
      const names=el('div',null,'overview-selection-list');
      list.forEach(c=>names.append(button(candidateName(c)+' '+labels[c.state],()=>{ids=new Set([c.id]);selectCandidate(c);})));root.append(names);return;
    }
    const item=list[0];root.append(thumbnail(item),el('p',labels[item.state]+' · 高清来源裁剪'));
    const f=state.frames.find(f=>f.id===item.frame_id);
    root.append(el('p',f.title,'footnote'));
    if(state.capabilities?.automatic_registration){root.append(el('p','本项由系统处理；如有遗漏，请使用“补充漏检”框选。'));return;}
    const controls=el('div',null,'form-actions');
    if(item.state!=='confirmed'&&item.state!=='merged')controls.append(button('确认为仪表',()=>review(item,'confirm'),'primary'));
    if(item.state!=='rejected'&&item.state!=='merged')controls.append(button('排除',()=>review(item,'reject')));
    if(['rejected','merged'].includes(item.state))controls.append(button('恢复待审',()=>review(item,'restore')));
    if(map().status==='invalidated')controls.querySelectorAll('button').forEach(n=>n.disabled=true);
    root.append(controls,button('查看 / 修正高清来源框',()=>{frameId=item.frame_id;selectedId=item.id;renderReview();showSource();}));
  }
  function sync() {
    const summary=map()?.overview;if(!summary)return;
    if(sceneId!==mapId){sceneId=mapId;ids=new Set();setMode('pan');fitScannedArea();}
    const visible=new Set(items().map(c=>c.id));
    ids=new Set([...ids].filter(id=>visible.has(id)));
    const invalid=map().status==='invalidated';
    $('overview-selected-count').textContent='已选 '+ids.size+' 项';
    $('overview-confirm').disabled=invalid||!ids.size||[...ids].some(id=>state.candidates.find(c=>c.id===id)?.state==='merged');
    $('overview-reject').disabled=invalid||!ids.size;
    $('overview-clear').disabled=!ids.size;
    $('overview-add').disabled=invalid||!summary.ring_projection;
    $('overview-confirm').hidden=Boolean(state.capabilities?.automatic_registration);
    $('overview-reject').hidden=false;
    $('overview-select').hidden=false;
    const projected=new Set([...svg.querySelectorAll('[data-candidate-id]')].map(n=>n.dataset.candidateId)).size;
    $('overview-visible-count').textContent='大图关联 '+projected+' / '+items().length+' 项'+(projected<items().length?'；其余可在高清来源中查看':'');
    svg.querySelectorAll('[data-candidate-id]').forEach(n=>{n.classList.toggle('selected',ids.has(n.dataset.candidateId));n.setAttribute('aria-pressed',String(ids.has(n.dataset.candidateId)));});
    applyView();inspector();if(typeof Workbench!=='undefined')Workbench.syncList();
  }
  function pick(item,event) {
    event.stopPropagation();
    if(suppressClick||mode==='add')return;
    if(event.ctrlKey||event.metaKey||event.shiftKey){if(ids.has(item.id))ids.delete(item.id);else if(ids.size<100)ids.add(item.id);else{toast('一次最多选择 100 项。',true);return;}}
    else ids=new Set([item.id]);
    selectCandidate(item);
  }
  async function batch(action) {
    if(!ids.size)return;
    const selected=typeof Workbench==='undefined'?[...ids]:Workbench.expandSelection([...ids]);
    if(selected.length>100){toast('所选候选关联超过 100 条观测，请分批排除。',true);return;}
    const result=await act('/api/review/many',{ids:selected,action},action==='confirm'?'选中仪表已确认并保存编号':'选中候选及其重复观测已排除，可恢复');
    if(result){ids.clear();renderOverview();}
  }
  function clearGesture() {
    if(gesture?.rect)gesture.rect.remove();gesture=null;
  }
  svg.addEventListener('wheel',event=>{if(!map()?.overview)return;event.preventDefault();changeZoom(Math.exp(-Math.max(-180,Math.min(180,event.deltaY))*.004),event);},{passive:false});
  svg.addEventListener('dblclick',event=>{if(mode==='pan')changeZoom(2,event);});
  svg.addEventListener('pointerdown',event=>{
    if(event.button!==0||busy||!map()?.overview||dialog.open)return;
    const p=point(svg,event);if(!p)return;
    gesture={id:event.pointerId,start:p,client:[event.clientX,event.clientY],cx,cy,moved:false,add:event.ctrlKey||event.metaKey||event.shiftKey};
    if(mode!=='pan'){svg.setPointerCapture(event.pointerId);event.preventDefault();gesture.rect=svgEl('rect',{class:'overview-draw-box',x:p[0],y:p[1],width:0,height:0});svg.append(gesture.rect);}
  });
  svg.addEventListener('pointermove',event=>{
    if(!gesture||gesture.id!==event.pointerId)return;
    const dx=event.clientX-gesture.client[0],dy=event.clientY-gesture.client[1];
    gesture.moved=gesture.moved||Math.hypot(dx,dy)>4;
    if(mode==='pan'){
      if(gesture.moved&&!svg.hasPointerCapture(event.pointerId))svg.setPointerCapture(event.pointerId);
      const r=svg.getBoundingClientRect(),v=svg.viewBox.baseVal;
      cx=gesture.cx-dx*v.width/r.width;cy=gesture.cy-dy*v.height/r.height;applyView();
    }else{
      const p=point(svg,event),s=map().overview,b=overviewRect(gesture.start,p,s.width,s.height);
      ['x','y','width','height'].forEach((k,i)=>gesture.rect.setAttribute(k,[b[0],b[1],b[2]-b[0],b[3]-b[1]][i]));
    }
  });
  svg.addEventListener('pointerup',async event=>{
    if(!gesture||gesture.id!==event.pointerId)return;
    const g=gesture,p=point(svg,event),s=map().overview,b=overviewRect(g.start,p,s.width,s.height),kind=mode;
    clearGesture();
    if(g.moved){suppressClick=true;setTimeout(()=>{suppressClick=false;},0);}
    if(kind==='pan'||!g.moved||b[2]-b[0]<2||b[3]-b[1]<2)return;
    if(kind==='select'){
      if(!g.add)ids.clear();
      const matched=new Set();
      svg.querySelectorAll('[data-candidate-id]').forEach(n=>{const r=n.getBBox();if(r.x<b[2]&&r.x+r.width>b[0]&&r.y<b[3]&&r.y+r.height>b[1])matched.add(n.dataset.candidateId);});
      if(new Set([...ids,...matched]).size>100){toast('一次最多选择 100 项，请缩小框选范围。',true);sync();return;}
      matched.forEach(id=>ids.add(id));sync();
    }else await propose(b);
  });
  svg.addEventListener('pointercancel',clearGesture);
  svg.addEventListener('lostpointercapture',()=>{if(gesture)clearGesture();});
  async function propose(box) {
    if(busy)return;busy=true;
    try{
      if(state.capabilities?.automatic_registration){
        await request('/api/annotations/from-overview',{revision:state.revision,map_id:mapId,overview_sha256:map().overview.sha256,bbox:box});
        setMode('pan');await load(true);toast(map()?.review_before_commissioning?'补漏已保存，审核完成后统一建档':'补漏已加入自动建档队列');return;
      }
      proposal=await request('/api/overview/sources',{revision:state.revision,map_id:mapId,overview_sha256:map().overview.sha256,bbox:box});
      $('overview-source-choice').replaceChildren();
      proposal.choices.forEach((choice,i)=>option($('overview-source-choice'),String(i),choice.title+' · '+choice.pose.pan+'°'));
      dialog.showModal();setSource(0);
    }catch(error){proposal=null;toast(error.message,true);}
    finally{busy=false;}
  }
  function sourcePreview() {
    const canvas=$('overview-source-svg');
    canvas.querySelector('rect')?.remove();
    canvas.append(svgEl('rect',{x:sourceBox[0],y:sourceBox[1],width:sourceBox[2]-sourceBox[0],height:sourceBox[3]-sourceBox[1],class:'overview-source-box'}));
    $('overview-source-crop').replaceChildren(thumbnail({frame_id:source.frame_id,bbox:sourceBox}));
    $('overview-source-status').textContent='蓝框 '+sourceBox.map(Math.round).join(', ')+' px；可在左侧原图直接拖框修正。';
  }
  function setSource(index) {
    source=proposal.choices[index];sourceBox=[...source.bbox];sourceReady=false;
    $('overview-source-save').disabled=true;
    const canvas=$('overview-source-svg');canvas.setAttribute('viewBox','0 0 '+source.width+' '+source.height);
    canvas.replaceChildren(svgEl('image',{href:'/images/'+source.image,width:source.width,height:source.height}));sourcePreview();
    let image=imageCache.get(source.image);if(!image){image=new Image();image.src='/images/'+source.image;imageCache.set(source.image,image);}
    const choice=source;
    const ready=()=>{if(source!==choice||!dialog.open)return;sourceReady=true;$('overview-source-save').disabled=false;};
    if(image.complete&&image.naturalWidth)ready();else{image.addEventListener('load',ready,{once:true});image.addEventListener('error',()=>{if(source===choice)$('overview-source-status').textContent='高清原图加载失败，请换一个来源或重新打开。';},{once:true});}
  }
  function closeSource(){dialog.close();proposal=null;source=null;sourceStart=null;setMode('pan');}
  $('overview-source-choice').addEventListener('change',()=>setSource(Number($('overview-source-choice').value)));
  $('overview-source-svg').addEventListener('pointerdown',event=>{
    if(event.button!==0||!sourceReady)return;event.preventDefault();sourceStart=point($('overview-source-svg'),event);$('overview-source-svg').setPointerCapture(event.pointerId);
  });
  $('overview-source-svg').addEventListener('pointermove',event=>{
    if(!sourceStart||!source)return;sourceBox=overviewRect(sourceStart,point($('overview-source-svg'),event),source.width,source.height);sourcePreview();
  });
  $('overview-source-svg').addEventListener('pointerup',event=>{
    if(!sourceStart||!source)return;sourceBox=overviewRect(sourceStart,point($('overview-source-svg'),event),source.width,source.height);sourceStart=null;
    if(sourceBox[2]-sourceBox[0]<2||sourceBox[3]-sourceBox[1]<2){sourceBox=[...source.bbox];toast('框选范围太小，已恢复建议框。',true);}sourcePreview();
  });
  $('overview-source-svg').addEventListener('pointercancel',()=>{sourceStart=null;if(source){sourceBox=[...source.bbox];sourcePreview();}});
  $('overview-source-save').addEventListener('click',async()=>{
    if(!proposal||!sourceReady||busy||sourceStart)return;
    if(state.revision!==proposal.revision){toast('审核清单已变化，请关闭后重新补框。',true);return;}
    $('overview-source-save').disabled=true;
    const result=await act('/api/candidates/from-overview',{map_id:proposal.map_id,overview_sha256:proposal.overview_sha256,
      bbox:proposal.bbox,frame_id:source.frame_id,source_bbox:sourceBox,source_reviewed:true},'漏检补框已保存为待审核，请核对后确认',true);
    if(result){ids=new Set([result.result]);closeSource();renderReview();}else $('overview-source-save').disabled=false;
  });
  ['overview-source-close','overview-source-cancel'].forEach(id=>$(id).addEventListener('click',closeSource));
  dialog.addEventListener('cancel',event=>{event.preventDefault();closeSource();});
  ['pan','select','add'].forEach(key=>$('overview-'+key).addEventListener('click',()=>setMode(key)));
  $('overview-plus').addEventListener('click',()=>changeZoom(1.5));
  $('overview-minus').addEventListener('click',()=>changeZoom(1/1.5));
  $('overview-fit').addEventListener('click',fitScannedArea);
  $('overview-expand').addEventListener('click',()=>{
    const expanded=$('overview-card').classList.toggle('expanded');
    document.body.classList.toggle('overview-expanded',expanded);$('overview-expand').textContent=expanded?'收起大图':'展开大图';applyView();
  });
  $('overview-confirm').addEventListener('click',()=>batch('confirm'));
  $('overview-reject').addEventListener('click',()=>batch('reject'));
  $('overview-clear').addEventListener('click',()=>{ids.clear();sync();});
  ['overview-pending','overview-rejected'].forEach(id=>$(id).addEventListener('change',renderOverview));
  document.addEventListener('keydown',event=>{
    if(event.key==='Escape'&&!dialog.open){clearGesture();setMode('pan');if($('overview-card').classList.contains('expanded'))$('overview-expand').click();}
  });
  if(typeof ResizeObserver!=='undefined')new ResizeObserver(applyView).observe($('overview-viewport'));
  window.addEventListener('resize',applyView);
  $('source-details').open=false;
  if(state)setTimeout(()=>renderOverview(),0);
  function toggle(id){if(ids.has(id))ids.delete(id);else if(ids.size<100)ids.add(id);sync();}
  function focus(id){
    const c=candidates().find(c=>c.id===id);if(!c)return;
    ids=new Set([id]);selectCandidate(c);
    const box=c.overview_boxes?.[0];
    if(box){cx=(box[0]+box[2])/2;cy=(box[1]+box[3])/2;zoom=Math.max(2,Math.min(12,map().overview.width/(Math.max(1,box[2]-box[0])*4)));applyView();}
  }
  return {toggle,focus,items,has:id=>ids.has(id),pick,sync,active:()=>Boolean(gesture||dialog.open)};
})();
