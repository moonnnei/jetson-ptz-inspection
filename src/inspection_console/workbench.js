'use strict';

// Convert clicks against the displayed picture, excluding object-fit black bars.
function liveImagePoint(rect,width,height,x,y){
  if(width<=0||height<=0||rect.width<=0||rect.height<=0)return null;
  const scale=Math.min(rect.width/width,rect.height/height);
  const w=width*scale,h=height*scale,left=rect.left+(rect.width-w)/2,top=rect.top+(rect.height-h)/2;
  const p=[(x-left)/w,(y-top)/h];
  return p.every(v=>v>=0&&v<=1)?p:null;
}

const Workbench=(()=>{
  let camera=null,polling=false,moving=false,corners=[],lastEpoch=null,listKey=null;
  let selectedBoxes=new Set(),selectedMap=null,focusedId=null,sending=false,pendingMove=null;
  function groups(){return map()?.review_groups||[];}
  function visibleIds(){
    const hidden=new Set(groups().flatMap(g=>g.alternatives||[]));
    return new Set(candidates().filter(c=>!hidden.has(c.id)).map(c=>c.id));
  }
  function expandSelection(ids){
    const result=new Set(ids);
    groups().forEach(g=>{const members=[g.candidate_id,...(g.alternatives||[])];if(members.some(id=>result.has(id)))members.forEach(id=>result.add(id));});
    return [...result];
  }
  const excluded=c=>groups().find(g=>g.candidate_id===c.id)?.excluded||['rejected','merged','auto_duplicate'].includes(c.state);
  const kept=()=>{const visible=visibleIds();return candidates().filter(c=>visible.has(c.id)&&!excluded(c));};
  const shown=()=>{const visible=visibleIds();return candidates().filter(c=>visible.has(c.id)&&($('overview-rejected').checked||!excluded(c)));};
  const blocked=()=>!state||state.runtime.active_job||state.schedule.enabled||
    state.scene_initialization?.active||state.automatic_registration?.active||
    state.automatic_registration?.queued&&!state.automatic_registration?.paused;
  function rangeText(){
    const describe=p=>p.pan.toFixed(1)+'° / '+p.tilt.toFixed(1)+'°';
    $('range-start-label').textContent=corners[0]?describe(corners[0]):'未记录';
    $('range-end-label').textContent=corners[1]?describe(corners[1]):'未记录';
    $('range-summary').textContent=$('scan-full-range').checked?'全范围：水平 0～350°，19 个视点，保持当前俯仰。':corners[0]&&corners[1]?
      '选定范围：水平 '+Math.min(...corners.map(p=>p.pan)).toFixed(1)+'～'+Math.max(...corners.map(p=>p.pan)).toFixed(1)+'°；俯仰 '+Math.min(...corners.map(p=>p.tilt)).toFixed(1)+'～'+Math.max(...corners.map(p=>p.tilt)).toFixed(1)+'°。':'移动镜头，把区域的两个对角位置依次置于画面中心并记录。';
  }
  async function mark(index){
    try{
      const info=await request('/api/camera/status');
      if(!info.available||info.active||blocked())throw Error('请等相机停稳后记录范围');
      corners[index]=info.pose;if(index===0&&corners[1])corners=corners.slice(0,1);
      rangeText();update(state);
      if(corners[0]&&corners[1]){
        const bounds={pan_min:Math.min(corners[0].pan,corners[1].pan),pan_max:Math.max(corners[0].pan,corners[1].pan),
          tilt_min:Math.min(corners[0].tilt,corners[1].tilt),tilt_max:Math.max(corners[0].tilt,corners[1].tilt)};
        const plan=await request('/api/initialization/plan',{revision:state.revision,bounds});
        $('range-summary').textContent+=' '+plan.scan.rows+' 行 × '+plan.scan.columns+' 列，共 '+plan.scan.expected_frames+' 个视点；扫描最长 6 分钟。';
      }
    }catch(e){toast(e.message,true);}
  }
  async function startScan(){
    let payload={review_first:true};
    if(!$('scan-full-range').checked){
      if(!corners[0]||!corners[1]){toast('先记录扫描范围的两个角点。',true);return null;}
      const bounds={pan_min:Math.min(corners[0].pan,corners[1].pan),pan_max:Math.max(corners[0].pan,corners[1].pan),
        tilt_min:Math.min(corners[0].tilt,corners[1].tilt),tilt_max:Math.max(corners[0].tilt,corners[1].tilt)};
      try{
        const p=await request('/api/initialization/plan',{revision:state.revision,bounds});
        payload.fingerprint=p.scan.fingerprint;
        $('range-summary').textContent=p.scan.rows+' 行 × '+p.scan.columns+' 列，共 '+p.scan.expected_frames+' 个视点；扫描后返回起点，先显示候选，等待审核。';
      }catch(e){toast(e.message,true);return null;}
    }
    return act('/api/initialization/start',payload,'扫描已开始，完成后先显示全景和候选列表。');
  }
  function syncList(){
    if(!state||!mapId)return;
    const root=$('workbench-candidates');if(!root)return;
    // Selection changes need a redraw; live status polling does not.
    if(selectedMap!==mapId){selectedMap=mapId;selectedBoxes.clear();focusedId=null;}
    const valid=new Set(shown().map(c=>c.id));selectedBoxes=new Set([...selectedBoxes].filter(id=>valid.has(id)));
    if(!valid.has(focusedId))focusedId=null;
    const key=mapId+':'+JSON.stringify(groups())+':'+candidates().map(c=>c.id+':'+c.state+':'+c.bbox.join(',')).join('|')+':'+[...selectedBoxes].join(',')+':'+focusedId+':'+$('overview-rejected').checked;
    if(listKey===key)return;listKey=key;root.replaceChildren();
    const items=shown();$('workbench-count').textContent=kept().length+' 个保留目标';
    if(!items.length){root.append(el('p','当前没有保留候选，可在全景上框选补漏。','muted'));return;}
    items.forEach(c=>{
      const card=el('div',null,'workbench-candidate'+(selectedBoxes.has(c.id)?' selected':'')+(focusedId===c.id?' focused':''));
      card.dataset.candidateId=c.id;
      card.addEventListener('click',event=>{if(!event.target.closest('button,input'))focus(c.id);});
      const input=el('input');input.type='checkbox';input.checked=selectedBoxes.has(c.id);
      input.setAttribute('aria-label','选择排除 '+candidateName(c));
      input.addEventListener('change',()=>toggle(c.id));
      const picture=button('',()=>focus(c.id),'candidate-picture');picture.append(thumbnail(c));
      picture.setAttribute('aria-label','在全景定位 '+candidateName(c));picture.title='点击在全景中显示编号框';
      const content=el('div');content.append(button(candidateName(c)+(c.instrument_id?' · '+c.instrument_id:''),()=>focus(c.id),'candidate-locate'),
        el('small',excluded(c)?'已排除':c.state==='pending'?'默认保留':labels[c.state]||c.state));
      const count=groups().find(g=>g.candidate_id===c.id)?.observation_count||1;
      if(count>1)content.append(el('small','已合并 '+count+' 条重复观测'));
      content.append(button(excluded(c)?'恢复':'排除',()=>exclude([c.id],excluded(c)?'restore':'reject'),'row-exclude'));
      card.append(input,picture,content);root.append(card);
    });
  }
  function update(s){
    if(!s)return;
    const busyNow=!!blocked()||moving||sending;
    $('commission-reviewed').disabled=busyNow||!mapId||!s.capabilities.automatic_registration||!kept().some(c=>c.state==='pending');
    $('initialize-start').disabled=busyNow||!s.scene_initialization?.available||(!$('scan-full-range').checked&&(!corners[0]||!corners[1]));
    ['range-start','range-end'].forEach(id=>$(id).disabled=busyNow||!s.manual_control?.available);
    if(!corners[0])$('range-end').disabled=true;
    renderSchedule();
    if(busyNow){$('run-once').disabled=true;$('arm').disabled=true;}
    document.querySelectorAll('[data-camera-command]').forEach(b=>b.disabled=!!blocked()||!camera?.available||!!camera?.stop_error);
    if($('scene-exclude'))$('scene-exclude').disabled=!selectedBoxes.size;
    if($('scene-selection'))$('scene-selection').textContent=selectedBoxes.size?'已选 '+selectedBoxes.size+' 项':'默认保留所有框；排除误检，画框补漏。';
    syncList();
  }
  async function move(command,point){
    if(blocked()){toast('自动任务正在运行，点击停止后即可手动控制。',true);return;}
    if(!camera?.available)return;
    pendingMove={command,point};
    if(sending)return;
    sending=true;
    try{while(pendingMove){const next=pendingMove;pendingMove=null;await sendMove(next.command,next.point);}}
    catch(e){toast(e.message,true);await poll();}
    finally{sending=false;update(state);}
  }
  async function sendMove(command,point){
    if(command==='center'){
      camera=await request('/api/camera/status');
      if(!window.LiveView?.ready()||!camera.view_token){toast('等待新的实时画面后再点击。',true);return;}
    }
    moving=true;update(state);
    try{
      const payload={revision:state.revision,command,step:Number($('manual-step').value)};
      if(point){
        // The backend is calibrated to the upright, rotated camera picture.
        const p=window.LiveView?.rotation()===180?point:[1-point[0],1-point[1]];
        Object.assign(payload,{x:p[0],y:p[1],view_token:camera.view_token});
      }
      const result=await request('/api/camera/move',payload);
      $('camera-status').textContent=result.limited?'正在移动；已到达部分机械边界。':'正在移动…';
      camera.active=true;await poll();
    }catch(e){moving=false;toast(e.message,true);await poll();}
  }
  async function poll(){
    if(polling||document.hidden)return;polling=true;
    try{
      const before=camera?.active;camera=await request('/api/camera/status');
      moving=!!camera.active;
      if(camera.pose)$('camera-pose').textContent='水平 '+camera.pose.pan.toFixed(1)+'° · 俯仰 '+camera.pose.tilt.toFixed(1)+'° · '+camera.pose.zoom.toFixed(1)+'×';
      // A PTZ command never closes or refreshes the video connection.
      lastEpoch=camera.epoch;
      const r=camera.result;
      $('camera-status').textContent=camera.stop_error?'相机停止尚未确认，控制已阻止':!camera.available?'相机控制未连接':camera.active?'移动中；可立即输入新指令替换当前动作':r?.status==='failed'?'本次移动失败，已请求停止':r?.limited?'已到机械边界，无法继续向此方向移动':'点击实时画面回中；方向箭头控制镜头视线。';
      update(state);
    }catch(e){moving=false;$('camera-status').textContent='相机状态暂时不可用';}
    finally{polling=false;}
  }
  function focus(id,{scroll=false}={}){
    if(!shown().some(c=>c.id===id))return;
    focusedId=id;listKey=null;syncList();window.PanoramaWorkbench?.focus(id);
    if(scroll)Array.from($('workbench-candidates').children).find(n=>n.dataset.candidateId===id)?.scrollIntoView({block:'nearest'});
  }
  function toggle(id){if(selectedBoxes.has(id))selectedBoxes.delete(id);else selectedBoxes.add(id);focus(id);update(state);window.PanoramaWorkbench?.render();}
  async function stopWork(){
    pendingMove=null;
    await request('/api/schedule/stop',{revision:state.revision});
    for(let i=0;i<40;i++){
      await load(true);
      if(!blocked()&&!state.manual_control?.active){moving=false;return;}
      await new Promise(resolve=>setTimeout(resolve,300));
    }
    throw Error('已请求停止，后台正在退出；画面和已有记录会保留。');
  }
  async function exclude(ids,action='reject'){
    if(!ids.length)return;
    try{
      if(blocked())await stopWork();
      await load();
      const result=await act('/api/review/many',{ids,action},action==='reject'?'已排除目标及其全部重复观测。':'已恢复目标。');
      if(result){selectedBoxes.clear();listKey=null;update(state);window.PanoramaWorkbench?.render();}
    }catch(e){toast(e.message,true);}
  }
  function bind(){
    // Keep advanced tools accessible, with a small default working surface.
    $('advanced-toggle').addEventListener('click',()=>{
      window.InspectionResults?.workspace('setup');
      const on=document.body.classList.toggle('advanced-mode');$('advanced-toggle').textContent=on?'返回简洁工作台':'更多记录与设置';
      if(!on)setTab('review');else renderReview();
    });
    const patrol=$('run-once').closest('article');$('quick-patrol').append(patrol);
    $('range-start').addEventListener('click',()=>mark(0));$('range-end').addEventListener('click',()=>mark(1));
    $('scan-full-range').addEventListener('change',()=>{rangeText();update(state);});
    document.querySelectorAll('[data-camera-command]').forEach(b=>b.addEventListener('click',()=>move(b.dataset.cameraCommand)));
    $('camera-stop').addEventListener('click',async()=>{
      try{await stopWork();await poll();}
      catch(e){toast(e.message,true);}
    });
    $('workflow-back').addEventListener('click',async()=>{
      try{await stopWork();Initialization.unfollow();selectedBoxes.clear();setTab('review');toast('已停止当前任务，可修改范围后重新扫描，已有照片和场景保留。');}
      catch(e){toast(e.message,true);}
    });
    $('range-clear').addEventListener('click',()=>{corners=[];rangeText();update(state);});
    $('live-image').addEventListener('click',event=>{
      if(document.body.dataset.workspace==='dashboard')return;
      if(!$('click-center').checked)return;
      const img=event.currentTarget,p=liveImagePoint(img.getBoundingClientRect(),img.naturalWidth,img.naturalHeight,event.clientX,event.clientY);
      if(p)move('center',p);
    });
    $('commission-reviewed').addEventListener('click',()=>act('/api/initialization/commission',{map_id:mapId},'审核完成，已开始为保留候选自动建立巡检位置。'));
    rangeText();poll();
    setInterval(()=>{if(moving)poll();},250);setInterval(()=>{if(!moving)poll();},1000);
  }
  document.addEventListener('DOMContentLoaded',bind);
  return {startScan,update,syncList,visibleIds,expandSelection,groups,shown,kept,toggle,focus,focused:()=>focusedId,
    has:id=>selectedBoxes.has(id),exclude,excludeSelected:()=>exclude([...selectedBoxes]),
    clearSelection:()=>{selectedBoxes.clear();focusedId=null;listKey=null;update(state);window.PanoramaWorkbench?.render();},stopWork};
})();
