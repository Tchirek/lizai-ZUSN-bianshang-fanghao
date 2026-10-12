#!/usr/bin/env python3
"""Collect government offices under the range map from a map provider's place search, county
by county: Baidu (server AK in ~/influ/private/baidu.env) or Tencent (key and signing secret
in ~/influ/private/tencent.env).

Each county (from the district list tools/amap_names.py keeps) is searched one keyword at a
time: every wanted kind where AMap's place search never reached, the custodial and media kinds
everywhere. A search returns at most 150 (Baidu) or 200 (Tencent) places, so the keywords are
narrow ones. Answers are kept per county in ~/influ/model/<provider>/<code>.json, so a run can
stop and go on; both give GCJ-02 coordinates, which build_tiles.py government converts.

Requests go one at a time and well under each key's rate: a rate refusal waits and asks again,
the day's quota spent stops the run.

usage: place_gov.py baidu|tencent
"""
import argparse
import hashlib
import json
import os
import sys
import time
import urllib.parse
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from amap_names import EXTENT, PROVINCES, inside, searched     # noqa: E402

MODEL = os.path.expanduser("~/influ/model")
DISTRICTS = os.path.join(MODEL, "amap_names", "districts.json")
# by the layer's categories
EVERY = ("人民政府", "街道办事处", "党委", "人大", "政协",
         "公安局", "派出所", "交警", "警务室",
         "法院", "检察院", "司法局", "司法所",
         "应急管理局", "消防",
         "市场监督管理", "城市管理", "综合行政执法", "生态环境局", "税务局", "交通运输执法",
         "卫生健康", "民政局", "教育局", "信访", "疾病预防控制", "政务服务中心")
NEW = ("看守所", "拘留所", "监狱", "戒毒所", "广播电视台", "融媒体中心", "报社")


class Spent(Exception):
    """The key's quota for the day is used up."""


def env(name):
    with open(os.path.expanduser(f"~/influ/private/{name}.env")) as f:
        return dict(line.strip().split("=", 1) for line in f if "=" in line)


class Baidu:
    """Place search v2 by region ("prefecture + county", so that two 鼓楼区 do not mix)."""
    PAGE, DEEPEST, GAP_S = 20, 150, 0.4           # the AK allows 3 at once
    BUSY, SPENT = (401, 402), (4, 302)

    def __init__(self):
        self.ak = env("baidu")["BAIDU_AK"]

    def url(self, word, county, page):
        return "https://api.map.baidu.com/place/v2/search?" + urllib.parse.urlencode(dict(
            query=word, region=county["region"], city_limit="true", page_size=self.PAGE, page_num=page,
            scope=1, ret_coordtype="gcj02ll", output="json", ak=self.ak))

    @staticmethod
    def places(d):
        return d.get("total", 0), [dict(uid=x.get("uid"), name=x["name"], lat=x["location"]["lat"],
                                        lng=x["location"]["lng"], area=x.get("area"), address=x.get("address"))
                                   for x in d.get("results", []) if x.get("location")]


class Tencent:
    """Place search v1 limited to the county by its code (region(code,0)), signed requests."""
    PAGE, DEEPEST, GAP_S = 20, 200, 0.3
    BUSY, SPENT = (120,), (121,)

    def __init__(self):
        e = env("tencent")
        self.key, self.secret = e["TENCENT_KEY"], e["TENCENT_SK"]

    def url(self, word, county, page):
        q = dict(keyword=word, boundary=f"region({county['code']},0)", page_size=self.PAGE,
                 page_index=page + 1, key=self.key)
        path = "/ws/place/v1/search"
        raw = path + "?" + "&".join(f"{k}={q[k]}" for k in sorted(q)) + self.secret
        q["sig"] = hashlib.md5(raw.encode()).hexdigest()
        return "https://apis.map.qq.com" + path + "?" + urllib.parse.urlencode(q)

    @staticmethod
    def places(d):
        return d.get("count", 0), [dict(uid=x["id"], name=x["title"], lat=x["location"]["lat"],
                                        lng=x["location"]["lng"], area=x["ad_info"].get("district"),
                                        address=x.get("address"), category=x.get("category"))
                                   for x in d.get("data", [])]


PROVIDERS = {"baidu": Baidu, "tencent": Tencent}


class Searcher:
    def __init__(self, provider):
        self.p, self.last = provider, 0.0

    def get(self, url):
        for attempt in range(5):
            time.sleep(max(0.0, self.last + self.p.GAP_S - time.time()))
            self.last = time.time()
            try:
                with urllib.request.urlopen(url, timeout=30) as r:
                    d = json.load(r)
            except OSError:
                time.sleep(2 * 3 ** attempt)
                continue
            if d.get("status") not in self.p.BUSY:      # too many at once: wait, ask again
                return d
            time.sleep(5 * (attempt + 1))
        raise OSError("no answer")

    def search(self, word, county):
        """Every place the provider has for `word` in `county`, page by page."""
        out, seen = [], set()
        for page in range(self.p.DEEPEST // self.p.PAGE):
            d = self.get(self.p.url(word, county, page))
            if d.get("status") in self.p.SPENT:
                raise Spent(d.get("message"))
            if d.get("status") != 0:
                raise OSError(f"{county['region']} {word}: {d.get('status')} {d.get('message')}")
            total, found = self.p.places(d)
            new = [x for x in found if x["uid"] not in seen]   # Tencent repeats its last page past the end
            seen |= {x["uid"] for x in new}
            out += new
            if len(found) < self.p.PAGE or not new or len(out) >= total:
                break
        return out


def counties():
    """Every county under the map: its code, region name, centre and the keywords it needs."""
    with open(DISTRICTS) as f:
        trees = json.load(f)
    s, w, n, e = EXTENT
    done = searched()
    out = []
    for p in PROVINCES:
        province = trees[p]
        cities = province["districts"]
        if p == "重庆市":
            cities = [dict(province, districts=[c for city in cities for c in city["districts"]])]
        for city in cities:
            for county in city["districts"]:
                lon, lat = county["center"] or (0, 0)
                if s <= lat <= n and w <= lon <= e:
                    out.append(dict(code=county["adcode"], region=city["name"] + county["name"],
                                    centre=county["center"],
                                    words=NEW if inside(county["center"], done) else EVERY + NEW))
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("provider", choices=sorted(PROVIDERS))
    a = ap.parse_args()
    out = os.path.join(MODEL, a.provider)
    os.makedirs(out, exist_ok=True)
    searcher = Searcher(PROVIDERS[a.provider]())
    todo = counties()
    print(f"{len(todo)} counties", flush=True)
    try:
        for i, county in enumerate(todo, 1):
            path = os.path.join(out, county["code"] + ".json")
            have = json.load(open(path)) if os.path.exists(path) else \
                dict(region=county["region"], centre=county["centre"], found={})
            for word in county["words"]:
                if word in have["found"]:
                    continue
                try:
                    have["found"][word] = searcher.search(word, county)
                except OSError as ex:
                    print(ex, flush=True)
                    continue
                with open(path + ".tmp", "w") as f:
                    json.dump(have, f, ensure_ascii=False)
                os.replace(path + ".tmp", path)
            if i % 25 == 0:
                print(f"{i}/{len(todo)} counties", flush=True)
    except Spent as ex:
        print(f"{a.provider} quota spent, stopped:", ex, flush=True)
        return
    print("done", flush=True)


if __name__ == "__main__":
    main()
