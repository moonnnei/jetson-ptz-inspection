"""Optional PC visual relocalization of already confirmed, stationary meters.

Global ORB/RANSAC predicts an ROI, then bounded template and inner-face checks
verify local appearance. Scores are correlations, not confidence probabilities.
No pose is inferred, no camera is controlled and no instrument is commissioned.
"""
import math
import cv2
import numpy as np

from geometry import align, project

MIN_SCORE = .72
MIN_INNER = .60
MIN_GAP = .08
MAX_LOCAL_SHIFT_FRACTION = .25


def checked_image(image):
    if (not isinstance(image, np.ndarray) or image.dtype != np.uint8
            or image.ndim != 3 or image.shape[2] != 3
            or min(image.shape[:2]) < 32 or max(image.shape[:2]) > 8192
            or image.shape[0] * image.shape[1] > 24000000):
        raise ValueError('expected a bounded uint8 BGR image')


def locate(source, target, box, registration=None):
    checked_image(source)
    checked_image(target)
    if (not isinstance(box, (list, tuple)) or len(box) != 4
            or any(type(x) not in (int, float) or not math.isfinite(x) for x in box)):
        raise ValueError('invalid reference bbox')
    x1, y1, x2, y2 = [int(round(x)) for x in box]
    height, width = source.shape[:2]
    if not (0 <= x1 < x2 <= width and 0 <= y1 < y2 <= height):
        raise ValueError('reference bbox outside source')
    w, h = x2 - x1, y2 - y1
    result = dict(status='uncertain', reason=None, bbox=None, quadrilateral=None,
                  motion_authorized=False, reading=None, reading_status='not_integrated')
    if min(w, h) < 16 or max(w, h) > 2048:
        result['reason'] = 'reference_size_outside_localizer_budget'
        return result
    template = cv2.cvtColor(source[y1:y2, x1:x2], cv2.COLOR_BGR2GRAY)
    if float(template.std()) < 6:
        result['reason'] = 'reference_has_insufficient_texture'
        return result
    registration = registration if registration is not None else align(source, target)
    result['registration'] = {k: v for k, v in registration.items() if k != 'matrix'}
    if not registration.get('accepted'):
        result['reason'] = 'scene_registration_rejected'
        return result
    try:
        matrix = np.asarray(registration['matrix'], np.float64)
        if (matrix.shape != (3, 3) or not np.isfinite(matrix).all()
                or abs(float(np.linalg.det(matrix))) < 1e-12):
            raise ValueError('invalid registration matrix')
        # Only rectify a small ROI, not another full-size image for every meter.
        margin = int(math.ceil(max(w, h) * .5))
        left, top = max(0, x1 - margin), max(0, y1 - margin)
        right, bottom = min(width, x2 + margin), min(height, y2 + margin)
        translation = np.array([[1, 0, -left], [0, 1, -top], [0, 0, 1.]])
        warp = translation.dot(np.linalg.inv(matrix))
        size = (right - left, bottom - top)
        patch = cv2.warpPerspective(target, warp, size)
        support = cv2.warpPerspective(np.ones(target.shape[:2], np.uint8), warp,
                                      size, flags=cv2.INTER_NEAREST)
        gray = cv2.cvtColor(patch, cv2.COLOR_BGR2GRAY)
        scores = cv2.matchTemplate(gray, template, cv2.TM_CCOEFF_NORMED)
        if not np.isfinite(scores).all():
            result['reason'] = 'nonfinite_correlation'
            return result
        unused, peak, unused, point = cv2.minMaxLoc(scores)
        px, py = point
        separated = scores.copy()
        # Suppress the same peak's neighborhood; a separate competing match
        # may be a repeated dial and must not silently acquire the same ID.
        separated[max(0, py-h//2):py+h//2+1, max(0, px-w//2):px+w//2+1] = -1
        second = float(separated.max())
        found = gray[py:py+h, px:px+w]
        mx, my = max(1, w//5), max(1, h//5)
        inner_template = template[my:h-my, mx:w-mx]
        inner_found = found[my:h-my, mx:w-mx]
        if min(float(inner_template.std()), float(inner_found.std())) < 6:
            inner = -1.
        else:
            inner = float(cv2.matchTemplate(inner_found, inner_template,
                                           cv2.TM_CCOEFF_NORMED)[0, 0])
        dx, dy = left + px - x1, top + py - y1
        support_fraction = float(support[py:py+h, px:px+w].mean())
        result.update(score=float(peak), inner_score=inner, peak_gap=float(peak-second),
                      local_offset_pixels=[dx, dy], support_fraction=support_fraction)
        if support_fraction < .995:
            result['reason'] = 'target_partly_outside_frame'
        elif peak < MIN_SCORE or inner < MIN_INNER:
            result['reason'] = 'local_appearance_rejected'
        elif peak - second < MIN_GAP:
            result['reason'] = 'repeated_appearance_ambiguous'
        elif max(abs(dx)/w, abs(dy)/h) > MAX_LOCAL_SHIFT_FRACTION:
            result['reason'] = 'target_displaced_from_scene_reference'
        else:
            quad = project([[left+px, top+py], [left+px+w, top+py],
                            [left+px+w, top+py+h], [left+px, top+py+h]], matrix)
            low, high = quad.min(axis=0), quad.max(axis=0)
            if (low < 2).any() or high[0] > target.shape[1]-2 or high[1] > target.shape[0]-2:
                result['reason'] = 'target_near_frame_boundary'
            else:
                result.update(status='matched_reference', reason='scene_and_local_appearance_match',
                              bbox=[float(low[0]), float(low[1]), float(high[0]), float(high[1])],
                              quadrilateral=quad.tolist())
        return result
    except (ValueError, np.linalg.LinAlgError, cv2.error):
        result['reason'] = 'invalid_local_projection'
        return result


def reject_overlaps(results, threshold=.35):
    """One detection cannot silently satisfy two different instrument IDs."""
    matched = [(i, r) for i, r in enumerate(results) if r['status'] == 'matched_reference']
    conflicts = set()
    for n, (i, a) in enumerate(matched):
        for j, b in matched[n+1:]:
            ax1, ay1, ax2, ay2 = a['bbox']
            bx1, by1, bx2, by2 = b['bbox']
            intersection = max(0, min(ax2, bx2)-max(ax1, bx1)) * max(0, min(ay2, by2)-max(ay1, by1))
            small = min((ax2-ax1)*(ay2-ay1), (bx2-bx1)*(by2-by1))
            if intersection / small > threshold:
                conflicts.update((i, j))
    for index in conflicts:
        results[index].update(status='uncertain', reason='multiple_ids_share_local_region',
                              bbox=None, quadrilateral=None)
    return results
