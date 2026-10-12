#!/usr/bin/env python3
"""Live amateur activity over the UV-K5 range map, and this week's FT8 tally.

Sources
  APRS-IS        rotate.aprs2.net:14580, receive-only login (passcode -1), range filter around
                 us: VHF APRS beacons (144.640 MHz here) -- who is on the air nearby, live.
                 Packets an igate heard on RF (q-construct qAR/qAO) are drawn brighter than
                 internet-only ones.
  PSK Reporter   regional MQTT push through HAMDATA_PROXY; HTTP cache as a slow fallback:
                 FT8/FT4 stations in the region (last 30 min), coloured by band as HamClock
                 does; paths drawn when either end is on the map.
  our FT8 log    ~/influ/ft8/auto.jsonl: CQs we sent and QSOs completed since Monday 00:00
                 local time.
  WSPR.live     regional weak-signal reception reports, one bounded query per 10 min.

Writes, atomically, ~/influ/model/activity.png (transparent layer for HamClock's map box, same
projection as rangemap.png) and ~/influ/model/ft8week.png (tally for the foot of the left
column); hcdisplay lays both over the clock.
"""
import json
import logging
from email.utils import parsedate_to_datetime
import math
import os
import socket
import threading
import time
import urllib.error
import urllib.request
import urllib.parse
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
           "&flowStartSeconds=-1800&appcontact={contact}")
PSK_EVERY_S = 300                      # one query per 5 min: PSK Reporter's own limit
log = logging.getLogger(__name__)


def own_call():
    """Our callsign (station.json): the APRS-IS login and the contact in API requests."""
    try:
        with open(STATION) as f:
            return json.load(f)["call"]
    except (OSError, ValueError, KeyError):
        return "N0CALL"


CALL = own_call()
UA = {"User-Agent": f"influ-activity/1.0 ({CALL})"}

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

    def squares(self):
        corners = [self.latlon(x, y) for x in range(0, W + 1, W // 8)
                   for y in range(0, H + 1, H // 4)]
        xs = [int((p[1] + 180) // 2) for p in corners]
        ys = [int(p[0] + 90) for p in corners]
        return [f'{chr(65 + x // 10)}{chr(65 + y // 10)}{x % 10}{y % 10}'
                for x in range(min(xs), max(xs) + 1) for y in range(min(ys), max(ys) + 1)]

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
        self.nodes = {}
        self.cache = os.path.join(MODEL, "aprs_positions.json")
        try:
            with open(self.cache) as f:
                self.stations = json.load(f)
        except (OSError, ValueError):
            pass

    def run(self):
        while True:
            try:
                self.session()
            except OSError:
                pass
            time.sleep(30)

    def session(self):
        with socket.create_connection(APRS_SERVER, timeout=60) as s:
            s.sendall(f"user {CALL} pass -1 vers influ-activity 1.0 filter {self.filter}\r\n".encode())
            buf = b""
            last_filter = time.time()
            while True:
                data = s.recv(4096)
                if not data:
                    return
                buf += data
                *lines, buf = buf.split(b"\n")
                for raw in lines:
                    self.take(raw.decode("utf-8", "replace").strip())
                if time.time() - last_filter >= 60:
                    missing = self.missing()
                    extra = " b/" + "/".join(missing) if missing else ""
                    s.sendall(f"#filter {self.filter}{extra}\r\n".encode())
                    last_filter = time.time()

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
            for hop in hops:
                if hop:
                    self.nodes[hop] = now
            for a, b in zip(hops, hops[1:]):
                if a and b:
                    self.links[a, b] = now
            if "latitude" in p:
                self.stations[p["from"]] = (p["latitude"], p["longitude"], len(hops) > 1, now)

    def missing(self):
        """Subscribe to exact calls we saw in a local RF path, never guess an alias's owner."""
        now = time.time()
        with self.lock:
            self.nodes = {k: t for k, t in self.nodes.items() if now - t <= APRS_SHOW_S}
            return sorted(k for k in self.nodes if k not in self.stations
                          or now - self.stations[k][3] > 2 * APRS_SHOW_S)[:64]

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
            with open(self.cache + ".tmp", "w") as f:
                json.dump(self.stations, f)
            os.replace(self.cache + ".tmp", self.cache)
            return heard, dict(self.stations), list(self.links)


APRS_ALIASES = ("WIDE", "RELAY", "TRACE", "TCPIP", "TCPXX", "NOGATE", "RFONLY", "GATE", "ECHO")
RF_Q = ("qAR", "qAO", "qAr", "qAo")


def rf_hops(source, path):
    """The RF hops of an APRS packet: source -> each digipeater that repeated it -> the igate
    that heard it on RF. In the path, digipeaters up to the last one marked '*' have repeated
    it; a generic alias (WIDE2*...) hides who did, so None preserves that gap instead of
    inventing a direct link. 'qAR,IGATE' (or qAO) names the igate. Internet-only packets give
    just [source]."""
    if any(h.rstrip("*").upper() in ("TCPIP", "TCPXX") for h in path):
        return [source]
    q = next((i for i, hop in enumerate(path) if hop.startswith("qA")), len(path))
    if q < len(path) and path[q] not in RF_Q:
        return [source]
    digis = path[:q]
    last = max((i for i, hop in enumerate(digis) if hop.endswith("*")), default=-1)
    hops = [source] + [None if d.upper().startswith(APRS_ALIASES) else d.rstrip("*")
                       for d in digis[:last + 1]]
    if q < len(path) - 1 and path[q] in RF_Q:
        hops.append(path[q + 1])
    return [h for i, h in enumerate(hops) if i == 0 or h != hops[i - 1]]


class Psk(threading.Thread):
    """FT8/FT4 reception reports for the map's Maidenhead fields.

    One query per PSK_EVERY_S, the fields taken in turn (each query covers the last 30 min, so
    a full round of four fields still overlaps). Results and the time the next query is allowed
    are kept on disk, so restarts never query early; a 429 backs off as long as the server asks
    (at least 30 min)."""

    STATE = os.path.join(MODEL, "psk_cache.json")
    EVERY_S = PSK_EVERY_S

    def __init__(self, fields):
        super().__init__(daemon=True)
        self.fields, self.lock = fields, threading.Lock()
        self.live = None
        try:
            with open(self.STATE) as f:
                self.state = json.load(f)
        except (OSError, ValueError):
            self.state = {}
        self.state.setdefault("next", 0)                     # earliest time for the next query
        self.state.setdefault("fields", {})                  # field -> {"t": ..., "reports": [...]}

    def fetch(self, field):
        req = urllib.request.Request(PSK_URL.format(field=field, contact=CALL.lower()), headers=UA)
        with urllib.request.urlopen(req, timeout=90) as response:
            root = ET.fromstring(response.read())
        return [{k: r.get(k, "") for k in (
            "senderCallsign", "senderLocator", "receiverCallsign", "receiverLocator",
            "frequency", "mode")} for r in root.iter("receptionReport")]

    def save_state(self):
        with open(self.STATE + ".tmp", "w") as f:
            json.dump(self.state, f)
        os.replace(self.STATE + ".tmp", self.STATE)

    def run(self):
        while True:
            time.sleep(max(0, self.state["next"] - time.time()))
            last = (self.live.last_wspr_received if isinstance(self, Wspr) else self.live.last_psk_received) if self.live else -math.inf
            if time.time() - last < 120:
                time.sleep(10)
                continue
            done = self.state["fields"]
            field = min(self.fields, key=lambda f: done.get(f, {}).get("t", 0))
            wait = self.EVERY_S
            with self.lock:
                self.state["next"] = time.time() + wait  # a restart during HTTP must not query early
                self.save_state()
            try:
                reports = self.fetch(field)
                with self.lock:
                    done[field] = {"t": time.time(), "reports": reports}
                log.info("%s %s: %d reports", type(self).__name__, field, len(reports))
            except urllib.error.HTTPError as e:
                if e.code == 429:
                    retry = e.headers.get("Retry-After") or "0"
                    try:
                        wait = max(int(retry), 1800)
                    except ValueError:
                        wait = 1800
                        try:
                            wait = max(wait, parsedate_to_datetime(retry).timestamp() - time.time())
                        except (TypeError, ValueError, OverflowError):
                            pass
                log.warning("%s HTTP %s; retry in %ds", type(self).__name__, e.code, wait)
            except (OSError, ET.ParseError, ValueError, KeyError) as e:
                log.warning("%s %s; retry in %ds", type(self).__name__, type(e).__name__, wait)
            with self.lock:
                self.state["next"] = time.time() + wait
                self.save_state()

    def current(self):
        """Reports from every field fetched within the last 40 min."""
        with self.lock:
            fresh = [v["reports"] for v in self.state["fields"].values() if time.time() - v["t"] < 2400]
        return [r for reports in fresh for r in reports]


class Wspr(Psk):
    """Same cache/refresh loop as PSK, an independent region-limited WSPR source."""

    STATE = os.path.join(MODEL, "wspr_cache.json")
    EVERY_S = 600

    def __init__(self, m):
        super().__init__(["region"])
        corners = [m.latlon(x, y) for x in (0, W / 2, W) for y in (0, H / 2, H)]
        south, north = min(p[0] for p in corners), max(p[0] for p in corners)
        west, east = min(p[1] for p in corners), max(p[1] for p in corners)
        south, west = math.floor(south * 10) / 10, math.floor(west * 10) / 10
        north, east = math.ceil(north * 10) / 10, math.ceil(east * 10) / 10
        bounds = [f"({who}_lat BETWEEN {south:.1f} AND {north:.1f} AND "
                  f"{who}_lon BETWEEN {west:.1f} AND {east:.1f})" for who in ("tx", "rx")]
        self.query = (
            "SELECT tx_sign AS senderCallsign, tx_loc AS senderLocator, "
            "rx_sign AS receiverCallsign, rx_loc AS receiverLocator, frequency, 'WSPR' AS mode "
            "FROM wspr.rx WHERE time >= now() - INTERVAL 30 MINUTE "
            "AND band IN (1,3,5,7,10,14,18,21,24,28,50,144,432) AND (" +
            " OR ".join(bounds) + ") ORDER BY time DESC LIMIT 2000 FORMAT JSON")

    def fetch(self, field):
        url = "https://db1.wspr.live/?query=" + urllib.parse.quote_plus(self.query)
        with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=45) as response:
            return json.load(response)["data"]


class LivePsk(threading.Thread):
    """Regional MQTT push through this connection's proxy; never changes Clash's selectors."""

    STATE = os.path.join(MODEL, "psk_live.json")

    def __init__(self, m):
        super().__init__(daemon=True)
        self.m, self.lock, self.reports = m, threading.Lock(), {}
        self.connected, self.received = False, 0
        self.last_received = -math.inf
        self.last_psk_received = self.last_wspr_received = -math.inf
        for path in (Psk.STATE, self.STATE):
            try:
                with open(path) as f:
                    state = json.load(f)
                for field in state.get("fields", {}).values():
                    if time.time() - field["t"] < 1800:
                        for r in field["reports"]:
                            self.reports[self.key(r)] = dict(r, _seen=r.get("_seen", field["t"]))
            except (OSError, ValueError, KeyError, TypeError):
                pass

    @staticmethod
    def key(r):
        return r["senderCallsign"], r["receiverCallsign"], band(int(r["frequency"]) / 1000)[0], r["mode"]

    def take(self, payload):
        try:
            v = json.loads(payload)
            r = dict(senderCallsign=v["sc"], receiverCallsign=v["rc"], senderLocator=v["sl"],
                     receiverLocator=v["rl"], frequency=int(v["f"]), mode=v["md"], _seen=time.time())
            if not r["senderCallsign"] or not r["receiverCallsign"] or r["frequency"] <= 0:
                return
            ends = [locator(r[k]) for k in ("senderLocator", "receiverLocator")]
            if not any(ll and self.m.inside(*self.m.xy(*ll)) for ll in ends):
                return
            with self.lock:
                self.reports[self.key(r)] = r
                self.received += 1
                self.last_received = time.time()
                if r['mode'] == 'WSPR':
                    self.last_wspr_received = self.last_received
                else:
                    self.last_psk_received = self.last_received
        except (ValueError, KeyError, TypeError, AttributeError):
            return

    def run(self):
        import paho.mqtt.client as mqtt
        import socks
        client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=f"influ-{time.time_ns()}")
        proxy = urllib.parse.urlsplit(os.getenv("HAMDATA_PROXY", ""))
        if proxy.hostname:
            client.proxy_set(proxy_type=socks.HTTP, proxy_addr=proxy.hostname, proxy_port=proxy.port)
        client.tls_set()
        client.reconnect_delay_set(10, 180)

        def connected(c, userdata, flags, reason, properties):
            self.connected = not reason.is_failure
            if self.connected:
                topics = []
                for field in self.m.fields():
                    topics += [(f"pskr/filter/v2_field/+/+/{field}/+", 0),
                               (f"pskr/filter/v2_field/+/+/+/{field}", 0)]
                for grid in self.m.squares():
                    for prefix in ('pskr/filter/v2', 'wspr/filter'):
                        topics += [(f'{prefix}/+/+/+/+/{grid}/+/#', 0),
                                   (f'{prefix}/+/+/+/+/+/{grid}/#', 0)]
                c.subscribe(topics)
            log.info("PSK MQTT: %s", reason)

        def disconnected(c, userdata, flags, reason, properties):
            self.connected = False
            log.warning("PSK MQTT disconnected: %s", reason)

        client.on_connect, client.on_disconnect = connected, disconnected
        client.on_message = lambda c, u, msg: self.take(msg.payload)
        client.connect_async("mqtt.pskreporter.info", 1884, 60)
        client.loop_forever(retry_first_connection=True)

    def current(self):
        now = time.time()
        with self.lock:
            self.reports = {k: r for k, r in self.reports.items() if now - r["_seen"] < 1800}
            rows = list(self.reports.values())
            with open(self.STATE + ".tmp", "w") as f:
                json.dump(dict(fields=dict(live=dict(t=now, reports=rows))), f)
            os.replace(self.STATE + ".tmp", self.STATE)
        return rows


def digital_paths(m, reports):
    stations, bands, paths = {}, set(), {}
    for r in reports:
        try:
            name, colour = band(int(r["frequency"]) / 1000)
            ll = [locator(r[k]) for k in ("senderLocator", "receiverLocator")]
            ends = [m.xy(*p) if p else None for p in ll]
            local = [i for i, p in enumerate(ends) if p and m.inside(*p)]
            if not local:
                continue
            calls = [r["senderCallsign"], r["receiverCallsign"]]
            bands.add((name, colour))
            for i in local:
                stations.setdefault(calls[i], ends[i] + (colour, i == 0))
            if all(ends):
                key = tuple(sorted(calls)) + (name,)
                paths[key] = dict(ends=ends, local=local, calls=calls, band=name, colour=colour)
        except (ValueError, KeyError, TypeError, AttributeError):
            continue
    return stations, bands, paths


def select_paths(stations, bands, paths):
    """Show local paths plus two distant direction bundles per station, retaining every peer."""
    inside, bundles, degrees = [], {}, {k: set() for k in stations}
    for p in paths.values():
        for i in p["local"]:
            degrees.setdefault(p["calls"][i], set()).add(p["calls"][1 - i])
        if len(p["local"]) == 2:
            inside.append(p)
            continue
        i = p["local"][0]
        (x, y), (xx, yy) = p["ends"][i], p["ends"][1 - i]
        sector = int((math.degrees(math.atan2(xx - x, y - yy)) % 360) // 30)
        key = p["calls"][i], p["band"], sector
        b = bundles.setdefault(key, dict(path=p, weight=0))
        b["weight"] += 1
    chosen = list(inside)
    for call in stations:
        options = [b for k, b in bundles.items() if k[0] == call]
        options.sort(key=lambda b: (b["path"]["band"] not in ("2", "70cm"), -b["weight"],
                                    b["path"]["band"], b["path"]["calls"]))
        chosen += [b["path"] for b in options[:2]]
    return stations, bands, chosen, {k: len(v) for k, v in degrees.items()}, len(paths)


def digital_graph(m, reports):
    return select_paths(*digital_paths(m, reports))


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

    stations, drawn, paths, degrees, total = digital_graph(m, reports)
    for p in paths:
        d.line(p["ends"], fill=p["colour"] + ((155 if len(p["local"]) == 2 else 48),), width=1)

    # APRS RF hops between stations whose positions we know
    for a, b in links:
        if a in positions and b in positions:
            ends = [m.xy(*positions[a][:2]), m.xy(*positions[b][:2])]
            alpha = 175 if all(m.inside(*p) for p in ends) else 48
            d.line(ends, fill=APRS_RF + (alpha,), width=1)

    # our own QSOs, from us
    for call, grid in qsos:
        ll = locator(grid)
        if ll:
            d.line([(W / 2, H / 2), m.xy(*ll)], fill=WHITE + (230,), width=2)

    # markers and labels on top of every line
    for call, (x, y, colour, sent) in sorted(stations.items(), key=lambda kv: not kv[1][3]):
        d.polygon([(x, y - 5), (x + 5, y), (x, y + 5), (x - 5, y)], fill=colour, outline=WHITE)
        text = call + (f" +{degrees[call]}" if degrees.get(call, 0) > 2 else "")
        label(x, y, text, WHITE if sent else DIM)
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
    local_aprs = sum(m.inside(*m.xy(*v[:2])) for v in heard.values())
    parts = [(APRS_RF, f"APRS {local_aprs}"), (WHITE, f"DIGI {len(stations)}"),
             (DIM, f"PATH {len(paths)}/{total}")]
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
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    m = Map()
    from map_panel import night_layer
    aprs, psk, wspr = Aprs(m.lat0, m.lon0), LivePsk(m), Wspr(m)
    backup = Psk(m.fields())
    backup.live = psk
    wspr.live = psk
    aprs.start()
    psk.start()
    wspr.start()
    backup.start()
    solar_minute = None
    while True:
        cq, qsos, monday = ft8_week()
        heard = aprs.recent()
        reports = backup.current() + wspr.current() + psk.current()
        save(draw_activity(m, heard, reports, qsos), "activity.png")
        save(draw_tally(cq, len(qsos), monday), "ft8week.png")
        if solar_minute != int(time.time() // 60):
            save(night_layer(m), "night.png")
            solar_minute = int(time.time() // 60)
        path = os.path.join(MODEL, "activity.json")
        with open(path + ".tmp", "w") as f:
            json.dump(dict(t=time.time(), reports=reports, aprs=heard[0],
                           aprs_positions=heard[1], aprs_links=heard[2], qsos=qsos,
                           mqtt_connected=psk.connected, mqtt_received=psk.received), f)
        os.replace(path + ".tmp", path)
        time.sleep(5)


if __name__ == "__main__":
    main()
