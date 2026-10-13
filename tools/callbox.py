#!/usr/bin/env python3
"""A picture for HamClock's call sign box: a field cut on the slant, red and black, a line of
text across it in white. HamClock draws it in place of the call when it finds it in its directory
(tools/patch_hamclock.py, item 6) as influ-callbox.rgb565: the box's raw pixels, row by row,
little-endian RGB565.

Text is laid out by Pillow with libraqm (HarfBuzz and FriBiDi), so Arabic joins, runs right to
left and keeps its vowel marks where they belong. The font is Amiri (hamclock/vendor/amiri, OFL),
Khaled Hosny's revival of the Bulaq Press Naskh, made for fully vowelled text.

usage: callbox.py TEXT [--dir ~/.hamclock] [--size 460x104] [--font PATH] [--preview PNG]
"""
import argparse
import os

from PIL import Image, ImageDraw, ImageFont, features

FONT = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'hamclock', 'vendor', 'amiri', 'Amiri-Bold.ttf')
RED, BLACK, WHITE = (200, 16, 32), (0, 0, 0), (255, 255, 255)
CUT = (0.62, 0.38)             # where the slant meets the top and the bottom, across the box
SUPER = 4                      # drawn this many times finer, then scaled down: a clean slant
MARGIN = 0.05                  # of the box, kept clear round the text


def picture(text, size, font_path):
    if not features.check('raqm'):
        raise SystemExit('Pillow needs libraqm to lay out Arabic')
    w, h = size
    big = Image.new('RGB', (w * SUPER, h * SUPER), BLACK)
    draw = ImageDraw.Draw(big)
    draw.polygon([(0, 0), (CUT[0] * w * SUPER, 0), (CUT[1] * w * SUPER, h * SUPER), (0, h * SUPER)], fill=RED)
    room_w, room_h = w * SUPER * (1 - 2 * MARGIN), h * SUPER * (1 - 2 * MARGIN)
    layout = dict(direction='rtl', language='ar', features=['kern', 'liga', 'calt', 'mark', 'mkmk'])
    px = h * SUPER
    while px > 4:                                   # the largest size the box holds, marks and all
        font = ImageFont.truetype(font_path, px, layout_engine=ImageFont.Layout.RAQM)
        left, top, right, bottom = draw.textbbox((0, 0), text, font=font, **layout)
        if right - left <= room_w and bottom - top <= room_h:
            break
        px -= SUPER
    draw.text(((w * SUPER - (right - left)) / 2 - left, (h * SUPER - (bottom - top)) / 2 - top), text,
              font=font, fill=WHITE, **layout)
    return big.resize(size, Image.Resampling.LANCZOS)


def rgb565(img):
    raw, out = img.tobytes(), bytearray()
    for i in range(0, len(raw), 3):
        r, g, b = raw[i:i + 3]
        v = (r & 0xF8) << 8 | (g & 0xFC) << 3 | b >> 3
        out += bytes((v & 0xFF, v >> 8))
    return bytes(out)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('text')
    ap.add_argument('--dir', default=os.path.expanduser('~/.hamclock'), help="HamClock's directory (its -d)")
    ap.add_argument('--size', default='460x104', help='the box in raw px: 230x52 app px at 1600x960')
    ap.add_argument('--font', default=FONT)
    ap.add_argument('--preview', help='also save the picture as this PNG')
    a = ap.parse_args()
    img = picture(a.text, tuple(int(v) for v in a.size.split('x')), a.font)
    path = os.path.join(a.dir, 'influ-callbox.rgb565')
    with open(path + '.part', 'wb') as f:
        f.write(rgb565(img))
    os.replace(path + '.part', path)               # HamClock never reads half of one
    if a.preview:
        img.save(a.preview)
    print(path)


if __name__ == '__main__':
    main()
