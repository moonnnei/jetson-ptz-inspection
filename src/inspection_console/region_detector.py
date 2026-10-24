"""Bounded one-image IPC to the existing single-class YOLO11 NCNN executable."""
import hashlib
import json
from pathlib import Path
import struct
import subprocess
import time
import os
import select
import threading
from contextlib import contextmanager


def iou(a,b):
    inter=max(0.,min(a[2],b[2])-max(a[0],b[0]))*max(0.,min(a[3],b[3])-max(a[1],b[1]))
    return inter/max(1e-9,(a[2]-a[0])*(a[3]-a[1])+(b[2]-b[0])*(b[3]-b[1])-inter)


class RegionDetector:
    def __init__(self,root):
        root=Path(root)
        self.executable=root/'build/meter_detector'
        self.param=root/'models/meter640/model.ncnn.param'
        self.weights=root/'models/meter640/model.ncnn.bin'
        self.hashes=dict(param=hashlib.sha256(self.param.read_bytes()).hexdigest(),
                         weights=hashlib.sha256(self.weights.read_bytes()).hexdigest())
        if not self.executable.is_file():raise ValueError('meter detector executable missing')
        self.worker=None;self.worker_lock=threading.RLock()

    @contextmanager
    def session(self):
        """Reuse the existing native framed loop without changing inference."""
        with self.worker_lock:
            if self.worker is not None:raise ValueError('detector session already active')
            p=subprocess.Popen([str(self.executable),str(self.param),str(self.weights),'640','2','0'],
                stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,bufsize=0)
            try:
                os.set_blocking(p.stdin.fileno(),False)
                self.worker=p
                yield
            finally:
                self.worker=None
                if p.poll() is None:
                    p.terminate()
                    try:p.wait(timeout=2)
                    except subprocess.TimeoutExpired:p.kill();p.wait(timeout=3)
                p.stdin.close();p.stdout.close()

    def exchange(self,raw):
        p=self.worker;deadline=time.monotonic()+10
        view=memoryview(struct.pack('<I',len(raw))+raw)
        while view:
            remaining=deadline-time.monotonic()
            if remaining<=0 or not select.select([],[p.stdin],[],remaining)[1]:raise RuntimeError('detector input timeout')
            try:n=os.write(p.stdin.fileno(),view)
            except BlockingIOError:continue
            if not n:raise RuntimeError('detector closed')
            view=view[n:]
        def read(size):
            chunks=bytearray()
            while len(chunks)<size:
                remaining=deadline-time.monotonic()
                if remaining<=0 or not select.select([p.stdout],[],[],remaining)[0]:raise RuntimeError('detector output timeout')
                data=os.read(p.stdout.fileno(),size-len(chunks))
                if not data:raise RuntimeError('detector exited')
                chunks.extend(data)
            return bytes(chunks)
        header=read(4);size=struct.unpack('<I',header)[0]
        if not 2<=size<=1000000:raise ValueError('detector JSON budget exceeded')
        payload=read(size);jpeg_header=read(4);jpeg_size=struct.unpack('<I',jpeg_header)[0]
        if not 4<=jpeg_size<=12000000:raise ValueError('detector JPEG budget exceeded')
        return header+payload+jpeg_header+read(jpeg_size)

    def predict(self,path):
        with self.worker_lock:return self._predict(path)

    def _predict(self,path):
        """Return native source-local detections; no registry or motion side effects."""
        if (hashlib.sha256(self.param.read_bytes()).hexdigest()!=self.hashes['param']
                or hashlib.sha256(self.weights.read_bytes()).hexdigest()!=self.hashes['weights']):
            raise ValueError('detector model changed during commissioning')
        raw=Path(path).read_bytes()
        if not 4<=len(raw)<=12000000:raise ValueError('invalid ROI JPEG size')
        args=[str(self.executable),str(self.param),str(self.weights),'640','2','0']
        start=time.monotonic()
        if self.worker is not None:
            try:output=self.exchange(raw)
            except Exception:
                self.worker.kill();self.worker.wait(timeout=3)
                raise
        else:
            process=subprocess.Popen(args,stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.DEVNULL)
            try:
                output,unused=process.communicate(struct.pack('<I',len(raw))+raw,timeout=10)
                if process.returncode:raise RuntimeError('ROI_detector_process_failed')
            finally:
                if process.poll() is None:
                    process.kill();process.wait(timeout=3)
        if len(output)<8:raise ValueError('ROI detector response incomplete')
        size=struct.unpack('<I',output[:4])[0]
        if not 2<=size<=1000000 or len(output)<size+8:raise ValueError('invalid detector JSON length')
        result=json.loads(output[4:4+size].decode())
        jpeg=struct.unpack('<I',output[4+size:8+size])[0]
        if not 4<=jpeg<=12000000 or len(output)!=size+8+jpeg:raise ValueError('invalid detector image framing')
        if (result.get('model')!='YOLO11n-meter-Pointer10K' or result.get('input_size')!=640
                or result.get('rotation')!=0 or result.get('confidence_threshold')!=.25
                or result.get('nms_iou_threshold')!=.45):
            raise ValueError('detector profile differs from the frozen meter model')
        import math
        w,h=result['width'],result['height']
        if type(w) is not int or type(h) is not int or not 16<=min(w,h)<=max(w,h)<=8192:
            raise ValueError('invalid native image dimensions')
        for d in result['detections']:
            a=d.get('bbox',[]);c=d.get('confidence')
            if (len(a)!=4 or any(type(v) not in (int,float) or not math.isfinite(v) for v in a)
                    or not 0<=a[0]<a[2]<=1 or not 0<=a[1]<a[3]<=1
                    or type(c) not in (int,float) or not math.isfinite(c) or not .25<=c<=1):
                raise ValueError('invalid native detection')
        result.update(model_sha256=self.hashes,elapsed_seconds=time.monotonic()-start)
        return result

    def infer(self,path,crop,expected):
        result=self.predict(path)
        x,y,r,b=crop;w,h=r-x,b-y
        if (result['width'],result['height'])!=(w,h):raise ValueError('ROI dimensions differ from detector response')
        hits=[]
        for item in result['detections']:
            a=item['bbox']
            box=[x+a[0]*w,y+a[1]*h,x+a[2]*w,y+a[3]*h]
            if iou(box,expected)>=.35:hits.append(dict(bbox=box,confidence=item['confidence']))
        # Two overlapping network proposals are not two independent votes.
        report=dict(accepted=len(hits)==1,reason='unique_near_annotation_meter' if len(hits)==1 else
            'no_meter_near_annotation' if not hits else 'multiple_meter_matches_ambiguous',
            hits=hits,model_sha256=self.hashes,elapsed_seconds=result['elapsed_seconds'],
            candidate_count=len(result['detections']),confidence_threshold=.25,nms_iou=.45)
        if len(hits)==1:report['bbox']=hits[0]['bbox']
        return report
