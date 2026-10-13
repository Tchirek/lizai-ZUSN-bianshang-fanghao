"""Exercise real map rendering and HTTP sessions without a station or terrain downloads."""
import os
os.environ['SDL_VIDEODRIVER'] = 'dummy'
os.environ['PYGAME_HIDE_SUPPORT_PROMPT'] = '1'
import base64
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
import io
import json
import http.client
from pathlib import Path
import tempfile
import socket
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
from webmap import MapData


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

    def __init__(self, clock=False):
        self.clock = clock

    def __enter__(self):
        self.stack = ExitStack()
        folder = self.stack.enter_context(tempfile.TemporaryDirectory())
        (Path(folder) / 'rangemap.json').write_text(json.dumps(dict(km_per_px=.05, our_agl_m=16,
                 reach_km=20, far_lat=30.1, far_lon=105.1, bearing=45, far_elev_m=100)))
        (Path(folder) / 'rangemap_osm.json').write_text('{"lines":[],"places":[]}')
        for name in ('government.json', 'embassies.json'):
            (Path(folder) / name).write_text('[]')
        self.stack.enter_context(patch.object(maps, 'OFFICES', str(Path(folder) / 'government.json')))
        self.stack.enter_context(patch.object(maps, 'EMBASSIES', str(Path(folder) / 'embassies.json')))
        for level in (1, 2):
            (Path(folder) / 'tiles' / str(level)).mkdir(parents=True)
            for x in range((a.W * level + 255) // 256):
                for y in range((a.H * level + 255) // 256):
                    Image.new('RGB', (256, 256), (35 + x * 5, 50 + y * 5, 45)).save(Path(folder) / 'tiles' / str(level) / f'{x}_{y}.png')
        (Path(folder) / 'tiles/levels.json').write_text(json.dumps(dict(levels=[1, 2], tile=256, w=a.W, h=a.H)))
        if self.clock:
            (Path(folder) / 'activity.json').write_text('{"reports":[],"aprs":{},"qsos":[]}')
            Image.new('RGB', (16, 16), '#ffba44').save(Path(folder) / 'sdo.png')
        for module in (a, display):
            self.stack.enter_context(patch.object(module, 'MODEL', folder))
        self.stack.enter_context(patch.object(maps, 'ground', ground))
        self.stack.enter_context(patch.object(panel, 'heights', lambda lat, lon: (np.zeros_like(lat), 12)))
        self.base = object.__new__(a.Map)
        self.base.lat0, self.base.lon0, self.base.km_per_px, self.base.labels = 30, 105, .05, []
        self.workers = {kind: self.stack.enter_context(ThreadPoolExecutor(1)) for kind in ('picture', 'scene')}
        self.pool = self.stack.enter_context(ThreadPoolExecutor(2))
        self.link = Link()
        self.renderer = web.Renderer(scene_factory=web.ClockScene if self.clock else self.scene, link=self.link)
        self.map_data = MapData(base=self.base, model=folder)
        self.server = web.Server(('127.0.0.1', 0), self.renderer, map_data=self.map_data)
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

    def test_the_native_popup_turns_the_weather_pane_while_open(self):
        f, calls = self.fixture, []

        class Weather:
            def show(self, owner, lat, lon):
                calls.append(('show', owner))

            def close(self, owner):
                calls.append(('close', owner))
        native = f.scene(web.SIZE, f.link)
        self.addCleanup(native.close)
        native.weather = Weather()
        native.tap((500, 300))                           # a place on the map: its popup
        native.tap((300, 250))                           # another place
        self.assertEqual(calls, [('show', 'native'), ('show', 'native')])
        self.assertEqual(native.popup(), native.inspector.target)
        native.key('Escape')
        self.assertEqual(calls[-1], ('close', 'native'))
        native.key('Escape')                             # nothing open: HamClock's key, no change
        self.assertEqual(len(calls), 3)

    def test_native_search_consumes_keys_and_locates_without_station_input(self):
        f = self.fixture
        native = f.scene(web.SIZE, f.link)
        self.addCleanup(native.close)
        native.update(f.source)
        native.key('s')
        self.assertIsNotNone(native.search)
        native.search.future.result(timeout=10)
        native.tick(0)
        native.key('Enter')
        self.assertIsNotNone(native.search)
        native.search.rows = [dict(name='Test airport', point=[700, 350], colour='#96afff', position='30.0000N 105.0000E'),
                              dict(name='Test town', point=[800, 350], colour='#969696', position='30.0000N 105.0000E')]
        native.search.query = 'test'
        native.search.filter()
        native.draw()
        native.key('ArrowDown')
        self.assertEqual(native.search.selected, 1)
        native.key('Enter')
        self.assertIsNone(native.search)
        self.assertEqual(native.canvas.camera.centre, (800, 350))
        self.assertEqual(native.canvas.camera.zoom, 8)
        np.testing.assert_allclose(native.inspector.target, f.base.latlon(800, 350))
        self.assertEqual(f.link.sent, [])
        native.key('S')
        native.key('Escape')
        self.assertIsNone(native.search)
        self.assertIsNotNone(native.inspector.point)

    def test_search_repeat_empty_close_hover_restore_and_long_names(self):
        import dots
        f = self.fixture
        native = f.scene(web.SIZE, f.link)
        self.addCleanup(native.close)
        native.update(f.source)
        previous_repeat = pygame.key.get_repeat()
        native.key('s')
        panel = native.search
        panel.future.result(timeout=10)
        native.tick(0)
        panel.rows = [dict(name='Test ' + str(i), point=[700, 350], colour='#96afff',
                           position='30.0000N 105.0000E') for i in range(3)]
        panel.query = 'test'
        panel.filter()
        native.draw()
        self.assertEqual(pygame.key.get_repeat(), (400, 40))
        panel.hover((panel.box.x + 8, panel.box.y + panel.row_height * 3 + 2))
        self.assertEqual((panel.hovered, panel.selected), (2, 0))
        panel.hover(None)
        self.assertEqual((panel.hovered, panel.selected), (None, 0))
        native.key('ArrowDown')
        panel.hover((panel.box.x + 8, panel.box.y + panel.row_height + 2))
        self.assertEqual((panel.hovered, panel.selected), (0, 1))
        panel.hover(None)
        self.assertEqual(panel.selected, 1)
        shown = dots.fit('Very long name ' * 20, 120)
        self.assertTrue(shown.endswith(('…', '...')))
        self.assertLessEqual(dots.width(shown), 120)
        self.assertEqual(dots.fit('Short', 120), 'Short')
        self.assertEqual(dots.fit('Long', 0), '')
        for _ in range(8):
            native.key('Backspace', repeat=True)
        self.assertEqual(panel.query, '')
        self.assertIs(native.search, panel)
        native.key('Backspace')
        self.assertIsNone(native.search)
        self.assertEqual(pygame.key.get_repeat(), previous_repeat)
        self.assertEqual(f.link.sent, [])

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

    def test_follow_up_frames_carry_only_changed_rectangles_and_rebuild_exactly(self):
        f = self.fixture
        token = f.open()
        status, headers, png = f.frame(token)
        self.assertEqual(status, 200)
        self.assertNotIn('X-Frame-Rects', headers)
        page = Image.open(io.BytesIO(png)).convert('RGB')
        revision = int(headers['X-Frame-Revision'])
        # The clock changes in one small corner, away from the map.
        pygame.draw.rect(f.source, (240, 240, 20), (20, 20, 60, 30))
        f.renderer.frames.put(('update', [(0, 0, f.source.subsurface((0, 0, 160, 80)).copy())]))
        time.sleep(.2)
        status, headers, png = f.frame(token, revision)
        self.assertEqual(status, 200)
        self.assertEqual(int(headers['X-Frame-Base']), revision)
        rects = [tuple(map(int, r.split(','))) for r in headers['X-Frame-Rects'].split(';')]
        piece = Image.open(io.BytesIO(png)).convert('RGB')
        self.assertLess(piece.width * piece.height, web.SIZE[0] * web.SIZE[1] / 20)
        y = 0
        for x0, y0, w, h in rects:
            page.paste(piece.crop((0, y, w, y + h)), (x0, y0))
            y += h
        whole = f.renderer.sessions[token].image
        self.assertEqual(page.tobytes(), whole.tobytes())
        # A page that missed a frame gets it whole.
        status, headers, png = f.frame(token, revision - 1)
        self.assertEqual(status, 200)
        self.assertNotIn('X-Frame-Rects', headers)
        self.assertEqual(Image.open(io.BytesIO(png)).convert('RGB').tobytes(), whole.tobytes())

    def test_changed_rects(self):
        old = Image.new('RGB', (200, 100))
        self.assertIsNone(web.changed_rects(None, old))
        self.assertEqual(web.changed_rects(old, old.copy()), [])
        new = old.copy()
        new.paste((255, 0, 0), (10, 5, 20, 40))          # crosses a band boundary: one rectangle
        new.paste((0, 255, 0), (150, 90, 160, 95))
        self.assertEqual(web.changed_rects(old, new), [(10, 5, 10, 35), (150, 90, 10, 5)])
        self.assertIsNone(web.changed_rects(old, Image.new('RGB', (200, 100), (1, 1, 1))))

    def test_gamepad_stick_hat_and_shoulders_move_the_map(self):
        class Pad:
            def __init__(self, axes=(0, 0), hat=(0, 0), buttons=()):
                self.axes, self.hat, self.buttons = axes, hat, buttons
            get_numaxes = lambda self: len(self.axes)
            get_axis = lambda self, i: self.axes[i]
            get_numhats = lambda self: 1
            get_hat = lambda self, i: self.hat
            get_numbuttons = lambda self: 11
            get_button = lambda self, i: i in self.buttons
        self.assertEqual(display.pad_motion(Pad((.1, -.15))), (0, 0, 0))
        self.assertEqual(display.pad_motion(Pad(hat=(-1, 1))), (1, 1, 0))     # as ArrowLeft + ArrowUp
        vx, vy, _ = display.pad_motion(Pad((1, .6)))
        self.assertEqual(vx, -1)
        self.assertAlmostEqual(vy, -.5)
        self.assertEqual(display.pad_motion(Pad(buttons={5}))[2], 1)
        self.assertEqual(display.pad_motion(Pad(buttons={4, 5}))[2], 0)

    @unittest.skipUnless(socket.has_dualstack_ipv6(), 'dual-stack IPv6 unavailable')
    def test_all_interface_listener_accepts_ipv4_and_ipv6(self):
        with web.Server(('::', 0), self.fixture.renderer) as server:
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                for host in ('127.0.0.1', '::1'):
                    connection = http.client.HTTPConnection(host, server.server_port, timeout=5)
                    try:
                        try:
                            connection.request('GET', '/')
                        except PermissionError:
                            if host == '::1' and os.name == 'nt':
                                self.skipTest('this Windows host blocks IPv6 loopback connections')
                            raise
                        response = connection.getresponse()
                        self.assertEqual(response.status, 200)
                        self.assertIn(b'<canvas', response.read())
                    finally:
                        connection.close()
            finally:
                server.shutdown()
                thread.join()


class MapTests(unittest.TestCase):
    def setUp(self):
        self.fixture = Fixture(clock=True).__enter__()
        self.addCleanup(self.fixture.__exit__, None, None, None)

    def test_browser_sessions_do_not_construct_server_side_maps(self):
        f = self.fixture
        first, second = f.open(), f.open()
        for token in (first, second):
            scene = f.renderer.sessions[token].scene
            self.assertIsNone(scene.canvas)
            self.assertIsNone(scene.inspector)
        self.assertTrue(all(worker is None for worker in f.renderer.workers.values()))
        status, _, data = f.frame(first)
        self.assertEqual(status, 200)
        image = Image.open(io.BytesIO(data))
        self.assertEqual(image.getpixel((600, 500)), (0, 0, 0))

    def test_configuration_tiles_and_cache_validation(self):
        f = self.fixture
        status, _, body = f.request('/map/config')
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)['levels'], [1, 2])
        status, headers, body = f.request('/map/tiles/1/0/0.png')
        self.assertEqual(status, 200)
        self.assertEqual(Image.open(io.BytesIO(body)).size, (256, 256))
        self.assertIn('max-age', headers['Cache-Control'])
        self.assertEqual(f.request('/map/tiles/1/0/0.png', headers={'If-None-Match': headers['ETag']})[0], 304)
        for path in ('/map/tiles/99/0/0.png', '/map/tiles/1/900/0.png', '/map/tiles/1/../station.json'):
            self.assertEqual(f.request(path)[0], 404)

    def test_projected_point_uses_the_existing_reachability_calculation(self):
        f = self.fixture
        point = f.map_data.coordinate((700, 330))
        status, _, body = f.request('/map/reachability', dict(x=point[0], y=point[1]))
        self.assertEqual(status, 200)
        result = json.loads(body)
        latitude, longitude = f.base.latlon(700, 330)
        expected = panel.path_profile(f.base, latitude, longitude)
        self.assertAlmostEqual(result['distance'], expected['distance'], places=5)
        self.assertEqual(result['status'], expected['status'])
        self.assertLessEqual(len(result['ground']), 322)
        self.assertEqual(f.input(f.open(), dict(type='release')), 204)
        self.assertEqual(f.link.sent, [])
        for value in (True, float('nan'), 1e20):
            self.assertEqual(f.request('/map/reachability', dict(x=value, y=0))[0], 400)

    def test_features_and_grid_are_local_map_data_asked_for_a_view(self):
        f = self.fixture
        query='?bbox='+','.join(map(str, f.map_data.extent))+'&zoom=1'
        status, _, body = f.request('/map/features'+query)
        self.assertEqual(status, 200)
        features = json.loads(body)['features']
        self.assertTrue(any(item['properties']['kind']=='station' for item in features))
        status, _, body = f.request('/map/grid'+query)
        self.assertEqual(status, 200)
        self.assertTrue(json.loads(body)['features'])
        self.assertEqual(f.request('/map/grid?bbox=nan,0,1,1')[0], 400)
        # the whole map at 48x is no page's view: refused instead of worked out
        self.assertEqual(f.request('/map/features' + query.replace('zoom=1', 'zoom=48'))[0], 400)

    def sunset(self):
        """The minute the sun sets at the station on an equinox (the terminator through the map)."""
        _, down = panel.sun_times(self.fixture.base.lat0, self.fixture.base.lon0, 1774008000)
        return int(down // 60)

    def test_night_is_the_native_maps_night_at_the_pages_px(self):
        f, minute = self.fixture, self.sunset()
        data, size = f.map_data, (round(a.W * .5), round(a.H * .5))
        _, _, line = maps.shade(f.base, (0, 0), .5, size, minute * 60)
        self.assertIsNotNone(line)
        (x, y, w, h), padded = line
        native = np.frombuffer(padded, np.uint8).reshape(h + 2, w + 2, 4)[1:-1, 1:-1]
        result = json.loads(data.terminator(tuple(data.extent), *size, 1, minute))
        mine = np.asarray(Image.open(io.BytesIO(base64.b64decode(result['line']['png'].split(',')[1]))))
        self.assertEqual((result['line']['x'], result['line']['y']), (x, y))
        np.testing.assert_array_equal(mine, native)              # the same gold line, px for px
        tag = result['tag']
        self.assertEqual(tag['name'], 'SUNSET')
        self.assertGreater(tag['lift'], 0)
        points = np.asarray(tag['points'])
        self.assertTrue(((points[:, 0] > data.extent[0]) & (points[:, 0] < data.extent[2])).all())
        self.assertTrue((np.diff(points[:, 1]) < 0).all())       # from the top down
        # at device px the line is drawn finer, not scaled: twice the px, the same place
        sharp = json.loads(data.terminator(tuple(data.extent), *size, 2, minute))['line']
        self.assertAlmostEqual(sharp['x'] / 2, x, delta=2)
        veil = Image.open(io.BytesIO(data.veil(tuple(data.extent), *size, minute)))
        self.assertEqual((veil.mode, veil.size), ('RGBA', size))
        alpha = np.asarray(veil)[..., 3]
        self.assertGreater(alpha[:, :x].mean() + alpha[:, x + w:].mean(), 20)   # night on one side

    def test_night_pictures_are_bounded(self):
        f = self.fixture
        bbox = ','.join(map(str, f.map_data.extent))
        status, headers, body = f.request(f'/map/veil.png?bbox={bbox}&w=330&h=165')
        self.assertEqual(status, 200)
        self.assertEqual(Image.open(io.BytesIO(body)).size, (330, 165))
        status, _, body = f.request(f'/map/terminator?bbox={bbox}&w=330&h=165&ratio=2')
        self.assertEqual(status, 200)
        self.assertIsInstance(json.loads(body), dict)
        for query in ('w=0&h=10', 'w=10&h=10&ratio=5', 'w=8000&h=8000', 'w=x&h=1'):
            self.assertEqual(f.request(f'/map/terminator?bbox={bbox}&{query}')[0], 400)

    def test_live_layer_legend_and_qso_lines_follow_the_native_map(self):
        f = self.fixture
        (Path(f.map_data.model) / 'activity.json').write_text(json.dumps(dict(reports=[], aprs={}, qsos=[['W1AW', 'OL39']])))
        bands = {('20', (255, 250, 0)), ('70cm', (180, 120, 255)), ('80', (131, 129, 0))}
        with patch.object(a, 'digital_graph', lambda base, reports: ({}, bands, [], {}, 0)):
            result = f.map_data.live()
        self.assertEqual([b['name'] for b in result['legend']['bands']], ['80', '20', '70cm'])
        kinds = {item['properties']['kind']: item['geometry'] for item in result['features']['features']}
        self.assertEqual(kinds['qso_path']['coordinates'][0], [0, 0])               # from us
        self.assertEqual(kinds['qso_path']['coordinates'][1], kinds['qso']['coordinates'])

    def test_a_pages_popup_turns_the_weather_pane_and_its_end_gives_it_back(self):
        f, calls = self.fixture, []

        class Weather:
            def show(self, owner, lat, lon):
                calls.append(('show', owner, round(lat, 4), round(lon, 4)))

            def close(self, owner):
                calls.append(('close', owner))
        f.renderer.weather = f.server.weather = Weather()
        token = f.open()
        point = f.map_data.coordinate((700, 330))
        self.assertEqual(f.request('/map/dx', dict(session=token, x=point[0], y=point[1]))[0], 204)
        lat, lon = f.base.latlon(700, 330)
        self.assertEqual(calls, [('show', 'web:' + token, round(lat, 4), round(lon, 4))])
        self.assertEqual(f.request('/map/dx', dict(session=token, close=True))[0], 204)
        self.assertEqual(calls[-1], ('close', 'web:' + token))
        self.assertEqual(f.request('/map/dx', dict(session='nobody', x=0, y=0))[0], 410)
        self.assertEqual(f.request('/map/dx', dict(session=token, x=float('nan'), y=0))[0], 400)
        self.assertEqual(f.request('/close', dict(session=token))[0], 204)   # the page went away
        self.assertEqual(calls[-1], ('close', 'web:' + token))

    def test_search_uses_all_local_names_and_returns_projected_points(self):
        f = self.fixture
        model = f.map_data.model
        (model / 'rangemap_osm.json').write_text(json.dumps(dict(lines=[], places=[
            dict(name='测试镇', lat=30, lon=105.02), dict(name='Outside', lat=0, lon=0)])))
        (model / 'airports.json').write_text(json.dumps(dict(runways=[], airports=[
            dict(code='ZUSN', name='测试机场', point=[30, 105.03])])))
        (model / 'activity.json').write_text(json.dumps(dict(reports=[], aprs={'BG8TEST-1': [30, 105.04, True, 0]})))
        for query, name in [('测试镇', '测试镇'), ('机场', 'ZUSN 测试机场'), ('ｚｕｓｎ', 'ZUSN 测试机场'), ('bg8test', 'BG8TEST-1')]:
            status, _, body = f.request('/map/search?q=' + urllib.parse.quote(query))
            self.assertEqual(status, 200)
            rows = json.loads(body)
            self.assertEqual(rows[0]['name'], name)
            self.assertEqual(len(rows[0]['point']), 2)
            self.assertRegex(rows[0]['position'], r'^\d+\.\d{4}[NS] \d+\.\d{4}[EW]$')
            self.assertNotIn('kind', rows[0])
            self.assertEqual(rows[0]['colour'], '#00dcff' if name == 'BG8TEST-1' else
                             '#96afff' if 'ZUSN' in name else '#969696')
        rows = json.loads(f.request('/map/search?q=bg8test')[2])
        np.testing.assert_allclose(rows[0]['point'], f.map_data.coordinate(f.base.xy(30, 105.04)))
        for query in ('', 'Outside', 'no-such-place'):
            self.assertEqual(json.loads(f.request('/map/search?q=' + query)[2]), [])
        self.assertEqual(f.request('/map/search?q=' + 'x' * 129)[0], 400)

    def test_search_box_is_hamclock_pixels_scaled_with_the_clock(self):
        from map_search import WIDTH, ROW
        f = self.fixture
        native = f.scene(web.SIZE, f.link)
        self.addCleanup(native.close)
        native.update(f.source)
        native.key('s')
        panel = native.search
        panel.future.result(timeout=10)
        native.tick(0)
        panel.rows = [dict(name='Test', point=[700, 350], colour='#96afff', position='30.0000N 105.0000E')]
        panel.query = 'test'
        panel.filter()
        native.draw()
        k = native.box.w / 1320
        self.assertEqual(panel.box.size, (round(WIDTH * k), round(2 * ROW * k)))
        self.assertAlmostEqual(panel.row_height, ROW * k)
        prompt = native.frame.subsurface(panel.box.x, panel.box.y, panel.box.w, round(ROW * k))
        self.assertGreater(sum(prompt.get_at((x, y))[0] > 128 for x in range(prompt.get_width())
                               for y in range(prompt.get_height())), 20)      # >test_ is lit

    def test_dots_are_hamclock_and_x11_glyphs_as_they_are(self):
        import dots
        self.assertEqual(dots.glyph('A')[:5], (-1, -1, 12, 13, 14))     # HamClock's record at 1600x960
        self.assertEqual(dots.width('BG8TEST'), 7 * 12)
        self.assertIsNone(dots.glyph(''))
        lit = dots.glyph('A')
        self.assertEqual(len(lit.bits), (13 * 14 + 7) // 8)
        f = self.fixture
        status, _, body = f.request('/map/dots?t=' + urllib.parse.quote('AA'))
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body), {'A': [-1, -1, 12, 13, 14, lit.bits.hex()]})
        self.assertEqual(f.request('/map/dots?t=' + 'x' * 513)[0], 400)
        self.assertEqual(f.request('/vendor/search-fonts/search.ttf')[0], 404)
        if not dots.SONG.exists():
            self.skipTest('no X11 16-dot Song here')
        g = dots.glyph('遂')
        self.assertEqual(g[:5], (0, -1, 17, 17, 16))
        rows = [[g.bits[(y * g.w + x) // 8] >> 7 - (y * g.w + x) % 8 & 1 for x in range(g.w)] for y in range(g.h)]
        for row in rows:                       # printed twice, a dot apart: no run is one dot wide
            runs = ''.join(map(str, row)).split('0')
            self.assertTrue(all(len(run) != 1 for run in runs), row)
        self.assertEqual(json.loads(f.request('/map/dots?t=' + urllib.parse.quote('遂'))[2])['遂'][5], g.bits.hex())

    def test_search_ranks_exact_then_prefix_and_bounds_results(self):
        from map_search import search
        rows = [dict(name=name, point=[660, 330], kind='地点') for name in
                ['xTest', 'test station', 'ＴＥＳＴ'] + ['test ' + str(i) for i in range(20)]]
        result = search(rows, ' T e s t ')
        self.assertEqual(result[0]['name'], 'ＴＥＳＴ')
        self.assertEqual(len(result), 8)
        self.assertNotIn('xTest', [r['name'] for r in result])
        self.assertEqual(search(rows, '  '), [])

    def test_frontend_has_no_loading_text_or_spinner(self):
        f = self.fixture
        status, _, body = f.request('/')
        self.assertEqual(status, 200)
        self.assertIn(b'openlayers', body)
        self.assertNotIn(b'id="status"', body)
        self.assertNotIn('加载中'.encode(), body)

    def test_geometry_reuses_native_thinning_without_losing_line_ends(self):
        data = self.fixture.map_data
        coordinates = [[float(x), 0] for x in np.linspace(0, 1000, 10000)]
        data.static_features = [data.feature('LineString', coordinates, 'river')]
        result = data.geometry_at(0, 1)['geometry']['coordinates']
        self.assertLess(len(result), 100)
        np.testing.assert_allclose(result[0], coordinates[0])
        np.testing.assert_allclose(result[-1], coordinates[-1])


if __name__ == '__main__':
    unittest.main()
