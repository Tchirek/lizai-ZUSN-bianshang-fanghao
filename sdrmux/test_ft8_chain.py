#!/usr/bin/env python3
"""End-to-end check of the FT8 receive chain without touching the radio or the SDR:
ft8sim audio -> USB upconversion into a 2.4 MS/s cu8 recording centred on 144.9 MHz
(signal at 144.174 MHz dial + audio offset, plus noise) -> ft8.decode_recording -> jt9.

usage: INFLU_FT8_DRYRUN=1 test_ft8_chain.py [--snr -12] [--late 0.14]
"""
import argparse
import json
import os
import subprocess
import tempfile
import wave

import numpy as np
from scipy import signal

import ft8

FS = 2_400_000
FC = 144_900_000


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--snr", type=float, default=-12)
    ap.add_argument("--late", type=float, default=0.14, help="recording start after slot start")
    ap.add_argument("--msg", default="CQ K1ABC FN31")
    a = ap.parse_args()
    os.environ["INFLU_FT8_DRYRUN"] = "1"
    d = tempfile.mkdtemp(prefix="ft8chain_")
    subprocess.run(["ft8sim", a.msg, "1200", "0.0", "0.0", "0.0", "1", str(a.snr)], cwd=d,
                   check=True, capture_output=True)
    with wave.open(os.path.join(d, "000000_000001.wav")) as w:
        audio = np.frombuffer(w.readframes(w.getnframes()), "<i2").astype(np.float32)
    audio /= np.abs(audio).max()
    skip = int(a.late * 12_000)                       # recording begins `late` s into the slot
    analytic = signal.hilbert(audio[skip:])
    up = signal.resample_poly(analytic, 200, 1)        # 12 kHz -> 2.4 MS/s
    n = np.arange(len(up))
    iq = up * np.exp(2j * np.pi * (ft8.FT8_DIALS[0] - FC) / FS * n) * 0.3
    iq += (np.random.randn(len(iq)) + 1j * np.random.randn(len(iq))) * 0.05
    cu8 = np.empty(2 * len(iq), np.uint8)
    cu8[0::2] = np.clip(iq.real * 127 + 127.4, 0, 255)
    cu8[1::2] = np.clip(iq.imag * 127 + 127.4, 0, 255)
    path = os.path.join(d, "test.cu8")
    cu8.tofile(path)
    slot = 1_800_000_000.0                             # any multiple of 15 s
    meta = dict(freq=FC, rate=FS, t_start=slot, first_t=slot + a.late)
    res = ft8.decode_recording(path, meta)
    got = [x["message"] for r in res for x in r["decodes"]]
    print(json.dumps(dict(sent=a.msg, snr=a.snr, slots=len(res), decoded=got,
                          detail=[x for r in res for x in r["decodes"]])))
    print("PASS" if a.msg in got else "FAIL")


if __name__ == "__main__":
    main()
