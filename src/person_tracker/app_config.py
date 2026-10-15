"""Package paths and non-secret settings; compatible with Python 3.5."""
import argparse
import json
import os
from pathlib import Path
from urllib.parse import urlsplit
from rtsp_capture import validate_cpu_list
PROJECT_ROOT=Path(__file__).resolve().parents[2]
DEFAULTS=dict(camera='http://192.168.1.64',user=None,host='0.0.0.0',port=8767,rotation=180,
              model_dir='models/yolo26n',detector='build/detector',runtime_dir='var/tracker',
              frame_source='snapshot',rtsp_url=None,rtsp_decoder='software',rtsp_fps=10,
              rtsp_width=None,rtsp_height=None,detector_threads=4,opencv_threads=1,
              detector_cpus=None,rtsp_cpus=None,frame_policy='latest',control_mode='fixed',loop_interval=0.02,
              motion_mode='serial',tracking_pulse_ms=200,motion_speed=20,preview_interval=0,max_control_age=0.5)
def parse_args(argv=None):
    p=argparse.ArgumentParser(description='Person tracking prototype; starts in observe mode.')
    p.add_argument('--config',help='JSON file; no passwords. Relative paths inside are project-relative.')
    for name in ('camera','user','host','model-dir','detector','runtime-dir','rtsp-url','detector-cpus','rtsp-cpus'):p.add_argument('--'+name)
    p.add_argument('--frame-source',choices=('snapshot','rtsp'))
    p.add_argument('--rtsp-decoder',choices=('software','hardware'))
    p.add_argument('--frame-policy',choices=('latest','fresh'))
    p.add_argument('--control-mode',choices=('fixed','adaptive'))
    p.add_argument('--motion-mode',choices=('serial','responsive'))
    p.add_argument('--tracking-pulse-ms',type=int)
    p.add_argument('--motion-speed',type=int)
    p.add_argument('--preview-interval',type=float)
    p.add_argument('--max-control-age',type=float)
    p.add_argument('--loop-interval',type=float)
    for name in ('rtsp-fps','rtsp-width','rtsp-height','detector-threads','opencv-threads'):p.add_argument('--'+name,type=int)
    p.add_argument('--port',type=int);p.add_argument('--rotation',type=int,choices=(0,90,180,270))
    p.add_argument('--daemon',action='store_true')
    args=p.parse_args(argv);settings=dict(DEFAULTS)
    if args.config:
        try:data=json.loads(Path(args.config).read_text(encoding='utf-8'))
        except (OSError,ValueError) as e:p.error('Cannot read config: '+str(e))
        if not isinstance(data,dict) or set(data)-set(DEFAULTS):p.error('Unknown config keys; password fields are not supported')
        settings.update(data)
    for name in DEFAULTS:
        value=getattr(args,name)
        if value is not None:settings[name]=value
    if not isinstance(settings['user'],str) or not settings['user'] or settings['user']=='YOUR_CAMERA_USER':
        p.error('Set your camera username with --user or config; password is requested interactively')
    parsed=urlsplit(settings['camera'])
    if (parsed.scheme not in ('http','https') or not parsed.hostname or parsed.username or parsed.password
        or parsed.path not in ('','/') or parsed.query or parsed.fragment):p.error('Camera must be an http(s) origin without credentials')
    if not isinstance(settings['port'],int) or not 1<=settings['port']<=65535:p.error('Invalid port')
    if settings['rotation'] not in (0,90,180,270):p.error('Invalid rotation')
    if settings['frame_source'] not in ('snapshot','rtsp'):p.error('Invalid frame source')
    if settings['rtsp_decoder'] not in ('software','hardware'):p.error('Invalid RTSP decoder')
    if type(settings['rtsp_fps']) is not int or not 1<=settings['rtsp_fps']<=30:p.error('Invalid RTSP fps')
    dims=(settings['rtsp_width'],settings['rtsp_height'])
    if dims!=(None,None) and any(type(v) is not int or not 16<=v<=4096 for v in dims):
        p.error('RTSP width and height must both be set, between 16 and 4096')
    if settings['rtsp_url'] is not None:
        try:
            stream=urlsplit(settings['rtsp_url']);stream_port=stream.port
            valid=(stream.scheme=='rtsp' and stream.hostname and not stream.username and not stream.password
                   and not stream.query and not stream.fragment and stream.path.startswith('/')
                   and (stream_port is None or 1<=stream_port<=65535))
        except (ValueError,TypeError):valid=False
        if not valid:p.error('RTSP URL must have no credentials, query, or fragment')
    if settings['frame_source']=='rtsp' and not settings['rtsp_url']:
        p.error('RTSP mode requires --rtsp-url with the actual camera RTSP port')
    for name in ('detector_threads','opencv_threads'):
        if type(settings[name]) is not int or not 1<=settings[name]<=6:p.error('Invalid '+name)
    if settings['frame_policy'] not in ('latest','fresh'):p.error('Invalid frame policy')
    if settings['control_mode'] not in ('fixed','adaptive'):p.error('Invalid control mode')
    if settings['motion_mode'] not in ('serial','responsive'):p.error('Invalid motion mode')
    if type(settings['tracking_pulse_ms']) is not int or not 100<=settings['tracking_pulse_ms']<=500:
        p.error('Invalid tracking pulse limit')
    if type(settings['motion_speed']) is not int or not 20<=settings['motion_speed']<=40:p.error('Invalid calibrated motion speed')
    for key,low,high in (('preview_interval',0,1),('max_control_age',0.15,1.5)):
        if type(settings[key]) not in (float,int) or not low<=settings[key]<=high:p.error('Invalid '+key)
    if not isinstance(settings['loop_interval'],(int,float)) or not 0.01<=settings['loop_interval']<=0.5:
        p.error('Invalid loop interval')
    if settings['detector_cpus'] is not None:
        try:
            parts=settings['detector_cpus'].split(',')
            cpus=[int(part) for part in parts]
            valid=bool(cpus) and len(set(cpus))==len(cpus) and all(0<=v<=127 for v in cpus)
        except (ValueError,AttributeError):valid=False
        if not valid:p.error('detector_cpus must be comma-separated CPU numbers')
    try:settings['rtsp_cpus']=validate_cpu_list(settings['rtsp_cpus'])
    except ValueError:p.error('rtsp_cpus must be unique comma-separated CPU numbers (0..127)')
    for name in ('model_dir','detector','runtime_dir'):
        value=Path(settings[name]).expanduser()
        # Python 3.5 Path.resolve() requires the final path to exist.
        # Build/output paths are allowed to be created later.
        settings[name]=Path(os.path.abspath(str(value if value.is_absolute() else PROJECT_ROOT/value)))
    for name,value in settings.items():setattr(args,name,value)
    return args
