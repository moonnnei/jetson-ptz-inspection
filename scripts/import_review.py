#!/usr/bin/env python3
"""Import original images and saved detections into the private review database."""
import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src' / 'inspection_console'))
from store import Store


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('manifest', help='JSON with title and frames; see REVIEW_CONSOLE.md')
    parser.add_argument('--data', default=str(ROOT / 'var' / 'inspection-console'))
    args = parser.parse_args()
    source = Path(args.manifest).resolve()
    manifest = json.loads(source.read_text(encoding='utf-8'))
    for frame in manifest['frames']:
        path = Path(frame['path'])
        frame['path'] = str(path if path.is_absolute() else source.parent / path)
        if frame.get('raw_source_path'):
            path=Path(frame['raw_source_path'])
            frame['raw_source_path']=str(path if path.is_absolute() else source.parent/path)
    if manifest.get('overview'):
        path=Path(manifest['overview']['path'])
        manifest['overview']['path']=str(path if path.is_absolute() else source.parent/path)
    result = Store(args.data).import_manifest(manifest)
    print(json.dumps(result, ensure_ascii=True))


if __name__ == '__main__':
    main()
