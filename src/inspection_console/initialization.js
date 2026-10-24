'use strict';
const Initialization=(()=>{
  let follow=false;
  const names={scanning:'环视采图',detecting:'发现仪表候选',mapping:'生成环视总览',importing:'保存新场景',
    commissioning:'逐表建立采集位置',completed:'初始化完成',completed_with_uncertain:'初始化结束，有待处理项',
    awaiting_review:'等待排除误检与补漏',awaiting_supplement:'等待补漏',stopped:'已停止',failed:'本次失败',interrupted:'重启后已暂停',timed_out:'执行超时'};
  function preferredMap(s){return follow?s.scene_initialization?.job?.map_id:null;}
  function unfollow(){follow=false;}
  function update(s){
    const info=s.scene_initialization;
    $('initialization-card').hidden=!info?.available;
    if(!info?.available)return;
    const a=s.automatic_registration||{},j=info.job;
    const blocked=info.active||s.schedule.enabled||s.runtime.active_job||a.active||a.queued||info.stop_error||a.stop_error;
    $('initialize-start').disabled=!!blocked;
    $('initialize-stop').disabled=!info.active&&!a.active&&!s.runtime.active_job&&!s.manual_control?.active;
    $('initialization-badge').textContent=j?(names[j.status]||j.status):'可建立新场景';
    const root=$('initialization-status');root.replaceChildren();
    const total=Math.max(1,j?.total_views||1),views=Math.min(total,j?.views||0),detected=Math.min(total,j?.detected_views??j?.views??0);
    let progress=0;
    if(j){
      if(j.status==='scanning')progress=60*views/total+25*(j.detected_views||0)/total;
      else if(j.status==='detecting')progress=60+25*detected/total;
      else if(j.status==='mapping')progress=90;
      else if(j.status==='importing')progress=95;
      else if(j.status==='commissioning')progress=100*(j.completed_candidates||0)/Math.max(1,j.scheduled_candidates||1);
      else if(['awaiting_review','awaiting_supplement','completed','completed_with_uncertain'].includes(j.status))progress=100;
    }
    $('scan-progress').value=Math.round(progress);
    $('scan-progress-label').textContent=j?(names[j.status]||j.status)+' · '+Math.round(progress)+'%':'尚未开始';
    const seconds=j?.started_at?Math.max(0,((info.active?Date.now():Date.parse(j.finished_at||j.updated_at))-Date.parse(j.started_at))/1000):0;
    $('scan-elapsed').textContent=j?'用时 '+Math.floor(seconds/60)+' 分 '+Math.floor(seconds%60)+' 秒':'';
    if(!j){root.append(el('p','点击后自动扫描和处理。现有场景继续保留。'));return;}
    root.append(el('strong',names[j.status]||j.status));
    if(j.status==='scanning'||j.status==='detecting')root.append(el('p','采图 '+views+' / '+total+'；检测 '+(j.detected_views??(j.status==='detecting'?views:0))+' / '+total+'。可随时停止并返回修改范围。'));
    if(j.status==='commissioning')root.append(el('p','已完成 '+(j.completed_candidates||0)+' / '+j.scheduled_candidates+' 个候选，正在自动定位、放大与回访核对。'));
    if(j.verified_instruments!==undefined)root.append(el('p','建立有效位置 '+j.verified_instruments+' 块；重复候选 '+j.duplicate_candidates+'；不确定 '+j.uncertain_candidates+'。'));
    if(j.deferred_candidates)root.append(el('p','本批还有 '+j.deferred_candidates+' 个候选未处理。保留在场景中，不能视为全部仪表均已找到。'));
    if(j.status==='awaiting_supplement')root.append(el('p','本轮没有检出候选。可放大新场景总览，圈出漏检表盘，系统会继续自动建档。'));
    if(j.ready_for_patrol)root.append(el('p',s.schedule.enabled?'周期执行中，可在巡检计划查看本批进度。':'采集位置已就绪，可进入巡检计划采集一轮或配置周期。'));
    if(j.error_type||j.failure_stage)root.append(el('p','停止阶段：'+(names[j.failure_stage]||j.failure_stage||'未知')+'。诊断记录已保留，可查看照片与现场条件后重新初始化。'));
    if(info.stop_error)root.append(el('p','相机停止尚未确认，新的运动已阻止。'));
  }
  function bind(){
    $('initialize-start').addEventListener('click',async()=>{
      follow=true;
      const result=typeof Workbench!=='undefined'?await Workbench.startScan():await act('/api/initialization/start',{review_first:true},'扫描已开始。');
      if(!result)follow=false;
    });
    $('initialize-stop').addEventListener('click',async()=>{
      try{await Workbench.stopWork();toast('已停止，可以直接修改或重试，无需刷新。');}catch(e){toast(e.message,true);}
    });
  }
  document.addEventListener('DOMContentLoaded',bind);
  return {update,preferredMap,unfollow};
})();
