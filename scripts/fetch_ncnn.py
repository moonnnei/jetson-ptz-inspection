#!/usr/bin/env python3
"""Fetch the pinned NCNN source, or safely unpack the original verified archive."""
import argparse
import hashlib
import io
from pathlib import Path
import shutil
import zipfile
from urllib.request import urlopen
ROOT=Path(__file__).resolve().parents[1]
COMMIT='6c52e59dfb610cbd77946bea19099aaf96f8dcc1'
URL='https://codeload.github.com/Tencent/ncnn/zip/'+COMMIT
ORIGINAL_SHA256='9f294a691b7921c2771a362e40cf27caf5d855b088106fa636fbd911abedc053'
def main():
    p=argparse.ArgumentParser();p.add_argument('--archive',help='Original ncnn-source.zip, SHA256 verified')
    args=p.parse_args();target=ROOT/'third_party/ncnn-master'
    if target.exists():raise SystemExit('Source already exists; not overwriting '+str(target))
    if args.archive:
        data=Path(args.archive).read_bytes()
        if hashlib.sha256(data).hexdigest()!=ORIGINAL_SHA256:raise SystemExit('Original archive SHA256 mismatch')
    else:
        print('Downloading fixed NCNN commit '+COMMIT)
        with urlopen(URL,timeout=60) as r:data=r.read(64*1024*1024+1)
        if len(data)>64*1024*1024:raise RuntimeError('Source archive exceeds limit')
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        names=z.namelist();tops=set(name.split('/')[0] for name in names)
        expected='ncnn-master' if args.archive else 'ncnn-'+COMMIT
        if tops!={expected}:raise RuntimeError('Unexpected archive root')
        for entry in z.infolist():
            parts=Path(entry.filename).parts
            if entry.filename.startswith(('/','\\')) or '..' in parts or ':' in entry.filename:
                raise RuntimeError('Unsafe archive path')
            if ((entry.external_attr>>16)&0o170000)==0o120000:raise RuntimeError('Archive contains symlink')
        target.mkdir(parents=True)
        for entry in z.infolist():
            rel=Path(*Path(entry.filename).parts[1:]);out=target/rel
            if entry.filename.endswith('/'):out.mkdir(parents=True,exist_ok=True)
            else:
                out.parent.mkdir(parents=True,exist_ok=True)
                with z.open(entry) as src,out.open('wb') as dst:shutil.copyfileobj(src,dst)
    (ROOT/'third_party/ncnn-source-revision.txt').write_text('commit='+COMMIT+'\narchive_sha256='+hashlib.sha256(data).hexdigest()+'\n')
    print('Source ready: '+str(target))
if __name__=='__main__':main()
