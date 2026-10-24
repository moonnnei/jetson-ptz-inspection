"""Real worker timing: replace an unfinished command and keep STOP decisive."""
from contextlib import contextmanager
import threading,time,unittest
import xml.etree.ElementTree as ET
from manual_control import ManualControl,click_target


@contextmanager
def lease():yield


class Camera:
    def __init__(self):
        self.pan=200.;self.tilt=10.;self.zoom=1.;self.calls=[];self.stops=0;self.sent=threading.Event()
    def status(self):return dict(azimuth=str(self.pan*10),elevation=str(self.tilt*10),absoluteZoom=str(self.zoom*10))
    def stop(self):self.stops+=1
    def xml(self,path,body):
        fields={n.tag.split('}')[-1]:n.text for n in ET.fromstring(body).iter() if len(n)==0}
        target=dict(pan=float(fields['azimuth'])/10,tilt=float(fields['elevation'])/10,zoom=float(fields['absoluteZoom'])/10)
        self.calls.append(target);self.sent.set()
        # Deliberately leave the first command unfinished until replaced.
        if len(self.calls)>1:self.pan=target['pan'];self.tilt=target['tilt'];self.zoom=target['zoom']


class ManualPriorityTests(unittest.TestCase):
    def test_latest_intent_replaces_running_command_without_waiting_for_arrival(self):
        c=Camera();m=ManualControl(c,lease=lease)
        try:
            m.status();m.start('left',step=5);self.assertTrue(c.sent.wait(1))
            begin=time.monotonic();r=m.start('right',step=2)
            self.assertLess(time.monotonic()-begin,.2);self.assertIsNotNone(r['supersedes'])
            for unused in range(80):
                if not m.active():break
                time.sleep(.02)
            self.assertFalse(m.active());self.assertEqual(len(c.calls),2)
            self.assertEqual(c.calls[-1]['pan'],198.)
            self.assertEqual(m.status()['result']['status'],'completed');self.assertGreaterEqual(c.stops,2)
        finally:m.close()

    def test_stop_discards_queued_intent(self):
        c=Camera();m=ManualControl(c,lease=lease)
        try:
            m.status();m.start('left');self.assertTrue(c.sent.wait(1));m.stop()
            for unused in range(60):
                if not m.active():break
                time.sleep(.02)
            self.assertFalse(m.active());self.assertEqual(len(c.calls),1)
        finally:m.close()

    def test_vertical_click_and_arrow_share_upright_axis(self):
        p=dict(pan=200,tilt=10,zoom=1)
        self.assertGreater(click_target(p,.5,.25)[0]['tilt'],p['tilt'])
        self.assertLess(click_target(p,.5,.75)[0]['tilt'],p['tilt'])

if __name__=='__main__':unittest.main()
