// Aircraft from the open ADS-B exchanges (readsb's JSON, as adsb.lol and adsb.fi serve it) as the
// station's tools/adsb_scope.py writes them for HamClock's ADS-B pane: one row each,
// [hex, lat, lon, alt ft (-1 unknown, -2 on the ground), track (-1 unknown), knots, category,
// mlat 0|1, name]. Military aircraft (dbFlags bit 0) are left out.

export const SOURCES = [
  {name: 'adsb.lol', url: (lat, lon, nm) => `https://api.adsb.lol/v2/point/${lat}/${lon}/${nm}`, list: d => d.ac},
  {name: 'adsb.fi', url: (lat, lon, nm) => `https://opendata.adsb.fi/api/v2/lat/${lat}/lon/${lon}/dist/${nm}`, list: d => d.aircraft},
];

export function military(ac) {
  return (Number(ac.dbFlags) & 1) === 1;
}

function ascii(text) {
  return String(text || '').replace(/[^\x21-\x7e]/g, '');
}

export function rows(list) {
  const out = [];
  let nopos = 0;
  for (const ac of list || []) {
    if (!ac || typeof ac.hex !== 'string' || military(ac)) continue;
    if (!Number.isFinite(ac.lat) || !Number.isFinite(ac.lon)) { nopos++; continue; }
    const alt = ac.alt_baro === 'ground' ? -2 : Number.isFinite(ac.alt_baro) ? Math.round(ac.alt_baro) : -1;
    const heading = Number.isFinite(ac.track) ? ac.track : ac.true_heading;
    const track = Number.isFinite(heading) ? Math.round(heading) % 360 : -1;
    const cat = String(ac.category || '-').toUpperCase().slice(0, 3);
    const mlat = (ac.mlat && ac.mlat.length) || (ac.tisb && ac.tisb.length) || ac.type === 'mlat' ? 1 : 0;
    const name = (ascii(ac.flight) || ac.hex.replace(/^~/, '').toUpperCase()).slice(0, 11);
    out.push([ac.hex.slice(0, 7), +ac.lat.toFixed(5), +ac.lon.toFixed(5), alt, track, Math.round(ac.gs || 0), cat, mlat, name]);
  }
  return {aircraft: out, nopos};
}
