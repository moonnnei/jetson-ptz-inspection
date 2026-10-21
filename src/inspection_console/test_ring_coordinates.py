"""Mathematical round trips do not measure physical mapping accuracy."""
import copy
import unittest

from ring_coordinates import parameters,panorama_point,source_point,source_box,panorama_box,segments,source_choices


def geometry():
    return dict(version=1,projection='angle_assisted_cylindrical',width=4096,height=600,
        source_width=2560,source_height=1440,focal_pixels=1565.59,yaw_image_sign=1,
        source_orientation='rotate180_from_raw',calibration_validated=False)


class CoordinateTests(unittest.TestCase):
    def test_point_roundtrip_at_mechanical_endpoints_and_both_yaw_signs(self):
        for sign in (-1,1):
            p=geometry();p['yaw_image_sign']=sign
            for pan in (0,1,175,349,350):
                for x,y in ((200,200),(1280,720),(2300,1200)):
                    u,v=source_point(x,y,pan,p)
                    a,b=panorama_point(u%p['width'],v,pan,p)
                    self.assertAlmostEqual(a,x,places=7);self.assertAlmostEqual(b,y,places=7)

    def test_source_box_crossing_panorama_seam_has_two_visible_parts(self):
        p=geometry();box=source_box([1200,600,1360,840],0,p)
        self.assertGreater(box[2],p['width'])
        parts=segments(box,p)
        self.assertEqual(len(parts),2)
        self.assertAlmostEqual(sum(b[2]-b[0] for b in parts),box[2]-box[0])
        mapped=panorama_box(box,0,p)
        self.assertLessEqual(mapped[0],1200+1e-6);self.assertGreaterEqual(mapped[2],1360-1e-6)

    def test_nearest_source_is_a_review_choice_not_a_visit_pose(self):
        p=geometry();box=source_box([1150,650,1300,850],200,p)
        frames=[dict(id=str(i),sha256='a'*64,width=2560,height=1440,orientation='rotate180_from_raw',
                     pose=dict(pan=angle,tilt=5.5,zoom=1)) for i,angle in enumerate((20,190,210))]
        choices=source_choices(box,frames,p)
        self.assertEqual(choices[0]['frame_id'],'2')
        self.assertNotIn('visit_pose',choices[0])

    def test_height_or_orientation_mismatch_does_not_invent_a_source(self):
        p=geometry();box=source_box([1000,600,1400,800],200,p)
        f=dict(id='source',sha256='a'*64,width=2560,height=1440,orientation='raw_camera',pose=dict(pan=200,tilt=5,zoom=1))
        self.assertEqual(source_choices(box,[f],p),[])

    def test_front_facing_and_vertical_limits_are_enforced(self):
        p=geometry()
        with self.assertRaises(ValueError):panorama_point(0,300,180,p)
        with self.assertRaises(ValueError):panorama_point(0,-100,0,p)

    def test_bad_or_false_calibration_descriptor_rejected(self):
        for changes in ({'focal_pixels':float('nan')},{'calibration_validated':True},{'yaw_image_sign':True},{'width':0}):
            p=copy.deepcopy(geometry());p.update(changes)
            with self.assertRaises(ValueError):parameters(p)


if __name__=='__main__':unittest.main()
