// GET /api/adsb?site=ID: the aircraft within the scope's widest range of a summit, from the open
// ADS-B exchanges (they send no CORS headers, so the page asks here). The exchanges rate-limit
// Cloudflare's shared addresses hard, so what one ask got is kept at the edge and given to the
// next asks: fresh for a few seconds, and while the exchanges refuse, for a few minutes more (its
// time says how old it is).
import sites from '../../sites.json';
import {SOURCES, rows} from '../../lib/adsb.js';

const NM = 45;                        // nautical miles: the scope's 50 mi
const FRESH_S = 10, KEEP_S = 300;
const HEADERS = {'User-Agent': 'influ-demo (+https://github.com/Tchirek/lizai-ZUSN-bianshang-fanghao)'};

export async function onRequestGet({request}) {
  const site = sites.find(s => s.id === new URL(request.url).searchParams.get('site'));
  if (!site) return new Response('unknown site', {status: 404});
  const key = new Request(`https://influ-demo.cache/adsb/${site.id}`), cache = caches.default;
  const kept = await cache.match(key).then(r => r && r.json()).catch(() => null), now = Date.now() / 1000;
  const reply = data => Response.json(data, {headers: {'Cache-Control': `public, max-age=${FRESH_S / 2}`}});
  if (kept && now - kept.t < FRESH_S) return reply(kept);
  const failed = [];
  for (const source of SOURCES) {
    try {
      const r = await fetch(source.url(site.lat, site.lon, NM), {headers: HEADERS, cf: {cacheTtlByStatus: {'200-299': FRESH_S, '400-599': 0}}});
      if (!r.ok) { failed.push(`${source.name} ${r.status}`); continue; }
      const found = {t: Math.floor(now), source: source.name, ...rows(source.list(await r.json()))};
      await cache.put(key, Response.json(found, {headers: {'Cache-Control': `public, max-age=${KEEP_S}`}})).catch(() => {});
      return reply(found);
    } catch (e) {
      failed.push(`${source.name} ${e.message}`);
    }
  }
  if (kept && now - kept.t < KEEP_S) return reply(kept);
  return new Response('no ADS-B exchange answered: ' + failed.join('; '), {status: 502});
}
