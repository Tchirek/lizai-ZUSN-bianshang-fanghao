"""Arrival-hazard model for new aircraft visits, combining 23+ days of piaware.log history
(coarse, all hours) with the collector's message-level data (fine, recent).

For each local hour h the visit arrival rate lambda_h gets a gamma posterior:
    prior    shape = W * lam_prior_h, rate = W           (W = prior weight in seconds)
    data     + count of new visits in hour h, + exposure seconds in hour h
lam_prior_h = kappa * (FlightAware reports/s at hour h) + floor
where kappa (visits per FA report) and floor (visits/s at hours with no reports) are fitted
on the period where both sources overlap. Decisions use an upper quantile of lambda_h, so
thin data makes the scheduler more cautious, never less.

Expected fully-hidden visits for a blind window of length D:
    E[hidden] = lambda_h * E[(D - dur)+]        (dur ~ empirical visit duration)
"""
import json
import time

import numpy as np
import pandas as pd
from scipy import stats as sstats

TZ_OFF = 8 * 3600


def local_hour(t):
    return ((np.asarray(t) + TZ_OFF) // 3600 % 24).astype(int)


def exposure_by_hour(intervals):
    e = np.zeros(24)
    for a, b in intervals:
        h0 = np.floor(a / 3600) * 3600
        while h0 < b:
            lo, hi = max(a, h0), min(b, h0 + 3600)
            if hi > lo:
                e[local_hour(h0)] += hi - lo
            h0 += 3600
    return e


def fit(hist_bins, vis, observed, prior_weight_s=4 * 3600, q=0.9):
    """hist_bins: piaware healthy 5-min bins (t, n). vis: fine visits. observed: fine intervals."""
    hb = hist_bins.copy()
    hb["hour"] = local_hour(hb.t.values)
    rep_rate = hb.groupby("hour").n.sum() / (hb.groupby("hour").size() * 300.0)
    rep_rate = rep_rate.reindex(range(24), fill_value=0.0).values

    # overlap: fine observed time that also has piaware bins
    exp_fine = exposure_by_hour(observed)
    cnt_fine = np.zeros(24)
    if len(vis):
        np.add.at(cnt_fine, local_hour(vis.t_first.values), 1)
    in_obs = np.zeros(len(hb), bool)
    for a, b in observed:
        in_obs |= (hb.t.values >= a) & (hb.t.values + 300 <= b)
    ov = hb[in_obs]
    reports = ov.n.sum()
    visits_ov = sum(((vis.t_first >= a) & (vis.t_first < b)).sum() for a, b in observed) if len(vis) else 0
    zero_bins = ov[ov.n == 0]
    if len(zero_bins):
        zt = zero_bins.t.values
        z_vis = sum(((vis.t_first.values >= t) & (vis.t_first.values < t + 300)).sum() for t in zt)
        floor = (z_vis + 0.5) / (len(zt) * 300.0)       # visits/s when FA saw nothing
    else:
        floor = 1.0 / 3600
    kappa = max((visits_ov - floor * ov.shape[0] * 300) / reports, 0.0) if reports else 0.0
    lam_prior = kappa * rep_rate + floor

    a_post = prior_weight_s * lam_prior + cnt_fine
    b_post = prior_weight_s + exp_fine
    lam_mean = a_post / b_post
    lam_q = sstats.gamma.ppf(q, a_post, scale=1 / b_post)
    durs = np.sort(vis.dur.values).tolist() if len(vis) else []
    return dict(
        fitted_at=time.time(), quantile=q, prior_weight_s=prior_weight_s,
        kappa=kappa, floor_per_h=floor * 3600, overlap_reports=int(reports),
        overlap_visits=int(visits_ov), hist_days=len(hb) / 288.0,
        fine_exposure_h=float(exp_fine.sum() / 3600), fine_visits=int(cnt_fine.sum()),
        lam_prior_per_h=(lam_prior * 3600).round(4).tolist(),
        lam_mean_per_h=(lam_mean * 3600).round(4).tolist(),
        lam_q_per_h=(lam_q * 3600).round(4).tolist(),
        fine_count=cnt_fine.astype(int).tolist(), fine_exposure_s=exp_fine.round(0).tolist(),
        durations=durs,
    )


class Hazard:
    """Runtime view of a fitted model (used by the scheduler)."""

    def __init__(self, d):
        self.d = d
        self.lam_q = np.array(d["lam_q_per_h"]) / 3600
        self.lam_mean = np.array(d["lam_mean_per_h"]) / 3600
        self.durs = np.array(d["durations"]) if d["durations"] else None

    @classmethod
    def load(cls, path):
        with open(path) as f:
            return cls(json.load(f))

    def shortfall(self, D):
        """E[(D - dur)+]; with fewer than 30 visits assume every visit is instantaneous."""
        if self.durs is None or len(self.durs) < 30:
            return D
        return float(np.mean(np.clip(D - self.durs, 0, None)))

    def expected_hidden(self, t, D, conservative=True):
        lam = (self.lam_q if conservative else self.lam_mean)[local_hour(t)]
        return float(lam * self.shortfall(D))

    def p_any_new(self, t, D):
        return float(1 - np.exp(-self.lam_q[local_hour(t)] * D))
