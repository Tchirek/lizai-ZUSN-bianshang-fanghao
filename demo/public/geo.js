// The map's geometry and the sun, as the station's Python works them out (build_rangemap.py,
// map_panel.py): azimuthal equidistant about the summit in metres (x east, y north), a sphere of
// 6371 km, NOAA's fractional-year sun.
export const R_EARTH = 6371, K_FACTOR = 4 / 3, W = 1320, H = 660;
export const SUNSET = -0.833, DARK = -18;
const RAD = Math.PI / 180;

// distance (km) and initial bearing (deg) from lat0, lon0
export function polar(lat0, lon0, lat, lon) {
  const p1 = lat0 * RAD, p2 = lat * RAD, dl = (lon - lon0) * RAD;
  const a = Math.sin((p2 - p1) / 2) ** 2 + Math.cos(p1) * Math.cos(p2) * Math.sin(dl / 2) ** 2;
  const d = 2 * R_EARTH * Math.asin(Math.sqrt(Math.min(1, Math.max(0, a))));
  const b = Math.atan2(Math.sin(dl) * Math.cos(p2), Math.cos(p1) * Math.sin(p2) - Math.sin(p1) * Math.cos(p2) * Math.cos(dl));
  return [d, (b / RAD + 360) % 360];
}
export function destination(lat0, lon0, bearing, km) {
  const p1 = lat0 * RAD, l1 = lon0 * RAD, th = bearing * RAD, dl = km / R_EARTH;
  const p2 = Math.asin(Math.sin(p1) * Math.cos(dl) + Math.cos(p1) * Math.sin(dl) * Math.cos(th));
  const l2 = l1 + Math.atan2(Math.sin(th) * Math.sin(dl) * Math.cos(p1), Math.cos(dl) - Math.sin(p1) * Math.sin(p2));
  return [p2 / RAD, ((l2 / RAD + 540) % 360) - 180];
}
export function forward(site, lat, lon) {
  const [d, b] = polar(site.lat, site.lon, lat, lon);
  return [d * 1000 * Math.sin(b * RAD), d * 1000 * Math.cos(b * RAD)];
}
export function inverse(site, x, y) {
  return destination(site.lat, site.lon, Math.atan2(x, y) / RAD, Math.hypot(x, y) / 1000);
}
export function mercator(lat, lon, z) {
  const n = 256 * 2 ** z, r = lat * RAD;
  return [(lon + 180) / 360 * n, (1 - Math.log(Math.tan(r) + 1 / Math.cos(r)) / Math.PI) / 2 * n];
}
// the coarsest elevation zoom with px no larger than the map's at `level` (build_tiles.dem_zoom)
export function demZoom(level, kmPerPx, lat) {
  const ratio = 156543.03 * Math.cos(lat * RAD) / (kmPerPx / level * 1000);
  return Math.min(12, Math.max(8, Math.ceil(Math.log2(ratio))));
}
export function demMetres(level, kmPerPx, lat) { return 156543.03 * Math.cos(lat * RAD) / 2 ** demZoom(level, kmPerPx, lat); }

// UTC hour, equation of time (min) and declination (rad) at unix time `when`
export function sun(when) {
  const d = new Date(when * 1000), year = d.getUTCFullYear(), leap = year % 4 === 0 && (year % 100 !== 0 || year % 400 === 0);
  const hour = d.getUTCHours() + d.getUTCMinutes() / 60 + d.getUTCSeconds() / 3600;
  const yday = Math.floor((Date.UTC(year, d.getUTCMonth(), d.getUTCDate()) - Date.UTC(year, 0, 1)) / 864e5) + 1;
  const g = 2 * Math.PI / (leap ? 366 : 365) * (yday - 1 + (hour - 12) / 24);
  const eq = 229.18 * (.000075 + .001868 * Math.cos(g) - .032077 * Math.sin(g) - .014615 * Math.cos(2 * g) - .040849 * Math.sin(2 * g));
  const decl = .006918 - .399912 * Math.cos(g) + .070257 * Math.sin(g) - .006758 * Math.cos(2 * g) + .000907 * Math.sin(2 * g)
    - .002697 * Math.cos(3 * g) + .00148 * Math.sin(3 * g);
  return {hour, eq, decl};
}
export function solarElevation(lat, lon, when, s = sun(when)) {
  const ha = ((s.hour * 60 + s.eq + 4 * lon) / 4 - 180) * RAD, p = lat * RAD;
  return Math.asin(Math.max(-1, Math.min(1, Math.sin(p) * Math.sin(s.decl) + Math.cos(p) * Math.cos(s.decl) * Math.cos(ha)))) / RAD;
}
// sunrise and sunset (unix) on the solar day whose noon is nearest `when`; nulls if none
export function sunTimes(lat, lon, when) {
  const {eq} = sun(when);
  let noon = when - when % 86400 + (720 - 4 * lon - eq) * 60;
  noon += Math.round((when - noon) / 86400) * 86400;
  const {decl} = sun(noon), phi = lat * RAD;
  const cos = (Math.sin(SUNSET * RAD) - Math.sin(phi) * Math.sin(decl)) / (Math.cos(phi) * Math.cos(decl));
  if (Math.abs(cos) > 1) return [null, null];
  const half = Math.acos(cos) / RAD * 240;
  return [noon - half, noon + half];
}
export function locator(grid) {
  const g = String(grid || '').trim().toUpperCase();
  if (g.length < 4 || !/^[A-R]{2}[0-9]{2}/.test(g)) return null;
  let lon = (g.charCodeAt(0) - 65) * 20 - 180 + Number(g[2]) * 2 + 1, lat = (g.charCodeAt(1) - 65) * 10 - 90 + Number(g[3]) + 0.5;
  if (g.length >= 6 && /^[A-X]{2}$/.test(g.slice(4, 6))) {
    lon += (g.charCodeAt(4) - 65) * 2 / 24 - 1 + 1 / 24;
    lat += (g.charCodeAt(5) - 65) / 24 - 0.5 + 1 / 48;
  }
  return [lat, lon];
}
