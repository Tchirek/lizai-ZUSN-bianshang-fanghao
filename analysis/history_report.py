#!/usr/bin/env python3
"""Historical (2026-09-06 ..) traffic profile from piaware.log; what it implies for switching."""
import numpy as np
import pandas as pd

from influ import piaware_hist

pd.set_option("display.width", 170)


def main():
    h = piaware_hist.parse()
    s = piaware_hist.healthy_bins(h)
    q, ac = h["quiet"], h["mlat15"]
    tz = "Asia/Shanghai"
    print(f"sent5 lines {len(h['sent5'])}  healthy 5-min bins {len(s)}  "
          f"({len(s) * 5 / 60 / 24:.1f} days)  mlat15 {len(ac)}  quiet-watchdog {len(q)}  "
          f"down-complaints {len(h['down'])}")
    if s.empty:
        return
    print(f"range {pd.Timestamp(s.t.min(), unit='s', tz=tz):%m-%d %H:%M} .. "
          f"{pd.Timestamp(s.t.max(), unit='s', tz=tz):%m-%d %H:%M}")

    s["hour"] = pd.to_datetime(s.t, unit="s", utc=True).dt.tz_convert(tz).dt.hour
    s["zero"] = s.n == 0
    ac = ac.copy()
    ac["hour"] = pd.to_datetime(ac.t, unit="s", utc=True).dt.tz_convert(tz).dt.hour
    prof = s.groupby("hour").agg(days=("t", lambda x: len(x) / 12), mean_reports_5m=("n", "mean"),
                                 p_zero_5m=("zero", "mean"))
    prof["aircraft_per_15m"] = ac[ac.ms_total >= 0].groupby("hour").ms_total.mean()
    print("\n== hourly profile (local time). p_zero_5m = share of 5-min bins with NO report sent")
    print(prof.round(3).to_string())

    # runs of consecutive zero bins = contiguous quiet periods (lower bound on true silences)
    z = s.sort_values("t")
    brk = (z.zero != z.zero.shift()) | (z.t - z.t.shift() > 330)
    z = z.assign(run=brk.cumsum())
    runs = z[z.zero].groupby("run").agg(start=("t", "min"), n=("t", "size"))
    runs["minutes"] = runs.n * 5
    print("\n== quiet runs (consecutive 5-min bins with zero reports)")
    print(runs.minutes.describe(percentiles=[.5, .75, .9, .99]).round(1).to_string())
    print(f"   total quiet time {runs.minutes.sum() / 60:.1f} h of {len(s) * 5 / 60:.1f} h observed "
          f"({runs.minutes.sum() / (len(s) * 5):.1%})")
    runs["hour"] = pd.to_datetime(runs.start, unit="s", utc=True).dt.tz_convert(tz).dt.hour
    print("   quiet runs >= 30 min by start hour:",
          runs[runs.minutes >= 30].groupby("hour").size().to_dict())

    if len(q):
        print(f"\n== piaware 'no new messages for >1h' events: {len(q)}, "
              f"longest {q.secs.max() / 3600:.1f} h")
        q["hour"] = pd.to_datetime(q.t, unit="s", utc=True).dt.tz_convert(tz).dt.hour
        print("   by local hour:", q.groupby("hour").size().to_dict())

    s.to_parquet("/home/x230/influ/hist_sent5.parquet")
    ac.to_parquet("/home/x230/influ/hist_mlat15.parquet")


if __name__ == "__main__":
    main()
