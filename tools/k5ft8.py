#!/usr/bin/env python3
"""CEC 0.3V FT8 over an ordinary programming cable (no audio interface).

Protocol source: phdlee/uvk5cec ceccommon.c and cecdigital.c, commit f17c0a5.
The radio must already show FT8/Ready (long-press MENU). A complete 79-tone
packet starts one finite transmission; the firmware returns to RX afterwards.
"""
import argparse
import fcntl
import json
import os
import re
import struct
import subprocess
import time

import serial

STX = bytes.fromhex("59575873")
ETX = bytes.fromhex("95758537")
PORT = "/dev/serial/by-id/usb-Prolific_Technology_Inc._USB-Serial_Controller_D-if00-port0"
DIALS = (144_174_000, 144_460_000)


class MissedSlot(RuntimeError):
    pass


def packet(cmd, data=b"\0" * 5, payload=b""):
    if len(data) != 5 or len(payload) > 110:
        raise ValueError("CEC header needs five bytes; payload limit is 110")
    return STX + bytes([cmd]) + struct.pack("<H", len(payload)) + data + ETX + payload


def encode(message):
    out = subprocess.run(["ft8code", message], check=True, capture_output=True,
                         text=True, timeout=5).stdout
    if "Channel symbols (79 tones):" not in out or re.search(r"\*|Cannot|invalid", out, re.I):
        raise ValueError(f"FT8 encoder rejected {message!r}")
    tones = "".join(re.findall(r"[0-7]+", out.split("Channel symbols (79 tones):", 1)[1]))
    if len(tones) != 79 or any(tones[i:i+7] != "3140652" for i in (0, 36, 72)):
        raise ValueError("unexpected ft8code output")
    return bytes(map(int, tones))


def tx_packet(tones, offset_hz=1200):
    if len(tones) != 79 or any(t > 7 for t in tones):
        raise ValueError("FT8 needs exactly 79 symbols in 0..7")
    if offset_hz % 10 or not 200 <= offset_hz <= 2800:
        raise ValueError("offset must be 200..2800 Hz in 10 Hz steps")
    return packet(0x35, struct.pack(">H", offset_hz // 10) + b"\0\0\0", tones)


def wait_until(when):
    deadline = time.monotonic() + when - time.time()
    while (left := deadline - time.monotonic()) > 0:
        time.sleep(min(left, 0.1))


class Radio:
    def __init__(self, port=PORT):
        lockdir = os.path.expanduser("~/influ/radio")
        os.makedirs(lockdir, exist_ok=True)
        self.lock = open(os.path.join(lockdir, "serial.lock"), "w")
        fcntl.flock(self.lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        self.s = serial.Serial(port, 57600, timeout=0.1, write_timeout=1, exclusive=True)

    def mode(self):
        self.s.reset_input_buffer()
        self.s.write(packet(0x21, b"\x01\0\0\0\0"))
        deadline, buf = time.monotonic() + 1, b""
        while time.monotonic() < deadline:
            buf += self.s.read(128)
            while (start := buf.find(STX)) >= 0 and len(buf) >= start + 16:
                h, buf = buf[start:start + 16], buf[start + 16:]
                if h[4:7] == b"\x21\0\0" and h[7] == 2 and h[12:16] == ETX:
                    return "FT8" if h[8] == 2 else "other"
        raise RuntimeError("no CEC mode response: radio must show FT8/Ready")

    def configure(self, dial):
        if dial not in DIALS:
            raise ValueError("only the two verified 2 m FT8 dials are supported")
        if self.mode() != "FT8":
            raise RuntimeError("radio is outside FT8 mode")
        self.s.write(packet(0x32, struct.pack("<I", dial // 10) + b"\0"))
        self.s.flush()
        time.sleep(0.1)
        if self.mode() != "FT8":
            raise RuntimeError("radio left FT8 mode while setting frequency")

    def transmit(self, message, slot, offset_hz=1200):
        tones = encode(message)
        target = slot + 0.45
        if time.time() > target + 0.08:
            raise MissedSlot("missed FT8 start; refusing a late transmission")
        if self.mode() != "FT8":
            raise RuntimeError("radio left FT8 mode")
        if time.time() > target + 0.08:
            raise MissedSlot("mode query missed FT8 start; skipping this slot")
        wait_until(target)
        self.s.write(tx_packet(tones, offset_hz))
        self.s.flush()
        # Firmware cannot process a stop command inside its blocking symbol loop.
        # Never queue another TX: wait for completion and a fresh mode response.
        wait_until(slot + 14)
        if self.mode() != "FT8":
            raise RuntimeError("radio failed to return to FT8 receive mode")
        return dict(message=message, slot=slot, offset_hz=offset_hz, tones=list(tones))

    def close(self):
        self.s.close()
        self.lock.close()


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("action", choices=["probe", "send"])
    ap.add_argument("--port", default=PORT)
    ap.add_argument("--dial", type=int, default=DIALS[0])
    ap.add_argument("--message", default="CQ BG8ASN OM20")
    a = ap.parse_args()
    r = Radio(a.port)
    try:
        r.configure(a.dial)
        if a.action == "probe":
            print(json.dumps(dict(mode=r.mode(), dial=a.dial)))
        else:
            slot = (int(time.time() / 15) + 1) * 15
            print(json.dumps(r.transmit(a.message, slot)))
    finally:
        r.close()


if __name__ == "__main__":
    main()
