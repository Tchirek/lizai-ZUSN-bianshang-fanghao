#!/usr/bin/env python3
"""sdrmux - one process owns the RTL-SDR and time-shares it between ADS-B and other bands.

  RTL-SDR --async 2.4 MS/s--> sdrmux --UC8 FIFO--> dump1090-fa (--ifile)  --> feeders
                                    +--> ham IQ recordings (during listen windows)

The device never stops streaming. Every sample block becomes exactly one equally long
block on the ADS-B FIFO: real 1090 MHz samples, or zeros (127) while tuned elsewhere or
while the tuner settles. dump1090's 12 MHz sample clock therefore stays continuous and
locked to real time, its TCP outputs never close, and feeders/MLAT stay connected; they
simply receive no messages during a window.

Honesty rule: if the SDR itself fails, sdrmux bridges at most --grace seconds with
zeros while reopening, then exits so the FIFO closes and the outage becomes visible.

Control: line-delimited JSON on a Unix socket (default /run/sdrmux/ctl.sock)
  {"cmd":"status"}
  {"cmd":"listen","freq":145500000,"seconds":20,"gain":null,"tag":"exp1"}
    -> blocks until back on 1090 MHz, returns timing of the switch
"""
import argparse
import ctypes
import ctypes.util
import fcntl
import json
import logging
import os
import queue
import socket
import stat
import threading
import time

ADSB_FREQ = 1_090_000_000
RATE = 2_400_000
BUF_LEN = 65536            # bytes per async buffer = 32768 IQ samples = 13.65 ms
BUF_NUM = 32               # ~0.44 s of USB buffering
BLANK_AFTER_RETUNE = 4     # buffers (~55 ms) zeroed after every retune
MAX_LISTEN = 120.0         # absolute cap per window, seconds
ZERO = b"\x7f" * BUF_LEN   # UC8 mid-scale ~ 0 + 0j
F_SETPIPE_SZ = 1031

log = logging.getLogger("sdrmux")


# ------------------------------------------------------------- librtlsdr ----

_lib = ctypes.CDLL(ctypes.util.find_library("rtlsdr") or "librtlsdr.so.0")
_dev_p = ctypes.c_void_p
_CB = ctypes.CFUNCTYPE(None, ctypes.POINTER(ctypes.c_ubyte), ctypes.c_uint32, ctypes.c_void_p)
for name, args in {
    "rtlsdr_get_device_count": [],
    "rtlsdr_open": [ctypes.POINTER(_dev_p), ctypes.c_uint32],
    "rtlsdr_close": [_dev_p],
    "rtlsdr_set_sample_rate": [_dev_p, ctypes.c_uint32],
    "rtlsdr_set_center_freq": [_dev_p, ctypes.c_uint32],
    "rtlsdr_get_center_freq": [_dev_p],
    "rtlsdr_set_tuner_gain_mode": [_dev_p, ctypes.c_int],
    "rtlsdr_get_tuner_gains": [_dev_p, ctypes.POINTER(ctypes.c_int)],
    "rtlsdr_set_tuner_gain": [_dev_p, ctypes.c_int],
    "rtlsdr_get_tuner_gain": [_dev_p],
    "rtlsdr_set_agc_mode": [_dev_p, ctypes.c_int],
    "rtlsdr_set_freq_correction": [_dev_p, ctypes.c_int],
    "rtlsdr_reset_buffer": [_dev_p],
    "rtlsdr_read_async": [_dev_p, _CB, ctypes.c_void_p, ctypes.c_uint32, ctypes.c_uint32],
    "rtlsdr_cancel_async": [_dev_p],
}.items():
    getattr(_lib, name).argtypes = args
    getattr(_lib, name).restype = ctypes.c_uint32 if name.endswith(("count", "center_freq")) \
        else ctypes.c_int


class SDR:
    def __init__(self, index=0, ppm=0):
        self.dev = _dev_p()
        if _lib.rtlsdr_open(ctypes.byref(self.dev), index) != 0:
            raise OSError("rtlsdr_open failed")
        self.ctl = threading.Lock()
        n = _lib.rtlsdr_get_tuner_gains(self.dev, None)
        arr = (ctypes.c_int * n)()
        _lib.rtlsdr_get_tuner_gains(self.dev, arr)
        self.gains = list(arr)
        _lib.rtlsdr_set_sample_rate(self.dev, RATE)
        if ppm:
            _lib.rtlsdr_set_freq_correction(self.dev, ppm)
        _lib.rtlsdr_reset_buffer(self.dev)

    def set_gain(self, db):
        """db=None -> tuner AGC; otherwise nearest supported manual gain."""
        with self.ctl:
            if db is None:
                _lib.rtlsdr_set_tuner_gain_mode(self.dev, 0)
                return None
            g = min(self.gains, key=lambda x: abs(x - db * 10))
            _lib.rtlsdr_set_tuner_gain_mode(self.dev, 1)
            _lib.rtlsdr_set_tuner_gain(self.dev, g)
            return g / 10

    def tune(self, hz):
        with self.ctl:
            r = _lib.rtlsdr_set_center_freq(self.dev, int(hz))
        if r != 0:
            raise OSError(f"set_center_freq({hz}) -> {r}")

    def run_async(self, cb):
        return _lib.rtlsdr_read_async(self.dev, cb, None, BUF_NUM, BUF_LEN)

    def cancel(self):
        _lib.rtlsdr_cancel_async(self.dev)

    def close(self):
        _lib.rtlsdr_close(self.dev)


# ------------------------------------------------------------------ mux ----

class Mux:
    def __init__(self, a):
        self.a = a
        self.q = queue.Queue(maxsize=400)        # ~5.5 s of samples
        self.lock = threading.Lock()
        self.seq = 0                              # buffers received from USB
        self.mode = "adsb"                        # adsb | ham | blank
        self.blank_until = 0
        self.ham_fh = None
        self.ham_meta = None
        self.window = threading.Lock()            # one listen window at a time
        self.dropped = 0
        self.fifo_fd = None
        self.last_rx = time.monotonic()
        self.stats = dict(buffers=0, zero_buffers=0, windows=0, sdr_errors=0, fifo_reopens=0)
        self.stop = threading.Event()
        self.sdr = None
        self.sdr_ok = threading.Event()
        self._cb = _CB(self._on_samples)          # keep a reference

    # ---- USB thread
    def _on_samples(self, buf, n, _ctx):
        data = ctypes.string_at(buf, n)
        self.last_rx = time.monotonic()
        with self.lock:
            self.seq += 1
            tag = "blank" if self.seq <= self.blank_until else self.mode
        try:
            self.q.put_nowait((tag, data))
        except queue.Full:
            self.dropped += 1

    # ---- control
    def retune(self, hz, gain_db, new_mode):
        with self.lock:
            self.mode = "blank"
        self.sdr.set_gain(gain_db)
        self.sdr.tune(hz)
        with self.lock:
            self.blank_until = self.seq + BLANK_AFTER_RETUNE
            self.mode = new_mode

    def listen(self, freq, seconds, gain=None, tag=""):
        seconds = max(0.5, min(float(seconds), self.a.max_listen))
        if not self.sdr_ok.is_set() or self.fifo_fd is None:
            return {"ok": False, "error": "SDR or dump1090 not healthy; refusing to switch"}
        if not self.window.acquire(blocking=False):
            return {"ok": False, "error": "window already active"}
        t_req = time.time()
        try:
            os.makedirs(self.a.ham_dir, exist_ok=True)
            stamp = time.strftime("%Y%m%dT%H%M%S", time.gmtime(t_req))
            base = os.path.join(self.a.ham_dir, f"{stamp}_{int(freq)}")
            meta = dict(start=t_req, freq=int(freq), rate=RATE, fmt="cu8", tag=tag,
                        gain=gain if gain is not None else self.a.gain, seconds=seconds)
            self.ham_fh = open(base + ".cu8", "wb")
            self.ham_meta = meta
            m0 = time.monotonic()
            self.retune(freq, meta["gain"], "ham")
            t_off_done = time.monotonic()
            deadline = m0 + seconds
            while time.monotonic() < deadline and not self.stop.is_set():
                time.sleep(0.02)
        finally:
            # always return to ADS-B, whatever happened above
            back0 = time.monotonic()
            err = None
            for attempt in range(5):
                try:
                    self.retune(ADSB_FREQ, self.a.gain, "adsb")
                    break
                except Exception as e:
                    err = str(e)
                    time.sleep(0.05)
            back1 = time.monotonic()
            fh, self.ham_fh = self.ham_fh, None
            if fh:
                fh.close()
            self.window.release()
        self.stats["windows"] += 1
        res = dict(ok=err is None, error=err, t_start=t_req,
                   switch_out_ms=round((t_off_done - m0) * 1000, 1),
                   switch_back_ms=round((back1 - back0) * 1000, 1),
                   off_1090_s=round(back1 - m0 + BLANK_AFTER_RETUNE * BUF_LEN / 2 / RATE, 3),
                   file=base + ".cu8")
        meta.update(res)
        with open(base + ".json", "w") as f:
            json.dump(meta, f)
        log.info("window %s", res)
        return res

    # ---- writer thread: USB blocks -> FIFO (+ ham file)
    def open_fifo(self):
        p = self.a.fifo
        if not os.path.exists(p):
            os.mkfifo(p, 0o644)
        elif not stat.S_ISFIFO(os.stat(p).st_mode):
            raise RuntimeError(f"{p} exists and is not a FIFO")
        os.chmod(p, 0o644)
        log.info("waiting for dump1090 to open %s", p)
        while not self.stop.is_set():
            try:
                fd = os.open(p, os.O_WRONLY | os.O_NONBLOCK)
                break
            except OSError:            # ENXIO: no reader yet; keep draining USB meanwhile
                self._drain(0.2)
        else:
            return
        os.set_blocking(fd, True)
        try:
            fcntl.fcntl(fd, F_SETPIPE_SZ, 1 << 20)
        except OSError:
            pass
        self.fifo_fd = fd
        log.info("dump1090 connected to FIFO")

    def _drain(self, secs):
        end = time.monotonic() + secs
        while time.monotonic() < end:
            try:
                self.q.get(timeout=0.05)
            except queue.Empty:
                pass

    def writer(self):
        while not self.stop.is_set():
            if self.fifo_fd is None:
                self.open_fifo()
                continue
            try:
                tag, data = self.q.get(timeout=1.0)
            except queue.Empty:
                continue
            out = data if tag == "adsb" else (ZERO if len(data) == BUF_LEN else b"\x7f" * len(data))
            if tag == "ham" and self.ham_fh:
                self.ham_fh.write(data)
            self.stats["buffers"] += 1
            if out is not data:
                self.stats["zero_buffers"] += 1
            try:
                os.write(self.fifo_fd, out)
            except (BrokenPipeError, OSError) as e:
                log.warning("FIFO write failed (%s); dump1090 restarted?", e)
                os.close(self.fifo_fd)
                self.fifo_fd = None
                self.stats["fifo_reopens"] += 1
            notify("WATCHDOG=1")

    # ---- SDR supervision
    def sdr_loop(self):
        """Run the device; on failure bridge with zeros for at most --grace seconds."""
        fail_since = None
        while not self.stop.is_set():
            try:
                self.sdr = SDR(self.a.device, self.a.ppm)
                self.sdr.set_gain(self.a.gain)
                self.sdr.tune(ADSB_FREQ)
                log.info("SDR open, gains=%s", self.sdr.gains)
                self.sdr_ok.set()
                fail_since = None
                r = self.sdr.run_async(self._cb)        # blocks until cancelled / USB error
                if self.stop.is_set():
                    break
                raise OSError(f"read_async returned {r}")
            except Exception as e:
                self.sdr_ok.clear()
                self.stats["sdr_errors"] += 1
                fail_since = fail_since or time.monotonic()
                log.error("SDR failure: %s", e)
                try:
                    if self.sdr:
                        self.sdr.close()
                except Exception:
                    pass
                self.sdr = None
                if time.monotonic() - fail_since > self.a.grace:
                    log.error("SDR down for > %ss: exiting so the outage is visible", self.a.grace)
                    self.stop.set()
                    os._exit(3)
                # keep dump1090's clock running with zeros while we retry
                t_end = time.monotonic() + 1.0
                while time.monotonic() < t_end:
                    try:
                        self.q.put(("blank", ZERO), timeout=0.1)
                    except queue.Full:
                        pass
                    time.sleep(BUF_LEN / 2 / RATE)

    # ---- control socket
    def serve(self):
        p = self.a.ctl
        if os.path.exists(p):
            os.remove(p)
        srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        srv.bind(p)
        os.chmod(p, 0o660)
        srv.listen(4)
        while not self.stop.is_set():
            conn, _ = srv.accept()
            threading.Thread(target=self._client, args=(conn,), daemon=True).start()

    def _client(self, conn):
        with conn, conn.makefile("rwb") as f:
            for line in f:
                try:
                    req = json.loads(line)
                    if req.get("cmd") == "status":
                        res = self.status()
                    elif req.get("cmd") == "listen":
                        res = self.listen(req["freq"], req["seconds"], req.get("gain"),
                                          req.get("tag", ""))
                    else:
                        res = {"ok": False, "error": "unknown cmd"}
                except Exception as e:
                    res = {"ok": False, "error": str(e)}
                f.write((json.dumps(res) + "\n").encode())
                f.flush()

    def status(self):
        return dict(ok=True, mode=self.mode, sdr_ok=self.sdr_ok.is_set(),
                    fifo_connected=self.fifo_fd is not None, queue=self.q.qsize(),
                    dropped=self.dropped, last_rx_age=round(time.monotonic() - self.last_rx, 3),
                    gain=self.a.gain, **self.stats)


def notify(msg):
    addr = os.environ.get("NOTIFY_SOCKET")
    if not addr:
        return
    if addr[0] == "@":
        addr = "\0" + addr[1:]
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as s:
            s.sendto(msg.encode(), addr)
    except OSError:
        pass


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--fifo", default="/run/sdrmux/adsb.cu8")
    ap.add_argument("--ctl", default="/run/sdrmux/ctl.sock")
    ap.add_argument("--ham-dir", default=os.path.expanduser("~/influ/ham"))
    ap.add_argument("--device", type=int, default=0)
    ap.add_argument("--ppm", type=int, default=0)
    ap.add_argument("--gain", type=float, default=None,
                    help="dB for 1090 MHz; omit for tuner AGC")
    ap.add_argument("--grace", type=float, default=60.0)
    ap.add_argument("--max-listen", type=float, default=MAX_LISTEN)
    a = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    m = Mux(a)
    threading.Thread(target=m.sdr_loop, daemon=True, name="sdr").start()
    threading.Thread(target=m.writer, daemon=True, name="writer").start()
    threading.Thread(target=m.serve, daemon=True, name="ctl").start()
    notify("READY=1")
    try:
        while not m.stop.is_set():
            time.sleep(1)
    finally:
        m.stop.set()
        if m.sdr:
            m.sdr.cancel()


if __name__ == "__main__":
    main()
