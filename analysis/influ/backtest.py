"""Virtual frequency-switch backtest.

For a blind window [t0, t0 + D) where D = listen + switch overhead, answer:
  Q1 would the model have approved switching at t0 (using only data before t0)?
  Q2 how many NEW targets (visit starting inside the window) appeared?
  Q3 how many of those were still received after the window ended?
  Q4 how many existed ONLY inside the window (fully missed if we had switched)?
     + cloud cross-check: reference aircraft inside our coverage during the window
  Q5 the same as a function of the switch overhead.

Models (each sees only the past):
  baseline   hour-of-day Poisson arrival rate of new visits, gamma-shrunk
  The expected number of fully-missed targets for a window of length D is
      E[miss] = lambda * E[(D - dur)+]
  since a visit of duration d is fully hidden iff it starts in [t0, t0 + D - d].
"""
import numpy as np
import pandas as pd


class BaselineModel:
    """Hour-of-day arrival rate with a global prior; visit-duration distribution."""

    def __init__(self, prior_weight_s=3600.0):
        self.w = prior_weight_s

    def fit(self, vis, observed_intervals):
        """vis: visits strictly before the decision time. observed_intervals: (start, end) list
        of time when the receiver was actually listening (exposure)."""
        hours = np.zeros(24)
        for a, b in observed_intervals:
            # exposure seconds per hour-of-day (local time UTC+8)
            for h0 in np.arange(np.floor(a / 3600) * 3600, b, 3600):
                lo, hi = max(a, h0), min(b, h0 + 3600)
                if hi > lo:
                    hours[int(((h0 / 3600) + 8) % 24)] += hi - lo
        counts = np.zeros(24)
        if len(vis):
            hh = ((vis.t_first.values // 3600 + 8) % 24).astype(int)
            np.add.at(counts, hh, 1)
        tot_rate = counts.sum() / max(hours.sum(), 1.0)
        # gamma-Poisson shrinkage toward the global rate
        self.rate = (counts + tot_rate * self.w) / (hours + self.w)
        self.exposure = hours
        self.durs = np.sort(vis.dur.values) if len(vis) else np.array([0.0])
        return self

    def lam(self, t):
        return self.rate[int(((t // 3600) + 8) % 24)]

    def expected_miss(self, t, D):
        return self.lam(t) * np.mean(np.clip(D - self.durs, 0, None))

    def p_new(self, t, D):
        return 1 - np.exp(-self.lam(t) * D)


def window_truth(vis, msgs_t, t0, D):
    """Ground truth for a virtual blind window from complete local reception."""
    t1 = t0 + D
    new = vis[(vis.t_first >= t0) & (vis.t_first < t1)]
    survived = new[new.t_last >= t1]
    hidden = new[new.t_last < t1]
    active = vis[(vis.t_first < t0) & (vis.t_last >= t0)]
    lost_msgs = int(((msgs_t >= t0) & (msgs_t < t1)).sum())
    return dict(q2_new=len(new), q3_survived=len(survived), q4_hidden=len(hidden),
                hidden_icaos=",".join(hidden.icao), active_at_t0=len(active), lost_msgs=lost_msgs)


def run(vis, msgs, observed, step=30.0, listen=20.0, overheads=(0.2, 3.0, 8.0),
        risk_max=0.01, min_quiet=120.0, warmup=6 * 3600, refit_every=3600, cloud_fn=None):
    """Walk forward through time; at each grid point decide and score.

    observed: list of (start, end) intervals with a healthy receiver.
    """
    msgs_t = np.sort(msgs.t.values)
    rows = []
    model, fitted_at = None, -np.inf
    t_start = observed[0][0] + warmup
    for a, b in observed:
        for t0 in np.arange(max(a, t_start), b - max(overheads) - listen, step):
            if t0 - fitted_at >= refit_every:
                past = vis[vis.t_last < t0]
                exp_iv = [(x, min(y, t0)) for x, y in observed if x < t0]
                model = BaselineModel().fit(past, exp_iv)
                fitted_at = t0
            before = vis[vis.t_first < t0]
            quiet = t0 - before.t_last.max() if len(before) else np.inf
            active = int(((vis.t_first < t0) & (vis.t_last >= t0 - 60)).sum())
            for ov in overheads:
                D = listen + ov
                em = model.expected_miss(t0, D)
                approve = active == 0 and quiet >= min_quiet and em <= risk_max
                r = dict(t0=t0, overhead=ov, D=D, lam_per_h=model.lam(t0) * 3600,
                         p_new=model.p_new(t0, D), exp_miss=em, active_recent=active,
                         quiet_s=quiet, q1_approve=approve)
                r.update(window_truth(vis, msgs_t, t0, D))
                if cloud_fn is not None:
                    r.update(cloud_fn(t0, D))
                rows.append(r)
    return pd.DataFrame(rows)


def summarize(res):
    out = []
    for ov, g in res.groupby("overhead"):
        ap = g[g.q1_approve]
        out.append(dict(
            overhead_s=ov, windows=len(g), approved=len(ap),
            approved_pct=100 * len(ap) / max(len(g), 1),
            listen_min_per_day=len(ap) * (g.D.iloc[0] - ov) / 60 / max(
                (g.t0.max() - g.t0.min()) / 86400, 1e-9) if len(ap) else 0.0,
            new_in_approved=int(ap.q2_new.sum()), survived_in_approved=int(ap.q3_survived.sum()),
            hidden_in_approved=int(ap.q4_hidden.sum()),
            lost_msgs_in_approved=int(ap.lost_msgs.sum()),
            pred_miss_in_approved=float(ap.exp_miss.sum()),
            hidden_all_windows=int(g.q4_hidden.sum()), pred_miss_all=float(g.exp_miss.sum()),
        ))
    return pd.DataFrame(out)
