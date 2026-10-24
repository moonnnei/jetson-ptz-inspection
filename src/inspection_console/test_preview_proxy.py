"""Real local HTTP checks for preview isolation, freshness and byte forwarding."""
from http.server import BaseHTTPRequestHandler,HTTPServer
import json
from pathlib import Path
from socketserver import ThreadingMixIn
import tempfile
import threading
import unittest
from urllib.error import HTTPError
from urllib.request import build_opener,ProxyHandler,Request

from preview_proxy import PreviewProxy
from server import Server
from store import Store

JPEG=b'\xff\xd8test-image\xff\xd9'
PART=b'--jetson-live\r\nContent-Type: image/jpeg\r\nContent-Length: '+str(len(JPEG)).encode()+b'\r\n\r\n'+JPEG+b'\r\n'


class FeedHandler(BaseHTTPRequestHandler):
    def log_message(self,*unused):pass

    def do_GET(self):
        self.server.paths.append(self.path)
        if self.path=='/api/status':
            raw=json.dumps(self.server.state).encode();kind='application/json'
        elif self.path=='/preview.jpg':raw=JPEG;kind='image/jpeg'
        elif self.path=='/live.mjpg':raw=PART+PART;kind='multipart/x-mixed-replace; boundary=jetson-live'
        else:self.send_error(404);return
        self.send_response(200);self.send_header('Content-Type',kind)
        self.send_header('Content-Length',str(len(raw)));self.end_headers();self.wfile.write(raw)


class Threaded( ThreadingMixIn,HTTPServer):
    daemon_threads=True


class PreviewTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.store=Store(Path(self.tmp.name)/'data')
        self.feed=Threaded(('127.0.0.1',0),FeedHandler)
        self.feed.paths=[]
        self.feed.state=dict(source=dict(state='streaming',last_frame_age=.04,sequence=17),
            result_age_ms=200,ipc_ms=300,processed_frames=9,preview_rotation=180,
            model='test-model',password='must-not-be-forwarded',error='private diagnostic')
        self.feed_thread=threading.Thread(target=self.feed.serve_forever);self.feed_thread.daemon=True;self.feed_thread.start()
        self.server=Server(('127.0.0.1',0),self.store,preview_port=self.feed.server_address[1])
        self.thread=threading.Thread(target=self.server.serve_forever);self.thread.daemon=True;self.thread.start()
        self.base='http://127.0.0.1:%d'%self.server.server_address[1]
        self.opener=build_opener(ProxyHandler({}))

    def tearDown(self):
        self.server.preview.close();self.server.shutdown();self.server.server_close();self.thread.join(2)
        self.feed.shutdown();self.feed.server_close();self.feed_thread.join(2);self.tmp.cleanup()

    def get(self,path):return self.opener.open(self.base+path,timeout=3)

    def test_status_is_whitelisted_and_does_not_change_registry(self):
        before=self.store.snapshot()
        with self.get('/api/live/status') as response:raw=response.read()
        report=json.loads(raw.decode())
        self.assertTrue(report['available']);self.assertEqual(report['rotation'],180)
        self.assertEqual(report['source_age_ms'],40)
        self.assertNotIn(b'must-not-be-forwarded',raw);self.assertNotIn(b'private diagnostic',raw)
        self.assertEqual(self.store.snapshot(),before)

    def test_mjpeg_bytes_and_detection_jpeg_are_forwarded_without_camera_operations(self):
        with self.get('/live.mjpg') as response:
            self.assertIn('multipart/x-mixed-replace',response.headers['Content-Type'])
            self.assertEqual(response.read(),PART+PART)
        with self.get('/live/detection.jpg') as response:self.assertEqual(response.read(),JPEG)
        self.assertTrue(set(self.feed.paths)<={'/api/status','/live.mjpg','/preview.jpg'})
        self.assertFalse(self.store.snapshot()['schedule']['enabled'])

    def test_stale_source_and_stale_detection_never_serve_old_detection_as_live(self):
        for source_age,detection_age in ((5,200),(.04,9000)):
            self.feed.state['source']['last_frame_age']=source_age
            self.feed.state['result_age_ms']=detection_age
            with self.assertRaises(HTTPError) as error:self.get('/live/detection.jpg')
            self.assertEqual(error.exception.code,503);error.exception.close()

    def test_stream_connection_limit_is_released_after_each_view(self):
        proxy=self.server.preview
        self.assertTrue(proxy.clients.acquire(False));self.assertTrue(proxy.clients.acquire(False))
        try:
            with self.assertRaises(HTTPError) as error:self.get('/live.mjpg')
            self.assertEqual(error.exception.code,429);error.exception.close()
        finally:proxy.clients.release();proxy.clients.release()
        with self.get('/live.mjpg') as response:self.assertEqual(response.read(),PART+PART)
        self.assertTrue(proxy.clients.acquire(False));proxy.clients.release()

    def test_unconfigured_preview_never_attempts_upstream_and_has_no_control_effect(self):
        self.server.preview=PreviewProxy()
        with self.get('/api/live/status') as response:self.assertFalse(json.loads(response.read().decode())['enabled'])
        with self.assertRaises(HTTPError) as error:self.get('/live.mjpg')
        self.assertEqual(error.exception.code,503);error.exception.close()
        self.assertEqual(self.feed.paths,[])
        self.assertFalse(self.server.engine.status()['control_enabled'])

    def test_browser_cannot_choose_a_remote_upstream_or_send_camera_commands(self):
        with self.get('/api/live/status?url=http://untrusted.invalid/') as response:
            self.assertTrue(json.loads(response.read().decode())['available'])
        self.assertEqual(self.feed.paths,['/api/status'])
        request=Request(self.base+'/live.mjpg',data=b'{}',headers={
            'Content-Type':'application/json','X-Control-Token':self.server.token})
        with self.assertRaises(HTTPError) as error:self.opener.open(request,timeout=3)
        self.assertEqual(error.exception.code,404);error.exception.close()

    def test_invalid_loopback_ports_are_rejected(self):
        for port in (True,0,65536,'8774','http://camera/'):
            with self.assertRaises(ValueError):PreviewProxy(port)


if __name__=='__main__':unittest.main()
