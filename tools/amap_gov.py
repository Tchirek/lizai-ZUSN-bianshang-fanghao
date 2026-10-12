#!/usr/bin/env python3
"""Collect government offices under the range map from AMap (高德), with a Web (JS API) key.

The key works in web pages only, so the searching happens in a browser: this serves a page
that loads AMap's JS API with the key from ~/influ/private/amap.env (AMAP_KEY, AMAP_SECRET =
its security code), asks this server for one area at a time, searches it by POI type
(PlaceSearch.searchInBounds) page by page and posts the results back. An area whose results
run past what AMap will page through is split in four and searched again. Done areas are kept
in ~/influ/model/amap_cells/, so a run can stop and go on; build_tiles.py government merges
them (AMap gives GCJ-02 coordinates, converted there).

Types searched: government offices of every level down to townships (130101-130105, 130107),
democratic parties (130300), police, procuratorates, courts and the like (130500), traffic and
vehicle administration (130600), market regulation and tax (130700). Village and community
committees (130106) are left out: they are not government offices, and outnumber them.

The page carries the key, so the server listens on this host only: reach it through an SSH
tunnel (ssh -L 8074:127.0.0.1:8074 <station>) and open http://localhost:8074/ there.

Passes, each with its own folder of areas: offices (by the types above, the default), media
(broadcasters and news media, type 141100) and custody (看守所, 拘留所, 监狱, 戒毒所 by keyword,
for AMap files them under no type of their own).

usage: amap_gov.py [--pass offices|media|custody] [--port 8074] [--cell 0.5]
"""
import argparse
import json
import math
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.expanduser("~/influ/model/amap_cells")
ENV = os.path.expanduser("~/influ/private/amap.env")
TYPES = "130101|130102|130103|130104|130105|130107|130300|130500|130600|130700"
KEYWORD = ""
PASSES = {"offices": (OUT, TYPES, ""),
          "media": (os.path.expanduser("~/influ/model/amap_media"), "141100", ""),
          "custody": (os.path.expanduser("~/influ/model/amap_custody"), "", "看守所|拘留所|监狱|戒毒所")}
PAGE = 50                     # PlaceSearch's largest page
DEEPEST = 200                 # dense searches sometimes return an empty fifth page; split sooner
EXTENT = (27.75, 99.27, 33.21, 111.98)                    # the range map's, south west north east

PAGE_HTML = """<!doctype html><html><meta charset="utf-8"><title>AMap government offices</title>
<style>body{font:14px system-ui;margin:24px}#log{white-space:pre;font:12px monospace}</style>
<p id="state">loading AMap…</p><div id="log"></div>
<script>window._AMapSecurityConfig = {securityJsCode: "@SECRET@"};</script>
<script src="https://webapi.amap.com/maps?v=2.0&key=@KEY@&plugin=AMap.PlaceSearch"></script>
<script>
const TYPES = "@TYPES@", KEYWORD = "@KEYWORD@", PAGE = @PAGE@;
const say = t => { document.getElementById('state').textContent = t; };
const log = t => { const l = document.getElementById('log'); l.textContent = (t + '\\n' + l.textContent).slice(0, 4000); };
const wait = ms => new Promise(r => setTimeout(r, ms));
function search(b, page) {
  return new Promise(resolve => {
    const ps = new AMap.PlaceSearch({type: TYPES, pageSize: PAGE, pageIndex: page, extensions: 'base'});
    ps.searchInBounds(KEYWORD, new AMap.Bounds([b[1], b[0]], [b[3], b[2]]), (status, r) => {
      if (status === 'complete') resolve({ok: true, count: r.poiList.count, pois: r.poiList.pois.map(p => (
        {id: p.id, name: p.name, type: p.type, address: p.address, lng: p.location.lng, lat: p.location.lat}))});
      else if (status === 'no_data') resolve({ok: true, count: 0, pois: []});
      else resolve({ok: false, error: (r && (r.info || r.message || r.type)) || String(r)});
    });
  });
}
async function run() {
  let tries = 0;
  while (true) {
    const job = await (await fetch('/next')).json();
    if (!job.cell) { say('done: ' + job.done + ' areas'); return; }
    say(`${job.done} areas done, ${job.left} to go; searching ${job.cell} page ${job.page}`);
    const r = await search(job.bounds, job.page);
    if (!r.ok) {                                      // a dropped request, or the key's limits
      log(job.cell + ' p' + job.page + ': ' + r.error + ', try ' + (tries + 1));
      if (++tries > 6) { say('AMap: ' + r.error + ' (stopped; reload to go on)'); return; }
      await wait(Math.min(120000, 2000 * 3 ** tries));
      continue;
    }
    tries = 0;
    await fetch('/result', {method: 'POST', headers: {'Content-Type': 'application/json'},
                            body: JSON.stringify({cell: job.cell, page: job.page, count: r.count, pois: r.pois})});
    log(`${job.cell} p${job.page}: ${r.pois.length} of ${r.count}`);
    await wait(350);                                  // well under the key's rate limit
  }
}
AMap.plugin('AMap.PlaceSearch', run);
</script></html>"""


def key():
    env = {}
    with open(ENV) as f:
        for line in f:
            if "=" in line:
                k, v = line.strip().split("=", 1)
                env[k] = v
    return env["AMAP_KEY"], env["AMAP_SECRET"]


class Areas:
    """The areas to search: cells named south_west_size, split into four while too full."""

    def __init__(self, size):
        self.lock = threading.Lock()
        os.makedirs(OUT, exist_ok=True)
        s, w, n, e = EXTENT
        self.todo = [(round(s + i * size, 4), round(w + j * size, 4), size)
                     for i in range(math.ceil((n - s) / size)) for j in range(math.ceil((e - w) / size))]
        self.pages = {}           # cell -> pages still to fetch, after its first
        self.found = {}           # cell -> pois so far
        self.counts = {}          # cell -> how many AMap says there are
        self.done = 0
        expanded = []
        for cell in self.todo:
            expanded += self.resolve(cell)
        self.todo = expanded

    @staticmethod
    def name(cell):
        return "%.4f_%.4f_%.4f" % cell

    def resolve(self, cell):
        """A cell as the areas still to search: itself, its quarters if it was split, or none."""
        path = os.path.join(OUT, self.name(cell) + ".json")
        if not os.path.exists(path):
            return [cell]
        with open(path) as f:
            found = json.load(f)
        if found.get("split"):
            out = []
            for q in self.quarters(cell):
                out += self.resolve(q)
            return out
        if cell[2] > 0.002 and (found.get("count") is None or
                                found["count"] > DEEPEST and len(found.get("pois", [])) < found["count"]):
            self.split(cell, found.get("pois", []), found.get("count"))
            return [q for quarter in self.quarters(cell) for q in self.resolve(quarter)]
        self.done += 1
        return []

    def split(self, cell, pois, count):
        """Keep already fetched POIs while finer cells replace a truncated search."""
        path = os.path.join(OUT, self.name(cell) + ".json")
        with open(path + ".tmp", "w") as f:
            json.dump(dict(split=True, count=count, pois=pois), f, ensure_ascii=False)
        os.replace(path + ".tmp", path)

    @staticmethod
    def quarters(cell):
        s, w, size = cell
        h = round(size / 2, 6)
        return [(round(s + a * h, 6), round(w + b * h, 6), h) for a in (0, 1) for b in (0, 1)]

    def next(self):
        with self.lock:
            for cell in self.todo:
                pages = self.pages.get(cell)
                if pages is None or pages:
                    page = 1 if pages is None else pages[0]
                    s, w, size = cell
                    return dict(cell=self.name(cell), bounds=[s, w, s + size, w + size], page=page,
                                done=self.done, left=len(self.todo))
            return dict(cell=None, done=self.done)

    def result(self, r):
        with self.lock:
            cell = next((c for c in self.todo if self.name(c) == r["cell"]), None)
            if cell is None:
                return
            path = os.path.join(OUT, self.name(cell) + ".json")
            if r["page"] == 1:
                if r["count"] > DEEPEST and cell[2] > 0.002:
                    self.split(cell, r["pois"], r["count"])
                    i = self.todo.index(cell)
                    self.todo[i:i + 1] = self.quarters(cell)
                    return
                self.pages[cell] = list(range(2, math.ceil(min(r["count"], DEEPEST) / PAGE) + 1))
                self.found[cell], self.counts[cell] = [], r["count"]
            elif self.pages.get(cell) and self.pages[cell][0] == r["page"]:
                if not r["pois"] and self.counts[cell] > len(self.found[cell]) and cell[2] > 0.002:
                    self.split(cell, self.found.pop(cell), self.counts.pop(cell))
                    i = self.todo.index(cell)
                    self.todo[i:i + 1] = self.quarters(cell)
                    self.pages.pop(cell)
                    return
                self.pages[cell].pop(0)
            self.found[cell] += r["pois"]
            if not self.pages[cell]:
                if len(self.found[cell]) < self.counts[cell] and cell[2] > 0.002:
                    self.split(cell, self.found.pop(cell), self.counts.pop(cell))
                    i = self.todo.index(cell)
                    self.todo[i:i + 1] = self.quarters(cell)
                    self.pages.pop(cell)
                    return
                with open(path + ".tmp", "w") as f:
                    json.dump(dict(count=self.counts.pop(cell), pois=self.found.pop(cell)), f, ensure_ascii=False)
                os.replace(path + ".tmp", path)
                self.todo.remove(cell)
                self.pages.pop(cell)
                self.done += 1


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
            self.reply(PAGE_HTML.replace("@KEY@", k).replace("@SECRET@", secret).replace("@TYPES@", TYPES).replace("@KEYWORD@", KEYWORD)
                       .replace("@PAGE@", str(PAGE)), "text/html; charset=utf-8")
        elif self.path == "/next":
            self.reply(json.dumps(self.server.areas.next()))
        else:
            self.send_error(404)

    def do_POST(self):
        if self.path != "/result":
            self.send_error(404)
            return
        length = int(self.headers.get("Content-Length", "0"))
        self.server.areas.result(json.loads(self.rfile.read(length)))
        self.reply("{}")

    def log_message(self, *args):
        pass


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--port", type=int, default=8074)
    ap.add_argument("--cell", type=float, default=0.5, help="starting area size, degrees")
    ap.add_argument("--pass", dest="what", choices=sorted(PASSES), default="offices")
    a = ap.parse_args()
    global OUT, TYPES, KEYWORD
    OUT, TYPES, KEYWORD = PASSES[a.what]
    server = ThreadingHTTPServer(("127.0.0.1", a.port), Handler)
    server.areas = Areas(a.cell)
    print(f"{server.areas.done} areas done, {len(server.areas.todo)} to search on 127.0.0.1:{a.port}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
