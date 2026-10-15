"""Bounded local smoke test of the tracker API; no camera credentials."""
import argparse,base64,json,re,time
from pathlib import Path
from urllib.request import Request,build_opener,ProxyHandler
urlopen=build_opener(ProxyHandler({})).open

BASE='http://192.168.1.4:8767'
ROOT=Path(__file__).resolve().parents[1]/'var/checks'
def get(path):
    with urlopen(BASE+path,timeout=5) as r:return r.read()
def get_json(path):
    # Python 3.5 json.loads accepts text, not the bytes returned by HTTP.
    return json.loads(get(path).decode('utf-8'))
def main():
    global BASE
    p=argparse.ArgumentParser();p.add_argument('action',choices=['status','capture','calibrate','track','stop','lease-test'])
    p.add_argument('--url',default=BASE);args=p.parse_args();BASE=args.url.rstrip('/');ROOT.mkdir(parents=True,exist_ok=True);token=re.search(r"const token='([a-f0-9]+)'",get('/').decode()).group(1)
    def post(action):
        with urlopen(Request(BASE+'/api/'+action,data=b'',headers={'X-Control-Token':token},method='POST'),timeout=5) as r:return r.read()
    if args.action in ('status','capture'):
        s=get_json('/api/frame' if args.action=='capture' else '/api/status')
        if args.action=='capture' and s.get('jpeg'):
            (ROOT/'live.jpg').write_bytes(base64.b64decode(s.pop('jpeg')))
        print(json.dumps(s,ensure_ascii=False,indent=2));return
    if args.action=='stop':post('stop');return
    if args.action=='lease-test':
        try:
            post('start');time.sleep(4.5)
            s=get_json('/api/status')
            assert not s['enabled'], 'Lease expiry did not disable motion'
            print('Lease-expiry stop verified')
        finally:post('stop')
        return
    samples=[]
    try:
        post('calibrate' if args.action=='calibrate' else 'start')
        deadline=time.monotonic()+(45 if args.action=='calibrate' else 10)
        while time.monotonic()<deadline:
            post('heartbeat');s=get_json('/api/status');samples.append(s)
            print(json.dumps({k:s.get(k) for k in ['mode','message','target','error','pipeline_ms']},ensure_ascii=False),flush=True)
            if args.action=='calibrate' and not s['calibrating']:
                assert s['calibrated'],s['error'];break
            time.sleep(.5)
        if args.action=='calibrate':
            s=get_json('/api/status')
            assert s['calibrated'] and not s['calibrating'],'Calibration did not finish before deadline'
    finally:
        post('stop')
        (ROOT/(args.action+'-samples.json')).write_text(json.dumps(samples,ensure_ascii=False,indent=2),encoding='utf-8')
if __name__=='__main__':main()
