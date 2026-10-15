#!/usr/bin/env python3
"""Refresh public file hashes; new files must be explicitly named with --add."""
import argparse
import hashlib
import json
from pathlib import Path
from check_release import public_name

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--add', nargs='*', default=[], metavar='PUBLIC_PATH')
    args = parser.parse_args()
    path = ROOT / 'FILE_MANIFEST.json'
    manifest = json.loads(path.read_text(encoding='utf-8'))
    names = {item['path'] for item in manifest['files']} | set(args.add)
    names.discard('FILE_MANIFEST.json')
    files = []
    for name in sorted(names):
        public_name(name)
        target = ROOT / name
        if target.is_symlink() or ROOT not in target.resolve().parents:
            raise ValueError('Refusing public symlink or escaped path: ' + name)
        data = target.read_bytes()
        files.append(dict(path=name, bytes=len(data), sha256=hashlib.sha256(data).hexdigest()))
    manifest.update(license='AGPL-3.0', files=files)
    path.write_bytes((json.dumps(manifest, ensure_ascii=False, indent=2) + '\n').encode('utf-8'))
    print('Updated public manifest:', len(files), 'files; historical experiment metadata preserved.')


if __name__ == '__main__':
    main()
