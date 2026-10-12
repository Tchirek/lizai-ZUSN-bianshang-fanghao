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
gestures come from Wayland, gestures.py), and so do the arrow keys, + and - (or PgUp, PgDn). Over the foot of
the left column goes this week's FT8 tally. Taps, drags and pointer moves over them stay here
instead of reaching what they cover.

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
KEY_ZOOM = 3                    # wheel notches a second + and - zoom
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


def on_map(at):
    x, y, w, h = MAP_BOX
    return at is not None and x <= at[0] < x + w and y <= at[1] < y + h


def main():
    from map_panel import MapPanel
    frames = queue.Queue()
    link = Link(frames)
    link.start()
    mirror, screen = Mirror(), Screen()
    fitted = Fitted(screen.view.size)
    overlays = [Overlay(name, box) for name, box in OVERLAYS]
    k = screen.view.w / APP_W                                       # app px -> frame px
    box = pygame.Rect(*(round(v * k) for v in MAP_BOX))
    canvas = gestures = None
    try:
        from map_view import Canvas, W as MAP_W, H as MAP_H
        canvas = Canvas(box.size)
        overlays = [o for o in overlays if o.box != MAP_BOX]      # it draws the whole map box
        from gestures import Gestures
        gestures = Gestures()
    except (OSError, ValueError, KeyError, ImportError) as e:
        print(f"{'touchpad gestures' if canvas else 'zoomable map'} unavailable: {e}", flush=True)
    try:
        inspector = MapPanel()
    except (OSError, ValueError):
        inspector = None
    fw, fh = screen.view.size
    # Wayland scrolls come in the window's own px; SDL scales the frame onto it
    surface_k = screen.surface.get_width() / pygame.display.get_window_size()[0]
    strips = [pygame.Rect(0, 0, fw, box.top), pygame.Rect(0, box.bottom, fw, fh - box.bottom),
              pygame.Rect(0, box.top, box.left, box.h), pygame.Rect(box.right, box.top, fw - box.right, box.h)]

    def covered(at):
        return (canvas and on_map(at)) or any(o.covers(at) for o in overlays)

    def map_xy(at):
        return (at[0] - MAP_BOX[0]) * MAP_W / MAP_BOX[2], (at[1] - MAP_BOX[1]) * MAP_H / MAP_BOX[3]

    clock, dt = pygame.time.Clock(), 0.0
    pressed = drag = None                # app px of the press; (last window px, dragging?)
    whole, last_move, last_input, last_check = True, 0.0, time.time(), 0.0
    while True:
        clock_dirty = map_dirty = False
        if time.time() - last_check > 5:
            last_check = time.time()
            clock_dirty = any([o.refresh() for o in overlays])     # list: refresh every one
        for ev in pygame.event.get():
            if ev.type == pygame.QUIT:
                return
            if ev.type in (pygame.WINDOWEXPOSED, pygame.WINDOWSIZECHANGED):
                whole = True
            if ev.type in (pygame.MOUSEMOTION, pygame.MOUSEBUTTONDOWN, pygame.MOUSEWHEEL, pygame.KEYDOWN):
                last_input = time.time()
                pygame.mouse.set_visible(True)
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
                    if inspector and ev.button == 1 and not ctrl and inspector.tap((at[0] * k, at[1] * k), box):
                        map_dirty = True
                    elif not covered(at):
                        button = 1 if ev.button == 2 or (ev.button == 1 and ctrl) else 0
                        link.send(f"set_touch?x={int(at[0])}&y={int(at[1])}&button={button}")
                pressed = drag = None
            elif ev.type == pygame.WINDOWLEAVE:
                link.send("set_mouse?x=-1&y=-1")
            elif ev.type == pygame.KEYDOWN and canvas and (ev.key in MAP_KEYS or ev.key in ZOOM_KEYS):
                pass                                    # held keys move the map, below
            elif ev.type == pygame.KEYDOWN and (name := key_name(ev)):
                if name == "Escape" and inspector and inspector.point is not None:
                    inspector.close()
                    map_dirty = True
                else:
                    mods = pygame.key.get_mods()
                    mod = ("C" if mods & pygame.KMOD_CTRL else "") + ("S" if mods & pygame.KMOD_SHIFT else "")
                    link.send(f"set_char?char={name}&mod={mod}")
            if ev.type == pygame.MOUSEMOTION and not drag and time.time() - last_move > POLL:
                last_move = time.time()
                at = screen.app_xy(ev.pos)
                at = (int(at[0]), int(at[1])) if at and not covered(at) else (-1, -1)
                link.send(f"set_mouse?x={at[0]}&y={at[1]}")

        if gestures:
            for e in gestures.poll():
                last_input = time.time()
                at = screen.app_xy(pygame.mouse.get_pos())
                if not on_map(at):
                    continue
                if e[0] == "pinch":                      # fingers spread, and their middle moved
                    canvas.pinch(e[1], map_xy(at))
                    canvas.pan(e[2] * surface_k * MAP_W / box.w, e[3] * surface_k * MAP_H / box.h)
                elif e[0] == "scroll":                   # two fingers or the TrackPoint: pan
                    canvas.pan(-e[1] * surface_k * MAP_W / box.w, -e[2] * surface_k * MAP_H / box.h)
                elif e[0] == "wheel":
                    canvas.scroll(e[1], map_xy(at))
        if canvas:
            held = pygame.key.get_pressed()
            vx = sum(v[0] for key, v in MAP_KEYS.items() if held[key])
            vy = sum(v[1] for key, v in MAP_KEYS.items() if held[key])
            zoom = sum(v for key, v in ZOOM_KEYS.items() if held[key])
            if vx or vy:
                canvas.pan(vx * KEY_PAN * dt, vy * KEY_PAN * dt)
            if zoom:
                canvas.scroll(max(-1, min(1, zoom)) * KEY_ZOOM * dt, (MAP_W / 2, MAP_H / 2))

        while not frames.empty():
            changed = mirror.apply(*frames.get())
            if mirror.image:
                skip = canvas and pygame.Rect(*(round(v * mirror.image.get_width() / APP_W) for v in MAP_BOX))
                fitted.update(mirror.image, changed, skip)
                clock_dirty = True
        if canvas:
            map_dirty = canvas.tick(dt) or map_dirty
            if inspector:
                inspector.m = canvas.view
        if inspector and inspector.future and inspector.future.done():
            map_dirty = True
        if fitted.image and (whole or clock_dirty or map_dirty):
            frame = screen.frame
            if canvas and not whole:             # the map box keeps the map drawn there
                if clock_dirty:
                    for r in strips:
                        frame.blit(fitted.image, r, r)
            else:
                frame.blit(fitted.image, (0, 0))
            if whole or clock_dirty or not canvas:
                for o in overlays:
                    o.blit(frame)
            if canvas and (whole or map_dirty):
                canvas.draw(frame, box)
            if inspector and (whole or map_dirty or not canvas):
                inspector.draw(frame, tuple(box))
            pygame.display.flip()
            whole = False
        if time.time() - last_input > CURSOR_IDLE:
            pygame.mouse.set_visible(False)
        dt = clock.tick(FPS) / 1000


if __name__ == "__main__":
    main()
