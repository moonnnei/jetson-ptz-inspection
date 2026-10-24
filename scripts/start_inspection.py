"""One-command console startup, compatible with TX2 Python 3.5.

Configuration contains paths and network addresses only. Camera credentials are
prompted locally and delivered to the child over stdin, never its arguments.
Starting the server does not start a scan, patrol or periodic schedule.
"""
import argparse
import getpass
import json
import os
from pathlib import Path
import subprocess
import sys
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]


def command(config):
    required = {'camera_origin', 'camera_user', 'detector_root'}
    optional = {'data', 'console_root', 'host', 'port', 'allowed_hosts',
                'preview_port', 'preview_focal_pixels'}
    if not isinstance(config, dict) or not required <= set(config) or set(config) - required - optional:
        raise ValueError('configuration keys do not match the password-free schema')
    origin = urlsplit(config['camera_origin'])
    if (origin.scheme not in ('http', 'https') or not origin.hostname
            or origin.username or origin.password or origin.query or origin.fragment
            or origin.path not in ('', '/')):
        raise ValueError('camera_origin must contain only an HTTP(S) origin, without credentials')
    user = config['camera_user']
    if not isinstance(user, str) or not user.strip() or len(user) > 64:
        raise ValueError('camera_user is required')
    def path(key, default=None):
        value = config.get(key, default)
        if not isinstance(value, str) or not value:
            raise ValueError('a nonempty path is required: ' + key)
        expanded = Path(os.path.expanduser(value))
        return str(expanded if expanded.is_absolute() else ROOT / expanded)
    port = config.get('port', 8775)
    preview = config.get('preview_port')
    if type(port) is not int or not 1024 <= port <= 65535:
        raise ValueError('port must be an integer in 1024..65535')
    if preview is not None and (type(preview) is not int or not 1024 <= preview <= 65535 or preview == port):
        raise ValueError('preview_port must be a different unprivileged local port')
    hosts = config.get('allowed_hosts', [])
    if not isinstance(hosts, list) or any(not isinstance(h, str) or not h or '/' in h for h in hosts):
        raise ValueError('allowed_hosts must be an array of host[:port] strings')
    data=path('data','var/inspection-console')
    console=path('console_root','.')
    if os.path.realpath(data)!=os.path.realpath(os.path.join(console,'var','inspection-console')):
        raise ValueError('data must equal console_root/var/inspection-console for the camera adapter')
    args = [sys.executable, str(ROOT / 'src/inspection_console/server.py'),
            '--data', data,
            '--host', config.get('host', '127.0.0.1'), '--port', str(port),
            '--first-capture-camera', config['camera_origin'].rstrip('/'),
            '--first-capture-user', user, '--first-capture-console-root', console,
            '--first-capture-project-root', str(ROOT), '--first-capture-password-stdin',
            '--software-patrol', '--auto-annotations', '--scene-initialization',
            '--meter-detector-root', path('detector_root')]
    for host in hosts:
        args.extend(['--allowed-host', host])
    if preview is not None:
        args.extend(['--preview-port', str(preview)])
    if 'preview_focal_pixels' in config:
        import math
        focal = config['preview_focal_pixels']
        if type(focal) not in (int, float) or not math.isfinite(focal) or not 100 <= focal <= 10000:
            raise ValueError('preview_focal_pixels must be a finite camera-profile estimate')
        args.extend(['--preview-focal-pixels', str(focal)])
    return args


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', required=True, help='Password-free local JSON configuration')
    parser.add_argument('--check', action='store_true', help='Validate configuration without camera access or startup')
    options = parser.parse_args()
    with open(options.config, encoding='utf8') as stream:
        config = json.load(stream)
    args = command(config)
    if options.check:
        print(json.dumps(dict(valid=True, camera_access=False, starts_scan=False,
                              starts_periodic_schedule=False, server=str(ROOT / 'src/inspection_console/server.py'))))
        return
    if not sys.platform.startswith('linux'):
        parser.error('hardware startup requires Linux; --check is available on a development computer')
    password = getpass.getpass('Camera password (memory only): ')
    child = None
    try:
        child = subprocess.Popen(args, stdin=subprocess.PIPE)
        child.stdin.write(json.dumps({'password': password}).encode('utf8'))
        child.stdin.close()
        del password
        try:
            code = child.wait()
        except KeyboardInterrupt:
            # Both foreground processes receive SIGINT; SIGTERM is also handled
            # by the console's bounded shutdown and STOP path.
            child.terminate()
            code = child.wait(timeout=35)
        raise SystemExit(code)
    finally:
        if child is not None and child.poll() is None:
            child.terminate()
            child.wait(timeout=35)


if __name__ == '__main__':
    main()
