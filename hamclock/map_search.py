"""Search the names already on the map, in base pixels, for both displays."""
import json
from pathlib import Path
from functools import lru_cache
import unicodedata

import activity as a
import map_view as maps


def entries(base, model=None):
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
    return result


def normalize(text):
    return ''.join(unicodedata.normalize('NFKC', text).casefold().split())


def search(rows, query):
    query = normalize(query)
    if not query:
        return []
    matches = [row for row in rows if query in normalize(row['name'])]
    return sorted(matches, key=lambda r: (normalize(r['name']) != query,
                  not normalize(r['name']).startswith(query),
                  (r['point'][0] - a.W / 2) ** 2 + (r['point'][1] - a.H / 2) ** 2,
                  r['name']))[:8]


@lru_cache(maxsize=8)
def font(px):
    import pygame.freetype
    face = pygame.freetype.Font(str(Path(__file__).parent / 'vendor/search-fonts/search.ttf'), px)
    face.antialiased, face.pad = False, True
    return face


def fit_name(text, face, width):
    if face.get_rect(text).width <= width:
        return text
    while text and face.get_rect(text + '...').width > width:
        text = text[:-1]
    return text + '...' if face.get_rect('...').width <= width else ''


class SearchPanel:
    """Small native counterpart of the web search; SDL owns composed text input."""

    def __init__(self, canvas):
        import pygame
        self.future = canvas.submit_scene(entries, canvas.camera.base)
        self.rows, self.results = [], []
        self.query = self.composition = self.error = ''
        self.selected = 0
        self.hovered = None
        self.backspace_down = False
        self.repeat = pygame.key.get_repeat()
        self.box = None

    def update(self):
        if not self.future or not self.future.done():
            return False
        try:
            self.rows = self.future.result()
        except Exception:
            self.error = 'Search unavailable'
        self.future = None
        self.filter()
        return True

    def filter(self):
        self.results = search(self.rows, self.query)
        self.selected = 0
        self.hovered = None

    def hover(self, at):
        row = int((at[1] - self.box.y) // self.row_height) - 1 if at and self.box and self.box.collidepoint(at) else -1
        hovered = row if 0 <= row < len(self.results) else None
        changed = hovered != self.hovered
        self.hovered = hovered
        return changed

    def draw(self, frame, map_box):
        import pygame
        k = map_box.w / a.W
        row = max(22, round(30 * k))
        status = self.error or ('No matches' if self.query.strip() and not self.future and not self.results else '')
        self.box = pygame.Rect(map_box.x + 4, map_box.y + 4, min(map_box.w - 8, round(600 * k)),
                               row * (1 + len(self.results) + bool(status)))
        self.row_height = row
        pygame.draw.rect(frame, (0, 0, 0), self.box)
        pygame.draw.rect(frame, (136, 136, 136), self.box, 1)
        clip = frame.get_clip()
        frame.set_clip(self.box.inflate(-8, -2))
        face, small = font(max(12, round(20 * k))), font(max(10, round(15 * k)))
        text = self.query + self.composition
        while text and face.get_rect('>' + text + '_').width > self.box.w - 12:
            text = text[1:]
        x, y = self.box.x + 6, self.box.y + 3
        face.render_to(frame, (x, y), '>' + text + '_', (255, 255, 255))
        for i, result in enumerate(self.results):
            y = self.box.y + (i + 1) * row
            if i == (self.hovered if self.hovered is not None else self.selected):
                pygame.draw.rect(frame, (45, 45, 45), (self.box.x + 1, y, self.box.w - 2, row))
            right = self.box.right - 6 - small.get_rect(result['position']).width
            frame.set_clip(pygame.Rect(x, y, max(0, right - x - 10), row))
            name = fit_name(result['name'], face, max(0, right - x - 10))
            if name:
                face.render_to(frame, (x, y + 3), name, (221, 221, 221))
            frame.set_clip(self.box.inflate(-8, -2))
            small.render_to(frame, (right, y + 5), result['position'], pygame.Color(result['colour']))
        if status:
            face.render_to(frame, (x, self.box.y + row + 3), status, (150, 150, 150))
        frame.set_clip(clip)
        pygame.key.set_text_input_rect(pygame.Rect(self.box.x, self.box.y, self.box.w, row)
                                       .move(frame.get_abs_offset()))
