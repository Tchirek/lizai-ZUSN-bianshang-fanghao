import os
os.environ['SDL_VIDEODRIVER'] = 'dummy'
os.environ['PYGAME_HIDE_SUPPORT_PROMPT'] = '1'
from collections import OrderedDict
import io
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
    s.rivers, s.marks, s.admin = v.Lines([]), {}, {}
    s.grid = [(v.Lines([]), (v.GRID_SUB, 1)), (v.Lines([]), (v.GRID_SQUARE, 1)), (v.Lines([]), (v.GRID_FIELD, 2))]
    s.squares = s.subsquares = (np.empty(0, int), np.empty(0, int), np.empty((0, 2)))
    return s


def scene_data(lines=(), campuses=(), labels=(), airports=(), runways=(), military=()):
    d = dict(stations=[], lines=list(lines), total=len(lines), bands={}, aprs=[], links=[], qsos=[],
             places=np.empty((0, 2)), campuses=list(campuses), far=(-100, -100),
             airports=list(airports), runways=list(runways), military=list(military),
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

    def test_a_swipe_glides_on_after_the_fingers_lift_unless_they_stopped(self):
        c = object.__new__(v.Canvas)
        c.camera, c.swiped, c.fling, c.moved_at, c.dirty = v.Camera(base()), [], None, 0, False
        c.camera.pinch(8, (v.W / 2, v.H / 2))
        for i in range(6):                                       # 600 view px/s to the left
            c.swipe(-6, 0, 1 + i / 100)
        c.release(1.06)
        self.assertAlmostEqual(c.fling[0], -600)
        before = c.camera.centre[0]
        while c.glide(1 / 60):
            pass
        self.assertIsNone(c.fling)
        self.assertAlmostEqual((c.camera.centre[0] - before) * 8, 600 * v.FLING_S, delta=15)
        c.swipe(-6, 0, 2.0)
        c.release(2.2)                                           # held still before lifting
        self.assertIsNone(c.fling)


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
        for (text, (x, y), *_), (tw, th), k in zip(items, sizes, spots[:, li]):
            if k >= 0:
                dx, dy = v.spot(int(k), tw)
                out.append((x * z + dx - 1, y * z + dy, x * z + dx + tw + 1, y * z + dy + th + 2))
        return out

    def test_labels_never_overlap_keep_their_side_and_only_appear_with_zoom(self):
        rng = np.random.default_rng(3)
        items = [(f'P{i}', tuple(rng.uniform(50, [v.W - 50, v.H - 50])), 13, 1, math.inf) for i in range(400)]
        items.append(('LATE', (700.0, 300.0), 13, 8, math.inf))
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

    def test_a_label_right_by_our_marker_still_finds_a_spot(self):
        sizes, spots = v.place_labels([('四川师范大学遂宁校区', (v.W / 2 + .3, v.H / 2 - .2), 15, 1, math.inf)], .8)
        self.assertTrue(np.all(spots[0] >= 4))                    # further out, at every zoom

    def test_a_town_name_moves_onto_its_point_once_zoomed_in(self):
        sizes, spots = v.place_labels([('Pengxi', (500.0, 200.0), 14, 1, v.CITY_ZOOM)], .8)
        steps = np.array(v.LABEL_ZOOMS)
        self.assertTrue(np.all(spots[0][steps < v.CITY_ZOOM] < 8))
        self.assertTrue(np.all(spots[0][steps >= v.CITY_ZOOM] == 8))
        sizes, spots = v.place_labels([('Suining', (v.W / 2 + 1, v.H / 2), 14, 1, v.CITY_ZOOM)], .8)
        self.assertTrue(np.all(spots[0] >= 0))                    # over our marker: beside it


class GridTests(unittest.TestCase):
    def setUp(self):
        pygame.display.init()
        pygame.display.set_mode((1, 1))
        pygame.freetype.init()
        self.m = base()
        self.scene = blank_scene(self.m)
        with patch('builtins.open', side_effect=lambda *args, **kw: io.StringIO('{"places": [], "lines": []}')):
            real = v.Scene(self.m, 1.0)                           # its grid, without the OSM file
        self.scene.grid, self.scene.squares, self.scene.subsquares = real.grid, real.squares, real.subsquares

    def names(self, view):
        self.scene.layer = pygame.Surface((v.W, v.H), pygame.SRCALPHA)
        return {text: at for text, _, _, at in self.scene.draw_grid(view, 1.0)}

    def test_squares_are_named_and_our_square_is_om20(self):
        self.assertEqual(v.square_name(52, 30), 'OM20')               # 104-106 E, 30-31 N
        self.assertEqual(v.square_name(51, 29), 'OL19')
        names = self.names(v.View(self.m))
        self.assertIn('OM20', names)
        self.assertIn('OL19', names)

    def test_zoomed_in_the_square_is_named_at_the_view_top_left_with_subsquares(self):
        view = v.View(self.m, 20, v.clamp((v.W / 2, v.H / 2), 20))   # about us, inside OM20
        names = self.names(view)
        self.assertEqual(names['OM20'], (7, 5))                      # held in the corner
        self.assertIn('tn', names)                                   # our subsquare: OM20tn
        self.assertTrue(all(len(t) in (2, 4) for t in names))

    def test_dashes_keep_their_places_on_the_line_as_the_view_pans(self):
        line = v.Lines([np.array([(500.0, 330.0), (800.0, 330.0)])])

        def dash_starts(view):
            """Where dashes start, in px from the line's own start."""
            layer = pygame.Surface((v.W, v.H), pygame.SRCALPHA)
            line.draw(layer, view, 1.0, (255, 255, 255, 255), 1, (7, 4))
            row = pygame.surfarray.pixels_alpha(layer)[:, round(view.project((0, 330))[1])] > 0
            return np.flatnonzero(np.diff(np.r_[False, row].astype(int)) == 1) - view.project((500, 0))[0]

        for centre in ((600, 330), (630.3, 330), (655.7, 330)):
            starts = dash_starts(v.View(self.m, 2, centre))
            starts = starts[starts > 1]                              # not cut by the view's edge
            self.assertGreater(len(starts), 20)
            self.assertLess(np.abs((starts + .5) % 11 - .5).max(), 1.01, centre)   # every 11 px


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

    def test_runways_to_scale_and_airport_rings_only_zoomed_out(self):
        runway = dict(line=np.array([(600.0, 300.0), (603.0, 300.0)]), width=45.0, area=False)   # ~2.7 km
        d = scene_data(airports=[(700.0, 300.0)], runways=[runway])
        self.scene.take(d)
        frame = pygame.Surface((v.W, v.H))
        self.scene.draw(frame, v.View(self.m), 33)
        self.assertNotEqual(frame.get_at((601, 300))[:3], v.RUNWAY)              # a speck: left out
        self.assertIn(v.AIRPORT, [frame.get_at((x, 300))[:3] for x in range(702, 707)])   # the ring
        frame.fill((0, 0, 0))
        view = v.View(self.m, 16, (601.5, 300))
        self.scene.draw(frame, view, 33)
        x, y = view.project((601.5, 300))
        lit = [frame.get_at((int(x), int(y) + dy))[:3] == v.RUNWAY for dy in range(-6, 7)]
        self.assertGreaterEqual(sum(lit), 2)                                     # never under 2 px
        frame.fill((0, 0, 0))
        view = v.View(self.m, 16, (700, 300))
        self.scene.draw(frame, view, 33)
        x, y = view.project((700, 300))
        self.assertNotIn(v.AIRPORT, [frame.get_at((int(x) + dx, int(y)))[:3] for dx in range(2, 7)])  # no ring

    def test_province_lines_give_way_to_city_and_county_outlines_and_town_dots_go(self):
        self.scene.admin = {4: v.Lines([np.array([(600.0, 250.0), (600.0, 400.0)])]),
                            6: v.Lines([np.array([(640.0, 250.0), (640.0, 400.0)])])}    # clear of the grid
        d = scene_data()
        d['places'] = np.array([(650.0, 330.0)])
        self.scene.take(d)
        frame = pygame.Surface((v.W, v.H))
        self.scene.draw(frame, v.View(self.m), 33)
        lit = lambda x: max(frame.get_at((x, y)).r for y in range(290, 320))
        self.assertGreater(lit(600), 100)                         # a province border
        self.assertEqual(lit(640), 0)                             # no county lines yet
        self.assertGreater(max(frame.get_at((650 + dx, 330 + dy)).r for dx in (-1, 0, 1) for dy in (-1, 0, 1)), 100)
        frame.fill((0, 0, 0))
        view = v.View(self.m, v.CITY_ZOOM, (650, 330))
        self.scene.draw(frame, view, 33)
        x_county = round(view.project((640, 0))[0])
        self.assertGreater(max(frame.get_at((x_county, y)).r for y in range(200, 300)), 30)
        self.assertEqual(max(frame.get_at((v.W // 2 + dx, v.H // 2 + dy)).r for dx in (-1, 0, 1) for dy in (-1, 0, 1)), 0)

    def test_military_areas_show_from_their_zoom(self):
        ring = np.array([(640.0, 320.0), (680.0, 320.0), (680.0, 340.0), (640.0, 340.0)])
        self.scene.take(scene_data(military=[dict(name='', rings=[ring], point=(660.0, 330.0))]))
        frame = pygame.Surface((v.W, v.H))
        self.scene.draw(frame, v.View(self.m, 2, (660, 330)), 33)
        edge = lambda: max(frame.get_at((v.W // 2 + dx, v.H // 2 - 40 * z)).r for dx in range(-5, 6))
        z = 2
        self.assertLess(edge(), 60)                                  # not yet
        frame.fill((0, 0, 0))
        self.scene.draw(frame, v.View(self.m, v.MILITARY_ZOOM, (660, 330)), 33)
        z = v.MILITARY_ZOOM
        self.assertGreater(max(frame.get_at((v.W // 2 + dx, v.H // 2 - 10 * z + dy)).r
                               for dx in range(-5, 6) for dy in (-1, 0, 1)), 150)

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
