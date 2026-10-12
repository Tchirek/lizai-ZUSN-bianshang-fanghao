import json
import os
import unittest

import flags

HERE = os.path.dirname(os.path.abspath(__file__))


class Tests(unittest.TestCase):
    def test_every_consulate_has_a_flag_and_sits_on_the_map(self):
        with open(os.path.join(HERE, "embassies.json"), encoding="utf-8") as f:
            data = json.load(f)
        self.assertGreaterEqual(len(data), 20)                 # Chengdu's and Chongqing's
        seen = set()
        for e in data:
            self.assertTrue(e["name"], e)
            self.assertTrue(flags.known(e["code"]), f'{e["name"]}: no flag for {e["code"]}')
            lat, lon = e["point"]
            self.assertTrue(27.75 < lat < 33.21 and 99.27 < lon < 111.98, e["name"])
            seen.add(e["code"])
        self.assertGreaterEqual(len(seen), 20)

    def test_labels_can_be_placed_all_the_way_zoomed_in(self):
        import map_view as m
        self.assertGreaterEqual(m.MAX_ZOOM, 64)                 # deeper than the 2 km scale
        self.assertGreaterEqual(m.LABEL_ZOOMS[-1], m.MAX_ZOOM * 0.9)

    def test_spots_agree_with_their_array_form(self):
        import numpy as np
        import map_view as m
        ks = np.arange(m.SPOTS)
        for tw, th in ((119, 23), (145, 25)):
            dx, dy = m.offsets(ks, tw, th)
            for k in range(m.SPOTS):
                self.assertEqual(m.spot(k, tw, th), (float(dx[k]), float(dy[k])), k)

    def test_flags_draw_at_the_label_height(self):
        for code in ("DE", "FR", "JP", "GB", "KR", "NP", "US"):
            pic = flags.flag(code, 13)
            self.assertEqual(pic.get_height(), 13, code)
            self.assertAlmostEqual(pic.get_width(), round(13 * flags.RATIO), delta=1, msg=code)
        self.assertEqual(flags.flag("ZZ", 13).get_height(), 13)   # unknown: neutral, still a flag


if __name__ == '__main__':
    unittest.main()
