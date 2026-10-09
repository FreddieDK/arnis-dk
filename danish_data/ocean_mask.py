"""Prepare an OSM coastline land mask and conservatively select coastal/land cells."""
import argparse
import hashlib
import json
import math
from pathlib import Path
import zipfile

from pyproj import CRS, Transformer
from shapely.geometry import box, mapping, shape
from shapely.ops import transform
from shapely.strtree import STRtree

SCHEMA = 'arnis-dk.land-mask/1'
SOURCE_URL = 'https://osmdata.openstreetmap.de/download/land-polygons-split-4326.zip'
TO_METRES = Transformer.from_crs(4326, 25832, always_xy=True)
TO_WGS84 = Transformer.from_crs(25832, 4326, always_xy=True)


def parse_bbox(value):
    b = list(map(float, value.split(',')))
    if len(b) != 4 or not all(math.isfinite(v) for v in b):
        raise ValueError('Expected south,west,north,east')
    s, w, n, e = b
    if not (50 <= s < n <= 62 and 0 <= w < e <= 25):
        raise ValueError('Land-mask coverage must be around Denmark (50..62 N, 0..25 E)')
    return b


def encloses(outer, inner):
    return outer[0] <= inner[0] and outer[1] <= inner[1] and outer[2] >= inner[2] and outer[3] >= inner[3]


def prepare(archive, bounds, output):
    """Stream the full-resolution split WGS84 land layer, clipping only for storage."""
    import shapefile
    from meld_bridge import atomic_json
    archive, output = Path(archive), Path(output)
    if output.exists():
        raise ValueError('Choose a new land-mask output file')
    s, w, n, e = bounds
    clip = box(w, s, e, n)
    features = []
    with zipfile.ZipFile(archive) as source:
        candidates = [name for name in source.namelist() if name.endswith('/land_polygons.shp') or name == 'land_polygons.shp']
        if len(candidates) != 1:
            raise ValueError('Expected exactly one land_polygons.shp in the OSM land archive')
        member = candidates[0]
        crs = CRS.from_wkt(source.read(member[:-4] + '.prj').decode('utf-8'))
        if not crs.equals(CRS.from_epsg(4326), ignore_axis_order=True):
            raise ValueError('Use the WGS84 / EPSG:4326 land archive')
        with source.open(member) as stream, shapefile.Reader(shp=stream) as reader:
            if reader.shapeType != shapefile.POLYGON:
                raise ValueError('Land source must contain polygons')
            for record in reader.iterShapes(bbox=(w, s, e, n)):
                geometry = shape(record.__geo_interface__)
                if not geometry.is_valid:
                    raise ValueError('Invalid coastline polygon; no partial mask written')
                # Coastline polygons are land, including inland lakes. Keep holes too:
                # an inland lake must never be mistaken for open sea.
                parts = list(geometry.geoms) if geometry.geom_type == 'MultiPolygon' else [geometry]
                for polygon in parts:
                    from shapely.geometry import Polygon
                    clipped = Polygon(polygon.exterior).intersection(clip)
                    if clipped.is_empty or clipped.area == 0:
                        continue
                    # A polygon touching the clipping rectangle can also produce
                    # zero-area lines/points beside its polygon pieces.
                    pieces = list(clipped.geoms) if clipped.geom_type == 'GeometryCollection' else [clipped]
                    for piece in pieces:
                        if piece.geom_type not in ('Polygon', 'MultiPolygon'):
                            if piece.area != 0:
                                raise ValueError('Unexpected clipped coastline geometry')
                            continue
                        metric = transform(TO_METRES.transform, piece)
                        if not metric.is_valid:
                            # Projection can turn a touching ring into a tiny
                            # self-intersection. Keep its complete envelope instead
                            # of risking the loss of any land when repairing it.
                            metric = box(*TO_METRES.transform_bounds(*piece.bounds, densify_pts=21))
                        features.append({'type': 'Feature', 'properties': {}, 'geometry': mapping(metric)})
            # Consume any trailing bytes so ZIP's CRC check also runs on the whole layer.
            while stream.read(1024 * 1024):
                pass
    with archive.open('rb') as stream:
        checksum = hashlib.file_digest(stream, 'sha256').hexdigest()
    output.parent.mkdir(parents=True, exist_ok=True)
    atomic_json(output, {'type': 'FeatureCollection', 'schema': SCHEMA, 'crs_epsg': 25832,
                        'coverage_bbox': bounds, 'features': features,
                        'source': {'url': SOURCE_URL, 'sha256': checksum,
                                   'attribution': 'OpenStreetMap contributors', 'license': 'ODbL-1.0'}})
    print(f'Prepared {len(features)} land polygons: {output}')


class LandMask:
    def __init__(self, path):
        document = json.loads(Path(path).read_text(encoding='utf-8'))
        if document.get('schema') != SCHEMA or document.get('crs_epsg') != 25832:
            raise ValueError('Unsupported land mask; prepare it with ocean_mask.py')
        self.coverage = parse_bbox(','.join(map(str, document['coverage_bbox'])))
        self.land = [shape(f['geometry']) for f in document['features']]
        if any(g.is_empty or not g.is_valid or g.geom_type not in ('Polygon', 'MultiPolygon')
               or not all(math.isfinite(v) for v in g.bounds) for g in self.land):
            raise ValueError('Invalid land geometry; refusing to classify ocean')
        self.tree = STRtree(self.land)

    def is_open_sea(self, bounds, coast_buffer_m):
        """Only true when the ENTIRE padded cell is known and away from all land."""
        if not math.isfinite(coast_buffer_m) or not 0 <= coast_buffer_m <= 10000:
            raise ValueError('Coast buffer must be between 0 and 10000 metres')
        s, w, n, e = bounds
        region = box(*TO_METRES.transform_bounds(w, s, e, n, densify_pts=21))
        # Square envelope is conservative, including corners outside the round buffer.
        x0, y0, x1, y1 = region.bounds
        search = box(x0-coast_buffer_m, y0-coast_buffer_m, x1+coast_buffer_m, y1+coast_buffer_m)
        west, south, east, north = TO_WGS84.transform_bounds(*search.bounds, densify_pts=21)
        if not encloses(self.coverage, [south, west, north, east]):
            return False  # Unknown coverage is retained, never interpreted as ocean.
        return len(self.tree.query(search, predicate='intersects')) == 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--archive', type=Path, required=True, help='OSM land-polygons-split-4326.zip')
    parser.add_argument('--bbox', type=parse_bbox, default=parse_bbox('53,6,59,17'), help='Coverage to keep, south,west,north,east')
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    try:
        prepare(args.archive, args.bbox, args.output)
    except (ValueError, OSError, KeyError, zipfile.BadZipFile) as error:
        parser.exit(1, f'Land mask: {error}\n')


if __name__ == '__main__':
    main()
