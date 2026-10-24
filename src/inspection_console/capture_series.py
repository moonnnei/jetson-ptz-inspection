"""At most three explicitly planned revisit trials, never a periodic scheduler."""
import copy
import hashlib
import json
import math
import os
from pathlib import Path
import statistics
import threading
import time

from capture_round import CaptureRound,round_plan,validate_round
from references import digest
from ring_capture import close_pose
from store import now


def series_plan(state,map_id,anchor,rounds=3):
    if type(rounds) is not int or not 2<=rounds<=3:
        raise ValueError('a finite repeatability trial requires two or three rounds')
    base=round_plan(state,map_id,anchor)
    plan=dict(schema_version=1,kind='bounded_repeatability_trial',round_plan=base,
        rounds=rounds,pause_seconds=3,
        budget_seconds=base['budget_seconds']*rounds+3*(rounds-1)+10,
        stop_on_any_incomplete_round=True,return_each_round=True,
        reference_policy='same_saved_reference_each_round',
        periodic_capture_enabled=False,visit_pose_verified=False,reading=None,
        ready_for_trial=base['ready_for_trial'],execution_enabled=False)
    plan['fingerprint']=digest(plan)
    return plan


def validate_series(plan,state):
    expected=series_plan(state,plan['round_plan']['map_id'],plan['round_plan']['anchor'],plan['rounds'])
    if plan!=expected:raise ValueError('repeatability plan or review changed')
    return copy.deepcopy(plan)


def save(path,value):
    temporary=path.with_suffix('.tmp')
    temporary.write_text(json.dumps(value,ensure_ascii=False,indent=2,allow_nan=False),encoding='utf-8')
    os.replace(str(temporary),str(path))


def read_records(directory):
    root=Path(directory).resolve();path=root/'series.json'
    if path.is_symlink() or path.stat().st_size>2000000:raise ValueError('invalid series record')
    series=json.loads(path.read_text(encoding='utf-8'))
    if series['status'] not in ('completed','failed','stopped'):
        raise ValueError('series is not finished')
    if (series.get('visit_pose_verified') is not False or series.get('periodic_capture_enabled') is not False
            or series.get('reading') is not None):raise ValueError('series cannot commission positions or readings')
    records=[];last_end=None;seen=set()
    for index,item in enumerate(series['rounds'],1):
        name='round-%02d'%index
        if item['directory']!=name:raise ValueError('series rounds must be unique ordered child directories')
        folder=root/name;record_path=folder/'record.json'
        if folder.is_symlink() or record_path.is_symlink() or folder.resolve().parent!=root:
            raise ValueError('round leaves series directory')
        raw=record_path.read_bytes()
        if len(raw)>5000000 or hashlib.sha256(raw).hexdigest()!=item['record_sha256']:
            raise ValueError('series round record changed')
        record=json.loads(raw.decode('utf-8'))
        if record['plan']!=series['plan']['round_plan'] or record['status']!=item['status']:
            raise ValueError('round plan or status differs from series binding')
        if item['record_sha256'] in seen:raise ValueError('replayed round record')
        seen.add(item['record_sha256'])
        if (record.get('visit_pose_verified') is not False or record.get('periodic_capture_enabled') is not False
                or record.get('reading') is not None):raise ValueError('round changed verification scope')
        moments=[f['received_monotonic'] for f in record['captures']]
        if any(not isinstance(t,(int,float)) or not math.isfinite(t) for t in moments):
            raise ValueError('invalid capture chronology')
        if any(a>b for a,b in zip(moments,moments[1:])) or last_end is not None and moments and moments[0]<=last_end:
            raise ValueError('round capture chronology overlaps or replays')
        if moments:last_end=moments[-1]
        records.append(record)
    if len(records)>series['plan']['rounds']:raise ValueError('too many rounds')
    if series['status']=='completed' and (len(records)!=series['plan']['rounds'] or
            any(r['status']!='completed' or not r.get('stop_confirmed') for r in records)):
        raise ValueError('series completion lacks successful stopped rounds')
    return series,records


def summarize(series,records):
    planned=series['plan']['rounds'];targets=series['plan']['round_plan']['stops']
    results=[]
    for target in targets:
        key=target['id'];visits=[r for record in records for r in record['results'] if r['instrument_id']==key]
        matched=[r for r in visits if r['status']=='captured_reference_checked' and r.get('matching_fresh_observations')==2]
        errors=[r['center_error'] for r in matched]
        times=[r['elapsed_seconds'] for r in visits if isinstance(r.get('elapsed_seconds'),(int,float))]
        attempted=sum(any(e.get('event')=='target_started' and e.get('instrument_id')==key
                         for e in record['events']) for record in records)
        results.append(dict(instrument_id=key,planned_visits=planned,attempted_visits=attempted,
            successful_visits=len(matched),success_fraction_of_planned=len(matched)/float(planned),
            success_fraction_of_attempted=len(matched)/float(attempted) if attempted else None,
            mean_visit_seconds=statistics.mean(times) if times else None,
            mean_center_error=[statistics.mean(e[k] for e in errors) for k in (0,1)] if errors else None,
            center_error_population_std=[statistics.pstdev(e[k] for e in errors) for k in (0,1)] if len(errors)>=2 else None,
            precisely_centered_visits=sum(abs(e[0])<=.035 and abs(e[1])<=.04 for e in errors)))
    times=[r['elapsed_seconds'] for r in records]
    return dict(planned_rounds=planned,attempted_rounds=len(records),
        completed_rounds=sum(r['status']=='completed' and bool(r.get('stop_confirmed')) for r in records),
        mean_round_seconds=statistics.mean(times) if times else None,
        min_round_seconds=min(times) if times else None,max_round_seconds=max(times) if times else None,
        fresh_capture_requests=sum(len(r['captures']) for r in records),targets=results,
        jitter_basis='normalized_image_center_at_same_saved_pose_not_encoder_accuracy',
        sample_scope='finite_fixed_scene_trials_not_long_term_reliability',
        reading_integrated=False,visit_poses_commissioned=False,periodic_capture_enabled=False)


class CaptureSeries:
    def __init__(self,io_factory,state_provider,directory,plan,cancel=None):
        self.io_factory=io_factory;self.state_provider=state_provider
        self.root=Path(directory);self.plan=copy.deepcopy(plan)
        self.cancel=cancel or threading.Event()
        self.record=dict(kind=plan['kind'],plan=copy.deepcopy(plan),status='prepared',rounds=[],
            reading=None,visit_pose_verified=False,periodic_capture_enabled=False)

    def persist(self):save(self.root/'series.json',self.record)

    def run(self):
        validate_series(self.plan,self.state_provider())
        if not self.plan['ready_for_trial']:raise ValueError('references are incomplete')
        self.root.mkdir(parents=True,exist_ok=False)
        started=time.monotonic();deadline=started+self.plan['budget_seconds']
        self.record.update(status='running',started_at=now());self.persist()
        timer=threading.Timer(self.plan['budget_seconds'],self.cancel.set);timer.daemon=True;timer.start()
        records=[]
        try:
            for index in range(1,self.plan['rounds']+1):
                if self.cancel.is_set() or time.monotonic()>deadline:raise RuntimeError('series_cancelled_or_expired')
                validate_series(self.plan,self.state_provider())
                io=self.io_factory(self.plan['round_plan'])
                self.record['active_round']=index;self.persist()
                folder=self.root/('round-%02d'%index)
                result=CaptureRound(io,folder,self.plan['round_plan'],self.cancel).run();records.append(result)
                self.record['rounds'].append(dict(directory=folder.name,status=result['status'],
                    record_sha256=hashlib.sha256((folder/'record.json').read_bytes()).hexdigest()))
                self.persist()
                if result['status']!='completed' or not result.get('stop_confirmed'):
                    raise RuntimeError('round_not_fully_successful_STOP_or_return_not_accepted')
                if not close_pose(io.position(),self.plan['round_plan']['anchor']):
                    raise RuntimeError('round_did_not_restore_anchor')
                if index<self.plan['rounds'] and self.cancel.wait(self.plan['pause_seconds']):
                    raise RuntimeError('series_cancelled_between_rounds')
            self.record.update(status='completed',active_round=None)
        except Exception as exc:
            stop_fault=bool(records) and records[-1].get('stop_confirmed') is False
            self.record.update(status='failed' if stop_fault else 'stopped' if self.cancel.is_set() else 'failed',
                               error=type(exc).__name__+': '+str(exc),active_round=None)
        finally:
            timer.cancel()
            self.record.update(finished_at=now(),elapsed_seconds=time.monotonic()-started,
                summary=summarize(self.record,records))
            self.persist()
        return copy.deepcopy(self.record)
