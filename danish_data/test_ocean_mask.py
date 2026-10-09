import argparse
import json
from pathlib import Path
import tempfile
import unittest
import zipfile
from unittest.mock import patch

from shapely.geometry import box, mapping
from shapely.ops import transform

import meld_bridge as bridge
from ocean_mask import LandMask, SCHEMA, TO_METRES, prepare


def write_mask(path, polygons=(), coverage=(54, 10, 57, 16)):
    bridge.atomic_json(path, {'schema': SCHEMA, 'crs_epsg': 25832,
        'coverage_bbox': list(coverage), 'features': [
            {'geometry': mapping(transform(TO_METRES.transform, p))} for p in polygons]})


class OceanTests(unittest.TestCase):
    def test_prepare_preserves_inland_lake_and_rejects_wrong_projection(self):
        import shapefile
        from pyproj import CRS
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root/'land_polygons'
            with shapefile.Writer(str(source), shapeType=shapefile.POLYGON) as writer:
                writer.field('id', 'N')
                writer.poly([[(11,55),(11,55.01),(11.01,55.01),(11.01,55),(11,55)],
                             [(11.003,55.003),(11.007,55.003),(11.007,55.007),(11.003,55.007),(11.003,55.003)]])
                writer.record(1)
            archive = root/'land.zip'
            def write_archive(epsg):
                with zipfile.ZipFile(archive, 'w') as z:
                    z.write(source.with_suffix('.shp'), 'land_polygons.shp')
                    z.writestr('land_polygons.prj', CRS.from_epsg(epsg).to_wkt())
            write_archive(4326)
            output=root/'mask.json'
            prepare(archive, [54,10,57,16], output)
            mask=LandMask(output)
            self.assertFalse(mask.is_open_sea([55.004,11.004,55.006,11.006],0))
            self.assertTrue(mask.is_open_sea([55.1,11.1,55.11,11.11],0))
            write_archive(3857)
            with self.assertRaisesRegex(ValueError, 'WGS84'):
                prepare(archive, [54,10,57,16], root/'invalid.json')
            self.assertFalse((root/'invalid.json').exists())

    def test_small_island_at_cell_corner_is_kept_even_if_center_is_sea(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/'mask.json'
            write_mask(path, [box(11.00001, 55.00001, 11.00005, 55.00005)])
            mask = LandMask(path)
            self.assertFalse(mask.is_open_sea([55, 11, 55.01, 11.01], 0))
            self.assertTrue(mask.is_open_sea([55.1, 11.1, 55.11, 11.11], 1000))

    def test_coastal_margin_keeps_nearby_sea_and_unknown_coverage_is_kept(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/'mask.json'
            write_mask(path, [box(11, 55, 11.001, 55.001)])
            mask = LandMask(path)
            sea = [55.005, 11, 55.006, 11.001]
            self.assertTrue(mask.is_open_sea(sea, 0))
            self.assertFalse(mask.is_open_sea(sea, 1000))
            self.assertFalse(mask.is_open_sea([57, 11, 57.01, 11.01], 0))
            # A search spilling beyond the data boundary must not assume sea.
            self.assertFalse(mask.is_open_sea([56.999, 11, 57, 11.001], 1000))

    def test_invalid_mask_and_invalid_margin_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/'mask.json'
            write_mask(path)
            mask = LandMask(path)
            with self.assertRaises(ValueError):
                mask.is_open_sea([55, 11, 55.01, 11.01], float('nan'))
            data = bridge.read_json(path)
            data['crs_epsg'] = 4326
            bridge.atomic_json(path, data)
            with self.assertRaises(ValueError):
                LandMask(path)

    def test_plan_skips_sea_before_danish_data_validation_but_honors_keep_bbox(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            mask = root/'mask.json'
            write_mask(mask)
            binary = root/'arnis'
            binary.write_bytes(b'fake')
            cells = [{'cell_key': str(i), 'bbox': {'south': 55, 'west': 11+i*.01,
                      'north': 55.005, 'east': 11+(i+1)*.01}} for i in range(3)]
            grid = argparse.Namespace(cells_for_bbox=lambda *a, **kw: cells)
            args = argparse.Namespace(scale=1, cell_regions=2, max_cells=1, threads=1,
                job=root/'job', bbox='55,11,55.005,11.03', meld_source=root,
                arnis=binary, danish_buildings=None, osm_file=None, caves=False,
                signage='full', land_mask=mask, coast_buffer_m=1000,
                keep_bbox=['55.001,11.011,55.004,11.019'])
            with patch.object(bridge, 'external_meld', return_value=(grid, {})), \
                    patch.object(bridge, 'probe'), patch('fetch.bounds') as bounds_check:
                bridge.plan(args)
                self.assertEqual(bounds_check.call_count, 1)
            doc = bridge.read_json(args.job/'plan.json')
            self.assertEqual([c['status'] for c in doc['cells']], ['skipped_ocean','pending','skipped_ocean'])
            self.assertEqual([c['id'] for c in doc['cells']], ['00000','00001','00002'])
            # Pin the mask just like the generator: changed inputs must not alter resume.
            mask.write_text('{}', encoding='utf-8')
            with patch.object(bridge, 'external_meld', return_value=(grid, {})):
                with self.assertRaisesRegex(ValueError, 'land_mask changed'):
                    bridge.verify_inputs(doc)

    def test_all_ocean_resume_needs_no_key_and_does_not_start_generation(self):
        with tempfile.TemporaryDirectory() as directory:
            job = Path(directory)
            bridge.atomic_json(job/'plan.json', {'arnis': {'path': 'fake'}, 'world_name': 'Test',
                'cells': [{'id': '0', 'status': 'skipped_ocean'}]})
            args = argparse.Namespace(job=job, limit=None, timeout=60, credentials_file=None)
            with patch.object(bridge, 'verify_inputs'), patch.object(bridge, 'probe'), \
                    patch('fetch.load_key') as key, patch.object(bridge, 'supplement_for_cell') as data, \
                    patch.object(bridge.subprocess, 'run') as process:
                bridge.run(args)
                key.assert_not_called()
                data.assert_not_called()
                process.assert_not_called()
            self.assertFalse((job/'worlds').exists())


if __name__ == '__main__':
    unittest.main()
