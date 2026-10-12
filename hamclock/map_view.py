"""Zoomable range map in the HamClock window: a pinch or the wheel zooms about the pointer,
a two-finger scroll, a drag or the arrow keys pan, + and - zoom about the middle.

The ground is a tile pyramid build_tiles.py renders once from local elevation tiles (relief
and line-of-sight coverage, down to ~33 m). For a view, a worker process pastes the tiles of
the next finer level and scales them (Lanczos) to exactly the screen's pixels: first the view
itself, in about a tenth of a second, then half a view more each side so a drag stays sharp.
While the view moves the latest picture is only rescaled, and new ones are asked for as soon
as the worker is free, so a long zoom sharpens on the way. Without the pyramid the worker
draws from the elevation tiles instead, more slowly.

Everything over the ground (rivers, grid, range rings, campuses, places, stations, contact
paths, APRS) is vector, drawn each frame at fixed screen sizes. Which labels show from which
zoom, and on which side of their point, is worked out per zoom step in a second worker, as
web maps place labels: panning never moves a label and zooming only adds them. The contact
paths shown are chosen once for the whole map, not per view.

Coordinates: base px are rangemap.png's, W x H, azimuthal equidistant about us (activity.Map).
A View maps them to view px of the same W x H box at its zoom; the screen shows that box at
`scale` physical px a view px (the map box is 1056 px wide on the X230: 0.8).
"""
import json
import logging
import math
import multiprocessing
import os
import time
from collections import Counter, OrderedDict
from itertools import islice
from concurrent.futures import ProcessPoolExecutor
from concurrent.futures.process import BrokenProcessPool

import numpy as np
import pygame
import pygame.freetype
from PIL import Image

import activity as a
import build_rangemap as terrain
import build_tiles as tiles
import flags
from map_panel import night_rgba, solar_elevation

W, H = a.W, a.H
MAX_ZOOM = 96
NOTCH = 0.18                  # log zoom per wheel notch (x1.2)
EASE_S = 0.065                # the wheel's zoom eases toward its goal with this time constant
REST_S = 0.09                 # the view must rest this long before it counts as still
FADE_S = 0.15                 # cross-fade to a sharper picture arriving at rest
RETRY_S = 30                  # after a failed picture
MARGIN = 0.5                  # wide pictures reach this many view widths past each side
LABEL_ZOOMS = tuple(2 ** (k / 2) for k in range(14))       # label placement steps, 1 .. 90.5
CAMPUS_ZOOM = 8               # campuses (outline, dot, name) show from this zoom on
MILITARY_ZOOM = 4             # military areas (outline, dot) from this zoom on, named from 6x
MILITARY = (255, 85, 70)
GOVERNMENT, REGULATOR = (240, 214, 130), (90, 215, 255)  # offices; radio regulators: as military
FAINT = {GOVERNMENT: 165}     # layers drawn see-through (alpha): the offices stay in the background
EMBASSY = (150, 250, 170)     # consulates and embassies: named with a mini flag at the left
LEAD = (160, 160, 150)        # the hairline tying a label placed off its point back to it
EMBASSY_ZOOM = 6              # their names show from this zoom on, as the regulators' do
EMBASSIES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "embassies.json")
OFFICES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "government.json")
OUTLINE_PX = 4                # an area this small on screen (physical px) is drawn as its dot
DOTS = 300                    # past this many dots in view, they are written as pixels
NEW_GLYPHS = 12               # labels first rendered in one frame while the view moves
FONTS = {"latin": ("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 0),
         "cjk": ("/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc", 2)}   # 2: SC
CITY, TOWN, GREY, CAMPUS = (230, 230, 230), (150, 150, 150), (139, 141, 139), (255, 130, 210)
AIRPORT, RUNWAY = (150, 175, 255), (205, 205, 210)
RING_ZOOM = 6                 # below this zoom an airport also gets a ring: its runways are specks
CITY_ZOOM = 4                 # from this zoom province borders give way to city and county
                              # outlines, and towns lose their dots (their names stay, centred)
SUB_ZOOM = 12                 # Maidenhead subsquares (5' x 2.5') show from this zoom on
# translucent lines: colour (RGBA), physical px width, dashes (on, off, ... px) or solid
GRID_FIELD, GRID_SQUARE, GRID_SUB = (175, 180, 255, 170), (175, 180, 255, 90), (175, 180, 255, 45)
GRID_TEXT, GRID_SUB_TEXT = (185, 190, 255), (145, 150, 210)
PROVINCE = (255, 255, 255, 190), 2, (9, 3, 2, 3)             # dash-dot, as on printed maps
PREFECTURE = (255, 255, 255, 150), 2, (7, 4)
COUNTY = (255, 255, 255, 70), 1, None
RING = (190, 190, 190, 90)
log = logging.getLogger(__name__)


class View:
    """`zoom` about `centre` (base px): base px <-> view px."""

    def __init__(self, base, zoom=1.0, centre=(W / 2, H / 2)):
        self.base, self.zoom, self.centre = base, zoom, tuple(centre)
        self.lat0, self.lon0 = base.lat0, base.lon0
        self.km_per_px = base.km_per_px / zoom

    def project(self, p):
        """Base px -> view px (numbers, or arrays of x and of y)."""
        return (W / 2 + (p[0] - self.centre[0]) * self.zoom,
                H / 2 + (p[1] - self.centre[1]) * self.zoom)

    def unproject(self, p):
        return (self.centre[0] + (p[0] - W / 2) / self.zoom,
                self.centre[1] + (p[1] - H / 2) / self.zoom)

    def points(self, p):
        """(..., 2) array of base px -> view px."""
        return (p - self.centre) * self.zoom + (W / 2, H / 2)

    def rect(self):
        """The base px rectangle in view: left, top, right, bottom."""
        return (*self.unproject((0, 0)), *self.unproject((W, H)))

    def xy(self, lat, lon):
        return self.project(self.base.xy(lat, lon))

    def latlon(self, x, y):
        return self.base.latlon(*self.unproject((x, y)))

    def grid_latlon(self, x, y):
        """latlon() for arrays of view px."""
        return grid_latlon(self.base, *self.unproject((x, y)))

    @staticmethod
    def inside(x, y, margin=4):
        return margin <= x < W - margin and margin <= y < H - margin


def grid_latlon(base, bx, by):
    """Lat/lon of arrays of base px."""
    dx, dy = (bx - W / 2) * base.km_per_px, (by - H / 2) * base.km_per_px
    return terrain.destination(base.lat0, base.lon0, np.degrees(np.arctan2(dx, -dy)), np.hypot(dx, dy))


def inside(p, margin=4):
    """inside() for a (..., 2) array of view px."""
    return ((p[..., 0] >= margin) & (p[..., 0] < W - margin)
            & (p[..., 1] >= margin) & (p[..., 1] < H - margin))


def clamp(centre, zoom):
    """The nearest centre that keeps a view at `zoom` inside the map."""
    hw, hh = W / 2 / zoom, H / 2 / zoom
    return min(W - hw, max(hw, centre[0])), min(H - hh, max(hh, centre[1]))


class Camera:
    """The wheel sets a zoom goal the view eases to, a pinch zooms at once; either holds the
    base point under the pointer where it is on screen. A pan moves the centre. The view never
    leaves the map."""

    def __init__(self, base):
        self.base = base
        self.zoom = self.goal = 1.0
        self.centre = (W / 2, H / 2)
        self.anchor = self.anchor_base = (W / 2, H / 2)   # this view px stays over that base px

    def view(self):
        return View(self.base, self.zoom, self.centre)

    def at(self, zoom):
        """The view at `zoom` with the anchor held."""
        return View(self.base, zoom, clamp((self.anchor_base[0] - (self.anchor[0] - W / 2) / zoom,
                                            self.anchor_base[1] - (self.anchor[1] - H / 2) / zoom),
                                           zoom))

    def scroll(self, notches, at):
        """The wheel turned `notches` (+ in) with the pointer at view px `at`; True if the goal
        changed."""
        goal = min(MAX_ZOOM, max(1.0, self.goal * math.exp(max(-8, min(8, notches)) * NOTCH)))
        if goal == self.goal:
            return False
        self.anchor, self.anchor_base = at, self.view().unproject(at)
        self.goal = goal
        return True

    def pinch(self, ratio, at):
        """Fingers spread by `ratio` about view px `at`; the zoom follows them at once."""
        zoom = min(MAX_ZOOM, max(1.0, self.zoom * ratio))
        if zoom == self.zoom:
            return False
        self.anchor, self.anchor_base = at, self.view().unproject(at)
        self.zoom = self.goal = zoom
        self.centre = self.at(zoom).centre
        return True

    def pan(self, dx, dy):
        """Moved by (dx, dy) view px; True if the view moved."""
        centre = clamp((self.centre[0] - dx / self.zoom, self.centre[1] - dy / self.zoom), self.zoom)
        if centre == self.centre:
            return False
        self.centre = centre
        self.anchor_base = self.view().unproject(self.anchor)
        return True

    def tick(self, dt):
        """Ease the zoom toward the goal; True if the view moved."""
        if self.zoom == self.goal:
            return False
        step = 1 - math.exp(-min(dt, 0.05) / EASE_S)
        zoom = self.zoom * (self.goal / self.zoom) ** step
        self.zoom = self.goal if abs(math.log(zoom / self.goal)) < 1e-4 else zoom
        self.centre = self.at(self.zoom).centre
        return True


# ------------------------------------------------------------------------- text ----

_fonts = {}


def font(kind, px):
    """FreeType face for "latin" or "cjk" text at `px` physical pixels, antialiased and padded
    to its line height so labels share a baseline."""
    if (kind, px) not in _fonts:
        path, index = FONTS[kind]
        f = pygame.freetype.Font(path if os.path.exists(path) else None, px, font_index=index)
        f.antialiased, f.pad = True, True
        _fonts[kind, px] = f
    return _fonts[kind, px]


def kind_of(text):
    return "cjk" if any(ord(ch) > 0x2e7f for ch in text) else "latin"


def text_px(size, scale):
    return max(9, round(size * scale))


def text_size(text, size, scale):
    """The outlined label's size in view px."""
    r = font(kind_of(text), text_px(size, scale)).get_rect(text)
    return (r.width + 2) / scale, (r.height + 2) / scale


AROUND = 8                    # spots right beside a label's point, all a label normally tries
STACK = 7                     # rows a stack column holds, the nearest first
SPOTS = 9 + 2 * STACK         # then 8, centred on the point, and the stack's two columns
SAME_PLACE = 0.004            # base px (~4 m): points this close share a building


def spot(k, tw, th=0):
    """Where a label in spot `k` puts its top left, from its point (view px). 0..7 beside the
    point: right, left, above, below, then the same a little further out; 8 centred on it (a
    town's name once its dot is gone); 9 on, only for points that share a building with others:
    a stack of rows right of the point, the nearest row first, then the same left of it."""
    if k < 9:
        return ((7, -7), (-tw - 7, -7), (-tw / 2, -22), (-tw / 2, 7),
                (18, -7), (-tw - 18, -7), (-tw / 2, -33), (-tw / 2, 18), (-tw / 2, -th / 2))[k]
    col, row = divmod(k - 9, STACK)
    step = (row + 1) // 2 * (1 if row % 2 else -1)          # 0, +1, -1, +2, -2 ...
    return (7 if col == 0 else -tw - 7, -th / 2 + step * (th + 1))


def offsets(k, tw, th):
    """spot() over arrays: the (dx, dy) of every label at once."""
    k = np.asarray(k)
    stack, col, row = k >= 9, (k - 9) // STACK, (k - 9) % STACK
    step = (row + 1) // 2 * np.where(row % 2 == 1, 1, -1)
    dx = np.select([k == 0, k == 1, k == 4, k == 5, stack & (col == 0), stack],
                   [7, -tw - 7, 18, -tw - 18, 7, -tw - 7], -tw / 2)
    dy = np.select([k == 2, k == 3, k == 6, k == 7, k == 8, stack],
                   [-22, 7, -33, 18, -th / 2, -th / 2 + step * (th + 1)], -7)
    return dx, dy


def flag_box(scale, size=13):
    """The mini flag at the left of a consulate's label, physical px: (width, gap, pad), the
    pad in view px by which the label's box reaches further left."""
    height = text_px(size, scale)
    width, gap = round(height * flags.RATIO), max(2, round(2 * scale))
    return width, gap, (width + gap) / scale + 1


def place_labels(items, scale, pads=None, limits=None):
    """For labels in priority order, each (text, base xy, size, from zoom, centred from zoom):
    their sizes (view px) and, per LABEL_ZOOMS step, the spot each takes (see spot()) or -1. At each
    step a label keeps its earlier spot if that is still clear, else takes the first clear one,
    else waits; nothing overlaps, and no label leaves the map. `pads` widens a label's box to
    the left by that much (view px), for what is drawn there: a consulate's flag. `limits` says
    how many spots each label may try: the eight beside its point, or all of them (the stack)
    for a point sharing its building with others."""
    pygame.freetype.init()
    sizes = [text_size(text, size, scale) for text, _, size, *_ in items]
    pads = [0.0] * len(items) if pads is None else pads
    limits = [AROUND] * len(items) if limits is None else limits
    spots = np.full((len(items), len(LABEL_ZOOMS)), -1, np.int8)
    cell = 128
    for li, z in enumerate(LABEL_ZOOMS):
        grid = {}

        def free(box):
            for cx in range(int(box[0] // cell), int(box[2] // cell) + 1):
                for cy in range(int(box[1] // cell), int(box[3] // cell) + 1):
                    for b in grid.get((cx, cy), ()):
                        if box[0] < b[2] and b[0] < box[2] and box[1] < b[3] and b[1] < box[3]:
                            return False
            return True

        def take(box):
            for cx in range(int(box[0] // cell), int(box[2] // cell) + 1):
                for cy in range(int(box[1] // cell), int(box[3] // cell) + 1):
                    grid.setdefault((cx, cy), []).append(box)

        take((W / 2 * z - 11, H / 2 * z - 11, W / 2 * z + 11, H / 2 * z + 11))        # us
        for i, ((text, (x, y), size, start, centred), (tw, th)) in enumerate(zip(items, sizes)):
            if z < start * 0.999:
                continue
            before = int(spots[i, li - 1]) if li else -1
            order = ([before] if before >= 0 and before != 8 else []) + \
                [k for k in range(limits[i]) if k not in (before, 8)]
            if z >= centred * 0.999:                     # centred if it can be, else beside
                order = [8] + order
            for k in order:
                dx, dy = spot(k, tw, th)
                box = (x * z + dx - pads[i] - 1, y * z + dy, x * z + dx + tw + 1, y * z + dy + th + 2)
                if box[0] >= 0 and box[1] >= 0 and box[2] <= W * z and box[3] <= H * z and free(box):
                    take(box)
                    spots[i, li] = k
                    break
    return np.array(sizes).reshape(-1, 2), spots


# ------------------------------------------------- scene worker: what goes on the map ----

_static = {}
SHORT = ((" General Aviation Airport", " GA"), (" International Airport", ""), (" Air Base", " AB"),
         (" Airbase", " AB"), (" Airport", ""))


def airport_label(code, name):
    """An airport by its code; one without (an air base, a general aviation field) by its
    name, shortened."""
    if code:
        return code
    for long, short in SHORT:
        name = name.replace(long, short)
    return " ".join(name.split())


def areas(base, path):
    """Named areas (campuses, military, government) from their file: outlines and the
    point for the name, in base px; those on the map."""
    out = []
    if os.path.exists(path):
        with open(path) as f:
            for c in json.load(f):
                rings = [r for r in (np.array([base.xy(*p) for p in ring]) for ring in c["rings"]) if len(r) > 2]
                point = base.xy(*c["point"])
                if 0 <= point[0] < W and 0 <= point[1] < H:
                    out.append(dict(name=c["name"], rings=rings, point=point, radio=c.get("radio", False),
                                    zoom=c.get("zoom", 6)))
    return out


def static_scene(base):
    """Places, campuses, military areas, government offices and radio regulators, consulates
    and embassies, airports with their runways, and the farthest point in base px; kept while
    their files stay."""
    paths = [os.path.join(a.MODEL, f) for f in ("rangemap_osm.json", "rangemap.json", "campuses.json",
                                                 "airports.json", "military.json")] + [OFFICES]
    key = tuple(os.path.getmtime(p) if os.path.exists(p) else 0 for p in paths + [EMBASSIES])
    if _static.get("key") == key:
        return _static
    with open(paths[0]) as f:
        osm = json.load(f)
    with open(paths[1]) as f:
        info = json.load(f)
    rank = {"yes": 0, "2": 0, "4": 1, "5": 2, "6": 3}
    places = sorted((p for p in osm["places"] if p.get("name")),
                    key=lambda p: (rank.get(p.get("capital", ""), 4), -p.get("pop", 0)))
    xy = np.array([base.xy(p["lat"], p["lon"]) for p in places]).reshape(-1, 2)
    campuses, military, offices = areas(base, paths[2]), areas(base, paths[4]), areas(base, paths[5])
    embassies = []
    if os.path.exists(EMBASSIES):
        with open(EMBASSIES, encoding="utf-8") as f:
            for e in json.load(f):
                point = base.xy(*e["point"])
                if 0 <= point[0] < W and 0 <= point[1] < H:
                    embassies.append(dict(name=e["name"], code=e["code"], point=point, rings=[],
                                          zoom=EMBASSY_ZOOM))
    airports, runways = [], []
    if os.path.exists(paths[3]):
        with open(paths[3]) as f:
            found = json.load(f)
        for x in sorted(found["airports"], key=lambda x: not x["code"]):          # coded first
            point = base.xy(*x["point"])
            if 0 <= point[0] < W and 0 <= point[1] < H:
                airports.append(dict(text=airport_label(x["code"], x["name"]), point=point))
        runways = [dict(line=np.array([base.xy(*p) for p in r["line"]]), width=r["width"], area=r["area"])
                   for r in found["runways"]]
    _static.clear()
    _static.update(key=key, place_names=[p["name"] for p in places], place_xy=xy,
                   place_city=[rank.get(p.get("capital", ""), 4) <= 2 for p in places],   # prefecture seat or up
                   airports=airports, runways=runways,
                   place_km=np.hypot(*(xy - (W / 2, H / 2)).T) * base.km_per_px,
                   name_km=info["reach_km"] * 1.1, campuses=campuses, military=military,
                   government=[c for c in offices if not c["radio"]],
                   regulators=[c for c in offices if c["radio"]], embassies=embassies,
                   far=(base.xy(info["far_lat"], info["far_lon"]),
                        f"{info['reach_km']:.0f} km {info['bearing']:.0f} deg {info['far_elev_m']:.0f} m"))
    return _static


_part = {}


def static_part(static, scale):
    """Everything static the scene draws, with its labels (campuses, military areas and
    government offices from the zoom each is named at, the larger area first, then towns)
    placed for every zoom step; worked out once for each version of static_scene's files."""
    key = (static["key"], scale)
    if _part.get("key") == key:
        return _part
    items, kinds = [], []
    for c in static["campuses"]:
        if c["name"]:
            items.append((c["name"], c["point"], 15, CAMPUS_ZOOM, math.inf))
            kinds.append(("campus", CAMPUS))
    size = lambda c: max((abs(r[:, 0] @ np.roll(r[:, 1], -1) - np.roll(r[:, 0], -1) @ r[:, 1]) for r in c["rings"]),
                         default=0)
    # consulates rank with the offices: same list, same order of priority
    for kind, colour, c in sorted([("military", MILITARY, c) for c in static["military"] if c["name"]]
                                  + [("government", GOVERNMENT, c) for c in static["government"] if c["name"]]
                                  + [("embassy", EMBASSY, c) for c in static["embassies"]],
                                  key=lambda k: (k[2]["zoom"], -size(k[2]))):
        items.append((c["name"], c["point"], 13, c["zoom"], math.inf))
        kinds.append((kind, colour, c["code"]) if kind == "embassy" else (kind, colour))
    places(static, items, kinds, False)
    pads = np.array([flag_box(scale)[2] if kind[0] == "embassy" else 0.0 for kind in kinds])
    # a point sharing its building with others gets the stack beside it, so that each name in
    # the building can show; every other label stays right beside its own point
    building = [(round(xy[0] / SAME_PLACE), round(xy[1] / SAME_PLACE)) for _, xy, *_ in items]
    shared = {b for b, n in Counter(building).items() if n > 1}
    limits = [SPOTS if b in shared else AROUND for b in building]
    sizes, spots = place_labels(items, scale, pads, limits)
    part = dict(key=key, places=static["place_xy"], campuses=static["campuses"], military=static["military"],
                government=static["government"], regulators=static["regulators"],
                embassies=static["embassies"], pads=pads,
                airports=[x["point"] for x in static["airports"]], runways=static["runways"], far=static["far"][0],
                labels=[(text, xy, size) for text, xy, size, *_ in items], kinds=kinds, sizes=sizes, spots=spots)
    _part.clear()
    _part.update(static_arrays(part))
    return _part


def places(static, items, kinds, cities):
    """Place names (cities, else towns) as labels; on the whole map only those within our reach
    are named, as on build_rangemap.py's."""
    for i, (name, xy, km, city) in enumerate(zip(static["place_names"], static["place_xy"],
                                                 static["place_km"], static["place_city"])):
        if city == cities:
            items.append((name, tuple(xy), 14, 1 if km <= static["name_km"] else 2, CITY_ZOOM))
            kinds.append(("place", CITY, i))


def label_boxes(xy, sizes, k, z, pads=None):
    """The boxes (zoomed px) place_labels gives labels at base px `xy` in spots `k` at zoom `z`."""
    dx, dy = offsets(k, sizes[:, 0], sizes[:, 1])
    pad = np.zeros(len(xy)) if pads is None else pads
    x0, y0 = xy[:, 0] * z + dx - pad - 1, xy[:, 1] * z + dy
    return np.stack([x0, y0, x0 + sizes[:, 0] + 2, y0 + sizes[:, 1] + 2], axis=1)


def overlapping(boxes, others):
    """Which of `boxes` overlap any of `others` (both (n, 4))."""
    hit = np.zeros(len(boxes), bool)
    if not len(boxes) or not len(others):
        return hit
    order = np.argsort(boxes[:, 0])
    x0 = boxes[order, 0]
    widest = (boxes[:, 2] - boxes[:, 0]).max()
    for b in others:
        near = order[np.searchsorted(x0, b[0] - widest):np.searchsorted(x0, b[2])]
        hit[near[(boxes[near, 2] > b[0]) & (boxes[near, 1] < b[3]) & (boxes[near, 3] > b[1])]] = True
    return hit


def load_scene(base, scale, have=None):
    """activity.json and the static layers -> everything the scene draws, in base px, with the
    labels placed for every zoom step: the dynamic ones (and cities, airports, regulators) now,
    over the static part's, which they hide where they meet. The static part itself goes along
    only if `have` (the key of the one the display holds) is not it."""
    with open(os.path.join(a.MODEL, "activity.json")) as f:
        data = json.load(f)
    static = static_scene(base)
    stations, bands, chosen, degrees, total = a.digital_graph(base, data["reports"])
    positions = data.get("aprs_positions") or data.get("aprs", {})
    aprs = sorted(((call, base.xy(*v[:2]), bool(v[2])) for call, v in data.get("aprs", {}).items()),
                  key=lambda c: not c[2])                                    # RF heard first
    qsos = [(call, base.xy(*ll)) for call, grid in data.get("qsos", []) if (ll := a.locator(grid))]
    senders_first = sorted(stations.items(), key=lambda kv: not kv[1][3])
    items, kinds = [], []                      # labels in priority order, and what each labels
    for call, xy in qsos:
        items.append((call, xy, 13, 1, math.inf))
        kinds.append(("qso", a.WHITE))
    for call, (x, y, colour, sent) in senders_first:
        items.append((call + (f" +{degrees[call]}" if degrees.get(call, 0) > 2 else ""), (x, y), 13, 1, math.inf))
        kinds.append(("station", a.WHITE if sent else a.DIM))
    for x in static["airports"]:
        if x["text"]:
            items.append((x["text"], x["point"], 13, 1, math.inf))
            kinds.append(("airport", AIRPORT))
    places(static, items, kinds, True)         # cities come before APRS calls, towns after all
    for c in static["regulators"]:             # radio regulators: never crowded out
        if c["name"]:
            items.append((c["name"], c["point"], 13, 6, math.inf))
            kinds.append(("regulator", REGULATOR))
    for call, xy, rf in aprs:
        if 0 <= xy[0] < W and 0 <= xy[1] < H:
            items.append((call, xy, 13, 1, math.inf))
            kinds.append(("aprs", a.APRS_RF if rf else a.APRS_NET))
    items.append((static["far"][1], static["far"][0], 13, 1, math.inf))
    kinds.append(("far", terrain.SEEN))
    sizes, spots = place_labels(items, scale)
    part = static_part(static, scale)
    xy = np.array([xy for _, xy, *_ in items], dtype=float).reshape(-1, 2)
    part_spots = part["spots"].copy()
    for li, z in enumerate(LABEL_ZOOMS):
        mine, theirs = spots[:, li] >= 0, np.flatnonzero(part_spots[:, li] >= 0)
        if mine.any() and len(theirs):
            under = overlapping(label_boxes(part["label_xy"][theirs], part["sizes"][theirs], part_spots[theirs, li], z,
                                            part["pads"][theirs]),
                                label_boxes(xy[mine], sizes[mine], spots[mine, li], z))
            part_spots[theirs[under], li] = -1
    return dynamic_arrays(dict(
        key=part["key"], part=None if have == part["key"] else part, part_spots=part_spots,
        stations=[((x, y), colour) for _, (x, y, colour, _) in senders_first],
        lines=[(p["ends"], p["colour"], 155 if len(p["local"]) == 2 else 48) for p in chosen],
        total=total, bands=dict(bands), aprs=[(xy, rf) for _, xy, rf in aprs],
        links=[(base.xy(*positions[x][:2]), base.xy(*positions[y][:2]))
               for x, y in data.get("aprs_links", []) if x in positions and y in positions],
        qsos=[xy for _, xy in qsos], labels=[(text, xy, size) for text, xy, size, *_ in items],
        kinds=kinds, sizes=sizes, spots=spots))


def base_xy(base, lat, lon):
    """activity.Map.xy for arrays of lat/lon: (..., 2) base px."""
    km, bearing = terrain.polar(base.lat0, base.lon0, lat, lon)
    r, b = km / base.km_per_px, np.radians(bearing)
    return np.stack([W / 2 + r * np.sin(b), H / 2 - r * np.cos(b)], axis=-1)


def load_admin(base):
    """admin.json as Lines per admin level (4 province, 5 prefecture, 6 county), its boundary
    ways joined end to end: fewer, longer lines draw faster."""
    with open(os.path.join(a.MODEL, "admin.json")) as f:
        found = json.load(f)["lines"]
    by_level = {}
    for line in found:
        by_level.setdefault(line["level"], []).append([tuple(p) for p in line["p"]])
    out = {}
    for level, ways in by_level.items():
        chains = [np.array(c, dtype=float) for c in tiles.rings(ways)]
        out[level] = Lines([base_xy(base, c[:, 0], c[:, 1]) for c in chains], cell=2)   # 1-2 px lines
    return out


def points(rows):
    return np.array(rows, dtype=float).reshape(-1, 2)


def place_rows(kinds):
    """Rows of `kinds` that label places, and which place each labels."""
    rows = [i for i, kind in enumerate(kinds) if kind[0] == "place"]
    return np.array(rows, dtype=int), np.array([kinds[i][2] for i in rows], dtype=int)


def static_arrays(d):
    """The static part's points and boxes as arrays, for projecting all at once."""
    d["label_xy"] = points([xy for _, xy, _ in d["labels"]])
    d["airport_xy"] = points(d["airports"])
    d["runway_box"] = np.array([(*r["line"].min(0), *r["line"].max(0)) for r in d["runways"]]).reshape(-1, 4)
    d["label_campus"] = np.array([kind[0] == "campus" for kind in d["kinds"]], dtype=bool)
    d["embassy_xy"] = points([e["point"] for e in d["embassies"]])
    for kind, key in (("campus", "campuses"), ("military", "military"), ("government", "government"),
                      ("regulator", "regulators")):
        boxes = [np.vstack(c["rings"]) if c["rings"] else np.array([c["point"]]) for c in d[key]]
        d[kind + "_box"] = np.array([(*b.min(0), *b.max(0)) for b in boxes]).reshape(-1, 4)
    d["place_rows"], d["place_index"] = place_rows(d["kinds"])
    return d


def dynamic_arrays(d):
    """The same for a scene's dynamic layers (worked out in the worker, so taking a new scene
    costs a frame nothing)."""
    d["station_xy"] = points([xy for xy, _ in d["stations"]])
    d["line_ends"] = np.array([ends for ends, _, _ in d["lines"]], dtype=float).reshape(-1, 2, 2)
    d["aprs_xy"] = points([xy for xy, _ in d["aprs"]])
    d["aprs_rf"] = np.array([rf for _, rf in d["aprs"]], dtype=bool)
    d["link_ends"] = np.array(d["links"], dtype=float).reshape(-1, 2, 2)
    d["qso_xy"] = points(d["qsos"])
    d["label_xy"] = points([xy for _, xy, _ in d["labels"]])
    d["place_rows"], d["place_index"] = place_rows(d["kinds"])
    return d


STATIC = ("key", "places", "campuses", "military", "government", "regulators", "embassies", "airports",
          "runways", "far", "airport_xy", "embassy_xy", "runway_box", "campus_box", "military_box",
          "government_box", "regulator_box")


def combined(d, part):
    """A scene to draw: the dynamic layers and labels first, then the static part's."""
    out = dict(d)
    out.update({k: part[k] for k in STATIC})
    n = len(d["labels"])
    out["labels"], out["kinds"] = d["labels"] + part["labels"], d["kinds"] + part["kinds"]
    out["sizes"] = np.vstack([d["sizes"].reshape(-1, 2), part["sizes"]])
    out["spots"] = np.vstack([d["spots"], d["part_spots"]])
    out["label_xy"] = np.vstack([d["label_xy"], part["label_xy"]])
    out["label_campus"] = np.concatenate([np.zeros(n, bool), part["label_campus"]])
    labelled = np.zeros((len(part["places"]), len(LABEL_ZOOMS)), bool)
    labelled[d["place_index"]] = d["spots"][d["place_rows"]] >= 0
    labelled[part["place_index"]] = d["part_spots"][part["place_rows"]] >= 0
    out["place_labelled"] = labelled
    return out


# ------------------------------------------------- picture worker: the ground ----

_tiles, _grounds = OrderedDict(), OrderedDict()


def pyramid():
    path = os.path.join(tiles.TILES, "levels.json")
    if not os.path.exists(path):
        return None
    with open(path) as f:
        return json.load(f)


def tile(level, col, row):
    """A pyramid tile, decoded and kept for later (the latest few hundred)."""
    key = level, col, row
    if key in _tiles:
        _tiles.move_to_end(key)
        return _tiles[key]
    path = os.path.join(tiles.TILES, str(level), f"{col}_{row}.png")
    picture = Image.open(path).convert("RGB") if os.path.exists(path) else None
    _tiles[key] = picture
    if len(_tiles) > 600:
        _tiles.popitem(last=False)
    return picture


def ground(base, origin, scale, size):
    """The ground where picture px (i, j) covers base px origin + (i, j) / scale, size w x h:
    from the pyramid level just finer than `scale`, else drawn from elevation tiles. Also the
    metres a terrain pixel. The latest few are kept for re-shading."""
    key = origin, scale, size
    if key in _grounds:
        return _grounds[key]
    info = pyramid()
    if info:
        level = next((L for L in info["levels"] if L >= scale * 0.999), info["levels"][-1])
        T = info["tile"]
        x0, y0 = origin[0] * level, origin[1] * level
        x1, y1 = x0 + size[0] * level / scale, y0 + size[1] * level / scale
        cols, rows = math.ceil(W * level / T), math.ceil(H * level / T)
        c0, r0 = max(0, math.floor(x0 / T)), max(0, math.floor(y0 / T))
        c1, r1 = min(cols - 1, math.ceil(x1 / T) - 1), min(rows - 1, math.ceil(y1 / T) - 1)
        canvas = Image.new("RGB", ((c1 - c0 + 1) * T, (r1 - r0 + 1) * T))
        for r in range(r0, r1 + 1):
            for c in range(c0, c1 + 1):
                if (t := tile(level, c, r)) is not None:
                    canvas.paste(t, ((c - c0) * T, (r - r0) * T))
        box = (x0 - c0 * T, y0 - r0 * T, x1 - c0 * T, y1 - r0 * T)
        picture = canvas.resize(size, Image.Resampling.LANCZOS, box=box)
        metres = tiles.dem_metres(level, base.km_per_px, base.lat0)
    else:
        x0, y0 = round(origin[0] * scale), round(origin[1] * scale)
        picture = Image.fromarray(tiles.render_ground(base, scale, x0, y0, x0 + size[0], y0 + size[1]))
        metres = tiles.dem_metres(scale, base.km_per_px, base.lat0)
    _grounds[key] = picture, metres
    if len(_grounds) > 4:
        _grounds.popitem(last=False)
    return picture, metres


def shade(base, origin, scale, size, when):
    """The ground with night laid on for time `when`, as RGB bytes with a 1 px border that
    repeats the edge (picture px (i, j) at (i + 1, j + 1)); and the metres a terrain pixel."""
    picture, metres = ground(base, origin, scale, size)
    step = 8                                         # night is smooth: every 8th pixel
    y, x = np.mgrid[0:size[1] + step:step, 0:size[0] + step:step] + step / 2
    lat, lon = grid_latlon(base, origin[0] + x / scale, origin[1] + y / scale)
    night = Image.fromarray(night_rgba(lat, lon, when)).resize(
        size, Image.Resampling.BILINEAR, box=(0, 0, size[0] / step, size[1] / step))
    rgb = np.asarray(Image.alpha_composite(picture.convert("RGBA"), night).convert("RGB"))
    return np.pad(rgb, ((1, 1), (1, 1), (0, 0)), mode="edge").tobytes(), metres


# ------------------------------------------------------------------ vector scene ----

def thin(points, zoom):
    """Drop polyline points sharing a view px at `zoom` with the one before; keep the ends."""
    cells = np.floor(points * zoom)
    keep = np.r_[True, np.any(np.diff(cells, axis=0), axis=1)]
    keep[-1] = True
    return points[keep]


def on_dash(t, pattern):
    """Which distances `t` (px) along a line fall on a dash of `pattern` (on, off, ... px)."""
    phase, on, edge = t % sum(pattern), np.zeros(len(t), bool), 0
    for k, length in enumerate(pattern):
        if k % 2 == 0:
            on |= (phase >= edge) & (phase < edge + length)
        edge += length
    return on


def stamp_dashes(surface, xy, along, segments, colour, width, pattern):
    """Dashed lines as pixels, all at once: every pixel step along each segment (from point i
    to i + 1 of `xy`, px) whose distance along its line (`along`, px, measured from the line's
    start, so the dashes keep their places as the view pans) falls on a dash, set to `colour`
    (RGBA) in `surface`, 1 or 2 px wide."""
    a0, a1 = along[segments], along[segments + 1]
    t0 = np.ceil(a0)
    n = np.maximum(0, np.ceil(a1) - t0).astype(int)
    if not n.sum():
        return
    which = np.repeat(np.arange(len(segments)), n)
    t = t0[which] + (np.arange(len(which)) - np.repeat(np.cumsum(n) - n, n))
    on = on_dash(t, pattern)
    which, t = which[on], t[on]
    f = ((t - a0[which]) / (a1 - a0)[which])[:, None]
    p0, p1 = xy[segments[which]], xy[segments[which] + 1]
    px, py = np.round(p0 + f * (p1 - p0)).astype(int).T
    w, h = surface.get_size()
    pixels, value = pygame.surfarray.pixels2d(surface), surface.map_rgb(colour) & 0xFFFFFFFF   # one write a pixel
    for dx, dy in ((0, 0), (1, 0), (0, 1), (1, 1))[:1 if width < 2 else 4]:
        x, y = px + dx, py + dy
        ok = (x >= 0) & (x < w) & (y >= 0) & (y < h)
        pixels[x[ok], y[ok]] = value
    del pixels                                           # unlock the surface


class Lines:
    """Polylines in base px, kept per zoom level thinned for it (a point every `cell` view px),
    cut into pieces of at most PIECE points with their bounding boxes (so a view only touches
    the pieces it shows), each point knowing its distance along its whole line (so dashes run
    on across pieces)."""
    PIECE = 48

    def __init__(self, lines, cell=1):
        self.levels = {}
        for level in (1, 2, 4, 8, 16, 32):
            pieces, along, owner = [], [], []
            for n, line in enumerate(lines):
                p = thin(line, level / cell)
                if len(p) < 2:
                    continue
                d = np.r_[0, np.cumsum(np.hypot(*np.diff(p, axis=0).T))]
                for i in range(0, len(p) - 1, self.PIECE - 1):          # pieces share their ends
                    pieces.append(p[i:i + self.PIECE])
                    along.append(d[i:i + self.PIECE])
                    owner.append(n)
            points = np.vstack(pieces) if pieces else np.empty((0, 2))
            starts = np.cumsum([0] + [len(p) for p in pieces])
            box = np.array([(*p.min(0), *p.max(0)) for p in pieces]).reshape(-1, 4)
            self.levels[level] = (points, starts, np.concatenate(along) if along else np.empty(0), box,
                                  np.array(owner, dtype=int))

    def draw(self, surface, view, s, colour, width, pattern=None):
        """The pieces in view: solid, or dashed by `pattern`."""
        points, starts, along, box, owner = self.levels[min(32, 2 ** math.ceil(math.log2(view.zoom)))]
        left, top, right, bottom = view.rect()
        pad = 2 / view.zoom
        shown = np.flatnonzero((box[:, 2] >= left - pad) & (box[:, 0] <= right + pad)
                               & (box[:, 3] >= top - pad) & (box[:, 1] <= bottom + pad))
        if not len(shown):
            return
        first, count = starts[shown], np.diff(starts)[shown]
        index = np.repeat(first - np.r_[0, np.cumsum(count)[:-1]], count) + np.arange(count.sum())
        xy = view.points(points[index]) * s
        ends = np.cumsum(count)
        if pattern is not None:                          # every segment but those between pieces
            valid = np.ones(len(xy) - 1, bool)
            valid[ends[:-1] - 1] = False
            stamp_dashes(surface, xy, along[index] * view.zoom * s, np.flatnonzero(valid), colour, width, pattern)
            return
        # one stroke for each run of pieces that follow on in the same line
        joined = (np.diff(shown) == 1) & (np.diff(owner[shown]) == 0)
        breaks = np.flatnonzero(~joined)
        for i, j in zip(np.r_[0, ends[breaks]].tolist(), np.r_[ends[breaks], ends[-1]].tolist()):
            pygame.draw.lines(surface, colour, False, xy[i:j].tolist(), width)


def square_name(i, j):
    """The Maidenhead square (e.g. OM20) whose south-west corner is 2 i deg E, j deg N."""
    lon, lat = i + 90, j + 90                            # squares from 180 W, 90 S
    return f"{chr(65 + lon // 10)}{chr(65 + lat // 10)}{lon % 10}{lat % 10}"


class Scene:
    """Vector layers kept in base px and drawn through a View at fixed physical sizes."""

    def __init__(self, base, scale):
        pygame.freetype.init()
        self.base, self.scale = base, scale
        self.glyphs = OrderedDict()
        self.data, self.future, self.mtime, self.checked = None, None, None, 0
        self.part = None                      # the static part of the scene, kept between loads
        self.admin, self.admin_future, self.admin_mtime = {}, None, None
        self.layer = None
        self.budget = math.inf                # glyphs this frame may still render
        with open(os.path.join(a.MODEL, "rangemap_osm.json")) as f:
            osm = json.load(f)
        # rivers: OSM ways joined end to end into whole rivers
        joined = tiles.rings([ln["p"] for ln in osm["lines"]])
        self.rivers = Lines([np.array([base.xy(*p) for p in ln]) for ln in joined if len(ln) > 1])
        self.marks = {}
        # the Maidenhead grid over the whole map: fields (20 x 10 deg), squares (2 x 1) and
        # subsquares (5' x 2.5') as lines, and the corners their names go by
        lat, lon = grid_latlon(base, *np.meshgrid(np.linspace(0, W, 40), np.linspace(0, H, 20)))
        south, north, west, east = lat.min() - .1, lat.max() + .1, lon.min() - .1, lon.max() + .1
        self.grid = []
        for dlon, dlat, style in ((1 / 12, 1 / 24, (GRID_SUB, 1)), (2, 1, (GRID_SQUARE, 1)), (20, 10, (GRID_FIELD, 2))):
            lats = np.linspace(south, north, round((north - south) / .05))
            lons = np.linspace(west, east, round((east - west) / .05))
            lines = [base_xy(base, lats, np.full(len(lats), i * dlon))
                     for i in range(math.ceil(west / dlon), math.floor(east / dlon) + 1)]
            lines += [base_xy(base, np.full(len(lons), j * dlat), lons)
                      for j in range(math.ceil(south / dlat), math.floor(north / dlat) + 1)]
            self.grid.append((Lines(lines, cell=2), style))
        ii, jj = np.meshgrid(np.arange(math.floor(west / 2), math.floor(east / 2) + 1),
                             np.arange(math.floor(south), math.floor(north) + 1))
        self.squares = ii.ravel(), jj.ravel(), base_xy(base, jj.ravel() + 1.0, 2.0 * ii.ravel())
        ii, jj = np.meshgrid(np.arange(math.floor(west * 12), math.floor(east * 12) + 1),
                             np.arange(math.floor(south * 24), math.floor(north * 24) + 1))
        self.subsquares = ii.ravel(), jj.ravel(), base_xy(base, (jj.ravel() + 1) / 24, ii.ravel() / 12)

    def poll(self, submit, now):
        """Pick up a scene loaded in the worker, and ask for a reload when activity.json
        changes (checked once a second); True if the scene changed."""
        changed = False
        if self.future and self.future.done():
            try:
                self.take(self.future.result())
                changed = True
            except Exception as e:                       # file caught mid-write, worker died...
                log.warning("map scene: %s", e)
                self.mtime = None
            self.future = None
        if self.future is None and now - self.checked > 1:
            self.checked = now
            try:
                mtime = os.path.getmtime(os.path.join(a.MODEL, "activity.json"))
            except OSError:
                mtime = None
            if mtime and mtime != self.mtime:
                self.mtime = mtime
                self.future = submit(load_scene, self.base, self.scale, self.part and self.part["key"])
        return self.poll_admin(submit) or changed

    def poll_admin(self, submit):
        """The same for the boundaries, when admin.json changes; True if they did."""
        changed = False
        if self.admin_future and self.admin_future.done():
            try:
                self.admin = self.admin_future.result()
                changed = True
            except Exception as e:
                log.warning("map boundaries: %s", e)
                self.admin_mtime = None
            self.admin_future = None
        if self.admin_future is None and self.future is None:
            try:
                mtime = os.path.getmtime(os.path.join(a.MODEL, "admin.json"))
            except OSError:
                mtime = None
            if mtime and mtime != self.admin_mtime:
                self.admin_mtime = mtime
                self.admin_future = submit(load_admin, self.base)
        return changed

    def take(self, d):
        """A loaded scene: its dynamic layers, with the static part it came with or the one kept."""
        if d["part"] is not None:
            self.part = d["part"]
        if self.part is None or self.part["key"] != d["key"]:
            self.mtime = None                 # the worker's part is newer than ours: load again
            return
        self.data = combined(d, self.part)

    # -------------------------------------------------------------------- text ----

    def glyph(self, text, colour, size, force=False):
        """Text with a 1 px black outline, rendered at the physical size, cached; None if not
        cached and this frame's budget is spent (unless `force`)."""
        key = text, colour, size
        if key in self.glyphs:
            self.glyphs.move_to_end(key)
            return self.glyphs[key]
        if self.budget <= 0 and not force:
            return None
        self.budget -= 1
        f = font(kind_of(text), text_px(size, self.scale))
        pic, _ = f.render(text, colour)
        shadow, _ = f.render(text, (0, 0, 0))
        out = pygame.Surface((pic.get_width() + 2, pic.get_height() + 2), pygame.SRCALPHA)
        for at in ((0, 0), (1, 0), (2, 0), (0, 1), (2, 1), (0, 2), (1, 2), (2, 2)):
            out.blit(shadow, at)
        out.blit(pic, (1, 1))
        out = out.convert_alpha() if pygame.display.get_surface() else out
        if colour in FAINT:
            out.set_alpha(FAINT[colour])
        self.glyphs[key] = out
        if len(self.glyphs) > 4096:
            self.glyphs.popitem(last=False)
        return self.glyphs[key]

    def warm(self, count):
        """Render up to `count` label glyphs not yet cached, most important first (when idle)."""
        if self.data is None:
            return
        saved, self.budget = self.budget, count
        for (text, _, size), kind in islice(zip(self.data["labels"], self.data["kinds"]), 3000):
            if self.budget <= 0:
                break
            self.glyph(text, kind[1], size)
        self.budget = saved

    def mark(self, shape, colour, grow=1.0):
        """A marker sprite at the physical size (grown `grow` times as the map zooms in), and
        the offset of its centre."""
        key = shape, colour, round(grow, 2)
        if key not in self.marks:
            s = self.scale
            r = {"diamond": 5, "dot": 4, "ring": 5, "square": 4, "campus": 3, "office": 2.4, "consulate": 4,
                 "city": 2, "town": 1}[shape] * s * grow
            n = math.ceil(2 * r) + 3
            pic = pygame.Surface((n, n), pygame.SRCALPHA)
            c = n / 2
            if shape == "diamond":
                pygame.draw.polygon(pic, colour, [(c, c - r), (c + r, c), (c, c + r), (c - r, c)])
            elif shape == "dot":
                pygame.draw.circle(pic, (0, 0, 0), (c, c), max(2, round(r)) + 1)
                pygame.draw.circle(pic, colour, (c, c), max(2, round(r)))
            elif shape == "ring":
                pygame.draw.circle(pic, (0, 0, 0), (c, c), round(r) + 1, max(1, round(2 * s)) + 2)
                pygame.draw.circle(pic, colour, (c, c), round(r), max(1, round(2 * s)))
            elif shape in ("campus", "office", "consulate"):   # a consulate's a little larger
                pygame.draw.circle(pic, colour + (FAINT.get(colour, 255),), (c, c), max(2, round(r)))
            else:                                        # square, city, town
                side = max(2, round(2 * r)) if shape != "town" else 2
                pic.fill(colour, (round(c - side / 2), round(c - side / 2), side, side))
            self.marks[key] = (pic.convert_alpha() if pygame.display.get_surface() else pic), c
        return self.marks[key]

    def stamp(self, surface, shape, colour, xy, grow=1.0):
        """The marker at each view px point of `xy` (an array)."""
        pic, c = self.mark(shape, colour, grow)
        s = self.scale
        surface.blits([(pic, (x, y)) for x, y in np.round(xy * s - c).astype(int).tolist()], False)

    def dots(self, surface, colour, xy, grow=1.0):
        """A small square at each view px point of `xy`, written as pixels all at once (a FAINT
        layer's a little smaller, blended)."""
        alpha = FAINT.get(colour)
        r = max(1, round(grow * (0.8 if alpha else 1)))
        p = np.round(xy * self.scale).astype(int)
        w, h = surface.get_size()
        p = p[(p[:, 0] >= r) & (p[:, 0] < w - r) & (p[:, 1] >= r) & (p[:, 1] < h - r)]
        if alpha:
            pixels, value = pygame.surfarray.pixels3d(surface), np.array(colour) * alpha
            for dx in range(-r, r + 1):
                for dy in range(-r, r + 1):
                    x, y = p[:, 0] + dx, p[:, 1] + dy
                    pixels[x, y] = (pixels[x, y].astype(np.int32) * (255 - alpha) + value) // 255
            del pixels
            return
        pixels, value = pygame.surfarray.pixels2d(surface), surface.map_rgb(colour) & 0xFFFFFFFF
        for dx in range(-r, r + 1):
            for dy in range(-r, r + 1):
                pixels[p[:, 0] + dx, p[:, 1] + dy] = value
        del pixels                                           # unlock the surface

    def text(self, surface, text, at, colour, size=12):
        """HUD text at view px `at` (top left)."""
        surface.blit(self.glyph(text, colour, size, True),
                     (round(at[0] * self.scale) - 1, round(at[1] * self.scale) - 1))

    def width(self, text, size=12):
        return self.glyph(text, a.WHITE, size, True).get_width() / self.scale

    # -------------------------------------------------------------------- draw ----

    def draw(self, surface, view, metres, resting=True):
        """Draw over the ground on `surface` (the map box at its physical size): opaque lines,
        then the translucent ones (rings, grid, boundaries, campuses, paths) blended at once,
        then the grid's names, markers and labels."""
        s = self.scale
        self.budget = math.inf if resting else NEW_GLYPHS
        if self.layer is None or self.layer.get_size() != surface.get_size():
            self.layer = pygame.Surface(surface.get_size(), pygame.SRCALPHA)
        self.layer.fill((0, 0, 0, 0))
        self.draw_ground_lines(view, surface, s)
        grid_names = self.draw_grid(view, s)
        self.draw_boundaries(view, s)
        self.draw_overlays(view, s)
        surface.blit(self.layer, (0, 0), special_flags=pygame.BLEND_ALPHA_SDL2)
        surface.blits([(self.glyph(text, colour, size, True), (round(x * s) - 1, round(y * s) - 1))
                       for text, colour, size, (x, y) in grid_names], False)
        legend = self.draw_markers(view, surface, s)
        self.draw_labels(view, surface, s)
        self.draw_hud(view, surface, s, metres, legend)

    def draw_ground_lines(self, view, surface, s):
        """Rivers and runways (to scale, never thinner than 2 px, on a dark edge so they show
        on any ground); range rings about us, spaced ~90-220 view px at any zoom (100 km on the
        whole map)."""
        self.rivers.draw(surface, view, s, terrain.RIVER, max(1, round(2 * s)))
        d = self.data
        if d is not None and view.zoom >= RING_ZOOM / 2:      # further out, rings mark airports
            left, top, right, bottom = view.rect()
            box = d["runway_box"]
            for i in np.flatnonzero((box[:, 2] >= left) & (box[:, 0] <= right) & (box[:, 3] >= top) & (box[:, 1] <= bottom)):
                r = d["runways"][i]
                pts = (view.points(r["line"]) * s).tolist()
                if r["area"]:
                    pygame.draw.polygon(surface, RUNWAY, pts)
                else:
                    width = max(2, round(r["width"] / 1000 / view.km_per_px * s))
                    pygame.draw.lines(surface, (40, 40, 40), False, pts, width + 2)
                    pygame.draw.lines(surface, RUNWAY, False, pts, width)
        de = view.project((W / 2, H / 2))
        km = math.hypot(de[0] - W / 2, de[1] - H / 2) * view.km_per_px
        span = math.hypot(W, H) / 2 * view.km_per_px
        want = 220 * view.km_per_px
        power = 10 ** math.floor(math.log10(want))
        step = max(v * power for v in (1, 2, 5) if v * power <= want)
        for r in np.arange(max(step, math.floor((km - span) / step) * step), km + span, step):
            pygame.draw.circle(self.layer, RING, (de[0] * s, de[1] * s), round(r / view.km_per_px * s), 1)

    def draw_grid(self, view, s):
        """The Maidenhead grid on the layer: fields, squares and, zoomed in, subsquares.
        Returns the names to put over it: each square's at its top left corner, kept in view
        while the square reaches past the view's top or left (so the square under the top left
        corner is always named); subsquares' two letters at theirs."""
        for lines, (colour, width) in self.grid[0 if view.zoom >= SUB_ZOOM else 1:]:
            lines.draw(self.layer, view, s, colour, width)
        names, taken = [], []
        ii, jj, corners = self.squares
        at = np.maximum(view.points(corners), 3)
        lat, lon = view.grid_latlon(at[:, 0] + 1, at[:, 1] + 1)        # still in that square?
        mine = (jj <= lat) & (lat < jj + 1) & (2 * ii <= lon) & (lon < 2 * ii + 2)             & (at[:, 0] <= W - 40) & (at[:, 1] <= H - 18)
        for i, j, (x, y) in zip(ii[mine].tolist(), jj[mine].tolist(), at[mine].tolist()):
            names.append((square_name(i, j), GRID_TEXT, 13, (x + 4, y + 2)))
            taken.append((x, y, x + 46, y + 18))
        if view.zoom >= SUB_ZOOM:
            ii, jj, corners = self.subsquares
            at = view.points(corners)
            shown = np.flatnonzero((at[:, 0] >= 0) & (at[:, 0] < W - 20) & (at[:, 1] >= 0) & (at[:, 1] < H - 14))
            for i, j, (x, y) in zip(ii[shown].tolist(), jj[shown].tolist(), at[shown].tolist()):
                if not any(b[0] - 18 < x < b[2] and b[1] - 14 < y < b[3] for b in taken):
                    names.append((chr(97 + (i + 2160) % 24) + chr(97 + (j + 2160) % 24), GRID_SUB_TEXT, 11, (x + 3, y + 2)))
        return names

    def draw_boundaries(self, view, s):
        """Province borders on the whole map; from CITY_ZOOM on, city (prefecture) and county
        outlines instead, a province border then showing as the city border it also is."""
        styles = [(4, PROVINCE)] if view.zoom < CITY_ZOOM else [(6, COUNTY), (5, PREFECTURE), (4, PREFECTURE)]
        for level, (colour, width, pattern) in styles:
            if level in self.admin:
                self.admin[level].draw(self.layer, view, s, colour, width, pattern)

    @staticmethod
    def big(box, view, s):
        """Which areas span OUTLINE_PX or more on screen: those get their outline."""
        return np.maximum(box[:, 2] - box[:, 0], box[:, 3] - box[:, 1]) * view.zoom * s >= OUTLINE_PX

    @staticmethod
    def areas(d):
        """(areas, their boxes, colour, from zoom) of each outlined layer, drawn in this order."""
        return ((d["military"], d["military_box"], MILITARY, MILITARY_ZOOM),
                (d["government"], d["government_box"], GOVERNMENT, MILITARY_ZOOM),
                (d["regulators"], d["regulator_box"], REGULATOR, MILITARY_ZOOM),
                (d["campuses"], d["campus_box"], CAMPUS, CAMPUS_ZOOM))

    def draw_overlays(self, view, s):
        """Military areas, government offices and campuses (zoomed in), and contact paths, on the
        layer."""
        d = self.data
        if d is None:
            return
        left, top, right, bottom = view.rect()
        for found, box, colour, zoom in self.areas(d):
            if view.zoom < zoom:
                continue
            here = (box[:, 2] >= left) & (box[:, 0] <= right) & (box[:, 3] >= top) & (box[:, 1] <= bottom)
            for i in np.flatnonzero(here & self.big(box, view, s)):
                for ring in found[i]["rings"]:
                    pts = (view.points(ring) * s).tolist()
                    fade = FAINT.get(colour, 255) / 255
                    pygame.draw.polygon(self.layer, colour + (round(40 * fade),), pts)
                    pygame.draw.lines(self.layer, colour + (round(210 * fade),), True, pts, max(1, round(1.5 * s)))
        ends = view.points(d["line_ends"])
        for (p, q), (_, colour, alpha) in zip(ends, d["lines"]):    # faint: to past the map
            pygame.draw.line(self.layer, colour + (alpha,), (p * s).tolist(), (q * s).tolist())
        links = view.points(d["link_ends"])
        for (p, q), both in zip(links, inside(links).all(axis=1)):
            pygame.draw.line(self.layer, a.APRS_RF + (175 if both else 48,), (p * s).tolist(), (q * s).tolist())
        de = view.project((W / 2, H / 2))
        for p in view.points(d["qso_xy"]):
            pygame.draw.line(self.layer, a.WHITE + (230,), (de[0] * s, de[1] * s), (p * s).tolist(),
                             max(1, round(2 * s)))

    def draw_markers(self, view, surface, s):
        """Airports (zoomed out), campus points (zoomed in), stations, APRS, our QSOs. Returns
        the legend's (colour, text) parts."""
        d = self.data
        if d is None:
            return []
        left, top, right, bottom = view.rect()
        grow = min(2.0, 1 + 0.22 * max(0.0, math.log2(view.zoom / 6)))     # dots stay findable
        if view.zoom < RING_ZOOM:
            airports = view.points(d["airport_xy"])
            self.stamp(surface, "ring", AIRPORT, airports[inside(airports)])
        for found, box, colour, zoom in self.areas(d):
            if view.zoom < zoom:
                continue
            here = (box[:, 2] >= left) & (box[:, 0] <= right) & (box[:, 3] >= top) & (box[:, 1] <= bottom)
            big = self.big(box, view, s)
            dots = [found[i]["point"] for i in np.flatnonzero(here)
                    if not (found[i]["rings"] and big[i])
                    and (colour != GOVERNMENT or view.zoom >= max(MILITARY_ZOOM, found[i]["zoom"] - 2))]
            xy = view.points(np.array(dots, dtype=float).reshape(-1, 2))
            if len(xy) > DOTS:
                self.dots(surface, colour, xy, grow)
            else:
                self.stamp(surface, {REGULATOR: "square", GOVERNMENT: "office"}.get(colour, "campus"), colour, xy, grow)
        if view.zoom >= MILITARY_ZOOM:
            emb = view.points(d["embassy_xy"])
            self.stamp(surface, "consulate", EMBASSY, emb[inside(emb)], grow)
        st = view.points(d["station_xy"])
        shown_st = np.flatnonzero(inside(st))
        for i in shown_st[::-1]:                                     # senders on top
            self.stamp(surface, "diamond", d["stations"][i][1], st[i:i + 1])
        heard = view.points(d["aprs_xy"])
        shown = np.flatnonzero(inside(heard))
        rf = d["aprs_rf"][shown]
        self.stamp(surface, "dot", a.APRS_NET, heard[shown[~rf]])
        self.stamp(surface, "dot", a.APRS_RF, heard[shown[rf]])      # RF heard on top
        qsos = view.points(d["qso_xy"])
        self.stamp(surface, "square", a.WHITE, qsos[inside(qsos)])
        order = {name: i for i, (_, name, _) in enumerate(a.BANDS)}
        parts = [(a.APRS_RF, f"APRS {len(shown)}"), (a.WHITE, f"DIGI {len(shown_st)}"),
                 (a.DIM, f"PATH {len(d['lines'])}/{d['total']}")]
        return parts + [(colour, name if name.endswith("cm") else name + "m")
                        for name, colour in sorted(d["bands"].items(), key=lambda b: order.get(b[0], 99))]

    def draw_labels(self, view, surface, s):
        """Places as squares (named at this zoom) or dots on the whole map, the farthest point,
        us; then every label placed for this zoom step."""
        d = self.data
        li = max(i for i, z in enumerate(LABEL_ZOOMS) if z <= view.zoom * 1.0001)
        if d is not None:
            if view.zoom < CITY_ZOOM:                    # zoomed in, towns are their outlines
                xy = view.points(d["places"])
                here = inside(xy)
                named = d["place_labelled"][:, li]
                self.stamp(surface, "town", TOWN, xy[here & ~named])
                self.stamp(surface, "city", CITY, xy[here & named])
            x, y = view.project(d["far"])
            if view.inside(x, y):
                pygame.draw.polygon(surface, terrain.SEEN, [(x * s, (y - 7) * s), ((x + 6) * s, (y + 5) * s),
                                                            ((x - 6) * s, (y + 5) * s)])
        x, y = view.project((W / 2, H / 2))
        if view.inside(x, y):
            pygame.draw.circle(surface, terrain.DE_GREEN, (x * s, y * s), round(9 * s), max(1, round(3 * s)))
            pygame.draw.circle(surface, (0, 0, 0), (x * s, y * s), max(1, round(3 * s)))
        if d is None:
            return
        k, (tw, th) = d["spots"][:, li], d["sizes"].T
        at = view.points(d["label_xy"])
        dx, dy = offsets(k, tw, th)
        x, y = at[:, 0] + dx, at[:, 1] + dy
        shown = (k >= 0) & (x + tw > 0) & (x < W) & (y + th > 0) & (y < H)
        if view.zoom < CAMPUS_ZOOM:
            shown &= ~d["label_campus"]
        jobs, leads = [], []
        for i, px, py in zip(np.flatnonzero(shown).tolist(), np.round(x[shown] * s - 1).astype(int).tolist(),
                             np.round(y[shown] * s - 1).astype(int).tolist()):
            text, _, size = d["labels"][i]
            kind = d["kinds"][i]
            pic = self.glyph(text, kind[1], size)
            if pic is None:
                continue
            if kind[0] == "embassy":                 # a consulate's flag at the label's left
                w, gap, _ = flag_box(s, size)
                jobs.append((flags.flag(kind[2], text_px(size, s)), (px - w - gap - 1, py + 1)))
            if k[i] >= 9:                            # one of a building's stack: tie it to the point
                ax, ay = at[i, 0] * s, at[i, 1] * s
                mid = py + th[i] * s / 2
                leads.append(((ax, ay), (px - 3, mid) if x[i] > at[i, 0] else (px + tw[i] * s + 3, mid)))
            jobs.append((pic, (px, py)))
        for a, b in leads:
            pygame.draw.aaline(surface, LEAD, a, b)
        surface.blits(jobs, False)

    def draw_hud(self, view, surface, s, metres, legend):
        """Scale bar and terrain detail bottom left, legend bottom right, sun top right."""
        km = 130 * view.km_per_px
        decade = 10 ** math.floor(math.log10(km))
        km = max(v * decade for v in (1, 2, 5) if v * decade <= km)
        bar = km / view.km_per_px
        detail = f"DEM {metres:.0f} m  {view.zoom:.1f}x"
        tw = self.width(detail)
        width = max(bar, self.width(f"{km:g} km")) + tw + 30
        pygame.draw.rect(surface, (0, 0, 0), (2 * s, (H - 26) * s, width * s, 24 * s))
        pygame.draw.line(surface, a.WHITE, (10 * s, (H - 8) * s), ((10 + bar) * s, (H - 8) * s), max(1, round(2 * s)))
        self.text(surface, f"{km:g} km", (10, H - 25), a.WHITE)
        self.text(surface, detail, (width - tw - 6, H - 21), a.DIM)
        if legend:
            widths = [self.width(text) + 18 for _, text in legend]
            x = W - 6 - sum(widths)
            pygame.draw.rect(surface, (0, 0, 0), ((x - 4) * s, (H - 22) * s, (W - x + 2) * s, 20 * s))
            pygame.draw.rect(surface, a.WHITE, ((x - 4) * s, (H - 22) * s, (W - x + 2) * s, 20 * s), 1)
            for (colour, text), w in zip(legend, widths):
                pygame.draw.rect(surface, colour, (x * s, (H - 15) * s, 8 * s, 8 * s))
                self.text(surface, text, (x + 12, H - 20), a.WHITE)
                x += w
        sun = f"SUN {float(solar_elevation(view.lat0, view.lon0, time.time())):+.0f} deg"
        tw = self.width(sun)
        pygame.draw.rect(surface, (0, 0, 0), ((W - tw - 16) * s, 4 * s, (tw + 12) * s, 20 * s))
        pygame.draw.rect(surface, GREY, ((W - tw - 16) * s, 4 * s, (tw + 12) * s, 20 * s), 1)
        self.text(surface, sun, (W - tw - 10, 6), a.WHITE)


# ------------------------------------------------------------------------ canvas ----

class Raster:
    """A ground picture: picture px (i, j) covers base px origin + (i, j) / scale. Its surface
    has a 1 px border repeating the edge, so a scaled sample can always reach half a pixel
    past it. `wide` if it reaches past the view it was made for."""

    def __init__(self, origin, scale, size, rgb, metres, minute, wide=False):
        self.origin, self.scale, self.size, self.metres, self.wide = origin, scale, size, metres, wide
        self.shaded(rgb, minute)

    def shaded(self, rgb, minute):
        surface = pygame.image.frombytes(rgb, (self.size[0] + 2, self.size[1] + 2), "RGB")
        self.surface = surface.convert() if pygame.display.get_surface() else surface
        self.minute = minute

    def rect(self):
        return (self.origin[0], self.origin[1], self.origin[0] + self.size[0] / self.scale,
                self.origin[1] + self.size[1] / self.scale)

    def covers(self, view):
        left, top, right, bottom = view.rect()
        x0, y0, x1, y1 = self.rect()
        return x0 <= left + 1e-6 and y0 <= top + 1e-6 and right <= x1 + 1e-6 and bottom <= y1 + 1e-6


def span(lo, hi, k, origin):
    """Along one axis, to cover picture coordinates lo..hi: the surface pixels [first, first +
    count) that smoothscale should scale to `size` px and place at `at`, so that picture
    coordinate u lands at (u - origin) * k (pixel i spans i..i+1, at surface index i + 1).
    smoothscale shrinks corner to corner, by area, but expands putting source pixel j's value
    at destination pixel j * (count - 1) / size."""
    if k > 1:
        first = math.floor(lo - 0.5)                     # padding covers -1 and the far edge
        count = math.ceil(hi - 0.5) - first + 1
        size = round(k * (count - 1))
        if size > count:
            return first + 1, count, size, round((first + 0.5 - origin) * k - 0.5)
    first = math.floor(lo)
    count = max(1, math.ceil(hi) - first)
    return first + 1, count, max(1, round(count * k)), round((first - origin) * k)


def raster_geometry(view, scale, margin):
    """(origin, scale, size) of a picture of `view` at `scale` physical px a view px,
    reaching `margin` view widths past each side (within the map), so placed that the view's
    own pixels are whole picture pixels."""
    s = view.zoom * scale
    left, top, right, bottom = view.rect()
    w, h = round(W * scale), round(H * scale)
    ml = min(round(margin * w), math.floor(left * s + 1e-6))
    mt = min(round(margin * h), math.floor(top * s + 1e-6))
    mr = min(round(margin * w), math.floor((W - right) * s + 1e-6))
    mb = min(round(margin * h), math.floor((H - bottom) * s + 1e-6))
    return (left - ml / s, top - mt / s), s, (ml + w + mr, mt + h + mb)


class Canvas:
    """HamClock's map box (`size` physical px): the ground under the vector scene, through
    the camera."""

    def __init__(self, size):
        base = a.Map()
        self.scale = size[0] / W
        self.camera, self.scene = Camera(base), Scene(base, self.scale)
        self.view = self.camera.view()
        self.workers = {"picture": None, "scene": None}
        minute = int(time.time() // 60)
        whole = (0.0, 0.0), self.scale, (round(W * self.scale), round(H * self.scale))   # stand-in
        self.whole = Raster(*whole, *shade(base, *whole, minute * 60), minute)
        self.detail = self.previous = None
        self.job = self.fading = None
        self.moved_at = self.loaded_at = -math.inf
        self.retry_at = 0
        self.dirty, self.resting = True, True
        self.scene.poll(self.submit_scene, time.monotonic())

    def submit(self, which, fn, *args):
        """Run fn(*args) in that worker process ("picture" or "scene", side by side); started
        on first use, restarted if it died."""
        try:
            if self.workers[which] is None:
                raise BrokenProcessPool
            return self.workers[which].submit(fn, *args)
        except BrokenProcessPool:
            self.workers[which] = ProcessPoolExecutor(1, mp_context=multiprocessing.get_context("spawn"))
            return self.workers[which].submit(fn, *args)

    def submit_scene(self, fn, *args):
        return self.submit("scene", fn, *args)

    def scroll(self, notches, at):
        if self.camera.scroll(notches, at):
            self.moved_at, self.retry_at = time.monotonic(), 0
            return True
        return False

    def pinch(self, ratio, at):
        if self.camera.pinch(ratio, at):
            self.moved_at, self.dirty = time.monotonic(), True
            return True
        return False

    def pan(self, dx, dy):
        if self.camera.pan(dx, dy):
            self.moved_at, self.dirty = time.monotonic(), True
            return True
        return False

    def tick(self, dt):
        """Advance the zoom and the background work; True if the map box needs drawing."""
        moved = self.camera.tick(dt)
        self.view = self.camera.view()
        now = time.monotonic()
        resting = self.camera.zoom == self.camera.goal and now - self.moved_at > REST_S
        changed = self.scene.poll(self.submit_scene, now) or moved or self.dirty \
            or now - self.loaded_at < FADE_S or resting != self.resting
        self.dirty, self.resting = False, resting
        if self.job and self.job[-1].done():
            changed = self.finish(now) or changed
        if self.job is None:
            self.job = self.next_job(now)
        if resting:
            self.scene.warm(40)
        return changed

    def next_job(self, now):
        """(what, geometry, minute, future) for the picture worker, or None: the goal view's
        own picture first (at exactly its scale once it rests, within 25% while it moves),
        then one reaching past it, then night brought up to the minute."""
        minute = int(time.time() // 60)
        goal = self.camera.at(self.camera.goal)
        d = self.detail
        near = d is not None and d.covers(goal) and \
            abs(math.log(d.scale / (goal.zoom * self.scale))) < (0.005 if self.resting else 0.22)
        if not near and now >= self.retry_at:
            geometry = raster_geometry(goal, self.scale, 0)
            return "view", geometry, minute, self.submit("picture", shade, goal.base, *geometry, minute * 60)
        if near and self.resting and not d.wide:
            geometry = raster_geometry(goal, self.scale, MARGIN)
            if geometry[2] != d.size:
                return "wide", geometry, minute, self.submit("picture", shade, goal.base, *geometry, minute * 60)
            d.wide = True                                # at the map's edge: nothing past it
        for raster in (self.whole, self.detail):
            if raster and raster.minute != minute:
                geometry = raster.origin, raster.scale, raster.size
                return raster, geometry, minute, self.submit("picture", shade, goal.base, *geometry, minute * 60)
        return None

    def finish(self, now):
        what, geometry, minute, future = self.job
        self.job = None
        try:
            rgb, metres = future.result()
        except Exception as e:                           # tiles unreadable, worker died...
            log.warning("map picture: %s", e)
            if isinstance(e, BrokenProcessPool):
                self.workers["picture"] = None
            if isinstance(what, Raster):
                what.minute = minute                     # keep the old night until the next
            else:
                self.retry_at = now + RETRY_S
            return False
        if isinstance(what, Raster):
            what.shaded(rgb, minute)
            return True
        if what == "view" and self.resting:              # fade in a sharper picture at rest;
            self.previous, self.loaded_at = self.detail or self.whole, now   # moving, just swap
        self.detail = Raster(*geometry, rgb, metres, minute, wide=what == "wide")
        return True

    def sample(self, raster, dest, part=False):
        """Draw `raster` under the current view, scaled onto `dest`: only if it covers the whole
        view, or with `part` whatever of it is in view. True if it drew."""
        if raster is None:
            return False
        left, top, right, bottom = self.view.rect()
        x0, y0 = (left - raster.origin[0]) * raster.scale, (top - raster.origin[1]) * raster.scale
        x1, y1 = (right - raster.origin[0]) * raster.scale, (bottom - raster.origin[1]) * raster.scale
        w, h = raster.size
        lo, hi = (max(0, x0), max(0, y0)), (min(w, x1), min(h, y1))
        whole = lo[0] - x0 < 1e-6 and lo[1] - y0 < 1e-6 and x1 - hi[0] < 1e-6 and y1 - hi[1] < 1e-6
        if not (whole or part) or hi[0] <= lo[0] or hi[1] <= lo[1]:
            return False
        k = dest.get_width() / (x1 - x0)
        if whole and abs(k - 1) < 1e-6 and abs(x0 - round(x0)) < 1e-3 and abs(y0 - round(y0)) < 1e-3:
            dest.blit(raster.surface, (0, 0), (round(x0) + 1, round(y0) + 1, *dest.get_size()))
            return True                                  # at rest: the picture pixel for pixel
        (fx, nx, sx, ax), (fy, ny, sy, ay) = (span(lo[i], hi[i], k, (x0, y0)[i]) for i in (0, 1))
        clip = dest.get_clip()
        if not whole:                                    # keep the padding off what lies beyond
            dest.set_clip(pygame.Rect(math.floor((lo[0] - x0) * k), math.floor((lo[1] - y0) * k),
                                      math.ceil((hi[0] - lo[0]) * k), math.ceil((hi[1] - lo[1]) * k)).clip(clip))
        dest.blit(pygame.transform.smoothscale(raster.surface.subsurface((fx, fy, nx, ny)), (sx, sy)), (ax, ay))
        dest.set_clip(clip)
        return True

    def ground(self, raster, dest):
        """`raster` over the whole view, else the whole map with `raster` where it reaches; the
        metres a terrain pixel of what covers the view."""
        if self.sample(raster, dest):
            return raster.metres
        self.sample(self.whole, dest)                    # views never leave the whole map
        self.sample(raster, dest, part=True)
        return self.whole.metres

    def draw(self, frame, box):
        """Draw the map into `box` (frame px) of `frame`."""
        area = frame.subsurface(box)
        metres = self.ground(self.detail, area)
        fade = (time.monotonic() - self.loaded_at) / FADE_S
        if fade < 1 and self.previous:
            if self.fading is None or self.fading.get_size() != area.get_size():
                self.fading = pygame.Surface(area.get_size()).convert()
            self.ground(self.previous, self.fading)
            self.fading.set_alpha(round((1 - fade) * 255))
            area.blit(self.fading, (0, 0))
        self.scene.draw(area, self.view, metres, self.resting)
