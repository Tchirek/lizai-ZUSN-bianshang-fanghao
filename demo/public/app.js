// The showcase: the station as if it stood on one of a few summits. Each visit starts from one of
// them, never the last visit's, and stays there; ?site=ID asks for one.
import {useFonts} from './tft.js';
import {useGlyphs} from './search.js';
import {start} from './clock.js';
import {startMap} from './map.js';

const LAST = 'influ-demo-last-site';

function choose(sites) {
  const asked = new URLSearchParams(location.search).get('site'), found = sites.find(s => s.id === asked);
  if (found) return found;
  let last = null;
  try { last = localStorage.getItem(LAST); } catch (e) { /* no storage: any summit */ }
  const others = sites.filter(s => s.id !== last), site = others[Math.floor(Math.random() * others.length)];
  try { localStorage.setItem(LAST, site.id); } catch (e) { /* not kept */ }
  return site;
}

const json = url => fetch(url).then(r => { if (!r.ok) throw Error(url); return r.json(); });
const [sites, glyphs] = await Promise.all([json('data/sites.json'), json('data/glyphs.json')]);
const chosen = choose(sites), reach = await json(`data/${chosen.id}/reach.json`);
const site = {...chosen, lat: reach.lat, lon: reach.lon};        // the summit's top, as its viewshed has it
useFonts(glyphs);
useGlyphs(glyphs);
document.title = `HamClock · ${site.name}`;
start(document.querySelector('#clock'), site);
startMap(site, reach);
