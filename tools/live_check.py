#!/usr/bin/env python3
"""Check hcdisplay's live-protocol mirror against HamClock's own framebuffer.

Runs the display's Link + Mirror headless on the read-only live port and, every few seconds,
compares the mirror with get_capture.bmp over the map (slowly changing, so both snapshots
agree unless the mirror is wrong). Also checks every update frame for consistency: the PNG
strip must be blok_w * (total blocks in the header) wide and blok_h tall.

usage: live_check.py [--seconds 300] [--every 5]
"""
import argparse
import io
import os
import queue
import sys
import time
import urllib.request

os.environ["SDL_VIDEODRIVER"] = "dummy"
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "hamclock"))
import pygame                                    # noqa: E402
from PIL import Image, ImageChops                # noqa: E402

from hcdisplay import Link, Mirror, decode       # noqa: E402

MAP = (540, 300, 1600, 950)                      # x0, y0, x1, y1 checked: the map (1600x960 px)


def capture():
    raw = urllib.request.urlopen("http://127.0.0.1:8070/get_capture.bmp", timeout=20).read()
    return Image.open(io.BytesIO(raw)).convert("RGB")


def frame_ok(header, png):
    bw, bh, n = header[0], header[1], header[2] << 8 | header[3]
    blocks = sum(header[6 + 3 * i] for i in range(n))
    w, h = Image.open(io.BytesIO(png)).size if n else (0, 0)
    return (not n) or (len(header) == 4 + 3 * n and w == bw * blocks and h == bh), (n, blocks, w, h)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--seconds", type=float, default=300)
    ap.add_argument("--every", type=float, default=5)
    a = ap.parse_args()
    pygame.init()
    frames = queue.Queue()
    Link(frames, url="ws://127.0.0.1:8072/live-ws", raw=True).start()
    mirror = Mirror()
    t0 = nxt = time.time()
    n_frames = n_full = 0
    while time.time() - t0 < a.seconds:
        while not frames.empty():
            f = frames.get()
            if f[0] == "full":
                n_full += 1
                print(f"{time.time() - t0:6.1f}s full image", flush=True)
            else:
                ok, info = frame_ok(f[1], f[2])
                if not ok:
                    print(f"{time.time() - t0:6.1f}s BAD FRAME n,blocks,w,h={info} hdr={len(f[1])}B",
                          flush=True)
            header, png = (None, f[1]) if f[0] == "full" else f[1:]
            mirror.apply(*decode(header, png))
            n_frames += 1
        if time.time() >= nxt and mirror.image:
            nxt = time.time() + a.every
            ours = Image.frombytes("RGB", mirror.image.get_size(),
                                   pygame.image.tobytes(mirror.image, "RGB"))
            try:
                theirs = capture()
            except OSError:
                print(f"{time.time() - t0:6.1f}s HamClock not reachable", flush=True)
                continue
            diff = ImageChops.difference(ours.crop(MAP), theirs.crop(MAP)).convert("L")
            bad = sum(1 for v in diff.getdata() if v > 60)
            if bad > 1000 and not os.path.exists("/tmp/live_ours.png"):
                ours.save("/tmp/live_ours.png")              # first big mismatch, for a look
                theirs.save("/tmp/live_theirs.png")
            print(f"{time.time() - t0:6.1f}s frames {n_frames} full {n_full} map pixels differing {bad}",
                  flush=True)
        time.sleep(0.02)

    # referee: a brand-new connection's full image is exactly what the live server shows now
    import websocket
    ws = websocket.create_connection("ws://127.0.0.1:8072/live-ws", timeout=20)
    ws.send("get_live.png?")
    while True:
        opcode, data = ws.recv_data()
        if opcode == websocket.ABNF.OPCODE_BINARY and data[:4] == b"\x89PNG":
            break
    ws.close()
    fresh = Image.open(io.BytesIO(data)).convert("RGB")
    ours = Image.frombytes("RGB", mirror.image.get_size(), pygame.image.tobytes(mirror.image, "RGB"))
    for name, img in (("mirror", ours), ("get_capture", capture())):
        diff = ImageChops.difference(img.crop(MAP), fresh.crop(MAP)).convert("L")
        print(f"fresh full image vs {name}: map pixels differing {sum(1 for v in diff.getdata() if v > 60)}")


if __name__ == "__main__":
    main()
