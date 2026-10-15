"""Bounded MJPEG display of the latest source JPEG, independent of detection.

No image encoding, rotation, detector calls or camera-control commands occur
here. Each client owns its read sequence, so preview cannot advance the
application's inference sequence. Python 3.5 socket.sendall uses a total
operation timeout, bounding each complete multipart frame write to one second.
"""
import threading


class LivePreview:
    BOUNDARY = b'jetson-live'
    WRITE_TIMEOUT = 1.0

    def __init__(self, capture, quit_event, max_clients=2):
        if capture is None:
            raise ValueError('Live preview requires an RTSP capture')
        if type(max_clients) is not int or not 1 <= max_clients <= 2:
            raise ValueError('Live preview allows one or two clients')
        self.capture = capture
        self.quit_event = quit_event
        self._clients = threading.BoundedSemaphore(max_clients)

    @staticmethod
    def _headers(handler, status, content_type, length=None):
        handler.send_response(status)
        handler.send_header('Content-Type', content_type)
        handler.send_header('Cache-Control', 'no-store, no-cache, must-revalidate')
        handler.send_header('Connection', 'close')
        if length is not None:
            handler.send_header('Content-Length', str(length))
        if status == 429:
            handler.send_header('Retry-After', '1')
        handler.end_headers()
        # Headers must precede direct socket writes even if a custom handler
        # buffers its wfile. Multipart body uses sendall to avoid partial writes.
        handler.wfile.flush()

    def _refuse(self, handler, status, body):
        try:
            handler.connection.settimeout(self.WRITE_TIMEOUT)
            self._headers(handler, status, 'text/plain; charset=utf-8', len(body))
            handler.connection.sendall(body)
        except Exception:
            # Socket/source diagnostics can include credentials. Only fixed
            # responses reach the client; failed connections are simply closed.
            pass

    def serve(self, handler):
        """Serve one HTTP handler until QUIT, source failure or write timeout."""
        handler.close_connection = True
        if self.quit_event.is_set():
            self._refuse(handler, 503, b'Live preview is stopping\n')
            return
        if not self._clients.acquire(blocking=False):
            self._refuse(handler, 429, b'Live preview client limit reached\n')
            return
        previous_timeout = None
        timeout_known = False
        try:
            previous_timeout = handler.connection.gettimeout()
            timeout_known = True
            handler.connection.settimeout(self.WRITE_TIMEOUT)
            self._headers(handler, 200,
                          'multipart/x-mixed-replace; boundary=' + self.BOUNDARY.decode('ascii'))
            sequence = 0
            while not self.quit_event.is_set():
                frame = self.capture.read(after_sequence=sequence, timeout=1)
                if self.quit_event.is_set():
                    break
                sequence = frame.sequence
                jpeg = frame.jpeg
                part = (b'--' + self.BOUNDARY + b'\r\nContent-Type: image/jpeg\r\n'
                        b'Content-Length: ' + str(len(jpeg)).encode('ascii') +
                        b'\r\nX-Source-Sequence: ' + str(sequence).encode('ascii') +
                        b'\r\n\r\n' + jpeg + b'\r\n')
                # One bounded write for the complete part, with no per-client
                # frame queue. A slow viewer cannot hold the detector or PTZ.
                handler.connection.sendall(part)
        except Exception:
            pass
        finally:
            if timeout_known:
                try:
                    handler.connection.settimeout(previous_timeout)
                except Exception:
                    pass
            self._clients.release()
