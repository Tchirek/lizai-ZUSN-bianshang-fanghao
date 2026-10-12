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
        self.assertFalse(t.NOT_GOVERNMENT.search('遂宁广播电视台'))    # media are offices now
        self.assertFalse(t.NOT_GOVERNMENT.search('遂宁日报社'))
        self.assertFalse(t.NOT_GOVERNMENT.search('翠屏区公安分局城北派出所宜宾市第二人民医院警务室'))
        self.assertTrue(t.NOT_GOVERNMENT.search('XX镇政府街社区居民委员会'))
        self.assertTrue(t.NOT_GOVERNMENT.search('XX村村民委员会'))
        self.assertTrue(t.NOT_GOVERNMENT.search('中共万古镇机关退休支部委员会'))
        self.assertTrue(t.NOT_GOVERNMENT.search('中共大兴社区委员会'))          # a社区's own, not a 区委
        self.assertTrue(t.NOT_GOVERNMENT.search('中共凉亭子社区党委'))
        self.assertFalse(t.NOT_GOVERNMENT.search('井口派出所松堡社区警务室'))   # the police room stays

    def test_government_layer_keeps_wanted_departments_only(self):
        def shown(name):                       # what write_government keeps
            return t.wanted(name) and not t.NOT_GOVERNMENT.search(name)
        keep = ['遂宁市人民政府', '中共遂宁市委', '遂宁市船山区人大常委会', '长宁镇人民代表大会',
                '湘西州州委', '政协遂宁市委员会', '中共永兴街道工作委员会', '东坡区富牛镇纪律检查委员会',
                '中共丹棱县委政法委员会', '遂宁市公安局船山分局', '介福路派出所', '遂宁市交警支队',
                '南岸区公安分局校园警务室', '翠屏区公安分局城北派出所宜宾市第二人民医院警务室',
                '井口派出所松堡社区警务室', '正安县凤仪街道人大工作委员会', '中共江南街道纪律检查工作委员会',
                '荣县人民法院', '石棉县人民检察院', '射洪市司法局', '河边司法所',
                '荣县人民法院双石人民法庭', '遂宁市看守所', '四川省川西监狱', '忠堡镇中心戒毒社区',
                '遂宁市应急管理局', '遂宁市消防救援支队', '竹海大道消防救援站', '青神县鸿化堰片区抗旱服务队',
                '遂宁市市场监督管理局', '白沙镇食品药品监督管理所', '涪陵区市场监管综合行政执法支队',
                '遂宁市城市管理行政执法局', '稻城县综合行政执法局', '木耳镇综合行政执法大队', '遂宁市生态环境局',
                '遂宁市交通运输综合行政执法支队', '国家税务总局遂宁市税务局', '遂宁市卫生健康委员会',
                '遂宁市民政局', '遂宁市教育局', '越西县教育体育和科学技术局', '遂宁市信访局',
                '遂宁市疾病预防控制中心', '龙马潭区鱼塘街道政务服务中心', '遂宁广播电视台',
                '船山区融媒体中心', '遂宁日报社']
        drop = ['遂宁市财政局', '遂宁市发展和改革委员会', '遂宁市自然资源和规划局', '遂宁市农业农村局',
                '遂宁市水利局', '遂宁市人力资源和社会保障局', '遂宁市医疗保障局', '遂宁市审计局',
                '遂宁市烟草专卖局', '遂宁海关', 'XX高新区管理委员会', '泸州市住房和城乡建设局',
                '桃江县交通运输局', '湘西土家族苗族自治州招商局', '资中县文化广播电视和旅游局',
                '泸县得胜镇人力资源和社会保障服务站', '团林苗族乡退役军人服务站',
                '中共资阳市交通运输局委员会', '中共北碚区融媒体中心委员会', '中共香格里拉市民政局委员会',
                '武候新城管理委员会', '双流区残疾人工作委员会', '中国共青团成都市委员会',
                '中国农工民主党铜梁区工作委员会', '雨城区行政管理局关心下一代工作委员会',
                '重庆市巴南区渔政协助巡护队']
        for name in keep:
            self.assertTrue(shown(name), name)
        for name in drop:
            self.assertFalse(shown(name), name)


if __name__ == '__main__':
    unittest.main()
