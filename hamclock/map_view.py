"""Zoomable range map in the HamClock window: the wheel or a two-finger scroll zooms about the
pointer, a drag pans.

The ground (relief, line-of-sight coverage, night) is a raster: at first the whole-map picture
build_rangemap.py saved; once the view rests, a worker process renders one for exactly that
view from finer elevation tiles (Terrarium zoom 8..12, ~530 m down to ~33 m a pixel) and it
fades in. While the view moves the latest raster is only rescaled. All heavy work (tiles,
relief, night, reading activity.json) stays in that process, so a frame never waits for it.

Everything over the ground (rivers, grid, range rings, places, stations, contact paths, APRS)
is vector, projected through the view every frame: lines stay thin, labels keep their size.

Coordinates: base px are rangemap.png's, W x H, azimuthal equidistant about us (activity.Map).
A View maps them to view px of the same W x H box at its zoom; the screen shows that box scaled
to the map box's physical size.
"""
import json
import logging
import math
import multiprocessing
import os
import time
from collections import OrderedDict
from concurrent.futures import ProcessPoolExecutor
from concurrent.futures.process import BrokenProcessPool

import numpy as np
import pygame
from PIL import Image

import activity as a
import build_rangemap as terrain
from map_panel import night_layer, solar_elevation

W, H = a.W, a.H
MAX_ZOOM = 24
NOTCH = 0.18                  # log zoom per wheel notch (x1.2); trackpads send fractions of one
EASE_S = 0.065                # the zoom eases toward its goal with this time constant
REST_S = 0.09                 # the view must rest this long before its own raster is rendered
FADE_S = 0.18                 # cross-fade from the previous raster
RETRY_S = 30                  # after a failed render (tiles unreachable)
NEW_GLYPHS = 12               # labels first rendered in one frame while the view moves
TILE0_M = 156543.03           # Web Mercator metres per tile pixel at zoom 0, on the equator
FONT = "/usr/share/fonts/truetype/freefont/FreeSansBold.ttf"
CITY, TOWN, GREY = (230, 230, 230), (150, 150, 150), (139, 141, 139)
log = logging.getLogger(__name__)


class View:
    """`zoom` about `centre` (base px): base px <-> view px."""

    def __init__(self, base, zoom=1.0, centre=(W / 2, H / 2)):
        self.base, self.zoom, self.centre = base, zoom, tuple(centre)
        self.lat0, self.lon0 = base.lat0, base.lon0
        self.km_per_px = base.km_per_px / zoom

    def key(self):
        return round(self.zoom, 4), round(self.centre[0], 3), round(self.centre[1], 3)

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

    def xy(self, lat, lon):
        return self.project(self.base.xy(lat, lon))

    def latlon(self, x, y):
        return self.base.latlon(*self.unproject((x, y)))

    def polar(self, x, y):
        """Distance (km) and bearing (deg) from us of view px; arrays welcome."""
        bx, by = self.unproject((x, y))
        dx, dy = (bx - W / 2) * self.base.km_per_px, (by - H / 2) * self.base.km_per_px
        return np.hypot(dx, dy), np.degrees(np.arctan2(dx, -dy)) % 360

    def grid_latlon(self, x, y):
        """latlon() for arrays of view px."""
        km, bearing = self.polar(x, y)
        return terrain.destination(self.lat0, self.lon0, bearing, km)

    @staticmethod
    def inside(x, y, margin=4):
        return margin <= x < W - margin and margin <= y < H - margin


def inside(p, margin=4):
    """inside() for a (..., 2) array of view px."""
    return ((p[..., 0] >= margin) & (p[..., 0] < W - margin)
            & (p[..., 1] >= margin) & (p[..., 1] < H - margin))


def clamp(centre, zoom):
    """The nearest centre that keeps a view at `zoom` inside the map."""
    hw, hh = W / 2 / zoom, H / 2 / zoom
    return min(W - hw, max(hw, centre[0])), min(H - hh, max(hh, centre[1]))


class Camera:
    """The zoom eases toward a goal the wheel sets, holding the base point under the pointer
    where it is on screen; a drag moves the centre. The view never leaves the map."""

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

    def pan(self, dx, dy):
        """Dragged by (dx, dy) view px; True if the view moved."""
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


# ------------------------------------------------- worker process: rasters and data ----

def dem_metres(zoom, lat):
    return TILE0_M * math.cos(math.radians(lat)) / 2 ** zoom


def dem_zoom(view):
    """The coarsest Terrarium zoom whose pixels are no larger than the view's (8..12)."""
    ratio = dem_metres(0, view.lat0) / (view.km_per_px * 1000)
    return min(12, max(terrain.DEM_ZOOM, math.ceil(math.log2(ratio))))


def render_ground(view):
    """Relief and line-of-sight coverage for exactly this view: (picture, metres a DEM px)."""
    y, x = np.mgrid[0:H, 0:W]
    lat, lon = view.grid_latlon(x, y)
    zoom = dem_zoom(view)
    dem = terrain.DEM(float(lat.min() - .01), float(lon.min() - .01),
                      float(lat.max() + .01), float(lon.max() + .01), zoom=zoom)
    rgb = terrain.relief(dem(lat, lon), view.km_per_px)
    # coverage comes from the saved viewshed (0.25 km steps, 1440 bearings), as on the whole
    # map: tinted, outlined; the map popup works out single paths on finer terrain
    with np.load(os.path.join(a.MODEL, "range_visibility.npz")) as saved:
        az, d, visible = saved["az"], saved["distance"], saved["visible"]
    km, bearing = view.polar(x, y)
    ia = np.round(bearing / (360 / len(az))).astype(int) % len(az)
    idd = np.clip(np.round(km / (d[1] - d[0])).astype(int) - 1, 0, len(d) - 1)
    seen = visible[ia, idd] & (km <= d[-1])
    inner = np.roll(seen, 1, 0) & np.roll(seen, -1, 0) & np.roll(seen, 1, 1) & np.roll(seen, -1, 1)
    rgb[seen] = rgb[seen] * 0.45 + np.array(terrain.SEEN) * 0.55
    rgb[seen & ~inner] = terrain.SEEN
    return Image.fromarray(np.clip(rgb, 0, 255).astype(np.uint8)), dem_metres(zoom, view.lat0)


def shade(ground, view, when):
    """The ground with night laid on for time `when`, as RGB bytes."""
    night = night_layer(view, when, hud=False)
    return Image.alpha_composite(ground.convert("RGBA"), night).convert("RGB").tobytes()


def render(view, when):
    ground, metres = render_ground(view)
    return ground, metres, shade(ground, view, when)


def load_contacts(base):
    """activity.json -> what the scene draws, in base px."""
    with open(os.path.join(a.MODEL, "activity.json")) as f:
        data = json.load(f)
    stations, bands, paths = a.digital_paths(base, data["reports"])
    positions = data.get("aprs_positions") or data.get("aprs", {})
    return dict(
        stations=stations, bands=bands, paths=list(paths.values()),
        aprs=[(call, base.xy(*v[:2]), bool(v[2])) for call, v in data.get("aprs", {}).items()],
        links=[(base.xy(*positions[x][:2]), base.xy(*positions[y][:2]))
               for x, y in data.get("aprs_links", []) if x in positions and y in positions],
        qsos=[(call, base.xy(*ll)) for call, grid in data.get("qsos", []) if (ll := a.locator(grid))])


# ------------------------------------------------------------------ vector scene ----

class Contacts:
    """load_contacts() output with its points as arrays, for projecting them all at once."""

    def __init__(self, data):
        self.paths, self.bands = data["paths"], data["bands"]
        self.path_ends = np.array([p["ends"] for p in self.paths], dtype=float).reshape(-1, 2, 2)
        self.station_calls = list(data["stations"])
        self.station_style = [v[2:] for v in data["stations"].values()]     # (colour, sent)
        self.station_xy = np.array([v[:2] for v in data["stations"].values()], dtype=float).reshape(-1, 2)
        self.aprs = [(call, rf) for call, _, rf in data["aprs"]]
        self.aprs_xy = np.array([p for _, p, _ in data["aprs"]], dtype=float).reshape(-1, 2)
        self.link_ends = np.array(data["links"], dtype=float).reshape(-1, 2, 2)
        self.qso_calls = [call for call, _ in data["qsos"]]
        self.qso_xy = np.array([p for _, p in data["qsos"]], dtype=float).reshape(-1, 2)


def thin(points, zoom):
    """Drop polyline points sharing a half view px cell at `zoom` with the one before; keep the
    ends."""
    cells = np.floor(points * zoom * 2)
    keep = np.r_[True, np.any(np.diff(cells, axis=0), axis=1)]
    keep[-1] = True
    return points[keep]


class Scene:
    """Vector layers kept in base px and drawn through a View at fixed physical sizes."""

    def __init__(self, base):
        pygame.font.init()
        self.base = base
        self.fonts, self.glyphs = {}, OrderedDict()
        self.contacts, self.future, self.mtime, self.checked = None, None, None, 0
        self.selection_key = self.selection = None
        self.layout, self.layout_key, self.taken = {}, None, []
        self.budget = math.inf                # glyphs this frame may still render
        self.hud = (330, 420)                 # widths of the bottom left and right boxes
        self.layer = None
        with open(os.path.join(a.MODEL, "rangemap_osm.json")) as f:
            osm = json.load(f)
        with open(os.path.join(a.MODEL, "rangemap.json")) as f:
            info = json.load(f)
        # rivers: per zoom level, one array of every line's points thinned for it
        lines = [np.array([base.xy(*p) for p in ln["p"]]) for ln in osm["lines"] if len(ln["p"]) > 1]
        self.river_box = np.array([(*p.min(0), *p.max(0)) for p in lines]).reshape(-1, 4)
        self.rivers = {}
        for level in (1, 2, 4, 8, 16, 32):
            kept = [thin(p, level) for p in lines]
            self.rivers[level] = (np.vstack(kept) if kept else np.empty((0, 2)),
                                  np.cumsum([0] + [len(p) for p in kept]))
        # places, most important first: they win the label spots
        rank = {"yes": 0, "2": 0, "4": 1, "5": 2, "6": 3}
        places = sorted((p for p in osm["places"] if p.get("name")),
                        key=lambda p: (rank.get(p.get("capital", ""), 4), -p.get("pop", 0)))
        self.place_names = [p["name"] for p in places]
        self.place_xy = np.array([base.xy(p["lat"], p["lon"]) for p in places]).reshape(-1, 2)
        self.place_km = np.hypot(*(self.place_xy - (W / 2, H / 2)).T) * base.km_per_px
        self.name_km = info["reach_km"] * 1.1             # named on the whole map within reach
        self.far = (base.xy(info["far_lat"], info["far_lon"]),
                    f"{info['reach_km']:.0f} km {info['bearing']:.0f} deg {info['far_elev_m']:.0f} m")
        # Maidenhead squares, 2 x 1 degrees, as HamClock's grid
        corners = [base.latlon(x, y) for x in (0, W) for y in (0, H)]
        south, north = min(c[0] for c in corners), max(c[0] for c in corners)
        west, east = min(c[1] for c in corners), max(c[1] for c in corners)
        self.grid = [np.array([base.xy(lat, lon) for lat in np.linspace(south, north, 50)])
                     for lon in range(int(west // 2) * 2, int(east) + 3, 2)]
        self.grid += [np.array([base.xy(lat, lon) for lon in np.linspace(west, east, 50)])
                      for lat in range(math.floor(south), math.ceil(north) + 1)]

    def poll(self, submit, now):
        """Pick up contacts loaded in the worker, and ask for a reload when activity.json
        changes (checked once a second); True if the contacts changed."""
        changed = False
        if self.future and self.future.done():
            try:
                self.contacts = Contacts(self.future.result())
                changed = True
            except Exception as e:                       # file caught mid-write, worker died...
                log.warning("map contacts: %s", e)
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
                self.future = submit(load_contacts, self.base)
        return changed

    # -------------------------------------------------------------------- text ----

    def glyph(self, text, colour, size):
        """Text with a 1 px black outline, rendered at the physical size, cached."""
        key = text, colour, size, self.scale
        if key in self.glyphs:
            self.glyphs.move_to_end(key)
            return self.glyphs[key]
        self.budget -= 1
        px = max(9, round(size * self.scale))
        if px not in self.fonts:
            self.fonts[px] = pygame.font.Font(FONT if os.path.exists(FONT) else None, px)
        pic, shadow = (self.fonts[px].render(text, False, c) for c in (colour, (0, 0, 0)))
        out = pygame.Surface((pic.get_width() + 2, pic.get_height() + 2), pygame.SRCALPHA)
        for at in ((0, 1), (2, 1), (1, 0), (1, 2)):
            out.blit(shadow, at)
        out.blit(pic, (1, 1))
        self.glyphs[key] = out.convert_alpha() if pygame.display.get_surface() else out
        if len(self.glyphs) > 1024:
            self.glyphs.popitem(last=False)
        return self.glyphs[key]

    def text_size(self, text, size=13, colour=a.WHITE):
        pic = self.glyph(text, colour, size)
        return pic.get_width() / self.scale, pic.get_height() / self.scale

    def text(self, surface, text, at, colour, size=13):
        """Draw at view px `at`; the box it took, in view px."""
        pic = self.glyph(text, colour, size)
        surface.blit(pic, (round(at[0] * self.scale) - 1, round(at[1] * self.scale) - 1))
        return pygame.Rect(round(at[0]), round(at[1]), round(pic.get_width() / self.scale),
                           round(pic.get_height() / self.scale))

    def label(self, surface, x, y, text, colour, size=13):
        """Text beside the point at view px (x, y): right, left, above or below, the first spot
        clear of other text. While the view moves each label keeps the spot it had, riding with
        its point; spots are chosen afresh once it rests. A moving frame renders only a few new
        labels; the rest follow in the next frames."""
        key = text, colour, size
        if self.budget <= 0 and (text, colour, size, self.scale) not in self.glyphs:
            return
        if key in self.layout:
            spot = self.layout[key]
            if spot:
                self.taken.append(self.text(surface, text, (x + spot[0], y + spot[1]), colour, size))
            return
        tw, th = self.text_size(text, size, colour)
        for dx, dy in ((7, -7), (-tw - 7, -7), (-tw / 2, -22), (-tw / 2, 7)):
            box = pygame.Rect(round(x + dx) - 1, round(y + dy), round(tw) + 2, round(th) + 2)
            if box.left >= 0 and box.top >= 0 and box.right <= W and box.bottom <= H \
                    and box.collidelist(self.taken) == -1:
                self.layout[key] = (dx, dy)
                self.taken.append(self.text(surface, text, (x + dx, y + dy), colour, size))
                return
        self.layout[key] = None

    # -------------------------------------------------------------------- draw ----

    def draw(self, surface, view, metres, resting=True):
        """Draw over the ground on `surface` (the map box at its physical size). While the view
        moves (not `resting`) labels keep their spots and paths their selection."""
        self.scale = s = surface.get_width() / W
        self.budget = math.inf if resting else NEW_GLYPHS
        layout_key = view.key(), self.mtime
        if resting and layout_key != self.layout_key:
            self.layout.clear()
            self.layout_key = layout_key
        # labels keep clear of the boxes draw_hud() puts in the corners
        self.taken = [pygame.Rect(0, H - 28, self.hud[0], 28), pygame.Rect(W - self.hud[1], H - 24, self.hud[1], 24),
                      pygame.Rect(W - 130, 0, 130, 28)]
        if self.layer is None or self.layer.get_size() != surface.get_size():
            self.layer = pygame.Surface(surface.get_size(), pygame.SRCALPHA)
        self.layer.fill((0, 0, 0, 0))                    # translucent lines: rings, then paths
        self.draw_geography(surface, view, s)
        legend = self.draw_contacts(surface, view, s, resting)
        self.draw_places(surface, view, s)
        self.draw_hud(surface, view, s, metres, legend)

    def draw_geography(self, surface, view, s):
        level = min(32, 2 ** math.ceil(math.log2(view.zoom)))
        points, starts = self.rivers[level]
        (left, top), (right, bottom) = view.unproject((0, 0)), view.unproject((W, H))
        box = self.river_box
        size = np.maximum(box[:, 2] - box[:, 0], box[:, 3] - box[:, 1]) * view.zoom
        shown = np.flatnonzero((box[:, 2] >= left) & (box[:, 0] <= right) & (box[:, 3] >= top)
                               & (box[:, 1] <= bottom) & (size >= 2))
        xy = view.points(points) * s
        width = max(1, round(2 * s))
        for i in shown:
            pygame.draw.lines(surface, terrain.RIVER, False, xy[starts[i]:starts[i + 1]].tolist(), width)
        for line in self.grid:
            pygame.draw.lines(surface, terrain.GRID, False, (view.points(line) * s).tolist())
        # range rings about us, spaced ~90-220 view px at any zoom (100 km on the whole map)
        de = view.project((W / 2, H / 2))
        km = math.hypot(de[0] - W / 2, de[1] - H / 2) * view.km_per_px
        span = math.hypot(W, H) / 2 * view.km_per_px
        want = 220 * view.km_per_px
        power = 10 ** math.floor(math.log10(want))
        step = max(v * power for v in (1, 2, 5) if v * power <= want)
        for r in np.arange(max(step, math.floor((km - span) / step) * step), km + span, step):
            pygame.draw.circle(self.layer, terrain.RING + (120,), (de[0] * s, de[1] * s), round(r / view.km_per_px * s), 1)

    def select(self, c, ends, ends_in, st_in):
        """Which paths to show for the stations in view (activity.select_paths): their indices,
        each station's peer count, the paths in view and their bands."""
        stations = {c.station_calls[i]: None for i in np.flatnonzero(st_in)}
        paths = {}
        for i in np.flatnonzero(ends_in.any(axis=1)):
            p = c.paths[i]
            local = [j for j in (0, 1) if ends_in[i, j]]
            paths[i] = dict(p, ends=[tuple(ends[i, 0]), tuple(ends[i, 1])], local=local, index=i)
            for j in local:          # a path end at the very edge may be off the whole map's list
                stations.setdefault(p["calls"][j], None)
        _, _, chosen, degrees, total = a.select_paths(stations, c.bands, paths)
        return [p["index"] for p in chosen], degrees, total, {p["band"]: p["colour"] for p in paths.values()}

    def draw_contacts(self, surface, view, s, resting):
        """Paths under every marker; stations, APRS and our QSOs with labels. Returns the
        legend's (colour, text) parts."""
        c = self.contacts
        if c is None:
            surface.blit(self.layer, (0, 0), special_flags=pygame.BLEND_ALPHA_SDL2)
            return []
        ends, st = view.points(c.path_ends), view.points(c.station_xy)
        ends_in, st_in = inside(ends), inside(st)
        if resting or self.selection_key is None or self.selection_key[0] != id(c):
            key = id(c), ends_in.tobytes()
            if key != self.selection_key:
                self.selection_key, self.selection = key, self.select(c, ends, ends_in, st_in)
        chosen, degrees, total, bands = self.selection

        for i in chosen:          # paths leaving the view are faint
            if ends_in[i].any():
                alpha = 155 if ends_in[i].all() else 48
                pygame.draw.line(self.layer, c.paths[i]["colour"] + (alpha,), (ends[i, 0] * s).tolist(),
                                 (ends[i, 1] * s).tolist())
        links = view.points(c.link_ends)
        for (p, q), both in zip(links, inside(links).all(axis=1)):
            pygame.draw.line(self.layer, a.APRS_RF + (175 if both else 48,), (p * s).tolist(), (q * s).tolist())
        de = view.project((W / 2, H / 2))
        qsos = view.points(c.qso_xy)
        for p in qsos:
            pygame.draw.line(self.layer, a.WHITE + (230,), (de[0] * s, de[1] * s), (p * s).tolist(),
                             max(1, round(2 * s)))
        surface.blit(self.layer, (0, 0), special_flags=pygame.BLEND_ALPHA_SDL2)

        r = 5 * s
        shown_st = np.flatnonzero(st_in)
        for i in sorted(shown_st, key=lambda i: not c.station_style[i][1]):    # senders first
            (x, y), (colour, sent), call = st[i], c.station_style[i], c.station_calls[i]
            pygame.draw.polygon(surface, colour, [(x * s, y * s - r), (x * s + r, y * s),
                                                  (x * s, y * s + r), (x * s - r, y * s)])
            self.label(surface, x, y, call + (f" +{degrees[call]}" if degrees.get(call, 0) > 2 else ""),
                       a.WHITE if sent else a.DIM)
        heard = view.points(c.aprs_xy)
        shown = np.flatnonzero(inside(heard))
        for i in sorted(shown, key=lambda i: c.aprs[i][1]):          # RF-heard on top
            call, rf = c.aprs[i]
            x, y = heard[i]
            colour = a.APRS_RF if rf else a.APRS_NET
            pygame.draw.circle(surface, (0, 0, 0), (x * s, y * s), max(2, round(4 * s)) + 1)
            pygame.draw.circle(surface, colour, (x * s, y * s), max(2, round(4 * s)))
            self.label(surface, x, y, call, colour)
        for call, (x, y) in zip(c.qso_calls, qsos):
            if view.inside(x, y):
                pygame.draw.rect(surface, a.WHITE, ((x - 4) * s, (y - 4) * s, 8 * s, 8 * s))
                self.label(surface, x, y, call, a.WHITE)
        order = {name: i for i, (_, name, _) in enumerate(a.BANDS)}
        parts = [(a.APRS_RF, f"APRS {len(shown)}"), (a.WHITE, f"DIGI {len(shown_st)}"),
                 (a.DIM, f"PATH {len(chosen)}/{total}")]
        return parts + [(colour, name if name.endswith("cm") else name + "m")
                        for name, colour in sorted(bands.items(), key=lambda b: order.get(b[0], 99))]

    def draw_places(self, surface, view, s):
        """Towns: named within reach on the whole map (and everywhere once zoomed in), the rest
        as dots; our farthest visible point; us."""
        xy = view.points(self.place_xy)
        named = 0
        for i in np.flatnonzero(inside(xy)):
            x, y = xy[i]
            if view.zoom >= 2 or (self.place_km[i] <= self.name_km and named < 40):
                named += 1
                pygame.draw.rect(surface, CITY, ((x - 2) * s, (y - 2) * s, 4 * s, 4 * s))
                self.label(surface, x, y, self.place_names[i], CITY, 14)
            else:
                pygame.draw.rect(surface, TOWN, (x * s, y * s, 2, 2))
        x, y = view.project(self.far[0])
        if view.inside(x, y):
            pygame.draw.polygon(surface, terrain.SEEN, [(x * s, (y - 7) * s), ((x + 6) * s, (y + 5) * s),
                                                        ((x - 6) * s, (y + 5) * s)])
            self.label(surface, x, y, self.far[1], terrain.SEEN)
        x, y = view.project((W / 2, H / 2))
        if view.inside(x, y):
            pygame.draw.circle(surface, terrain.DE_GREEN, (x * s, y * s), round(9 * s), max(1, round(3 * s)))
            pygame.draw.circle(surface, (0, 0, 0), (x * s, y * s), max(1, round(3 * s)))

    def draw_hud(self, surface, view, s, metres, legend):
        """Scale bar and terrain detail bottom left, legend bottom right, sun top right."""
        km = 130 * view.km_per_px
        decade = 10 ** math.floor(math.log10(km))
        km = max(v * decade for v in (1, 2, 5) if v * decade <= km)
        bar = km / view.km_per_px
        detail = f"DEM {metres:.0f} m  {view.zoom:.1f}x"
        width = max(bar, self.text_size(f"{km:g} km", 12)[0]) + self.text_size(detail, 12)[0] + 30
        pygame.draw.rect(surface, (0, 0, 0), (2 * s, (H - 26) * s, width * s, 24 * s))
        right = 0
        pygame.draw.line(surface, a.WHITE, (10 * s, (H - 8) * s), ((10 + bar) * s, (H - 8) * s), max(1, round(2 * s)))
        self.text(surface, f"{km:g} km", (10, H - 25), a.WHITE, 12)
        self.text(surface, detail, (width - self.text_size(detail, 12)[0] - 6, H - 21), a.DIM, 12)

        if legend:
            widths = [self.text_size(text, 12)[0] + 18 for _, text in legend]
            x = W - 6 - sum(widths)
            right = W - x + 6
            pygame.draw.rect(surface, (0, 0, 0), ((x - 4) * s, (H - 22) * s, (W - x + 2) * s, 20 * s))
            pygame.draw.rect(surface, a.WHITE, ((x - 4) * s, (H - 22) * s, (W - x + 2) * s, 20 * s), 1)
            for (colour, text), w in zip(legend, widths):
                pygame.draw.rect(surface, colour, (x * s, (H - 15) * s, 8 * s, 8 * s))
                self.text(surface, text, (x + 12, H - 20), a.WHITE, 12)
                x += w
        self.hud = (width + 4, right)

        sun = f"SUN {float(solar_elevation(view.lat0, view.lon0, time.time())):+.0f} deg"
        tw = self.text_size(sun, 12)[0]
        pygame.draw.rect(surface, (0, 0, 0), ((W - tw - 16) * s, 4 * s, (tw + 12) * s, 20 * s))
        pygame.draw.rect(surface, GREY, ((W - tw - 16) * s, 4 * s, (tw + 12) * s, 20 * s), 1)
        self.text(surface, sun, (W - tw - 10, 6), a.WHITE, 12)


# ------------------------------------------------------------------------ canvas ----

class Raster:
    """The ground for one view: the picture without night, and with night as a surface. The
    surface has a 1 px border repeating the edge (pixel i is at i + 1), so a scaled sample can
    always reach half a pixel past the edge."""

    def __init__(self, view, ground, metres, rgb, minute):
        self.view, self.ground, self.metres = view, ground, metres
        self.shaded(rgb, minute)

    def shaded(self, rgb, minute):
        pic = pygame.image.frombytes(rgb, (W, H), "RGB")
        surface = pygame.Surface((W + 2, H + 2))
        surface.blit(pic, (1, 1))
        for at, area in (((1, 0), (0, 0, W, 1)), ((1, H + 1), (0, H - 1, W, 1)),
                         ((0, 1), (0, 0, 1, H)), ((W + 1, 1), (W - 1, 0, 1, H))):
            surface.blit(pic, at, area)
        for at, src in (((0, 0), (0, 0)), ((W + 1, 0), (W - 1, 0)), ((0, H + 1), (0, H - 1)),
                        ((W + 1, H + 1), (W - 1, H - 1))):
            surface.set_at(at, pic.get_at(src))
        self.surface = surface.convert() if pygame.display.get_surface() else surface
        self.minute = minute


def span(lo, hi, k, origin):
    """Along one axis, to cover raster coordinates lo..hi: the pixels [first, first + count)
    (as surface indices) that smoothscale should scale to `size` px and place at `at`, so that
    raster coordinate u lands at (u - origin) * k (pixel i spans i..i+1). smoothscale shrinks
    corner to corner, by area, but expands putting source pixel j's value at destination pixel
    j * (count - 1) / size."""
    if k > 1:
        first = math.floor(lo - 0.5)                     # padding covers -1 and the far edge
        count = math.ceil(hi - 0.5) - first + 1
        size = round(k * (count - 1))
        if size > count:
            return first + 1, count, size, round((first + 0.5 - origin) * k - 0.5)
    first = math.floor(lo)
    count = max(1, math.ceil(hi) - first)
    return first + 1, count, max(1, round(count * k)), round((first - origin) * k)


class Canvas:
    """HamClock's map box: the ground raster under the vector scene, through the camera."""

    def __init__(self):
        base = a.Map()
        self.camera, self.scene = Camera(base), Scene(base)
        self.view = self.camera.view()
        ground = Image.open(os.path.join(a.MODEL, "rangeground.png")).convert("RGB")
        if ground.size != (W, H):
            raise ValueError("rangeground.png does not fit the map box; rerun build_rangemap.py")
        whole, minute = View(base), int(time.time() // 60)
        self.whole = Raster(whole, ground, dem_metres(terrain.DEM_ZOOM, base.lat0),
                            shade(ground, whole, minute * 60), minute)
        self.detail = self.previous = None
        self.workers = self.job = self.fading = None
        self.moved_at = self.loaded_at = -math.inf
        self.retry_at = 0
        self.dirty, self.resting = True, True
        # the scene's pass at its contacts covers the time before the first frame
        self.scene.poll(self.submit, time.monotonic())

    def submit(self, fn, *args):
        """Run fn(*args) in the worker process; started on first use, restarted if it died."""
        try:
            if self.workers is None:
                raise BrokenProcessPool
            return self.workers.submit(fn, *args)
        except BrokenProcessPool:
            self.workers = ProcessPoolExecutor(1, mp_context=multiprocessing.get_context("spawn"))
            return self.workers.submit(fn, *args)

    def scroll(self, notches, at):
        if self.camera.scroll(notches, at):
            self.moved_at, self.retry_at = time.monotonic(), 0
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
        changed = self.scene.poll(self.submit, now) or moved or self.dirty or now - self.loaded_at < FADE_S \
            or resting != self.resting                   # at rest: labels and paths chosen afresh
        self.dirty, self.resting = False, resting
        if self.job and self.job[-1].done():
            changed = self.finish(now) or changed
        if self.job is None:
            self.job = self.next_job(now)
        return changed

    def next_job(self, now):
        """(what, subject, minute, future) for the worker, or None: the resting view's own
        raster first, then night brought up to the minute."""
        minute = int(time.time() // 60)
        if now - self.moved_at > REST_S:
            goal = self.camera.at(self.camera.goal)
            if goal.zoom == 1:
                self.detail = None                       # the whole map's raster is that
            elif now >= self.retry_at and (self.detail is None or self.detail.view.key() != goal.key()):
                return "ground", goal, minute, self.submit(render, goal, minute * 60)
        for raster in (self.whole, self.detail):
            if raster and raster.minute != minute:
                return "night", raster, minute, self.submit(shade, raster.ground, raster.view, minute * 60)
        return None

    def finish(self, now):
        what, subject, minute, future = self.job
        self.job = None
        try:
            result = future.result()
        except Exception as e:                           # tiles unreachable, worker died...
            log.warning("map %s: %s", what, e)
            if isinstance(e, BrokenProcessPool):
                self.workers = None
            if what == "ground":
                self.retry_at = now + RETRY_S
            else:
                subject.minute = minute                  # keep the old night until the next
            return False
        if what == "night":
            subject.shaded(result, minute)
            return True
        self.previous = self.detail or self.whole
        self.detail = Raster(subject, *result, minute)
        self.loaded_at = now
        return True

    def sample(self, raster, dest, part=False):
        """Draw `raster` under the current view, scaled onto `dest`: only if it covers the whole
        view, or with `part` whatever of it is in view. True if it drew."""
        if raster is None:
            return False
        src = raster.view
        (x0, y0), (x1, y1) = (src.project(self.view.unproject(p)) for p in ((0, 0), (W, H)))
        lo, hi = (max(0, x0), max(0, y0)), (min(W, x1), min(H, y1))
        whole = lo[0] - x0 < 1e-6 and lo[1] - y0 < 1e-6 and x1 - hi[0] < 1e-6 and y1 - hi[1] < 1e-6
        if not (whole or part) or hi[0] <= lo[0] or hi[1] <= lo[1]:
            return False
        k = self.view.zoom / src.zoom * dest.get_width() / W
        (fx, nx, sx, ax), (fy, ny, sy, ay) = (span(lo[i], hi[i], k, (x0, y0)[i]) for i in (0, 1))
        clip = dest.get_clip()
        if not whole:                                    # keep the padding off what lies beyond
            dest.set_clip(pygame.Rect(math.floor((lo[0] - x0) * k), math.floor((lo[1] - y0) * k),
                                      math.ceil((hi[0] - lo[0]) * k), math.ceil((hi[1] - lo[1]) * k)).clip(clip))
        dest.blit(pygame.transform.smoothscale(raster.surface.subsurface((fx, fy, nx, ny)), (sx, sy)), (ax, ay))
        dest.set_clip(clip)
        return True

    def ground(self, raster, dest):
        """`raster` over the whole view, else the whole map with `raster` sharp where it
        reaches; the metres a DEM px of what covers the view."""
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
