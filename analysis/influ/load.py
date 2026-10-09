"""Readers for collector output (~/influ/data/<stream>/YYYYMMDD/HH.<ext>[.gz])."""
import glob
import gzip
import json
import os

import numpy as np
import pandas as pd

DATA = os.path.expanduser("~/influ/data")


def _files(stream, t0=None, t1=None, data=DATA):
    out = []
    for p in sorted(glob.glob(os.path.join(data, stream, "*", "*"))):
        day = os.path.basename(os.path.dirname(p))
        hour = os.path.basename(p).split(".")[0]
        ts = pd.Timestamp(f"{day}T{hour}:00:00Z").timestamp()
        if t0 is not None and ts + 3600 < t0:
            continue
        if t1 is not None and ts > t1:
            continue
        out.append(p)
    return out


def _lines(path):
    op = gzip.open if path.endswith(".gz") else open
    try:
        with op(path, "rt", encoding="utf-8") as f:
            for line in f:
                yield line
    except (EOFError, OSError):
        return  # truncated tail of a file still being written / crash


def beast(t0=None, t1=None, data=DATA):
    """All Beast frames. Columns: t, ts12, sig, raw(bytes hex), df, icao.

    Also returns connection gaps (CONNECT/DISCONNECT markers) as a second frame.
    """
    rows, marks = [], []
    for p in _files("beast", t0, t1, data):
        for line in _lines(p):
            parts = line.rstrip("\n").split("\t")
            if len(parts) < 4:
                continue
            t = float(parts[0])
            if parts[1] in ("CONNECT", "DISCONNECT"):
                marks.append((t, parts[1]))
                continue
            rows.append((t, int(parts[1], 16), int(parts[2]), parts[3]))
    df = pd.DataFrame(rows, columns=["t", "ts12", "sig", "raw"])
    if t0 is not None:
        df = df[df.t >= t0]
    if t1 is not None:
        df = df[df.t < t1]
    df = df.reset_index(drop=True)
    n = df.raw.str.len()
    df["kind"] = np.where(n == 4, "modeac", np.where(n == 14, "short", "long"))
    df["df"] = -1
    df["icao"] = None
    ms = df.kind != "modeac"
    df.loc[ms, "df"] = df.loc[ms, "raw"].map(lambda r: int(r[:2], 16) >> 3)
    df.loc[ms, "icao"] = df.loc[ms, "raw"].map(_icao)
    return df, pd.DataFrame(marks, columns=["t", "event"])


_CRC_TABLE = []
for _i in range(256):
    _c = _i << 16
    for _ in range(8):
        _c = ((_c << 1) ^ 0xFFF409) if _c & 0x800000 else (_c << 1)
    _CRC_TABLE.append(_c & 0xFFFFFF)


def _crc24(data):
    c = 0
    for b in data:
        c = ((c << 8) & 0xFFFFFF) ^ _CRC_TABLE[((c >> 16) ^ b) & 0xFF]
    return c


def _icao(raw):
    """Aircraft address: AA field for DF11/17/18, CRC xor parity for address/parity DFs."""
    b = bytes.fromhex(raw)
    df = b[0] >> 3
    if df in (11, 17, 18):
        return b[1:4].hex()
    if df in (0, 4, 5, 16, 20, 21):
        p = int.from_bytes(b[-3:], "big")
        return f"{_crc24(b[:-3]) ^ p:06x}"
    return None


def aircraft(t0=None, t1=None, data=DATA, every=1):
    """aircraft.json snapshots flattened to one row per (snapshot, aircraft)."""
    rows = []
    k = 0
    for p in _files("aircraft", t0, t1, data):
        for line in _lines(p):
            try:
                d = json.loads(line)
            except ValueError:
                continue
            now = d["now"]
            if (t0 is not None and now < t0) or (t1 is not None and now >= t1):
                continue
            k += 1
            if k % every:
                continue
            for a in d.get("aircraft", []):
                rows.append((
                    now, a["hex"].lower(), a.get("seen"), a.get("seen_pos"), a.get("messages"),
                    a.get("lat"), a.get("lon"), a.get("alt_baro"), a.get("gs"), a.get("track"),
                    a.get("rssi"), a.get("flight", "").strip() or None,
                    bool(a.get("mlat")), a.get("category"),
                ))
    cols = ["now", "hex", "seen", "seen_pos", "messages", "lat", "lon", "alt", "gs", "track",
            "rssi", "flight", "mlat", "category"]
    df = pd.DataFrame(rows, columns=cols)
    df["alt"] = pd.to_numeric(df["alt"], errors="coerce")  # "ground" -> NaN
    return df


def stats(t0=None, t1=None, data=DATA):
    rows = []
    for p in _files("stats", t0, t1, data):
        for line in _lines(p):
            try:
                d = json.loads(line)
            except ValueError:
                continue
            b = d["last1min"]
            loc = b.get("local", {})
            acc = loc.get("accepted") or [0]
            rows.append(dict(
                t=b.get("end"), messages=b.get("messages"), accepted=sum(acc),
                signal=loc.get("signal"), noise=loc.get("noise"),
                peak=loc.get("peak_signal"), strong=loc.get("strong_signals"),
                gain=loc.get("gain_db"), dropped=loc.get("samples_dropped"),
                processed=loc.get("samples_processed"),
                tracks=b.get("tracks", {}).get("all"),
                cpr_air=b.get("cpr", {}).get("airborne"),
            ))
    return pd.DataFrame(rows)


def cloud(t0=None, t1=None, data=DATA):
    """Reference positions from other networks. t_pos = snapshot time - seen_pos."""
    rows = []
    for p in _files("cloud", t0, t1, data):
        for line in _lines(p):
            try:
                d = json.loads(line)
            except ValueError:
                continue
            if "ac" not in d:
                continue
            now = (d.get("now") or d["t"] * 1000) / 1000.0
            for a in d["ac"]:
                if a.get("lat") is None:
                    continue
                rows.append((
                    d["src"], d["t"], now - (a.get("seen_pos") or 0), a["hex"].lower().lstrip("~"),
                    a["lat"], a["lon"], a.get("alt_baro"), a.get("gs"), a.get("track"),
                    (a.get("flight") or "").strip() or None, a.get("t"), a.get("type"),
                ))
    cols = ["src", "fetch_t", "t", "hex", "lat", "lon", "alt", "gs", "track", "flight",
            "actype", "msgtype"]
    df = pd.DataFrame(rows, columns=cols)
    df["alt"] = pd.to_numeric(df["alt"], errors="coerce")
    if t0 is not None:
        df = df[df.t >= t0]
    if t1 is not None:
        df = df[df.t < t1]
    # the two sources overlap heavily; keep one row per (hex, rounded second)
    df = df.sort_values("t").assign(ts=df.t.round(0))
    df = df.drop_duplicates(["hex", "ts"]).drop(columns="ts")
    return df.reset_index(drop=True)


def switch_windows(ham_dir=os.path.expanduser("~/influ/ham")):
    """Real sdrmux listen windows as (start, end) - the receiver was blind on 1090 MHz."""
    out = []
    for p in glob.glob(os.path.join(ham_dir, "*.json")):
        try:
            d = json.load(open(p))
            out.append((d["t_start"], d["t_start"] + d["off_1090_s"]))
        except (ValueError, KeyError, OSError):
            continue
    return sorted(out)


def weather(t0=None, t1=None, data=DATA):
    rows = []
    for p in _files("weather", t0, t1, data):
        for line in _lines(p):
            try:
                d = json.loads(line)
            except ValueError:
                continue
            c = d.get("current") or {}
            c = dict(c)
            c["t"] = d["t"]
            rows.append(c)
    return pd.DataFrame(rows)
