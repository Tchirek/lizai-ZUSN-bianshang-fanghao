"""Exercise real map rendering and HTTP sessions without a station or terrain downloads."""
import os
os.environ['SDL_VIDEODRIVER'] = 'dummy'
os.environ['PYGAME_HIDE_SUPPORT_PROMPT'] = '1'
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
import io
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
import urllib.error
import urllib.request

import numpy as np
import pygame
from PIL import Image
import activity as a
import hcdisplay as display
import hcweb as web
import map_panel as panel
import map_view as maps


class Link:
    def __init__(self):
        self.sent = []

    def start(self):
        pass

    def send(self, message):
        self.sent.append(message)


def ground(base, origin, scale, size):
    y, x = np.mgrid[:size[1], :size[0]]
    x, y = x / scale + origin[0], y / scale + origin[1]
    rgb = np.stack([35 + (x // 30 % 2) * 35, 50 + (y // 30 % 2) * 30,
                    np.full_like(x, 45)], axis=-1).astype(np.uint8)
    return Image.fromarray(rgb), 33


class Fixture:
    """Actual Canvas/MapPanel, with synthetic ground and a shared clock frame."""

    def __enter__(self):
        self.stack = ExitStack()
        folder = self.stack.enter_context(tempfile.TemporaryDirectory())
        (Path(folder) / 'rangemap.json').write_text(json.dumps(dict(km_per_px=.05, our_agl_m=16)))
        (Path(folder) / 'rangemap_osm.json').write_text('{"lines":[]}')
        for module in (a, display):
            self.stack.enter_context(patch.object(module, 'MODEL', folder))
        self.stack.enter_context(patch.object(maps, 'ground', ground))
        self.stack.enter_context(patch.object(panel, 'heights', lambda lat, lon: (np.zeros_like(lat), 12)))
        self.base = object.__new__(a.Map)
        self.base.lat0, self.base.lon0, self.base.km_per_px, self.base.labels = 30, 105, .05, []
        self.workers = {kind: self.stack.enter_context(ThreadPoolExecutor(1)) for kind in ('picture', 'scene')}
        self.pool = self.stack.enter_context(ThreadPoolExecutor(2))
        self.link = Link()
        self.renderer = web.Renderer(scene_factory=self.scene, link=self.link)
        self.server = web.Server(('127.0.0.1', 0), self.renderer)
        self.renderer.start()
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.url = f'http://127.0.0.1:{self.server.server_port}'
        self.source = pygame.Surface((1600, 960))
        self.source.fill((12, 25, 55))
        pygame.draw.rect(self.source, (180, 80, 30), (5, 100, 250, 70))
        self.renderer.frames.put(('full', self.source))
        return self

    def scene(self, size, link):
        return display.Scene(size, link, canvas_options=dict(base=self.base, workers=self.workers),
                             panel_options=dict(base=self.base, pool=self.pool))

    def request(self, path, data=None, headers=None):
        body = json.dumps(data).encode() if data is not None else None
        request = urllib.request.Request(self.url + path, body, headers or {})
        try:
            response = urllib.request.urlopen(request, timeout=25)
        except urllib.error.HTTPError as e:
            response = e
        with response:
            return response.status, response.headers, response.read()

    def open(self):
        status, _, body = self.request('/session', {})
        assert status == 200, (status, body)
        return json.loads(body)['session']

    def input(self, token, *events):
        return self.request('/input', dict(session=token, events=list(events)))[0]

    def frame(self, token, after=-1):
        return self.request(f'/frame.png?session={token}&after={after}')

    def __exit__(self, *args):
        self.server.shutdown()
        self.thread.join()
        self.server.server_close()
        self.renderer.stop()
        return self.stack.__exit__(*args)


class Tests(unittest.TestCase):
    def setUp(self):
        self.fixture = Fixture().__enter__()
        self.addCleanup(self.fixture.__exit__, None, None, None)

    def test_lossless_http_frame_matches_native_scene_pixel_for_pixel(self):
        f = self.fixture
        token = f.open()
        native = f.scene(web.SIZE, f.link)
        self.addCleanup(native.close)
        native.update(f.source)
        native.tick(0)
        self.assertTrue(native.draw())
        status, _, png = f.frame(token)
        self.assertEqual(status, 200)
        self.assertEqual(Image.open(io.BytesIO(png)).convert('RGB').tobytes(),
                         pygame.image.tobytes(native.frame, 'RGB'))

    def test_two_pages_and_native_have_independent_pan_zoom_and_popups(self):
        f = self.fixture
        first, second = f.open(), f.open()
        native = f.scene(web.SIZE, f.link)
        self.addCleanup(native.close)
        one, two = (f.renderer.sessions[t].scene for t in (first, second))
        self.assertEqual(f.input(first, dict(type='scroll', x=500, y=300, notches=4)), 204)
        time.sleep(.2)
        self.assertGreater(one.canvas.camera.zoom, 1)
        self.assertEqual(two.canvas.camera.zoom, 1)
        self.assertEqual(native.canvas.camera.zoom, 1)
        centre = one.canvas.camera.centre
        self.assertEqual(f.input(first, dict(type='pan', x=500, y=300, dx=70, dy=20)), 204)
        self.assertNotEqual(one.canvas.camera.centre, centre)
        self.assertEqual(two.canvas.camera.centre, (maps.W / 2, maps.H / 2))
        self.assertEqual(f.input(first, dict(type='tap', x=400, y=300)), 204)
        self.assertIsNotNone(one.inspector.target)
        self.assertIsNone(two.inspector.target)
        self.assertIsNone(native.inspector.target)
        self.assertEqual(f.input(second, dict(type='tap', x=650, y=350)), 204)
        self.assertNotEqual(one.inspector.target, two.inspector.target)
        self.assertEqual(f.input(first, dict(type='key', key='Escape', down=True)), 204)
        self.assertIsNone(one.inspector.point)
        self.assertIsNotNone(two.inspector.point)
        self.assertEqual(f.link.sent, [])                 # browsing never reaches the engine

    def test_station_keys_are_forwarded_and_releasing_keys_stops_local_motion(self):
        f = self.fixture
        token = f.open()
        self.assertEqual(f.input(token, dict(type='key', key='Enter', down=True)), 204)
        self.assertEqual(f.link.sent, ['set_char?char=Enter&mod='])
        scene = f.renderer.sessions[token].scene
        self.assertEqual(f.input(token, dict(type='key', key='+', down=True)), 204)
        self.assertIn('+', scene.held)
        self.assertEqual(f.input(token, dict(type='release')), 204)
        self.assertEqual(scene.held, set())

    def test_popup_blocks_gestures_and_closes_when_tapping_another_clock_pane(self):
        f = self.fixture
        token = f.open()
        self.assertEqual(f.input(token, dict(type='tap', x=400, y=300)), 204)
        self.assertEqual(f.frame(token)[0], 200)
        scene = f.renderer.sessions[token].scene
        box = scene.inspector.box
        x, y = box.centerx / scene.k, box.centery / scene.k
        centre, zoom = scene.canvas.camera.centre, scene.canvas.camera.goal
        self.assertEqual(f.input(token, dict(type='scroll', x=x, y=y, notches=2),
                                      dict(type='pan', x=x, y=y, dx=30, dy=20)), 204)
        self.assertEqual(scene.canvas.camera.centre, centre)
        self.assertEqual(scene.canvas.camera.goal, zoom)
        self.assertEqual(f.input(token, dict(type='tap', x=20, y=80)), 204)
        self.assertIsNone(scene.inspector.point)
        self.assertTrue(scene.map_dirty)
        self.assertEqual(f.link.sent, ['set_touch?x=20&y=80&button=0'])

    def test_invalid_input_is_rejected_before_any_event_is_applied(self):
        f = self.fixture
        token = f.open()
        scene = f.renderer.sessions[token].scene
        for bad in ({'type': []}, {'type': 'tap', 'x': float('nan'), 'y': 2},
                    {'type': 'key', 'key': 'Enter&set_restart', 'down': True},
                    {'type': 'tap', 'x': 2, 'y': 2, 'button': True}):
            self.assertEqual(f.input(token, dict(type='key', key='+', down=True), bad), 400)
            self.assertEqual(scene.held, set())
        self.assertEqual(f.request('/input', dict(session=token, events=[dict(type='release')]),
                                   {'Origin': 'http://another-host'})[0], 403)
        self.assertEqual(f.request('/close', {'session': []})[0], 400)
        self.assertEqual(f.frame('not-a-session')[0], 410)

    def test_idle_and_closed_pages_release_views_without_stopping_shared_workers(self):
        f = self.fixture
        first, second = f.open(), f.open()
        one = f.renderer.sessions[first].scene
        self.assertEqual(f.request('/close', dict(session=first))[0], 204)
        self.assertIsNone(one.inspector.point)
        self.assertEqual(f.frame(first)[0], 410)
        self.assertEqual(f.frame(second)[0], 200)
        f.renderer.sessions[second].seen = time.monotonic() - web.IDLE_S - 1
        time.sleep(.1)
        self.assertEqual(f.frame(second)[0], 410)
        self.assertEqual(f.frame(f.open())[0], 200)

    def test_frame_revision_and_page_cap(self):
        f = self.fixture
        with patch.object(web, 'MAX_SESSIONS', 2):
            first, second = f.open(), f.open()
            self.assertEqual(f.request('/session', {})[0], 503)
            status, headers, _ = f.frame(first)
            self.assertEqual(status, 200)
            scene = f.renderer.sessions[first].scene
            with patch.object(scene, 'draw', return_value=False):
                self.assertEqual(f.frame(first, int(headers['X-Frame-Revision']))[0], 204)
            self.assertEqual(f.request('/close', dict(session=second))[0], 204)
            self.assertTrue(f.open())


if __name__ == '__main__':
    unittest.main()
