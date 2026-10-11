"""Local VHF/UHF path inspection and a restrained solar terminator layer."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import calendar
import json
import math
import os
import time

import numpy as np
from PIL import Image, ImageDraw

import activity as a
import build_rangemap as terrain
import build_tiles as tiles

FREQUENCIES = (144.174, 144.640, 145.025, 435.000)
HEIGHTS = (10, 20, 40, 80)
PW, PH = 320, 248
GREY = (139, 141, 139)
REDRAW_S = 10                 # the popup's station list follows activity.json this often


def solar_elevation(lat, lon, when):
    """NOAA's fractional-year approximation; degrees above the geometric horizon."""
    dt = datetime.fromtimestamp(when, timezone.utc)
    hour = dt.hour + dt.minute / 60 + dt.second / 3600
    gamma = 2 * math.pi / (366 if calendar.isleap(dt.year) else 365) * (dt.timetuple().tm_yday - 1 + (hour - 12) / 24)
    eq = 229.18 * (.000075 + .001868 * math.cos(gamma) - .032077 * math.sin(gamma)
                  - .014615 * math.cos(2 * gamma) - .040849 * math.sin(2 * gamma))
    decl = (.006918 - .399912 * math.cos(gamma) + .070257 * math.sin(gamma)
            - .006758 * math.cos(2 * gamma) + .000907 * math.sin(2 * gamma)
            - .002697 * math.cos(3 * gamma) + .00148 * math.sin(3 * gamma))
    ha = np.radians((hour * 60 + eq + 4 * np.asarray(lon)) / 4 - 180)
    lat = np.radians(lat)
    return np.degrees(np.arcsin(np.clip(np.sin(lat) * math.sin(decl) + np.cos(lat) * math.cos(decl) * np.cos(ha), -1, 1)))


def night_rgba(lat, lon, when):
    """Night over a grid of lat/lon: a light veil, full 6 degrees below the horizon, and a pale
    line along the terminator; RGBA."""
    alt = solar_elevation(lat, lon, when)
    rgba = np.zeros(alt.shape + (4,), dtype=np.uint8)
    rgba[..., :3] = (2, 12, 30)
    rgba[..., 3] = np.clip(-alt / 6, 0, 1) * 32  # terrain stays readable even at midnight
    night = alt < 0
    edge = np.zeros(night.shape, dtype=bool)
    edge[1:] |= night[1:] != night[:-1]
    edge[:, 1:] |= night[:, 1:] != night[:, :-1]
    rgba[edge] = (170, 175, 190, 130)
    return rgba


def night_layer(m, when=None, hud=True):
    when = time.time() if when is None else when
    y, x = np.mgrid[0:a.H:4, 0:a.W:4]
    if hasattr(m, 'grid_latlon'):
        lat, lon = m.grid_latlon(x, y)
    else:
        dx, dy = (x - a.W / 2) * m.km_per_px, (y - a.H / 2) * m.km_per_px
        lat, lon = terrain.destination(m.lat0, m.lon0, np.degrees(np.arctan2(dx, -dy)), np.hypot(dx, dy))
    rgba = night_rgba(lat, lon, when)
    img = Image.fromarray(rgba).resize((a.W, a.H), Image.Resampling.BILINEAR)
    if not hud:
        return img
    d = ImageDraw.Draw(img)
    text = f"SUN {float(solar_elevation(m.lat0, m.lon0, when)):+.0f} deg"
    tw = d.textlength(text, font=a.F_SMALL)
    d.rectangle([a.W - tw - 16, 4, a.W - 4, 24], fill=(0, 0, 0, 220), outline=GREY)
    d.text((a.W - tw - 10, 6), text, font=a.F_SMALL, fill=a.WHITE)
    return img


class CachedDEM(terrain.DEM):
    @staticmethod
    def tile(tx, ty, zoom=terrain.DEM_ZOOM):
        path = os.path.join(terrain.MODEL, 'dem', f'{zoom}_{tx}_{ty}.png')
        if not os.path.exists(path):
            raise OSError('terrain tile not cached')
        return terrain.DEM.tile(tx, ty, zoom)


def heights(lats, lons):
    """Terrain along a path: the finest local elevation tiles (~33 m), else the map's own
    (~530 m)."""
    try:
        return tiles.elevations(lats, lons, 12), 12
    except (OSError, SyntaxError):
        bounds = (float(lats.min() - .02), float(lons.min() - .02), float(lats.max() + .02), float(lons.max() + .02))
        return CachedDEM(*bounds)(lats, lons), terrain.DEM_ZOOM


def path_profile(m, lat, lon):
    distance, bearing = (float(v) for v in terrain.polar(m.lat0, m.lon0, lat, lon))
    d = np.linspace(0, max(distance, .001), max(3, int(distance / .03) + 1))   # 30 m steps
    lats, lons = terrain.destination(m.lat0, m.lon0, bearing, d)
    ground, zoom = heights(lats, lons)
    with open(os.path.join(a.MODEL, 'rangemap.json')) as f:
        info = json.load(f)
    our_agl = info['our_agl_m']
    effective = ground + (d * 1000) * ((d[-1] - d) * 1000) / (2 * terrain.K_FACTOR * terrain.R_EARTH * 1000)
    status = []
    for freq in FREQUENCIES:
        radius = np.sqrt((299.792458 / freq) * (d * 1000) * ((d[-1] - d) * 1000) / (d[-1] * 1000))
        row = []
        for agl in HEIGHTS:
            ray = ground[0] + our_agl + (ground[-1] + agl - ground[0] - our_agl) * d / d[-1]
            clear = ray[1:-1] - effective[1:-1]
            row.append('OK' if np.all(clear >= .6 * radius[1:-1]) else 'LOS' if np.all(clear >= 0) else 'BLK')
        status.append(row)
    ray = ground[0] + our_agl + (ground[-1] + HEIGHTS[0] - ground[0] - our_agl) * d / d[-1]
    return dict(distance=distance, bearing=bearing, d=d, ground=effective, ray=ray,
                clearance=float((ray - effective)[1:-1].min()), status=status, our_agl=our_agl,
                fspl=[32.45 + 20 * math.log10(f) + 20 * math.log10(max(distance, .001))
                      for f in FREQUENCIES],
                dem_m=156543.03 * math.cos(math.radians(m.lat0)) / 2 ** zoom)


def nearby(m, x, y):
    calls, peers = set(), set()
    try:
        with open(os.path.join(a.MODEL, 'activity.json')) as f:
            data = json.load(f)
        for r in data.get('reports', []):
            local = False
            for role in ('sender', 'receiver'):
                ll = a.locator(r.get(role + 'Locator', ''))
                if ll:
                    xx, yy = m.xy(*ll)
                    if math.hypot(xx - x, yy - y) * m.km_per_px <= 20:
                        calls.add(r[role + 'Callsign'])
                        local = True
            if local:
                peers.add(tuple(sorted((r['senderCallsign'], r['receiverCallsign']))))
        for call, v in data.get('aprs', {}).items():
            xx, yy = m.xy(*v[:2])
            if math.hypot(xx - x, yy - y) * m.km_per_px <= 20:
                calls.add(call)
    except (OSError, ValueError, TypeError, KeyError):
        pass
    return sorted(calls), len(peers)


def panel_image(m, point, profile=None, error=None):
    x, y = point
    lat, lon = m.latlon(x, y)
    distance, bearing = (float(v) for v in terrain.polar(m.lat0, m.lon0, lat, lon))
    img = Image.new('RGB', (PW, PH), a.BLACK)
    d = ImageDraw.Draw(img)
    d.fontmode = '1'
    f = a.font(13, False)
    title = 'VHF/UHF DE-DX'
    d.rectangle([0, 0, PW - 1, PH - 1], outline=GREY)
    d.text(((PW - d.textlength(title, font=a.F_SMALL)) / 2, 5), title, font=a.F_SMALL, fill=a.WHITE)
    d.rectangle([PW - 22, 3, PW - 4, 21], outline=GREY)
    d.text((PW - 18, 3), 'x', font=a.F_SMALL, fill=a.WHITE)
    d.text((8, 27), f'{distance:.1f} km  {bearing:.0f} deg   {lat:.2f},{lon:.2f}', font=f, fill=(255, 180, 0))
    d.text((8, 47), 'MHz', font=f, fill=GREY)
    for j, height in enumerate(HEIGHTS):
        d.text((84 + j * 56, 47), f'{height}m', font=f, fill=a.WHITE)
    colours = dict(OK=(0, 145, 0), LOS=(175, 125, 0), BLK=(145, 0, 0))
    for i, freq in enumerate(FREQUENCIES):
        yy = 65 + i * 20
        d.text((7, yy + 2), f'{freq:.3f}', font=f, fill=a.WHITE)
        for j in range(len(HEIGHTS)):
            text = profile['status'][i][j] if profile else '?'
            xx = 76 + j * 56
            d.rectangle([xx, yy, xx + 55, yy + 19], fill=colours.get(text, (25, 25, 25)), outline=(60, 60, 60))
            d.text((xx + 12, yy + 2), text, font=f, fill=a.WHITE)
    if profile:
        text = f'CLR10m {profile["clearance"]:+.0f}m  FSL {profile["fspl"][0]:.0f}-{profile["fspl"][-1]:.0f}dB'
        d.text((8, 148), text, font=f, fill=GREY)
        ground, ray = profile['ground'], profile['ray']
        lo, hi = min(float(ground.min()), float(ray.min())), max(float(ground.max()), float(ray.max()))
        xs = np.linspace(8, PW - 9, len(ground))
        ys = 195 - (ground - lo) / max(hi - lo, 1) * 29
        yr = 195 - (ray - lo) / max(hi - lo, 1) * 29
        d.polygon([(8, 196)] + list(zip(xs, ys)) + [(PW - 9, 196)], fill=(80, 65, 45))
        d.line(list(zip(xs, yr)), fill=a.WHITE, width=1)
    else:
        d.text((8, 161), error or 'Calculating terrain...', font=f, fill=GREY)
    calls, paths = nearby(m, x, y)
    d.text((8, 200), f'20km: {len(calls)} stations  {paths} paths', font=f, fill=(0, 220, 220))
    if calls:
        shown = list(calls)
        while len(shown) > 1 and d.textlength(' '.join(shown) + (f' +{len(calls)-len(shown)}' if len(shown)<len(calls) else ''), font=f) > PW-16:
            shown.pop()
        text = ' '.join(shown) + (f' +{len(calls)-len(shown)}' if len(shown)<len(calls) else '')
        d.text((8, 216), text, font=f, fill=a.WHITE)
    our = profile['our_agl'] if profile else 16
    resolution = f'  DEM {profile["dem_m"]:.0f}m' if profile else ''
    d.text((8, PH - 17), f'DE {our:.0f}m DX AGL k=4/3 F1 60%{resolution}', font=a.font(11, False), fill=GREY)
    return img


class MapPanel:
    """The popup for a tapped map point; it stays on that place as the map zooms and pans
    (whoever draws the map keeps `m` set to its current view)."""

    def __init__(self):
        self.m = a.Map()
        self.pool = ThreadPoolExecutor(max_workers=1)
        self.point = self.target = self.future = self.profile = self.error = None
        self.box = self.surface = None
        self.drawn_at = 0

    def open(self, point):
        if self.future:
            self.future.cancel()
        self.point, self.profile, self.error, self.surface = point, None, None, None
        self.target = self.m.latlon(*point)
        self.future = self.pool.submit(path_profile, self.m, *self.target)

    def close(self):
        self.point, self.box = None, None
        if self.future:
            self.future.cancel()
        self.future = None

    def tap(self, at, map_box):
        if self.box and self.box.collidepoint(at):
            if at[0] >= self.box.right - 24 and at[1] < self.box.top + 25:
                self.close()
            return True
        x, y, w, h = map_box
        if x <= at[0] < x + w and y <= at[1] < y + h:
            self.open(((at[0] - x) * a.W / w, (at[1] - y) * a.H / h))
            return True
        self.close()
        return False

    def draw(self, frame, map_box):
        import pygame
        if self.point is None:
            return
        self.point = self.m.xy(*self.target)
        if self.future and self.future.done():
            try:
                self.profile = self.future.result()
            except Exception:                    # tiles unreachable, broken tile, ...
                self.error = 'Terrain unavailable'
            self.future, self.surface = None, None
        mx, my, mw, mh = map_box
        pw, ph = round(PW * mw / a.W), round(PH * mh / a.H)     # scaled as the map box is
        if self.surface is None or time.monotonic() - self.drawn_at > REDRAW_S:
            img = panel_image(self.m, self.point, self.profile, self.error)
            pic = pygame.image.frombytes(img.tobytes(), img.size, 'RGB')
            self.surface = pic if (pw, ph) == (PW, PH) else pygame.transform.smoothscale(pic, (pw, ph))
            self.drawn_at = time.monotonic()
        x = mx + self.point[0] * mw / a.W
        y = my + self.point[1] * mh / a.H
        left = min(max(mx, x + 12), mx + mw - pw)
        top = min(max(my, y + 12), my + mh - ph)
        self.box = pygame.Rect(round(left), round(top), pw, ph)
        de = self.m.xy(self.m.lat0, self.m.lon0)
        clip = frame.get_clip()
        frame.set_clip(pygame.Rect(map_box))
        pygame.draw.line(frame, (230, 230, 180), (mx + de[0] * mw / a.W, my + de[1] * mh / a.H), (x, y), 1)
        pygame.draw.line(frame, (255, 255, 255), (x - 5, y), (x + 5, y), 1)
        pygame.draw.line(frame, (255, 255, 255), (x, y - 5), (x, y + 5), 1)
        frame.blit(self.surface, self.box)
        frame.set_clip(clip)
