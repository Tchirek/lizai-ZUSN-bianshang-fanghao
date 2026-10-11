#!/usr/bin/env python3
"""Sample HamClock screenshots and check that pane 2 shows the ADS-B scope with a border
identical to pane 1's (grey 139,141,139 at box columns 0/318 and rows 0/294).

usage: check_pane.py [--seconds 120] [--every 2]
"""
import argparse
import io
import time
import urllib.request
from collections import Counter

from PIL import Image

G = (139, 141, 139)


def border(px, x0):
    return (sum(px[x0, y] == G for y in range(295)), sum(px[x0 + 318, y] == G for y in range(295)),
            sum(px[x, 0] == G for x in range(x0, x0 + 319)), sum(px[x, 294] == G for x in range(x0, x0 + 319)))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--seconds", type=float, default=120)
    ap.add_argument("--every", type=float, default=2)
    a = ap.parse_args()
    kinds, bad, n = Counter(), [], 0
    t0 = time.time()
    while time.time() - t0 < a.seconds:
        raw = urllib.request.urlopen("http://127.0.0.1:8070/get_capture.bmp", timeout=20).read()
        px = Image.open(io.BytesIO(raw)).convert("RGB").load()
        title = sum(1 for x in range(812, 880) for y in range(2, 22)
                    if px[x, y][0] > 200 and 100 < px[x, y][1] < 150)
        green = sum(1 for x in range(830, 1110, 4) for y in range(80, 280, 4)
                    if px[x, y][1] > 200 and px[x, y][0] < 60)
        sun = sum(1 for x in range(830, 1110, 4) for y in range(40, 280, 4)
                  if px[x, y][0] > 200 and 100 < px[x, y][1] < 200 and px[x, y][2] < 80)
        kind = "scope" if title > 20 and green < 300 and sun < 300 else \
            ("VOACAP" if green >= 300 else ("SDO" if sun >= 300 else "other"))
        b2, b1 = border(px, 810), border(px, 470)
        kinds[kind] += 1
        n += 1
        if kind != "scope" or b2 != b1:
            bad.append((round(time.time() - t0), kind, b2, b1))
        time.sleep(a.every)
    print(f"{n} samples: {dict(kinds)}; pane2 border == pane1 border in {n - sum(1 for b in bad if b[2] != b[3])}/{n}")
    print("anomalies:", bad[:10] if bad else "none")


if __name__ == "__main__":
    main()
