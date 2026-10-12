#!/usr/bin/env python3
"""Tiny national flags beside the map's consulate labels: drawn in code at the label's own
height, 3:2-ish, cached per (code, height). No image files and no emoji: at thirteen-odd
pixels what reads is the pattern — bands, a disc, a cross — so small emblems are hinted at
or left out."""
import math

import pygame

RATIO = 19 / 13                  # a little wider than 3:2, as flags read at this size

RED = (206, 17, 38)
BLUE = (0, 51, 153)
NAVY = (1, 33, 105)
WHITE = (244, 244, 244)
GREEN = (0, 122, 61)
GOLD = (255, 205, 0)
YELLOW = (254, 221, 0)
BLACK = (24, 24, 24)

_flags = {}


def known(code):
    return code.upper() in _DESIGNS


def flag(code, height):
    """The country's flag, `height` px tall, as a cached surface."""
    key = (code.upper(), height)
    if key not in _flags:
        width = max(6, round(height * RATIO))
        big = pygame.Surface((width * 4, height * 4), pygame.SRCALPHA)
        kind, data = _DESIGNS.get(key[0], ("fn", _generic))
        if kind == "h":
            _bands(big, data, True)
        elif kind == "v":
            _bands(big, data, False)
        else:
            data(big, big.get_width(), big.get_height())
        _flags[key] = pygame.transform.smoothscale(big, (width, height))
    return _flags[key]


def _bands(pic, bands, across):
    """Bands by (weight, colour), filling the surface."""
    w, h = pic.get_size()
    total = sum(x for x, _ in bands)
    at = 0
    for x, colour in bands:
        span = (h if across else w) * x // total
        pic.fill(colour, (0, at, w, span) if across else (at, 0, span, h))
        at += span
    pic.fill(bands[-1][1], (0, at, w, h) if across else (at, 0, w, h))


def _star(pic, cx, cy, r, colour, points=5, width=0):
    verts = []
    for i in range(points * 2):
        rr = r if i % 2 == 0 else r * 0.42
        a = -math.pi / 2 + i * math.pi / points
        verts.append((cx + rr * math.cos(a), cy + rr * math.sin(a)))
    pygame.draw.polygon(pic, colour, verts, width)


def _half_circle(pic, colour, c, r, top):
    pygame.draw.circle(pic, colour, c, r, draw_top_left=top, draw_top_right=top,
                       draw_bottom_left=not top, draw_bottom_right=not top)


def _jp(pic, w, h):
    pic.fill(WHITE)
    pygame.draw.circle(pic, RED, (w / 2, h / 2), h * 0.29)


def _kr(pic, w, h):
    pic.fill(WHITE)
    c, r = (w / 2, h / 2), h * 0.26
    _half_circle(pic, RED, c, r, True)
    _half_circle(pic, (0, 71, 160), c, r, False)
    for dx, dy in ((-1, -1), (1, -1), (-1, 1), (1, 1)):
        a = (w / 2 + dx * w * 0.26, h / 2 + dy * h * 0.30)
        pygame.draw.line(pic, BLACK, a, (a[0] + dx * w * 0.14, a[1] + dy * h * 0.16),
                         max(1, round(h / 10)))


def _gb(pic, w, h):
    pic.fill(NAVY)
    t = h / 5
    pygame.draw.line(pic, WHITE, (0, 0), (w, h), round(t * 1.8))
    pygame.draw.line(pic, WHITE, (w, 0), (0, h), round(t * 1.8))
    pygame.draw.line(pic, RED, (0, 0), (w, h), max(1, round(t * 0.9)))
    pygame.draw.line(pic, RED, (w, 0), (0, h), max(1, round(t * 0.9)))
    pygame.draw.line(pic, WHITE, (w / 2, 0), (w / 2, h), round(t * 2.4))
    pygame.draw.line(pic, WHITE, (0, h / 2), (w, h / 2), round(t * 2.4))
    pygame.draw.line(pic, RED, (w / 2, 0), (w / 2, h), round(t * 1.4))
    pygame.draw.line(pic, RED, (0, h / 2), (w, h / 2), round(t * 1.4))


LEAF = ((0, -0.55), (0.14, -0.30), (0.34, -0.36), (0.26, -0.10), (0.46, -0.02), (0.16, 0.14),
        (0.22, 0.34), (0.04, 0.26), (0, 0.55), (-0.04, 0.26), (-0.22, 0.34), (-0.16, 0.14),
        (-0.46, -0.02), (-0.26, -0.10), (-0.34, -0.36), (-0.14, -0.30))


def _ca(pic, w, h):
    _bands(pic, [(1, RED), (2, WHITE), (1, RED)], False)
    pygame.draw.polygon(pic, RED, [(w / 2 + x * h, h / 2 + y * h) for x, y in LEAF])


def _canton(pic, w, h):
    mini = pygame.Surface((round(w * 0.5), round(h * 0.5)))
    _gb(mini, *mini.get_size())
    pic.blit(mini, (0, 0))


def _au(pic, w, h):
    pic.fill(NAVY)
    _canton(pic, w, h)
    _star(pic, w * 0.22, h * 0.78, h * 0.16, WHITE, 7)
    for x, y in ((0.62, 0.24), (0.80, 0.36), (0.88, 0.66), (0.68, 0.74), (0.52, 0.50)):
        _star(pic, w * x, h * y, h * 0.09, WHITE, 5)


def _nz(pic, w, h):
    pic.fill(NAVY)
    _canton(pic, w, h)
    for x, y in ((0.62, 0.30), (0.80, 0.44), (0.70, 0.74), (0.90, 0.62)):
        _star(pic, w * x, h * y, h * 0.11, WHITE, 5)
        _star(pic, w * x, h * y, h * 0.07, RED, 5)


def _sg(pic, w, h):
    pic.fill(WHITE)
    pic.fill((237, 49, 54), (0, 0, w, h // 2))
    r, c = h * 0.22, (w * 0.26, h * 0.28)
    pygame.draw.circle(pic, WHITE, c, r)
    pygame.draw.circle(pic, (237, 49, 54), (c[0] + r * 0.45, c[1]), r * 0.85)
    for k in range(5):
        a = -math.pi / 2 + k * 2 * math.pi / 5
        pygame.draw.circle(pic, WHITE, (c[0] + r * 1.9 * math.cos(a), c[1] + r * 1.9 * math.sin(a)),
                           max(1, round(h / 12)))


def _pk(pic, w, h):
    pic.fill((1, 66, 44))
    pic.fill(WHITE, (0, 0, round(w * 0.25), h))
    r, cx, cy = h * 0.30, w * 0.58, h * 0.5
    pygame.draw.circle(pic, WHITE, (cx, cy), r)
    pygame.draw.circle(pic, (1, 66, 44), (cx + r * 0.40, cy - r * 0.12), r * 0.86)
    _star(pic, cx + r * 1.05, cy - r * 0.62, h * 0.11, WHITE, 5)


def _np(pic, w, h):
    shape = [(0.03 * w, 0.02 * h), (0.80 * w, 0.30 * h), (0.52 * w, 0.42 * h),
             (0.92 * w, 0.60 * h), (0.03 * w, 0.98 * h)]
    pygame.draw.polygon(pic, (196, 30, 58), shape)
    pygame.draw.polygon(pic, (0, 56, 147), shape, max(1, round(h / 9)))
    pygame.draw.circle(pic, WHITE, (0.26 * w, 0.30 * h), max(1, round(h * 0.10)))
    pygame.draw.circle(pic, WHITE, (0.26 * w, 0.66 * h), max(1, round(h * 0.12)))


def _il(pic, w, h):
    pic.fill(WHITE)
    stripe = max(1, round(h * 0.13))
    pic.fill(BLUE, (0, round(h * 0.14), w, stripe))
    pic.fill(BLUE, (0, h - round(h * 0.14) - stripe, w, stripe))
    cx, cy, s = w / 2, h / 2, h * 0.27
    up, down = [(cx, cy - s), (cx - s * 0.87, cy + s * 0.5), (cx + s * 0.87, cy + s * 0.5)], \
               [(cx, cy + s), (cx - s * 0.87, cy - s * 0.5), (cx + s * 0.87, cy - s * 0.5)]
    pygame.draw.polygon(pic, BLUE, up, max(1, round(h / 10)))
    pygame.draw.polygon(pic, BLUE, down, max(1, round(h / 10)))


def _cl(pic, w, h):
    pic.fill(WHITE)
    pic.fill((213, 43, 30), (0, h // 2, w, h - h // 2))
    pic.fill((0, 57, 116), (0, 0, round(w * 0.36), h // 2))
    _star(pic, w * 0.18, h * 0.25, h * 0.17, WHITE, 5)


def _uy(pic, w, h):
    for i in range(9):
        pic.fill(WHITE if i % 2 == 0 else (0, 56, 168), (0, i * h // 9, w, h // 9 + 1))
    pic.fill(WHITE, (0, 0, round(w * 0.42), round(h * 0.55)))
    pygame.draw.circle(pic, (252, 209, 22), (w * 0.21, h * 0.28), max(1, round(h * 0.14)))


def _tr(pic, w, h):
    pic.fill((227, 10, 23))
    r, cx, cy = h * 0.30, w * 0.42, h * 0.5
    pygame.draw.circle(pic, WHITE, (cx, cy), r)
    pygame.draw.circle(pic, (227, 10, 23), (cx + r * 0.42, cy - r * 0.06), r * 0.80)
    _star(pic, w * 0.72, cy, h * 0.16, WHITE, 5)


def _br(pic, w, h):
    pic.fill((0, 151, 57))
    pygame.draw.polygon(pic, YELLOW, [(w / 2, h * 0.07), (w * 0.93, h / 2), (w / 2, h * 0.93),
                                      (w * 0.07, h / 2)])
    pygame.draw.circle(pic, (0, 39, 118), (w / 2, h / 2), max(2, round(h * 0.19)))
    pygame.draw.line(pic, WHITE, (w * 0.34, h * 0.42), (w * 0.66, h * 0.47), max(1, round(h / 10)))


def _kh(pic, w, h):
    _bands(pic, [(1, (3, 45, 152)), (1, (215, 25, 35)), (1, (3, 45, 152))], True)
    pic.fill(WHITE, (round(w * 0.42), round(h * 0.36), round(w * 0.16), round(h * 0.28)))


def _ph(pic, w, h):
    pic.fill((0, 56, 168), (0, 0, w, h // 2))
    pic.fill((206, 17, 38), (0, h // 2, w, h - h // 2))
    pygame.draw.polygon(pic, WHITE, [(0, 0), (w * 0.40, h / 2), (0, h)])
    pygame.draw.circle(pic, (252, 209, 22), (w * 0.14, h / 2), max(1, round(h * 0.10)))


def _vn(pic, w, h):
    pic.fill((218, 37, 29))
    _star(pic, w / 2, h / 2, h * 0.32, YELLOW, 5)


def _mm(pic, w, h):
    _bands(pic, [(1, YELLOW), (1, (52, 178, 51)), (1, (234, 40, 57))], True)
    _star(pic, w / 2, h / 2, h * 0.34, WHITE, 5)


def _mx(pic, w, h):
    _bands(pic, [(1, (0, 104, 71)), (1, WHITE), (1, (206, 17, 38))], False)
    pygame.draw.circle(pic, (140, 90, 40), (w / 2, h / 2), max(1, round(h * 0.12)))


def _cn(pic, w, h):
    pic.fill((238, 28, 37))
    _star(pic, w * 0.22, h * 0.30, h * 0.22, YELLOW, 5)
    for x, y in ((0.46, 0.12), (0.55, 0.24), (0.55, 0.40), (0.46, 0.52)):
        _star(pic, w * x, h * y, h * 0.08, YELLOW, 5)


def _generic(pic, w, h):
    """One we have no design for: neutral, with a ring where a flag would carry its emblem."""
    pic.fill((90, 100, 120))
    pygame.draw.circle(pic, WHITE, (w / 2, h / 2), max(2, round(h * 0.28)), max(1, round(h / 8)))


_DESIGNS = {
    "DE": ("h", [(1, BLACK), (1, RED), (1, GOLD)]),
    "AT": ("h", [(1, RED), (1, WHITE), (1, RED)]),
    "HU": ("h", [(1, RED), (1, WHITE), (1, GREEN)]),
    "TH": ("h", [(1, RED), (1, WHITE), (2, NAVY), (1, WHITE), (1, RED)]),
    "ES": ("h", [(1, RED), (2, GOLD), (1, RED)]),
    "BY": ("h", [(2, RED), (1, GREEN)]),
    "FR": ("v", [(1, BLUE), (1, WHITE), (1, RED)]),
    "IT": ("v", [(1, GREEN), (1, WHITE), (1, RED)]),
    "MX": ("fn", _mx),
    "MM": ("fn", _mm),
    "CN": ("fn", _cn),
    "JP": ("fn", _jp),
    "KR": ("fn", _kr),
    "GB": ("fn", _gb),
    "CA": ("fn", _ca),
    "AU": ("fn", _au),
    "NZ": ("fn", _nz),
    "SG": ("fn", _sg),
    "PK": ("fn", _pk),
    "NP": ("fn", _np),
    "IL": ("fn", _il),
    "CL": ("fn", _cl),
    "UY": ("fn", _uy),
    "TR": ("fn", _tr),
    "BR": ("fn", _br),
    "KH": ("fn", _kh),
    "PH": ("fn", _ph),
    "VN": ("fn", _vn),
}
