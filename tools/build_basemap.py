#!/usr/bin/env python3
"""Fetch a vector base map around the station from OpenStreetMap (Overpass) for the ADS-B
scope: rivers, lakes/reservoirs, expressways, railways, county boundaries, runways, airports and
towns, out to the scope's largest range (100 mi, 161 km). Simplified, more coarsely past NEAR_KM,
where the scope shows a pixel for a kilometre or more, and cached as ~/influ/model/basemap.json.
Map data (c) OpenStreetMap contributors, ODbL.

usage: build_basemap.py [--radius-km 165] [--out PATH]
"""
import argparse
import json
import math
import os
import urllib.parse
import urllib.request

OUT = os.path.expanduser("~/influ/model/basemap.json")
NEAR_KM = 60                   # within this the 10-30 mi ranges show it: full detail
FAR_TOL_M = 400                # past it, simplified to this
OVERPASS = ["https://overpass-api.de/api/interpreter", "https://overpass.kumi.systems/api/interpreter",
            "https://overpass.private.coffee/api/interpreter"]


def simplify(pts, tol_m):
    """Douglas-Peucker on lat/lon, tolerance in metres (local equirectangular)."""
    if len(pts) < 3:
        return pts
    lat0 = math.radians(pts[0][0])
    xy = [(p[1] * 111320 * math.cos(lat0), p[0] * 110540) for p in pts]

    def rec(i, j, keep):
        (x1, y1), (x2, y2) = xy[i], xy[j]
        dx, dy = x2 - x1, y2 - y1
        n = math.hypot(dx, dy) or 1e-9
        best, k = -1, None
        for m in range(i + 1, j):
            d = abs(dy * xy[m][0] - dx * xy[m][1] + x2 * y1 - y2 * x1) / n
            if d > best:
                best, k = d, m
        if best > tol_m:
            rec(i, k, keep)
            keep.append(k)
            rec(k, j, keep)

    keep = [0]
    rec(0, len(pts) - 1, keep)
    keep.append(len(pts) - 1)
    return [pts[i] for i in sorted(set(keep))]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--radius-km", type=float, default=165)
    ap.add_argument("--out", default=OUT)
    a = ap.parse_args()
    st = json.load(open(os.path.expanduser("~/influ/station.json")))
    # query a coarse box (2 decimals) so the request does not carry the exact station position
    lat, lon = round(st["lat"], 2), round(st["lon"], 2)
    dlat = a.radius_km / 110.5
    dlon = a.radius_km / (111.3 * math.cos(math.radians(lat)))
    bbox = f"{lat - dlat:.3f},{lon - dlon:.3f},{lat + dlat:.3f},{lon + dlon:.3f}"
    q = f"""[out:json][timeout:300];
(
  way["waterway"="river"]({bbox});
  way["natural"="water"]["water"~"^(lake|reservoir|river)$"]({bbox});
  way["highway"~"^(motorway|trunk)$"]({bbox});
  way["railway"="rail"]({bbox});
  way["aeroway"="runway"]({bbox});
  nwr["aeroway"="aerodrome"]({bbox});
  node["place"~"^(city|town)$"]({bbox});
  relation["boundary"="administrative"]["admin_level"="6"]({bbox});
);
out tags geom;"""
    els = None
    for url in OVERPASS:                       # public mirrors; the main one is often busy
        req = urllib.request.Request(url, data=urllib.parse.urlencode({"data": q}).encode(),
                                     headers={"User-Agent": "influ-adsb-scope/1.0 (personal station)"})
        try:
            els = json.load(urllib.request.urlopen(req, timeout=360))["elements"]
            print("from", url)
            break
        except (OSError, ValueError) as e:
            print(url, "failed:", e)
    if els is None:
        raise SystemExit("all Overpass mirrors failed")

    lines, places, airports = [], [], []
    tol = {"river": 60, "water": 60, "motorway": 60, "trunk": 80, "rail": 60, "boundary": 120,
           "runway": 5}

    def near(p):
        return math.hypot(p[0] - lat, (p[1] - lon) * math.cos(math.radians(lat))) * 110.5 < NEAR_KM

    def add(kind, geom):
        pts = [(g["lat"], g["lon"]) for g in geom if g]
        pts = simplify(pts, tol[kind] if any(map(near, pts)) else max(tol[kind], FAR_TOL_M))
        if len(pts) >= 2:
            lines.append({"k": kind, "p": [[round(p[0], 5), round(p[1], 5)] for p in pts]})

    for e in els:
        t = e.get("tags", {})
        name = t.get("name:en") or t.get("name:zh-Latn-pinyin") or t.get("name") or ""
        if e["type"] == "way" and "geometry" in e:
            if t.get("waterway") == "river":
                add("river", e["geometry"])
            elif t.get("natural") == "water":
                add("water", e["geometry"])
            elif t.get("highway") in ("motorway", "trunk"):
                add(t["highway"], e["geometry"])
            elif t.get("railway") == "rail":
                add("rail", e["geometry"])
            elif t.get("aeroway") == "runway":
                add("runway", e["geometry"])
        if e["type"] == "relation" and t.get("boundary") == "administrative":
            for m in e.get("members", []):
                if m.get("type") == "way" and m.get("geometry"):
                    add("boundary", m["geometry"])
        if t.get("aeroway") == "aerodrome":
            c = e.get("center") or ({"lat": e["lat"], "lon": e["lon"]} if "lat" in e else None)
            if c is None and e.get("bounds"):
                b = e["bounds"]
                c = {"lat": (b["minlat"] + b["maxlat"]) / 2, "lon": (b["minlon"] + b["maxlon"]) / 2}
            if c:
                airports.append({"name": t.get("icao") or name, "lat": c["lat"], "lon": c["lon"]})
        if e["type"] == "node" and t.get("place") in ("city", "town"):
            places.append({"name": name, "zh": t.get("name", ""), "kind": t["place"],
                           "lat": e["lat"], "lon": e["lon"]})
    # boundary ways are shared by neighbouring counties: drop duplicates
    seen, uniq = set(), []
    for ln in lines:
        key = (ln["k"], tuple(map(tuple, ln["p"][:2])), tuple(map(tuple, ln["p"][-2:])))
        if key not in seen:
            seen.add(key)
            uniq.append(ln)
    os.makedirs(os.path.dirname(a.out), exist_ok=True)
    with open(a.out + ".part", "w") as f:
        json.dump({"bbox": bbox, "lines": uniq, "places": places, "airports": airports,
                   "attribution": "(c) OpenStreetMap contributors"}, f)
    os.replace(a.out + ".part", a.out)
    from collections import Counter
    print("lines:", dict(Counter(l["k"] for l in uniq)), "points:", sum(len(l["p"]) for l in uniq))
    print("airports:", [(x["name"]) for x in airports])
    print("places:", [(p["name"], p["zh"], p["kind"]) for p in places][:30])


if __name__ == "__main__":
    main()
