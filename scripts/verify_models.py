#!/usr/bin/env python3
"""Check packaged model bytes; no network or device access."""
import hashlib
import json
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
def main():
    folder=ROOT/'models/yolo26n'
    manifest=json.loads((folder/'SHA256.json').read_text())
    for name,expected in sorted(manifest.items()):
        actual=hashlib.sha256((folder/name).read_bytes()).hexdigest()
        if actual!=expected:raise RuntimeError('SHA256 mismatch: '+name)
        print('OK SHA256 '+name)
if __name__=='__main__':main()
