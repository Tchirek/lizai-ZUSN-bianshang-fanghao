"""Package HamClock's cluster glyphs and X11's 16-dot Song into one local font.

Build only: python tools/build_search_font.py (Pillow and fontTools).
"""
import gzip
from pathlib import Path
import re

from fontTools.fontBuilder import FontBuilder
from fontTools.pens.ttGlyphPen import TTGlyphPen
from PIL import Image
from PIL.PcfFontFile import PcfFontFile, PCF_BDF_ENCODINGS

ROOT = Path(__file__).resolve().parents[1] / 'hamclock/vendor/search-fonts'
glyphs, metrics, cmap = {}, {}, {}


def add(code, bitmap, left, top, advance, unit):
    pen = TTGlyphPen(None)
    for y in range(bitmap.height):
        x = 0
        while x < bitmap.width:
            if not bitmap.getpixel((x, y)):
                x += 1
                continue
            start = x
            while x < bitmap.width and bitmap.getpixel((x, y)):
                x += 1
            x0, x1 = round((left + start) * unit), round((left + x) * unit)
            y0, y1 = round((top - y - 1) * unit), round((top - y) * unit)
            pen.moveTo((x0, y0)); pen.lineTo((x0, y1)); pen.lineTo((x1, y1)); pen.lineTo((x1, y0)); pen.closePath()
    name = f'uni{code:04X}'
    glyphs[name] = pen.glyph()
    metrics[name] = round(advance * unit), round(left * unit)
    cmap[code] = name


source = (ROOT / 'CourierPrimeSans6.cpp').read_text().split('#elif')[0]
bits, records = re.findall(r'PROGMEM = \{(.*?)\};', source, re.S)[:2]
bits = bytes(int(n, 16) for n in re.findall(r'0x[0-9A-Fa-f]+', bits))
for code, record in enumerate(re.findall(r'\{([^}]+)\}', re.sub(r'//[^\n]*', '', records)), 32):
    offset, w, h, advance, left, top = map(int, record.split(','))
    bitmap = Image.new('1', (w, h))
    bitmap.putdata([bool(bits[offset + i // 8] & (128 >> (i % 8))) for i in range(w * h)])
    add(code, bitmap, left, 14 - top, advance, 80)  # same 1600 -> 1280 scale as the clock

with gzip.open(ROOT / 'gb16st.pcf.gz', 'rb') as fp:
    font = PcfFontFile(fp)
    sizes = font._load_metrics()
    bitmaps = font._load_bitmaps(sizes)
    fp, _, i16, _ = font._getformat(PCF_BDF_ENCODINGS)
    first_col, last_col, first_row, last_row, _ = [i16(fp.read(2)) for _ in range(5)]
    for row in range(first_row, last_row + 1):
        for col in range(first_col, last_col + 1):
            index = i16(fp.read(2))
            if index == 65535:
                continue
            try:
                code = ord(bytes((row + 128, col + 128)).decode('gb2312'))
            except UnicodeDecodeError:
                continue
            _, _, left, _, width, ascent, _, _ = sizes[index]
            add(code, bitmaps[index], left, ascent, width, 100)

glyphs['.notdef'] = TTGlyphPen(None).glyph()
metrics['.notdef'] = 1600, 0
font = FontBuilder(1600, isTTF=True)
font.setupGlyphOrder(['.notdef', *cmap.values()])
font.setupCharacterMap(cmap)
font.setupGlyf(glyphs)
font.setupHorizontalMetrics(metrics)
font.setupHorizontalHeader(ascent=1400, descent=-300)
font.setupNameTable(dict(familyName='HamClock Search', styleName='Regular',
                        uniqueFontIdentifier='HamClockSearch-1', fullName='HamClock Search',
                        psName='HamClockSearch', version='Version 1.0'))
font.setupOS2(sTypoAscender=1400, sTypoDescender=-300, usWinAscent=1600, usWinDescent=400)
font.setupPost()
font.setupMaxp()
font.save(ROOT / 'search.ttf')
assert all(ord(c) in cmap for c in 'BG8TEST 遂宁机场')

