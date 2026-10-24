'use strict';
window.FirstCapture = (() => {
  const node = id => document.getElementById(id);
  let context = null, plan = null, hardware = false, active = false, busy = false, polling = false;
  const statusLabels = {prepared:'准备中',running:'采集中',completed:'采集已完成',failed:'采集未完成',stopped:'已停止',idle:'等待准备'};
  async function request(path, data) {
    const options = data === undefined ? {} : {method:'POST',headers:{'Content-Type':'application/json',
      'X-Control-Token':document.querySelector('meta[name="control-token"]').content},body:JSON.stringify(data)};
    const response = await fetch(path,options), body = await response.json();
    if(!response.ok)throw new Error(body.error || '请求未完成，请刷新后检查任务状态');
    return body;
  }
  function controls() {
    node('first-prepare').disabled=busy||active||!context?.mapId;
    node('first-start').disabled=busy||active||!hardware||!plan?.ready_for_trial||!node('first-site-ready').checked;
    node('first-stop').disabled=!active;
  }
  function reset() {
    plan=null;node('first-site-ready').checked=false;controls();
  }
  function line(text) {
    const p=document.createElement('p');p.textContent=text;node('first-plan').append(p);
  }
  function renderPlan() {
    node('first-plan').replaceChildren();
    line('清单版本 '+plan.revision+' · '+plan.stops.length+' 块仪表 · 预计最长 '+Math.ceil(plan.budget_seconds/60)+' 分钟');
    line('顺序：'+(plan.stops.map(s=>s.id+' '+s.name).join(' → ')||'暂无可执行目标'));
    line('水平 '+plan.pan_limits.join('～')+'°，俯仰 '+plan.tilt_limits.join('～')+'°，倍率 '+plan.zoom_limits.join('～')+' 倍。每表成功后返回起点，失败即结束。');
    if(plan.mode==='adaptive')line('自适应采集：先尝试居中，受机械边界限制时保留可达位置；根据表盘边缘调整倍率，并标记偏心和画面完整性。');
    const reasons={oriented_scan_source_pose_required:'缺少带拍摄位置的扫描原图',scan_resolution_profile_mismatch:'扫描原图尺寸不符合相机配置',one_times_scan_reference_required:'需要一倍扫描原图'};
    plan.blocked.forEach(item=>line(item.id+'：'+(reasons[item.reason]||'来源资料需要补全')));
    if(!hardware)line('当前为计划预览。连接相机执行端后重新准备，可读取实际起点并开始采集。');
  }
  async function refreshStatus() {
    if(polling)return;
    polling=true;
    try {
      const status=await request('/api/first-capture/status');
      hardware=status.available;active=status.active;
      const progress=status.total_targets===undefined?'':' · 已完成 '+status.completed_targets+' / '+status.total_targets+' 块';
      const target=status.active_target?' · 当前 '+status.active_target:'';
      node('first-status').textContent=(statusLabels[status.status]||status.status)+progress+target;
      if(status.outcome?.next_round_ready)node('first-status').textContent+=' · 已建立采集参考，可检查回访计划';
      if(status.outcome?.status==='failed')node('first-status').textContent+=' · 请检查记录与现场条件';
    } catch(error) {
      hardware=false;node('first-status').textContent='连接中断，请先核对任务状态，勿重复启动';
    } finally {polling=false;controls();}
  }
  node('first-prepare').addEventListener('click',async()=>{
    if(busy||active||!context?.mapId)return;
    busy=true;reset();const selected={...context};
    try {
      const body={map_id:selected.mapId,revision:selected.revision,mode:'adaptive'};
      const result=hardware?await request('/api/first-capture/prepare',body):
        await request('/api/plan/first-capture',{...body,start:selected.start});
      if(context.mapId!==selected.mapId||context.revision!==selected.revision)throw new Error('清单已变化，请重新生成计划');
      plan=result.plan||result;renderPlan();
    } catch(error) {reset();node('first-plan').textContent=error.message;}
    finally {busy=false;controls();}
  });
  node('first-site-ready').addEventListener('change',controls);
  node('first-start').addEventListener('click',async()=>{
    if(node('first-start').disabled||!plan||!context)return;
    busy=true;controls();
    const selected=plan;
    try {
      await request('/api/first-capture/start',{map_id:selected.map_id,revision:selected.revision,
        fingerprint:selected.fingerprint,site_ready:node('first-site-ready').checked});
      reset();active=true;node('first-status').textContent='采集已启动，请保持相机底座和仪表位置不变';
    } catch(error) {reset();node('first-status').textContent=error.message+'；先检查状态，再重新准备。';}
    finally {busy=false;await refreshStatus();controls();}
  });
  node('first-stop').addEventListener('click',async()=>{
    if(!active||!context)return;
    try {
      await request('/api/first-capture/stop',{revision:context.revision});
      node('first-status').textContent='已请求停止，等待相机停止确认';
    } catch(error) {node('first-status').textContent=error.message;}
  });
  function update(state,mapId,start) {
    if(!context||context.mapId!==mapId||context.revision!==state.revision) {
      reset();node('first-plan').textContent='确认清单后生成计划；此操作不会转动相机。';
    }
    context={mapId,revision:state.revision,start:start||{pan:0,tilt:0,zoom:1}};
    controls();refreshStatus();
  }
  controls();
  return {update};
})();
