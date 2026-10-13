#!/usr/bin/env python3
"""Beacon the station's position to APRS-IS, so it shows on aprs.fi and the like.

One position packet a run, sent over a short APRS-IS login (rotate.aprs2.net:14580). The
position is the station's (station.json) with APRS position ambiguity 2: minutes only, the
hundredths and tenths left blank, about 1.8 km, coarser than the 0.01 deg the station gives
anywhere public. The login needs the call's APRS-IS passcode, which the operator puts in
~/influ/private/aprs.env:

    APRS_PASSCODE=12345
    APRS_SSID=10            (optional: beacon as CALL-10; none by default)
    APRS_COMMENT=...        (optional)

Without that file it does nothing. Run every half hour (deploy/influ-aprs.timer).

usage: aprs_beacon.py [--dry-run]
"""
import argparse
import json
import os
import re
import socket
import sys
import time

STATION = os.path.expanduser("~/influ/station.json")
SECRETS = os.path.expanduser("~/influ/private/aprs.env")
SERVER = ("rotate.aprs2.net", 14580)
TOCALL = "APZINF"                    # APZ: experimental software
COMMENT = "FT8 144.174MHz"


def settings(path=SECRETS):
    out = {}
    try:
        with open(path) as f:
            for line in f:
                key, sep, value = line.strip().partition("=")
                if sep and not key.startswith("#"):
                    out[key.strip()] = value.strip().strip("'\"")
    except OSError:
        pass
    return out


def ambiguous(value, degree_digits, hemispheres):
    """A coordinate as APRS writes it (ddmm.hh / dddmm.hh) to the whole minute, the rest blank."""
    hemi = hemispheres[0] if value >= 0 else hemispheres[1]
    minutes = round(abs(value) * 60)
    return f"{minutes // 60:0{degree_digits}d}{minutes % 60:02d}.  {hemi}"


def position(call, lat, lon, comment):
    return f"{call}>{TOCALL},TCPIP*:!{ambiguous(lat, 2, 'NS')}/{ambiguous(lon, 3, 'EW')}-{comment}"


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dry-run", action="store_true", help="print the packet, send nothing")
    a = ap.parse_args()
    with open(STATION) as f:
        st = json.load(f)
    cfg = settings()
    call = st["call"] + (f"-{cfg['APRS_SSID']}" if cfg.get("APRS_SSID") else "")
    packet = position(call, st["lat"], st["lon"], cfg.get("APRS_COMMENT", COMMENT))
    if a.dry_run:
        print(packet)
        return
    if not re.fullmatch(r"\d{1,5}", cfg.get("APRS_PASSCODE", "")):
        return                                   # no passcode, no beacon
    with socket.create_connection(SERVER, timeout=30) as s:
        f = s.makefile("rwb")
        f.readline()                             # server banner
        f.write(f"user {call} pass {cfg['APRS_PASSCODE']} vers influ 1.0\r\n".encode())
        f.flush()
        reply = f.readline().decode(errors="replace")
        if " verified" not in reply or "unverified" in reply:
            sys.exit(f"APRS-IS login not verified: {reply.strip()}")
        f.write(packet.encode() + b"\r\n")
        f.flush()
        time.sleep(3)                            # let the server take it before the line closes
    print(packet, flush=True)


if __name__ == "__main__":
    main()
