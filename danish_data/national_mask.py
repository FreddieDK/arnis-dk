"""Keep a Meld job to one country: classify cells, then void chunks on foreign land.

The country is a GeoJSON (Multi)Polygon in WGS84 that includes its territorial
waters, such as the OpenStreetMap boundary of Denmark (relation 50046). Land is
the planner's land mask. Foreign land is land outside the country, so a coastline
that differs between the two never counts as foreign: only another country's land
does. Cells are block rectangles in the UTM zone 32 frame.
"""
import json
from pathlib import Path

import shapely
from shapely.geometry import box, shape
from shapely.ops import transform, unary_union
from shapely.prepared import prep

import mcdk


def utm(x_lon, y_lat):
    """WGS84 lon/lat to EPSG:25832 metres, for shapely.ops.transform."""
    import numpy as np
    pts = [mcdk.utm32(lat, lon) for lon, lat in zip(np.atleast_1d(x_lon), np.atleast_1d(y_lat))]
    return [p[0] for p in pts], [p[1] for p in pts]


def rect_metres(rect, scale):
    """The UTM box of an inclusive MCDK block rectangle."""
    x0, z0, x1, z1 = rect
    return box(x0 / scale + mcdk.EAST_OFFSET, mcdk.NORTH_OFFSET - (z1 + 1) / scale,
               (x1 + 1) / scale + mcdk.EAST_OFFSET, mcdk.NORTH_OFFSET - z0 / scale)


class CountryMask:
    def __init__(self, country_path, land_mask):
        document = json.loads(Path(country_path).read_text(encoding='utf-8'))
        features = document.get('features', [document])
        geometries = [shape(f['geometry'] if 'geometry' in f else f) for f in features]
        if not geometries or any(g.geom_type not in ('Polygon', 'MultiPolygon') or not g.is_valid
                                 for g in geometries):
            raise ValueError('Country must be valid GeoJSON (Multi)Polygon features')
        # Points every 0.005 degrees first: a long straight edge in lat/lon is a curve in UTM.
        self.country = unary_union([transform(utm, shapely.segmentize(g, 0.005)) for g in geometries])
        self.prepared = prep(self.country)
        self.land = land_mask.land
        self.tree = land_mask.tree

    def _land_in(self, area):
        return [shapely.clip_by_rect(self.land[i], *area.bounds) for i in self.tree.query(area)]

    def classify(self, rect, scale, buffer_m):
        """(inside the country, own land within buffer_m, foreign land in the cell)."""
        cell = rect_metres(rect, scale)
        if not self.prepared.intersects(cell):
            return False, False, False
        near = cell.buffer(buffer_m, join_style=2)
        own = any(not g.is_empty and self.prepared.intersects(g) for g in self._land_in(near))
        foreign = any(not g.is_empty and not self.prepared.contains(g)
                      and g.difference(self.country).area > 1.0 for g in self._land_in(cell))
        return True, own, foreign

    def foreign_chunks(self, rect, scale):
        """Chunks of the rectangle whose centre lies on foreign land."""
        x0, z0, x1, z1 = rect
        cell = rect_metres(rect, scale)
        foreign = [g.difference(self.country) for g in self._land_in(cell)]
        foreign = unary_union([g for g in foreign if not g.is_empty])
        if foreign.is_empty:
            return []
        import numpy as np
        cxs = np.arange(x0 // 16, x1 // 16 + 1)
        czs = np.arange(z0 // 16, z1 // 16 + 1)
        gx, gz = np.meshgrid(cxs, czs)
        e = (gx * 16 + 8) / scale + mcdk.EAST_OFFSET
        n = mcdk.NORTH_OFFSET - (gz * 16 + 8) / scale
        hit = shapely.contains_xy(foreign, e, n)
        return [(int(cx), int(cz)) for cx, cz in zip(gx[hit], gz[hit])]


def remove_chunks(world, chunks):
    """Delete chunks from the world's region files; returns how many were present."""
    from seal_void_edges import region_bytes, region_payloads, replace_bytes, world_lock
    by_region = {}
    for cx, cz in chunks:
        by_region.setdefault((cx // 32, cz // 32), []).append((cx % 32) + (cz % 32) * 32)
    removed = 0
    with world_lock(world):
        for (rx, rz), indices in sorted(by_region.items()):
            path = Path(world) / 'region' / f'r.{rx}.{rz}.mca'
            if not path.exists():
                continue
            payloads, timestamps = region_payloads(path)
            present = [i for i in indices if i in payloads]
            if not present:
                continue
            for i in present:
                del payloads[i]
            removed += len(present)
            if payloads:
                replace_bytes(path, region_bytes(payloads, timestamps))
            else:
                # An empty region file is still a valid one; keep the file, never delete it.
                replace_bytes(path, bytes(8192))
    return removed
