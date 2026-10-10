#!/usr/bin/env python3
"""Tiny client for sdrmux's control socket.

  sdrmuxctl.py status
  sdrmuxctl.py listen <freq_hz> <seconds> [--gain DB] [--tag TEXT]
  sdrmuxctl.py set [--gain DB|agc] [--rtl-agc on|off]      (1090 MHz settings)
"""
import argparse
import json
import socket
import sys

SOCK = "/run/sdrmux/ctl.sock"


def call(req, sock=SOCK, timeout=200):
    s = socket.socket(socket.AF_UNIX)
    s.settimeout(timeout)
    s.connect(sock)
    s.sendall((json.dumps(req) + "\n").encode())
    return json.loads(s.makefile().readline())


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["status", "listen", "set"])
    ap.add_argument("freq", nargs="?", type=float)
    ap.add_argument("seconds", nargs="?", type=float)
    ap.add_argument("--gain")
    ap.add_argument("--rtl-agc", choices=["on", "off"])
    ap.add_argument("--tag", default="")
    a = ap.parse_args()
    if a.cmd == "status":
        req = {"cmd": "status"}
    elif a.cmd == "set":
        req = {"cmd": "set"}
        if a.gain is not None:
            req["gain"] = None if a.gain == "agc" else float(a.gain)
        if a.rtl_agc is not None:
            req["rtl_agc"] = a.rtl_agc == "on"
    else:
        if a.freq is None or a.seconds is None:
            sys.exit("listen needs freq and seconds")
        req = {"cmd": "listen", "freq": int(a.freq), "seconds": a.seconds,
               "gain": None if a.gain in (None, "agc") else float(a.gain), "tag": a.tag}
    print(json.dumps(call(req), indent=1))


if __name__ == "__main__":
    main()
