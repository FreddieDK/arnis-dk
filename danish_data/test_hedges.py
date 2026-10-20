import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import struct
import tempfile
import unittest
from unittest.mock import patch

import numpy as np
from shapely import from_wkt
from shapely.geometry import Point

import hedges
import meld_bridge as bridge

AT = datetime(2026, 10, 13, tzinfo=timezone.utc)


def tiff(array, rows_per_strip=2, compression=1):
    """A little-endian, striped, float32 TIFF like the height model's WCS sends."""
    array = np.asarray(array, dtype='<f4')
    height, width = array.shape
    strips = [array[r:r + rows_per_strip].tobytes() for r in range(0, height, rows_per_strip)]
    entries = [(256, 4, [width]), (257, 4, [height]), (258, 3, [32]), (259, 3, [compression]),
               (273, 4, None), (277, 3, [1]), (278, 4, [rows_per_strip]),
               (279, 4, [len(s) for s in strips]), (339, 3, [3])]
    ifd_size = 2 + 12 * len(entries) + 4
    arrays_at = 8 + ifd_size
    arrays = b''
    offsets_at = {}
    for tag, kind, values in entries:
        n = len(strips) if values is None else len(values)
        if (2 if kind == 3 else 4) * n > 4:
            offsets_at[tag] = arrays_at + len(arrays)
            arrays += b'\0' * (4 * n)
    data_at = arrays_at + len(arrays)
    strip_offsets, position = [], data_at
    for strip in strips:
        strip_offsets.append(position)
        position += len(strip)
    arrays = bytearray(arrays)
    out = bytearray(b'II*\x00' + struct.pack('<I', 8) + struct.pack('<H', len(entries)))
    for tag, kind, values in entries:
        values = strip_offsets if values is None else values
        fmt = '<' + ('H' if kind == 3 else 'I') * len(values)
        packed = struct.pack(fmt, *values)
        if tag in offsets_at:
            start = offsets_at[tag] - arrays_at
            arrays[start:start + len(packed)] = packed
            out += struct.pack('<HHII', tag, kind, len(values), offsets_at[tag])
        else:
            out += struct.pack('<HHI', tag, kind, len(values)) + packed.ljust(4, b'\0')
    out += struct.pack('<I', 0) + bytes(arrays) + b''.join(strips)
    return bytes(out)


class Heights:
    """Growth of 1.5 m west of x=50 and 0.1 m east of it."""

    def sample(self, xs, ys):
        return np.where(xs < 50, 1.5, 0.1).astype(np.float32)


def hedge(key, kind, wkt):
    return {'id_lokalId': key, 'hegnstype': kind, 'status': 'Anlagt', 'geometristatus': 'Endelig',
            'geometri': {'wkt': wkt}}


BUILDING = {'status': 'Anlagt', 'geometristatus': 'Endelig',
            'geometri': 'POLYGON ((5 -3, 15 -3, 15 3, 5 3, 5 -3))'}
ROWS = [hedge('living', 'Levende', 'LINESTRING Z (0 0 30, 20 0 30)'),
        hedge('wire', 'Tråd', 'LINESTRING (0 10, 20 10)'),
        hedge('grown', 'Ikke tildelt', 'LINESTRING (0 20, 20 20)'),
        hedge('bare', 'Ikke tildelt', 'LINESTRING (60 20, 90 20)')]
SCRUB = [{'id_lokalId': 'scrub', 'status': 'Anlagt', 'geometristatus': 'Endelig',
          'geometri': {'wkt': 'POLYGON ((0 -10, 30 -10, 30 -2, 0 -2, 0 -10))'}}]


class HedgeTests(unittest.TestCase):
    def test_the_height_model_tiff_is_read_without_extra_libraries(self):
        grid = np.arange(12, dtype=np.float32).reshape(4, 3)
        np.testing.assert_array_equal(hedges.read_float_tiff(tiff(grid)), grid)
        np.testing.assert_array_equal(hedges.read_float_tiff(tiff(grid, rows_per_strip=4)), grid)
        with self.assertRaisesRegex(ValueError, 'uncompressed'):
            hedges.read_float_tiff(tiff(grid, compression=5))
        with self.assertRaisesRegex(ValueError, 'little-endian'):
            hedges.read_float_tiff(b'MM\x00*' + b'\0' * 8)

    def test_living_and_grown_hedges_are_kept_and_fences_are_not(self):
        lines, scrub, counts = hedges.select(ROWS, SCRUB, [BUILDING], AT, Heights())
        kept = {key.split('/')[0] for key, _ in lines}
        self.assertEqual(kept, {'living', 'grown'})
        self.assertEqual((counts['fence_lines'], counts['untyped_kept'], counts['untyped_dropped']), (1, 1, 1))
        # Cut back from the house that the typed hedge runs into.
        footprint = from_wkt(BUILDING['geometri'])
        for key, line in lines:
            self.assertGreaterEqual(line.distance(footprint), hedges.BUILDING_CLEARANCE_M - 0.01, key)
        self.assertEqual(sum(key.startswith('living') for key, _ in lines), 2)
        self.assertEqual(len(scrub), 1)
        self.assertGreaterEqual(scrub[0][1].distance(footprint), hedges.BUILDING_CLEARANCE_M - 0.01)

    def test_without_a_height_model_only_typed_hedges_are_kept(self):
        lines, _, counts = hedges.select(ROWS, [], [], AT, None)
        self.assertEqual({key.split('/')[0] for key, _ in lines}, {'living'})
        self.assertEqual(counts['untyped_unmeasured'], 2)

    def test_a_hedge_is_cut_back_from_every_building_it_passes_and_left_whole_elsewhere(self):
        houses = [dict(BUILDING, geometri=f'POLYGON (({x} 30, {x + 6} 30, {x + 6} 36, {x} 36, {x} 30))')
                  for x in (0, 20, 200)]
        row = hedge('row', 'Levende', 'LINESTRING (-10 33, 40 33)')
        lone = hedge('lone', 'Levende', 'LINESTRING (100 0, 120 0)')
        lines, _, _ = hedges.select([row, lone], [], houses, AT, None)
        pieces = {key: line for key, line in lines}
        self.assertEqual(sorted(pieces), ['lone/0', 'row/0', 'row/1', 'row/2'])
        for key, line in pieces.items():
            for house in houses:
                self.assertGreaterEqual(line.distance(from_wkt(house['geometri'])),
                                        hedges.BUILDING_CLEARANCE_M - 0.01, key)
        self.assertEqual(pieces['lone/0'].length, 20)

    def test_growth_under_a_building_does_not_make_a_hedge(self):
        roofline = hedge('roof', 'Ikke tildelt', 'LINESTRING (6 0, 14 0)')
        lines, _, counts = hedges.select([roofline], [], [BUILDING], AT, Heights())
        self.assertEqual(lines, [])
        self.assertEqual(counts['untyped_unmeasured'], 1)

    def test_add_writes_hedge_and_scrub_ways_and_keeps_the_token_out(self):
        pages = {'GEODKV_Hegn': ROWS, 'GEODKV_KratBevoksning': SCRUB}

        def transport(register, query, variables, key):
            entity = 'GEODKV_Hegn' if 'GEODKV_Hegn(' in query else 'GEODKV_KratBevoksning'
            return {'data': {entity: {'nodes': pages[entity], 'pageInfo': {'hasNextPage': False}}}}

        def opener(params):
            # 4 x 4 cells of 0.8 m; the surface stands 1.5 m over the terrain.
            level = 31.5 if params['coverage'] == 'dhm_overflade' else 30.0
            return tiff(np.full((4, 4), level))

        supplement = {'arnis_dk': {'attribution': 'GeoDanmark'}, 'elements': []}
        with patch.object(hedges, 'TILE_PX', 4):
            counts = hedges.add(supplement, [55.0, 11.0, 55.001, 11.001], [BUILDING], 'key', AT,
                                token='secret-token', transport=transport, opener=opener)
        ways = [e for e in supplement['elements'] if e['type'] == 'way']
        self.assertEqual({w['tags'].get('barrier') or w['tags'].get('natural') for w in ways}, {'hedge', 'scrub'})
        self.assertTrue(all(w['tags']['arnis:hedge'] == 'geodanmark' for w in ways))
        nodes = {e['id']: e for e in supplement['elements'] if e['type'] == 'node'}
        self.assertTrue(all(n in nodes for w in ways for n in w['nodes']))
        self.assertEqual(counts['untyped_kept'], 2)
        self.assertTrue(supplement['arnis_dk']['hedges']['height_model'])
        self.assertIn('Højdemodel', supplement['arnis_dk']['attribution'])
        self.assertNotIn('secret-token', json.dumps(supplement))

    def test_a_large_area_asks_for_hedges_in_tiles_and_keeps_each_line_once(self):
        tiles = []

        def transport(register, query, variables, key):
            entity = 'GEODKV_Hegn' if 'GEODKV_Hegn(' in query else 'GEODKV_KratBevoksning'
            tiles.append((entity, variables['where']['geometri']['intersects']['wkt']))
            return {'data': {entity: {'nodes': [ROWS[0]] if entity == 'GEODKV_Hegn' else SCRUB,
                                      'pageInfo': {'hasNextPage': False}}}}

        supplement = {'arnis_dk': {'attribution': 'GeoDanmark'}, 'elements': []}
        counts = hedges.add(supplement, [55.400, 11.300, 55.472, 11.427], [], 'key', AT, transport=transport)
        self.assertEqual(sum(entity == 'GEODKV_Hegn' for entity, _ in tiles), 9)
        self.assertEqual(len({wkt for _, wkt in tiles}), 9)
        self.assertEqual((counts['hedge_pieces'], counts['scrub_areas']), (1, 1))

    def test_height_tiles_are_fetched_together_once_each_before_the_lines_are_measured(self):
        calls = []

        def opener(params):
            calls.append((params['coverage'], params['bbox']))
            # Growth of 1.5 m in the tiles west of x=40 and 0.1 m in those east of it.
            left = float(params['bbox'].split(',')[0])
            surface = 31.5 if left < 40 else 30.1
            return tiff(np.full((50, 50), surface if params['coverage'] == 'dhm_overflade' else 30.0))

        with patch.object(hedges, 'TILE_PX', 50):  # tiles of 40 m
            model = hedges.HeightModel('t', opener, sleep=lambda s: None)
            lines, _, counts = hedges.select(ROWS, [], [BUILDING], AT, model)
        self.assertEqual(len(calls), len(set(calls)), 'no tile is downloaded twice')
        self.assertEqual(len(calls), 2 * len(model.tiles))
        self.assertEqual({key.split('/')[0] for key, _ in lines}, {'living', 'grown'})
        self.assertEqual((counts['untyped_kept'], counts['untyped_dropped']), (1, 1))

    def test_a_dropped_tile_is_tried_again_and_a_lasting_failure_names_its_cause(self):
        from urllib.error import HTTPError
        calls = []

        def flaky(params):
            calls.append(params['coverage'])
            if len(calls) <= 2:
                raise HTTPError('url', 503, 'Service Unavailable', {}, None)
            return tiff(np.full((4, 4), 1.0))

        with patch.object(hedges, 'TILE_PX', 4):
            model = hedges.HeightModel('t', flaky, sleep=lambda s: None)
            self.assertEqual(float(model.sample(np.array([1.0]), np.array([1.0]))[0]), 0.0)
        self.assertEqual(len(calls), 4)

        def down(params):
            raise HTTPError('url', 503, 'Service Unavailable', {}, None)

        def refused(params):
            raise HTTPError('url', 403, 'Forbidden', {}, None)

        def silent(params):
            raise TimeoutError()

        with patch.object(hedges, 'TILE_PX', 4):
            with self.assertRaisesRegex(ValueError, 'HTTP 503'):
                hedges.HeightModel('t', down, sleep=lambda s: None).sample(np.array([1.0]), np.array([1.0]))
            slept = []
            with self.assertRaisesRegex(ValueError, 'HTTP 403'):
                hedges.HeightModel('t', refused, sleep=slept.append).sample(np.array([1.0]), np.array([1.0]))
            self.assertEqual(slept, [], 'a refused token is not tried again')
            with self.assertRaisesRegex(ValueError, 'timed out'):
                hedges.HeightModel('t', silent, sleep=lambda s: None).sample(np.array([1.0]), np.array([1.0]))

    def test_the_token_is_read_from_the_credentials_file(self):
        with tempfile.TemporaryDirectory() as name:
            path = Path(name) / 'keys.env'
            path.write_text('DATAFORDELER_API_KEY=a\nDATAFORSYNINGEN_TOKEN="t0ken"\n', encoding='utf-8')
            self.assertEqual(hedges.load_token(path), 't0ken')
            path.write_text('DATAFORDELER_API_KEY=a\n', encoding='utf-8')
            self.assertIsNone(hedges.load_token(path))


class BridgeHedgeTests(unittest.TestCase):
    def test_a_hedge_plan_needs_the_height_model_token(self):
        with tempfile.TemporaryDirectory() as name:
            job = Path(name)
            keys = job / 'keys.env'
            keys.write_text('DATAFORDELER_API_KEY=abc\n', encoding='utf-8')
            doc = {'schema': bridge.SCHEMA, 'arnis': {'path': 'arnis.exe'}, 'world_name': 'Test',
                   'threads': 1, 'scale': 1, 'signage': 'full', 'caves': False, 'hedges': True,
                   'snapshot': AT.isoformat(),
                   'cells': [{'id': '0', 'meld_cell': '0', 'bbox': [55, 11, 55.001, 11.001], 'status': 'pending'}]}
            bridge.atomic_json(job / 'plan.json', doc)
            args = argparse.Namespace(job=job, limit=1, timeout=60, credentials_file=keys)
            with patch.object(bridge, 'verify_inputs'), patch.object(bridge, 'probe'), \
                    patch.object(bridge.subprocess, 'run') as process:
                with self.assertRaisesRegex(ValueError, 'DATAFORSYNINGEN_TOKEN'):
                    bridge.run(args)
                process.assert_not_called()

    def test_a_cached_supplement_must_match_the_plans_hedges(self):
        with tempfile.TemporaryDirectory() as name:
            folder = Path(name)
            cell = {'data_bbox': [55, 11, 55.001, 11.001]}
            bridge.atomic_json(folder / 'buildings.json',
                               {'arnis_dk': {'bbox': cell['data_bbox'], 'at': AT.isoformat()}, 'elements': []})
            document = {'snapshot': AT.isoformat(), 'hedges': True}
            with self.assertRaisesRegex(ValueError, 'hedges'):
                bridge.supplement_for_cell(document, cell, folder, 'key', 'token')
            self.assertEqual(bridge.supplement_for_cell({**document, 'hedges': False}, cell, folder, 'key'),
                             folder / 'buildings.json')


if __name__ == '__main__':
    unittest.main()
