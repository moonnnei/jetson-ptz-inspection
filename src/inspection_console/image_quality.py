"""Basic image validity checks, separate from identity and numeric readability."""
import math


def assess(reference,current,box,width,height,center_error):
    for q in (reference,current):
        if any(type(q.get(k)) not in (int,float) or not math.isfinite(q[k])
               for k in ('sharpness','clipped_fraction')):
            raise ValueError('non-finite image quality metrics')
        if q['sharpness']<0 or not 0<=q['clipped_fraction']<=1:
            raise ValueError('invalid image quality metrics')
    x,y,r,b=box
    reasons=[];warnings=[]
    if min(r-x,b-y)<64:reasons.append('target_too_small')
    if not (.005*width<=x<r<=(1-.005)*width and .005*height<=y<b<=(1-.005)*height):
        reasons.append('target_near_image_boundary')
    if current['clipped_fraction']>.35:reasons.append('exposure_clipping')
    ratio=current['sharpness']/max(1.,reference['sharpness'])
    if reference['sharpness']<1:reasons.append('reference_has_insufficient_detail')
    elif ratio<.35:reasons.append('blur_worse_than_reference')
    if abs(center_error[0])>.035 or abs(center_error[1])>.04:warnings.append('reachable_off_center')
    return dict(accepted=not reasons,reasons=reasons,warnings=warnings,method='relative_picture_checks_v1',
        sharpness_ratio=ratio,reference_metrics=reference,current_metrics=current,
        thresholds=dict(minimum_side_pixels=64,maximum_clipped_fraction=.35,
                        minimum_relative_sharpness=.35,frame_margin=.005),
        thresholds_scope='engineering_heuristics_not_general_readability_acceptance',
        occlusion_status='not_assessed',numeric_readability='not_assessed',super_resolution=False)
