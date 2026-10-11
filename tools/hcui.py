#!/usr/bin/env python3
"""Drive HamClock's UI remotely (what its live web page does): tap, type, screenshot.

  hcui.py shot OUT.png [x0 y0 x1 y1]     screenshot (1600x960 capture, optional crop)
  hcui.py tap X Y                         tap at base (800x480) coordinates
  hcui.py type TEXT                       type characters (Space handled)
  hcui.py key NAME                        Enter, Tab, Backspace, Escape, ArrowUp, ...
  hcui.py bootshots DIR [secs]            restart HamClock, screenshot every second
"""
import io
import os
import sys
import time
import urllib.request

REST = "http://127.0.0.1:8070"
WS = "ws://127.0.0.1:8071/live-ws"


def get(path, timeout=10):
    return urllib.request.urlopen(REST + path, timeout=timeout).read()


def shot(out, box=None):
    from PIL import Image
    im = Image.open(io.BytesIO(get("/get_capture.bmp", 20)))
    (im.crop(box) if box else im).save(out)


def tap(x, y):
    return get(f"/set_touch?x={int(x)}&y={int(y)}").decode().strip()


def send_keys(keys):
    import websocket
    ws = websocket.create_connection(WS, timeout=10)
    for k in keys:
        ws.send(f"set_char?char={'Space' if k == ' ' else k}&mod=")
        time.sleep(0.15)
    ws.close()


def main():
    cmd, args = sys.argv[1], sys.argv[2:]
    if cmd == "shot":
        shot(args[0], tuple(map(int, args[1:5])) if len(args) >= 5 else None)
    elif cmd == "tap":
        print(tap(args[0], args[1]))
    elif cmd == "type":
        send_keys(list(" ".join(args)))
    elif cmd == "key":
        send_keys(args)
    elif cmd == "bootshots":
        d, secs = args[0], int(args[1]) if len(args) > 1 else 12
        os.makedirs(d, exist_ok=True)
        try:
            get("/restart", 5)
        except OSError:
            pass
        t0 = time.time()
        while time.time() - t0 < 30:
            try:
                get("/get_sys.txt", 2)
                break
            except OSError:
                time.sleep(0.3)
        for i in range(secs):
            try:
                shot(os.path.join(d, f"boot{i:02d}.png"))
            except OSError as e:
                print("shot failed", e)
            time.sleep(1)
        print(f"rest up after {time.time() - t0:.1f}s")


if __name__ == "__main__":
    main()
