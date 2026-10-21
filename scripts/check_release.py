#!/usr/bin/env python3
"""Audit the explicit public file list without network or hardware access."""
import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import subprocess
import sys
from urllib.parse import unquote, urlsplit

ROOT = Path(__file__).resolve().parents[1]
PRIVATE_PARTS = {'.git', '.codex', '.agents', '.aws', '.ssh', '.venv',
                 '__pycache__', 'build', 'var', 'runs', 'tmp'}
PRIVATE_SUFFIXES = ('.local.json', '.log', '.jsonl', '.pid', '.jpg', '.jpeg',
                    '.mp4', '.pem', '.key', '.pfx', '.p12', '.pyc', '.zip',
                    '.pt', '.pth', '.ckpt', '.onnx', '.engine', '.sqlite', '.sqlite3', '.db')
PATTERNS = (
    ('private-key', rb'-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----'),
    ('github-token', rb'(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{40,})'),
    ('aws-access-key', rb'AKIA[0-9A-Z]{16}'),
    ('personal-windows-home', rb'[A-Za-z]:[\\/]Users[\\/][^\\/\s]+'),
)


def public_name(name):
    if not isinstance(name, str) or '\\' in name or ':' in name:
        raise ValueError('Invalid public path')
    path = PurePosixPath(name)
    if (not name or path.is_absolute() or any(p in ('', '.', '..') for p in name.split('/'))
            or any(p.lower() in PRIVATE_PARTS for p in path.parts)
            or name.lower().endswith(PRIVATE_SUFFIXES)
            or any(p.lower().startswith('.env') for p in path.parts)):
        raise ValueError('Private or invalid public path: ' + name)
    if name.lower().endswith('.png') and not name.startswith('docs/assets/'):
        raise ValueError('Only reviewed documentation figures may be listed: ' + name)
    return name


def _repository_files(root):
    if (root / '.git').exists():
        output = subprocess.check_output(['git', '-C', str(root), 'ls-files', '-z'])
        return {p for p in output.decode('utf-8').split('\0') if p}
    found = set()
    for directory, children, names in os.walk(str(root)):
        children[:] = [n for n in children if n.lower() not in PRIVATE_PARTS]
        for name in names:
            if name.endswith(('.pyc', '.pyo')):
                continue
            found.add((Path(directory) / name).relative_to(root).as_posix())
    return found


def audit(root, verify_manifest=False):
    root = Path(root).resolve()
    errors = []
    manifest = json.loads((root / 'FILE_MANIFEST.json').read_text(encoding='utf-8'))
    records = manifest['files']
    names = [entry['path'] for entry in records]
    if len(names) != len(set(names)):
        errors.append('Duplicate paths in FILE_MANIFEST.json')
    for entry in records:
        name = entry['path']
        try:
            public_name(name)
            target = root / name
            if target.is_symlink() or root not in target.resolve().parents:
                raise ValueError('Public file is a link or escapes project: ' + name)
            data = target.read_bytes()
            if verify_manifest and (len(data) != entry['bytes'] or
                    hashlib.sha256(data).hexdigest() != entry['sha256']):
                errors.append('Manifest mismatch: ' + name)
            if target.suffix.lower() not in ('.bin', '.png'):
                for label, pattern in PATTERNS:
                    if re.search(pattern, data):
                        # Never print the matching secret or an entire line.
                        errors.append('Possible ' + label + ' in ' + name)
        except (ValueError, OSError) as exc:
            errors.append(str(exc))
    expected = set(names) | {'FILE_MANIFEST.json'}
    if verify_manifest:
        actual = _repository_files(root)
        for name in sorted(actual - expected):
            errors.append('Unlisted public/tracked file: ' + name)
        for name in sorted(expected - actual):
            errors.append('Listed file absent from public tree/index: ' + name)

    license_path = root / 'LICENSE'
    if not license_path.is_file() or 'GNU AFFERO GENERAL PUBLIC LICENSE' not in license_path.read_text(encoding='utf-8'):
        errors.append('Missing root AGPL-3.0 LICENSE')
    for required in ('README.md', 'CONTRIBUTING.md', 'SECURITY.md', 'THIRD_PARTY_NOTICES.md',
                     '.github/workflows/ci.yml', 'docs/PUBLISHING.md'):
        if required not in names:
            errors.append('Missing release document: ' + required)

    for path in sorted((root / 'config').glob('*.example.json')):
        config = json.loads(path.read_text(encoding='utf-8'))
        if config.get('user') != 'YOUR_CAMERA_USER':
            errors.append('Example username is not a placeholder: ' + path.name)
        if any(k.lower() in ('password', 'passwd', 'secret', 'token') for k in config):
            errors.append('Credential field in example config: ' + path.name)
        for key in ('camera', 'rtsp_url'):
            url = config.get(key)
            if url and (urlsplit(url).username is not None or urlsplit(url).password is not None):
                errors.append('Credentials in example URL: ' + path.name)

    for name in names:
        if not name.endswith('.md') or not (root / name).is_file():
            continue
        text = (root / name).read_text(encoding='utf-8')
        text = re.sub(r'```.*?```', '', text, flags=re.S)
        for raw in re.findall(r'!?\[[^\]]*\]\(([^)]+)\)', text):
            link = raw.split(' "', 1)[0].strip('<>')
            if not link or link.startswith('#') or urlsplit(link).scheme:
                continue
            relative = unquote(link.split('#', 1)[0])
            target = (root / relative.lstrip('/')) if relative.startswith('/') else (root / name).parent / relative
            if not target.exists():
                errors.append('Broken local Markdown link in ' + name + ': ' + relative)
    return errors


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--verify-manifest', action='store_true', help='Check hashes and the tracked/clean file set')
    args = parser.parse_args()
    errors = audit(ROOT, verify_manifest=args.verify_manifest)
    if errors:
        for message in errors:
            print('ERROR: ' + message, file=sys.stderr)
        return 1
    print('Public release audit: OK. No network, credentials or hardware requested.')
    print('Pattern checks do not replace owner review of rights, photos and Git history.')
    return 0


if __name__ == '__main__':
    sys.exit(main())
