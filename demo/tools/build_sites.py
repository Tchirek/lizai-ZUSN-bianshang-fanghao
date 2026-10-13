#!/usr/bin/env python3
"""Build the showcase page's data: for each summit the station roams to (demo/sites.json), what
its antenna sees -- the gold of the coverage map -- with the places, major rivers and civil
airports around it; and HamClock's own glyph tables for the page's text. (The ADS-B scope's
base map the page takes from OpenStreetMap's vector tiles itself.)

  reach    the line-of-sight viewshed of build_rangemap.py from the summit: reach.png, a bit a
           (bearing, distance) cell as build_rangemap.py keeps it, and reach.json
  map      the cities and towns, rivers and civil airports in the 1x map, from Natural Earth
           (map.json)
  spell    each Chinese name's pinyin as pypinyin reads it in the name, for the search
  glyphs   HamClock's Courier Prime Sans and Germano tables (1600x960), and the 16-dot Song for
           the Chinese characters in the data (glyphs.json)

No military place goes in: no military areas, no air bases. reach and map need the network
(Terrarium elevation tiles, Natural Earth, public domain); spell and glyphs need pypinyin,
xfonts-base's gb16st and HamClock's source (--hamclock-src), as on the station.

usage: build_sites.py [reach map spell glyphs] [--only ID] [--hamclock-src DIR]
"""
import argparse
import base64
import json
import math
import os
from pathlib import Path
import re
import sys
import unicodedata
import urllib.request

import numpy as np
from PIL import Image

DEMO = Path(__file__).resolve().parents[1]
REPO = DEMO.parent
sys.path[:0] = [str(REPO / 'hamclock'), str(REPO / 'tools')]
import build_basemap                     # noqa: E402
import build_rangemap as terrain         # noqa: E402

SITES = DEMO / 'sites.json'
OUT = DEMO / 'public' / 'data'
CACHE = Path(os.path.expanduser('~/.cache/influ-demo'))
W, H = 1320, 660                         # the map box at 1600x960, as the station's
RIVER_TOL_M = 250                        # how far a river's line may stray, simplified
SUMMIT_ZOOM = 12                         # elevation tiles the summit is found on
SUMMIT_KM = 0.6                          # how far from a site's given point its top may be
STEPS = ('reach', 'map', 'spell', 'glyphs')
MILITARY = re.compile(r'air ?base|air force|naval air|marine corps|\barmy\b|military|\b(raf|afb|nas|jasdf)\b'
                      r'|基地|空軍|空军|海軍|海军|陸軍|陆军|自衛隊|自卫队', re.I)


def sites(only=None):
    found = json.loads(SITES.read_text(encoding='utf-8'))
    return [s for s in found if only in (None, s['id'])]


def folder(site):
    path = OUT / site['id']
    path.mkdir(parents=True, exist_ok=True)
    return path


def write(path, data):
    part = path.with_name(path.name + '.part')
    part.write_text(json.dumps(data, ensure_ascii=False, separators=(',', ':')), encoding='utf-8')
    os.replace(part, path)


def military(name):
    """Whether a name says the place is military (an air base, a naval air station, ...)."""
    return bool(MILITARY.search(name))


# ------------------------------------------------------------------ reach ----

def summit(site):
    """The top of a site: the highest point of the finest elevation tiles (~30 m) within SUMMIT_KM
    of where it is given, and its height."""
    lat, lon = site['lat'], site['lon']
    fine = terrain.DEM(lat - .02, lon - .02, lat + .02, lon + .02, zoom=SUMMIT_ZOOM)
    dy, dx = np.mgrid[-SUMMIT_KM:SUMMIT_KM:41j, -SUMMIT_KM:SUMMIT_KM:41j]
    lats, lons = lat + dy / 111.2, lon + dx / (111.2 * math.cos(math.radians(lat)))
    heights = np.where(np.hypot(dx, dy) <= SUMMIT_KM, fine(lats, lons), -np.inf)
    top = np.unravel_index(np.argmax(heights), heights.shape)
    return float(lats[top]), float(lons[top]), float(heights[top])


def reach(site):
    """The summit's viewshed, as build_rangemap.py works it out for the station."""
    terrain.MODEL = str(CACHE)                       # its elevation tiles are kept there
    lat0, lon0, top = summit(site)
    reach_km = site['reach_km']
    span_lat = reach_km / 111 + 0.5
    span_lon = reach_km / (111 * math.cos(math.radians(lat0))) + 0.5
    mosaic = terrain.DEM(round(lat0 - span_lat, 1), round(lon0 - span_lon, 1),
                         round(lat0 + span_lat, 1), round(lon0 + span_lon, 1))

    def dem(lat, lon):
        return np.maximum(mosaic(lat, lon), 0)      # the tiles hold the sea floor: the sea's top it is
    # the coarse mosaic (~0.5 km) shaves a summit down by a few hundred metres: the antenna is
    # raised by what the finest tiles say the summit stands above it
    lift = max(0.0, top - float(dem(np.array([lat0]), np.array([lon0]))[0]))
    az, d, vis, h_obs = terrain.viewshed(dem, lat0, lon0, site['our_agl'] + lift, site['their_agl'], reach_km)
    far = np.where(vis.any(axis=1), d[vis.shape[1] - 1 - np.argmax(vis[:, ::-1], axis=1)], 0)
    i_max = int(np.argmax(far))
    farthest = float(far[i_max])
    flat, flon = terrain.destination(lat0, lon0, az[i_max], farthest)
    peak = float(dem(np.array([flat]), np.array([flon]))[0])
    path = folder(site)
    Image.fromarray(vis).save(path / 'reach.png', optimize=True)           # 1 bit a cell
    summary = dict(lat=round(lat0, 5), lon=round(lon0, 5), reach_km=farthest, bearing=float(az[i_max]),
                   far_lat=float(flat), far_lon=float(flon), far_elev_m=peak, our_asl_m=h_obs, our_agl_m=site['our_agl'], their_agl_m=site['their_agl'],
                   km_per_px=farthest * 1.08 / (H / 2), step_km=float(d[1] - d[0]), bearings=len(az),
                   steps=len(d))
    write(path / 'reach.json', summary)
    print(site['id'], f"reach {farthest:.0f} km at {az[i_max]:.0f} deg, antenna {h_obs:.0f} m ASL,",
          f"{vis.mean():.1%} of cells seen", flush=True)
    return summary


# -------------------------------------------------------------------- map ----

def bounds(site, info):
    """South, west, north, east of the 1x map: its edge, azimuthal equidistant about the summit."""
    km = info['km_per_px']
    t = np.linspace(-1, 1, 41)
    xs = np.r_[t * W / 2, t * W / 2, np.full(41, -W / 2), np.full(41, W / 2)] * km
    ys = np.r_[np.full(41, -H / 2), np.full(41, H / 2), t * H / 2, t * H / 2] * km
    lat, lon = terrain.destination(site['lat'], site['lon'], np.degrees(np.arctan2(xs, ys)), np.hypot(xs, ys))
    return float(lat.min()), float(lon.min()), float(lat.max()), float(lon.max())


NATURAL_EARTH = 'https://raw.githubusercontent.com/nvkelso/natural-earth-vector/master/geojson/{}.geojson'
RIVER_FILES = ('ne_10m_rivers_lake_centerlines', 'ne_10m_rivers_europe', 'ne_10m_rivers_north_america')


def natural_earth(name):
    """A Natural Earth layer (public domain), downloaded once into the cache."""
    path = CACHE / 'ne' / f'{name}.geojson'
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        request = urllib.request.Request(NATURAL_EARTH.format(name), headers=terrain.UA)
        path.write_bytes(urllib.request.urlopen(request, timeout=300).read())
    return json.loads(path.read_text(encoding='utf-8'))['features']


def site_map(site):
    """The 1x map's cities and towns, rivers and civil airports, from Natural Earth: the same
    everywhere the station may stand, and no air bases (an airport typed military is left out)."""
    info = json.loads((folder(site) / 'reach.json').read_text())
    south, west, north, east = bounds(site, info)

    def here(lat, lon):
        return south <= lat <= north and west <= lon <= east

    places, airports, rivers = [], [], []
    capital = {'Admin-0 capital': 'yes', 'Admin-0 capital alt': 'yes', 'Admin-1 capital': '4', 'Admin-1 region capital': '4'}
    for f in natural_earth('ne_10m_populated_places'):
        p, (lon, lat) = f['properties'], f['geometry']['coordinates']
        if here(lat, lon) and (p['FEATURECLA'] in capital or p['FEATURECLA'].lower() == 'populated place'):
            places.append(dict(name=p['NAME_EN'] or p['NAME'], zh=p.get('NAME_ZH') or '', lat=round(lat, 5), lon=round(lon, 5),
                               pop=int(p['POP_MAX'] or 0), capital=capital.get(p['FEATURECLA'], '')))
    for f in natural_earth('ne_10m_airports'):
        p, (lon, lat) = f['properties'], f['geometry']['coordinates']
        if here(lat, lon) and p['iata_code'] and 'military' not in p['type'] and not military(p['name']):
            airports.append(dict(code=p['iata_code'], icao=p['gps_code'] or '', zh=p.get('name_zh') or '',
                                 name=p['name_en'] or p['name'], lat=round(lat, 5), lon=round(lon, 5)))
    for name in RIVER_FILES:
        for f in natural_earth(name):
            g = f['geometry'] or {}
            parts = [g['coordinates']] if g.get('type') == 'LineString' else g.get('coordinates', []) if g.get('type') == 'MultiLineString' else []
            for part in parts:
                run = []
                for lon, lat in part + [[None, None]]:          # runs of points on the map, a point past it each end
                    if lat is not None and here(lat, lon):
                        run.append([lat, lon])
                    elif run:
                        rivers.append([[round(a, 4), round(b, 4)] for a, b in build_basemap.simplify(run, RIVER_TOL_M)])
                        run = []
    found = dict(places=places, airports=airports, rivers=[r for r in rivers if len(r) > 1],
                 attribution='Natural Earth (public domain)')
    write(folder(site) / 'map.json', found)
    print(site['id'], f'{len(places)} places, {len(airports)} airports, {len(found["rivers"])} river lines', flush=True)


# ------------------------------------------------------------- spell, glyphs ----

def spell(site):
    """Each Chinese name's pinyin, a sound a character of its normal form (map_search.spelled)."""
    from pypinyin import lazy_pinyin
    path = folder(site) / 'map.json'
    found = json.loads(path.read_text(encoding='utf-8'))
    for row in found['places'] + found['airports']:
        plain = unicodedata.normalize('NFKC', row['zh']).casefold()
        sounds = lazy_pinyin(plain, errors=list)
        if row['zh'] and len(sounds) == len(plain):
            row['sounds'] = sounds
    write(path, found)


def table(glyphs):
    """{char: [x offset, y offset, advance, width, height, bits in base64]}"""
    return {c: [g.xo, g.yo, g.adv, g.w, g.h, base64.b64encode(g.bits).decode()] for c, g in glyphs.items()}


def glyphs(source):
    """HamClock's four fonts as its 1600x960 frame has them, and the Song for the Han in the data."""
    import dots
    fonts = dict(fast=dots.latin())
    for name, file in (('small', 'Germano-Regular-16.cpp'), ('smallBold', 'Germano-Bold-16.cpp'),
                       ('large', 'Germano-Bold-30.cpp')):
        fonts[name] = dots.latin(Path(source) / file)
    text = ''.join(s.get('zh', '') for s in sites())
    for site in sites():
        found = json.loads((folder(site) / 'map.json').read_text(encoding='utf-8'))
        text += ''.join(row['zh'] for row in found['places'] + found['airports'])
    han = {c: g for c in dict.fromkeys(text + dots.ELLIPSIS) if c not in fonts['fast'] and (g := dots.glyph(c))}
    data = {name: table(font) for name, font in fonts.items()}
    data['song'] = table(han)
    write(OUT / 'glyphs.json', data)
    print('glyphs', {name: len(t) for name, t in data.items()}, flush=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('steps', nargs='*', default=['reach', 'map'], choices=STEPS)
    ap.add_argument('--only', help='one site by its id')
    ap.add_argument('--hamclock-src', help="HamClock's source, for its Germano fonts")
    args = ap.parse_args()
    for site in sites(args.only):
        if 'reach' in args.steps:
            reach(site)
        if 'map' in args.steps:
            site_map(site)
        if 'spell' in args.steps:
            spell(site)
    if 'glyphs' in args.steps:
        if not args.hamclock_src:
            sys.exit('glyphs needs --hamclock-src')
        glyphs(args.hamclock_src)


if __name__ == '__main__':
    main()
