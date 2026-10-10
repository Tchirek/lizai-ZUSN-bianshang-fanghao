#!/usr/bin/env python3
"""Small self-check: encoder/framing, both QSO roles, retries and incomplete logs."""
import unittest
from unittest.mock import Mock, patch
import os
import subprocess
import tempfile
import wave

import numpy as np

import ft8_auto as auto
import k5ft8


class Tests(unittest.TestCase):
    def receive(self, q, slot, message):
        q.receive(slot, [dict(message=message, snr=-12)])

    def test_encoder_and_framing(self):
        symbols = k5ft8.encode("CQ BG8ASN OM20")
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
                radio.transmit("CQ BG8ASN OM20", 0)
        radio.s.write.assert_not_called()

    def test_answer_cq(self):
        q = auto.QSO("BG8ASN", "OM20tn")
        self.receive(q, 0, "CQ W1ABC FN42")
        self.assertIsNone(q.due(0))
        self.assertEqual(q.due(15), "W1ABC BG8ASN OM20")
        q.transmitted(15)
        self.receive(q, 30, "BG8ASN W1ABC -09")
        self.assertEqual(q.due(45), "W1ABC BG8ASN R-12")
        q.transmitted(45)
        self.receive(q, 60, "BG8ASN W1ABC RR73")
        self.assertFalse(q.completed)
        self.assertEqual(q.due(75), "W1ABC BG8ASN 73")
        q.transmitted(75)
        self.assertEqual(len(q.completed), 1)
        self.assertEqual(q.completed[0]["rst_rcvd"], "-09")
        self.assertIn("<RST_SENT:3>-12", auto.adif(q.completed[0], q.call, q.grid, 144174000))

    def test_cq_responder(self):
        q = auto.QSO("BG8ASN", "OM20")
        self.receive(q, 0, "BG8ASN W1ABC FN42")
        self.assertEqual(q.due(15), "W1ABC BG8ASN -12")
        q.transmitted(15)
        self.receive(q, 30, "BG8ASN W1ABC R-09")
        self.assertEqual(q.due(45), "W1ABC BG8ASN RR73")
        q.transmitted(45)
        self.assertEqual(len(q.completed), 1)

    def test_unrelated_or_incomplete(self):
        q = auto.QSO("BG8ASN", "OM20")
        self.receive(q, 0, "BG8ASN W1ABC RR73")
        self.assertIsNone(q.pending)
        self.receive(q, 30, "CQ W1ABC FN42")
        self.receive(q, 60, "W2DEF W1ABC -09")
        self.assertEqual(q.due(75), "W1ABC BG8ASN OM20")
        q.transmitted(75)
        q.transmitted(105)
        q.transmitted(135)
        self.assertIsNone(q.peer)
        self.assertFalse(q.completed)

    def test_late_decodes_and_timeout(self):
        q = auto.QSO("BG8ASN", "OM20")
        self.receive(q, 30, "CQ W1ABC FN42")
        self.receive(q, 0, "CQ W2DEF FN20")
        self.assertEqual(q.peer, "W1ABC")
        self.assertIsNone(q.due(315))

    def test_unsent_report_cannot_complete(self):
        q = auto.QSO("BG8ASN", "OM20")
        self.receive(q, 0, "BG8ASN W1ABC FN42")
        self.receive(q, 30, "BG8ASN W1ABC R-09")
        self.assertFalse(q.finish_after_tx)
        self.assertFalse(q.completed)

    @unittest.skipUnless(os.environ.get("INFLU_FT8_SIM_TEST"), "optional real codec round trip")
    def test_codec_exchange(self):
        q = auto.QSO("BG8ASN", "OM20")
        with tempfile.TemporaryDirectory(prefix="influ-qso-") as work:
            for slot, message in [(0, "CQ W1ABC FN42"), (30, "BG8ASN W1ABC -09"),
                                  (60, "BG8ASN W1ABC RR73")]:
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
