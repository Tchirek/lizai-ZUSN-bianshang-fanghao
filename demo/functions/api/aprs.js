// GET /api/aprs?site=ID: APRS stations heard within the hour around a summit, and the RF hops
// between them, as the station's activity.py keeps them. APRS-IS is a TCP feed with no history, so
// each ask listens a few seconds (receive-only login, range filter) and adds what it heard to what
// earlier asks heard, kept in the edge's cache.
import {connect} from 'cloudflare:sockets';
import sites from '../../sites.json';
import {parse, rfHops} from '../../lib/aprs.js';

const SERVER = {hostname: 'rotate.aprs2.net', port: 14580};
const RANGE_KM = 650;
const LISTEN_MS = 4000;
const FRESH_S = 20;                   // asks within this of the last listen get what it kept
const SHOW_S = 3600;
const LOGIN = 'INFLU';                // unverified: APRS-IS lets it read, never send

async function listen(site, keep) {
  const socket = connect(SERVER), writer = socket.writable.getWriter(), reader = socket.readable.getReader();
  const filter = `r/${site.lat.toFixed(2)}/${site.lon.toFixed(2)}/${RANGE_KM}`;
  await writer.write(new TextEncoder().encode(`user ${LOGIN} pass -1 vers influ-demo 0.1 filter ${filter}\r\n`));
  const decoder = new TextDecoder(), until = Date.now() + LISTEN_MS, now = Date.now() / 1000;
  let rest = '';
  try {
    while (Date.now() < until) {
      const got = await Promise.race([reader.read(), new Promise(resolve => setTimeout(resolve, until - Date.now()))]);
      if (!got || got.done) break;
      const lines = (rest + decoder.decode(got.value, {stream: true})).split('\n');
      rest = lines.pop();
      for (const line of lines) {
        const packet = parse(line.trim());
        if (!packet) continue;
        const hops = rfHops(packet.from, packet.path);
        hops.slice(1).forEach((hop, i) => { if (hop && hops[i]) keep.links[hops[i] + ' ' + hop] = now; });
        if ('lat' in packet) keep.stations[packet.name] = [+packet.lat.toFixed(4), +packet.lon.toFixed(4), hops.length > 1 ? 1 : 0, now];
      }
    }
  } finally {
    socket.close().catch(() => {});
  }
}

export async function onRequestGet({request}) {
  const site = sites.find(s => s.id === new URL(request.url).searchParams.get('site'));
  if (!site) return new Response('unknown site', {status: 404});
  const key = new Request(`https://influ-demo.cache/aprs/${site.id}`), cache = caches.default;
  const kept = await cache.match(key).then(r => r && r.json()).catch(() => null);
  let keep = kept || {t: 0, stations: {}, links: {}};
  if (Date.now() / 1000 - keep.t > FRESH_S) {
    try {
      await listen(site, keep);
    } catch (e) {
      // APRS-IS unreachable: what was kept
    }
    const now = Date.now() / 1000;
    keep = {t: now,
            stations: Object.fromEntries(Object.entries(keep.stations).filter(([, v]) => now - v[3] <= SHOW_S)),
            links: Object.fromEntries(Object.entries(keep.links).filter(([, t]) => now - t <= SHOW_S))};
    await cache.put(key, Response.json(keep, {headers: {'Cache-Control': `public, max-age=${SHOW_S}`}})).catch(() => {});
  }
  return Response.json({t: keep.t, stations: Object.entries(keep.stations).map(([call, v]) => [call, ...v]),
                        links: Object.keys(keep.links).map(k => k.split(' '))},
                       {headers: {'Cache-Control': 'no-store'}});
}
