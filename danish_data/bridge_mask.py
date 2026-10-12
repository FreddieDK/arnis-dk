"""Preserve mapped road/rail bridges when open-sea cells are omitted."""
import json
import math
import time
import urllib.parse
import urllib.request
from pathlib import Path

from shapely.geometry import LineString, Polygon, box
from shapely.ops import transform
from shapely.strtree import STRtree

from ocean_mask import TO_METRES, encloses

ENDPOINTS = ('https://overpass-api.de/api/interpreter',
             'https://overpass.kumi.systems/api/interpreter')
BUFFER_METRES = 2000


def is_bridge(tags):
    return (tags.get('man_made') == 'bridge' or
            (tags.get('bridge', 'no') not in ('no', 'false', '0') and
             bool(tags.get('highway') or tags.get('railway'))))


def fetch_document(bounds):
    """Fail closed on unavailable/incomplete Overpass data; never drop bridges silently."""
    s, w, n, e = bounds
    area = f'{s},{w},{n},{e}'
    query = (f'[out:json][timeout:180];('
             f'way[highway][bridge][bridge!="no"]({area});'
             f'way[railway][bridge][bridge!="no"]({area});'
             f'way[man_made=bridge]({area}););out geom;')
    body = urllib.parse.urlencode({'data': query}).encode()
    errors = []
    for attempt in range(4):
        endpoint = ENDPOINTS[attempt % len(ENDPOINTS)]
        try:
            request = urllib.request.Request(endpoint, data=body,
                headers={'User-Agent': 'Arnis-DK bridge corridor planner',
                         'Content-Type': 'application/x-www-form-urlencoded'})
            with urllib.request.urlopen(request, timeout=210) as response:
                raw = response.read(64 * 1024 * 1024 + 1)
            if len(raw) > 64 * 1024 * 1024:
                raise ValueError('Bridge response exceeds 64 MiB')
            document = json.loads(raw)
            if document.get('remark') or not isinstance(document.get('elements'), list):
                raise ValueError('Incomplete bridge response')
            document['arnis_dk_bridge_coverage'] = list(bounds)
            document['arnis_dk_bridge_source'] = endpoint
            BridgeMask(document, bounds)  # Validate before storing the snapshot.
            return document
        except (OSError, ValueError) as error:
            errors.append(str(error))
            if attempt < 3:
                time.sleep(5 * (attempt + 1))
    raise ValueError('Unable to fetch bridge corridors: ' + '; '.join(errors))


class BridgeMask:
    def __init__(self, source, bounds, buffer_metres=BUFFER_METRES):
        if not math.isfinite(buffer_metres) or not 0 < buffer_metres <= 10000:
            raise ValueError('Bridge buffer must be positive and at most 10000 metres')
        document = (json.loads(Path(source).read_text(encoding='utf-8'))
                    if isinstance(source, (str, Path)) else source)
        coverage = document.get('arnis_dk_bridge_coverage')
        if (not coverage or len(coverage) != 4 or
                not all(math.isfinite(v) for v in coverage) or
                not encloses(coverage, bounds)):
            raise ValueError('Bridge snapshot must cover the entire planned bbox')
        if document.get('remark') or not isinstance(document.get('elements'), list):
            raise ValueError('Incomplete bridge snapshot')
        corridors = []
        self.ids = []
        for element in document['elements']:
            tags = element.get('tags', {})
            if element.get('type') != 'way' or not is_bridge(tags):
                continue
            points = [(p['lon'], p['lat']) for p in element.get('geometry', [])]
            if (len(points) < 2 or any(not math.isfinite(x) or not math.isfinite(y)
                                      or not -180 <= x <= 180 or not -90 <= y <= 90
                                      for x, y in points)):
                raise ValueError('Missing/invalid bridge geometry')
            geometry = (Polygon(points) if tags.get('man_made') == 'bridge'
                        and len(points) >= 4 and points[0] == points[-1]
                        else LineString(points))
            if geometry.is_empty or not geometry.is_valid:
                raise ValueError('Invalid bridge geometry')
            corridors.append(transform(TO_METRES.transform, geometry).buffer(buffer_metres))
            self.ids.append(element['id'])
        self.tree = STRtree(corridors)

    def intersects(self, bounds):
        s, w, n, e = bounds
        area = box(*TO_METRES.transform_bounds(w, s, e, n, densify_pts=21))
        return bool(len(self.tree.query(area, predicate='intersects')))
