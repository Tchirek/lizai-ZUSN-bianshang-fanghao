#!/usr/bin/env python3
"""Small self-check: encoder/framing, both QSO roles, retries and incomplete logs."""
import unittest
from unittest.mock import Mock, patch
import os
import subprocess
import tempfile
import threading
import wave

import numpy as np

import ft8_auto as auto
import k5ft8
from ft8_web import Control


class Tests(unittest.TestCase):
    def receive(self, q, slot, message):
        q.receive(slot, [dict(message=message, snr=-12)])

    def test_encoder_and_framing(self):
        symbols = k5ft8.encode("CQ K1ABC FN31")
        self.assertEqual(len(symbols), 79)
        p = k5ft8.tx_packet(symbols)
        self.assertEqual(p[:16].hex(), "59575873354f00007800000095758537")
        self.assertEqual(p[16:], symbols)
        with self.assertRaises(ValueError):
            k5ft8.tx_packet(bytes([8]) * 79)
        with self.assertRaises(ValueError):
            k5ft8.tx_packet(symbols, 15001)

    def test_serial_unsolicited_frame(self):
        radio = object.__new__(k5ft8.Radio)
        radio.s = Mock()
        radio.s.read.return_value = (k5ft8.packet(0x21, b"\xB1\0\0\0\0")
                                     + k5ft8.packet(0x21, b"\x02\x02\0\0\0"))
        self.assertEqual(radio.mode(), "FT8")

    def test_late_mode_query_never_transmits(self):
        radio = object.__new__(k5ft8.Radio)
        radio.s, radio.mode = Mock(), Mock(return_value="FT8")
        with patch.object(k5ft8.time, "time", side_effect=[0.4, 0.6]):
            with self.assertRaises(k5ft8.MissedSlot):
                radio.transmit("CQ K1ABC FN31", 0)
        radio.s.write.assert_not_called()

    def test_slow_decode_does_not_spend_extra_windows_or_repeat_tx(self):
        controller = object.__new__(auto.Auto)
        controller.lock, controller.decoding = threading.Lock(), threading.Event()
        controller.chat = None
        controller.web = Control('K1ABC', 'FN31', lambda s: s)
        controller.qso = auto.QSO("K1ABC", "FN31")
        controller.decoding.set()
        self.assertFalse(controller.want_receive(15))
        self.assertFalse(controller.transmit(15))
        controller.decoding.clear()
        self.receive(controller.qso, 0, "CQ W1ABC FN42")
        self.assertFalse(controller.want_receive(30))
        self.assertEqual(controller.qso.due(45), "W1ABC K1ABC FN31")
        controller.qso.transmitted(45)
        self.assertTrue(controller.want_receive(60))

    def test_answer_cq(self):
        q = auto.QSO("K1ABC", "FN31pr")
        self.receive(q, 0, "CQ W1ABC FN42")
        self.assertIsNone(q.due(0))
        self.assertEqual(q.due(15), "W1ABC K1ABC FN31")
        q.transmitted(15)
        self.receive(q, 30, "K1ABC W1ABC -09")
        self.assertEqual(q.due(45), "W1ABC K1ABC R-12")
        q.transmitted(45)
        self.receive(q, 60, "K1ABC W1ABC RR73")
        self.assertFalse(q.completed)
        self.assertEqual(q.due(75), "W1ABC K1ABC 73")
        q.transmitted(75)
        self.assertEqual(len(q.completed), 1)
        self.assertEqual(q.completed[0]["rst_rcvd"], "-09")
        self.assertIn("<RST_SENT:3>-12", auto.adif(q.completed[0], q.call, q.grid, 144174000))

    def test_a_sked_station_is_answered_first(self):
        q = auto.QSO("K1ABC", "FN31")
        q.prefer = {"W2DEF"}
        q.receive(0, [dict(message="CQ W1ABC FN42", snr=0), dict(message="CQ W2DEF FN20", snr=-20)])
        self.assertEqual(q.peer, "W2DEF")

    def test_cq_responder(self):
        q = auto.QSO("K1ABC", "FN31")
        self.receive(q, 0, "K1ABC W1ABC FN42")
        self.assertEqual(q.due(15), "W1ABC K1ABC -12")
        q.transmitted(15)
        self.receive(q, 30, "K1ABC W1ABC R-09")
        self.assertEqual(q.due(45), "W1ABC K1ABC RR73")
        q.transmitted(45)
        self.assertEqual(len(q.completed), 1)

    def test_unrelated_or_incomplete(self):
        q = auto.QSO("K1ABC", "FN31")
        self.receive(q, 0, "K1ABC W1ABC RR73")
        self.assertIsNone(q.pending)
        self.receive(q, 30, "CQ W1ABC FN42")
        self.receive(q, 60, "W2DEF W1ABC -09")
        self.assertEqual(q.due(75), "W1ABC K1ABC FN31")
        q.transmitted(75)
        q.transmitted(105)
        q.transmitted(135)
        self.assertIsNone(q.peer)
        self.assertFalse(q.completed)

    def test_late_decodes_and_timeout(self):
        q = auto.QSO("K1ABC", "FN31")
        self.receive(q, 30, "CQ W1ABC FN42")
        self.receive(q, 0, "CQ W2DEF FN20")
        self.assertEqual(q.peer, "W1ABC")
        self.assertIsNone(q.due(315))

    def test_unsent_report_cannot_complete(self):
        q = auto.QSO("K1ABC", "FN31")
        self.receive(q, 0, "K1ABC W1ABC FN42")
        self.receive(q, 30, "K1ABC W1ABC R-09")
        self.assertFalse(q.finish_after_tx)
        self.assertFalse(q.completed)

    @unittest.skipUnless(os.environ.get("INFLU_FT8_SIM_TEST"), "optional real codec round trip")
    def test_codec_exchange(self):
        q = auto.QSO("K1ABC", "FN31")
        with tempfile.TemporaryDirectory(prefix="influ-qso-") as work:
            for slot, message in [(0, "CQ W1ABC FN42"), (30, "K1ABC W1ABC -09"),
                                  (60, "K1ABC W1ABC RR73")]:
                subprocess.run(["ft8sim", message, "1200", "0", "0", "0", "1", "-12"],
                               cwd=work, capture_output=True, check=True, timeout=10)
                with wave.open(os.path.join(work, "000000_000001.wav")) as w:
                    pcm = np.frombuffer(w.readframes(w.getnframes()), "<i2")
                decodes = auto.ft8.jt9(pcm, slot, work)
                self.assertTrue(any(d["message"] == message for d in decodes), decodes)
                q.receive(slot, decodes)
                response = q.due(slot + 15)
                self.assertIsNotNone(response)
                self.assertEqual(len(k5ft8.encode(response)), 79)
                q.transmitted(slot + 15)
        self.assertEqual(len(q.completed), 1)


if __name__ == "__main__":
    unittest.main()
