"""Skeds: times agreed over the FT8 chat to work a station again.

The chat (ft8_llm.py) books one when the other station proposes or agrees to a time and our
confirmation goes out. From LEAD_S before that time until HOLD_S after it, the scheduler keeps
the receiver on 2 m as it does through an exchange: every receive slot, over aircraft, training
hours and window caps, though not over a fault. A sked ends early once that station has been
worked in it, and is dropped when they call it off. One sked per station: a new time replaces
the old one.

Kept in ~/influ/ft8/skeds.json, each sked with the span it is kept for (`start`, `end`).
"""
import json
import os
import threading
import time

LEAD_S, HOLD_S = 300, 900     # listen from 5 min before the agreed time to 15 min after it
SOON_S = 120                  # an agreed time must be at least this far ahead ...
AHEAD_S = 7 * 86400           # ... and no further than this
PER_DAY = 4                   # skeds a UTC day at most


class Skeds:
    def __init__(self, path):
        self.path, self.lock = path, threading.Lock()
        try:
            with open(path) as f:
                self.items = json.load(f)["skeds"]
        except (OSError, ValueError, KeyError):
            self.items = []

    def save(self):
        tmp = self.path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(dict(skeds=self.items), f)
        os.replace(tmp, self.path)

    def room(self, call, t, now):
        """Whether `call` can be booked at `t` (unix time): far enough ahead, not too far, and
        the day not full (their own earlier sked does not count, as it would be replaced)."""
        if not now + SOON_S <= t <= now + AHEAD_S:
            return False
        day = time.gmtime(t)[:3]
        with self.lock:
            return sum(1 for s in self.items if s["call"] != call and s["end"] > now
                       and time.gmtime(s["t"])[:3] == day) < PER_DAY

    def book(self, call, t, said, now):
        """Book `call` at `t`, replacing their earlier sked; False if there is no room."""
        if not self.room(call, t, now):
            return False
        with self.lock:
            kept = [s for s in self.items if s["call"] != call and s["end"] > now]
            sked = dict(call=call, t=t, start=t - LEAD_S, end=t + HOLD_S, said=said, booked=now, worked=None)
            self.items = sorted(kept + [sked], key=lambda s: s["t"])
            self.save()
        return True

    def cancel(self, call, now):
        """Drop `call`'s sked still to come; True if there was one."""
        with self.lock:
            kept = [s for s in self.items if not (s["call"] == call and s["end"] > now)]
            dropped = len(kept) != len(self.items)
            if dropped:
                self.items = kept
                self.save()
        return dropped

    def open(self, now):
        """The sked being kept now (the earliest, if two overlap), else None."""
        with self.lock:
            return next((dict(s) for s in self.items if not s["worked"] and s["start"] <= now <= s["end"]), None)

    def worked(self, call, now):
        """`call` has just been worked: the sked kept for them now is fulfilled."""
        with self.lock:
            for s in self.items:
                if s["call"] == call and not s["worked"] and s["start"] <= now <= s["end"]:
                    s["worked"] = now
                    self.save()
