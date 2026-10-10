#!/usr/bin/env python3
"""A/B test of 1090 MHz receive settings through sdrmux's runtime "set" command.

  gain_ab.py run [--hours 24] [--block-min 10]   rotate settings in randomized blocks
  gain_ab.py report                               compare blocks using collector data

Traffic varies hour to hour, so each block is compared with the blocks right next to it
(log ratio of its rate to the mean of its neighbours), which cancels slow traffic trends.
"""
import argparse
import json
import os
import random
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "sdrmux"))
LOG = os.path.expanduser("~/influ/ab/gain_ab.jsonl")
SETTINGS = {
    "m49.6": {"gain": 49.6, "rtl_agc": False},
    "m49.6+dagc": {"gain": 49.6, "rtl_agc": True},
    "tagc+dagc": {"gain": None, "rtl_agc": True},
}


def run(a):
    from sdrmuxctl import call
    os.makedirs(os.path.dirname(LOG), exist_ok=True)
    end = time.time() + a.hours * 3600
    names = list(SETTINGS)
    while time.time() < end:
        random.shuffle(names)
        for name in names:
            r = call(dict(cmd="set", **SETTINGS[name]))
            t0 = time.time()
            time.sleep(a.block_min * 60)
            with open(LOG, "a") as f:
                f.write(json.dumps(dict(name=name, t0=t0, t1=time.time(), set=r)) + "\n")
    # leave the receiver on the setting that was configured at start
    call(dict(cmd="set", **SETTINGS[a.restore]))


def report(a):
    import numpy as np
    import pandas as pd
    from influ import load

    blocks = pd.read_json(LOG, lines=True)
    blocks = blocks[blocks.set.map(lambda s: s.get("ok", False))].reset_index(drop=True)
    msgs, _ = load.beast(blocks.t0.min(), blocks.t1.max())
    ms = msgs[msgs.icao.notna()]
    st = load.stats(blocks.t0.min(), blocks.t1.max())
    rows = []
    for b in blocks.itertuples():
        g = ms[(ms.t >= b.t0 + 5) & (ms.t < b.t1)]
        s = st[(st.t > b.t0 + 60) & (st.t <= b.t1)]
        dur = (b.t1 - b.t0 - 5) / 60
        rows.append(dict(name=b.name, t0=b.t0, msg_per_min=len(g) / dur,
                         df17_per_min=(g.df == 17).sum() / dur, aircraft=g.icao.nunique(),
                         signal=s.signal.mean(), noise=s.noise.mean()))
    df = pd.DataFrame(rows).sort_values("t0").reset_index(drop=True)
    for col in ("msg_per_min", "df17_per_min", "aircraft"):
        v = np.log(df[col] + 1)
        neigh = (v.shift(1) + v.shift(-1)) / 2
        df[col + "_rel"] = v - neigh
    print(f"{len(df)} blocks, {(df.t0.max() - df.t0.min()) / 3600:.1f} h")
    agg = df.groupby("name").agg(blocks=("t0", "size"), msg_per_min=("msg_per_min", "mean"),
                                 df17_per_min=("df17_per_min", "mean"), aircraft=("aircraft", "mean"),
                                 signal=("signal", "mean"), noise=("noise", "mean"),
                                 rel_msgs=("msg_per_min_rel", "mean"),
                                 rel_msgs_se=("msg_per_min_rel", "sem"),
                                 rel_df17=("df17_per_min_rel", "mean"),
                                 rel_aircraft=("aircraft_rel", "mean"))
    print(agg.round(3).to_string())
    print("rel_* = mean log-ratio vs neighbouring blocks (+0.10 ~ 10% more than neighbours)")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["run", "report"])
    ap.add_argument("--hours", type=float, default=24)
    ap.add_argument("--block-min", type=float, default=10)
    ap.add_argument("--restore", default="m49.6")
    a = ap.parse_args()
    run(a) if a.cmd == "run" else report(a)


if __name__ == "__main__":
    main()
