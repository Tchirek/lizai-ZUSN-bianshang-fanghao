#!/usr/bin/env python3
"""Make the zoomable range map's ground local and quick to load.

dem     every Terrarium elevation tile (zoom 8..12, ~530 m down to ~33 m) under the range map,
        into ~/influ/model/dem/ beside the ones build_rangemap.py cached: ~14,500 tiles, ~1 GB.
campuses  every higher education campus under the map from OpenStreetMap (amenity university
        or college, less schools named as such: outlines, else a point), fetched in cells,
        nearest first, as busy servers allow, into ~/influ/model/campuses.json.
render  the ground as build_rangemap.py draws it (relief, line-of-sight coverage) as a tile
        pyramid in the map's own projection: level L is the W x H base map at L times its
        scale, cut into 256 px tiles, ~/influ/model/tiles/L/col_row.png. The map window only
        pastes and scales these, so any view comes up sharp at once.

Both steps skip what is already there; rerun render (with --force) after build_rangemap.py,
which redoes the viewshed. Tiles past the map's edge repeat the edge.

usage: build_tiles.py [dem] [campuses] [render] [--workers 3] [--force]
"""
import argparse
import io
import json
import math
import os
import sys
import time
import urllib.parse
import urllib.request
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor

import numpy as np
from PIL import Image
from scipy.ndimage import map_coordinates

import activity as a
import build_rangemap as terrain

W, H = a.W, a.H
LEVELS = (1, 2, 4, 8, 16, 24)          # base scale multiples; 24 at ~37 m a pixel
TILE = 256
BLOCK = 6                              # tiles a side rendered together (one elevation mosaic)
TILES = os.path.join(a.MODEL, "tiles")
DEM_ZOOMS = range(terrain.DEM_ZOOM, 13)


def extent():
    """The map's lat/lon bounds, from points along its edge."""
    m = a.Map()
    edge = [m.latlon(x, y) for x in range(0, W + 1, 10) for y in (0, H)]
    edge += [m.latlon(x, y) for y in range(0, H + 1, 10) for x in (0, W)]
    return (min(p[0] for p in edge), min(p[1] for p in edge),
            max(p[0] for p in edge), max(p[1] for p in edge))


def dem_tiles():
    south, west, north, east = extent()
    for z in DEM_ZOOMS:
        x0, y0 = terrain.merc_px(north, west, z)
        x1, y1 = terrain.merc_px(south, east, z)
        for ty in range(int(y0 // TILE), int(y1 // TILE) + 1):
            for tx in range(int(x0 // TILE), int(x1 // TILE) + 1):
                yield z, tx, ty


def fetch(key):
    """Download one elevation tile unless it is here; True if it is here now."""
    z, x, y = key
    path = os.path.join(a.MODEL, "dem", f"{z}_{x}_{y}.png")
    if os.path.exists(path):
        return True
    for attempt in range(4):
        try:
            data = urllib.request.urlopen(urllib.request.Request(
                terrain.DEM_URL.format(z=z, x=x, y=y), headers=terrain.UA), timeout=30).read()
            Image.open(io.BytesIO(data)).verify()
            tmp = f"{path}.{os.getpid()}.tmp"
            with open(tmp, "wb") as f:
                f.write(data)
            os.replace(tmp, path)
            return True
        except (OSError, SyntaxError):
            time.sleep(2 * (attempt + 1))
    return False


def dem():
    os.makedirs(os.path.join(a.MODEL, "dem"), exist_ok=True)
    keys = list(dem_tiles())
    missing = 0
    with ThreadPoolExecutor(max_workers=16) as pool:
        for i, ok in enumerate(pool.map(fetch, keys), 1):
            missing += not ok
            if i % 500 == 0 or i == len(keys):
                print(f"dem {i}/{len(keys)} tiles, {missing} failed", flush=True)
    return missing == 0


def rings(ways):
    """Join a multipolygon's way geometries into rings by their shared end points."""
    ways, out = [list(map(tuple, w)) for w in ways if len(w) > 1], []
    while ways:
        ring = ways.pop()
        while ring[0] != ring[-1]:
            for i, w in enumerate(ways):
                if w[0] == ring[-1]:
                    ring += w[1:]
                elif w[-1] == ring[-1]:
                    ring += w[-2::-1]
                elif w[-1] == ring[0]:
                    ring = w + ring[1:]
                elif w[0] == ring[0]:
                    ring = w[::-1] + ring[1:]
                else:
                    continue
                ways.pop(i)
                break
            else:
                break                                    # left open: drawn as it is
        out.append(ring)
    return out


MIRRORS = terrain.OVERPASS + ["https://maps.mail.ru/osm/tools/overpass/api/interpreter",
                              "https://overpass.openstreetmap.fr/api/interpreter"]
CELLS = os.path.join(a.MODEL, "campus_cells")
# amenity=college also tags schools that are not higher education; their names give them away
NOT_HIGHER = ("小学", "中学", "幼儿园", "党校", "社会主义学院", "技工学校", "技校", "进修", "培训", "中专",
              "中等专业", "职业高中", "职业中学")


def overpass(query):
    """Overpass JSON elements from the first mirror that answers."""
    for url in MIRRORS:
        try:
            req = urllib.request.Request(url, data=urllib.parse.urlencode({"data": query}).encode(),
                                         headers=terrain.UA)
            return json.load(urllib.request.urlopen(req, timeout=200))["elements"]
        except (OSError, ValueError) as e:
            print(url, "failed:", e, flush=True)
    raise OSError("all Overpass mirrors failed")


def campus_cells():
    """The map's extent in 2 x 2 degree cells, nearest us first: one query for all of it
    times out on busy servers."""
    south, west, north, east = extent()
    m = a.Map()
    cells = [(s, w) for s in range(math.floor(south), math.ceil(north), 2)
             for w in range(math.floor(west), math.ceil(east), 2)]
    for s, w in sorted(cells, key=lambda c: math.hypot(c[0] + 1 - m.lat0, c[1] + 1 - m.lon0)):
        yield s, w, f"{max(s, south):.3f},{max(w, west):.3f},{min(s + 2, north):.3f},{min(w + 2, east):.3f}"


def campuses(rounds=30):
    """Fetch the cells not yet kept (round after round while the servers are busy), writing
    campuses.json from what is kept after each new cell."""
    os.makedirs(CELLS, exist_ok=True)
    for attempt in range(rounds):
        missing = 0
        for s, w, box in campus_cells():
            path = os.path.join(CELLS, f"{s}_{w}.json")
            if os.path.exists(path):
                continue
            try:
                found = overpass(f"""[out:json][timeout:180];
nwr["amenity"~"^(university|college)$"]({box});
out tags geom;""")
            except OSError:
                missing += 1
                continue
            with open(path + ".tmp", "w") as f:
                json.dump(found, f)
            os.replace(path + ".tmp", path)
            write_campuses()
        print(f"campuses: round {attempt + 1}, {missing} cells to go", flush=True)
        if not missing:
            return True
        time.sleep(min(600, 60 * (attempt + 1)))
    return False


def write_campuses():
    """campuses.json from the kept cells: outlines (multipolygon ways joined into rings) with
    a middle for the name, else a point."""
    found = {}
    for name in os.listdir(CELLS):
        if name.endswith(".json"):
            with open(os.path.join(CELLS, name)) as f:
                found.update({(e["type"], e["id"]): e for e in json.load(f)})
    out = []
    for e in found.values():
        tags = e.get("tags", {})
        name = tags.get("name:zh") or tags.get("name") or tags.get("name:en") or ""
        if any(word in name for word in NOT_HIGHER):
            continue
        if e["type"] == "node":
            out.append(dict(name=name, rings=[], point=[e["lat"], e["lon"]]))
            continue
        if e["type"] == "way":
            ways = [[(g["lat"], g["lon"]) for g in e.get("geometry", []) if g]]
        else:
            ways = [[(g["lat"], g["lon"]) for g in m.get("geometry", []) if g]
                    for m in e.get("members", []) if m.get("type") == "way" and m.get("role", "outer") in ("outer", "")]
        found_rings = [r for r in rings(ways) if len(r) > 2]
        if found_rings:
            ring = max(found_rings, key=len)
            point = [sum(p[0] for p in ring) / len(ring), sum(p[1] for p in ring) / len(ring)]
            out.append(dict(name=name, rings=[[list(p) for p in r] for r in found_rings], point=point))
    with open(os.path.join(a.MODEL, "campuses.json.tmp"), "w") as f:
        json.dump(out, f, ensure_ascii=False)
    os.replace(os.path.join(a.MODEL, "campuses.json.tmp"), os.path.join(a.MODEL, "campuses.json"))
    print(f"campuses: {sum(bool(c['rings']) for c in out)} outlined, {sum(not c['rings'] for c in out)} points",
          flush=True)


# --------------------------------------------------------------- elevations ----

_heights = {}


def elevations(lat, lon, zoom=12):
    """Terrain height (m) at points, bilinear within each Terrarium tile at `zoom`; reads only
    the tiles the points fall in (kept for later)."""
    lat, lon = np.atleast_1d(lat), np.atleast_1d(lon)
    x, y = terrain.merc_px(lat, lon, zoom)
    tx, ty = (x // TILE).astype(int), (y // TILE).astype(int)
    out = np.empty(len(x))
    for key in set(zip(tx.tolist(), ty.tolist())):
        if (zoom, *key) not in _heights:
            if len(_heights) > 300:
                _heights.pop(next(iter(_heights)))
            _heights[zoom, *key] = terrain.DEM.tile(*key, zoom)
        here = (tx == key[0]) & (ty == key[1])
        out[here] = map_coordinates(_heights[zoom, *key], [y[here] - key[1] * TILE, x[here] - key[0] * TILE],
                                    order=1, mode="nearest")
    return out


# ------------------------------------------------------------------- render ----

def dem_zoom(scale, km_per_px, lat):
    """The coarsest elevation zoom with pixels no larger than the map's at `scale` (8..12)."""
    ratio = 156543.03 * math.cos(math.radians(lat)) / (km_per_px / scale * 1000)
    return min(12, max(terrain.DEM_ZOOM, math.ceil(math.log2(ratio))))


def dem_metres(scale, km_per_px, lat):
    return 156543.03 * math.cos(math.radians(lat)) / 2 ** dem_zoom(scale, km_per_px, lat)


def render_ground(m, scale, x0, y0, x1, y1, widen=False):
    """The ground as build_rangemap.py draws it -- relief tinted where our antenna sees, the
    seen area outlined -- for pixels [x0, x1) x [y0, y1) of the map at `scale` pixels a base
    px, as an RGB array. Past the map's edge the edge repeats. `widen` grows far visible
    patches by a pixel, as build_rangemap.py does on the whole map."""
    # one pixel more each side: the hillshade's slopes and the outline need neighbours
    y, x = np.mgrid[y0 - 1:y1 + 1, x0 - 1:x1 + 1].astype(np.float64)
    bx = np.clip((x + 0.5) / scale, 0, W)            # pixel centres in base px
    by = np.clip((y + 0.5) / scale, 0, H)
    dx, dy = (bx - W / 2) * m.km_per_px, (by - H / 2) * m.km_per_px
    km, bearing = np.hypot(dx, dy), np.degrees(np.arctan2(dx, -dy)) % 360
    lat, lon = terrain.destination(m.lat0, m.lon0, bearing, km)
    grid = terrain.DEM(float(lat.min() - .01), float(lon.min() - .01), float(lat.max() + .01),
                       float(lon.max() + .01), zoom=dem_zoom(scale, m.km_per_px, m.lat0))
    rgb = terrain.relief(grid(lat, lon), m.km_per_px / scale)
    with np.load(os.path.join(a.MODEL, "range_visibility.npz")) as saved:
        az, d, visible = saved["az"], saved["distance"], saved["visible"]
    ia = np.round(bearing / (360 / len(az))).astype(int) % len(az)
    idd = np.clip(np.round(km / (d[1] - d[0])).astype(int) - 1, 0, len(d) - 1)
    seen = visible[ia, idd] & (km <= d[-1])
    if widen:
        grown = seen | np.roll(seen, 1, 0) | np.roll(seen, -1, 0) | np.roll(seen, 1, 1) | np.roll(seen, -1, 1)
        seen = np.where(km > 40, grown, seen)
    inner = np.roll(seen, 1, 0) & np.roll(seen, -1, 0) & np.roll(seen, 1, 1) & np.roll(seen, -1, 1)
    rgb[seen] = rgb[seen] * 0.45 + np.array(terrain.SEEN) * 0.55
    rgb[seen & ~inner] = terrain.SEEN
    return np.clip(rgb[1:-1, 1:-1], 0, 255).astype(np.uint8)


def render_block(level, bc, br, force=False):
    """Render the BLOCK x BLOCK tiles from column bc, row br of a level; tiles written."""
    cols, rows = math.ceil(W * level / TILE), math.ceil(H * level / TILE)
    todo = [(c, r) for r in range(br, min(br + BLOCK, rows)) for c in range(bc, min(bc + BLOCK, cols))
            if force or not os.path.exists(os.path.join(TILES, str(level), f"{c}_{r}.png"))]
    if not todo:
        return 0
    picture = Image.fromarray(render_ground(a.Map(), level, bc * TILE, br * TILE, min(bc + BLOCK, cols) * TILE,
                                            min(br + BLOCK, rows) * TILE, widen=level == 1))
    os.makedirs(os.path.join(TILES, str(level)), exist_ok=True)
    for c, r in todo:
        path = os.path.join(TILES, str(level), f"{c}_{r}.png")
        left, top = (c - bc) * TILE, (r - br) * TILE
        picture.crop((left, top, left + TILE, top + TILE)).save(path + ".tmp.png", compress_level=3)
        os.replace(path + ".tmp.png", path)
    return len(todo)


def render(workers, force):
    jobs = [(level, bc, br) for level in LEVELS
            for br in range(0, math.ceil(H * level / TILE), BLOCK)
            for bc in range(0, math.ceil(W * level / TILE), BLOCK)]
    done, started = 0, time.time()
    with ProcessPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(render_block, *job, force) for job in jobs]
        for i, f in enumerate(futures, 1):
            done += f.result()
            if i % 20 == 0 or i == len(futures):
                print(f"render {i}/{len(futures)} blocks, {done} tiles, {time.time() - started:.0f} s", flush=True)
    with open(os.path.join(TILES, "levels.json"), "w") as f:
        json.dump(dict(levels=LEVELS, tile=TILE, w=W, h=H,
                       km_per_px=json.load(open(os.path.join(a.MODEL, "rangemap.json")))["km_per_px"]), f)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("steps", nargs="*", default=["dem", "campuses", "render"])
    ap.add_argument("--workers", type=int, default=3)
    ap.add_argument("--force", action="store_true")
    args = ap.parse_args()
    if "dem" in args.steps and not dem():
        sys.exit("some elevation tiles failed; run again")
    if "campuses" in args.steps and not campuses():
        print("some campus cells are still missing; run campuses again later", flush=True)
    if "render" in args.steps:
        render(args.workers, args.force)


if __name__ == "__main__":
    main()
