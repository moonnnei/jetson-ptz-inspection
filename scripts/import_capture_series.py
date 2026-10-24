#!/usr/bin/env python3
"""Import a finite repeatability trial as one registry change."""
import argparse
import json
from pathlib import Path
import sys

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'src/inspection_console'))
from capture_series_history import import_series
from store import Store


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--data',required=True);parser.add_argument('--directory',required=True)
    parser.add_argument('--revision',type=int,required=True);args=parser.parse_args()
    print(json.dumps(import_series(Store(args.data),args.directory,args.revision)))
