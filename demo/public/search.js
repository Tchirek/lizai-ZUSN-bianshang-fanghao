// The map's search, as the station's (hamclock/map_search.py, webmap.js), worked out in the page:
// S or / opens it; every word held in a name, as written or as said in pinyin ("quoted" only as
// written), none after a -, of the kind type: names (place, airport, station), near the city
// city: (or in:) names; a page at a time, the lit row boxed on the map. Its letters are HamClock's
// dots (data/glyphs.json: Courier Prime Sans, and the Song for Chinese).
import {W, H, polar} from './geo.js';
import {bits} from './tft.js';

const SEARCH = {width: 600, row: 30, pad: 6, gap: 12, cursor: 8, page: 8, scroll: 90}, LIMIT = 200, NEAR_KM = 25, ELLIPSIS = '…', MARK = 9;
const WORD = /(-?)(?:([a-z]+):)?(?:"([^"]*)"?|“([^”]*)”?|(\S+))/g, OPERATORS = ['city', 'in', 'type'];
const stage = document.querySelector('#stage'), element = document.querySelector('#search'), input = document.querySelector('#search-query');
const results = document.querySelector('#search-results'), note = document.querySelector('#search-note'), canvas = document.querySelector('#search-dots');
const dots = new Map();
let ctx = null, rows = [], index = 0, hover = null, timer = 0, focusBefore = null, marked = null, scrolled = 0;

export function searchOpen() { return !element.hidden; }
// HamClock's own Latin and the Song, as data/glyphs.json holds them
export function useGlyphs(tables) {
  for (const name of ['fast', 'song']) for (const [c, [xo, yo, adv, w, h, b]] of Object.entries(tables[name] || {}))
    if (!dots.has(c)) dots.set(c, [xo, yo, adv, w, h, bits(b)]);
}

// ------------------------------------------------------------------ names as said ----

const normal = text => text.normalize('NFKC').toLowerCase();
const alnum = ch => /[\p{L}\p{N}]/u.test(ch);
// a name as written, the sound of each character (its pinyin, else itself), and its letters run
// together with where each syllable or word starts (map_search.spelled)
function spelled(name, sounds) {
  const plain = [...normal(name)], said = sounds && sounds.length === plain.length ? sounds : plain;
  let text = '', spell = '', word = true;
  const out = [], starts = new Set();
  plain.forEach((ch, i) => {
    if (!/\s/.test(ch)) { text += ch; out.push(said[i]); }
    if (!alnum(ch)) { word = true; return; }
    if (said[i] !== ch || word) starts.add(spell.length);
    spell += said[i]; word = false;
  });
  return {text, sounds: out, spell, starts};
}
function heard(said, spell) {
  let at = said.spell.indexOf(spell);
  while (at >= 0 && !said.starts.has(at)) at = said.spell.indexOf(spell, at + 1);
  return at >= 0;
}
function parse(query) {
  const words = [];
  for (const [, out, op0, quoted, curly, bare] of normal(query).matchAll(WORD)) {
    let op = op0 || '', text = quoted ?? curly ?? bare ?? '';
    if (op && !OPERATORS.includes(op)) { text = op + ':' + text; op = ''; }
    text = text.replace(/\s+/g, '');
    if (text) words.push({out: !!out, op, text, literal: quoted !== undefined || curly !== undefined});
  }
  return words;
}
// what is asked in letters: Chinese read as a name holding it reads it, else as names mostly do
function spellOf(text, all) {
  let sounds = null;
  for (const row of all) for (const s of row.said) { const at = s.text.indexOf(text); if (at >= 0 && s.sounds.slice(at, at + [...text].length).some((v, i) => v !== [...text][i])) { sounds = s.sounds.slice(at, at + [...text].length); break; } }
  sounds ??= [...text];
  return [...text].map((ch, i) => alnum(ch) ? sounds[i] : '').join('');
}

// ------------------------------------------------------------------ rows ----

let deps = null;
const KIND = {city: 'place', town: 'place', airport: 'airport', digital: 'station', aprs: 'station'};
function entries() {
  const out = [], {sources} = deps;
  for (const source of [sources.static, sources.live]) for (const f of source.getFeatures()) {
    const kind = KIND[f.get('kind')];
    if (!kind) continue;
    const point = f.getGeometry().getCoordinates(), x = W / 2 + point[0] / deps.metres, y = H / 2 - point[1] / deps.metres;
    if (x < 0 || x >= W || y < 0 || y >= H) continue;
    const name = f.get('full') || f.get('call') || f.get('name'), zh = f.get('zh') || '';
    const colour = f.get('kind') === 'aprs' ? (f.get('rf') ? '#00dcff' : '#00647d') : f.get('colour') || {city: '#e6e6e6', town: '#969696', airport: '#96afff'}[f.get('kind')];
    const [lat, lon] = deps.latlon(...point);
    out.push({name: zh && zh !== name ? `${name} ${zh}` : name, point, colour, kind, lat, lon,
      position: `${Math.abs(lat).toFixed(4)}${lat >= 0 ? 'N' : 'S'} ${Math.abs(lon).toFixed(4)}${lon >= 0 ? 'E' : 'W'}`,
      said: [spelled(name), ...zh ? [spelled(zh, f.get('sounds'))] : []], far: Math.hypot(...point)});
  }
  return out;
}
export function search(all, query) {
  const words = parse(query);
  if (!words.some(w => !w.out)) return [];
  let keep = all.map(() => true), written = all.map(() => true);
  for (const {out, op, text, literal} of words) {
    let hit;
    if (op === 'type') hit = all.map(r => r.kind.startsWith(text));
    else if (op) {
      const spell = spellOf(text, all), towns = all.filter(r => r.kind === 'place' && r.said.some(s => s.text.startsWith(text) || spell && s.spell.startsWith(spell)));
      hit = all.map(r => towns.some(t => polar(t.lat, t.lon, r.lat, r.lon)[0] <= NEAR_KM));
    } else {
      const spell = literal ? '' : spellOf(text, all), plain = all.map(r => r.said.some(s => s.text.includes(text)));
      hit = all.map((r, i) => plain[i] || !!spell && r.said.some(s => heard(s, spell)));
      if (!out) written = written.map((w, i) => w && plain[i]);
    }
    keep = keep.map((k, i) => k && (out ? !hit[i] : hit[i]));
  }
  const terms = words.filter(w => !w.out && !w.op).map(w => w.text).join('');
  const found = all.map((r, i) => [r, written[i]]).filter((_, i) => keep[i]);
  const rank = ([r, w]) => [!r.said.some(s => s.text === terms), !(terms && r.said.some(s => s.text.startsWith(terms))), !w, r.far];
  found.sort((a, b) => { const x = rank(a), y = rank(b); for (let i = 0; i < x.length; i++) if (x[i] !== y[i]) return x[i] < y[i] ? -1 : 1; return a[0].name < b[0].name ? -1 : 1; });
  return found.slice(0, LIMIT).map(([r]) => r);
}

// ------------------------------------------------------------------ the box, in dots ----

function dotWidth(text) { let w = 0; for (const c of text) w += dots.get(c)?.[2] ?? 0; return w; }
function dotFit(text, room) {
  if (dotWidth(text) <= room) return text;
  const tail = dots.has(ELLIPSIS) ? ELLIPSIS : '...', chars = [...text];
  while (chars.length && dotWidth(chars.join('') + tail) > room) chars.pop();
  return dotWidth(tail) <= room ? chars.join('') + tail : '';
}
function packed(colour) { const [r, g, b] = [1, 3, 5].map(i => parseInt(colour.slice(i, i + 2), 16)); return (255 << 24 | b << 16 | g << 8 | r) >>> 0; }
function dotFill(image, x, y, w, h, colour) { const px = new Uint32Array(image.data.buffer), c = packed(colour); for (let row = y; row < y + h; row++) px.fill(c, row * image.width + x, row * image.width + x + w); }
function dotPrint(image, text, x, y, colour, right = image.width) {
  const px = new Uint32Array(image.data.buffer), c = packed(colour);
  for (const ch of text) {
    const g = dots.get(ch);
    if (!g) continue;
    const [xo, yo, adv, w, h, bits] = g;
    for (let i = 0; i < w * h; i++) if (bits[i >> 3] & 128 >> (i & 7)) {
      const dx = x + xo + i % w, dy = y + yo + Math.floor(i / w);
      if (dx >= 0 && dx < right && dy >= 0 && dy < image.height) px[dy * image.width + dx] = c;
    }
    x += adv;
  }
}
function page() { const first = Math.floor(index / SEARCH.page) * SEARCH.page; return [first, rows.slice(first, first + SEARCH.page)]; }
function draw() {
  if (element.hidden) return;
  const {width, row, pad, gap, cursor} = SEARCH, status = note.textContent, [first, shown] = page(), lines = 1 + shown.length + (status ? 1 : 0);
  const image = new ImageData(width, row * lines);
  dotFill(image, 0, 0, width, row * lines, '#000000');
  const ruler = rows.length > SEARCH.page ? `${first + 1}-${first + shown.length}/${rows.length}` : '', room = width - pad - (ruler ? dotWidth(ruler) + gap : 0);
  dotPrint(image, ruler, width - pad - dotWidth(ruler), cursor, '#969696');
  const value = input.value, at = input.selectionStart ?? value.length, before = [...value.slice(0, at)];
  while (before.length && dotWidth('>' + before.join('') + '_') > room - pad) before.shift();
  dotPrint(image, '>' + before.join('') + value.slice(at), pad, cursor, '#ffffff', room);
  dotPrint(image, '_', pad + dotWidth('>' + before.join('')), cursor, '#ffffff', room);
  const lit = hover ?? index;
  shown.forEach((r, j) => {
    const i = first + j, y = (j + 1) * row, right = width - pad - dotWidth(r.position);
    if (i === lit) dotFill(image, 1, y, width - 2, row, '#2d2d2d');
    dotPrint(image, dotFit(r.name, right - gap - pad), pad, y + cursor, '#dddddd');
    dotPrint(image, r.position, right, y + cursor, r.colour);
  });
  if (status) dotPrint(image, status, pad, row + cursor, '#969696');
  dotFill(image, 0, 0, width, 1, '#888888'); dotFill(image, 0, row * lines - 1, width, 1, '#888888');
  dotFill(image, 0, 0, 1, row * lines, '#888888'); dotFill(image, width - 1, 0, 1, row * lines, '#888888');
  canvas.width = width; canvas.height = row * lines; canvas.style.setProperty('--lines', lines);
  canvas.getContext('2d').putImageData(image, 0, 0);
}
// the lit row on the map: a box round its point, in its colour; the map is left where it is
function mark() {
  const row = element.hidden ? null : rows[hover ?? index] ?? null;
  if (row === marked) return;
  marked = row; deps.sources.mark.clear();
  if (!row) return;
  const f = new ol.Feature(new ol.geom.Point(row.point)), k = stage.clientWidth / 1600;
  const square = (stroke, width) => new ol.style.Style({image: new ol.style.RegularShape({points: 4, radius: MARK * k * Math.SQRT2, angle: Math.PI / 4, stroke: new ol.style.Stroke({color: stroke, width})})});
  f.setStyle([square('#000', 4), square(row.colour, 2)]);
  deps.sources.mark.addFeature(f);
}
function highlight() {
  const lit = hover ?? index;
  [...results.children].forEach((item, i) => item.setAttribute('aria-selected', String(i === lit)));
  if (rows.length) input.setAttribute('aria-activedescendant', results.children[lit].id); else input.removeAttribute('aria-activedescendant');
  draw(); mark();
}
function select(i) { index = i; highlight(); }
function step(delta) { if (rows.length) { const from = hover ?? index; hover = null; select((from + delta + rows.length) % rows.length); } }
function turn(delta) {
  const from = Math.floor((hover ?? index) / SEARCH.page), to = Math.min(Math.max(from + delta, 0), Math.ceil(rows.length / SEARCH.page) - 1);
  if (rows.length && to !== from) { hover = null; select(to * SEARCH.page); }
}
function close() {
  clearTimeout(timer); element.hidden = true; input.setAttribute('aria-expanded', 'false'); mark();
  input.blur(); (focusBefore && focusBefore !== document.body ? focusBefore : deps.mapElement).focus({preventScroll: true});
}
function open() { focusBefore = document.activeElement; input.value = ''; element.hidden = false; input.focus({preventScroll: true}); queue(); }
function choose(i) {
  const row = rows[i];
  if (!row) return;
  const view = deps.view();
  close(); view.setResolution(Math.min(view.getResolution(), deps.initialResolution() / 8)); view.setCenter(row.point); deps.inspect(row.point);
  deps.mapElement.focus({preventScroll: true});
}
function queue() {
  clearTimeout(timer); rows = []; hover = null; results.replaceChildren(); note.textContent = ''; select(0); input.setAttribute('aria-expanded', 'false');
  const query = input.value.trim();
  if (!query) return;
  timer = setTimeout(() => {
    rows = search(entries(), query);
    rows.forEach((row, i) => { const item = document.createElement('div'); item.id = 'search-result-' + i; item.setAttribute('role', 'option'); item.textContent = row.name + ' ' + row.position; item.addEventListener('click', () => choose(i)); results.append(item); });
    input.setAttribute('aria-expanded', String(rows.length > 0)); note.textContent = rows.length ? '' : 'No matches'; select(0);
  }, 150);
}
function line(e) { const box = canvas.getBoundingClientRect(); return Math.floor((e.clientY - box.top) * canvas.height / box.height / SEARCH.row) - 1; }

export function startSearch(given) {
  deps = given;
  input.addEventListener('input', e => { draw(); if (!e.isComposing) queue(); });
  for (const event of ['select', 'keyup', 'click']) input.addEventListener(event, draw);
  document.addEventListener('selectionchange', () => { if (document.activeElement === input) draw(); });
  canvas.addEventListener('mousedown', e => e.preventDefault());
  deps.mapElement.addEventListener('mousedown', e => { if (!element.hidden) e.preventDefault(); });
  canvas.addEventListener('pointermove', e => { const i = line(e), [first, shown] = page(), h = i >= 0 && i < shown.length ? first + i : null; if (h !== hover) { hover = h; highlight(); } });
  canvas.addEventListener('pointerleave', () => { if (hover !== null) { hover = null; highlight(); } });
  canvas.addEventListener('click', e => { const i = line(e), [first, shown] = page(); if (i >= 0 && i < shown.length) choose(first + i); else input.focus({preventScroll: true}); });
  element.addEventListener('wheel', e => {
    e.preventDefault(); e.stopPropagation();
    if (e.deltaMode) { turn(Math.sign(e.deltaY)); return; }
    scrolled += e.deltaY;
    if (Math.abs(scrolled) >= SEARCH.scroll) { turn(Math.sign(scrolled)); scrolled = 0; }
  }, {passive: false});
  input.addEventListener('compositionstart', () => clearTimeout(timer));
  input.addEventListener('compositionend', queue);
  element.addEventListener('keydown', e => {
    e.stopPropagation();
    if (e.isComposing || e.keyCode === 229) return;
    if (e.key === 'Escape') { e.preventDefault(); close(); }
    else if (e.target === input && e.key === 'Backspace' && !input.value) { e.preventDefault(); if (!e.repeat) close(); }
    else if (e.target === input && ['ArrowUp', 'ArrowDown', 'Tab', 'Enter'].includes(e.key)) {
      e.preventDefault();
      if (e.key === 'Enter') choose(hover ?? index); else step(e.key === 'ArrowUp' || e.key === 'Tab' && e.shiftKey ? -1 : 1);
    } else if (e.target === input && (['PageUp', 'PageDown'].includes(e.key) || e.ctrlKey && ['b', 'f'].includes(e.key.toLowerCase()))) {
      e.preventDefault(); turn(['PageDown', 'f', 'F'].includes(e.key) ? 1 : -1);
    }
  });
  element.addEventListener('keyup', e => e.stopPropagation());
  document.addEventListener('keydown', e => {
    if (e.defaultPrevented || e.isComposing || e.ctrlKey || e.altKey || e.metaKey || e.target.closest('input,textarea,select,[contenteditable]')) return;
    if ((e.key.toLowerCase() === 's' || e.key === '/') && deps.view()) { e.preventDefault(); if (!e.repeat) open(); }
  }, true);
}
