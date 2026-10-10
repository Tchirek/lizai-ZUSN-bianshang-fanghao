#!/usr/bin/env python3
"""Post-process one listen window, then delete the raw IQ (~96 MB per 20 s):
  * FT8: decode every FT8 dial frequency inside the recording (ft8.py)
  * FM: find active 12.5 kHz channels, demodulate the strongest, keep speech candidates

Writes <base>.summary.json; speech candidates as <base>_<freq>.wav.
Runs one job at a time (flock), so a burst of windows cannot exhaust memory.
usage: ham_process.py FILE.cu8 [--keep-iq]
"""
import argparse
import fcntl
import json
import os

import numpy as np

import ft8
import ham_demod
import iqtools

CH = 12_500
SPEECH_MOD_DB = 10.0      # 20 ms energy p90 - p10; noise/RFI ~3 dB. Candidates only: the
                          # 145.025 MHz clanging of 2026-10-10 also passed this test.
MAX_FM_CHANNELS = 8
KNOWN_RFI = os.path.expanduser("~/influ/sched/known_rfi.json")   # [freq_hz, ...]
LOCK = os.path.expanduser("~/influ/sched/ham_process.lock")


def active_channels(path, fc, fs, thresh_db=10.0):
    freqs, S = iqtools.spectrogram(path, fs)
    keep = (np.abs(freqs) > 15e3) & (np.abs(freqs) < 0.45 * fs)
    floor = np.median(10 * np.log10(S.mean(0)[keep] + 1e-20))
    out = []
    for cf in np.arange(np.ceil((fc - 0.45 * fs) / CH) * CH, fc + 0.45 * fs, CH):
        if abs(cf - fc) < 15e3:
            continue
        m = np.abs(freqs - (cf - fc)) < CH * 0.4
        p = 10 * np.log10(S[:, m].mean(1) + 1e-20) - floor
        if p.max() > thresh_db:
            out.append(dict(freq=int(cf), peak_db=round(float(p.max()), 1),
                            active=round(float((p > thresh_db).mean()), 3)))
    return float(floor), out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("file")
    ap.add_argument("--keep-iq", action="store_true")
    ap.add_argument("--skip-ft8", action="store_true", help="scheduler already decoded and published FT8")
    a = ap.parse_args()
    os.makedirs(os.path.dirname(LOCK), exist_ok=True)
    with open(LOCK, "w") as lk:
        fcntl.flock(lk, fcntl.LOCK_EX)
        process(a)


def process(a):
    if not os.path.exists(a.file):
        return
    base = os.path.splitext(a.file)[0]
    meta = json.load(open(base + ".json"))
    fc, fs = meta["freq"], meta["rate"]
    summary = dict(meta, seconds=iqtools.n_samples(a.file) / fs)

    summary["ft8"] = [] if a.skip_ft8 else ft8.decode_recording(a.file, meta)

    rfi = set(json.load(open(KNOWN_RFI))) if os.path.exists(KNOWN_RFI) else set()
    floor, chans = active_channels(a.file, fc, fs)
    summary["floor_db"] = round(floor, 1)
    results = []
    cand = [c for c in chans if c["freq"] not in rfi]
    for c in sorted(cand, key=lambda c: -c["peak_db"])[:MAX_FM_CHANNELS]:
        x = ham_demod.channel(a.file, fs, c["freq"] - fc)
        audio = ham_demod.fm_demod(x)
        c.update(ham_demod.describe(x, audio))
        c["speech_candidate"] = c["syllabic_mod_db"] >= SPEECH_MOD_DB
        if c["speech_candidate"]:
            c["wav"] = f"{base}_{c['freq']}.wav"
            ham_demod.write_wav(c["wav"], audio, ham_demod.FS_AUDIO)
        results.append(c)
    summary["active"] = results
    summary["active_total"] = len(chans)
    summary["speech_candidates"] = [c["freq"] for c in results if c["speech_candidate"]]
    with open(base + ".summary.json", "w") as f:
        json.dump(summary, f)
    if not a.keep_iq:
        os.remove(a.file)
    print(json.dumps(dict(file=os.path.basename(a.file), active=len(chans),
                          speech_candidates=summary["speech_candidates"],
                          ft8=[d["message"] for f in summary["ft8"] for d in f["decodes"]])))


if __name__ == "__main__":
    main()
