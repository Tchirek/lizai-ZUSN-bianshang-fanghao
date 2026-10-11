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
"""
import io
import os
import queue
import threading
import time

os.environ.setdefault("SDL_VIDEODRIVER", "wayland,x11")
os.environ.setdefault("PYGAME_HIDE_SUPPORT_PROMPT", "1")
import pygame                                    # noqa: E402
import websocket                                 # noqa: E402

URL = "ws://127.0.0.1:8071/live-ws"
APP_W, APP_H = 800, 480         # HamClock's own coordinates for touch and mouse
POLL = 0.1                      # s between update requests, as live.html (UPDATE_MS)
JITTER = 5                      # app px a press may move and still count as a tap
CURSOR_IDLE = 3                 # s before the pointer is hidden
KEYS = {pygame.K_RETURN: "Enter", pygame.K_KP_ENTER: "Enter", pygame.K_TAB: "Tab",
        pygame.K_ESCAPE: "Escape", pygame.K_BACKSPACE: "Backspace", pygame.K_SPACE: "Space",
        pygame.K_LEFT: "ArrowLeft", pygame.K_RIGHT: "ArrowRight",
        pygame.K_UP: "ArrowUp", pygame.K_DOWN: "ArrowDown"}


class Link(threading.Thread):
    """Live connection to HamClock. Posts ("full", png) and ("update", header, png) frames to a
    queue for the display thread; send() forwards input the other way."""

    def __init__(self, frames):
        super().__init__(daemon=True)
        self.frames = frames
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
        ws = websocket.create_connection(URL, timeout=10)
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
            self.frames.put(("update", header, data) if header else ("full", data))
            header = None
            time.sleep(POLL)
            ws.send("get_live.bin?")


class Screen:
    """Full-size copy of the clock, drawn scaled and centred on the display."""

    def __init__(self):
        pygame.display.init()
        self.surface = pygame.display.set_mode((0, 0), pygame.FULLSCREEN)
        pygame.display.set_caption("HamClock")
        self.clock = None                        # pygame.Surface at HamClock's build size
        self.view = None                         # pygame.Rect where it appears on screen

    def full(self, png):
        self.clock = pygame.image.load(io.BytesIO(png), "live.png").convert()
        sw, sh = self.surface.get_size()
        cw, ch = self.clock.get_size()
        k = min(sw / cw, sh / ch)
        self.view = pygame.Rect(0, 0, round(cw * k), round(ch * k))
        self.view.center = (sw // 2, sh // 2)

    def update(self, header, png):
        bw, bh, n = header[0], header[1], header[2] << 8 | header[3]
        if not (n and self.clock):
            return
        strip = pygame.image.load(io.BytesIO(png), "regions.png").convert()
        sx = 0
        for i in range(n):
            x, y, blocks = header[4 + 3 * i: 7 + 3 * i]
            w = blocks * bw
            self.clock.blit(strip, (x * bw, y * bh), pygame.Rect(sx, 0, w, bh))
            sx += w

    def draw(self):
        self.surface.fill((0, 0, 0))
        if self.clock:
            self.surface.blit(pygame.transform.smoothscale(self.clock, self.view.size), self.view)
        pygame.display.flip()

    def app_xy(self, pos):
        """Screen position -> HamClock coordinates, or None outside the clock."""
        if not self.view or not self.view.collidepoint(pos):
            return None
        return ((pos[0] - self.view.x) * APP_W // self.view.w,
                (pos[1] - self.view.y) * APP_H // self.view.h)


def key_name(ev):
    if ev.key in KEYS:
        return KEYS[ev.key]
    ch = ev.unicode
    return ch if len(ch) == 1 and 33 <= ord(ch) <= 126 else None


def main():
    frames = queue.Queue()
    link = Link(frames)
    link.start()
    screen = Screen()
    pressed, last_move, last_input = None, 0.0, time.time()
    while True:
        changed = False
        for ev in pygame.event.get():
            if ev.type == pygame.QUIT:
                return
            if ev.type in (pygame.WINDOWEXPOSED, pygame.WINDOWSIZECHANGED):
                changed = True
            if ev.type in (pygame.MOUSEMOTION, pygame.MOUSEBUTTONDOWN, pygame.KEYDOWN):
                last_input = time.time()
                pygame.mouse.set_visible(True)
            if ev.type == pygame.MOUSEBUTTONDOWN and ev.button in (1, 2, 3):
                pressed = screen.app_xy(ev.pos)
            elif ev.type == pygame.MOUSEBUTTONUP and ev.button in (1, 2, 3) and pressed:
                at = screen.app_xy(ev.pos)
                if at and abs(at[0] - pressed[0]) <= JITTER and abs(at[1] - pressed[1]) <= JITTER:
                    ctrl = pygame.key.get_mods() & pygame.KMOD_CTRL
                    button = 1 if ev.button == 2 or (ev.button == 1 and ctrl) else 0
                    link.send(f"set_touch?x={at[0]}&y={at[1]}&button={button}")
                pressed = None
            elif ev.type == pygame.MOUSEMOTION and time.time() - last_move > POLL:
                last_move = time.time()
                at = screen.app_xy(ev.pos) or (-1, -1)
                link.send(f"set_mouse?x={at[0]}&y={at[1]}")
            elif ev.type == pygame.WINDOWLEAVE:
                link.send("set_mouse?x=-1&y=-1")
            elif ev.type == pygame.KEYDOWN and (name := key_name(ev)):
                mods = pygame.key.get_mods()
                mod = ("C" if mods & pygame.KMOD_CTRL else "") + ("S" if mods & pygame.KMOD_SHIFT else "")
                link.send(f"set_char?char={name}&mod={mod}")

        while not frames.empty():
            kind, *data = frames.get()
            if kind == "full":
                screen.full(*data)
            else:
                screen.update(*data)
            changed = True
        if changed:
            screen.draw()
        if time.time() - last_input > CURSOR_IDLE:
            pygame.mouse.set_visible(False)
        time.sleep(0.02)


if __name__ == "__main__":
    main()
