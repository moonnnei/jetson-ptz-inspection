"""Bounded scan and visit planning. Angles are degrees, zoom is optical ratio.

Planning never connects to hardware. Coverage is a design estimate until the
camera field of view and overlapping views have been measured in the scene.
"""
import math


def number(value, lower, upper, name):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(name + ' must be a number')
    if not math.isfinite(value) or not lower <= value <= upper:
        raise ValueError(name + ' outside allowed range')
    return float(value)


def pose(value):
    if not isinstance(value, dict) or set(value) != {'pan', 'tilt', 'zoom'}:
        raise ValueError('pose requires pan, tilt and zoom')
    return dict(pan=number(value['pan'], 0, 350, 'pan'),
                tilt=number(value['tilt'], 0, 90, 'tilt'),
                zoom=number(value['zoom'], 1, 2, 'zoom'))


def _axis(start, end, step):
    count = max(1, int(math.ceil((end - start) / step)))
    return [start] if start == end else [start + (end - start) * i / count
                                       for i in range(count + 1)]


def scan_grid(config):
    required = {'pan_min', 'pan_max', 'tilt_min', 'tilt_max', 'hfov', 'vfov',
                'overlap', 'zoom'}
    if not isinstance(config, dict) or set(config) != required:
        raise ValueError('scan configuration fields do not match')
    p0 = number(config['pan_min'], 0, 350, 'pan_min')
    p1 = number(config['pan_max'], p0, 350, 'pan_max')
    t0 = number(config['tilt_min'], 0, 90, 'tilt_min')
    t1 = number(config['tilt_max'], t0, 90, 'tilt_max')
    h = number(config['hfov'], 1, 120, 'hfov')
    v = number(config['vfov'], 1, 100, 'vfov')
    overlap = number(config['overlap'], .2, .7, 'overlap')
    zoom = number(config['zoom'], 1, 2, 'zoom')
    pans, tilts = _axis(p0, p1, h * (1 - overlap)), _axis(t0, t1, v * (1 - overlap))
    if len(pans) * len(tilts) > 240:
        raise ValueError('scan exceeds the 240-view budget')
    points = []
    for row, tilt in enumerate(tilts):
        for pan in (pans if row % 2 == 0 else list(reversed(pans))):
            points.append(dict(index=len(points) + 1, row=row,
                               pan=round(pan, 4), tilt=round(tilt, 4), zoom=zoom))
    return dict(points=points, count=len(points), rows=len(tilts),
                columns=len(pans), execution_enabled=False,
                coverage_status='requires_measured_fov_and_scene_validation',
                pan_wrap=False)


def route(instruments, start):
    """Nearest-neighbor route over validated poses; never use circular wrapping."""
    current = pose(start)
    pending = []
    blocked = []
    for item in instruments:
        if not item.get('enabled'):
            continue
        if item.get('pose_status') != 'verified' or not item.get('visit_pose'):
            blocked.append(dict(id=item['id'], reason='visit_pose_unverified'))
        else:
            pending.append(dict(id=item['id'], name=item['name'],
                                pose=pose(item['visit_pose'])))
    result = []
    total = 0.0
    while pending:
        # This is an angle-space surrogate, not measured motor travel time.
        def cost(item):
            p = item['pose']
            return math.hypot(p['pan'] - current['pan'], p['tilt'] - current['tilt'])
        item = min(pending, key=lambda x: (cost(x), x['id']))
        total += cost(item)
        pending.remove(item)
        result.append(item)
        current = item['pose']
    return dict(stops=result, blocked=blocked, angular_cost=round(total, 4),
                algorithm='nearest_neighbor_bounded_axes', optimality_claim=False)
