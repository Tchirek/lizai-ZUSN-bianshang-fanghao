import os
os.environ['SDL_VIDEODRIVER'] = 'dummy'
os.environ['PYGAME_HIDE_SUPPORT_PROMPT'] = '1'
from collections import OrderedDict
import json
import math
import tempfile
import unittest
from unittest.mock import patch

import numpy as np
import pygame
from PIL import Image

import activity as a
import map_view as v


def base():
    m = object.__new__(a.Map)
    m.lat0, m.lon0, m.km_per_px, m.labels = 30.55, 105.62, .8975, []
    return m


def raster(surface, origin=(0.0, 0.0), scale=1.0):
    """A Raster of a W x H-or-so surface, padded as the worker pads its pictures."""
    rgb = np.pad(pygame.surfarray.array3d(surface).transpose(1, 0, 2), ((1, 1), (1, 1), (0, 0)), mode='edge')
    return v.Raster(origin, scale, surface.get_size(), rgb.tobytes(), 33, 0)


def blank_scene(m, scale=1.0):
    """A Scene with no geography, for drawing what a test gives it."""
    s = object.__new__(v.Scene)
    s.base, s.scale, s.glyphs, s.layer, s.budget = m, scale, OrderedDict(), None, math.inf
    s.data, s.future, s.mtime, s.checked = None, None, None, 0
    s.rivers = {level: (np.empty((0, 2)), np.zeros(1, int)) for level in (1, 2, 4, 8, 16, 32)}
    s.grid, s.marks = np.empty((0, 50, 2)), {}
    return s


def scene_data(lines=(), campuses=(), labels=()):
    d = dict(stations=[], lines=list(lines), total=len(lines), bands={}, aprs=[], links=[], qsos=[],
             places=np.empty((0, 2)), campuses=list(campuses), far=(-100, -100),
             labels=[(text, xy, 13) for text, xy, _ in labels], kinds=[kind for _, _, kind in labels],
             sizes=np.full((len(labels), 2), 30.0), spots=np.zeros((len(labels), len(v.LABEL_ZOOMS)), np.int8))
    return d


class CameraTests(unittest.TestCase):
    def test_wheel_eases_in_holding_the_point_under_the_pointer_then_back_out(self):
        camera = v.Camera(base())
        point = (300.25, 250.75)
        held = camera.view().unproject(point)
        self.assertTrue(camera.scroll(.05, point))
        last = 1
        for _ in range(120):
            camera.tick(1 / 60)
            self.assertGreaterEqual(camera.zoom, last)
            last = camera.zoom
            now = camera.view().unproject(point)
            self.assertAlmostEqual(now[0], held[0], places=8)
            self.assertAlmostEqual(now[1], held[1], places=8)
        self.assertEqual(camera.zoom, camera.goal)
        camera.scroll(-.05, point)
        for _ in range(120):
            camera.tick(1 / 60)
        self.assertEqual((camera.zoom, camera.centre), (1, (v.W / 2, v.H / 2)))
        self.assertFalse(camera.scroll(-1, point))               # already the whole map

    def test_pinch_follows_the_fingers_at_once_about_the_pointer(self):
        camera = v.Camera(base())
        point = (400, 200)
        held = camera.view().unproject(point)
        self.assertTrue(camera.pinch(1.5, point))
        self.assertEqual((camera.zoom, camera.goal), (1.5, 1.5))
        self.assertFalse(camera.tick(1 / 60))                    # nothing left to ease
        now = camera.view().unproject(point)
        self.assertAlmostEqual(now[0], held[0])
        self.assertAlmostEqual(now[1], held[1])
        for _ in range(40):
            camera.pinch(1.3, point)
        self.assertEqual(camera.zoom, v.MAX_ZOOM)
        self.assertFalse(camera.pinch(1.1, point))

    def test_maximum_zoom_stays_inside_the_map_and_round_trips(self):
        camera = v.Camera(base())
        for _ in range(5):
            camera.scroll(8, (v.W - 1, v.H - 1))
        for _ in range(180):
            camera.tick(1 / 60)
        self.assertEqual(camera.zoom, v.MAX_ZOOM)
        left, top, right, bottom = camera.view().rect()
        self.assertLessEqual(right, v.W)
        self.assertLessEqual(bottom, v.H)
        lat, lon = camera.view().latlon(417.2, 231.5)
        x, y = camera.view().xy(lat, lon)
        self.assertAlmostEqual(x, 417.2, places=6)
        self.assertAlmostEqual(y, 231.5, places=6)

    def test_pan_stays_in_the_map_and_a_later_zoom_does_not_jump(self):
        camera = v.Camera(base())
        self.assertFalse(camera.pan(50, 0))                      # nothing to pan at 1x
        camera.pinch(4, (v.W / 2, v.H / 2))
        before = camera.centre
        self.assertTrue(camera.pan(40, -20))
        self.assertAlmostEqual(camera.centre[0], before[0] - 10)
        self.assertAlmostEqual(camera.centre[1], before[1] + 5)
        point = (200, 100)
        held = camera.view().unproject(point)
        camera.scroll(-1, point)
        camera.tick(1 / 60)
        now = camera.view().unproject(point)
        self.assertLess(abs(now[0] - held[0]) + abs(now[1] - held[1]), 1e-6)
        camera.pan(1e6, 1e6)                                     # far past the corner
        self.assertAlmostEqual(camera.view().rect()[0], 0)
        self.assertAlmostEqual(camera.view().rect()[1], 0)


class PictureTests(unittest.TestCase):
    def setUp(self):
        pygame.display.init()
        pygame.display.set_mode((1, 1))

    def test_geometry_puts_the_view_on_whole_pixels_and_stops_margins_at_the_edge(self):
        m = base()
        view = v.View(m, 3.3, v.clamp((500.37, 300.11), 3.3))
        origin, s, size = v.raster_geometry(view, .8, 0)
        self.assertAlmostEqual(s, 3.3 * .8)
        self.assertEqual(size, (1056, 528))
        origin, s, size = v.raster_geometry(view, .8, .5)
        left = (view.rect()[0] - origin[0]) * s
        self.assertAlmostEqual(left, round(left), places=6)
        self.assertEqual(size, (1056 * 2, 528 * 2))
        corner = v.View(m, 4, v.clamp((0, 0), 4))
        origin, s, size = v.raster_geometry(corner, .8, .5)
        self.assertEqual(origin, (0, 0))                         # nothing past the map
        self.assertEqual(size, (1056 + 528, 528 + 264))

    def test_at_rest_the_picture_is_copied_pixel_for_pixel(self):
        m = base()
        noise = np.random.default_rng(2).integers(0, 256, (2112, 1056, 3), dtype=np.uint8)
        view = v.View(m, 2, v.clamp((500, 300), 2))
        geometry = v.raster_geometry(view, .8, .5)
        r = raster(pygame.surfarray.make_surface(noise[:geometry[2][0], :geometry[2][1]]), *geometry[:2])
        c = object.__new__(v.Canvas)
        c.view = view
        dest = pygame.Surface((1056, 528))
        self.assertTrue(c.sample(r, dest))
        x0 = round((view.rect()[0] - geometry[0][0]) * geometry[1])
        y0 = round((view.rect()[1] - geometry[0][1]) * geometry[1])
        self.assertTrue(np.array_equal(pygame.surfarray.array3d(dest), noise[x0:x0 + 1056, y0:y0 + 528]))

    def test_scaled_samples_do_not_drift_and_part_ones_stay_in_their_place(self):
        m = base()
        surface = pygame.Surface((v.W, v.H))
        for x in range(0, v.W, 2):
            surface.fill((255, 255, 255), (x, 0, 1, v.H))         # 1 px stripes
        r = raster(surface)
        c = object.__new__(v.Canvas)
        for zoom in (8, 24):
            c.view = v.View(m, zoom, v.clamp((400.3, 200.7), zoom))
            dest = pygame.Surface((1056, 528))
            self.assertTrue(c.sample(r, dest))
            row = pygame.surfarray.array3d(dest)[:, 264, 0].astype(float)
            k = 1056 / v.W * zoom
            u = (np.arange(1056) + .5) / k + c.view.rect()[0]       # picture coordinate shown
            expect = np.where(np.floor(u) % 2 == 0, 255, 0)
            centred = np.abs(u - np.floor(u) - .5) < .25
            self.assertLess(np.abs(row - expect)[centred].max(), 100, zoom)
        detail = raster(pygame.Surface((1320, 660)), (330.0, 165.0), 2.0)  # the middle, at 2x
        c.view = v.View(m)
        dest = pygame.Surface((v.W, v.H))
        dest.fill((0, 0, 255))
        self.assertFalse(c.sample(detail, dest))
        self.assertTrue(c.sample(detail, dest, part=True))
        self.assertEqual(dest.get_at((v.W // 2, v.H // 2))[:3], (0, 0, 0))
        self.assertEqual(dest.get_at((v.W // 4 - 2, v.H // 2))[:3], (0, 0, 255))
        self.assertEqual(dest.get_at((v.W // 4 + 2, v.H // 2))[:3], (0, 0, 0))

    def test_ground_is_cut_from_the_pyramid_level_just_finer(self):
        m = base()
        with tempfile.TemporaryDirectory() as tmp:
            for level in (1, 2):
                os.makedirs(os.path.join(tmp, str(level)))
                for c in range(math.ceil(v.W * level / 256)):
                    for r in range(math.ceil(v.H * level / 256)):
                        Image.new('RGB', (256, 256), (level * 100, c, r)).save(os.path.join(tmp, str(level), f'{c}_{r}.png'))
            with open(os.path.join(tmp, 'levels.json'), 'w') as f:
                json.dump(dict(levels=[1, 2], tile=256), f)
            with patch.object(v.tiles, 'TILES', tmp):
                v._tiles.clear(), v._grounds.clear()
                picture, metres = v.ground(m, (300.0, 200.0), 1.6, (64, 32))   # level 2
                self.assertEqual(picture.getpixel((10, 10)), (200, 2, 1))       # 2 * 300 // 256
                picture, _ = v.ground(m, (0.0, 0.0), .8, (64, 32))             # level 1
                self.assertEqual(picture.getpixel((5, 5)), (100, 0, 0))
                self.assertLess(metres, 300)


class LabelTests(unittest.TestCase):
    def setUp(self):
        pygame.freetype.init()

    def boxes(self, items, sizes, spots, li):
        z = v.LABEL_ZOOMS[li]
        out = []
        for (text, (x, y), _, _), (tw, th), k in zip(items, sizes, spots[:, li]):
            if k >= 0:
                dx, dy = v.spot(int(k), tw)
                out.append((x * z + dx - 1, y * z + dy, x * z + dx + tw + 1, y * z + dy + th + 2))
        return out

    def test_labels_never_overlap_keep_their_side_and_only_appear_with_zoom(self):
        rng = np.random.default_rng(3)
        items = [(f'P{i}', tuple(rng.uniform(50, [v.W - 50, v.H - 50])), 13, 1) for i in range(400)]
        items.append(('LATE', (700.0, 300.0), 13, 8))
        sizes, spots = v.place_labels(items, .8)
        for li in range(len(v.LABEL_ZOOMS)):
            b = self.boxes(items, sizes, spots, li)
            for i in range(len(b)):
                for j in range(i):
                    self.assertFalse(b[i][0] < b[j][2] and b[j][0] < b[i][2] and b[i][1] < b[j][3] and b[j][1] < b[i][3])
        shown = (spots >= 0).sum(axis=0)
        self.assertTrue(np.all(np.diff(shown) >= 0))              # zooming in only adds labels
        self.assertGreater(shown[-1], shown[0])
        kept = [(spots[i, li] == spots[i, li + 1]) for i in range(len(items)) for li in range(len(v.LABEL_ZOOMS) - 1)
                if spots[i, li] >= 0]
        self.assertGreater(np.mean(kept), .95)                    # sides mostly stay put
        late = spots[-1]
        self.assertTrue(np.all(late[:6] == -1) and late[6] >= 0)  # from zoom 8 on only


class SceneTests(unittest.TestCase):
    def setUp(self):
        pygame.display.init()
        pygame.display.set_mode((1, 1))
        pygame.freetype.init()
        self.m = base()
        self.scene = blank_scene(self.m)

    def test_paths_to_past_the_map_are_faint_and_stay_through_zoom(self):
        yellow = (255, 250, 0)
        d = scene_data([([(600, 300), (700, 300)], yellow, 155), ([(600, 400), (5000, 400)], yellow, 48)])
        self.scene.take(d)
        frame = pygame.Surface((v.W, v.H))
        self.scene.draw(frame, v.View(self.m), 33)
        self.assertGreater(frame.get_at((650, 300)).r, 120)
        self.assertTrue(0 < frame.get_at((900, 400)).r < 80)
        frame.fill((0, 0, 0))
        self.scene.draw(frame, v.View(self.m, 4, (650, 300)), 33)  # same lines, just bigger
        self.assertGreater(frame.get_at((v.W // 2, v.H // 2)).r, 120)

    def test_campuses_only_show_from_their_zoom(self):
        ring = np.array([(640.0, 320.0), (680.0, 320.0), (680.0, 340.0), (640.0, 340.0)])
        d = scene_data(campuses=[dict(name='大学', rings=[ring], point=(660.0, 330.0))],
                       labels=[('大学', (660.0, 330.0), ('campus', v.CAMPUS))])
        self.scene.take(d)
        frame = pygame.Surface((v.W, v.H))
        self.scene.draw(frame, v.View(self.m, 4, (660, 330)), 33)
        self.assertEqual(frame.get_at((v.W // 2 - 70, v.H // 2 - 30))[:3], (0, 0, 0))   # not drawn
        self.scene.draw(frame, v.View(self.m, v.CAMPUS_ZOOM, (660, 330)), 33)
        self.assertGreater(frame.get_at((v.W // 2 - 70, v.H // 2 - 30)).r, 0)


if __name__ == '__main__':
    unittest.main()
