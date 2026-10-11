import json
import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

import activity as a


class Map:
    labels = []

    def xy(self, lat, lon):
        return lon, lat

    def inside(self, x, y):
        return 4 <= x < a.W - 4 and 4 <= y < a.H - 4


class Tests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.directory = tmp.name
        p = patch.object(a, 'MODEL', tmp.name)
        p.start()
        self.addCleanup(p.stop)
        for cls, name in ((a.Psk, 'psk.json'), (a.Wspr, 'wspr.json'), (a.LivePsk, 'live.json')):
            p = patch.object(cls, 'STATE', str(Path(tmp.name) / name))
            p.start()
            self.addCleanup(p.stop)

    def test_rf_hops_and_unknown_repeater_gaps(self):
        self.assertEqual(a.rf_hops('A', ['B', 'C*', 'WIDE2-1', 'qAR', 'I']),
                         ['A', 'B', 'C', 'I'])
        self.assertEqual(a.rf_hops('A', ['WIDE1*', 'B*', 'qAO', 'I']),
                         ['A', None, 'B', 'I'])
        self.assertEqual(a.rf_hops('A', ['TCPIP*', 'qAC', 'SERVER']), ['A'])
        self.assertEqual(a.rf_hops('A', ['B*', 'qAC', 'SERVER']), ['A'])
        for q in ('qAr', 'qAo'):
            self.assertEqual(a.rf_hops('A', ['B*', q, 'I']), ['A', 'B', 'I'])

    def test_packets_store_only_known_rf_links_and_expire(self):
        feed = a.Aprs(0, 0)
        packet = dict(**{'from': 'A'}, path=['WIDE1*', 'B*', 'qAR', 'I'],
                      latitude=100, longitude=100)
        with patch.object(a.aprslib, 'parse', return_value=packet), patch.object(a.time, 'time', return_value=100):
            feed.take('packet')
            heard, positions, links = feed.recent()
        self.assertEqual(links, [('B', 'I')])
        self.assertTrue(heard['A'][2])
        with patch.object(a.time, 'time', return_value=100 + a.APRS_SHOW_S + 1):
            heard, positions, links = feed.recent()
        self.assertEqual((heard, links), ({}, []))
        self.assertIn('A', positions)

    def test_missing_nodes_subscription_and_positions_survive_restart(self):
        feed = a.Aprs(0, 0)
        packet = dict(**{'from': 'A'}, path=['B*', 'qAR', 'I'], latitude=100, longitude=100)
        with patch.object(a.aprslib, 'parse', return_value=packet), patch.object(a.time, 'time', return_value=100):
            feed.take('packet')
            self.assertEqual(feed.missing(), ['B', 'I'])
            feed.recent()
            restarted = a.Aprs(0, 0)
            self.assertEqual(restarted.recent()[0]['A'][:2], [100, 100])
        with patch.object(a.time, 'time', return_value=100 + a.APRS_SHOW_S + 1):
            self.assertEqual(feed.missing(), [])

    def test_wspr_bounded_query_and_report_format(self):
        with patch.object(a.Wspr, 'STATE', str(Path(self.directory) / 'wspr.json')):
            feed = a.Wspr(Mock(latlon=lambda x,y: (30 + y/200, 100 + x/200)))
        self.assertIn('INTERVAL 30 MINUTE', feed.query)
        self.assertIn('band IN', feed.query)
        self.assertIn('LIMIT 2000', feed.query)
        self.assertIn('tx_lat BETWEEN', feed.query)
        self.assertIn('rx_lat BETWEEN', feed.query)
        self.assertEqual(feed.EVERY_S, 600)
        report = dict(self.report('S', 'R'), mode='WSPR')
        response = io.BytesIO(json.dumps(dict(data=[report])).encode())
        with patch.object(a.urllib.request, 'urlopen', return_value=response) as http:
            self.assertEqual(feed.fetch('region'), [report])
            self.assertTrue(http.call_args.args[0].full_url.startswith('https://db1.wspr.live/?query='))

    def test_rate_limit_is_persisted_without_immediate_retry(self):
        path = str(Path(self.directory) / 'psk.json')
        with patch.object(a.Psk, 'STATE', path):
            feed = a.Psk(['OM'])
        feed.STATE = path
        feed.fetch = Mock(side_effect=a.urllib.error.HTTPError('url', 429, 'limited', {}, None))
        with patch.object(a.time, 'time', return_value=100), patch.object(a.time, 'sleep', side_effect=[None, StopIteration]):
            with self.assertRaises(StopIteration):
                feed.run()
        self.assertEqual(json.loads(Path(path).read_text())['next'], 1900)
        feed.fetch.assert_called_once_with('OM')

    def test_active_ft8_push_suspends_http_queries(self):
        feed = a.Psk(['OM'])
        feed.live = Mock(last_psk_received=100)
        feed.fetch = Mock()
        with patch.object(a.time, 'time', return_value=110), patch.object(a.time, 'sleep', side_effect=[None, None, StopIteration]):
            with self.assertRaises(StopIteration):
                feed.run()
        feed.fetch.assert_not_called()

    def report(self, sender, receiver):
        return dict(senderCallsign='S', receiverCallsign='R', senderLocator=sender,
                    receiverLocator=receiver, frequency='14074000', mode='FT8')

    def test_psk_connects_local_and_off_map_partners_and_deduplicates(self):
        locations = {'S': (100, 100), 'R': (100, a.W + 100), 'F': (-100, -100)}
        report = self.report('S', 'R')
        with patch.object(a, 'locator', side_effect=locations.get):
            im = a.draw_activity(Map(), ({}, {}, []), [report, report, self.report('F', 'R')], [])
        self.assertEqual(im.getpixel((500, 100)), (255, 250, 0, 48))
        self.assertEqual(im.getpixel((a.W - 1, 100)), (255, 250, 0, 48))
        self.assertEqual(im.getpixel((500, 300)), (0, 0, 0, 0))

    def test_missing_partner_position_never_invents_a_line(self):
        with patch.object(a, 'locator', side_effect=lambda grid: (100, 100) if grid == 'S' else None):
            im = a.draw_activity(Map(), ({}, {}, []), [self.report('S', '')], [])
        self.assertEqual(im.getpixel((500, 100)), (0, 0, 0, 0))

    def test_aprs_and_our_completed_qso_connections(self):
        positions = dict(A=(200, 100, True, 0), B=(200, 300, True, 0))
        with patch.object(a, 'locator', return_value=(a.H / 2, a.W / 2 + 200)):
            im = a.draw_activity(Map(), ({}, positions, [('A', 'B'), ('A', 'UNKNOWN')]),
                                 [], [('W1ABC', 'FN42')])
        self.assertEqual(im.getpixel((200, 200)), a.APRS_RF + (175,))
        self.assertEqual(im.getpixel((int(a.W / 2 + 100), int(a.H / 2))), a.WHITE + (230,))

    def test_dense_paths_preserve_peers_and_limit_off_map_bundles(self):
        rows = [dict(self.report('S', f'R{i}'), receiverCallsign=f'R{i}') for i in range(100)]
        locations = {'S': (200, 200), **{f'R{i}': (-100 + i * 10, 2000) for i in range(100)}}
        with patch.object(a, 'locator', side_effect=locations.get):
            stations, _, paths, peers, total = a.digital_graph(Map(), rows)
        self.assertEqual(set(stations), {'S'})
        self.assertLessEqual(len(paths), 2)
        self.assertEqual((peers['S'], total), (100, 100))

    def test_live_payload_regional_filter_cache_and_expiry(self):
        feed = a.LivePsk(Map())
        msg = dict(sc='S', rc='R', sl='S', rl='R', f=14074000, md='FT8')
        with patch.object(a, 'locator', side_effect={'S': (100, 100), 'R': (100, 2000)}.get):
            with patch.object(a.time, 'time', return_value=100):
                feed.take(json.dumps(msg)); feed.take(json.dumps(msg))
                self.assertEqual(len(feed.current()), 1)
                self.assertEqual(feed.received, 2)
                self.assertEqual(len(a.LivePsk(Map()).current()), 1)
            with patch.object(a.time, 'time', return_value=1901):
                self.assertEqual(feed.current(), [])
        feed.take('bad json')
        feed.take(json.dumps(dict(msg, f='bad')))
        self.assertEqual(feed.reports, {})

    def test_week_tally_preserves_completed_peers(self):
        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp) / 'auto.jsonl'
            monday = a.ft8_week()[2]
            rows = [dict(t=monday - 1, event='transmit', message='CQ OLD AA00'),
                    dict(t=monday + 1, event='transmit', message='CQ BG8ASN OM20'),
                    dict(t=monday + 2, event='receive', decodes=[]),
                    dict(t=monday + 3, event='qso', call='W1ABC', grid='FN42')]
            log.write_text(''.join(json.dumps(r) + '\n' for r in rows))
            with patch.object(a, 'FT8_LOG', str(log)):
                cq, peers, start = a.ft8_week()
            self.assertEqual((cq, peers, start), (1, [('W1ABC', 'FN42')], monday))
            self.assertEqual(a.draw_tally(cq, len(peers), start).size, (a.TALLY_W, a.TALLY_H))


if __name__ == '__main__':
    unittest.main()
