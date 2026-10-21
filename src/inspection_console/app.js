'use strict';
const $ = id => document.getElementById(id);
const token = document.querySelector('meta[name="control-token"]').content;
const labels = {pending:'待审核',confirmed:'已确认',rejected:'已排除',merged:'已合并'};
const actionLabels = {import_map:'导入场景',review_confirm:'确认仪表',review_reject:'排除候选',review_restore:'恢复待审核',review_merge:'合并重复候选',review_bbox:'修正检测框',candidate_add:'人工补框',instrument_update:'更新仪表',map_invalidated:'场景失效',schedule_configure:'保存巡检配置',schedule_stop:'暂停巡检',schedule_arm:'启用巡检',restart_recovery:'重启恢复'};
let state = null, mapId = null, frameId = null, selectedId = null;
let drawMode = null, drawStart = null, drawRect = null, busy = false, toastTimer;
const imageCache = new Map();
function el(tag, text, cls) {
  const node = document.createElement(tag);
  if (text !== undefined && text !== null) node.textContent = text;
  if (cls) node.className = cls;
  return node;
}
function svgEl(tag, attrs) {
  const node = document.createElementNS('http://www.w3.org/2000/svg', tag);
  Object.entries(attrs).forEach(([key,value]) => node.setAttribute(key, value));
  return node;
}
function toast(message, error = false) {
  $('toast').textContent = message;
  $('toast').className = error ? 'error' : '';
  $('toast').hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => {$('toast').hidden = true;}, error ? 8000 : 3500);
}
async function request(path, data) {
  const options = data === undefined ? {} : {method:'POST',headers:{'Content-Type':'application/json','X-Control-Token':token},body:JSON.stringify(data)};
  const response = await fetch(path, options);
  const body = await response.json();
  if (!response.ok) throw new Error(response.status === 409 ? '数据已更新或任务正在执行，请刷新后重试。' : body.error || '请求失败');
  return body;
}
async function act(path, data, message, selectResult = false) {
  if (busy) return;
  busy = true;
  document.body.setAttribute('aria-busy','true');
  try {
    const result = await request(path, {...data,revision:state.revision});
    if (selectResult) selectedId = result.result;
    await load(true);
    if (message) toast(message);
    return result;
  } catch (error) {
    await load(true).catch(() => {});
    toast(error.message, true);
  } finally {
    busy = false;
    document.body.removeAttribute('aria-busy');
  }
}
function map() {return state.maps.find(item => item.id === mapId);}
function frame() {return state.frames.find(item => item.id === frameId);}
function candidates() {return state.candidates.filter(item => item.map_id === mapId);}
function reviewCandidates() {return candidates().filter(c=>!map()?.panorama_detection||!$('panorama-only').checked||c.panorama_observations?.length||c.source.startsWith('human_'));}
function selected() {return state.candidates.find(item => item.id === selectedId);}
function candidateName(item) {return 'C' + String(candidates().findIndex(c => c.id === item.id) + 1).padStart(2,'0');}
function stamp(value) {return value ? new Date(value).toLocaleString('zh-CN', {hour12:false}) : '未记录拍摄时间';}
function button(label, fn, cls) {
  const node = el('button',label,cls);
  node.type = 'button';
  node.addEventListener('click', event => {event.stopPropagation();fn();});
  return node;
}
function option(select, value, label) {const node = el('option',label);node.value=value;select.append(node);}
function selectCandidate(item) {
  selectedId = item.id;
  if (frameId !== item.frame_id) frameId = item.frame_id;
  setDrawMode(null);
  renderReview();
}
function setTab(name) {
  const titles = {review:'场景与候选',assets:'仪表清单',plan:'巡检计划',history:'记录与追溯'};
  Object.keys(titles).forEach(key => {
    $(key + '-panel').hidden = key !== name || !mapId;
    document.querySelector('[data-tab="' + key + '"]').classList.toggle('active',key===name);
  });
  $('page-title').textContent = titles[name];
  document.body.dataset.tab = name;
}
async function load(force = false) {
  const fresh = await request('/api/state');
  const changed = !state || state.revision !== fresh.revision;
  state = fresh;
  if(changed)$('reference-result').hidden=true;
  $('connection').textContent = '已保存 · 修订 ' + state.revision + ' · 审核模式';
  if (!changed && !force) return;
  if (!state.maps.some(item => item.id === mapId)) mapId = state.maps.length ? state.maps[state.maps.length - 1].id : null;
  if (!state.frames.some(item => item.id === frameId && item.map_id === mapId)) frameId = reviewCandidates()[0]?.frame_id || state.frames.find(item => item.map_id === mapId)?.id || null;
  if (!reviewCandidates().some(item => item.id === selectedId)) selectedId = reviewCandidates().find(item => item.frame_id === frameId)?.id || null;
  $('map-select').replaceChildren();
  state.maps.forEach(item => option($('map-select'), item.id, item.title));
  $('map-select').value = mapId || '';
  $('empty').hidden = !!mapId;
  $('map-status').textContent = map()?.status === 'invalidated' ? '场景已失效' : map()?.provenance==='bounded_scan_capture' ? '有限扫描视图 · 待回访验证' : '历史图像 · 待回访验证';
  $('provenance-note').textContent = map()?.provenance==='bounded_scan_capture' ? '当前是有限扫描的原始视图索引；尚未拼接为全景，裁剪图不代表逐表居中或放大采集。拍摄时间为电脑收到图片的时间。' : '当前展示历史现场图像及已有模型候选。裁剪预览来自同一张原图，不代表已完成云台放大采集或全景拼接。';
  if(map()?.overview)$('provenance-note').textContent=map().overview.source_mapping?'点击全景候选查看高清来源图并人工复审。确认后还需为每块仪表建立采集位置，才能定期回访；全景中的位置关联为近似结果。':'总览由来源视图配准生成，仅用于位置浏览；检测框与补框均保存在高清来源图中。裁剪预览不代表逐表放大拍摄。';
  $('map-date').textContent = map() ? '建档 ' + stamp(map().created_at) : '';
  setTab(document.body.dataset.tab || 'review');
  renderReview();
  renderAssets();
  renderSchedule();
  renderHistory();
}
function renderReview() {
  if (!frame()) return;
  renderOverview();
  const items = candidates();
  const counts = [ ['全部候选',items.length,''], ['待审核',items.filter(i=>i.state==='pending').length,''], ['已确认',items.filter(i=>i.state==='confirmed').length,'green'], ['排除 / 合并',items.filter(i=>['rejected','merged'].includes(i.state)).length,''] ];
  $('stats').replaceChildren();
  counts.forEach(([label,count,cls])=>{const box=el('div',null,'stat '+cls);box.append(el('span',label),el('strong',count));$('stats').append(box);});
  $('frame-select').replaceChildren();
  state.frames.filter(item=>item.map_id===mapId).forEach(item=>option($('frame-select'),item.id,item.title+(map().overview&&!item.to_overview&&!map().overview.source_mapping?' · 未配准':'')));
  $('frame-select').value = frameId;
  const f = frame();
  $('panorama-only').disabled=!map()?.panorama_detection;
  $('image-description').textContent = '拍摄 ' + stamp(f.captured_at);
  $('image-info').textContent = f.width+' × '+f.height+' px · 原图保留 · '+(f.pose ? '历史拍摄角度 '+f.pose.pan+'° / '+f.pose.tilt+'° / '+f.pose.zoom+'×' : '拍摄角度未绑定');
  $('original').href = '/images/' + f.image;
  const scene = $('scene');
  scene.setAttribute('viewBox','0 0 '+f.width+' '+f.height);
  scene.replaceChildren(svgEl('image',{href:'/images/'+f.image,width:f.width,height:f.height}));
  const visible = reviewCandidates().filter(item=>item.frame_id===frameId && ($('show-rejected').checked || !['rejected','merged'].includes(item.state)));
  visible.forEach(item => {
    const b = item.bbox;
    const rect = svgEl('rect',{x:b[0],y:b[1],width:b[2]-b[0],height:b[3]-b[1],class:'target '+item.state+(item.id===selectedId?' selected':''),tabindex:'0','aria-label':candidateName(item)+' '+labels[item.state]});
    rect.addEventListener('click',()=>{if(!drawMode)selectCandidate(item);});
    rect.addEventListener('keydown',e=>{if(e.key==='Enter')selectCandidate(item);});
    scene.append(rect);
    const label=svgEl('text',{x:b[0]+8,y:Math.max(b[1]+f.width/65,f.width/65),'font-size':Math.max(16,f.width/75)});
    label.textContent=candidateName(item);scene.append(label);
  });
  $('candidate-count').textContent = visible.length;
  $('candidates').replaceChildren();
  if(!visible.length)$('candidates').append(el('p','本视图没有待显示候选。可手动补框，或显示已排除项。'));
  visible.forEach(item=>renderCandidate(item));
  renderDetail();
  const invalid = map().status === 'invalidated';
  $('draw-add').disabled = invalid;
  $('draw-edit').disabled = invalid || !selected() || selected().state==='merged';
}
function renderOverview() {
  const summary=map()?.overview;
  $('overview-card').hidden=!summary;
  if(!summary)return;
  const ring=Boolean(summary.source_mapping),mapped=summary.source_mapping==='estimated_cylindrical_review';
  $('overview-title').textContent=ring?'环视扫描总览':'局部全景总览';
  $('overview-hint').textContent=mapped?'点击候选，在下方高清原图中确认、排除或修正。虚线框的位置来自近似关联；点击只切换图像。':'请切换下方来源视图审核高清原图；总览与完整覆盖、云台回访标定需要分别核对。';
  const ringLabel=summary.kind==='pose_projected_ring_preview'?'按角度投影的环视预览 · '+summary.projected_views+' 张原图':summary.kind==='stitched_ring_preview'?'环视拼接预览 · '+summary.stitched_views+' / '+summary.total_views+' 个视图拼入':'来源视图索引 · '+summary.total_views+' 张原图';
  $('overview-status').textContent=ring?ringLabel+' · '+summary.width+' × '+summary.height+' px'+(mapped?' · 候选待人工复审':'；请在来源视图中审核'):summary.registered_views+' / '+summary.total_views+' 个视图已配准 · '+summary.width+' × '+summary.height+' px 总览';
  const canvas=$('overview');canvas.replaceChildren();
  canvas.setAttribute('viewBox','0 0 '+summary.width+' '+summary.height);
  canvas.append(svgEl('image',{href:'/images/'+summary.image,width:summary.width,height:summary.height}));
  const warp=(h,x,y)=>{const d=h[2][0]*x+h[2][1]*y+h[2][2];return [(h[0][0]*x+h[0][1]*y+h[0][2])/d,(h[1][0]*x+h[1][1]*y+h[1][2])/d];};
  reviewCandidates().filter(c=>$('show-rejected').checked||!['rejected','merged'].includes(c.state)).forEach(item=>{
    const f=state.frames.find(f=>f.id===item.frame_id),h=f?.to_overview;
    const [x,y,r,b]=item.bbox;
    const shapes=h?[[[x,y],[r,y],[r,b],[x,b]].map(p=>warp(h,...p))]:(item.overview_boxes||[]).map(([l,t,r,b])=>[[l,t],[r,t],[r,b],[l,b]]);
    shapes.forEach(points=>{
      const polygon=svgEl('polygon',{points:points.map(p=>p.join(',')).join(' '),class:'overview-target '+item.state+(!h?' estimated':'')+(item.id===selectedId?' selected':''),tabindex:0,role:'button','aria-label':'查看高清原图 '+candidateName(item)});
      polygon.addEventListener('click',()=>selectCandidate(item));
      polygon.addEventListener('keydown',event=>{if(event.key==='Enter'||event.key===' '){event.preventDefault();selectCandidate(item);}});
      canvas.append(polygon);
    });
  });
}
function thumbnail(item) {
  const canvas=el('canvas');canvas.width=440;canvas.height=250;
  canvas.setAttribute('aria-label',candidateName(item)+' 原图裁剪预览');
  const f=state.frames.find(x=>x.id===item.frame_id);
  const draw=img=>{
    const ctx=canvas.getContext('2d');const b=item.bbox;
    const w=b[2]-b[0],h=b[3]-b[1],scale=Math.min(430/w,240/h);
    ctx.fillStyle='#f0f3f5';ctx.fillRect(0,0,440,250);
    ctx.drawImage(img,b[0],b[1],w,h,(440-w*scale)/2,(250-h*scale)/2,w*scale,h*scale);
  };
  let img=imageCache.get(f.image);
  if(!img){img=new Image();img.src='/images/'+f.image;imageCache.set(f.image,img);}
  if(img.complete && img.naturalWidth)draw(img);else img.addEventListener('load',()=>draw(img),{once:true});
  return canvas;
}
function renderCandidate(item) {
  const box=el('article',null,'candidate'+(item.id===selectedId?' selected':''));
  box.addEventListener('click',()=>selectCandidate(item));
  box.append(thumbnail(item));
  const header=el('div',null,'candidate-head');
  header.append(el('strong',candidateName(item)+(item.instrument_id?' · '+item.instrument_id:'')),el('span',labels[item.state],'badge '+item.state));
  box.append(header);
  const confidence=item.confidence===null?'人工补框':(item.confidence*100).toFixed(1)+'% 检测分数';
  box.append(el('div',confidence+' · 同帧原图裁剪','meta'));
  const actions=el('div',null,'actions');
  if(['rejected','merged'].includes(item.state)) actions.append(button('恢复待审核',()=>review(item,'restore')));
  else {
    if(item.state!=='confirmed')actions.append(button('确认为仪表',()=>review(item,'confirm'),'confirm'));
    actions.append(button('排除',()=>review(item,'reject')));
  }
  if(map().status==='invalidated')actions.querySelectorAll('button').forEach(b=>b.disabled=true);
  box.append(actions);$('candidates').append(box);
}
function review(item, action, extra={}) {
  return act('/api/review',{id:item.id,action,...extra},{confirm:'已确认并保存仪表编号',reject:'已排除，可恢复',restore:'已恢复待审核',merge:'已合并，原始证据保留',bbox:'检测框已修正；回访坐标需重新验证'}[action]);
}
function renderDetail() {
  const panel=$('selected-detail');panel.replaceChildren();
  const item=selected();
  if(!item){panel.append(el('p','选择一个候选，或在原图中手动补框。'));return;}
  const asset=state.instruments.find(a=>a.id===item.instrument_id);
  panel.append(el('p',candidateName(item)+' · '+labels[item.state]+' · '+item.bbox.map(v=>Math.round(v)).join(', ')+' px'));
  const observation=item.panorama_observations?.at(-1);
  if(observation)panel.append(el('p','全景检测 '+(observation.panorama_confidence*100).toFixed(1)+'%'+(observation.source_confidence===null?' · 高清复检未匹配':' · 高清复检 '+(observation.source_confidence*100).toFixed(1)+'%')+'。检测分数不能替代人工确认。','footnote'));
  if(observation)panel.append(el('p',observation.source_association==='appearance_consistent'?'来源图外观一致性通过；仍需人工核对是否为同一块仪表。':'来源图仅按近似几何位置关联，外观尚未确认。','footnote'));
  const nameLabel=el('label','仪表名称','field');
  const input=el('input');input.type='text';input.maxLength=100;input.id='candidate-name';input.value=asset?.name || '';input.placeholder='例如：左侧电流表';nameLabel.append(input);panel.append(nameLabel);
  const controls=el('div',null,'form-actions');
  controls.append(button(item.state==='confirmed'?'保存名称':'命名并确认',()=>{
    const extra=input.value.trim()?{name:input.value.trim()}:{};review(item,'confirm',extra);
  },'primary'));
  if(item.state==='confirmed')controls.append(el('span',asset.id+' · 待回访标定','muted'));
  panel.append(controls);
  const others=candidates().filter(c=>c.id!==item.id&&!['rejected','merged'].includes(c.state));
  if(others.length && item.state!=='merged'){
    const row=el('div',null,'form-actions'),select=el('select');select.id='merge-target';select.setAttribute('aria-label','选择保留的候选');
    others.forEach(c=>option(select,c.id,'保留 '+candidateName(c)+(c.instrument_id?' / '+c.instrument_id:'')));
    row.append(select,button('合并到该候选',()=>review(item,'merge',{target_id:select.value})));
    panel.append(row,el('p','仅合并你确认是同一块仪表的重复候选；两块相邻仪表应分别保留。','footnote'));
  }
  if(map().status==='invalidated' || item.state==='merged')panel.querySelectorAll('button,input,select').forEach(n=>n.disabled=true);
}
function renderAssets() {
  const root=$('asset-list');root.replaceChildren();
  const assets=state.instruments.filter(a=>a.map_id===mapId);
  if(!assets.length){root.append(el('p','尚未确认仪表。请在候选审核中保留需要采集的表盘。'));return;}
  const wrap=el('div',null,'table-wrap'),table=el('table'),head=el('tr');
  ['计划采集','编号','名称','审核状态','云台回访','操作'].forEach(t=>head.append(el('th',t)));
  const thead=el('thead');thead.append(head);table.append(thead);const body=el('tbody');
  assets.forEach(asset=>{
    const candidate=state.candidates.find(c=>c.id===asset.candidate_id),row=el('tr');
    const enabled=el('input');enabled.type='checkbox';enabled.checked=asset.enabled;enabled.setAttribute('aria-label','采集 '+asset.id);
    enabled.disabled=candidate.state!=='confirmed'||map().status==='invalidated';
    const name=el('input');name.type='text';name.value=asset.name;name.maxLength=100;name.setAttribute('aria-label',asset.id+' 名称');
    const cell=el('td');cell.append(enabled);row.append(cell,el('td',asset.id));const nc=el('td');nc.append(name);row.append(nc);
    row.append(el('td',labels[candidate.state]),el('td',asset.pose_status==='verified'?'已标定':asset.pose_status==='invalidated'?'场景已失效':'待标定','asset-state'));
    const actions=el('td');const save=button('保存',()=>act('/api/instruments/update',{id:asset.id,name:name.value,enabled:enabled.checked},'仪表清单已保存'));
    save.disabled=map().status==='invalidated';actions.append(save);row.append(actions);body.append(row);
  });
  table.append(body);wrap.append(table);root.append(wrap);
  $('invalidate').disabled=map()?.status==='invalidated';
}
function renderSchedule() {
  const schedule=state.schedule;
  if(document.activeElement!==$('interval'))$('interval').value=schedule.interval_seconds;
  const selectedAssets=state.instruments.filter(a=>a.map_id===mapId&&a.enabled);
  $('schedule-status').replaceChildren(el('strong',schedule.enabled?'巡检已启用':'巡检未启用'));
  $('schedule-status').append(el('div','已选 '+selectedAssets.length+' 块仪表；间隔 '+schedule.interval_seconds+' 秒。'));
  $('schedule-status').append(el('div','当前阻碍：尚未接入实机采集，需完成场景定位与仪表回访标定。'));
  if(schedule.map_id && schedule.map_id!==mapId)$('schedule-status').append(el('div','已保存配置属于另一个场景；保存后将切换到当前场景。'));
  $('arm').disabled=!state.runtime.hardware_ready||map()?.status==='invalidated';
  $('reference-plan').disabled=!mapId||map()?.status==='invalidated';
}
function renderHistory() {
  $('jobs').replaceChildren();
  const jobs=state.jobs.filter(j=>j.map_id===mapId).slice().reverse();
  if(!jobs.length)$('jobs').append(el('p','尚未执行本流程的实机巡检。历史原图不会计入本轮采集记录。'));
  jobs.forEach(job=>{const box=el('div',null,'job');box.append(el('strong',job.id+' · '+job.status),el('p',stamp(job.started_at)+' · '+job.mode));job.results.forEach(r=>box.append(el('p',r.instrument_id+' · '+r.status+' · '+stamp(r.captured_at))));$('jobs').append(box);});
  $('audit').replaceChildren();
  state.audit.forEach(item=>{const row=el('div',null,'audit-row');row.append(el('time',stamp(item.timestamp)),el('b',actionLabels[item.action]||item.action),el('code',JSON.stringify(item.detail)));$('audit').append(row);});
}
function setDrawMode(mode) {
  drawMode=mode;drawStart=null;
  if(drawRect)drawRect.remove();drawRect=null;
  $('scene').classList.toggle('drawing',!!mode);
  $('cancel-draw').hidden=!mode;
  $('draw-hint').className=mode?'hint drawing':'hint';
  $('draw-hint').textContent=mode?(mode==='add'?'在原图上拖动，补充一个遗漏的表盘框；松开后保存为待审核。':'在原图上拖出修正后的完整表盘框；松开后保存。'):'点击候选框查看原图裁剪；人工确认后加入仪表清单。';
}
function imagePoint(event) {
  const rect=$('scene').getBoundingClientRect(),f=frame();
  return [Math.max(0,Math.min(f.width,(event.clientX-rect.left)/rect.width*f.width)),Math.max(0,Math.min(f.height,(event.clientY-rect.top)/rect.height*f.height))];
}
$('scene').addEventListener('pointerdown',event=>{
  if(!drawMode||busy||event.button!==0)return;
  event.preventDefault();drawStart=imagePoint(event);
  $('scene').setPointerCapture(event.pointerId);
  drawRect=svgEl('rect',{x:drawStart[0],y:drawStart[1],width:0,height:0,fill:'#6ed9ff33',stroke:'#44b5ef','stroke-width':3,'vector-effect':'non-scaling-stroke'});
  $('scene').append(drawRect);
});
$('scene').addEventListener('pointermove',event=>{
  if(!drawStart)return;const p=imagePoint(event);
  const b=[Math.min(drawStart[0],p[0]),Math.min(drawStart[1],p[1]),Math.abs(drawStart[0]-p[0]),Math.abs(drawStart[1]-p[1])];
  ['x','y','width','height'].forEach((key,i)=>drawRect.setAttribute(key,b[i]));
});
$('scene').addEventListener('pointerup',event=>{
  if(!drawStart)return;const p=imagePoint(event),mode=drawMode;
  const box=[Math.min(drawStart[0],p[0]),Math.min(drawStart[1],p[1]),Math.max(drawStart[0],p[0]),Math.max(drawStart[1],p[1])];
  setDrawMode(null);
  if(box[2]-box[0]<2||box[3]-box[1]<2){toast('框选范围太小，请重新拖动。',true);return;}
  if(mode==='add')act('/api/candidates/add',{frame_id:frameId,bbox:box},'已补充候选，请人工确认',true);
  else if(selected())review(selected(),'bbox',{bbox:box});
});
$('scene').addEventListener('pointercancel',()=>setDrawMode(null));
document.addEventListener('keydown',event=>{if(event.key==='Escape')setDrawMode(null);});
document.querySelectorAll('[data-tab]').forEach(node=>node.addEventListener('click',()=>setTab(node.dataset.tab)));
$('refresh').addEventListener('click',()=>load(true).catch(error=>toast(error.message,true)));
$('map-select').addEventListener('change',()=>{mapId=$('map-select').value;frameId=null;selectedId=null;$('reference-result').hidden=true;setDrawMode(null);load(true);});
$('frame-select').addEventListener('change',()=>{frameId=$('frame-select').value;selectedId=reviewCandidates().find(c=>c.frame_id===frameId)?.id;setDrawMode(null);renderReview();});
$('show-rejected').addEventListener('change',renderReview);
$('panorama-only').addEventListener('change',()=>{const candidate=reviewCandidates()[0];selectedId=candidate?.id||null;if(candidate)frameId=candidate.frame_id;setDrawMode(null);renderReview();});
$('draw-add').addEventListener('click',()=>setDrawMode('add'));
$('draw-edit').addEventListener('click',()=>setDrawMode('edit'));
$('cancel-draw').addEventListener('click',()=>setDrawMode(null));
$('stop').addEventListener('click',()=>act('/api/schedule/stop',{},'巡检已暂停；当前审核版本不发送云台命令'));
$('invalidate').addEventListener('click',()=>{
  if(confirm('将当前场景置为失效，并暂停该场景的仪表采集。历史图像与审核记录会保留，之后需要重新建图。'))act('/api/maps/invalidate',{map_id:mapId},'场景已失效，需重新建图');
});
$('schedule-form').addEventListener('submit',event=>{event.preventDefault();act('/api/schedule/configure',{map_id:mapId,interval_seconds:Number($('interval').value)},'巡检间隔已保存；执行保持暂停');});
$('arm').addEventListener('click',()=>act('/api/schedule/arm',{},'周期巡检已启用'));
$('reference-plan').addEventListener('click',async()=>{
  const requestedMap=mapId,revision=state.revision;
  try{
    const plan=await request('/api/plan/references',{map_id:requestedMap,revision});
    if(mapId!==requestedMap||state.revision!==revision){toast('清单已变化，请重新检查回访准备。',true);return;}
    const root=$('reference-result');root.replaceChildren();root.hidden=false;
    root.dataset.revision=String(revision);
    root.append(el('strong','回访参考 · 清单版本 '+revision));
    if(!plan.targets.length)root.append(el('p','当前没有已确认并启用的仪表。'));
    plan.targets.forEach(target=>{
      const ref=target.references[0],row=el('div',null,'job');
      row.append(el('strong',target.instrument_id+' · '+target.name));
      row.append(el('p','已绑定原图区域；共有 '+target.references.length+' 条已审核观测。'));
      if(ref.source_pose){const p=ref.source_pose;row.append(el('p','来源视点：水平 '+p.pan+'° / 俯仰 '+p.tilt+'° / '+p.zoom+' 倍。'));}
      else row.append(el('p','来源视点未记录，需要重新建立定位。'));
      const link=el('a','查看参考原图');link.href='/images/'+encodeURIComponent(ref.image);link.target='_blank';link.rel='noopener';row.append(link);
      root.append(row);
    });
    root.append(el('p','来源视点用于重新寻找仪表，不是已居中的采集位置。还需当前图像匹配、运动标定和现场采集验证；本次检查不转动相机。','footnote'));
  }catch(error){toast(error.message,true);}
});
$('route-plan').addEventListener('click',async()=>{
  const start=frame()?.pose||{pan:0,tilt:0,zoom:1};
  try{const result=await request('/api/plan/route',{map_id:mapId,start});$('route-result').replaceChildren();
    if(!result.stops.length&&!result.blocked.length)$('route-result').append(el('p','当前没有启用的仪表。'));
    result.stops.forEach((item,index)=>$('route-result').append(el('div',(index+1)+' → '+item.id+' '+item.name)));
    result.blocked.forEach(item=>$('route-result').append(el('div',item.id+'：待回访标定，未纳入可执行路线')));
    if(result.stops.length)$('route-result').append(el('p','预估角距离 '+result.angular_cost+'°；非全局最优，实际耗时待验证。'));
  }catch(error){toast(error.message,true);}
});
$('scan-form').addEventListener('submit',async event=>{
  event.preventDefault();const config={};new FormData(event.target).forEach((value,key)=>config[key]=Number(value));
  try{const result=await request('/api/plan/scan',config),root=$('scan-result');root.replaceChildren();
    root.append(el('p',result.rows+' 行 × '+result.columns+' 列，共 '+result.count+' 个视点。以下为假定视场角下的布点，不表示已扫描或覆盖验证。'));
    const svg=svgEl('svg',{viewBox:'0 0 780 250',class:'scan-map','aria-label':'扫描路线示意'});
    const point=p=>[45+(p.pan-config.pan_min)/Math.max(1,config.pan_max-config.pan_min)*680,210-(p.tilt-config.tilt_min)/Math.max(1,config.tilt_max-config.tilt_min)*165];
    svg.append(svgEl('polyline',{points:result.points.map(p=>point(p).join(',')).join(' ')}));
    result.points.forEach(p=>{const [x,y]=point(p);svg.append(svgEl('circle',{cx:x,cy:y,r:5}));const label=svgEl('text',{x:x+8,y:y-8});label.textContent=String(p.index);svg.append(label);});
    root.append(svg);const detail=el('p',result.points.map(p=>p.index+': '+p.pan+'° / '+p.tilt+'°').join('　 →　 '),'footnote');root.append(detail);
  }catch(error){toast(error.message,true);}
});
load().catch(error=>{$('connection').textContent='连接失败';toast(error.message,true);});
setInterval(()=>{if(!busy&&!drawMode)load().catch(()=>{$('connection').textContent='连接中断 · 本地记录仍保留';});},5000);
