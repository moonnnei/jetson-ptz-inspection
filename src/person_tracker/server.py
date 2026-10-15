#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Hikvision JPEG/RTSP latest frame -> YOLO26n -> bounded PTZ pulse."""
import argparse
import base64
import getpass
import json
import logging
import math
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import select
import signal
import struct
import subprocess
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, HTTPServer
from socketserver import ThreadingMixIn
from urllib.error import HTTPError
from urllib.request import Request
from ptz_control import Camera
from tracking import Target, correction, validate_matrix
from app_config import PROJECT_ROOT, parse_args
from rtsp_capture import RTSPCapture, CaptureError
from live_preview import LivePreview

ROOT=Path(__file__).resolve().parent
QUIT=threading.Event()

class EngineError(RuntimeError):pass

class FastCamera(Camera):
    def request(self,path,body=None):
        req=Request(self.url+path,data=body,method='PUT' if body is not None else 'GET',
                    headers={'Content-Type':'application/xml','Cache-Control':'no-cache'})
        with self.opener.open(req,timeout=1.5) as r:
            data=r.read(12000001)
        if len(data)>12000000:raise ValueError('Camera response too large')
        return data

class Engine:
    def __init__(self,model,rotation=180,detector=None,threads=4,opencv_threads=None,detector_cpus=None):
        detector=detector or PROJECT_ROOT/'build/detector'
        command=[str(detector),str(model/'model.ncnn.param'),str(model/'model.ncnn.bin'),str(rotation),str(threads)]
        if opencv_threads is not None:command.append(str(opencv_threads))
        if detector_cpus:command=['taskset','-c',detector_cpus]+command
        self.p=subprocess.Popen(command,
                                stdin=subprocess.PIPE,stdout=subprocess.PIPE,bufsize=0)
        # Linux/Python 3.5+: select() alone cannot bound a large blocking write.
        # Keep the pipe nonblocking so a stalled detector cannot hold operation forever.
        try:os.set_blocking(self.p.stdin.fileno(),False)
        except Exception:
            self.close()
            raise
    def write(self,data):
        view=memoryview(data);deadline=time.monotonic()+3
        while view:
            remaining=deadline-time.monotonic()
            if remaining<=0 or not select.select([],[self.p.stdin],[],remaining)[1]:
                raise RuntimeError('Detector input timeout')
            try:n=os.write(self.p.stdin.fileno(),view)
            except BlockingIOError:continue
            if not n:raise RuntimeError('Detector pipe closed')
            view=view[n:]
    def read(self,n):
        result=bytearray();deadline=time.monotonic()+3
        while len(result)<n:
            remaining=deadline-time.monotonic()
            if remaining<=0 or not select.select([self.p.stdout],[],[],remaining)[0]:raise RuntimeError('Detector timeout')
            data=os.read(self.p.stdout.fileno(),n-len(result))
            if not data:raise RuntimeError('Detector exited')
            result.extend(data)
        return bytes(result)
    def infer(self,jpeg,include_flow=True,include_preview=True,hold_reference=False):
        size=len(jpeg)
        if size>12000000:raise ValueError('Detector input too large')
        if hold_reference and not include_flow:raise ValueError('Held reference requires flow')
        # Bits 31/30/29 opt out of flow/preview or hold a flow reference.
        flags=(0 if include_flow else 0x80000000)|(0 if include_preview else 0x40000000)|(0x20000000 if hold_reference else 0)
        data=struct.pack('<I',size|flags)+jpeg
        self.write(data)
        size=struct.unpack('<I',self.read(4))[0]
        if size>1000000:raise RuntimeError('Invalid detector header')
        result=json.loads(self.read(size).decode())
        if hold_reference and result.get('flow_reference_held') is not True:
            raise RuntimeError('Detector does not support held flow reference')
        size=struct.unpack('<I',self.read(4))[0]
        if size>12000000:raise RuntimeError('Invalid detector JPEG')
        if include_preview and not size:raise RuntimeError('Detector omitted requested preview')
        if not include_preview and (size or result.get('preview_generated') is not False):
            raise RuntimeError('Detector preview flag mismatch')
        return result,self.read(size) if size else b''
    def close(self):
        if self.p.poll() is None:
            self.p.terminate()
            try:self.p.wait(timeout=2)
            except subprocess.TimeoutExpired:self.p.kill();self.p.wait()

class App:
    def __init__(self,url,user,password,model,rotation=180,detector=None,runtime_dir=None,
                 frame_source='snapshot',rtsp_url=None,rtsp_decoder='software',
                 rtsp_fps=10,rtsp_width=None,rtsp_height=None,detector_threads=4,
                 opencv_threads=1,detector_cpus=None,frame_policy='latest',
                 control_mode='fixed',loop_interval=0.02,rtsp_cpus=None,
                 motion_mode='serial',tracking_pulse_ms=200,preview_interval=0,max_control_age=0.5,motion_speed=20):
        if type(motion_speed) is not int or not 20<=motion_speed<=40:raise ValueError('Invalid calibrated motion speed')
        self.camera=FastCamera(url,user,password)
        self.motion=FastCamera(url,user,password)
        self.motion.stop()  # Verify authentication and STOP before starting workers.
        self.engine=Engine(model,rotation,detector,detector_threads,opencv_threads,detector_cpus)
        self.runtime_dir=runtime_dir or PROJECT_ROOT/'var/tracker'
        self.runtime_dir.mkdir(parents=True,exist_ok=True)
        self.lock=threading.RLock();self.command=threading.Lock();self.operation=threading.Lock()
        self.enabled=False;self.calibrating=False;self.generation=0;self.lease=0;self.stop_error=None
        self.token=uuid.uuid4().hex;self.matrix=None;self.target=Target();self.calibration_error=None
        self.last_frame=0;self.last_request=0;self.jpeg=None;self.frames=0
        self.jpeg_b64=None;self.preview_state=None;self.last_preview=0;self.preview_received=0
        self.preview_requested_until=0
        self.motion_thread=None;self.motion_context=None
        self.motion_mode=motion_mode;self.tracking_pulse_ms=tracking_pulse_ms;self.motion_speed=motion_speed
        self.preview_interval=preview_interval;self.max_control_age=max_control_age
        self.frame_source=frame_source;self.video_source=None
        self.frame_policy=frame_policy;self.source_barrier=0
        self.control_mode=control_mode;self.loop_interval=loop_interval
        self.source_sequence=0;self.source_restarts=0;self.frame_geometry=None
        self.state=dict(mode='observe',message='仅检测，云台未启动',persons=[],target=None,error=None,
                        frame_source=frame_source,frame_policy=frame_policy,control_mode=control_mode,
                        detector_threads=detector_threads,opencv_threads=opencv_threads,detector_cpus=detector_cpus,
                        rtsp_cpus=rtsp_cpus,rotation=rotation,motion_mode=motion_mode,
                        tracking_pulse_ms=tracking_pulse_ms,motion_speed=motion_speed,preview_interval=preview_interval,
                        motion_busy=False,motion_phase='idle')
        self.logger=logging.getLogger('tracker');self.logger.setLevel(logging.INFO)
        handler=RotatingFileHandler(str(self.runtime_dir/'tracker-events.jsonl'),maxBytes=2000000,backupCount=2)
        self.logger.addHandler(handler)
        try:
            if frame_source=='rtsp':
                self.video_source=RTSPCapture(rtsp_url,user,password,decoder=rtsp_decoder,
                                             fps=rtsp_fps,width=rtsp_width,height=rtsp_height,rtsp_cpus=rtsp_cpus)
                self.video_source.start()
        except Exception:
            if self.video_source:self.video_source.close()
            self.engine.close()
            raise
        self.log('startup',camera=url,frame_source=frame_source)
        self.live_preview=LivePreview(self.video_source,QUIT) if self.video_source else None
    def log(self,event,**data):
        data.update(event=event,time=time.strftime('%Y-%m-%dT%H:%M:%S%z'))
        self.logger.info(json.dumps(data,ensure_ascii=False))
    def update(self,**data):
        with self.lock:self.state.update(data)
    def confirm_stop(self):
        # Keep the result in the same order as the serialized camera commands.
        with self.command:
            try:self.motion.stop()
            except Exception as e:
                message='停止指令未确认：'+type(e).__name__
                with self.lock:
                    self.stop_error=message;self.enabled=False;self.generation+=1
                    self.state.update(error=message,mode='observe',message='停止未确认，控制已禁用')
                self.log('stop-unconfirmed',kind=type(e).__name__)
                return False
            with self.lock:
                if self.state.get('error')==self.stop_error:self.state['error']=None
                self.stop_error=None
            return True
    def stop(self,reason='用户停止'):
        with self.lock:
            if self.motion_context:self.motion_context['cancel'].set()
            self.enabled=False;self.generation+=1
            self.state.update(mode='observe',message=reason)
        confirmed=self.confirm_stop()
        if confirmed:self.log('stop',reason=reason)
        return confirmed
    def heartbeat(self):
        with self.lock:self.lease=time.monotonic()+3
    def start(self):
        with self.lock:
            if self.stop_error:raise ValueError('停止尚未确认，请再次点击停止后重试')
            if self.calibrating:raise ValueError('正在校准')
            if self.matrix is None:raise ValueError('请先校准方向')
            if self.engine.p.poll() is not None:raise ValueError('推理进程已退出，请重启服务')
            if time.monotonic()-self.last_request>1.5:raise ValueError('没有新鲜画面')
            self.target.reset();self.enabled=True;self.generation+=1;self.lease=time.monotonic()+3
            self.state.update(mode='searching',message='寻找并锁定一个人',error=None)
        self.log('start')
    def valid(self,generation,calibration=False):
        with self.lock:
            if self.video_source:
                source=self.video_source.status()
                age=source.get('last_frame_age')
                if source.get('restarts',0)!=self.source_restarts or age is None or age>1.5:return False
            return (not QUIT.is_set() and not self.stop_error and generation==self.generation and time.monotonic()<self.lease
                    and (self.calibrating if calibration else self.enabled))
    def pulse(self,axis,speed,generation,calibration=False,duration=500,cancel_event=None,
              frame_received_at=None,processed_at=None):
        if (axis not in ('pan','tilt') or type(speed) is not int or abs(speed)!=self.motion_speed or type(duration) is not int
            or not 100<=duration<=500 or (calibration and duration!=500)):
            raise ValueError('Invalid bounded motion')
        command_start=time.monotonic()
        try:
            with self.command:
                if (cancel_event and cancel_event.is_set()) or not self.valid(generation,calibration):return False
                issued=time.monotonic()
                if frame_received_at is not None and (issued-frame_received_at>self.max_control_age
                                                       or frame_received_at<self.source_barrier):return False
                self.motion.move(duration=duration,**{axis:speed})
                accepted=time.monotonic()
            self.log('pulse',axis=axis,speed=speed,duration_ms=duration,calibration=calibration,
                     command_ms=round((accepted-command_start)*1000,1),command_http_ms=round((accepted-issued)*1000,1),
                     result_to_command_ms=round((issued-processed_at)*1000,1) if processed_at is not None else None)
            if processed_at is not None:
                self.update(result_to_command_ms=round((issued-processed_at)*1000,1),
                            command_http_ms=round((accepted-issued)*1000,1),last_pulse_duration_ms=duration)
            until=time.monotonic()+(0.85 if calibration else duration/1000.0+0.05)
            while (time.monotonic()<until and self.valid(generation,calibration)
                   and not (cancel_event and cancel_event.is_set())):QUIT.wait(0.01)
        finally:
            if cancel_event:self.update(motion_phase='stopping')
            if not self.confirm_stop():raise RuntimeError('停止指令未确认')
        if cancel_event:self.update(motion_phase='settling')
        QUIT.wait(0.12)
        with self.lock:self.source_barrier=time.monotonic()
        return self.valid(generation,calibration)
    def cancel_motion(self,reason):
        # Cancellation invalidates the old worker before STOP. No replacement
        # can start until that worker's finally/settle has completely finished.
        with self.lock:
            context=self.motion_context
            if context is None or context['cancel'].is_set():return True
            context['cancel'].set();self.generation+=1
            self.state['motion_phase']='stopping'
        self.log('motion-cancel',reason=reason)
        return self.confirm_stop()
    def update_motion(self,action,generation,received_at):
        """Latest feedback only: one bounded worker, no pending motion queue."""
        with self.lock:
            if generation!=self.generation or not self.valid(generation):return False
            context=self.motion_context
            stale=(not math.isfinite(received_at) or time.monotonic()-received_at>self.max_control_age)
            cancel=(context is not None and (stale or action is None or
                    action['axis']!=context['axis'] or action['speed']!=context['speed']))
        if cancel:
            if not self.cancel_motion('target missing/centered/direction changed/stale'):
                raise RuntimeError('停止指令未确认')
            return False
        with self.lock:
            if self.motion_context is not None or action is None or stale:return False
            if (generation!=self.generation or not self.valid(generation)
                    or received_at<self.source_barrier):return False
            bounded=dict(action);bounded['duration']=min(action['duration'],self.tracking_pulse_ms)
            if (bounded['axis'] not in ('pan','tilt') or type(bounded['speed']) is not int or abs(bounded['speed'])!=self.motion_speed
                    or type(bounded['duration']) is not int or not 100<=bounded['duration']<=500):
                raise ValueError('Invalid responsive motion')
            context=dict(bounded,cancel=threading.Event(),generation=generation,received_at=received_at,
                         processed_at=self.last_frame)
            self.motion_context=context
            self.state.update(motion_busy=True,motion_phase='moving')
            self.motion_thread=threading.Thread(target=self._motion_worker,args=(context,),daemon=True)
            try:self.motion_thread.start()
            except Exception:
                self.motion_context=None;self.motion_thread=None
                self.state.update(motion_busy=False,motion_phase='idle')
                raise
        return True
    def _motion_worker(self,context):
        try:
            self.log('motion-submit',axis=context['axis'],speed=context['speed'],duration_ms=context['duration'],
                     received_to_submit_ms=round((time.monotonic()-context['received_at'])*1000,1))
            self.pulse(context['axis'],context['speed'],context['generation'],duration=context['duration'],
                       cancel_event=context['cancel'],frame_received_at=context['received_at'],
                       processed_at=context['processed_at'])
        except Exception as exc:
            self.stop('云台动作异常，追踪已暂停')
            self.update(error='MotionError: '+type(exc).__name__)
            self.log('motion-fault',kind=type(exc).__name__)
        finally:
            with self.lock:
                if self.motion_context is context:
                    self.motion_context=None;self.state.update(motion_busy=False,motion_phase='idle')
    def wait_motion(self,timeout=4):
        thread=self.motion_thread
        if thread and thread is not threading.current_thread():thread.join(timeout)
        return thread is None or not thread.is_alive()
    def set_pulse_limit(self,duration):
        if type(duration) is not int or not 100<=duration<=500:raise ValueError('Pulse limit must be 100..500 ms')
        with self.lock:
            if self.enabled or self.calibrating or self.motion_context:raise ValueError('Stop tracking before changing pulse limit')
            self.tracking_pulse_ms=duration;self.state['tracking_pulse_ms']=duration
        self.log('pulse-limit',duration_ms=duration)
    def set_motion_speed(self,speed):
        if type(speed) is not int or not 20<=speed<=40:raise ValueError('Calibrated speed must be 20..40')
        with self.lock:
            if self.enabled or self.calibrating or self.motion_context or self.stop_error:
                raise ValueError('Confirm STOP before changing calibrated speed')
            self.motion_speed=speed;self.matrix=None;self.target.reset();self.generation+=1;self.calibration_error=None
            self.state.update(motion_speed=speed,mode='observe',error=None,message='速度已更改，请重新校准方向')
        self.log('motion-speed',speed=speed,calibration_invalidated=True)
    def capture(self,hold_reference=False):
        if self.engine.p.poll() is not None:raise EngineError('Detector exited; restart the service')
        start=time.monotonic()
        received=start
        if self.video_source:
            # Observe/track consumes latest unused frame; after motion, require
            # arrival after STOP+settle. Calibration retains strict fresh reads.
            barrier=start if self.frame_policy=='fresh' or self.calibrating else self.source_barrier
            deadline=start+2
            while True:
                remaining=deadline-time.monotonic()
                if remaining<=0:raise CaptureError('No new RTSP frame after capture barrier')
                frame=self.video_source.read(after_sequence=self.source_sequence,timeout=remaining)
                self.source_sequence=frame.sequence
                if frame.received_at>=barrier:break
            source=self.video_source.status()
            if source.get('restarts',0)!=self.source_restarts:
                self.source_restarts=source.get('restarts',0)
                raise CaptureError('RTSP reconnected; recalibrate before enabling control')
            jpeg=frame.jpeg;received=frame.received_at
        else:jpeg=self.camera.request('/ISAPI/Streaming/channels/101/picture')
        if not jpeg.startswith(b'\xff\xd8'):raise ValueError('Camera returned no JPEG')
        include_preview=(self.calibrating or self.preview_interval==0 or
                         (start<self.preview_requested_until and start-self.last_preview>=self.preview_interval))
        try:result,preview=self.engine.infer(jpeg,include_flow=self.calibrating,include_preview=include_preview,
                                           hold_reference=hold_reference)
        except Exception as e:
            # Callers hold operation here. Close before releasing that lock so
            # another frame/calibration cannot consume a stale partial packet.
            try:self.engine.close()
            finally:raise EngineError(type(e).__name__+': '+str(e))
        now=time.monotonic()
        if now-received>1.5:raise RuntimeError('Frame too old to control PTZ')
        geometry=(result.get('width'),result.get('height'))
        if self.video_source:
            source=self.video_source.status()
            if source.get('restarts',0)!=self.source_restarts:
                raise CaptureError('RTSP changed during inference')
            if self.frame_geometry is not None and geometry!=self.frame_geometry:
                self.frame_geometry=geometry
                raise CaptureError('RTSP geometry changed; recalibrate before enabling control')
        self.frame_geometry=geometry
        encoded=base64.b64encode(preview).decode() if preview else None
        with self.lock:
            self.last_request=received;self.last_frame=now;self.frames+=1
            self.state.update(result,frames=self.frames,pipeline_ms=round((now-start)*1000,1),
                              source_sequence=self.source_sequence,decoded_frame_age_ms=round((now-received)*1000,1),error=None)
            if preview:
                self.jpeg=preview;self.jpeg_b64=encoded;self.last_preview=now;self.preview_received=received
                self.preview_state=dict(result,frames=self.frames,source_sequence=self.source_sequence,target=None)
        return result
    def loop(self):
        while not QUIT.is_set():
            try:
                with self.operation:
                    if QUIT.is_set():break
                    result=self.capture()
                    with self.lock:
                        p=self.target.update(result['persons'],time.monotonic())
                        self.state['target']=p
                        if self.preview_state and self.preview_state['frames']==self.frames:self.preview_state['target']=p
                        active=self.enabled;generation=self.generation;matrix=self.matrix
                    if active:
                        if p is None:
                            self.update(mode='searching',message='目标未确认或已丢失，云台保持停止')
                            if self.motion_mode=='responsive':self.update_motion(None,generation,self.last_request)
                            elif not self.confirm_stop():raise RuntimeError('停止指令未确认')
                        else:
                            action=correction(p['bbox'],matrix,mode=self.control_mode,speed=self.motion_speed)
                            self.update(mode='tracking',message='正在跟随' if action else '目标已在中心区域')
                            if self.motion_mode=='responsive':
                                self.update_motion(action,generation,self.last_request)
                            elif action:
                                self.log('target',box=p['bbox'],confidence=p['confidence'],error=action['error'])
                                self.pulse(action['axis'],action['speed'],generation,duration=action['duration'])
            except Exception as e:
                self.stop('采集或推理异常，追踪已暂停')
                if isinstance(e,CaptureError):
                    with self.lock:self.matrix=None;self.target.reset();self.state['target']=None
                self.update(error=type(e).__name__+': '+str(e))
                self.log('fault',kind=type(e).__name__)
                if isinstance(e,EngineError):self.engine.close();break
                if isinstance(e,HTTPError) and e.code in (401,403):break
                if self.engine.p.poll() is not None:break
                QUIT.wait(1)
            QUIT.wait(self.loop_interval)
    def watchdog(self):
        while not QUIT.wait(0.1):
            with self.lock:
                expired=(self.enabled or self.calibrating) and time.monotonic()>self.lease
                stale=self.enabled and time.monotonic()-self.last_frame>2
            if expired or stale:self.stop('页面断开或画面超时，已停止')
    def calibrate(self):
        with self.lock:
            if self.stop_error:raise ValueError('停止尚未确认，请再次点击停止后重试')
            if self.enabled or self.calibrating:raise ValueError('先停止追踪')
            if self.motion_context:raise ValueError('云台动作尚未结束，请稍后校准')
            if self.engine.p.poll() is not None:raise ValueError('推理进程已退出，请重启服务')
            self.calibrating=True;self.generation+=1;g=self.generation;self.lease=time.monotonic()+3;self.matrix=None
            self.calibration_error=None
            self.state.update(mode='calibrating',message='正在校准方向，请站住并保持相机底座不动',error=None)
        threading.Thread(target=self._calibrate,args=(g,),daemon=True).start()
    def _calibrate(self,g):
        shifts=[]
        try:
            with self.operation:
                for axis in ('pan','tilt'):
                    self.capture()
                    before=self.motion.status()
                    shift=[0,0];responses=[];observations=[];effective=[];positions=[]
                    # At low speed, motor backlash and encoder latency can hide
                    # the first small step. Use at most three bounded pulses.
                    for count in range(1,4):
                        # Each pulse has its own baseline. A slack/takeup pulse
                        # is not part of the denominator for a measurable pulse.
                        if count>1:self.capture()
                        pulse_before=self.motion.status()
                        if not self.pulse(axis,self.motion_speed,g,True):raise RuntimeError('Calibration cancelled')
                        # Local receive freshness does not guarantee exposure
                        # after motion. Give the RTSP image time to show the
                        # full displacement before the inverse return pulse.
                        for unused in range(6):
                            frame=self.capture(hold_reference=True);responses.append(frame['shift_response'])
                            # Compare directly to the pre-motion reference.
                            # Summing small consecutive medians can lose the
                            # displacement when feature selection changes.
                            shift=list(frame['shift'])
                            observations.append(dict(pulse=count,index=unused+1,sequence=self.source_sequence,
                                                     shift=frame['shift'],response=frame['shift_response'],
                                                     decoded_frame_age_ms=self.state.get('decoded_frame_age_ms')))
                        positions.append(dict(pulse=count,before=pulse_before,after=self.motion.status()))
                        if math.hypot(*shift)>=0.005:effective.append(shift)
                        if math.hypot(*shift)>=0.008:break
                    self.log('calibration-step',axis=axis,shift=shift,pulses=count,response=min(responses),
                             before=before,after=self.motion.status(),observations=observations,
                             displacement_mode='per-pulse-fixed-reference',positions=positions,
                             effective_pulses=len(effective),speed=self.motion_speed)
                    if min(responses)<0.6:raise RuntimeError('场景纹理不足或有人移动，请重试校准')
                    if not effective:raise RuntimeError('Camera movement too small or calibration axes ambiguous')
                    shifts.append([sum(s[i] for s in effective)/len(effective) for i in (0,1)])
                    for unused in range(count):
                        if not self.pulse(axis,-self.motion_speed,g,True):raise RuntimeError('Calibration cancelled')
                    for unused in range(6):self.capture()
                m=[[shifts[0][0],shifts[1][0]],[shifts[0][1],shifts[1][1]]]
                validate_matrix(m)
                if not self.valid(g,True):raise RuntimeError('Calibration cancelled')
                with self.lock:self.matrix=m;self.target.reset();self.calibration_error=None
                self.log('calibration',matrix=m,speed=self.motion_speed)
                self.update(mode='observe',message='校准完成，可以开始追踪')
        except Exception as e:
            # capture() already discarded a failed detector under operation.
            with self.lock:self.calibration_error=str(e)
            self.update(mode='observe',error=str(e),message='校准失败，云台已停止')
            self.log('calibration-failed',kind=type(e).__name__,detail=str(e))
        finally:
            with self.lock:self.calibrating=False
            self.confirm_stop()
    def snapshot(self,include_image=False):
        with self.lock:
            s=dict(self.state);s.update(enabled=self.enabled,calibrating=self.calibrating,calibrated=self.matrix is not None,
               frame_age=round(time.monotonic()-self.last_frame,2) if self.last_frame else None)
            if self.stop_error:s['error']=self.stop_error
            elif self.calibration_error and not s.get('error'):s['error']=self.calibration_error
            if self.video_source:s['video_source']=self.video_source.status()
            if include_image:
                if self.preview_interval>0:self.preview_requested_until=time.monotonic()+2
                s['jpeg']=self.jpeg_b64
                s['preview']=dict(self.preview_state) if self.preview_state else None
                s['preview_frame_age']=round(time.monotonic()-self.preview_received,3) if self.preview_received else None
            return s

class Server(ThreadingMixIn,HTTPServer):daemon_threads=True
class Handler(BaseHTTPRequestHandler):
    def log_message(self,*args):pass
    def reply(self,data,status=200,ctype='application/json'):
        if not isinstance(data,bytes):data=json.dumps(data,ensure_ascii=False).encode()
        self.send_response(status);self.send_header('Content-Type',ctype);self.send_header('Content-Length',str(len(data)))
        self.send_header('Cache-Control','no-store');self.end_headers();self.wfile.write(data)
    def do_GET(self):
        if self.path=='/':
            self.reply((ROOT/'index.html').read_text().replace('__TOKEN__',self.server.app.token).encode(),ctype='text/html; charset=utf-8')
        elif self.path=='/api/status':self.reply(self.server.app.snapshot())
        elif self.path=='/api/frame':self.reply(self.server.app.snapshot(True))
        elif self.path.split('?',1)[0]=='/api/live.mjpeg':
            preview=self.server.app.live_preview
            if preview:preview.serve(self)
            else:self.reply({'error':'Live preview requires RTSP'},409)
        else:self.reply({'error':'Not found'},404)
    def do_POST(self):
        app=self.server.app
        if self.headers.get('X-Control-Token')!=app.token:return self.reply({'error':'Invalid control token'},403)
        origin=self.headers.get('Origin')
        if origin and origin!='http://'+self.headers.get('Host',''):return self.reply({'error':'Origin mismatch'},403)
        if int(self.headers.get('Content-Length','0'))>1024:return self.reply({'error':'Too large'},413)
        try:
            if self.path=='/api/heartbeat':app.heartbeat()
            elif self.path=='/api/start':app.start()
            elif self.path=='/api/stop':
                if not app.stop():return self.reply({'error':app.snapshot()['error']},409)
            elif self.path=='/api/calibrate':app.calibrate()
            elif self.path=='/api/pulse-limit':
                body=json.loads(self.rfile.read(int(self.headers.get('Content-Length','0'))).decode())
                if not isinstance(body,dict) or set(body)!={'duration_ms'}:raise ValueError('Expected duration_ms only')
                app.set_pulse_limit(body['duration_ms'])
            elif self.path=='/api/motion-speed':
                body=json.loads(self.rfile.read(int(self.headers.get('Content-Length','0'))).decode())
                if not isinstance(body,dict) or set(body)!={'speed'}:raise ValueError('Expected speed only')
                app.set_motion_speed(body['speed'])
            else:return self.reply({'error':'Not found'},404)
            self.reply({'ok':True})
        except Exception as e:self.reply({'error':str(e)},409)

def main():
    args=parse_args()
    for path in (args.model_dir/'model.ncnn.param',args.model_dir/'model.ncnn.bin',args.detector):
        if not path.is_file():raise RuntimeError('Required file missing: '+str(path))
    args.runtime_dir.mkdir(parents=True,exist_ok=True)
    password=getpass.getpass('Camera password (memory only): ')
    # Validate before daemonizing, so bad credentials are reported in the terminal.
    cam=FastCamera(args.camera,args.user,password);cam.status();cam.stop()
    if args.daemon:
        child=os.fork()
        if child:print('Tracker PID:',child,flush=True);return
        os.setsid()
        fd=os.open(str(args.runtime_dir/'server.log'),os.O_WRONLY|os.O_CREAT|os.O_APPEND,0o600)
        os.dup2(fd,1);os.dup2(fd,2);os.close(fd)
        fd=os.open('/dev/null',os.O_RDONLY);os.dup2(fd,0);os.close(fd)
    app=None;server=None
    def shutdown(*unused):QUIT.set()
    signal.signal(signal.SIGTERM,shutdown);signal.signal(signal.SIGINT,shutdown)
    try:
        app=App(args.camera,args.user,password,args.model_dir,args.rotation,args.detector,args.runtime_dir,
                frame_source=args.frame_source,rtsp_url=args.rtsp_url,rtsp_decoder=args.rtsp_decoder,
                rtsp_fps=args.rtsp_fps,rtsp_width=args.rtsp_width,rtsp_height=args.rtsp_height,rtsp_cpus=args.rtsp_cpus,
                detector_threads=args.detector_threads,opencv_threads=args.opencv_threads,
                detector_cpus=args.detector_cpus,frame_policy=args.frame_policy,
                control_mode=args.control_mode,loop_interval=args.loop_interval,
                motion_mode=args.motion_mode,tracking_pulse_ms=args.tracking_pulse_ms,motion_speed=args.motion_speed,
                preview_interval=args.preview_interval,max_control_age=args.max_control_age)
        del password
        server=Server((args.host,args.port),Handler);server.app=app;server.timeout=0.3
        threads=[threading.Thread(target=app.loop,daemon=True),threading.Thread(target=app.watchdog,daemon=True)]
        for t in threads:t.start()
        (args.runtime_dir/'server.pid').write_text(str(os.getpid()))
        while not QUIT.is_set():server.handle_request()
    finally:
        QUIT.set()
        if app:
            app.stop('服务退出')
            app.wait_motion()
            if app.video_source:app.video_source.close()
            app.engine.close()
        if server:server.server_close()

if __name__=='__main__':main()
