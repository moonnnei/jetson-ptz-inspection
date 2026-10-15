#!/usr/bin/env python3
"""Run non-hardware checks; requires only Python 3.5+ standard library."""
import ast
import subprocess
import sys
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
def main():
    for folder in ('src','scripts','tools'):
        for path in (ROOT/folder).rglob('*.py'):
            if '__pycache__' in path.parts or 'work' in path.parts:continue
            # Export/validation tools run on modern PC Python, others on TX2 3.5.
            kwargs={'feature_version':(3,5)} if sys.version_info>=(3,8) and 'model_export' not in path.parts else {}
            ast.parse(path.read_text(encoding='utf-8'),filename=str(path),**kwargs)
    print('Python syntax: OK',flush=True)
    subprocess.check_call([sys.executable,str(ROOT/'scripts/verify_models.py')])
    subprocess.check_call([sys.executable,'-m','unittest','discover','-s',str(ROOT/'src/person_tracker'),'-p','test_*.py','-v'])
    subprocess.check_call([sys.executable,'-m','unittest','discover','-s',str(ROOT/'src/inspection_demo'),'-p','test_*.py','-v'])
    subprocess.check_call([sys.executable,'-m','unittest','discover','-s',str(ROOT/'scripts'),'-p','test_release.py','-v'])
    print('Local checks passed. No camera connected or moved.',flush=True)
if __name__=='__main__':main()
