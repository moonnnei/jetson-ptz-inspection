"""Read-only same-origin preview relay to an explicitly enabled loopback service.

The upstream owns RTSP capture. This relay never opens a camera connection,
starts inference or sends PTZ commands. Slow stream clients are disconnected.
"""
import json
import math
import threading
from urllib.request import build_opener, HTTPRedirectHandler, ProxyHandler


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


class PreviewProxy:
    def __init__(self, port=None):
        if port is not None and (type(port) is not int or not 1 <= port <= 65535):
            raise ValueError('preview port must be a local TCP port')
        self.port=port
        self.opener=build_opener(ProxyHandler({}),NoRedirect())
        self.clients=threading.BoundedSemaphore(2)
        self.closed=threading.Event()

    def open(self, path):
        if self.port is None or self.closed.is_set():
            raise ValueError('preview is not enabled')
        # All callers use fixed paths. No browser-supplied URL is accepted.
        return self.opener.open('http://127.0.0.1:%d%s'%(self.port,path),timeout=2)

    @staticmethod
    def nonnegative(value):
        if isinstance(value,bool) or not isinstance(value,(int,float)) or not math.isfinite(value) or value<0:
            return None
        return value

    def status(self):
        report=dict(enabled=self.port is not None,available=False,
                    rotation=0,source='rtsp',message='视频服务未连接')
        if self.port is None:return report
        try:
            with self.open('/api/status') as response:
                data=response.read(200001)
            if len(data)>200000:raise ValueError('oversized status')
            data=json.loads(data.decode('utf-8'))
            source=data.get('source',{})
            age=self.nonnegative(source.get('last_frame_age'))
            rotation=data.get('preview_rotation',0)
            if rotation not in (0,180):raise ValueError('unsupported preview orientation')
            report.update(available=source.get('state')=='streaming' and age is not None and age<=1.5,
                rotation=rotation,source_state=source.get('state') if source.get('state') in
                    ('starting','streaming','retrying','failed','closed','not-started') else 'unknown',
                source_sequence=self.nonnegative(source.get('sequence')),
                source_age_ms=None if age is None else round(age*1000,1),
                detection_age_ms=self.nonnegative(data.get('result_age_ms')),
                detection_ms=self.nonnegative(data.get('ipc_ms')),
                detection_sequence=self.nonnegative(data.get('processed_frames')),
                model=data.get('model') if isinstance(data.get('model'),str) else None)
            report['message']='视频已连接' if report['available'] else '视频暂时不可用，正在重连'
        except Exception:
            # Never forward upstream errors or secret-bearing decoder diagnostics.
            report['message']='视频暂时不可用，正在重连'
        return report

    def detection_image(self):
        status=self.status()
        age=status.get('detection_age_ms')
        if not status['available'] or age is None or age>3000:
            raise ValueError('fresh detection preview unavailable')
        with self.open('/preview.jpg') as response:
            if response.headers.get_content_type()!='image/jpeg':raise ValueError('not a JPEG')
            image=response.read(2000001)
        if len(image)>2000000 or not image.startswith(b'\xff\xd8') or not image.endswith(b'\xff\xd9'):
            raise ValueError('invalid detection preview')
        return image

    def stream(self, handler):
        if self.port is None or self.closed.is_set():
            return handler.respond(dict(error='实时视频未连接'),503)
        if not self.clients.acquire(blocking=False):
            return handler.respond(dict(error='实时监看连接已满，请稍后重试'),429)
        upstream=None
        headers_sent=False
        try:
            upstream=self.open('/live.mjpg')
            if upstream.headers.get('Content-Type','').split(';')[0]!='multipart/x-mixed-replace':
                raise ValueError('upstream is not a live stream')
            handler.close_connection=True
            handler.connection.settimeout(1)
            handler.send_response(200)
            handler.send_header('Content-Type',upstream.headers['Content-Type'])
            handler.send_header('Cache-Control','no-store, no-cache, must-revalidate')
            handler.send_header('Connection','close')
            handler.send_header('X-Content-Type-Options','nosniff')
            handler.end_headers();handler.wfile.flush()
            headers_sent=True
            while not self.closed.is_set():
                # read1 returns bytes already available rather than waiting to
                # accumulate several JPEGs into a full large read buffer.
                block=upstream.read1(16384)
                if not block:break
                handler.connection.sendall(block)
        except Exception:
            if not headers_sent:
                try:handler.respond(dict(error='视频暂时不可用'),503)
                except Exception:pass
        finally:
            if upstream is not None:upstream.close()
            self.clients.release()

    def close(self):
        self.closed.set()
