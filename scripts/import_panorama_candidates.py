#!/usr/bin/env python3
"""Attach saved panorama-first proposals to an existing private review map."""
import argparse
import json
from pathlib import Path
import sys

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'src/inspection_console'))
from store import Store


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('payload');parser.add_argument('--data',default=str(ROOT/'var/inspection-console'))
    parser.add_argument('--expected-revision',type=int)
    args=parser.parse_args()
    payload=json.loads(Path(args.payload).read_text(encoding='utf-8'))
    store=Store(args.data)
    revision=store.snapshot()['revision'] if args.expected_revision is None else args.expected_revision
    result=store.import_panorama_candidates(revision,payload)
    print(json.dumps(result,ensure_ascii=True))


if __name__=='__main__':main()
