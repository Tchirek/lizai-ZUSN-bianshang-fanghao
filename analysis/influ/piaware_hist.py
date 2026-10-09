"""Historical activity from /var/log/piaware.log* (available since 2026-09-06).

Extracted series:
  sent5   every 5 min: reports forwarded to FlightAware in the last 5 min (activity proxy)
  mlat15  every 15 min: aircraft tracked by mlat-client ("Aircraft: a of B Mode S, c of D ADS-B")
  quiet   piaware watchdog lines "no new messages received in N seconds" -> exact long silences
  down    decoder not listening (outage) timestamps
"""
import glob
import gzip
import re

import pandas as pd

LINE = re.compile(r"^(\S+) \S+ piaware\[\d+\]: (.*)$")
SENT = re.compile(r"msgs recv'd from dump1090(?:-fa)? \((\d+) in last (\d+)m\); (\d+) msgs sent")
MLAT_AC = re.compile(r"Aircraft: (\d+) of (\d+) Mode S, (\d+) of (\d+) ADS-B used")
MLAT_RX = re.compile(r"Receiver:\s+([\d.]+) msg/s received\s+([\d.]+) msg/s processed")
QUIET = re.compile(r"no new messages received in (\d+) seconds")
DOWN = re.compile(r"no ADS-B data program (?:seen listening|is serving)")


def parse(paths=None):
    paths = paths or sorted(glob.glob("/var/log/piaware.log*"), reverse=True)
    sent, ac, rx, quiet, down = [], [], [], [], []
    for p in paths:
        op = gzip.open if p.endswith(".gz") else open
        with op(p, "rt", errors="replace") as f:
            for line in f:
                m = LINE.match(line)
                if not m:
                    continue
                t = pd.Timestamp(m.group(1)).timestamp()
                msg = m.group(2)
                if (s := SENT.search(msg)):
                    sent.append((t, int(s.group(1)), int(s.group(2))))
                elif (s := MLAT_AC.search(msg)):
                    ac.append((t, *map(int, s.groups())))
                elif (s := MLAT_RX.search(msg)):
                    rx.append((t, float(s.group(1))))
                elif (s := QUIET.search(msg)):
                    quiet.append((t, int(s.group(1))))
                elif DOWN.search(msg):
                    down.append(t)
    return dict(
        sent5=pd.DataFrame(sent, columns=["t", "n", "mins"]).drop_duplicates("t").sort_values("t"),
        mlat15=pd.DataFrame(ac, columns=["t", "ms_used", "ms_total", "adsb_used", "adsb_total"]
                            ).drop_duplicates("t").sort_values("t"),
        rx=pd.DataFrame(rx, columns=["t", "msg_s"]).drop_duplicates("t").sort_values("t"),
        quiet=pd.DataFrame(quiet, columns=["t", "secs"]).drop_duplicates("t").sort_values("t"),
        down=pd.Series(sorted(set(down)), dtype=float),
    )


def healthy_bins(h, bin_s=300):
    """5-min bins where the decoder was demonstrably up (a 'sent' line exists and no
    'not listening' complaint within the bin)."""
    s = h["sent5"].copy()
    s["bin"] = (s.t // bin_s) * bin_s
    d = (h["down"] // bin_s) * bin_s
    s = s[~s.bin.isin(set(d)) & ~s.bin.isin(set(d - bin_s))]
    return s
