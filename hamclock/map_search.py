"""Search the names already on the map, in base pixels, for both displays."""
import json
from pathlib import Path
import unicodedata

import activity as a
import dots
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


WIDTH, ROW, PAD, GAP = 600, 30, 6, 12   # HamClock px: panel, line, margin, name to position
CURSOR = 8                              # a line's top to HamClock's text cursor


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
        """Drawn in HamClock's own pixels, as its panes are, then scaled with the clock."""
        import pygame
        status = self.error or ('No matches' if self.query.strip() and not self.future and not self.results else '')
        panel = pygame.Surface((WIDTH, ROW * (1 + len(self.results) + bool(status))))
        text = self.query + self.composition
        while text and dots.width('>' + text + '_') > WIDTH - 2 * PAD:
            text = text[1:]
        dots.blit(panel, '>' + text + '_', (PAD, CURSOR), (255, 255, 255))
        lit = self.hovered if self.hovered is not None else self.selected
        for i, result in enumerate(self.results):
            y = (i + 1) * ROW
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
