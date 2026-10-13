// Live amateur activity around the summit, as the station's activity.py gathers it: APRS-IS (by
// way of /api/aprs), FT8/FT4 reception reports pushed by PSK Reporter's MQTT feed (straight from
// the page, over its WebSocket), WSPR from wspr.live; and which paths of them to draw.
import {W, H, locator} from './geo.js';
import {band} from './clock.js';

const MQTT = 'wss://mqtt.pskreporter.info:1886/mqtt';
const KEEP_S = 1800, APRS_S = 30, WSPR_S = 600;
export const live = {aprs: new Map(), links: new Map(), reports: new Map(), connected: false};
let site = null, toBase = null, changed = () => {};

export function inside([x, y], margin = 4) { return x >= margin && x < W - margin && y >= margin && y < H - margin; }

// the Maidenhead squares and fields the map covers (activity.Map.squares, .fields)
function cover(latlon) {
  const squares = new Set(), fields = new Set();
  for (let x = 0; x <= W; x += W / 8) for (let y = 0; y <= H; y += H / 4) {
    const [lat, lon] = latlon(x, y);
    fields.add(String.fromCharCode(65 + Math.floor((lon + 180) / 20), 65 + Math.floor((lat + 90) / 10)));
  }
  const corners = [];
  for (let x = 0; x <= W; x += W / 8) for (let y = 0; y <= H; y += H / 4) corners.push(latlon(x, y));
  const xs = corners.map(p => Math.floor((p[1] + 180) / 2)), ys = corners.map(p => Math.floor(p[0] + 90));
  for (let x = Math.min(...xs); x <= Math.max(...xs); x++) for (let y = Math.min(...ys); y <= Math.max(...ys); y++)
    squares.add(String.fromCharCode(65 + Math.floor(x / 10), 65 + Math.floor(y / 10)) + x % 10 + y % 10);
  return {squares: [...squares], fields: [...fields], corners};
}

// ---------------------------------------------------------------- MQTT ----
// MQTT 3.1.1, just enough to subscribe at QoS 0 and take what is published

function utf8(text) { const b = new TextEncoder().encode(text); return [b.length >> 8, b.length & 255, ...b]; }
function packet(type, body) {
  const length = [];
  let n = body.length;
  do { let byte = n % 128; n = Math.floor(n / 128); if (n) byte |= 128; length.push(byte); } while (n);
  return new Uint8Array([type, ...length, ...body]);
}
function mqtt(topics) {
  const ws = new WebSocket(MQTT, ['mqtt']);
  ws.binaryType = 'arraybuffer';
  let buffer = new Uint8Array(0), ping = 0;
  ws.onopen = () => {
    ws.send(packet(0x10, [...utf8('MQTT'), 4, 2, 0, 60, ...utf8('influ-demo-' + Math.random().toString(36).slice(2, 10))]));
    for (let i = 0; i < topics.length; i += 50)
      ws.send(packet(0x82, [0, i / 50 + 1, ...topics.slice(i, i + 50).flatMap(t => [...utf8(t), 0])]));
    ping = setInterval(() => ws.readyState === 1 && ws.send(new Uint8Array([0xc0, 0])), 30000);
    live.connected = true;
  };
  ws.onmessage = ({data}) => {
    const joined = new Uint8Array(buffer.length + data.byteLength);
    joined.set(buffer); joined.set(new Uint8Array(data), buffer.length); buffer = joined;
    for (;;) {
      let length = 0, shift = 1, at = 1;
      while (at < buffer.length && buffer[at] & 128) { length += (buffer[at] & 127) * shift; shift *= 128; at++; }
      if (at >= buffer.length) return;
      length += (buffer[at] & 127) * shift; at++;
      if (buffer.length < at + length) return;
      const body = buffer.subarray(at, at + length);
      if (buffer[0] >> 4 === 3) {
        const t = body[0] << 8 | body[1], skip = 2 + t + ((buffer[0] >> 1 & 3) ? 2 : 0);
        take(new TextDecoder().decode(body.subarray(skip)));
      }
      buffer = buffer.slice(at + length);
    }
  };
  ws.onclose = () => { clearInterval(ping); live.connected = false; setTimeout(() => mqtt(topics), 30000); };
}
function key(r) { return [r.senderCallsign, r.receiverCallsign, band(r.frequency / 1000)?.name, r.mode].join(' '); }
function take(payload) {
  try {
    const v = JSON.parse(payload);
    const r = {senderCallsign: v.sc, receiverCallsign: v.rc, senderLocator: v.sl, receiverLocator: v.rl, frequency: Number(v.f), mode: v.md, seen: Date.now() / 1000};
    if (!r.senderCallsign || !r.receiverCallsign || !(r.frequency > 0)) return;
    const ends = [r.senderLocator, r.receiverLocator].map(locator);
    if (!ends.some(ll => ll && inside(toBase(...ll)))) return;
    live.reports.set(key(r), r);
  } catch (e) {
    // not a report
  }
}

// ---------------------------------------------------------------- WSPR, APRS ----

async function wspr(corners) {
  const lats = corners.map(p => p[0]), lons = corners.map(p => p[1]);
  const s = Math.floor(Math.min(...lats) * 10) / 10, n = Math.ceil(Math.max(...lats) * 10) / 10;
  const w = Math.floor(Math.min(...lons) * 10) / 10, e = Math.ceil(Math.max(...lons) * 10) / 10;
  const box = who => `(${who}_lat BETWEEN ${s.toFixed(1)} AND ${n.toFixed(1)} AND ${who}_lon BETWEEN ${w.toFixed(1)} AND ${e.toFixed(1)})`;
  const query = 'SELECT tx_sign AS senderCallsign, tx_loc AS senderLocator, rx_sign AS receiverCallsign, rx_loc AS receiverLocator, '
    + "frequency, 'WSPR' AS mode FROM wspr.rx WHERE time >= now() - INTERVAL 30 MINUTE "
    + `AND band IN (1,3,5,7,10,14,18,21,24,28,50,144,432) AND (${box('tx')} OR ${box('rx')}) ORDER BY time DESC LIMIT 2000 FORMAT JSON`;
  const r = await fetch('https://db1.wspr.live/?query=' + encodeURIComponent(query));
  if (!r.ok) return;
  const now = Date.now() / 1000;
  for (const row of (await r.json()).data) live.reports.set(key(row), {...row, frequency: Number(row.frequency), seen: now});
}
async function aprs() {
  const r = await fetch('api/aprs?site=' + site.id);
  if (!r.ok) return;
  const data = await r.json(), now = Date.now() / 1000;
  for (const [call, lat, lon, rf, t] of data.stations) live.aprs.set(call, [lat, lon, rf, t]);
  for (const [a, b] of data.links) live.links.set(a + ' ' + b, now);
}
function prune() {
  const now = Date.now() / 1000;
  for (const [k, r] of live.reports) if (now - r.seen > KEEP_S) live.reports.delete(k);
  for (const [k, v] of live.aprs) if (now - v[3] > 3600) live.aprs.delete(k);
  for (const [k, t] of live.links) if (now - t > 3600) live.links.delete(k);
}

// ---------------------------------------------------------------- paths ----
// activity.digital_paths and select_paths: stations on the map, local paths whole, and for each
// station its two strongest bundles of distant paths, 2 m and 70 cm first

export function digitalGraph() {
  const stations = new Map(), bands = new Map(), paths = new Map();
  for (const r of live.reports.values()) {
    const b = band(r.frequency / 1000);
    if (!b) continue;
    const ends = [r.senderLocator, r.receiverLocator].map(g => { const ll = locator(g); return ll ? toBase(...ll) : null; });
    const local = [0, 1].filter(i => ends[i] && inside(ends[i]));
    if (!local.length) continue;
    const calls = [r.senderCallsign, r.receiverCallsign];
    bands.set(b.name, b.rgb);
    for (const i of local) if (!stations.has(calls[i])) stations.set(calls[i], {xy: ends[i], rgb: b.rgb, sent: i === 0});
    if (ends[0] && ends[1]) paths.set([...calls].sort().join(' ') + ' ' + b.name, {ends, local, calls, band: b.name, rgb: b.rgb});
  }
  const inner = [], bundles = new Map(), degrees = new Map([...stations.keys()].map(k => [k, new Set()]));
  for (const p of paths.values()) {
    for (const i of p.local) { if (!degrees.has(p.calls[i])) degrees.set(p.calls[i], new Set()); degrees.get(p.calls[i]).add(p.calls[1 - i]); }
    if (p.local.length === 2) { inner.push(p); continue; }
    const i = p.local[0], [x, y] = p.ends[i], [xx, yy] = p.ends[1 - i];
    const sector = Math.floor(((Math.atan2(xx - x, y - yy) * 180 / Math.PI + 360) % 360) / 30), k = [p.calls[i], p.band, sector].join(' ');
    if (!bundles.has(k)) bundles.set(k, {call: p.calls[i], path: p, weight: 0});
    bundles.get(k).weight++;
  }
  const chosen = [...inner];
  for (const call of stations.keys()) {
    const options = [...bundles.values()].filter(b => b.call === call);
    options.sort((a, b) => ((a.path.band === '2' || a.path.band === '70cm') ? 0 : 1) - ((b.path.band === '2' || b.path.band === '70cm') ? 0 : 1)
      || b.weight - a.weight || (a.path.band < b.path.band ? -1 : a.path.band > b.path.band ? 1 : 0));
    chosen.push(...options.slice(0, 2).map(b => b.path));
  }
  return {stations, bands, chosen, degrees: new Map([...degrees].map(([k, v]) => [k, v.size])), total: paths.size};
}

// stations heard within 20 km of a point (base px), and the paths they are on (map_panel.nearby)
export function nearby(point, kmPerPx) {
  const calls = new Set(), peers = new Set(), close = xy => xy && Math.hypot(xy[0] - point[0], xy[1] - point[1]) * kmPerPx <= 20;
  for (const r of live.reports.values()) {
    let local = false;
    for (const role of ['sender', 'receiver']) {
      const ll = locator(r[role + 'Locator']);
      if (ll && close(toBase(...ll))) { calls.add(r[role + 'Callsign']); local = true; }
    }
    if (local) peers.add([r.senderCallsign, r.receiverCallsign].sort().join(' '));
  }
  for (const [call, v] of live.aprs) if (close(toBase(v[0], v[1]))) calls.add(call);
  return {calls: [...calls].sort(), paths: peers.size};
}

export function startLive(chosen, base, latlon, onChange) {
  site = chosen; toBase = base; changed = onChange;
  const {squares, fields, corners} = cover(latlon), topics = [];
  for (const field of fields) topics.push(`pskr/filter/v2_field/+/+/${field}/+`, `pskr/filter/v2_field/+/+/+/${field}`);
  for (const grid of squares) for (const prefix of ['pskr/filter/v2', 'wspr/filter'])
    topics.push(`${prefix}/+/+/+/+/${grid}/+/#`, `${prefix}/+/+/+/+/+/${grid}/#`);
  mqtt(topics);
  const run = (seconds, task) => { const go = () => task().catch(() => {}).finally(() => { prune(); changed(); }); go(); setInterval(go, seconds * 1000); };
  run(APRS_S, aprs);
  run(WSPR_S, () => wspr(corners));
  setInterval(() => { prune(); changed(); }, 15000);
}
