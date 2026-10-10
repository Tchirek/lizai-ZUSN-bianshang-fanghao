#!/usr/bin/env python3
"""One real CQ, recorded locally to verify the complete FT8 TX/RX path.

Retains IQ and measurements under ~/influ/radio. Self-reception is never spotted.
Requires the radio antenna separated from the SDR, or an attenuated dummy load.
"""
import json
import os
import sys
import threading
import time

import numpy as np

import k5ft8

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "sdrmux"))
import ft8
import iqtools
import sdrmuxctl


def main():
    root = os.path.expanduser("~/influ/radio")
    st = ft8.station()
    message = f"CQ {st['call']} {st['grid'][:4]}"
    radio = k5ft8.Radio()
    radio.configure(k5ft8.DIALS[0])
    slot = (int(time.time() / 15) + 1) * 15
    result = {}

    def record():
        k5ft8.wait_until(slot - 0.1)
        result.update(sdrmuxctl.call(dict(cmd="listen", freq=144_900_000,
                                         seconds=14.2, gain=0, tag="ft8-calibration")))

    worker = threading.Thread(target=record)
    worker.start()
    try:
        sent = radio.transmit(message, slot)
    finally:
        worker.join(timeout=20)
        radio.close()
    if not result.get("ok"):
        raise RuntimeError(result)
    path = result["file"]
    meta = json.load(open(os.path.splitext(path)[0] + ".json"))
    os.environ["INFLU_FT8_DRYRUN"] = "1"
    decoded = ft8.decode_recording(path, meta)
    # Phase slope in the centre of each symbol measures actual tone spacing.
    x = iqtools.channelize(path, meta["rate"], k5ft8.DIALS[0] + 1500 - meta["freq"],
                           10_000, 12_000)
    t_first = meta["first_t"]
    measurements = []
    for i, tone in enumerate(sent["tones"]):
        start = slot + 0.50 + i * 0.160 + 0.045
        a, b = int((start - t_first) * 12000), int((start + 0.07 - t_first) * 12000)
        seg = x[max(0, a):max(0, b)]
        if len(seg) < 500:
            continue
        f = np.angle(np.vdot(seg[:-1], seg[1:])) / (2 * np.pi) * 12000 + 1500
        measurements.append(dict(symbol=i, tone=tone, hz=float(f)))
    frequencies = [float(np.median([v["hz"] for v in measurements if v["tone"] == t]))
                   for t in range(8)]
    found = [d for rec in decoded for d in rec["decodes"] if d["message"] == message]
    report = dict(sent=sent, window=result, decoded=decoded, frequencies_hz=frequencies,
                  spacings_hz=np.diff(frequencies).tolist(), pass_decode=bool(found),
                  measured_at=time.time())
    out = os.path.join(root, "calibration.json")
    with open(out, "w") as f:
        json.dump(report, f, indent=2)
    print(json.dumps(report, indent=2))
    if not found:
        raise SystemExit("real FT8 did not decode; auto TX must remain disabled")


if __name__ == "__main__":
    main()
