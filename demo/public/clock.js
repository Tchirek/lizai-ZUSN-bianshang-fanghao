// HamClock's panes around the map, drawn as the station's patched HamClock draws them (call sign,
// DE local clock, GOES X-ray, the ADS-B scope, DE weather, space weather, DX cluster), from open
// sources: NOAA SWPC, Open-Meteo, HamQTH's DX cluster, and the open ADS-B exchanges (/api/adsb).
import {C, SCALE, Tft, rgb565} from './tft.js';

const BOXES = {
  call: {x: 0, y: 0, w: 230, h: 52}, clock: {x: 0, y: 65, w: 230, h: 49}, aux: {x: 0, y: 113, w: 204, h: 32},
  xray: {x: 235, y: 0, w: 160, h: 148}, adsb: {x: 405, y: 0, w: 160, h: 148}, wx: {x: 575, y: 0, w: 160, h: 148},
  stats: {x: 738, y: 0, w: 62, h: 148}, cluster: {x: 0, y: 148, w: 139, h: 332},
};
const BORDER = C.GRAY, GRID = rgb565(55, 55, 55);
// HamClock's band colours (activity.BANDS), by lower edge in kHz
export const BANDS = [[1800, '160', [131, 0, 0]], [3500, '80', [131, 129, 0]], [5300, '60', [230, 24, 74]],
  [7000, '40', [246, 129, 49]], [10100, '30', [205, 178, 16]], [14000, '20', [255, 250, 0]], [18068, '17', [90, 182, 8]],
  [21000, '15', [65, 255, 172]], [24890, '12', [0, 129, 255]], [28000, '10', [255, 190, 213]], [50000, '6', [139, 161, 8]],
  [144000, '2', [98, 101, 98]], [430000, '70cm', [180, 120, 255]]];
const BAND_TOP = {160: 2000, 80: 4000, 60: 5450, 40: 7300, 30: 10150, 20: 14350, 17: 18168, 15: 21450, 12: 24990, 10: 29700, 6: 54000, 2: 148000, '70cm': 450000};
export function band(khz) {
  let hit = null;
  for (const b of BANDS) if (khz >= b[0]) hit = b;
  return hit && khz <= BAND_TOP[hit[1]] ? {name: hit[1], rgb: hit[2]} : null;
}

let tft = null, site = null, started = Date.now();
const state = {xray: null, stats: null, wx: null, dx: null, dxWx: null, spots: [], adsb: null, trails: new Map(), today: new Set(), range: 0};

function frame(box, bottom = false) {
  const r = box.x + box.w - 1, b = box.y + box.h - 1;
  tft.fillRect(box.x, box.y, box.w, box.h, C.BLACK);
  tft.line(box.x, box.y, box.x, b, BORDER); tft.line(box.x, box.y, r, box.y, BORDER); tft.line(r, box.y, r, b, BORDER);
  if (bottom) tft.line(box.x, b, r, b, BORDER);
}
function centred(box, text, y, colour) { tft.setColour(colour); tft.setCursor(box.x + Math.floor((box.w - tft.textWidth(text)) / 2), y); tft.print(text); }
// maxStringW: text shortened from its end until it fits
function fitted(text, room) { while (text && tft.textWidth(text) >= room) text = text.slice(0, -1); return text; }
function message(box, colour, text) { frame(box); tft.setFont('fast'); centred(box, text, box.y + box.h / 2 - 4, colour); }
async function json(url) { const r = await fetch(url); if (!r.ok) throw Error(url + ' ' + r.status); return r.json(); }
function every(seconds, task) { const run = () => task().catch(e => console.warn(e.message)); run(); setInterval(run, seconds * 1000); }

// ------------------------------------------------------------- call, clock ----

// the call box: the summit's name, in the largest of HamClock's fonts it fits
function drawCall() {
  const box = BOXES.call, text = site.name;
  tft.fillRect(box.x, box.y, box.w, box.h, rgb565(0, 0, 96));
  for (const [font, base] of [['large', box.h / 2 + 20], ['smallBold', box.h / 2 + 10], ['small', box.h / 2 + 10], ['fast', box.h / 2 - 4]]) {
    tft.setFont(font);
    if (tft.textWidth(text) < box.w - 4 || font === 'fast') { centred(box, text, base, C.WHITE); break; }
  }
}
function local(now) {
  const parts = Object.fromEntries(new Intl.DateTimeFormat('en-US', {timeZone: site.tz, hourCycle: 'h23', weekday: 'short',
    month: 'short', day: 'numeric', year: 'numeric', hour: '2-digit', minute: '2-digit', second: '2-digit'}).formatToParts(now).map(p => [p.type, p.value]));
  return parts;
}
function drawClock() {
  const now = new Date(), t = local(now), box = BOXES.clock, hms = box.h - 6;
  tft.fillRect(0, 54, 230, 10, C.BLACK);
  tft.setFont('fast'); tft.setColour(C.GRAY);
  const up = Math.floor((Date.now() - started) / 1000), m = Math.floor(up / 60) % 60, h = Math.floor(up / 3600);
  tft.setCursor(0, 55); tft.print('Up ' + (up < 60 ? `    ${String(up).padStart(2)}s` : up < 3600 ? `${String(m).padStart(2)}m ${String(up % 60).padStart(2)}s` : `${String(h).padStart(2)}h ${String(m).padStart(2)}m`));
  tft.setCursor(86, 55); tft.print('DE ' + gridSquare(site.lat, site.lon));
  tft.setCursor(188, 55); tft.print('V4.22');
  // H:M right-justified in the left two thirds, seconds above the line to its right
  tft.fillRect(box.x, box.y, box.w - 14, hms + 4, C.BLACK);
  tft.setFont('large'); tft.setColour(C.WHITE); tft.setCursor(box.x + 2 * box.w / 3 - 135, box.y + hms); tft.print(`${t.hour}:${t.minute}`);
  tft.setFont('smallBold'); tft.setCursor(box.x + Math.floor(2 * box.w / 3), box.y + hms - 19); tft.print(t.second);
  // LOC down the button at the top right: the main clock shows DE's own time
  const offset = new Date(now.toLocaleString('en-US', {timeZone: site.tz})) - new Date(now.toLocaleString('en-US', {timeZone: 'UTC'}));
  const label = offset ? 'LOC' : 'UTC', bx = box.x + box.w - 14, gap = Math.floor((hms - 1 - 24) / 4);
  tft.fillRect(bx, box.y, 14, hms - 1, C.WHITE); tft.setFont('fast'); tft.setColour(C.BLACK);
  [...label].forEach((ch, i) => { tft.setCursor(bx + 4, box.y + (i + 1) * gap + i * 8 + 1); tft.print(ch); });
  // the stopwatch and the lock, as icons
  tft.circleRaw(2 * 168, 2 * 106, 16, C.GRAY, true); tft.fillRectRaw(2 * 166, 2 * 95, 8, 6, C.GRAY);
  tft.lineRaw(336, 212, 336, 200, 2, C.BLACK); tft.lineRaw(336, 212, 344, 216, 2, C.BLACK);
  tft.fillRect(217, 125, 11, 9, C.WHITE); tft.circleRaw(2 * 222 + 1, 2 * 124, 6, C.WHITE); tft.fillRect(221, 128, 3, 3, C.BLACK);
  // the date below
  const aux = BOXES.aux, date = `${t.weekday}  ${t.month} ${String(t.day).padStart(2)}, ${t.year}`;
  tft.fillRect(aux.x, aux.y, aux.w, aux.h, C.BLACK); tft.setFont('small');
  tft.setColour(C.WHITE); tft.setCursor(Math.max(0, aux.x + Math.floor((aux.w - tft.textWidth(date)) / 2)), aux.y + aux.h - 8); tft.print(date);
}
export function gridSquare(lat, lon) {
  const x = lon + 180, y = lat + 90;
  return String.fromCharCode(65 + Math.floor(x / 20), 65 + Math.floor(y / 10)) + Math.floor(x % 20 / 2) + Math.floor(y % 10)
    + String.fromCharCode(97 + Math.floor(x % 2 * 12), 97 + Math.floor(y % 1 * 24));
}

// ------------------------------------------------------------- plotXY: X-ray ----

function ticks(min, max, n) {
  let scale = Math.abs(max - min);
  const delta = scale / n;
  for (const f of [1, 2, 5]) { const s = 10 ** Math.ceil(Math.log10(delta / f)) * f; if (s < scale) scale = s; }
  const out = [], lo = Math.floor(min / scale);
  for (let i = 0, v; (v = scale * (lo + i)) < max + scale; i++) out.push(v);
  return out;
}
export function xrayLevel(flux) {
  if (!(flux >= 1e-8)) return 'A0.0';
  const power = Math.min(-4, Math.floor(Math.log10(flux)));
  return 'ABCMX'[8 + power] + (flux * 10 ** -power).toFixed(1);
}
function drawXray() {
  const box = BOXES.xray, data = state.xray, red = rgb565(255, 50, 50), blue = rgb565(50, 50, 255);
  if (!data) return message(box, red, 'X-Ray loading');
  const TGAP = 10, BGAP = 15, FW = 6, FH = 8, xt = ticks(-24, 0, 10), yt = ticks(-9, -2, 10), minx = xt[0], maxx = xt.at(-1), dx = maxx - minx;
  tft.fillRect(box.x, box.y, box.w, box.h, C.BLACK);
  tft.setFont('fast'); tft.setColour(red);
  const tx = box.x + 2 * FW + 2 + 5, stepH = Math.floor((box.h - BGAP - TGAP) / yt.length), LGAP = tx - box.x;
  const yOf = v => Math.round(box.y + TGAP + (box.h - BGAP - TGAP) * (1 - (v + 9) / 7));
  for (const v of yt) {
    const ty = yOf(v);
    tft.line(tx - 2, ty, tx, ty, red); tft.line(tx, ty, box.x + box.w - 1, ty, GRID);
    const letter = {[-8]: 'A', [-7]: 'B', [-6]: 'C', [-5]: 'M', [-4]: 'X'}[v];
    if (letter) { tft.setCursor(tx - FW - 1, ty - stepH + Math.floor((stepH - FH) / 2) - 1); tft.print(letter); }
    if (v === -9 || v === -2) { tft.setCursor(tx - 2 - 2 * FW - 1, ty - FH / 2); tft.print(v); }
  }
  const title = `GOES ${data.satellite} X-Ray`;
  tft.setCursor(box.x + LGAP + Math.floor((box.w - LGAP - tft.textWidth(title)) / 2), box.y + Math.floor((TGAP - FH) / 2) + 1); tft.print(title);
  const ty = box.y + box.h - FH - 2;
  tft.setCursor(box.x + LGAP, ty); tft.print(minx);
  tft.setCursor(box.x + box.w - tft.textWidth(String(maxx)) - 1, ty); tft.print(maxx);
  for (const v of xt) { const x = Math.round(box.x + LGAP + (box.w - LGAP - 1) * (v - minx) / dx); tft.line(x, box.y + box.h - BGAP, x, box.y + box.h - BGAP + 2, red); tft.line(x, box.y + box.h - BGAP, x, box.y + TGAP, GRID); }
  tft.setCursor(box.x + LGAP + Math.floor((box.w - LGAP - tft.textWidth('Hours')) / 2), ty); tft.print('Hours');
  for (const [series, colour] of [[data.long, red], [data.short, blue]]) {
    let prev = null;
    for (const [h, v] of series) {
      const p = [Math.floor(box.x + LGAP + 1 + (box.w - LGAP - 3) * (h - minx) / dx), Math.floor(box.y + TGAP + (box.h - BGAP - TGAP) * (1 - (Math.max(-9, Math.min(-2, v)) + 9) / 7))];
      if (prev && (prev[0] !== p[0] || prev[1] !== p[1])) tft.line(...prev, ...p, colour);
      prev = p;
    }
  }
  tft.drawRect(box.x + LGAP, box.y + TGAP, box.w - LGAP, box.h - BGAP - TGAP + 1, BORDER);
  const label = xrayLevel(data.flux);
  tft.setFont('large'); tft.setColour(C.BRGRAY);
  const {w, h} = tft.textBounds(label);
  tft.setCursor(box.x + LGAP + Math.floor((box.w - LGAP - w) / 2), box.y + TGAP + Math.floor((box.h - TGAP - BGAP) / 25) + h); tft.print(label);
  for (const edge of [[box.x, box.y, box.x + box.w - 1, box.y], [box.x, box.y, box.x, box.y + box.h - 1], [box.x + box.w - 1, box.y, box.x + box.w - 1, box.y + box.h - 1]]) tft.line(...edge, BORDER);
}
async function fetchSpace() {
  const [xrays, kp, dst, wind] = await Promise.allSettled([
    json('https://services.swpc.noaa.gov/json/goes/primary/xrays-1-day.json'),
    json('https://services.swpc.noaa.gov/products/noaa-planetary-k-index.json'),
    json('https://services.swpc.noaa.gov/products/kyoto-dst.json'),
    json('https://services.swpc.noaa.gov/json/rtsw/rtsw_wind_1m.json')]);
  if (xrays.status === 'fulfilled' && xrays.value.length) {
    const now = Date.now(), long = [], short = [];
    for (const r of xrays.value) if (r.flux > 0) (r.energy === '0.1-0.8nm' ? long : short).push([(Date.parse(r.time_tag) - now) / 3.6e6, Math.log10(r.flux)]);
    const last = xrays.value.filter(r => r.energy === '0.1-0.8nm').at(-1);
    state.xray = {long, short, flux: last?.flux, satellite: last?.satellite ?? xrays.value[0].satellite};
  }
  const value = (r, f) => r.status === 'fulfilled' ? f(r.value) : null;
  state.stats = {
    wind: value(wind, w => { const v = w.filter(r => r.active && r.proton_density != null && r.proton_speed != null).sort((a, b) => a.time_tag < b.time_tag ? -1 : 1).at(-1); return v ? v.proton_density * v.proton_speed * 1e-3 : null; }),
    dst: value(dst, d => d.at(-1)?.dst), kp: value(kp, k => k.at(-1)?.Kp), xray: state.xray?.flux,
  };
  drawXray(); drawStats();
}

// ------------------------------------------------------------- space weather ----

function drawStats() {
  const box = BOXES.stats, s = state.stats || {}, h = Math.floor(box.h / 4), vo = Math.floor(24 * h / 37), to = Math.floor(28 * h / 37);
  const fields = [['Sol Wind', s.wind, v => v.toFixed(1), C.MAGENTA], ['DST', s.dst, v => v.toFixed(0), rgb565(184, 134, 11)],
                  ['Kp', s.kp, v => v.toFixed(1), C.YELLOW], ['X-Ray', s.xray, xrayLevel, rgb565(255, 134, 0)]];
  fields.forEach(([title, v, format, colour], i) => {
    const y = Math.floor(i * box.h / 4), text = v == null ? 'Err' : format(v);
    tft.fillRect(box.x + 1, y, box.w - 2, h, C.BLACK);
    tft.setFont('small'); tft.setColour(v == null ? C.RED : colour); tft.setCursor(box.x + Math.floor((box.w - tft.textWidth(text)) / 2), y + vo); tft.print(text);
    tft.setFont('fast'); tft.setColour(C.WHITE); tft.setCursor(box.x + Math.floor((box.w - tft.textWidth(title)) / 2), y + to); tft.print(title);
  });
  tft.drawRect(box.x, box.y, box.w, box.h, BORDER);
}

// ------------------------------------------------------------- weather ----

// Open-Meteo's WMO weather codes in the words of HamClock's weather service
const SKY = {0: 'Clear sky', 1: 'Few clouds', 2: 'Scattered clouds', 3: 'Overcast clouds', 45: 'Fog', 48: 'Rime fog',
  51: 'Light drizzle', 53: 'Drizzle', 55: 'Heavy drizzle', 56: 'Freezing drizzle', 57: 'Freezing drizzle',
  61: 'Light rain', 63: 'Moderate rain', 65: 'Heavy rain', 66: 'Freezing rain', 67: 'Freezing rain',
  71: 'Light snow', 73: 'Snow', 75: 'Heavy snow', 77: 'Snow grains', 80: 'Rain showers', 81: 'Rain showers',
  82: 'Heavy showers', 85: 'Snow showers', 86: 'Snow showers', 95: 'Thunderstorm', 96: 'Thunderstorm', 99: 'Thunderstorm'};
const COMPASS = ['N', 'NE', 'E', 'SE', 'S', 'SW', 'W', 'NW'];
// the weather pane shows DE's weather in orange, or green the place a map popup is open on
// (hamclock/dxwx.py drives the native pane the same way)
function drawWx() {
  const box = BOXES.wx, w = state.dx ? state.dxWx : state.wx, colour = state.dx ? C.GREEN : C.DE;
  if (!w) return message(box, colour, 'Weather loading');
  frame(box);
  const dy0 = Math.floor(box.h / 3), ddy = Math.floor(box.h / 5);
  tft.setColour(colour); tft.setFont('large');
  const temp = fitted(`${Math.round(w.temperature_2m)} C`, box.w);
  tft.setCursor(box.x + Math.floor((box.w - tft.textWidth(temp)) / 2), box.y + dy0); tft.print(temp);
  const unit = tft.textBounds(temp.slice(-2));
  tft.setFont('smallBold'); tft.x -= unit.w * SCALE; tft.y -= Math.floor(2 * unit.h / 3) * SCALE; tft.print('o');
  tft.setFont('small');
  const lines = [`${Math.round(w.relative_humidity_2m)}% ${Math.round(w.pressure_msl)} hPa`,
                 `${COMPASS[Math.round(w.wind_direction_10m / 45) % 8]} @ ${Math.round(w.wind_speed_10m)} kph`, SKY[w.weather_code] || ''];
  lines.forEach((text, i) => { text = fitted(text, box.w); tft.setCursor(box.x + Math.floor((box.w - tft.textWidth(text)) / 2), box.y + dy0 + (i + 1) * ddy); tft.print(text); });
}
function weather(lat, lon) {
  const q = `latitude=${lat.toFixed(3)}&longitude=${lon.toFixed(3)}&current=temperature_2m,relative_humidity_2m,pressure_msl,wind_speed_10m,wind_direction_10m,weather_code&wind_speed_unit=kmh`;
  return json('https://api.open-meteo.com/v1/forecast?' + q).then(r => r.current);
}
async function fetchWx() { state.wx = await weather(site.lat, site.lon); drawWx(); }
export function showWeather(latlon) {
  state.dx = latlon; state.dxWx = null; drawWx(); tft.show();
  if (latlon) weather(...latlon).then(w => { if (state.dx === latlon) { state.dxWx = w; drawWx(); tft.show(); } }).catch(() => {});
}

// ------------------------------------------------------------- DX cluster ----

function age(seconds) {
  return seconds < 60 ? `${Math.max(0, seconds)}s` : seconds < 3600 ? `${Math.floor(seconds / 60)}m` : `${Math.floor(seconds / 3600)}h`;
}
function drawCluster() {
  const box = BOXES.cluster, green = C.GREEN, rows = Math.floor((box.h - 47) / 14);
  frame(box, true);
  tft.drawRect(box.x + 5, box.y + 5, 22, 11, green); tft.setFont('fast'); tft.setColour(green); tft.setCursor(box.x + 6, box.y + 7); tft.print('CLR');
  tft.setFont('small'); centred(box, 'Cluster', box.y + 27, green);
  tft.setFont('fast'); centred(box, 'www.hamqth.com', box.y + 32, green);
  const shown = state.spots.slice(-rows), now = Date.now() / 1000;
  shown.forEach((s, row) => {
    const x = box.x + 1, y = box.y + 47 + row * 14, b = band(s.khz), bg = b ? rgb565(...b.rgb) : C.BLACK;
    const light = b && 0.299 * b.rgb[0] + 0.587 * b.rgb[1] + 0.114 * b.rgb[2] > 100;
    tft.fillRect(x, y - 3, box.w - 2, 12, C.BLACK); tft.fillRect(x, y - 3, 50, 12, bg);
    tft.setColour(light ? C.BLACK : C.WHITE); tft.setCursor(x, y); tft.print((s.khz < 1e6 ? s.khz.toFixed(1) : s.khz.toFixed(0)).padStart(8));
    tft.setColour(C.WHITE); tft.print(` ${s.call.slice(0, 9).padEnd(9)} ` + age(Math.round(now - s.t)).padStart(3));
  });
}
async function fetchCluster() {
  const text = await (await fetch('https://www.hamqth.com/dxc_csv.php?limit=60')).text(), spots = [];
  for (const line of text.trim().split('\n')) {
    const [call, khz, , , when] = line.split('^'), m = /^(\d\d)(\d\d) (\d{4}-\d\d-\d\d)$/.exec(when || '');
    if (call && Number(khz) > 0 && m) spots.push({call, khz: Number(khz), t: Date.parse(`${m[3]}T${m[1]}:${m[2]}:00Z`) / 1000});
  }
  state.spots = spots.sort((a, b) => a.t - b.t);
  drawCluster();
}

// ------------------------------------------------------------- the ADS-B scope ----
// tools/hamclock-adsb.cpp, line for line where it can be

const RANGES = [10, 20, 30, 50], MI_KM = 1.609344, EARTH_KM = 6371;
const STYLES = {water: [rgb565(20, 45, 110), 1, 100], river: [rgb565(30, 70, 160), 1, 100], rail: [rgb565(70, 70, 70), 1, 50],
  trunk: [rgb565(75, 55, 20), 1, 50], motorway: [rgb565(120, 85, 25), 1, 100], runway: [rgb565(175, 175, 175), 3, 100]};
const ALT_BANDS = [[2000, rgb565(255, 64, 64), '0'], [10000, rgb565(246, 129, 49), '2'], [20000, rgb565(255, 255, 0), '10'],
  [30000, rgb565(0, 255, 0), '20'], [99999, rgb565(0, 255, 255), '30k']];
const RING = rgb565(41, 40, 98), TEXT = rgb565(110, 110, 110), CITY = rgb565(70, 150, 70), TOWN = rgb565(45, 90, 45), TITLE = C.DE;
let scopeMap = null;

// The scope's base map from OpenStreetMap, as OpenFreeMap serves it in vector tiles (OpenMapTiles
// layers): roads, rail, rivers, water and towns at zoom 9 over the widest range, the civil
// airfields' runways at zoom 10; air bases (aerodrome class military) are left out.
const OSM_TILES = 'https://tiles.openfreemap.org/planet', OSM_KM = 85, EXTENT = 4096;
const ROADS = {motorway: 'motorway', trunk: 'trunk', rail: 'rail'};
function tileRange(z) {
  const n = 2 ** z, dlat = OSM_KM / 111.2, dlon = OSM_KM / (111.2 * Math.cos(site.lat * Math.PI / 180));
  const tx = lon => Math.floor((lon + 180) / 360 * n), ty = lat => { const r = lat * Math.PI / 180; return Math.floor((1 - Math.log(Math.tan(r) + 1 / Math.cos(r)) / Math.PI) / 2 * n); };
  const out = [];
  for (let x = tx(site.lon - dlon); x <= tx(site.lon + dlon); x++) for (let y = ty(site.lat + dlat); y <= ty(site.lat - dlat); y++) out.push([z, x, y]);
  return out;
}
async function osmTile(url, [z, x, y]) {
  const r = await fetch(url.replace('{z}', z).replace('{x}', x).replace('{y}', y));
  if (!r.ok) throw Error('tile ' + r.status);
  const n = 2 ** z, latlon = ([px, py]) => {
    const yy = Math.PI * (1 - 2 * (y + py / EXTENT) / n);
    return [+(Math.atan(Math.sinh(yy)) * 180 / Math.PI).toFixed(5), +((x + px / EXTENT) / n * 360 - 180).toFixed(5)];
  };
  return {latlon, features: new ol.format.MVT({featureClass: ol.Feature}).readFeatures(await r.arrayBuffer())};
}
// a line's runs as lat, lon; a polygon's outline less where the tile cut it
function runs(geometry, latlon, outline) {
  const lines = {LineString: [geometry.getCoordinates()], MultiLineString: geometry.getCoordinates(),
                 Polygon: geometry.getCoordinates(), MultiPolygon: geometry.getCoordinates().flat()}[geometry.getType()] || [];
  const edge = (a, b) => [0, 1].some(i => (a[i] <= 0 && b[i] <= 0) || (a[i] >= EXTENT && b[i] >= EXTENT));
  const out = [];
  for (const line of lines) {
    let run = [];
    for (let i = 0; i < line.length; i++) {
      if (outline && i && edge(line[i - 1], line[i])) { if (run.length > 1) out.push(run); run = []; }
      run.push(latlon(line[i]));
    }
    if (run.length > 1) out.push(run);
  }
  return out;
}
async function osmScope() {
  const url = (await json(OSM_TILES)).tiles[0], lines = [], places = [], airports = [], fields = new Set();
  for (const {latlon, features} of await Promise.all(tileRange(9).map(t => osmTile(url, t)))) {
    for (const f of features) {
      const layer = f.get('layer'), kind = f.get('class'), g = f.getGeometry();
      if (layer === 'transportation' && ROADS[kind]) lines.push(...runs(g, latlon).map(p => ({k: ROADS[kind], p})));
      else if (layer === 'waterway' && kind === 'river') lines.push(...runs(g, latlon).map(p => ({k: 'river', p})));
      else if (layer === 'water') lines.push(...runs(g, latlon, true).map(p => ({k: 'water', p})));
      else if (layer === 'place' && (kind === 'city' || kind === 'town')) {
        const [lat, lon] = latlon(g.getCoordinates());
        places.push({name: f.get('name_en') || f.get('name:latin') || f.get('name') || '', kind: kind === 'city' ? 'city' : 'town', lat, lon});
      } else if (layer === 'aerodrome_label' && kind !== 'military' && !/air ?base|air force|military/i.test(f.get('name') || '')) {
        const [lat, lon] = latlon(g.getCoordinates()), code = f.get('icao') || f.get('iata') || '';
        airports.push({name: /^[A-Z0-9]{3,5}$/.test(code) ? code : '', lat, lon});
      }
    }
  }
  // runways: zoom 10's aeroway layer, in the tiles of the civil airfields
  const n = 2 ** 10;
  for (const a of airports) { const r = a.lat * Math.PI / 180; fields.add([10, Math.floor((a.lon + 180) / 360 * n), Math.floor((1 - Math.log(Math.tan(r) + 1 / Math.cos(r)) / Math.PI) / 2 * n)].join()); }
  const near = (p, a) => Math.abs(p[0] - a.lat) < .04 && Math.abs(p[1] - a.lon) < .05;
  for (const {latlon, features} of await Promise.all([...fields].map(k => osmTile(url, k.split(',').map(Number)).catch(() => ({features: []}))))) {
    for (const f of features) if (f.get('layer') === 'aeroway' && f.get('class') === 'runway')
      for (const p of runs(f.getGeometry(), latlon)) if (airports.some(a => near(p[0], a))) lines.push({k: 'runway', p});
  }
  return {lines, places, airports};
}

function project(sc, lat, lon) {
  const r = Math.PI / 180, p1 = site.lat * r, p2 = lat * r, dl = (lon - site.lon) * r;
  const a = Math.sin((p2 - p1) / 2) ** 2 + Math.cos(p1) * Math.cos(p2) * Math.sin(dl / 2) ** 2, km = 2 * EARTH_KM * Math.asin(Math.sqrt(Math.min(1, a)));
  const b = Math.atan2(Math.sin(dl) * Math.cos(p2), Math.cos(p1) * Math.sin(p2) - Math.sin(p1) * Math.cos(p2) * Math.cos(dl)), rr = km / sc.km * sc.r;
  return {x: Math.round(sc.cx + rr * Math.sin(b)), y: Math.round(sc.cy - rr * Math.cos(b)), in: km <= sc.km};
}
const clearOf = (a, taken) => taken.every(t => a.x + a.w < t.x || a.x > t.x + t.w || a.y + a.h < t.y || a.y > t.y + t.h);
function fast(x, y, colour, text) { tft.setColour(colour); tft.setCursor(x, y); tft.print(text); }
function altColour(alt) {
  if (alt === -1) return C.WHITE;
  if (alt === -2) return ALT_BANDS[0][1];
  return (ALT_BANDS.find(b => alt < b[0]) || ALT_BANDS.at(-1))[1];
}
function dim(c) { const r = (c >> 11 & 31) << 3, g = (c >> 5 & 63) << 2, b = (c & 31) << 3; return rgb565(r * 45 / 100, g * 45 / 100, b * 45 / 100); }
function compassPoint(sc, i) {
  const s = Math.sin(i * Math.PI / 2), c = Math.cos(i * Math.PI / 2), r = sc.r - 20, w = tft.textWidth('NESW'[i]);
  return {x: Math.round((sc.cx + r * s) / SCALE) - Math.floor(w / 2), y: Math.round((sc.cy - r * c) / SCALE) - 4, w, h: 9};
}
function symbol(x, y, ac, colour) {
  const [, , , , track, speed, cat, mlat] = ac, fill = !mlat, t = (track < 0 ? 0 : track) * Math.PI / 180, ct = Math.cos(t), st = Math.sin(t);
  const rot = (px, py) => [Math.round(x + px * ct - py * st), Math.round(y + px * st + py * ct)];
  if ((cat[0] === 'A' && cat[1] >= '3' && cat[1] <= '6') || (cat[0] === '-' && speed > 250)) {
    const s = cat === 'A5' ? 6 : 4.5, tip = rot(0, -1.7 * s), right = rot(s, s), notch = rot(0, 0.35 * s), left = rot(-s, s);
    if (fill) { tft.fillTriangleRaw(...tip, ...right, ...notch, colour); tft.fillTriangleRaw(...tip, ...notch, ...left, colour); }
    else { tft.lineRaw(...tip, ...right, 1, colour); tft.lineRaw(...right, ...notch, 1, colour); tft.lineRaw(...notch, ...left, 1, colour); tft.lineRaw(...left, ...tip, 1, colour); }
  } else if (cat[0] === 'A' && (cat[1] === '1' || cat[1] === '2')) tft.circleRaw(x, y, 3, colour, fill);
  else if (cat === 'A7') { tft.lineRaw(x - 4, y - 4, x + 4, y + 4, 2, colour); tft.lineRaw(x - 4, y + 4, x + 4, y - 4, 2, colour); }
  else if (cat[0] === 'B') {
    if (fill) { tft.fillTriangleRaw(x, y - 5, x + 4, y, x - 4, y, colour); tft.fillTriangleRaw(x, y + 5, x + 4, y, x - 4, y, colour); }
    else { tft.lineRaw(x, y - 5, x + 4, y, 1, colour); tft.lineRaw(x + 4, y, x, y + 5, 1, colour); tft.lineRaw(x, y + 5, x - 4, y, 1, colour); tft.lineRaw(x - 4, y, x, y - 5, 1, colour); }
  } else if (cat[0] === 'C') { if (fill) tft.fillRectRaw(x - 3, y - 3, 7, 7, colour); else tft.drawRectRaw(x - 3, y - 3, 6, 6, colour); }
  else tft.triangleRaw(x, y - 5, x + 4, y + 4, x - 4, y + 4, colour);
}
function drawAdsb() {
  const box = BOXES.adsb, mi = RANGES[state.range], sc = {cx: SCALE * (box.x + box.w / 2), cy: SCALE * (box.y + 74), r: SCALE * 64, km: mi * MI_KM};
  const feed = state.adsb, live = feed && Date.now() / 1000 - feed.t < 30;
  frame(box); tft.setFont('fast');
  const taken = [{x: box.x, y: box.y, w: 40, h: 20}, {x: box.x + box.w - 62, y: box.y, w: 62, h: 20}, ...[0, 1, 2, 3].map(i => compassPoint(sc, i))];
  // the aircraft in range, nearest first, labelled where there is room; the map's names give way
  const shown = [];
  if (live) for (const ac of feed.aircraft) {
    const p = project(sc, ac[1], ac[2]);
    if (p.in) shown.push({...p, r: Math.hypot(p.x - sc.cx, p.y - sc.cy), ac, colour: altColour(ac[3]), fl: ac[3] === -2 ? 'GND' : ac[3] >= 0 ? String(Math.floor(ac[3] / 100)).padStart(3, '0') : ''});
  }
  shown.sort((a, b) => a.r - b.r);
  for (const s of shown) {
    const h = s.fl ? 18 : 9, w = Math.max(tft.textWidth(s.ac[8]), tft.textWidth(s.fl)), x = Math.floor(s.x / SCALE), y = Math.floor(s.y / SCALE);
    for (const a of [{x: x + 4, y: y - Math.floor(h / 2), w, h}, {x: x - 4 - w, y: y - Math.floor(h / 2), w, h}, {x: x - Math.floor(w / 2), y: y - 6 - h, w, h}, {x: x - Math.floor(w / 2), y: y + 6, w, h}]) {
      if (a.x > box.x + 1 && a.x + a.w < box.x + box.w - 1 && a.y > box.y + 1 && a.y + a.h < box.y + box.h - 11 && clearOf(a, taken)) { s.label = a; taken.push(a); break; }
    }
  }
  // the base map: lines in their order, then cities (towns as specks) and airfields where clear
  if (scopeMap) {
    for (const [kind, [colour, thick, to]] of Object.entries(STYLES)) {
      if (mi > to) continue;
      for (const line of scopeMap.lines) {
        if (line.k !== kind) continue;
        let a = project(sc, ...line.p[0]);
        for (let i = 1; i < line.p.length; i++) {
          const b = project(sc, ...line.p[i]), last = i + 1 === line.p.length;
          if (a.in && b.in && !last && Math.abs(b.x - a.x) < 2 && Math.abs(b.y - a.y) < 2) continue;
          if (a.in && b.in) tft.lineRaw(a.x, a.y, b.x, b.y, thick, colour);
          a = b;
        }
      }
    }
    for (const pass of [0, 1]) for (const pl of scopeMap.places) {
      if ((pass === 0) !== (pl.kind === 'city')) continue;
      const p = project(sc, pl.lat, pl.lon);
      if (!p.in) continue;
      if (pl.kind !== 'city') { tft.fillRectRaw(p.x, p.y, 1, 1, TOWN); continue; }
      tft.fillRectRaw(p.x - 1, p.y - 1, 3, 3, CITY);
      const b = {x: Math.floor(p.x / SCALE) + 2, y: Math.floor(p.y / SCALE) - 4, w: tft.textWidth(pl.name), h: 9};
      if (pl.name && b.x + b.w < box.x + box.w - 2 && clearOf(b, taken)) { taken.push(b); fast(b.x, b.y, CITY, pl.name); }
    }
    for (const ap of scopeMap.airports) {
      const p = project(sc, ap.lat, ap.lon), b = {x: Math.floor(p.x / SCALE) + 3, y: Math.floor(p.y / SCALE) + 1, w: tft.textWidth(ap.name), h: 9};
      if (p.in && b.x + b.w < box.x + box.w - 2 && clearOf(b, taken)) { taken.push(b); fast(b.x, b.y, STYLES.runway[0], ap.name); }
    }
  }
  // rings, ticks, compass points and the centre
  tft.circleRaw(sc.cx, sc.cy, Math.floor(sc.r / 3), RING); tft.circleRaw(sc.cx, sc.cy, Math.floor(2 * sc.r / 3), RING); tft.circleRaw(sc.cx, sc.cy, sc.r, C.GRAY);
  for (let a = 0; a < 360; a += 30) {
    const s = Math.sin(a * Math.PI / 180), c = Math.cos(a * Math.PI / 180), r0 = sc.r - (a % 90 ? 6 : 10);
    tft.lineRaw(sc.cx + r0 * s, sc.cy - r0 * c, sc.cx + sc.r * s, sc.cy - sc.r * c, 1, C.GRAY);
  }
  [0, 1, 2, 3].forEach(i => { const a = compassPoint(sc, i); fast(a.x, a.y, C.GRAY, 'NESW'[i]); });
  for (const i of [1, 2]) { const r = sc.r * i / 3; fast(Math.round((sc.cx + r * 0.707) / SCALE) + 1, Math.round((sc.cy - r * 0.707) / SCALE) - 9, RING, (mi * i / 3).toFixed(0)); }
  tft.lineRaw(sc.cx - 4, sc.cy, sc.cx + 4, sc.cy, 1, C.WHITE); tft.lineRaw(sc.cx, sc.cy - 4, sc.cx, sc.cy + 4, 1, C.WHITE);
  // trails solid, the next minute dotted, then the symbols and their labels
  for (const s of shown) {
    const d = dim(s.colour), trail = state.trails.get(s.ac[0]) || [];
    let prev = null;
    for (const [lat, lon] of trail) { const p = project(sc, lat, lon); if (p.in && prev) tft.lineRaw(prev.x, prev.y, p.x, p.y, 1, d); prev = p.in ? p : null; }
    if (prev) tft.lineRaw(prev.x, prev.y, s.x, s.y, 1, d);
    const [, , , , track, speed] = s.ac;
    if (speed > 0 && track >= 0) {
      const r = speed * 1.852 / 60 / sc.km * sc.r, t = track * Math.PI / 180;
      let x1 = s.x + r * Math.sin(t), y1 = s.y - r * Math.cos(t);
      const ddx = x1 - s.x, ddy = y1 - s.y, fx = s.x - sc.cx, fy = s.y - sc.cy, rr = sc.r - 1, A = ddx * ddx + ddy * ddy, B = fx * ddx + fy * ddy, Cc = fx * fx + fy * fy - rr * rr;
      if (A > 0 && Cc <= 0) { const k = (-B + Math.sqrt(B * B - A * Cc)) / A; if (k < 1) { x1 = s.x + k * ddx; y1 = s.y + k * ddy; } }
      const n = Math.floor(Math.hypot(x1 - s.x, y1 - s.y) / 4);
      for (let i = 2; i <= n; i++) tft.fillRectRaw(Math.round(s.x + (x1 - s.x) * i / n), Math.round(s.y + (y1 - s.y) * i / n), 2, 2, d);
    }
  }
  for (const s of shown) symbol(s.x, s.y, s.ac, s.colour);
  for (const s of shown) if (s.label) { fast(s.label.x, s.label.y, s.colour, s.ac[8]); if (s.fl) fast(s.label.x, s.label.y + 9, s.colour, s.fl); }
  // the corners: name and range, today's count and now, the legend and the time
  fast(box.x + 2, box.y + 2, TITLE, 'ADS-B'); fast(box.x + 2, box.y + 11, TEXT, `${mi} mi`);
  if (feed) { const t = `${state.today.size} AC`; fast(box.x + box.w - tft.textWidth(t) - 2, box.y + 2, C.WHITE, t); }
  if (live) { const t = feed.nopos > 0 ? `${shown.length} now +${feed.nopos}` : `${shown.length} now`; fast(box.x + box.w - tft.textWidth(t) - 2, box.y + 11, TEXT, t); }
  let x = box.x + 2;
  for (const [, colour, label] of ALT_BANDS) { tft.fillRect(x, box.y + box.h - 7, 4, 4, colour); fast(x + 5, box.y + box.h - 10, TEXT, label); x += 5 + tft.textWidth(label) + 3; }
  if (feed) { const d = new Date(feed.t * 1000), t = `${String(d.getUTCHours()).padStart(2, '0')}:${String(d.getUTCMinutes()).padStart(2, '0')}Z`; fast(box.x + box.w - tft.textWidth(t) - 2, box.y + box.h - 10, TEXT, t); }
  const why = !feed ? 'No ADS-B feed' : !live ? 'Feed stopped' : null;
  if (why) { const w = tft.textWidth(why) + 6; tft.fillRect(box.x + Math.floor((box.w - w) / 2), box.y + 22, w, 11, C.RED); fast(box.x + Math.floor((box.w - w) / 2) + 3, box.y + 24, C.WHITE, why); }
}
async function fetchAdsb() {
  const feed = await json('api/adsb?site=' + site.id), now = Date.now() / 1000;
  for (const ac of feed.aircraft) {
    state.today.add(ac[0]);
    const trail = state.trails.get(ac[0]) || [];
    const last = trail.at(-1);
    if (!last || last[0] !== ac[1] || last[1] !== ac[2]) trail.push([ac[1], ac[2], now]);
    state.trails.set(ac[0], trail.filter(p => now - p[2] < 600).slice(-60));
  }
  for (const [hex, trail] of state.trails) if (now - trail.at(-1)[2] > 600) state.trails.delete(hex);
  state.adsb = feed;
  drawAdsb();
}
// a tap on the scope below its top edge takes the next range, as the native menu would choose it
function tapAdsb(x, y) {
  const box = BOXES.adsb;
  if (x < box.x || x >= box.x + box.w || y < box.y + 27 || y >= box.y + box.h) return false;
  state.range = (state.range + 1) % RANGES.length;
  drawAdsb(); tft.show();
  return true;
}

export async function start(canvas, chosen) {
  site = chosen;
  tft = new Tft(canvas);
  state.range = Math.max(0, RANGES.indexOf(site.scope_mi));
  osmScope().then(found => { scopeMap = found; drawAdsb(); tft.show(); }).catch(e => console.warn('scope map', e.message));
  drawCall(); drawClock(); drawXray(); drawAdsb(); drawWx(); drawStats(); drawCluster(); tft.show();
  setInterval(() => { drawClock(); tft.show(); }, 1000);
  every(300, fetchSpace); every(900, fetchWx); every(60, fetchCluster); every(5, fetchAdsb);
  setInterval(drawCluster, 10000);                     // the spots' ages
  canvas.addEventListener('click', e => {
    const r = canvas.getBoundingClientRect();
    tapAdsb((e.clientX - r.left) * 800 / r.width, (e.clientY - r.top) * 480 / r.height);
  });
}
