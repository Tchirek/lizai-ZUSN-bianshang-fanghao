import json
import os
import struct
import tempfile
import unittest

import pskreport as p


class PSKReportTests(unittest.TestCase):
    def test_sender_of_each_kind_of_message(self):
        self.assertEqual(p.sender("CQ K1ABC FN42"), ("K1ABC", "FN42"))
        self.assertEqual(p.sender("CQ DX K1ABC FN42"), ("K1ABC", "FN42"))
        self.assertEqual(p.sender("W1ABC K1ABC FN42"), ("K1ABC", "FN42"))
        self.assertEqual(p.sender("W1ABC K1ABC -12"), ("K1ABC", ""))
        self.assertEqual(p.sender("W1ABC K1ABC RR73"), ("K1ABC", ""))
        self.assertIsNone(p.sender("<...> K1ABC -12".replace("K1ABC", "<K1ABC>")))
        self.assertIsNone(p.sender("TNX 73 GL"))

    def test_spots_are_new_one_a_sender_and_never_ourselves(self):
        with tempfile.TemporaryDirectory() as d:
            now = 1_800_000_000.0
            day = __import__("time").strftime("%Y%m%d", __import__("time").gmtime(now))
            rows = [dict(slot=now - 600, dial=144174000, df=900, snr=-5, message="CQ K1ABC FN42"),
                    dict(slot=now - 300, dial=144174000, df=905, snr=-1, message="W1ABC K1ABC -12"),
                    dict(slot=now - 200, dial=144174000, df=1200, snr=9, message="CQ W1ABC FN31"),
                    dict(slot=now - 2000, dial=144174000, df=700, snr=0, message="CQ K2OLD FN20")]
            with open(os.path.join(d, f"decodes-{day}.jsonl"), "w") as f:
                f.write("\n".join(json.dumps(r) for r in rows) + "\n")
            spots = p.spots_since(now - 900, "W1ABC", logdir=d, now=now)
        self.assertEqual([(s["call"], s["grid"], s["snr"], s["hz"]) for s in spots],
                         [("K1ABC", "FN42", -1, 144174905)])

    def test_packet_is_ipfix_with_both_templates_and_records(self):
        spots = [dict(call="K1ABC", grid="FN42", hz=144174905, snr=-7, t=1_800_000_000)]
        data = p.packet(dict(call="W1ABC", grid="FN31pr"), spots, 5, 0x1234, 1_800_000_100)
        version, length, _, sequence, domain = struct.unpack(">HHIII", data[:16])
        self.assertEqual((version, length, sequence, domain), (10, len(data), 5, 0x1234))
        sets, at = [], 16
        while at < len(data):
            set_id, size = struct.unpack(">HH", data[at:at + 4])
            self.assertEqual(size % 4, 0)
            sets.append((set_id, data[at + 4:at + size]))
            at += size
        self.assertEqual([s for s, _ in sets], [3, 2, p.RECEIVER_ID, p.SENDER_ID])
        receiver = sets[2][1]
        self.assertEqual(receiver[:6], b"\x05W1ABC")
        record = sets[3][1]
        self.assertEqual(record[:6], b"\x05K1ABC")
        self.assertEqual(struct.unpack(">Ib", record[6:11]), (144174905, -7))
        self.assertEqual(record[11:15], b"\x03FT8")
        self.assertEqual(record[15:20], b"\x04FN42")


if __name__ == "__main__":
    unittest.main()
