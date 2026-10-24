"""Local reference verification only after restoring a saved capture pose.

This does not discover meters, infer identity from pose alone, or move hardware.
The original scene-wide localizer remains available for first-capture teaching.
Compatible with Python 2.7 and OpenCV 3.3.1 on the legacy TX2 image worker.
"""
from __future__ import division
import math

import cv2
import numpy as np

from geometry import align, project
from relocalize import (checked_image, finite, locate, MIN_SCORE, MIN_INNER,
                        MIN_GAP, MAX_LOCAL_SHIFT_FRACTION)

STRATEGY = 'saved_pose_roi_v1'
POSE_TOLERANCE = dict(pan=.3, tilt=.3, zoom=.05)


def uncertain(reason, **details):
    result = dict(status='uncertain', reason=reason, bbox=None, quadrilateral=None,
                  strategy=STRATEGY, motion_authorized=False,
                  reading=None, reading_status='not_integrated')
    result.update(details)
    return result


def checked_pose(value):
    if not isinstance(value, dict) or set(value) != set(POSE_TOLERANCE):
        raise ValueError('saved and observed PTZ poses are required')
    for axis, maximum in (('pan', 350), ('tilt', 90), ('zoom', 2)):
        number = value[axis]
        minimum = 1 if axis == 'zoom' else 0
        if (type(number) not in (int, float) or not finite(number)
                or not minimum <= number <= maximum):
            raise ValueError('invalid fixed-revisit pose')
    return value


def competing_peak(source, target, box, matrix, found_box):
    """Check alternatives outside the tiny ORB ROI's same-peak neighborhood.

    Tiny windows can suppress every alternative and yield a meaningless peak
    gap. This larger window leaves room for a separate competing meter.
    Strong alternative full-face AND inner-face matches cause rejection.
    """
    x1, y1, x2, y2 = [int(round(v)) for v in box]
    w, h = x2-x1, y2-y1
    margin = max(w, h)
    left, top = max(0, x1-margin), max(0, y1-margin)
    right, bottom = min(source.shape[1], x2+margin), min(source.shape[0], y2+margin)
    if (right-left)*(bottom-top) > 6000000:
        return dict(accepted=False, reason='competition_region_exceeds_budget')
    translation = np.array([[1., 0, -left], [0, 1., -top], [0, 0, 1.]])
    warp = translation.dot(np.linalg.inv(matrix))
    size = (right-left, bottom-top)
    rectified = cv2.warpPerspective(target, warp, size)
    support = cv2.warpPerspective(np.ones(target.shape[:2], np.uint8), warp,
                                  size, flags=cv2.INTER_NEAREST)
    template = cv2.cvtColor(source[y1:y2, x1:x2], cv2.COLOR_BGR2GRAY)
    gray = cv2.cvtColor(rectified, cv2.COLOR_BGR2GRAY)
    scores = cv2.matchTemplate(gray, template, cv2.TM_CCOEFF_NORMED)
    if not np.isfinite(scores).all():
        return dict(accepted=False, reason='nonfinite_competition_correlation')
    q = project(found_box, np.linalg.inv(matrix))
    px, py = [int(round(v)) for v in q.min(axis=0)-[left, top]]
    if not (0 <= py < scores.shape[0] and 0 <= px < scores.shape[1]):
        return dict(accepted=False, reason='competition_projection_outside_search')
    chosen = float(scores[py, px])
    alternatives = scores.copy()
    alternatives[max(0, py-h//2):py+h//2+1,
                 max(0, px-w//2):px+w//2+1] = -2
    unused, second, unused, point = cv2.minMaxLoc(alternatives)
    sx, sy = point
    mx, my = max(1, w//5), max(1, h//5)
    other = gray[sy:sy+h, sx:sx+w]
    a, b = template[my:h-my, mx:w-mx], other[my:h-my, mx:w-mx]
    inner = -1.
    if min(float(a.std()), float(b.std())) >= 6:
        inner = float(cv2.matchTemplate(b, a, cv2.TM_CCOEFF_NORMED)[0, 0])
    alternative_support = float(support[sy:sy+h, sx:sx+w].mean())
    ambiguous = (second >= MIN_SCORE and inner >= MIN_INNER
                 and alternative_support >= .995 and chosen-second < MIN_GAP)
    return dict(accepted=not ambiguous,
                reason='competing_meter_appearance' if ambiguous else 'no_strong_competing_peak',
                search_roi=[left, top, right, bottom], chosen_score=chosen,
                alternative_score=float(second), alternative_inner_score=inner,
                alternative_support_fraction=alternative_support,
                compared_separate_location=bool(second > -2))


def locate_saved_pose(source, target, box, source_pose, target_pose):
    checked_image(source)
    checked_image(target)
    if source.shape != target.shape:
        raise ValueError('fixed-revisit image dimensions changed')
    reference, observed = checked_pose(source_pose), checked_pose(target_pose)
    delta = {axis: abs(reference[axis]-observed[axis]) for axis in POSE_TOLERANCE}
    if any(delta[axis] > POSE_TOLERANCE[axis]+1e-9 for axis in delta):
        return uncertain('saved_capture_pose_not_restored', pose_delta=delta)
    if (not isinstance(box, (list, tuple)) or len(box) != 4
            or any(type(v) not in (int, float) or not finite(v) for v in box)):
        raise ValueError('invalid reference bbox')
    x1, y1, x2, y2 = [int(round(v)) for v in box]
    height, width = source.shape[:2]
    if not (0 <= x1 < x2 <= width and 0 <= y1 < y2 <= height):
        raise ValueError('reference bbox outside source')
    w, h = x2-x1, y2-y1
    if min(w, h) < 16 or max(w, h) > 2048:
        return uncertain('reference_size_outside_localizer_budget', pose_delta=delta)
    margin = int(math.ceil(max(w, h)*MAX_LOCAL_SHIFT_FRACTION))
    left, top = max(0, x1-margin), max(0, y1-margin)
    right, bottom = min(width, x2+margin), min(height, y2+margin)
    a, b = source[top:bottom, left:right], target[top:bottom, left:right]
    local_box = [box[i]-[left, top, left, top][i] for i in range(4)]
    registration = align(a, b)
    result = locate(a, b, local_box, registration)
    result.update(strategy=STRATEGY, search_roi=[left, top, right, bottom], pose_delta=delta)
    if result['status'] != 'matched_reference':
        return result
    try:
        translate = np.array([[1., 0, left], [0, 1., top], [0, 0, 1.]])
        matrix = translate.dot(np.asarray(registration['matrix'])).dot(np.linalg.inv(translate))
        original = np.array([[x1,y1], [x2,y1], [x2,y2], [x1,y2]], np.float64)
        prediction = project(original, matrix)
        displacement = np.max(np.abs(prediction-original)/[w, h])
        area = float(cv2.contourArea(prediction.astype(np.float32), oriented=True))/(w*h)
        result.update(projected_corner_shift_fraction=float(displacement),
                      projected_meter_area_ratio=area)
        if not .8 <= area <= 1.25 or displacement > MAX_LOCAL_SHIFT_FRACTION:
            result.update(status='uncertain', reason='saved_pose_local_geometry_rejected',
                          bbox=None, quadrilateral=None)
            return result
        full_quad = np.asarray(result['quadrilateral'])+[left, top]
        competition = competing_peak(source, target, box, matrix, full_quad)
        result['competition'] = competition
        if not competition['accepted']:
            result.update(status='uncertain', reason=competition['reason'],
                          bbox=None, quadrilateral=None)
            return result
        low, high = full_quad.min(axis=0), full_quad.max(axis=0)
        result.update(bbox=[float(low[0]),float(low[1]),float(high[0]),float(high[1])],
                      quadrilateral=full_quad.tolist(), reason='saved_pose_roi_and_appearance_match')
        return result
    except (ValueError, np.linalg.LinAlgError, cv2.error):
        result.update(status='uncertain', reason='invalid_fixed_revisit_projection',
                      bbox=None, quadrilateral=None)
        return result
