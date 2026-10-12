from datetime import datetime
import json
from pathlib import Path
import tempfile
import unittest

from ft8log_http import TZ, content, outlook, tail


class Tests(unittest.TestCase):
    def test_empty_and_whitelist(self):
        with tempfile.TemporaryDirectory() as work:
            root = Path(work)
            self.assertIn('尚无已完成通联', content(root, '/').decode())
            self.assertEqual(content(root, '/qsos.adi'), b'')
            self.assertIsNone(content(root, '/../../private/llm.env'))

    def test_qso_and_events(self):
        with tempfile.TemporaryDirectory() as work:
            root = Path(work)
            qso = dict(t=0,event='qso',end=0,call='W1ABC',grid='FN42',rst_sent='-12',rst_rcvd='-09')
            (root/'auto.jsonl').write_text(json.dumps(qso)+'\n'+json.dumps(dict(t=15,event='transmit',message='73'))+'\n'+json.dumps(dict(t=30,event='receive',decodes=[dict(message='CQ W1ABC FN42')]))+'\n{', encoding='utf-8')
            self.assertIn('W1ABC  FN42  -12/-09', content(root, '/').decode())
            self.assertIn('transmit  73', content(root, '/events').decode())
            self.assertIn('receive  CQ W1ABC FN42', content(root, '/events').decode())
            (root/'qsos.adi').write_bytes(b'<CALL:5>W1ABC <EOR>')
            self.assertEqual(content(root, '/qsos.adi'), b'<CALL:5>W1ABC <EOR>')

    def test_a_sked_shows_on_the_day_and_in_words(self):
        with tempfile.TemporaryDirectory() as work:
            home = Path(work)
            (home/'ft8').mkdir()
            now = datetime(2026, 10, 12, 20, 0, tzinfo=TZ).timestamp()
            t = now + 3600
            (home/'ft8'/'skeds.json').write_text(json.dumps(dict(skeds=[
                dict(call='W1ABC', t=t, start=t - 300, end=t + 900, worked=None),
                dict(call='W2DEF', t=now - 3600, start=now - 3900, end=now - 2700, worked=None)])))
            o = outlook(home, now)
            self.assertEqual([i for i, c in enumerate(o['cells']) if c['sked']], [41, 42])   # 20:55-21:15
            self.assertIn('约定 W1ABC：10-12 21:00（13:00Z），20:55–21:15 守在 2 米。', o['text'])
            self.assertNotIn('W2DEF', o['text'])                                            # over


if __name__ == '__main__':
    unittest.main()
