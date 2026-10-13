// The ground of the map, drawn in this worker as the station's build_tiles.py renders its tiles:
// relief from the public Terrarium elevation tiles, muted hypsometric tint times a soft NW
// hillshade (build_rangemap.relief), and where the summit's antenna sees, tinted and outlined in
// gold (build_tiles.render_ground) from the viewshed the summit came with (reach.png).
import {W, H, demZoom, mercator, destination} from './geo.js';

const DEM_URL = 'https://s3.amazonaws.com/elevation-tiles-prod/terrarium/{z}/{x}/{y}.png';
const TILE = 256, SEEN = [255, 215, 0], SEA = [18, 36, 84];      // the sea: Terrarium holds its floor
const STOPS = [[0, [34, 66, 40]], [500, [52, 86, 46]], [1200, [98, 92, 54]], [2500, [132, 112, 82]], [4000, [160, 154, 146]], [5500, [222, 224, 232]]];
const SUN_AZ = 315 * Math.PI / 180, SUN_ALT = 45 * Math.PI / 180;
let site = null, info = null, visible = null;
const heights = new Map();

async function pixels(url) {
  const r = await fetch(url);
  if (!r.ok) throw Error(url + ' ' + r.status);
  const bitmap = await createImageBitmap(await r.blob()), canvas = new OffscreenCanvas(bitmap.width, bitmap.height);
  const c = canvas.getContext('2d');
  c.drawImage(bitmap, 0, 0);
  return c.getImageData(0, 0, bitmap.width, bitmap.height);
}
// a Terrarium tile's heights (m), kept for the next tiles that need it
function dem(z, x, y) {
  const key = `${z}/${x}/${y}`;
  if (!heights.has(key)) {
    if (heights.size > 400) heights.delete(heights.keys().next().value);
    heights.set(key, pixels(DEM_URL.replace('{z}', z).replace('{x}', x).replace('{y}', y)).then(image => {
      const out = new Float32Array(TILE * TILE), d = image.data;
      for (let i = 0; i < out.length; i++) out[i] = d[4 * i] * 256 + d[4 * i + 1] + d[4 * i + 2] / 256 - 32768;
      return out;
    }).catch(e => { heights.delete(key); throw e; }));
  }
  return heights.get(key);
}
// heights at points (lat, lon pairs), bilinear in the Terrarium px of zoom z, edges repeating
export async function sample(lats, lons, z) {
  const n = lats.length, us = new Float64Array(n), vs = new Float64Array(n), need = new Set(), max = 256 * 2 ** z - 1;
  for (let i = 0; i < n; i++) {
    const [u, v] = mercator(lats[i], lons[i], z);
    us[i] = Math.min(max, Math.max(0, u)); vs[i] = Math.min(max, Math.max(0, v));
    for (const tx of [Math.floor(us[i]) >> 8, Math.min(max, Math.floor(us[i]) + 1) >> 8])
      for (const ty of [Math.floor(vs[i]) >> 8, Math.min(max, Math.floor(vs[i]) + 1) >> 8]) need.add(tx + ',' + ty);
  }
  const tiles = new Map();
  await Promise.all([...need].map(async key => { const [x, y] = key.split(',').map(Number); tiles.set(key, await dem(z, x, y)); }));
  const at = (x, y) => { x = Math.min(max, x); y = Math.min(max, y); return tiles.get((x >> 8) + ',' + (y >> 8))[(y & 255) * TILE + (x & 255)]; };
  const out = new Float32Array(n);
  for (let i = 0; i < n; i++) {
    const x = Math.floor(us[i]), y = Math.floor(vs[i]), fx = us[i] - x, fy = vs[i] - y;
    out[i] = (at(x, y) * (1 - fx) + at(x + 1, y) * fx) * (1 - fy) + (at(x, y + 1) * (1 - fx) + at(x + 1, y + 1) * fx) * fy;
  }
  return out;
}

async function start(data) {
  ({site, info} = data);
  const image = await pixels(data.reach);
  visible = new Uint8Array(info.bearings * info.steps);
  for (let i = 0; i < visible.length; i++) visible[i] = image.data[4 * i] > 127 ? 1 : 0;
}

// one tile of the map at `level` base-map scale: the 256 px square at column, row, and the px
// round it that the slopes and the outline need
async function tile(level, column, row) {
  const n = TILE + 2, km = info.km_per_px / level, lats = new Float64Array(n * n), lons = new Float64Array(n * n);
  const dist = new Float64Array(n * n), bearing = new Float64Array(n * n);
  for (let j = 0; j < n; j++) for (let i = 0; i < n; i++) {
    // px centres in base px, the map's edge repeating past it
    const bx = Math.min(W, Math.max(0, (column * TILE + i - 1 + 0.5) / level)), by = Math.min(H, Math.max(0, (row * TILE + j - 1 + 0.5) / level));
    const dx = (bx - W / 2) * info.km_per_px, dy = (by - H / 2) * info.km_per_px, k = j * n + i;
    dist[k] = Math.hypot(dx, dy); bearing[k] = (Math.atan2(dx, -dy) * 180 / Math.PI + 360) % 360;
    [lats[k], lons[k]] = destination(site.lat, site.lon, bearing[k], dist[k]);
  }
  const below = await sample(lats, lons, demZoom(level, info.km_per_px, site.lat)), elev = below.map(h => Math.max(0, h));
  // seen where the viewshed cell under the px is; far patches widened a px on the whole map
  const seen = new Uint8Array(n * n), reach = info.steps * info.step_km;
  for (let k = 0; k < n * n; k++) {
    const ia = Math.round(bearing[k] / (360 / info.bearings)) % info.bearings;
    const id = Math.min(info.steps - 1, Math.max(0, Math.round(dist[k] / info.step_km) - 1));
    seen[k] = dist[k] <= reach && visible[ia * info.steps + id];
  }
  const grown = level === 1 ? seen.map((v, k) => v || dist[k] > 40 && (seen[k - 1] || seen[k + 1] || seen[k - n] || seen[k + n]) ? 1 : 0) : seen;
  const out = new Uint8ClampedArray(TILE * TILE * 4), step = km * 1000;
  for (let j = 1; j <= TILE; j++) for (let i = 1; i <= TILE; i++) {
    const k = j * n + i, gx = (elev[k + 1] - elev[k - 1]) / (2 * step), gy = (elev[k + n] - elev[k - n]) / (2 * step);
    const slope = Math.atan(Math.hypot(gx, gy) * 3), aspect = Math.atan2(-gx, gy);
    const shade = Math.sin(SUN_ALT) * Math.cos(slope) + Math.cos(SUN_ALT) * Math.sin(slope) * Math.cos(SUN_AZ - aspect);
    const light = Math.min(1.25, Math.max(0.35, 0.55 + 0.6 * shade)), h = Math.min(5500, Math.max(0, elev[k]));
    let s = 0;
    while (s < STOPS.length - 2 && h > STOPS[s + 1][0]) s++;
    const t = (h - STOPS[s][0]) / (STOPS[s + 1][0] - STOPS[s][0]), o = ((j - 1) * TILE + i - 1) * 4;
    for (let c = 0; c < 3; c++) out[o + c] = (below[k] < 0 ? SEA[c] : STOPS[s][1][c] + (STOPS[s + 1][1][c] - STOPS[s][1][c]) * t) * light;
    if (grown[k]) {
      const inner = grown[k - 1] && grown[k + 1] && grown[k - n] && grown[k + n];
      for (let c = 0; c < 3; c++) out[o + c] = inner ? out[o + c] * 0.45 + SEEN[c] * 0.55 : SEEN[c];
    }
    out[o + 3] = 255;
  }
  return out;
}

self.onmessage = async ({data}) => {
  try {
    if (data.type === 'start') { await start(data); self.postMessage({id: data.id}); }
    else if (data.type === 'tile') { const px = await tile(data.level, data.column, data.row); self.postMessage({id: data.id, px}, [px.buffer]); }
    else if (data.type === 'heights') self.postMessage({id: data.id, heights: await sample(data.lats, data.lons, data.zoom)});
  } catch (e) {
    self.postMessage({id: data.id, error: e.message});
  }
};
