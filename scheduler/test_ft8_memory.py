import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

import ft8_auto
import ft8_llm as llm
from ft8_memory import Memory
from ft8_web import Control

T0 = 1_800_000_000.0                       # a slot start, UTC


def worked(q, start, peer='W1ABC', grid='FN42'):
    """Run a full exchange in which `peer` answers our CQ; the completed QSO."""
    q.cq = (start, 'CQ K1ABC FN31')
    q.receive(start + 15, [dict(message=f'K1ABC {peer} {grid}', snr=-12, df=1200)])
    q.transmitted(start + 30)
    q.receive(start + 45, [dict(message=f'K1ABC {peer} R-09', snr=-10, df=1200)])
    q.transmitted(start + 60)
    return q.completed.pop()


class Tests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = str(Path(tmp.name) / 'memory.json')
        self.memory = Memory(self.path)

    def chat(self):
        with patch.dict('os.environ', OPENAI_API_KEY='test-key', OPENAI_MODEL='test-model'):
            chat = llm.Chat('K1ABC', Mock(), self.memory)
        self.addCleanup(chat.close)
        return chat

    def test_a_new_station_gets_no_greeting(self):
        chat = self.chat()
        qso = worked(ft8_auto.QSO('K1ABC', 'FN31'), T0)
        self.memory.record_qso(qso)
        with patch.object(llm, 'ask') as ask:
            chat.start(qso, opening=True)
        self.assertIsNone(chat.future)
        ask.assert_not_called()
        self.assertEqual(chat.recall, '')

    def test_a_station_worked_before_is_recalled_and_greeted_once(self):
        q = ft8_auto.QSO('K1ABC', 'FN31')
        first = worked(q, T0)
        self.memory.record_qso(first)
        self.memory.record_chat('W1ABC', False, 'NAME JOHN', T0 + 75)
        self.memory.record_chat('W1ABC', True, 'TNX JOHN', T0 + 90)
        self.memory.set_notes('W1ABC', ['name John'], T0 + 90)
        q.recent.clear()                                       # a day later
        second = worked(q, T0 + 86400)
        self.memory.record_qso(second)
        chat = self.chat()
        with patch.object(llm, 'ask', return_value='GM JOHN AGN') as ask:
            chat.start(second, opening=True)
            chat.future.result(timeout=5)
        text = ask.call_args.args[0]
        for part in ('Earlier contacts with W1ABC: 1.', 'their grid FN42', 'they: NAME JOHN', 'we: TNX JOHN',
                     'Notes about them: name John', 'show that you remember it', 'open the chat with ONE short greeting'):
            self.assertIn(part, text)
        end = second['end'] - 13.2                             # our last frame's slot
        self.assertIsNone(chat.due(end + 15))                   # their slot
        self.assertEqual(chat.due(end + 30), 'GM JOHN AGN')
        chat.sent('GM JOHN AGN')
        self.assertIsNone(chat.due(end + 60))                   # once only
        self.assertEqual(self.memory.stations['W1ABC']['episodes'][-1]['chat'][-1][:2], [True, 'GM JOHN AGN'])

    def test_nothing_to_say_sends_nothing(self):
        q = ft8_auto.QSO('K1ABC', 'FN31')
        self.memory.record_qso(worked(q, T0))
        q.recent.clear()
        second = worked(q, T0 + 86400)
        self.memory.record_qso(second)
        chat = self.chat()
        with patch.object(llm, 'ask', return_value='NONE'):
            chat.start(second, opening=True)
            with self.assertRaises(llm.Quiet):
                chat.future.result(timeout=5)
        self.assertIsNone(chat.due(second['end'] - 13.2 + 30))
        chat.write.assert_any_call('llm_quiet', peer='W1ABC')

    def test_replies_know_the_earlier_contacts_too(self):
        q = ft8_auto.QSO('K1ABC', 'FN31')
        self.memory.record_qso(worked(q, T0))
        q.recent.clear()
        second = worked(q, T0 + 86400)
        self.memory.record_qso(second)
        chat = self.chat()
        with patch.object(llm, 'ask', return_value='NONE'):
            chat.start(second, opening=True)
            chat.future.exception(timeout=5)
        chat.future = None
        slot = second['end'] - 13.2 + 45                       # their parity
        with patch.object(llm, 'ask', return_value='YES 2ND QSO') as ask:
            chat.receive(dict(slot=slot, decodes=[dict(df=1200, message='WKD B4?')]))
            self.assertEqual(chat.future.result(timeout=5), 'YES 2ND QSO')
        self.assertIn('Earlier contacts with W1ABC: 1.', ask.call_args.args[0])
        self.assertEqual(self.memory.stations['W1ABC']['episodes'][-1]['chat'][-1][:2], [False, 'WKD B4?'])

    def test_a_finished_chat_becomes_notes_once(self):
        q = ft8_auto.QSO('K1ABC', 'FN31')
        qso = worked(q, T0)
        self.memory.record_qso(qso)
        self.memory.record_chat('W1ABC', False, 'QTH BOSTON', T0 + 75)
        chat = self.chat()
        chat.start(qso)
        reply = json.dumps(dict(notes=['QTH Boston']))
        with patch.object(llm, 'ask', return_value=reply) as ask:
            chat.tick(qso['end'] + 300)                        # the chat window is over
            chat.pool.submit(lambda: None).result(timeout=5)   # the notes job ran before it
            chat.tick(qso['end'] + 315)
            chat.pool.submit(lambda: None).result(timeout=5)
        self.assertEqual(ask.call_count, 1)
        self.assertIn('they: QTH BOSTON', ask.call_args.args[1][0]['content'])
        self.assertEqual(Memory(self.path).stations['W1ABC']['notes'], ['QTH Boston'])

    def test_memory_rebuilds_from_the_log(self):
        log = Path(self.path).with_name('auto.jsonl')
        log.write_text('\n'.join(json.dumps(r) for r in [
            dict(t=T0 + 60, event='qso', call='W1ABC', grid='FN42', rst_sent='-12', rst_rcvd='-09',
                 start=T0 + 15, end=T0 + 73.2),
            dict(t=T0 + 80, event='llm_receive', peer='W1ABC', message='HOW R U?'),
            dict(t=T0 + 95, event='llm_transmit', peer='W1ABC', message='FINE TNX')]) + '\n')
        m = Memory(str(Path(self.path).with_name('fresh.json')), str(log))
        self.assertTrue(m.worked_before('W1ABC', T0 + 86400))
        self.assertEqual([c[:2] for c in m.stations['W1ABC']['episodes'][0]['chat']],
                         [[False, 'HOW R U?'], [True, 'FINE TNX']])

    def test_a_greeting_under_way_holds_the_exchange(self):
        c = object.__new__(ft8_auto.Auto)
        c.lock, c.qso, c.last_cq = threading.Lock(), ft8_auto.QSO('K1ABC', 'FN31'), -1000
        c.web = Control('K1ABC', 'FN31', lambda s: s)
        c.chat = self.chat()
        c.chat.peer = dict(call='W1ABC', end=T0, rx_parity=0, df=1200)
        self.assertFalse(c.in_qso(T0 + 30))                     # nothing under way
        c.chat.future = Mock()
        self.assertTrue(c.in_qso(T0 + 30))
        self.assertFalse(c.in_qso(T0 + 300))                    # four minutes at most


if __name__ == '__main__':
    unittest.main()
