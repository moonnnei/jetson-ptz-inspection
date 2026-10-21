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
from ring_coordinates import decorate_state
from scheduler import Scheduler
from store import Conflict, Store

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

    def __init__(self, address, store, allowed_hosts=None):
        HTTPServer.__init__(self, address, Handler)
        self.store = store
        self.engine = Scheduler(store)
        self.token = os.urandom(32).hex()
        port = self.server_address[1]
        names = set(allowed_hosts or []) | {'127.0.0.1', 'localhost'}
        if address[0] != '0.0.0.0':
            names.add(address[0])
        self.hosts = {name + ':' + str(port) for name in names}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *unused):
        pass

    def respond(self, value, status=200, mime='application/json; charset=utf-8', download=None):
        data = value if isinstance(value, bytes) else json.dumps(value, ensure_ascii=False,
                                                                allow_nan=False).encode('utf-8')
        self.connection.settimeout(5)
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
            if path in ('/app.js', '/style.css'):
                return self.respond((ROOT / path[1:]).read_bytes(),
                    mime='application/javascript; charset=utf-8' if path.endswith('.js') else 'text/css; charset=utf-8')
            if path in ('/api/state', '/api/export'):
                state = self.server.store.snapshot(include_history=path.endswith('export'))
                state['runtime'] = self.server.engine.status()
                state['capabilities'] = dict(review=True, scan_planning=True,
                    panoramic_stitching=False, imported_panorama_preview=True, hardware_scan=False,
                    periodic_capture=False, reading=False, revisit_reference_planning=True)
                if path=='/api/state':decorate_state(state)
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
        if not self.host_ok() or self.headers.get('X-Control-Token') != self.server.token:
            return self.respond(dict(error='control token or host rejected'), 403)
        origin = self.headers.get('Origin')
        if origin is not None and origin != 'http://' + self.headers.get('Host', ''):
            return self.respond(dict(error='cross-origin request rejected'), 403)
        if self.headers.get('Content-Type', '').split(';')[0] != 'application/json':
            return self.respond(dict(error='JSON content type required'), 415)
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
            if path == '/api/review':
                self.fields(body, ('revision', 'id', 'action'), ('name', 'target_id', 'bbox'))
                result = store.review(body['revision'], body['id'], body['action'],
                    name=body.get('name'), target_id=body.get('target_id'), box=body.get('bbox'))
            elif path == '/api/candidates/add':
                self.fields(body, ('revision', 'frame_id', 'bbox'))
                result = store.add_candidate(body['revision'], body['frame_id'], body['bbox'])
            elif path == '/api/instruments/update':
                self.fields(body, ('revision', 'id', 'name', 'enabled'))
                result = store.update_instrument(body['revision'], body['id'], body['name'], body['enabled'])
            elif path == '/api/maps/invalidate':
                self.fields(body, ('revision', 'map_id'))
                result = store.invalidate(body['revision'], body['map_id'])
            elif path == '/api/schedule/configure':
                self.fields(body, ('revision', 'map_id', 'interval_seconds'))
                result = store.configure_schedule(body['revision'], body['map_id'], body['interval_seconds'])
            elif path == '/api/schedule/arm':
                self.fields(body, ('revision',))
                result = engine.arm(body['revision'])
            elif path == '/api/schedule/stop':
                self.fields(body, ('revision',))
                result = engine.stop(body['revision'])
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
    args = parser.parse_args()
    quit_event = threading.Event()
    with instance_lock(Path(args.data)):
        server = Server((args.host, args.port), Store(args.data), args.allowed_host)
        server.timeout = .25
        def stop(*unused):
            quit_event.set()
        signal.signal(signal.SIGINT, stop)
        signal.signal(signal.SIGTERM, stop)
        def scheduler_loop():
            while not quit_event.wait(.25):
                try:
                    server.engine.tick()
                except Exception:
                    # A scheduler fault must not keep a schedule armed silently.
                    try:
                        server.engine.stop()
                    except Exception:
                        pass
        worker = threading.Thread(target=scheduler_loop, name='inspection-scheduler')
        worker.daemon = True
        worker.start()
        print('Inspection review console listening on %s:%d; hardware disabled' %
              (args.host, args.port), flush=True)
        try:
            while not quit_event.is_set():
                server.handle_request()
        finally:
            quit_event.set()
            if server.engine.active_id or server.store.snapshot()['schedule']['enabled']:
                server.engine.stop()
            worker.join(timeout=2)
            server.server_close()


if __name__ == '__main__':
    main()
