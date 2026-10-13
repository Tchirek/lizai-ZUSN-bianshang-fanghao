"""A tapped place's weather in HamClock's weather pane.

Opening a place's popup on the map (the native window's or a web page's) sets HamClock's DX
there and turns the pane showing DE's weather (DE_Wx) to DX's (DX_Wx), so it shows that place's
weather; closing the popup turns the pane back as it was. The last popup opened owns the pane:
closing an older one changes nothing. What was changed is kept in ~/influ/model/dxwx.json, under
a lock both displays take, so a display that stopped with a popup open has it undone when it
starts again.

HamClock is asked over its REST port; the calls run on a worker thread, never the caller's.
"""
import fcntl
import json
import logging
import os
import queue
import threading
import urllib.error
import urllib.parse
import urllib.request

REST = "http://127.0.0.1:8070"
STATE = os.path.expanduser("~/influ/model/dxwx.json")
log = logging.getLogger(__name__)


def weather_pane(config):
    """The pane now showing DE's weather in get_config.txt ("Pane3     DE_Wx Moon rotating in 40
    seconds": the current choice first), and its choices as set_pane takes them ("DE_Wx,Moon");
    None if no pane shows it."""
    for line in config.splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[0][:4] == "Pane" and parts[0][4:].isdigit() and parts[1] == "DE_Wx":
            choices = parts[1:parts.index("rotating")] if "rotating" in parts else parts[1:]
            return int(parts[0][4:]), ",".join(choices)
    return None


class DXWeather:
    """`who` names this display ("native", "web"); owners are it or "who:something"."""

    def __init__(self, who, rest=REST, state=STATE):
        self.who, self.rest, self.state = who, rest, state
        self.jobs = queue.Queue()
        threading.Thread(target=self.work, daemon=True).start()
        self.jobs.put(("start", None, None))

    def show(self, owner, lat, lon):
        self.jobs.put(("show", owner, (lat, lon)))

    def close(self, owner):
        self.jobs.put(("close", owner, None))

    def ask(self, command, **args):
        """HamClock's reply, a refusal's too (HTTP 400, the reason in the body)."""
        url = f"{self.rest}/{command}" + ("?" + urllib.parse.urlencode(args, safe=",") if args else "")
        try:
            return urllib.request.urlopen(url, timeout=10).read().decode()
        except urllib.error.HTTPError as e:
            return e.read().decode()

    def read(self):
        try:
            with open(self.state) as f:
                return json.load(f)
        except (OSError, ValueError):
            return None

    def write(self, st):
        if st is None:
            try:
                os.unlink(self.state)
            except FileNotFoundError:
                pass
            return
        with open(self.state + ".part", "w") as f:
            json.dump(st, f)
        os.replace(self.state + ".part", self.state)

    def restore(self, st):
        if st.get("pane") is not None:
            self.ask("set_pane", **{f"Pane{st['pane']}": st["choices"]})

    def run(self, kind, owner, where):
        st = self.read()
        if kind == "start":
            if st and (st["owner"] == self.who or st["owner"].startswith(self.who + ":")):
                self.restore(st)                # left by this display's last run
                self.write(None)
        elif kind == "show":
            if st is None:                      # the pane to turn, the first time only
                found = weather_pane(self.ask("get_config.txt"))
                st = dict(pane=None, choices=None)
                if found:
                    reply = self.ask("set_pane", **{f"Pane{found[0]}": "DX_Wx"})
                    if reply.startswith(f"Pane{found[0]}"):        # else refused, e.g. DX_Wx already shown
                        st = dict(pane=found[0], choices=found[1])
            st["owner"] = owner
            self.write(st)
            self.ask("set_newdx", lat=f"{where[0]:.4f}", lng=f"{where[1]:.4f}")
        elif kind == "close" and st and st["owner"] == owner:
            self.restore(st)
            self.write(None)

    def work(self):
        while True:
            kind, owner, where = self.jobs.get()
            try:
                os.makedirs(os.path.dirname(self.state), exist_ok=True)
                with open(self.state + ".lock", "a") as lock:
                    fcntl.flock(lock, fcntl.LOCK_EX)    # both displays: one at a time
                    self.run(kind, owner, where)
            except (OSError, urllib.error.URLError, ValueError, KeyError) as e:
                log.warning("DX weather %s: %s", kind, e)
            finally:
                self.jobs.task_done()
