#!/usr/bin/env python3
"""Import one new finite round; never enables periodic capture or verifies poses."""
import argparse
import json
from pathlib import Path
import sys

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'src/inspection_console'))
from capture_round_history import import_round
from store import Store


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--data',required=True);parser.add_argument('--directory',required=True)
    parser.add_argument('--revision',required=True,type=int);args=parser.parse_args()
    print(json.dumps(import_round(Store(args.data),args.directory,args.revision)))
