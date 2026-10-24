#!/usr/bin/env python3
"""Read saved finite-trial evidence and emit statistics; no hardware access."""
import argparse
import json
from pathlib import Path
import sys

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'src/inspection_console'))
from capture_series import read_records,summarize


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('directory')
    parser.add_argument('--output',required=True);args=parser.parse_args()
    series,records=read_records(args.directory);summary=summarize(series,records)
    with Path(args.output).open('x',encoding='utf-8') as stream:json.dump(summary,stream,ensure_ascii=False,indent=2)
    print(json.dumps(summary))
