from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

import numpy as np
from shapely.geometry import box
from shapely.ops import unary_union

import hedges
from fetch import ENTITIES, atomic_json, fetch
from prepare import prepare
import store
from test_hedges import tiff

NOW = datetime(2026, 10, 20, tzinfo=timezone.utc)


def bbox_around(x0, y0, x1, y1):
    west, south, east, north = store.TO_WGS84.transform_bounds(x0, y0, x1, y1, densify_pts=21)
    return [south, west, north, east]


def polygon(x0, y0, x1, y1):
    return f'POLYGON (({x0} {y0},{x1} {y0},{x1} {y1},{x0} {y1},{x0} {y0}))'


# A house on the border of tiles 648_6150 and 651_6150, with its address and door,
# a living hedge and two untyped ones beside it, and some scrub.
HOUSE = {'id_lokalId': 'geo-1', 'BBRUUID': 'bbr-1', 'status': 'Anlagt', 'geometristatus': 'Endelig',
         'geometri': {'crs': 25832, 'wkt': polygon(650990, 6150990, 651010, 6151010)}}
RECORDS = {
    'GEODKV': [HOUSE],
    'BBR': [{'id_lokalId': 'bbr-1', 'status': '6', 'husnummer': 'dar-1', 'byg054AntalEtager': 2}],
    'DAR': [{'id_lokalId': 'dar-1', 'status': '3', 'husnummertekst': '7', 'adgangspunkt': 'point-1',
             'adgangsadressebetegnelse': 'Testvej 7, 4200 Slagelse', 'geoDanmarkBygning': 'geo-1'}],
    'DAR_POINT': [{'id_lokalId': 'point-1', 'status': '8', 'oprindelse_tekniskStandard': 'TD',
                   'position': {'wkt': 'POINT (651000 6151005)', 'crs': 25832}}]}


def hedge(key, kind, wkt):
    return {'id_lokalId': key, 'hegnstype': kind, 'status': 'Anlagt', 'geometristatus': 'Endelig',
            'geometri': {'wkt': wkt, 'crs': 25832}}


HEDGES = [hedge('living', 'Levende', 'LINESTRING (650960 6150980, 651040 6150980)'),
          hedge('grown', 'Ikke tildelt', 'LINESTRING (650960 6150960, 651000 6150960)'),
          hedge('bare', 'Ikke tildelt', 'LINESTRING (651020 6150960, 651060 6150960)')]
SCRUB = [{'id_lokalId': 'scrub', 'status': 'Anlagt', 'geometristatus': 'Endelig',
          'geometri': {'wkt': polygon(650950, 6151020, 650980, 6151040), 'crs': 25832}}]


def transport(register, query, variables, key):
    """Every geometry query answers with the same rows, as two neighbouring tiles do for
    the features on their border."""
    if 'GEODKV_Hegn(' in query:
        entity, rows = 'GEODKV_Hegn', HEDGES
    elif 'GEODKV_KratBevoksning(' in query:
        entity, rows = 'GEODKV_KratBevoksning', SCRUB
    else:
        entity, rows = ENTITIES[register], RECORDS[register]
    return {'data': {entity: {'nodes': [dict(row) for row in rows],
                              'pageInfo': {'hasNextPage': False, 'endCursor': None}}}}


def opener(params):
    """Growth of 1.5 m in the height tiles west of x=651000 and 0.1 m east of it."""
    left = float(params['bbox'].split(',')[0])
    surface = 31.5 if left < 651000 else 30.1
    level = surface if params['coverage'] == 'dhm_overflade' else 30.0
    return tiff(np.full((hedges.TILE_PX, hedges.TILE_PX), level))


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)
        # Height tiles of 40 m keep the fake WCS small.
        self.patch = patch.object(hedges, 'TILE_PX', 50)
        self.patch.start()
        self.addCleanup(self.patch.stop)

    def test_tiles_cover_the_bbox_on_a_fixed_3_km_grid(self):
        bbox = bbox_around(649000, 6149500, 652500, 6151500)
        keys = store.tile_keys(bbox)
        self.assertEqual(sorted(keys), ['648_6147', '648_6150', '651_6147', '651_6150'])
        squares = [box(*store.square(key)) for key in keys]
        self.assertTrue(all(s.area == store.TILE_M ** 2 for s in squares))
        x0, y0, x1, y1 = store.TO_UTM.transform_bounds(bbox[1], bbox[0], bbox[3], bbox[2], densify_pts=21)
        self.assertTrue(unary_union(squares).covers(box(x0, y0, x1, y1)))

    def test_the_store_gives_the_same_supplement_as_a_direct_download(self):
        bbox = bbox_around(650900, 6150900, 651100, 6151100)  # across two tiles
        geo, bbr, dar, at = fetch(bbox, 'key', NOW, transport)
        paths = [self.dir / 'direct' / name for name in ('g.json', 'b.json', 'd.json')]
        for path, rows in zip(paths, (geo, bbr, dar)):
            atomic_json(path, rows)
        direct = prepare(*paths, bbox=bbox, at=at)
        hedges.add(direct, bbox, geo, 'key', at, token='t', transport=transport, opener=opener)

        fetched, failed = store.build(self.dir / 'store', store.tile_keys(bbox), 'key', 't',
                                      transport=transport, opener=opener)
        self.assertEqual((len(fetched), failed), (2, []))
        assembled = store.supplement(self.dir / 'store', bbox, self.dir / 'out' / 'buildings.json')

        self.assertEqual(assembled['elements'], direct['elements'])
        self.assertEqual(assembled['arnis_dk']['counts'], direct['arnis_dk']['counts'])
        self.assertEqual(assembled['arnis_dk']['hedges'], direct['arnis_dk']['hedges'])
        self.assertEqual(assembled['arnis_dk']['hedges']['counts']['untyped_kept'], 1)
        self.assertEqual(assembled['arnis_dk']['bbox'], bbox)
        self.assertEqual(assembled['arnis_dk']['store']['tiles'], 2)
        written = json.loads((self.dir / 'out' / 'buildings.json').read_text(encoding='utf-8'))
        self.assertEqual(written['elements'], direct['elements'])

    def test_a_missing_tile_stops_assembly_unless_the_store_may_fetch_it(self):
        bbox = bbox_around(650900, 6150900, 651100, 6151100)
        with self.assertRaisesRegex(ValueError, 'lacks 2 of the 2 tiles'):
            store.supplement(self.dir / 'store', bbox, self.dir / 'out.json')
        asked = []

        def credentials():
            asked.append(True)
            return 'key', None
        result = store.supplement(self.dir / 'store', bbox, self.dir / 'out.json', credentials,
                                  transport=transport)
        self.assertEqual(asked, [True])
        self.assertEqual(result['arnis_dk']['counts']['selected_buildings'], 1)
        # Kept: the next map over the same tiles needs no credentials at all.
        store.supplement(self.dir / 'store', bbox, self.dir / 'again.json', lambda: self.fail('fetched'))
        # Without the height model only the typed hedge is measured.
        self.assertEqual(result['arnis_dk']['hedges']['counts']['untyped_unmeasured'], 2)

    def test_the_newest_tile_wins_when_two_disagree_about_a_record(self):
        bbox = bbox_around(650900, 6150900, 651100, 6151100)
        store.build(self.dir / 'store', store.tile_keys(bbox), 'key', transport=transport)
        older, newer = sorted(store.tile_keys(bbox))
        for key, floors, stamp in ((older, 3, NOW), (newer, 1, NOW - timedelta(days=30))):
            doc = store.load(self.dir / 'store', key)
            doc['bbr'][0]['byg054AntalEtager'] = floors
            doc['snapshot'] = stamp.isoformat()
            store.save(self.dir / 'store', doc)
        result = store.supplement(self.dir / 'store', bbox, self.dir / 'out.json')
        house = next(e for e in result['elements'] if e.get('tags', {}).get('arnis:geodanmark_id') == 'geo-1')
        self.assertEqual(house['tags']['building:levels'], '3')

    def test_build_skips_kept_tiles_and_fetches_old_ones_again(self):
        keys = store.tile_keys(bbox_around(650900, 6150900, 651100, 6151100))
        calls = []

        def counting(*args):
            calls.append(args[0])
            return transport(*args)
        store.build(self.dir / 'store', keys, 'key', transport=counting)
        first = len(calls)
        self.assertEqual(store.build(self.dir / 'store', keys, 'key', transport=counting), ([], []))
        self.assertEqual(len(calls), first)
        old = time.time() - 40 * 86400
        os.utime(store.tile_path(self.dir / 'store', keys[0]), (old, old))
        fetched, _ = store.build(self.dir / 'store', keys, 'key', older_than=timedelta(days=30), transport=counting)
        self.assertEqual(fetched, [keys[0]])

    def test_a_failing_tile_is_reported_and_the_others_are_kept(self):
        keys = store.tile_keys(bbox_around(650900, 6150900, 651100, 6151100))
        bad = sorted(keys)[0]

        def flaky(register, query, variables, key):
            wkt = (variables.get('where') or {}).get('geometri', {}).get('intersects', {}).get('wkt', '')
            if wkt.startswith(f'POLYGON (({int(bad.split("_")[0]) * 1000} '):
                raise ValueError('GEODKV: HTTP 503; check Datafordeler access/service status')
            return transport(register, query, variables, key)
        logged = []
        fetched, failed = store.build(self.dir / 'store', keys, 'key', transport=flaky, log=logged.append)
        self.assertEqual(failed, [bad])
        self.assertEqual(len(fetched), 2)
        self.assertIn('HTTP 503', logged[0])
        self.assertFalse(store.tile_path(self.dir / 'store', bad).exists())
        self.assertTrue(all(store.tile_path(self.dir / 'store', k).exists() for k in keys if k != bad))


if __name__ == '__main__':
    unittest.main()
