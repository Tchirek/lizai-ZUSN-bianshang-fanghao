import math
import unittest

import build_tiles as t


class Tests(unittest.TestCase):
    def test_amap_points_come_back_to_wgs84(self):
        for lat, lon in ((30.533, 105.592), (30.66, 104.06), (29.61, 106.51), (32.6, 110.8)):
            dlat, dlon = t.gcj_offset(lat, lon)
            metres = math.hypot(dlat * 111_000, dlon * 111_000 * math.cos(math.radians(lat)))
            self.assertTrue(100 < metres < 800, metres)              # the shift in this part of China
            back = t.wgs84(lat + dlat, lon + dlon)
            self.assertLess(abs(back[0] - lat) + abs(back[1] - lon), 1e-6)

    def test_names_show_from_the_zoom_their_level_deserves(self):
        self.assertEqual(t.name_zoom('遂宁市人民政府'), 6)
        self.assertEqual(t.name_zoom('遂宁市无线电管理'), 6)
        self.assertEqual(t.name_zoom('遂宁市财政局', '地市级政府及事业单位'), 6)
        self.assertEqual(t.name_zoom('蓬溪县人民法院'), 8)
        self.assertEqual(t.name_zoom('大石镇人民政府', '乡镇级政府及事业单位'), 12)

    def test_nested_sites_keep_only_the_outer_area(self):
        sites = [
            dict(point=[5, 5], rings=[[[0, 0], [0, 10], [10, 10], [10, 0]]]),
            dict(point=[5, 5], rings=[[[4, 4], [4, 6], [6, 6], [6, 4]]]),
            dict(point=[5, 5], rings=[]),
            dict(point=[20, 20], rings=[]),
        ]
        self.assertEqual(t.nested(sites), {1, 2})

    def test_office_name_filter_keeps_regulators_and_drops_a_restaurant(self):
        self.assertFalse(t.NOT_GOVERNMENT.search('遂宁市市场监督管理局'))
        self.assertFalse(t.NOT_GOVERNMENT.search('四川省药品监督管理局'))
        self.assertTrue(t.NOT_GOVERNMENT.search('华凤园餐厅'))


if __name__ == '__main__':
    unittest.main()
