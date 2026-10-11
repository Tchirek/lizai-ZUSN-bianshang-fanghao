import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import activity as a


class Map:
    labels = []

    def xy(self, lat, lon):
        return lon, lat

    def inside(self, x, y):
        return 4 <= x < a.W - 4 and 4 <= y < a.H - 4


class Tests(unittest.TestCase):
    def test_rf_hops_and_unknown_repeater_gaps(self):
        self.assertEqual(a.rf_hops('A', ['B', 'C*', 'WIDE2-1', 'qAR', 'I']),
                         ['A', 'B', 'C', 'I'])
        self.assertEqual(a.rf_hops('A', ['WIDE1*', 'B*', 'qAO', 'I']),
                         ['A', None, 'B', 'I'])
        self.assertEqual(a.rf_hops('A', ['TCPIP*', 'qAC', 'SERVER']), ['A'])
        self.assertEqual(a.rf_hops('A', ['B*', 'qAC', 'SERVER']), ['A'])

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

    def report(self, sender, receiver):
        return dict(senderCallsign='S', receiverCallsign='R', senderLocator=sender,
                    receiverLocator=receiver, frequency='14074000', mode='FT8')

    def test_psk_connects_local_and_off_map_partners_and_deduplicates(self):
        locations = {'S': (100, 100), 'R': (100, a.W + 100), 'F': (-100, -100)}
        report = self.report('S', 'R')
        with patch.object(a, 'locator', side_effect=locations.get):
            im = a.draw_activity(Map(), ({}, {}, []), [report, report, self.report('F', 'R')], [])
        self.assertEqual(im.getpixel((500, 100)), (255, 250, 0, 170))
        self.assertEqual(im.getpixel((a.W - 1, 100)), (255, 250, 0, 170))
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
        self.assertEqual(im.getpixel((200, 200)), a.APRS_RF + (200,))
        self.assertEqual(im.getpixel((int(a.W / 2 + 100), int(a.H / 2))), a.WHITE + (230,))

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
