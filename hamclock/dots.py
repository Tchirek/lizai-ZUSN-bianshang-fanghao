"""Text as HamClock prints it: dots lit from glyph tables, no font engine.

Latin is HamClock's own Courier Prime Sans table for its 1600x960 frame, read from the C source
it is compiled from and plotted as its plotChar does: the cursor at the top of the line, each glyph
a run of bits, a dot wherever one is set. Chinese is the 16-dot Song that ISAS drew in 1988 and
Debian's xfonts-base installs for X11, read from the system's own PCF file and emboldened as
UCDOS did, by printing each glyph a second time one dot to the right (a dot wider apart, so
neighbours do not touch). Characters in neither are skipped, as HamClock skips what its table
does not hold.
"""
from collections import namedtuple
from functools import lru_cache
import gzip
from pathlib import Path
import re
import struct

HAMCLOCK = Path(__file__).parent / 'vendor/hamclock-font/CourierPrimeSans6.cpp'
SONG = Path('/usr/share/fonts/X11/misc/gb16st.pcf.gz')
BASE = 13                     # baseline below the cursor: where Courier's capitals stand
ELLIPSIS = '…'

Glyph = namedtuple('Glyph', 'xo yo adv w h bits')   # bits: rows run on, high bit first, as GFX


@lru_cache(maxsize=None)
def latin(path=HAMCLOCK):
    """{char: Glyph} of a HamClock font's 1600x960 table (its C source), Courier Prime Sans unless
    another is named."""
    source = Path(path).read_text(encoding='ascii')
    block = re.search(r'#if defined\(_CLOCK_1600x960\)(.*?)#(?:elif|else|endif)', source, re.S).group(1)
    bits, records = re.findall(r'PROGMEM = \{(.*?)\};', block, re.S)[:2]
    bits = bytes(int(n, 16) for n in re.findall(r'0x[0-9A-Fa-f]+', bits))
    first = int(re.search(r'0x([0-9A-Fa-f]+), 0x[0-9A-Fa-f]+,\s*\d+\s*\};', block).group(1), 16)
    table = {}
    for code, record in enumerate(re.findall(r'\{([^}]+)\}', re.sub(r'//[^\n]*', '', records)), first):
        offset, w, h, adv, xo, yo = map(int, record.split(','))
        table[chr(code)] = Glyph(xo, yo, adv, w, h, bits[offset:offset + (w * h + 7) // 8])
    return table


class Song:
    """A PCF file's tables; each glyph is unpacked when it is first asked for."""

    def __init__(self, path):
        self.data = data = gzip.decompress(path.read_bytes())
        count, = struct.unpack_from('<I', data, 4)
        self.toc = {kind: offset for kind, _, _, offset in
                    (struct.unpack_from('<4I', data, 8 + 16 * i) for i in range(count))}
        at, fmt, e = self.table(4)                        # metrics
        if fmt & 0x100:
            count, = struct.unpack_from(e + 'h', data, at)
            self.metrics = [tuple(b - 0x80 for b in data[at + 2 + 5 * i:at + 7 + 5 * i]) for i in range(count)]
        else:
            count, = struct.unpack_from(e + 'i', data, at)
            self.metrics = [struct.unpack_from(e + '5h', data, at + 4 + 12 * i) for i in range(count)]
        at, fmt, e = self.table(8)                        # bitmaps
        assert fmt & 8 and not fmt & 0x30, 'PCF bitmaps must be bytes, high bit first'
        count, = struct.unpack_from(e + 'i', data, at)
        self.offsets = struct.unpack_from(e + '%di' % count, data, at + 4)
        self.bitmaps, self.pad = at + 4 + 4 * count + 16, 1 << (fmt & 3)
        at, fmt, e = self.table(0x20)                     # encodings: GB 2312 rows and columns
        col0, col1, row0, row1, _ = struct.unpack_from(e + '5h', data, at)
        self.cols, self.rows = (col0, col1), (row0, row1)
        self.index = struct.unpack_from(e + '%dH' % ((col1 - col0 + 1) * (row1 - row0 + 1)), data, at + 10)

    def table(self, kind):
        at = self.toc[kind]
        fmt, = struct.unpack_from('<I', self.data, at)
        return at + 4, fmt, '>' if fmt & 4 else '<'

    def glyph(self, char):
        try:
            row, col = (b - 0x80 for b in char.encode('gb2312'))
        except (UnicodeEncodeError, ValueError):
            return None
        (col0, col1), (row0, row1) = self.cols, self.rows
        if not (row0 <= row <= row1 and col0 <= col <= col1):
            return None
        i = self.index[(row - row0) * (col1 - col0 + 1) + col - col0]
        if i == 0xffff:
            return None
        lsb, rsb, adv, ascent, descent = self.metrics[i]
        w, h = rsb - lsb, ascent + descent
        stride = (w + 8 * self.pad - 1) // (8 * self.pad) * self.pad
        at = self.bitmaps + self.offsets[i]
        bits = 0
        for y in range(h):
            line = int.from_bytes(self.data[at + y * stride:at + (y + 1) * stride], 'big') >> (8 * stride - w)
            bits = bits << (w + 1) | line << 1 | line          # and once more, a dot to the right
        n = (w + 1) * h
        return Glyph(lsb, BASE - ascent, adv + 1, w + 1, h, (bits << (-n % 8)).to_bytes((n + 7) // 8, 'big'))


@lru_cache(maxsize=None)
def song():
    return Song(SONG) if SONG.exists() else None


@lru_cache(maxsize=4096)
def glyph(char):
    if char in latin():
        return latin()[char]
    return song().glyph(char) if song() else None


def width(text):
    return sum(g.adv for g in map(glyph, text) if g)


def fit(text, room):
    """text, or as much of it as fits in room dots with an ellipsis after it."""
    if width(text) <= room:
        return text
    tail = ELLIPSIS if glyph(ELLIPSIS) else '...'
    while text and width(text + tail) > room:
        text = text[:-1]
    return text + tail if width(tail) <= room else ''


def table(text):
    """{char: [xo, yo, advance, width, height, hex bits]} for the characters that have dots."""
    return {c: [g.xo, g.yo, g.adv, g.w, g.h, g.bits.hex()] for c in dict.fromkeys(text) if (g := glyph(c))}


def dots(text):
    """Each lit dot of text, (x, y) from the cursor."""
    x = 0
    for g in map(glyph, text):
        if g:
            yield from ((x + g.xo + i % g.w, g.yo + i // g.w) for i in range(g.w * g.h)
                        if g.bits[i // 8] & 0x80 >> i % 8)
            x += g.adv


@lru_cache(maxsize=256)
def surface(text, colour):
    """(pygame surface of text in colour, clear between the dots; where the cursor is on it)."""
    import pygame
    lit = list(dots(text))
    left, top = min([0] + [x for x, _ in lit]), min([0] + [y for _, y in lit])
    out = pygame.Surface((max([0] + [x for x, _ in lit]) - left + 1, max([0] + [y for _, y in lit]) - top + 1),
                         pygame.SRCALPHA)
    for x, y in lit:
        out.set_at((x - left, y - top), colour)
    return out, (-left, -top)


def blit(dest, text, at, colour):
    """Print text on dest with HamClock's cursor at `at`."""
    out, (x, y) = surface(text, tuple(colour))
    dest.blit(out, (at[0] - x, at[1] - y))
