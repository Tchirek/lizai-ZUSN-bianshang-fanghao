#!/usr/bin/env python3
"""Live amateur activity over the UV-K5 range map, and this week's FT8 tally.

Sources
  APRS-IS        rotate.aprs2.net:14580, receive-only login (passcode -1), range filter around
                 us: VHF APRS beacons (144.640 MHz here) -- who is on the air nearby, live.
                 Packets an igate heard on RF (q-construct qAR/qAO) are drawn brighter than
                 internet-only ones.
  PSK Reporter   one query per 5 min, the Maidenhead fields the map covers taken in turn:
                 FT8/FT4 stations in the region (last 30 min), coloured by band as HamClock
                 does; paths drawn when either end is on the map.
  our FT8 log    ~/influ/ft8/auto.jsonl: CQs we sent and QSOs completed since Monday 00:00
                 local time.

Writes, atomically, ~/influ/model/activity.png (transparent layer for HamClock's map box, same
projection as rangemap.png) and ~/influ/model/ft8week.png (tally for the foot of the left
column); hcdisplay lays both over the clock.
"""
import json
import math
import os
import socket
import threading
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET

import aprslib
from PIL import Image, ImageDraw, ImageFont

MODEL = os.path.expanduser("~/influ/model")
FT8_LOG = os.path.expanduser("~/influ/ft8/auto.jsonl")
STATION = os.path.expanduser("~/influ/station.json")
W, H = 1320, 660                       # HamClock map box at 1600x960
TALLY_W, TALLY_H = 274, 96             # last 3 DX cluster rows of the left column
APRS_SERVER = ("rotate.aprs2.net", 14580)
APRS_RANGE_KM = 650
APRS_SHOW_S = 3600                     # stations heard within the last hour
PSK_URL = ("https://retrieve.pskreporter.info/query?callsign={field}&modify=grid"
           "&flowStartSeconds=-1800&appcontact=bg8asn")
PSK_EVERY_S = 300                      # one query per 5 min: PSK Reporter's own limit
UA = {"User-Agent": "influ-activity/1.0 (BG8ASN)"}

# HamClock's band colours (get_config.txt), keyed by lower band edge in kHz
BANDS = [(1800, "160", (131, 0, 0)), (3500, "80", (131, 129, 0)), (5300, "60", (230, 24, 74)),
         (7000, "40", (246, 129, 49)), (10100, "30", (205, 178, 16)), (14000, "20", (255, 250, 0)),
         (18068, "17", (90, 182, 8)), (21000, "15", (65, 255, 172)), (24890, "12", (0, 129, 255)),
         (28000, "10", (255, 190, 213)), (50000, "6", (139, 161, 8)), (144000, "2", (98, 101, 98)),
         (430000, "70cm", (180, 120, 255))]
APRS_RF, APRS_NET = (0, 255, 255), (0, 130, 150)
WHITE, BLACK, DIM = (255, 255, 255), (0, 0, 0), (150, 150, 150)


def font(size, bold=True):
    name = "FreeSansBold.ttf" if bold else "FreeSans.ttf"
    path = os.path.join("/usr/share/fonts/truetype/freefont", name)
    return ImageFont.truetype(path, size) if os.path.exists(path) else ImageFont.load_default()


F_CALL, F_SMALL, F_BIG = font(12), font(13), font(30)


def band(freq_khz):
    hit = None
    for lo, name, colour in BANDS:
        if freq_khz >= lo:
            hit = (name, colour)
    return hit or ("?", DIM)


def locator(grid):
    """Maidenhead -> (lat, lon) of the square's centre, or None."""
    g = grid.strip().upper()
    if len(g) < 4 or not (g[:2].isalpha() and g[2:4].isdigit()):
        return None
    lon = (ord(g[0]) - 65) * 20 - 180 + int(g[2]) * 2 + 1
    lat = (ord(g[1]) - 65) * 10 - 90 + int(g[3]) + 0.5
    if len(g) >= 6 and g[4:6].isalpha():
        lon += (ord(g[4]) - 65) * 2 / 24 - 1 + 1 / 24
        lat += (ord(g[5]) - 65) / 24 - 0.5 + 1 / 48
    return lat, lon


class Map:
    """Projection of rangemap.png: azimuthal equidistant around the station."""

    def __init__(self):
        st = json.load(open(STATION))
        self.lat0, self.lon0 = st["lat"], st["lon"]
        info = json.load(open(os.path.join(MODEL, "rangemap.json")))
        self.km_per_px = info["km_per_px"]
        self.labels = [tuple(b) for b in info.get("labels", [])]   # base map's own labels

    def xy(self, lat, lon):
        p1, p2 = math.radians(self.lat0), math.radians(lat)
        dl = math.radians(lon - self.lon0)
        a = math.sin((p2 - p1) / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
        r = 2 * 6371 * math.asin(math.sqrt(min(a, 1))) / self.km_per_px
        b = math.atan2(math.sin(dl) * math.cos(p2),
                       math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(dl))
        return W / 2 + r * math.sin(b), H / 2 - r * math.cos(b)

    def inside(self, x, y, margin=4):
        return margin <= x < W - margin and margin <= y < H - margin

    def fields(self):
        """Maidenhead fields (e.g. OL, OM) touched by the map's corners and edges."""
        out = set()
        for x in range(0, W + 1, W // 8):
            for y in range(0, H + 1, H // 4):
                lat, lon = self.latlon(x, y)
                out.add(chr(65 + int((lon + 180) // 20)) + chr(65 + int((lat + 90) // 10)))
        return sorted(out)

    def latlon(self, x, y):
        dx, dy = (x - W / 2) * self.km_per_px, (y - H / 2) * self.km_per_px
        d, b = math.hypot(dx, dy) / 6371, math.atan2(dx, -dy)
        p1, l1 = math.radians(self.lat0), math.radians(self.lon0)
        p2 = math.asin(math.sin(p1) * math.cos(d) + math.cos(p1) * math.sin(d) * math.cos(b))
        l2 = l1 + math.atan2(math.sin(b) * math.sin(d) * math.cos(p1),
                             math.cos(d) - math.sin(p1) * math.sin(p2))
        return math.degrees(p2), math.degrees(l2)


class Aprs(threading.Thread):
    """Receive-only APRS-IS feed; keeps the latest position of each station."""

    def __init__(self, lat, lon):
        super().__init__(daemon=True)
        self.filter = f"r/{lat:.2f}/{lon:.2f}/{APRS_RANGE_KM}"
        self.stations, self.links, self.lock = {}, {}, threading.Lock()

    def run(self):
        while True:
            try:
                self.session()
            except OSError:
                pass
            time.sleep(30)

    def session(self):
        with socket.create_connection(APRS_SERVER, timeout=60) as s:
            s.sendall(f"user BG8ASN pass -1 vers influ-activity 1.0 filter {self.filter}\r\n".encode())
            buf = b""
            while True:
                data = s.recv(4096)
                if not data:
                    return
                buf += data
                *lines, buf = buf.split(b"\n")
                for raw in lines:
                    self.take(raw.decode("utf-8", "replace").strip())

    def take(self, line):
        if not line or line.startswith("#"):
            return
        try:
            p = aprslib.parse(line)
        except (aprslib.ParseError, aprslib.UnknownFormat):
            return
        hops = rf_hops(p["from"], p.get("path", []))
        now = time.time()
        with self.lock:
            for a, b in zip(hops, hops[1:]):
                if a and b:
                    self.links[a, b] = now
            if "latitude" in p:
                self.stations[p["from"]] = (p["latitude"], p["longitude"], len(hops) > 1, now)

    def recent(self):
        """(stations heard within the hour, positions known for any station, RF links within
        the hour)."""
        now = time.time()
        with self.lock:
            for k in [k for k, v in self.stations.items() if now - v[3] > 2 * APRS_SHOW_S]:
                del self.stations[k]
            for k in [k for k, t in self.links.items() if now - t > APRS_SHOW_S]:
                del self.links[k]
            heard = {k: v for k, v in self.stations.items() if now - v[3] <= APRS_SHOW_S}
            return heard, dict(self.stations), list(self.links)


APRS_ALIASES = ("WIDE", "RELAY", "TRACE", "TCPIP", "TCPXX", "NOGATE", "RFONLY", "GATE", "ECHO")


def rf_hops(source, path):
    """The RF hops of an APRS packet: source -> each digipeater that repeated it -> the igate
    that heard it on RF. In the path, digipeaters up to the last one marked '*' have repeated
    it; a generic alias (WIDE2*...) hides who did, so None preserves that gap instead of
    inventing a direct link. 'qAR,IGATE' (or qAO) names the igate. Internet-only packets give
    just [source]."""
    if any(h.rstrip("*").upper() in ("TCPIP", "TCPXX") for h in path):
        return [source]
    q = next((i for i, hop in enumerate(path) if hop.startswith("qA")), len(path))
    if q < len(path) and path[q] not in ("qAR", "qAO"):
        return [source]
    digis = path[:q]
    last = max((i for i, hop in enumerate(digis) if hop.endswith("*")), default=-1)
    hops = [source] + [None if d.upper().startswith(APRS_ALIASES) else d.rstrip("*")
                       for d in digis[:last + 1]]
    if q < len(path) - 1 and path[q] in ("qAR", "qAO"):
        hops.append(path[q + 1])
    return [h for i, h in enumerate(hops) if i == 0 or h != hops[i - 1]]


class Psk(threading.Thread):
    """FT8/FT4 reception reports for the map's Maidenhead fields.

    One query per PSK_EVERY_S, the fields taken in turn (each query covers the last 30 min, so
    a full round of four fields still overlaps). Results and the time the next query is allowed
    are kept on disk, so restarts never query early; a 429 backs off as long as the server asks
    (at least 30 min)."""

    STATE = os.path.join(MODEL, "psk_cache.json")

    def __init__(self, fields):
        super().__init__(daemon=True)
        self.fields, self.lock = fields, threading.Lock()
        try:
            self.state = json.load(open(self.STATE))
        except (OSError, ValueError):
            self.state = {}
        self.state.setdefault("next", 0)                     # earliest time for the next query
        self.state.setdefault("fields", {})                  # field -> {"t": ..., "reports": [...]}

    def run(self):
        while True:
            time.sleep(max(0, self.state["next"] - time.time()))
            done = self.state["fields"]
            field = min(self.fields, key=lambda f: done.get(f, {}).get("t", 0))
            wait = PSK_EVERY_S
            try:
                req = urllib.request.Request(PSK_URL.format(field=field), headers=UA)
                root = ET.fromstring(urllib.request.urlopen(req, timeout=90).read())
                reports = [{k: r.get(k, "") for k in (
                    "senderCallsign", "senderLocator", "receiverCallsign", "receiverLocator",
                    "frequency", "mode")} for r in root.iter("receptionReport")]
                with self.lock:
                    done[field] = {"t": time.time(), "reports": reports}
            except urllib.error.HTTPError as e:
                if e.code == 429:
                    wait = max(int(e.headers.get("Retry-After") or 0), 1800)
            except (OSError, ET.ParseError):
                pass
            with self.lock:
                self.state["next"] = time.time() + wait
                json.dump(self.state, open(self.STATE, "w"))

    def current(self):
        """Reports from every field fetched within the last 40 min."""
        with self.lock:
            fresh = [v["reports"] for v in self.state["fields"].values() if time.time() - v["t"] < 2400]
        return [r for reports in fresh for r in reports]


def ft8_week():
    """(CQs sent, completed QSOs as (call, grid), week start) since Monday 00:00 local time."""
    lt = time.localtime()
    monday = time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday - lt.tm_wday, 0, 0, 0, 0, 0, -1))
    cq, qsos = 0, []
    try:
        with open(FT8_LOG) as f:
            for line in f:
                e = json.loads(line)
                if e.get("t", 0) < monday:
                    continue
                if e.get("event") == "transmit" and str(e.get("message", "")).startswith("CQ "):
                    cq += 1
                elif e.get("event") == "qso":
                    qsos.append((e.get("call", ""), e.get("grid", "")))
    except (OSError, ValueError):
        pass
    return cq, qsos, monday


def draw_activity(m, aprs, reports, qsos):
    """aprs: (heard, positions, links) from Aprs.recent(); reports: PSK Reporter; qsos: ours.

    Both ends of every contact are joined: PSK sender-receiver (band colour, running off the
    map toward a far partner), APRS RF hops (station - digipeater - igate), and our own FT8
    QSOs (white, from us). Lines go under all markers and labels."""
    heard, positions, links = aprs
    img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.fontmode = "1"
    taken = list(m.labels)                                  # keep clear of the base map's text

    def label(x, y, text, colour):
        tw = d.textlength(text, font=F_CALL)
        for lx, ly in ((x + 7, y - 7), (x - 7 - tw, y - 7), (x - tw / 2, y - 21), (x - tw / 2, y + 7)):
            box = (lx - 1, ly, lx + tw + 1, ly + 14)            # right, left, above, below
            if box[0] < 0 or box[2] >= W or box[1] < 0 or box[3] >= H:
                continue
            if not any(not (box[2] < b[0] or box[0] > b[2] or box[3] < b[1] or box[1] > b[3])
                       for b in taken):
                taken.append(box)
                d.text((lx, ly), text, font=F_CALL, fill=colour, stroke_width=1, stroke_fill=BLACK)
                return

    # FT8 / FT4: a line for every report touching the map, each station on the map once
    stations, drawn, pairs = {}, set(), set()               # drawn: bands with a station shown
    for r in reports:
        try:
            name, colour = band(int(r["frequency"]) / 1000)
        except ValueError:
            continue
        ends = {}
        for who in ("sender", "receiver"):
            ll = locator(r[who + "Locator"])
            if ll:
                ends[who] = m.xy(*ll)
        on_map = [who for who, (x, y) in ends.items() if m.inside(x, y)]
        if not on_map:
            continue
        drawn.add((name, colour))
        for who in on_map:
            stations.setdefault(r[who + "Callsign"], ends[who] + (colour, who == "sender"))
        pair = (r["senderCallsign"], r["receiverCallsign"], name)
        if len(ends) == 2 and pair not in pairs:
            pairs.add(pair)
            d.line([ends["sender"], ends["receiver"]], fill=colour + (170,), width=1)

    # APRS RF hops between stations whose positions we know
    for a, b in links:
        if a in positions and b in positions:
            d.line([m.xy(*positions[a][:2]), m.xy(*positions[b][:2])], fill=APRS_RF + (200,), width=2)

    # our own QSOs, from us
    for call, grid in qsos:
        ll = locator(grid)
        if ll:
            d.line([(W / 2, H / 2), m.xy(*ll)], fill=WHITE + (230,), width=2)

    # markers and labels on top of every line
    for call, (x, y, colour, sent) in sorted(stations.items(), key=lambda kv: not kv[1][3]):
        d.polygon([(x, y - 5), (x + 5, y), (x, y + 5), (x - 5, y)], fill=colour, outline=WHITE)
        label(x, y, call, WHITE if sent else DIM)
    for call, (lat, lon, rf, t) in sorted(heard.items(), key=lambda kv: kv[1][2]):
        x, y = m.xy(lat, lon)
        if not m.inside(x, y):
            continue
        colour = APRS_RF if rf else APRS_NET
        d.ellipse([x - 4, y - 4, x + 4, y + 4], fill=colour, outline=BLACK)
        label(x, y, call, colour)
    for call, grid in qsos:
        ll = locator(grid)
        if ll and m.inside(*m.xy(*ll)):
            x, y = m.xy(*ll)
            d.rectangle([x - 4, y - 4, x + 4, y + 4], fill=WHITE, outline=BLACK)
            label(x, y, call, WHITE)

    # legend, bottom right, like HamClock's map labels
    order = {name: i for i, (_, name, _) in enumerate(BANDS)}
    parts = [(APRS_RF, f"APRS {len(heard)}"), (WHITE, f"FT8/FT4 {len(stations)}")]
    parts += [(colour, name if name.endswith("cm") else name + "m")
              for name, colour in sorted(drawn, key=lambda b: order.get(b[0], 99))]
    widths = [d.textlength(text, font=F_SMALL) + 18 for _, text in parts]
    x0 = W - 6 - sum(widths)
    d.rectangle([x0 - 4, H - 22, W - 2, H - 2], fill=BLACK + (255,), outline=WHITE)
    for (colour, text), w in zip(parts, widths):
        d.rectangle([x0, H - 15, x0 + 8, H - 7], fill=colour, outline=WHITE)
        d.text((x0 + 12, H - 20), text, font=F_SMALL, fill=WHITE)
        x0 += w
    return img


def draw_tally(cq, qso, monday):
    img = Image.new("RGB", (TALLY_W, TALLY_H), BLACK)
    d = ImageDraw.Draw(img)
    d.fontmode = "1"
    d.line([0, 0, TALLY_W - 1, 0], fill=(139, 141, 139))      # rule, as HamClock's pane border
    d.text((8, 4), "FT8 this week", font=F_SMALL, fill=(255, 125, 0))
    since = time.strftime("since %a %m-%d", time.localtime(monday))
    d.text((TALLY_W - 8 - d.textlength(since, font=F_CALL), 6), since, font=F_CALL, fill=DIM)
    for i, (name, value, colour) in enumerate((("CQ sent", cq, (255, 250, 0)),
                                              ("Auto QSO", qso, (0, 255, 0)))):
        x = 8 + i * TALLY_W // 2
        d.text((x, 28), name, font=F_SMALL, fill=DIM)
        d.text((x, 50), str(value), font=F_BIG, fill=colour)
    return img


def save(img, name):
    path = os.path.join(MODEL, name)
    img.save(path + ".tmp.png")
    os.replace(path + ".tmp.png", path)


def main():
    m = Map()
    aprs, psk = Aprs(m.lat0, m.lon0), Psk(m.fields())
    aprs.start()
    psk.start()
    while True:
        cq, qsos, monday = ft8_week()
        save(draw_activity(m, aprs.recent(), psk.current(), qsos), "activity.png")
        save(draw_tally(cq, len(qsos), monday), "ft8week.png")
        time.sleep(30)


if __name__ == "__main__":
    main()
