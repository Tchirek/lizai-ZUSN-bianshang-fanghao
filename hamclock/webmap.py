"""Local tiles and map data for OpenLayers; projection and terrain maths stay shared."""
from functools import lru_cache
import io
import json
import math
from pathlib import Path
import threading
import time

import numpy as np
from PIL import Image

import activity as a
import build_tiles as tiles
import map_panel as panel
import map_view as maps


def collection(features):
    return dict(type='FeatureCollection', features=features)


class MapData:
    def __init__(self, base=None, model=None):
        self.base = base if base is not None else a.Map()
        self.model = Path(model or a.MODEL)
        self.tiles = self.model / 'tiles'
        self.metres = self.base.km_per_px * 1000
        self.extent = [-a.W / 2 * self.metres, -a.H / 2 * self.metres,
                       a.W / 2 * self.metres, a.H / 2 * self.metres]
        self.lock = threading.RLock()
        self.static_key = self.static_features = self.static_bounds = None
        self.night_minute = self.night_png = None

    def coordinate(self, pixel):
        x, y = pixel
        return [(float(x) - a.W / 2) * self.metres, (a.H / 2 - float(y)) * self.metres]

    def pixel(self, coordinate):
        x, y = coordinate
        return a.W / 2 + x / self.metres, a.H / 2 - y / self.metres

    def config(self):
        path = self.tiles / 'levels.json'
        info = json.loads(path.read_text())
        return dict(extent=self.extent, metres_per_pixel=self.metres,
                    levels=info['levels'], tile_size=info['tile'],
                    tile_version=path.stat().st_mtime_ns,
                    dem=[tiles.dem_metres(level, self.base.km_per_px, self.base.lat0) for level in info['levels']])

    def tile_path(self, level, column, row):
        info = self.config()
        if level not in info['levels'] or not 0 <= column < math.ceil(a.W * level / info['tile_size']) \
                or not 0 <= row < math.ceil(a.H * level / info['tile_size']):
            raise FileNotFoundError('tile outside map')
        return self.tiles / str(level) / f'{column}_{row}.png'

    def feature(self, geometry, coordinates, kind, name='', **properties):
        return dict(type='Feature', geometry=dict(type=geometry, coordinates=coordinates),
                    properties=dict(kind=kind, name=name, **properties))

    def point(self, pixel, kind, name='', **properties):
        return self.feature('Point', self.coordinate(pixel), kind, name, **properties)

    def line(self, pixels, kind, **properties):
        return self.feature('LineString', [self.coordinate(p) for p in pixels], kind, **properties)

    def static(self, bbox, zoom):
        with self.lock:
            data = maps.static_scene(self.base)
            admin_path = self.model / 'admin.json'
            key = data['key'], admin_path.stat().st_mtime_ns if admin_path.exists() else 0
            if key != self.static_key:
                features = []
                osm = json.loads((self.model / 'rangemap_osm.json').read_text())
                for river in tiles.rings([line['p'] for line in osm.get('lines', [])]):
                    if len(river) > 1:
                        features.append(self.line([self.base.xy(*p) for p in river], 'river'))
                if admin_path.exists():
                    for line in json.loads(admin_path.read_text()).get('lines', []):
                        features.append(self.line([self.base.xy(*p) for p in line['p']], 'boundary',
                                                  level=line['level'], min_zoom={4: 1, 5: 3, 6: 10}.get(line['level'], 1)))
                for name, pixel, distance, city in zip(data['place_names'], data['place_xy'],
                                                     data['place_km'], data['place_city']):
                    features.append(self.point(pixel, 'city' if city else 'town', name,
                                               min_zoom=1 if distance <= data['name_km'] else 2))
                for kind in ('campuses', 'military', 'government', 'regulators', 'embassies'):
                    for area in data[kind]:
                        minimum = {'campuses': 8, 'military': 4, 'regulators': 6, 'embassies': 6}.get(kind, area.get('zoom', 12))
                        properties = dict(min_zoom=minimum, country=area.get('code', ''))
                        for ring in area['rings']:
                            coordinates = [self.coordinate(p) for p in ring]
                            if coordinates and coordinates[-1] != coordinates[0]:
                                coordinates.append(coordinates[0])
                            features.append(self.feature('Polygon', [coordinates], kind, **properties))
                        features.append(self.point(area['point'], kind, area['name'], **properties))
                for airport in data['airports']:
                    features.append(self.point(airport['point'], 'airport', airport['text']))
                for runway in data['runways']:
                    features.append(self.line(runway['line'], 'runway', min_zoom=3,
                                              width=runway['width']))
                features.append(self.point(data['far'][0], 'far', data['far'][1]))
                features.append(self.point((a.W / 2, a.H / 2), 'station', 'DE', colour='#ffffff'))
                bounds = []
                for feature in features:
                    geometry = feature['geometry']
                    points = np.asarray(geometry['coordinates'], dtype=float).reshape(-1, 2)
                    bounds.append([*points.min(axis=0), *points.max(axis=0)])
                self.static_key, self.static_features = key, features
                self.static_bounds = np.asarray(bounds).reshape(-1, 4)
                self.geometry_at.cache_clear()
            bounds = self.static_bounds
            here = (bounds[:, 0] <= bbox[2]) & (bounds[:, 2] >= bbox[0]) \
                & (bounds[:, 1] <= bbox[3]) & (bounds[:, 3] >= bbox[1])
            level = 2 ** math.ceil(math.log2(zoom))
            features = [self.geometry_at(int(i), level) for i in np.flatnonzero(here)
                        if self.static_features[i]['properties'].get('min_zoom', 1) <= zoom]
            visible = []
            for feature in features:
                if feature is None:
                    continue
                geometry = feature['geometry']
                if geometry['type'] != 'LineString':
                    visible.append(feature)
                    continue
                coordinates = np.asarray(geometry['coordinates'])
                for start in range(0, len(coordinates) - 1, maps.Lines.PIECE - 1):
                    piece = coordinates[start:start + maps.Lines.PIECE]
                    lo, hi = piece.min(0), piece.max(0)
                    if lo[0] <= bbox[2] and hi[0] >= bbox[0] and lo[1] <= bbox[3] and hi[1] >= bbox[1]:
                        visible.append(dict(feature, geometry=dict(type='LineString', coordinates=piece.tolist())))
            return collection(visible)

    @lru_cache(maxsize=8192)
    def geometry_at(self, index, level):
        feature = self.static_features[index]
        geometry = feature['geometry']
        if geometry['type'] == 'Point':
            return feature
        def thin(points):
            return (maps.thin(np.asarray(points) / self.metres, level) * self.metres).tolist()
        if geometry['type'] == 'LineString':
            coordinates = thin(geometry['coordinates'])
        else:
            coordinates = [ring for ring in (thin(ring) for ring in geometry['coordinates']) if len(ring) >= 4]
            if not coordinates:
                return None
        return dict(feature, geometry=dict(type=geometry['type'], coordinates=coordinates))

    def live(self):
        data = json.loads((self.model / 'activity.json').read_text())
        stations, bands, chosen, degrees, total = a.digital_graph(self.base, data.get('reports', []))
        features = []
        for call, (x, y, colour, sent) in stations.items():
            name = call + (f' +{degrees[call]}' if degrees.get(call, 0) > 2 else '')
            features.append(self.point((x, y), 'digital', name, colour='#%02x%02x%02x' % colour))
        for path in chosen:
            features.append(self.line(path['ends'], 'digital_path', colour='#%02x%02x%02x' % path['colour'],
                                      local=len(path['local']) == 2))
        positions = data.get('aprs_positions') or data.get('aprs', {})
        for call, value in data.get('aprs', {}).items():
            features.append(self.point(self.base.xy(*value[:2]), 'aprs', call, rf=bool(value[2])))
        for first, second in data.get('aprs_links', []):
            if first in positions and second in positions:
                features.append(self.line([self.base.xy(*positions[p][:2]) for p in (first, second)], 'aprs_path'))
        for call, grid in data.get('qsos', []):
            latlon = a.locator(grid)
            if latlon:
                features.append(self.point(self.base.xy(*latlon), 'qso', call))
        elevation, parts = panel.sun_text(self.base.lat0, self.base.lon0, time.time())
        return dict(features=collection(features), sun=dict(elevation=elevation, parts=parts),
                    minute=int(time.time() // 60),
                    legend=dict(aprs=len(data.get('aprs', {})), digital=len(stations),
                                paths=len(chosen), total=total,
                                bands=[dict(name=name, colour='#%02x%02x%02x' % colour) for name, colour in sorted(bands)]))

    def grid(self, bbox, zoom):
        corners = [self.base.latlon(*self.pixel((x, y)))
                   for x in (bbox[0], (bbox[0] + bbox[2]) / 2, bbox[2])
                   for y in (bbox[1], (bbox[1] + bbox[3]) / 2, bbox[3])]
        south, north = max(-85, min(p[0] for p in corners) - .1), min(85, max(p[0] for p in corners) + .1)
        west, east = max(-180, min(p[1] for p in corners) - .1), min(180, max(p[1] for p in corners) + .1)
        features = []
        families = [(20, 10, 'field'), (2, 1, 'square')]
        if zoom >= 12:
            families.append((1 / 12, 1 / 24, 'subsquare'))
        lats = np.linspace(south, north, max(2, math.ceil((north - south) / .05)))
        lons = np.linspace(west, east, max(2, math.ceil((east - west) / .05)))
        for dlon, dlat, kind in families:
            for longitude in np.arange(math.ceil(west / dlon), math.floor(east / dlon) + 1) * dlon:
                features.append(self.line([self.base.xy(float(lat), float(longitude)) for lat in lats], kind))
            for latitude in np.arange(math.ceil(south / dlat), math.floor(north / dlat) + 1) * dlat:
                features.append(self.line([self.base.xy(float(latitude), float(lon)) for lon in lons], kind))
        for i in range(math.floor(west / 2), math.ceil(east / 2)):
            for j in range(math.floor(south), math.ceil(north)):
                features.append(self.point(self.base.xy(j + 1, i * 2), 'grid_name', maps.square_name(i, j)))
        if zoom >= 12:
            for i in range(math.floor(west * 12), math.ceil(east * 12)):
                for j in range(math.floor(south * 24), math.ceil(north * 24)):
                    name = chr(97 + i % 24) + chr(97 + j % 24)
                    features.append(self.point(self.base.xy((j + 1) / 24, i / 12), 'subgrid_name', name))
        return collection(features)

    def night(self):
        minute = int(time.time() // 60)
        with self.lock:
            if minute != self.night_minute:
                out = io.BytesIO()
                panel.night_layer(self.base, when=minute * 60, hud=False).save(out, format='PNG')
                self.night_png, self.night_minute = out.getvalue(), minute
            return self.night_png

    def reachability(self, x, y):
        if not self.extent[0] <= x <= self.extent[2] or not self.extent[1] <= y <= self.extent[3]:
            raise ValueError('point outside map')
        return self.profile(round(x, 1), round(y, 1), (self.model / 'rangemap.json').stat().st_mtime_ns)

    @lru_cache(maxsize=128)
    def profile(self, x, y, model_version):
        pixel = self.pixel((x, y))
        lat, lon = self.base.latlon(*pixel)
        result = panel.path_profile(self.base, lat, lon)
        # Keep each plot bucket's ground extremes; the status was computed at full DEM resolution.
        indices = {0, len(result['d']) - 1}
        for group in np.array_split(np.arange(len(result['d'])), min(160, len(result['d']))):
            indices.add(int(group[np.argmax(result['ground'][group])]))
            indices.add(int(group[np.argmin(result['ground'][group])]))
        indices = sorted(indices)
        calls, paths = panel.nearby(self.base, *pixel)
        return dict(lat=lat, lon=lon, distance=result['distance'], bearing=result['bearing'],
                    frequencies=panel.FREQUENCIES, heights=panel.HEIGHTS, status=result['status'],
                    clearance=result['clearance'], fspl=result['fspl'], our_agl=result['our_agl'],
                    dem_m=result['dem_m'], calls=calls, paths=paths,
                    d=result['d'][indices].tolist(), ground=result['ground'][indices].tolist(),
                    ray=result['ray'][indices].tolist())
