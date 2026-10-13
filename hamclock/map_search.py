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
import unicodedata

import activity as a
import dots
import map_view as maps


def entries(base, model=None, said=True):
    data = maps.static_scene(base)
    rows = [(name, point, maps.CITY if city else maps.TOWN)
            for name, point, city in zip(data['place_names'], data['place_xy'], data['place_city'])]
    for kind, colour in (('campuses', maps.CAMPUS), ('military', maps.MILITARY),
                         ('government', maps.GOVERNMENT), ('regulators', maps.REGULATOR),
                         ('embassies', maps.EMBASSY), ('airports', maps.AIRPORT)):
        rows.extend((p.get('name', p.get('text', '')), p['point'], colour) for p in data[kind])
    try:
        live = json.loads((Path(model or a.MODEL) / 'activity.json').read_text(encoding='utf-8'))
    except FileNotFoundError:
        live = {}
    stations, *_ = a.digital_graph(base, live.get('reports', []))
    rows.extend((call, value[:2], value[2]) for call, value in stations.items())
    rows.extend((call, base.xy(*value[:2]), a.APRS_RF if value[2] else a.APRS_NET,
                 '#00dcff' if value[2] else '#00647d')
                for call, value in live.get('aprs', {}).items())
    rows.extend((call, base.xy(*ll), a.WHITE) for call, grid in live.get('qsos', []) if (ll := a.locator(grid)))
    result = []
    for name, (x, y), colour, *web_colour in rows:
        if name and 0 <= x < a.W and 0 <= y < a.H:
            lat, lon = base.latlon(x, y)
            result.append(dict(name=name, point=[float(x), float(y)], colour='#%02x%02x%02x' % tuple(colour),
                               position=f'{abs(lat):.4f}{"N" if lat >= 0 else "S"} {abs(lon):.4f}{"E" if lon >= 0 else "W"}'))
            if web_colour:
                result[-1]['web_colour'] = web_colour[0]
            if said:
                result[-1]['said'] = spelled(name)
    return result


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
        heard_as = {}
        for row in rows:
            row.setdefault('said', spelled(row['name']))
            for ch, sound in zip(row['said'].text, row['said'].sounds):
                if sound != ch:
                    heard_as.setdefault(ch, Counter())[sound] += 1
        self.heard_as = {ch: sounds.most_common(1)[0][0] for ch, sounds in heard_as.items()}

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


def search(rows, query):
    """The rows whose names hold query as written or as said: the name itself first, then those
    it begins, those holding it as written, and the nearer the station the sooner."""
    rows = rows if isinstance(rows, Index) else Index(rows)
    text = normalize(query)
    if not text:
        return []
    spell = rows.spell(text)
    found = [(row, text in row['said'].text) for row in rows.rows]
    found = [(row, written) for row, written in found if written or spell and heard(row['said'], spell)]
    return [row for row, _ in sorted(found, key=lambda f: (
        f[0]['said'].text != text, not f[0]['said'].text.startswith(text), not f[1],
        (f[0]['point'][0] - a.W / 2) ** 2 + (f[0]['point'][1] - a.H / 2) ** 2, f[0]['name']))][:LIMIT]


LIMIT, PAGE = 200, 8                    # results kept, and shown a page at a time
SCROLL = 90                             # window px of a two-finger scroll that turn a page
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
        self.repeat = pygame.key.get_repeat()
        self.box = None

    def update(self):
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
