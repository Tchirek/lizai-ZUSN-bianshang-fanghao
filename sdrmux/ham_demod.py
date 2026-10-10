#!/usr/bin/env python3
"""NBFM-demodulate one channel of a sdrmux IQ recording to a 16 kHz WAV, and describe it.

usage: ham_demod.py FILE.cu8 FREQ_HZ [--out x.wav] [--bw 12500]
"""
import argparse
import json
import os
import wave

import numpy as np
from scipy import signal

import iqtools

FS_IF = 48_000
FS_AUDIO = 16_000


def channel(path, fs, offset, bw=12_500):
    return iqtools.channelize(path, fs, offset, bw, FS_IF)


def fm_demod(x):
    d = np.angle(x[1:] * np.conj(x[:-1]))
    # amateur NBFM uses 6 dB/oct pre-emphasis from ~300 Hz: integrate it back out
    b, a = signal.butter(1, 300, fs=FS_IF, btype="low")
    d = signal.lfilter(b, a, d)
    sos = signal.butter(6, [300, 3000], fs=FS_IF, btype="band", output="sos")
    audio = signal.sosfilt(sos, d)
    return signal.resample_poly(audio, 1, FS_IF // FS_AUDIO)


def describe(x, audio):
    """Cheap features that separate voice from carriers/hum/data."""
    p = np.abs(x) ** 2
    k = FS_IF // 20                                               # 50 ms frames
    env = 10 * np.log10(p[: len(p) // k * k].reshape(-1, k).mean(1) + 1e-20) if len(p) >= k \
        else np.array([0.0])
    f, psd = signal.welch(audio, FS_AUDIO, nperseg=1024)
    band = (f > 300) & (f < 3000)
    flat = np.exp(np.mean(np.log(psd[band] + 1e-20))) / (np.mean(psd[band]) + 1e-20)
    frames = audio[: len(audio) // 320 * 320].reshape(-1, 320)   # 20 ms
    e = 10 * np.log10((frames ** 2).mean(1) + 1e-20)
    return dict(carrier_level_db_sd=round(float(env.std()), 2),
                audio_rms=round(float(np.sqrt(np.mean(audio ** 2))), 4),
                spectral_flatness=round(float(flat), 3),            # ~1 noise, <<1 tonal
                syllabic_mod_db=round(float(np.percentile(e, 90) - np.percentile(e, 10)), 1),
                peak_tone_hz=round(float(f[band][np.argmax(psd[band])]), 0))


def write_wav(path, audio, rate):
    pcm = np.clip(audio / (np.abs(audio).max() + 1e-9) * 0.9 * 32767, -32768, 32767).astype("<i2")
    with wave.open(path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm.tobytes())


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("file")
    ap.add_argument("freq", type=float)
    ap.add_argument("--out")
    ap.add_argument("--bw", type=float, default=12_500)
    a = ap.parse_args()
    meta = json.load(open(os.path.splitext(a.file)[0] + ".json"))
    x = channel(a.file, meta["rate"], a.freq - meta["freq"], a.bw)
    audio = fm_demod(x)
    out = a.out or os.path.splitext(a.file)[0] + f"_{int(a.freq)}.wav"
    write_wav(out, audio, FS_AUDIO)
    print(json.dumps(dict(freq=a.freq, wav=out, **describe(x, audio))))


if __name__ == "__main__":
    main()
