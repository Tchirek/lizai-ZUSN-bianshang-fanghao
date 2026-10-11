#!/usr/bin/env python3
"""Filtering DX-cluster proxy for HamClock.

HamClock connects to 127.0.0.1:7300 instead of the cluster; every byte it sends (login,
show/dx) goes upstream unchanged, and spot lines coming back are only forwarded if they
are relevant to this station:
  * frequency >= 50 MHz (VHF and up), or
  * the DX (spotted) station is in Asia-Pacific (prefix list below), or
  * the spotter is a Chinese station (B*)
Everything that is not a spot (banners, prompts, replies) passes through untouched.
Undo: curl '127.0.0.1:8070/set_cluster?host=k1ttt.net&port=7373'

usage: dxc_filter.py [--listen 127.0.0.1:7300] [--upstream k1ttt.net:7373]
"""
import argparse
import asyncio
import logging
import re

log = logging.getLogger("dxc")

ASIA_PAC = (
    # China (all B prefixes), Hong Kong, Macau
    "B", "VR", "XX9",
    # Japan, Korea
    "JA", "JD", "JE", "JF", "JG", "JH", "JI", "JJ", "JK", "JL", "JM", "JN", "JO", "JP", "JQ",
    "JR", "JS", "7J", "7K", "7L", "7M", "7N", "8J", "8N", "HL", "DS", "DT", "6K", "6L", "6M",
    "6N", "D7", "D8", "D9", "P5",
    # South-East Asia
    "HS", "E2", "XV", "3W", "XU", "XW", "XY", "XZ", "9M", "9W", "9V", "YB", "YC", "YD", "YE",
    "YF", "YG", "YH", "8A", "8B", "8C", "8D", "8E", "8F", "8G", "8H", "8I", "DU", "DV", "DW",
    "DX", "DY", "DZ", "4D", "4E", "4F", "4G", "4H", "4I", "V8", "4W",
    # Mongolia, Central/South Asia neighbours
    "JT", "JU", "JV", "UN", "UP", "UQ", "EX", "EY", "EZ", "UK", "9N", "A5", "S2", "VU", "AT",
    "8Q", "4S", "AP",
    # Oceania
    "VK", "ZL", "P2", "H4", "YJ", "FK", "FO", "KH2", "KH0", "T8", "V6", "V7", "3D2", "5W",
    "A3", "E5", "ZK", "T2", "T3",
)
DX_DE = re.compile(r"^DX de\s+(\S+?):?\s+([\d.]+)\s+(\S+)")
SHOW_DX = re.compile(r"^\s*([\d.]+)\s+(\S+)\s+\d{1,2}-\w{3}-\d{4}\s+\d{4}Z.*<(\S+)>\s*$")


def base_call(call):
    """Strip portable prefixes/suffixes: 'I8/SQ3SWF' -> 'I8', 'EA8DSJ/P' -> 'EA8DSJ'."""
    parts = [p for p in call.upper().split("/") if p]
    if not parts:
        return ""
    if len(parts) > 1 and len(parts[0]) <= 3 and any(c.isdigit() for c in parts[0]):
        return parts[0]                                   # operating-country prefix first
    return max(parts, key=len)


def wanted(freq_khz, dx, spotter):
    try:
        if float(freq_khz) >= 50_000:
            return True
    except ValueError:
        return True
    if base_call(dx).startswith(ASIA_PAC):
        return True
    return base_call(spotter).startswith("B")


def keep_line(line):
    text = line.decode("latin-1", "replace")
    m = DX_DE.match(text)
    if m:
        spotter, freq, dx = m.groups()
        return wanted(freq, dx, spotter)
    m = SHOW_DX.match(text)
    if m:
        freq, dx, spotter = m.groups()
        return wanted(freq, dx, spotter)
    return True


async def pump_up(reader, writer):
    while data := await reader.read(4096):
        writer.write(data)
        await writer.drain()


async def pump_down(reader, writer, stats):
    buf = b""
    while data := await reader.read(4096):
        buf += data
        lines = buf.split(b"\n")
        buf = lines.pop()                       # keep partial last line
        out = []
        for ln in lines:
            if keep_line(ln):
                out.append(ln)
                stats["kept"] += 1
            else:
                stats["dropped"] += 1
        if out:
            writer.write(b"\n".join(out) + b"\n")
        if buf.rstrip(b"\r").endswith((b": ", b"> ", b":", b">")) and len(buf) < 200:
            writer.write(buf)                   # prompts like 'Please enter your call: '
            buf = b""
        await writer.drain()


async def handle(c_reader, c_writer, upstream):
    host, port = upstream
    stats = {"kept": 0, "dropped": 0}
    peer = c_writer.get_extra_info("peername")
    try:
        u_reader, u_writer = await asyncio.wait_for(asyncio.open_connection(host, port), 20)
    except (OSError, asyncio.TimeoutError) as e:
        log.warning("upstream %s:%s failed: %s", host, port, e)
        c_writer.close()
        return
    log.info("client %s <-> %s:%s", peer, host, port)
    tasks = [asyncio.create_task(pump_up(c_reader, u_writer)),
             asyncio.create_task(pump_down(u_reader, c_writer, stats))]
    await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    for t in tasks:
        t.cancel()
    for w in (u_writer, c_writer):
        w.close()
    log.info("closed %s: kept %d spots/lines, dropped %d", peer, stats["kept"], stats["dropped"])


async def main_async(a):
    lh, lp = a.listen.rsplit(":", 1)
    uh, up = a.upstream.rsplit(":", 1)
    srv = await asyncio.start_server(lambda r, w: handle(r, w, (uh, int(up))), lh, int(lp))
    log.info("listening on %s -> %s", a.listen, a.upstream)
    async with srv:
        await srv.serve_forever()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--listen", default="127.0.0.1:7300")
    ap.add_argument("--upstream", default="k1ttt.net:7373")
    a = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    asyncio.run(main_async(a))


if __name__ == "__main__":
    main()
