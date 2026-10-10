"""Training-activity schedule and 60-mile traffic gate; no SDR ownership."""
from collections import defaultdict
import json
import math
import os

KM_PER_MILE = 1.609344


def ident(a):
    return (a.get("flight") or "").strip()


def active(a):
    seen = a.get("seen", 1e9)
    return isinstance(seen, (int, float)) and 0 <= seen <= 60 and a.get("alt_baro") != "ground"


def fit(snapshots, cloud_records):
    """Classify over whole history, so a missed identification packet isn't a small plane."""
    known, categories, reception = set(), defaultdict(set), defaultdict(set)
    exposure, by_minute = set(), defaultdict(set)
    for rec in cloud_records:
        for a in rec.get("ac", []):
            if ident(a):
                known.add(a["hex"].lower().lstrip("~"))
    for rec in snapshots:
        now = rec["now"]
        exposure.add(int(now // 60))
        for a in rec.get("aircraft", []):
            h = a["hex"].lower().lstrip("~")
            if ident(a):
                known.add(h)
            if a.get("category"):
                categories[h].add(a["category"])
            if active(a) and a.get("messages", 0) >= 3:
                minute = int((now - a["seen"]) // 60)
                by_minute[minute].add(h)
                reception[h].add(minute)
    light = {h for h in reception if h not in known and categories[h] == {"A1"}}
    # Persistent uncategorised Mode S targets may include training aircraft; label them uncertain.
    untyped = {h for h in reception if h not in known and not categories[h]
               and len(reception[h]) >= 3}
    counts, observed = [0] * 48, [0] * 48
    for minute in exposure:
        observed[(minute + 480) % 1440 // 30] += 1
    for minute, aircraft in by_minute.items():
        if minute in exposure and aircraft & (light | untyped):
            counts[(minute + 480) % 1440 // 30] += 1
    busy = {i for i in range(48) if counts[i] >= 2 and counts[i] / max(1, observed[i]) >= 0.02}
    protected = sorted({(i + d) % 48 for i in busy for d in (-1, 0, 1)})
    return dict(known_ident=sorted(known), light_no_ident=sorted(light),
                untyped_no_ident=sorted(untyped), observed_minutes=observed,
                active_minutes=counts, busy_bins=sorted(busy), protected_bins=protected,
                exposure_hours=len(exposure) / 60,
                observed_days=len({(m + 480) // 1440 for m in exposure}))


def distance(lat0, lon0, lat, lon):
    p0, p = math.radians(lat0), math.radians(lat)
    dlon = math.radians(lon - lon0)
    h = math.sin((p - p0) / 2) ** 2 + math.cos(p0) * math.cos(p) * math.sin(dlon / 2) ** 2
    return 6371 * 2 * math.asin(math.sqrt(min(1, max(0, h))))


def position_fresh(a, ref_t, now):
    lat, lon = a.get("lat"), a.get("lon")
    if not isinstance(lat, (int, float)) or not isinstance(lon, (int, float)):
        return False
    if not (math.isfinite(lat) and math.isfinite(lon) and -90 <= lat <= 90 and -180 <= lon <= 180):
        return False
    age = max(0, now - ref_t + (a.get("seen_pos") or 0))
    return age <= 120


def near_or_approaching(a, ref_t, now, lat0, lon0, radius_km, horizon=60):
    if a.get("alt_baro") == "ground" or not position_fresh(a, ref_t, now):
        return False
    lat, lon = a["lat"], a["lon"]
    age = max(0, now - ref_t + (a.get("seen_pos") or 0))
    dist = distance(lat0, lon0, lat, lon)
    if dist <= radius_km:
        return True
    speed = max(0, min(700, a.get("gs") or 0)) * 1.852 / 3600
    course = a.get("track")
    if course is None:
        return dist - speed * (age + horizon) <= radius_km
    x = 6371 * math.cos(math.radians(lat0)) * math.radians(lon - lon0)
    y = 6371 * math.radians(lat - lat0)
    vx, vy = speed * math.sin(math.radians(course)), speed * math.cos(math.radians(course))
    if speed == 0:
        return False
    closest_t = min(age + horizon, max(age, -(x * vx + y * vy) / speed ** 2))
    return math.hypot(x + vx * closest_t, y + vy * closest_t) <= radius_km


class Policy:
    def __init__(self, profile, latest, station, radius_mi=60):
        self.profile_path, self.latest_path = profile, latest
        with open(station) as f:
            st = json.load(f)
        self.lat, self.lon = st["lat"], st["lon"]
        self.radius = radius_mi * KM_PER_MILE
        self.profile, self.mtime = None, None

    def assess(self, now, local):
        reasons, threats = [], []
        try:
            mtime = os.path.getmtime(self.profile_path)
            if mtime != self.mtime:
                with open(self.profile_path) as f:
                    self.profile = json.load(f)
                self.mtime = mtime
        except (OSError, ValueError):
            reasons.append("training_profile_unavailable")
        if self.profile:
            if now - self.profile.get("fitted_at", 0) > 2 * 3600:
                reasons.append("training_profile_stale")
            slot = int((now + 8 * 3600) % 86400 // 1800)
            if slot in self.profile["protected_bins"]:
                reasons.append("training_activity_window")
            if self.profile["observed_minutes"][slot] < 15:
                reasons.append("training_hour_unobserved")
        positions = {}
        try:
            with open(self.latest_path) as f:
                latest = json.load(f)
            fresh = [r for r in latest.values() if not r.get("error") and now - r["t"] <= 45]
            if not fresh:
                reasons.append("cloud_tracks_stale")
            for rec in fresh:
                ref = rec.get("now") or rec["t"]
                if ref > 1e11:
                    ref /= 1000
                for a in rec.get("ac", []):
                    if position_fresh(a, ref, now):
                        positions[a["hex"].lower().lstrip("~")] = (a, ref)
                    if near_or_approaching(a, ref, now, self.lat, self.lon, self.radius):
                        reasons.append("cloud_aircraft_near_or_approaching")
                        threats.append(a["hex"])
        except (OSError, ValueError, KeyError):
            reasons.append("cloud_tracks_unavailable")
        for a in local.get("aircraft", []):
            if not active(a):
                continue
            ref = local["now"]
            h = a["hex"].lower().lstrip("~")
            if not position_fresh(a, ref, now):
                if h in positions:
                    a, ref = positions[h]
                else:
                    reasons.append("local_aircraft_without_position")
                    threats.append(h)
                    continue
            if near_or_approaching(a, ref, now, self.lat, self.lon, self.radius):
                reasons.append("local_aircraft_near_or_approaching")
                threats.append(h)
        return dict(reasons=sorted(set(reasons)), threats=sorted(set(threats)))
