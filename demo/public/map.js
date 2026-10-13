// The coverage map in HamClock's map box, as the station's web page shows it (hamclock/webmap.js),
// worked out here in the page instead of on the station: azimuthal equidistant about the summit,
// the ground drawn by a worker (terrain.js), the night, the Maidenhead grid, range rings, places,
// rivers and civil airports, live APRS, FT8 and WSPR, and the VHF/UHF path popup.
import {W, H, K_FACTOR, R_EARTH, SUNSET, DARK, forward, inverse, polar, destination, demMetres, solarElevation, sun, sunTimes} from './geo.js';
import {live, startLive, digitalGraph, nearby} from './live.js';
import {showWeather} from './clock.js';
import {startSearch, searchOpen} from './search.js';

const LEVELS = [1, 2, 4, 8, 16, 24], TILE = 256, MAX_ZOOM = 96;
const FREQUENCIES = [144.174, 144.640, 145.025, 435.000], HEIGHTS = [10, 20, 40, 80];
const colours = {river: '#4678d2', field: 'rgba(175,180,255,.65)', square: 'rgba(175,180,255,.35)', subsquare: 'rgba(175,180,255,.18)',
  grid_name: '#b9beff', subgrid_name: '#9196d2', city: '#e6e6e6', town: '#969696', airport: '#96afff', runway: '#cdcdd2',
  far: '#bfce25', station: '#ffffff', aprs: '#00b4dc', aprs_path: 'rgba(0,180,220,.5)'};
const LABEL_FONT = 'px Arial,"Microsoft YaHei","PingFang SC",sans-serif', TAG_FONT = 'italic bold 13' + LABEL_FONT, SUN_GOLD = 'rgb(255,210,120)';
const SDO = 'https://sdo.gsfc.nasa.gov/assets/img/latest/latest_256_HMIIC.jpg';

const stage = document.querySelector('#stage'), mapElement = document.querySelector('#map'), profileElement = document.querySelector('#profile');
const zoomIn = new Set(['+', '=', 'PageUp']), zoomOut = new Set(['-', '_', 'PageDown']);
const mapKeys = new Set(['ArrowLeft', 'ArrowRight', 'ArrowUp', 'ArrowDown', ...zoomIn, ...zoomOut]);
export const sources = {static: new ol.source.Vector(), grid: new ol.source.Vector(), live: new ol.source.Vector(), ring: new ol.source.Vector(),
  selected: new ol.source.Vector(), tag: new ol.source.Vector(), mark: new ol.source.Vector()};
const styles = new Map(), keys = new Set(), measure = document.createElement('canvas').getContext('2d'), widths = new Map();
export let map = null, view = null, site = null, info = null, initialResolution = 1;
let projection = null, metres = 1, extent = null, tag = null, legend = null, worker = null, asks = new Map(), asked = 0;
let veilSource = null, lineSource = null, profilePoint = null, profileSerial = 0, gridTimer = 0, heldInteraction = false;
let lastPadEnter = false, lastPadEscape = false, shiftDown = false, lastTick = performance.now();

function textWidth(text, font) { const k = font + '\n' + text; if (!widths.has(k)) { measure.font = font; widths.set(k, measure.measureText(text).width); } return widths.get(k); }
export function zoomRatio() { return view ? Math.max(1, Math.min(MAX_ZOOM, initialResolution / view.getResolution())) : 1; }
export function toBase(lat, lon) { const [x, y] = forward(site, lat, lon); return [W / 2 + x / metres, H / 2 - y / metres]; }
export function baseLatLon(x, y) { return inverse(site, (x - W / 2) * metres, (H / 2 - y) * metres); }
export function coordinate(lat, lon) { return forward(site, lat, lon); }
function point(xy, kind, name = '', props = {}) { const f = new ol.Feature({geometry: new ol.geom.Point(xy), kind, name, ...props}); return f; }
function line(xys, kind, props = {}) { return new ol.Feature({geometry: new ol.geom.LineString(xys), kind, ...props}); }
function ask(message, transfer = []) { return new Promise((resolve, reject) => { const id = ++asked; asks.set(id, {resolve, reject}); worker.postMessage({...message, id}, transfer); }); }

// ------------------------------------------------------------------ styles ----

function colour(f) { return f.get('colour') || colours[f.get('kind')] || '#dddddd'; }
function shownAt(f) { return (f.get('min_zoom') || 1) <= zoomRatio(); }
function geometryStyle(f) {
  const kind = f.get('kind'), type = f.getGeometry().getType();
  if (type === 'Point' || !shownAt(f)) return null;
  const c = colour(f), width = kind === 'runway' ? Math.max(2, Math.round((f.get('width') || 0) / view.getResolution())) : kind === 'river' ? 2 : 1;
  const k = kind + ':' + c + ':' + f.get('local') + ':' + width;
  if (!styles.has(k)) {
    const result = [new ol.style.Style({stroke: new ol.style.Stroke({color: kind === 'digital_path' ? (f.get('local') ? c : c + '50') : c, width})})];
    if (kind === 'river') result.unshift(new ol.style.Style({stroke: new ol.style.Stroke({color: '#13131c', width: 4})}));
    styles.set(k, result);
  }
  return styles.get(k);
}
function labelFont(grid) { return (grid ? 'bold 11' : 'bold 13') + LABEL_FONT; }
function labelOffset(kind) { const grid = kind === 'grid_name' || kind === 'subgrid_name'; return [grid ? 3 : 7, grid ? 8 : -8]; }
function labelStyle(f) {
  if (f.getGeometry().getType() !== 'Point' || !shownAt(f)) return null;
  const kind = f.get('kind'), name = f.get('name') || '', c = colour(f), label = f.get('label') || c, rf = f.get('rf'), k = kind + ':' + name + ':' + c + ':' + label + ':' + rf;
  if (!styles.has(k)) {
    const grid = kind === 'grid_name' || kind === 'subgrid_name';
    let image;
    if (!grid) {
      if (kind === 'digital') image = new ol.style.RegularShape({points: 4, radius: 5, angle: 0, fill: new ol.style.Fill({color: c}), stroke: new ol.style.Stroke({color: '#000', width: 1})});
      else image = new ol.style.Circle({radius: kind === 'station' || kind === 'airport' ? 4 : kind === 'city' ? 2 : 3, fill: new ol.style.Fill({color: kind === 'aprs' ? (rf ? '#00dcff' : '#00647d') : c}), stroke: new ol.style.Stroke({color: kind === 'airport' ? c : '#000', width: 1})});
    }
    const [dx, dy] = labelOffset(kind);
    const text = name ? new ol.style.Text({text: name, font: labelFont(grid), fill: new ol.style.Fill({color: label}), stroke: new ol.style.Stroke({color: '#000', width: 3}), offsetX: dx, offsetY: dy, textAlign: 'left', overflow: false}) : undefined;
    styles.set(k, new ol.style.Style({image, text}));
  }
  return styles.get(k);
}
function inView(f, e) { const p = f.getGeometry().getCoordinates(); return p[0] >= e[0] && p[0] <= e[2] && p[1] >= e[1] && p[1] <= e[3]; }

// ------------------------------------------------------------------ what is on the map ----

async function loadStatic() {
  const found = await (await fetch(`data/${site.id}/map.json`)).json().catch(() => ({places: [], airports: [], runways: [], rivers: []}));
  const features = [], rank = {yes: 0, 2: 0, 4: 1, 5: 2, 6: 3}, nameKm = info.reach_km * 1.1;
  for (const river of found.rivers) features.push(line(river.map(p => coordinate(...p)), 'river'));
  for (const r of found.runways || []) features.push(line(r.line.map(p => coordinate(...p)), 'runway', {min_zoom: 3, width: r.width}));
  for (const p of found.places.filter(p => p.name).sort((a, b) => (rank[a.capital] ?? 4) - (rank[b.capital] ?? 4) || b.pop - a.pop)) {
    const xy = toBase(p.lat, p.lon);
    if (xy[0] < 0 || xy[0] >= W || xy[1] < 0 || xy[1] >= H) continue;
    features.push(point(coordinate(p.lat, p.lon), (rank[p.capital] ?? 4) <= 2 ? 'city' : 'town', p.name, {min_zoom: polar(site.lat, site.lon, p.lat, p.lon)[0] <= nameKm ? 1 : 2, zh: p.zh, sounds: p.sounds}));
  }
  for (const a of found.airports) features.push(point(coordinate(a.lat, a.lon), 'airport', a.code, {full: [a.code, a.name].filter(Boolean).join(' '), zh: a.zh, sounds: a.sounds}));
  features.push(point(coordinate(info.far_lat, info.far_lon), 'far', `${Math.round(info.reach_km)} km ${Math.round(info.bearing)} deg ${Math.round(info.far_elev_m)} m`));
  features.push(point([0, 0], 'station', 'DE', {colour: '#ffffff'}));
  sources.static.addFeatures(features);
}
// the Maidenhead grid in view (webmap.MapData.grid)
function grid() {
  if (!map) return;
  const e = view.calculateExtent(map.getSize()), m = view.getResolution() * 150, b = [e[0] - m, e[1] - m, e[2] + m, e[3] + m], zoom = zoomRatio();
  const corners = [];
  for (const x of [b[0], (b[0] + b[2]) / 2, b[2]]) for (const y of [b[1], (b[1] + b[3]) / 2, b[3]]) corners.push(inverse(site, x, y));
  const south = Math.max(-85, Math.min(...corners.map(p => p[0])) - .1), north = Math.min(85, Math.max(...corners.map(p => p[0])) + .1);
  const west = Math.max(-180, Math.min(...corners.map(p => p[1])) - .1), east = Math.min(180, Math.max(...corners.map(p => p[1])) + .1);
  const steps = (lo, hi) => { const n = Math.max(2, Math.ceil((hi - lo) / .05)); return Array.from({length: n}, (_, i) => lo + (hi - lo) * i / (n - 1)); };
  const lats = steps(south, north), lons = steps(west, east), features = [], families = [[20, 10, 'field'], [2, 1, 'square']];
  if (zoom >= 12) families.push([1 / 12, 1 / 24, 'subsquare']);
  for (const [dlon, dlat, kind] of families) {
    for (let i = Math.ceil(west / dlon); i <= Math.floor(east / dlon); i++) features.push(line(lats.map(lat => coordinate(lat, i * dlon)), kind));
    for (let j = Math.ceil(south / dlat); j <= Math.floor(north / dlat); j++) features.push(line(lons.map(lon => coordinate(j * dlat, lon)), kind));
  }
  for (let i = Math.floor(west / 2); i < Math.ceil(east / 2); i++) for (let j = Math.floor(south); j < Math.ceil(north); j++) {
    const lon = i + 90, lat = j + 90;
    features.push(point(coordinate(j + 1, i * 2), 'grid_name', String.fromCharCode(65 + Math.floor(lon / 10), 65 + Math.floor(lat / 10)) + lon % 10 + lat % 10));
  }
  if (zoom >= 12) for (let i = Math.floor(west * 12); i < Math.ceil(east * 12); i++) for (let j = Math.floor(south * 24); j < Math.ceil(north * 24); j++)
    features.push(point(coordinate((j + 1) / 24, i / 12), 'subgrid_name', String.fromCharCode(97 + ((i % 24) + 24) % 24, 97 + ((j % 24) + 24) % 24)));
  sources.grid.clear(true); sources.grid.addFeatures(features); placeTag();
}
function scheduleGrid() { clearTimeout(gridTimer); gridTimer = setTimeout(grid, 180); }
const hex = rgb => '#' + rgb.map(v => v.toString(16).padStart(2, '0')).join('');
function showLive() {
  const {stations, bands, chosen, degrees, total} = digitalGraph(), features = [], at = xy => [(xy[0] - W / 2) * metres, (H / 2 - xy[1]) * metres];
  for (const [call, s] of stations) features.push(point(at(s.xy), 'digital', call + ((degrees.get(call) || 0) > 2 ? ` +${degrees.get(call)}` : ''), {colour: hex(s.rgb), label: s.sent ? '#ffffff' : '#969696', call}));
  for (const p of chosen) features.push(line(p.ends.map(at), 'digital_path', {colour: hex(p.rgb), local: p.local.length === 2}));
  for (const [call, [lat, lon, rf]] of live.aprs) features.push(point(coordinate(lat, lon), 'aprs', call, {rf: !!rf, call}));
  for (const k of live.links.keys()) { const [a, b] = k.split(' '); if (live.aprs.has(a) && live.aprs.has(b)) features.push(line([a, b].map(c => coordinate(...live.aprs.get(c).slice(0, 2))), 'aprs_path')); }
  sources.live.clear(true); sources.live.addFeatures(features);
  legend = {paths: chosen.length, total, bands: [...bands].sort((a, b) => BAND_ORDER(a[0]) - BAND_ORDER(b[0])).map(([name, rgb]) => ({name, colour: hex(rgb)}))};
  updateLegend(); placeTag();
}
const BAND_ORDER = name => ['160', '80', '60', '40', '30', '20', '17', '15', '12', '10', '6', '2', '70cm'].indexOf(name);
function updateLegend() {
  const element = document.querySelector('#legend');
  element.replaceChildren();
  if (!legend || !view) return;
  const e = view.calculateExtent(map.getSize()), r = view.getResolution() * 4, inner = [e[0] + r, e[1] + r, e[2] - r, e[3] - r];
  let aprs = 0, digital = 0;
  for (const f of sources.live.getFeatures()) { const kind = f.get('kind'); if ((kind === 'aprs' || kind === 'digital') && inView(f, inner)) kind === 'aprs' ? aprs++ : digital++; }
  for (const item of [{name: 'APRS ' + aprs, colour: '#00ffff'}, {name: 'DIGI ' + digital, colour: '#fff'}, {name: 'PATH ' + legend.paths + '/' + legend.total, colour: '#969696'},
    ...legend.bands.map(b => ({name: b.name.endsWith('cm') ? b.name : b.name + 'm', colour: b.colour}))]) {
    const span = document.createElement('span'), i = document.createElement('i');
    i.style.background = item.colour; span.append(i, document.createTextNode(item.name)); element.append(span);
  }
}
function showSun() {
  const now = Date.now() / 1000, elevation = solarElevation(site.lat, site.lon, now), element = document.querySelector('#sun');
  const time = t => new Intl.DateTimeFormat('en-GB', {timeZone: site.tz, hour: '2-digit', minute: '2-digit', hourCycle: 'h23'}).format(new Date(t * 1000));
  const parts = [['SUN', `${elevation >= 0 ? '+' : '−'}${Math.abs(Math.round(elevation))}°`]];
  sunTimes(site.lat, site.lon, now).forEach((t, i) => { if (t !== null) parts.push([i ? 'SET' : 'RISE', time(t)]); });
  const image = document.createElement('img');
  image.src = SDO; image.style.opacity = elevation > SUNSET ? '1' : '.43';
  element.replaceChildren(image);
  for (const [name, value] of parts) { const item = document.createElement('span'), b = document.createElement('b'); item.textContent = name; b.textContent = value; item.append(b); element.append(item); }
}

// ------------------------------------------------------------------ the night ----
// map_panel.twilight: a veil deepening from blue hour to indigo, golden hour faintly on the day
// side, a soft shadow on the terminator's night side, and the terminator, a gold line

const STEP = 8, BLUE_HOUR = [70, 60, 140], NIGHT = [8, 14, 44], VEIL = 112, GOLDEN = [255, 178, 86], GLOW = 30, GLOW_DEG = 5;
const EDGE = [26, 16, 0], EDGE_ALPHA = 0.6, LINE_PX = 2, SHADOW_PX = 14, SHADOW = 0.35;
let nightCache = null;
// the night over a canvas of `size` px showing extent e, both layers' pictures worked out at once
function nightAt(e, size) {
  const minute = Math.floor(Date.now() / 60000), key = [...e, ...size, minute].join();
  if (nightCache?.key === key) return nightCache;
  const [w, h] = size.map(Math.round), step = STEP, cols = Math.ceil(w / step) + 1, rows = Math.ceil(h / step) + 1, s = sun(minute * 60);
  const alt = new Float32Array(cols * rows);
  for (let j = 0; j < rows; j++) for (let i = 0; i < cols; i++) {
    const [lat, lon] = inverse(site, e[0] + (i * step + step / 2) / w * (e[2] - e[0]), e[3] - (j * step + step / 2) / h * (e[3] - e[1]));
    alt[j * cols + i] = solarElevation(lat, lon, 0, s);
  }
  const grads = [];
  for (let j = 0; j < rows - 1; j++) for (let i = 0; i < cols - 1; i++) grads.push(Math.hypot(alt[j * cols + i + 1] - alt[j * cols + i], alt[(j + 1) * cols + i] - alt[j * cols + i]));
  grads.sort((a, b) => a - b);
  const perPx = Math.max((grads[grads.length >> 1] || 0) / step, 1e-9);
  const coarse = new ImageData(cols, rows);
  for (let k = 0; k < cols * rows; k++) {
    const a = alt[k], o = 4 * k;
    if (a < SUNSET) { const t = Math.min(1, Math.max(0, (SUNSET - a) / (SUNSET - DARK))); for (let c = 0; c < 3; c++) coarse.data[o + c] = BLUE_HOUR[c] + (NIGHT[c] - BLUE_HOUR[c]) * t; coarse.data[o + 3] = VEIL * (1 - (1 - t) ** 2); }
    else { const u = Math.min(1, (a - SUNSET) / GLOW_DEG); for (let c = 0; c < 3; c++) coarse.data[o + c] = GOLDEN[c]; coarse.data[o + 3] = GLOW * (1 - u) ** 2; }
  }
  const small = new OffscreenCanvas(cols, rows); small.getContext('2d').putImageData(coarse, 0, 0);
  const veil = document.createElement('canvas'), lineCanvas = document.createElement('canvas');
  veil.width = lineCanvas.width = w; veil.height = lineCanvas.height = h;
  const v = veil.getContext('2d');
  v.imageSmoothingEnabled = true; v.drawImage(small, 0, 0, cols, rows, 0, 0, cols * step, rows * step);
  // px near the line, one by one: alt by bilinear between the samples
  const veilPx = v.getImageData(0, 0, w, h), linePx = new ImageData(w, h), reach = LINE_PX / 2 + 1.5;
  for (let j = 0; j < rows - 1; j++) for (let i = 0; i < cols - 1; i++) {
    const a = [alt[j * cols + i], alt[j * cols + i + 1], alt[(j + 1) * cols + i], alt[(j + 1) * cols + i + 1]];
    if (Math.max(...a) - SUNSET < -(SHADOW_PX + 2 * step) * perPx || Math.min(...a) - SUNSET > (reach + 2 * step) * perPx) continue;
    for (let y = Math.round(j * step + step / 2); y < Math.min(h, Math.round((j + 1) * step + step / 2)); y++) for (let x = Math.round(i * step + step / 2); x < Math.min(w, Math.round((i + 1) * step + step / 2)); x++) {
      const fx = (x - i * step - step / 2) / step, fy = (y - j * step - step / 2) / step;
      const off = ((a[0] * (1 - fx) + a[1] * fx) * (1 - fy) + (a[2] * (1 - fx) + a[3] * fx) * fy - SUNSET) / perPx, o = 4 * (y * w + x);
      if (off < 0 && off > -SHADOW_PX) over(veilPx.data, o, NIGHT, SHADOW * (1 + off / SHADOW_PX) ** 2);
      const d = Math.abs(off);
      if (d < reach) { over(linePx.data, o, EDGE, EDGE_ALPHA * Math.min(1, Math.max(0, reach - d))); over(linePx.data, o, [255, 210, 120], Math.min(1, Math.max(0, LINE_PX / 2 + .5 - d))); }
    }
  }
  v.putImageData(veilPx, 0, 0); lineCanvas.getContext('2d').putImageData(linePx, 0, 0);
  nightCache = {key, veil, line: lineCanvas, tag: crossings(e, size, minute * 60)};
  return nightCache;
}
function over(px, o, rgb, cover) {
  const under = px[o + 3] / 255 * (1 - cover), alpha = cover + under;
  for (let c = 0; c < 3; c++) px[o + c] = (cover * rgb[c] + px[o + c] * under) / Math.max(alpha, 1e-6);
  px[o + 3] = alpha * 255;
}
// where the terminator crosses rows 11 px apart, from the top down (map_view.crossings)
function crossings(e, [width, height], when) {
  const at = (x, y) => inverse(site, e[0] + x / width * (e[2] - e[0]), e[3] - y / height * (e[3] - e[1]));
  const corners = [[0, 0], [width, 0], [0, height], [width, height]].map(([x, y]) => solarElevation(...at(x, y), when) >= SUNSET);
  if (corners.every(c => c === corners[0])) return null;
  const xs = Array.from({length: 133}, (_, i) => i * width / 132), on = [];
  let first = null;
  for (let y = 5.5; y < height; y += 11) {
    let prev = null;
    for (let i = 0; i < xs.length; i++) {
      const a = solarElevation(...at(xs[i], y), when) - SUNSET;
      if (prev !== null && (prev >= 0) !== (a >= 0)) { on.push([xs[i - 1] + (xs[i] - xs[i - 1]) * prev / (prev - a), y]); first ??= at(xs[i - 1], y); break; }
      prev = a;
    }
  }
  if (on.length < 2) return null;
  const setting = solarElevation(...first, when + 60) < solarElevation(...first, when);
  const angle = Math.atan2(on.at(-1)[1] - on[0][1], on.at(-1)[0] - on[0][0]) * 180 / Math.PI;
  return {name: setting ? 'SUNSET' : 'SUNRISE', angle: Math.round(angle), lift: LINE_PX / 2 + 1 + 1.5,
          points: on.map(([x, y]) => [e[0] + x / width * (e[2] - e[0]), e[3] - y / height * (e[3] - e[1])])};
}
function nightSource(part) {
  return new ol.source.ImageCanvas({projection, ratio: 1.5, canvasFunction: (e, resolution, ratio, size) => {
    const found = nightAt(e, size);
    if (part === 'line') { tag = found.tag; setTimeout(placeTag); }
    return found[part];
  }});
}
function labelBoxes() {
  const boxes = [], e = view.calculateExtent(map.getSize());
  for (const source of [sources.grid, sources.static, sources.live]) for (const f of source.getFeatures()) {
    const name = f.get('name');
    if (!name || f.getGeometry().getType() !== 'Point' || !inView(f, e) || !shownAt(f)) continue;
    const kind = f.get('kind'), g = kind === 'grid_name' || kind === 'subgrid_name', size = g ? 11 : 13, [dx, dy] = labelOffset(kind), p = map.getPixelFromCoordinate(f.getGeometry().getCoordinates());
    boxes.push([p[0] + dx, p[1] + dy - size / 2 - 2, p[0] + dx + textWidth(name, labelFont(g)) + 2, p[1] + dy + size / 2 + 2]);
  }
  return boxes;
}
// SUNSET or SUNRISE on the line where it covers the least of the names (webmap.placeTag)
function placeTag() {
  sources.tag.clear(true);
  if (!tag || !map || !view) return;
  const [w, h] = map.getSize(), rad = tag.angle * Math.PI / 180, up = [Math.sin(rad), -Math.cos(rad)], along = [Math.cos(rad), Math.sin(rad)];
  const tw = textWidth(tag.name, TAG_FONT) + 2, th = 14, hw = (Math.abs(along[0]) * tw + Math.abs(up[0]) * th) / 2, hh = (Math.abs(along[1]) * tw + Math.abs(up[1]) * th) / 2;
  const taken = labelBoxes();
  let best = null, least = Infinity;
  for (const p0 of tag.points) {
    const p = map.getPixelFromCoordinate(p0);
    if (!p) continue;
    const base = [p[0] + up[0] * tag.lift, p[1] + up[1] * tag.lift], mid = [base[0] + up[0] * th * .35, base[1] + up[1] * th * .35], box = [mid[0] - hw, mid[1] - hh, mid[0] + hw, mid[1] + hh];
    if (box[0] < 4 || box[2] > w - 4 || box[1] < 28 || box[3] > h - 30) continue;
    let cover = 0;
    for (const t of taken) { const x = Math.min(box[2], t[2]) - Math.max(box[0], t[0]), y = Math.min(box[3], t[3]) - Math.max(box[1], t[1]); if (x > 0 && y > 0) cover += x * y; }
    if (cover < least) { least = cover; best = base; }
  }
  if (!best) return;
  const f = new ol.Feature(new ol.geom.Point(map.getCoordinateFromPixel(best)));
  f.setStyle(new ol.style.Style({text: new ol.style.Text({text: tag.name, font: TAG_FONT, rotation: rad, textAlign: 'center', textBaseline: 'alphabetic', fill: new ol.style.Fill({color: SUN_GOLD}), stroke: new ol.style.Stroke({color: '#000', width: 3})})}));
  sources.tag.addFeature(f);
}

// ------------------------------------------------------------------ rings, scale ----

function rings() {
  if (!view || !map) return;
  const wanted = view.getResolution() * 180, power = 10 ** Math.floor(Math.log10(wanted)), step = Math.max(...[1, 2, 5].map(x => x * power).filter(x => x <= wanted));
  const e = view.calculateExtent(map.getSize()), centre = view.getCenter(), distance = Math.hypot(...centre), span = Math.hypot(e[2] - e[0], e[3] - e[1]) / 2, features = [];
  for (let r = Math.max(step, Math.floor((distance - span) / step) * step); r < distance + span; r += step) features.push(new ol.Feature(new ol.geom.Circle([0, 0], r)));
  sources.ring.clear(true); sources.ring.addFeatures(features);
  const resolution = view.getResolution(), index = LEVELS.findIndex(level => metres / level <= resolution), i = index < 0 ? LEVELS.length - 1 : index;
  const most = 130 * mapElement.clientWidth / 1320 * resolution / 1000, decade = 10 ** Math.floor(Math.log10(most)), km = Math.max(...[1, 2, 5].map(x => x * decade).filter(x => x <= most));
  document.querySelector('#km').textContent = +km.toPrecision(3) + ' km';
  document.querySelector('#bar').style.width = Math.round(km * 1000 / resolution) + 'px';
  document.querySelector('#dem').textContent = 'DEM ' + Math.round(demMetres(LEVELS[i], info.km_per_px, site.lat)) + ' m  ' + zoomRatio().toFixed(1) + 'x';
}

// ------------------------------------------------------------------ the view ----

function makeView(center, ratio) {
  initialResolution = (extent[2] - extent[0]) / mapElement.clientWidth;
  const made = new ol.View({projection, center, extent, enableRotation: false, maxResolution: initialResolution, minResolution: initialResolution / MAX_ZOOM,
    resolution: initialResolution / ratio, constrainResolution: false, smoothExtentConstraint: false});
  made.on('change:resolution', rings);
  return made;
}
function resized() {
  if (!view || !mapElement.clientWidth) return;
  const ratio = zoomRatio(), center = view.getCenter();
  if (heldInteraction) { heldInteraction = false; view.endInteraction(0); }
  view = makeView(center, ratio); map.setView(view); rings(); scheduleGrid(); updateLegend();
}

export async function startMap(chosen, reach) {
  site = chosen; info = reach; metres = info.km_per_px * 1000;
  extent = [-W / 2 * metres, -H / 2 * metres, W / 2 * metres, H / 2 * metres];
  projection = new ol.proj.Projection({code: 'INFLU:AEQD:' + site.id, units: 'm', extent});
  worker = new Worker('terrain.js', {type: 'module'});
  worker.onmessage = ({data}) => { const a = asks.get(data.id); if (!a) return; asks.delete(data.id); data.error ? a.reject(Error(data.error)) : a.resolve(data); };
  await ask({type: 'start', site, info, reach: new URL(`data/${site.id}/reach.png`, location.href).href});
  const tileGrid = new ol.tilegrid.TileGrid({extent, origin: [extent[0], extent[3]], resolutions: LEVELS.map(l => metres / l), tileSize: TILE});
  const ground = new ol.layer.Tile({source: new ol.source.ImageTile({projection, tileGrid, wrapX: false, transition: 0, loader: async (z, x, y) => {
    const {px} = await ask({type: 'tile', level: LEVELS[z], column: x, row: y});
    return createImageBitmap(new ImageData(new Uint8ClampedArray(px.buffer), TILE, TILE));
  }})});
  veilSource = nightSource('veil'); lineSource = nightSource('line');
  view = makeView([0, 0], 1);
  const vector = source => new ol.layer.Vector({source, style: geometryStyle, renderBuffer: 150});
  const labels = source => new ol.layer.Vector({source, style: labelStyle, declutter: 'labels', renderBuffer: 150});
  const always = {updateWhileAnimating: true, updateWhileInteracting: true};
  map = new ol.Map({target: mapElement, view, controls: [], interactions: ol.interaction.defaults.defaults({altShiftDragRotate: false, pinchRotate: false, keyboard: false}),
    layers: [ground, new ol.layer.Image({source: veilSource}), vector(sources.static), vector(sources.grid),
      new ol.layer.Vector({source: sources.ring, style: new ol.style.Style({stroke: new ol.style.Stroke({color: 'rgba(190,190,190,.35)', width: 1})})}),
      vector(sources.live), new ol.layer.Image({source: lineSource}), labels(sources.grid), labels(sources.static), labels(sources.live),
      new ol.layer.Vector({source: sources.mark, ...always}),
      new ol.layer.Vector({source: sources.selected, style: new ol.style.Style({stroke: new ol.style.Stroke({color: '#e6e6b4', width: 1}), image: new ol.style.RegularShape({points: 4, radius: 6, radius2: 0, angle: 0, stroke: new ol.style.Stroke({color: '#fff', width: 1})})})}),
      new ol.layer.Vector({source: sources.tag})]});
  map.on('singleclick', event => { if (touching()) return; if (!searchOpen()) mapElement.focus({preventScroll: true}); inspect(event.coordinate); });
  map.on('moveend', () => { scheduleGrid(); rings(); positionProfile(); updateLegend(); placeTag(); });
  map.on('postrender', positionProfile); map.on('change:size', resized);
  await loadStatic();
  grid(); rings(); showSun(); setInterval(showSun, 60000);
  setInterval(() => { nightCache = null; veilSource.changed(); lineSource.changed(); }, 60000);
  startLive(site, toBase, baseLatLon, showLive);
  startSearch({sources, metres, latlon: (x, y) => inverse(site, x, y), view: () => view, initialResolution: () => initialResolution, inspect, mapElement});
  if (document.activeElement === document.body) mapElement.focus({preventScroll: true});
  requestAnimationFrame(controls);
}

// ------------------------------------------------------------------ touch mode ----
// the middle button makes the touchpad a touch screen for the map, as on the X230

const TOUCH_GAIN = 0.15;
let touchRest = 0;
export function touching() { return document.pointerLockElement === mapElement; }
mapElement.addEventListener('mousedown', e => { if (e.button === 1) e.preventDefault(); });
mapElement.addEventListener('auxclick', e => { if (e.button !== 1 || !view) return; e.preventDefault(); if (touching()) document.exitPointerLock(); else mapElement.requestPointerLock(); });
document.addEventListener('mousemove', e => {
  if (!touching() || !view) return;
  if (!touchRest) view.beginInteraction();
  clearTimeout(touchRest); touchRest = setTimeout(() => { touchRest = 0; view.endInteraction(0); }, 150);
  const [x, y] = view.getCenter(), r = view.getResolution() * TOUCH_GAIN;
  view.setCenter([x - e.movementX * r, y + e.movementY * r]);
});

// ------------------------------------------------------------------ the path popup ----
// map_panel.path_profile: the ground along the path from the summit, the straight ray to a
// station at each height, Fresnel zone 60% clear (OK), line of sight (LOS) or blocked (BLK)

function closeProfile() { if (profilePoint) showWeather(null); profileSerial++; profilePoint = null; profileElement.hidden = true; sources.selected.clear(); }
document.querySelector('#profile-close').addEventListener('click', () => { closeProfile(); mapElement.focus({preventScroll: true}); });
function positionProfile() {
  if (!profilePoint || profileElement.hidden || !map) return;
  const p = map.getPixelFromCoordinate(profilePoint);
  if (!p) return;
  const rect = mapElement.getBoundingClientRect(), root = stage.getBoundingClientRect(), w = profileElement.offsetWidth, h = profileElement.offsetHeight;
  profileElement.style.left = (rect.left - root.left + Math.min(Math.max(0, p[0] + 12), Math.max(0, rect.width - w))) + 'px';
  profileElement.style.top = (rect.top - root.top + Math.min(Math.max(0, p[1] + 12), Math.max(0, rect.height - h))) + 'px';
}
function table(status) {
  const element = document.querySelector('#reach-table'), header = document.createElement('tr');
  element.replaceChildren();
  for (const title of ['MHz', ...HEIGHTS.map(h => h + 'm')]) { const th = document.createElement('th'); th.textContent = title; header.append(th); }
  element.append(header);
  FREQUENCIES.forEach((frequency, i) => {
    const row = document.createElement('tr'), label = document.createElement('td');
    label.textContent = frequency.toFixed(3); row.append(label);
    HEIGHTS.forEach((_, j) => { const cell = document.createElement('td'), value = status ? status[i][j] : '—'; cell.textContent = value; cell.className = value; row.append(cell); });
    element.append(row);
  });
}
async function profile(lat, lon) {
  const [distance, bearing] = polar(site.lat, site.lon, lat, lon);
  // the finest tiles a short path needs; a long one coarser, as the map's own ground
  const zoom = distance <= 60 ? 12 : distance <= 120 ? 11 : distance <= 250 ? 10 : 9, step = Math.max(.03, distance / 4000);
  const n = Math.max(3, Math.floor(distance / step) + 1), d = Float64Array.from({length: n}, (_, i) => i * Math.max(distance, .001) / (n - 1));
  const lats = new Float64Array(n), lons = new Float64Array(n);
  d.forEach((km, i) => { [lats[i], lons[i]] = destination(site.lat, site.lon, bearing, km); });
  const {heights} = await ask({type: 'heights', lats, lons, zoom});
  const ground = Float64Array.from(heights);
  ground[0] = info.our_asl_m - info.our_agl_m;                   // the summit, as the viewshed has it
  const our = info.our_agl_m, end = d[n - 1] * 1000;
  const effective = ground.map((g, i) => g + (d[i] * 1000) * (end - d[i] * 1000) / (2 * K_FACTOR * R_EARTH * 1000));
  const rayTo = agl => d.map((km, i) => ground[0] + our + (ground[n - 1] + agl - ground[0] - our) * km / d[n - 1]);
  const status = FREQUENCIES.map(f => HEIGHTS.map(agl => {
    const ray = rayTo(agl);
    let ok = true, los = true;
    for (let i = 1; i < n - 1; i++) {
      const clear = ray[i] - effective[i], radius = Math.sqrt((299.792458 / f) * (d[i] * 1000) * (end - d[i] * 1000) / end);
      if (clear < .6 * radius) ok = false;
      if (clear < 0) { los = false; break; }
    }
    return ok ? 'OK' : los ? 'LOS' : 'BLK';
  }));
  const ray = rayTo(HEIGHTS[0]);
  let clearance = Infinity;
  for (let i = 1; i < n - 1; i++) clearance = Math.min(clearance, ray[i] - effective[i]);
  return {distance, bearing, d, ground: effective, ray, status, clearance: Number.isFinite(clearance) ? clearance : 0,
          fspl: FREQUENCIES.map(f => 32.45 + 20 * Math.log10(f) + 20 * Math.log10(Math.max(distance, .001))), dem_m: 156543.03 * Math.cos(site.lat * Math.PI / 180) / 2 ** zoom};
}
export async function inspect(at) {
  if (!extent || at[0] < extent[0] || at[0] > extent[2] || at[1] < extent[1] || at[1] > extent[3]) return;
  const serial = ++profileSerial, [lat, lon] = inverse(site, ...at);
  profilePoint = at.slice(); profileElement.hidden = false; table(null); showWeather([lat, lon]);
  for (const id of ['loss', 'nearby', 'conditions']) document.querySelector('#' + id).textContent = '';
  document.querySelector('#position').textContent = (Math.hypot(...at) / 1000).toFixed(1) + ' km  ' + Math.round((Math.atan2(at[0], at[1]) * 180 / Math.PI + 360) % 360) + '°';
  const chart = document.querySelector('#terrain'), c = chart.getContext('2d');
  c.clearRect(0, 0, chart.width, chart.height);
  sources.selected.clear(); sources.selected.addFeatures([new ol.Feature(new ol.geom.LineString([[0, 0], at])), new ol.Feature(new ol.geom.Point(at))]);
  positionProfile();
  const near = nearby([W / 2 + at[0] / metres, H / 2 - at[1] / metres], info.km_per_px);
  try {
    const p = await profile(lat, lon);
    if (serial !== profileSerial) return;
    table(p.status);
    document.querySelector('#position').textContent = p.distance.toFixed(1) + ' km  ' + Math.round(p.bearing) + '°  ' + lat.toFixed(2) + ',' + lon.toFixed(2);
    document.querySelector('#loss').textContent = 'CLR10m ' + Math.round(p.clearance) + 'm  FSL ' + Math.round(p.fspl[0]) + '–' + Math.round(p.fspl.at(-1)) + ' dB';
    document.querySelector('#nearby').textContent = '20km: ' + near.calls.length + ' stations  ' + near.paths + ' paths' + (near.calls.length ? ' · ' + near.calls.join(' ') : '');
    document.querySelector('#conditions').textContent = 'DE ' + info.our_agl_m + 'm  DX AGL  k=4/3  F1 60%  DEM ' + Math.round(p.dem_m) + 'm';
    const lo = Math.min(...p.ground, ...p.ray), hi = Math.max(...p.ground, ...p.ray);
    const xy = (v, i) => [p.d[i] / Math.max(.001, p.d.at(-1)) * chart.width, chart.height - 3 - (v - lo) / Math.max(1, hi - lo) * (chart.height - 6)];
    c.beginPath(); c.moveTo(0, chart.height); p.ground.forEach((v, i) => c.lineTo(...xy(v, i))); c.lineTo(chart.width, chart.height); c.fillStyle = '#50412d'; c.fill();
    c.beginPath(); p.ray.forEach((v, i) => i ? c.lineTo(...xy(v, i)) : c.moveTo(...xy(v, i))); c.strokeStyle = '#fff'; c.stroke();
    positionProfile();
  } catch (e) {
    if (serial === profileSerial) document.querySelector('#conditions').textContent = '地形数据暂不可用';
  }
}

// ------------------------------------------------------------------ keys, pad ----

function keyName(e) { return e.key === ' ' ? 'Space' : e.key; }
function zoomStep(step) { view.animate({resolution: view.getResolution() * Math.exp(-step * 2 * .18), duration: 150}); }
function zooming(name) { return shiftDown && keys.has(name); }
stage.addEventListener('keydown', e => {
  if (e.defaultPrevented || e.isComposing || searchOpen() || e.target.closest('input,textarea,select,[contenteditable]') || e.ctrlKey || e.altKey || e.metaKey) return;
  shiftDown = e.shiftKey;
  const name = keyName(e);
  if (mapKeys.has(name) && view) {
    e.preventDefault();
    if (!e.repeat && !e.shiftKey && (zoomIn.has(name) || zoomOut.has(name))) zoomStep(zoomIn.has(name) ? 1 : -1);
    keys.add(name);
    return;
  }
  if (name === 'Escape' && profilePoint) { e.preventDefault(); closeProfile(); return; }
  if (name === 'Enter' && view && mapElement.contains(document.activeElement)) { e.preventDefault(); inspect(view.getCenter()); }
});
stage.addEventListener('keyup', e => { shiftDown = e.shiftKey; const name = keyName(e); keys.delete(name); if (zoomIn.has(name) || zoomOut.has(name)) for (const k of [...zoomIn, ...zoomOut]) keys.delete(k); });
function padState() {
  let x = 0, y = 0, z = 0, enter = false, escape = false;
  if (navigator.getGamepads) for (const pad of navigator.getGamepads()) {
    if (!pad || !pad.connected) continue;
    const b = i => !!(pad.buttons[i] && pad.buttons[i].pressed), axis = i => { const v = pad.axes[i] || 0; return Math.abs(v) < .2 ? 0 : Math.sign(v) * (Math.abs(v) - .2) / .8; };
    x += axis(0) + (b(15) ? 1 : 0) - (b(14) ? 1 : 0); y += axis(1) + (b(13) ? 1 : 0) - (b(12) ? 1 : 0); z += (b(5) || b(7) ? 1 : 0) - (b(4) || b(6) ? 1 : 0); enter = enter || b(0); escape = escape || b(1);
  }
  return {x: Math.max(-1, Math.min(1, x)), y: Math.max(-1, Math.min(1, y)), z: Math.max(-1, Math.min(1, z)), enter, escape};
}
function controls(now) {
  const dt = Math.min(.05, (now - lastTick) / 1000);
  lastTick = now;
  if (view && !searchOpen() && !document.hidden && document.hasFocus()) {
    let pad = {x: 0, y: 0, z: 0, enter: false, escape: false};
    try { pad = padState(); } catch (e) { /* no pads */ }
    const x = (keys.has('ArrowRight') ? 1 : 0) - (keys.has('ArrowLeft') ? 1 : 0) + pad.x, y = (keys.has('ArrowDown') ? 1 : 0) - (keys.has('ArrowUp') ? 1 : 0) + pad.y;
    const z = ([...zoomIn].some(zooming) ? 1 : 0) - ([...zoomOut].some(zooming) ? 1 : 0) + pad.z, moving = !!(x || y || z);
    if (moving && !heldInteraction) { view.beginInteraction(); heldInteraction = true; }
    if (x || y) { const r = view.getResolution(); view.adjustCenter([x * 700 * dt * r, -y * 700 * dt * r]); }
    if (z) view.adjustResolution(Math.exp(-z * 3 * .18 * dt), view.getCenter());
    if (!moving && heldInteraction) { heldInteraction = false; view.endInteraction(0); }
    if (pad.enter && !lastPadEnter) inspect(view.getCenter());
    if (pad.escape && !lastPadEscape) closeProfile();
    lastPadEnter = pad.enter; lastPadEscape = pad.escape;
  }
  requestAnimationFrame(controls);
}
export function release() { keys.clear(); shiftDown = false; if (heldInteraction && view) { heldInteraction = false; view.endInteraction(0); } }
window.addEventListener('blur', release);
document.addEventListener('visibilitychange', () => { if (document.hidden) release(); });
