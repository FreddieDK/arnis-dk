"""Facade colours for OSM buildings from the Danish oblique aerial photos (skråfoto).

Every Danish address is photographed from the air at 45 degrees, looking north, south, east
and west, at 10 cm. Each photo carries its camera: position and rotation in UTM 32 and DVR90,
focal length and pixel size. So a point on a facade can be projected straight into the photo,
with no structure from motion. This script does that for every street-side metre of every
OSM building in a bbox:

1. OSM building ways and multipolygons, with their node ids, from Overpass or from a local
   index of a Geofabrik extract (--build-osm-index, --osm-index); with --supplement also the
   Danish register's houses that Arnis adds beside them or puts in place of a block. Arnis
   matches facade walls to buildings by node id, and a world read from its OSM tile archive
   numbers the vertices by coordinate instead, so every wall names its nodes both ways.
2. Terrain and surface heights from DHM (Danmarks Højdemodel): the wall's foot is the
   terrain outside it, its top the surface just inside it.
3. For each wall, a cell per metre along it and per metre up it. A cell is seen by a photo
   when the photo looks at the wall's outside and the line to the camera clears the surface
   model, so the house across a narrow street hides the ground floors it really hides.
4. Each cell takes the colour of its pixels in the photo that sees the wall best; dark
   pixels on a light wall are windows. A cell the sun did not reach when its photo was
   taken (it faced away, or the height model stood in the way) is lifted to sunlight by
   one shade-to-sun ratio measured over the whole run.
5. Each roof takes the median colour of its footprint in the straight-down photo.

The output is the folder `--mapillary-facades-dir` reads (src/mapillary/facades.rs): one
JSON per building, per wall a PNG at 1 px = 1 m (RGB colour, alpha class) and an 8 px/m
texture for the photo panels, and the roof's colour. Arnis then colours those walls; at one
block per metre it takes the building colour and a colour per floor band, from two blocks
per metre also the windows.

Skråfoto and DHM are free data from Klimadatastyrelsen; both need a Dataforsyningen token
(DATAFORSYNINGEN_TOKEN, or --credentials-file).
"""
import argparse
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
import json
import math
import os
import struct
import sys
import threading
import time
import warnings
import zlib
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

import numpy as np
import shapely
from pyproj import Transformer
from shapely.geometry import Polygon

from hedges import HeightModel, load_token

STAC = 'https://api.dataforsyningen.dk/rest/skraafoto_api/v1.0'
OVERPASS = 'https://overpass-api.de/api/interpreter'
DIRECTIONS = ('north', 'south', 'east', 'west')
HEIGHT_RES_M = 0.4
HEIGHT_TILE_PX = 1000
TEX_PX_PER_M = 8
# Margin around the bbox for heights and photo windows: a ray from a facade to a camera
# 45 degrees up clears any Copenhagen roof within this distance.
MARGIN_M = 80.0
MIN_WALL_M = 1.0
MIN_WALL_HEIGHT_M = 2.0
MAX_WALL_HEIGHT_M = 60.0
# A photo has to look at the wall's outside, not along it: at 0.25 a wall seen nearly
# edge-on smeared one window over five metres.
MIN_FACING = 0.4
RAY_STEP_M = 0.4
# Surface model noise and eaves: a ray counts as blocked only this far below the surface.
RAY_CLEARANCE_M = 0.6
# The ray starts this far out from the wall, past most of the eave overhang.
RAY_START_M = 0.5
# Window pixels are this much darker than the wall around them.
WINDOW_DARK = 0.62
WINDOW_SHARE = 0.35
CHROMA_GAIN = 2.0
# Buildings whose walls measure the shade-to-sun gain of an area.
GAIN_SAMPLE = 100
TIER_A = 0.5
TIER_B = 0.2
CLASS_ALPHA = {'wall': 255, 'window': 192, 'door': 128, 'unknown': 64, 'none': 0}
# Mirrors of SYNTHETIC_ID_BASE and MAX_LAT_E7 in src/osm_tiles.rs.
TILE_ID_BASE = 1 << 61
MAX_LAT_E7 = 900_000_000
# Below BLOCK_HOUSE_INSIDE in src/danish_buildings.rs (0.5), so every house Arnis may put in
# place of a block is sampled; the others are as there.
BLOCK_HOUSE_INSIDE = 0.3
BLOCK_MIN_HOUSES = 3
BLOCK_HOUSE_MIN_AREA = 25.0
OUTBUILDINGS = {'shed', 'garage', 'garages', 'carport', 'greenhouse', 'roof', 'hut', 'outbuilding'}
TO_UTM = Transformer.from_crs(4326, 25832, always_xy=True)


def get_json(url, data=None, timeout=120):
    for attempt in range(4):
        try:
            request = Request(url, data=data, headers={'User-Agent': 'Arnis-DK'})
            with urlopen(request, timeout=timeout) as response:
                return json.loads(response.read())
        except HTTPError as error:
            if error.code not in (429, 500, 502, 503, 504) or attempt == 3:
                raise ValueError(f'HTTP {error.code} from {url.split("?")[0]}') from None
        except OSError:
            if attempt == 3:
                raise ValueError(f'No answer from {url.split("?")[0]}') from None
        time.sleep(2 ** (attempt + 1))


# --------------------------------------------------------------------------- OSM

def fetch_buildings(bbox):
    """Building ways and multipolygon relations in the bbox, with every node they use."""
    s, w, n, e = bbox
    query = (f'[out:json][timeout:120];(way["building"]({s},{w},{n},{e});'
             f'relation["building"]({s},{w},{n},{e}););out body;>;out skel qt;')
    return get_json(OVERPASS, data=urlencode({'data': query}).encode(), timeout=180)


def join_rings(ways):
    """Closed rings from the node lists of a relation's outer ways."""
    pending = [list(w) for w in ways if len(w) >= 2]
    rings = []
    while pending:
        ring = pending.pop(0)
        while ring[0] != ring[-1]:
            for i, way in enumerate(pending):
                if way[0] == ring[-1]:
                    ring += way[1:]
                elif way[-1] == ring[-1]:
                    ring += way[-2::-1]
                else:
                    continue
                pending.pop(i)
                break
            else:
                break
        if ring[0] == ring[-1] and len(ring) >= 4:
            rings.append(ring)
    return rings


def has_walls(tags):
    """A building with walls above ground: not a bare roof, not underground."""
    if 'building' not in tags or tags['building'] in ('roof', 'no'):
        return False
    if tags.get('location') == 'underground':
        return False
    try:
        return float(tags.get('layer', '0').split(';')[0]) >= 0
    except ValueError:
        return True


def tile_node_ids(lat, lon):
    """The ids Arnis' OSM tile archive gives the vertex at (lat, lon) instead of its OSM id:
    `coordinate_node_id` (src/osm_tiles.rs) of the coordinate in 1e-7 degrees, first as an
    AOT1 archive stores it (1e-6, rounded half away from zero, scaled up on read), then as
    an AOT2 one does. On Nyhavn the AOT1 ids matched 247 buildings, as many as the OSM ids
    match in an Overpass world; rounding half to even matched 241, truncating 78."""
    def e7(v):
        return int(round(v * 1e7))

    def via_e6(v):
        return ((v + 5) // 10 if v >= 0 else -((-v + 5) // 10)) * 10

    return [TILE_ID_BASE | ((la + MAX_LAT_E7) << 30) | (lo % (1 << 30))
            for la, lo in ((via_e6(e7(lat)), via_e6(e7(lon))), (e7(lat), e7(lon)))]


def buildings_from_osm(osm, tile_ids=True):
    """(owner, rings of node ids) per building, the node positions in UTM, and per node the
    ids the tile archive gives it (none with `tile_ids` off, for a source whose ids Arnis
    keeps as they are). A way building owns its own ring; a multipolygon owns the rings
    joined from its outer ways."""
    nodes = {}
    ways = {}
    relations = []
    for el in osm.get('elements', []):
        if el['type'] == 'node':
            nodes[el['id']] = (el['lon'], el['lat'])
        elif el['type'] == 'way':
            ways[el['id']] = el
        elif el['type'] == 'relation':
            relations.append(el)
    xs, ys = TO_UTM.transform(*zip(*nodes.values())) if nodes else ((), ())
    utm = {nid: (x, y) for nid, x, y in zip(nodes, xs, ys)}
    aliases = ({nid: tile_node_ids(lat, lon) for nid, (lon, lat) in nodes.items()}
               if tile_ids else {})

    out = []
    for way in ways.values():
        tags = way.get('tags', {})
        refs = way.get('nodes', [])
        if has_walls(tags) and len(refs) >= 4 and refs[0] == refs[-1]:
            out.append((('way', way['id']), [refs]))
    for rel in relations:
        if rel.get('tags', {}).get('type') != 'multipolygon' or not has_walls(rel.get('tags', {})):
            continue
        outer = [ways[m['ref']]['nodes'] for m in rel.get('members', [])
                 if m['type'] == 'way' and m.get('role') == 'outer' and m['ref'] in ways]
        rings = join_rings(outer)
        if rings:
            out.append((('relation', rel['id']), rings))
    return [(owner, [r for r in rings if all(n in utm for n in r)]) for owner, rings in out], utm, aliases


def build_osm_index(pbf, out):
    """Every OSM building way with walls in a .osm.pbf, as flat arrays in `out`: the way
    ids, each way's slice of the node arrays, the node ids and their coordinates. A
    country's worth loads in a second and is cut per area with `osm_from_index`."""
    import array
    import osmium

    class Handler(osmium.SimpleHandler):
        def __init__(self):
            super().__init__()
            self.ids, self.ends = array.array('q'), array.array('q')
            self.nodes, self.lat, self.lon = array.array('q'), array.array('d'), array.array('d')

        def way(self, w):
            if 'building' not in w.tags:
                return
            tags = {k: w.tags[k] for k in ('building', 'layer', 'location') if k in w.tags}
            if not has_walls(tags) or len(w.nodes) < 4 or w.nodes[0].ref != w.nodes[-1].ref:
                return
            try:
                coords = [(n.ref, n.lat, n.lon) for n in w.nodes]
            except osmium.InvalidLocationError:
                return
            for ref, lat, lon in coords:
                self.nodes.append(ref)
                self.lat.append(lat)
                self.lon.append(lon)
            self.ids.append(w.id)
            self.ends.append(len(self.nodes))

    handler = Handler()
    handler.apply_file(str(pbf), locations=True, idx='flex_mem')
    out = Path(out)
    out.mkdir(parents=True, exist_ok=True)
    ends = np.frombuffer(handler.ends, dtype=np.int64)
    lat = np.frombuffer(handler.lat, dtype=np.float64)
    lon = np.frombuffer(handler.lon, dtype=np.float64)
    starts = np.r_[0, ends[:-1]]
    for name, values in (('ids', np.frombuffer(handler.ids, dtype=np.int64)), ('ends', ends),
                         ('nodes', np.frombuffer(handler.nodes, dtype=np.int64)), ('lat', lat), ('lon', lon),
                         ('min_lat', np.minimum.reduceat(lat, starts)), ('max_lat', np.maximum.reduceat(lat, starts)),
                         ('min_lon', np.minimum.reduceat(lon, starts)), ('max_lon', np.maximum.reduceat(lon, starts))):
        np.save(out / f'{name}.npy', values)
    print(f'{len(ends)} building ways, {len(lat)} nodes in {out}')


def osm_from_index(index_dir, bbox):
    """The buildings of an OSM index that reach into `bbox`, as Overpass would answer."""
    d = Path(index_dir)
    get = lambda name: np.load(d / f'{name}.npy', mmap_mode='r')
    s, w, n, e = bbox
    hit = np.flatnonzero((get('max_lat') >= s) & (get('min_lat') <= n)
                         & (get('max_lon') >= w) & (get('min_lon') <= e))
    ids, ends, nodes, lat, lon = get('ids'), get('ends'), get('nodes'), get('lat'), get('lon')
    elements, seen = [], set()
    for i in hit.tolist():
        start = int(ends[i - 1]) if i else 0
        refs = nodes[start:int(ends[i])].tolist()
        for k, ref in enumerate(refs):
            if ref not in seen:
                seen.add(ref)
                elements.append({'type': 'node', 'id': ref, 'lat': float(lat[start + k]),
                                 'lon': float(lon[start + k])})
        elements.append({'type': 'way', 'id': int(ids[i]), 'nodes': refs, 'tags': {'building': 'yes'}})
    return {'elements': elements}


def footprints(owners, utm):
    """Each building's outer rings as one shapely geometry (UTM)."""
    out = []
    for _, rings in owners:
        polys = [Polygon([utm[nid] for nid in ring]) for ring in rings]
        out.append(shapely.make_valid(shapely.union_all(polys)) if polys else None)
    return out


def in_area(owners, utm, bounds):
    """The buildings whose outline reaches into `bounds` (UTM)."""
    x0, y0, x1, y1 = bounds
    keep = []
    for owner, rings in owners:
        pts = np.array([utm[nid] for ring in rings for nid in ring])
        if len(pts) and pts[:, 0].max() >= x0 and pts[:, 0].min() <= x1                 and pts[:, 1].max() >= y0 and pts[:, 1].min() <= y1:
            keep.append((owner, rings))
    return keep


def without_overlaps(extra, extra_utm, base, base_utm):
    """The `extra` buildings that overlap none of `base` with a positive area: the rule by
    which Arnis adds a Danish register building next to the OSM ones."""
    base_shapes = [g for g in footprints(base, base_utm) if g is not None and not g.is_empty]
    tree = shapely.STRtree(base_shapes)
    keep = []
    for (owner, rings), shape in zip(extra, footprints(extra, extra_utm)):
        if shape is None or shape.is_empty:
            continue
        if not any(shape.intersection(base_shapes[i]).area > 0.01 for i in tree.query(shape)):
            keep.append((owner, rings))
    return keep


def in_blocks(extra, extra_utm, base, base_utm, outbuildings=frozenset()):
    """The `extra` buildings lying mostly inside a `base` building that holds at least
    BLOCK_MIN_HOUSES houses of them (`outbuildings` and small ones not counted): a block
    drawn as one outline, which Arnis may replace by its register houses (`blocks` in
    src/danish_buildings.rs). Arnis makes that call with stricter thresholds, so these are
    sampled as well as the block, and its walls have facades either way."""
    base_shapes = footprints(base, base_utm)
    valid = [i for i, g in enumerate(base_shapes) if g is not None and not g.is_empty]
    tree = shapely.STRtree([base_shapes[i] for i in valid])
    members = {}
    for (owner, rings), shape in zip(extra, footprints(extra, extra_utm)):
        if shape is None or shape.is_empty or shape.area <= 0:
            continue
        shared = [(shape.intersection(base_shapes[valid[k]]).area, valid[k]) for k in tree.query(shape)]
        if shared:
            area, block = max(shared)
            if area >= BLOCK_HOUSE_INSIDE * shape.area:
                main = owner not in outbuildings and shape.area >= BLOCK_HOUSE_MIN_AREA
                members.setdefault(block, []).append(((owner, rings), main))
    return [house for houses in members.values() if sum(main for _, main in houses) >= BLOCK_MIN_HOUSES
            for house, _ in houses]


# --------------------------------------------------------------------------- heights

class Heights(HeightModel):
    """Terrain and surface heights in DVR90 at 0.4 m, fetched in tiles as needed. Only the
    download and its retries come from the hedge model."""

    def __init__(self, token):
        super().__init__(token)
        self.size = HEIGHT_TILE_PX * HEIGHT_RES_M

    def _tile(self, tx, ty):
        if (tx, ty) not in self.tiles:
            left, bottom = tx * self.size, ty * self.size
            layers = []
            for coverage in ('dhm_terraen', 'dhm_overflade'):
                layer = self._download({
                    'service': 'WCS', 'version': '1.0.0', 'request': 'GetCoverage',
                    'coverage': coverage, 'crs': 'EPSG:25832', 'format': 'GTiff',
                    'bbox': f'{left},{bottom},{left + self.size},{bottom + self.size}',
                    'width': HEIGHT_TILE_PX, 'height': HEIGHT_TILE_PX}).astype(np.float32)
                layer[layer < -100] = np.nan
                layers.append(layer)
            self.tiles[(tx, ty)] = np.stack(layers)
        return self.tiles[(tx, ty)]

    def sample(self, xs, ys, layer):
        """Layer 0 is the terrain, 1 the surface."""
        xs, ys = np.asarray(xs, dtype=np.float64), np.asarray(ys, dtype=np.float64)
        out = np.full(xs.shape, np.nan, dtype=np.float32)
        tx = np.floor(xs / self.size).astype(np.int64)
        ty = np.floor(ys / self.size).astype(np.int64)
        for key in set(zip(tx.ravel().tolist(), ty.ravel().tolist())):
            tile = self._tile(*key)[layer]
            m = (tx == key[0]) & (ty == key[1])
            col = ((xs[m] - key[0] * self.size) / HEIGHT_RES_M).astype(np.int64)
            row = HEIGHT_TILE_PX - 1 - ((ys[m] - key[1] * self.size) / HEIGHT_RES_M).astype(np.int64)
            out[m] = tile[np.clip(row, 0, HEIGHT_TILE_PX - 1), np.clip(col, 0, HEIGHT_TILE_PX - 1)]
        return out


# --------------------------------------------------------------------------- photos

class Photo:
    """One oblique photo: its camera, and windows of its pixels read on demand."""

    def __init__(self, item):
        p = item['properties']
        io = p['pers:interior_orientation']
        self.id = item['id']
        self.direction = p['direction']
        self.datetime = p['datetime']
        self.url = item['assets']['data']['href']
        self.f = io['focal_length']
        self.pix = io['pixel_spacing'][0]
        self.cols, self.rows = io['sensor_array_dimensions']
        self.ppo = io.get('principal_point_offset') or [0.0, 0.0]
        self.centre = np.array(p['pers:perspective_center'], dtype=np.float64)
        self.rotation = np.array(p['pers:rotation_matrix'], dtype=np.float64).reshape(3, 3)
        # Unit vector (east, north, up) towards the sun when the photo was taken.
        self.sun = None
        self._levels = None

    def project(self, x, y, z):
        """Pixel column and row of world points (UTM 32, DVR90), by the collinearity
        equations. The principal point offset is where the principal point sits from the
        sensor's middle, in millimetres; the side-looking cameras have 6.68 mm of it."""
        d = np.stack([np.asarray(x) - self.centre[0], np.asarray(y) - self.centre[1],
                      np.asarray(z) - self.centre[2]])
        u = np.tensordot(self.rotation, d, axes=1)
        xm = -self.f * u[0] / u[2] + self.ppo[0]
        ym = -self.f * u[1] / u[2] + self.ppo[1]
        return self.cols / 2 + xm / self.pix, self.rows / 2 - ym / self.pix

    def levels(self):
        """Per channel, the photo's 0.5th and 99.5th percentiles, from its overview. Haze
        lifts the dark end and tints it blue, which made every wall in the first trial
        read paler and colder than it is; these stretch it back. Taken over the whole photo
        so that neighbouring buildings, read through separate windows, match."""
        if self._levels is None:
            cached = LEVELS_CACHE / f'{self.id}.json' if LEVELS_CACHE else None
            if cached and cached.exists():
                lo, hi = json.loads(cached.read_text(encoding='utf-8'))
                self._levels = (np.array(lo, dtype=np.float32), np.array(hi, dtype=np.float32))
                return self._levels
            src = open_photo(self.url)
            shape = (3, max(1, int(self.rows) // 32), max(1, int(self.cols) // 32))
            small = read_retrying(src, out_shape=shape).reshape(3, -1).T.astype(np.float32)
            self._levels = (np.percentile(small, 0.5, axis=0), np.percentile(small, 99.5, axis=0))
            if cached:
                # A photo spans about 1.4 by 1 km, so neighbouring areas share most of
                # theirs; its overview is read once per machine, not once per area.
                tmp = cached.with_name(f'{cached.name}.{os.getpid()}.tmp')
                tmp.write_text(json.dumps([self._levels[0].tolist(), self._levels[1].tolist()]), encoding='utf-8')
                os.replace(tmp, cached)
        return self._levels

    def view(self, bounds, zmin, zmax, pad=16):
        """The pixels that show `bounds` (UTM) between the two heights, levelled, or None
        when that part of the ground is not in the photo, or the photo cannot be read: a
        photo that fails is skipped for the rest of the run (see skip_photo), not the
        building."""
        if photo_skipped(self.url):
            return None
        try:
            return self._view(bounds, zmin, zmax, pad)
        except Exception as error:
            skip_photo(self, error)
            return None

    def _view(self, bounds, zmin, zmax, pad):
        x0, y0, x1, y1 = bounds
        gx, gy, gz = np.meshgrid([x0, x1], [y0, y1], [zmin, zmax])
        c, r = self.project(gx.ravel(), gy.ravel(), gz.ravel())
        c0 = int(max(0, math.floor(c.min()) - pad))
        r0 = int(max(0, math.floor(r.min()) - pad))
        c1 = int(min(self.cols, math.ceil(c.max()) + pad))
        r1 = int(min(self.rows, math.ceil(r.max()) + pad))
        if c1 - c0 < 4 or r1 - r0 < 4:
            return None
        lo, hi = self.levels()
        source = open_photo(self.url)
        # Imported after open_photo, which needs rasterio itself: without it, as in CI, a
        # photo that cannot be opened is still the error that is logged.
        from rasterio.windows import Window
        data = read_retrying(source, window=Window(c0, r0, c1 - c0, r1 - r0))
        image = np.clip((np.moveaxis(data, 0, -1).astype(np.float32) - lo)
                        / np.maximum(hi - lo, 1.0) * 255.0, 0, 255)
        return PhotoView(self, image, (c0, r0))


class PhotoView:
    """A window of one photo's pixels, sampled through that photo's camera."""

    def __init__(self, photo, image, origin):
        self.photo = photo
        self.image = image
        self.origin = origin
        self.id = photo.id
        self.direction = photo.direction
        self.centre = photo.centre
        self.sun = photo.sun

    def sample(self, x, y, z):
        """Bilinear RGB at world points; NaN outside the window."""
        c, r = self.photo.project(x, y, z)
        c = c - self.origin[0] - 0.5
        r = r - self.origin[1] - 0.5
        h, w = self.image.shape[:2]
        inside = (c >= 0) & (r >= 0) & (c < w - 1) & (r < h - 1)
        out = np.full(c.shape + (3,), np.nan, dtype=np.float32)
        ci, ri = np.floor(c[inside]).astype(np.int64), np.floor(r[inside]).astype(np.int64)
        fc, fr = (c[inside] - ci)[:, None], (r[inside] - ri)[:, None]
        img = self.image
        out[inside] = (img[ri, ci] * (1 - fc) * (1 - fr) + img[ri, ci + 1] * fc * (1 - fr)
                       + img[ri + 1, ci] * (1 - fc) * fr + img[ri + 1, ci + 1] * fc * fr)
        return out


_OPEN = OrderedDict()
_OPEN_LOCK = threading.Lock()
OPEN_PHOTOS = 512
# With --readers above 1, a building's five photos are read side by side. It did not pay
# off: eight samplers with five readers each on one address went from about 5 to 1
# building a second each, as the photo server spaced out their requests.
_READERS = None
# Folder of each photo's levels, shared by the runs on one machine (--levels-cache).
LEVELS_CACHE = None
# Tries at opening or reading a photo before it is skipped. When the photo service
# answered 404 for files it holds, every further try only cost the building's neighbours
# their turn; the next best photo of the direction serves the building instead.
PHOTO_ATTEMPTS = 1
# Photos that failed those tries, by URL, with why: skipped for the rest of the run, so
# no other building waits on them again. PhotoIndex.best passes them over, and the
# building takes the next best photo of that direction.
_SKIPPED = {}


def photo_skipped(url):
    with _OPEN_LOCK:
        return url in _SKIPPED


def skip_photo(photo, error):
    """Marks `photo` unreadable for the rest of the run and logs it, once."""
    reason = f'{type(error).__name__}: {error}'[:160]
    with _OPEN_LOCK:
        if photo.url in _SKIPPED:
            return
        _SKIPPED[photo.url] = (photo.id, reason)
    print(f'  photo {photo.id} skipped for the rest of this run: {reason}', flush=True)


def skipped_photos():
    """The photos skipped so far, as {id: reason}."""
    with _OPEN_LOCK:
        return dict(_SKIPPED.values())


def open_photo(url):
    """A shared, open dataset per photo, the least recently used closed past OPEN_PHOTOS.
    GDAL's block cache then serves neighbouring buildings' windows without a new download.
    Opening one costs its header over HTTP, about half a second; a 2 km square uses
    some 300 photos, and with room for only 48 a sampler spent most of its time
    opening them again."""
    import rasterio

    with _OPEN_LOCK:
        if url in _OPEN:
            _OPEN.move_to_end(url)
            return _OPEN[url]
    for attempt in range(PHOTO_ATTEMPTS):
        try:
            src = rasterio.open(url)
            break
        except Exception:
            if attempt == PHOTO_ATTEMPTS - 1:
                raise
            time.sleep(2 ** (attempt + 1))
    with _OPEN_LOCK:
        _OPEN[url] = src
        while len(_OPEN) > OPEN_PHOTOS:
            _OPEN.popitem(last=False)[1].close()
    return src


def read_retrying(src, **kwargs):
    for attempt in range(PHOTO_ATTEMPTS):
        try:
            return src.read(indexes=[1, 2, 3], **kwargs)
        except Exception:
            if attempt == PHOTO_ATTEMPTS - 1:
                raise
            time.sleep(2 ** (attempt + 1))


def oklab_from_srgb(rgb):
    c = np.asarray(rgb, dtype=np.float32) / 255.0
    c = np.where(c <= 0.04045, c / 12.92, ((c + 0.055) / 1.055) ** 2.4)
    lms = np.cbrt(c @ np.array([[0.4122214708, 0.2119034982, 0.0883024619],
                                [0.5363325363, 0.6806995451, 0.2817188376],
                                [0.0514459929, 0.1073969566, 0.6299787005]], dtype=np.float32))
    return lms @ np.array([[0.2104542553, 1.9779984951, 0.0259040371],
                           [0.7936177850, -2.4285922050, 0.7827717662],
                           [-0.0040720468, 0.4505937099, -0.8086757660]], dtype=np.float32)


def srgb_from_oklab(lab):
    lms = lab @ np.array([[1.0, 1.0, 1.0],
                          [0.3963377774, -0.1055613458, -0.0894841775],
                          [0.2158037573, -0.0638541728, -1.2914855480]], dtype=np.float32)
    c = (lms ** 3) @ np.array([[4.0767416621, -1.2684380046, -0.0041960863],
                               [-3.3077115913, 2.6097574011, -0.7034186147],
                               [0.2309699292, -0.3413193965, 1.7076147010]], dtype=np.float32)
    c = np.clip(c, 0.0, 1.0)
    return np.where(c <= 0.0031308, c * 12.92, 1.055 * c ** (1 / 2.4) - 0.055) * 255.0


def restore_chroma(rgb):
    """Gives the facade colours back the saturation the air took. Measured on Nyhavn's
    canal front: the photo's most saturated facade pixels reach 0.054 OkLab chroma,
    about half what its ochre and red paint has, so every wall read as grey stone once
    Arnis matched it to a block. Lightness and hue stay; greys stay grey."""
    lab = oklab_from_srgb(rgb)
    lab[..., 1:] *= CHROMA_GAIN
    return srgb_from_oklab(lab)


def sun_direction(when, lat, lon):
    """Unit vector (east, north, up) towards the sun at an ISO time (UTC), by NOAA's
    approximation, good to a fraction of a degree."""
    t = datetime.fromisoformat(when.replace('Z', '+00:00')).astimezone(timezone.utc)
    hour = t.hour + t.minute / 60 + t.second / 3600
    g = 2 * math.pi / 365 * (t.timetuple().tm_yday - 1 + (hour - 12) / 24)
    eqtime = 229.18 * (0.000075 + 0.001868 * math.cos(g) - 0.032077 * math.sin(g)
                       - 0.014615 * math.cos(2 * g) - 0.040849 * math.sin(2 * g))
    decl = (0.006918 - 0.399912 * math.cos(g) + 0.070257 * math.sin(g) - 0.006758 * math.cos(2 * g)
            + 0.000907 * math.sin(2 * g) - 0.002697 * math.cos(3 * g) + 0.00148 * math.sin(3 * g))
    ha = math.radians((hour * 60 + eqtime + 4 * lon) / 4 - 180)
    phi = math.radians(lat)
    elevation = math.asin(math.sin(phi) * math.sin(decl) + math.cos(phi) * math.cos(decl) * math.cos(ha))
    azimuth = math.atan2(math.sin(ha), math.cos(ha) * math.sin(phi) - math.tan(decl) * math.cos(phi)) + math.pi
    return np.array([math.sin(azimuth) * math.cos(elevation), math.cos(azimuth) * math.cos(elevation),
                     math.sin(elevation)])


def to_linear(rgb):
    c = np.asarray(rgb, dtype=np.float32) / 255.0
    return np.where(c <= 0.04045, c / 12.92, ((c + 0.055) / 1.055) ** 2.4)


def to_srgb(lin):
    c = np.clip(lin, 0.0, 1.0)
    return np.where(c <= 0.0031308, c * 12.92, 1.055 * c ** (1 / 2.4) - 0.055) * 255.0


class PhotoIndex:
    """Every photo of a collection over an area, by direction. A 2 km square of
    Copenhagen has about 300 of them; each building takes, per direction, the one that
    shows it nearest the middle of the frame."""

    def __init__(self, collection, bbox, token):
        s, w, n, e = bbox
        query = urlencode({'bbox': f'{w},{s},{e},{n}',
                           'bbox-crs': 'http://www.opengis.net/def/crs/OGC/1.3/CRS84',
                           'limit': 1000, 'token': token})
        url = f'{STAC}/collections/{collection}/items?{query}'
        lat, lon = (s + n) / 2, (w + e) / 2
        self.by_direction = {d: [] for d in DIRECTIONS + ('nadir',)}
        seen = set()
        while url:
            page = get_json(url)
            for item in page.get('features', []):
                photo = Photo(item)
                if photo.direction in self.by_direction and photo.id not in seen:
                    seen.add(photo.id)
                    photo.sun = sun_direction(photo.datetime, lat, lon)
                    self.by_direction[photo.direction].append(photo)
            url = next((link['href'] for link in page.get('links', [])
                        if link.get('rel') == 'next' and link.get('method', 'GET') == 'GET'), None)
            if url and 'token=' not in url:
                url += ('&' if '?' in url else '?') + urlencode({'token': token})

    def __len__(self):
        return sum(len(v) for v in self.by_direction.values())

    def best(self, direction, x, y, z):
        """The photo of `direction` that shows (x, y, z) nearest its middle, or None.
        Photos skipped as unreadable are passed over."""
        best, best_d = None, 0.45
        for photo in self.by_direction[direction]:
            if photo_skipped(photo.url):
                continue
            c, r = photo.project(np.array([x]), np.array([y]), np.array([z]))
            d = max(abs(c[0] / photo.cols - 0.5), abs(r[0] / photo.rows - 0.5))
            if d < best_d:
                best, best_d = photo, d
        return best


# --------------------------------------------------------------------------- walls

def signed_area(points):
    x, y = points[:, 0], points[:, 1]
    return 0.5 * float(np.sum(x * np.roll(y, -1) - np.roll(x, -1) * y))


def visible(heights, px, py, pz, normal, camera, zmax):
    """Whether the straight line from each point to the camera clears the surface model.
    The points sit on one wall; the line starts just outside it."""
    dx, dy, dz = camera[0] - px, camera[1] - py, camera[2] - pz
    horiz = np.hypot(dx, dy)
    ux, uy = dx / horiz, dy / horiz
    slope = dz / horiz
    sx = px + normal[0] * RAY_START_M
    sy = py + normal[1] * RAY_START_M
    reach = float(np.max((zmax - pz) / np.maximum(slope, 0.05)))
    steps = np.arange(0.0, max(reach, RAY_STEP_M) + RAY_STEP_M, RAY_STEP_M)
    rx = sx[:, None] + ux[:, None] * steps[None, :]
    ry = sy[:, None] + uy[:, None] * steps[None, :]
    rz = pz[:, None] + slope[:, None] * (steps[None, :] + RAY_START_M)
    surface = heights.sample(rx, ry, 1)
    blocked = np.nan_to_num(surface, nan=-1e9) > rz + RAY_CLEARANCE_M
    return ~blocked.any(axis=1)


def sample_wall(a, b, normal, heights, photos, zmax):
    """The wall from `a` to `b` (UTM) as the photos show it, before any colour correction,
    or None when they see too little of it. Every texture pixel keeps which photo it came
    from and whether the sun reached its cell when that photo was taken."""
    length = float(np.hypot(*(b - a)))
    t = (b - a) / length
    cols = int(math.ceil(length))
    along = np.minimum(np.arange(cols) + 0.5, length - 0.05)
    foot = heights.sample(a[0] + t[0] * along + normal[0] * 2.0, a[1] + t[1] * along + normal[1] * 2.0, 0)
    if np.all(np.isnan(foot)):
        return None
    base = float(np.nanmedian(foot))
    # The wall's top per metre column: the highest surface just inside it, across the
    # column. A column with nothing standing there is no wall, whatever OSM says. OSM
    # outlines sit a metre off the height model often enough that 0.7 m in alone cut
    # the top off one column in three, so the search reaches 1.5 m in and each column
    # takes the highest of itself and its neighbours.
    tops = np.full(cols, np.nan, dtype=np.float32)
    for inward in (0.7, 1.5):
        for off in (-0.3, 0.0, 0.3):
            s = np.clip(along + off, 0.0, length)
            tops = np.fmax(tops, heights.sample(a[0] + t[0] * s - normal[0] * inward,
                                                a[1] + t[1] * s - normal[1] * inward, 1))
    col_h = np.nan_to_num(tops - base, nan=0.0)
    padded = np.pad(col_h, 1, mode='edge')
    col_h = np.maximum(np.maximum(padded[:-2], padded[1:-1]), padded[2:])
    standing = col_h >= MIN_WALL_HEIGHT_M
    if not standing.any():
        return None
    wall_h = float(np.percentile(col_h[standing], 85))
    if wall_h > MAX_WALL_HEIGHT_M:
        return None
    rows = int(math.ceil(wall_h))

    # Cell centres, row 0 at the top as the export has it.
    cc, rr = np.meshgrid(np.arange(cols) + 0.5, np.arange(rows) + 0.5)
    col_of = np.floor(cc.ravel()).astype(np.int64)
    s = np.minimum(cc.ravel(), length - 0.05)
    up = rows - rr.ravel()
    cx = a[0] + t[0] * s + normal[0] * 0.05
    cy = a[1] + t[1] * s + normal[1] * 0.05
    cz = base + up
    # A cell over open ground, or above its column's top, has no wall to photograph.
    on_wall = standing[col_of] & (up - 0.5 < col_h[col_of])

    # Each photo's view of the wall: how squarely it looks at it, and which cells it sees.
    views = []
    for k, photo in enumerate(photos):
        if photo.direction == 'nadir':
            continue
        to_cam = photo.centre[:2] - (a + b) / 2
        facing = float(np.dot(normal, to_cam / np.linalg.norm(to_cam)))
        if facing < MIN_FACING:
            continue
        seen = on_wall & visible(heights, cx, cy, cz, normal, photo.centre, zmax)
        if seen.any():
            views.append((facing * seen.sum(), k, seen))
    if not views:
        return None
    views.sort(key=lambda v: -v[0])
    owner = np.full(cols * rows, -1, dtype=np.int64)
    for _, k, seen in views:
        owner[(owner < 0) & seen] = k

    # Sunlit cells: when its photo was taken, the wall faced the sun and the line to the
    # sun cleared the surface model.
    lit = np.zeros(cols * rows, dtype=bool)
    mid = np.array([(a[0] + b[0]) / 2, (a[1] + b[1]) / 2, base])
    for k in set(owner[owner >= 0].tolist()):
        sun = photos[k].sun
        if sun[2] <= 0 or float(np.dot(normal, sun[:2])) <= 0.02:
            continue
        mine = owner == k
        lit[mine] = visible(heights, cx[mine], cy[mine], cz[mine], normal, mid + sun * 50000.0, zmax)

    # The texture, each cell from the photo that owns it.
    n = TEX_PX_PER_M
    tc, tr = np.meshgrid((np.arange(cols * n) + 0.5) / n, (np.arange(rows * n) + 0.5) / n)
    tex_s = np.minimum(tc.ravel(), length - 0.01)
    tx = a[0] + t[0] * tex_s + normal[0] * 0.05
    ty = a[1] + t[1] * tex_s + normal[1] * 0.05
    tz = base + rows - tr.ravel()
    cell_of = (np.floor(tr.ravel()).astype(np.int64) * cols + np.floor(tc.ravel()).astype(np.int64))
    tex = np.full((tx.size, 3), np.nan, dtype=np.float32)
    px_owner = owner[cell_of]
    for k in set(owner[owner >= 0].tolist()):
        m = px_owner == k
        tex[m] = photos[k].sample(tx[m], ty[m], tz[m])

    valid = ~np.isnan(tex).any(axis=1)
    n_seen = valid.reshape(rows, n, cols, n).sum(axis=(1, 3)).ravel()
    seen_share = float((n_seen >= n * n // 4).mean())
    if seen_share < TIER_B:
        return None
    return {
        'length': length, 'cols': cols, 'rows': rows, 'base': base, 'height': wall_h,
        'seen': seen_share, 'tex': tex, 'lit': lit[cell_of],
        'photos': sorted({photos[k].id for k in set(owner[owner >= 0].tolist())}),
    }


def shadow_gain(samples):
    """Per channel, in linear light, how much brighter the sunlit facade pixels are than
    the shaded ones over every wall of the run: the ratio of their medians. A shaded wall
    is lit by the blue sky alone; in the first trial that turned Charlottenborg's red
    brick, on the shaded side of Nyhavn, into dark violet grey."""
    if not samples:
        # No wall came out of the spread (a cell of one shed seen in no photo): nothing to lift.
        return np.ones(3, dtype=np.float32), 0, 0
    sun, shade = [], []
    for w in samples:
        valid = ~np.isnan(w['tex']).any(axis=1)
        sun.append(w['tex'][valid & w['lit']][::3])
        shade.append(w['tex'][valid & ~w['lit']][::3])
    sun, shade = np.concatenate(sun), np.concatenate(shade)
    if len(sun) < 5000 or len(shade) < 5000:
        return np.ones(3, dtype=np.float32), len(sun), len(shade)
    gain = np.median(to_linear(sun), axis=0) / np.maximum(np.median(to_linear(shade), axis=0), 1e-4)
    return np.clip(gain, 1.0, 6.0).astype(np.float32), len(sun), len(shade)


def finish_wall(w, gain):
    """Cells and texture of a sampled wall: the shade lifted to sunlight by `gain`, the
    saturation the air took given back, and windows told from wall."""
    rows, cols, n = w['rows'], w['cols'], TEX_PX_PER_M
    tex = w['tex'].copy()
    valid = ~np.isnan(tex).any(axis=1)
    shaded = valid & ~w['lit']
    if shaded.any():
        tex[shaded] = to_srgb(to_linear(tex[shaded]) * gain)
    tex[valid] = restore_chroma(tex[valid])

    # Cells: windows are dark against the wall they sit in; a wall cell takes the median of
    # its light pixels, so its windows do not darken it.
    lum = tex @ np.array([0.299, 0.587, 0.114], dtype=np.float32)
    wall_lum = float(np.nanpercentile(lum[valid], 60)) if valid.any() else 0.0
    # Texture pixels grouped by cell: (cell, pixel in cell).
    per_cell = tex.reshape(rows, n, cols, n, 3).transpose(0, 2, 1, 3, 4).reshape(rows * cols, n * n, 3)
    cell_lum = lum.reshape(rows, n, cols, n).transpose(0, 2, 1, 3).reshape(rows * cols, n * n)
    seen_px = ~np.isnan(cell_lum)
    dark = seen_px & (cell_lum < WINDOW_DARK * wall_lum)
    n_seen, n_dark = seen_px.sum(axis=1), dark.sum(axis=1)
    light_px = np.where((seen_px & ~dark)[..., None], per_cell, np.nan)
    with np.errstate(all='ignore'), warnings.catch_warnings():
        warnings.simplefilter('ignore', RuntimeWarning)
        colour = np.where(((n_seen - n_dark) >= n_seen // 5)[:, None],
                          np.nanmedian(light_px, axis=1), np.nanmedian(per_cell, axis=1))
    window = (wall_lum > 60) & (n_dark >= WINDOW_SHARE * n_seen)
    enough = n_seen >= n * n // 4
    cells = np.zeros((cols * rows, 4), dtype=np.uint8)
    cells[enough, :3] = np.clip(np.nan_to_num(colour[enough]), 0, 255).round().astype(np.uint8)
    cells[enough, 3] = np.where(window[enough], CLASS_ALPHA['window'], CLASS_ALPHA['wall'])

    texture = np.zeros((tex.shape[0], 4), dtype=np.uint8)
    texture[valid, :3] = np.clip(tex[valid], 0, 255).round().astype(np.uint8)
    texture[valid, 3] = 255
    return cells.reshape(rows, cols, 4), texture.reshape(rows * n, cols * n, 4)


def roof_colour(ring, utm, heights, photo):
    """The roof's colour from the straight-down photo: the median of the pixels over the
    footprint, both slopes, with the saturation restored. Without it the roof took the
    facade's block, so a white house got a white roof. The sunlit slope alone, tried
    first, is washed out in the photo and turned Nyhavn's red tiles pale pink."""
    poly = Polygon([utm[nid] for nid in ring]).buffer(-0.8)
    if poly.is_empty:
        return None
    x0, y0, x1, y1 = poly.bounds
    gx, gy = np.meshgrid(np.arange(x0, x1, 0.5), np.arange(y0, y1, 0.5))
    inside = shapely.contains_xy(poly, gx, gy)
    xs, ys = gx[inside], gy[inside]
    if xs.size < 8:
        return None
    top = heights.sample(xs, ys, 1)
    ground = heights.sample(xs, ys, 0)
    on_roof = np.nan_to_num(top - ground, nan=0.0) > MIN_WALL_HEIGHT_M
    if on_roof.sum() < 8:
        return None
    rgb = photo.sample(xs[on_roof], ys[on_roof], top[on_roof])
    rgb = rgb[~np.isnan(rgb).any(axis=1)]
    if len(rgb) < 8:
        return None
    return restore_chroma(np.median(rgb, axis=0)[None])[0]


# --------------------------------------------------------------------------- output

def write_png(path, rgba):
    """An 8-bit RGBA PNG, without an imaging library (the venv has none)."""
    h, w = rgba.shape[:2]
    raw = b''.join(b'\x00' + rgba[row].tobytes() for row in range(h))

    def chunk(kind, data):
        body = kind + data
        return struct.pack('>I', len(data)) + body + struct.pack('>I', zlib.crc32(body) & 0xFFFFFFFF)

    Path(path).write_bytes(b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', struct.pack('>IIBBBBB', w, h, 8, 6, 0, 0, 0))
                           + chunk(b'IDAT', zlib.compress(raw, 6)) + chunk(b'IEND', b''))


def sample_building(rings, utm, heights, index):
    """Every wall of one building that the photos see, sampled but not yet coloured, and
    the straight-down view of its roof. Each direction's photo is the one that shows the
    building nearest its middle, read through a window around the building alone."""
    pts = np.array([utm[nid] for ring in rings for nid in ring], dtype=np.float64)
    x0, y0 = pts.min(axis=0) - 1.5
    x1, y1 = pts.max(axis=0) + 1.5
    gx, gy = np.meshgrid(np.arange(x0, x1 + 2.0, 2.0), np.arange(y0, y1 + 2.0, 2.0))
    ground = heights.sample(gx, gy, 0)
    top = heights.sample(gx, gy, 1)
    if np.all(np.isnan(ground)) or np.all(np.isnan(top)):
        return [], None
    zmin, ztop = float(np.nanmin(ground)) - 2.0, float(np.nanmax(top)) + 2.0
    # The highest surface the line to a camera could meet; 45 degrees up, it has cleared
    # anything standing further off than its height.
    reach = max(30.0, ztop - zmin + 10.0)
    hx, hy = np.meshgrid(np.arange(x0 - reach, x1 + reach, 4.0), np.arange(y0 - reach, y1 + reach, 4.0))
    zmax = float(np.nanmax(heights.sample(hx, hy, 1))) + 1.0
    cx, cy = (x0 + x1) / 2, (y0 + y1) / 2

    def view_of(direction):
        # The best photo of the direction; one that cannot be read is skipped, and the next
        # best tried in its place.
        for _ in range(3):
            photo = index.best(direction, cx, cy, (zmin + ztop) / 2)
            if photo is None:
                return None
            view = photo.view((x0, y0, x1, y1), zmin, ztop)
            if not photo_skipped(photo.url):
                return view
        return None

    if _READERS:
        pending = [_READERS.submit(view_of, d) for d in DIRECTIONS + ('nadir',)]
        views = [v for v in (f.result() for f in pending) if v is not None]
    else:
        views = [v for v in (view_of(d) for d in DIRECTIONS + ('nadir',)) if v is not None]
    oblique = [v for v in views if v.direction != 'nadir']
    walls = []
    for ring in rings:
        ring_pts = np.array([utm[nid] for nid in ring], dtype=np.float64)
        # UTM has y north, so a positive area runs anticlockwise and the outside is right.
        ccw = signed_area(ring_pts[:-1]) > 0
        for i in range(len(ring) - 1):
            a, b = ring_pts[i], ring_pts[i + 1]
            d = b - a
            length = float(np.hypot(*d))
            if length < MIN_WALL_M:
                continue
            normal = np.array([d[1], -d[0]]) / length if ccw else np.array([-d[1], d[0]]) / length
            sampled = sample_wall(a, b, normal, heights, oblique, zmax)
            if sampled is not None:
                walls.append((ring[i], ring[i + 1], sampled))
    nadir = next((v for v in views if v.direction == 'nadir'), None)
    roof = roof_colour(rings[0], utm, heights, nadir) if walls and nadir is not None else None
    return walls, roof


def wall_edges(node_a, node_b, length, aliases):
    """The wall's edge once per id scheme: OSM's own ids for a world read from Overpass,
    the tile archive's for one read from it. Arnis places the one whose ids are on the
    building's ring and passes over the others."""
    pairs = [(node_a, node_b)] + list(zip(aliases.get(node_a, []), aliases.get(node_b, [])))
    edges = []
    for a, b in pairs:
        if a != b and not any(e['node_a'] == a and e['node_b'] == b for e in edges):
            edges.append({'node_a': a, 'node_b': b, 's0': 0.0, 's1': length})
    return edges


def write_building(out, owner, walls, roof, gain, aliases, textures=True):
    kind, osm_id = owner
    prefix = f'{"w" if kind == "way" else "r"}{osm_id}'
    records = []
    colours = []
    for node_a, node_b, w in walls:
        cells, texture = finish_wall(w, gain)
        key = f'{prefix}_{len(records)}'
        write_png(out / f'{key}.png', cells)
        record = {
            'key': key, 'cols': w['cols'], 'rows': w['rows'],
            'tier': 'A' if w['seen'] >= TIER_A else 'B',
            'png': f'{key}.png', 'extent': {'s_l': 0.0},
            'edges': wall_edges(node_a, node_b, w['length'], aliases),
            'seen_share': round(w['seen'], 3), 'height_m': round(w['height'], 2),
            'skraafoto': w['photos'],
        }
        # The photo panels need the 8 px/m texture; a One World build, which places
        # blocks only, does not, and it is most of an export's size.
        if textures:
            write_png(out / f'{key}_tex.png', texture)
            record['tex'] = f'{key}_tex.png'
        records.append(record)
        flat = cells.reshape(-1, 4)
        colours.append(flat[flat[:, 3] == CLASS_ALPHA['wall'], :3])
    every = np.concatenate(colours) if colours else np.zeros((0, 3))
    record = {'kind': kind, 'way_id': osm_id if kind == 'way' else None, 'walls': records,
              'source': 'skraafoto'}
    if kind == 'relation':
        record['relation_id'] = osm_id
    if len(every):
        record['building_colour'] = {'rgb': np.median(every, axis=0).round().astype(int).tolist()}
    if roof is not None:
        record['roof_colour'] = {'rgb': np.clip(roof, 0, 255).round().astype(int).tolist()}
    (out / f'{prefix}.json').write_text(json.dumps(record, indent=1), encoding='utf-8')
    return len(records)


def load_buildings(args, bbox, bounds):
    """The buildings of the area: OSM's (from an index, a saved Overpass answer or
    Overpass itself) and the Danish register's that Arnis would add beside them."""
    if args.osm_index:
        osm = osm_from_index(args.osm_index, bbox)
    elif args.osm_cache and args.osm_cache.exists():
        osm = json.loads(args.osm_cache.read_text(encoding='utf-8'))
    else:
        osm = fetch_buildings(bbox)
        if args.osm_cache:
            args.osm_cache.write_text(json.dumps(osm), encoding='utf-8')
    owners, utm, aliases = buildings_from_osm(osm)
    owners = in_area(owners, utm, bounds)
    n_osm = len(owners)
    if args.supplement:
        supplement = json.loads(Path(args.supplement).read_text(encoding='utf-8'))
        extra, extra_utm, _ = buildings_from_osm(supplement, tile_ids=False)
        extra = in_area(extra, extra_utm, bounds)
        outbuildings = {(el['type'], el['id']) for el in supplement.get('elements', [])
                        if el.get('tags', {}).get('building') in OUTBUILDINGS}
        extra = (without_overlaps(extra, extra_utm, owners, utm)
                 + in_blocks(extra, extra_utm, owners, utm, outbuildings))
        owners += extra
        utm.update(extra_utm)
    return owners, utm, aliases, n_osm


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    p.add_argument('--bbox', help='min_lat,min_lng,max_lat,max_lng, as Arnis takes it')
    p.add_argument('--out', type=Path, required=True, help='Folder for --mapillary-facades-dir')
    p.add_argument('--collection', default='skraafotos2025')
    p.add_argument('--credentials-file', type=Path)
    p.add_argument('--osm-cache', type=Path, help='Keep the Overpass answer here and reuse it')
    p.add_argument('--osm-index', type=Path, help='Read OSM buildings from an index made by --build-osm-index')
    p.add_argument('--supplement', type=Path,
                   help="An arnis-dk Danish building supplement: its buildings that no OSM building "
                        "overlaps get facades too")
    p.add_argument('--no-textures', action='store_true',
                   help='Leave out the 8 px/m textures, which only the photo panels use')
    p.add_argument('--build-osm-index', type=Path, metavar='PBF',
                   help='Index the building ways of this .osm.pbf into --out and exit')
    p.add_argument('--levels-cache', type=Path,
                   help="Keep each photo's levels in this folder and reuse them across runs")
    p.add_argument('--readers', type=int, default=1, help="Photos read at once per building")
    args = p.parse_args(argv)
    global LEVELS_CACHE, _READERS
    if args.readers > 1:
        _READERS = ThreadPoolExecutor(max_workers=args.readers)
    if args.levels_cache:
        args.levels_cache.mkdir(parents=True, exist_ok=True)
        LEVELS_CACHE = args.levels_cache

    if args.build_osm_index:
        build_osm_index(args.build_osm_index, args.out)
        return
    if not args.bbox:
        p.error('--bbox is required')
    token = load_token(args.credentials_file)
    if not token:
        sys.exit('A Dataforsyningen token is needed: DATAFORSYNINGEN_TOKEN or --credentials-file')
    # GDAL reads these as it opens the photos: their COGs are read window by window
    # over HTTP, and its block cache serves neighbouring buildings the same tiles.
    for key, value in (('GDAL_DISABLE_READDIR_ON_OPEN', 'EMPTY_DIR'), ('CPL_VSIL_CURL_ALLOWED_EXTENSIONS', '.tif'),
                       ('GDAL_HTTP_MULTIRANGE', 'YES'), ('GDAL_HTTP_MERGE_CONSECUTIVE_RANGES', 'YES'),
                       ('GDAL_CACHEMAX', '1024'), ('VSI_CACHE', 'TRUE'), ('VSI_CACHE_SIZE', '134217728'),
                       ('GDAL_HTTP_MAX_RETRY', str(PHOTO_ATTEMPTS - 1)), ('GDAL_HTTP_RETRY_DELAY', '2')):
        os.environ.setdefault(key, value)
    s, w, n, e = (float(v) for v in args.bbox.split(','))
    args.out.mkdir(parents=True, exist_ok=True)
    started = time.time()

    xs, ys = TO_UTM.transform([w, e, w, e], [s, s, n, n])
    bounds = (min(xs), min(ys), max(xs), max(ys))
    owners, utm, aliases, n_osm = load_buildings(args, (s, w, n, e), bounds)
    print(f'{len(owners)} buildings ({n_osm} OSM, {len(owners) - n_osm} register)', flush=True)
    # Neighbours one after another, in 200 m strips: they share photos, height tiles and
    # the photos' cached blocks.
    def place(item):
        x, y = utm[item[1][0][0]]
        return (int((bounds[3] - y) // 200), x if int((bounds[3] - y) // 200) % 2 == 0 else -x)
    owners.sort(key=place)
    if not owners:
        write_manifest(args, (s, w, n, e), [], None, 0, 0, started)
        return

    index = PhotoIndex(args.collection, (s, w, n, e), token)
    print(f'{len(index)} photos', flush=True)
    heights = Heights(token)

    # The shade-to-sun gain is measured over an even spread of the area's buildings, kept
    # in memory; everything else is written as it is sampled.
    step = max(1, len(owners) // GAIN_SAMPLE)
    sampled = {}
    for i in range(0, len(owners), step):
        try:
            sampled[i] = sample_building(owners[i][1], utm, heights, index)
        except Exception as error:
            print(f'  {owners[i][0]}: {type(error).__name__}: {error}', flush=True)
    gain, n_sun, n_shade = shadow_gain([w for walls, _ in sampled.values() for _, _, w in walls])
    print(f'shade to sun gain (linear R G B) {gain.round(2).tolist()} from {n_sun} sunlit and '
          f'{n_shade} shaded pixels', flush=True)

    walls_total = buildings_done = failed = 0
    used = set()
    for i, (owner, rings) in enumerate(owners):
        try:
            walls, roof = sampled.pop(i) if i in sampled else sample_building(rings, utm, heights, index)
            if walls:
                walls_total += write_building(args.out, owner, walls, roof, gain, aliases,
                                              textures=not args.no_textures)
                buildings_done += 1
                used.update(pid for _, _, w in walls for pid in w['photos'])
        except Exception as error:
            failed += 1
            print(f'  {owner}: {type(error).__name__}: {error}', flush=True)
        if (i + 1) % 500 == 0:
            print(f'  {i + 1}/{len(owners)} buildings, {walls_total} walls, '
                  f'{time.time() - started:.0f} s', flush=True)
    skipped = skipped_photos()
    write_manifest(args, (s, w, n, e), sorted(used), gain, buildings_done, walls_total, started, failed, skipped)
    if skipped:
        print(f'{len(skipped)} photos skipped as unreadable: {", ".join(sorted(skipped))}', flush=True)
    print(f'{buildings_done} of {len(owners)} buildings got {walls_total} facade walls in {args.out} '
          f'({failed} failed, {time.time() - started:.0f} s)')


def write_manifest(args, bbox, photos, gain, buildings, walls, started, failed=0, skipped=None):
    manifest = {
        'source': 'skraafoto', 'collection': args.collection, 'bbox': list(bbox),
        'photos': photos, 'shadow_gain': None if gain is None else gain.round(3).tolist(),
        'buildings': buildings, 'walls': walls, 'failed': failed,
        # Photos the service would not serve during the run, {id: why}: their buildings were
        # sampled from the other photos, or the next best of the direction.
        'skipped_photos': skipped or {},
        'seconds': round(time.time() - started, 1),
        'attribution': 'Skråfoto og Danmarks Højdemodel: Klimadatastyrelsen. Bygninger: OpenStreetMap-bidragydere'
                       + (', GeoDanmark' if args.supplement else '') + '.',
        'credits': [],
    }
    (args.out / 'manifest.json').write_text(json.dumps(manifest, indent=1, ensure_ascii=False), encoding='utf-8')


if __name__ == '__main__':
    main()
