"""Chunked processing of sdrmux .cu8 recordings (2.4 MS/s, ~4.8 MB/s) in bounded memory."""
import numpy as np
from scipy import signal

CHUNK = 1_200_000          # samples per chunk (0.5 s, ~10 MB as complex64)


def iter_iq(path, chunk=CHUNK):
    with open(path, "rb") as f:
        while True:
            raw = np.frombuffer(f.read(chunk * 2), dtype=np.uint8)
            if raw.size < 2:
                return
            raw = raw[: raw.size // 2 * 2].astype(np.float32)
            yield ((raw[0::2] - 127.4) + 1j * (raw[1::2] - 127.4)).astype(np.complex64) / 128.0


def n_samples(path):
    import os
    return os.path.getsize(path) // 2


def channelize(path, fs, offset, bw, out_rate):
    """Shift `offset` Hz to 0, low-pass to +-bw/2, decimate to out_rate. Phase-continuous
    across chunks; returns complex64 at out_rate."""
    dec = int(round(fs / out_rate))
    assert CHUNK % dec == 0
    sos = signal.butter(8, bw / 2, fs=fs, output="sos")
    zi = np.zeros((sos.shape[0], 2), dtype=np.complex128)
    out = []
    n0 = 0
    w = -2j * np.pi * offset / fs
    for x in iter_iq(path):
        n = np.arange(n0, n0 + len(x), dtype=np.float64)
        y = x * np.exp(w * n).astype(np.complex64)
        y, zi = signal.sosfilt(sos, y, zi=zi)
        out.append(y[(-n0) % dec::dec].astype(np.complex64))
        n0 += len(x)
    return np.concatenate(out) if out else np.zeros(0, np.complex64)


def spectrogram(path, fs, n=4096, slice_s=0.25):
    """Mean power spectrum per time slice (fftshifted), plus bin frequencies (offsets)."""
    per = max(1, int(slice_s * fs / n))
    win = np.hanning(n).astype(np.float32)
    rows, acc, k = [], np.zeros(n), 0
    tail = np.zeros(0, np.complex64)
    for x in iter_iq(path):
        x = np.concatenate([tail, x])
        m = len(x) // n
        tail = x[m * n:]
        if m == 0:
            continue
        p = np.abs(np.fft.fftshift(np.fft.fft(x[: m * n].reshape(m, n) * win, axis=1),
                                   axes=1)) ** 2
        for row in p:
            acc += row
            k += 1
            if k == per:
                rows.append(acc / per)
                acc, k = np.zeros(n), 0
    freqs = np.fft.fftshift(np.fft.fftfreq(n, 1 / fs))
    return freqs, np.array(rows)
