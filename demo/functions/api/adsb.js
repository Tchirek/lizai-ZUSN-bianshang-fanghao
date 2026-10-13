// GET /api/adsb?site=ID: the aircraft within the scope's widest range of a summit, from the open
// ADS-B exchanges (they send no CORS headers, so the page asks here); cached a few seconds.
import sites from '../../sites.json';
import {SOURCES, rows} from '../../lib/adsb.js';

const NM = 45;                        // nautical miles: the scope's 50 mi
const FRESH_S = 5;
const HEADERS = {'User-Agent': 'influ-demo (+https://github.com/Tchirek/lizai-ZUSN-bianshang-fanghao)'};

export async function onRequestGet({request}) {
  const site = sites.find(s => s.id === new URL(request.url).searchParams.get('site'));
  if (!site) return new Response('unknown site', {status: 404});
  for (const source of SOURCES) {
    try {
      const r = await fetch(source.url(site.lat, site.lon, NM), {headers: HEADERS, cf: {cacheTtl: FRESH_S, cacheEverything: true}});
      if (!r.ok) continue;
      const found = rows(source.list(await r.json()));
      return Response.json({t: Math.floor(Date.now() / 1000), source: source.name, ...found},
                           {headers: {'Cache-Control': `public, max-age=${FRESH_S}`}});
    } catch (e) {
      // the next exchange
    }
  }
  return new Response('no ADS-B exchange answered', {status: 502});
}
