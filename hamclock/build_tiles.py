#!/usr/bin/env python3
"""Make the zoomable range map's ground local and quick to load.

dem     every Terrarium elevation tile (zoom 8..12, ~530 m down to ~33 m) under the range map,
        into ~/influ/model/dem/ beside the ones build_rangemap.py cached: ~14,500 tiles, ~1 GB.
campuses  every higher education campus under the map from OpenStreetMap, live (amenity
        university or college; amenity school or education land named as a university or
        college; less schools named as such: outlines, else a point), fetched in cells,
        nearest first, as busy servers allow, into ~/influ/model/campuses.json.
airports  every aerodrome under the map from OpenStreetMap, live, by its code (ICAO, else
        IATA), and every runway (centre line and width, or outline), the same way, into
        ~/influ/model/airports.json.
admin   province (admin level 4), prefecture (5) and county (6) boundaries under the map from
        OpenStreetMap, live, each boundary way once with the highest level it bounds, the same
        way, into ~/influ/model/admin.json.
military  every military area under the map from OpenStreetMap, live (landuse military, or a
        military base, barracks, airfield, naval base, range or training area): outlines with
        their name if mapped, else a point, the same way, into ~/influ/model/military.json.
government  the wanted government departments under the map (WANTED: party and government
        organs, congress and CPPCC, public security, courts and procuratorates, custodial
        facilities, emergency response and fire, enforcement and regulation bureaus,
        livelihood and public services, broadcasters and news media) from OpenStreetMap, the
        AMap offices tools/amap_gov.py collected and those tools/amap_names.py named and
        geocoded, live, radio regulators (a layer of their own) marked; the regulators OSM lacks come from radio_regulators.json (official
        addresses), into ~/influ/model/government.json.
render  the ground as build_rangemap.py draws it (relief, line-of-sight coverage) as a tile
        pyramid in the map's own projection: level L is the W x H base map at L times its
        scale, cut into 256 px tiles, ~/influ/model/tiles/L/col_row.png. The map window only
        pastes and scales these, so any view comes up sharp at once.

Both steps skip what is already there; rerun render (with --force) after build_rangemap.py,
which redoes the viewshed. Tiles past the map's edge repeat the edge.

usage: build_tiles.py [dem] [campuses] [airports] [admin] [military] [government] [render]
                      [--workers 3] [--force]
"""
import argparse
import io
import json
import math
import os
import re
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
CELLS = os.path.join(a.MODEL, "campus_cells2")
OLD_AIRPORT_CELLS = os.path.join(a.MODEL, "airport_cells")
AIRPORT_CELLS = os.path.join(a.MODEL, "airport_cells2")
# "out geom", not "out tags geom": the latter leaves out a relation's members, and big airports
# (ZUUU) and some campuses are multipolygon relations
AIRPORT_QUERY = """[out:json][timeout:180];
(nwr["aeroway"="aerodrome"]({box});
 way["aeroway"="runway"]({box}););
out geom;"""
MILITARY_CELLS = os.path.join(a.MODEL, "military_cells")
MILITARY_QUERY = """[out:json][timeout:180];
(nwr["landuse"="military"]({box});
 nwr["military"~"^(base|barracks|airfield|naval_base|range|training_area)$"]({box}););
out geom;"""
OLD_GOVERNMENT_CELLS = os.path.join(a.MODEL, "government_cells")
GOVERNMENT_CELLS = os.path.join(a.MODEL, "government_cells2")
# by tag, and by the name ending most offices carry whatever they are tagged as
GOVERNMENT_QUERY = """[out:json][timeout:240];
(nwr["office"="government"]({box});
 nwr["amenity"~"^(townhall|courthouse|police)$"]({box});
 nwr["government"]({box});
 nwr["building"="government"]({box});
 nwr["name"~"(人民政府|政府|管委会|委员会|办事处|局|厅|署|法院|检察院|公安|派出所|税务所|海关|人大|政协|党委|党工委|县委|市委|区委|管理所|管理站)$"]({box});
 nwr["name"~"无线电"]({box}););
out geom;"""
# names that carry an office's word without being one
NOT_GOVERNMENT = re.compile("邮政|邮局|电信|移动|联通|银行|信用社|酒店|宾馆|饭店|餐厅|招待所|书店|超市|药(?!品监督管理)|宿舍|小区|家属|公寓|"
                            "旧址|遗址|纪念|博物|学院|公司|集团|广场|公园|站台|车站|加油|维修|商店|"
                            "专卖|门市|营业|网点|服务部|经营部|服务窗口|停车场|放映|门诊|诊所|殡仪|陵园|出版|支部|党组|居民委员会|村民"
                            "委员会|居委会|村委会|社区委员会|社区党委|社区工作委员会|社区纪律检查|市场(?!监督|监管)|"
                            "共青团|青年团|关心下一代|林长制|河长制|残疾人|工会|离退休|学会|协会|基金会|"
                            "国民党革命委员会|民主同盟|民主建国会|民主促进会|农工民主党|致公党|九三学社|台湾民主自治同盟")
# the departments the layer keeps, by the map's categories: the party and government organs;
# congress and the CPPCC; public security; the courts and procuratorates; the custodial and
# treatment facilities; emergency response and fire; the enforcement and regulatory bureaus;
# the livelihood and public service bureaus; and broadcasters and news media. Anything else
# mapped as an office is left out; the radio regulators (cyan) are a layer of their own.
WANTED = re.compile(
    "党委|党工委|市委|县委|区委|镇委|乡委|州委|省委|盟委|"                        # 党政机关
    "政府|街道办事处|街道办|机关工作委员会|街道工作委员会|新区工作委员会|"
    "开发区工作委员会|园区工作委员会|纪律检查委员会|纪律检查工作委员会|政法委员会|"
    "人大|人民代表大会|政协(?!助)|"                                             # 权力与协商机关
    "公安|派出所|交警|交通警察|警务|车管所|车辆管理所|"                            # 公安机关
    "法院|人民法庭|检察|司法局|司法所|"                                          # 司法与法律监督机关
    "看守所|拘留所|监狱|戒毒|"                                                  # 监管场所
    "应急管理|消防|防汛|抗旱|"                                                  # 应急救援机关
    "市场监管|市场监督管理|药品监督管理|城管(?!理)|城市管理|综合行政执法|生态环境|环境保护局|环保局|交通执法|交通运输.*?执法|税务|"   # 行政执法与监管机关
    "卫生健康|卫生和计划生育|卫生局|民政|教育|信访|疾病预防控制|疾控|政务服务中心|"   # 民生与公共服务机关
    "广播电视局|广播电视台|广播电视总台|广播电台|电视台|融媒体|新闻传媒|报社|日报")   # 宣传与新闻传媒机构
# a 中共 committee counts only for a jurisdiction's own (市委, 县委, 纪委, 机关工委 ...), not
# for the committee or the branch inside some other unit (中共XX局委员会, 中共XX学校支部委员会)
PARTY = re.compile("党委|党工委|市委|县委|区委|镇委|乡委|州委|省委|盟委|"
                   "机关工作委员会|街道工作委员会|新区工作委员会|开发区工作委员会|园区工作委员会|"
                   "纪律检查委员会|纪律检查工作委员会|政法委员会")


def wanted(name):
    """Whether the government layer shows this office. A name's bracketed part is left aside:
    it may be a branch (XX司法局(黄洋司法所)) or only somewhere nearby (XX幼儿园(XX镇人民政府北))."""
    name = re.sub(r"[（(][^）)]*[）)]", "", name)
    if NOT_GOVERNMENT.search(name) or "中共" in name and not PARTY.search(name):
        return False
    return bool(WANTED.search(name))
REGULATORS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "radio_regulators.json")
AMAP_CELLS = os.path.join(a.MODEL, "amap_cells")          # tools/amap_gov.py's passes
AMAP_PASSES = (AMAP_CELLS, os.path.join(a.MODEL, "amap_media"), os.path.join(a.MODEL, "amap_custody"))
AMAP_NAMES = os.path.join(a.MODEL, "amap_names", "geo.jsonl")   # tools/amap_names.py's
PLACES = [os.path.join(a.MODEL, p) for p in ("baidu", "tencent")]   # tools/place_gov.py's
# where an office's name shows from: city level and up as military areas do, then counties
HIGH = re.compile("(省|自治州|市|州|地区)(人民政府|政府|委员会|中级人民法院|人民检察院|公安局)$")
COUNTY = re.compile("(县|区|市|旗)(人民政府|政府|委员会|人民法院|人民检察院|公安局|税务局)$")
HIGH_LEVELS = ("国家级机关及事业单位", "省直辖市级政府及事业单位", "地市级政府及事业单位")
ADMIN_CELLS = os.path.join(a.MODEL, "admin_cells")
# the areas' relations (members only), then their boundary ways in the cell with geometry
ADMIN_QUERY = """[out:json][timeout:180];
rel["boundary"="administrative"]["admin_level"~"^(4|5|6)$"]({box})->.r;
.r out body;
way(r.r)({box});
out geom;"""
RELATION_CELLS = os.path.join(a.MODEL, "campus_relations")
RELATION_QUERY = """[out:json][timeout:180];
(rel["amenity"~"^(university|college)$"]({box});
 rel["amenity"="school"]["name"~"大学|学院|高等专科|高专"]({box});
 rel["landuse"="education"]["name"~"大学|学院|高等专科|高专"]({box}););
out geom;"""
OLD_CELLS = os.path.join(a.MODEL, "campus_cells")     # the first query's, until a cell is redone
CAMPUS_QUERY = """[out:json][timeout:180];
(nwr["amenity"~"^(university|college)$"]({box});
 nwr["amenity"="school"]["name"~"大学|学院|高等专科|高专"]({box});
 wr["landuse"="education"]["name"~"大学|学院|高等专科|高专"]({box}););
out geom;"""
# Higher education by name, for campuses tagged only school or education land (some colleges
# are mapped as amenity=school): a university, college or 高专, maybe with its campus
HIGHER = re.compile(r"(大学|学院|高等专科学校|高专)([（(][^）)]*[）)]|.{0,6}(校区|分院|分校))?$")
# university and college tags also cover schools that are not higher education; so do names
NOT_HIGHER = ("小学", "中学", "幼儿园", "附属", "附中", "实验学校", "党校", "干部", "社会主义学院", "老年",
              "技工学校", "技校", "技师学院", "检察官学院", "进修", "培训", "中专", "中等专业", "职业高中", "职业中学")


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


def fetch_cells(folder, query, write, what, rounds=30):
    """Fetch the cells of `query` not yet kept in `folder` (round after round while the
    servers are busy), calling write() after each new cell."""
    os.makedirs(folder, exist_ok=True)
    for attempt in range(rounds):
        missing = 0
        for s, w, box in campus_cells():
            path = os.path.join(folder, f"{s}_{w}.json")
            if os.path.exists(path):
                continue
            try:
                found = overpass(query.replace("{box}", box))
            except OSError:
                missing += 1
                continue
            with open(path + ".tmp", "w") as f:
                json.dump(found, f)
            os.replace(path + ".tmp", path)
            write()
        print(f"{what}: round {attempt + 1}, {missing} cells to go", flush=True)
        if not missing:
            return True
        time.sleep(min(600, 60 * (attempt + 1)))
    return False


def campuses(rounds=30):
    """The campus cells, then their relations again in full (cells fetched before the query
    kept members have relations without them)."""
    return (fetch_cells(CELLS, CAMPUS_QUERY, write_campuses, "campuses", rounds)
            and fetch_cells(RELATION_CELLS, RELATION_QUERY, write_campuses, "campus relations", rounds))


def airports(rounds=30):
    return fetch_cells(AIRPORT_CELLS, AIRPORT_QUERY, write_airports, "airports", rounds)


def military(rounds=30):
    return fetch_cells(MILITARY_CELLS, MILITARY_QUERY, write_military, "military", rounds)


def write_military():
    """military.json: each military area's outline (multipolygons joined into rings) and name
    (English if given, else as mapped; often none) at its middle, else a point; areas mapped
    inside a larger one dropped as parts of it."""
    out = []
    for e in kept(MILITARY_CELLS).values():
        tags = e.get("tags", {})
        name = tags.get("name:en") or tags.get("name") or ""
        if e["type"] == "node":
            out.append(dict(name=name, rings=[], point=[e["lat"], e["lon"]]))
        elif found := outer_rings(e):
            out.append(dict(name=name, rings=[[list(p) for p in r] for r in found],
                            point=middle(max(found, key=lambda r: abs(area(r))))))
    inner = nested(out)
    out = [c for i, c in enumerate(out) if i not in inner]
    with open(os.path.join(a.MODEL, "military.json.tmp"), "w") as f:
        json.dump(out, f, ensure_ascii=False)
    os.replace(os.path.join(a.MODEL, "military.json.tmp"), os.path.join(a.MODEL, "military.json"))
    print(f"military: {sum(bool(c['rings']) for c in out)} outlined, {sum(not c['rings'] for c in out)} points, "
          f"{sum(bool(c['name']) for c in out)} named", flush=True)


def government(rounds=30):
    ok = fetch_cells(GOVERNMENT_CELLS, GOVERNMENT_QUERY, write_government, "government", rounds)
    write_government()                       # AMap's may have come in since
    return ok


def name_zoom(name, level=""):
    """The zoom an office's name shows from: 6 (as military areas) for radio regulators and
    city level and up, 8 for counties, 12 for townships and everything else."""
    if "无线电" in name or level in HIGH_LEVELS or HIGH.search(name):
        return 6
    if level == "区县级政府及事业单位" or COUNTY.search(name):
        return 8
    return 12


def gcj_offset(lat, lon):
    """GCJ-02 minus WGS-84 at a WGS-84 point, degrees: the published transform."""
    x, y = lon - 105.0, lat - 35.0
    dlat = (-100 + 2 * x + 3 * y + 0.2 * y * y + 0.1 * x * y + 0.2 * math.sqrt(abs(x))
            + (20 * math.sin(6 * x * math.pi) + 20 * math.sin(2 * x * math.pi)) * 2 / 3
            + (20 * math.sin(y * math.pi) + 40 * math.sin(y / 3 * math.pi)) * 2 / 3
            + (160 * math.sin(y / 12 * math.pi) + 320 * math.sin(y * math.pi / 30)) * 2 / 3)
    dlon = (300 + x + 2 * y + 0.1 * x * x + 0.1 * x * y + 0.1 * math.sqrt(abs(x))
            + (20 * math.sin(6 * x * math.pi) + 20 * math.sin(2 * x * math.pi)) * 2 / 3
            + (20 * math.sin(x * math.pi) + 40 * math.sin(x / 3 * math.pi)) * 2 / 3
            + (150 * math.sin(x / 12 * math.pi) + 300 * math.sin(x / 30 * math.pi)) * 2 / 3)
    a_, ee = 6378245.0, 0.00669342162296594323
    rad = math.radians(lat)
    magic = 1 - ee * math.sin(rad) ** 2
    dlat = dlat * 180 / (a_ * (1 - ee) / (magic * math.sqrt(magic)) * math.pi)
    dlon = dlon * 180 / (a_ / math.sqrt(magic) * math.cos(rad) * math.pi)
    return dlat, dlon


def wgs84(lat, lon):
    """A GCJ-02 point (as AMap gives) back in WGS-84, to well under a metre."""
    wlat, wlon = lat, lon
    for _ in range(4):
        dlat, dlon = gcj_offset(wlat, wlon)
        wlat, wlon = lat - dlat, lon - dlon
    return wlat, wlon


def amap_offices():
    """AMap's offices (every pass of tools/amap_gov.py), each once, in WGS-84."""
    out, seen = [], set()
    files = [os.path.join(folder, name) for folder in AMAP_PASSES if os.path.isdir(folder)
             for name in sorted(os.listdir(folder)) if name.endswith(".json")]
    for path in files:
        with open(path) as f:
            pois = json.load(f).get("pois", [])
        for p in pois:
            if p["id"] in seen or not p.get("name"):
                continue
            seen.add(p["id"])
            radio = "无线电" in p["name"]
            if not radio and not wanted(p["name"]):
                continue
            lat, lon = wgs84(p["lat"], p["lng"])
            out.append(dict(name=p["name"], rings=[], point=[lat, lon], radio=radio,
                            zoom=name_zoom(p["name"], p.get("type", "").split(";")[-1])))
    return out


def geocoded_offices():
    """The offices tools/amap_names.py named and AMap's Geocoder placed: only answers that are
    a point of interest whose address ends with the very name asked for (a county's police
    asked for and the city's found does not count), within 80 km of the place it was asked for,
    each named as AMap names it, once, in WGS-84; named from the zoom its level deserves (a
    prefecture's offices as city level, a county's as county level)."""
    out, seen = [], set()
    if not os.path.exists(AMAP_NAMES):
        return out
    levels = {}
    with open(os.path.join(os.path.dirname(AMAP_NAMES), "districts.json"), encoding="utf-8") as f:
        stack = list(json.load(f).values())
    while stack:
        d = stack.pop()
        levels.setdefault(d["name"], d["level"])
        stack += d["districts"]
    tier = {"province": 8, "city": 6, "district": 8}
    with open(AMAP_NAMES, encoding="utf-8") as f:
        for line in f:
            try:
                r = json.loads(line)
            except ValueError:
                continue
            q, (lon0, lat0) = plain(r["job"]["q"]), r["job"]["centre"]
            for g in r["found"]:                          # the first candidate that is the office
                address = plain(g["address"])
                if g["level"] != "兴趣点" or not address.endswith(q) or not g["location"]:
                    continue
                name = address                                # less the province, city, ... it starts with
                for part in g.get("parts") or []:
                    if part and name.startswith(part) and name[len(part):].endswith(q):
                        name = name[len(part):]
                if "无线电" not in name and not wanted(name):
                    continue
                lat, lon = wgs84(g["location"][1], g["location"][0])
                if math.dist((lat, lon), (lat0, lon0)) > 0.8 or (name, round(lat, 4), round(lon, 4)) in seen:
                    continue
                seen.add((name, round(lat, 4), round(lon, 4)))
                zoom = min(name_zoom(name), tier.get(levels.get(r["job"]["unit"]), 12))
                out.append(dict(name=name, rings=[], point=[lat, lon], radio="无线电" in name, zoom=zoom))
                break
    return out


def place_offices():
    """The offices tools/place_gov.py found county by county, each once, in WGS-84."""
    out, seen = [], set()
    for path in [os.path.join(d, n) for d in PLACES if os.path.isdir(d) for n in sorted(os.listdir(d))]:
        if not path.endswith(".json"):
            continue
        with open(path, encoding="utf-8") as f:
            county = json.load(f)
        for places in county["found"].values():
            for p in places:
                if p["uid"] in seen:
                    continue
                seen.add(p["uid"])
                radio = "无线电" in p["name"]
                if not radio and not wanted(p["name"]):
                    continue
                lat, lon = wgs84(p["lat"], p["lng"])
                out.append(dict(name=p["name"], rings=[], point=[lat, lon], radio=radio, zoom=name_zoom(p["name"])))
    return out


def plain(name):
    return re.sub(r"[（(][^）)]*[）)]|\s", "", name)


def write_government():
    """government.json: only the departments WANTED names (the yellow areas and points, by
    the map's categories), outlined or a point, each with its name; radio regulators (无线电
    in the name) are flagged and kept whatever the list says, and those OSM lacks added from
    radio_regulators.json unless one is mapped within ~2 km."""
    out = []
    for e in kept(OLD_GOVERNMENT_CELLS, GOVERNMENT_CELLS).values():
        tags = e.get("tags", {})
        name = tags.get("name:zh") or tags.get("name") or tags.get("name:en") or ""
        radio = "无线电" in name
        # the wanted departments, less the bus stop, bus route or shop that merely carries one's name
        if not radio and (not wanted(name) or tags.get("highway") or tags.get("railway") or tags.get("route")
                          or tags.get("public_transport") or tags.get("shop")):
            continue
        if e["type"] == "node":
            out.append(dict(name=name, rings=[], point=[e["lat"], e["lon"]], radio=radio, zoom=name_zoom(name)))
        elif found := outer_rings(e):
            out.append(dict(name=name, rings=[[list(p) for p in r] for r in found],
                            point=middle(max(found, key=lambda r: abs(area(r)))), radio=radio, zoom=name_zoom(name)))
    inner = nested(out)                      # an office inside a compound is part of it ...
    out = [c for i, c in enumerate(out) if i not in inner or c["radio"]]     # ... unless a regulator
    # AMap's offices, less those OSM has under the same name within ~400 m
    mapped = {}
    for c in out:
        mapped.setdefault(plain(c["name"]), []).append(c["point"])
    amap = []
    for c in amap_offices() + geocoded_offices() + place_offices():
        if not any(math.dist(c["point"], p) < 0.004 for p in mapped.get(plain(c["name"]), ())):
            amap.append(c)
            mapped.setdefault(plain(c["name"]), []).append(c["point"])
    out += amap
    with open(REGULATORS, encoding="utf-8") as f:     # those found nowhere else, placed by hand
        for r in json.load(f):
            if not any(c["radio"] and math.dist(c["point"], r["point"]) < 0.02 for c in out):
                out.append(dict(name=r["name"], rings=[], point=r["point"], radio=True, zoom=6))
    with open(os.path.join(a.MODEL, "government.json.tmp"), "w") as f:
        json.dump(out, f, ensure_ascii=False)
    os.replace(os.path.join(a.MODEL, "government.json.tmp"), os.path.join(a.MODEL, "government.json"))
    print(f"government: {sum(bool(c['rings']) for c in out)} outlined, {sum(not c['rings'] for c in out)} points "
          f"({len(amap)} from AMap, Baidu and Tencent), {sum(c['radio'] for c in out)} radio regulators", flush=True)


def admin(rounds=30):
    return fetch_cells(ADMIN_CELLS, ADMIN_QUERY, write_admin, "admin boundaries", rounds)


def write_admin():
    """admin.json: every boundary way of a province, prefecture or county under the map once,
    as a lat/lon line with the lowest admin level among the areas it bounds (a province's
    border is also its prefectures' and counties')."""
    found = kept(ADMIN_CELLS)
    level = {}
    for e in found.values():
        if e["type"] == "relation":
            rank = re.match(r"\d+", e.get("tags", {}).get("admin_level", ""))
            for m in e.get("members", []) if rank else ():
                if m["type"] == "way":
                    level[m["ref"]] = min(level.get(m["ref"], 9), int(rank.group()))
    lines = [dict(level=level[e["id"]], p=[[g["lat"], g["lon"]] for g in e["geometry"] if g])
             for e in found.values() if e["type"] == "way" and e["id"] in level and len(e.get("geometry", [])) > 1]
    with open(os.path.join(a.MODEL, "admin.json.tmp"), "w") as f:
        json.dump(dict(lines=lines), f)
    os.replace(os.path.join(a.MODEL, "admin.json.tmp"), os.path.join(a.MODEL, "admin.json"))
    print(f"admin: {sum(x['level'] == 4 for x in lines)} province, {sum(x['level'] == 5 for x in lines)} prefecture, "
          f"{sum(x['level'] == 6 for x in lines)} county boundary ways, {sum(len(x['p']) for x in lines)} points", flush=True)


def kept(*folders):
    """OSM elements of the kept cells by (type, id); a cell in a later folder (a newer query
    for the same thing) replaces that cell in an earlier one."""
    files = {}
    for folder in folders:
        if os.path.isdir(folder):
            files.update({name: os.path.join(folder, name) for name in os.listdir(folder) if name.endswith(".json")})
    found = {}
    for path in files.values():
        with open(path) as f:
            found.update({(e["type"], e["id"]): e for e in json.load(f)})
    return found


def outer_rings(e):
    """The closed rings of a way or multipolygon's outer ways (lat, lon)."""
    if e["type"] == "way":
        ways = [[(g["lat"], g["lon"]) for g in e.get("geometry", []) if g]]
    else:
        ways = [[(g["lat"], g["lon"]) for g in m.get("geometry", []) if g]
                for m in e.get("members", []) if m.get("type") == "way" and m.get("role", "outer") in ("outer", "")]
    return [r for r in rings(ways) if len(r) > 2]


def write_airports():
    """airports.json: each aerodrome's code (ICAO, else IATA; empty for a field with neither)
    and name (English if given) at its middle; every runway as its centre line (or outline if mapped as an area)
    with its width in metres (45 when untold)."""
    airports, runways = {}, []
    for e in kept(OLD_AIRPORT_CELLS, AIRPORT_CELLS).values():
        tags = e.get("tags", {})
        if tags.get("aeroway") == "aerodrome":
            icao, iata = tags.get("icao", "").strip().upper(), tags.get("iata", "").strip().upper()
            code = icao if re.fullmatch("[A-Z]{4}", icao) else iata if re.fullmatch("[A-Z]{3}", iata) else ""
            if e["type"] == "node":
                point, size = [e["lat"], e["lon"]], 0
            elif found := outer_rings(e):
                ring = max(found, key=lambda r: abs(area(r)))
                point, size = middle(ring), abs(area(ring))
            else:
                continue
            # one per code: OSM has some old fields under the code of their successor too
            key = code or (e["type"], e["id"])
            if key not in airports or size > airports[key]["size"]:
                airports[key] = dict(code=code, name=tags.get("name:en") or tags.get("name", ""), point=point, size=size)
        elif tags.get("aeroway") == "runway" and e["type"] == "way":
            line = [[g["lat"], g["lon"]] for g in e.get("geometry", []) if g]
            width = re.match(r"\s*([0-9.]+)", tags.get("width", ""))
            if len(line) > 1:
                runways.append(dict(ref=tags.get("ref", ""), line=line, width=float(width.group(1)) if width else 45.0,
                                    area=line[0] == line[-1] and tags.get("area") == "yes"))
    with open(os.path.join(a.MODEL, "airports.json.tmp"), "w") as f:
        airports = [dict(code=x["code"], name=x["name"], point=x["point"]) for x in airports.values()]
        json.dump(dict(airports=airports, runways=runways), f, ensure_ascii=False)
    os.replace(os.path.join(a.MODEL, "airports.json.tmp"), os.path.join(a.MODEL, "airports.json"))
    print(f"airports: {len(airports)} ({sum(bool(x['code']) for x in airports)} with a code), {len(runways)} runways",
          flush=True)


def area(ring):
    """Signed shoelace area of a lat/lon ring (square degrees)."""
    y, x = np.array(ring, dtype=float).T
    return float((x * np.roll(y, -1) - np.roll(x, -1) * y).sum() / 2)


def middle(ring):
    """Area centroid of a lat/lon ring, for its name (the vertex mean if it has no area)."""
    y, x = np.array(ring, dtype=float).T
    cross = x * np.roll(y, -1) - np.roll(x, -1) * y
    area = cross.sum() / 2
    if abs(area) < 1e-12:
        return [float(y.mean()), float(x.mean())]
    return [float(((y + np.roll(y, -1)) * cross).sum() / (6 * area)),
            float(((x + np.roll(x, -1)) * cross).sum() / (6 * area))]


def inside_ring(point, ring):
    """Ray casting: is the lat/lon point inside the ring?"""
    y, x = np.array(ring, dtype=float).T
    py, px = point
    crosses = ((y > py) != (np.roll(y, -1) > py)) &         (px < (np.roll(x, -1) - x) * (py - y) / np.where(np.roll(y, -1) == y, 1e-12, np.roll(y, -1) - y) + x)
    return bool(crosses.sum() % 2)


def nested(campuses):
    """Indices of campuses whose middle lies within a larger campus's outline: a school or
    department mapped inside a campus is part of it."""
    outlines = [(i, np.asarray(c["rings"][0]), abs(area(c["rings"][0])))
                for i, c in enumerate(campuses) if c["rings"]]
    if not outlines:
        return set()
    indices = np.array([i for i, _, _ in outlines])
    sizes = np.array([s for _, _, s in outlines])
    bounds = np.array([(*r.min(0), *r.max(0)) for _, r, _ in outlines])
    own_size = np.zeros(len(campuses))
    own_size[indices] = sizes
    found = set()
    for i, c in enumerate(campuses):
        (lat, lon) = c["point"]
        possible = (indices != i) & (sizes > own_size[i]) & (bounds[:, 0] <= lat) & \
                   (bounds[:, 1] <= lon) & (lat <= bounds[:, 2]) & (lon <= bounds[:, 3])
        for j in np.flatnonzero(possible):
            if inside_ring(c["point"], outlines[j][1]):
                found.add(i)
                break
    return found


def write_campuses():
    """campuses.json from the kept cells (each from the latest query that has it): outlines
    (multipolygon ways joined into rings) with their middle for the name, else a point."""
    out = []
    found = kept(OLD_CELLS, CELLS)
    found.update(kept(RELATION_CELLS))                   # relations again, now with their members
    for e in found.values():
        tags = e.get("tags", {})
        name = tags.get("name:zh") or tags.get("name") or tags.get("name:en") or ""
        if any(word in name for word in NOT_HIGHER):
            continue
        if tags.get("amenity") not in ("university", "college") and not HIGHER.search(name):
            continue
        if e["type"] == "node":
            out.append(dict(name=name, rings=[], point=[e["lat"], e["lon"]]))
        elif found := outer_rings(e):
            out.append(dict(name=name, rings=[[list(p) for p in r] for r in found],
                            point=middle(max(found, key=lambda r: abs(area(r))))))
    inner = nested(out)
    out = [c for i, c in enumerate(out) if i not in inner]
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
    ap.add_argument("steps", nargs="*",
                    default=["dem", "campuses", "airports", "admin", "military", "government", "render"])
    ap.add_argument("--workers", type=int, default=3)
    ap.add_argument("--force", action="store_true")
    args = ap.parse_args()
    if "dem" in args.steps and not dem():
        sys.exit("some elevation tiles failed; run again")
    if "campuses" in args.steps and not campuses():
        print("some campus cells are still missing; run campuses again later", flush=True)
    if "airports" in args.steps and not airports():
        print("some airport cells are still missing; run airports again later", flush=True)
    if "admin" in args.steps and not admin():
        print("some admin boundary cells are still missing; run admin again later", flush=True)
    if "military" in args.steps and not military():
        print("some military cells are still missing; run military again later", flush=True)
    if "government" in args.steps and not government():
        print("some government cells are still missing; run government again later", flush=True)
    if "render" in args.steps:
        render(args.workers, args.force)


if __name__ == "__main__":
    main()
