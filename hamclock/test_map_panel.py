import os
os.environ['SDL_VIDEODRIVER'] = 'dummy'
import json
import math
from datetime import datetime, timezone
from pathlib import Path
from concurrent.futures import Future
import tempfile
import unittest
from unittest.mock import Mock, patch

import numpy as np
import pygame
import activity as a
import map_panel as p


class Tests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        patcher = patch.object(a, 'MODEL', tmp.name)
        patcher.start(); self.addCleanup(patcher.stop)
        (self.root/'rangemap.json').write_text(json.dumps(dict(our_agl_m=16)))
        self.m = object.__new__(a.Map)
        self.m.lat0, self.m.lon0, self.m.km_per_px, self.m.labels = 0, 0, 1, []

    def test_solar_noon_midnight_and_readable_night_layer(self):
        noon = datetime(2026, 3, 20, 12, tzinfo=timezone.utc).timestamp()
        self.assertGreater(float(p.solar_elevation(0, 0, noon)), 85)
        self.assertLess(float(p.solar_elevation(0, 0, noon - 43200)), -85)
        night = p.night_layer(self.m, noon - 43200)
        self.assertEqual(night.size, (a.W, a.H))
        self.assertEqual(night.getpixel((a.W//2, a.H//2))[3], 32)
        self.assertEqual(p.night_layer(self.m, noon).getpixel((a.W//2, a.H//2))[3], 0)

    def test_flat_short_path_clear_and_ridge_blocks_every_frequency(self):
        flat = lambda lat,lon: np.zeros_like(lat)
        with patch.object(p, 'CachedDEM', return_value=flat):
            profile = p.path_profile(self.m, 0, .006)
        self.assertTrue(all(cell == 'OK' for row in profile['status'] for cell in row))
        def ridge(lat, lon):
            return np.where((lon > .004) & (lon < .006), 500, 0)
        with patch.object(p, 'CachedDEM', return_value=ridge):
            blocked = p.path_profile(self.m, 0, .01)
        self.assertTrue(all(cell == 'BLK' for row in blocked['status'] for cell in row))
        self.assertLess(blocked['clearance'], -400)

    def test_map_point_roundtrip_nearby_and_panel_size(self):
        lat,lon = self.m.latlon(700, 300)
        x,y = self.m.xy(lat,lon)
        self.assertAlmostEqual(x,700); self.assertAlmostEqual(y,300)
        (self.root/'activity.json').write_text(json.dumps(dict(aprs={'TEST':(lat,lon,True,0)})))
        calls,paths = p.nearby(self.m,700,300)
        self.assertEqual((calls,paths),(['TEST'],0))
        self.assertEqual(p.panel_image(self.m,(700,300)).size,(p.PW,p.PH))

    def test_click_open_close_bounds_and_async_failure(self):
        panel = object.__new__(p.MapPanel)
        panel.m, panel.pool = self.m, Mock()
        future = Future(); future.set_exception(OSError('no tile'))
        panel.pool.submit.return_value = future
        panel.point = panel.future = panel.profile = panel.error = panel.box = None
        box = (278, 298, 1320, 660)
        self.assertTrue(panel.tap((1590,950),box))
        frame = pygame.Surface((1600,960))
        panel.draw(frame,box)
        self.assertEqual(panel.error,'Terrain unavailable')
        self.assertTrue(pygame.Rect(box).contains(panel.box))
        self.assertTrue(panel.tap((panel.box.right-10,panel.box.top+10),box))
        self.assertIsNone(panel.point)
        self.assertFalse(panel.tap((10,10),box))


if __name__=='__main__': unittest.main()
