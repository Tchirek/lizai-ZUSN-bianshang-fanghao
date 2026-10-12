"""Station coverage model: P(we receive an aircraft | azimuth, distance, altitude).

Positives/negatives come from cloud reference positions: a cloud position at time t counts
as "received" if our local decoder has any message from that ICAO within +-tol seconds.
Local-only positions (aircraft the cloud never saw) are kept separately as evidence of
coverage the cloud cannot confirm.
"""
import functools
import json
import os

import numpy as np
import pandas as pd

STATION = os.path.expanduser("~/influ/station.json")
R_EARTH = 6371.0

AZ_BINS = np.arange(0, 361, 22.5)
DIST_BINS = np.array([0, 20, 40, 60, 80, 100, 130, 160, 200, 250, 300, 400, 500])
ALT_BINS = np.array([-1e9, 3000, 10000, 20000, 30000, 1e9])


@functools.lru_cache(maxsize=1)
def station():
    with open(STATION) as f:
        s = json.load(f)
    return s["lat"], s["lon"]


def polar(lat, lon):
    lat0, lon0 = station()
    lat1, lon1 = np.radians(lat0), np.radians(lon0)
    lat2, lon2 = np.radians(lat), np.radians(lon)
    dlon = lon2 - lon1
    a = np.sin((lat2 - lat1) / 2) ** 2 + np.cos(lat1) * np.cos(lat2) * np.sin(dlon / 2) ** 2
    dist = 2 * R_EARTH * np.arcsin(np.sqrt(a))
    y = np.sin(dlon) * np.cos(lat2)
    x = np.cos(lat1) * np.sin(lat2) - np.sin(lat1) * np.cos(lat2) * np.cos(dlon)
    az = (np.degrees(np.arctan2(y, x)) + 360) % 360
    return az, dist


def label_cloud(cloud, msgs, tol=30.0):
    """Mark each cloud position received/not by local messages from the same ICAO."""
    c = cloud.copy()
    c["az"], c["dist"] = polar(c.lat.values, c.lon.values)
    by = {k: np.sort(g.t.values) for k, g in msgs[msgs.icao.notna()].groupby("icao")}
    rec = np.zeros(len(c), bool)
    for i, (h, t) in enumerate(zip(c.hex.values, c.t.values)):
        ts = by.get(h)
        if ts is not None:
            j = np.searchsorted(ts, t - tol)
            rec[i] = j < len(ts) and ts[j] <= t + tol
    c["received"] = rec
    return c


def fit(labeled, prior_a=1.0, prior_b=1.0):
    """Beta-binomial grid estimate of P(receive) per (az, dist, alt) cell."""
    L = labeled.dropna(subset=["alt"])
    ia = np.digitize(L.az, AZ_BINS) - 1
    idist = np.clip(np.digitize(L.dist, DIST_BINS) - 1, 0, len(DIST_BINS) - 2)
    ialt = np.clip(np.digitize(L.alt, ALT_BINS) - 1, 0, len(ALT_BINS) - 2)
    shape = (len(AZ_BINS) - 1, len(DIST_BINS) - 1, len(ALT_BINS) - 1)
    pos = np.zeros(shape)
    tot = np.zeros(shape)
    np.add.at(pos, (ia, idist, ialt), L.received.values.astype(float))
    np.add.at(tot, (ia, idist, ialt), 1.0)
    return dict(p=(pos + prior_a) / (tot + prior_a + prior_b), n=tot, pos=pos)


def p_receive(model, lat, lon, alt):
    az, dist = polar(np.asarray(lat), np.asarray(lon))
    ia = np.clip(np.digitize(az, AZ_BINS) - 1, 0, len(AZ_BINS) - 2)
    idist = np.clip(np.digitize(dist, DIST_BINS) - 1, 0, len(DIST_BINS) - 2)
    ialt = np.clip(np.digitize(np.nan_to_num(alt, nan=20000), ALT_BINS) - 1, 0, len(ALT_BINS) - 2)
    return model["p"][ia, idist, ialt]


def range_table(labeled):
    """Reception fraction by distance ring (all azimuths/altitudes) for a quick look."""
    L = labeled.copy()
    L["ring"] = pd.cut(L.dist, DIST_BINS)
    return L.groupby("ring", observed=True).agg(cloud_pos=("received", "size"),
                                                received=("received", "sum"),
                                                frac=("received", "mean"),
                                                aircraft=("hex", "nunique"))
