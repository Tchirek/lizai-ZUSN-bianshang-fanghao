"""MIL-STD-2525 / APP-6 land unit symbols, drawn from their geometry, and the board they sit on.

A symbol is a frame for its affiliation (friend, hostile, neutral, unknown), filled with the
standard's light colour and outlined in black, with the icon of its function inside, in the
standard's 200 x 200 drawing space (y down, the friend frame 150 x 100 in the middle). Icons
that span the frame (infantry's X, reconnaissance's slash, medical's cross, signal's bolt, air
defence's dome) run to each frame's own corners; the rest are the same size in every frame.

shapes() gives every symbol as plain geometry, for pygame here and the web page alike; Board
keeps the symbols placed on the map (~/influ/model/symbols.json), shared by both displays.
"""
import fcntl
import json
import math
import os
import secrets
import time

import activity as a

AFFILIATIONS = ('friend', 'hostile', 'neutral', 'unknown')          # F1 .. F4
FUNCTIONS = ('infantry', 'armour', 'mechanised', 'artillery', 'reconnaissance',
             'engineer', 'air_defence', 'signal', 'medical', 'aviation')   # 1 .. 9, 0
FILL = {'friend': (128, 224, 255), 'hostile': (255, 128, 128),
        'neutral': (170, 255, 170), 'unknown': (255, 255, 128)}
LINE = 5                                # drawing-space units a line is wide
SIZE = 0.28                             # HamClock px a drawing-space unit: the friend frame 42 x 28
HELD = 140                              # alpha of a symbol in hand, or about to be put down


def arc(cx, cy, rx, ry, start, end, n=24):
    """Points along an ellipse from angle start to end (degrees, 0 east, y down)."""
    return [(cx + rx * math.cos(math.radians(start + (end - start) * i / n)),
             cy + ry * math.sin(math.radians(start + (end - start) * i / n))) for i in range(n + 1)]


# the unknown frame: four half circles of radius 35 round the middle, meeting at its corners
FOIL = (arc(100, 65, 35, 35, 180, 360)[:-1] + arc(135, 100, 35, 35, 270, 450)[:-1]
        + arc(100, 135, 35, 35, 0, 180)[:-1] + arc(65, 100, 35, 35, 90, 270)[:-1])
FRAMES = {                              # the frame's ring, and the corners icons that span it run to
    'friend': ([(25, 50), (175, 50), (175, 150), (25, 150)], (25, 50, 175, 150)),
    'hostile': ([(100, 28), (172, 100), (100, 172), (28, 100)], (64, 64, 136, 136)),
    'neutral': ([(45, 45), (155, 45), (155, 155), (45, 155)], (45, 45, 155, 155)),
    'unknown': (FOIL, (65, 65, 135, 135)),
}


def icon(function, box):
    """[(points, closed, filled)] of a function's icon; box: the corners spanning icons reach."""
    x0, y0, x1, y1 = box
    at = lambda u, v: (x0 + (x1 - x0) * u, y0 + (y1 - y0) * v)          # noqa: E731
    oval = arc(80, 100, 20, 20, 90, 270, 12) + arc(120, 100, 20, 20, -90, 90, 12)
    cross = [([at(0, 0), at(1, 1)], False, False), ([at(0, 1), at(1, 0)], False, False)]
    return {
        'infantry': cross,
        'armour': [(oval, True, False)],
        'mechanised': cross + [(oval, True, False)],
        'artillery': [(arc(100, 100, 15, 15, 0, 360, 24), True, True)],
        'reconnaissance': [([at(0, 1), at(1, 0)], False, False)],
        'engineer': [([(65, 117), (65, 85), (135, 85), (135, 117)], False, False), ([(100, 85), (100, 117)], False, False)],
        'air_defence': [(arc((x0 + x1) / 2, y1, (x1 - x0) / 2, (y1 - y0) * 0.3, 180, 360), False, False)],
        'signal': [([at(0, 0), at(0.5, 0.72), at(0.5, 0.28), at(1, 1)], False, False)],
        'medical': [([at(0.5, 0), at(0.5, 1)], False, False), ([at(0, 0.5), at(1, 0.5)], False, False)],
        'aviation': [([(62, 78), (100, 100), (62, 122)], True, False), ([(138, 78), (100, 100), (138, 122)], True, False)],
    }[function]


def shape(affiliation, function):
    """The frame ring, its fill, and the icon's strokes."""
    ring, box = FRAMES[affiliation]
    return dict(frame=[list(p) for p in ring], fill='#%02x%02x%02x' % FILL[affiliation],
                icon=[dict(points=[list(p) for p in points], closed=closed, filled=filled)
                      for points, closed, filled in icon(function, box)])


def shapes():
    """Every symbol's geometry, for the web page: {affiliation: {function: shape}}, and how wide
    a line is and how big a unit."""
    return dict(line=LINE, size=SIZE, affiliations=AFFILIATIONS, functions=FUNCTIONS,
                shapes={af: {fn: shape(af, fn) for fn in FUNCTIONS} for af in AFFILIATIONS})


_drawn = {}


def surface(affiliation, function, scale, alpha=255):
    """A pygame surface of the symbol, scale px a drawing-space unit, drawn four times as large
    and shrunk so its lines come out smooth; the drawing space's middle is the surface's."""
    key = affiliation, function, round(scale, 3), alpha
    if key not in _drawn:
        import pygame
        big = 4 * scale
        canvas = pygame.Surface((math.ceil(200 * big), math.ceil(200 * big)), pygame.SRCALPHA)
        line = max(1, round(LINE * big))
        sh = shape(affiliation, function)
        ring = [(x * big, y * big) for x, y in sh['frame']]
        pygame.draw.polygon(canvas, pygame.Color(sh['fill']), ring)
        pygame.draw.polygon(canvas, (0, 0, 0), ring, line)
        for stroke in sh['icon']:
            points = [(x * big, y * big) for x, y in stroke['points']]
            if stroke['filled']:
                pygame.draw.polygon(canvas, (0, 0, 0), points)
            else:
                pygame.draw.lines(canvas, (0, 0, 0), stroke['closed'], points, line)
                for p in points:                              # round joints
                    pygame.draw.circle(canvas, (0, 0, 0), p, line / 2)
        out = pygame.transform.smoothscale(canvas, (math.ceil(200 * scale), math.ceil(200 * scale)))
        if alpha < 255:
            out.fill((255, 255, 255, alpha), special_flags=pygame.BLEND_RGBA_MULT)
        _drawn[key] = out
    return _drawn[key]


def reach(affiliation, scale):
    """How far the frame reaches from the middle, px at scale: (x, y), for telling a click on it."""
    ring = FRAMES[affiliation][0]
    return (max(abs(x - 100) for x, _ in ring) * scale, max(abs(y - 100) for _, y in ring) * scale)


class Board:
    """The symbols placed on the map: [{id, affiliation, function, lat, lon}], kept in one file
    both displays change under a lock, each change counted (rev) so the other one notices."""

    def __init__(self, path=None):
        self.path = path or os.path.join(a.MODEL, 'symbols.json')
        self.seen, self.rev, self.symbols = None, 0, []

    def stamp(self):
        """The file's (mtime, inode): each change replaces it, so the inode tells two changes apart
        within one tick of the clock."""
        try:
            st = os.stat(self.path)
            return st.st_mtime_ns, st.st_ino
        except FileNotFoundError:
            return None

    def poll(self):
        """Read the file again if it changed; True if it did."""
        stamp = self.stamp()
        if stamp == self.seen:
            return False
        self.seen = stamp
        data = self.read()
        self.rev, self.symbols = data['rev'], data['symbols']
        return True

    def read(self):
        try:
            with open(self.path, encoding='utf-8') as f:
                data = json.load(f)
            return dict(rev=int(data.get('rev', 0)), symbols=list(data.get('symbols', [])))
        except (FileNotFoundError, ValueError):
            return dict(rev=0, symbols=[])

    def change(self, op, **fields):
        """Apply one change under the file's lock: add (affiliation, function, lat, lon; an id
        given back restores a symbol removed), move (id, lat, lon) or remove (id). Returns the
        symbol as it was before (None for a new one), for undoing."""
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        with open(self.path + '.lock', 'w') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            data = self.read()
            symbols = {s['id']: s for s in data['symbols']}
            before = symbols.get(fields.get('id'))
            if op == 'add':
                if fields['affiliation'] not in AFFILIATIONS or fields['function'] not in FUNCTIONS:
                    raise ValueError('unknown symbol')
                sid = fields.get('id') or secrets.token_hex(6)
                symbols[sid] = dict(id=sid, affiliation=fields['affiliation'], function=fields['function'],
                                    lat=round(float(fields['lat']), 6), lon=round(float(fields['lon']), 6),
                                    placed=fields.get('placed', time.time()))
            elif op == 'move' and before:
                symbols[before['id']] = dict(before, lat=round(float(fields['lat']), 6), lon=round(float(fields['lon']), 6))
            elif op == 'remove':
                symbols.pop(fields.get('id'), None)
            elif op != 'move':
                raise ValueError('unknown change')
            out = dict(rev=data['rev'] + 1, symbols=sorted(symbols.values(), key=lambda s: s['placed']))
            with open(self.path + '.tmp', 'w', encoding='utf-8') as f:
                json.dump(out, f, ensure_ascii=False)
            os.replace(self.path + '.tmp', self.path)
            self.seen, self.rev, self.symbols = self.stamp(), out['rev'], out['symbols']
        return before


class Desk:
    """One display's hand on the board: the symbol it is about to put down (armed), the one it
    holds (carried, following the pointer until put down), and what it did, to undo."""

    def __init__(self, board=None):
        self.board = board or Board()
        self.armed = None                       # (affiliation, function)
        self.carried = None                     # the symbol as it was when picked up
        self.done = []                          # (op, the symbol before) to undo, last last
        self.board.poll()

    def symbols(self):
        return [s for s in self.board.symbols if not (self.carried and s['id'] == self.carried['id'])]

    def arm(self, affiliation, function):
        self.armed = affiliation, function

    def place(self, lat, lon):
        sid = secrets.token_hex(6)
        self.board.change('add', id=sid, affiliation=self.armed[0], function=self.armed[1], lat=lat, lon=lon)
        self.done.append(('add', None, sid))

    def pick(self, sid):
        self.carried = next((s for s in self.board.symbols if s['id'] == sid), None)

    def drop(self, lat, lon):
        if self.carried:
            before = self.board.change('move', id=self.carried['id'], lat=lat, lon=lon)
            self.done.append(('move', before, self.carried['id']))
            self.carried = None

    def remove(self):
        if self.carried:
            before = self.board.change('remove', id=self.carried['id'])
            self.done.append(('remove', before, self.carried['id']))
            self.carried = None

    def cancel(self):
        """Esc: put back what is held, else stop putting down; False if there was neither."""
        if self.carried:
            self.carried = None
        elif self.armed:
            self.armed = None
        else:
            return False
        return True

    def undo(self):
        if not self.done:
            return False
        op, before, sid = self.done.pop()
        if op == 'add':
            self.board.change('remove', id=sid)
        elif before:                            # moved or removed: as it was
            self.board.change('add' if op == 'remove' else 'move', **before)
        return True

    def at(self, symbol, view, scale):
        """Where a symbol is on the map box, px."""
        x, y = view.project(view.base.xy(symbol['lat'], symbol['lon']))
        return x * scale, y * scale

    def hit(self, view, scale, at):
        """The id of the symbol under at (map box px), the topmost; None if none."""
        for s in reversed(self.symbols()):
            x, y = self.at(s, view, scale)
            rx, ry = reach(s['affiliation'], SIZE * scale)
            if abs(at[0] - x) <= rx and abs(at[1] - y) <= ry:
                return s['id']
        return None

    def draw(self, area, view, scale, pointer):
        """The symbols on the map box (area), the one held and the one armed at the pointer
        (map box px, None when it is off the map), see-through."""
        k = SIZE * scale
        for s in self.symbols():
            sym = surface(s['affiliation'], s['function'], k)
            x, y = self.at(s, view, scale)
            area.blit(sym, (round(x - sym.get_width() / 2), round(y - sym.get_height() / 2)))
        held = self.carried and (self.carried['affiliation'], self.carried['function']) or self.armed
        where = pointer or (self.carried and self.at(self.carried, view, scale))     # held off the map: where it was
        if held and where:
            sym = surface(*held, k, HELD)
            area.blit(sym, (round(where[0] - sym.get_width() / 2), round(where[1] - sym.get_height() / 2)))
