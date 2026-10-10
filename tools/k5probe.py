#!/usr/bin/env python3
"""Read-only probe of a Quansheng UV-K5 on the K1 programming cable.

1. listen passively (a radio in bootloader/flash mode broadcasts 0x0518 on its own)
2. send one firmware "hello" (0x0514) and decode the reply (0x0515 = firmware version)
Never writes EEPROM or flash, never keys the transmitter.

usage: k5probe.py [/dev/ttyUSB0] [--baud 38400]
"""
import argparse
import struct
import time

import serial

XOR = bytes([0x16, 0x6C, 0x14, 0xE6, 0x2E, 0x91, 0x0D, 0x40,
             0x21, 0x35, 0xD5, 0x40, 0x13, 0x03, 0xE9, 0x80])


def crc16(data):
    crc = 0
    for b in data:
        crc ^= b << 8
        for _ in range(8):
            crc = ((crc << 1) ^ 0x1021) if crc & 0x8000 else (crc << 1)
            crc &= 0xFFFF
    return crc


def xor(data):
    return bytes(b ^ XOR[i % 16] for i, b in enumerate(data))


def frame(payload):
    body = payload + struct.pack("<H", crc16(payload))
    return b"\xAB\xCD" + struct.pack("<H", len(payload)) + xor(body) + b"\xDC\xBA"


def parse(buf):
    """Yield (cmd, data) for every complete frame in buf."""
    i = 0
    while True:
        i = buf.find(b"\xAB\xCD", i)
        if i < 0 or i + 4 > len(buf):
            return
        n = struct.unpack("<H", buf[i + 2:i + 4])[0]
        end = i + 4 + n + 2 + 2
        if end > len(buf):
            return
        plain = xor(buf[i + 4:i + 4 + n + 2])
        cmd, ln = struct.unpack("<HH", plain[:4])
        yield cmd, plain[4:4 + ln]
        i = end


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("port", nargs="?", default="/dev/ttyUSB0")
    ap.add_argument("--baud", type=int, default=38400)
    a = ap.parse_args()
    with serial.Serial(a.port, a.baud, timeout=0.2) as s:
        s.reset_input_buffer()
        t_end, rx = time.time() + 3, b""
        while time.time() < t_end:
            rx += s.read(256)
        print(f"passive 3 s: {len(rx)} bytes {rx[:48].hex()}")
        for cmd, data in parse(rx):
            print(f"  frame 0x{cmd:04x}: {data[:40].hex()}  {data[16:40]!r}")
        hello = struct.pack("<HHI", 0x0514, 4, 0x6457396A)
        s.write(frame(hello))
        t_end, rx = time.time() + 1.5, b""
        while time.time() < t_end:
            rx += s.read(256)
        print(f"hello reply: {len(rx)} bytes {rx[:64].hex()}")
        for cmd, data in parse(rx):
            txt = data[:16].split(b"\0")[0].decode("ascii", "replace")
            print(f"  frame 0x{cmd:04x} len {len(data)}: version/text {txt!r} raw {data[:32].hex()}")


if __name__ == "__main__":
    main()
