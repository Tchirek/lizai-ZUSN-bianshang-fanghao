#!/usr/bin/env python3
"""A/B test of 1090 MHz receive settings through sdrmux's runtime "set" command.

  gain_ab.py run [--hours 24] [--block-min 10] [--arms a,b]   rotate settings in randomized blocks
  gain_ab.py report [--draws 20000]                              the diagnosis (influ/abtest.py)

Each cycle of a run gives every arm one block, in a fresh random order; the report compares
arms within cycles and tests each difference by redoing that randomization. It prints the
diagnosis and keeps it in ~/influ/ab/diagnosis.json.
"""
import argparse
import json
import math
import os
import random
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "sdrmux"))
LOG = os.path.expanduser("~/influ/ab/gain_ab.jsonl")
OUT = os.path.expanduser("~/influ/ab/diagnosis.json")
FT8_LOG = os.path.expanduser("~/influ/ft8/auto.jsonl")
HAZARD = os.path.expanduser("~/influ/model/hazard.json")
SETTINGS = {
    "m49.6": {"gain": 49.6, "rtl_agc": False},
    "m49.6+dagc": {"gain": 49.6, "rtl_agc": True},
    "tagc+dagc": {"gain": None, "rtl_agc": True},
}
LABELS = dict(modes="Mode S/min", adsb="ADS-B/min", positions="positions/min", modeac="Mode A/C/min",
              aircraft="aircraft", tracked="tracked", false="false frames/h", phantoms="phantoms")


def run(a):
    from sdrmuxctl import call
    os.makedirs(os.path.dirname(LOG), exist_ok=True)
    end = time.time() + a.hours * 3600
    names = a.arms.split(",") if a.arms else list(SETTINGS)
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
    import pandas as pd
    from influ import abtest

    d = abtest.diagnose(LOG, FT8_LOG, HAZARD, draws=a.draws)
    pct = lambda x: f"{100 * (math.exp(x) - 1):+.1f}%"
    when = lambda t: pd.Timestamp(t, unit="s", tz="Asia/Shanghai").strftime("%m-%d %H:%M")
    pd.set_option("display.width", 180)

    print("== runs (each cycle holds every arm of its run once, in random order)")
    for r in d["runs"].itertuples():
        print(f"  run {r.Index}: {when(r.start)} .. {when(r.end)}  {r.blocks} blocks, "
              f"{r.cycles} complete cycles, arms {r.arms}")
    bl = d["blind"]
    print(f"  taken out as blind: {bl['listen_windows']} sdrmux listen windows, "
          f"{bl['transmissions']} FT8 transmissions, outages; {bl['blind_h']:.2f} h in all")
    print(f"  addresses: {d['real']} real aircraft, {d['phantoms']} one-off phantoms, {d['false']} false "
          f"frames on real addresses; {d['pairs']} same-aircraft pairs across changes; "
          f"{d['points']} cloud positions within reach")

    print("\n== per arm: block means over exposure (lam_per_h = arrivals the hazard model expects "
          "then, a check that the arms saw like traffic)")
    print(d["summary"].round(3).to_string())
    print("   noise/signal are digital levels: the RTL2832 AGC rescales them, so they compare only "
          "between arms with the same AGC")

    print("\n== differences within cycles (second arm against first; 95% interval over cycles; "
          "p from re-randomizing the two arms within cycles)")
    for res in d["comparisons"]:
        print(f"\n{res['c']}  vs  {res['a']}   [{res['scope']}]")
        for key, r in res["metrics"].items():
            print(f"  {LABELS[key]:16s}{pct(r['est']):>9s}   [{pct(r['lo'])} .. {pct(r['hi'])}]"
                  f"   p {r['p']:.4f}   {r['cycles']} cycles")
        if res["boundary"]:
            r = res["boundary"]
            print(f"  {'same aircraft':16s}{pct(r['est']):>9s}   frame rate, {r['n']} aircraft across changes"
                  f"   p {r['p']:.4f}")
        else:
            print(f"  {'same aircraft':16s}      n/a   too few aircraft heard across a change")
        if res["coverage"]:
            r = res["coverage"]
            print(f"  {'vs coverage':16s}{pct(r['est']):>9s}   heard/expected {r['oe'][0]:.2f} -> "
                  f"{r['oe'][1]:.2f} over {r['n'][0]} / {r['n'][1]} cloud positions   p {r['p']:.4f}")
        else:
            print(f"  {'vs coverage':16s}      n/a   too few cloud positions where we can hear")
        print("  =>", abtest.verdict(res))

    keep = dict(at=time.time(), runs=d["runs"].reset_index().to_dict("records"), blind=bl,
                summary=d["summary"].reset_index().to_dict("records"),
                comparisons=[dict(res, verdict=abtest.verdict(res)) for res in d["comparisons"]])
    with open(OUT + ".tmp", "w") as f:
        json.dump(keep, f, default=float, indent=1)
    os.replace(OUT + ".tmp", OUT)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["run", "report"])
    ap.add_argument("--hours", type=float, default=24)
    ap.add_argument("--block-min", type=float, default=10)
    ap.add_argument("--restore", default="m49.6")
    ap.add_argument("--arms", help="comma-separated subset of " + ",".join(SETTINGS))
    ap.add_argument("--draws", type=int, default=20000)
    a = ap.parse_args()
    run(a) if a.cmd == "run" else report(a)


if __name__ == "__main__":
    main()
