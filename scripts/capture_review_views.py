#!/usr/bin/env python3
"""Default is read-only preflight. --execute runs a bounded +/-5 degree scan.

Use only when the platform is stationary, the camera fixed and the local area
clear. This produces raw views, not a panorama or instrument identity map.
"""
import argparse
from contextlib import contextmanager
import getpass
import json
import os
from pathlib import Path
import signal
import socket
import stat
import sys
import threading
from urllib.parse import urlsplit
from urllib.request import build_opener, ProxyHandler, Request

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'src/person_tracker'))
from ptz_control import Camera
sys.path.insert(0,str(ROOT/'src/inspection_console'))
from scan_capture import BoundedCapture, ISAPIScanIO
from store import identifier


class SnapshotCamera(Camera):
    def request(self,path,body=None):
        request=Request(self.url+path,data=body,method='PUT' if body is not None else 'GET',
                        headers={'Content-Type':'application/xml','Cache-Control':'no-cache'})
        with self.opener.open(request,timeout=3) as response:
            data=response.read(12000001)
        if len(data)>12000000:
            raise ValueError('camera_response_too_large')
        return data


@contextmanager
def motion_lease():
    import fcntl
    path='/tmp/jetson-ptz-inspection-motion.lock'
    fd=os.open(path,os.O_RDWR|os.O_CREAT|getattr(os,'O_NOFOLLOW',0),0o600)
    try:
        info=os.fstat(fd)
        if info.st_uid!=os.geteuid() or not stat.S_ISREG(info.st_mode):
            raise RuntimeError('motion lease ownership mismatch')
        fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
        yield
    finally:
        os.close(fd)


def check_other_services():
    opener=build_opener(ProxyHandler({}))
    for port in range(8767,8774):
        sock=socket.socket();sock.settimeout(.3)
        try:
            listening=sock.connect_ex(('127.0.0.1',port))==0
        finally:
            sock.close()
        if not listening:
            continue
        with opener.open('http://127.0.0.1:%d/api/status'%port,timeout=2) as response:
            data=json.loads(response.read().decode('utf-8'))
        if not any(key in data for key in ('active','enabled','control_enabled','tracking')):
            raise RuntimeError('unrecognized existing control service')
        if any(data.get(key) for key in ('active','enabled','control_enabled','tracking')):
            raise RuntimeError('another control service is active')


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--camera',required=True,help='Camera http(s) origin without credentials')
    parser.add_argument('--user',required=True)
    parser.add_argument('--password-stdin',action='store_true',help='One JSON object on stdin; secret is not logged')
    parser.add_argument('--execute',action='store_true')
    parser.add_argument('--output',help='New run directory; must not exist')
    args=parser.parse_args()
    if args.execute and not sys.platform.startswith('linux'):
        parser.error('execute mode runs on the Linux TX2 with its shared motion lease')
    url=urlsplit(args.camera)
    if url.scheme not in ('http','https') or not url.hostname or url.username or url.password or url.path not in ('','/') or url.query or url.fragment:
        parser.error('camera must be an origin without credentials or path')
    secret=json.loads(sys.stdin.read())['password'] if args.password_stdin else getpass.getpass('Camera password: ')
    camera=SnapshotCamera(args.camera,args.user,secret)
    del secret
    io=ISAPIScanIO(camera)
    if not args.execute:
        print(json.dumps(io.preflight()),flush=True)
        return 0
    cancel=threading.Event()
    def stop(*unused):
        cancel.set()
    signal.signal(signal.SIGINT,stop)
    signal.signal(signal.SIGTERM,stop)
    with motion_lease():
        check_other_services()
        io.preflight()
        output=Path(args.output) if args.output else ROOT/'var/scans'/identifier('scan')
        run=BoundedCapture(io,output,cancel)
        result=run.run()
        print(json.dumps(dict(directory=str(output),status=result['status'],
                             frames=len(result['frames']),error=result.get('error'))),flush=True)
        return 0 if result['status']=='completed' else 1


if __name__=='__main__':
    sys.exit(main())
