#!/usr/bin/env python3
"""Jetson TX2 L4T R28 camera preview; Python 3.5 standard library only."""
import json
import os
import signal
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from socketserver import ThreadingMixIn

STATE = {'jpeg': None, 'timestamp': 0, 'frames': 0, 'error': None}
LOCK = threading.Lock()
PAGE = '''<!doctype html><html lang="zh-CN"><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>TX2 板载摄像头</title><style>
body{margin:0;background:#101827;color:#e8eef8;font-family:system-ui,sans-serif}
main{max-width:1000px;margin:40px auto;padding:0 24px}h1{font-size:28px}
.tag{color:#80dbb4}img{display:block;width:100%;aspect-ratio:16/9;object-fit:contain;
background:#05080f;border-radius:12px}p{color:#aebed3;line-height:1.7}
button,a{display:inline-block;background:#264b79;color:white;border:0;border-radius:7px;
padding:10px 16px;margin:12px 10px 0 0;font-size:15px;text-decoration:none}
</style><main><span class="tag">JETSON TX2 / LIVE CAMERA</span>
<h1>板载摄像头实时预览</h1><p id="status">正在获取画面…</p>
<img id="view" alt="TX2 摄像头画面"><button id="toggle">暂停预览</button>
<a href="/frame.jpg" target="_blank">打开当前图片</a>
<p>CSI 摄像头 → TX2 采集与 JPEG 编码 → 局域网浏览器<br>
预览尺寸 640 × 360。暂停仅停止浏览器刷新，摄像头服务仍在运行。未启用录像。</p></main>
<script>
let paused=false; const view=document.getElementById('view');
function refresh(){if(!paused)view.src='/frame.jpg?t='+Date.now();}
view.onload=()=>setTimeout(refresh,200);view.onerror=()=>setTimeout(refresh,1000);
document.getElementById('toggle').onclick=function(){paused=!paused;
this.textContent=paused?'继续预览':'暂停预览';if(!paused)refresh();};
async function status(){try{let r=await fetch('/status');let s=await r.json();
document.getElementById('status').textContent=s.live?
'采集中 · 已采集 '+s.frames+' 帧 · 最近画面 '+s.age_seconds.toFixed(1)+' 秒前':
'画面暂不可用：'+(s.error||'等待摄像头');}catch(e){
document.getElementById('status').textContent='连接中断，请检查 TX2 网络';}}
refresh();status();setInterval(status,1500);
</script></html>'''.encode('utf-8')

def capture():
    pending = b''
    try:
        while True:
            chunk = os.read(PROCESS.stdout.fileno(), 65536)
            if not chunk:
                break
            pending += chunk
            while True:
                start = pending.find(b'\xff\xd8')
                if start < 0:
                    pending = pending[-1:]
                    break
                if start:
                    pending = pending[start:]
                end = pending.find(b'\xff\xd9', 2)
                if end < 0:
                    if len(pending) > 4000000:
                        pending = b''
                    break
                frame, pending = pending[:end+2], pending[end+2:]
                with LOCK:
                    STATE.update(jpeg=frame, timestamp=time.monotonic(),
                                 frames=STATE['frames']+1)
        with LOCK:
            STATE['error'] = 'Camera process ended; inspect camera.log'
    except Exception as exc:
        with LOCK:
            STATE['error'] = str(exc)

class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        path = self.path.split('?', 1)[0]
        with LOCK:
            age = time.monotonic()-STATE['timestamp'] if STATE['timestamp'] else None
            live = age is not None and age < 3 and PROCESS.poll() is None
            frame = STATE['jpeg']
            status = {'live': live, 'frames': STATE['frames'],
                      'age_seconds': age, 'error': STATE['error']}
        if path == '/':
            body, mime, code = PAGE, 'text/html; charset=utf-8', 200
        elif path == '/status':
            body, mime, code = json.dumps(status).encode(), 'application/json', 200
        elif path == '/frame.jpg' and live:
            body, mime, code = frame, 'image/jpeg', 200
        else:
            body, mime, code = b'No live frame' if path == '/frame.jpg' else b'Not found', 'text/plain', 503 if path == '/frame.jpg' else 404
        try:
            self.send_response(code)
            self.send_header('Content-Type', mime)
            self.send_header('Content-Length', str(len(body)))
            self.send_header('Cache-Control', 'no-store')
            self.end_headers()
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def log_message(self, *args):
        pass

class Server(ThreadingMixIn, HTTPServer):
    daemon_threads = True
    allow_reuse_address = True

if __name__ == '__main__':
    # Bind before starting camera so port conflicts do not leave a capture process.
    server = Server(('0.0.0.0', 8765), Handler)
    command = ['gst-launch-1.0', '-q', 'nvcamerasrc', '!',
        'video/x-raw(memory:NVMM),width=1280,height=720,format=I420,framerate=30/1',
        '!', 'nvvidconv', '!', 'video/x-raw,format=I420,width=640,height=360',
        '!', 'jpegenc', 'quality=75', '!', 'fdsink', 'fd=1']
    log = open('camera.log', 'ab')
    PROCESS = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=log)
    worker = threading.Thread(target=capture)
    worker.daemon = True
    worker.start()
    def stop(signum, frame):
        raise KeyboardInterrupt()
    signal.signal(signal.SIGTERM, stop)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        if PROCESS.poll() is None:
            PROCESS.terminate()
            try:
                PROCESS.wait(timeout=5)
            except subprocess.TimeoutExpired:
                PROCESS.kill()
                PROCESS.wait()
        log.close()
