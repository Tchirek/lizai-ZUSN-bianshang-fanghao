#!/usr/bin/env python3
"""Refit the arrival-hazard model and write ~/influ/model/hazard.json (run hourly)."""
import json
import os
import time

import numpy as np

from influ import events, hazard, load, piaware_hist

OUT = os.path.expanduser("~/influ/model/hazard.json")


def main():
    h = piaware_hist.parse()
    hb = piaware_hist.healthy_bins(h)
    t1 = time.time()
    msgs, marks = load.beast(t1 - 30 * 86400, t1)
    st = load.stats(t1 - 30 * 86400, t1)
    vis = events.visits(msgs)
    lo, hi = (msgs.t.min(), msgs.t.max()) if len(msgs) else (t1, t1)
    gaps = sorted(events.outage_mask(st, marks, lo, hi) + load.switch_windows())
    observed, cur = [], lo
    for g0, g1 in gaps:
        if g0 > cur:
            observed.append((cur, g0))
        cur = max(cur, g1)
    if hi > cur:
        observed.append((cur, hi))
    m = hazard.fit(hb, vis, observed)
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    tmp = OUT + ".tmp"
    with open(tmp, "w") as f:
        json.dump(m, f)
    os.replace(tmp, OUT)
    hz = hazard.Hazard(m)
    print(f"kappa {m['kappa']:.3f} visits/report, floor {m['floor_per_h']:.2f} visits/h, "
          f"hist {m['hist_days']:.1f} d, fine {m['fine_exposure_h']:.1f} h / {m['fine_visits']} visits")
    print("hour  prior/h  post_mean/h  q90/h   E[hidden|20s]  E[hidden|20s] mean")
    now0 = (int(t1) // 86400) * 86400 - 8 * 3600
    for hr in range(24):
        t = now0 + hr * 3600 + 1800
        print(f"{hr:4d}  {m['lam_prior_per_h'][hr]:7.2f}  {m['lam_mean_per_h'][hr]:11.2f}  "
              f"{m['lam_q_per_h'][hr]:6.2f}  {hz.expected_hidden(t, 20.2):13.5f}  "
              f"{hz.expected_hidden(t, 20.2, conservative=False):.5f}")


if __name__ == "__main__":
    main()
