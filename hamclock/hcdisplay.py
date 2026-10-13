#!/usr/bin/env python3
"""HamClock on the X230's own screen, as a native full-screen window (no browser).

The HamClock engine (web build) keeps running as a service; this window speaks the same
live protocol its live.html page uses, on ws://127.0.0.1:8071/live-ws:

  get_live.png?   ->  PNG of the whole clock
  get_live.bin?   ->  header  [blok_w, blok_h, n_regn (2 bytes), (x, y, n_long) * n_regn]
                      then one PNG strip, blok_h tall, holding the changed regions side by side

It keeps a full-size copy of the clock, shows it scaled to the screen with its aspect ratio
kept, and sends taps, pointer moves and keys back (set_touch / set_mouse / set_char), so it
behaves like a HamClock window. Reconnects by itself whenever HamClock restarts.

Over HamClock's world map goes the UV-K5 range map with the live amateur activity (map_view.py;
if it cannot start, the pictures build_rangemap.py and activity.py leave in ~/influ/model stand
in). A touchpad pinch or the wheel zooms it, a two-finger scroll or a drag pans it (touchpad
gestures come from Wayland, gestures.py), and so do the arrow keys, + and - (or PgUp, PgDn: a press
zooms a step; held with Shift they zoom on and on), and a gamepad (pygame.joystick: left stick or D-pad pans, shoulders
zoom, A is Enter, B Escape). Tapping a place opens its popup (map_panel.py), and HamClock's weather
pane shows that place's weather until it closes (dxwx.py). Over the foot of the left column goes
this week's FT8 tally. Taps, drags and pointer moves over them stay here instead of reaching what
they cover.

The window is the clock's aspect at the panel's size (1280x768 on the X230's 1366x768); the
clock is scaled to it once, then only where it changed, and while the map moves only the map
box is redrawn.
"""
import io
import os
import queue
import threading
import time
from fractions import Fraction

os.environ.setdefault("SDL_VIDEODRIVER", "wayland,x11")
os.environ.setdefault("PYGAME_HIDE_SUPPORT_PROMPT", "1")
# app id / WM class: lets KDE find influ-hamclock.desktop and its icon
os.environ.setdefault("SDL_VIDEO_WAYLAND_WMCLASS", "influ-hamclock")
os.environ.setdefault("SDL_VIDEO_X11_WMCLASS", "influ-hamclock")
import pygame                                    # noqa: E402
import websocket                                 # noqa: E402

URL = "ws://127.0.0.1:8071/live-ws"
APP_W, APP_H = 800, 480         # HamClock's own coordinates for touch and mouse
MAP_BOX = (139, 149, 660, 330)  # HamClock's map_b in those coordinates
TALLY_BOX = (1, 430, 137, 48)   # last 3 DX cluster rows of the left column (rows: 192 + 14 k)
MODEL = os.path.expanduser("~/influ/model")
OVERLAYS = [("rangemap.png", MAP_BOX), ("night.png", MAP_BOX), ("activity.png", MAP_BOX), ("ft8week.png", TALLY_BOX)]
POLL = 0.1                      # s between update requests, as live.html (UPDATE_MS)
JITTER = 5                      # app px a press may move and still count as a tap
CURSOR_IDLE = 3                 # s before the pointer is hidden
FPS = 60
MAP_KEYS = {pygame.K_LEFT: (1, 0), pygame.K_RIGHT: (-1, 0), pygame.K_UP: (0, 1), pygame.K_DOWN: (0, -1)}
ZOOM_KEYS = {pygame.K_EQUALS: 1, pygame.K_PLUS: 1, pygame.K_KP_PLUS: 1, pygame.K_PAGEUP: 1,
             pygame.K_MINUS: -1, pygame.K_KP_MINUS: -1, pygame.K_PAGEDOWN: -1}
KEY_PAN = 700                   # view px a second the arrow keys move the map
KEY_ZOOM = 3                    # wheel notches a second + and - zoom while held with Shift
KEY_STEP = 2                    # wheel notches a press of + or - zooms at once (x1.44), Shift up
PAD_DEAD = 0.2                  # stick travel ignored around the centre
PAD_ZOOM = {5: 1, 4: -1}        # gamepad buttons held to zoom: right / left shoulder
PAD_KEYS = {0: "Enter", 1: "Escape"}   # gamepad buttons sent as keys: A, B
KEYS = {pygame.K_RETURN: "Enter", pygame.K_KP_ENTER: "Enter", pygame.K_TAB: "Tab",
        pygame.K_ESCAPE: "Escape", pygame.K_BACKSPACE: "Backspace", pygame.K_SPACE: "Space",
        pygame.K_LEFT: "ArrowLeft", pygame.K_RIGHT: "ArrowRight",
        pygame.K_UP: "ArrowUp", pygame.K_DOWN: "ArrowDown"}


class Link(threading.Thread):
    """Live connection to HamClock. Posts its frames, already decoded (see decode()), to a queue
    for the display thread; send() forwards input the other way. With raw=True the frames stay
    as received: ("full", png) and ("update", header, png)."""

    def __init__(self, frames, url=URL, raw=False):
        super().__init__(daemon=True)
        self.frames = frames
        self.url = url
        self.raw = raw
        self.ws = None

    def send(self, msg):
        ws = self.ws
        if ws:
            try:
                ws.send(msg)
            except OSError:
                pass

    def run(self):
        while True:
            try:
                self.session()
            except (OSError, websocket.WebSocketException):
                pass
            self.ws = None
            time.sleep(2)

    def session(self):
        ws = websocket.create_connection(self.url, timeout=10)
        self.ws = ws
        ws.send("get_live.png?")
        header = None
        while True:
            try:
                opcode, data = ws.recv_data()
            except websocket.WebSocketTimeoutException:
                header = None                    # lost a reply: just ask again
                ws.send("get_live.bin?")
                continue
            if opcode != websocket.ABNF.OPCODE_BINARY:
                continue                         # text messages are browser hints
            if data[:4] != b"\x89PNG":
                header = data                    # region list; its PNG strip comes next
                continue
            if self.raw:
                self.frames.put(("update", header, data) if header else ("full", data))
            else:
                self.frames.put(decode(header, data))
            header = None
            time.sleep(POLL)
            ws.send("get_live.bin?")


def decode(header, png):
    """A live frame as pictures: ("full", surface) without a region header, else ("update",
    [(x, y, surface), ...]) in clock px. Decoding here keeps it off the display thread."""
    if not header:
        return "full", pygame.image.load(io.BytesIO(png), "live.png")
    bw, bh, n = header[0], header[1], header[2] << 8 | header[3]
    if not n:
        return "update", []
    # The strip can be far wider than 65535 px when much of the clock changes (e.g. while
    # HamClock starts), and SDL blits wrap source x at 65536. So cut each region out of the
    # strip's raw rows instead of blitting from a source rectangle.
    strip = pygame.image.load(io.BytesIO(png), "regions.png")
    rows, row = pygame.image.tobytes(strip, "RGB"), strip.get_width() * 3
    sx, regions = 0, []
    for i in range(n):
        x, y, blocks = header[4 + 3 * i: 7 + 3 * i]
        w = blocks * bw
        cut = b"".join(rows[r * row + sx * 3: r * row + (sx + w) * 3] for r in range(bh))
        regions.append((x * bw, y * bh, pygame.image.frombuffer(cut, (w, bh), "RGB")))
        sx += w
    return "update", regions


class Mirror:
    """Full-size copy of the clock, kept current from live frames."""

    def __init__(self):
        self.image = None                        # pygame.Surface at HamClock's build size

    def apply(self, kind, data):
        """Apply a decoded frame; the changed rectangles (clock px), or None if all changed."""
        if kind == "full":
            self.image = data
            return None
        if not self.image:
            return []
        for x, y, piece in data:
            self.image.blit(piece, (x, y))
        return [pygame.Rect((x, y), piece.get_size()) for x, y, piece in data]


class Fitted:
    """The mirror scaled to the window: in full once, then only where the clock changed.

    The scale is a ratio p/q of small integers (1600x960 -> 1280x768 is 4/5), so a region
    widened to whole q px cells scales to whole pixels, the same as in a full rescale."""

    def __init__(self, size):
        self.size, self.image = size, None

    def update(self, source, rects=None, skip=None):
        """Rescale `rects` of `source` (all of it if None), leaving out those inside `skip`."""
        sw, sh = source.get_size()
        k = Fraction(self.size[0], sw)
        p, q = k.numerator, k.denominator
        if (self.image is None or rects is None or Fraction(self.size[1], sh) != k
                or q > 16 or sw % q or sh % q):
            self.image = pygame.transform.smoothscale(source, self.size)
            return
        for r in rects:
            if skip and skip.contains(r):
                continue
            x0, y0 = r.left // q * q, r.top // q * q
            x1, y1 = min(sw, -(-r.right // q) * q), min(sh, -(-r.bottom // q) * q)
            cell = pygame.transform.smoothscale(source.subsurface((x0, y0, x1 - x0, y1 - y0)),
                                                ((x1 - x0) * p // q, (y1 - y0) * p // q))
            self.image.blit(cell, (x0 * p // q, y0 * p // q))


class Overlay:
    """One picture over one box of the clock, reloaded whenever its file changes."""

    def __init__(self, name, box):
        self.path, self.box = os.path.join(MODEL, name), box
        self.mtime, self.image, self.scaled = None, None, None

    def refresh(self):
        """Reload if the file changed; True when the picture changed."""
        try:
            mtime = os.path.getmtime(self.path)
        except OSError:
            mtime = None
        if mtime == self.mtime:
            return False
        self.mtime, self.scaled = mtime, None
        try:
            self.image = pygame.image.load(self.path) if mtime else None
        except pygame.error:
            self.image = None                    # caught mid-write: next check reloads
            self.mtime = None
        return True

    def covers(self, at):
        x, y, w, h = self.box
        return self.image is not None and x <= at[0] < x + w and y <= at[1] < y + h

    def blit(self, frame):
        if self.image:
            k = frame.get_width() / APP_W
            box = pygame.Rect(*(round(v * k) for v in self.box))
            if self.scaled is None or self.scaled.get_size() != box.size:
                self.scaled = self.image if self.image.get_size() == box.size else \
                    pygame.transform.smoothscale(self.image, box.size)
            frame.blit(self.scaled, box)


class Scene:
    """One independent view, drawn identically in a window or onto an HTTP frame."""

    def __init__(self, size, link, frame=None, canvas_options=None, panel_options=None):
        from map_panel import MapPanel
        self.link, self.k = link, size[0] / APP_W
        self.frame = frame if frame is not None else pygame.Surface(size)
        self.box = pygame.Rect(*(round(v * self.k) for v in MAP_BOX))
        self.fitted = Fitted(size)
        self.overlays = [Overlay(name, box) for name, box in OVERLAYS]
        self.canvas = self.inspector = None
        self.map_size = (1320, 660)
        try:
            from map_view import Canvas, W, H
            self.map_size = W, H
            self.canvas = Canvas(self.box.size, **(canvas_options or {}))
            self.overlays = [o for o in self.overlays if o.box != MAP_BOX]
        except (OSError, ValueError, KeyError, ImportError) as e:
            print(f"zoomable map unavailable: {e}", flush=True)
        try:
            self.inspector = MapPanel(**(panel_options or {}))
        except (OSError, ValueError, KeyError):
            pass
        w, h = size
        b = self.box
        self.strips = [pygame.Rect(0, 0, w, b.top), pygame.Rect(0, b.bottom, w, h - b.bottom),
                       pygame.Rect(0, b.top, b.left, b.h), pygame.Rect(b.right, b.top, w - b.right, b.h)]
        self.whole = self.clock_dirty = self.map_dirty = True
        self.last_check = self.last_panel = 0
        self.held = set()
        self.weather = None                  # dxwx.DXWeather: the weather pane follows the popup
        self.search = None

    def close_search(self):
        if self.search:
            pygame.key.set_repeat(*self.search.repeat)
        if self.search and self.search.future:
            self.search.future.cancel()
        self.search = None
        pygame.key.stop_text_input()
        self.map_dirty = True

    def choose_search(self):
        if not self.search.results:
            return
        from map_view import clamp
        before = self.popup()
        index = self.search.hovered if self.search.hovered is not None else self.search.selected
        point = self.search.results[index]['point']
        camera = self.canvas.camera
        camera.zoom = camera.goal = max(8, camera.zoom)
        camera.centre = clamp(point, camera.zoom)
        camera.anchor = (self.map_size[0] / 2, self.map_size[1] / 2)
        camera.anchor_base = camera.centre
        self.canvas.view = camera.view()
        self.canvas.dirty = True
        if self.inspector:
            self.inspector.m = self.canvas.view
            self.inspector.open(self.canvas.view.project(point))
        self.follow(before)
        self.close_search()

    def covered(self, at):
        return (self.canvas and on_map(at)) or any(o.covers(at) for o in self.overlays)

    def popup(self):
        """Where the open popup is, (lat, lon); None if none is."""
        return self.inspector.target if self.inspector and self.inspector.point is not None else None

    def follow(self, before):
        """HamClock's weather pane shows the open popup's place, and DE again once it closes."""
        now = self.popup()
        if self.weather and now != before:
            if now:
                self.weather.show("native", *now)
            else:
                self.weather.close("native")

    def map_xy(self, at):
        return ((at[0] - MAP_BOX[0]) * self.map_size[0] / MAP_BOX[2],
                (at[1] - MAP_BOX[1]) * self.map_size[1] / MAP_BOX[3])

    def panel_contains(self, at):
        return bool(self.inspector and self.inspector.box
                    and self.inspector.box.collidepoint(at[0] * self.k, at[1] * self.k))

    def tap(self, at, button=1, ctrl=False):
        if self.search:
            panel = self.search
            if panel.box and panel.box.collidepoint(at[0] * self.k, at[1] * self.k):
                row = int((at[1] * self.k - panel.box.y) // panel.row_height) - 1
                if button == 1 and 0 <= row < len(panel.results):
                    panel.selected = row
                    panel.hovered = row
                    self.choose_search()
            else:
                self.close_search()
            return
        if self.canvas and self.inspector:
            self.inspector.m = self.canvas.view
        if self.inspector and button == 1 and not ctrl:
            before = self.popup()
            consumed = self.inspector.tap((at[0] * self.k, at[1] * self.k), self.box)
            self.map_dirty = consumed or before is not None or self.map_dirty
            self.follow(before)
            if consumed:
                return
        if not self.covered(at):
            alternate = button == 2 or button == 1 and ctrl
            self.link.send(f"set_touch?x={int(at[0])}&y={int(at[1])}&button={int(alternate)}")

    def hover(self, at):
        x, y = (int(at[0]), int(at[1])) if at and not self.covered(at) else (-1, -1)
        self.link.send(f"set_mouse?x={x}&y={y}")

    def key(self, name, ctrl=False, shift=False, repeat=False):
        if self.search:
            if name == 'Escape':
                self.close_search()
            elif name == 'Enter':
                self.choose_search()
            elif name in ('ArrowUp', 'ArrowDown') and self.search.results:
                self.search.selected = (self.search.selected + (1 if name == 'ArrowDown' else -1)) % len(self.search.results)
            elif name == 'Backspace':
                if not self.search.query:
                    if not repeat:                  # held down to empty it: the box stays
                        self.close_search()
                else:
                    self.search.query = '' if ctrl else self.search.query[:-1]
                    self.search.filter()
            self.map_dirty = True
        elif name.lower() == 's' and not ctrl and self.canvas:
            from map_search import SearchPanel
            self.search = SearchPanel(self.canvas)
            pygame.key.set_repeat(400, 40)          # Backspace held deletes on and on
            self.held.clear()
            pygame.key.start_text_input()
            self.map_dirty = True
        elif name == "Escape" and self.inspector and self.inspector.point is not None:
            before = self.popup()
            self.inspector.close()
            self.map_dirty = True
            self.follow(before)
        else:
            mod = ("C" if ctrl else "") + ("S" if shift else "")
            self.link.send(f"set_char?char={name}&mod={mod}")

    def update(self, source, changed=None):
        if changed == []:
            return
        skip = self.canvas and pygame.Rect(*(round(v * source.get_width() / APP_W) for v in MAP_BOX))
        self.fitted.update(source, changed, skip)
        self.clock_dirty = True

    def tick(self, dt):
        now = time.monotonic()
        if self.search:
            self.map_dirty = self.search.update() or self.map_dirty
        if now - self.last_check > 5:
            self.last_check = now
            self.clock_dirty = any([o.refresh() for o in self.overlays]) or self.clock_dirty
        if self.canvas:
            vx = int('ArrowLeft' in self.held) - int('ArrowRight' in self.held)
            vy = int('ArrowUp' in self.held) - int('ArrowDown' in self.held)
            zoom = int(bool(self.held & {'+', '=', 'PageUp'})) - int(bool(self.held & {'-', 'PageDown'}))
            if vx or vy:
                self.canvas.pan(vx * KEY_PAN * dt, vy * KEY_PAN * dt)
            if zoom:
                self.canvas.scroll(zoom * KEY_ZOOM * dt, tuple(v / 2 for v in self.map_size))
            self.map_dirty = self.canvas.tick(dt) or self.map_dirty
            if self.inspector:
                self.inspector.m = self.canvas.view
        if self.inspector and self.inspector.point is not None \
                and ((self.inspector.future and self.inspector.future.done()) or now - self.last_panel > 10):
            self.map_dirty = True
            self.last_panel = now

    def draw(self):
        if self.fitted.image is None or not (self.whole or self.clock_dirty or self.map_dirty):
            return False
        frame = self.frame
        if self.canvas and not self.whole:
            if self.clock_dirty:
                for r in self.strips:
                    frame.blit(self.fitted.image, r, r)
        else:
            frame.blit(self.fitted.image, (0, 0))
        if self.whole or self.clock_dirty or not self.canvas:
            for o in self.overlays:
                o.blit(frame)
        if self.canvas and (self.whole or self.map_dirty):
            self.canvas.draw(frame, self.box)
        if self.inspector and (self.whole or self.map_dirty or not self.canvas):
            self.inspector.draw(frame, tuple(self.box))
        if self.search and (self.whole or self.map_dirty):
            self.search.draw(frame, self.box)
        self.whole = self.clock_dirty = self.map_dirty = False
        return True

    def close(self):
        if self.search:
            self.close_search()
        if self.canvas:
            self.canvas.close()
        if self.inspector:
            self.inspector.shutdown()


class Screen:
    """The full-screen window; `frame` is the part showing the clock, centred, aspect kept."""

    def __init__(self):
        pygame.display.init()
        dw, dh = pygame.display.get_desktop_sizes()[0]
        k = min(dw / APP_W, dh / APP_H)
        try:                                     # SDL scales this to the panel, letterboxed
            self.surface = pygame.display.set_mode((round(APP_W * k), round(APP_H * k)),
                                                   pygame.FULLSCREEN | pygame.SCALED, vsync=1)
        except pygame.error:
            self.surface = pygame.display.set_mode((0, 0), pygame.FULLSCREEN)
        pygame.display.set_caption("HamClock")
        sw, sh = self.surface.get_size()
        k = min(sw / APP_W, sh / APP_H)
        self.view = pygame.Rect(0, 0, round(APP_W * k), round(APP_H * k))
        self.view.center = (sw // 2, sh // 2)
        self.frame = self.surface.subsurface(self.view)

    def app_xy(self, pos):
        """Window position -> HamClock coordinates (fractional); None outside the clock."""
        if not self.view.collidepoint(pos):
            return None
        return ((pos[0] - self.view.x) * APP_W / self.view.w,
                (pos[1] - self.view.y) * APP_H / self.view.h)


def key_name(ev):
    if ev.key in KEYS:
        return KEYS[ev.key]
    ch = ev.unicode
    return ch if len(ch) == 1 and 33 <= ord(ch) <= 126 else None


def pad_motion(pad):
    """How a gamepad moves the map: (vx, vy, zoom), each in -1..1, as the arrow keys and + / -.
    The left stick moves it in proportion, the D-pad (hat) at full speed."""
    def stick(axis):
        v = pad.get_axis(axis) if pad.get_numaxes() > axis else 0.0
        return 0.0 if abs(v) < PAD_DEAD else (abs(v) - PAD_DEAD) / (1 - PAD_DEAD) * (1 if v > 0 else -1)
    hx, hy = pad.get_hat(0) if pad.get_numhats() else (0, 0)
    vx = max(-1.0, min(1.0, -hx - stick(0)))
    vy = max(-1.0, min(1.0, hy - stick(1)))
    zoom = sum(v for b, v in PAD_ZOOM.items() if b < pad.get_numbuttons() and pad.get_button(b))
    return vx, vy, zoom


def on_map(at):
    x, y, w, h = MAP_BOX
    return at is not None and x <= at[0] < x + w and y <= at[1] < y + h


def main():
    frames = queue.Queue()
    link = Link(frames)
    link.start()
    mirror, screen = Mirror(), Screen()
    pygame.key.stop_text_input()
    scene = Scene(screen.view.size, link, screen.frame)
    from dxwx import DXWeather
    scene.weather = DXWeather("native")
    canvas, inspector, box, k = scene.canvas, scene.inspector, scene.box, scene.k
    MAP_W, MAP_H = scene.map_size
    gestures = None
    if canvas:
        try:
            from gestures import Gestures
            gestures = Gestures()
        except (OSError, ValueError, KeyError, ImportError) as e:
            print(f"touchpad gestures unavailable: {e}", flush=True)
    # Wayland scrolls come in the window's own px; SDL scales the frame onto it
    surface_k = screen.surface.get_width() / pygame.display.get_window_size()[0]
    map_xy = scene.map_xy
    pygame.joystick.init()
    pads = {}                            # plugged-in gamepads by instance id (hot-plug events)

    clock, dt = pygame.time.Clock(), 0.0
    pressed = drag = None                # app px of the press; (last window px, dragging?)
    last_move, last_input = 0.0, time.time()
    while True:
        for ev in pygame.event.get():
            if ev.type == pygame.QUIT:
                scene.close()
                return
            if ev.type in (pygame.WINDOWEXPOSED, pygame.WINDOWSIZECHANGED):
                scene.whole = True
            if ev.type in (pygame.MOUSEMOTION, pygame.MOUSEBUTTONDOWN, pygame.MOUSEWHEEL, pygame.KEYDOWN):
                last_input = time.time()
                pygame.mouse.set_visible(True)
            if scene.search:
                if ev.type == pygame.TEXTINPUT:
                    scene.search.query = (scene.search.query + ev.text)[:128]
                    scene.search.composition = ''
                    scene.search.filter()
                    scene.map_dirty = True
                elif ev.type == pygame.TEXTEDITING:
                    scene.search.composition = ev.text
                    scene.map_dirty = True
                elif ev.type == pygame.KEYDOWN and not scene.search.composition:
                    repeat = ev.key == pygame.K_BACKSPACE and scene.search.backspace_down
                    if ev.key == pygame.K_BACKSPACE:
                        scene.search.backspace_down = True
                    scene.key(key_name(ev) or '', bool(ev.mod & pygame.KMOD_CTRL), repeat=repeat)
                elif ev.type == pygame.KEYUP and ev.key == pygame.K_BACKSPACE:
                    scene.search.backspace_down = False
                elif ev.type in (pygame.MOUSEMOTION, pygame.WINDOWLEAVE):
                    at = screen.app_xy(ev.pos) if ev.type == pygame.MOUSEMOTION else None
                    scene.map_dirty = scene.search.hover((at[0] * k, at[1] * k) if at else None) or scene.map_dirty
                elif ev.type == pygame.MOUSEBUTTONDOWN:
                    at = screen.app_xy(ev.pos)
                    if at:
                        scene.tap(at, ev.button)
                elif ev.type == pygame.JOYBUTTONDOWN and ev.button in PAD_KEYS:
                    scene.key(PAD_KEYS[ev.button])
                if ev.type not in (pygame.JOYDEVICEADDED, pygame.JOYDEVICEREMOVED):
                    pressed = drag = None
                    continue
            if ev.type == pygame.MOUSEWHEEL:              # without Wayland gestures: zoom
                at = screen.app_xy(pygame.mouse.get_pos())
                if canvas and not gestures and on_map(at):
                    canvas.scroll(ev.precise_y * (-1 if ev.flipped else 1), map_xy(at))
            elif ev.type == pygame.MOUSEBUTTONDOWN and ev.button in (1, 2, 3):
                pressed, drag = screen.app_xy(ev.pos), None
                on_panel = inspector and inspector.box and pressed and \
                    inspector.box.collidepoint(pressed[0] * k, pressed[1] * k)
                if ev.button == 1 and canvas and on_map(pressed) and not on_panel:
                    drag = ev.pos, False                            # a drag if it moves enough
            elif ev.type == pygame.MOUSEMOTION and drag:
                at, (last, dragging) = screen.app_xy(ev.pos), drag
                if dragging or (at and max(abs(at[0] - pressed[0]), abs(at[1] - pressed[1])) > JITTER):
                    canvas.pan((ev.pos[0] - last[0]) * MAP_W / box.w, (ev.pos[1] - last[1]) * MAP_H / box.h)
                    drag = ev.pos, True
            elif ev.type == pygame.MOUSEBUTTONUP and ev.button in (1, 2, 3) and pressed:
                at = screen.app_xy(ev.pos)
                if not (drag and drag[1]) and at and abs(at[0] - pressed[0]) <= JITTER \
                        and abs(at[1] - pressed[1]) <= JITTER:
                    ctrl = pygame.key.get_mods() & pygame.KMOD_CTRL
                    scene.tap(at, ev.button, bool(ctrl))
                pressed = drag = None
            elif ev.type == pygame.WINDOWLEAVE:
                link.send("set_mouse?x=-1&y=-1")
            elif ev.type == pygame.KEYDOWN and canvas and (ev.key in MAP_KEYS or ev.key in ZOOM_KEYS):
                if ev.key in ZOOM_KEYS and not ev.mod & pygame.KMOD_SHIFT:   # a step; with Shift, below
                    canvas.scroll(ZOOM_KEYS[ev.key] * KEY_STEP, (MAP_W / 2, MAP_H / 2))
            elif ev.type == pygame.KEYDOWN and (name := key_name(ev)):
                mods = pygame.key.get_mods()
                if not mods & (pygame.KMOD_ALT | pygame.KMOD_GUI):
                    scene.key(name, bool(mods & pygame.KMOD_CTRL), bool(mods & pygame.KMOD_SHIFT))
            elif ev.type == pygame.JOYDEVICEADDED:
                pad = pygame.joystick.Joystick(ev.device_index)
                pads[pad.get_instance_id()] = pad
            elif ev.type == pygame.JOYDEVICEREMOVED:
                pads.pop(ev.instance_id, None)
            elif ev.type == pygame.JOYBUTTONDOWN and ev.button in PAD_KEYS:
                scene.key(PAD_KEYS[ev.button])
            if ev.type == pygame.MOUSEMOTION and not drag and time.time() - last_move > POLL:
                last_move = time.time()
                at = screen.app_xy(ev.pos)
                scene.hover(at)

        if gestures:
            for e in gestures.poll():
                last_input = time.time()
                at = screen.app_xy(pygame.mouse.get_pos())
                if scene.search or not on_map(at):
                    continue
                if e[0] == "pinch":                      # fingers spread, and their middle moved
                    canvas.pinch(e[1], map_xy(at))
                    canvas.pan(e[2] * surface_k * MAP_W / box.w, e[3] * surface_k * MAP_H / box.h)
                elif e[0] == "scroll":                   # two fingers or the TrackPoint: pan
                    canvas.pan(-e[1] * surface_k * MAP_W / box.w, -e[2] * surface_k * MAP_H / box.h)
                elif e[0] == "wheel":
                    canvas.scroll(e[1], map_xy(at))
        if canvas and not scene.search:
            held = pygame.key.get_pressed()
            vx = sum(v[0] for key, v in MAP_KEYS.items() if held[key])
            vy = sum(v[1] for key, v in MAP_KEYS.items() if held[key])
            shift = pygame.key.get_mods() & pygame.KMOD_SHIFT        # zoom keys held with Shift: on and on
            zoom = sum(v for key, v in ZOOM_KEYS.items() if held[key] and shift)
            for pad in pads.values():
                px, py, pz = pad_motion(pad)
                vx, vy, zoom = vx + px, vy + py, zoom + pz
            if vx or vy:
                canvas.pan(vx * KEY_PAN * dt, vy * KEY_PAN * dt)
            if zoom:
                canvas.scroll(max(-1, min(1, zoom)) * KEY_ZOOM * dt, (MAP_W / 2, MAP_H / 2))

        while not frames.empty():
            changed = mirror.apply(*frames.get())
            if mirror.image:
                scene.update(mirror.image, changed)
        scene.tick(dt)
        if scene.draw():
            pygame.display.flip()
        if time.time() - last_input > CURSOR_IDLE:
            pygame.mouse.set_visible(False)
        dt = clock.tick(FPS) / 1000


if __name__ == "__main__":
    main()
