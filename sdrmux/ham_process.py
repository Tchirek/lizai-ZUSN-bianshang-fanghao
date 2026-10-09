#!/usr/bin/env python3
"""Post-process one listen window: find active channels, demodulate them, keep only what
looks like speech, then delete the raw IQ (96 MB per 20 s).

Writes <base>.summary.json; speech-like channels as <base>_<freq>.wav.
usage: ham_process.py FILE.cu8 [--keep-iq]
"""
import argparse
import json
import os

import numpy as np

import ham_demod

CH = 12_500
SPEECH_MOD_DB = 10.0      # 20 ms energy p90 - p10 in demodulated audio; noise/RFI ~3 dB


def active_channels(iq, fc, fs, n=4096, thresh_db=10.0):
    frames = len(iq) // n
    x = iq[: frames * n].reshape(frames, n) * np.hanning(n)
    spec = np.abs(np.fft.fftshift(np.fft.fft(x, axis=1), axes=1)) ** 2
    freqs = fc + np.fft.fftshift(np.fft.fftfreq(n, 1 / fs))
    keep = (np.abs(freqs - fc) > 15e3) & (np.abs(freqs - fc) < 0.45 * fs)
    floor = np.median(10 * np.log10(spec.mean(0)[keep] + 1e-20))
    per = max(1, int(0.25 * fs / n))
    S = spec[: frames // per * per].reshape(-1, per, n).mean(1)
    out = []
    for cf in np.arange(np.ceil((fc - 0.45 * fs) / CH) * CH, fc + 0.45 * fs, CH):
        if abs(cf - fc) < 15e3:
            continue
        m = np.abs(freqs - cf) < CH * 0.4
        p = 10 * np.log10(S[:, m].mean(1) + 1e-20) - floor
        if p.max() > thresh_db:
            out.append(dict(freq=int(cf), peak_db=round(float(p.max()), 1),
                            active=round(float((p > thresh_db).mean()), 3)))
    return floor, out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("file")
    ap.add_argument("--keep-iq", action="store_true")
    a = ap.parse_args()
    base = os.path.splitext(a.file)[0]
    meta = json.load(open(base + ".json"))
    iq = ham_demod.load(a.file)
    fc, fs = meta["freq"], meta["rate"]
    floor, chans = active_channels(iq, fc, fs)
    results = []
    for c in sorted(chans, key=lambda c: -c["peak_db"])[:20]:
        x = ham_demod.channel(iq, fs, c["freq"] - fc)
        audio = ham_demod.fm_demod(x)
        feat = ham_demod.describe(x, audio)
        c.update(feat)
        c["speech_like"] = feat["syllabic_mod_db"] >= SPEECH_MOD_DB
        if c["speech_like"]:
            wav = f"{base}_{c['freq']}.wav"
            import wave
            pcm = np.clip(audio / (np.abs(audio).max() + 1e-9) * 0.9 * 32767, -32768,
                          32767).astype("<i2")
            with wave.open(wav, "wb") as w:
                w.setnchannels(1)
                w.setsampwidth(2)
                w.setframerate(ham_demod.FS_AUDIO)
                w.writeframes(pcm.tobytes())
            c["wav"] = wav
        results.append(c)
    summary = dict(meta, floor_db=round(float(floor), 1), seconds=len(iq) / fs,
                   active=results, speech_channels=[c["freq"] for c in results if c["speech_like"]])
    with open(base + ".summary.json", "w") as f:
        json.dump(summary, f)
    if not a.keep_iq:
        os.remove(a.file)
    print(json.dumps(dict(file=os.path.basename(a.file), active=len(results),
                          speech=summary["speech_channels"])))


if __name__ == "__main__":
    main()
