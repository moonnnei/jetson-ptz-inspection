#!/usr/bin/env python3
"""Reproduce the pinned Annotorious/Pixi no-new-Function browser bundle.

Annotorious's UMD embeds Pixi; a second global PIXI patch would affect the
wrong instance. Apply the official 7.4.3 static uniform implementation to the
embedded ShaderSystem before the annotator can instantiate any renderer.
This changes no browser CSP or security settings. Run on the PC, not TX2.
"""
import base64
import hashlib
import io
import json
from pathlib import Path
import re
import tarfile
from urllib.request import urlopen

ROOT=Path(__file__).resolve().parents[2]
OUT=ROOT/'src/inspection_console/vendor'
ORIGINAL='4ea2fd48e94d3a88f23feddb40957619ce42f9727d38c5427b70f1a2d85bf0da'


def package(name,version):
    with urlopen('https://registry.npmjs.org/'+name+'/'+version,timeout=20) as response:
        meta=json.loads(response.read().decode())
    with urlopen(meta['dist']['tarball'],timeout=30) as response:raw=response.read()
    integrity='sha512-'+base64.b64encode(hashlib.sha512(raw).digest()).decode()
    if integrity!=meta['dist']['integrity']:raise ValueError('npm integrity mismatch')
    archive=tarfile.open(fileobj=io.BytesIO(raw),mode='r:gz')
    return meta,archive


def main():
    meta,archive=package('@annotorious/openseadragon','3.9.5')
    original=archive.extractfile('package/dist/annotorious-openseadragon.js').read()
    if hashlib.sha256(original).hexdigest()!=ORIGINAL:raise ValueError('unexpected upstream bundle')
    poly,pkg=package('@pixi/unsafe-eval','7.4.3')
    uniform=pkg.extractfile('package/lib/syncUniforms.mjs').read().decode()
    install=pkg.extractfile('package/lib/install.mjs').read().decode()
    uniform=re.sub(r'export\s*\{\s*syncUniforms\s*\};','',uniform)
    uniform=re.sub(r'//# sourceMappingURL=.*','',uniform)
    install=install[install.index('function selfInstall()'):]
    install=re.sub(r'export\s*\{\s*install\s*\};','',install)
    install=re.sub(r'//# sourceMappingURL=.*','',install)
    # These symbols are assertions against the pinned original, not a guess
    # about arbitrary upstream versions.
    source=original.decode()
    if source.count('class lm{constructor(t){this.destroyed=!1,this.renderer=t,this.systemCheck()')!=1:
        raise ValueError('embedded Pixi ShaderSystem binding differs')
    if source.count('Kt.ShapeType=yt,')!=1:raise ValueError('UMD export boundary differs')
    install=install.replace('ShaderSystem.prototype','lm.prototype')
    block=('\n/* INSPECTION_CSP_PATCH_BEGIN: official @pixi/unsafe-eval 7.4.3 (MIT). */\n'
        '(function(){\n'+uniform+'\n'+install+'\n})();\n'
        'Kt.inspectionCspSafe=true;\n/* INSPECTION_CSP_PATCH_END */\n')
    if 'new Function' in block or re.search(r'\beval\s*\(',block):raise ValueError('patch is not static')
    result=source.replace('Kt.ShapeType=yt,',block+'Kt.ShapeType=yt,').encode()
    OUT.mkdir(exist_ok=True);(OUT/'annotorious-openseadragon.js').write_bytes(result)
    with urlopen('https://raw.githubusercontent.com/pixijs/pixijs/v7.4.3/LICENSE',timeout=20) as response:license_text=response.read()
    if b'MIT' not in license_text or b'Permission is hereby granted' not in license_text:
        raise ValueError('unexpected Pixi license')
    (OUT/'pixi-unsafe-eval-LICENSE.txt').write_bytes(license_text)
    path=OUT/'MANIFEST.json';manifest=json.loads(path.read_text(encoding='utf8'))
    for row in manifest:
        if row['file']=='annotorious-openseadragon.js':
            row.update(upstream_sha256=ORIGINAL,sha256=hashlib.sha256(result).hexdigest(),bytes=len(result),
                modified=True,patch='tools/web_ui/build_csp_vendor.py',
                compatibility_package=dict(name='@pixi/unsafe-eval',version='7.4.3',license='MIT',
                    source=poly['dist']['tarball'],integrity=poly['dist']['integrity']))
    manifest=[r for r in manifest if r['file']!='pixi-unsafe-eval-LICENSE.txt']
    manifest.append(dict(file='pixi-unsafe-eval-LICENSE.txt',package='@pixi/unsafe-eval',version='7.4.3',license='MIT',
        source='https://raw.githubusercontent.com/pixijs/pixijs/v7.4.3/LICENSE',
        sha256=hashlib.sha256(license_text).hexdigest(),bytes=len(license_text)))
    path.write_text(json.dumps(manifest,indent=2)+'\n',encoding='utf8')
    print(json.dumps(dict(status='built',sha256=hashlib.sha256(result).hexdigest(),bytes=len(result))))


if __name__=='__main__':main()
