import os
import tempfile
import unittest

import aprs_beacon as b


class APRSBeaconTests(unittest.TestCase):
    def test_position_is_to_the_minute_with_the_rest_blank(self):
        self.assertEqual(b.ambiguous(30.6789, 2, "NS"), "3041.  N")
        self.assertEqual(b.ambiguous(-33.9, 2, "NS"), "3354.  S")
        self.assertEqual(b.ambiguous(105.5, 3, "EW"), "10530.  E")
        self.assertEqual(b.ambiguous(-0.999, 3, "EW"), "00100.  W")
        self.assertEqual(b.position("W1ABC-10", 42.0, -71.5, "FT8"),
                         "W1ABC-10>APZINF,TCPIP*:!4200.  N/07130.  W-FT8")

    def test_settings_read_from_the_private_file(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "aprs.env")
            with open(path, "w") as f:
                f.write("# a comment\nAPRS_PASSCODE='123'\nAPRS_SSID=10\n")
            self.assertEqual(b.settings(path), {"APRS_PASSCODE": "123", "APRS_SSID": "10"})
            self.assertEqual(b.settings(os.path.join(d, "missing")), {})


if __name__ == "__main__":
    unittest.main()
