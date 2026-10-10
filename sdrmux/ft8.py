#!/usr/bin/env python3
"""FT8 receive path for sdrmux recordings.

For every FT8 dial frequency inside a recording, USB-demodulate 0-3 kHz above the dial to
12 kHz audio, cut it into the 15 s UTC slots the recording fully covers, decode each with
WSJT-X's jt9, log the decodes and forward them to the local WSJT-X UDP listener (the
HamClock bridge) as Status + Decode messages, exactly like WSJT-X itself would.

usage: ft8.py FILE.cu8        (decode one recording, print decodes)
"""
import json
import os
import re
import shutil
import socket
import struct
import subprocess
import sys
import tempfile
import time
import wave

import numpy as np

import iqtools

FT8_DIALS = [144_174_000, 144_460_000]
FS_AUDIO = 12_000
SLOT = 15.0
STATION = os.path.expanduser("~/influ/station.json")
LOGDIR = os.path.expanduser("~/influ/ft8")
UDP = ("127.0.0.1", 2237)
CLIENT_ID = "influ-sdrmux"
LINE = re.compile(r"^(\d{6})\s+(-?\d+)\s+(-?[\d.]+)\s+(\d+)\s+~\s+(.*?)\s*$")
BLANK_S = 4 * 65536 / 2 / 2_400_000      # sdrmux zero-fills 4 buffers after a retune


def station():
    try:
        return json.load(open(STATION))
    except (OSError, ValueError):
        return {"call": "", "grid": ""}


def usb_audio(path, meta, dial):
    """Complex mix so that dial+1500 Hz sits at 0, keep +-1500 Hz, shift back: USB 0-3 kHz."""
    x = iqtools.channelize(path, meta["rate"], dial + 1500 - meta["freq"], 3000, FS_AUDIO)
    n = np.arange(len(x))
    return (x * np.exp(2j * np.pi * 1500 / FS_AUDIO * n)).real.astype(np.float32)


def slots(audio, t_first):
    """Yield (slot_start, 15 s int16 array) for every slot whose 0.2-13.6 s span is covered."""
    t_last = t_first + len(audio) / FS_AUDIO
    s = np.floor(t_first / SLOT) * SLOT
    while s + 13.6 <= t_last:
        if t_first <= s + 0.2:
            buf = np.zeros(int(SLOT * FS_AUDIO), np.float32)
            i0 = int(round((t_first - s) * FS_AUDIO))        # >= 0: recording starts after s
            seg = audio[max(0, -i0): max(0, -i0) + len(buf) - max(i0, 0)]
            buf[max(i0, 0): max(i0, 0) + len(seg)] = seg
            rms = np.sqrt(np.mean(seg ** 2)) + 1e-12
            yield s, np.clip(buf / rms * 1000, -32768, 32767).astype("<i2")
        s += SLOT


def jt9(pcm, slot_start, workdir):
    name = time.strftime("%y%m%d_%H%M%S", time.gmtime(slot_start)) + ".wav"
    p = os.path.join(workdir, name)
    with wave.open(p, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(FS_AUDIO)
        w.writeframes(pcm.tobytes())
    st = station()
    cmd = ["jt9", "-8", "-p", "15", "-d", "3", "-a", workdir, "-t", workdir]
    if st.get("call"):
        cmd += ["-c", st["call"], "-G", st.get("grid", "")[:4]]
    out = subprocess.run(cmd + [p], capture_output=True, text=True, cwd=workdir, timeout=120).stdout
    res = []
    for line in out.splitlines():
        m = LINE.match(line)
        if m:
            res.append(dict(snr=int(m.group(2)), dt=float(m.group(3)), df=int(m.group(4)),
                            message=m.group(5)))
    return res


def decode_recording(path, meta):
    t_first = meta.get("first_t") or (meta["t_start"] + meta.get("switch_out_ms", 85) / 1000
                                      + BLANK_S)
    lo, hi = meta["freq"] - 0.45 * meta["rate"], meta["freq"] + 0.45 * meta["rate"]
    out = []
    work = tempfile.mkdtemp(prefix="ft8_")
    try:
        for dial in FT8_DIALS:
            if not lo < dial < hi - 3000:
                continue
            audio = usb_audio(path, meta, dial)
            for s, pcm in slots(audio, t_first):
                dec = jt9(pcm, s, work)
                for d in dec:
                    d["rf_hz"] = dial + d["df"]
                rec = dict(dial=dial, slot=s, decodes=dec)
                out.append(rec)
                publish(rec)
    finally:
        shutil.rmtree(work, ignore_errors=True)
    return out


# ------------------------------------------------------- log + WSJT-X UDP ----

def _utf8(s):
    b = s.encode()
    return struct.pack(">I", len(b)) + b


def _header(msg_type):
    return struct.pack(">III", 0xADBCCBDA, 2, msg_type) + _utf8(CLIENT_ID)


def publish(rec):
    if os.environ.get("INFLU_FT8_DRYRUN"):      # tests: never log or spot synthetic signals
        return
    os.makedirs(LOGDIR, exist_ok=True)
    day = time.strftime("%Y%m%d", time.gmtime(rec["slot"]))
    with open(os.path.join(LOGDIR, f"decodes-{day}.jsonl"), "a") as f:
        for d in rec["decodes"]:
            f.write(json.dumps(dict(slot=rec["slot"], dial=rec["dial"], **d)) + "\n")
    if not rec["decodes"]:
        return
    st = station()
    status = (_header(1) + struct.pack(">Q", rec["dial"]) + _utf8("FT8") + _utf8("") + _utf8("")
              + _utf8("FT8") + struct.pack(">???", False, False, False) + struct.pack(">II", 1500, 1500)
              + _utf8(st.get("call", "")) + _utf8(st.get("grid", "")) + _utf8(""))
    ms = int((rec["slot"] % 86400) * 1000)
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.sendto(status, UDP)
        for d in rec["decodes"]:
            pkt = (_header(2) + struct.pack(">?Iid I", True, ms, d["snr"], d["dt"], d["df"])
                   + _utf8("~") + _utf8(d["message"]) + struct.pack(">??", False, False))
            s.sendto(pkt, UDP)


if __name__ == "__main__":
    f = sys.argv[1]
    meta = json.load(open(os.path.splitext(f)[0] + ".json"))
    for r in decode_recording(f, meta):
        print(time.strftime("%H:%M:%S", time.gmtime(r["slot"])), r["dial"], r["decodes"])
