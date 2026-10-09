#!/usr/bin/env python3
"""Status + backtest report over everything collected so far.

usage: report.py [--hours N] [--listen 20] [--step 30] [--warmup-h 6]
"""
import argparse
import time

import numpy as np
import pandas as pd

from influ import backtest, coverage, events, load

pd.set_option("display.width", 160)
pd.set_option("display.max_columns", 30)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--hours", type=float, default=24 * 30)
    ap.add_argument("--listen", type=float, default=20.0)
    ap.add_argument("--step", type=float, default=30.0)
    ap.add_argument("--warmup-h", type=float, default=6.0)
    ap.add_argument("--risk", type=float, default=0.01)
    a = ap.parse_args()

    t1 = time.time()
    t0 = t1 - a.hours * 3600
    msgs, marks = load.beast(t0, t1)
    st = load.stats(t0, t1)
    cl = load.cloud(t0, t1)
    if msgs.empty:
        print("no data yet")
        return
    lo, hi = msgs.t.min(), msgs.t.max()
    print(f"== data {pd.Timestamp(lo, unit='s', tz='Asia/Shanghai'):%m-%d %H:%M} .. "
          f"{pd.Timestamp(hi, unit='s', tz='Asia/Shanghai'):%m-%d %H:%M}  ({(hi - lo) / 3600:.1f} h)")
    print(f"   frames {len(msgs)}  modeS {int((msgs.kind != 'modeac').sum())}  "
          f"cloud positions {len(cl)} ({cl.hex.nunique() if len(cl) else 0} aircraft)")

    vis = events.visits(msgs)
    print(f"\n== visits (gap {events.VISIT_GAP:.0f}s): {len(vis)}  aircraft {vis.icao.nunique()}")
    if len(vis):
        q = vis.dur.quantile([.1, .25, .5, .75, .9]).round(0).to_dict()
        print("   duration quantiles s:", q)
        for D in (10, 20, 30, 60):
            print(f"   share of visits shorter than {D:>2}s: {(vis.dur < D).mean():.1%}  "
                  f"(<=2 msgs: {((vis.dur < D) & (vis.n <= 2)).mean():.1%})")

    gaps = sorted(events.outage_mask(st, marks, lo, hi) + load.switch_windows())
    observed, cur = [], lo
    for g0, g1 in gaps:
        if g0 > cur:
            observed.append((cur, g0))
        cur = max(cur, g1)
    if hi > cur:
        observed.append((cur, hi))
    print(f"   receiver outages: {len(gaps)}, observed {sum(b - a for a, b in observed) / 3600:.1f} h")

    cov = None
    if len(cl):
        lab = coverage.label_cloud(cl, msgs)
        print("\n== coverage vs cloud reference (cloud position counted received if we heard the "
              "ICAO within +-30 s)")
        print(coverage.range_table(lab).to_string())
        cov = coverage.fit(lab)
        local_only = set(vis.icao) - set(cl.hex)
        print(f"   aircraft we received that the cloud never reported: {len(local_only)} of "
              f"{vis.icao.nunique()}")

    def cloud_check(w0, D):
        if cov is None:
            return {}
        c = cl[(cl.t >= w0 - 15) & (cl.t < w0 + D + 15)]
        if c.empty:
            return dict(cloud_in_window=0, cloud_likely_receivable=0)
        p = coverage.p_receive(cov, c.lat.values, c.lon.values, c.alt.values)
        likely = set(c.hex[p >= 0.5])
        return dict(cloud_in_window=c.hex.nunique(), cloud_likely_receivable=len(likely))

    span = hi - lo
    warm = min(a.warmup_h * 3600, span / 2)
    res = backtest.run(vis, msgs[msgs.icao.notna()], observed, step=a.step, listen=a.listen,
                       risk_max=a.risk, warmup=warm, cloud_fn=cloud_check)
    if res.empty:
        print("\nnot enough data for backtest yet")
        return
    print(f"\n== walk-forward virtual switch backtest: listen {a.listen:.0f}s, step {a.step:.0f}s, "
          f"risk_max {a.risk}, warmup {warm / 3600:.1f} h")
    print(backtest.summarize(res).round(3).to_string(index=False))
    res.to_parquet(f"{load.DATA}/../backtest_last.parquet")

    # calibration: predicted expected misses vs actual hidden targets, by decile of prediction
    g = res[res.overhead == res.overhead.min()].copy()
    if g.exp_miss.nunique() > 3:
        g["bin"] = pd.qcut(g.exp_miss, 5, duplicates="drop")
        print("\n== calibration (overhead %.1fs)" % g.overhead.iloc[0])
        print(g.groupby("bin", observed=True).agg(windows=("t0", "size"), pred=("exp_miss", "sum"),
                                                  actual_hidden=("q4_hidden", "sum"),
                                                  actual_new=("q2_new", "sum")).to_string())


if __name__ == "__main__":
    main()
