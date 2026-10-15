#!/usr/bin/env python2
# -*- coding: utf-8 -*-
"""YOLOv3-tiny on the existing TX2 CUDA 9 stack; uses pjreddie Darknet."""
from __future__ import print_function
import argparse
import ctypes
import json
import os
import sys
import threading
import time
import urllib2
from BaseHTTPServer import BaseHTTPRequestHandler, HTTPServer
from SocketServer import ThreadingMixIn
import cv2
import numpy as np

ROOT = os.path.dirname(os.path.abspath(__file__))
os.chdir(ROOT)
sys.path.insert(0, os.path.join(ROOT, 'python'))
import darknet as dn

LOCK = threading.Lock()
STATE = dict(jpeg=None, frames=0, timestamp=0, detections=[], error='Loading model',
             inference_ms=0, pipeline_ms=0, model='YOLOv3-tiny COCO', device='TX2 CUDA GPU')
STOP = threading.Event()

class Detector(object):
    def __init__(self, threshold):
        dn.set_gpu(0)
        self.net = dn.load_net('cfg/yolov3-tiny.cfg', 'yolov3-tiny.weights', 0)
        if not self.net:
            raise RuntimeError('Darknet failed to load network')
        self.meta = dn.load_meta('cfg/coco.data')
        self.threshold = threshold

    def infer(self, bgr):
        height, width = bgr.shape[:2]
        # network_predict_image does the model's letterbox resize internally.
        rgb_chw = np.ascontiguousarray(bgr[:, :, ::-1].transpose(2, 0, 1), dtype=np.float32)
        rgb_chw /= 255.0
        image = dn.IMAGE(width, height, 3, rgb_chw.ctypes.data_as(ctypes.POINTER(ctypes.c_float)))
        started = time.time()
        dn.predict_image(self.net, image)
        count = ctypes.c_int(0)
        boxes = dn.get_network_boxes(self.net, width, height, self.threshold, 0.5,
                                     None, 0, ctypes.pointer(count))
        results = []
        try:
            dn.do_nms_sort(boxes, count.value, self.meta.classes, 0.45)
            for index in range(count.value):
                box = boxes[index]
                for category in range(self.meta.classes):
                    score = float(box.prob[category])
                    if score < self.threshold:
                        continue
                    b = box.bbox
                    xyxy = [max(0, int(b.x-b.w/2)), max(0, int(b.y-b.h/2)),
                            min(width-1, int(b.x+b.w/2)), min(height-1, int(b.y+b.h/2))]
                    if xyxy[2] <= xyxy[0] or xyxy[3] <= xyxy[1]:
                        continue
                    results.append(dict(label=self.meta.names[category].decode('utf-8'),
                                        class_id=category, confidence=round(score, 4), bbox=xyxy))
        finally:
            dn.free_detections(boxes, count.value)
        results.sort(key=lambda item: -item['confidence'])
        return results, (time.time()-started)*1000

def annotate(bgr, results, latency):
    image = bgr.copy()
    for item in results:
        left, top, right, bottom = item['bbox']
        color = (100, 230, 80)
        cv2.rectangle(image, (left, top), (right, bottom), color, 2)
        text = '{} {:.0f}%'.format(item['label'], item['confidence']*100)
        cv2.putText(image, text, (left, max(18, top-7)), cv2.FONT_HERSHEY_SIMPLEX,
                    0.55, (0, 0, 0), 3)
        cv2.putText(image, text, (left, max(18, top-7)), cv2.FONT_HERSHEY_SIMPLEX,
                    0.55, color, 1)
    cv2.putText(image, 'YOLOv3-tiny | GPU | {:.0f} ms'.format(latency), (12, image.shape[0]-12),
                cv2.FONT_HERSHEY_SIMPLEX, 0.48, (80, 255, 255), 1)
    return image

def worker(threshold, source):
    try:
        detector = Detector(threshold)
    except Exception as exc:
        with LOCK:
            STATE['error'] = str(exc)
        return
    while not STOP.is_set():
        started = time.time()
        try:
            response = urllib2.urlopen(source, timeout=3)
            try:
                data = response.read()
            finally:
                response.close()
            frame = cv2.imdecode(np.frombuffer(data, dtype=np.uint8), cv2.IMREAD_COLOR)
            if frame is None:
                raise ValueError('Camera returned an invalid JPEG')
            results, latency = detector.infer(frame)
            marked = annotate(frame, results, latency)
            valid, encoded = cv2.imencode('.jpg', marked, [cv2.IMWRITE_JPEG_QUALITY, 82])
            if not valid:
                raise ValueError('JPEG encoding failed')
            with LOCK:
                STATE.update(jpeg=encoded.tostring(), timestamp=time.time(),
                             frames=STATE['frames']+1, detections=results, error=None,
                             inference_ms=round(latency, 1), pipeline_ms=round((time.time()-started)*1000, 1))
        except Exception as exc:
            with LOCK:
                STATE['error'] = str(exc)
            STOP.wait(1)
        # Fetch a new frame after processing; never queue old camera frames.
        STOP.wait(max(0, 0.12-(time.time()-started)))

PAGE = u'''<!doctype html><html lang="zh-CN"><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>TX2 · YOLO 通用物体检测</title><style>
*{box-sizing:border-box}body{margin:0;background:#0b1423;color:#e4edf7;font-family:system-ui,sans-serif}
main{max-width:1100px;margin:30px auto;padding:0 24px}h1{font-size:28px;margin:10px 0}
.tag{font-size:13px;color:#74e0b4;letter-spacing:2px}.metrics{display:flex;gap:12px;flex-wrap:wrap;margin:20px 0}
.metric{padding:14px 20px;background:#18263c;border:1px solid #2a3c54;border-radius:10px;min-width:155px}
.metric small{display:block;color:#9bacbf;margin-bottom:5px}.metric strong{font-size:20px}
img{display:block;width:100%;background:#020609;border-radius:12px;aspect-ratio:16/9;object-fit:contain}
p{color:#aabbcf;line-height:1.7}button,a{background:#224a70;color:white;padding:10px 15px;
border:0;border-radius:7px;margin:12px 8px 0 0;display:inline-block;text-decoration:none;font-size:14px}
table{width:100%;border-collapse:collapse;margin:14px 0}td,th{padding:10px;text-align:left;border-bottom:1px solid #293a51}
th{color:#92a9c5}#status{min-height:25px}.foot{font-size:13px}
</style><main><span class="tag">JETSON TX2 / CUDA GPU</span><h1>YOLO 通用物体检测</h1>
<p>YOLOv3-tiny · COCO 80 类预训练模型 · 板载摄像头</p>
<div class="metrics"><div class="metric"><small>模型输入</small><strong>416 × 416</strong></div>
<div class="metric"><small>推理与框处理</small><strong id="latency">—</strong></div>
<div class="metric"><small>处理帧率（近期）</small><strong id="fps">—</strong></div>
<div class="metric"><small>当前目标数</small><strong id="count">—</strong></div></div>
<p id="status">正在加载检测结果…</p><img id="view" alt="YOLO 实时检测画面">
<button id="toggle">暂停预览</button><a href="/frame.jpg" target="_blank">打开当前检测图片</a>
<a href="http://192.168.1.4:8765/" target="_blank">原始摄像头画面</a>
<table><thead><tr><th>类别</th><th>置信度</th><th>原图位置 x1, y1, x2, y2</th></tr></thead><tbody id="objects"></tbody></table>
<p class="foot">可尝试人、杯子、瓶子、手机等类别。小目标或复杂背景可能漏检。
推理在 TX2 上执行，网页用于显示；暂停预览不会停止检测。未启用录像。</p></main>
<script>
let paused=false,lastFrames=null,lastTime=null;const image=document.getElementById('view');
function refresh(){if(!paused)image.src='/frame.jpg?t='+Date.now();}
image.onload=()=>setTimeout(refresh,180);image.onerror=()=>setTimeout(refresh,1000);
document.getElementById('toggle').onclick=function(){paused=!paused;this.textContent=paused?'继续预览':'暂停预览';if(!paused)refresh();};
async function update(){try{const r=await fetch('/status');const s=await r.json();const now=Date.now();
document.getElementById('status').textContent=s.live?'GPU 检测运行中 · 已处理 '+s.frames+' 帧':'检测暂不可用：'+(s.error||'等待新画面');
document.getElementById('latency').textContent=s.live?s.inference_ms+' ms':'—';
document.getElementById('count').textContent=s.live?s.detections.length:'—';
document.getElementById('fps').textContent=s.live&&lastFrames!==null?Math.max(0,(s.frames-lastFrames)*1000/(now-lastTime)).toFixed(1)+' FPS':'—';
lastFrames=s.frames;lastTime=now;let body=document.getElementById('objects');body.textContent='';
if(s.live){for(const d of s.detections){let row=document.createElement('tr');for(const t of [d.label,(d.confidence*100).toFixed(1)+'%',d.bbox.join(', ')]){let cell=document.createElement('td');cell.textContent=t;row.appendChild(cell);}body.appendChild(row);}
if(!s.detections.length){let row=document.createElement('tr');let cell=document.createElement('td');cell.colSpan=3;cell.textContent='当前没有超过阈值的目标';row.appendChild(cell);body.appendChild(row);}}
}catch(e){document.getElementById('status').textContent='连接中断，请检查 TX2 网络';}}
refresh();update();setInterval(update,1000);
</script></html>'''.encode('utf-8')

class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        path = self.path.split('?', 1)[0]
        with LOCK:
            status = {k: v for k, v in STATE.items() if k != 'jpeg'}
            age = time.time()-STATE['timestamp'] if STATE['timestamp'] else None
            live = age is not None and age < 5 and STATE['error'] is None
            status.update(live=live, age_seconds=age)
            frame = STATE['jpeg']
        if path == '/':
            body, mime, code = PAGE, 'text/html; charset=utf-8', 200
        elif path == '/status':
            body, mime, code = json.dumps(status).encode('utf-8'), 'application/json', 200
        elif path == '/frame.jpg' and live:
            body, mime, code = frame, 'image/jpeg', 200
        else:
            body, mime, code = b'No live result', 'text/plain', 503
        try:
            self.send_response(code)
            self.send_header('Content-Type', mime)
            self.send_header('Content-Length', str(len(body)))
            self.send_header('Cache-Control', 'no-store')
            self.end_headers()
            self.wfile.write(body)
        except IOError:
            pass
    def log_message(self, *args):
        pass

class Server(ThreadingMixIn, HTTPServer):
    daemon_threads = True
    allow_reuse_address = True

if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--image')
    parser.add_argument('--threshold', type=float, default=0.35)
    parser.add_argument('--source', default='http://127.0.0.1:8765/frame.jpg')
    parser.add_argument('--port', type=int, default=8766)
    args = parser.parse_args()
    if args.image:
        detector = Detector(args.threshold)
        frame = cv2.imread(args.image)
        if frame is None:
            raise ValueError('Cannot read input image')
        results, latency = detector.infer(frame)
        if not os.path.isdir('results'):
            os.makedirs('results')
        output = dict(model='YOLOv3-tiny COCO', backend='Darknet CUDA',
                      image=args.image, inference_ms=latency, detections=results)
        with open('results/test-detection.json', 'w') as handle:
            json.dump(output, handle, indent=2)
        assert cv2.imwrite('results/test-detection.jpg', annotate(frame, results, latency))
        print(json.dumps(output, indent=2))
    else:
        server = Server(('0.0.0.0', args.port), Handler)
        thread = threading.Thread(target=worker, args=(args.threshold, args.source))
        thread.daemon = True
        thread.start()
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            STOP.set()
            server.server_close()
