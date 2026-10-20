"""Real hedges and scrub from GeoDanmark, checked against the Danish height model.

GeoDanmark's Hegn covers living hedges, wire fences and walls alike, and most lines carry
no type ("Ikke tildelt"). A line typed "Levende" is a hedge, and other types are not. An
untyped line counts as a hedge only where the height model shows growth along it. A hedge
fills a 1.2 m band with growth above 0.7 m; a plank or wire fence barely shows. Without a
height model only typed hedges are kept. Hedges and scrub are cut back 1 m from buildings,
so Arnis grows them in gardens and never inside a building.
"""
from concurrent.futures import ThreadPoolExecutor
import hashlib
from http.client import HTTPException
import math
import os
import struct
import time
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

import numpy as np
from pyproj import Transformer
import shapely
from shapely import force_2d, from_wkt
from shapely.geometry import LineString, Polygon
from shapely.strtree import STRtree
from shapely.ops import transform, unary_union

from fetch import envelopes, request_page, unique
from prepare import active

HEDGE = 'Levende'
UNTYPED = 'Ikke tildelt'
# Measured on Slagelse villa streets: typed hedges fill about 0.8 of the band, wire
# fences 0.1. At 0.8 m cells the hedge/fence split agreed with 0.4 m on 94% of lines.
RESOLUTION_M = 0.8
BAND_HALF_WIDTH_M = 0.6
GROWTH_M = 0.7
HEDGE_SHARE = 0.5
BUILDING_CLEARANCE_M = 1.0
MIN_PIECE_M = 2.0
TILE_PX = 1000
WCS = 'https://api.dataforsyningen.dk/dhm_wcs_DAF'
# Seconds between tries of one tile; the service drops the odd request under load.
RETRY_DELAYS = (2, 4, 8)
# Tiles downloaded at the same time. One tile is a few seconds of waiting on the WCS;
# a 4 x 4 km town needs about 25 of them.
TILE_WORKERS = 4
TAG = 'geodanmark'


def load_token(path=None):
    """Dataforsyningen token for the height model, from DATAFORSYNINGEN_TOKEN or a .env file."""
    token = os.environ.get('DATAFORSYNINGEN_TOKEN', '').strip()
    if path is not None:
        values = dict(line.strip().split('=', 1) for line in Path(path).read_text(encoding='utf-8-sig').splitlines()
                      if line.strip() and not line.lstrip().startswith('#') and '=' in line)
        token = values.get('DATAFORSYNINGEN_TOKEN', '').strip().strip('"\'')
    if token and any(c.isspace() for c in token):
        raise ValueError('DATAFORSYNINGEN_TOKEN must not contain whitespace')
    return token or None


def download(entity, fields, wkt, at, key, transport=request_page):
    query = f'''query Rows($where: {entity}FilterInput!, $at: DafDateTime!, $after: String) {{
      {entity}(first: 100, after: $after, registreringstid: $at, virkningstid: $at, where: $where) {{
        nodes {{ {fields} }} pageInfo {{ hasNextPage endCursor }}
      }}
    }}'''
    where = {'geometri': {'intersects': {'wkt': wkt, 'crs': 25832}},
             'status': {'eq': 'Anlagt'}, 'geometristatus': {'eq': 'Endelig'}}
    cursor, cursors, rows = None, set(), []
    for _ in range(1000):
        doc = transport('GEODKV', query, {'where': where, 'at': at.isoformat(), 'after': cursor}, key)
        try:
            page = doc['data'][entity]
            nodes, info = page['nodes'], page['pageInfo']
        except (KeyError, TypeError):
            raise ValueError(f'{entity}: missing result or pagination metadata') from None
        rows.extend(nodes)
        if not info['hasNextPage']:
            return rows
        cursor = info.get('endCursor')
        if not cursor or cursor in cursors:
            raise ValueError(f'{entity}: pagination did not advance')
        cursors.add(cursor)
    raise ValueError(f'{entity}: page limit reached; reduce bbox')


def read_float_tiff(raw):
    """The single float32 band of an uncompressed, striped little-endian TIFF, as the
    height model's WCS delivers it. Anything else is refused rather than misread."""
    if raw[:4] != b'II*\x00':
        raise ValueError('Height model: expected a little-endian TIFF')
    (ifd,) = struct.unpack_from('<I', raw, 4)
    (count,) = struct.unpack_from('<H', raw, ifd)
    sizes = {1: 1, 3: 2, 4: 4}
    tags = {}
    for i in range(count):
        tag, kind, n, value = struct.unpack_from('<HHII', raw, ifd + 2 + 12 * i)
        size = sizes.get(kind)
        if size is None:
            continue
        fmt = {1: 'B', 3: 'H', 4: 'I'}[kind]
        offset = ifd + 2 + 12 * i + 8 if size * n <= 4 else value
        tags[tag] = list(struct.unpack_from('<' + fmt * n, raw, offset))
    width, height = tags[256][0], tags[257][0]
    if tags.get(259, [1])[0] != 1 or tags.get(258, [0])[0] != 32 or tags.get(339, [1])[0] != 3 \
            or tags.get(277, [1])[0] != 1 or 322 in tags:
        raise ValueError('Height model: expected an uncompressed float32 single-band TIFF')
    data = b''.join(raw[o:o + n] for o, n in zip(tags[273], tags[279]))
    if len(data) != width * height * 4:
        raise ValueError('Height model: truncated TIFF')
    return np.frombuffer(data, dtype='<f4').reshape(height, width)


class HeightModel:
    """Growth above the terrain (surface minus terrain), fetched in tiles as needed."""

    def __init__(self, token, opener=None, sleep=time.sleep):
        self.token = token
        self.opener = opener or self._open
        self.sleep = sleep
        self.tiles = {}
        self.size = TILE_PX * RESOLUTION_M

    def _open(self, params):
        url = WCS + '?' + urlencode({**params, 'token': self.token})
        with urlopen(Request(url, headers={'User-Agent': 'Arnis-DK'}), timeout=120) as response:
            return response.read(64 * 1024 * 1024)

    def _download(self, params):
        """One coverage tile, tried again on a dropped request. The final error names the
        HTTP status or a timeout, which the cloud runner then waits out; a refused token
        fails at once. Never includes the URL, which carries the token."""
        for attempt in range(len(RETRY_DELAYS) + 1):
            last = attempt == len(RETRY_DELAYS)
            try:
                return read_float_tiff(self.opener(params))
            except HTTPError as error:
                if error.code not in (429, 500, 502, 503, 504) or last:
                    raise ValueError(f'Height model: HTTP {error.code}') from None
            except (OSError, HTTPException):
                if last:
                    raise ValueError('Height model: connection failed or timed out') from None
            except ValueError:
                # An XML service exception instead of a tile, as the WCS sends under load.
                if last:
                    raise ValueError('Height model: service unavailable') from None
            self.sleep(RETRY_DELAYS[attempt])

    def _growth(self, tx, ty):
        left, bottom = tx * self.size, ty * self.size
        layers = []
        for coverage in ('dhm_overflade', 'dhm_terraen'):
            params = {'service': 'WCS', 'version': '1.0.0', 'request': 'GetCoverage',
                      'coverage': coverage, 'crs': 'EPSG:25832', 'format': 'GTiff',
                      'bbox': f'{left},{bottom},{left + self.size},{bottom + self.size}',
                      'width': TILE_PX, 'height': TILE_PX}
            layers.append(self._download(params))
        growth = layers[0] - layers[1]
        # Unset cells are large negative values in either model.
        growth[(layers[0] < -100) | (layers[1] < -100)] = np.nan
        return growth

    def _tile(self, tx, ty):
        if (tx, ty) not in self.tiles:
            self.tiles[(tx, ty)] = self._growth(tx, ty)
        return self.tiles[(tx, ty)]

    def prefetch(self, geometries):
        """Downloads every tile under `geometries`, TILE_WORKERS at a time, rather than
        one by one as sample() reaches them."""
        keys = set()
        for geometry in geometries:
            x0, y0, x1, y1 = geometry.bounds
            keys.update((tx, ty) for tx in range(math.floor(x0 / self.size), math.floor(x1 / self.size) + 1)
                        for ty in range(math.floor(y0 / self.size), math.floor(y1 / self.size) + 1))
        missing = sorted(keys - self.tiles.keys())
        with ThreadPoolExecutor(TILE_WORKERS) as pool:
            for key, growth in zip(missing, pool.map(lambda key: self._growth(*key), missing)):
                self.tiles[key] = growth

    def sample(self, xs, ys):
        out = np.full(len(xs), np.nan, dtype=np.float32)
        tx = np.floor(xs / self.size).astype(np.int64)
        ty = np.floor(ys / self.size).astype(np.int64)
        for key in set(zip(tx.tolist(), ty.tolist())):
            tile = self._tile(*key)
            m = (tx == key[0]) & (ty == key[1])
            col = ((xs[m] - key[0] * self.size) / RESOLUTION_M).astype(np.int64)
            row = TILE_PX - 1 - ((ys[m] - key[1] * self.size) / RESOLUTION_M).astype(np.int64)
            out[m] = tile[np.clip(row, 0, TILE_PX - 1), np.clip(col, 0, TILE_PX - 1)]
        return out


def growth_share(line, heights, buildings):
    """Share of the band along `line` that has growth, leaving out cells under buildings."""
    band = line.buffer(BAND_HALF_WIDTH_M)
    x0, y0, x1, y1 = band.bounds
    gx, gy = np.meshgrid(np.arange(x0, x1, RESOLUTION_M / 2), np.arange(y0, y1, RESOLUTION_M / 2))
    xs, ys = gx.ravel(), gy.ravel()
    inside = shapely.contains_xy(band, xs, ys)
    if buildings is not None:
        inside &= ~shapely.contains_xy(buildings, xs, ys)
    if inside.sum() < 3:
        return None
    values = heights.sample(xs[inside], ys[inside])
    values = values[~np.isnan(values)]
    if len(values) < 3:
        return None
    return float((values > GROWTH_M).mean())


def _pieces(geometry):
    if geometry.is_empty:
        return []
    return list(geometry.geoms) if hasattr(geometry, 'geoms') else [geometry]


def select(hedge_rows, scrub_rows, building_rows, at, heights=None):
    """Hedge lines and scrub polygons to grow, in EPSG:25832, with counts."""
    outlines = [force_2d(from_wkt(r['geometri'] if isinstance(r['geometri'], str) else r['geometri']['wkt']))
                for r in building_rows
                if active(r, 'Anlagt', at) and r.get('geometristatus') == 'Endelig'
                and r.get('metode3D') != 'Under terræn']
    footprints = unary_union(outlines) if outlines else None
    keep_out = footprints.buffer(BUILDING_CLEARANCE_M) if footprints is not None else None
    if footprints is not None:
        # A town has thousands of each. The point test gets a prepared index, and a
        # line or area is cut only by the clearance pieces it touches: same result,
        # without testing every hedge against every building in the area.
        shapely.prepare(footprints)
        clearance = _pieces(keep_out)
        nearby = STRtree(clearance)

        def cut(geometry):
            hits = nearby.query(geometry, predicate='intersects')
            return geometry.difference(unary_union([clearance[i] for i in hits])) if len(hits) else geometry
    counts = {'hedge_lines': 0, 'fence_lines': 0, 'untyped_kept': 0, 'untyped_dropped': 0,
              'untyped_unmeasured': 0}
    if heights is not None and hasattr(heights, 'prefetch'):
        heights.prefetch(force_2d(from_wkt(row['geometri']['wkt'])).buffer(BAND_HALF_WIDTH_M)
                         for row in hedge_rows if row.get('hegnstype') == UNTYPED)
    hedges = []
    for row in sorted(hedge_rows, key=lambda r: r['id_lokalId']):
        kind = row.get('hegnstype')
        line = force_2d(from_wkt(row['geometri']['wkt']))
        if kind == HEDGE:
            counts['hedge_lines'] += 1
        elif kind == UNTYPED:
            share = growth_share(line, heights, footprints) if heights is not None else None
            if share is None:
                counts['untyped_unmeasured'] += 1
                continue
            if share < HEDGE_SHARE:
                counts['untyped_dropped'] += 1
                continue
            counts['untyped_kept'] += 1
        else:
            counts['fence_lines'] += 1
            continue
        if keep_out is not None:
            line = cut(line)
        for i, piece in enumerate(_pieces(line)):
            if piece.geom_type == 'LineString' and piece.length >= MIN_PIECE_M:
                hedges.append((f"{row['id_lokalId']}/{i}", piece))
    scrub = []
    for row in sorted(scrub_rows, key=lambda r: r['id_lokalId']):
        area = force_2d(from_wkt(row['geometri']['wkt']))
        if keep_out is not None:
            area = cut(area)
        for i, piece in enumerate(_pieces(area)):
            if piece.geom_type == 'Polygon' and piece.area >= 4:
                scrub.append((f"{row['id_lokalId']}/{i}", piece))
    counts['hedge_pieces'] = len(hedges)
    counts['hedge_m'] = round(sum(line.length for _, line in hedges))
    counts['scrub_areas'] = len(scrub)
    return hedges, scrub, counts


def append_elements(supplement, hedges, scrub):
    """Adds hedges as barrier=hedge ways and scrub as natural=scrub areas."""
    to_ll = Transformer.from_crs(25832, 4326, always_xy=True)
    ids = {e['id'] for e in supplement['elements']}

    def osm_id(key):
        value = (1 << 60) | (int.from_bytes(hashlib.sha256(('hedge:' + key).encode()).digest()[:8], 'big')
                             & ((1 << 60) - 1))
        if value in ids:
            raise ValueError('Synthetic ID collision')
        ids.add(value)
        return value

    def way(key, coords, tags, closed):
        nodes = []
        points = list(coords)[:-1] if closed else list(coords)
        for index, (x, y) in enumerate(points):
            lon, lat = to_ll.transform(x, y)
            node = osm_id(f'{key}/node/{index}')
            nodes.append(node)
            supplement['elements'].append({'type': 'node', 'id': node, 'lon': lon, 'lat': lat})
        if closed:
            nodes.append(nodes[0])
        supplement['elements'].append({'type': 'way', 'id': osm_id(key), 'nodes': nodes, 'tags': tags})

    for key, line in hedges:
        way(key, line.coords, {'barrier': 'hedge', 'arnis:hedge': TAG}, False)
    for key, area in scrub:
        way(key, area.exterior.coords, {'natural': 'scrub', 'arnis:hedge': TAG}, True)


def add(supplement, bbox, building_rows, key, at, token=None, transport=request_page, opener=None):
    """Adds GeoDanmark's real hedges and scrub inside `bbox` to a prepared supplement."""
    fields = 'id_lokalId status geometristatus geometri { wkt crs }'
    tiles = envelopes(bbox)
    hedge_rows = unique([row for wkt in tiles for row in download(
        'GEODKV_Hegn', fields + ' hegnstype', wkt, at, key, transport)], 'GEODKV_Hegn')
    scrub_rows = unique([row for wkt in tiles for row in download(
        'GEODKV_KratBevoksning', fields, wkt, at, key, transport)], 'GEODKV_KratBevoksning')
    heights = HeightModel(token, opener) if token else None
    hedges, scrub, counts = select(hedge_rows, scrub_rows, building_rows, at, heights)
    append_elements(supplement, hedges, scrub)
    meta = supplement['arnis_dk']
    meta['hedges'] = {'height_model': bool(token), 'counts': counts}
    if token:
        meta['attribution'] += '; Danmarks Højdemodel (Klimadatastyrelsen)'
    return counts
