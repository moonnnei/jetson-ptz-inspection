"""First teaching uses cross-view matching; saved-pose rechecks use the ROI matcher."""
from teaching_io import TeachingIO


class FirstCaptureIO(TeachingIO):
    def __init__(self, camera, project_root, console_root, references, interlocks=None):
        TeachingIO.__init__(self, camera, project_root, console_root, references)
        self.interlocks = interlocks

    def guard(self, cancel):
        TeachingIO.guard(self, cancel)
        if self.interlocks:
            self.interlocks()

    def locate(self, reference, frame):
        if (not frame.get('purpose', '').startswith('independent_revisit_')
                and frame.get('purpose') != 'adaptive_zoom_recovery'):
            return TeachingIO.locate(self, reference, frame)
        if (reference.get('orientation') != 'rotate180_from_raw'
                or frame.get('orientation') != reference['orientation']):
            raise ValueError('independent-revisit image orientations differ')
        return self.vision(dict(action='locate_saved_pose', source=str(self.source_path(reference)),
                                target=str(self.capture_root/frame['path']), bbox=reference['bbox'],
                                source_pose=reference['source_pose'], target_pose=frame['pose']))
