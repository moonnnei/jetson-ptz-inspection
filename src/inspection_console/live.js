'use strict';
// One live connection; inactive/hidden pages release it. No control requests.
(() => {
  const node=id=>document.getElementById(id);
  const card=node('live-card'),image=node('live-image'),placeholder=node('live-placeholder');
  let mode='raw',paused=false,active=false,stopped=false,statusBusy=false;
  let timer=null,retryAt=0,currentStatus=null,lastRefresh=Date.now();
  window.LiveView={ready:()=>wanted()&&active&&mode==='raw'&&currentStatus?.available,
    rotation:()=>mode==='raw'?currentStatus?.rotation||0:0,
    refresh:()=>update(),invalidate:()=>{lastRefresh=Date.now();}};
  const badge=(text,ok=false)=>{node('live-badge').textContent=text;node('live-badge').className='badge '+(ok?'confirmed':'neutral');};
  const wanted=()=>!stopped&&!paused&&card.open&&!document.hidden;
  function disconnect(message){
    active=false;clearTimeout(timer);timer=null;
    image.hidden=true;image.removeAttribute('src');
    placeholder.hidden=false;placeholder.textContent=message;
  }
  function show(){
    if(!wanted()||active||!currentStatus?.available||Date.now()<retryAt)return;
    active=true;image.hidden=false;placeholder.hidden=true;
    image.classList.toggle('rotate180',mode==='raw'&&currentStatus.rotation===180);
    image.src=mode==='raw'?'/live.mjpg?view='+Date.now():'/live/detection.jpg?t='+Date.now();
  }
  function choose(value){
    mode=value;retryAt=0;disconnect('正在切换画面…');
    node('live-raw').setAttribute('aria-pressed',String(mode==='raw'));
    node('live-detection').setAttribute('aria-pressed',String(mode==='detection'));
    node('live-caption').textContent=mode==='raw'?'实时画面不等待模型推理；显示的是 RTSP 视频最新帧。下方场景图和采集记录仍为历史照片。':'检测框与当前检测图来自同一帧；模型计算会使此模式落后于实时画面。';
    show();
  }
  image.addEventListener('load',()=>{
    if(!wanted())return;
    badge(mode==='raw'?'实时画面':'检测画面',true);
    if(mode==='detection')timer=setTimeout(()=>{
      if(wanted()&&active)image.src='/live/detection.jpg?t='+Date.now();
    },300);
  });
  image.addEventListener('error',()=>{
    if(!active)return;
    retryAt=Date.now()+1500;disconnect('画面暂时中断，正在重连…');badge('重连中');
  });
  node('live-raw').addEventListener('click',()=>choose('raw'));
  node('live-detection').addEventListener('click',()=>choose('detection'));
  node('live-pause').addEventListener('click',()=>{
    paused=!paused;node('live-pause').textContent=paused?'恢复画面':'暂停画面';
    if(paused){disconnect('画面已暂停');badge('已暂停');}else{retryAt=0;show();}
  });
  node('live-fullscreen').addEventListener('click',async()=>{
    try{if(document.fullscreenElement)await document.exitFullscreen();else await node('live-stage').requestFullscreen();}
    catch(error){badge('浏览器未开启全屏');}
  });
  card.addEventListener('toggle',()=>{if(wanted())show();else disconnect(paused?'画面已暂停':'展开后继续监看');});
  document.addEventListener('visibilitychange',()=>{if(wanted())show();else disconnect('切回网页后恢复监看');});
  window.addEventListener('pagehide',()=>{stopped=true;disconnect('监看已关闭');});
  window.addEventListener('pageshow',()=>{stopped=false;update();});
  async function update(){
    if(statusBusy||stopped||document.hidden)return;
    statusBusy=true;
    const controller=new AbortController(),deadline=setTimeout(()=>controller.abort(),3500);
    try{
      const response=await fetch('/api/live/status',{cache:'no-store',signal:controller.signal});
      if(!response.ok)throw new Error('preview status');
      currentStatus=await response.json();
      const parts=[];
      if(Number.isFinite(currentStatus.source_age_ms))parts.push('最新视频帧到达 '+(currentStatus.source_age_ms/1000).toFixed(2)+' 秒前');
      if(mode==='detection'&&Number.isFinite(currentStatus.detection_ms))parts.push('推理 '+Math.round(currentStatus.detection_ms)+' ms');
      node('live-metrics').textContent=parts.join(' · ');
      if(!wanted())return;
      if(!currentStatus.available){badge('正在等待新帧');if(!active)placeholder.textContent=currentStatus.message||'视频暂时不可用';}
      else show();
    }catch(error){if(wanted())badge(active?'视频连接保持 · 状态查询稍后重试':'正在连接');}
    finally{clearTimeout(deadline);statusBusy=false;}
  }
  update();setInterval(update,1000);
})();
