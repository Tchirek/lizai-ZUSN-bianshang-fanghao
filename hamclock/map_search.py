"""Search the names already on the map, in base pixels, for both displays.

A name is found as written or as said: each is spelled out in pinyin (by pypinyin, which reads a
character within its word, 重庆 as chongqing), so 遂宁 finds Suining and Suining finds 遂宁. What
is asked is spelled the way the names that hold it are, so the displays never load pypinyin
themselves; only the worker building the rows does.
"""
from collections import Counter, namedtuple
from contextlib import nullcontext
from functools import lru_cache
import json
from pathlib import Path
import re
import time
import unicodedata

import numpy as np

import activity as a
import dots
import map_view as maps


KINDS = ('place', 'region', 'campus', 'military', 'office', 'regulator', 'embassy', 'airport', 'station')
REGION = (255, 255, 255)                # as the boundaries are drawn


def entries(base, model=None, said=True):
    """Everything named on the map, each with its kind (KINDS), the point it is at in base px,
    the colour it is drawn in, and the outline of an area (rings, base px)."""
    data = maps.static_scene(base)
    rows = [(name, point, maps.CITY if city else maps.TOWN, 'place', [])
            for name, point, city in zip(data['place_names'], data['place_xy'], data['place_city'])]
    rows.extend((name, point, REGION, 'region', rings) for name, point, rings in regions(base, model))
    for group, kind, colour in (('campuses', 'campus', maps.CAMPUS), ('military', 'military', maps.MILITARY),
                                ('government', 'office', maps.GOVERNMENT), ('regulators', 'regulator', maps.REGULATOR),
                                ('embassies', 'embassy', maps.EMBASSY), ('airports', 'airport', maps.AIRPORT)):
        rows.extend((p.get('name', p.get('text', '')), p['point'], colour, kind, p.get('rings', []))
                    for p in data[group])
    try:
        live = json.loads((Path(model or a.MODEL) / 'activity.json').read_text(encoding='utf-8'))
    except FileNotFoundError:
        live = {}
    stations, *_ = a.digital_graph(base, live.get('reports', []))
    rows.extend((call, value[:2], value[2], 'station', []) for call, value in stations.items())
    rows.extend((call, base.xy(*value[:2]), a.APRS_RF if value[2] else a.APRS_NET, 'station', [],
                 '#00dcff' if value[2] else '#00647d')
                for call, value in live.get('aprs', {}).items())
    rows.extend((call, base.xy(*ll), a.WHITE, 'station', [])
                for call, grid in live.get('qsos', []) if (ll := a.locator(grid)))
    result = []
    for name, (x, y), colour, kind, rings, *web_colour in rows:
        if name and 0 <= x < a.W and 0 <= y < a.H:
            lat, lon = base.latlon(x, y)
            result.append(dict(name=name, point=[float(x), float(y)], colour='#%02x%02x%02x' % tuple(colour),
                               position=f'{abs(lat):.4f}{"N" if lat >= 0 else "S"} {abs(lon):.4f}{"E" if lon >= 0 else "W"}',
                               kind=kind, rings=rings))
            if web_colour:
                result[-1]['web_colour'] = web_colour[0]
            if said:
                result[-1]['said'] = spelled(name)
    return result


_regions = {}


def regions(base, model=None):
    """(name, label point, rings) of each province, prefecture and county, in base px, from
    admin_areas.json (build_tiles.py admin); kept while the file stays."""
    path = Path(model or a.MODEL) / 'admin_areas.json'
    key = (id(base), path, path.stat().st_mtime_ns if path.exists() else 0)
    if _regions.get('key') != key:
        found = json.loads(path.read_text(encoding='utf-8')) if path.exists() else []
        _regions.update(key=key, regions=[
            (r['name'], base.xy(*r['point']),
             [maps.base_xy(base, *np.array(ring, float).T).astype(np.float32) for ring in r['rings']])
            for r in found])
    return _regions['regions']


def normalize(text):
    return ''.join(unicodedata.normalize('NFKC', text).casefold().split())


Spelling = namedtuple('Spelling', 'text sounds spell starts')


@lru_cache(maxsize=None)
def spelled(name):
    """name as normalize() writes it, the sound of each of its characters (its pinyin, else the
    character), and all the letters run together with where each syllable or word starts."""
    plain = unicodedata.normalize('NFKC', name).casefold()
    try:
        from pypinyin import lazy_pinyin
        sounds = lazy_pinyin(plain, errors=list)
    except ImportError:                                 # names then match only as written
        sounds = list(plain)
    if len(sounds) != len(plain):
        sounds = list(plain)
    text, said, spell, starts, word = '', [], '', [], True
    for ch, sound in zip(plain, sounds):
        if not ch.isspace():
            text += ch
            said.append(sound)
        if not ch.isalnum():
            word = True
            continue
        if sound != ch or word:
            starts.append(len(spell))
        spell += sound
        word = False
    return Spelling(text, tuple(said), spell, frozenset(starts))


def heard(said, spell):
    """Whether spell is said in a name from one of its syllables or words on."""
    at = said.spell.find(spell)
    while at >= 0 and at not in said.starts:
        at = said.spell.find(spell, at + 1)
    return at >= 0


class Index:
    """The rows, and how the Chinese characters in their names are said, to spell what is asked."""

    def __init__(self, rows):
        self.rows = rows
        self.areas, self.masks = {}, {}          # what inside() found by the region asked for; masks
        self.points = np.array([row['point'] for row in rows], float).reshape(-1, 2)
        heard_as = {}
        for row in rows:
            row.setdefault('said', spelled(row['name']))
            for ch, sound in zip(row['said'].text, row['said'].sounds):
                if sound != ch:
                    heard_as.setdefault(ch, Counter())[sound] += 1
        self.heard_as = {ch: sounds.most_common(1)[0][0] for ch, sounds in heard_as.items()}

    def kinds(self, text):
        """Which rows are of a kind whose name begins with text (type:air for airports)."""
        return np.array([row.get('kind', '').startswith(text) for row in self.rows], bool)

    def inside(self, text):
        """Which rows are inside a region whose name begins with text, as written or as said."""
        if text not in self.areas:
            spell = self.spell(text)
            hit = np.zeros(len(self.rows), bool)
            for i, row in enumerate(self.rows):
                said = row['said']
                if row.get('kind') == 'region' and (said.text.startswith(text) or spell and said.spell.startswith(spell)):
                    if i not in self.masks:
                        self.masks[i] = mask(row['rings'])
                    lo, area = self.masks[i]
                    at = np.floor((self.points - lo) * GRID).astype(int)
                    on = (at >= 0).all(axis=1) & (at[:, 0] < area.shape[1]) & (at[:, 1] < area.shape[0])
                    hit[on] |= area[at[on, 1], at[on, 0]]
            self.areas[text] = hit
        return self.areas[text]

    def spell(self, text):
        """text in letters: Chinese read as a name holding it reads it, else as names mostly do."""
        sounds = None
        if any(ch in self.heard_as for ch in text):
            sounds = next((row['said'].sounds[at:at + len(text)] for row in self.rows
                           if (at := row['said'].text.find(text)) >= 0), None)
        sounds = sounds or [self.heard_as.get(ch, ch) for ch in text]
        return ''.join(sound for ch, sound in zip(text, sounds) if ch.isalnum())


_index = {}


def index(base, model=None, lock=None):
    """Index of entries(), kept while the map's files and the activity stay as they were; the
    rows are gathered under lock, the new names spelled out after it."""
    with lock or nullcontext():
        path = Path(model or a.MODEL) / 'activity.json'
        key = (id(base), maps.static_scene(base)['key'], path.stat().st_mtime_ns if path.exists() else 0)
        if _index.get('key') == key:
            return _index['index']
        rows = entries(base, model, said=False)
    built = Index(rows)
    _index.update(key=key, index=built)
    return built


# a word of a query: - to leave out, an operator (city: or its like in:, type:), then the words
# themselves, "quoted" to be found only as written
WORD = re.compile(r'(-?)(?:([a-z]+):)?(?:"([^"]*)"?|“([^”]*)”?|(\S+))')
OPERATORS = ('city', 'in', 'type')


def parse(query):
    """[(leave out?, operator or '', the words, as written only?)] of a query."""
    words = []
    for out, op, quoted, curly, bare in WORD.findall(unicodedata.normalize('NFKC', query).casefold()):
        text = quoted or curly or bare
        if op and op not in OPERATORS:
            op, text = '', f'{op}:{text}'
        if normalize(text):
            words.append((bool(out), op, normalize(text), bool(quoted or curly)))
    return words


GRID = 4                                # mask px a base px: how finely an area is told


def mask(rings):
    """(where it starts in base px, the area inside rings as a mask GRID times finer), even-odd
    so a ring within another is a hole, cut to the map."""
    from PIL import Image, ImageDraw
    lo = np.floor(np.maximum(np.min([r.min(axis=0) for r in rings], axis=0), 0)).astype(int)
    hi = np.ceil(np.minimum(np.max([r.max(axis=0) for r in rings], axis=0), (a.W, a.H))).astype(int)
    size = tuple(int(v) for v in np.maximum((hi - lo) * GRID, 1))
    out = np.zeros(size[::-1], bool)
    for ring in rings:
        im = Image.new('1', size)
        ImageDraw.Draw(im).polygon([tuple(p) for p in (ring - lo) * GRID], fill=1)
        out ^= np.asarray(im)
    return lo, out


def search(rows, query):
    """The rows a query finds, as a search engine's: every word held in the name, as written or
    as said ("quoted" ones as written), none of those after a -, inside the area city: names (in:
    too) and of the kind type: names. The name itself first, then those it begins, those found as
    written, and the nearer the station the sooner."""
    rows = rows if isinstance(rows, Index) else Index(rows)
    words = parse(query)
    if not any(not out for out, *_ in words):
        return []
    keep = np.ones(len(rows.rows), bool)
    written = np.ones(len(rows.rows), bool)
    for out, op, text, literal in words:
        if op:
            hit = rows.kinds(text) if op == 'type' else rows.inside(text)
        else:
            spell = '' if literal else rows.spell(text)
            plain = np.array([text in row['said'].text for row in rows.rows], bool)
            hit = plain | np.array([bool(spell) and heard(row['said'], spell) for row in rows.rows], bool)
            if not out:
                written &= plain
        keep &= ~hit if out else hit
    terms = ''.join(text for out, op, text, _ in words if not out and not op)
    found = [(rows.rows[i], written[i]) for i in np.flatnonzero(keep)]
    return [row for row, _ in sorted(found, key=lambda f: (
        f[0]['said'].text != terms, not (terms and f[0]['said'].text.startswith(terms)), not f[1],
        (f[0]['point'][0] - a.W / 2) ** 2 + (f[0]['point'][1] - a.H / 2) ** 2, f[0]['name']))][:LIMIT]


LIMIT, PAGE = 200, 8                    # results kept, and shown a page at a time
SCROLL = 90                             # window px of a two-finger scroll that turn a page
SETTLE = 0.12                           # s the keys rest before the search runs again
WIDTH, ROW, PAD, GAP = 600, 30, 6, 12   # HamClock px: panel, line, margin, name to position
CURSOR = 8                              # a line's top to HamClock's text cursor


class SearchPanel:
    """Small native counterpart of the web search; SDL owns composed text input."""

    def __init__(self, canvas):
        import pygame
        self.future = canvas.submit_scene(entries, canvas.camera.base)
        self.rows, self.results = Index([]), []
        self.query = self.composition = self.error = ''
        self.selected = 0
        self.hovered = None
        self.backspace_down = False
        self.scrolled = 0
        self.due = None                         # when to search again for what was typed
        self.repeat = pygame.key.get_repeat()
        self.box = None

    def ask(self):
        """The query changed: search again once the keys rest a moment, as the web page does."""
        self.due = time.monotonic() + SETTLE

    def update(self):
        if self.due and time.monotonic() >= self.due and not self.future:
            self.filter()
            return True
        if not self.future or not self.future.done():
            return False
        try:
            self.rows = Index(self.future.result())
        except Exception:
            self.error = 'Search unavailable'
        self.future = None
        self.filter()
        return True

    def filter(self):
        self.due = None
        self.results = search(self.rows, self.query)
        self.selected = 0
        self.hovered = None

    def step(self, delta):
        """Light the next or previous result after the one lit; the keys take over from the pointer."""
        if self.results:
            lit = self.hovered if self.hovered is not None else self.selected
            self.selected, self.hovered = (lit + delta) % len(self.results), None

    def page(self, delta):
        """Turn a page on or back, its first result lit; at either end it stays."""
        lit = self.hovered if self.hovered is not None else self.selected
        to = min(max(lit // PAGE + delta, 0), (len(self.results) - 1) // PAGE)
        if self.results and to != lit // PAGE:
            self.selected, self.hovered = to * PAGE, None
            return True
        return False

    def turn(self, gesture):
        """A page turned by a wheel's notch, or by a two-finger scroll once it goes far enough."""
        if gesture[0] == 'wheel':
            return self.page(-1 if gesture[1] > 0 else 1)
        self.scrolled += gesture[2]
        if abs(self.scrolled) < SCROLL:
            return False
        delta, self.scrolled = (1 if self.scrolled > 0 else -1), 0
        return self.page(delta)

    def shown(self):
        """The first result on the page shown, and that page's results."""
        first = self.selected // PAGE * PAGE
        return first, self.results[first:first + PAGE]

    def hover(self, at):
        row = int((at[1] - self.box.y) // self.row_height) - 1 if at and self.box and self.box.collidepoint(at) else -1
        first, page = self.shown()
        hovered = first + row if 0 <= row < len(page) else None
        changed = hovered != self.hovered
        self.hovered = hovered
        return changed

    def draw(self, frame, map_box):
        """Drawn in HamClock's own pixels, as its panes are, then scaled with the clock."""
        import pygame
        status = self.error or ('No matches' if self.query.strip() and not self.future and not self.results else '')
        first, page = self.shown()
        panel = pygame.Surface((WIDTH, ROW * (1 + len(page) + bool(status))))
        # the ruler, as a pager's: which of how many are on the page
        ruler = f'{first + 1}-{first + len(page)}/{len(self.results)}' if len(self.results) > PAGE else ''
        dots.blit(panel, ruler, (WIDTH - PAD - dots.width(ruler), CURSOR), (150, 150, 150))
        text = self.query + self.composition
        while text and dots.width('>' + text + '_') > WIDTH - 2 * PAD - (dots.width(ruler) + GAP if ruler else 0):
            text = text[1:]
        dots.blit(panel, '>' + text + '_', (PAD, CURSOR), (255, 255, 255))
        lit = self.hovered if self.hovered is not None else self.selected
        for i, result in enumerate(page, first):
            y = (i - first + 1) * ROW
            if i == lit:
                panel.fill((45, 45, 45), (1, y, WIDTH - 2, ROW))
            right = WIDTH - PAD - dots.width(result['position'])
            dots.blit(panel, dots.fit(result['name'], right - GAP - PAD), (PAD, y + CURSOR), (221, 221, 221))
            dots.blit(panel, result['position'], (right, y + CURSOR), pygame.Color(result['colour'])[:3])
        if status:
            dots.blit(panel, status, (PAD, ROW + CURSOR), (150, 150, 150))
        pygame.draw.rect(panel, (136, 136, 136), panel.get_rect(), 1)
        k = map_box.w / a.W
        size = round(panel.get_width() * k), round(panel.get_height() * k)
        self.box = pygame.Rect((map_box.x + round(4 * k), map_box.y + round(4 * k)), size)
        self.row_height = ROW * k
        frame.blit(panel if size == panel.get_size() else pygame.transform.smoothscale(panel, size), self.box)
        pygame.key.set_text_input_rect(pygame.Rect(self.box.x, self.box.y, self.box.w, round(self.row_height))
                                       .move(frame.get_abs_offset()))
