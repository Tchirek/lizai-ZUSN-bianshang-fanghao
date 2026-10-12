#!/usr/bin/env python3
"""Fill in the government layer where AMap's place search did not reach, and the kinds it does
not carry, from the AMap services the Web (JS API) key still has.

  districts  DistrictSearch lists every prefecture, county and township under the map, each
             with its code and centre.
  names      For each of them the offices of the wanted kinds are named as such offices are
             named (XX县公安局, 遂宁市公安局船山区分局, XX镇人民政府, XX派出所, XX县看守所,
             XX县融媒体中心 ...): every kind for the places place search never reached, the
             custodial and media kinds it does not carry everywhere. Prisons come by name from
             the provinces' lists (PRISONS).
  geocode    The Geocoder places each name, one request at a time; every answer is kept in
             ~/influ/model/amap_names/geo.jsonl, so a run can stop and go on.

build_tiles.py government takes an answer only if it is a point of interest whose own name ends
with the name asked for; one resolved to a district or a street, or to some other office (a
county's police asked for, the city's found), is dropped.

The page carries the key, so the server listens on this host only; open it there in a browser
(headless Firefox will do) or through an SSH tunnel.

usage: amap_names.py [--port 8075]
"""
import argparse
import json
import os
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

OUT = os.path.expanduser("~/influ/model/amap_names")
CELLS = os.path.expanduser("~/influ/model/amap_cells")       # amap_gov.py's searched areas
ENV = os.path.expanduser("~/influ/private/amap.env")
EXTENT = (27.75, 99.27, 33.21, 111.98)                       # the range map's, south west north east
PROVINCES = ("四川省", "重庆市", "贵州省", "陕西省", "甘肃省", "湖北省", "湖南省", "云南省")

# the offices each kind of place has, as they are named; {p} the prefecture, {c} the county,
# {t} the township, {s} the township without 镇/乡/街道. Categories as the layer's.
CITY = ("{p}人民政府", "中共{p}委员会", "{p}人大常委会", "政协{p}委员会",
        "{p}公安局", "{p}公安局交通警察支队", "{p}中级人民法院", "{p}人民检察院", "{p}司法局",
        "{p}应急管理局", "{p}消防救援支队",
        "{p}市场监督管理局", "{p}生态环境局", "{p}综合行政执法局", "{p}城市管理行政执法局",
        "{p}交通运输综合行政执法支队", "国家税务总局{p}税务局",
        "{p}卫生健康委员会", "{p}民政局", "{p}教育局", "{p}信访局", "{p}疾病预防控制中心", "{p}政务服务中心")
COUNTY = ("{c}人民政府", "中共{c}委员会", "{c}人大常委会", "政协{c}委员会",
          "{c}公安局", "{p}公安局{c}分局", "{c}公安局交通警察大队", "{c}人民法院", "{c}人民检察院", "{c}司法局",
          "{c}应急管理局", "{c}消防救援大队",
          "{c}市场监督管理局", "{p}生态环境局{c}分局", "{c}综合行政执法局", "{c}交通运输综合行政执法大队",
          "国家税务总局{p}{c}税务局", "国家税务总局{c}税务局",
          "{c}卫生健康局", "{c}民政局", "{c}教育局", "{c}教育和体育局", "{c}信访局", "{c}疾病预防控制中心",
          "{c}政务服务中心")
TOWNSHIP = ("{t}人民政府", "{t}办事处", "{s}派出所", "{t}派出所", "{s}司法所", "{t}司法所")
# the kinds place search does not carry: wanted everywhere
CITY_EXTRA = ("{p}看守所", "{p}拘留所", "{p}强制隔离戒毒所", "{p}广播电视台", "{p}融媒体中心", "{p}日报社")
COUNTY_EXTRA = ("{c}看守所", "{c}拘留所", "{c}融媒体中心", "{c}广播电视台")
PRISONS = {      # the provinces' prison administrations' own lists (Sichuan 2024, Chongqing)
    "四川省": ("阿坝", "巴中", "成都病犯", "成都女子", "崇州", "川北", "川东", "川西", "川中", "达州", "德阳", "甘孜",
            "广元", "汉王山", "嘉陵", "嘉州", "金堂", "锦江", "雷马屏", "凉山", "眉州", "绵阳", "攀西", "荞窝",
            "邛崃", "女子", "雅安", "宜宾", "邑州", "自贡", "内江", "大英", "新源", "成都"),
    "重庆市": ("渝西", "渝都", "渝州", "永川", "垫江", "凤城", "南川", "三合", "三峡", "九龙", "女子", "长康",
            "武陵", "大坪", "涪陵"),
}

PAGE_HTML = """<!doctype html><html><meta charset="utf-8"><title>AMap names</title>
<style>body{font:14px system-ui;margin:24px}#log{white-space:pre;font:12px monospace}</style>
<p id="state">loading AMap…</p><div id="log"></div>
<script>window._AMapSecurityConfig = {securityJsCode: "@SECRET@"};</script>
<script src="https://webapi.amap.com/maps?v=2.0&key=@KEY@&plugin=AMap.DistrictSearch,AMap.Geocoder"></script>
<script>
const say = t => { document.getElementById('state').textContent = t; };
const log = t => { const l = document.getElementById('log'); l.textContent = (t + '\\n' + l.textContent).slice(0, 4000); };
const wait = ms => new Promise(r => setTimeout(r, ms));
// AMap sometimes never calls back: a request with no answer in time counts as failed
const within = (ms, p) => Promise.race([p, wait(ms).then(() => ({ok: false, error: 'no answer'}))]);
const ll = p => p && [p.lng, p.lat];
const tree = d => ({name: d.name, adcode: d.adcode, level: d.level, center: ll(d.center),
                    districts: (d.districtList || []).map(tree)});
function district(name) {
  return new Promise(resolve => new AMap.DistrictSearch({level: 'province', subdistrict: 3, extensions: 'base'})
    .search(name, (s, r) => resolve(s === 'complete' ? {ok: true, tree: tree(r.districtList[0])}
                                                     : {ok: false, error: (r && (r.info || r.type)) || s})));
}
function geocode(q, city) {
  return new Promise(resolve => new AMap.Geocoder({city: city}).getLocation(q, (s, r) => {
    if (s === 'complete') resolve({ok: true, found: r.geocodes.map(g => ({address: g.formattedAddress, level: g.level,
      location: ll(g.location), parts: g.addressComponent && [g.addressComponent.province, g.addressComponent.city,
      g.addressComponent.district, g.addressComponent.township]}))});
    else if (s === 'no_data') resolve({ok: true, found: []});
    else resolve({ok: false, error: (r && (r.info || r.type)) || s});
  }));
}
async function run() {
  let tries = 0, failing = 0;                         // on this job; jobs failed in a row
  while (true) {
    const job = await (await fetch('/next')).json();
    if (!job.kind) { say('done: ' + job.done); return; }
    say(`${job.done} done, ${job.left} to go: ${job.q}`);
    let r = await within(15000, job.kind === 'district' ? district(job.q) : geocode(job.q, job.city));
    if (!r.ok) {
      log(job.q + ': ' + r.error + ', try ' + (tries + 1));
      if (++tries < 3) { await wait(2000 * 3 ** tries); continue; }
      // one name AMap cannot take is passed over; many in a row means the key is spent
      if (++failing > 5 || job.kind === 'district') { say('AMap: ' + r.error + ' (stopped; reload to go on)'); return; }
      r = {ok: true, found: [], error: r.error};
    } else {
      failing = 0;
    }
    tries = 0;
    await fetch('/result', {method: 'POST', headers: {'Content-Type': 'application/json'},
                            body: JSON.stringify(Object.assign({job}, r))});
    await wait(350);
  }
}
AMap.plugin(['AMap.DistrictSearch', 'AMap.Geocoder'], run);
</script></html>"""


def key():
    env = {}
    with open(ENV) as f:
        for line in f:
            if "=" in line:
                k, v = line.strip().split("=", 1)
                env[k] = v
    return env["AMAP_KEY"], env["AMAP_SECRET"]


def searched():
    """The areas amap_gov.py finished: (south, west, size) each."""
    out = []
    if os.path.isdir(CELLS):
        for name in os.listdir(CELLS):
            if name.endswith(".json"):
                with open(os.path.join(CELLS, name)) as f:
                    if json.load(f).get("split"):
                        continue
                out.append(tuple(float(x) for x in name[:-5].split("_")))
    return out


def inside(point, areas):
    lon, lat = point
    return any(s <= lat < s + size and w <= lon < w + size for s, w, size in areas)


def short(township):
    return re.sub("(街道|镇|乡)$", "", township)


def names(trees):
    """The (name, city code, centre, unit) jobs for every place under the map."""
    s, w, n, e = EXTENT
    done = searched()
    jobs = []

    def on_map(d):
        return d["center"] and s <= d["center"][1] <= n and w <= d["center"][0] <= e

    for province in trees:
        cities = province["districts"]
        if province["name"] in ("重庆市", "北京市", "天津市", "上海市"):     # a municipality is its own prefecture
            cities = [dict(province, districts=[c for city in cities for c in city["districts"]])]
        for city in cities:
            p = city["name"]
            if on_map(city):
                full = not inside(city["center"], done)
                for t in (CITY + CITY_EXTRA if full else CITY_EXTRA):
                    jobs.append((t.format(p=p), city["adcode"], city["center"], p))
            for county in city["districts"]:
                if not on_map(county):
                    continue
                c = county["name"]
                full = not inside(county["center"], done)
                for t in (COUNTY + COUNTY_EXTRA if full else COUNTY_EXTRA):
                    jobs.append((t.format(p=p, c=c), county["adcode"], county["center"], c))
                for town in county["districts"]:
                    if not re.search("(镇|乡|街道)$", town["name"]):
                        continue                         # 管理办公室, farms ...: not named this way
                    if on_map(town) and not inside(town["center"], done):
                        for t in TOWNSHIP:
                            if ("{t}办事处" in t) != town["name"].endswith("街道"):
                                continue                     # a 街道 has an office, a 镇 or 乡 a government
                            if "{s}" in t and short(town["name"]) == town["name"]:
                                continue
                            jobs.append((t.format(t=town["name"], s=short(town["name"])), county["adcode"],
                                         town["center"], town["name"]))
        if province["name"] in PRISONS:
            for prison in PRISONS[province["name"]]:
                jobs.append((f"{province['name']}{prison}监狱", province["adcode"], province["center"], province["name"]))
            if province["name"] == "四川省":
                jobs.append(("四川省未成年犯管教所", province["adcode"], province["center"], province["name"]))
            else:
                jobs.append(("重庆市未成年犯管教所", province["adcode"], province["center"], province["name"]))
    seen, out = set(), []
    for job in jobs:
        if (job[0], job[1]) not in seen:
            seen.add((job[0], job[1]))
            out.append(job)
    return out


class Work:
    """The district lookups, then the names to geocode, each done once."""

    def __init__(self):
        os.makedirs(OUT, exist_ok=True)
        self.lock = threading.Lock()
        self.trees, self.answered = {}, set()
        path = os.path.join(OUT, "districts.json")
        if os.path.exists(path):
            with open(path) as f:
                self.trees = json.load(f)
        path = os.path.join(OUT, "geo.jsonl")
        if os.path.exists(path):
            with open(path) as f:
                for line in f:
                    try:
                        job = json.loads(line)["job"]
                        self.answered.add((job["q"], job["city"]))
                    except (ValueError, KeyError):
                        continue
        self.jobs = None

    def next(self):
        with self.lock:
            todo = [p for p in PROVINCES if p not in self.trees]
            if todo:
                return dict(kind="district", q=todo[0], done=len(self.trees), left=len(todo))
            if self.jobs is None:
                self.jobs = [j for j in names([self.trees[p] for p in PROVINCES])
                             if (j[0], j[1]) not in self.answered]
                print(f"{len(self.jobs)} names to geocode, {len(self.answered)} done before", flush=True)
            if not self.jobs:
                return dict(kind=None, done=len(self.answered))
            q, city, centre, unit = self.jobs[0]
            return dict(kind="geocode", q=q, city=city, centre=centre, unit=unit,
                        done=len(self.answered), left=len(self.jobs))

    def result(self, r):
        with self.lock:
            job = r["job"]
            if job["kind"] == "district":
                self.trees[job["q"]] = r["tree"]
                with open(os.path.join(OUT, "districts.json.tmp"), "w") as f:
                    json.dump(self.trees, f, ensure_ascii=False)
                os.replace(os.path.join(OUT, "districts.json.tmp"), os.path.join(OUT, "districts.json"))
                return
            with open(os.path.join(OUT, "geo.jsonl"), "a") as f:
                f.write(json.dumps(dict(job=job, found=r["found"], error=r.get("error")), ensure_ascii=False) + "\n")
            self.answered.add((job["q"], job["city"]))
            if self.jobs and (self.jobs[0][0], self.jobs[0][1]) == (job["q"], job["city"]):
                self.jobs.pop(0)


class Handler(BaseHTTPRequestHandler):
    def reply(self, body, kind="application/json; charset=utf-8"):
        body = body.encode() if isinstance(body, str) else body
        self.send_response(200)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/":
            k, secret = key()
            self.reply(PAGE_HTML.replace("@KEY@", k).replace("@SECRET@", secret), "text/html; charset=utf-8")
        elif self.path == "/next":
            self.reply(json.dumps(self.server.work.next(), ensure_ascii=False))
        else:
            self.send_error(404)

    def do_POST(self):
        if self.path != "/result":
            self.send_error(404)
            return
        self.server.work.result(json.loads(self.rfile.read(int(self.headers.get("Content-Length", "0")))))
        self.reply("{}")

    def log_message(self, *args):
        pass


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--port", type=int, default=8075)
    a = ap.parse_args()
    server = ThreadingHTTPServer(("127.0.0.1", a.port), Handler)
    server.work = Work()
    print(f"{len(server.work.trees)} provinces listed, {len(server.work.answered)} names geocoded; "
          f"open http://127.0.0.1:{a.port}/ on this host", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
