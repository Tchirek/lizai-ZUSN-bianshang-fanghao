#!/usr/bin/env python3
"""FT8 auto-sequence for CEC firmware, driven by budget-approved SDR windows.

No independent SDR owner or risk budget. Receive work finishes before the next
eligible transmit slot; missed slots are skipped. Only confirmed exchanges go
to ADIF. Exiting Ready on the radio disables transmission immediately after the
current finite 79-symbol frame.
"""
import json
import logging
import os
import re
import sys
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "tools"))
sys.path.insert(0, os.path.join(HERE, "..", "sdrmux"))
import ft8
import k5ft8

log = logging.getLogger("ft8-auto")
CALL = re.compile(r"(?=.{3,11}$)[A-Z0-9]*[A-Z][0-9][A-Z0-9]*[A-Z](?:/[A-Z0-9]+)?$")
GRID = re.compile(r"[A-R]{2}[0-9]{2}$")
REPORT = re.compile(r"R?([+-][0-9]{2})$")


def is_grid(text):
    # RR73 also fits Maidenhead syntax; FT8 reserves it for the final acknowledgement.
    return text != "RR73" and bool(GRID.fullmatch(text))


class QSO:
    def __init__(self, call, grid):
        if not CALL.fullmatch(call) or not GRID.fullmatch(grid[:4]):
            raise ValueError("invalid station call or grid")
        self.call, self.grid = call, grid[:4]
        self.peer = None
        self.pending = None
        self.rx_parity = None
        self.started = 0
        self.last_tx = -1
        self.tries = 0
        self.sent_report = ""
        self.received_report = ""
        self.peer_grid = ""
        self.peer_df = 0
        self.finish_after_tx = False
        self.completed = []
        self.last_decode = -1
        self.last_sent = ""
        self.recent = {}

    def reset(self):
        self.peer, self.pending, self.rx_parity = None, None, None
        self.finish_after_tx = False
        self.tries = 0
        self.last_sent = ""

    def receive(self, slot, decodes):
        if slot <= self.last_decode:
            return
        self.last_decode = slot
        if self.peer and slot - self.started > 240:
            log.info("QSO timeout: %s", self.peer)
            self.reset()
        directed, cq = [], []
        for d in sorted(decodes, key=lambda v: v["snr"], reverse=True):
            parts = d["message"].strip().split()
            if len(parts) != 3:
                continue
            dst, src, body = parts
            if (src == self.call or not CALL.fullmatch(src)
                    or (not self.peer and slot - self.recent.get(src, -1e9) < 600)):
                continue
            if self.peer and src != self.peer:
                continue
            if dst == self.call:
                directed.append((src, body, d["snr"], d.get("df", 0)))
            elif dst == "CQ" and is_grid(body):
                cq.append((src, body, d["snr"], d.get("df", 0)))
        choices = directed or (cq if not self.peer else [])
        if not choices:
            return
        src, body, snr, self.peer_df = choices[0]
        if not self.peer:
            # Reports without an exchange history cannot establish a new QSO.
            if not is_grid(body) or slot - self.recent.get(src, -1e9) < 600:
                return
            self.peer, self.started = src, slot
            self.peer_grid = body
            self.rx_parity = int(slot // 15) % 2
            self.sent_report, self.received_report = "", ""
            self.last_sent = ""
        if is_grid(body):
            self.peer_grid = body
            if directed:
                self.sent_report = f"{max(-50, min(49, snr)):+03d}"
                reply = self.sent_report
            else:
                reply = self.grid
        elif m := REPORT.fullmatch(body):
            self.received_report = m[1]
            if body.startswith("R"):
                if not self.sent_report or not REPORT.fullmatch(self.last_sent):
                    return
                reply = "RR73"
                self.finish_after_tx = True
            else:
                self.sent_report = f"{max(-50, min(49, snr)):+03d}"
                reply = "R" + self.sent_report
        elif (body in ("RRR", "RR73") and self.sent_report and self.received_report
              and REPORT.fullmatch(self.last_sent)):
            reply = "73"
            self.finish_after_tx = True
        elif body == "73" and self.sent_report and self.received_report:
            # A duplicate final acknowledgement does not create another record.
            return
        else:
            return
        new = f"{self.peer} {self.call} {reply}"
        if new != self.pending:
            self.tries = 0
        self.pending = new

    def due(self, slot):
        if self.peer and slot - self.started > 240:
            self.reset()
        if not self.pending or self.tries >= 3:
            return None
        if int(slot // 15) % 2 == self.rx_parity or slot <= self.last_tx:
            return None
        # A decoded slot must have finished before its reply can be transmitted.
        if slot <= self.last_decode:
            return None
        return self.pending

    def transmitted(self, slot):
        self.last_sent = self.pending.split()[-1]
        self.last_tx, self.tries = slot, self.tries + 1
        if self.finish_after_tx:
            self.recent[self.peer] = slot
            self.completed.append(dict(call=self.peer, grid=self.peer_grid,
                                       rst_sent=self.sent_report, rst_rcvd=self.received_report,
                                       start=self.started, end=slot + 13.2,
                                       df=self.peer_df, rx_parity=self.rx_parity))
            self.reset()
        elif self.tries >= 3:
            log.info("QSO retry limit: %s", self.peer)
            self.reset()


def adif(qso, station_call, station_grid, dial):
    def field(name, value):
        value = str(value)
        return f"<{name}:{len(value)}>{value}"
    start, end = time.gmtime(qso["start"]), time.gmtime(qso["end"])
    values = dict(CALL=qso["call"], GRIDSQUARE=qso["grid"], STATION_CALLSIGN=station_call,
                  MY_GRIDSQUARE=station_grid, QSO_DATE=time.strftime("%Y%m%d", start),
                  TIME_ON=time.strftime("%H%M%S", start), QSO_DATE_OFF=time.strftime("%Y%m%d", end),
                  TIME_OFF=time.strftime("%H%M%S", end), BAND="2m", MODE="FT8",
                  FREQ=f"{dial / 1e6:.6f}", RST_SENT=qso["rst_sent"], RST_RCVD=qso["rst_rcvd"])
    return " ".join(field(k, v) for k, v in values.items()) + " <EOR>\n"


class Auto:
    def __init__(self, port, calibration, out, dial=144_174_000, llm=False):
        cal = json.load(open(calibration))
        if not cal.get("pass_decode") or cal.get("sent", {}).get("offset_hz") != 1200:
            raise ValueError("a successful real TX/RX calibration is required")
        if any(abs(s - 6.25) > 1 for s in cal["spacings_hz"]):
            raise ValueError("tone calibration exceeds 1 Hz spacing tolerance")
        st = ft8.station()
        if not any(d["message"] == f"CQ {st['call']} {st['grid'][:4]}"
                   for rec in cal["decoded"] for d in rec["decodes"]):
            raise ValueError("calibration station identity does not match")
        self.call, self.grid = st["call"], st["grid"]
        self.qso = QSO(self.call, self.grid)
        self.lock = threading.Lock()
        self.decoding = threading.Event()
        self.chat = None
        if llm:
            from ft8_llm import Chat
            self.chat = Chat(self.call, self.write)
        self.radio = k5ft8.Radio(port)
        self.radio.configure(dial)
        self.dial, self.out = dial, out
        self.last_cq = 0
        os.makedirs(out, exist_ok=True)

    def write(self, event, **values):
        rec = dict(t=time.time(), event=event, **values)
        with open(os.path.join(self.out, "auto.jsonl"), "a") as f:
            f.write(json.dumps(rec) + "\n")
        log.info("%s %s", event, values)

    def receive(self, path, can_start=True):
        meta = json.load(open(os.path.splitext(path)[0] + ".json"))
        for rec in ft8.decode_recording(path, meta, dials=[self.dial]):
            with self.lock:
                if self.qso.peer or can_start:
                    self.qso.receive(rec["slot"], rec["decodes"])
                chatting = self.chat is not None and self.chat.active(rec["slot"])
                if chatting and not self.qso.peer:
                    self.chat.receive(rec)
                # CQ only follows an actually approved RX window, never blind.
                if can_start and not chatting and not self.qso.peer and rec["slot"] - self.last_cq >= 120:
                    self.qso.rx_parity = int(rec["slot"] // 15) % 2
                    self.qso.pending = f"CQ {self.call} {self.grid[:4]}"
                    self.qso.started = rec["slot"]
                    self.qso.tries = 0
            self.write("receive", slot=rec["slot"], decodes=rec["decodes"])

    def want_receive(self, slot):
        if self.decoding.is_set():
            return False
        with self.lock:
            if self.qso.pending and self.qso.last_tx < self.qso.last_decode:
                return False            # send the decoded reply before spending another RX window
            parity = self.qso.rx_parity
            if self.chat and not self.qso.peer and self.chat.active(slot):
                parity = self.chat.peer["rx_parity"]
            return parity is None or int(slot // 15) % 2 == parity

    def in_qso(self, slot):
        with self.lock:
            if self.qso.pending and slot - self.qso.started > 240:
                self.qso.reset()
            return bool(self.qso.peer and (self.qso.last_tx >= self.qso.started
                        or 0 <= self.qso.started - self.last_cq <= 240))

    def transmit(self, slot):
        if self.decoding.is_set():
            return False                # never repeat an old message while its reply is decoding
        with self.lock:
            message = self.qso.due(slot)
            chat_reply = False
            if not message and self.chat and not self.qso.peer:
                message = self.chat.due(slot)
                chat_reply = message is not None
            if not message:
                return False
            try:
                if chat_reply:
                    self.radio.transmit(message, slot, free_text=True)
                else:
                    self.radio.transmit(message, slot)
            except k5ft8.MissedSlot:
                self.write("skip_late", slot=slot, message=message)
                return False
            except ValueError:
                if not chat_reply:
                    raise
                self.chat.future = None
                self.write("llm_error", error="invalid_free_text_encoding")
                return False
            if chat_reply:
                self.chat.sent(message)
            elif message.startswith("CQ "):
                self.last_cq = slot
                self.qso.pending = None
            else:
                self.qso.transmitted(slot)
            self.write("transmit", slot=slot, message=message, dial=self.dial, offset_hz=1200)
            for qso in self.qso.completed:
                with open(os.path.join(self.out, "qsos.adi"), "a") as f:
                    f.write(adif(qso, self.call, self.grid, self.dial))
                self.write("qso", **qso)
                if self.chat:
                    self.chat.start(qso)
            self.qso.completed.clear()
            return True

    def close(self):
        if self.chat:
            self.chat.close()
        self.radio.close()
