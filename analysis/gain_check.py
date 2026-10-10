#!/usr/bin/env python3
"""Did the switch to sdrmux (fixed gain) change ADS-B reception?

Compares, per local hour, the 'since' period (sdrmux mode) against the piaware.log history
before it: FlightAware reports per 5 min and mlat-client aircraft per 15 min. Also prints
message-level stats from the collector for both periods.
"""
import argparse

import numpy as np
import pandas as pd

from influ import events, load, piaware_hist

TZ = "Asia/Shanghai"


def hour(t):
    return pd.to_datetime(t, unit="s", utc=True).dt.tz_convert(TZ).dt.hour


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", default="2026-10-10T01:45:00+08:00", help="sdrmux mode start")
    a = ap.parse_args()
    t_switch = pd.Timestamp(a.since).timestamp()
    h = piaware_hist.parse()
    s = piaware_hist.healthy_bins(h)
    s["hour"] = hour(s.t)
    s["period"] = np.where(s.t >= t_switch, "sdrmux", "direct")
    ac = h["mlat15"].copy()
    ac["hour"] = hour(ac.t)
    ac["period"] = np.where(ac.t >= t_switch, "sdrmux", "direct")
    hours_new = sorted(s[s.period == "sdrmux"].hour.unique())
    rows = []
    for hr in hours_new:
        old = s[(s.period == "direct") & (s.hour == hr)].n
        new = s[(s.period == "sdrmux") & (s.hour == hr)].n
        ao = ac[(ac.period == "direct") & (ac.hour == hr)].ms_total
        an = ac[(ac.period == "sdrmux") & (ac.hour == hr)].ms_total
        # where does today's mean sit within the distribution of historical daily means?
        hist_days = s[(s.period == "direct") & (s.hour == hr)].assign(
            d=lambda x: pd.to_datetime(x.t, unit="s").dt.date).groupby("d").n.mean()
        pct = (hist_days < new.mean()).mean() * 100 if len(hist_days) else np.nan
        rows.append(dict(hour=hr, hist_reports_5m=round(old.mean(), 2),
                         today_reports_5m=round(new.mean(), 2), today_pctile_of_days=round(pct),
                         hist_aircraft_15m=round(ao.mean(), 2), today_aircraft_15m=round(an.mean(), 2)))
    df = pd.DataFrame(rows)
    print("== FlightAware reports / mlat aircraft per hour: history (direct) vs today (sdrmux)")
    print(df.to_string(index=False))
    tot_old = s[(s.period == "direct") & s.hour.isin(hours_new)].n.mean()
    tot_new = s[s.period == "sdrmux"].n.mean()
    print(f"same hours overall: history {tot_old:.2f} vs today {tot_new:.2f} reports/5min "
          f"({tot_new / tot_old - 1:+.0%})")

    st = load.stats()
    st["period"] = np.where(st.t >= t_switch, "sdrmux", "direct")
    st["hour"] = hour(st.t)
    print("\n== receiver stats per minute (collector)")
    print(st.groupby("period")[["signal", "noise", "peak", "messages", "accepted", "cpr_air"]]
          .mean().round(2).to_string())
    msgs, _ = load.beast()
    vis = events.visits(msgs)
    vis["period"] = np.where(vis.t_first >= t_switch, "sdrmux", "direct")
    print("\n== visits: n, median messages, share <=2 msgs, median duration")
    print(vis.groupby("period").agg(n=("n", "size"), med_msgs=("n", "median"),
                                    glimpses=("n", lambda x: (x <= 2).mean()),
                                    med_dur=("dur", "median"), max_sig=("sig_max", "median"))
          .round(2).to_string())


if __name__ == "__main__":
    main()
