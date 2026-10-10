#!/usr/bin/env python3
"""influ scheduler - decides every 30 s whether the RTL-SDR may leave 1090 MHz.

Decision (Q1) gates, all must hold:
  * sdrmux healthy, dump1090 stats fresh, collector-independent Beast feed alive
  * no confirmed aircraft heard in the last --active-s seconds
  * no confirmed aircraft heard for at least --min-quiet seconds
  * E[hidden new aircraft] = lambda_q90(hour) * E[(D - dur)+] <= --risk-max
  * today's spent risk + this window <= --day-risk, blind seconds <= --day-blind,
    windows this hour < --max-per-hour
Every decision is logged (shadow mode logs only). In live mode an approved decision opens
one sdrmux listen window, then the 120 s after it are watched for aircraft that appear
without having been heard in the 5 min before (Q3: arrived during/around the window and
were still received afterwards).
"""
import argparse
import json
import logging
import os
import socket
import subprocess
import sys
import threading
import time
from collections import deque

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "analysis"))
sys.path.insert(0, os.path.join(HERE, "..", "collector"))
from influ.hazard import Hazard, local_hour            # noqa: E402
from influ.load import _icao                            # noqa: E402
from influ_collector import beast_frames                # noqa: E402

log = logging.getLogger("sched")
SOCK = "/run/sdrmux/ctl.sock"


class Tracker:
    """Real-time per-ICAO first/last heard from the Beast stream (confirmed ICAOs only)."""

    def __init__(self):
        self.lock = threading.Lock()
        self.confirmed = {}       # icao -> last DF11/17/18 time
        self.last = {}            # icao -> last heard (any DF, once confirmed)
        self.first_in_visit = {}  # icao -> start of current visit
        self.last_frame = 0.0
        self.started = time.time()          # quiet time can't be claimed before we listened
        self.events = deque(maxlen=20000)   # (t, icao, new_visit)

    def feed(self, t, raw):
        b0 = int(raw[:2], 16) >> 3 if len(raw) >= 2 else -1
        icao = _icao(raw) if len(raw) in (14, 28) else None
        with self.lock:
            self.last_frame = t
            if icao is None:
                return
            if b0 in (11, 17, 18):
                self.confirmed[icao] = t
            if icao not in self.confirmed:
                return
            prev = self.last.get(icao)
            new = prev is None or t - prev > 300
            if new:
                self.first_in_visit[icao] = t
            self.last[icao] = t
            self.events.append((t, icao, new))

    def snapshot(self, now):
        with self.lock:
            last_any = max(self.last.values(), default=self.started)
            active = sum(1 for v in self.last.values() if now - v < 60)
            return dict(quiet_s=now - max(last_any, self.started), active_60s=active,
                        beast_age=now - self.last_frame if self.last_frame else 1e9)

    def appeared(self, t0, t1):
        """ICAOs whose visit started in [t0, t1), i.e. not heard in the 300 s before."""
        with self.lock:
            return sorted({ic for t, ic, new in self.events if new and t0 <= t < t1})

    def run(self, host="127.0.0.1", port=30005):
        while True:
            try:
                with socket.create_connection((host, port), timeout=10) as s:
                    s.settimeout(300)
                    for _typ, _ts, _sig, payload in beast_frames(s):
                        self.feed(time.time(), payload.hex())
            except Exception as e:
                log.warning("beast: %s", e)
                time.sleep(3)


def sdrmux(req, timeout=200):
    s = socket.socket(socket.AF_UNIX)
    s.settimeout(timeout)
    s.connect(SOCK)
    s.sendall((json.dumps(req) + "\n").encode())
    return json.loads(s.makefile().readline())


def dump1090_fresh():
    try:
        with open("/run/dump1090-fa/aircraft.json") as f:
            return time.time() - json.load(f)["now"] < 5
    except Exception:
        return False


class Budget:
    def __init__(self, path):
        self.path = path
        self.day = None
        self.risk = 0.0
        self.blind = 0.0
        self.per_hour = {}
        self.hour_risk = {}
        try:
            d = json.load(open(path))
            if d["day"] == self.today():
                self.day, self.risk, self.blind = d["day"], d["risk"], d["blind"]
                self.per_hour = {int(k): v for k, v in d["per_hour"].items()}
                self.hour_risk = {int(k): v for k, v in d.get("hour_risk", {}).items()}
        except Exception:
            pass

    @staticmethod
    def today():
        return time.strftime("%Y-%m-%d", time.localtime())

    def roll(self):
        if self.day != self.today():
            self.day, self.risk, self.blind = self.today(), 0.0, 0.0
            self.per_hour, self.hour_risk = {}, {}

    def spend(self, risk, blind, hour):
        self.risk += risk
        self.blind += blind
        self.per_hour[hour] = self.per_hour.get(hour, 0) + 1
        self.hour_risk[hour] = self.hour_risk.get(hour, 0.0) + risk
        with open(self.path + ".tmp", "w") as f:
            json.dump(dict(day=self.day, risk=self.risk, blind=self.blind,
                           per_hour=self.per_hour, hour_risk=self.hour_risk), f)
        os.replace(self.path + ".tmp", self.path)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=["shadow", "live"], default="shadow")
    ap.add_argument("--model", default=os.path.expanduser("~/influ/model/hazard.json"))
    ap.add_argument("--out", default=os.path.expanduser("~/influ/sched"))
    # 15 s period = FT8 slot; 14 s windows cover one full slot (signals end at 13.14 s)
    ap.add_argument("--period", type=float, default=15.0)
    ap.add_argument("--listen", type=float, default=14.0)
    ap.add_argument("--overhead", type=float, default=0.25)
    # 144.9 MHz centre: 143.82-145.98 MHz usable, incl. FT8 144.174/144.460 and most 2 m FM
    ap.add_argument("--freq", type=float, default=144_900_000)
    ap.add_argument("--risk-max", type=float, default=0.015)
    ap.add_argument("--day-risk", type=float, default=0.1)
    ap.add_argument("--hour-risk", type=float, default=0.02,
                    help="spread the daily budget: max expected misses spent per hour")
    ap.add_argument("--day-blind", type=float, default=1800)
    ap.add_argument("--max-per-hour", type=int, default=6)
    ap.add_argument("--min-quiet", type=float, default=120)
    ap.add_argument("--active-s", type=float, default=60)
    ap.add_argument("--post-hook", default=os.path.join(HERE, "post_window.sh"))
    ap.add_argument("--ft8-tx", action="store_true", help="enable calibrated CEC FT8 auto-sequence")
    ap.add_argument("--ft8-llm", action="store_true", help="optional OpenAI replies to FT8 free text")
    ap.add_argument("--radio-port", default="/dev/serial/by-id/usb-Prolific_Technology_Inc._USB-Serial_Controller_D-if00-port0")
    a = ap.parse_args()
    if a.ft8_llm and not (a.ft8_tx and a.mode == "live"):
        ap.error("--ft8-llm requires --ft8-tx and --mode live")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    os.makedirs(a.out, exist_ok=True)

    tr = Tracker()
    threading.Thread(target=tr.run, daemon=True).start()
    budget = Budget(os.path.join(a.out, "budget.json"))
    auto = None
    if a.ft8_tx and a.mode == "live":
        from ft8_auto import Auto
        auto = Auto(a.radio_port, os.path.expanduser("~/influ/radio/calibration.json"),
                    os.path.expanduser("~/influ/ft8"), llm=a.ft8_llm)
        log.info("FT8 auto-sequence enabled for %s", auto.call)
    hz, hz_mtime = None, 0
    time.sleep(5)
    log.info("scheduler started in %s mode", a.mode)

    while True:
        t = time.time()
        time.sleep(a.period - (t % a.period))
        now = time.time()
        try:
            mt = os.path.getmtime(a.model)
            if mt != hz_mtime:
                hz, hz_mtime = Hazard.load(a.model), mt
        except OSError:
            hz = None
        budget.roll()
        D = a.listen + a.overhead
        snap = tr.snapshot(now)
        try:
            mux = sdrmux({"cmd": "status"}, timeout=5)
        except Exception as e:
            mux = {"ok": False, "error": str(e)}
        if auto and mux.get("sdr_ok") and mux.get("fifo_connected") and mux.get("mode") == "adsb":
            try:
                if auto.transmit(int(now // 15) * 15):
                    # The V4 remains on 1090 during TX; never open a simultaneous RX window.
                    continue
            except Exception as e:
                log.exception("FT8 TX disabled: %s", e)
                auto.write("disabled", error=str(e))
                auto.close()
                auto = None
        hour = int(local_hour(now))
        exp_hidden = hz.expected_hidden(now, D) if hz else 1.0
        reasons = []
        if auto and not auto.want_receive(int(now // 15) * 15):
            reasons.append("ft8_transmit_parity")
        if not (mux.get("ok") and mux.get("sdr_ok") and mux.get("fifo_connected")):
            reasons.append("sdrmux_unhealthy")
        if not dump1090_fresh():
            reasons.append("dump1090_stale")
        if hz is None:
            reasons.append("no_model")
        if snap["active_60s"] > 0:
            reasons.append("aircraft_active")
        if snap["quiet_s"] < a.min_quiet:
            reasons.append("not_quiet_long_enough")
        if exp_hidden > a.risk_max:
            reasons.append("risk_above_max")
        if budget.risk + exp_hidden > a.day_risk:
            reasons.append("day_risk_budget")
        if budget.hour_risk.get(hour, 0.0) + exp_hidden > a.hour_risk:
            reasons.append("hour_risk_budget")
        if budget.blind + D > a.day_blind:
            reasons.append("day_blind_budget")
        if budget.per_hour.get(hour, 0) >= a.max_per_hour:
            reasons.append("hour_cap")
        approve = not reasons
        rec = dict(t=now, mode=a.mode, hour=hour, approve=approve, reasons=reasons, D=D,
                   exp_hidden=round(exp_hidden, 6),
                   exp_hidden_mean=round(hz.expected_hidden(now, D, False), 6) if hz else None,
                   p_any_new=round(hz.p_any_new(now, D), 5) if hz else None,
                   budget_risk=round(budget.risk, 5), budget_blind=round(budget.blind, 1),
                   **{k: round(v, 1) for k, v in snap.items()})
        if approve and a.mode == "live":
            try:
                res = sdrmux({"cmd": "listen", "freq": int(a.freq), "seconds": a.listen,
                              "tag": "sched"})
            except Exception as e:
                res = {"ok": False, "error": str(e)}
            rec["window"] = res
            if res.get("ok"):
                budget.spend(exp_hidden, res["off_1090_s"], hour)
                w0, w1 = res["t_start"], res["t_start"] + res["off_1090_s"]
                threading.Thread(target=follow_up, args=(tr, w0, w1, a, rec), daemon=True).start()
                if auto:
                    # Start only if two more RX windows fit the same risk budgets.
                    can_start = (budget.risk + 2 * exp_hidden <= a.day_risk
                                 and budget.hour_risk.get(hour, 0) + 2 * exp_hidden <= a.hour_risk
                                 and budget.blind + 2 * D <= a.day_blind
                                 and budget.per_hour.get(hour, 0) + 2 <= a.max_per_hour)
                    auto.decoding.set()
                    threading.Thread(target=decode_then_post,
                                     args=(auto, res["file"], a.post_hook, can_start), daemon=True).start()
                elif os.path.exists(a.post_hook):
                    subprocess.Popen(["/bin/sh", a.post_hook, res["file"]],
                                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        write(a.out, "decisions", rec)


def decode_then_post(auto, path, hook, can_start):
    try:
        auto.receive(path, can_start)
    except Exception:
        log.exception("FT8 receive decode failed for %s", path)
        return                       # keep IQ for diagnosis; don't invent an empty receive slot
    finally:
        auto.decoding.clear()
    if os.path.exists(hook):
        subprocess.Popen(["/bin/sh", hook, path, "--skip-ft8"],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def follow_up(tr, w0, w1, a, rec):
    """Q3 evidence for a real window: aircraft whose visit started within 120 s after it."""
    time.sleep(125)
    after = tr.appeared(w1, w1 + 120)
    write(a.out, "windows", dict(t_start=w0, t_end=w1, exp_hidden=rec["exp_hidden"],
                                 appeared_after_120s=after, n_appeared_after=len(after)))


def write(out, kind, rec):
    path = os.path.join(out, f"{kind}-{time.strftime('%Y%m%d')}.jsonl")
    with open(path, "a") as f:
        f.write(json.dumps(rec) + "\n")


if __name__ == "__main__":
    main()
