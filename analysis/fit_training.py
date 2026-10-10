#!/usr/bin/env python3
"""Refit unidentified/light-aircraft activity bins from the existing collector history."""
import json
import os
import time
from bisect import bisect_right

from influ import load, traffic


def records(stream, since, gaps=()):
    starts, ends = [], []
    for a, b in gaps:
        starts.append(a)
        ends.append(max(b, ends[-1] if ends else b))
    for path in load._files(stream, since):
        for line in load._lines(path):
            try:
                rec = json.loads(line)
                t = rec.get("t", rec.get("now", 0))
                i = bisect_right(starts, t) - 1
                if t >= since and not (i >= 0 and t < ends[i]):
                    yield rec
            except ValueError:
                continue


def main():
    now = time.time()
    model = traffic.fit(records("aircraft", now - 30 * 86400, load.switch_windows()),
                        records("cloud", now - 30 * 86400))
    model["fitted_at"] = now
    path = os.path.expanduser("~/influ/model/training.json")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path + ".tmp", "w") as f:
        json.dump(model, f)
    os.replace(path + ".tmp", path)
    print(json.dumps({k: v for k, v in model.items() if k not in
                      ("known_ident", "light_no_ident", "untyped_no_ident")}))
    print("identified:", len(model["known_ident"]), "unidentified light:",
          len(model["light_no_ident"]), "unidentified untyped:", len(model["untyped_no_ident"]))


if __name__ == "__main__":
    main()
