import os
os.environ['SDL_VIDEODRIVER'] = 'dummy'
os.environ['PYGAME_HIDE_SUPPORT_PROMPT'] = '1'
import unittest

import numpy as np
import pygame

import hcdisplay as h


class FittedTests(unittest.TestCase):
    def test_rescaling_changed_regions_matches_a_full_rescale(self):
        rng = np.random.default_rng(1)
        source = pygame.surfarray.make_surface(rng.integers(0, 256, (1600, 960, 3), dtype=np.uint8))
        fitted = h.Fitted((1280, 768))
        fitted.update(source)
        changed = [pygame.Rect(37, 11, 96, 23), pygame.Rect(1501, 937, 99, 23), pygame.Rect(400, 400, 1, 1)]
        for r in changed:
            source.fill(tuple(int(c) for c in rng.integers(0, 256, 3)), r)
        fitted.update(source, changed)
        full = pygame.transform.smoothscale(source, (1280, 768))
        diff = np.abs(pygame.surfarray.array3d(fitted.image).astype(int) - pygame.surfarray.array3d(full))
        self.assertLessEqual(diff.max(), 1)

    def test_regions_under_the_map_are_left_alone(self):
        source = pygame.Surface((1600, 960))
        fitted = h.Fitted((1280, 768))
        fitted.update(source)
        source.fill((255, 0, 0), (300, 300, 50, 50))
        fitted.update(source, [pygame.Rect(300, 300, 50, 50)], skip=pygame.Rect(278, 298, 1320, 660))
        self.assertEqual(fitted.image.get_at((260, 260))[:3], (0, 0, 0))

    def test_an_awkward_ratio_falls_back_to_full_rescales(self):
        source = pygame.Surface((1600, 960))
        fitted = h.Fitted((1366, 819))
        fitted.update(source)
        source.fill((0, 255, 0), (0, 0, 10, 10))
        fitted.update(source, [pygame.Rect(0, 0, 10, 10)])
        self.assertGreater(fitted.image.get_at((2, 2)).g, 245)


if __name__ == '__main__':
    unittest.main()
