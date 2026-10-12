#!/usr/bin/env python3
"""Optional HTTP HamClock display; each page has its own map and reachability popup.

Run separately from hcdisplay.py. One live HamClock connection feeds all pages; the
native window and every page keep independent browsing state. No new dependencies.
"""
import argparse
from concurrent.futures import Future, ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
import logging
import math
import os
from pathlib import Path
import queue
import secrets
import socket
import threading
import time
from urllib.parse import parse_qs, urlsplit

os.environ['SDL_VIDEODRIVER'] = 'dummy'
os.environ.setdefault('PYGAME_HIDE_SUPPORT_PROMPT', '1')
import pygame
from PIL import Image, ImageChops

from hcdisplay import APP_W, APP_H, MAP_BOX, Fitted, Link, Mirror, Scene, on_map

SIZE = (1280, 768)                         # same frame size as the X230 window
IDLE_S = 120
MAX_SESSIONS = 8
MAP_KEYS = {'ArrowLeft', 'ArrowRight', 'ArrowUp', 'ArrowDown', '+', '=', '-', 'PageUp', 'PageDown'}
KEYS = MAP_KEYS | {'Enter', 'Tab', 'Escape', 'Backspace', 'Space'}
BAND = 32                                  # rows per band when looking for changed pixels
WHOLE = 0.6                                # changes over this share of the frame go whole
log = logging.getLogger(__name__)


class Busy(Exception):
    pass


class Gone(Exception):
    pass


def number(event, name, limit):
    value = event.get(name)
    if isinstance(value, bool) or not isinstance(value, (int, float)) \
            or not math.isfinite(value) or abs(value) > limit:
        raise ValueError(f'invalid {name}')
    return value


def validate_event(event):
    if not isinstance(event, dict):
        raise ValueError('invalid event')
    kind = event.get('type')
    if not isinstance(kind, str):
        raise ValueError('invalid event type')
    if kind in {'tap', 'hover', 'scroll', 'pinch', 'pan'}:
        x, y = number(event, 'x', APP_W), number(event, 'y', APP_H)
        if not (0 <= x < APP_W and 0 <= y < APP_H):
            raise ValueError('point outside frame')
    if kind == 'tap':
        if type(event.get('button', 1)) is not int or event.get('button', 1) not in (1, 2, 3):
            raise ValueError('invalid button')
    elif kind == 'scroll':
        number(event, 'notches', 8)
    elif kind == 'pinch':
        if not 0.25 <= number(event, 'ratio', 4) <= 4:
            raise ValueError('invalid pinch ratio')
    elif kind == 'pan':
        number(event, 'dx', APP_W)
        number(event, 'dy', APP_H)
    elif kind == 'key':
        name = event.get('key')
        if not isinstance(name, str) or not (name in KEYS or len(name) == 1 and 33 <= ord(name) <= 126):
            raise ValueError('invalid key')
        if not isinstance(event.get('down'), bool):
            raise ValueError('invalid key state')
    elif kind not in {'tap', 'hover', 'release'}:
        raise ValueError('unknown event')
    for flag in ('ctrl', 'shift'):
        if flag in event and not isinstance(event[flag], bool):
            raise ValueError(f'invalid {flag}')
    return event


def apply_event(scene, event):
    kind = event['type']
    at = (event['x'], event['y']) if 'x' in event else None
    if kind == 'tap':
        scene.tap(at, event.get('button', 1), event.get('ctrl', False))
    elif kind == 'hover':
        scene.hover(at)
    elif kind == 'key':
        name = event['key']
        if scene.canvas and name in MAP_KEYS:
            (scene.held.add if event['down'] else scene.held.discard)(name)
        elif event['down']:
            scene.key(name, event.get('ctrl', False), event.get('shift', False))
    elif kind == 'release':
        scene.held.clear()
    elif scene.canvas:
        if on_map(at) and not scene.panel_contains(at):
            if kind == 'pan':
                scene.canvas.pan(event['dx'] * scene.map_size[0] / MAP_BOX[2],
                                 event['dy'] * scene.map_size[1] / MAP_BOX[3])
            elif kind == 'scroll':
                scene.canvas.scroll(event['notches'], scene.map_xy(at))
            elif kind == 'pinch':
                scene.canvas.pinch(event['ratio'], scene.map_xy(at))


def changed_rects(old, new):
    """Rectangles (x, y, w, h) where `new` differs from `old`: [] if none, None if most of it."""
    if old is None:
        return None
    diff = ImageChops.difference(old, new)
    bbox = diff.getbbox()
    if bbox is None:
        return []
    rects = []
    for top in range(bbox[1] // BAND * BAND, bbox[3], BAND):
        box = diff.crop((0, top, new.width, min(new.height, top + BAND))).getbbox()
        if box is None:
            continue
        x0, y0, x1, y1 = box[0], top + box[1], box[2], top + box[3]
        if rects:
            px, py, pw, ph = rects[-1]
            if py + ph == y0 and x0 < px + pw and px < x1:   # touches the band above: join
                x0, x1 = min(px, x0), max(px + pw, x1)
                rects[-1] = (x0, py, x1 - x0, y1 - py)
                continue
        rects.append((x0, y0, x1 - x0, y1 - y0))
    if sum(w * h for _, _, w, h in rects) > WHOLE * new.width * new.height:
        return None
    return rects


def strip(image, rects):
    """The rectangles stacked top to bottom in one lossless PNG."""
    out = Image.new('RGB', (max(w for _, _, w, _ in rects), sum(h for _, _, _, h in rects)))
    y = 0
    for x0, y0, w, h in rects:
        out.paste(image.crop((x0, y0, x0 + w, y0 + h)), (0, y))
        y += h
    return png(out)


def png(image):
    out = io.BytesIO()
    image.save(out, format='PNG', compress_level=1)
    return out.getvalue()


class Session:
    def __init__(self, scene):
        self.scene = scene
        self.seen = self.input_at = time.monotonic()
        self.clock_revision = -1
        self.revision = 0
        self.image = None                  # the page's latest frame
        self.full = None                   # its PNG, encoded only when a page needs it whole
        self.delta = None                  # (rects, PNG strip) from revision - 1 to revision


class Renderer(threading.Thread):
    """Owns all Pygame state. HTTP threads pass bounded commands through this queue."""

    def __init__(self, scene_factory=Scene, link=None):
        super().__init__(daemon=True)
        self.frames, self.commands = queue.Queue(maxsize=8), queue.Queue(maxsize=256)
        self.link = link if link is not None else Link(self.frames)
        self.scene_factory = scene_factory
        self.sessions = {}
        self.workers = {'picture': None, 'scene': None}
        self.pool = ThreadPoolExecutor(max_workers=2)
        self.base = None
        self.mirror, self.fitted, self.revision = Mirror(), Fitted(SIZE), 0
        self.stopping = threading.Event()

    def call(self, kind, payload=None):
        future = Future()
        try:
            self.commands.put_nowait((future, kind, payload))
        except queue.Full:
            raise Busy('display is busy')
        try:
            return future.result(timeout=20)
        except TimeoutError:
            future.cancel()
            raise Busy('display is busy')

    def execute(self, kind, payload):
        if kind == 'open':
            if len(self.sessions) >= MAX_SESSIONS:
                raise Busy('too many open pages')
            options = {}
            if self.scene_factory is Scene:
                if self.base is None:
                    from activity import Map
                    self.base = Map()
                options = dict(canvas_options=dict(base=self.base, workers=self.workers),
                               panel_options=dict(base=self.base, pool=self.pool))
            session = Session(self.scene_factory(SIZE, self.link, **options))
            token = secrets.token_urlsafe(24)
            self.sessions[token] = session
            return dict(session=token, width=SIZE[0], height=SIZE[1])
        token = payload['session']
        if not isinstance(token, str):
            raise ValueError('invalid session')
        session = self.sessions.get(token)
        if session is None:
            raise Gone('page expired; reload')
        session.seen = time.monotonic()
        scene = session.scene
        if kind == 'close':
            scene.close()
            del self.sessions[token]
        elif kind == 'input':
            session.input_at = session.seen
            for event in payload['events']:
                apply_event(scene, event)
        elif kind == 'frame':
            if self.fitted.image is not None and session.clock_revision != self.revision:
                scene.fitted.image = self.fitted.image
                scene.clock_dirty = True
                session.clock_revision = self.revision
            if scene.draw():
                # Lossless, drawn only when a page asks; only the changed parts are encoded.
                image = Image.frombytes('RGB', SIZE, pygame.image.tobytes(scene.frame, 'RGB'))
                rects = changed_rects(session.image, image)
                if rects != []:
                    session.delta = (rects, strip(image, rects)) if rects else None
                    session.image, session.full = image, None
                    session.revision += 1
            if session.image is None:
                raise Busy('waiting for HamClock')
            after = payload['after']
            if after == session.revision:
                return None
            if after == session.revision - 1 and session.delta:
                return session.revision, session.delta[1], session.delta[0]
            if session.full is None:
                session.full = png(session.image)
            return session.revision, session.full, None

    def run(self):
        pygame.display.init()
        pygame.display.set_mode((1, 1))
        self.link.start()
        previous = time.monotonic()
        try:
            while not self.stopping.is_set():
                now = time.monotonic()
                dt, previous = min(0.1, now - previous), now
                while not self.frames.empty():
                    changed = self.mirror.apply(*self.frames.get())
                    if self.mirror.image is not None and changed != []:
                        self.fitted.update(self.mirror.image, changed)
                        self.revision += 1
                for token, session in list(self.sessions.items()):
                    if now - session.seen > IDLE_S:
                        session.scene.close()
                        del self.sessions[token]
                    else:
                        if now - session.input_at > 3:
                            session.scene.held.clear()
                        try:
                            session.scene.tick(dt)
                        except Exception:
                            log.exception('map update failed')
                            session.scene.close()
                            del self.sessions[token]
                for _ in range(32):
                    try:
                        future, kind, payload = self.commands.get_nowait()
                    except queue.Empty:
                        break
                    if not future.set_running_or_notify_cancel():
                        continue
                    try:
                        future.set_result(self.execute(kind, payload))
                    except Exception as e:
                        future.set_exception(e)
                self.stopping.wait(1 / 30)
        finally:
            for session in self.sessions.values():
                session.scene.close()
            self.sessions.clear()
            self.pool.shutdown(wait=False, cancel_futures=True)
            for worker in self.workers.values():
                if worker:
                    worker.shutdown(wait=False, cancel_futures=True)
            pygame.display.quit()

    def stop(self):
        self.stopping.set()
        self.join(timeout=25)


class Server(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address, renderer):
        self.renderer = renderer
        self.address_family = socket.AF_INET6 if ':' in address[0] else socket.AF_INET
        super().__init__(address, Handler)

    def server_bind(self):
        if self.address_family == socket.AF_INET6:
            self.socket.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 0)
        super().server_bind()


class Handler(BaseHTTPRequestHandler):
    def setup(self):
        super().setup()
        self.connection.settimeout(10)

    def reply(self, status, body=b'', content_type='text/plain; charset=utf-8', revision=None, rects=None):
        self.send_response(status)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        if revision is not None:
            self.send_header('X-Frame-Revision', str(revision))
        if rects is not None:                # a strip of these rectangles over revision - 1
            self.send_header('X-Frame-Base', str(revision - 1))
            self.send_header('X-Frame-Rects', ';'.join(','.join(map(str, r)) for r in rects))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        route = urlsplit(self.path)
        try:
            if route.path == '/':
                self.reply(200, Path(__file__).with_name('hcweb.html').read_bytes(), 'text/html; charset=utf-8')
            elif route.path == '/frame.png':
                args = parse_qs(route.query)
                result = self.server.renderer.call('frame', dict(session=args['session'][0],
                                                                after=int(args.get('after', ['-1'])[0])))
                if result is None:
                    self.reply(204)
                else:
                    revision, body, rects = result
                    self.reply(200, body, 'image/png', revision, rects)
            else:
                self.reply(404)
        except (KeyError, ValueError):
            self.reply(400, b'invalid request')
        except Gone as e:
            self.reply(410, str(e).encode())
        except Busy as e:
            self.reply(503, str(e).encode())
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception:
            log.exception('HTTP frame failed')
            self.reply(500, b'display unavailable')

    def do_POST(self):
        origin = self.headers.get('Origin')
        if origin and (urlsplit(origin).scheme not in ('http', 'https')
                       or urlsplit(origin).netloc != self.headers.get('Host', '')):
            self.reply(403)
            return
        try:
            length = int(self.headers.get('Content-Length', '0'))
            if not 0 < length <= 8192:
                raise ValueError('invalid body length')
            data = json.loads(self.rfile.read(length))
            if not isinstance(data, dict):
                raise ValueError('invalid request')
            route = urlsplit(self.path).path
            if route == '/session':
                result = self.server.renderer.call('open')
                self.reply(200, json.dumps(result).encode(), 'application/json')
            elif route == '/input':
                events = data.get('events')
                if not isinstance(data.get('session'), str) or not isinstance(events, list) or not 1 <= len(events) <= 64:
                    raise ValueError('invalid input')
                for event in events:
                    validate_event(event)
                self.server.renderer.call('input', data)
                self.reply(204)
            elif route == '/close':
                self.server.renderer.call('close', data)
                self.reply(204)
            else:
                self.reply(404)
        except (KeyError, ValueError) as e:
            self.reply(400, str(e).encode())
        except Gone as e:
            self.reply(410, str(e).encode())
        except Busy as e:
            self.reply(503, str(e).encode())
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception:
            log.exception('HTTP request failed')
            self.reply(500, b'display unavailable')

    def log_message(self, *_):
        pass


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--host', default='127.0.0.1')
    parser.add_argument('--port', type=int, default=8075)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO)
    renderer = Renderer()
    with Server((args.host, args.port), renderer) as server:
        renderer.start()
        host = f'[{args.host}]' if ':' in args.host else args.host
        log.info('HamClock HTTP: http://%s:%s', host, args.port)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            renderer.stop()


if __name__ == '__main__':
    main()
