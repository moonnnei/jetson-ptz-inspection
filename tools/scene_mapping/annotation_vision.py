"""Python 2/3 geometry and image quality for annotation-seeded acquisition.

No old-image correlation threshold here. Registration transfers a source box;
it does not prove a meter category, a readable pointer, or physical visibility.
"""
from __future__ import division
import cv2
import numpy as np
from geometry import align, project


def transfer(source,target,box):
    registration=align(source,target)
    result=dict(status='uncertain',reason='source_scene_unregistered',registration=registration)
    if not registration['accepted']:return result
    x1,y1,x2,y2=box
    corners=project([[x1,y1],[x2,y1],[x2,y2],[x1,y2]],registration['matrix'])
    low,high=corners.min(axis=0),corners.max(axis=0)
    mapped=[float(low[0]),float(low[1]),float(high[0]),float(high[1])]
    h,w=target.shape[:2]
    ratio=(mapped[2]-mapped[0])*(mapped[3]-mapped[1])/((x2-x1)*(y2-y1))
    if not (0<=mapped[0]<mapped[2]<=w and 0<=mapped[1]<mapped[3]<=h and .5<=ratio<=2.):
        result['reason']='annotation_projection_outside_bounds'
        return result
    result.update(status='matched_reference',reason='annotation_transferred_to_new_image',
        bbox=mapped,basis='existing_annotation_plus_same_pose_scene_registration',
        category_verified_by_this_operation=False,old_template_appearance_required=False)
    return result


def quality(image,box):
    x1,y1,x2,y2=[int(round(v)) for v in box]
    if not 0<=x1<x2<=image.shape[1] or not 0<=y1<y2<=image.shape[0]:
        raise ValueError('quality ROI outside image')
    crop=image[y1:y2,x1:x2]
    gray=cv2.cvtColor(crop,cv2.COLOR_BGR2GRAY)
    return dict(sharpness=float(cv2.Laplacian(gray,cv2.CV_64F).var()),
        clipped_fraction=float(np.mean((gray<=3)|(gray>=252))),
        crop_size=[x2-x1,y2-y1],quality_threshold_validated=False)
