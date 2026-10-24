"""TX2-local scan/projection/discovery backend; modern PC frameworks not needed."""
import hashlib
import json
import math
from pathlib import Path
import subprocess
import threading
import time
import queue

from region_detector import iou
from ring_capture import RingCapture,ISAPIRingIO,ring_plan
from scan_capture import ISAPIScanIO


def merge(rows):
    selected=[]
    for row in sorted(rows,key=lambda r:(-r['confidence'],r['bbox'])):
        if not any(iou(row['bbox'],r['bbox'])>=.45 for r in selected):selected.append(row)
    return selected


def map_hits(result,crop,width,height,source):
    x,y,r,b=crop;w,h=r-x,b-y
    if (result['width'],result['height'])!=(w,h):raise ValueError('detector crop dimensions changed')
    rows=[]
    for d in result['detections']:
        a=d['bbox'];box=[x+a[0]*w,y+a[1]*h,x+a[2]*w,y+a[3]*h]
        edge=(x>0 and a[0]<=.02 or y>0 and a[1]<=.02 or r<width and a[2]>=.98 or b<height and a[3]>=.98)
        rows.append(dict(bbox=box,confidence=d['confidence'],source='ncnn_initialization/'+source,internal_edge=bool(edge)))
    return rows


class SceneInitializationIO:
    def __init__(self,camera,project_root,detector,fallback_focal=1542.84959310078):
        self.camera,self.root,self.detector=camera,Path(project_root),detector
        self.fallback_focal=fallback_focal
        self.active_scan=None;self.process=None;self.lock=threading.RLock()

    def prepare(self,bounds=None):
        from capture_review_views import motion_lease,check_other_services
        with motion_lease():
            check_other_services()
            anchor=ISAPIScanIO(self.camera).preflight()['anchor']
        from area_scan import area_plan
        scan=ring_plan(anchor) if bounds is None else area_plan(anchor,bounds,self.fallback_focal)
        return dict(scan=scan,model_sha256=self.detector.hashes,
                    fallback_focal_pixels=self.fallback_focal,orientation='rotate180_from_raw',
                    processing_budget_seconds=900,maximum_candidates=12,per_candidate_seconds=180,
                    source_resolution=[2560,1440],periodic_capture_enabled=False)

    def stop(self):
        with self.lock:scan,process=self.active_scan,self.process
        if scan is not None:scan.stop()
        if process is not None and process.poll() is None:process.kill()

    def vision(self,request,cancel,timeout=30):
        if cancel.is_set():raise RuntimeError('initialization_cancelled')
        args=['python2',str(self.root/'tools/scene_mapping/initialization_vision.py')]
        process=subprocess.Popen(args,stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.DEVNULL)
        with self.lock:self.process=process
        try:
            output,unused=process.communicate(json.dumps(request).encode(),timeout=timeout)
            if cancel.is_set():raise RuntimeError('initialization_cancelled')
            if process.returncode:raise RuntimeError('initialization_image_worker_failed')
            result=json.loads(output.decode())
            if result['opencv']!='3.3.1':raise ValueError('image worker runtime differs from verified TX2')
            return result
        finally:
            if process.poll() is None:process.kill();process.wait(timeout=3)
            with self.lock:self.process=None

    def run(self,prepared,directory,cancel,progress):
        from capture_review_views import motion_lease,check_other_services
        directory=Path(directory)
        directory.mkdir(parents=True,exist_ok=False)
        with motion_lease():
            check_other_services()
            if prepared['scan']['kind']=='bounded_area_scan':
                from area_scan import AreaScanIO,AreaCapture
                io=AreaScanIO(self.camera,prepared['scan']);io.preflight()
                capture=AreaCapture(io,directory/'scan',prepared['scan'],cancel)
            else:
                io=ISAPIRingIO(self.camera,prepared['scan']);io.preflight()
                capture=RingCapture(io,directory/'scan',prepared['scan'],cancel)
            original=capture.persist
            sent=[-1]
            def persist():
                original()
                count=len(capture.state['frames'])
                if count!=sent[0]:
                    sent[0]=count;progress('scanning',dict(views=count,total_views=prepared['scan']['expected_frames']))
            capture.persist=persist
            with self.lock:self.active_scan=capture
            pending=queue.Queue();processed=[];errors=[];published=[0]
            def stream():
                while True:
                    if cancel.is_set():raise RuntimeError('initialization_cancelled')
                    try:item=pending.get(timeout=.25)
                    except queue.Empty:continue
                    if item is None:return
                    yield item
            def pipeline_progress(stage,details):
                if self.active_scan is not None:
                    details=dict(details,detected_views=details.get('views',0),views=len(capture.state['frames']))
                    stage='scanning'
                progress(stage,details)
            def process():
                try:
                    with self.detector.session():
                        processed.append(self.process_scan(capture.state,directory,cancel,pipeline_progress,
                            stream=stream(),total=prepared['scan']['expected_frames']))
                except Exception as exc:errors.append(exc);cancel.set()
            original_persist=capture.persist
            def publish():
                original_persist()
                for row in capture.state['frames'][published[0]:]:pending.put(dict(row))
                published[0]=len(capture.state['frames'])
            capture.persist=publish
            worker=threading.Thread(target=process,name='scan-image-pipeline');worker.daemon=True;worker.start()
            try:
                record=capture.run()
                if record['status']!='completed':cancel.set()
            finally:
                with self.lock:self.active_scan=None
                pending.put(None)
                worker.join(timeout=920)
            if worker.is_alive():cancel.set();raise RuntimeError('scan_pipeline_shutdown_unconfirmed')
            if errors:raise errors[0]
            if record['status']!='completed':raise RuntimeError('initialization_scan_'+record.get('error',record['status']))
            if len(processed)!=1:raise RuntimeError('scan_processing_missing')
            manifest=processed[0]
        return manifest

    def process_scan(self,record,directory,cancel,progress,stream=None,total=None):
        started=time.monotonic();frames=[];calls=0
        total=total if total is not None else len(record['frames'])
        for index,raw in enumerate(record['frames'] if stream is None else stream):
            self.check(cancel,started)
            folder=directory/('view-%02d'%(index+1));folder.mkdir()
            metadata=self.vision(dict(action='prepare',source=str(directory/'scan'/raw['path']),
                output=str(folder),orientation='rotate180_from_raw'),cancel)
            if (metadata['width'],metadata['height'])!=(2560,1440):raise ValueError('review initialization source profile')
            kept=[];edges=[]
            for item in metadata['crops']:
                self.check(cancel,started)
                result=self.detector.predict(folder/item['path']);calls+=1
                for row in map_hits(result,item['box'],2560,1440,item['source']):
                    (edges if row['internal_edge'] else kept).append(row)
            # Recheck up to four merged artificial seam fragments in expanded
            # original-image crops. A crop cut is never accepted as a complete box.
            for n,row in enumerate(merge(edges)[:4]):
                self.check(cancel,started)
                x,y,r,b=row['bbox'];dx=(r-x)*.65;dy=(b-y)*.65
                box=[max(0,int(math.floor(x-dx))),max(0,int(math.floor(y-dy))),
                     min(2560,int(math.ceil(r+dx))),min(1440,int(math.ceil(b+dy)))]
                path=folder/('expanded-%02d.jpg'%n)
                self.vision(dict(action='crop',source=str(folder/metadata['path']),bbox=box,output=str(path)),cancel)
                result=self.detector.predict(path);calls+=1
                kept.extend(d for d in map_hits(result,box,2560,1440,'edge_recheck') if not d['internal_edge'])
            detections=[{k:d[k] for k in ('bbox','confidence','source')} for d in merge(kept)]
            frame=dict(raw,path=str(folder/metadata['path']),sha256=metadata['sha256'],
                raw_source_path=str(directory/'scan'/raw['path']),raw_source_sha256=raw['sha256'],
                orientation='rotate180_from_raw',detections=detections)
            frames.append(frame)
            progress('detecting',dict(views=index+1,total_views=total,model_calls=calls,
                                      candidates=sum(len(f['detections']) for f in frames)))
        self.check(cancel,started)
        progress('mapping',dict(views=len(frames),total_views=len(frames),model_calls=calls))
        preview=directory/'ring-preview.png'
        projected=self.vision(dict(action='preview',frames=frames,output=str(preview),
            fallback_focal_pixels=self.fallback_focal,yaw_image_sign=1),cancel,timeout=120)
        self.check(cancel,started)
        manifest=dict(title='自动初始化 / '+record['started_at'],provenance='bounded_scan_capture',frames=frames,
            overview=dict(path=str(preview),sha256=projected['sha256'],kind='pose_projected_ring_preview',
                registered_views=0,total_views=len(frames),stitched_views=0,projected_views=len(frames),
                source_mapping='source_picker_only',full_panorama=False,ptz_calibration=False),
            projection=projected['projection'],processing=dict(model_calls=calls,seconds=time.monotonic()-started,
                detector_sha256=self.detector.hashes,projection=projected,super_resolution=False,
                persistent_detector=stream is not None,pipelined_acquisition=stream is not None))
        (directory/'manifest.json').write_text(json.dumps(manifest,indent=2),encoding='utf8')
        return manifest

    @staticmethod
    def check(cancel,started):
        if cancel.is_set():raise RuntimeError('initialization_cancelled')
        if time.monotonic()-started>900:raise RuntimeError('initialization_processing_budget_exhausted')
