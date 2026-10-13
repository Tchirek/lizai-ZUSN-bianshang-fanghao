#!/usr/bin/env python3
"""ADS-B feed for HamClock's own ADS-B pane (hamclock-adsb.cpp, added by patch_hamclock.py).

Follows dump1090's aircraft.json every second, keeps short tracks and the count of aircraft heard
today, and writes them for HamClock, which draws the radar scope itself, around DE, in its own
fonts, as a pane choice like any other (range chosen by tapping the pane):

  ~/.hamclock/influ-adsb.txt      every second: the time, today's count, whether the SDR is
                                  away on 2 m, and each aircraft with its trail
  ~/.hamclock/influ-adsb-map.txt  the base map, from build_basemap.py's basemap.json, written
                                  at start and whenever that changes

Both are plain text, a line each (formats in hamclock-adsb.cpp), written whole (a temporary file
renamed), so HamClock never reads half of one. Today's count is kept across restarts in
~/influ/model/adsb_today.json, a new day filled from the collector's snapshots. Names are
written in ASCII, which HamClock's fonts have.

usage: adsb_scope.py [--dir ~/.hamclock] [--basemap ~/influ/model/basemap.json] [--once]
"""
import argparse
import glob
import gzip
import json
import os
import socket
import time
import unicodedata
from collections import defaultdict, deque

JSON = "/run/dump1090-fa/aircraft.json"
SDRMUX = "/run/sdrmux/ctl.sock"
TODAY = os.path.expanduser("~/influ/model/adsb_today.json")
SNAPSHOTS = os.path.expanduser("~/influ/data/aircraft")      # the collector's, UTC day/hour files
TZ_S = 8 * 3600
FEED, MAP = "influ-adsb.txt", "influ-adsb-map.txt"
TRAIL_S, TRAIL_EVERY = 300, 4      # trails: the last 5 minutes, a point every 4 s
SEEN_S = 60                        # aircraft (and positions) older than this are gone


def heard(ac):
    """An aircraft dump1090 is tracking now on two messages or more (one may be noise)."""
    return ac.get("seen", 99) <= 60 and ac.get("messages", 0) >= 2 and not ac["hex"].startswith("~")


class Today:
    """Aircraft heard since local midnight."""

    def __init__(self, path=TODAY, snapshots=SNAPSHOTS):
        self.path, self.snapshots = path, snapshots
        self.day, self.hexes = None, set()
        try:
            with open(path) as f:
                d = json.load(f)
            self.day, self.hexes = d["day"], set(d["hex"])
        except (OSError, ValueError, KeyError):
            pass

    @staticmethod
    def day_of(t):
        return time.strftime("%Y-%m-%d", time.gmtime(t + TZ_S))

    def backfill(self, now):
        """What the collector heard today before this process saw it."""
        midnight = (now + TZ_S) // 86400 * 86400 - TZ_S
        found = set()
        for day in {time.strftime("%Y%m%d", time.gmtime(t)) for t in (midnight, now)}:
            for path in sorted(glob.glob(os.path.join(self.snapshots, day, "*"))):
                try:
                    with (gzip.open if path.endswith(".gz") else open)(path, "rt") as f:
                        for i, line in enumerate(f):
                            if i % 5:                  # aircraft stay for many snapshots
                                continue
                            try:
                                d = json.loads(line)
                            except ValueError:
                                continue
                            if d.get("now", 0) >= midnight:
                                found |= {ac["hex"] for ac in d.get("aircraft", []) if heard(ac)}
                except (OSError, EOFError):
                    continue
        return found

    def add(self, now, aircraft):
        day = self.day_of(now)
        if day != self.day:
            self.day, self.hexes = day, self.backfill(now)
            self.save()
        new = {ac["hex"] for ac in aircraft if heard(ac)} - self.hexes
        if new:
            self.hexes |= new
            self.save()
        return len(self.hexes)

    def save(self):
        try:
            with open(self.path + ".tmp", "w") as f:
                json.dump(dict(day=self.day, hex=sorted(self.hexes)), f)
            os.replace(self.path + ".tmp", self.path)
        except OSError:
            pass


def sdr_away():
    """True while sdrmux has the SDR away from 1090 MHz (a 2 m listen window)."""
    try:
        s = socket.socket(socket.AF_UNIX)
        s.settimeout(1)
        s.connect(SDRMUX)
        s.sendall(b'{"cmd":"status"}\n')
        st = json.loads(s.makefile().readline())
        s.close()
        return st.get("mode") == "ham"
    except (OSError, ValueError):
        return None


def ascii_name(name):
    """`name` in the ASCII HamClock's fonts have: accents dropped, anything else left out."""
    plain = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()
    return " ".join("".join(c for c in plain if c.isprintable()).split())


def spots(points):
    return " ".join(f"{lat:.5f} {lon:.5f}" for lat, lon in points)


class Feed:
    """What dump1090 hears, with short trails and today's count, as the ADS-B pane reads it."""

    def __init__(self, today=None):
        self.trails = defaultdict(lambda: deque(maxlen=TRAIL_S // TRAIL_EVERY + 1))   # hex -> (t, lat, lon)
        self.today = today if today is not None else Today()
        self.now, self.aircraft, self.count = time.time(), [], 0

    def sample(self, path=JSON):
        try:
            with open(path) as f:
                j = json.load(f)
        except (OSError, ValueError):
            return
        self.now = j.get("now", time.time())
        self.aircraft = j.get("aircraft", [])
        self.count = self.today.add(self.now, self.aircraft)
        for ac in self.aircraft:
            if ac.get("lat") is not None and ac.get("seen_pos", 99) < 2:
                tr = self.trails[ac["hex"]]
                if not tr or self.now - tr[-1][0] >= TRAIL_EVERY:
                    tr.append((self.now, ac["lat"], ac["lon"]))
        for h in [h for h, tr in self.trails.items() if self.now - tr[-1][0] > TRAIL_S]:
            del self.trails[h]

    def text(self, away=False):
        """The feed: T, N, then an A line for each aircraft with a position, its trail in a P line."""
        lines, nopos = [], 0
        for ac in sorted(self.aircraft, key=lambda a: a["hex"]):      # a stable order: labels don't hop
            if ac.get("seen", 99) > SEEN_S:
                continue
            if ac.get("lat") is None or ac.get("seen_pos", 99) > SEEN_S:
                nopos += 1
                continue
            alt = ac.get("alt_baro")
            alt = -2 if alt == "ground" else round(alt) if isinstance(alt, (int, float)) else -1
            track = ac.get("track", ac.get("true_heading"))
            track = round(track) % 360 if isinstance(track, (int, float)) else -1
            cat = (ac.get("category") or "-").upper()[:3]
            mlat = int(bool(ac.get("mlat")) or bool(ac.get("tisb")))
            name = ascii_name((ac.get("flight") or "").strip()) or ac["hex"].upper()
            lines.append(f"A {ac['hex'][:7]} {ac['lat']:.5f} {ac['lon']:.5f} {alt} {track} "
                         f"{round(ac.get('gs') or 0)} {cat} {mlat} {name[:11]}")
            trail = self.trails.get(ac["hex"])
            if trail and len(trail) > 1:
                lines.append("P " + spots((la, lo) for _, la, lo in trail))
        head = ["# influ ADS-B feed for HamClock's ADS-B pane (tools/adsb_scope.py)",
                f"T {int(self.now)} {self.count} {int(bool(away))}", f"N {nopos}"]
        return "\n".join(head + lines) + "\n"


def map_text(basemap):
    """The base map as the ADS-B pane reads it: L lines by kind, C cities, W towns, R airfields."""
    lines = ["# influ ADS-B base map for HamClock's ADS-B pane (tools/adsb_scope.py, from basemap.json)"]
    for ln in basemap.get("lines", []):
        if len(ln["p"]) > 1:
            lines.append(f"L {ln['k']} {spots(ln['p'])}")
    for p in basemap.get("places", []):
        if p["kind"] == "city":
            lines.append(f"C {p['lat']:.5f} {p['lon']:.5f} {ascii_name(p['name'])}".rstrip())
        else:
            lines.append(f"W {p['lat']:.5f} {p['lon']:.5f}")
    for ap in basemap.get("airports", []):            # named by code (ZUSN), not by a long name
        code = ap["name"] if ap["name"].isascii() and ap["name"].isalnum() and len(ap["name"]) <= 5 else ""
        lines.append(f"R {ap['lat']:.5f} {ap['lon']:.5f} {code}".rstrip())
    return "\n".join(lines) + "\n"


def write(path, text):
    """Write the file whole: HamClock never reads half of one."""
    part = path + ".part"
    with open(part, "w") as f:
        f.write(text)
    os.replace(part, path)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", default=os.path.expanduser("~/.hamclock"), help="HamClock's directory (its -d)")
    ap.add_argument("--basemap", default=os.path.expanduser("~/influ/model/basemap.json"))
    ap.add_argument("--once", action="store_true", help="print the feed once and exit")
    a = ap.parse_args()
    feed = Feed()
    if a.once:
        feed.sample()
        print(feed.text(sdr_away()), end="")
        return
    map_seen = None
    while True:
        try:
            mtime = os.path.getmtime(a.basemap)
            if mtime != map_seen:
                with open(a.basemap) as f:
                    write(os.path.join(a.dir, MAP), map_text(json.load(f)))
                map_seen = mtime
        except (OSError, ValueError) as e:
            if map_seen is not False:
                print("base map:", e, flush=True)
            map_seen = False
        feed.sample()
        try:
            write(os.path.join(a.dir, FEED), feed.text(sdr_away()))
        except OSError as e:
            print("feed:", e, flush=True)
        time.sleep(1)


if __name__ == "__main__":
    main()
