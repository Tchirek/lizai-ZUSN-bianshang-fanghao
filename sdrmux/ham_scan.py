#!/usr/bin/env python3
"""Look at a sdrmux IQ recording: band noise floor, carriers, per-channel activity.

usage: ham_scan.py FILE.cu8 [--chan-khz 12.5] [--fft 4096]
"""
import argparse
import json
import os

import numpy as np


def load(path):
    raw = np.fromfile(path, dtype=np.uint8).astype(np.float32)
    iq = (raw[0::2] - 127.4) + 1j * (raw[1::2] - 127.4)
    return iq / 128.0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("file")
    ap.add_argument("--fft", type=int, default=4096)
    ap.add_argument("--chan-khz", type=float, default=12.5)
    ap.add_argument("--slice-s", type=float, default=0.25)
    a = ap.parse_args()
    meta = json.load(open(os.path.splitext(a.file)[0] + ".json"))
    fc, fs = meta["freq"], meta["rate"]
    iq = load(a.file)
    n = a.fft
    frames = len(iq) // n
    x = iq[: frames * n].reshape(frames, n) * np.hanning(n)
    spec = np.abs(np.fft.fftshift(np.fft.fft(x, axis=1), axes=1)) ** 2
    freqs = fc + np.fft.fftshift(np.fft.fftfreq(n, 1 / fs))
    # ignore the DC spike and the filter roll-off at the band edges
    keep = (np.abs(freqs - fc) > 15e3) & (np.abs(freqs - fc) < 0.45 * fs)
    psd = 10 * np.log10(spec.mean(0) + 1e-20)
    floor = np.median(psd[keep])
    print(f"{os.path.basename(a.file)}: {len(iq) / fs:.2f} s, center {fc / 1e6:.4f} MHz, "
          f"gain {meta.get('gain')}, noise floor {floor:.1f} dB(rel)")
    print(f"   raw |IQ| rms {np.sqrt(np.mean(np.abs(iq) ** 2)):.3f}  "
          f"clip fraction {np.mean((np.abs(iq.real) > 0.98) | (np.abs(iq.imag) > 0.98)):.2e}")

    # channelize: power per channel per time slice
    ch_w = a.chan_khz * 1e3
    chans = np.arange(np.ceil((fc - 0.45 * fs) / ch_w) * ch_w, fc + 0.45 * fs, ch_w)
    per_slice = max(1, int(a.slice_s * fs / n))
    S = spec[: (frames // per_slice) * per_slice].reshape(-1, per_slice, n).mean(1)
    rows = []
    for cf in chans:
        if abs(cf - fc) < 15e3:
            continue
        m = np.abs(freqs - cf) < ch_w / 2 * 0.8
        p = 10 * np.log10(S[:, m].mean(1) + 1e-20) - floor
        rows.append((cf, p.max(), (p > 10).mean(), p.mean()))
    rows.sort(key=lambda r: -r[1])
    print("   strongest channels (dB above floor: peak / mean, share of time >10 dB):")
    for cf, pk, act, mean in rows[:12]:
        print(f"     {cf / 1e6:9.4f} MHz  peak {pk:5.1f}  mean {mean:5.1f}  active {act:5.1%}")
    hot = [r for r in rows if r[1] > 10]
    print(f"   channels with any >10 dB activity: {len(hot)} of {len(rows)}")


if __name__ == "__main__":
    main()
