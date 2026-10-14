import argparse
import io
import json
from pathlib import Path
import tempfile
import unittest
import zlib
from unittest.mock import patch

from shapely.geometry import box, mapping

import mcdk
import meld_bridge as bridge
import national_mask
import seal_void_edges as edge
from ocean_mask import LandMask
from test_ocean_mask import write_mask
from test_seal_void_edges import area, chunk

# A made-up border along 54.90 N: the country to the north, land on both sides.
BORDER = 54.90


def write_country(path):
    bridge.atomic_json(path, {'type': 'FeatureCollection', 'features': [
        {'type': 'Feature', 'properties': {}, 'geometry': mapping(box(8.5, BORDER, 10.0, 55.5))}]})


def rect_at(lat, lon, size=256):
    x, z = mcdk.block(lat, lon)
    x0, z0 = int(x // 16) * 16 - size // 2, int(z // 16) * 16 - size // 2
    return [x0, z0, x0 + size - 1, z0 + size - 1]


class NationalMaskTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.land_path, self.country_path = root / 'land.json', root / 'country.json'
        write_mask(self.land_path, [box(8.5, 54.5, 10.0, 55.5)], coverage=(54, 8, 57, 11))
        write_country(self.country_path)
        self.mask = national_mask.CountryMask(self.country_path, LandMask(self.land_path))

    def tearDown(self):
        self.tmp.cleanup()

    def test_cells_are_classified_by_the_border(self):
        self.assertEqual(self.mask.classify(rect_at(55.1, 9.3), 1, 2000), (True, True, False))
        self.assertEqual(self.mask.classify(rect_at(BORDER, 9.3), 1, 2000), (True, True, True))
        self.assertEqual(self.mask.classify(rect_at(54.7, 9.3), 1, 2000), (False, False, False))

    def test_only_chunks_on_foreign_land_are_voided(self):
        rect = rect_at(BORDER, 9.3)
        chunks = self.mask.foreign_chunks(rect, 1)
        total = (rect[2] - rect[0] + 1) // 16 * ((rect[3] - rect[1] + 1) // 16)
        self.assertTrue(0 < len(chunks) < total)
        for cx, cz in chunks:
            lat, _ = mcdk.latlon(cx * 16 + 8, cz * 16 + 8)
            self.assertLess(lat, BORDER)
        kept = {(cx, cz) for cx in range(rect[0] // 16, rect[2] // 16 + 1)
                for cz in range(rect[1] // 16, rect[3] // 16 + 1)} - set(chunks)
        for cx, cz in kept:
            lat, _ = mcdk.latlon(cx * 16 + 8, cz * 16 + 8)
            self.assertGreaterEqual(lat, BORDER - 1e-9)
        self.assertEqual(self.mask.foreign_chunks(rect_at(55.1, 9.3), 1), [])

    def test_chunks_leave_the_region_file_and_the_rest_stays_byte_for_byte(self):
        with tempfile.TemporaryDirectory() as d:
            world = Path(d) / 'world'
            (world / 'region').mkdir(parents=True)
            f = io.BytesIO()
            chunk().write(f)
            payload = b'\x02' + zlib.compress(f.getvalue())
            path = world / 'region' / 'r.0.0.mca'
            path.write_bytes(edge.region_bytes({0: payload, 1: payload, 33: payload}, bytes(4096)))
            other = world / 'region' / 'r.1.0.mca'
            other.write_bytes(edge.region_bytes({0: payload}, bytes(4096)))
            self.assertEqual(national_mask.remove_chunks(world, [(1, 0), (5, 5), (32, 0)]), 2)
            chunks, _ = edge.region_payloads(path)
            self.assertEqual(sorted(chunks), [0, 33])
            self.assertEqual(chunks[33], payload)
            # A region left without chunks stays as an empty, valid file.
            self.assertEqual(other.read_bytes(), bytes(8192))
            self.assertEqual(national_mask.remove_chunks(world, [(1, 0)]), 0)

    def test_the_seal_follows_the_cut_along_the_border(self):
        areas = [area(0, 0, 47, 47)]
        # Void the eastern column of chunks, as at a land border.
        regions = edge.edge_columns(areas, [(2, z) for z in range(3)])
        cols = regions[0, 0]
        self.assertIn((15, 8), cols[1, 1])
        self.assertIn((14, 8), cols[1, 1])
        self.assertNotIn((2, 1), cols)
        self.assertNotIn((13, 8), cols[1, 1])

    def test_the_planner_skips_cells_abroad_and_marks_border_cells(self):
        root = Path(self.tmp.name)
        binary = root / 'arnis'
        binary.write_bytes(b'fake')
        snapshot = root / 'bridges.json'
        bridge.atomic_json(snapshot, {'arnis_dk_bridge_coverage': [54, 8, 57, 11], 'elements': []})
        args = argparse.Namespace(scale=1, cell_regions=1, max_cells=100, threads=1, frame='mcdk',
                                  job=root / 'job', bbox='54.86,9.28,54.95,9.32',
                                  arnis=binary, danish_buildings=None, osm_file=None, caves=True,
                                  signage='full', land_mask=self.land_path, coast_buffer_m=0,
                                  keep_bbox=[], bridge_mask=snapshot, country=self.country_path)
        with patch.object(bridge, 'probe'), patch('fetch.bounds'):
            bridge.plan(args)
        doc = bridge.read_json(args.job / 'plan.json')
        self.assertEqual(doc['schema'], 6)
        self.assertEqual(Path(doc['country']['path']), self.country_path.resolve())
        abroad = [c for c in doc['cells'] if c.get('skip_reason') == 'abroad']
        border = [c for c in doc['cells'] if c.get('foreign_land')]
        home = [c for c in doc['cells'] if c['status'] == 'pending' and not c.get('foreign_land')]
        self.assertTrue(abroad and border and home, [c.get('skip_reason') or c['status'] for c in doc['cells']])
        self.assertTrue(all(c['status'] == 'skipped_ocean' for c in abroad))
        self.assertTrue(all(c['status'] == 'pending' for c in border))

    def test_a_border_cell_loses_its_foreign_chunks_after_generation(self):
        with tempfile.TemporaryDirectory() as d:
            job = Path(d)
            world = job / 'world'
            (world / 'region').mkdir(parents=True)
            f = io.BytesIO()
            chunk().write(f)
            payload = b'\x02' + zlib.compress(f.getvalue())
            (world / 'region' / 'r.0.0.mca').write_bytes(edge.region_bytes({0: payload, 1: payload}, bytes(4096)))
            fake = argparse.Namespace(foreign_chunks=lambda rect, scale: [(1, 0)])
            cell = {'rect': [0, 0, 31, 15], 'foreign_land': True}
            bridge.void_foreign_land({'scale': 1}, job, world, cell, {'mask': fake})
            self.assertEqual(cell['masked_chunks'], 1)
            self.assertEqual(bridge.read_json(job / 'national-mask.json'), {'chunks': [[1, 0]]})
            chunks, _ = edge.region_payloads(world / 'region' / 'r.0.0.mca')
            self.assertEqual(sorted(chunks), [0])
            # Again after an interruption: noted once, nothing left to remove.
            bridge.void_foreign_land({'scale': 1}, job, world, cell, {'mask': fake})
            self.assertEqual(bridge.read_json(job / 'national-mask.json'), {'chunks': [[1, 0]]})


if __name__ == '__main__':
    unittest.main()
