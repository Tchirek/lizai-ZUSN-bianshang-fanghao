import os
os.environ['SDL_VIDEODRIVER'] = 'dummy'
import json
from datetime import datetime, timezone
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import numpy as np
import activity as a
import map_panel as p


class Tests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        patcher = patch.object(a, 'MODEL', tmp.name)
        patcher.start(); self.addCleanup(patcher.stop)
        (Path(tmp.name)/'rangemap.json').write_text(json.dumps(dict(our_agl_m=16)))
        self.m = object.__new__(a.Map)
        self.m.lat0, self.m.lon0, self.m.km_per_px, self.m.labels = 0, 0, 1, []

    def test_sun_overhead_at_noon_and_below_at_midnight(self):
        noon = datetime(2026, 3, 20, 12, tzinfo=timezone.utc).timestamp()
        self.assertGreater(float(p.solar_elevation(0, 0, noon)), 85)
        self.assertLess(float(p.solar_elevation(0, 0, noon - 43200)), -85)

    def test_flat_short_path_clear_and_ridge_blocks_every_frequency(self):
        flat = lambda lat,lon: (np.zeros_like(lat), 12)
        with patch.object(p, 'heights', flat):
            profile = p.path_profile(self.m, 0, .006)
        self.assertTrue(all(cell == 'OK' for row in profile['status'] for cell in row))
        def ridge(lat, lon):
            return np.where((lon > .004) & (lon < .006), 500, 0), 12
        with patch.object(p, 'heights', ridge):
            blocked = p.path_profile(self.m, 0, .01)
        self.assertTrue(all(cell == 'BLK' for row in blocked['status'] for cell in row))
        self.assertLess(blocked['clearance'], -400)


if __name__=='__main__': unittest.main()
