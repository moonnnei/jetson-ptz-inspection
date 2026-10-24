#!/usr/bin/env python3
"""Prepare a no-motion single-meter plan; execute only after site readiness.

Secrets arrive in one JSON object on stdin. Do not put credentials in argv.
This command does not enable the periodic scheduler or edit the registry.
"""
import argparse
import json
from pathlib import Path
import signal
import sys
import threading

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'src/inspection_console'))
sys.path.insert(0,str(ROOT/'scripts'))
from capture_review_views import SnapshotCamera, motion_lease, check_other_services
from capture_ring_views import check_additional_services
from references import reference_plan, validate_current
from scan_capture import ISAPIScanIO
from store import Store
from teaching import pilot_plan, validate_plan, Teaching
from teaching_io import TeachingIO


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--console-root',required=True)
    parser.add_argument('--references',required=True)
    parser.add_argument('--instrument',default='M0007')
    parser.add_argument('--allow-center-residual',action='store_true',
                        help='Separate bounded capture mode; never commissions visit poses')
    parser.add_argument('--approach-source',action='store_true',
                        help='Include the source-view search range in the prepared capture plan')
    parser.add_argument('--plan-out')
    parser.add_argument('--plan')
    parser.add_argument('--execute',action='store_true')
    parser.add_argument('--acknowledge-plan')
    parser.add_argument('--output')
    args=parser.parse_args()
    if args.approach_source and not args.allow_center_residual:
        parser.error('--approach-source requires --allow-center-residual')
    if args.execute and (not args.plan or not args.output or args.plan_out or not sys.platform.startswith('linux')):
        parser.error('execution requires Linux, --plan, --output and a prepared plan acknowledgment')
    if not args.execute and (not args.plan_out or args.plan or args.output):
        parser.error('preparation requires a new --plan-out')
    references=json.loads(Path(args.references).read_text(encoding='utf-8'))
    console=Path(args.console_root)
    validate_current(references,Store(console/'var/inspection-console').snapshot())
    auth=json.load(sys.stdin)
    camera=SnapshotCamera('http://192.168.1.64','admin',auth.pop('password'))
    check_other_services()
    check_additional_services()
    if not args.execute:
        report=ISAPIScanIO(camera).preflight()
        if args.allow_center_residual:
            from best_effort import capture_plan
            plan=capture_plan(references,report['anchor'],args.instrument,args.approach_source)
        else:plan=pilot_plan(references,report['anchor'],args.instrument)
        with Path(args.plan_out).open('x',encoding='utf-8') as stream:
            json.dump(plan,stream,ensure_ascii=False,indent=2)
        print(json.dumps(dict(motion_sent=False,plan=plan)),flush=True)
        return 0
    plan=json.loads(Path(args.plan).read_text(encoding='utf-8'))
    if plan['kind']=='bounded_meter_constrained_capture':
        from best_effort import validate_capture_plan,ConstrainedCapture
        plan=validate_capture_plan(plan,references)
        controller=ConstrainedCapture
    else:
        plan=validate_plan(plan,references)
        controller=Teaching
    if args.acknowledge_plan!=plan['fingerprint']:
        parser.error('explicit acknowledgment must match the prepared fingerprint')
    cancel=threading.Event()
    def stop(*unused):cancel.set()
    signal.signal(signal.SIGINT,stop)
    signal.signal(signal.SIGTERM,stop)
    with motion_lease():
        # Recheck within the shared lease, immediately before the real pilot.
        check_other_services()
        check_additional_services()
        io=TeachingIO(camera,ROOT,console,references)
        result=controller(io,args.output,plan,cancel).run()
    print(json.dumps(dict(status=result['status'],error=result.get('error'),
        revisit_verified=result.get('revisit_verified',False),stop_confirmed=result['stop_confirmed'],
        elapsed_seconds=result['elapsed_seconds'],directory=args.output)),flush=True)
    return 0 if result['status'] in ('completed','partial_capture_completed') else 1


if __name__=='__main__':
    sys.exit(main())
