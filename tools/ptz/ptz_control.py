#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Small ISAPI PTZ verification console, Python 3.5+. Password stays in RAM."""
import argparse
import getpass
import json
import os
import time
import xml.etree.ElementTree as ET
from urllib.request import (Request, build_opener, HTTPDigestAuthHandler,
                            HTTPPasswordMgrWithDefaultRealm, ProxyHandler)
from urllib.error import HTTPError
from probe_camera import NoRedirect


class Camera:
    def __init__(self, url, user, password):
        self.url = url.rstrip('/')
        manager = HTTPPasswordMgrWithDefaultRealm()
        manager.add_password(None, self.url + '/', user, password)
        self.opener = build_opener(ProxyHandler({}), NoRedirect(), HTTPDigestAuthHandler(manager))

    def request(self, path, body=None):
        req = Request(self.url + path, data=body, method='PUT' if body is not None else 'GET',
                      headers={'Content-Type': 'application/xml'})
        with self.opener.open(req, timeout=3) as response:
            data = response.read(12 * 1024 * 1024)
        return data

    def xml(self, path, body=None):
        data = self.request(path, body)
        root = ET.fromstring(data)
        values = {n.tag.split('}')[-1]: n.text for n in root.iter() if len(n) == 0}
        if root.tag.split('}')[-1] == 'ResponseStatus' and values.get('statusCode') != '1':
            raise RuntimeError('ISAPI response: ' + json.dumps(values))
        return values

    def move(self, pan=0, tilt=0, zoom=0, duration=None):
        suffix = 'momentary' if duration is not None else 'continuous'
        extra = '<Momentary><duration>{}</duration></Momentary>'.format(duration) if duration is not None else ''
        body = ('<PTZData version="2.0" xmlns="http://www.hikvision.com/ver20/XMLSchema">'
                '<pan>{}</pan><tilt>{}</tilt><zoom>{}</zoom>{}</PTZData>').format(pan, tilt, zoom, extra)
        return self.xml('/ISAPI/PTZCtrl/channels/1/' + suffix, body.encode('utf-8'))

    def stop(self):
        return self.move()

    def status(self):
        return self.xml('/ISAPI/PTZCtrl/channels/1/status')


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--url', default='http://192.168.1.64')
    parser.add_argument('--user', required=True)
    args = parser.parse_args()
    camera = Camera(args.url, args.user, getpass.getpass('Camera password (not saved): '))
    os.makedirs('verification', exist_ok=True)
    logfile = os.path.join('verification', 'events-{}.jsonl'.format(time.strftime('%Y%m%d-%H%M%S')))
    def record(event, **values):
        values.update(event=event, time=time.strftime('%Y-%m-%dT%H:%M:%S%z'))
        with open(logfile, 'a') as f:
            f.write(json.dumps(values) + '\n')
        print(json.dumps(values), flush=True)
    print('Commands: status, stop, snapshot, streams, ports, momentary-check, pan N MS, tilt N MS, zoom N MS, quit')
    try:
        while True:
            line = input('ptz> ').split()
            if not line:
                continue
            try:
                cmd = line[0]
                if cmd == 'quit':
                    break
                elif cmd == 'status':
                    record(cmd, result=camera.status())
                elif cmd == 'stop':
                    record(cmd, result=camera.stop())
                elif cmd == 'momentary-check':
                    record(cmd, result=camera.move(duration=100))
                elif cmd in ('streams', 'ports'):
                    path = '/ISAPI/Streaming/channels' if cmd == 'streams' else '/ISAPI/Security/adminAccesses'
                    data = camera.request(path)
                    target = 'verification/' + cmd + '.xml'
                    with open(target, 'wb') as f:
                        f.write(data)
                    record(cmd, file=target)
                elif cmd == 'snapshot':
                    data = camera.request('/ISAPI/Streaming/channels/101/picture')
                    if not data.startswith(b'\xff\xd8'):
                        raise ValueError('Not a JPEG snapshot')
                    target = 'verification/snapshot-{}.jpg'.format(time.strftime('%Y%m%d-%H%M%S'))
                    with open(target, 'wb') as f:
                        f.write(data)
                    record(cmd, file=target, bytes=len(data))
                elif cmd in ('pan', 'tilt', 'zoom') and len(line) == 3:
                    speed, duration = int(line[1]), int(line[2])
                    if not -20 <= speed <= 20 or not 50 <= duration <= 500:
                        raise ValueError('Verification limits: speed -20..20, duration 50..500 ms')
                    camera.stop()
                    before = camera.status()
                    record('before', axis=cmd, result=before)
                    try:
                        reply = camera.move(duration=duration, **{cmd: speed})
                        record('momentary-command', axis=cmd, speed=speed, duration_ms=duration, result=reply)
                        time.sleep(duration / 1000.0 + 0.3)
                    finally:
                        record('stop', result=camera.stop())
                    time.sleep(0.7)
                    after = camera.status()
                    time.sleep(1)
                    stable = camera.status()
                    record('after', result=after, later=stable, unchanged=after == stable)
                else:
                    print('Unknown command. Use the listed commands.')
            except HTTPError as exc:
                record('error', kind='HTTPError', code=exc.code)
                if exc.code in (401, 403):
                    break
            except (OSError, ValueError, RuntimeError, ET.ParseError) as exc:
                record('error', kind=type(exc).__name__, detail=str(exc))
    except (KeyboardInterrupt, EOFError):
        pass
    finally:
        try:
            record('final-stop', result=camera.stop())
        except Exception as exc:
            print('STOP NOT CONFIRMED: ' + type(exc).__name__, flush=True)


if __name__ == '__main__':
    main()
