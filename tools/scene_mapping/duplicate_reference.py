"""Cross-zoom duplicate check for independently commissioned photographs only.

The ordinary scene localizer keeps its original bounds. This path additionally
requires nearby saved poses, optical-scale consistency, scene inliers and the
same full-face/inner-face appearance checks. It grants no motion authority.
"""
from __future__ import division
from geometry import align
from fixed_revisit import checked_pose
from relocalize import checked_image,locate


def locate_commissioned(source,target,box,source_pose,target_pose):
    checked_image(source);checked_image(target)
    a,b=checked_pose(source_pose),checked_pose(target_pose)
    result=dict(status='uncertain',reason='commissioned_poses_too_far',bbox=None,
                motion_authorized=False,reading=None,strategy='commissioned_cross_zoom_v1')
    if (source.shape!=target.shape or abs((a['pan']-b['pan']+180)%360-180)>5
            or abs(a['tilt']-b['tilt'])>5):return result
    registration=align(source,target,area_limits=(.2,5.))
    expected=(b['zoom']/a['zoom'])**2
    result.update(expected_area_ratio=expected,registration={k:v for k,v in registration.items() if k!='matrix'})
    if not registration['accepted']:
        result['reason']='cross_zoom_scene_registration_rejected';return result
    ratio=registration['area_ratio']/expected
    if not .7<=ratio<=1.3:
        result.update(reason='optical_scale_inconsistent',relative_scale_ratio=ratio);return result
    found=locate(source,target,box,registration)
    found.update(strategy='commissioned_cross_zoom_v1',expected_area_ratio=expected,relative_scale_ratio=ratio)
    return found
