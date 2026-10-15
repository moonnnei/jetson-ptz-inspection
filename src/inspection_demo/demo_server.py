#!/usr/bin/env python3
"""Single-owner real person-as-instrument inspection demo; no automatic motion on boot."""
import base64
import csv
import getpass
import io
import json
import os
from pathlib import Path
import signal
import shutil
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from socketserver import ThreadingMixIn
from urllib.parse import unquote, urlsplit

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT.parent / 'person_tracker'))
import server as tracker
from app_config import parse_args
from controller import Controller
from real_io import RealIO


class Server(ThreadingMixIn, HTTPServer):
    daemon_threads = True


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def reply(self, data, status=200, ctype='application/json; charset=utf-8', filename=None):
        if not isinstance(data, bytes):
            data = json.dumps(data, ensure_ascii=False).encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type', ctype)
        self.send_header('Content-Length', str(len(data)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        if filename:
            self.send_header('Content-Disposition', 'attachment; filename="' + filename + '"')
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        app, controller, device = self.server.app, self.server.controller, self.server.device
        path = urlsplit(self.path).path
        if path == '/':
            self.reply((ROOT / 'index.html').read_text(encoding='utf-8').replace('__TOKEN__', app.token).encode('utf-8'),
                       ctype='text/html; charset=utf-8')
        elif path == '/api/status':
            state = controller.snapshot()
            detection = app.snapshot()
            state.update(camera=device.last_position, capable=device.capable, focus_style=device.focus_style,
                         detector={k: detection.get(k) for k in ('persons', 'frames', 'network_ms', 'frame_age', 'rotation', 'error')})
            if device.stop_error:
                state['error'] = device.stop_error
            self.reply(state)
        elif path == '/api/frame':
            with app.lock:
                p = dict(app.preview_state or {})
                frame = dict(jpeg=app.jpeg_b64, persons=p.get('persons', []), sequence=p.get('frames', 0),
                             frame_age=round(time.monotonic() - app.preview_received, 3) if app.preview_received else None)
            self.reply(frame)
        elif path in ('/api/export.json', '/api/export.csv'):
            state = controller.snapshot()
            if path.endswith('.json'):
                self.reply(state, filename='inspection-results.json')
            else:
                output = io.StringIO()
                fields = ['target_id', 'status', 'timestamp', 'pan', 'tilt', 'zoom', 'confidence',
                          'reading', 'reading_status', 'sharpness', 'image', 'crop', 'error']
                writer = csv.DictWriter(output, fieldnames=fields, extrasaction='ignore')
                writer.writeheader()
                for record in state.get('results', []):
                    record = dict(record, timestamp=record.get('captured_at'))
                    record.update(record.get('pose') or {})
                    # No untrusted labels are interpreted as spreadsheet formulas.
                    safe = {k: ("'" + str(v) if isinstance(v, str) and v.startswith(('=', '+', '-', '@')) else v)
                            for k, v in record.items()}
                    writer.writerow(safe)
                self.reply(('\ufeff' + output.getvalue()).encode('utf-8'), ctype='text/csv; charset=utf-8', filename='inspection-results.csv')
        elif path.startswith('/captures/'):
            parts = unquote(path[len('/captures/'):]).split('/')
            if len(parts) != 2 or any(not p or p in ('.', '..') or '\\' in p or ':' in p or '\x00' in p for p in parts):
                return self.reply({'error': 'Invalid path'}, 400)
            if not parts[1].endswith(('.jpg', '.json')):
                return self.reply({'error': 'Not found'}, 404)
            target = self.server.results_dir / parts[0] / parts[1]
            if not target.is_file():
                return self.reply({'error': 'Not found'}, 404)
            try:
                target = target.resolve()
                target.relative_to(self.server.results_dir.resolve())
            except (ValueError, OSError):
                return self.reply({'error': 'Invalid path'}, 400)
            self.reply(target.read_bytes(), ctype='image/jpeg' if target.suffix == '.jpg' else 'application/json')
        else:
            self.reply({'error': 'Not found'}, 404)

    def do_POST(self):
        app, controller = self.server.app, self.server.controller
        if self.headers.get('X-Control-Token') != app.token:
            return self.reply({'error': 'Invalid control token'}, 403)
        if self.headers.get('Origin') not in (None, 'http://' + self.headers.get('Host', '')):
            return self.reply({'error': 'Origin mismatch'}, 403)
        try:
            length = int(self.headers.get('Content-Length', '0'))
            if not 0 <= length <= 1024:
                return self.reply({'error': 'Invalid body size'}, 413)
            body = json.loads(self.rfile.read(length).decode('utf-8')) if length else {}
            if self.path == '/api/heartbeat':
                if body != {}:
                    raise ValueError('Expected empty JSON object')
                controller.heartbeat()
            elif self.path == '/api/stop':
                if body != {}:
                    raise ValueError('Expected empty JSON object')
                controller.stop('用户停止')
                if self.server.device.stop_error or controller.snapshot().get('stop_error'):
                    return self.reply({'error': self.server.device.stop_error or controller.snapshot()['stop_error']}, 409)
            elif self.path == '/api/start':
                if not isinstance(body, dict) or set(body) != {'scan'} or type(body['scan']) is not bool:
                    raise ValueError('Expected a boolean scan field')
                if self.server.device.stop_error:
                    raise RuntimeError(self.server.device.stop_error)
                state = app.snapshot()
                if state.get('error') or state.get('frame_age') is None or state['frame_age'] > 1:
                    raise RuntimeError('没有新鲜检测图像')
                controller.start(scan=body['scan'])
            else:
                return self.reply({'error': 'Not found'}, 404)
            self.reply({'ok': True})
        except Exception as exc:
            self.reply({'error': str(exc)}, 409)


def main():
    stdin_secret = '--password-stdin' in sys.argv
    if stdin_secret:
        sys.argv.remove('--password-stdin')
    args = parse_args()
    if args.port == 8771:
        raise RuntimeError('Use dedicated demo port 8772 and stop the old controller first')
    subprocess.check_output([shutil.which('python2') or sys.executable,
                             str(ROOT / 'evidence_worker.py'), '--check'], timeout=8)
    import fcntl
    lockfile = open('/tmp/jetson-ptz-inspection-motion.lock', 'a')
    fcntl.flock(lockfile, fcntl.LOCK_EX | fcntl.LOCK_NB)
    args.runtime_dir.mkdir(parents=True, exist_ok=True)
    password = json.loads(sys.stdin.read(4096))['password'] if stdin_secret else getpass.getpass('Camera password (memory only): ')
    # Validate credentials and existing state before daemonization. No nonzero movement here.
    camera = tracker.FastCamera(args.camera, args.user, password)
    camera.status()
    camera.stop()
    if args.daemon:
        child = os.fork()
        if child:
            print('Inspection demo PID:', child, flush=True)
            return
        os.setsid()
        fd = os.open(str(args.runtime_dir / 'server.log'), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        os.dup2(fd, 1)
        os.dup2(fd, 2)
        os.close(fd)
        fd = os.open('/dev/null', os.O_RDONLY)
        os.dup2(fd, 0)
        os.close(fd)
    app = device = controller = httpd = None
    threads = []
    def shutdown(*unused):
        tracker.QUIT.set()
        if controller:
            controller.stop('服务退出')
    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)
    try:
        app = tracker.App(args.camera, args.user, password, args.model_dir, args.rotation, args.detector, args.runtime_dir,
                          frame_source=args.frame_source, rtsp_url=args.rtsp_url, rtsp_decoder=args.rtsp_decoder,
                          rtsp_fps=args.rtsp_fps, rtsp_width=args.rtsp_width, rtsp_height=args.rtsp_height,
                          rtsp_cpus=args.rtsp_cpus, detector_threads=args.detector_threads,
                          opencv_threads=args.opencv_threads, detector_cpus=args.detector_cpus,
                          frame_policy='latest', motion_mode='responsive', preview_interval=.2)
        del password
        app.preview_requested_until = float('inf')
        device = RealIO(app)
        results_dir = args.runtime_dir / 'captures'
        controller = Controller(device, results_dir)
        httpd = Server((args.host, args.port), Handler)
        httpd.app, httpd.device, httpd.controller, httpd.results_dir = app, device, controller, results_dir
        httpd.timeout = .25
        threads = [threading.Thread(target=app.loop, daemon=True)]
        for thread in threads:
            thread.start()
        (args.runtime_dir / 'server.pid').write_text(str(os.getpid()))
        print('Inspection demo ready; observation only; port', args.port, flush=True)
        while not tracker.QUIT.is_set():
            httpd.handle_request()
    finally:
        tracker.QUIT.set()
        if controller:
            controller.stop('服务退出')
            worker = getattr(controller, '_thread', None)
            if worker:
                worker.join(10)
        if app:
            app.stop('服务退出')
            for thread in threads:
                thread.join(4)
            if app.video_source:
                app.video_source.close()
            app.engine.close()
        if httpd:
            httpd.server_close()
        lockfile.close()


if __name__ == '__main__':
    main()
