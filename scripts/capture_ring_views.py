#!/usr/bin/env python3
"""Prepare a single-row scan; execute only an explicitly saved, reviewed plan."""
import argparse
import getpass
import json
from pathlib import Path
import signal
import socket
import sys
import threading
from urllib.parse import urlsplit
from urllib.request import build_opener,ProxyHandler

from capture_review_views import SnapshotCamera, motion_lease, check_other_services, ROOT
from scan_capture import ISAPIScanIO
from ring_capture import ISAPIRingIO, RingCapture, ring_plan, validate_plan
from store import identifier


def check_additional_services():
    op=build_opener(ProxyHandler({}))
    for port in (8774,8775):
        sock=socket.socket();sock.settimeout(.4)
        try:listening=sock.connect_ex(('127.0.0.1',port))==0
        finally:sock.close()
        if not listening:continue
        path='/api/state' if port==8775 else '/api/status'
        with op.open('http://127.0.0.1:%d%s'%(port,path),timeout=3) as response:
            state=json.loads(response.read().decode('utf-8'))
        if port==8775:
            if state['runtime']['mode']!='review_only' or state['runtime']['active_job'] or state['schedule']['enabled']:
                raise RuntimeError('review console has an active hardware task')
        else:
            keys=('active','enabled','control_enabled','tracking')
            if not any(k in state for k in keys) or any(state.get(k) for k in keys):
                raise RuntimeError('another camera control service may be active')


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--camera',required=True)
    parser.add_argument('--user',required=True)
    parser.add_argument('--password-stdin',action='store_true')
    parser.add_argument('--plan-out',help='New plan JSON file; preparation sends no movement')
    parser.add_argument('--plan',help='Previously prepared plan for execution')
    parser.add_argument('--execute',action='store_true')
    parser.add_argument('--output',help='New private capture directory')
    args=parser.parse_args()
    if args.execute and (not args.plan or args.plan_out or not sys.platform.startswith('linux')):
        parser.error('execution needs --plan, runs on Linux TX2, and cannot prepare a plan at the same time')
    if not args.execute and (args.plan or args.output):
        parser.error('use --plan-out for read-only preparation')
    url=urlsplit(args.camera)
    if url.scheme not in ('http','https') or not url.hostname or url.username or url.password or url.path not in ('','/') or url.query or url.fragment:
        parser.error('camera must be an origin without credentials')
    secret=json.loads(sys.stdin.read())['password'] if args.password_stdin else getpass.getpass('Camera password: ')
    camera=SnapshotCamera(args.camera,args.user,secret);del secret
    if not args.execute:
        preflight=ISAPIScanIO(camera).preflight()
        plan=ring_plan(preflight['anchor'])
        if args.plan_out:
            with Path(args.plan_out).open('x',encoding='utf-8') as stream:
                json.dump(plan,stream,ensure_ascii=False,indent=2)
        print(json.dumps(dict(motion_sent=False,plan=plan)),flush=True)
        return 0
    plan=validate_plan(json.loads(Path(args.plan).read_text(encoding='utf-8')))
    cancel=threading.Event()
    def stop(*unused):cancel.set()
    signal.signal(signal.SIGINT,stop);signal.signal(signal.SIGTERM,stop)
    with motion_lease():
        check_other_services();check_additional_services()
        io=ISAPIRingIO(camera,plan);io.preflight()
        output=Path(args.output) if args.output else ROOT/'var/scans'/identifier('ring')
        result=RingCapture(io,output,plan,cancel).run()
        print(json.dumps(dict(directory=str(output),status=result['status'],frames=len(result['frames']),
            error=result.get('error'),full_panorama=False)),flush=True)
        return 0 if result['status']=='completed' else 1


if __name__=='__main__':sys.exit(main())
