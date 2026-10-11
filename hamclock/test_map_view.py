import os
os.environ['SDL_VIDEODRIVER'] = 'dummy'
os.environ['PYGAME_HIDE_SUPPORT_PROMPT'] = '1'
from collections import OrderedDict
import math
import unittest

import numpy as np
import pygame

import activity as a
import map_view as v


def base():
    m = object.__new__(a.Map)
    m.lat0, m.lon0, m.km_per_px, m.labels = 30.55, 105.62, .8975, []
    return m


def blank_scene(m):
    """A Scene with no geography, for drawing contacts alone."""
    s = object.__new__(v.Scene)
    s.base, s.fonts, s.glyphs = m, {}, OrderedDict()
    s.contacts, s.future, s.mtime, s.checked = None, None, None, 0
    s.selection_key = s.selection = s.layer = None
    s.layout, s.layout_key, s.taken, s.hud, s.budget = {}, None, [], (330, 420), math.inf
    s.rivers = {level: (np.empty((0, 2)), np.zeros(1, int)) for level in (1, 2, 4, 8, 16, 32)}
    s.river_box, s.grid = np.empty((0, 4)), []
    s.place_names, s.place_xy, s.place_km, s.name_km = [], np.empty((0, 2)), np.empty(0), 300
    s.far = ((-100, -100), 'far')
    return s


def contacts(paths=(), stations=None, aprs=(), links=(), qsos=()):
    return v.Contacts(dict(paths=list(paths), bands={(p['band'], p['colour']) for p in paths},
                           stations=stations or {}, aprs=list(aprs), links=list(links), qsos=list(qsos)))


class CameraTests(unittest.TestCase):
    def test_wheel_eases_in_holding_the_point_under_the_pointer_then_back_out(self):
        camera = v.Camera(base())
        point = (300.25, 250.75)
        held = camera.view().unproject(point)
        self.assertTrue(camera.scroll(.05, point))
        self.assertGreater(camera.goal, 1)
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

    def test_maximum_zoom_stays_inside_the_map_and_round_trips(self):
        camera = v.Camera(base())
        for _ in range(5):
            camera.scroll(8, (v.W - 1, v.H - 1))
        for _ in range(180):
            camera.tick(1 / 60)
        self.assertEqual(camera.zoom, v.MAX_ZOOM)
        view = camera.view()
        self.assertLessEqual(view.unproject((v.W, v.H))[0], v.W)
        self.assertLessEqual(view.unproject((v.W, v.H))[1], v.H)
        lat, lon = view.latlon(417.2, 231.5)
        x, y = view.xy(lat, lon)
        self.assertAlmostEqual(x, 417.2, places=6)
        self.assertAlmostEqual(y, 231.5, places=6)
        self.assertEqual(v.dem_zoom(view), 12)
        self.assertEqual(v.dem_zoom(v.View(base())), 8)

    def test_drag_pans_within_the_map_and_a_later_zoom_does_not_jump(self):
        camera = v.Camera(base())
        self.assertFalse(camera.pan(50, 0))                      # nothing to pan at 1x
        camera.scroll(8, (v.W / 2, v.H / 2))
        for _ in range(60):
            camera.tick(1 / 60)
        before = camera.centre
        self.assertTrue(camera.pan(40, -20))
        self.assertAlmostEqual(camera.centre[0], before[0] - 40 / camera.zoom)
        self.assertAlmostEqual(camera.centre[1], before[1] + 20 / camera.zoom)
        moved = camera.centre
        camera.tick(1 / 60)                                      # settled: no jump back
        self.assertEqual(camera.centre, moved)
        point = (200, 100)
        held = camera.view().unproject(point)
        camera.scroll(-1, point)
        camera.tick(1 / 60)
        now = camera.view().unproject(point)
        self.assertLess(abs(now[0] - held[0]) + abs(now[1] - held[1]), 1e-6)
        camera.pan(1e6, 1e6)                                     # far past the corner
        self.assertAlmostEqual(camera.view().unproject((0, 0))[0], 0)
        self.assertAlmostEqual(camera.view().unproject((0, 0))[1], 0)


class CanvasTests(unittest.TestCase):
    def test_raster_sample_lands_within_a_pixel_and_refuses_what_it_does_not_cover(self):
        m = base()
        surface = pygame.Surface((v.W, v.H))
        surface.fill((0, 0, 0))
        surface.fill((255, 255, 255), (700, 300, 2, 2))           # a bright spot at base 701,301
        raster = object.__new__(v.Raster)
        raster.view = v.View(m)
        raster.shaded(pygame.image.tobytes(surface, 'RGB'), 0)
        c = object.__new__(v.Canvas)
        c.view = v.View(m, 8, (690.37, 296.81))
        dest = pygame.Surface((v.W, v.H))
        self.assertTrue(c.sample(raster, dest))
        pixels = pygame.surfarray.array3d(dest)[..., 0]
        x, y = np.unravel_index(np.argmax(pixels), pixels.shape)
        spot = c.view.project((701, 301))
        self.assertLess(abs(x - spot[0]), 8)                       # an 8x8 px blob at 8x
        self.assertLess(abs(y - spot[1]), 8)
        lit = np.argwhere(pixels > 128)
        centre = lit.mean(axis=0)
        self.assertLess(abs(centre[0] + .5 - spot[0]), 1)          # pixel i spans i..i+1
        self.assertLess(abs(centre[1] + .5 - spot[1]), 1)
        detail = object.__new__(v.Raster)
        detail.view, detail.surface = v.View(m, 8, (690, 297)), raster.surface
        c.view = v.View(m, 4, (690, 297))                         # wider than the detail
        self.assertFalse(c.sample(detail, dest))
        dest.fill((0, 0, 255))
        self.assertTrue(c.sample(detail, dest, part=True))         # just the middle half
        self.assertEqual(dest.get_at((v.W // 2, v.H // 2))[:3], (0, 0, 0))
        self.assertEqual(dest.get_at((v.W // 8, v.H // 2))[:3], (0, 0, 255))
        self.assertEqual(dest.get_at((v.W // 4 - 2, v.H // 2))[:3], (0, 0, 255))
        self.assertNotEqual(dest.get_at((v.W // 4 + 2, v.H // 2))[:3], (0, 0, 255))

    def test_sample_scales_shrinking_and_expanding_without_drift(self):
        m = base()
        surface = pygame.Surface((v.W, v.H))
        for x in range(0, v.W, 2):
            surface.fill((255, 255, 255), (x, 0, 1, v.H))         # 1 px stripes
        raster = object.__new__(v.Raster)
        raster.view = v.View(m)
        raster.shaded(pygame.image.tobytes(surface, 'RGB'), 0)
        c = object.__new__(v.Canvas)
        for zoom in (1, 1.1, 8, 24):
            c.view = v.View(m, zoom, v.clamp((400.3, 200.7), zoom))
            dest = pygame.Surface((1056, 528))
            self.assertTrue(c.sample(raster, dest))
            row = pygame.surfarray.array3d(dest)[:, 264, 0].astype(float)
            if zoom < 2:                                            # aliased greys, but no gaps
                self.assertGreater(row.min(), 20, zoom)
                continue
            k = 1056 / v.W * zoom
            u = (np.arange(1056) + .5) / k + c.view.unproject((0, 0))[0]   # raster coordinate shown
            expect = np.where(np.floor(u) % 2 == 0, 255, 0)
            centred = np.abs(u - np.floor(u) - .5) < .25            # near a raster pixel's centre
            self.assertLess(np.abs(row - expect)[centred].max(), 100, zoom)

class SceneTests(unittest.TestCase):
    def setUp(self):
        pygame.display.init()
        pygame.display.set_mode((1, 1))
        pygame.font.init()
        self.m = base()
        self.scene = blank_scene(self.m)

    def test_paths_leaving_the_view_are_faint_and_inside_ones_strong(self):
        yellow = (255, 250, 0)
        local = dict(ends=[(600, 300), (700, 300)], local=[0, 1], calls=['A', 'B'], band='20', colour=yellow)
        far = dict(ends=[(600, 400), (5000, 400)], local=[0], calls=['A', 'X'], band='20', colour=yellow)
        self.scene.contacts = contacts([local, far], {'A': (600, 300, yellow, True), 'B': (700, 300, yellow, False)})
        frame = pygame.Surface((v.W, v.H))
        self.scene.draw(frame, v.View(self.m), 530)
        strong, faint = frame.get_at((650, 300)), frame.get_at((900, 400))
        self.assertGreater(strong.r, 120)
        self.assertGreater(faint.r, 0)
        self.assertLess(faint.r, 80)

    def test_zoom_keeps_a_path_whose_far_end_left_the_view(self):
        yellow = (255, 250, 0)
        path = dict(ends=[(v.W / 2, v.H / 2), (900, v.H / 2)], local=[0, 1], calls=['S', 'R'], band='20', colour=yellow)
        self.scene.contacts = contacts([path], {'S': (v.W / 2, v.H / 2, yellow, True)})
        frame = pygame.Surface((v.W, v.H))
        self.scene.draw(frame, v.View(self.m, 4), 33)
        self.assertGreater(frame.get_at((int(v.W / 2 + 200), int(v.H / 2))).r, 0)
        chosen, degrees, total, bands = self.scene.selection
        self.assertEqual((chosen, degrees, total, bands), ([0], {'S': 1}, 1, {'20': yellow}))

    def test_labels_keep_their_spot_while_moving_and_relayout_at_rest(self):
        self.scene.scale = 1
        self.scene.taken = [pygame.Rect(600, 280, 200, 40)]      # right of the point is taken
        frame = pygame.Surface((v.W, v.H))
        self.scene.label(frame, 600, 300, 'BG8ASN', a.WHITE)
        spot = self.scene.layout[('BG8ASN', a.WHITE, 13)]
        self.assertLess(spot[0], 0)                              # went left
        self.scene.taken = []
        self.scene.label(frame, 610, 300, 'BG8ASN', a.WHITE)      # moving: same side
        self.assertEqual(self.scene.layout[('BG8ASN', a.WHITE, 13)], spot)
        self.scene.layout.clear()                                # at rest: chosen afresh
        self.scene.label(frame, 610, 300, 'BG8ASN', a.WHITE)
        self.assertGreater(self.scene.layout[('BG8ASN', a.WHITE, 13)][0], 0)


if __name__ == '__main__':
    unittest.main()
