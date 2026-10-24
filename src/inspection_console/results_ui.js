'use strict';
window.InspectionResults=(()=>{
  const Model=window.ResultGalleryModel,pictures=new Map(),saving=new Set();
  let latest=null,model=null,currentMap=null,archive=[],archiveLoaded=false,archiveLoading=false;
  let selectedHistory=null,historyKey=null,historyMap=null,historyRounds=[],comparison=false,workspaceChosen=false;
  let renameRow=null,view=null,paintVersion=0,drag=null;
  const status={running:'采集中',completed:'已完成',completed_with_errors:'部分未成功',failed:'失败',stopped:'已停止',interrupted:'已中断'};
  const time=value=>value?new Date(value).toLocaleString('zh-CN',{timeZone:'Asia/Shanghai',hour12:false}):'尚未拍摄';
  const editable=()=>latest&&!latest.schedule?.enabled&&!latest.runtime?.active_job&&!latest.manual_control?.active&&
    !latest.automatic_registration?.active&&!latest.scene_initialization?.active&&!saving.size;
  const lazy=typeof IntersectionObserver==='function'?new IntersectionObserver(entries=>entries.forEach(entry=>{
    if(entry.isIntersecting){lazy.unobserve(entry.target);entry.target.loadPicture?.();}
  }),{rootMargin:'180px'}):null;
  function picture(key){
    if(pictures.has(key)){const img=pictures.get(key);pictures.delete(key);pictures.set(key,img);return img;}
    const img=new Image();img.src='/images/'+key;pictures.set(key,img);
    while(pictures.size>12)pictures.delete(pictures.keys().next().value);
    return img;
  }
  function sourceText(row){
    if(row.snapshot)return row.failed?'该轮未成功采集':'第 '+row.sourceRound+' 轮采集图';
    if(row.kind==='provisional')return '本轮新图 · 待整轮确认';
    if(row.kind==='current')return '本轮新图';
    if(row.kind==='previous')return '第 '+row.sourceRound+' 轮旧图 · '+(row.failed?'本轮未成功':'本轮待采集');
    return row.failed?'本轮未成功，暂无历史成功图':'等待首次采集';
  }
  const rowKey=row=>JSON.stringify([row.id,row.name,row.kind,row.failed,row.photo,row.sourceRound,row.currentRound,row.snapshot]);
  function photoRect(row,img){return row.photo.nativeCrop?{x:0,y:0,width:img.naturalWidth,height:img.naturalHeight}:
    Model.cropRect(row.photo.bbox,img.naturalWidth,img.naturalHeight);}
  function onImage(img,success,failure){
    if(img.complete){if(img.naturalWidth)success();else failure();}
    else{img.addEventListener('load',success,{once:true});img.addEventListener('error',failure,{once:true});}
  }
  function imageView(row,open){
    const wrap=el('div',null,'result-image-wrap');
    if(!row.photo){wrap.append(el('p',sourceText(row),'result-placeholder'));return wrap;}
    const trigger=button('',open,'result-picture'),canvas=el('canvas'),note=el('span','载入图片…','result-image-note');
    trigger.setAttribute('aria-label','放大查看 '+row.name+' · '+time(row.photo.capturedAt));
    canvas.setAttribute('role','img');canvas.setAttribute('aria-label',row.name+'，'+sourceText(row));
    trigger.append(canvas,note);wrap.append(trigger);
    // Thumbnails use at most 640 px; only the viewer copies all native pixels.
    trigger.loadPicture=()=>{
      const img=picture(row.photo.source);
      onImage(img,()=>{
        const rect=photoRect(row,img);
        if(!rect){canvas.hidden=true;note.textContent='缺少配对裁剪框 · 点击查看原图信息';return;}
        const scale=Math.min(1,640/Math.max(rect.width,rect.height));
        canvas.width=Math.max(1,Math.round(rect.width*scale));canvas.height=Math.max(1,Math.round(rect.height*scale));
        const ctx=canvas.getContext('2d');
        if(!ctx){note.textContent='点击打开原图信息';return;}
        ctx.drawImage(img,rect.x,rect.y,rect.width,rect.height,0,0,canvas.width,canvas.height);
        note.textContent=rect.width+' × '+rect.height+' px · 点击放大';
      },()=>{canvas.hidden=true;note.textContent='载入失败 · 点击重试查看';pictures.delete(row.photo.source);});
    };
    if(lazy)lazy.observe(trigger);else trigger.loadPicture();
    return wrap;
  }
  function card(row,canEdit){
    const node=el('article',null,'result-tile'+(row.failed?' has-failure':''));node.dataset.instrumentId=row.id;
    node.dataset.key=rowKey(row)+':'+canEdit+':'+(canEdit?editable():false);
    const header=el('div',null,'result-tile-heading'),title=el('div');title.append(el('h3',row.name),el('span',row.id,'result-id'));header.append(title);
    if(canEdit&&row.asset){
      const edit=button('✎',()=>openRename(row),'result-rename');edit.setAttribute('aria-label','修改 '+row.name+' 的名称');
      edit.disabled=!editable();edit.title=editable()?'修改仪表名称':'采集或运动结束，并暂停周期后可改名';header.append(edit);
    }
    const open=()=>openViewer(row,canEdit?model.rows:historyRowsForViewer(row));
    node.append(header,imageView(row,open),el('p',sourceText(row),'result-source '+row.kind),el('time',time(row.photo?.capturedAt),'result-timestamp'));
    if(row.failed)node.append(el('p',row.snapshot?'此轮没有通过校验的新图。':'沿用旧图，拍摄时间保持不变。','result-error'));
    return node;
  }
  function dispose(node){
    if(lazy){if(node.loadPicture)lazy.unobserve(node);node.querySelectorAll?.('.result-picture').forEach(n=>lazy.unobserve(n));}
    node.remove();
  }
  function reconcile(root,rows,canEdit){
    const existing=new Map(Array.from(root.children).filter(n=>n.dataset.instrumentId).map(n=>[n.dataset.instrumentId,n]));
    rows.forEach((row,index)=>{
      const key=rowKey(row)+':'+canEdit+':'+(canEdit?editable():false),old=existing.get(row.id);
      const node=old?.dataset.key===key?old:card(row,canEdit);
      if(old&&old!==node)dispose(old);
      if(root.children[index]!==node)root.insertBefore(node,root.children[index]||null);
      existing.delete(row.id);
    });
    existing.forEach(dispose);
    Array.from(root.children).filter(n=>!n.dataset.instrumentId).forEach(dispose);
    if(!rows.length)root.append(el('p','暂无采集结果。完成建档并采集后，仪表图片会显示在这里。','muted'));
  }
  function openRename(row){
    if(!editable())return;
    renameRow={...row,mapId:currentMap};$('result-rename-id').textContent=row.id;
    $('result-rename-input').value=row.name;$('result-rename-save').disabled=false;
    $('result-rename-dialog').showModal();$('result-rename-input').focus();$('result-rename-input').select();
  }
  async function rename(){
    if(!renameRow||!editable())return toast('请先结束采集或运动，并暂停周期任务。',true);
    const row=renameRow,input=$('result-rename-input'),value=input.value.trim();
    if(!value)return toast('请输入仪表名称。',true);
    saving.add(row.id);$('result-rename-save').disabled=true;
    try{
      const fresh=await request('/api/state');
      if(row.mapId!==currentMap||!fresh.instruments.some(a=>a.id===row.id&&a.map_id===row.mapId))throw Error('场景已经切换，请重新选择仪表。');
      await request('/api/instruments/rename',{revision:fresh.revision,id:row.id,name:value});
      saving.delete(row.id);historyKey=null;$('result-rename-dialog').close();renameRow=null;
      await load(true);toast('仪表名称已保存。');
    }catch(error){toast(error.message,true);}
    finally{saving.delete(row.id);$('result-rename-save').disabled=!editable();}
  }
  function historyRowsForViewer(row){
    const historical=Model.build(latest,historyMap,archive),round=historical.history.find(r=>r.number===row.sourceRound);
    return round?Model.historyRows(round,historical.assets):[row];
  }
  function transform(){
    if(!view)return;
    $('result-viewer-canvas').style.transform='translate(-50%, -50%) translate('+view.x+'px, '+view.y+'px) scale('+view.scale+')';
    $('result-viewer-zoom').textContent=Math.round(view.scale*100)+'%';
  }
  function fit(){
    if(!view?.rect)return;
    const stage=$('result-viewer-stage');view.scale=Math.min((stage.clientWidth-32)/view.rect.width,(stage.clientHeight-32)/view.rect.height,1);
    view.scale=Math.max(.02,view.scale);view.x=0;view.y=0;transform();
  }
  function zoom(factor,point){
    if(!view?.rect)return;
    const scale=Math.max(.02,Math.min(16,view.scale*factor)),ratio=scale/view.scale;
    const x=point?.x||0,y=point?.y||0;view.x=x-(x-view.x)*ratio;view.y=y-(y-view.y)*ratio;view.scale=scale;transform();
  }
  function openViewer(row,rows){
    // Snapshot the chosen photo: background polling must not replace an image being inspected.
    view={row,rows:rows.slice(),mapId:row.asset?.map_id||currentMap,scale:1,x:0,y:0,rect:null};drag=null;
    const version=++paintVersion,p=row.photo,dialog=$('result-viewer');
    if(!dialog.open)dialog.showModal();
    $('result-viewer-title').textContent=row.name+' · '+row.id;
    $('result-viewer-meta').textContent=sourceText(row)+' · 拍摄时间 '+time(p?.capturedAt);
    $('result-viewer-detail').textContent=row.photo?.quality?.warnings?.includes('reachable_off_center')?'在可达位置采集，保留偏心构图。':'滚轮缩放 · 按住拖动 · ← → 切换仪表 · Esc 关闭';
    $('result-viewer-fresh').hidden=true;
    const index=rows.findIndex(r=>r.id===row.id);
    $('result-viewer-prev').disabled=index<=0;$('result-viewer-next').disabled=index<0||index>=rows.length-1;
    const canvas=$('result-viewer-canvas');canvas.hidden=true;canvas.width=1;canvas.height=1;
    $('result-viewer-download').disabled=true;$('result-viewer-message').hidden=false;
    $('result-viewer-message').textContent=p?'载入原始裁剪…':'此轮未取得通过校验的图片。';
    $('result-viewer-original').hidden=!p;$('result-viewer-original').href=p?'/images/'+p.image:'';
    if(!p)return;
    const img=picture(p.source);
    onImage(img,()=>{
      if(version!==paintVersion||!view)return;
      const rect=photoRect(row,img);
      if(!rect){$('result-viewer-message').textContent='缺少与本照片配对的裁剪框，可打开完整原图。';return;}
      canvas.width=rect.width;canvas.height=rect.height;const ctx=canvas.getContext('2d');
      if(!ctx){$('result-viewer-message').textContent='浏览器画布不可用，请打开完整原图。';return;}
      ctx.drawImage(img,rect.x,rect.y,rect.width,rect.height,0,0,rect.width,rect.height);
      canvas.setAttribute('aria-label',row.name+' · '+time(p.capturedAt));canvas.hidden=false;
      $('result-viewer-message').hidden=true;$('result-viewer-download').disabled=false;
      view.rect=rect;fit();$('result-viewer-stage').focus();
    },()=>{if(version===paintVersion){pictures.delete(p.source);$('result-viewer-message').textContent='图片未载入，请关闭后重试。';}});
  }
  function stepViewer(delta){
    if(!view)return;const index=view.rows.findIndex(r=>r.id===view.row.id)+delta;
    if(index>=0&&index<view.rows.length)openViewer(view.rows[index],view.rows);
  }
  function refreshNotice(){
    const row=view&&!view.row.snapshot&&view.mapId===currentMap?model.rows.find(r=>r.id===view.row.id):null;
    $('result-viewer-fresh').hidden=!row||!row.photo||rowKey(row)===rowKey(view.row);
  }
  function download(){
    if(!view?.rect||$('result-viewer-download').disabled)return;
    const row=view.row,source=$('result-viewer-canvas'),canvas=el('canvas');canvas.width=Math.max(320,source.width);canvas.height=source.height+58;
    const ctx=canvas.getContext('2d');if(!ctx)return;
    ctx.fillStyle='#f2f6fa';ctx.fillRect(0,0,canvas.width,canvas.height);ctx.drawImage(source,(canvas.width-source.width)/2,0);
    ctx.fillStyle='#18354b';ctx.font='600 14px sans-serif';ctx.fillText(row.name+' · '+row.id,10,source.height+21,canvas.width-20);
    ctx.font='12px sans-serif';ctx.fillText(time(row.photo.capturedAt)+' · 第 '+row.sourceRound+' 轮',10,source.height+44,canvas.width-20);
    canvas.toBlob(blob=>{
      if(!blob)return toast('导出失败，请重试。',true);
      const url=URL.createObjectURL(blob),link=el('a');link.href=url;
      link.download=(row.name+'_'+row.id).replace(/[<>:"/\\|?*\u0000-\u001f]/g,'_').slice(0,90)+'_'+row.photo.capturedAt.replace(/[:.]/g,'-')+'.png';
      document.body.append(link);link.click();link.remove();setTimeout(()=>URL.revokeObjectURL(url),2000);
    },'image/png');
  }
  function renderComparison(rounds,historical){
    const root=$('results-history-comparison'),table=el('table',null,'result-compare-table'),head=el('tr');head.append(el('th','仪表'));
    const columns=rounds.map(round=>({round,rows:Model.historyRows(round,historical.assets)}));
    columns.forEach(({round})=>{const th=el('th');th.append(el('strong',round.label),el('small',time(round.job.started_at)),el('small',status[round.job.status]||round.job.status));head.append(th);});
    const thead=el('thead');thead.append(head);table.append(thead);
    const ids=new Set(columns.flatMap(c=>c.rows.map(r=>r.id))),registry=historical.assets.filter(a=>ids.has(a.id)).map(a=>a.id);
    const body=el('tbody');[...registry,...[...ids].filter(id=>!registry.includes(id)).sort()].forEach(id=>{
      const tr=el('tr'),name=historical.assets.find(a=>a.id===id)?.name||id,th=el('th');th.append(el('strong',name),el('small',id));tr.append(th);
      columns.forEach(({rows})=>{const cell=el('td'),row=rows.find(r=>r.id===id);
        if(row){cell.append(imageView(row,()=>openViewer(row,rows)),el('small',row.failed?'本轮未成功':time(row.photo?.capturedAt)));}
        else cell.append(el('p','该轮未列入计划','muted'));
        tr.append(cell);
      });body.append(tr);
    });table.append(body);Array.from(root.children).forEach(dispose);root.append(table);
  }
  function renderHistory(){
    if(!model)return;
    if(!latest.maps?.some(m=>m.id===historyMap))historyMap=currentMap;
    const scenes=Model.scenes(latest,archive),sceneSelect=$('results-history-scene');
    const sceneOptions=JSON.stringify(scenes.map(s=>[s.id,s.historyLabel]));
    if(sceneSelect.dataset.options!==sceneOptions){sceneSelect.replaceChildren();scenes.slice().reverse().forEach(s=>option(sceneSelect,s.id,s.historyLabel));sceneSelect.dataset.options=sceneOptions;}
    sceneSelect.value=historyMap||'';
    const historical=Model.build(latest,historyMap,archive),scene=scenes.find(s=>s.id===historyMap),date=$('results-date').value;
    historyRounds=historical.history.filter(r=>!date||r.date===date);
    if(!historyRounds.some(r=>r.job.id===selectedHistory))selectedHistory=historyRounds.at(-1)?.job.id||null;
    const select=$('results-history-round'),optionsKey=JSON.stringify(historyRounds.map(r=>[r.job.id,r.label,r.job.status]));
    if(select.dataset.options!==optionsKey){select.replaceChildren();historyRounds.slice().reverse().forEach(r=>option(select,r.job.id,r.label+' · '+time(r.job.started_at)+' · '+(status[r.job.status]||r.job.status)));select.dataset.options=optionsKey;}
    select.disabled=!historyRounds.length;select.value=selectedHistory||'';
    const index=historyRounds.findIndex(r=>r.job.id===selectedHistory),round=historyRounds[index],rows=round?Model.historyRows(round,historical.assets):[];
    $('results-history-prev').disabled=index<=0;$('results-history-next').disabled=index<0||index>=historyRounds.length-1;
    $('results-history-grid').hidden=comparison;$('results-history-comparison').hidden=!comparison;
    $('results-compare-toggle').setAttribute('aria-pressed',String(comparison));
    const range=historyRounds.slice(Math.max(0,index-3),index+1);
    const key=historyMap+':'+date+':'+comparison+':'+selectedHistory+':'+JSON.stringify((comparison?range.flatMap(r=>Model.historyRows(r,historical.assets)):rows).map(rowKey));
    if(historyKey===key)return;historyKey=key;
    if(!round){
      reconcile($('results-history-grid'),[],false);$('results-history-grid').firstChild.textContent=date?'该日期没有已结束的巡检轮次。':'尚无已结束的巡检轮次。';
      Array.from($('results-history-comparison').children).forEach(dispose);$('results-history-summary').textContent='';return;
    }
    if(comparison)renderComparison(range,historical);else reconcile($('results-history-grid'),rows,false);
    $('results-history-summary').textContent='场景 '+(scene?.number||'')+' · '+(comparison?range.map(r=>r.label).join(' / '):round.label+' · '+(status[round.job.status]||round.job.status)+' · 成功 '+rows.filter(r=>r.kind==='current').length+' / '+rows.length)+' · '+time(round.job.started_at);
  }
  function workspace(value,remember=true){
    const mode=value==='setup'?'setup':'dashboard';document.body.dataset.workspace=mode;
    if(remember){workspaceChosen=true;try{window.localStorage?.setItem('inspection.workspace.v1',mode);}catch(_){}}
    $('workspace-dashboard').setAttribute('aria-pressed',String(mode==='dashboard'));$('workspace-setup').setAttribute('aria-pressed',String(mode==='setup'));
    $('workspace-hint').textContent=mode==='dashboard'?'看当前照片、放大核对、按轮查看历史':'扫描建图、排除误检、补漏和控制相机';
    if(mode==='dashboard'){
      document.body.classList.remove('advanced-mode');$('advanced-toggle').textContent='更多记录与设置';$('page-title').textContent='仪表采集看板';
    }else if(typeof setTab==='function')setTab(document.body.dataset.tab||'review');
    $('patrol-controls').open=mode==='setup';
    // Keep the single MJPEG element mounted; switching spaces never reopens the stream.
    if(mode==='setup')window.PanoramaWorkbench?.render();
  }
  function render(s,mapId){
    if(!s)return;latest=s;
    if(!workspaceChosen){workspace((s.instruments||[]).some(a=>a.map_id===mapId)?'dashboard':'setup',false);workspaceChosen=true;}
    if(!mapId)return;
    const switched=currentMap!==mapId;
    if(switched){currentMap=mapId;historyMap=mapId;historyKey=null;selectedHistory=null;$('results-date').value='';}
    const retained=new Map(archive.map(j=>[j.id,j]));(s.jobs||[]).filter(j=>j.status!=='running').forEach(j=>retained.set(j.id,j));archive=[...retained.values()];
    model=Model.build(s,mapId,archive);$('results-panel').hidden=false;
    const c=model.current,stateText=c.job?(status[c.job.status]||c.job.status):'尚未开始';
    $('results-current-summary').textContent=c.label+' · '+stateText+' · 已确认新图 '+model.completed+' / '+model.targets+(model.previews?' · '+model.previews+' 张待整轮确认':'');
    $('results-progress').max=Math.max(1,model.targets);$('results-progress').value=model.completed+model.previews;
    $('results-current-note').textContent='待采或失败时保留上一轮成功图。卡片上的时间始终是照片的实际拍摄时间。';
    $('dashboard-run-status').textContent=s.schedule?.enabled?'周期巡检已启用':s.runtime?.active_job?'任务执行中':'巡检已暂停';
    reconcile($('results-current-grid'),model.rows,true);refreshNotice();renderHistory();
    if(renameRow)$('result-rename-save').disabled=!editable();
  }
  async function loadArchive(){
    if(archiveLoaded||archiveLoading)return;archiveLoading=true;$('results-archive-state').textContent='读取历史…';
    try{
      const exported=await request('/api/export'),retained=new Map(archive.map(j=>[j.id,j]));
      (exported.job_history||[]).forEach(j=>retained.set(j.id,j));archive=[...retained.values()];archiveLoaded=true;
      $('results-archive-state').textContent='完整历史已载入';historyKey=null;if(latest)render(latest,currentMap);
    }catch(error){$('results-archive-state').textContent='历史读取失败，可重试';toast(error.message,true);}
    finally{archiveLoading=false;}
  }
  function tab(history){
    $('results-current').hidden=history;$('results-history').hidden=!history;
    $('results-current-tab').setAttribute('aria-selected',String(!history));$('results-history-tab').setAttribute('aria-selected',String(history));
    if(history){loadArchive();renderHistory();}
  }
  function historyStep(delta){const index=historyRounds.findIndex(r=>r.job.id===selectedHistory)+delta;if(index>=0&&index<historyRounds.length){selectedHistory=historyRounds[index].job.id;historyKey=null;renderHistory();}}
  function bindViewer(){
    $('result-viewer-close').addEventListener('click',()=>$('result-viewer').close());
    $('result-viewer').addEventListener('close',()=>{
      if(document.fullscreenElement===$('result-viewer-content'))document.exitFullscreen().catch(()=>{});
      view=null;drag=null;paintVersion++;$('result-viewer-canvas').width=1;$('result-viewer-canvas').height=1;
    });
    $('result-viewer-prev').addEventListener('click',()=>stepViewer(-1));$('result-viewer-next').addEventListener('click',()=>stepViewer(1));
    $('result-viewer-in').addEventListener('click',()=>zoom(1.25));$('result-viewer-out').addEventListener('click',()=>zoom(.8));$('result-viewer-fit').addEventListener('click',fit);
    $('result-viewer-native').addEventListener('click',()=>{if(view){view.scale=1;view.x=0;view.y=0;transform();}});
    $('result-viewer-download').addEventListener('click',download);
    $('result-viewer-fresh').addEventListener('click',()=>{const row=model?.rows.find(r=>r.id===view?.row.id);if(row&&view.mapId===currentMap)openViewer(row,model.rows);});
    $('result-viewer-fullscreen').addEventListener('click',async()=>{
      // The Fullscreen standard disallows requestFullscreen() on a dialog itself.
      try{if(document.fullscreenElement===$('result-viewer-content'))await document.exitFullscreen();else await $('result-viewer-content').requestFullscreen();}
      catch(_){toast('当前浏览器无法进入全屏，可以继续在大图窗口查看。',true);}
    });
    document.addEventListener('fullscreenchange',()=>{fit();$('result-viewer-fullscreen').textContent=document.fullscreenElement===$('result-viewer-content')?'退出全屏':'全屏';});
    window.addEventListener('resize',()=>{if(view)fit();});
    const stage=$('result-viewer-stage');
    stage.addEventListener('wheel',event=>{if(!view?.rect)return;event.preventDefault();const rect=stage.getBoundingClientRect();zoom(event.deltaY<0?1.15:1/1.15,{x:event.clientX-rect.left-rect.width/2,y:event.clientY-rect.top-rect.height/2});},{passive:false});
    stage.addEventListener('pointerdown',event=>{if(!view?.rect||event.button!==0)return;drag={id:event.pointerId,x:event.clientX,y:event.clientY,panX:view.x,panY:view.y};stage.setPointerCapture(event.pointerId);});
    stage.addEventListener('pointermove',event=>{if(!view||!drag||event.pointerId!==drag.id)return;view.x=drag.panX+event.clientX-drag.x;view.y=drag.panY+event.clientY-drag.y;transform();});
    const end=()=>{drag=null;};stage.addEventListener('pointerup',end);stage.addEventListener('pointercancel',end);stage.addEventListener('lostpointercapture',end);
    $('result-viewer').addEventListener('keydown',event=>{if(event.key==='ArrowLeft'){event.preventDefault();stepViewer(-1);}else if(event.key==='ArrowRight'){event.preventDefault();stepViewer(1);}});
  }
  document.addEventListener('DOMContentLoaded',()=>{
    $('workspace-dashboard').addEventListener('click',()=>workspace('dashboard'));$('workspace-setup').addEventListener('click',()=>workspace('setup'));
    try{const saved=window.localStorage?.getItem('inspection.workspace.v1');if(['dashboard','setup'].includes(saved))workspace(saved);}catch(_){}
    $('results-current-tab').addEventListener('click',()=>tab(false));$('results-history-tab').addEventListener('click',()=>tab(true));
    $('results-date').addEventListener('change',()=>{selectedHistory=null;historyKey=null;renderHistory();});
    $('results-history-round').addEventListener('change',event=>{selectedHistory=event.target.value;historyKey=null;renderHistory();});
    $('results-history-scene').addEventListener('change',event=>{historyMap=event.target.value;selectedHistory=null;historyKey=null;$('results-date').value='';renderHistory();});
    $('results-history-prev').addEventListener('click',()=>historyStep(-1));$('results-history-next').addEventListener('click',()=>historyStep(1));
    $('results-compare-toggle').addEventListener('click',()=>{comparison=!comparison;historyKey=null;renderHistory();});
    $('results-load-archive').addEventListener('click',()=>{archiveLoaded=false;loadArchive();});
    $('result-rename-form').addEventListener('submit',event=>{event.preventDefault();return rename();});
    $('result-rename-cancel').addEventListener('click',()=>$('result-rename-dialog').close());$('result-rename-dialog').addEventListener('close',()=>{renameRow=null;});
    bindViewer();if(state)render(state,mapId);loadArchive();
  });
  function showHistory(id){
    workspace('dashboard');
    if(latest?.maps?.some(m=>m.id===id)){historyMap=id;selectedHistory=null;historyKey=null;$('results-date').value='';renderHistory();}
    tab(true);
  }
  return {render,showHistory,workspace};
})();
