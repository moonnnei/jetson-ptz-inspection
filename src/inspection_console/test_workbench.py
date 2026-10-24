"""User-selected range, image coordinates, review gate and stale-click checks."""
import copy
import math
import threading
import unittest

from area_scan import area_plan,validate_area
from manual_control import ManualControl,click_target
from ring_coordinates import source_point,panorama_point,source_box,source_choices
from store import Conflict
import test_scene_initialization as init_fixtures


class WorkbenchMathTests(unittest.TestCase):
    def test_selected_range_reduces_views_and_never_wraps(self):
        p=area_plan(dict(pan=220,tilt=5,zoom=2),dict(pan_min=180,pan_max=220,tilt_min=0,tilt_max=0))
        self.assertEqual(p['expected_frames'],1)
        self.assertEqual(p['points'][0]['pan'],200)
        self.assertTrue(all(v['zoom']==1 for v in p['points']))
        self.assertEqual(validate_area(p),p)
        changed=copy.deepcopy(p);changed['points'][0]['pan']+=1
        with self.assertRaises(ValueError):validate_area(changed)

    def test_multirow_serpentine_and_capacity_are_bounded(self):
        p=area_plan(dict(pan=180,tilt=0,zoom=1),dict(pan_min=130,pan_max=230,tilt_min=0,tilt_max=60))
        self.assertEqual(p['rows'],2)
        self.assertEqual(p['expected_frames'],4)
        self.assertNotEqual(p['points'][0]['pan'],p['points'][1]['pan'])
        self.assertEqual(p['points'][1]['pan'],p['points'][2]['pan'])
        self.assertLessEqual(area_plan(p['anchor'],dict(pan_min=0,pan_max=350,tilt_min=0,tilt_max=90))['expected_frames'],24)

    def test_click_respects_zoom_and_mechanical_edges(self):
        p=dict(pan=200,tilt=10,zoom=1)
        self.assertEqual(click_target(p,.5,.5)[0],p)
        wide,_=click_target(p,.75,.5);tele,_=click_target(dict(p,zoom=2),.75,.5)
        self.assertLess(wide['pan'],tele['pan'])
        limited,flag=click_target(dict(p,pan=0,tilt=0),1,1)
        self.assertTrue(flag);self.assertEqual(limited['pan'],0);self.assertEqual(limited['tilt'],0)
        for x in (True,float('nan'),-1,2):
            with self.assertRaises(ValueError):click_target(p,x,.5)

    def test_row_projection_roundtrip_and_source_selection(self):
        p=dict(version=2,projection='angle_assisted_cylindrical',width=4096,height=1200,
            source_width=2560,source_height=1440,focal_pixels=1542.85,yaw_image_sign=1,
            source_orientation='rotate180_from_raw',calibration_validated=False,row_tilts=[0.,15.],row_height=600)
        for tilt in p['row_tilts']:
            u,v=source_point(1300,700,200,p,tilt)
            x,y=panorama_point(u,v,200,p,tilt)
            self.assertAlmostEqual(x,1300);self.assertAlmostEqual(y,700)
        box=source_box([1100,600,1400,900],200,p,15.)
        frames=[dict(id=str(t),width=2560,height=1440,orientation='rotate180_from_raw',sha256='0'*64,
                     pose=dict(pan=200,tilt=t,zoom=1)) for t in (0.,15.)]
        self.assertEqual([c['frame_id'] for c in source_choices(box,frames,p)],['15.0'])

    def test_click_token_expires_on_stop_and_pose_change(self):
        class Camera:
            def __init__(self):self.pan=200;self.stopped=0
            def status(self):return dict(azimuth=str(self.pan*10),elevation='0',absoluteZoom='10')
            def stop(self):self.stopped+=1
        camera=Camera();manual=ManualControl(camera)
        token=manual.status()['view_token'];camera.pan=210
        with self.assertRaises(Conflict):manual.start('center',token,.6,.5)
        camera.pan=200;manual.stop()
        with self.assertRaises(Conflict):manual.start('center',token,.6,.5)


class ReviewGateTests(unittest.TestCase):
    def setUp(self):
        self.fixture=init_fixtures.InitializationTests();self.fixture.setUp()
    def tearDown(self):self.fixture.tearDown()
    def test_new_scan_waits_for_review_then_queues_retained_only(self):
        f=self.fixture;service=f.service(init_fixtures.Backend(f.manifest))
        service.start(f.store.snapshot()['revision'],review_first=True);service.thread.join(4)
        self.assertEqual(service.status()['job']['status'],'awaiting_review')
        self.assertFalse(service.status()['active']);self.assertTrue(f.annotations.status()['paused'])
        self.assertEqual(f.annotations.status()['queued'],0)
        state=f.store.snapshot();mid=state['maps'][-1]['id']
        service.commission(state['revision'],mid)
        self.assertEqual(f.annotations.status()['queued'],1)
        self.assertEqual(service.status()['job']['status'],'commissioning')

    def test_rejected_candidate_is_not_queued_after_review(self):
        f=self.fixture;service=f.service(init_fixtures.Backend(f.manifest))
        service.start(f.store.snapshot()['revision'],review_first=True);service.thread.join(4)
        state=f.store.snapshot();mid=state['maps'][-1]['id']
        c=next(c for c in state['candidates'] if c['map_id']==mid)
        f.store.review_many(state['revision'],[c['id']],'reject')
        service.commission(f.store.snapshot()['revision'],mid)
        self.assertEqual(f.annotations.status()['queued'],0)

    def test_supplement_stays_visible_and_paused_before_commissioning(self):
        f=self.fixture;service=f.service(init_fixtures.Backend(f.manifest))
        service.start(f.store.snapshot()['revision'],review_first=True);service.thread.join(4)
        state=f.store.snapshot();mid=state['maps'][-1]['id'];frame=next(v for v in state['frames'] if v['map_id']==mid)
        f.annotations.queue(state['revision'],frame['id'],[500,500,650,650])
        state=f.store.snapshot()
        self.assertEqual(f.annotations.status()['queued'],0)
        c=state['candidates'][-1];self.assertEqual(c['state'],'pending')
        service.commission(state['revision'],mid)
        self.assertEqual(f.annotations.status()['queued'],2)
        self.assertIn(c['id'],[q['candidate_id'] for q in f.store.snapshot()['annotation_queue']])

    def test_supplement_over_existing_box_does_not_queue_second_visit(self):
        f=self.fixture;service=f.service(init_fixtures.Backend(f.manifest))
        service.start(f.store.snapshot()['revision'],review_first=True);service.thread.join(4)
        state=f.store.snapshot();mid=state['maps'][-1]['id'];candidate=next(c for c in state['candidates'] if c['map_id']==mid)
        f.annotations.queue(state['revision'],candidate['frame_id'],candidate['bbox'])
        service.commission(f.store.snapshot()['revision'],mid)
        self.assertEqual(f.annotations.status()['queued'],1)

if __name__=='__main__':unittest.main()
