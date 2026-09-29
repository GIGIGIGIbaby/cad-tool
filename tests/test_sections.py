import unittest
from pathlib import Path
from unittest.mock import patch

from cad3 import rendering, runtime


class ParseSectionTests(unittest.TestCase):
    def test_valid_forms(self):
        self.assertEqual(rendering.parse_section('y=12.5'), ('y', 12.5))
        self.assertEqual(rendering.parse_section(' Z = -3 '), ('z', -3.0))
        self.assertEqual(rendering.parse_section(('x', 0)), ('x', 0.0))

    def test_invalid_forms(self):
        for bad in ('y', 'w=1', 'x=nan', 'x=inf', 'x=abc', 'x=1e12'):
            with self.subTest(bad=bad), self.assertRaises(rendering.RenderError):
                rendering.parse_section(bad)


class FitCameraTests(unittest.TestCase):
    class Cam:
        scale = None

        def SetParallelScale(self, value):
            self.scale = value

    class Ren:
        def ResetCamera(self):
            pass

        def ResetCameraClippingRange(self):
            pass

    def test_long_part_fills_width(self):
        cam = self.Cam()
        # 400 x 20 x 20 bar seen from the front (camera -Y, up Z): width-limited
        rendering._fit_camera(self.Ren(), cam, (-200, -10, -10), (200, 10, 10),
                              (0.0, -1.0, 0.0), (0.0, 0.0, 1.0), 800, 400)
        # half width 200 / aspect 2 = 100 > half height 10
        self.assertAlmostEqual(cam.scale, 100 * rendering.CAMERA_FIT_MARGIN)

    def test_tall_part_fills_height(self):
        cam = self.Cam()
        rendering._fit_camera(self.Ren(), cam, (-5, -5, 0), (5, 5, 300),
                              (0.0, -1.0, 0.0), (0.0, 0.0, 1.0), 800, 600)
        self.assertAlmostEqual(cam.scale, 150 * rendering.CAMERA_FIT_MARGIN)


class RuntimeSectionValidationTests(unittest.TestCase):
    def test_bad_section_rejected_before_worker(self):
        with patch.object(runtime, 'discover_freecad', return_value=Path('/bin/true')):
            result = runtime._invoke(action='render', root=Path('/tmp'),
                                     document=Path('/tmp/none.FCStd'),
                                     sections=['q=1'])
        self.assertFalse(result['ok'])
        self.assertIn('section', str(result['errors']))

    def test_no_views_no_sections_rejected(self):
        with patch.object(runtime, 'discover_freecad', return_value=Path('/bin/true')):
            result = runtime._invoke(action='render', root=Path('/tmp'),
                                     document=Path('/tmp/none.FCStd'), views=())
        self.assertFalse(result['ok'])


if __name__ == '__main__':
    unittest.main()
