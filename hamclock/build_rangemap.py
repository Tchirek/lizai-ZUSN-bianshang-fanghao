#!/usr/bin/env python3
"""Build the UV-K5 range map shown in place of HamClock's world map.

A UV-K5 works VHF/UHF FM: contacts are line of sight, and 5 W is not the limit -- at 145 MHz
the free-space loss to 350 km is ~127 dB, leaving ~40 dB above a typical -122 dBm receiver.
So the theoretical reach is the radio viewshed: for 1440 bearings, step out along the ground and
keep the points from which a station antenna `--their-agl` m above ground has a clear path to
ours (`--our-agl` m above ground at the station), with standard refraction (k = 4/3).

Terrain: public Terrarium elevation tiles (zoom 8, ~0.5 km), cached in ~/influ/model/dem/.
Places and major rivers: OpenStreetMap via Overpass, cached once.
Output: ~/influ/model/rangemap.png at the size of HamClock's map box (1320x660 at 1600x960),
azimuthal-equidistant around the station, drawn in HamClock's look; and rangemap.json.

usage: build_rangemap.py [--our-agl 16] [--their-agl 10] [--reach-km 500]
"""
import argparse
import io
import json
import math
import os
import urllib.parse
import urllib.request

import numpy as np
from PIL import Image, ImageDraw, ImageFont
from scipy.ndimage import map_coordinates

MODEL = os.path.expanduser("~/influ/model")
W, H = 1320, 660                       # HamClock map box at 1600x960 (660x330 app px, x2)
R_EARTH = 6371.0                       # km
K_FACTOR = 4 / 3                       # standard atmospheric refraction
DEM_ZOOM = 8
DEM_URL = "https://s3.amazonaws.com/elevation-tiles-prod/terrarium/{z}/{x}/{y}.png"
OVERPASS = ["https://overpass-api.de/api/interpreter", "https://overpass.kumi.systems/api/interpreter",
            "https://overpass.private.coffee/api/interpreter"]
RIVERS = "长江|嘉陵江|涪江|岷江|沱江|渠江|乌江|大渡河|青衣江|雅砻江|金沙江|琼江|郪江"
UA = {"User-Agent": "influ-rangemap/1.0 (personal station)"}

# HamClock-like palette
GRID = (70, 70, 140)                   # Maidenhead lines (HamClock map grid, lifted for dark land)
RING = (150, 150, 200)
SEEN = (255, 215, 0)                   # areas in line of sight
RIVER = (70, 120, 210)
TOWN = (150, 150, 150)
DE_GREEN = (0, 255, 0)
WHITE, BLACK = (255, 255, 255), (0, 0, 0)


def font(size, bold=True):
    name = "FreeSansBold.ttf" if bold else "FreeSans.ttf"
    for d in ("/usr/share/fonts/truetype/freefont", "/usr/share/fonts/truetype/liberation"):
        for n in (name, "LiberationSans-Bold.ttf" if bold else "LiberationSans-Regular.ttf"):
            if os.path.exists(os.path.join(d, n)):
                return ImageFont.truetype(os.path.join(d, n), size)
    return ImageFont.load_default()


# ---------------------------------------------------------------- geometry ----

def destination(lat0, lon0, bearing_deg, dist_km):
    """Great-circle destination; arrays welcome. Returns lat, lon in degrees."""
    p1, l1 = math.radians(lat0), math.radians(lon0)
    th, dl = np.radians(bearing_deg), np.asarray(dist_km) / R_EARTH
    p2 = np.arcsin(np.sin(p1) * np.cos(dl) + np.cos(p1) * np.sin(dl) * np.cos(th))
    l2 = l1 + np.arctan2(np.sin(th) * np.sin(dl) * np.cos(p1), np.cos(dl) - np.sin(p1) * np.sin(p2))
    return np.degrees(p2), (np.degrees(l2) + 540) % 360 - 180


def polar(lat0, lon0, lat, lon):
    """Distance (km) and bearing (deg) from the station; arrays welcome."""
    p1, p2 = math.radians(lat0), np.radians(lat)
    dl = np.radians(np.asarray(lon) - lon0)
    a = np.sin((p2 - p1) / 2) ** 2 + np.cos(p1) * np.cos(p2) * np.sin(dl / 2) ** 2
    d = 2 * R_EARTH * np.arcsin(np.sqrt(np.clip(a, 0, 1)))
    b = np.degrees(np.arctan2(np.sin(dl) * np.cos(p2),
                              np.cos(p1) * np.sin(p2) - np.sin(p1) * np.cos(p2) * np.cos(dl)))
    return d, b % 360


def merc_px(lat, lon, z=DEM_ZOOM):
    n = 256 * 2 ** z
    x = (np.asarray(lon) + 180) / 360 * n
    lr = np.radians(lat)
    y = (1 - np.log(np.tan(lr) + 1 / np.cos(lr)) / math.pi) / 2 * n
    return x, y


# --------------------------------------------------------------------- DEM ----

class DEM:
    """Terrarium elevation mosaic for a lat/lon box, bilinear lookup in metres."""

    def __init__(self, south, west, north, east):
        x0, y0 = merc_px(north, west)
        x1, y1 = merc_px(south, east)
        self.tx0, self.ty0 = int(x0 // 256), int(y0 // 256)
        tx1, ty1 = int(x1 // 256), int(y1 // 256)
        rows = []
        for ty in range(self.ty0, ty1 + 1):
            rows.append(np.hstack([self.tile(tx, ty) for tx in range(self.tx0, tx1 + 1)]))
        self.grid = np.vstack(rows)

    @staticmethod
    def tile(tx, ty):
        path = os.path.join(MODEL, "dem", f"{DEM_ZOOM}_{tx}_{ty}.png")
        if not os.path.exists(path):
            os.makedirs(os.path.dirname(path), exist_ok=True)
            data = urllib.request.urlopen(urllib.request.Request(
                DEM_URL.format(z=DEM_ZOOM, x=tx, y=ty), headers=UA), timeout=60).read()
            open(path, "wb").write(data)
        a = np.asarray(Image.open(path).convert("RGB"), dtype=np.float32)
        return a[..., 0] * 256 + a[..., 1] + a[..., 2] / 256 - 32768

    def __call__(self, lat, lon):
        x, y = merc_px(lat, lon)
        return map_coordinates(self.grid, [y - self.ty0 * 256, x - self.tx0 * 256], order=1,
                               mode="nearest")


# ---------------------------------------------------------------- viewshed ----

def viewshed(dem, lat0, lon0, our_agl, their_agl, reach_km, step_km=0.25, n_az=1440):
    """Visibility on a (bearing, distance) grid: True where a station `their_agl` m above the
    ground can see our antenna, with earth curvature and k-factor refraction."""
    az = np.arange(n_az) * 360 / n_az
    d = np.arange(1, int(reach_km / step_km) + 1) * step_km
    lat, lon = destination(lat0, lon0, az[:, None], d[None, :])
    ground = dem(lat, lon)
    h_obs = float(dem(np.array([lat0]), np.array([lon0]))[0]) + our_agl
    drop = (d * 1000) ** 2 / (2 * K_FACTOR * R_EARTH * 1000)     # m below our horizontal
    terrain = (ground - drop - h_obs) / (d * 1000)                # tangent of elevation angle
    target = (ground + their_agl - drop - h_obs) / (d * 1000)
    horizon = np.maximum.accumulate(terrain, axis=1)
    blocked = np.concatenate([np.full((n_az, 1), -np.inf), horizon[:, :-1]], axis=1)
    return az, d, target >= blocked, h_obs


# ------------------------------------------------------------------ vectors ----

def overpass(query):
    for url in OVERPASS:
        try:
            req = urllib.request.Request(url, data=urllib.parse.urlencode({"data": query}).encode(),
                                         headers=UA)
            return json.load(urllib.request.urlopen(req, timeout=180))["elements"]
        except (OSError, ValueError) as e:
            print(url, "failed:", e)
    raise SystemExit("all Overpass mirrors failed")


def vectors(south, west, north, east):
    path = os.path.join(MODEL, "rangemap_osm.json")
    if os.path.exists(path):
        return json.load(open(path))
    bb = f"{south:.2f},{west:.2f},{north:.2f},{east:.2f}"
    els = overpass(f"""[out:json][timeout:180];
(
  node["place"="city"]({bb});
  way["waterway"="river"]["name"~"^({RIVERS})$"]({bb});
);
out tags geom({bb});""")
    places, lines = [], []
    for e in els:
        t = e.get("tags", {})
        if e["type"] == "node":
            pop = "".join(ch for ch in t.get("population", "") if ch.isdigit())
            places.append({"name": t.get("name:en") or t.get("name:zh-Latn-pinyin") or t.get("name", ""),
                           "lat": e["lat"], "lon": e["lon"], "pop": int(pop or 0),
                           "capital": t.get("capital", "")})
        elif e.get("geometry"):
            lines.append({"k": "river", "p": [[g["lat"], g["lon"]] for g in e["geometry"] if g]})
    out = {"places": places, "lines": lines}
    json.dump(out, open(path, "w"))
    return out


# ------------------------------------------------------------------ drawing ----

def relief(elev, km_per_px):
    """HamClock-ish land: muted hypsometric tint times a soft NW hillshade."""
    gy, gx = np.gradient(elev, km_per_px * 1000)
    slope = np.arctan(np.hypot(gx, gy) * 3)
    aspect = np.arctan2(-gx, gy)
    sun_az, sun_alt = math.radians(315), math.radians(45)
    shade = (np.sin(sun_alt) * np.cos(slope)
             + np.cos(sun_alt) * np.sin(slope) * np.cos(sun_az - aspect))
    stops = [(0, (34, 66, 40)), (500, (52, 86, 46)), (1200, (98, 92, 54)),
             (2500, (132, 112, 82)), (4000, (160, 154, 146)), (5500, (222, 224, 232))]
    h = np.clip(elev, stops[0][0], stops[-1][0])
    rgb = np.zeros(elev.shape + (3,))
    for c in range(3):
        rgb[..., c] = np.interp(h, [s[0] for s in stops], [s[1][c] for s in stops])
    return rgb * np.clip(0.55 + 0.6 * shade, 0.35, 1.25)[..., None]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--our-agl", type=float, default=16)
    ap.add_argument("--their-agl", type=float, default=10)
    ap.add_argument("--reach-km", type=float, default=500)
    a = ap.parse_args()
    st = json.load(open(os.path.expanduser("~/influ/station.json")))
    lat0, lon0 = st["lat"], st["lon"]

    # terrain far enough for the rays, and for the wide (2:1) map around them
    span_lat = a.reach_km / 111 + 0.5
    span_lon = 2 * a.reach_km / (111 * math.cos(math.radians(lat0))) + 0.5
    box = (round(lat0 - span_lat, 1), round(lon0 - span_lon, 1),
           round(lat0 + span_lat, 1), round(lon0 + span_lon, 1))
    dem = DEM(*box)
    az, d, vis, h_obs = viewshed(dem, lat0, lon0, a.our_agl, a.their_agl, a.reach_km)

    # reach per bearing and the farthest visible point overall
    far = np.where(vis.any(axis=1), d[vis.shape[1] - 1 - np.argmax(vis[:, ::-1], axis=1)], 0)
    i_max = int(np.argmax(far))
    reach = float(far[i_max])
    flat, flon = destination(lat0, lon0, az[i_max], reach)
    peak = float(dem(np.array([flat]), np.array([flon]))[0])

    # map scale: the farthest visible point sits inside the map's half height
    km_per_px = reach * 1.08 / (H / 2)
    ys, xs = np.mgrid[0:H, 0:W]
    dx, dy = (xs - W / 2) * km_per_px, (ys - H / 2) * km_per_px
    rho, brg = np.hypot(dx, dy), np.degrees(np.arctan2(dx, -dy)) % 360
    plat, plon = destination(lat0, lon0, brg, rho)
    rgb = relief(dem(plat, plon), km_per_px)

    # mark what our antenna can see: tinted, outlined; distant patches widened by a pixel so a
    # single visible mountain face still shows at this scale
    ia = np.round(brg / (360 / len(az))).astype(int) % len(az)
    idd = np.clip(np.round(rho / (d[1] - d[0])).astype(int) - 1, 0, len(d) - 1)
    seen = vis[ia, idd] & (rho <= d[-1])
    grown = seen | np.roll(seen, 1, 0) | np.roll(seen, -1, 0) | np.roll(seen, 1, 1) | np.roll(seen, -1, 1)
    seen = np.where(rho > 40, grown, seen)
    inner = (np.roll(seen, 1, 0) & np.roll(seen, -1, 0) & np.roll(seen, 1, 1) & np.roll(seen, -1, 1))
    rgb[seen] = rgb[seen] * 0.45 + np.array(SEEN) * 0.55
    rgb[seen & ~inner] = SEEN
    img = Image.fromarray(np.clip(rgb, 0, 255).astype(np.uint8))
    draw = ImageDraw.Draw(img)
    draw.fontmode = "1"

    def xy(lat, lon):
        dist, b = polar(lat0, lon0, np.asarray(lat), np.asarray(lon))
        r = dist / km_per_px
        return W / 2 + r * np.sin(np.radians(b)), H / 2 - r * np.cos(np.radians(b))

    # Maidenhead squares (2 x 1 degrees), as HamClock's grid
    f_small, f_label, f_title = font(13), font(15), font(17)
    for glon in range(int(box[1] // 2 * 2), int(box[3]) + 2, 2):
        x, y = xy(np.linspace(box[0], box[2], 60), np.full(60, glon))
        draw.line(list(zip(x, y)), fill=GRID)
    for glat in range(int(box[0]), int(box[2]) + 2):
        x, y = xy(np.full(60, glat), np.linspace(box[1], box[3], 60))
        draw.line(list(zip(x, y)), fill=GRID)
    for glon in range(int(box[1] // 2 * 2), int(box[3]) + 1, 2):
        for glat in range(int(box[0]), int(box[2]) + 1):
            name = (chr(65 + int((glon + 180) // 20)) + chr(65 + int((glat + 90) // 10))
                    + str(int((glon + 180) % 20 // 2)) + str(int((glat + 90) % 10)))
            x, y = xy(np.array([glat + 0.08]), np.array([glon + 0.08]))
            if 0 < x[0] < W - 40 and 16 < y[0] < H:
                draw.text((x[0] + 2, y[0] - 15), name, font=f_small, fill=GRID)

    # major rivers
    osm = vectors(*box)
    for ln in osm["lines"]:
        x, y = xy([p[0] for p in ln["p"]], [p[1] for p in ln["p"]])
        draw.line(list(zip(x, y)), fill=RIVER, width=2)

    # range rings every 100 km
    for r_km in range(100, int(W / 2 * km_per_px) + 100, 100):
        r = r_km / km_per_px
        draw.ellipse([W / 2 - r, H / 2 - r, W / 2 + r, H / 2 + r], outline=RING)
        if H / 2 - r - 16 > 0:
            draw.text((W / 2 + 3, H / 2 - r - 16), f"{r_km} km", font=f_small, fill=RING)

    # cities: named within reach (capitals and big cities first), plain dots beyond it
    def importance(p):
        rank = {"yes": 0, "2": 0, "4": 1, "5": 2, "6": 3}.get(p.get("capital", ""), 4)
        return rank, -p.get("pop", 0), polar(lat0, lon0, p["lat"], p["lon"])[0]

    taken = [(0, 0, 230, 26), (0, H - 24, 520, H)]           # title and note boxes
    for p in sorted(osm["places"], key=importance):
        x, y = (v[0] for v in xy([p["lat"]], [p["lon"]]))
        dist = polar(lat0, lon0, p["lat"], p["lon"])[0]
        if not (4 < x < W - 4 and 4 < y < H - 4):
            continue
        if dist > reach * 1.1 or not p["name"]:
            draw.point([(x, y)], fill=TOWN)
            continue
        tw = draw.textlength(p["name"], font=f_label)
        if dist < 12:                                    # our own town: label beside the DE ring
            x, y = W / 2 + 8, H / 2
        bx = (x - 3, y - 9, x + 5 + tw, y + 9)
        if bx[2] < W and not any(not (bx[2] < b[0] or bx[0] > b[2] or bx[3] < b[1] or bx[1] > b[3])
                                 for b in taken):
            taken.append(bx)
            draw.rectangle([x - 2, y - 2, x + 2, y + 2], fill=WHITE)
            draw.text((x + 5, y - 9), p["name"], font=f_label, fill=WHITE)
        else:
            draw.point([(x, y)], fill=TOWN)

    # farthest point and the station (HamClock's DE marker)
    fx, fy = (v[0] for v in xy([flat], [flon]))
    draw.polygon([(fx, fy - 7), (fx + 6, fy + 5), (fx - 6, fy + 5)], fill=SEEN, outline=BLACK)
    note = f"{reach:.0f} km {az[i_max]:.0f}°  {peak:.0f} m"
    nx = fx + 9 if fx < W - 160 else fx - 9 - draw.textlength(note, font=f_label)
    draw.text((nx, fy - 8), note, font=f_label, fill=SEEN)
    taken.append((nx, fy - 8, nx + draw.textlength(note, font=f_label), fy + 10))
    taken.append((W / 2 - 10, H / 2 - 10, W / 2 + 10, H / 2 + 10))      # DE marker
    draw.ellipse([W / 2 - 9, H / 2 - 9, W / 2 + 9, H / 2 + 9], outline=DE_GREEN, width=4)
    draw.ellipse([W / 2 - 3, H / 2 - 3, W / 2 + 3, H / 2 + 3], fill=BLACK)

    # title box like HamClock's map style label, and the assumptions
    title = "UV-K5 line of sight"
    tw = draw.textlength(title, font=f_title)
    draw.rectangle([2, 2, tw + 12, 24], fill=BLACK, outline=WHITE)
    draw.text((7, 4), title, font=f_title, fill=WHITE)
    note = f"our ant {a.our_agl:.0f} m AGL ({h_obs:.0f} m ASL)  their ant {a.their_agl:.0f} m  k=4/3"
    draw.rectangle([2, H - 22, draw.textlength(note, font=f_small) + 10, H - 2], fill=BLACK)
    draw.text((6, H - 20), note, font=f_small, fill=RING)

    os.makedirs(MODEL, exist_ok=True)
    img.save(os.path.join(MODEL, "rangemap.png"))
    summary = {"reach_km": reach, "bearing": float(az[i_max]), "far_lat": float(flat),
               "far_lon": float(flon), "far_elev_m": peak, "our_asl_m": h_obs,
               "our_agl_m": a.our_agl, "their_agl_m": a.their_agl, "km_per_px": km_per_px,
               "visible_km2": float(seen.sum() * km_per_px ** 2),
               "labels": [[round(v) for v in b] for b in taken]}     # for layers drawn on top
    json.dump(summary, open(os.path.join(MODEL, "rangemap.json"), "w"), indent=1)
    print(json.dumps(summary, indent=1))


if __name__ == "__main__":
    main()
