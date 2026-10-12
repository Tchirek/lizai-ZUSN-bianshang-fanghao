"""Local VHF/UHF path inspection, and the night over the map: twilight shading and the
terminator."""
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

SUNSET = -0.833               # the sun's elevation at sunrise and sunset: upper limb, refracted
DARK = -18                    # full night: astronomical twilight is over
# the veil deepens from blue hour at the terminator to indigo at full night, never so far that
# the relief stops reading; golden hour warms the day side a little; the terminator is a gold line
BLUE_HOUR, NIGHT, VEIL = (70, 60, 140), (8, 14, 44), 112
GOLDEN_HOUR, GLOW, GLOW_DEG = (255, 178, 86), 30, 5
SUN_GOLD, EDGE, EDGE_ALPHA = (255, 210, 120), (26, 16, 0), 0.6
LINE_PX = 2                   # the terminator's width in the picture's px, on a 1 px dark edge
SHADOW_PX, SHADOW = 14, 0.35  # and a soft shadow this wide on its night side, this dark at the line
SUN_ICON_R, SUN_DOWN = 8, 110  # the sun in the HUD: radius (px of the map), alpha once it has set
SDO_ORANGE = (250, 140, 30)   # its colour in SDO's continuum pictures, for want of one


def sun(when):
    """UTC hour, equation of time (minutes) and declination (radians) at `when`; NOAA's
    fractional-year approximation."""
    dt = datetime.fromtimestamp(when, timezone.utc)
    hour = dt.hour + dt.minute / 60 + dt.second / 3600
    gamma = 2 * math.pi / (366 if calendar.isleap(dt.year) else 365) * (dt.timetuple().tm_yday - 1 + (hour - 12) / 24)
    eq = 229.18 * (.000075 + .001868 * math.cos(gamma) - .032077 * math.sin(gamma)
                  - .014615 * math.cos(2 * gamma) - .040849 * math.sin(2 * gamma))
    decl = (.006918 - .399912 * math.cos(gamma) + .070257 * math.sin(gamma)
            - .006758 * math.cos(2 * gamma) + .000907 * math.sin(2 * gamma)
            - .002697 * math.cos(3 * gamma) + .00148 * math.sin(3 * gamma))
    return hour, eq, decl


def solar_elevation(lat, lon, when):
    """Degrees above the geometric horizon."""
    hour, eq, decl = sun(when)
    ha = np.radians((hour * 60 + eq + 4 * np.asarray(lon)) / 4 - 180)
    lat = np.radians(lat)
    return np.degrees(np.arcsin(np.clip(np.sin(lat) * math.sin(decl) + np.cos(lat) * math.cos(decl) * np.cos(ha), -1, 1)))


def sun_times(lat, lon, when):
    """Sunrise and sunset (unix times) on the solar day whose noon is nearest `when`, by NOAA's
    sunrise equation; (None, None) on a day the sun stays up or down."""
    _, eq, _ = sun(when)
    noon = when - when % 86400 + (720 - 4 * lon - eq) * 60
    noon += round((when - noon) / 86400) * 86400
    _, _, decl = sun(noon)
    phi = math.radians(lat)
    cos_ha = (math.sin(math.radians(SUNSET)) - math.sin(phi) * math.sin(decl)) / (math.cos(phi) * math.cos(decl))
    if abs(cos_ha) > 1:
        return None, None
    half = math.degrees(math.acos(cos_ha)) * 240                # 4 minutes a degree
    return noon - half, noon + half


def sun_text(lat, lon, when):
    """The sun at lat/lon for a HUD: its elevation, and that day's sunrise and sunset in local
    time, as (label, value) pairs."""
    elevation = float(solar_elevation(lat, lon, when))
    parts = [("SUN", f"{elevation:+.0f}°".replace("-", "−"))]
    for name, t in zip(("RISE", "SET"), sun_times(lat, lon, when)):
        if t is not None:
            parts.append((name, time.strftime("%H:%M", time.localtime(t))))
    return elevation, parts


def sun_picture(radius, alpha=255):
    """The sun for a HUD, `radius` px: SDO's latest picture of it, as HamClock's SDO pane shows
    it (activity.py keeps sdo.png), else a plain disc in its colour; RGBA, `alpha` opaque."""
    try:
        with Image.open(os.path.join(a.MODEL, "sdo.png")) as f:
            disc = f.convert("RGBA")
    except OSError:
        disc = Image.new("RGBA", (64, 64))
        ImageDraw.Draw(disc).ellipse([0, 0, 63, 63], fill=SDO_ORANGE + (255,))
    n = max(1, round(2 * radius))
    pic = disc.resize((n, n), Image.Resampling.LANCZOS)
    pic.putalpha(pic.getchannel("A").point(lambda v: v * alpha // 255))
    return pic


def over(px, colour, cover):
    """Lay `colour` at `cover` (0..1 each) over the straight-alpha RGBA rows `px`, in place."""
    under = px[:, 3] / 255 * (1 - cover)
    alpha = cover + under
    px[:, :3] = (np.multiply.outer(cover, colour) + px[:, :3] * under[:, None]) / np.maximum(alpha, 1e-6)[:, None]
    px[:, 3] = alpha * 255


def twilight(alt, size, step):
    """The night over a picture `size` (w, h) px, from the sun's elevation `alt` sampled at the
    middle of every `step` px square from its top left: as an RGBA picture, a veil deepening
    from blue hour at the terminator to indigo at full night, golden hour faintly warming the
    day side and a soft shadow along the terminator's night side; and the terminator itself,
    where the sun rises or sets now, a gold line on a dark edge drawn at the picture's own px
    (sharp at any zoom), as (x, y, RGBA array) of the box of px it covers, or None. The line
    is apart so that it can go over what is drawn on the ground (rivers, paths)."""
    alt = np.asarray(alt, dtype=np.float32)
    box = (0, 0, size[0] / step, size[1] / step)
    t = np.clip((SUNSET - alt) / (SUNSET - DARK), 0, 1)[..., None]      # 0 at the line, 1 at night
    u = np.clip((alt - SUNSET) / GLOW_DEG, 0, 1)[..., None]             # 0 at the line, 1 past golden hour
    night = alt[..., None] < SUNSET
    rgb = np.where(night, np.add(BLUE_HOUR, np.subtract(NIGHT, BLUE_HOUR) * t), GOLDEN_HOUR)
    alpha = np.where(night, VEIL * (1 - (1 - t) ** 2), GLOW * (1 - u) ** 2)
    coarse = Image.fromarray(np.concatenate([rgb, alpha], axis=-1).round().astype(np.uint8))
    picture = coarse.resize(size, Image.Resampling.BILINEAR, box=box)    # smooth: every step-th px is enough
    # the sun's elevation falls one degree a degree of arc from the point under it, so its
    # gradient is the same everywhere: px from the line is the elevation off SUNSET over it
    per_px = max(float(np.median(np.hypot(*np.gradient(alt)))) / step, 1e-9)
    reach = LINE_PX / 2 + 1.5
    # only the px around samples near the line are worked out one by one
    j, i = np.nonzero((alt - SUNSET < (reach + 1.5 * step) * per_px) & (alt - SUNSET > -(SHADOW_PX + 1.5 * step) * per_px))
    if not len(j):
        return picture, None
    x0, y0 = max(0, (int(i.min()) - 1) * step), max(0, (int(j.min()) - 1) * step)
    x1, y1 = min(size[0], (int(i.max()) + 2) * step), min(size[1], (int(j.max()) + 2) * step)
    if x1 <= x0 or y1 <= y0:
        return picture, None
    fine = np.asarray(Image.fromarray(alt, "F").resize(
        (x1 - x0, y1 - y0), Image.Resampling.BILINEAR, box=(x0 / step, y0 / step, x1 / step, y1 / step)))
    off = (fine - SUNSET) / per_px                                      # px from the line, + by day
    ys, xs = np.nonzero((off < 0) & (off > -SHADOW_PX))
    if len(ys):
        rgba = np.array(picture.crop((x0, y0, x1, y1)))
        px = rgba[ys, xs].astype(np.float32)
        over(px, NIGHT, SHADOW * (1 + off[ys, xs] / SHADOW_PX) ** 2)
        rgba[ys, xs] = px.round().astype(np.uint8)
        picture.paste(Image.fromarray(rgba), (x0, y0))
    ys, xs = np.nonzero(np.abs(off) < reach)
    if not len(ys):
        return picture, None
    d = np.abs(off[ys, xs])
    px = np.zeros((len(ys), 4), np.float32)
    over(px, EDGE, EDGE_ALPHA * np.clip(reach - d, 0, 1))
    over(px, SUN_GOLD, np.clip(LINE_PX / 2 + 0.5 - d, 0, 1))
    line = np.zeros((ys.max() - ys.min() + 1, xs.max() - xs.min() + 1, 4), np.uint8)
    line[..., :3] = EDGE                                 # clear px take the edge's colour: no fringe when scaled
    line[ys - ys.min(), xs - xs.min()] = px.round().astype(np.uint8)
    return picture, (x0 + int(xs.min()), y0 + int(ys.min()), line)


def night_layer(m, when=None, hud=True):
    """The night over the whole map as a transparent picture, the sun's HUD at its top right."""
    when = time.time() if when is None else when
    step = 4
    y, x = np.mgrid[0:a.H + step:step, 0:a.W + step:step] + step / 2
    if hasattr(m, 'grid_latlon'):
        lat, lon = m.grid_latlon(x, y)
    else:
        dx, dy = (x - a.W / 2) * m.km_per_px, (y - a.H / 2) * m.km_per_px
        lat, lon = terrain.destination(m.lat0, m.lon0, np.degrees(np.arctan2(dx, -dy)), np.hypot(dx, dy))
    img, line = twilight(solar_elevation(lat, lon, when), (a.W, a.H), step)
    if line is not None:
        img.alpha_composite(Image.fromarray(line[2]), line[:2])
    if not hud:
        return img
    d = ImageDraw.Draw(img)
    elevation, parts = sun_text(m.lat0, m.lon0, when)
    f = a.F_SMALL
    widths = [d.textlength(name + " ", font=f) + d.textlength(value, font=f) for name, value in parts]
    right = a.W - 4
    left = right - 8 - 22 - sum(widths) - 12 * (len(parts) - 1)
    d.rectangle([left, 4, right, 24], fill=(0, 0, 0, 220), outline=GREY)
    icon = sun_picture(SUN_ICON_R, 255 if elevation > SUNSET else SUN_DOWN)
    img.alpha_composite(icon, (round(left + 15 - icon.width / 2), round(14 - icon.height / 2)))
    x = left + 30
    for (name, value), w in zip(parts, widths):
        d.text((x, 6), name, font=f, fill=a.DIM)
        d.text((x + d.textlength(name + " ", font=f), 6), value, font=f, fill=a.WHITE)
        x += w + 12
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
