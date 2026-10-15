"""Real YOLO/RTSP observations and bounded ISAPI positioning. Python 3.5+."""
import base64
import json
import math
import statistics
import subprocess
import shutil
import sys
import threading
import time
import xml.etree.ElementTree as ET
from pathlib import Path


class RealIO:
    CALIBRATION_PRELOAD = 3.0
    CALIBRATION_STEPS = (1.5, 3.0)

    def __init__(self, app):
        self.app = app
        self.command = threading.RLock()
        self.stop_error = None
        self.last_position = None
        self.anchor = None
        self.calibrations = {}
        self.events = []
        self.capable = False
        self.focus_style = 'unknown'
        self.source_restarts = app.video_source.status().get('restarts', 0) if app.video_source else 0
        self._check_capabilities()
        self.anchor = self.position()

    def _check_capabilities(self):
        data = self.app.camera.request('/ISAPI/PTZCtrl/channels/1/capabilities')
        root = ET.fromstring(data)
        names = {node.tag.split('}')[-1] for node in root.iter()}
        if not {'AbsolutePanTiltPositionSpace', 'AbsoluteZoomPositionSpace'} <= names:
            raise RuntimeError('Camera does not advertise absolute PTZ positioning')
        # Demo profile is specifically for the tested S6 limits, never inferred from speed units.
        def ranges(name):
            node = next(n for n in root.iter() if n.tag.split('}')[-1] == name)
            return [float(n.text) for n in node.iter() if n.tag.split('}')[-1] in ('Min', 'Max')]
        self.limits = ranges('AbsolutePanTiltPositionSpace') + ranges('AbsoluteZoomPositionSpace')
        if len(self.limits) != 6 or self.limits != [0., 3500., 0., 900., 10., 20.]:
            raise RuntimeError('Camera limits differ from this S6 demo profile; review units first')
        try:
            values = self.app.camera.xml('/ISAPI/Image/channels/1/focusConfiguration')
            self.focus_style = values.get('focusStyle', 'unknown')
        except Exception:
            pass
        self.capable = True

    def event(self, kind, **data):
        data.update(event=kind, time=time.strftime('%Y-%m-%dT%H:%M:%S%z'))
        self.events.append(data)
        self.events = self.events[-200:]
        self.app.log('inspection-' + kind, **{k: v for k, v in data.items() if k not in ('event', 'time')})

    def position(self):
        with self.command:
            values = self.app.motion.status()
        result = {key: float(values[field]) / 10. for key, field in
                  [('pan', 'azimuth'), ('tilt', 'elevation'), ('zoom', 'absoluteZoom')]}
        if not all(math.isfinite(v) for v in result.values()):
            raise RuntimeError('Non-finite camera position')
        self.last_position = result
        return dict(result)

    def stop(self):
        with self.command:
            try:
                self.app.motion.stop()
                self.stop_error = None
            except Exception as exc:
                self.stop_error = 'STOP unconfirmed: ' + type(exc).__name__
                self.event('stop-failed', error=self.stop_error)
                raise RuntimeError(self.stop_error)

    @staticmethod
    def check_cancel(cancel):
        if cancel.is_set():
            raise RuntimeError('Inspection cancelled')

    def check_source(self):
        if self.app.engine.p.poll() is not None:
            raise RuntimeError('Detector process exited')
        with self.app.lock:
            error = self.app.state.get('error')
        if error:
            raise RuntimeError('Capture/inference error: ' + str(error))
        if self.app.video_source:
            status = self.app.video_source.status()
            if status.get('restarts', 0) != self.source_restarts:
                self.calibrations.clear()
                raise RuntimeError('RTSP reconnected; restart demo and recalibrate')
            age = status.get('last_frame_age')
            if age is None or not math.isfinite(age) or not 0 <= age <= 1.5:
                raise RuntimeError('RTSP frame source is stale')

    def wait(self, seconds, cancel):
        if cancel.wait(seconds):
            raise RuntimeError('Inspection cancelled')

    def move(self, pan, tilt, zoom, cancel):
        target = dict(pan=round(float(pan), 1), tilt=round(float(tilt), 1), zoom=round(float(zoom), 1))
        if not all(math.isfinite(v) for v in target.values()):
            raise ValueError('Invalid position')
        if not (0 <= target['pan'] <= 350 and 0 <= target['tilt'] <= 90 and 1 <= target['zoom'] <= 1.5):
            raise ValueError('Position exceeds demo limits')
        if self.anchor and (abs(target['pan'] - self.anchor['pan']) > 25 or
                            abs(target['tilt'] - self.anchor['tilt']) > 15):
            raise ValueError('Position outside local demo envelope')
        raw = {k: int(round(v * 10)) for k, v in target.items()}
        body = ('<PTZData version="2.0" xmlns="http://www.hikvision.com/ver20/XMLSchema">'
                '<AbsoluteHigh><elevation>{tilt}</elevation><azimuth>{pan}</azimuth>'
                '<absoluteZoom>{zoom}</absoluteZoom></AbsoluteHigh></PTZData>').format(**raw).encode()
        deadline = time.monotonic() + 7
        settled = 0
        try:
            with self.command:
                self.check_cancel(cancel)
                self.check_source()
                if self.stop_error:
                    raise RuntimeError(self.stop_error)
                self.app.motion.xml('/ISAPI/PTZCtrl/channels/1/absolute', body)
                self.event('move', target=target)
            while time.monotonic() < deadline:
                self.check_cancel(cancel)
                self.check_source()
                actual = self.position()
                reached = (abs(actual['pan'] - target['pan']) <= .3 and
                           abs(actual['tilt'] - target['tilt']) <= .3 and
                           abs(actual['zoom'] - target['zoom']) <= .05)
                settled = settled + 1 if reached else 0
                if settled >= 3:
                    self.stop()
                    self.wait(.5, cancel)
                    self.event('arrived', position=actual)
                    return actual
                self.wait(.12, cancel)
            raise RuntimeError('Absolute positioning timed out')
        finally:
            self.stop()

    def observe(self, after=None, timeout=3):
        # Default requires a newly received, newly processed frame after this call.
        started = time.monotonic()
        deadline = started + timeout
        while time.monotonic() < deadline:
            self.check_source()
            with self.app.lock:
                p = dict(self.app.preview_state) if self.app.preview_state else None
                jpeg = self.app.jpeg
                received = self.app.preview_received
                error = self.app.state.get('error')
                seq = p.get('frames', 0) if p else 0
            if error:
                raise RuntimeError('Capture/inference error: ' + str(error))
            age = time.monotonic() - received
            if p and jpeg and math.isfinite(received) and 0 <= age <= .8 and received > (started if after is None else after):
                return dict(sequence=seq, received_at=received, persons=p['persons'], jpeg=jpeg,
                            width=p.get('width'), height=p.get('height'))
            if self.app.engine.p.poll() is not None:
                raise RuntimeError('Detector process exited')
            time.sleep(.025)
        raise RuntimeError('No fresh matched detection frame')

    def _calibration_direction(self, axis, pose):
        # These are absolute-angle margins, not motor speed or angular rate.
        radius, maximum = (25., 350.) if axis == 'pan' else (15., 90.)
        lower, upper = 0., maximum
        if self.anchor:
            lower = max(lower, self.anchor[axis] - radius)
            upper = min(upper, self.anchor[axis] + radius)
        # Preload and measurement use the same direction. Reserve their full
        # possible span plus the move's .3-degree arrival tolerance.
        reserve = self.CALIBRATION_PRELOAD + max(self.CALIBRATION_STEPS) + .3
        if not lower <= pose[axis] <= upper:
            raise RuntimeError('Current position outside safe calibration envelope')
        if upper - pose[axis] >= reserve:
            return 1.
        if pose[axis] - lower >= reserve:
            return -1.
        raise RuntimeError('Insufficient safe range for bounded calibration')

    def _calibration_frame(self, cancel, hold_reference=False):
        self.check_cancel(cancel)
        self.check_source()
        with self.app.lock:
            previous_frame = self.app.frames
            previous_source = self.app.source_sequence
        started = time.monotonic()
        result = self.app.capture(hold_reference=hold_reference)
        self.check_cancel(cancel)
        self.check_source()
        # App.capture() owns the source barrier while calibrating and updates
        # these fields from the same inference. They remain local receive times,
        # not a guarantee of the camera's original exposure timestamp.
        with self.app.lock:
            frame = dict(shift=result['shift'], response=result['shift_response'],
                         sequence=self.app.frames, source_sequence=self.app.source_sequence,
                         received_at=self.app.last_request,
                         decoded_frame_age_ms=self.app.state.get('decoded_frame_age_ms'))
        now = time.monotonic()
        if (frame['sequence'] <= previous_frame or not math.isfinite(frame['received_at']) or
                not started <= frame['received_at'] <= now or now - frame['received_at'] > 1.5 or
                (self.app.video_source and frame['source_sequence'] <= previous_source)):
            raise RuntimeError('Calibration requires a newly received, newly processed frame')
        return frame

    def calibrate(self, zoom, cancel):
        """Local, zoom-specific image displacement per degree for this fixed demo.

        Reusing a cache assumes the same base, image geometry, scene and small
        operating region. It is not a universal PTZ model or a velocity estimate.
        A second bounded measurement may help, but does not guarantee calibration.
        A same-direction preload precedes each reference to reduce sensitivity
        to reversal dead travel; it does not prove that reported angles equal
        physical optical-axis angles.
        """
        zoom = round(float(zoom), 1)
        self.check_cancel(cancel)
        self.check_source()
        if zoom in self.calibrations:
            return self.calibrations[zoom]
        home = self.position()
        # Reject insufficient range on either axis before any nonzero command.
        directions = {axis: self._calibration_direction(axis, home) for axis in ('pan', 'tilt')}
        columns = []
        # Own the detector stream during held-reference flow requests.
        with self.app.operation:
            self.app.calibrating = True
            try:
                for axis in ('pan', 'tilt'):
                    direction = directions[axis]
                    for attempt, step in enumerate(self.CALIBRATION_STEPS, 1):
                        before = after = reference = delta = column = preload = None
                        observations = []
                        failure = None
                        try:
                            self.check_cancel(cancel)
                            self.check_source()
                            # Every attempt approaches its reference and probe
                            # in one direction after returning to the home pose.
                            # Measuring immediately after a reversal can report
                            # a position change with little optical displacement.
                            self.move(home['pan'], home['tilt'], zoom, cancel)
                            preload_target = dict(home, zoom=zoom)
                            preload_target[axis] += direction * self.CALIBRATION_PRELOAD
                            preload = self.move(preload_target['pan'], preload_target['tilt'], zoom, cancel)
                            self.wait(.35, cancel)
                            reference = self._calibration_frame(cancel)
                            before = self.position()
                            target = dict(before, zoom=zoom)
                            target[axis] += direction * step
                            self.move(target['pan'], target['tilt'], zoom, cancel)
                            after = self.position()
                            delta = after[axis] - before[axis]
                            for unused in range(5):
                                observations.append(self._calibration_frame(cancel, hold_reference=True))
                            accepted = [o['shift'] for o in observations[-3:]
                                        if math.isfinite(o['response']) and o['response'] >= .6 and
                                        len(o['shift']) == 2 and all(math.isfinite(v) for v in o['shift'])]
                            if delta * direction < 1.:
                                failure = 'Insufficient or opposite actual calibration angle'
                            elif len(accepted) < 2:
                                failure = 'Calibration optical flow inconsistent; keep scene stationary'
                            else:
                                column = [statistics.median(v[i] for v in accepted) / delta for i in range(2)]
                                if (not all(math.isfinite(v) for v in column) or
                                        not .001 <= math.hypot(*column) <= .1):
                                    failure = 'Calibration column is weak or exceeds local model limits'
                        except Exception as exc:
                            # Transport/source/cancellation errors never trigger
                            # another nonzero movement, including a return home.
                            self.event('calibration-attempt', axis=axis, zoom=zoom, attempt=attempt,
                                       step_degrees=step, direction=direction, before=before, after=after,
                                       home=home, preload=preload, preload_degrees=self.CALIBRATION_PRELOAD,
                                       reference_pose=before,
                                       delta=delta, reference=reference, observations=observations,
                                       column=column, accepted=False,
                                       failure_reason=type(exc).__name__ + ': ' + str(exc))
                            raise
                        self.event('calibration-attempt', axis=axis, zoom=zoom, attempt=attempt,
                                   step_degrees=step, direction=direction, before=before, after=after,
                                   home=home, preload=preload, preload_degrees=self.CALIBRATION_PRELOAD,
                                   reference_pose=before,
                                   delta=delta, reference=reference, observations=observations,
                                   column=column, accepted=failure is None, failure_reason=failure)
                        if failure is None:
                            columns.append(column)
                            self.event('calibration-axis', axis=axis, zoom=zoom, attempt=attempt,
                                       before=before, after=after, delta=delta,
                                       home=home, preload=preload, preload_degrees=self.CALIBRATION_PRELOAD,
                                       reference_pose=before,
                                       reference=reference, observations=observations, column=column)
                            break
                        if attempt == 2:
                            raise RuntimeError('Calibration failed after two bounded attempts: ' + failure)
                matrix = [[columns[0][0], columns[1][0]], [columns[0][1], columns[1][1]]]
                if not all(math.isfinite(v) for row in matrix for v in row):
                    raise RuntimeError('Calibration matrix is not finite')
                lengths = [math.hypot(*col) for col in columns]
                det = matrix[0][0] * matrix[1][1] - matrix[0][1] * matrix[1][0]
                if min(lengths) < .001 or max(lengths) > .1 or abs(det) / (lengths[0] * lengths[1]) < .25:
                    raise RuntimeError('Calibration matrix is weak or ambiguous')
                self.move(home['pan'], home['tilt'], zoom, cancel)
                self.check_cancel(cancel)
                self.check_source()
            finally:
                self.app.calibrating = False
                self.stop()
        # Commit only a complete two-axis result, after return and confirmed STOP.
        with self.command:
            self.check_cancel(cancel)
            self.check_source()
            if self.stop_error:
                raise RuntimeError(self.stop_error)
            self.event('calibration', zoom=zoom, matrix=matrix, home=home,
                       scope='fixed_base_local_scene_zoom_specific')
            self.calibrations[zoom] = matrix
            return matrix

    def save(self, observation, record, run_dir):
        box = record.get('bbox') or record.get('box')
        if not box:
            raise ValueError('Evidence requires selected target bbox')
        stem = str(record['target_id'])
        if not stem.replace('-', '').isalnum():
            raise ValueError('Invalid evidence ID')
        directory = Path(run_dir)
        directory.mkdir(parents=True, exist_ok=True)
        (directory / (stem + '.jpg')).write_bytes(observation['jpeg'])
        # This legacy TX2 has OpenCV's Python 2 bindings; no system package change needed.
        worker = Path(__file__).resolve().parent / 'evidence_worker.py'
        output = subprocess.check_output([shutil.which('python2') or sys.executable, str(worker),
                    str(directory / (stem + '.jpg')), str(directory / (stem + '-crop.jpg')),
                    json.dumps(box)], timeout=8)
        metrics = json.loads(output.decode('utf-8'))
        return dict(image=stem + '.jpg', crop=stem + '-crop.jpg', sharpness=metrics['sharpness'],
                    sharpness_status='recorded_not_meter_validated', focus_style=self.focus_style,
                    source_sequence=observation['sequence'])
