import json
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
import unittest

from influ_scheduler import window_reasons
from influ.traffic import fit, near_or_approaching, Policy
from ft8_auto import Auto, QSO
from ft8_web import Control


class Tests(unittest.TestCase):
    def test_history_ident_and_size(self):
        snapshots = [dict(now=3600 + 60 * i, aircraft=[
            dict(hex='light', category='A1', seen=0, messages=10),
            dict(hex='airliner', category='A3', seen=0, messages=10),
            dict(hex='untyped', seen=0, messages=10, alt_baro=4500, gs=95),
            dict(hex='mode-s-airliner', seen=0, messages=10, alt_baro=33000, gs=450),
            dict(hex='identified', category='A1', seen=0, messages=10, flight='C172ABC' if i else '')
        ]) for i in range(4)]
        model = fit(snapshots, [])
        self.assertEqual(model['light_no_ident'], ['light'])
        self.assertEqual(model['untyped_no_ident'], ['untyped'])       # not the high, fast one
        self.assertEqual(model['untyped_high_or_fast'], 1)
        self.assertIn('identified', model['known_ident'])
        self.assertEqual(model['active_minutes'][18], 4)
        self.assertIn(17, model['protected_bins'])
        self.assertIn(19, model['protected_bins'])

    def test_sixty_miles_and_inbound_projection(self):
        base = dict(lat=0, seen_pos=0, alt_baro=10000)
        self.assertTrue(near_or_approaching(dict(base, lon=0.85), 0, 0, 0, 0, 60 * 1.609344))
        self.assertFalse(near_or_approaching(dict(base, lon=1.0), 0, 0, 0, 0, 60 * 1.609344))
        self.assertTrue(near_or_approaching(dict(base, lon=0.9, gs=300, track=270), 0, 0, 0, 0, 60 * 1.609344))
        self.assertFalse(near_or_approaching(dict(base, lon=0.9, gs=300, track=90), 0, 0, 0, 0, 60 * 1.609344))

    def test_qso_overrides_aircraft_and_budgets_not_hardware(self):
        args = SimpleNamespace(policy='traffic', day_blind=1800, max_per_hour=6)
        budget = SimpleNamespace(blind=1800, per_hour={0:6})
        mux = dict(ok=True, sdr_ok=True, fifo_connected=True)
        state = dict(reasons=['training_activity_window', 'cloud_aircraft_near_or_approaching'])
        get = lambda qso: window_reasons(args, mux, True, {}, None, 1, budget, 0, 14.25, state, qso)
        self.assertEqual(get(True), [])
        self.assertIn('training_activity_window', get(False))
        self.assertIn('day_blind_budget', get(False))
        mux['sdr_ok'] = False
        self.assertEqual(get(True), ['sdrmux_unhealthy'])

    def test_only_started_qso_gets_priority_and_it_expires(self):
        controller = object.__new__(Auto)
        controller.lock, controller.qso, controller.last_cq = threading.Lock(), QSO('K1ABC','FN31'), -1000
        controller.web = Control('K1ABC', 'FN31', lambda s:s)
        controller.qso.receive(30, [dict(message='CQ W1ABC FN42', snr=-12)])
        self.assertFalse(controller.in_qso(45))
        controller.qso.transmitted(45)
        self.assertTrue(controller.in_qso(60))
        self.assertFalse(controller.in_qso(285))

    def test_unlocated_local_aircraft_uses_cloud_position(self):
        with tempfile.TemporaryDirectory() as work:
            root = Path(work)
            (root/'station').write_text(json.dumps(dict(lat=0,lon=0)))
            (root/'profile').write_text(json.dumps(dict(fitted_at=0, protected_bins=[], observed_minutes=[30]*48)))
            cloud = dict(source=dict(t=30, now=30000, ac=[dict(hex='a',lat=0,lon=2,seen_pos=0)]))
            # API timestamps can be seconds or milliseconds, not relative milliseconds.
            cloud['source']['now'] = 30
            (root/'cloud').write_text(json.dumps(cloud))
            policy = Policy(str(root/'profile'),str(root/'cloud'),str(root/'station'))
            local = dict(now=30,aircraft=[dict(hex='a',seen=0)])
            self.assertEqual(policy.assess(30,local)['reasons'], [])
            cloud['source']['t'] = -100
            (root/'cloud').write_text(json.dumps(cloud))
            self.assertIn('cloud_tracks_stale', policy.assess(30,local)['reasons'])
            self.assertIn('local_aircraft_without_position', policy.assess(30,local)['reasons'])


if __name__ == '__main__':
    unittest.main()
