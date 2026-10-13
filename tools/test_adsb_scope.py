"""The ADS-B feed and base map, as HamClock's ADS-B pane (hamclock-adsb.cpp) reads them."""
import json
import os
import tempfile
import unittest

import adsb_scope as scope


class Today:
    def add(self, now, aircraft):
        return 141


class Tests(unittest.TestCase):
    def feed(self, aircraft, now=1000.0):
        feed = scope.Feed(today=Today())
        with tempfile.NamedTemporaryFile('w', suffix='.json', delete=False) as f:
            json.dump(dict(now=now, aircraft=aircraft), f)
        self.addCleanup(os.unlink, f.name)
        feed.sample(f.name)
        return feed, f.name

    def test_aircraft_lines_carry_what_the_pane_draws(self):
        feed, _ = self.feed([
            dict(hex='7801ab', lat=30.5, lon=105.6, seen=1, seen_pos=1, alt_baro=35012, track=123.6, gs=450.4,
                 category='a3', flight='CCA1234 '),
            dict(hex='~abc123', lat=30.6, lon=105.7, seen=1, seen_pos=1, alt_baro='ground', mlat=['lat']),
            dict(hex='7801ac', seen=2),                                  # heard, no position
            dict(hex='7801ad', lat=30.6, lon=105.7, seen=90, seen_pos=90),   # gone
        ])
        lines = feed.text(away=True).splitlines()
        self.assertEqual(lines[1], 'T 1000 141 1')
        self.assertEqual(lines[2], 'N 1')
        aircraft = [line.split() for line in lines if line.startswith('A ')]
        self.assertEqual(aircraft[0], ['A', '7801ab', '30.50000', '105.60000', '35012', '124', '450', 'A3', '0', 'CCA1234'])
        # on the ground, track unknown, from MLAT, named by its address
        self.assertEqual(aircraft[1], ['A', '~abc123', '30.60000', '105.70000', '-2', '-1', '0', '-', '1', '~ABC123'])
        self.assertEqual(len(aircraft), 2)

    def test_trails_follow_their_aircraft_and_age_out(self):
        feed, path = self.feed([dict(hex='7801ab', lat=30.5, lon=105.6, seen=0, seen_pos=0)])
        for t, lat in ((1004, 30.51), (1008, 30.52)):
            with open(path, 'w') as f:
                json.dump(dict(now=t, aircraft=[dict(hex='7801ab', lat=lat, lon=105.6, seen=0, seen_pos=0)]), f)
            feed.sample(path)
        lines = feed.text().splitlines()
        trail = lines[lines.index(next(line for line in lines if line.startswith('A '))) + 1]
        self.assertEqual(trail, 'P 30.50000 105.60000 30.51000 105.60000 30.52000 105.60000')
        with open(path, 'w') as f:
            json.dump(dict(now=1008 + scope.TRAIL_S + 5, aircraft=[]), f)
        feed.sample(path)
        self.assertEqual(dict(feed.trails), {})

    def test_base_map_in_ascii_by_kind(self):
        text = scope.map_text(dict(
            lines=[dict(k='river', p=[[30.1, 105.1], [30.2, 105.2]]), dict(k='rail', p=[[30, 105]])],
            places=[dict(kind='city', name='Lángzhōng', lat=31.5, lon=106.0),
                    dict(kind='city', name='遂宁', lat=30.5, lon=105.6),
                    dict(kind='town', name='Anju', lat=30.4, lon=105.4)],
            airports=[dict(name='ZUSN', lat=30.47, lon=105.61),
                      dict(name='Baiguo General Aviation Airport', lat=30.9, lon=104.9)]))
        lines = text.splitlines()[1:]
        self.assertEqual(lines, ['L river 30.10000 105.10000 30.20000 105.20000',
                                 'C 31.50000 106.00000 Langzhong',
                                 'C 30.50000 105.60000',                 # no ASCII name: a dot only
                                 'W 30.40000 105.40000',
                                 'R 30.47000 105.61000 ZUSN',
                                 'R 30.90000 104.90000'])                # airfields by code only

    def test_files_are_written_whole(self):
        with tempfile.TemporaryDirectory() as folder:
            path = os.path.join(folder, scope.FEED)
            scope.write(path, 'T 1 2 0\n')
            with open(path) as f:
                self.assertEqual(f.read(), 'T 1 2 0\n')
            self.assertEqual(os.listdir(folder), [scope.FEED])


if __name__ == '__main__':
    unittest.main()
