import io
import json
from concurrent.futures import Future
import tempfile
import threading
import unittest
from unittest.mock import ANY, Mock, patch
from pathlib import Path

import ft8_auto
import ft8_llm as llm
import k5ft8
from ft8_web import Control


class Tests(unittest.TestCase):
    def chat(self):
        with patch.dict('os.environ', OPENAI_API_KEY='test-key', OPENAI_MODEL='test-model'):
            chat = llm.Chat('K1ABC', Mock())
        self.addCleanup(chat.close)
        chat.start(dict(call='W1ABC', end=13.2, rx_parity=0, df=1200))
        future = Future()
        future.set_result('FINE TNX')
        chat.pool.submit = Mock(return_value=future)
        return chat

    def test_only_free_text(self):
        self.assertEqual(llm.free_text('how r u?'), 'HOW R U?')
        for text in ('CQ K1ABC FN31', 'RR73', '-12', 'R-09', 'FN31',
                     'K1ABC W1ABC -09', '你好', 'ß', 'THIS IS TOO LONG', 'HI;REBOOT'):
            with self.subTest(text=text), self.assertRaises(ValueError):
                llm.free_text(text)

    def test_api_contract_and_validation(self):
        response = dict(status='completed', output=[dict(type='message', role='assistant',
                        content=[dict(type='output_text', text='FINE TNX')])])
        with patch.object(llm.urllib.request, 'urlopen', return_value=io.BytesIO(json.dumps(response).encode())) as send:
            self.assertEqual(llm.reply('HOW R U?', [], 'K1ABC', 'W1ABC', 'fake', 'test-model'), 'FINE TNX')
            request = send.call_args.args[0]
            body = json.loads(request.data)
            self.assertEqual(request.full_url, 'https://api.openai.com/v1/responses')
            self.assertFalse(body['store'])
            self.assertEqual(body['model'], 'test-model')
            self.assertEqual(body['input'], [dict(role='user', content='HOW R U?')])
            self.assertEqual(send.call_args.kwargs['timeout'], 8)
        response['output'][0]['content'][0]['text'] = 'THIS RESPONSE IS MUCH TOO LONG'
        with patch.object(llm.urllib.request, 'urlopen', return_value=io.BytesIO(json.dumps(response).encode())):
            with self.assertRaises(ValueError):
                llm.reply('HI', [], 'K1ABC', 'W1ABC', 'fake', 'test-model')

    def test_the_model_sees_the_whole_exchange_the_state_machine_ran(self):
        q = ft8_auto.QSO('K1ABC', 'FN31')
        q.cq = (0, 'CQ K1ABC FN31')
        q.receive(15, [dict(message='K1ABC W1ABC FN42', snr=-12, df=1200)])     # answers our CQ
        q.transmitted(30)                                                       # W1ABC K1ABC -12
        q.receive(45, [dict(message='K1ABC W1ABC R-09', snr=-10, df=1200)])
        q.transmitted(60)                                                       # W1ABC K1ABC RR73
        qso = q.completed[0]
        self.assertEqual([f['message'] for f in qso['log']],
                         ['CQ K1ABC FN31', 'K1ABC W1ABC FN42', 'W1ABC K1ABC -12', 'K1ABC W1ABC R-09',
                          'W1ABC K1ABC RR73'])
        chat = self.chat()
        chat.start(qso)
        chat.receive(dict(slot=105, decodes=[dict(df=1200, message='TNX QSO')]))
        self.assertIs(chat.pool.submit.call_args.args[-1], qso)
        response = dict(status='completed', output=[dict(type='message', role='assistant',
                        content=[dict(type='output_text', text='TNX 73')])])
        with patch.dict('os.environ', FT8_LLM_CONTACT='TG EXAMPLE'), \
                patch.object(llm.urllib.request, 'urlopen', return_value=io.BytesIO(json.dumps(response).encode())) as send:
            self.assertEqual(llm.reply('TNX QSO', [], 'K1ABC', 'W1ABC', 'fake', 'test-model', qso), 'TNX 73')
        text = json.loads(send.call_args.args[0].data)['instructions']
        for part in ('their grid FN42', 'report sent -12', 'report received -09', 'K1ABC sent: CQ K1ABC FN31',
                     'heard: K1ABC W1ABC R-09 (-10 dB)', 'K1ABC sent: W1ABC K1ABC RR73', 'reply exactly TG EXAMPLE'):
            self.assertIn(part, text)

    def test_standard_frames_never_call_api(self):
        chat = self.chat()
        chat.receive(dict(slot=30, decodes=[dict(df=1200, message=m) for m in
                     ('CQ K1ABC FN31', 'K1ABC W1ABC -09', 'RR73', '73')]))
        chat.pool.submit.assert_not_called()

    def test_deepseek_endpoint_and_non_thinking(self):
        response = dict(status='completed', output=[dict(type='message', role='assistant',
                        content=[dict(type='output_text', text='FINE TNX')])])
        with patch.dict('os.environ', OPENAI_BASE_URL='https://api.deepseek.com', OPENAI_REASONING_EFFORT='none'):
            with patch.object(llm.urllib.request, 'urlopen', return_value=io.BytesIO(json.dumps(response).encode())) as send:
                self.assertEqual(llm.reply('HI', [], 'K1ABC', 'W1ABC', 'fake', 'deepseek-flash'), 'FINE TNX')
                self.assertEqual(send.call_args.args[0].full_url, 'https://api.deepseek.com/responses')
                self.assertEqual(json.loads(send.call_args.args[0].data)['reasoning'], dict(effort='none'))

    def test_free_chat_one_reply_on_opposite_slot(self):
        chat = self.chat()
        rec = dict(slot=30, decodes=[dict(df=1200, message='HOW R U?')])
        chat.receive(rec)
        self.assertIsNone(chat.due(30))
        self.assertEqual(chat.due(45), 'FINE TNX')
        chat.sent('FINE TNX')
        chat.receive(dict(rec, slot=60))
        self.assertEqual(chat.pool.submit.call_count, 1)
        self.assertEqual(chat.history[-1], dict(role='assistant', content='FINE TNX'))

    def test_unrelated_tones_and_stale_replies(self):
        chat = self.chat()
        chat.receive(dict(slot=30, decodes=[dict(df=1500, message='HI')]))
        chat.pool.submit.assert_not_called()
        chat.receive(dict(slot=30, decodes=[dict(df=1200, message='HI')]))
        self.assertIsNone(chat.due(135))
        self.assertIsNone(chat.future)

    def test_encoder_rejects_standard_type_for_llm(self):
        out = ('Source-encoded message, 77 bits:\n' + '0' * 76 + '1' +
               '\nChannel symbols (79 tones):\n' + '3140652' + '0' * 29 +
               '3140652' + '0' * 29 + '3140652')
        with patch.object(k5ft8.subprocess, 'run', return_value=Mock(stdout=out)):
            with self.assertRaises(ValueError):
                k5ft8.encode('CQ K1ABC FN31', free_text=True)
            self.assertEqual(len(k5ft8.encode('CQ K1ABC FN31')), 79)

    def test_auto_receive_uses_hook_only_after_qso(self):
        controller = object.__new__(ft8_auto.Auto)
        controller.lock = threading.Lock()
        controller.web = Control('K1ABC', 'FN31', lambda s:s)
        controller.qso = ft8_auto.QSO('K1ABC', 'FN31')
        controller.chat = self.chat()
        controller.last_cq, controller.call, controller.grid = 0, 'K1ABC', 'FN31'
        controller.dial, controller.write = 144174000, Mock()
        rec = dict(slot=30, decodes=[dict(message='HI', snr=-12, df=1200)])
        with tempfile.TemporaryDirectory() as work:
            path = Path(work) / 'test.cu8'
            path.with_suffix('.json').write_text('{}')
            with patch.object(ft8_auto.ft8, 'decode_recording', return_value=[rec]):
                controller.receive(str(path))
        controller.chat.pool.submit.assert_called_once()
        self.assertIsNone(controller.qso.pending)
        controller.decoding, controller.radio = threading.Event(), Mock()
        self.assertTrue(controller.transmit(45))
        controller.radio.transmit.assert_called_once_with('FINE TNX', 45, free_text=True, guard=ANY)


if __name__ == '__main__':
    unittest.main()
