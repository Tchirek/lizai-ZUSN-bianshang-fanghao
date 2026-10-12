from concurrent.futures import Future
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

from ft8_auto import Auto, QSO
from ft8_llm import Chat
from ft8_memory import Memory
from ft8_web import Control
import k5ft8

SID = 'page123456'


class Tests(unittest.TestCase):
    def gate(self):
        self.now = 100
        return Control('K1ABC', 'FN31', lambda s:s, clock=lambda:self.now)

    def test_viewing_is_automatic_and_close_is_immediate(self):
        web = self.gate()
        self.assertFalse(web.handle(dict(action='status',session=SID))['manual'])
        self.assertTrue(web.allow('W1ABC K1ABC -12','状态机'))
        web.handle(dict(action='send',session=SID,message='HI'))
        web.sent('HI',100,web.queued()['id'])
        self.assertFalse(web.allow('FINE TNX','LLM'))
        web.handle(dict(action='close',session=SID))
        self.assertTrue(web.allow('FINE TNX','LLM'))
        self.assertIsNone(web.queued())

    def test_exact_approval_once_and_expiry(self):
        web = self.gate()
        web.handle(dict(action='send',session=SID,message='HI'))
        web.sent(web.queued()['message'],100,web.queued()['id'])
        self.assertFalse(web.allow('R-12','状态机'))
        old_id = web.suggestion['id']
        web.allow('R-09','状态机')
        with self.assertRaises(ValueError):
            web.handle(dict(action='approve',session=SID,id=old_id))
        web.handle(dict(action='approve',session=SID,id=web.suggestion['id']))
        self.assertTrue(web.allow('R-09','状态机'))
        web.sent('R-09',115)
        self.assertFalse(web.allow('R-09','状态机'))
        self.now += 7
        self.assertTrue(web.allow('R-09','状态机'))

    def controller(self, chat=False):
        c = object.__new__(Auto)
        c.call, c.grid, c.tx_df, c.last_cq = 'K1ABC','FN31',1127,0
        c.dial = 144174000
        c.lock, c.decoding = threading.Lock(), threading.Event()
        c.qso, c.radio, c.write = QSO(c.call,c.grid), Mock(), Mock()
        c.web = Control(c.call,c.grid,lambda s:s,clock=lambda:0)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        c.out = self.tmp.name
        c.memory = Memory(str(Path(self.tmp.name)/'memory.json'))
        c.chat = None
        if chat:
            with patch.dict('os.environ',OPENAI_API_KEY='test',OPENAI_MODEL='test'):
                c.chat = Chat(c.call,c.write,c.memory)
            self.addCleanup(c.chat.close)
        return c

    def test_manually_typed_cq_requires_approval_then_close_restores_auto(self):
        c = self.controller()
        c.web.handle(dict(action='send',session=SID,message='CQ K1ABC FN31'))
        self.assertTrue(c.transmit(0))
        self.assertEqual(c.last_cq,0)
        c.qso.receive(15,[dict(message='K1ABC W1ABC FN42',snr=-12,df=1200)])
        self.assertFalse(c.transmit(30))
        self.assertEqual(c.radio.transmit.call_count,1)
        c.web.handle(dict(action='approve',session=SID,id=c.web.suggestion['id']))
        self.assertTrue(c.transmit(30))
        c.qso.receive(45,[dict(message='K1ABC W1ABC R-09',snr=-12,df=1200)])
        self.assertFalse(c.transmit(60))
        c.web.handle(dict(action='close',session=SID))
        self.assertTrue(c.transmit(60))
        self.assertEqual(c.radio.transmit.call_count,3)

    def test_one_click_cq_is_auto_and_opens_existing_receive_window(self):
        from influ_scheduler import window_reasons
        from types import SimpleNamespace
        c = self.controller()
        state = c.web.handle(dict(action='cq',session=SID))
        self.assertFalse(state['manual'])
        c.web.handle(dict(action='close',session=SID))
        self.assertTrue(c.has_manual())     # an automatic CQ survives closing its initiating page
        self.assertTrue(c.transmit(0))
        self.assertTrue(c.in_qso(15))
        self.assertTrue(c.want_receive(15))
        args = SimpleNamespace(policy='traffic')
        mux = dict(ok=True,sdr_ok=True,fifo_connected=True)
        self.assertEqual(window_reasons(args,mux,True,{},None,1,None,0,14.25,
                         dict(reasons=['training_activity_window']),c.in_qso(15)), [])
        c.qso.receive(15,[dict(message='K1ABC W1ABC FN42',snr=-12,df=1200)])
        self.assertTrue(c.transmit(30))
        self.assertIsNone(c.web.owner)
        self.assertIsNone(c.web.suggestion)
        self.assertFalse(c.web.recent(241))

    def test_cq_exits_a_previous_manual_approval_session(self):
        web = self.gate()
        web.handle(dict(action='send',session=SID,message='HI'))
        web.sent('HI',100,web.queued()['id'])
        self.assertFalse(web.allow('HELLO','LLM'))
        state = web.handle(dict(action='cq',session=SID))
        self.assertFalse(state['manual'])
        self.assertTrue(web.allow('HELLO','LLM'))

    def test_unanswered_proactive_sends_are_logged_without_a_qso(self):
        for action, message in [('cq', 'CQ K1ABC FN31'), ('send', 'HI'),
                                ('send', 'W1ABC K1ABC FN31')]:
            with self.subTest(action=action, message=message):
                c = self.controller()
                c.web.handle(dict(action=action, session=SID, message=message))
                self.assertTrue(c.transmit(0))
                c.write.assert_any_call('transmit', slot=0, message=message,
                                        dial=144174000, offset_hz=1200,
                                        source='web_cq' if action == 'cq' else 'manual')
                self.assertEqual(c.qso.completed, [])
                self.assertFalse(any(call.args[0] == 'qso' for call in c.write.call_args_list))

    def test_recent_cq_receive_window_survives_service_restart(self):
        from pathlib import Path
        c = self.controller()
        (Path(c.out)/'auto.jsonl').write_text(json.dumps(dict(event='transmit',message='CQ K1ABC FN31',slot=90))+'\n')
        c.restore_cq(120)
        self.assertTrue(c.in_qso(135))
        self.assertTrue(c.want_receive(135))
        self.assertIsNone(c.web.owner)

    def test_manual_free_text_binds_llm_and_needs_approval(self):
        c = self.controller(chat=True)
        c.web.handle(dict(action='send',session=SID,message='HOW R U?'))
        self.assertTrue(c.transmit(0))
        self.assertEqual(c.chat.peer['df'],1127)
        future = Future()
        future.set_result('FINE TNX')
        c.chat.pool.submit = Mock(return_value=future)
        c.chat.receive(dict(slot=15,decodes=[dict(message='HI',df=1127)]))
        self.assertFalse(c.transmit(30))
        self.assertEqual(c.web.suggestion['source'],'LLM')
        c.web.handle(dict(action='approve',session=SID,id=c.web.suggestion['id']))
        self.assertTrue(c.transmit(30))
        self.assertEqual(c.radio.transmit.call_args.kwargs['free_text'],True)

    def test_validation_does_not_arm_and_rejects_spoofed_or_long_text(self):
        c = self.controller()
        c.web.validate = c.validate_manual
        for text in ('你好','A LONG FREE TEXT MESSAGE','CQ W1ABC FN42'):
            with self.assertRaises(ValueError):
                c.web.handle(dict(action='send',session=SID,message=text))
        self.assertIsNone(c.web.owner)

    def test_manual_standard_message_cancels_old_llm_reply(self):
        c = self.controller(chat=True)
        c.chat.start(dict(call='W2XYZ',end=13.2,df=1500,rx_parity=1))
        c.chat.future = Future()
        c.chat.future.set_result('OLD REPLY')
        c.web.handle(dict(action='send',session=SID,message='W1ABC K1ABC FN31'))
        self.assertTrue(c.transmit(0))
        self.assertIsNone(c.chat.future)
        self.assertIsNone(c.chat.peer)
        self.assertEqual(c.qso.peer,'W1ABC')

    def test_other_tab_cannot_approve_or_release(self):
        web = self.gate()
        web.handle(dict(action='send',session=SID,message='HI'))
        web.sent('HI',100,web.queued()['id'])
        web.allow('HELLO','LLM')
        with self.assertRaises(ValueError):
            web.handle(dict(action='approve',session='other1234',id=web.suggestion['id']))
        web.handle(dict(action='close',session='other1234'))
        self.assertFalse(web.allow('HELLO','LLM'))

    def test_cancelled_at_the_last_moment_never_writes_serial(self):
        radio = object.__new__(k5ft8.Radio)
        radio.s, radio.mode = Mock(), Mock(return_value='FT8')
        with patch.object(k5ft8,'encode',return_value=bytes(79)), patch.object(k5ft8,'wait_until'), patch.object(k5ft8.time,'time',return_value=0):
            with self.assertRaises(k5ft8.TransmitCancelled):
                radio.transmit('HI',0,guard=lambda:False)
        radio.s.write.assert_not_called()


if __name__ == '__main__':
    unittest.main()
