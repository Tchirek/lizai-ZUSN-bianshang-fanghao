#!/usr/bin/env python3
"""influ data collector - read-only recorder for ADS-B time-share modeling.

Records, into hourly files under DATA_DIR/<stream>/YYYYMMDD/HH.<ext>:
  beast    every Beast frame from dump1090 (wall time, 12 MHz ts, signal, payload)
  aircraft every aircraft.json snapshot (one JSON line per refresh)
  stats    stats.json "last1min" block once a minute (receiver health)
  cloud    adsb.lol / adsb.fi point queries (reference tracks from other receivers)
  weather  Open-Meteo current conditions every 15 min

Never touches the SDR, the decoder config or the feeders. stdlib only.
Finished hours are gzip-compressed in the background.
"""
import argparse
import gzip
import json
import logging
import os
import shutil
import socket
import threading
import time
import urllib.request
from datetime import datetime, timezone

STATION = os.path.expanduser("~/influ/station.json")


def station_point():
    """The station rounded to 0.01 deg (~1 km): it goes out in queries to public APIs."""
    with open(STATION) as f:
        s = json.load(f)
    return round(s["lat"], 2), round(s["lon"], 2)


LAT, LON = station_point()
CLOUD_RADIUS_NM = 250
UA = "influ-collector/1.0 (personal ADS-B feeder research)"

log = logging.getLogger("influ")
stop = threading.Event()
cloud_lock = threading.Lock()
cloud_latest = {}


class HourlyWriter:
    """Append lines to DATA_DIR/<stream>/YYYYMMDD/HH.<ext>; gzip finished hours."""

    def __init__(self, root, stream, ext):
        self.dir = os.path.join(root, stream)
        self.ext = ext
        self.lock = threading.Lock()
        self.key = None
        self.fh = None
        self.last_flush = 0.0

    def _path(self, key):
        day, hour = key
        return os.path.join(self.dir, day, f"{hour}.{self.ext}")

    def write(self, line, t=None):
        t = time.time() if t is None else t
        dt = datetime.fromtimestamp(t, timezone.utc)
        key = (dt.strftime("%Y%m%d"), dt.strftime("%H"))
        with self.lock:
            if key != self.key:
                self._rotate(key)
            self.fh.write(line)
            now = time.monotonic()
            if now - self.last_flush > 5:
                self.fh.flush()
                self.last_flush = now

    def _rotate(self, key):
        if self.fh:
            self.fh.close()
            old = self._path(self.key)
            threading.Thread(target=compress, args=(old,), daemon=True).start()
        path = self._path(key)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        self.fh = open(path, "a", encoding="utf-8")
        self.key = key

    def close(self):
        with self.lock:
            if self.fh:
                self.fh.close()


def compress(path):
    if not os.path.exists(path):
        return
    try:
        with open(path, "rb") as src, gzip.open(path + ".gz", "ab", compresslevel=6) as dst:
            shutil.copyfileobj(src, dst)
        os.remove(path)
    except Exception:
        log.exception("compress %s", path)


def compress_leftovers(root):
    """gzip any plain hourly files from previous runs except the current hour."""
    cur = datetime.now(timezone.utc).strftime("%Y%m%d/%H")
    for dirpath, _, files in os.walk(root):
        for f in files:
            if f.endswith((".tsv", ".jsonl")):
                p = os.path.join(dirpath, f)
                rel = os.path.relpath(p, root).replace(os.sep, "/")
                if cur not in rel:
                    compress(p)


# ---------------------------------------------------------------- Beast ----

BEAST_LEN = {0x31: 2, 0x32: 7, 0x33: 14}  # payload bytes for type '1','2','3'


def beast_frames(sock):
    """Yield (type, ts, signal, payload) from a Beast TCP stream."""
    buf = b""
    while not stop.is_set():
        chunk = sock.recv(65536)
        if not chunk:
            raise ConnectionError("beast EOF")
        buf += chunk
        n = len(buf)
        pos = 0
        while True:
            s = buf.find(b"\x1a", pos)
            if s < 0:
                pos = n
                break
            if s + 1 >= n:
                pos = s
                break
            typ = buf[s + 1]
            if typ not in BEAST_LEN:
                pos = s + 1  # escaped 0x1a or unknown frame type: resync
                continue
            need = 7 + BEAST_LEN[typ]
            out = bytearray()
            j = s + 2
            state = "ok"
            while len(out) < need:
                if j >= n:
                    state = "short"
                    break
                b = buf[j]
                if b == 0x1A:
                    if j + 1 >= n:
                        state = "short"
                        break
                    if buf[j + 1] != 0x1A:
                        state = "corrupt"  # unescaped 0x1a inside a frame
                        break
                    j += 2
                else:
                    j += 1
                out.append(b)
            if state == "short":
                pos = s  # wait for more bytes
                break
            if state == "corrupt":
                pos = s + 1
                continue
            yield typ, int.from_bytes(out[0:6], "big"), out[6], bytes(out[7:])
            pos = j
        buf = buf[pos:]


def run_beast(w, host, port):
    backoff = 1
    while not stop.is_set():
        try:
            with socket.create_connection((host, port), timeout=10) as s:
                s.settimeout(120)
                log.info("beast connected %s:%s", host, port)
                w.write(f"{time.time():.3f}\tCONNECT\t\t\n")
                backoff = 1
                for typ, ts, sig, payload in beast_frames(s):
                    w.write(f"{time.time():.3f}\t{ts:012x}\t{sig}\t{payload.hex()}\n")
        except Exception as e:
            log.warning("beast: %s", e)
            w.write(f"{time.time():.3f}\tDISCONNECT\t\t{type(e).__name__}\n")
        stop.wait(backoff)
        backoff = min(backoff * 2, 30)


# --------------------------------------------------------- JSON pollers ----

def run_aircraft(w, json_dir):
    path = os.path.join(json_dir, "aircraft.json")
    last_now = None
    while not stop.is_set():
        try:
            with open(path, "rb") as f:
                raw = f.read()
            d = json.loads(raw)
            if d.get("now") != last_now:
                last_now = d.get("now")
                w.write(json.dumps(d, separators=(",", ":")) + "\n")
        except Exception as e:
            log.debug("aircraft: %s", e)
        stop.wait(0.5)


def run_stats(w, json_dir):
    path = os.path.join(json_dir, "stats.json")
    last_end = None
    while not stop.is_set():
        try:
            with open(path) as f:
                d = json.load(f)
            blk = d.get("last1min", {})
            if blk.get("end") != last_end:
                last_end = blk.get("end")
                w.write(json.dumps({"t": time.time(), "last1min": blk}, separators=(",", ":")) + "\n")
        except Exception as e:
            log.debug("stats: %s", e)
        stop.wait(10)


def fetch_json(url, timeout=20):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


CLOUD_SOURCES = {
    "adsblol": f"https://api.adsb.lol/v2/point/{LAT}/{LON}/{CLOUD_RADIUS_NM}",
    "adsbfi": f"https://opendata.adsb.fi/api/v2/lat/{LAT}/lon/{LON}/dist/{CLOUD_RADIUS_NM}",
}


def run_cloud(w, name, url, period, offset, latest):
    stop.wait(offset)
    fails = 0
    while not stop.is_set():
        t0 = time.time()
        try:
            d = fetch_json(url)
            ac = d.get("ac") or d.get("aircraft") or []
            rec = {"t": t0, "src": name, "now": d.get("now"), "ac": ac}
            w.write(json.dumps(rec, separators=(",", ":")) + "\n")
            fails = 0
        except Exception as e:
            fails += 1
            rec = {"t": t0, "src": name, "error": str(e)[:200]}
            w.write(json.dumps(rec) + "\n")
            log.warning("cloud %s: %s", name, e)
        try:
            with cloud_lock:
                cloud_latest[name] = rec
                with open(latest + ".tmp", "w") as f:
                    json.dump(cloud_latest, f)
                os.replace(latest + ".tmp", latest)
        except OSError:
            log.exception("cloud latest cache")
        # back off on repeated errors (rate limit / outage)
        stop.wait(period * min(2 ** max(fails - 1, 0), 30))


WEATHER_URL = (
    "https://api.open-meteo.com/v1/forecast"
    f"?latitude={LAT}&longitude={LON}"
    "&current=temperature_2m,relative_humidity_2m,dew_point_2m,surface_pressure,"
    "precipitation,rain,cloud_cover,visibility,wind_speed_10m,wind_direction_10m,weather_code"
    "&timezone=UTC"
)


def run_weather(w, period):
    while not stop.is_set():
        t0 = time.time()
        try:
            d = fetch_json(WEATHER_URL)
            w.write(json.dumps({"t": t0, "current": d.get("current")}, separators=(",", ":")) + "\n")
        except Exception as e:
            log.warning("weather: %s", e)
        stop.wait(period)


def run_heartbeat(path, writers):
    while not stop.is_set():
        try:
            tmp = path + ".tmp"
            with open(tmp, "w") as f:
                json.dump({"t": time.time(), "pid": os.getpid(),
                           "files": {k: v.key for k, v in writers.items()}}, f)
            os.replace(tmp, path)
        except Exception:
            log.exception("heartbeat")
        stop.wait(30)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", default=os.path.expanduser("~/influ/data"))
    ap.add_argument("--json-dir", default="/run/dump1090-fa")
    ap.add_argument("--beast", default="127.0.0.1:30005")
    ap.add_argument("--cloud-period", type=float, default=10.0)
    ap.add_argument("--no-cloud", action="store_true")
    ap.add_argument("-v", action="store_true")
    a = ap.parse_args()
    logging.basicConfig(level=logging.DEBUG if a.v else logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    os.makedirs(a.data, exist_ok=True)
    compress_leftovers(a.data)

    W = {
        "beast": HourlyWriter(a.data, "beast", "tsv"),
        "aircraft": HourlyWriter(a.data, "aircraft", "jsonl"),
        "stats": HourlyWriter(a.data, "stats", "jsonl"),
        "cloud": HourlyWriter(a.data, "cloud", "jsonl"),
        "weather": HourlyWriter(a.data, "weather", "jsonl"),
    }
    host, port = a.beast.rsplit(":", 1)
    jobs = [
        (run_beast, (W["beast"], host, int(port))),
        (run_aircraft, (W["aircraft"], a.json_dir)),
        (run_stats, (W["stats"], a.json_dir)),
        (run_weather, (W["weather"], 900)),
        (run_heartbeat, (os.path.join(a.data, "heartbeat.json"), W)),
    ]
    if not a.no_cloud:
        for k, (name, url) in enumerate(CLOUD_SOURCES.items()):
            jobs.append((run_cloud, (W["cloud"], name, url, a.cloud_period,
                                     k * a.cloud_period / len(CLOUD_SOURCES),
                                     os.path.join(a.data, "cloud-latest.json"))))
    threads = [threading.Thread(target=f, args=args, daemon=True, name=f.__name__) for f, args in jobs]
    for t in threads:
        t.start()
    log.info("collector started, data=%s", a.data)
    try:
        while not stop.is_set():
            stop.wait(1)
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()
        for w in W.values():
            w.close()


if __name__ == "__main__":
    import signal
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    main()
