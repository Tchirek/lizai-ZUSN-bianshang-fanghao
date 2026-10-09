"""Turn raw Mode S messages into per-aircraft "visits" (continuous reception episodes)."""
import numpy as np
import pandas as pd

VISIT_GAP = 300.0  # s without messages from an ICAO ends a visit


def confirmed_icaos(msgs):
    """ICAOs seen in at least one CRC-checked DF11/17/18 frame.

    DF0/4/5/16/20/21 carry address-XOR-parity, so any noise burst yields some address;
    only trust those addresses once the aircraft has identified itself cleanly.
    """
    m = msgs[msgs.df.isin([11, 17, 18]) & msgs.icao.notna()]
    return set(m.icao)


def visits(msgs, gap=VISIT_GAP):
    m = msgs[msgs.icao.notna()]
    ok = confirmed_icaos(m)
    m = m[m.icao.isin(ok)].sort_values(["icao", "t"])
    if m.empty:
        return pd.DataFrame(columns=["icao", "t_first", "t_last", "n", "n_pos", "sig_max", "dur"])
    new = (m.icao != m.icao.shift()) | (m.t - m.t.shift() > gap)
    m = m.assign(vid=new.cumsum(), is_pos=m.df.isin([17, 18]) & m.raw.map(_is_airborne_pos))
    v = m.groupby("vid").agg(icao=("icao", "first"), t_first=("t", "min"), t_last=("t", "max"),
                             n=("t", "size"), n_pos=("is_pos", "sum"), sig_max=("sig", "max"))
    v["dur"] = v.t_last - v.t_first
    return v.reset_index(drop=True).sort_values("t_first").reset_index(drop=True)


def _is_airborne_pos(raw):
    if len(raw) != 28:
        return False
    tc = int(raw[8:10], 16) >> 3
    return 9 <= tc <= 18 or 20 <= tc <= 22


def outage_mask(stats_df, beast_marks, t_lo, t_hi):
    """Intervals where the receiver itself was not observing (collector or decoder down).

    Returns a list of (start, end). Backtests must not treat these as quiet sky.
    """
    gaps = []
    if not stats_df.empty:
        t = np.sort(stats_df.t.dropna().values)
        for a, b in zip(t[:-1], t[1:]):
            if b - a > 150:  # stats.json refreshed every 60 s
                gaps.append((a, b))
        bad = stats_df[(stats_df.processed.fillna(0) == 0)]
        gaps += [(r.t - 60, r.t) for r in bad.itertuples()]
    if not beast_marks.empty:
        disc = beast_marks[beast_marks.event == "DISCONNECT"].t.values
        conn = beast_marks[beast_marks.event == "CONNECT"].t.values
        for d in disc:
            later = conn[conn > d]
            gaps.append((d, later[0] if len(later) else t_hi))
    return sorted(gaps)
