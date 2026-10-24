'use strict';
(function(root,factory){const value=factory();if(typeof module==='object'&&module.exports)module.exports=value;else root.ResultGalleryModel=value;})(typeof window!=='undefined'?window:globalThis,()=>{
  const imageKey=value=>typeof value==='string'&&/^[a-f0-9]{64}\.(jpg|png)$/.test(value);
  const patrol=job=>['software_preset_capture','bounded_uncommissioned_capture_round'].includes(job.mode);
  const boxValid=b=>Array.isArray(b)&&b.length===4&&b.every(Number.isFinite)&&b[0]>=0&&b[1]>=0&&b[2]>b[0]&&b[3]>b[1];
  function successful(job,result){
    return !!result&&['captured','captured_reference_checked'].includes(result.status)&&imageKey(result.image)&&
      Number.isFinite(Date.parse(result.captured_at))&&result.quality?.accepted!==false&&
      !/diagnostic|not_accepted/.test(result.image_status||'')&&!/STOP/i.test(job.error||'')&&
      (!result.image_sha256||result.image.startsWith(result.image_sha256+'.'));
  }
  function photo(result){
    if(!result||!imageKey(result.image))return null;
    const hash=result.image.split('.')[0];
    let bbox=boxValid(result.bbox)?result.bbox:null;
    if(!bbox&&result.evidence?.image_sha256===hash&&boxValid(result.evidence.bbox))bbox=result.evidence.bbox;
    if(!bbox&&result.reference_bbox?.image_sha256===hash&&boxValid(result.reference_bbox.bbox))bbox=result.reference_bbox.bbox;
    const native=imageKey(result.crop_image)&&result.crop_source_sha256===hash&&
      result.crop_image.startsWith(result.crop_sha256+'.');
    return {image:result.image,source:native?result.crop_image:result.image,nativeCrop:!!native,
      bbox:bbox?bbox.slice():null,capturedAt:result.captured_at,quality:result.quality||null};
  }
  function mergeJobs(state,archive){
    const rows=new Map();[...(archive||[]),...(state.jobs||[])].forEach(j=>{if(j?.id)rows.set(j.id,j);});
    return [...rows.values()].sort((a,b)=>(Date.parse(a.started_at)||0)-(Date.parse(b.started_at)||0)||a.id.localeCompare(b.id));
  }
  function targetIds(job,assets){
    const order=new Map(assets.map((a,i)=>[a.id,i]));
    // Display follows the registry, independently of the route taken this round.
    return [...new Set([...(job?.route||[]),...(job?.results||[]).map(r=>r.instrument_id),
      ...(!job?assets.filter(a=>a.enabled).map(a=>a.id):[])])].filter(Boolean)
      .sort((a,b)=>(order.get(a)??Infinity)-(order.get(b)??Infinity)||a.localeCompare(b));
  }
  function dateKey(value){const ms=Date.parse(value);return Number.isFinite(ms)?new Date(ms+8*3600000).toISOString().slice(0,10):'';}
  function scenes(state,archive=[]){
    const jobs=mergeJobs(state,archive);
    return (state.maps||[]).map((scene,index)=>{
      const assets=(state.instruments||[]).filter(a=>a.map_id===scene.id);
      const rounds=jobs.filter(j=>j.map_id===scene.id&&patrol(j));
      const date=Number.isFinite(Date.parse(scene.created_at))?new Date(scene.created_at).toLocaleString('zh-CN',
        {timeZone:'Asia/Shanghai',hour12:false}):'建档时间未记录';
      return {id:scene.id,title:scene.title,number:index+1,instruments:assets.length,rounds:rounds.length,
        label:'场景 '+(index+1)+' · '+date+' · '+assets.length+' 块仪表',
        historyLabel:'场景 '+(index+1)+' · '+date+' · '+rounds.length+' 轮'};
    });
  }
  function sceneSelection(storage){
    const key='inspection.selected-scene.v1';let remembered=null;
    try{remembered=storage?.getItem(key)||null;}catch(_){}
    function remember(id){
      if(!id||remembered===id)return;remembered=id;
      try{storage?.setItem(key,id);}catch(_){}
    }
    function resolve(maps,current,preferred){
      const valid=id=>maps.some(m=>m.id===id);
      const chosen=valid(preferred)?preferred:valid(current)?current:valid(remembered)?remembered:maps.at(-1)?.id||null;
      remember(chosen);return chosen;
    }
    return {resolve,remember};
  }
  function build(state,mapId,archive=[]){
    const assets=(state.instruments||[]).filter(a=>a.map_id===mapId),byId=new Map(assets.map(a=>[a.id,a]));
    const rounds=mergeJobs(state,archive).filter(j=>j.map_id===mapId&&patrol(j)).map((j,i)=>({job:j,number:i+1,
      label:'第 '+(i+1)+' 轮'+(j.mode==='bounded_uncommissioned_capture_round'?'（试采）':''),date:dateKey(j.started_at)}));
    const schedule=state.schedule||{},latest=rounds.at(-1);
    const upcoming=schedule.enabled&&schedule.map_id===mapId&&schedule.batch_id&&latest?.job.schedule_batch_id!==schedule.batch_id;
    const current=upcoming||!latest?{job:null,number:rounds.length+1,label:'等待第 '+(rounds.length+1)+' 轮'}:latest;
    const previous=rounds.filter(r=>r.number<current.number),ids=targetIds(current.job,assets);
    const rows=ids.map(id=>{
      const asset=byId.get(id)||{id,name:id,enabled:false};
      const result=current.job?.results.find(r=>r.instrument_id===id);
      const preview=current.job?.status==='running'?current.job.progress_results?.find(r=>r.instrument_id===id):null;
      let source=null,from=null,kind='empty';
      if(current.job&&successful(current.job,result)){source=result;from=current;kind='current';}
      else if(current.job&&successful(current.job,preview)){source=preview;from=current;kind='provisional';}
      else{
        for(const round of previous.slice().reverse()){
          const found=round.job.results.find(r=>r.instrument_id===id);
          if(successful(round.job,found)){source=found;from=round;kind='previous';break;}
        }
      }
      const failed=!!(current.job&&((result&&!successful(current.job,result))||
        (current.job.status!=='running'&&!successful(current.job,result))||
        (preview&&!successful(current.job,preview))));
      return {id,name:asset.name||id,asset,kind,failed,photo:photo(source),sourceRound:from?.number||null,
        result:result||null,preview:preview||null,currentRound:current.number};
    });
    return {rounds,current,rows,assets,completed:rows.filter(r=>r.kind==='current').length,
      previews:rows.filter(r=>r.kind==='provisional').length,targets:rows.length,
      history:rounds.filter(r=>r.job.status!=='running')};
  }
  function historyRows(round,assets){
    const byId=new Map(assets.map(a=>[a.id,a]));
    return targetIds(round.job,assets).map(id=>{
      const result=round.job.results.find(r=>r.instrument_id===id),ok=successful(round.job,result);
      return {id,name:byId.get(id)?.name||id,asset:byId.get(id),kind:ok?'current':'empty',failed:!ok,
        photo:ok?photo(result):null,result:result||null,sourceRound:round.number,currentRound:round.number,snapshot:true};
    });
  }
  function cropRect(box,width,height,padding=.03){
    if(!boxValid(box)||width<=0||height<=0)return null;
    const dx=(box[2]-box[0])*padding,dy=(box[3]-box[1])*padding;
    const x=Math.max(0,Math.floor(box[0]-dx)),y=Math.max(0,Math.floor(box[1]-dy));
    const right=Math.min(width,Math.ceil(box[2]+dx)),bottom=Math.min(height,Math.ceil(box[3]+dy));
    return right>x&&bottom>y?{x,y,width:right-x,height:bottom-y}:null;
  }
  return {build,historyRows,photo,cropRect,successful,patrol,mergeJobs,dateKey,scenes,sceneSelection};
});
