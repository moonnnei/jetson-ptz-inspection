#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Read-only ISAPI probe; Python 3.5+, standard library only."""
import argparse
import getpass
import os
import sys
import xml.etree.ElementTree as ET
from urllib.request import Request, build_opener, HTTPDigestAuthHandler, HTTPPasswordMgrWithDefaultRealm, HTTPRedirectHandler, ProxyHandler
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def main():
    parser = argparse.ArgumentParser(description='Read camera identity and PTZ capabilities. No movement commands.')
    parser.add_argument('--url', required=True, help='Example: http://192.168.1.10')
    parser.add_argument('--user', required=True)
    parser.add_argument('--channel', type=int, default=1)
    parser.add_argument('--out', default='camera-probe')
    args = parser.parse_args()
    parsed = urlsplit(args.url)
    if (parsed.scheme not in ('http', 'https') or not parsed.hostname or
            parsed.username or parsed.password or parsed.path not in ('', '/') or
            parsed.query or parsed.fragment or args.channel < 1):
        parser.error('Use an http(s) origin without credentials/path, and a positive channel.')
    base = args.url.rstrip('/')
    password = getpass.getpass('Camera password (not saved): ')
    manager = HTTPPasswordMgrWithDefaultRealm()
    manager.add_password(None, base + '/', args.user, password)
    opener = build_opener(ProxyHandler({}), NoRedirect(), HTTPDigestAuthHandler(manager))
    os.makedirs(args.out, exist_ok=True)
    endpoints = [
        ('device-info', '/ISAPI/System/deviceInfo'),
        ('ptz-channels', '/ISAPI/PTZCtrl/channels'),
        ('ptz-capabilities', '/ISAPI/PTZCtrl/channels/{}/capabilities'.format(args.channel)),
        ('ptz-status', '/ISAPI/PTZCtrl/channels/{}/status'.format(args.channel)),
    ]
    failures = 0
    for name, path in endpoints:
        try:
            with opener.open(Request(base + path, headers={'Accept': 'application/xml'}), timeout=5) as response:
                body = response.read(1024 * 1024 + 1)
            if len(body) > 1024 * 1024:
                raise ValueError('Response exceeds size limit')
            root = ET.fromstring(body)
            if root.tag.split('}')[-1] == 'ResponseStatus':
                fields = {node.tag.split('}')[-1]: node.text for node in root}
                if fields.get('statusCode') != '1':
                    raise ValueError('Device returned a non-success ResponseStatus')
            # Remove serial number, MAC and device UUID before writing diagnostics.
            for node in root.iter():
                if node.tag.split('}')[-1].lower() in ('serialnumber', 'macaddress', 'deviceid'):
                    node.text = '[redacted]'
            target = os.path.join(args.out, name + '.xml')
            ET.ElementTree(root).write(target, encoding='utf-8', xml_declaration=True)
            print('OK: {} -> {}'.format(path, target))
        except HTTPError as exc:
            failures += 1
            print('HTTP {}: {}'.format(exc.code, path))
            if exc.code in (401, 403):
                print('Check credentials/permissions/ISAPI authentication; stopping to avoid repeated login failures.')
                break
        except (URLError, OSError, ValueError, ET.ParseError) as exc:
            failures += 1
            print('Failed {}: {}'.format(path, type(exc).__name__))
    print('No PTZ movement or device configuration changes were sent.')
    return 1 if failures else 0


if __name__ == '__main__':
    sys.exit(main())
