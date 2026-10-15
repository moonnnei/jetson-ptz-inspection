"""Private RTSP GI worker: stdin JSON configuration, stdout concatenated JPEG.

Invoked by rtsp_capture.py, not as an interactive CLI. No credentials occur in
argv, launch strings, diagnostic output or environment. Supports the TX2's
GStreamer 1.8.3 and Python 3.5 GI package.
"""
import json
import signal
import sys


def _launch_description(config):
    # Only validated numeric/non-secret settings enter launch syntax. Keep this
    # path identical to the working TX2 probe; connection secrets are properties.
    caps = 'video/x-raw,format=I420'
    if config['width'] is not None:
        caps += ',width={},height={}'.format(int(config['width']), int(config['height']))
    if config['decoder'] == 'hardware':
        decode = 'omxh264dec ! nvvidconv ! ' + caps
    else:
        decode = 'avdec_h264 ! videoconvert ! videoscale ! ' + caps
    return ('rtspsrc name=source latency={} protocols=tcp tcp-timeout={} '
            '! application/x-rtp,media=video,encoding-name=H264 '
            '! rtph264depay ! h264parse ! {} '
            '! queue leaky=downstream max-size-buffers=1 max-size-bytes=0 max-size-time=0 '
            '! videorate ! video/x-raw,framerate={}/1 ! jpegenc quality=85 '
            '! fdsink name=output fd=1 sync=false').format(
                int(config['latency']), int(config['tcp_timeout']), decode, int(config['fps']))


def main():
    pipeline = None
    try:
        payload = sys.stdin.buffer.read(4097)
        if len(payload) > 4096:
            return 2
        config = json.loads(payload.decode('utf-8'))
        import gi
        gi.require_version('Gst', '1.0')
        from gi.repository import Gst
        Gst.init(None)
        pipeline = Gst.parse_launch(_launch_description(config))
        source = pipeline.get_by_name('source')
        source.set_property('location', config['url'])
        source.set_property('user-id', config['user'])
        source.set_property('user-pw', config['password'])
        if source.find_property('drop-on-latency'):
            source.set_property('drop-on-latency', True)
        del config['password']
        stopping = [False]

        def stop(unused_signum, unused_frame):
            stopping[0] = True

        signal.signal(signal.SIGTERM, stop)
        signal.signal(signal.SIGINT, stop)
        if pipeline.set_state(Gst.State.PLAYING) == Gst.StateChangeReturn.FAILURE:
            return 2
        bus = pipeline.get_bus()
        while not stopping[0]:
            message = bus.timed_pop_filtered(100 * Gst.MSECOND,
                                            Gst.MessageType.ERROR | Gst.MessageType.EOS)
            if message is not None:
                # Deliberately do not parse/print source diagnostics: URLs or
                # credentials may be present. Parent reports a fixed error.
                return 2
        return 0
    except Exception:
        return 2
    finally:
        if pipeline is not None:
            try:
                pipeline.set_state(Gst.State.NULL)
            except Exception:
                pass


if __name__ == '__main__':
    sys.exit(main())
