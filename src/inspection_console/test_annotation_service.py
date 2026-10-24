"""Queue API and persistent pause behavior; no real camera or motion lease."""
import json
import threading
import unittest
from urllib.request import Request,build_opener,ProxyHandler

from annotation_service import AnnotationService
from server import Server
import test_registry_annotations as fixtures


class AnnotationServiceTests(unittest.TestCase):
    setUp=fixtures.AnnotationQueueTests.setUp
    tearDown=fixtures.AnnotationQueueTests.tearDown
    commission_all=fixtures.AnnotationQueueTests.commission_all
    queue_new=fixtures.AnnotationQueueTests.queue_new

    def test_no_hardware_default_does_not_process_queued_boxes(self):
        self.commission_all();self.queue_new()
        service=AnnotationService(self.store)
        service.tick()
        self.assertFalse(service.status()['available'])
        self.assertEqual(service.status()['queued'],1)
        self.assertIsNone(service.thread)

    def test_pause_survives_service_recreation_and_inactive_stop_sends_no_camera_command(self):
        self.commission_all();self.queue_new()
        class Camera:
            calls=0
            def stop(inner):inner.calls+=1
        camera=Camera()
        service=AnnotationService(self.store,camera,'unused','unused')
        service.stop()
        self.assertEqual(camera.calls,0)
        recreated=AnnotationService(self.store,camera,'unused','unused')
        recreated.tick()
        self.assertTrue(recreated.status()['paused'])
        self.assertIsNone(recreated.thread)
        recreated.resume(self.store.snapshot()['revision'])
        self.assertFalse(recreated.status()['paused'])

    def test_restart_pauses_queued_missed_box_before_scheduler_tick(self):
        self.commission_all();self.queue_new()
        service=AnnotationService(self.store,object(),'unused','unused')
        service.recover();service.tick()
        self.assertTrue(service.status()['paused'])
        self.assertEqual(service.status()['queued'],1)
        self.assertIsNone(service.thread)
        recreated=AnnotationService(self.store,object(),'unused','unused')
        recreated.tick()
        self.assertTrue(recreated.status()['paused'])
        self.assertIsNone(recreated.thread)

    def test_restart_pauses_unconsumed_discovery_without_losing_request(self):
        self.commission_all()
        def pending(state):state['maps'][0]['automatic_registration_requested']=True
        self.store.change(None,'test_pending_discovery',{},pending)
        service=AnnotationService(self.store,object(),'unused','unused',object())
        service.recover();service.tick()
        self.assertTrue(service.status()['paused'])
        self.assertTrue(self.store.snapshot()['maps'][0]['automatic_registration_requested'])
        self.assertIsNone(service.thread)

    def test_http_missed_box_queues_without_extra_human_confirmation_and_global_stop_pauses_queue(self):
        self.commission_all()
        annotations=AnnotationService(self.store,object(),'unused','unused')
        server=Server(('127.0.0.1',0),self.store,annotations=annotations)
        thread=threading.Thread(target=server.serve_forever);thread.daemon=True;thread.start()
        base='http://127.0.0.1:'+str(server.server_address[1])
        opener=build_opener(ProxyHandler({}))
        def post(path,body):
            req=Request(base+path,data=json.dumps(body).encode(),method='POST',headers={
                'Content-Type':'application/json','X-Control-Token':server.token,'Origin':base})
            with opener.open(req,timeout=3) as response:return json.loads(response.read().decode())
        try:
            state=self.store.snapshot();before=len(state['candidates'])
            result=post('/api/candidates/add',dict(revision=state['revision'],frame_id=state['frames'][0]['id'],
                bbox=[1200,300,1400,500]))
            self.assertFalse(result['result']['manual_confirmation_required'])
            self.assertEqual(len(self.store.snapshot()['candidates']),before)
            self.assertEqual(annotations.status()['queued'],1)
            post('/api/schedule/stop',dict(revision=self.store.snapshot()['revision']))
            self.assertTrue(annotations.status()['paused'])
            self.assertFalse(self.store.snapshot()['schedule']['enabled'])
        finally:
            server.shutdown();server.server_close();thread.join(2)


if __name__=='__main__':unittest.main()
