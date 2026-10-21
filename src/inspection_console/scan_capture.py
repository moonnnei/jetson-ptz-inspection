"""Finite three-view acquisition for commissioning, not a full-panorama scan.

The IO object owns real commands. The core can be tested without a camera. Only
successful acquisition and final STOP produce a completed import manifest.
"""
import hashlib
import json
import math
import os
from pathlib import Path
import threading
import time

from planner import pose
from store import identifier, image_size, now


def commissioning_points(anchor):
    anchor = pose(anchor)
    pans = sorted(set([round(max(0, anchor['pan'] - 5), 1), anchor['pan'],
                       round(min(350, anchor['pan'] + 5), 1)]))
    return [dict(pan=p, tilt=anchor['tilt'], zoom=anchor['zoom']) for p in pans]


class BoundedCapture:
    MAX_BUDGET = 90

    def __init__(self, io, directory, cancel=None, budget=90):
        if not 1 <= budget <= self.MAX_BUDGET:
            raise ValueError('invalid capture budget')
        self.io = io
        self.root = Path(directory)
        self.cancel = cancel or threading.Event()
        self.budget = budget
        self.done = threading.Event()
        self.stop_error = None
        self.lock = threading.RLock()
        self.state = dict(id=identifier('scan'), status='prepared', frames=[],
                          full_panorama=False, candidate_detection=False,
                          image_time_basis='host_receive_time_not_exposure_time')

    def persist(self):
        with self.lock:
            temporary = self.root / 'record.tmp'
            temporary.write_text(json.dumps(self.state, ensure_ascii=False, indent=2), encoding='utf-8')
            os.replace(str(temporary), str(self.root / 'record.json'))

    def stop(self):
        try:
            self.io.stop()
        except Exception:
            with self.lock:
                self.stop_error = 'STOP_unconfirmed'
            raise RuntimeError('STOP unconfirmed')

    def check(self):
        if self.cancel.is_set():
            raise RuntimeError('scan_cancelled')
        if time.monotonic() > self.deadline:
            self.cancel.set()
            raise RuntimeError('scan_timed_out')
        if self.stop_error:
            raise RuntimeError(self.stop_error)

    def watchdog(self):
        if not self.done.wait(self.budget):
            self.cancel.set()
            try:
                self.stop()
            except Exception:
                pass

    def run(self):
        self.root.mkdir(parents=True, exist_ok=False)
        self.deadline = time.monotonic() + self.budget
        worker = threading.Thread(target=self.watchdog, name='bounded-scan-watchdog')
        worker.daemon = True
        worker.start()
        try:
            self.check()
            anchor = pose(self.io.position())
            points = commissioning_points(anchor)
            self.state.update(status='running', anchor=anchor, points=points, started_at=now())
            self.persist()
            for index, target in enumerate(points):
                self.check()
                self.io.move(target, self.cancel)
                self.check()
                before = pose(self.io.position())
                if any(abs(before[k]-target[k]) > tolerance for k,tolerance in (('pan',.3),('tilt',.3),('zoom',.05))):
                    raise RuntimeError('capture_position_not_reached')
                raw = self.io.snapshot()
                received = now()
                self.check()
                after = pose(self.io.position())
                if any(abs(before[k]-after[k]) > tolerance for k,tolerance in (('pan',.3),('tilt',.3),('zoom',.05))):
                    raise RuntimeError('camera_moved_during_capture')
                size, suffix = image_size(raw)
                if suffix != '.jpg' or not raw.endswith(b'\xff\xd9') or len(raw) > 12000000 or min(size)<1 or max(size)>24000:
                    raise ValueError('incomplete snapshot')
                name = 'view-%02d.jpg' % (index + 1)
                (self.root / name).write_bytes(raw)
                self.state['frames'].append(dict(path=name,title='扫描视图 %02d' % (index+1),
                    width=size[0],height=size[1],sha256=hashlib.sha256(raw).hexdigest(),
                    captured_at=received,pose=after,source_kind='bounded_scan_capture',
                    orientation='raw_camera',detections=[]))
                self.persist()
            self.check()
            self.io.move(anchor, self.cancel)
            self.check()
            restored = pose(self.io.position())
            if any(abs(restored[k]-anchor[k])>t for k,t in (('pan',.3),('tilt',.3),('zoom',.05))):
                raise RuntimeError('return_position_not_reached')
            self.state.update(status='completed', restored_pose=restored)
        except Exception as exc:
            self.state.update(status='stopped' if self.cancel.is_set() else 'failed', error=type(exc).__name__)
        finally:
            try:
                self.stop()
            except Exception:
                self.state.update(status='failed', error='STOP_unconfirmed')
            self.done.set()
            worker.join(timeout=4)
            if worker.is_alive() or self.stop_error:
                self.state.update(status='failed', error=self.stop_error or 'watchdog_still_active')
            self.state['finished_at'] = now()
            self.persist()
        if self.state['status'] == 'completed':
            manifest = dict(title='有限扫描 / ' + self.state['started_at'],
                            provenance='bounded_scan_capture', frames=self.state['frames'])
            (self.root / 'manifest.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2),encoding='utf-8')
        return self.state


class ISAPIScanIO:
    """Exact S6 profile, narrow absolute positions, unchanged tilt and zoom."""
    def __init__(self, camera):
        self.camera = camera
        self.command_lock = threading.RLock()
        self.stop_error = False
        self.anchor = None

    def preflight(self):
        import xml.etree.ElementTree as ET
        info = self.camera.xml('/ISAPI/System/deviceInfo')
        if 'DS-2DC2402IW-DE3' not in info.get('model',''):
            raise ValueError('camera model differs from the commissioned profile')
        root = ET.fromstring(self.camera.request('/ISAPI/PTZCtrl/channels/1/capabilities'))
        def ranges(name):
            parent = next(n for n in root.iter() if n.tag.split('}')[-1] == name)
            return [float(n.text) for n in parent.iter() if n.tag.split('}')[-1] in ('Min','Max')]
        if ranges('AbsolutePanTiltPositionSpace') + ranges('AbsoluteZoomPositionSpace') != [0,3500,0,900,10,20]:
            raise ValueError('camera range or units differ from the verified profile')
        self.anchor = self.position()
        return dict(anchor=self.anchor, points=commissioning_points(self.anchor),
                    motion_sent=False, full_panorama=False)

    def position(self):
        with self.command_lock:
            values = self.camera.status()
        return pose(dict(pan=float(values['azimuth'])/10.,tilt=float(values['elevation'])/10.,
                         zoom=float(values['absoluteZoom'])/10.))

    def stop(self):
        with self.command_lock:
            try:
                self.camera.stop()
                self.stop_error = False
            except Exception:
                self.stop_error = True
                raise

    def move(self, target, cancel):
        target = pose(target)
        if not self.anchor:
            raise ValueError('read-only preflight is required')
        if abs(target['pan']-self.anchor['pan']) > 5.01 or abs(target['tilt']-self.anchor['tilt']) > .001 or abs(target['zoom']-self.anchor['zoom']) > .001:
            raise ValueError('outside five-degree commissioning envelope')
        raw = {k:int(round(v*10)) for k,v in target.items()}
        body = ('<PTZData version="2.0" xmlns="http://www.hikvision.com/ver20/XMLSchema">'
                '<AbsoluteHigh><elevation>{tilt}</elevation><azimuth>{pan}</azimuth>'
                '<absoluteZoom>{zoom}</absoluteZoom></AbsoluteHigh></PTZData>').format(**raw).encode()
        try:
            with self.command_lock:
                if cancel.is_set() or self.stop_error:
                    raise RuntimeError('cancelled_or_stop_unconfirmed')
                self.camera.xml('/ISAPI/PTZCtrl/channels/1/absolute', body)
            deadline, stable = time.monotonic()+7, 0
            while time.monotonic()<deadline:
                if cancel.is_set():
                    raise RuntimeError('scan_cancelled')
                actual=self.position()
                reached=all(abs(actual[k]-target[k])<=t for k,t in (('pan',.3),('tilt',.3),('zoom',.05)))
                stable=stable+1 if reached else 0
                if stable>=3:
                    self.stop()
                    if cancel.wait(.7):
                        raise RuntimeError('scan_cancelled')
                    return
                cancel.wait(.15)
            raise RuntimeError('position_timeout')
        finally:
            self.stop()

    def snapshot(self):
        # Camera-generated fresh request after settling. Exposure timestamp is
        # not available from this endpoint and must not be invented.
        return self.camera.request('/ISAPI/Streaming/channels/101/picture')
