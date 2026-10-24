#!/usr/bin/env python3
"""Local review console. Ships with NoHardware; no PTZ commands are sent."""
import argparse
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
import os
from pathlib import Path
import signal
from socketserver import ThreadingMixIn
import threading
from urllib.parse import urlsplit

from planner import route, scan_grid
from references import reference_plan
from capture_round import round_plan
from overview_review import source_proposal
from ring_coordinates import decorate_state
from scheduler import Scheduler
from store import Conflict, Store
from preview_proxy import PreviewProxy
from first_capture import first_capture_plan
from first_capture_service import FirstCaptureService
from annotation_service import AnnotationService
from scene_initialization import SceneInitialization
from manual_control import ManualControl

ROOT = Path(__file__).resolve().parent


@contextmanager
def instance_lock(root):
    root.mkdir(parents=True, exist_ok=True)
    stream = (root / 'server.lock').open('a+b')
    try:
        if os.name == 'nt':
            import msvcrt
            if stream.tell() == 0:
                stream.write(b'0')
                stream.flush()
            stream.seek(0)
            msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield
    finally:
        stream.close()


class Server(ThreadingMixIn, HTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address, store, allowed_hosts=None, preview_port=None, first_capture=None, patrol_adapter=None, annotations=None, initialization=None, manual=None):
        HTTPServer.__init__(self, address, Handler)
        self.store = store
        self.engine = Scheduler(store,patrol_adapter)
        self.preview = PreviewProxy(preview_port)
        self.first_capture = first_capture or FirstCaptureService(store)
        self.annotations=annotations or AnnotationService(store)
        self.initialization=initialization or SceneInitialization(store,self.annotations)
        self.operation_lock=threading.RLock()
        self.manual=manual or ManualControl()
        self.manual.check=self.manual_idle

        self.token = os.urandom(32).hex()
        port = self.server_address[1]
        names = set(allowed_hosts or []) | {'127.0.0.1', 'localhost'}
        if address[0] != '0.0.0.0':
            names.add(address[0])
        self.hosts = {name + ':' + str(port) for name in names}

    def manual_idle(self):
        state=self.store.snapshot();a=self.annotations.status()
        runtime=self.engine.status()
        if (state['schedule']['enabled'] or runtime.get('active_job') or runtime.get('stop_pending') or runtime.get('stop_error') or
                self.initialization.status()['active'] or self.first_capture.status()['active'] or
                a['active'] or (a['queued'] and not a['paused']) or a.get('stop_error')):
            raise Conflict('pause automatic work before manual PTZ control')



class Handler(BaseHTTPRequestHandler):
    def log_message(self, *unused):
        pass

    def respond(self, value, status=200, mime='application/json; charset=utf-8', download=None):
        data = value if isinstance(value, bytes) else json.dumps(value, ensure_ascii=False,
                                                                allow_nan=False).encode('utf-8')
        # Full-resolution PNG responses can exceed the small-response deadline
        # on a constrained LAN. Keep a bounded send deadline for these images.
        self.connection.settimeout(20 if len(data) > 1024 * 1024 else 5)
        self.send_response(status)
        self.send_header('Content-Type', mime)
        self.send_header('Content-Length', str(len(data)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Referrer-Policy', 'no-referrer')
        self.send_header('Content-Security-Policy', "default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; script-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
        if download:
            self.send_header('Content-Disposition', 'attachment; filename="' + download + '"')
        self.end_headers()
        self.wfile.flush()
        self.connection.sendall(data)

    def host_ok(self):
        return self.headers.get('Host', '') in self.server.hosts

    def do_GET(self):
        if not self.host_ok():
            return self.respond(dict(error='unrecognized host'), 403)
        path = urlsplit(self.path).path
        try:
            if path == '/':
                page = (ROOT / 'index.html').read_text(encoding='utf-8')
                return self.respond(page.replace('__TOKEN__', self.server.token).encode('utf-8'),
                                    mime='text/html; charset=utf-8')
            if path in ('/app.js', '/overview.js', '/live.js', '/first_capture.js', '/initialization.js', '/workbench.js', '/panorama_workbench.js', '/results_model.js', '/results_ui.js', '/results.css', '/style.css', '/workbench.css'):
                return self.respond((ROOT / path[1:]).read_bytes(),
                    mime='application/javascript; charset=utf-8' if path.endswith('.js') else 'text/css; charset=utf-8')
            if path in ('/vendor/openseadragon.min.js','/vendor/annotorious-openseadragon.js','/vendor/annotorious-openseadragon.css'):
                return self.respond((ROOT/path[1:]).read_bytes(),mime='text/css; charset=utf-8' if path.endswith('.css') else 'application/javascript; charset=utf-8')
            if path == '/api/camera/status':
                return self.respond(self.server.manual.status())
            if path == '/api/live/status':
                return self.respond(self.server.preview.status())
            if path == '/api/first-capture/status':
                return self.respond(self.server.first_capture.status())
            if path == '/api/annotations/status':
                return self.respond(self.server.annotations.status())
            if path == '/api/initialization/status':
                return self.respond(self.server.initialization.status())
            if path == '/live.mjpg':
                return self.server.preview.stream(self)
            if path == '/live/detection.jpg':
                try:return self.respond(self.server.preview.detection_image(),mime='image/jpeg')
                except Exception:return self.respond(dict(error='检测画面暂时不可用'),503)
            if path in ('/api/state', '/api/export'):
                state = self.server.store.snapshot(include_history=path.endswith('export'))
                state['runtime'] = self.server.engine.status()
                state['automatic_registration']=self.server.annotations.status()
                state['scene_initialization']=self.server.initialization.status()
                state['manual_control']=dict(available=self.server.manual.camera is not None,active=self.server.manual.active(),stop_error=self.server.manual.stop_error)
                state['capabilities'] = dict(review=True, scan_planning=True,
                    panoramic_stitching=False, imported_panorama_preview=True,
                    hardware_scan=self.server.initialization.backend is not None,
                    scene_initialization=self.server.initialization.backend is not None,
                    periodic_capture=self.server.engine.adapter.mode=='software_preset_capture',
                    single_round_capture=self.server.engine.adapter.mode=='software_preset_capture',
                    automatic_registration=self.server.annotations.enabled,
                    reading=False, revisit_reference_planning=True,
                    first_capture_planning=True, first_capture_execution=self.server.first_capture.enabled)
                if path=='/api/state':
                    from review_groups import decorate_review
                    decorate_review(decorate_state(state))
                return self.respond(state, download='inspection-registry.json' if path.endswith('export') else None)
            if path.startswith('/images/'):
                file = self.server.store.image_path(path[len('/images/'):])
                return self.respond(file.read_bytes(), mime='image/png' if file.suffix == '.png' else 'image/jpeg')
            return self.respond(dict(error='not found'), 404)
        except (ValueError, OSError):
            return self.respond(dict(error='resource unavailable'), 404)

    @staticmethod
    def fields(body, required, optional=()):
        if not isinstance(body, dict) or not set(required) <= set(body) or set(body) - set(required) - set(optional):
            raise ValueError('request fields do not match this operation')

    def do_POST(self):
        with self.server.operation_lock:
            return self.post_locked()

    def reject_post(self,message,status):
        # Drain only a small explicitly sized body before closing an HTTP/1.0
        # rejection. Closing with unread data can reset Windows clients before
        # they receive the intended 403/415 response.
        try:
            length=int(self.headers.get('Content-Length','0'))
            if 0<length<=65536 and not self.headers.get('Transfer-Encoding'):
                self.connection.settimeout(.25)
                self.rfile.read(length)
        except (ValueError,OSError):
            pass
        return self.respond(dict(error=message),status)

    def post_locked(self):
        if not self.host_ok() or self.headers.get('X-Control-Token') != self.server.token:
            return self.reject_post('control token or host rejected',403)
        origin = self.headers.get('Origin')
        if origin is not None and origin != 'http://' + self.headers.get('Host', ''):
            return self.reject_post('cross-origin request rejected',403)
        if self.headers.get('Content-Type', '').split(';')[0] != 'application/json':
            return self.reject_post('JSON content type required',415)
        try:
            length = int(self.headers.get('Content-Length', '0'))
            if not 2 <= length <= 65536 or self.headers.get('Transfer-Encoding'):
                raise ValueError('invalid request size')
            self.connection.settimeout(5)
            def invalid_constant(value):
                raise ValueError('non-finite JSON number')
            body = json.loads(self.rfile.read(length).decode('utf-8'), parse_constant=invalid_constant)
            if isinstance(body, dict) and 'revision' in body and (type(body['revision']) is not int or body['revision'] < 0):
                raise ValueError('a nonnegative integer revision is required')
            path = urlsplit(self.path).path
            store, engine = self.server.store, self.server.engine
            first_capture = self.server.first_capture
            registration=self.server.annotations.status()
            initialization=self.server.initialization.status()
            if self.server.manual.stop_error and path not in ('/api/schedule/stop','/api/camera/stop'):
                raise Conflict('manual STOP is unconfirmed')
            if self.server.manual.active() and path not in ('/api/camera/move','/api/camera/stop','/api/schedule/stop'):
                raise Conflict('manual camera movement is active')
            if initialization['stop_error'] and path not in ('/api/schedule/stop','/api/initialization/stop'):
                raise Conflict('initialization STOP is unconfirmed')
            if initialization['active'] and path not in ('/api/schedule/stop','/api/initialization/stop'):
                if not (initialization['job']['status']=='commissioning' and path in
                        ('/api/candidates/add','/api/annotations/from-overview')):
                    raise Conflict('场景初始化正在执行，可查看进度或停止')
            if registration.get('stop_error') and path in ('/api/patrol/run-once','/api/schedule/arm','/api/annotations/discover'):
                raise ValueError('automatic registration STOP is unconfirmed')
            if (registration['active'] or registration['queued'] and not registration['paused']) and path in ('/api/patrol/run-once','/api/schedule/arm'):
                raise Conflict('自动建档队列完成后才能开始巡检')
            if registration['active'] and path not in ('/api/schedule/stop','/api/candidates/add',
                    '/api/annotations/from-overview','/api/annotations/stop','/api/initialization/stop') and not path.startswith('/api/plan/'):
                raise Conflict('自动建档正在进行，可补充漏检框或暂停任务')
            if (first_capture.status()['active'] and path not in
                    ('/api/first-capture/stop', '/api/schedule/stop') and not path.startswith('/api/plan/')):
                raise Conflict('首次采集正在进行，请先停止任务再修改清单')
            if path == '/api/camera/move':
                self.fields(body,('revision','command'),('view_token','x','y','step'))
                self.server.manual_idle()
                if body['command']=='center' and not self.server.preview.status()['available']:
                    raise Conflict('fresh live video is required for click-to-center')
                result=self.server.manual.start(body['command'],body.get('view_token'),body.get('x'),body.get('y'),body.get('step',2.))
            elif path == '/api/initialization/plan':
                self.fields(body,('revision','bounds'))
                self.server.manual_idle()
                result=self.server.initialization.prepare(body['revision'],body['bounds'])
            elif path == '/api/initialization/commission':
                self.fields(body,('revision','map_id'))
                self.server.manual_idle()
                result=self.server.initialization.commission(body['revision'],body['map_id'])
            elif path == '/api/initialization/start':
                self.fields(body,('revision',),('review_first','fingerprint'))
                result=self.server.initialization.start(body['revision'],body.get('review_first',False),body.get('fingerprint'))
            elif path == '/api/initialization/stop':
                self.fields(body,('revision',))
                # Progress revisions change frequently; a stop must not be
                # rejected just because the latest frame arrived meanwhile.
                result=self.server.initialization.stop()
            elif path == '/api/review':
                self.fields(body, ('revision', 'id', 'action'), ('name', 'target_id', 'bbox'))
                result = store.review(body['revision'], body['id'], body['action'],
                    name=body.get('name'), target_id=body.get('target_id'), box=body.get('bbox'))
            elif path == '/api/review/many':
                self.fields(body, ('revision', 'ids', 'action'))
                result = store.review_many(body['revision'], body['ids'], body['action'])
            elif path == '/api/overview/sources':
                self.fields(body, ('revision', 'map_id', 'overview_sha256', 'bbox'))
                state = store.snapshot()
                if body['revision'] != state['revision']:
                    raise Conflict('review changed; refresh the overview')
                result = source_proposal(state, body['map_id'], body['overview_sha256'], body['bbox'])
            elif path == '/api/candidates/from-overview':
                self.fields(body, ('revision', 'map_id', 'overview_sha256', 'bbox',
                                   'frame_id', 'source_bbox', 'source_reviewed'))
                result = store.add_overview_candidate(body['revision'], body['map_id'],
                    body['overview_sha256'], body['bbox'], body['frame_id'],
                    body['source_bbox'], body['source_reviewed'])
            elif path == '/api/candidates/add':
                self.fields(body, ('revision', 'frame_id', 'bbox'))
                result = (self.server.annotations.queue(body['revision'],body['frame_id'],body['bbox'])
                          if self.server.annotations.enabled else store.add_candidate(body['revision'],body['frame_id'],body['bbox']))
            elif path == '/api/annotations/from-overview':
                self.fields(body,('revision','map_id','overview_sha256','bbox'))
                state=store.snapshot()
                if state['revision']!=body['revision']:raise Conflict('scene changed; refresh before adding a missed box')
                proposal=source_proposal(state,body['map_id'],body['overview_sha256'],body['bbox'])
                choice=proposal['choices'][0]
                result=self.server.annotations.queue(body['revision'],choice['frame_id'],choice['bbox'])
            elif path == '/api/annotations/discover':
                self.fields(body,('revision','map_id'),('candidate_ids',))
                result=self.server.annotations.discover(body['revision'],body['map_id'],body.get('candidate_ids'))
            elif path == '/api/annotations/stop':
                self.fields(body,('revision',))
                result=self.server.annotations.stop()
            elif path == '/api/annotations/resume':
                self.fields(body,('revision',))
                result=self.server.annotations.resume(body['revision'])
            elif path == '/api/instruments/update':
                self.fields(body, ('revision', 'id', 'name', 'enabled'))
                result = store.update_instrument(body['revision'], body['id'], body['name'], body['enabled'])
            elif path == '/api/instruments/rename':
                self.fields(body, ('revision', 'id', 'name'))
                result = store.rename_instrument(body['revision'], body['id'], body['name'])
            elif path == '/api/maps/invalidate':
                self.fields(body, ('revision', 'map_id'))
                result = store.invalidate(body['revision'], body['map_id'])
            elif path == '/api/schedule/configure':
                self.fields(body, ('revision', 'map_id', 'interval_seconds'),('round_limit',))
                result = store.configure_schedule(body['revision'], body['map_id'], body['interval_seconds'],body.get('round_limit'))
            elif path == '/api/schedule/arm':
                self.fields(body, ('revision',))
                result = engine.arm(body['revision'])
            elif path == '/api/patrol/run-once':
                self.fields(body,('revision','map_id'))
                result=engine.run_once(body['revision'],body['map_id'])
            elif path in ('/api/schedule/stop','/api/camera/stop'):
                self.fields(body, ('revision',))
                result=dict(patrol=engine.stop(None))
                if self.server.manual.camera is not None:result['manual']=self.server.manual.stop()
                if self.server.initialization.status()['active']:result['initialization']=self.server.initialization.stop()
                if first_capture.status()['active']:result['first_capture']=first_capture.stop()
                if self.server.annotations.enabled:result['annotations']=self.server.annotations.stop()
            elif path == '/api/plan/first-capture':
                self.fields(body, ('map_id', 'revision', 'start'), ('mode',))
                state = store.snapshot()
                if state['revision'] != body['revision']:
                    raise Conflict('review changed; regenerate first-capture plan')
                result = first_capture_plan(state, body['map_id'], body['start'],mode=body.get('mode','adaptive'))
            elif path == '/api/first-capture/prepare':
                self.fields(body, ('map_id', 'revision'), ('mode',))
                result = first_capture.prepare(body['map_id'], body['revision'],body.get('mode','adaptive'))
            elif path == '/api/first-capture/start':
                self.fields(body, ('map_id', 'revision', 'fingerprint', 'site_ready'))
                result = first_capture.start(body['map_id'], body['revision'], body['fingerprint'], body['site_ready'])
            elif path == '/api/first-capture/stop':
                self.fields(body, ('revision',))
                result = first_capture.stop()
            elif path == '/api/plan/scan':
                result = scan_grid(body)
            elif path == '/api/plan/route':
                self.fields(body, ('map_id', 'start'))
                state = store.snapshot()
                store.find(state, 'maps', body['map_id'])
                result = route([a for a in state['instruments'] if a['map_id'] == body['map_id']], body['start'])
            elif path == '/api/plan/references':
                self.fields(body, ('map_id', 'revision'))
                result = reference_plan(store.snapshot(), body['map_id'], body['revision'])
            elif path == '/api/plan/capture-round':
                self.fields(body, ('map_id','revision','start'))
                state=store.snapshot()
                if state['revision']!=body['revision']:raise Conflict('review changed; regenerate the round plan')
                result=round_plan(state,body['map_id'],body['start'])
            else:
                return self.respond(dict(error='not found'), 404)
            return self.respond(result)
        except Conflict as exc:
            return self.respond(dict(error=str(exc), refresh_required=True), 409)
        except (ValueError, KeyError, TypeError) as exc:
            return self.respond(dict(error=str(exc)), 400)
        except Exception as exc:
            # Never return credential-bearing adapter exceptions or file paths.
            return self.respond(dict(error=type(exc).__name__), 500)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--data', default=str(ROOT.parents[1] / 'var' / 'inspection-console'))
    parser.add_argument('--host', default='127.0.0.1')
    parser.add_argument('--port', type=int, default=8775)
    parser.add_argument('--allowed-host', action='append', default=[])
    parser.add_argument('--preview-port',type=int,help='Enable read-only relay to a loopback RTSP preview service')
    parser.add_argument('--first-capture-camera', help='Optional camera origin for finite first captures')
    parser.add_argument('--first-capture-user')
    parser.add_argument('--first-capture-console-root')
    parser.add_argument('--first-capture-project-root')
    parser.add_argument('--first-capture-password-stdin', action='store_true')
    parser.add_argument('--software-patrol',action='store_true',
                        help='Use the optional camera adapter for commissioned software-preset patrols')
    parser.add_argument('--auto-annotations',action='store_true',help='Fixed-site automatic commissioning of queued missed boxes')
    parser.add_argument('--meter-detector-root',help='Optional existing single-class NCNN detector for automatic model proposal rechecks')
    parser.add_argument('--scene-initialization',action='store_true',help='Enable finite TX2-local ring scan, discovery and commissioning')
    parser.add_argument('--preview-focal-pixels',type=float,default=1542.84959310078,
                        help='Uncalibrated fallback wide-lens focal estimate for this camera profile')
    args = parser.parse_args()
    capture_args = (args.first_capture_camera, args.first_capture_user,
                    args.first_capture_console_root, args.first_capture_project_root)
    if any(capture_args) and (not all(capture_args) or not args.first_capture_password_stdin
                              or not __import__('sys').platform.startswith('linux')):
        parser.error('first capture needs Linux, all four adapter arguments and --first-capture-password-stdin')
    first_capture = None
    patrol_adapter = None
    annotations=None
    initialization=None
    if args.scene_initialization and (not args.auto_annotations or not args.meter_detector_root):
        parser.error('--scene-initialization needs --auto-annotations and --meter-detector-root')
    if args.auto_annotations and not args.software_patrol:
        parser.error('--auto-annotations requires --software-patrol and a fixed camera/site')
    if args.software_patrol and not all(capture_args):
        parser.error('--software-patrol requires the explicit camera adapter arguments')
    quit_event = threading.Event()
    with instance_lock(Path(args.data)):
        store = Store(args.data)
        if all(capture_args):
            import sys
            sys.path.insert(0, str(Path(args.first_capture_project_root) / 'scripts'))
            from capture_review_views import SnapshotCamera
            auth = json.load(sys.stdin)
            camera = SnapshotCamera(args.first_capture_camera, args.first_capture_user, auth.pop('password'))
            if args.software_patrol:
                from preset_patrol import SoftwarePresetAdapter
                patrol_adapter=SoftwarePresetAdapter(store,camera,args.first_capture_project_root,
                                                     args.first_capture_console_root)
                if args.auto_annotations:
                    from region_detector import RegionDetector
                    detector=RegionDetector(args.meter_detector_root) if args.meter_detector_root else None
                    annotations=AnnotationService(store,camera,args.first_capture_project_root,
                                                  args.first_capture_console_root,detector)
                    if args.scene_initialization:
                        from scene_initialization_io import SceneInitializationIO
                        backend=SceneInitializationIO(camera,args.first_capture_project_root,detector,args.preview_focal_pixels)
                        initialization=SceneInitialization(store,annotations,backend,args.first_capture_project_root)
            else:
                first_capture = FirstCaptureService(store, args.first_capture_project_root,
                                                    args.first_capture_console_root, camera)
        manual=None
        if all(capture_args):
            from capture_review_views import motion_lease,check_other_services
            manual=ManualControl(camera,args.preview_focal_pixels,lease=motion_lease,check=check_other_services)
        server = Server((args.host, args.port), store, args.allowed_host,args.preview_port,first_capture,patrol_adapter,annotations,initialization,manual)
        # The manual callback also checks external services, and does not treat
        # its own worker as an overlapping automatic job.
        if manual:
            def idle_check():
                server.manual_idle();check_other_services()
            manual.check=idle_check
        server.annotations.recover()
        server.initialization.recover()
        server.timeout = .25
        def stop(*unused):
            quit_event.set()
        signal.signal(signal.SIGINT, stop)
        signal.signal(signal.SIGTERM, stop)
        def scheduler_loop():
            while not quit_event.wait(.25):
                try:
                    with server.operation_lock:
                        if server.manual.active():continue
                        server.initialization.tick()
                        server.annotations.tick()
                        if not server.initialization.status()['active']:server.engine.tick()
                except Exception:
                    # A scheduler fault must not keep a schedule armed silently.
                    try:
                        server.engine.stop()
                    except Exception:
                        pass
        worker = threading.Thread(target=scheduler_loop, name='inspection-scheduler')
        worker.daemon = True
        worker.start()
        print('Inspection console listening on %s:%d; %s; periodic disabled' %
              (args.host, args.port,server.engine.adapter.mode), flush=True)
        try:
            while not quit_event.is_set():
                server.handle_request()
        finally:
            quit_event.set()
            server.manual.close()
            server.initialization.close()
            server.first_capture.close()
            server.annotations.close()
            server.preview.close()
            if server.engine.active_id or server.store.snapshot()['schedule']['enabled']:
                server.engine.stop()
            if server.engine.worker is not None:
                server.engine.worker.join(timeout=20)
                if server.engine.worker.is_alive():raise RuntimeError('patrol_worker_shutdown_unconfirmed')
            worker.join(timeout=2)
            server.server_close()


if __name__ == '__main__':
    main()
