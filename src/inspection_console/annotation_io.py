"""Optional TX2 IO; shares motion interlocks, fresh capture and finite limits."""
import subprocess
import threading

from annotation_capture import validate_annotation
from first_capture_io import FirstCaptureIO
from ring_capture import close_pose
from scan_capture import ISAPIScanIO


class AnnotationIO(FirstCaptureIO):
    def __init__(self,camera,project_root,console_root,interlocks=None):
        FirstCaptureIO.__init__(self,camera,project_root,console_root,None,interlocks)

    def guard(self,cancel):
        if cancel.is_set() or self.hard_stop_failed:
            raise RuntimeError('cancelled_or_STOP_unconfirmed')
        state=self.state()
        if self.plan:validate_annotation(self.plan,state)
        if state['schedule']['enabled'] or any(j['status']=='running' for j in state['jobs']):
            raise RuntimeError('registry_became_active')
        if self.interlocks:self.interlocks()

    def preflight(self,plan):
        self.plan=validate_annotation(plan,self.state())
        self.guard(threading.Event())
        report=ISAPIScanIO.preflight(self)
        if not close_pose(report['anchor'],plan['anchor']):
            raise ValueError('camera_pose_changed_since_plan')
        self.source_path(plan['target']['references'][0])
        if subprocess.check_output(['python2','-c','import cv2;print(cv2.__version__)']).decode().strip()!='3.3.1':
            raise ValueError('review image worker runtime before using this profile')
        return report

    def bootstrap(self,reference,frame):
        if not close_pose(reference['source_pose'],frame['pose']):
            raise ValueError('annotation transfer requires the original source pose')
        return self.vision(dict(action='annotation_transfer',source=str(self.source_path(reference)),
            target=str(self.capture_root/frame['path']),bbox=reference['bbox']))

    def quality(self,frame):
        path=str(self.capture_root/frame['path'])
        return self.vision(dict(action='quality',source=path,target=path,bbox=frame['bbox']))

    def move(self,target,cancel):
        # Avoid another absolute command/focus cycle at a position already held.
        self.guard(cancel)
        if self.plan and close_pose(self.position(),target):
            self.stop()
            return
        return FirstCaptureIO.move(self,target,cancel)
