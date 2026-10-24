#!/usr/bin/env python3
"""Publish a failed or limited capture to the review console, without motion."""
import argparse
import json
from pathlib import Path
import sys
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'src/inspection_console'))
from store import Store
from teaching_history import import_failure,import_partial_capture


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--data',required=True)
    p.add_argument('--pilot-directory',required=True)
    p.add_argument('--revision',required=True,type=int)
    p.add_argument('--partial-capture',action='store_true',help='Import an uncommissioned limited capture')
    args=p.parse_args()
    importer=import_partial_capture if args.partial_capture else import_failure
    print(json.dumps(importer(Store(args.data),args.pilot_directory,args.revision)))


if __name__=='__main__':main()
