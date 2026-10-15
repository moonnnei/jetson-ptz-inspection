"""Bounded, stationary-person inspection demo; Python 3.5 standard library only.

People stand in for instruments. Locations are associated geometrically, never by
identity, and no instrument reading is manufactured. All hardware access belongs
to the injected IO adapter. ``after`` and ``received_at`` use time.monotonic().
"""
from __future__ import division

import copy
import datetime
import json
import math
import os
import threading
import time
import uuid


class InspectionError(Exception):
    pass


class Cancelled(InspectionError):
    pass


class StopFailure(InspectionError):
    pass


def inverse_2x2(matrix):
    """Validate a local image-displacement-per-degree Jacobian and invert it."""
    try:
        if len(matrix) != 2 or any(len(row) != 2 for row in matrix):
            raise ValueError()
        a, b = float(matrix[0][0]), float(matrix[0][1])
        c, d = float(matrix[1][0]), float(matrix[1][1])
    except (TypeError, ValueError, IndexError):
        raise InspectionError('invalid calibration matrix')
    if not all(math.isfinite(v) for v in (a, b, c, d)):
        raise InspectionError('non-finite calibration matrix')
    determinant = a * d - b * c
    if abs(determinant) < 1e-8:
        raise InspectionError('singular calibration matrix')
    return [[d / determinant, -b / determinant],
            [-c / determinant, a / determinant]]


def correction_degrees(matrix, error, gain=1.0, limit=None):
    inv = inverse_2x2(matrix)
    result = [-gain * (row[0] * error[0] + row[1] * error[1])
              for row in inv]
    if limit is not None:
        # A common scale preserves direction when one axis reaches its limit.
        factor = min(1.0, float(limit) / max(max(abs(v) for v in result), 1e-12))
        result = [v * factor for v in result]
    return result


def angle_distance(first, second):
    # Deliberately no wrap: the adapter restricts this demo to a local sector.
    return math.hypot(first['pan'] - second['pan'], first['tilt'] - second['tilt'])


def nearest_route(targets, position):
    """Greedy angular-distance route, not a globally optimal time estimate."""
    pending = list(targets)
    result = []
    current = position
    while pending:
        selected = min(pending, key=lambda item: (angle_distance(item, current),
                                                 item['target_id']))
        pending.remove(selected)
        result.append(selected['target_id'])
        current = selected
    return result


def valid_person(person, threshold=0.65):
    try:
        box = [float(v) for v in person['bbox']]
        confidence = float(person['confidence'])
    except (KeyError, TypeError, ValueError):
        return None
    if len(box) != 4 or not all(math.isfinite(v) for v in box + [confidence]):
        return None
    x1, y1, x2, y2 = box
    if confidence < threshold or confidence > 1.0:
        return None
    # A cut-off or very small person is unsuitable as a complete instrument proxy.
    if min(x1, y1) < 0.015 or max(x2, y2) > 0.985:
        return None
    if x2 - x1 < 0.025 or y2 - y1 < 0.08 or (x2 - x1) * (y2 - y1) < 0.005:
        return None
    return {'bbox': box, 'confidence': confidence}


def project_person(person, pose, matrix):
    box = person['bbox']
    center = [(box[0] + box[2]) / 2.0, (box[1] + box[3]) / 2.0]
    delta = correction_degrees(matrix, [center[0] - 0.5, center[1] - 0.5])
    return {'pan': pose['pan'] + delta[0], 'tilt': pose['tilt'] + delta[1],
            'bbox': list(box), 'confidence': person['confidence']}


def merge_sightings(targets, candidates, pose, sequence, distance=3.0):
    """One-to-one matching, preserving separate people seen in the same frame.

    Near-equal competing matches are skipped rather than silently changing the
    chosen person. Spatial deduplication cannot identify a moving person.
    Returns the number of ambiguous candidates rejected.
    """
    original = list(targets)
    proposals = []
    new_candidates = []
    ambiguous = 0
    for index, candidate in enumerate(candidates):
        matches = sorted((angle_distance(candidate, target), ti)
                         for ti, target in enumerate(original)
                         if angle_distance(candidate, target) <= distance)
        if not matches:
            new_candidates.append(candidate)
        elif len(matches) > 1 and matches[1][0] - matches[0][0] < 0.75:
            ambiguous += 1
        else:
            proposals.append((matches[0][0], index, matches[0][1]))
    claimed = set()
    for unused_distance, index, ti in sorted(proposals):
        if ti in claimed:
            ambiguous += 1
            continue
        claimed.add(ti)
        target = original[ti]
        candidate = candidates[index]
        if target.get('last_sequence') == sequence:
            continue
        count = target['hits']
        for axis in ('pan', 'tilt'):
            target[axis] = (target[axis] * count + candidate[axis]) / (count + 1)
        target['hits'] = count + 1
        target['confidence'] = max(target['confidence'], candidate['confidence'])
        target['bbox'] = list(candidate['bbox'])
        target['last_pose'] = dict(pose)
        target['last_sequence'] = sequence
    for candidate in new_candidates:
        item = copy.deepcopy(candidate)
        item.update({'target_id': 'person-%03d' % (len(targets) + 1),
                     'hits': 1, 'discovery_pose': dict(pose),
                     'last_pose': dict(pose), 'last_sequence': sequence,
                     'status': 'candidate'})
        targets.append(item)
    return ambiguous


class Controller(object):
    HEARTBEAT_TIMEOUT = 3.0
    TOTAL_TIMEOUT = 180.0
    CENTER_TIMEOUT = 20.0
    MAX_FRAME_AGE = 0.8
    ASSOCIATION_GATE = 3.0
    MAX_TARGETS = 20

    def __init__(self, io, runtime_dir):
        self.io = io
        self.runtime_dir = os.path.abspath(str(runtime_dir))
        self._lock = threading.RLock()
        self._lifecycle_lock = threading.RLock()
        self._action_lock = threading.RLock()
        self._stop_lock = threading.Lock()
        self._persist_lock = threading.Lock()
        self._cancel = threading.Event()
        self._thread = None
        self._watchdog = None
        self._last_heartbeat = 0.0
        self._deadline = 0.0
        self._cancel_reason = None
        self._stop_error = None
        self._run_stop_failed = False
        self._run_dir = None
        self._matrices = {}
        self._state = {'run_id': None, 'active': False, 'phase': 'idle',
                       'message': '已就绪；请让作为仪表替身的人保持站定。',
                       'targets': [], 'route': [], 'active_target': None,
                       'results': [], 'events': [], 'error': None,
                       'stop_error': None}

    def snapshot(self):
        with self._lock:
            return copy.deepcopy(self._state)

    def start(self, scan=True):
        with self._lifecycle_lock:
            with self._lock:
                if self._stop_error is not None:
                    raise InspectionError('STOP not confirmed; call stop successfully before restarting')
                if self._state['active'] or (self._thread is not None and self._thread.is_alive()):
                    raise InspectionError('inspection already active')
                # An old watchdog may have committed to issuing STOP. It must
                # exit before a new run can own the hardware.
                if self._watchdog is not None and self._watchdog.is_alive():
                    raise InspectionError('previous inspection watchdog is still finishing')
                now = datetime.datetime.now()
                run_id = now.strftime('%Y%m%d-%H%M%S-') + uuid.uuid4().hex[:8]
                self._run_dir = os.path.join(self.runtime_dir, run_id)
                os.makedirs(self._run_dir)
                self._cancel = threading.Event()
                self._cancel_reason = None
                self._run_stop_failed = False
                self._matrices = {}
                self._last_heartbeat = time.monotonic()
                self._deadline = self._last_heartbeat + self.TOTAL_TIMEOUT
                self._state = {'run_id': run_id, 'active': True, 'phase': 'starting',
                               'message': '正在启动人体替代仪表的定点巡检。',
                               'started_at': datetime.datetime.now(datetime.timezone.utc).isoformat(),
                               'scan': bool(scan),
                               'targets': [], 'route': [], 'active_target': None,
                               'results': [], 'events': [], 'error': None,
                               'stop_error': None,
                               'association': 'position_only_not_person_identity',
                               'reading_mode': 'not_applicable_person_proxy',
                               'planner': 'greedy_pan_tilt_angular_distance',
                               'quality_status': 'instrument_thresholds_not_validated',
                               'capture_kind': 'same_frame_detection_preview',
                               'instrument_resolution_validated': False}
                self._thread = threading.Thread(target=self._run, args=(bool(scan),),
                                                name='inspection-worker')
                self._thread.daemon = True
                self._watchdog = threading.Thread(target=self._watch, args=(self._cancel,),
                                                  name='inspection-watchdog')
                self._watchdog.daemon = True
            # Lock ordering: persist_lock -> state lock. Never call _persist
            # while holding the state lock, including during repeated starts.
            try:
                self._persist()
            except Exception as exc:
                self._cancel.set()
                with self._lock:
                    self._state['active'] = False
                    self._state['phase'] = 'error'
                    self._state['error'] = '%s: %s' % (type(exc).__name__, exc)
                    self._state['message'] = '无法保存本轮运行记录，巡检未启动。'
                raise
            self._thread.start()
            self._watchdog.start()
        return self.snapshot()

    def heartbeat(self):
        with self._lock:
            if self._state['active'] and not self._cancel.is_set():
                self._last_heartbeat = time.monotonic()

    def stop(self, reason='user_stop'):
        # Set cancellation before issuing STOP; no subsequent guarded action can
        # start. The adapter must also check this event before each physical send.
        with self._lifecycle_lock:
            with self._lock:
                if self._cancel_reason is None:
                    self._cancel_reason = str(reason)
                self._cancel.set()
                active = self._state['active']
            stopped = self._safe_stop()
            # Drain an action that may already have entered the adapter.
            with self._action_lock:
                pass
            if active:
                self._event('已请求停止：' + str(reason), kind='stop')
            elif stopped:
                self._event('已确认停止；可重新启动巡检。', kind='stop')
            else:
                with self._lock:
                    self._state['phase'] = 'error'
                self._event('停止指令失败；确认停止成功后才能再次启动。', kind='stop_error')
            return self.snapshot()

    def _watch(self, cancel):
        while not cancel.wait(0.1):
            with self._lock:
                if cancel is not self._cancel or not self._state['active']:
                    return
                heartbeat_age = time.monotonic() - self._last_heartbeat
                expired = time.monotonic() >= self._deadline
            if expired or heartbeat_age > self.HEARTBEAT_TIMEOUT:
                self.stop('total_deadline' if expired else 'heartbeat_timeout')
                return

    def _require(self):
        if self._run_stop_failed:
            raise StopFailure('STOP failed during this run')
        if time.monotonic() >= self._deadline:
            with self._lock:
                if not self._cancel_reason:
                    self._cancel_reason = 'total_deadline'
                self._cancel.set()
        if self._cancel.is_set():
            raise Cancelled(self._cancel_reason or 'cancelled')

    def _action(self, method, *args):
        with self._action_lock:
            self._require()
            try:
                result = method(*(args + (self._cancel,)))
            except Exception:
                # Adapters may use RuntimeError when cancellation interrupts IO.
                # Classify cancellation before treating it as a target failure.
                self._require()
                raise
            self._require()
            return result

    def _safe_stop(self):
        with self._stop_lock:
            try:
                self.io.stop()
            except Exception as exc:
                with self._lock:
                    self._stop_error = '%s: %s' % (type(exc).__name__, exc)
                    self._state['stop_error'] = self._stop_error
                    self._state['error'] = self._stop_error
                    self._run_stop_failed = True
                    self._cancel_reason = 'stop_failed'
                    self._cancel.set()
                return False
            with self._lock:
                self._stop_error = None
                self._state['stop_error'] = None
            return True

    def _event(self, message, kind='info', **details):
        with self._lock:
            event = {'time': datetime.datetime.now(datetime.timezone.utc).isoformat(),
                     'monotonic': time.monotonic(), 'phase': self._state['phase'],
                     'kind': kind, 'message': message}
            event.update(details)
            self._state['events'].append(event)
            self._state['events'] = self._state['events'][-100:]
            self._state['message'] = message
        self._persist()

    def _phase(self, phase, message):
        with self._lock:
            self._state['phase'] = phase
        self._event(message)

    def _persist(self):
        with self._persist_lock:
            if self._run_dir is None:
                return
            value = self.snapshot()
            temp = os.path.join(self._run_dir, 'run.json.tmp')
            with open(temp, 'w', encoding='utf-8') as handle:
                json.dump(value, handle, ensure_ascii=False, indent=2, allow_nan=False)
                handle.flush()
            os.replace(temp, os.path.join(self._run_dir, 'run.json'))

    def _position(self):
        self._require()
        pose = self.io.position()
        self._require()
        try:
            result = {key: float(pose[key]) for key in ('pan', 'tilt', 'zoom')}
        except (KeyError, ValueError, TypeError):
            raise InspectionError('invalid camera position')
        if not all(math.isfinite(v) for v in result.values()):
            raise InspectionError('non-finite camera position')
        return result

    def _move(self, pan, tilt, zoom):
        self._require()
        pan, tilt = min(350.0, max(0.0, pan)), min(90.0, max(0.0, tilt))
        zoom = min(1.5, max(1.0, zoom))
        self._event('正在转向指定位置。', kind='move',
                    requested_pose={'pan': pan, 'tilt': tilt, 'zoom': zoom})
        self._action(self.io.move, pan, tilt, zoom)
        # Frames acquired during the move cannot be used for its feedback.
        return time.monotonic()

    def _matrix(self, zoom):
        key = round(float(zoom), 1)
        if key not in self._matrices:
            self._phase('calibrating', '正在标定 %.1f 倍变焦下的图像位移响应。' % key)
            matrix = self._action(self.io.calibrate, key)
            inverse_2x2(matrix)
            self._matrices[key] = [[float(v) for v in row] for row in matrix]
            self._event('局部标定已通过。', zoom=key, matrix=matrix)
        return self._matrices[key]

    def _observe(self, after=None, timeout=3.0, previous_sequence=None):
        deadline = min(time.monotonic() + timeout, self._deadline)
        if after is None:
            after = time.monotonic()
        while time.monotonic() < deadline:
            self._require()
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            obs = self.io.observe(after=after, timeout=min(remaining, 3.0))
            self._require()
            if not isinstance(obs, dict):
                continue
            try:
                stamp = float(obs['received_at'])
                sequence = int(obs['sequence'])
            except (KeyError, TypeError, ValueError):
                continue
            now = time.monotonic()
            if not math.isfinite(stamp) or stamp <= after or stamp > now + 0.05:
                continue
            if now - stamp > self.MAX_FRAME_AGE or sequence == previous_sequence:
                continue
            return obs
        raise InspectionError('fresh observation timeout')

    def _candidates(self, observation, pose, matrix):
        result = []
        for raw in observation.get('persons', []):
            person = valid_person(raw)
            if person is None:
                continue
            item = project_person(person, pose, matrix)
            if not (0 <= item['pan'] <= 350 and 0 <= item['tilt'] <= 90):
                continue
            result.append(item)
        return result

    def _discover(self, home, matrix, scan):
        angles = [home['pan'] + offset for offset in (-8.0, 0.0, 8.0)] if scan else [home['pan']]
        positions = []
        for value in angles:
            clipped = min(350.0, max(0.0, value))
            if clipped not in positions:
                positions.append(clipped)
        self._phase('scanning', '正在扫描限定区域；请保持站定。')
        targets = []
        for pan in positions:
            after = self._move(pan, home['tilt'], 1.0)
            pose = self._position()
            sequence = None
            for unused in range(2):
                obs = self._observe(after=after, previous_sequence=sequence)
                candidates = self._candidates(obs, pose, matrix)
                ambiguous = merge_sightings(targets, candidates, pose, obs['sequence'])
                if len(targets) > self.MAX_TARGETS:
                    raise InspectionError('too many candidates for bounded demo')
                after, sequence = obs['received_at'], obs['sequence']
                with self._lock:
                    self._state['targets'] = copy.deepcopy(targets)
                self._event('已处理一帧新的扫描画面。', sequence=sequence,
                            valid_people=len(candidates), ambiguous_matches=ambiguous)
        confirmed = [item for item in targets if item['hits'] >= 2]
        for item in confirmed:
            item['status'] = 'pending'
        with self._lock:
            self._state['targets'] = copy.deepcopy(confirmed)
            self._state['unconfirmed_candidates'] = len(targets) - len(confirmed)
        self._event('扫描完成。', confirmed_targets=len(confirmed))
        return confirmed

    def _select(self, observation, pose, matrix, target):
        candidates = self._candidates(observation, pose, matrix)
        matches = sorted([(angle_distance(item, target), index, item)
                          for index, item in enumerate(candidates)
                          if angle_distance(item, target) <= self.ASSOCIATION_GATE],
                         key=lambda value: (value[0], value[1]))
        if not matches:
            raise InspectionError('target missing, incomplete, or outside position gate')
        if len(matches) > 1 and matches[1][0] - matches[0][0] < 0.75:
            raise InspectionError('ambiguous nearby people; refusing target substitution')
        return matches[0][2]

    @staticmethod
    def _error(person):
        box = person['bbox']
        return [(box[0] + box[2]) / 2.0 - 0.5, (box[1] + box[3]) / 2.0 - 0.5]

    @staticmethod
    def _centered(error):
        return abs(error[0]) <= 0.035 and abs(error[1]) <= 0.05

    def _center(self, target, matrix, zoom, after=None):
        self._phase('centering', '正在视觉微调，使目标居中：' + target['target_id'])
        deadline = min(self._deadline, time.monotonic() + self.CENTER_TIMEOUT)
        stable, previous_sequence, missing = 0, None, 0
        while time.monotonic() < deadline:
            self._require()
            obs = self._observe(after=after, timeout=min(3.0, deadline - time.monotonic()),
                                previous_sequence=previous_sequence)
            pose = self._position()
            after, previous_sequence = obs['received_at'], obs['sequence']
            try:
                selected = self._select(obs, pose, matrix, target)
            except InspectionError as exc:
                stable = 0
                missing += 1
                if not self._safe_stop():
                    raise StopFailure('STOP failed after target association loss')
                self._require()
                if 'ambiguous' in str(exc) or missing >= 3:
                    raise
                continue
            missing = 0
            error = self._error(selected)
            if self._centered(error):
                stable += 1
                if stable >= 3:
                    return obs, selected, pose
                continue
            stable = 0
            delta = correction_degrees(matrix, error, gain=0.65, limit=2.0)
            after = self._move(pose['pan'] + delta[0], pose['tilt'] + delta[1], zoom)
        raise InspectionError('visual centering exceeded 20 second budget')

    def _capture(self, target, matrix, after):
        self._phase('capturing', '正在确认连续三帧目标完整且稳定，随后保存图像。')
        deadline = min(self._deadline, time.monotonic() + 8.0)
        count, previous, sequence = 0, None, None
        while time.monotonic() < deadline:
            obs = self._observe(after=after, previous_sequence=sequence,
                                timeout=min(3.0, deadline - time.monotonic()))
            pose = self._position()
            selected = self._select(obs, pose, matrix, target)
            box = selected['bbox']
            error = self._error(selected)
            after, sequence = obs['received_at'], obs['sequence']
            if not self._centered(error):
                raise InspectionError('target left center before capture')
            if previous is not None and max(abs(a - b) for a, b in zip(box, previous)) > 0.025:
                count = 0
            previous = box
            count += 1
            if count >= 3:
                if not isinstance(obs.get('jpeg'), bytes) or not obs['jpeg']:
                    raise InspectionError('same-frame JPEG unavailable')
                return obs, selected, pose
        raise InspectionError('target did not remain stable for capture')

    def _target_status(self, target_id, status):
        with self._lock:
            for target in self._state['targets']:
                if target['target_id'] == target_id:
                    target['status'] = status

    def _visit(self, target):
        with self._lock:
            self._state['active_target'] = target['target_id']
        self._target_status(target['target_id'], 'visiting')
        self._phase('positioning', '正在粗定位至目标：' + target['target_id'])
        after = self._move(target['pan'], target['tilt'], 1.0)
        matrix = self._matrix(1.0)
        obs, person, pose = self._center(target, matrix, 1.0, after=after)
        box = person['bbox']
        width, height = box[2] - box[0], box[3] - box[1]
        desired = min(1.5, 0.65 / height, 0.90 / width, 0.90 / height)
        zoom = max(1.0, math.floor((desired + 1e-8) * 10.0) / 10.0)
        if zoom > 1.0:
            self._phase('zooming', '正在放大至 %.1f 倍，并保持目标完整。' % zoom)
            self._move(pose['pan'], pose['tilt'], zoom)
            matrix = self._matrix(zoom)
            # Calibration must return to the same pose; only later frames count.
            obs, person, pose = self._center(target, matrix, zoom, after=time.monotonic())
        obs, person, pose = self._capture(target, matrix, obs['received_at'])
        self._require()
        record = {'target_id': target['target_id'], 'status': 'captured',
                  'captured_at': datetime.datetime.now(datetime.timezone.utc).isoformat(),
                  'source_sequence': obs['sequence'],
                  'frame_received_at_monotonic': obs['received_at'],
                  'bbox': list(person['bbox']), 'confidence': person['confidence'],
                  'pose': pose, 'reading': None, 'unit': None,
                  'reading_status': 'not_applicable_person_proxy',
                  'quality_status': 'instrument_thresholds_not_validated',
                  'capture_kind': 'same_frame_detection_preview',
                  'instrument_resolution_validated': False}
        saved = self.io.save(obs, record, self._run_dir)
        self._require()
        if not isinstance(saved, dict) or not saved.get('image') or not saved.get('crop'):
            raise InspectionError('image save did not return image and crop evidence')
        # IO can add evidence fields but cannot redefine the reading/target claim.
        for key in ('image', 'crop', 'sharpness', 'source_sequence',
                    'focus_style', 'sharpness_status'):
            if key in saved:
                record[key] = saved[key]
        if record['source_sequence'] != obs['sequence']:
            raise InspectionError('saved evidence does not match detection frame')
        self._target_status(target['target_id'], 'captured')
        self._event('目标图像已保存；人体替代测试不生成仪表读数。',
                    target_id=target['target_id'], image=record['image'])
        return record

    def _run(self, scan):
        terminal_phase = 'error'
        terminal_message = '巡检意外结束。'
        try:
            home = self._position()
            with self._lock:
                self._state['home'] = home
            self._move(home['pan'], home['tilt'], 1.0)
            matrix = self._matrix(1.0)
            targets = self._discover(home, matrix, scan)
            self._require()
            if not targets:
                raise InspectionError('no complete person confirmed in two fresh frames')
            self._phase('planning', '正在按角运动距离规划近邻访问顺序。')
            route = nearest_route(targets, self._position())
            with self._lock:
                self._state['route'] = route
            self._persist()
            by_id = {item['target_id']: item for item in targets}
            for target_id in route:
                self._require()
                try:
                    record = self._visit(by_id[target_id])
                except Cancelled:
                    raise
                except Exception as exc:
                    # Cancellation and STOP failure take precedence over a
                    # recoverable failure of an individual target.
                    self._require()
                    if not self._safe_stop():
                        raise StopFailure('STOP failed after target error')
                    self._require()
                    self._target_status(target_id, 'failed')
                    record = {'target_id': target_id, 'status': 'failed',
                              'reading': None, 'reading_status': 'not_applicable_person_proxy',
                              'error': '%s: %s' % (type(exc).__name__, exc)}
                    self._event('该目标处理失败；准备继续下一个位置。',
                                kind='target_error', target_id=target_id, error=record['error'])
                with self._lock:
                    self._state['results'].append(record)
                    self._state['active_target'] = None
                self._persist()
            count = sum(1 for item in self.snapshot()['results'] if item['status'] == 'captured')
            self._require()
            failed = len(route) - count
            terminal_phase = 'completed_with_errors' if failed else 'completed'
            terminal_message = '本轮结束：成功 %d/%d，失败 %d；%s' % (
                count, len(route), failed,
                '失败原因见目标记录。' if failed else '目标图像已保存。')
        except Cancelled as exc:
            target_id = self.snapshot()['active_target']
            if target_id:
                self._target_status(target_id, 'cancelled')
            terminal_phase = 'stopped'
            terminal_message = '巡检已取消：' + str(exc)
        except Exception as exc:
            if self._cancel.is_set() and not self._run_stop_failed:
                target_id = self.snapshot()['active_target']
                if target_id:
                    self._target_status(target_id, 'cancelled')
                terminal_phase = 'stopped'
                terminal_message = '巡检已取消：' + str(self._cancel_reason or 'cancelled')
            else:
                with self._lock:
                    if not self._state['error']:
                        self._state['error'] = '%s: %s' % (type(exc).__name__, exc)
                terminal_phase = 'error'
                terminal_message = '巡检失败：' + str(self._state['error'])
        finally:
            # Completion is published only after the final STOP succeeds. A
            # later successful STOP can clear the next-start interlock, but it
            # never rewrites a run that experienced a STOP failure as completed.
            self._safe_stop()
            with self._lock:
                if self._run_stop_failed:
                    terminal_phase = 'error'
                    saved = sum(1 for item in self._state['results'] if item['status'] == 'captured')
                    terminal_message = '已保存 %d/%d 个目标图像，但本轮停止指令失败，整轮运行未通过。' % (
                        saved, len(self._state['route']))
                    target_id = self._state['active_target']
                    if target_id:
                        self._target_status(target_id, 'failed')
                elif self._cancel.is_set() and terminal_phase in ('completed', 'completed_with_errors'):
                    terminal_phase = 'stopped'
                    terminal_message = '巡检已取消：' + str(self._cancel_reason or 'cancelled')
                self._cancel.set()
                self._state['phase'] = terminal_phase
                self._state['active'] = False
                self._state['active_target'] = None
                self._state['finished_at'] = datetime.datetime.now(datetime.timezone.utc).isoformat()
            self._event(terminal_message, kind='terminal')
