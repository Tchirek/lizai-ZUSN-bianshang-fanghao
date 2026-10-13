// APRS-IS lines as the station's activity.py takes them (with aprslib there): who is where, and
// the RF hops a packet took from its source through digipeaters to the igate that heard it.

const ALIASES = ['WIDE', 'RELAY', 'TRACE', 'TCPIP', 'TCPXX', 'NOGATE', 'RFONLY', 'GATE', 'ECHO'];
const RF_Q = ['qAR', 'qAO', 'qAr', 'qAo'];

// DDMM.mmN and DDDMM.mmE, spaces of position ambiguity read as nought
function degrees(text, width) {
  const t = text.replace(/ /g, '0');
  const d = Number(t.slice(0, width)), m = Number(t.slice(width, width + 5)), hemi = t[width + 5];
  if (!Number.isFinite(d) || !Number.isFinite(m) || !'NSEW'.includes(hemi)) return null;
  const v = d + m / 60;
  return hemi === 'S' || hemi === 'W' ? -v : v;
}

function base91(text) {
  let v = 0;
  for (const ch of text) {
    const c = ch.charCodeAt(0) - 33;
    if (c < 0 || c > 90) return null;
    v = v * 91 + c;
  }
  return v;
}

// a position report's body: plain (4903.50N/07201.75W) or compressed (/5L!!<*e7)
export function position(body) {
  if (/^[0-9 ]{4}\.[0-9 ]{2}[NS].[0-9 ]{5}\.[0-9 ]{2}[EW]/.test(body)) {
    const lat = degrees(body.slice(0, 8), 2), lon = degrees(body.slice(9, 18), 3);
    return lat === null || lon === null ? null : {lat, lon};
  }
  if (/^[\/\\A-Za-j]/.test(body) && body.length >= 10) {
    const y = base91(body.slice(1, 5)), x = base91(body.slice(5, 9));
    if (y === null || x === null) return null;
    return {lat: 90 - y / 380926, lon: -180 + x / 190463};
  }
  return null;
}

// Mic-E: the latitude in the destination field, the longitude in the first bytes after the type
export function micE(destination, body) {
  const dest = destination.split('-')[0];
  if (dest.length !== 6 || body.length < 3) return null;
  let digits = '';
  for (const ch of dest) {
    if (ch >= '0' && ch <= '9') digits += ch;
    else if (ch >= 'A' && ch <= 'J') digits += String(ch.charCodeAt(0) - 65);
    else if (ch >= 'P' && ch <= 'Y') digits += String(ch.charCodeAt(0) - 80);
    else if (ch === 'K' || ch === 'L' || ch === 'Z') digits += '0';
    else return null;
  }
  const north = dest[3] >= 'P', plus100 = dest[4] >= 'P', west = dest[5] >= 'P';
  let lat = Number(digits.slice(0, 2)) + Number(digits.slice(2, 4) + '.' + digits.slice(4)) / 60;
  let d = body.charCodeAt(0) - 28 + (plus100 ? 100 : 0);
  if (d >= 180 && d <= 189) d -= 80;
  else if (d >= 190 && d <= 199) d -= 190;
  let m = body.charCodeAt(1) - 28;
  if (m >= 60) m -= 60;
  const h = body.charCodeAt(2) - 28;
  let lon = d + (m + h / 100) / 60;
  if (!north) lat = -lat;
  if (west) lon = -lon;
  return Math.abs(lat) <= 90 && Math.abs(lon) <= 180 ? {lat, lon} : null;
}

// SRC>DEST,PATH...:payload as {from, destination, path, lat?, lon?}; objects and items are where
// their own names are; null for a line that is no packet
export function parse(line) {
  const colon = line.indexOf(':'), gt = line.indexOf('>');
  if (line.startsWith('#') || colon < 0 || gt < 0 || gt > colon) return null;
  const from = line.slice(0, gt), [destination, ...path] = line.slice(gt + 1, colon).split(',');
  const payload = line.slice(colon + 1), packet = {from, destination, path};
  let at = null, name = from;
  switch (payload[0]) {
    case '!': case '=': at = position(payload.slice(1)); break;
    case '/': case '@': at = position(payload.slice(8)); break;
    case ';': name = payload.slice(1, 10).trim(); at = payload[10] === '*' ? position(payload.slice(18)) : null; break;
    case ')': {
      const end = payload.slice(1, 11).search(/[!_]/);
      if (end >= 3) { name = payload.slice(1, 1 + end); at = payload[1 + end] === '!' ? position(payload.slice(2 + end)) : null; }
      break;
    }
    case '`': case "'": at = micE(destination, payload.slice(1)); break;
  }
  if (at && Number.isFinite(at.lat) && Number.isFinite(at.lon)) Object.assign(packet, {name, lat: at.lat, lon: at.lon});
  return packet;
}

// The RF hops of a packet: source, each digipeater that repeated it (an alias hides which, so
// null keeps the gap), the igate that heard it on RF; internet-only packets give just the source
// (activity.rf_hops)
export function rfHops(source, path) {
  if (path.some(h => ['TCPIP', 'TCPXX'].includes(h.replace(/\*$/, '').toUpperCase()))) return [source];
  let q = path.findIndex(h => h.startsWith('qA'));
  if (q < 0) q = path.length;
  if (q < path.length && !RF_Q.includes(path[q])) return [source];
  const digis = path.slice(0, q);
  let last = -1;
  digis.forEach((h, i) => { if (h.endsWith('*')) last = i; });
  const hops = [source, ...digis.slice(0, last + 1).map(d => ALIASES.some(a => d.toUpperCase().startsWith(a)) ? null : d.replace(/\*$/, ''))];
  if (q < path.length - 1 && RF_Q.includes(path[q])) hops.push(path[q + 1]);
  return hops.filter((h, i) => i === 0 || h !== hops[i - 1]);
}
