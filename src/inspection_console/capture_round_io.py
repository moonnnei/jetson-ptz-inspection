"""Exact S6 IO for a finite saved-photo revisit trial; no periodic adapter."""
import subprocess
import sqlite3
import threading

from capture_round import validate_round
from references import read_reference_image
from ring_capture import close_pose
from scan_capture import ISAPIScanIO
from teaching_io import TeachingIO


class CaptureRoundIO(TeachingIO):
    def __init__(self,camera,project_root,console_root,plan,interlocks=None):
        TeachingIO.__init__(self,camera,project_root,console_root,plan['confirmed_references'])
        self.round_plan=plan
        self.interlocks=interlocks

    def guard(self,cancel):
        if cancel.is_set() or self.hard_stop_failed:
            raise RuntimeError('cancelled_or_STOP_unconfirmed')
        state=self.state();validate_round(self.round_plan,state)
        if state['schedule']['enabled'] or any(j['status']=='running' for j in state['jobs']):
            raise RuntimeError('registry_became_active')
        if self.interlocks:self.interlocks()

    def review_changed(self):
        # This monitor also notices a web pause while an image worker runs.
        connection=sqlite3.connect(str(self.store.path),timeout=.3)
        try:return connection.execute('SELECT revision FROM registry WHERE id=1').fetchone()[0]!=self.round_plan['revision']
        finally:connection.close()

    def locate(self, reference, frame):
        if self.round_plan.get('appearance_strategy') != 'saved_pose_roi_v1':
            raise ValueError('new saved-pose matching plan is required')
        if (reference.get('orientation') != 'rotate180_from_raw'
                or frame.get('orientation') != reference['orientation']):
            raise ValueError('reference and capture orientations must match')
        return self.vision(dict(action='locate_saved_pose',
            source=str(self.source_path(reference)), target=str(self.capture_root/frame['path']),
            bbox=reference['bbox'], source_pose=reference['source_pose'], target_pose=frame['pose']))

    def preflight(self,plan):
        validate_round(plan,self.state())
        if not plan['ready_for_trial']:raise ValueError('round reference preparation is incomplete')
        self.plan=plan;self.guard(threading.Event())
        report=ISAPIScanIO.preflight(self)
        if not close_pose(report['anchor'],plan['anchor']):
            raise ValueError('camera_pose_changed_since_plan')
        for target in plan['stops']:
            read_reference_image(target['reference'],self.console/'var/inspection-console/images')
        if subprocess.check_output(['python2','-c','import cv2;print(cv2.__version__)']).decode().strip()!='3.3.1':
            raise ValueError('image-worker runtime needs review')
        return report
