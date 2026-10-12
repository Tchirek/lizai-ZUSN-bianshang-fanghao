"""Diagnosis of the 1090 MHz receive-settings A/B test that gain_ab.py runs.

The design it reads: a run shuffles its arms into a fresh order every cycle, so each cycle
holds every arm once, side by side in time (a randomized complete block design). Traffic
drifts over hours, so arms are compared within cycles only, and every difference is tested
by redoing that randomization (labels shuffled within cycles): no distribution is assumed
and the night run and the day run keep their own traffic.

Measured per block, over its exposure (time on 1090 MHz, less the first seconds after the
change, sdrmux listen windows, our own FT8 transmissions and receiver outages):
  modes      Mode S frames a minute from real aircraft, false frames left out
  adsb       ADS-B (DF17/18) frames a minute from real aircraft
  positions  airborne position frames a minute (what a feeder's tracks are made of)
  aircraft   real aircraft heard in a CRC-checked frame (DF11/17/18), and tracked (a position)
  modeac     Mode A/C frames a minute (no address: noise and weak replies alike)
  false      false frames an hour: an address/parity frame (DF0/4/5/16/20/21) alone, nothing
             else from that aircraft within a minute, whose altitude or squawk its own other
             frames contradict - noise that the decoder mapped onto an address it had just heard
  phantoms   addresses that occur once and nowhere else (no DF17/18, cloud or position)
Two more views use the models built for the scheduler:
  boundary   the same aircraft just before and just after a change of setting (passes from
             influ.events): its frame rate under each arm, its own trend cancelling out
             because the order of the arms is random
  coverage   cloud positions (other networks) where the coverage model gives us a chance:
             the share heard under each arm against the share the model expects
"""
import json
import math

import numpy as np
import pandas as pd

from . import coverage, events, hazard, load

SETTLE_S = 10                 # the AGC settles in well under a second; sdrmux retunes too
ALONE_S = 60                  # a frame with nothing else from its aircraft this near is alone
AP_DF = (0, 4, 5, 16, 20, 21) # address/parity formats: the address is recovered from the CRC
RUN_GAP_S = 60                # blocks further apart than this are different runs
TX_GUARD = (-1, 15)           # an FT8 frame: from its slot to ~13 s after, with margin
MIN_EXPOSURE = 0.5            # blocks with less of their time observed are left out
BOUNDARY_S = 300              # seconds either side of a change for the same-aircraft view
DRAWS = 20000
SMALLEST = math.log(1.10)     # the difference worth acting on: 10%
PER_MINUTE = ("modes", "adsb", "positions", "modeac")
COMPARED = PER_MINUTE + ("aircraft", "tracked", "false", "phantoms")


def blocks(path):
    """The blocks whose setting applied, in order, numbered by run and by cycle."""
    b = pd.read_json(path, lines=True)
    b = b[b.set.map(lambda s: bool(s.get("ok")))].sort_values("t0").reset_index(drop=True)
    b["run"] = (b.t0 - b.t1.shift() > RUN_GAP_S).cumsum()
    cycle, cycles = 0, []
    for _, g in b.groupby("run", sort=True):
        arms, seen = g.name.nunique(), set()
        for name in g.name:
            if name in seen or len(seen) == arms:       # a full cycle, or a broken one, ends
                cycle, seen = cycle + 1, set()
            seen.add(name)
            cycles.append(cycle)
        cycle += 1
    b["cycle"] = cycles
    b["complete"] = b.groupby("cycle").name.transform("size") == b.groupby("run").name.transform("nunique")
    return b


def merged(gaps):
    out = []
    for a, b in sorted(gaps):
        if out and a <= out[-1][1]:
            out[-1][1] = max(out[-1][1], b)
        else:
            out.append([a, b])
    return out


def overlap(t0, t1, gaps):
    return sum(max(0.0, min(b, t1) - max(a, t0)) for a, b in gaps)


def transmissions(path, t0, t1):
    out = []
    try:
        with open(path) as f:
            for line in f:
                try:
                    r = json.loads(line)
                except ValueError:
                    continue
                if r.get("event") == "transmit" and t0 - 60 <= r.get("slot", 0) <= t1:
                    out.append((r["slot"] + TX_GUARD[0], r["slot"] + TX_GUARD[1]))
    except OSError:
        pass
    return out


def outside(t, gaps):
    """Mask of times not inside any (merged, sorted) gap."""
    if not gaps:
        return np.ones(len(t), bool)
    starts = np.array([a for a, _ in gaps])
    ends = np.array([b for _, b in gaps])
    i = np.searchsorted(starts, t, side="right") - 1
    return ~((i >= 0) & (t < ends[np.clip(i, 0, None)]))


def addresses(msgs, cloud, positioned):
    """Real aircraft and one-off phantoms among the CRC-confirmed addresses."""
    confirmed = events.confirmed_icaos(msgs)
    m = msgs[msgs.icao.isin(confirmed)]
    per = m.groupby("icao").agg(n=("t", "size"), adsb=("df", lambda d: int(d.isin([17, 18]).sum())))
    phantom = set(per[(per.n <= 3) & (per.adsb == 0)].index) - set(cloud) - set(positioned)
    return confirmed - phantom, phantom


def _bits(raw):
    return bin(int(raw, 16))[2:].zfill(len(raw) * 4)


def altitude(raw):
    """Altitude in feet from the AC13 field of DF0/4/16/20; None unless a 25 ft code."""
    ac = _bits(raw)[19:32]
    if ac[6] == "1" or ac[8] == "0":           # metric, or the old 100 ft Gillham code
        return None
    return int(ac[:6] + ac[7] + ac[9:], 2) * 25 - 1000


def squawk(raw):
    """The Mode A code from the ID13 field of DF5/21."""
    c1, a1, c2, a2, c4, a4, _, b1, d1, b2, d2, b4, d4 = (int(v) for v in _bits(raw)[19:32])
    return f"{a4 * 4 + a2 * 2 + a1}{b4 * 4 + b2 * 2 + b1}{c4 * 4 + c2 * 2 + c1}{d4 * 4 + d2 * 2 + d1}"


def false_frames(msgs, real):
    """Mask of false frames among msgs: address/parity frames alone (nothing else from the
    aircraft within ALONE_S) whose altitude or squawk the aircraft's own frames contradict.
    A real reply agrees: consecutive altitude replies within two minutes match within 1000 ft
    99.6% of the time here, and a squawk does not change."""
    out = np.zeros(len(msgs), bool)
    m = msgs[msgs.icao.isin(real)]
    for _, g in m.groupby("icao"):
        at = msgs.index.get_indexer(g.index)
        t = g.t.values
        order = np.argsort(t)
        ts = t[order]
        gap = np.minimum(np.r_[np.inf, np.diff(ts)], np.r_[np.diff(ts), np.inf])
        alone = np.zeros(len(t), bool)
        alone[order] = gap > ALONE_S
        dfs, raws = g.df.values, g.raw.values
        ap = np.isin(dfs, AP_DF)
        if not (alone & ap).any():
            continue
        ref_t, ref_alt, ref_sq = [], [], set()
        for k in np.flatnonzero(~alone & ap):
            if dfs[k] in (5, 21):
                ref_sq.add(squawk(raws[k]))
            elif (v := altitude(raws[k])) is not None:
                ref_t.append(t[k])
                ref_alt.append(v)
        ref_t, ref_alt = np.array(ref_t), np.array(ref_alt)
        for k in np.flatnonzero(alone & ap):
            if dfs[k] in (5, 21):
                bad = bool(ref_sq) and squawk(raws[k]) not in ref_sq
            else:
                v = altitude(raws[k])
                bad = (v is not None and len(ref_t) > 0
                       and abs(ref_alt[np.argmin(np.abs(ref_t - t[k]))] - v) > 1000)
            out[at[k]] = bad
    return out


def per_block(b, msgs, real, phantom, stats):
    rows = []
    for r in b.itertuples():
        m = msgs[(msgs.t >= r.t0 + SETTLE_S) & (msgs.t < r.t1)]
        minutes = r.exposure_s / 60
        live = m[m.icao.isin(real) & ~m.false]
        s = stats[(stats.t > r.t0 + 60) & (stats.t <= r.t1)]
        rows.append(dict(
            modes=len(live) / minutes, adsb=int(live.df.isin([17, 18]).sum()) / minutes,
            positions=int(live.pos.sum()) / minutes, aircraft=live[live.df.isin([11, 17, 18])].icao.nunique(),
            tracked=live[live.pos].icao.nunique(), modeac=int((m.kind == "modeac").sum()) / minutes,
            false=int(m.false.sum()) / minutes * 60, phantoms=m[m.icao.isin(phantom)].icao.nunique(),
            strong=(s.strong / s.messages.clip(lower=1)).mean() if len(s) else np.nan,
            noise=s.noise.mean() if len(s) else np.nan, signal=s.signal.mean() if len(s) else np.nan,
            dropped=s.dropped.sum() if len(s) else np.nan))
    return pd.DataFrame(rows, index=b.index)


def relabel(b, draws, rng, a, c):
    """Arm codes of the blocks as the randomization might have drawn them were arms a and c
    alike: in each cycle holding both, their two blocks swap labels at even odds, the other
    arms keep theirs. Row 0 is what happened."""
    codes = b.arm.values
    labels = np.tile(codes, (draws + 1, 1))
    for idx in b.groupby("cycle").indices.values():
        ia, ic = idx[codes[idx] == a], idx[codes[idx] == c]
        if len(ia) == 1 and len(ic) == 1:
            swap = rng.random(draws) < 0.5
            labels[1:, ia[0]] = np.where(swap, c, a)
            labels[1:, ic[0]] = np.where(swap, a, c)
    return labels


def within_cycles(y, b, labels, a, c, rng):
    """log(c / a) of a per-block value (already logged), cycle by cycle: estimate, 95%
    bootstrap interval over cycles, two-sided permutation p, cycles used."""
    usable = []
    for cyc, idx in b.groupby("cycle").indices.items():
        arms = set(b.arm.values[idx])
        if {a, c} <= arms and not np.isnan(y[idx]).any():
            usable.append(idx)
    if len(usable) < 3:
        return None
    stats = []
    for idx in usable:
        lab, v = labels[:, idx], y[idx]
        stats.append(((lab == c) * v).sum(1) - ((lab == a) * v).sum(1))
    diffs = np.array(stats).T                  # draws + 1 by cycles
    mean = diffs.mean(1)
    boot = rng.choice(diffs[0], (5000, len(usable))).mean(1)
    return dict(est=float(mean[0]), lo=float(np.percentile(boot, 2.5)), hi=float(np.percentile(boot, 97.5)),
                p=float((np.sum(np.abs(mean[1:]) >= abs(mean[0]) - 1e-12) + 1) / len(mean)),
                cycles=len(usable), sd=float(diffs[0].std(ddof=1)))


def boundary_pairs(b, msgs, gaps):
    """For every change of setting, each real aircraft heard on both sides: log of its frame rate
    after over before, with the blocks either side."""
    out = []
    order = b.sort_values("t0")
    for (u, ru), (v, rv) in zip(order.iloc[:-1].iterrows(), order.iloc[1:].iterrows()):
        if ru.run != rv.run:
            continue
        t = rv.t0
        before, after = (t - BOUNDARY_S, t), (t + SETTLE_S, t + SETTLE_S + BOUNDARY_S)
        e0 = BOUNDARY_S - overlap(*before, gaps)
        e1 = BOUNDARY_S - overlap(*after, gaps)
        if min(e0, e1) < BOUNDARY_S / 2:
            continue
        m = msgs[(msgs.t >= before[0]) & (msgs.t < after[1])]
        n0 = m[m.t < t].groupby("icao").size()
        n1 = m[m.t >= after[0]].groupby("icao").size()
        for icao in n0.index.intersection(n1.index):
            out.append((u, v, math.log(n1[icao] / e1) - math.log(n0[icao] / e0)))
    return pd.DataFrame(out, columns=["before", "after", "d"])


def boundary_effect(pairs, b, labels, a, c):
    if pairs.empty:
        return None
    pos = {k: i for i, k in enumerate(b.index)}
    lu = labels[:, [pos[k] for k in pairs.before]]
    lv = labels[:, [pos[k] for k in pairs.after]]
    sign = ((lu == a) & (lv == c)).astype(float) - ((lu == c) & (lv == a))
    used = np.abs(sign).sum(1)
    stat = np.where(used > 0, (sign * pairs.d.values).sum(1) / np.maximum(used, 1), 0.0)
    if used[0] < 10:
        return None
    return dict(est=float(stat[0]), p=float((np.sum(np.abs(stat[1:]) >= abs(stat[0]) - 1e-12) + 1) / len(stat)),
                n=int(used[0]))


def coverage_effect(points, labels, a, c):
    """log of (heard / expected) under c over the same under a, for cloud positions in blocks."""
    if points.empty:
        return None
    lab = labels[:, points.block_pos.values]
    o, p = points.received.values.astype(float), points.p.values
    out = []
    for arm in (a, c):
        m = lab == arm
        out.append(((m * o).sum(1) + 0.5) / ((m * p).sum(1) + 0.5))
    stat = np.log(out[1]) - np.log(out[0])
    n = [int((lab[0] == arm).sum()) for arm in (a, c)]
    if min(n) < 20:
        return None
    return dict(est=float(stat[0]), p=float((np.sum(np.abs(stat[1:]) >= abs(stat[0]) - 1e-12) + 1) / len(stat)),
                n=n, oe=[float(out[0][0]), float(out[1][0])])


def cycles_needed(sd, power_z=0.8416, alpha_z=1.96):
    """Cycles for 80% power to see a 10% difference at 5%, from the spread of the per-cycle
    differences seen so far."""
    return math.ceil(((alpha_z + power_z) * sd / SMALLEST) ** 2)


def diagnose(log_path, ft8_log, hazard_path, draws=DRAWS, seed=1):
    rng = np.random.default_rng(seed)
    b = blocks(log_path)
    names = sorted(b.name.unique())
    b["arm"] = b.name.map({n: i for i, n in enumerate(names)})
    lo, hi = b.t0.min(), b.t1.max()

    msgs, marks = load.beast(lo - 3600, hi + 600)
    stats = load.stats(lo - 600, hi + 600)
    gaps = merged(load.switch_windows() + transmissions(ft8_log, lo, hi)
                  + events.outage_mask(stats, marks, lo, hi))
    b["exposure_s"] = [r.t1 - r.t0 - SETTLE_S - overlap(r.t0 + SETTLE_S, r.t1, gaps) for r in b.itertuples()]
    msgs = msgs[outside(msgs.t.values, gaps)].copy()
    msgs["pos"] = msgs.df.isin([17, 18]) & msgs.raw.map(events._is_airborne_pos)

    cloud = load.cloud(lo - 7 * 86400, hi)
    snaps = load.aircraft(lo, hi, every=5)
    real, phantom = addresses(msgs, cloud.hex.unique(), snaps[snaps.lat.notna()].hex.unique())
    msgs["false"] = false_frames(msgs, real)
    m = per_block(b, msgs, real, phantom, stats)
    short = b.exposure_s < MIN_EXPOSURE * (b.t1 - b.t0)
    m[short] = np.nan
    b = b.join(m)

    model = hazard.Hazard.load(hazard_path)
    b["lam_per_h"] = model.lam_mean[hazard.local_hour((b.t0 + b.t1).values / 2)] * 3600

    # the coverage model, from a week of cloud positions against everything we heard
    week = load.beast(lo - 7 * 86400, hi)[0]
    cov = coverage.fit(coverage.label_cloud(cloud, week[week.icao.notna()]))
    inside = cloud[(cloud.t >= lo) & (cloud.t < hi) & outside(cloud.t.values, gaps)].copy()
    blk = np.searchsorted(b.t0.values, inside.t.values, side="right") - 1
    ok = (blk >= 0) & (inside.t.values < b.t1.values[np.clip(blk, 0, None)])
    inside, blk = inside[ok], blk[ok]
    points = coverage.label_cloud(inside, msgs[msgs.icao.notna()])
    points["p"] = coverage.p_receive(cov, points.lat.values, points.lon.values, points.alt.values)
    points["block_pos"] = blk
    points = points[(points.p >= 0.05) & ~short.values[blk]]

    pairs = boundary_pairs(b, msgs[msgs.icao.isin(real) & ~msgs.false], gaps)

    comparisons = []
    for i, a in enumerate(names):
        for c in names[i + 1:]:
            for scope, runs in [("all", sorted(b.run.unique()))] + [(f"run {r}", [r]) for r in sorted(b.run.unique())]:
                sel = b.run.isin(runs).values
                if not {a, c} <= set(b.name[sel]):
                    continue
                ia, ic = names.index(a), names.index(c)
                sub = b[sel]
                lab = relabel(sub, draws, rng, ia, ic)
                res = dict(a=a, c=c, scope=scope, metrics={})
                for key in COMPARED:
                    y = np.log(sub[key].values.astype(float) + 1.0)
                    r = within_cycles(y, sub, lab, ia, ic, rng)
                    if r:
                        res["metrics"][key] = r
                pp = pairs[pairs.before.isin(sub.index) & pairs.after.isin(sub.index)]
                res["boundary"] = boundary_effect(pp, sub, lab, ia, ic)
                pts = points[np.isin(points.block_pos.values, np.flatnonzero(sel))].copy()
                pts["block_pos"] = np.searchsorted(np.flatnonzero(sel), pts.block_pos.values)
                res["coverage"] = coverage_effect(pts, lab, ia, ic)
                comparisons.append(res)

    summary = b.groupby("name").agg(blocks=("t0", "size"), exposure_h=("exposure_s", lambda s: s.sum() / 3600),
                                    lam_per_h=("lam_per_h", "mean"),
                                    **{k: (k, "mean") for k in COMPARED},
                                    noise=("noise", "mean"), signal=("signal", "mean"), strong=("strong", "mean"),
                                    dropped=("dropped", "sum"))
    runs = b.groupby("run").agg(start=("t0", "min"), end=("t1", "max"), blocks=("t0", "size"),
                                arms=("name", lambda s: ",".join(sorted(set(s)))),
                                cycles=("cycle", lambda s: int(b.loc[s.index].groupby("cycle").complete.first().sum())))
    blind = dict(listen_windows=sum(1 for a, c in load.switch_windows() if lo <= a < hi),
                 transmissions=len(transmissions(ft8_log, lo, hi)),
                 blind_h=float(overlap(lo, hi, gaps) / 3600))
    return dict(blocks=b, summary=summary, runs=runs, comparisons=comparisons, blind=blind,
                phantoms=len(phantom), real=len(real), points=len(points), pairs=len(pairs),
                false=int(msgs.false.sum()))


def verdict(res):
    """What a comparison says: real reception on the feeder's own measure (positions a minute),
    then the noise the second arm lets through that the first does not."""
    pct = lambda x: f"{100 * (math.exp(x) - 1):+.0f}%"
    r = res["metrics"].get("positions")
    if r is None:
        return "too few cycles"
    if r["p"] < 0.05:
        say = f"positions {pct(r['est'])} (p = {r['p']:.3f})"
    elif r["lo"] > -SMALLEST and r["hi"] < SMALLEST:
        say = f"positions within 10% ({pct(r['lo'])} .. {pct(r['hi'])})"
    else:
        say = (f"positions not settled, {pct(r['est'])} ({pct(r['lo'])} .. {pct(r['hi'])}): about "
               f"{cycles_needed(r['sd'])} cycles would show a 10% difference ({r['cycles']} so far)")
    noise = [f"{what} {pct(m['est'])}" for key, what in (("false", "false frames"), ("modeac", "Mode A/C"))
             if (m := res["metrics"].get(key)) and m["p"] < 0.05]
    return say + ("; noise: " + ", ".join(noise) if noise else "; noise unchanged")
