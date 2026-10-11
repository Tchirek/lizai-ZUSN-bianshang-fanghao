#!/usr/bin/env python3
"""ADS-B radar scope drawn in HamClock's own style and pushed into one of its panes.

Reads dump1090's aircraft.json every second, keeps short tracks, and every few seconds
renders a 318x294 azimuthal-equidistant scope centred on the station (30 mi by default)
over a dim OpenStreetMap base map (build_basemap.py),
then POSTs it to HamClock's RESTful set_bmp. Station position comes from
~/influ/station.json (never committed).

Symbols   jet/heavy (A3-A6) chevron along track, heavy drawn larger
          light/small (A1-A2) dot, rotorcraft (A7) cross, glider/balloon/UAV (B*) diamond,
          surface/obstacle (C*) square, unknown category triangle
          hollow symbol = position from MLAT/TIS-B, filled = ADS-B
Colour    altitude band (legend at the bottom), white = altitude unknown
Vector    1-minute track projection; dotted trail of the last 5 minutes

usage: adsb_scope.py [--pane 2] [--range-mi 30] [--every 5] [--once out.png]
"""
import argparse
import io
import json
import math
import os
import socket
import time
import urllib.request
from collections import defaultdict, deque

from PIL import Image, ImageDraw, ImageFont

W, H = 318, 294
BLACK = (0, 0, 0)
GRID = (41, 40, 98)            # HamClock Map_grid colour
BORDER = (139, 141, 139)       # HamClock pane border grey
DIM = (110, 110, 110)
TITLE = (255, 125, 0)          # HamClock Short_path orange
WHITE = (255, 255, 255)
RED = (255, 0, 0)
ALT_BANDS = [(2000, (255, 64, 64)), (10000, (246, 129, 49)), (20000, (255, 255, 0)),
             (30000, (0, 255, 0)), (99999, (0, 255, 255))]
FONT_DIRS = ["/usr/share/fonts/truetype/freefont", "/usr/share/fonts/truetype/liberation"]
JSON = "/run/dump1090-fa/aircraft.json"
SDRMUX = "/run/sdrmux/ctl.sock"
MI_KM = 1.609344
# base map: dim, so aircraft stay the brightest thing on the scope (old vector radar look)
MAP_STYLE = {"water": ((20, 45, 110), 1), "river": ((30, 70, 160), 1), "rail": ((70, 70, 70), 1),
             "trunk": ((75, 55, 20), 1), "motorway": ((120, 85, 25), 1),
             "runway": ((175, 175, 175), 3)}
MAP_CITY = (70, 150, 70)
MAP_TOWN = (45, 90, 45)


def font(bold, size):
    names = (["FreeSansBold.ttf", "LiberationSans-Bold.ttf"] if bold
             else ["FreeSans.ttf", "LiberationSans-Regular.ttf"])
    for d in FONT_DIRS:
        for n in names:
            p = os.path.join(d, n)
            if os.path.exists(p):
                return ImageFont.truetype(p, size)
    return ImageFont.load_default()


F_TITLE, F_LABEL, F_SMALL = font(True, 20), font(True, 16), font(True, 14)


def alt_colour(alt):
    if not isinstance(alt, (int, float)):
        return WHITE if alt != "ground" else ALT_BANDS[0][1]
    for top, c in ALT_BANDS:
        if alt < top:
            return c
    return ALT_BANDS[-1][1]


def polar(lat0, lon0, lat, lon):
    """Great-circle distance (km) and initial bearing (deg) from the station."""
    p1, p2 = math.radians(lat0), math.radians(lat)
    dl = math.radians(lon - lon0)
    a = math.sin((p2 - p1) / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    d = 2 * 6371.0 * math.asin(math.sqrt(a))
    brg = math.degrees(math.atan2(math.sin(dl) * math.cos(p2),
                                  math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(dl)))
    return d, brg % 360


class Scope:
    def __init__(self, a):
        st = json.load(open(os.path.expanduser("~/influ/station.json")))
        self.lat0, self.lon0 = st["lat"], st["lon"]
        self.range_km = a.range_mi * MI_KM
        self.cx, self.cy = W // 2, H // 2 + 6
        self.R = min(W // 2, H // 2 - 6) - 16
        self.trails = defaultdict(lambda: deque(maxlen=300))     # hex -> (t, lat, lon)
        self.last = {}
        self.base = self.static_layer(a.basemap)

    def static_layer(self, path):
        """Base map + range rings + compass, drawn once and reused for every frame."""
        img = Image.new("RGB", (W, H), BLACK)
        d = ImageDraw.Draw(img)
        d.fontmode = "1"
        try:
            bm = json.load(open(path))
        except (OSError, ValueError):
            bm = None
        if bm:
            self.basemap(d, bm)
        self.frame(d)
        return img

    def inside(self, x, y, margin=0):
        return (x - self.cx) ** 2 + (y - self.cy) ** 2 <= (self.R + margin) ** 2

    def basemap(self, d, bm):
        """Old vector-radar style: dim single-pixel lines, runways solid, a few town names."""
        for kind in ("water", "river", "rail", "trunk", "motorway", "runway"):
            col, width = MAP_STYLE[kind]
            for ln in bm["lines"]:
                if ln["k"] != kind:
                    continue
                pts = [self.xy(la, lo)[:2] for la, lo in ln["p"]]
                for (x1, y1), (x2, y2) in zip(pts, pts[1:]):
                    if self.inside(x1, y1) and self.inside(x2, y2):
                        d.line([x1, y1, x2, y2], fill=col, width=width)
        taken = []
        for p in sorted(bm["places"], key=lambda p: p["kind"] != "city"):
            x, y, dist = self.xy(p["lat"], p["lon"])
            if dist > self.range_km * 0.97:
                continue
            if p["kind"] != "city":
                d.point([(x, y)], fill=MAP_TOWN)
                continue
            d.rectangle([x - 1, y - 1, x + 1, y + 1], fill=MAP_CITY)
            name = p["name"]
            tw = d.textlength(name, font=F_SMALL)
            box = (x + 4, y - 7, x + 4 + tw, y + 7)
            if self.inside(box[2], y) and not any(
                    not (box[2] < b[0] or box[0] > b[2] or box[3] < b[1] or box[1] > b[3]) for b in taken):
                taken.append(box)
                d.text((box[0], box[1]), name, font=F_SMALL, fill=MAP_CITY)
        done = set()
        for ap in bm["airports"]:
            x, y, dist = self.xy(ap["lat"], ap["lon"])
            if dist < self.range_km * 0.95 and ap["name"] not in done:
                done.add(ap["name"])
                d.text((x + 6, y + 2), ap["name"], font=F_SMALL, fill=MAP_STYLE["runway"][0])
        d.text((W - 4 - d.textlength("OSM", font=F_SMALL), H - 32), "OSM", font=F_SMALL, fill=GRID)

    def xy(self, lat, lon):
        d, b = polar(self.lat0, self.lon0, lat, lon)
        r = d / self.range_km * self.R
        return self.cx + r * math.sin(math.radians(b)), self.cy - r * math.cos(math.radians(b)), d

    def sample(self):
        try:
            with open(JSON) as f:
                j = json.load(f)
        except (OSError, ValueError):
            return
        now = j.get("now", time.time())
        self.last = {"now": now, "aircraft": j.get("aircraft", [])}
        for ac in self.last["aircraft"]:
            if ac.get("lat") is not None and ac.get("seen_pos", 99) < 2:
                tr = self.trails[ac["hex"]]
                if not tr or now - tr[-1][0] >= 4:
                    tr.append((now, ac["lat"], ac["lon"]))
        for h in [h for h, tr in self.trails.items() if now - tr[-1][0] > 300]:
            del self.trails[h]

    # ------------------------------------------------------------- drawing --
    def frame(self, d):
        cx, cy, R = self.cx, self.cy, self.R
        for frac in (1 / 3, 2 / 3):
            r = R * frac
            d.ellipse([cx - r, cy - r, cx + r, cy + r], outline=GRID)
        d.ellipse([cx - R, cy - R, cx + R, cy + R], outline=BORDER)
        for ang in range(0, 360, 30):
            s, c = math.sin(math.radians(ang)), math.cos(math.radians(ang))
            r0 = R - (6 if ang % 90 else 10)
            d.line([cx + r0 * s, cy - r0 * c, cx + R * s, cy - R * c], fill=BORDER)
        for txt, ang in (("N", 0), ("E", 90), ("S", 180), ("W", 270)):
            s, c = math.sin(math.radians(ang)), math.cos(math.radians(ang))
            tw = d.textlength(txt, font=F_LABEL)
            d.text((cx + (R + 9) * s - tw / 2, cy - (R + 9) * c - 8), txt, font=F_LABEL, fill=BORDER)
        mi = self.range_km / MI_KM
        for frac, lab in ((1 / 3, mi / 3), (2 / 3, mi * 2 / 3)):
            r = R * frac
            d.text((cx + r * 0.707 + 1, cy - r * 0.707 - 15), f"{lab:.0f}", font=F_SMALL, fill=GRID)
        d.line([cx - 4, cy, cx + 4, cy], fill=WHITE)
        d.line([cx, cy - 4, cx, cy + 4], fill=WHITE)

    def symbol(self, d, x, y, ac, col):
        cat = (ac.get("category") or "").upper()
        hollow = bool(ac.get("mlat")) or bool(ac.get("tisb"))
        fill = None if hollow else col
        trk = math.radians(ac.get("track") or ac.get("true_heading") or 0)

        def rot(px, py):
            return (x + px * math.cos(trk) - py * math.sin(trk), y + px * math.sin(trk) + py * math.cos(trk))

        if cat in ("A3", "A4", "A5", "A6") or (not cat and (ac.get("gs") or 0) > 250):
            s = 6 if cat == "A5" else 4.5
            pts = [rot(0, -s * 1.7), rot(s, s), rot(0, s * 0.35), rot(-s, s)]
            d.polygon(pts, outline=col, fill=fill)
        elif cat in ("A1", "A2"):
            d.ellipse([x - 3, y - 3, x + 3, y + 3], outline=col, fill=fill)
        elif cat == "A7":
            d.line([x - 4, y - 4, x + 4, y + 4], fill=col, width=2)
            d.line([x - 4, y + 4, x + 4, y - 4], fill=col, width=2)
        elif cat.startswith("B"):
            d.polygon([(x, y - 5), (x + 4, y), (x, y + 5), (x - 4, y)], outline=col, fill=fill)
        elif cat.startswith("C"):
            d.rectangle([x - 3, y - 3, x + 3, y + 3], outline=col, fill=fill)
        else:
            d.polygon([(x, y - 5), (x + 4, y + 4), (x - 4, y + 4)], outline=col, fill=None)

    def render(self, away=None):
        img = self.base.copy()                # base map, rings and compass
        d = ImageDraw.Draw(img)
        d.fontmode = "1"                      # crisp, un-antialiased like HamClock
        now = self.last.get("now", time.time())
        acs = self.last.get("aircraft", [])
        placed, labels = [], []
        n_pos = n_nopos = 0
        for ac in sorted(acs, key=lambda a: a["hex"]):      # stable order: labels don't hop
            if ac.get("seen", 99) > 60:
                continue
            if ac.get("lat") is None or ac.get("seen_pos", 99) > 60:
                n_nopos += 1
                continue
            x, y, dist = self.xy(ac["lat"], ac["lon"])
            if dist > self.range_km:
                continue
            n_pos += 1
            col = alt_colour(ac.get("alt_baro"))
            dim = tuple(int(c * 0.45) for c in col)
            for _, la, lo in list(self.trails.get(ac["hex"], []))[:-1]:
                tx, ty, _ = self.xy(la, lo)
                d.point([(tx, ty)], fill=dim)
            if ac.get("gs") and ac.get("track") is not None:
                km = ac["gs"] * 1.852 / 60                       # 1 minute ahead
                r = km / self.range_km * self.R
                t = math.radians(ac["track"])
                d.line([x, y, x + r * math.sin(t), y - r * math.cos(t)], fill=dim)
            self.symbol(d, x, y, ac, col)
            name = (ac.get("flight") or "").strip() or ac["hex"].upper()
            alt = ac.get("alt_baro")
            sub = "GND" if alt == "ground" else (f"{int(alt) // 100:03d}" if isinstance(alt, (int, float)) else "")
            labels.append((x, y, name, sub, col))
        for x, y, name, sub, col in labels:
            tw = max(d.textlength(name, font=F_LABEL), d.textlength(sub, font=F_SMALL))
            box = (x + 7, y - 15, x + 7 + tw, y + 16)
            if box[2] > W - 2:
                box = (x - 7 - tw, box[1], x - 7, box[3])
            if any(not (box[2] < b[0] or box[0] > b[2] or box[3] < b[1] or box[1] > b[3]) for b in placed):
                continue
            placed.append(box)
            d.text((box[0], box[1]), name, font=F_LABEL, fill=col)
            if sub:
                d.text((box[0], box[1] + 16), sub, font=F_SMALL, fill=col)

        d.text((4, 1), "ADS-B", font=F_TITLE, fill=TITLE)
        mi = self.range_km / MI_KM
        d.text((4, 24), f"{mi:.0f} mi", font=F_SMALL, fill=DIM)
        cnt = f"{n_pos} AC"
        d.text((W - 4 - d.textlength(cnt, font=F_TITLE), 1), cnt, font=F_TITLE, fill=WHITE)
        if n_nopos:
            s = f"+{n_nopos} no pos"
            d.text((W - 4 - d.textlength(s, font=F_SMALL), 24), s, font=F_SMALL, fill=DIM)
        # altitude legend, bottom-left: colour chips for 0/2/10/20/30 kft
        x0 = 4
        for (top, c), lab in zip(ALT_BANDS, ("0", "2", "10", "20", "30k")):
            d.rectangle([x0, H - 9, x0 + 7, H - 3], fill=c)
            d.text((x0 + 9, H - 17), lab, font=F_SMALL, fill=DIM)
            x0 += 11 + d.textlength(lab, font=F_SMALL) + 4
        clock = time.strftime("%H:%MZ", time.gmtime(now))
        d.text((W - 4 - d.textlength(clock, font=F_SMALL), H - 17), clock, font=F_SMALL, fill=DIM)
        if away:
            msg = " SDR ON 2m "
            tw = d.textlength(msg, font=F_LABEL)
            d.rectangle([self.cx - tw / 2 - 2, 44, self.cx + tw / 2 + 2, 63], fill=RED)
            d.text((self.cx - tw / 2, 45), msg, font=F_LABEL, fill=WHITE)
        # the image's last column/row land on HamClock's own pane border: paint them as border
        d.line([W - 1, 0, W - 1, H - 1], fill=BORDER)
        d.line([0, H - 1, W - 1, H - 1], fill=BORDER)
        return img


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


class PaneWatch:
    """Follow HamClock's diagnostic log; report when it repainted our pane (or restarted),
    so the scope can be pushed again at once instead of leaving HamClock's content visible."""

    LOG = os.path.expanduser("~/.hamclock/diagnostic-log.txt")

    def __init__(self, pane):
        self.marks = (f"Pane {pane} now showing".encode(), b"Setup:",
                      b"reading local latest_")      # SDO image reload (if SDO is underneath)
        self.f, self.ino = None, None
        self.reopen(at_end=True)

    def reopen(self, at_end=False):
        try:
            self.f = open(self.LOG, "rb")
            self.ino = os.fstat(self.f.fileno()).st_ino
            if at_end:
                self.f.seek(0, os.SEEK_END)
        except OSError:
            self.f = None

    def redrawn(self):
        try:
            st = os.stat(self.LOG)
        except OSError:
            return False
        if self.f is None or st.st_ino != self.ino or st.st_size < self.f.tell():
            self.reopen()                       # HamClock restarted / log rotated
            return True
        data = self.f.read()
        return any(m in data for m in self.marks)


def push(img, pane, host):
    b = io.BytesIO()
    img.save(b, "BMP")
    req = urllib.request.Request(f"http://{host}/set_bmp?pane={pane}&fit=crop", data=b.getvalue(),
                                 method="POST", headers={"Content-Type": "application/octet-stream"})
    return urllib.request.urlopen(req, timeout=10).read().decode().strip()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--pane", type=int, default=2)
    ap.add_argument("--range-mi", type=float, default=30)
    ap.add_argument("--basemap", default=os.path.expanduser("~/influ/model/basemap.json"))
    ap.add_argument("--every", type=float, default=5, help="render interval, s")
    ap.add_argument("--keepalive", type=float, default=15, help="re-push unchanged image, s")
    ap.add_argument("--hamclock", default="127.0.0.1:8070")
    ap.add_argument("--once", help="render one frame to this PNG and exit (no push)")
    a = ap.parse_args()
    sc = Scope(a)
    if a.once:
        for _ in range(3):
            sc.sample()
            time.sleep(1)
        sc.render(sdr_away()).save(a.once)
        return
    watch = PaneWatch(a.pane)
    last_img, last_push, nxt, again = None, 0.0, 0.0, 0.0
    while True:
        sc.sample()
        redrawn = watch.redrawn()
        now = time.time()
        if redrawn:
            again = now + 1.5                  # the log line may precede HamClock's actual draw
        elif again and now >= again:
            redrawn, again = True, 0.0
        if now >= nxt or redrawn:
            img = sc.render(sdr_away())
            nxt = now + a.every
            blob = img.tobytes()
            # push only on change, when HamClock repainted the pane, or as a periodic safety net
            if blob != last_img or redrawn or now - last_push > a.keepalive:
                try:
                    r = push(img, a.pane, a.hamclock)
                    if r != "ok":
                        print("hamclock:", r, flush=True)
                    last_img, last_push = blob, now
                except OSError as e:
                    print("push failed:", e, flush=True)
        time.sleep(0.25 if redrawn else 0.5)


if __name__ == "__main__":
    main()
