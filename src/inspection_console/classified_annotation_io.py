"""Fresh semantic rechecks for automatic proposals; human boxes retain provenance."""
from annotation_io import AnnotationIO


class ClassifiedAnnotationIO(AnnotationIO):
    def __init__(self,camera,project_root,console_root,detector=None,interlocks=None):
        AnnotationIO.__init__(self,camera,project_root,console_root,interlocks)
        self.detector=detector

    def automatic(self):
        return self.plan['target']['references'][0]['selection']=='automatic_unverified_proposal'

    def category(self,frame,box):
        if self.detector is None:raise ValueError('automatic proposals require the meter detector')
        path=self.capture_root/frame['path']
        output=self.capture_root/(frame['path'].replace('.jpg','-detector-roi.jpg'))
        crop=self.vision(dict(action='crop_region',source=str(path),target=str(path),bbox=box,output=str(output)))
        return self.detector.infer(output,crop['crop'],box)

    def bootstrap(self,reference,frame):
        match=AnnotationIO.bootstrap(self,reference,frame)
        if not self.automatic():
            if match['status']=='matched_reference' or self.detector is None:return match
            checked=self.category(frame,reference['bbox'])
            if checked['accepted']:
                return dict(status='matched_reference',reason='fresh_meter_near_human_annotation',
                    bbox=checked['bbox'],basis='fixed_pose_human_box_plus_fresh_NCNN_meter',
                    category_verified=True,category_recheck=checked)
            return match
        # A failed old-scene registration does not block a NEW image or a
        # bounded semantic check at the original posed annotation.
        box=match.get('bbox') or reference['bbox']
        classified=self.category(frame,box)
        if not classified['accepted']:
            return dict(status='uncertain',reason='automatic_category_recheck_rejected',
                        category_recheck=classified)
        return dict(status='matched_reference',reason='fresh_meter_near_posed_proposal',
            bbox=classified['bbox'],basis='posed_proposal_plus_fresh_NCNN_meter',
            category_verified=True,category_recheck=classified)

    def locate(self,reference,frame):
        match=AnnotationIO.locate(self,reference,frame)
        if (not self.automatic() or match['status']!='matched_reference' or
                frame['purpose'] not in ('annotation_seed_second_fresh_check','independent_revisit_1','independent_revisit_2')):
            return match
        classified=self.category(frame,match['bbox'])
        match['category_verified']=classified['accepted']
        match['category_recheck']=classified
        if not classified['accepted']:
            match.update(status='uncertain',reason='automatic_category_recheck_rejected')
        return match
