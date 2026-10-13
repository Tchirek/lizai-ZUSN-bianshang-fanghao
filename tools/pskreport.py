#!/usr/bin/env python3
"""Report the FT8 the station hears to PSK Reporter, as WSJT-X does.

Every decode sdrmux/ft8.py logs (~/influ/ft8/decodes-YYYYMMDD.jsonl) that names its sender
becomes one spot: the sender's call, its grid when the message carries one, the RF frequency,
SNR and slot time, heard by us (station.json: call and 6-character grid). The spots go in one
IPFIX datagram to report.pskreporter.info:4739, the format PSK Reporter publishes for
reporting programs. A sender is spotted once a run, at its best SNR; our own signal is never
spotted. With nothing heard the run still sends our receiver record now and then, so the
station shows among PSK Reporter's active monitors.

Run every five minutes (deploy/influ-pskreport.timer); what it has sent is kept in
~/influ/ft8/pskreport.json.

usage: pskreport.py [--test] [--dry-run]
       --test sends to PSK Reporter's test port (14739), whose reports are not published
"""
import argparse
import json
import os
import random
import re
import socket
import struct
import sys
import time

LOGDIR = os.path.expanduser("~/influ/ft8")
STATION = os.path.expanduser("~/influ/station.json")
STATE = os.path.join(LOGDIR, "pskreport.json")
SERVER = "report.pskreporter.info"
PORT, TEST_PORT = 4739, 14739
SOFTWARE = "influ (jt9)"
HEARTBEAT_S = 1800                   # the receiver record alone, when nothing was heard this long
ENTERPRISE = 30351                   # PSK Reporter's IPFIX enterprise number
RECEIVER_ID, SENDER_ID = 0x9992, 0x9993
CALL = re.compile(r"(?=.{3,11}$)[A-Z0-9]*[A-Z][0-9][A-Z0-9]*[A-Z](?:/[A-Z0-9]+)?$")
GRID = re.compile(r"[A-R]{2}[0-9]{2}$")
VARIABLE = 0xFFFF


def field(element, length=VARIABLE):
    """A PSK Reporter field of a template: its element (enterprise bit set), length, enterprise."""
    return struct.pack(">HHI", 0x8000 | element, length, ENTERPRISE)


# receiverCallsign, receiverLocator, decodingSoftware
RECEIVER_FIELDS = [field(2), field(4), field(8)]
# senderCallsign, frequency, sNR, mode, senderLocator, informationSource, flowStartSeconds (IANA 150)
SENDER_FIELDS = [field(1), field(5, 4), field(6, 1), field(10), field(3), field(11, 1),
                 struct.pack(">HH", 150, 4)]


def text(value):
    raw = value.encode("ascii", "replace")[:254]
    return bytes([len(raw)]) + raw


def ipfix_set(set_id, body):
    body += b"\0" * (-len(body) % 4)
    return struct.pack(">HH", set_id, 4 + len(body)) + body


def packet(receiver, spots, sequence, domain, now):
    """One IPFIX message: both templates, our receiver record and the spots."""
    sets = [ipfix_set(3, struct.pack(">HHH", RECEIVER_ID, len(RECEIVER_FIELDS), 0) + b"".join(RECEIVER_FIELDS)),
            ipfix_set(2, struct.pack(">HH", SENDER_ID, len(SENDER_FIELDS)) + b"".join(SENDER_FIELDS)),
            ipfix_set(RECEIVER_ID, text(receiver["call"]) + text(receiver["grid"]) + text(SOFTWARE))]
    if spots:
        sets.append(ipfix_set(SENDER_ID, b"".join(
            text(s["call"]) + struct.pack(">I", s["hz"]) + struct.pack(">b", max(-128, min(127, s["snr"])))
            + text("FT8") + text(s["grid"]) + b"\x01" + struct.pack(">I", int(s["t"])) for s in spots)))
    body = b"".join(sets)
    return struct.pack(">HHIII", 10, 16 + len(body), int(now), sequence, domain) + body


def sender(message):
    """(call, grid) of who sent an FT8 message, grid '' if it carries none; None if it names
    no sender plainly (hashed <calls>, free text, telemetry)."""
    words = message.upper().split()
    if len(words) < 2:
        return None
    if words[0] == "CQ":
        words = words[1:]
        if len(words) > 1 and not CALL.fullmatch(words[0]) and words[0].isalpha() and len(words[0]) <= 4:
            words = words[1:]                    # CQ DX, CQ NA, ...
        call, rest = words[0], words[1:]
    else:
        call, rest = words[1], words[2:]
    if not CALL.fullmatch(call):
        return None
    grid = rest[0] if rest and GRID.fullmatch(rest[0]) and rest[0] != "RR73" else ""
    return call, grid


def spots_since(after, own, logdir=LOGDIR, now=None):
    """The spots in the decode logs heard after `after` (unix s): one a sender, at its best SNR."""
    now = now or time.time()
    best = {}
    for day in sorted({time.strftime("%Y%m%d", time.gmtime(t)) for t in (now - 86400, now)}):
        path = os.path.join(logdir, f"decodes-{day}.jsonl")
        try:
            with open(path) as f:
                lines = f.readlines()
        except OSError:
            continue
        for line in lines:
            try:
                d = json.loads(line)
                if d["slot"] <= after:
                    continue
                who = sender(d["message"])
                hz = int(d.get("rf_hz") or d["dial"] + d["df"])
            except (ValueError, KeyError, TypeError):
                continue
            if not who or who[0].split("/")[0] == own.split("/")[0]:
                continue
            spot = dict(call=who[0], grid=who[1], hz=hz, snr=int(d["snr"]), t=d["slot"])
            if who[0] not in best or spot["snr"] > best[who[0]]["snr"]:
                best[who[0]] = dict(spot, grid=spot["grid"] or best.get(who[0], {}).get("grid", ""))
    return sorted(best.values(), key=lambda s: s["t"])


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--test", action="store_true", help="send to the test port, nothing published")
    ap.add_argument("--dry-run", action="store_true", help="print what would be sent")
    a = ap.parse_args()
    with open(STATION) as f:
        st = json.load(f)
    if not CALL.fullmatch(st.get("call", "")) or not re.fullmatch(r"[A-R]{2}[0-9]{2}([a-x]{2})?", st.get("grid", "")):
        sys.exit("station.json needs a call and a grid")
    try:
        with open(STATE) as f:
            state = json.load(f)
    except (OSError, ValueError):
        state = {}
    now = time.time()
    state.setdefault("domain", random.getrandbits(32))
    state.setdefault("sequence", 0)
    start = state.get("last_slot", now - 900)
    spots = spots_since(start, st["call"], now=now)
    if not spots and now - state.get("sent_at", 0) < HEARTBEAT_S:
        return
    data = packet(dict(call=st["call"], grid=st["grid"][:6]), spots, state["sequence"], state["domain"], now)
    if a.dry_run:
        print(f"{len(spots)} spots, {len(data)} bytes:", *(f"{s['call']} {s['grid']} {s['hz']} {s['snr']}" for s in spots), sep="\n  ")
        return
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.sendto(data, (SERVER, TEST_PORT if a.test else PORT))
    state.update(sequence=state["sequence"] + len(spots), sent_at=now,
                 last_slot=max([s["t"] for s in spots] + [start]))
    os.makedirs(LOGDIR, exist_ok=True)
    with open(STATE + ".tmp", "w") as f:
        json.dump(state, f)
    os.replace(STATE + ".tmp", STATE)
    print(f"sent {len(spots)} spots", flush=True)


if __name__ == "__main__":
    main()
