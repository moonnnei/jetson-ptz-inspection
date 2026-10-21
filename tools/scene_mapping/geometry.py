"""Optional PC-side panorama preview with explicit source-image transforms.

This is image registration, never a PTZ calibration or a coverage certificate.
Detection coordinates must remain in each full-resolution source image.
"""
import cv2
import numpy as np


def project(points, matrix):
    points = np.asarray(points, dtype=np.float64)
    h = np.asarray(matrix, dtype=np.float64)
    if h.shape != (3, 3) or not np.isfinite(h).all():
        raise ValueError('invalid homography')
    homogeneous = np.column_stack((points, np.ones(len(points)))).dot(h.T)
    divisor = homogeneous[:, 2]
    if (not np.isfinite(homogeneous).all() or np.min(np.abs(divisor)) < 1e-8
            or np.min(divisor) * np.max(divisor) <= 0):
        raise ValueError('projection crosses infinity')
    return homogeneous[:, :2] / divisor[:, None]


def corners(image):
    height, width = image.shape[:2]
    return np.array([[0, 0], [width, 0], [width, height], [0, height]], np.float64)


def prepare(image):
    height, width = image.shape[:2]
    scale = min(1., 1200. / max(width, height))
    work = cv2.resize(image, (int(round(width * scale)), int(round(height * scale))))
    gray = cv2.cvtColor(work, cv2.COLOR_BGR2GRAY)
    # ORB is available in both OpenCV 3 and 4. Cross-check descriptor matching
    # and geometric validation are still necessary for repeated meter patterns.
    detector = cv2.ORB_create(nfeatures=4500, fastThreshold=12)
    keys, descriptors = detector.detectAndCompute(gray, None)
    return keys, descriptors, np.diag([work.shape[1]/width, work.shape[0]/height, 1.]), work.shape[:2]


def align(source, target, source_features=None, target_features=None):
    sk, sd, ss, sw = source_features or prepare(source)
    tk, td, ts, tw = target_features or prepare(target)
    result = dict(accepted=False, matches=0, inliers=0)
    if sd is None or td is None or min(len(sk), len(tk)) < 20:
        result['reason'] = 'too_few_features'
        return result
    matcher = cv2.BFMatcher(cv2.NORM_HAMMING)
    forward = matcher.knnMatch(sd, td, k=2)
    reverse = matcher.knnMatch(td, sd, k=2)
    back = {m.queryIdx:m.trainIdx for pair in reverse if len(pair)==2
            for m,n in [pair] if m.distance < .75*n.distance}
    matches = [m for pair in forward if len(pair)==2 for m,n in [pair]
               if m.distance < .75*n.distance and back.get(m.trainIdx)==m.queryIdx]
    result['matches'] = len(matches)
    if len(matches) < 20:
        result['reason'] = 'too_few_mutual_matches'
        return result
    a = np.float32([sk[m.queryIdx].pt for m in matches])
    b = np.float32([tk[m.trainIdx].pt for m in matches])
    cv2.setRNGSeed(11)
    h, mask = cv2.findHomography(a, b, cv2.RANSAC, 3.)
    if h is None or mask is None:
        result['reason'] = 'homography_failed'
        return result
    inliers = mask.ravel().astype(bool)
    count = int(inliers.sum())
    result.update(inliers=count, inlier_ratio=float(count/len(matches)))
    if count < 20 or count / len(matches) < .45:
        result['reason'] = 'inliers_insufficient'
        return result
    try:
        residual = np.linalg.norm(project(a[inliers], h)-b[inliers], axis=1)
        median = float(np.median(residual))
        cover_a = float(cv2.contourArea(cv2.convexHull(a[inliers]))) / (sw[0]*sw[1])
        cover_b = float(cv2.contourArea(cv2.convexHull(b[inliers]))) / (tw[0]*tw[1])
        full = np.linalg.inv(ts).dot(h).dot(ss)
        full /= full[2, 2]
        quad = project(corners(source), full).astype(np.float32)
        source_area = source.shape[0]*source.shape[1]
        area = abs(float(cv2.contourArea(quad)))
        overlap, unused = cv2.intersectConvexConvex(quad, corners(target).astype(np.float32))
        result.update(median_error_work_pixels=median, source_feature_coverage=cover_a,
                      target_feature_coverage=cover_b, area_ratio=area/source_area,
                      overlap_fraction=float(overlap/max(1, area)))
        if (median > 2.5 or min(cover_a, cover_b) < .025
                or not cv2.isContourConvex(quad) or not .4 <= area/source_area <= 2.5
                or overlap/max(1, area) < .1 or np.max(np.abs(quad)) > 100000):
            result['reason'] = 'geometry_quality_rejected'
            return result
        result.update(accepted=True, matrix=full.tolist(), reason='accepted_preview_only')
        return result
    except (ValueError, np.linalg.LinAlgError, cv2.error):
        result['reason'] = 'invalid_projection'
        return result


def build_overview(images, max_side=4096, max_pixels=12000000):
    if not 1 <= len(images) <= 24:
        raise ValueError('preview supports 1-24 source views')
    if max_side < 128 or max_side > 8192 or not 16384 <= max_pixels <= 24000000:
        raise ValueError('invalid preview memory budget')
    for image in images:
        if (not isinstance(image, np.ndarray) or image.dtype != np.uint8 or image.ndim != 3
                or image.shape[2] != 3 or min(image.shape[:2]) < 32 or max(image.shape[:2]) > 8192):
            raise ValueError('expected bounded uint8 BGR source views')
    features = [prepare(image) for image in images]
    transforms = {0:np.eye(3)}
    edges = []
    # Try nearby previously registered views; disconnected views remain explicit.
    # One global planar preview is deliberately limited to a small scan sector.
    for index in range(1, len(images)):
        choices = []
        for reference in sorted(transforms, reverse=True)[:3]:
            result = align(images[index], images[reference], features[index], features[reference])
            result.update(source=index, reference=reference)
            edges.append(result)
            if result['accepted']:
                choices.append(result)
        if choices:
            best = max(choices, key=lambda r:r['inliers'])
            transforms[index] = transforms[best['reference']].dot(np.asarray(best['matrix']))
    points = np.concatenate([project(corners(images[i]), h) for i,h in transforms.items()])
    lower, upper = np.floor(points.min(axis=0)), np.ceil(points.max(axis=0))
    width, height = upper-lower
    if not np.isfinite([width,height]).all() or min(width,height) <= 0 or max(width,height) > 100000:
        raise ValueError('unbounded panorama projection')
    scale = min(1., max_side/max(width,height), np.sqrt(max_pixels/(width*height)))
    translation = np.array([[scale,0,-lower[0]*scale],[0,scale,-lower[1]*scale],[0,0,1.]])
    size = (int(np.ceil(width*scale)), int(np.ceil(height*scale)))
    canvas = np.zeros((size[1],size[0],3), np.uint8)
    priority = np.zeros((size[1],size[0]), np.float32)
    entries = []
    for index,image in enumerate(images):
        if index not in transforms:
            entries.append(dict(index=index, registered=False, to_overview=None))
            continue
        h = translation.dot(transforms[index])
        ih, iw = image.shape[:2]
        yy,xx = np.ogrid[:ih,:iw]
        weight = np.minimum(np.minimum(xx+1,iw-xx),np.minimum(yy+1,ih-yy)).astype(np.float32)
        weight /= max(1.,float(weight.max()))
        warped = cv2.warpPerspective(image,h,size)
        weights = cv2.warpPerspective(weight,h,size)
        use = weights > priority
        canvas[use] = warped[use]
        priority[use] = weights[use]
        entries.append(dict(index=index,registered=True,to_overview=h.tolist(),
                            footprint=project(corners(image),h).tolist()))
    count = len(transforms)
    return canvas, dict(schema_version=1, kind='registered_sector_preview' if count>1 else 'view_index',
        registered_views=count,total_views=len(images),frames=entries,edges=edges,
        full_panorama=False,ptz_calibration=False,projection='local_planar_homography',
        disconnected=[i for i in range(len(images)) if i not in transforms],
        note='Preview only: parallax, moving objects and stitching seams may remain. Original views are authoritative.')
