import argparse
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import meld_bridge as bridge


class BridgeTests(unittest.TestCase):
    def test_padding_includes_geometry_and_grows_at_lower_scale(self):
        area = [55.0, 11.0, 55.001, 11.001]
        a, b = bridge.padded(area, 1), bridge.padded(area, .5)
        self.assertTrue(bridge.contains(a, area))
        self.assertTrue(bridge.contains(b, a))
        self.assertGreater((area[0] - a[0]) * 111320, 100)

    def test_exclusive_job_lock_does_not_remove_another_runner_lock(self):
        with tempfile.TemporaryDirectory() as name:
            job = Path(name)
            with bridge.job_lock(job):
                with self.assertRaises(ValueError):
                    with bridge.job_lock(job):
                        self.fail('Lock admitted two runners')
                self.assertTrue((job / 'runner.lock').exists())
            self.assertFalse((job / 'runner.lock').exists())

    def test_changed_binary_refuses_resume(self):
        with tempfile.TemporaryDirectory() as name:
            path = Path(name) / 'arnis.exe'
            path.write_bytes(b'first')
            doc = {'schema': 1, 'meld_source': name, 'meld_hashes': {},
                   'arnis': bridge.input_record(path)}
            path.write_bytes(b'different')
            with patch.object(bridge, 'external_meld', return_value=(None, {})):
                with self.assertRaisesRegex(ValueError, 'arnis changed'):
                    bridge.verify_inputs(doc)

    def test_command_uses_one_shared_world_and_preserves_signs_and_danish_data(self):
        doc = {'arnis': {'path': 'arnis.exe'}, 'world_name': 'Test', 'scale': 1,
               'signage': 'full', 'caves': True, 'osm_file': {'path': 'osm.json'}}
        cmd = bridge.command(doc, {'bbox': [55, 11, 55.01, 11.01]}, Path('job'), Path('buildings.json'))
        self.assertIn('--one-world', cmd)
        self.assertEqual(cmd[cmd.index('--signage') + 1], 'full')
        self.assertEqual(cmd[cmd.index('--danish-buildings') + 1], 'buildings.json')
        self.assertIn('--caves', cmd)
        self.assertEqual(cmd[cmd.index('--world-type') + 1], 'void')
        self.assertNotIn('--master-origin-lat', cmd)

    def test_new_plans_use_the_normal_height_and_older_plans_keep_their_world(self):
        base = {'arnis': {'path': 'arnis.exe'}, 'world_name': 'Test', 'scale': 1,
                'signage': 'full', 'caves': False}
        cell = {'bbox': [55, 11, 55.01, 11.01]}
        cmd = bridge.command({**base, 'world_height': 'vanilla'}, cell, Path('job'), Path('b.json'))
        self.assertEqual(cmd[cmd.index('--one-world-height') + 1], 'vanilla')
        for doc in (base, {**base, 'world_height': 'earth'}):
            self.assertNotIn('--one-world-height', bridge.command(doc, cell, Path('job'), Path('b.json')))
        with patch.object(bridge, 'external_meld', return_value=(None, {})):
            for schema in (1, 2, bridge.SCHEMA):
                bridge.verify_inputs({'schema': schema, 'meld_source': 'meld', 'meld_hashes': {}})
            with self.assertRaisesRegex(ValueError, 'Unsupported'):
                bridge.verify_inputs({'schema': bridge.SCHEMA + 1, 'meld_source': 'meld', 'meld_hashes': {}})

    def test_plan_pins_the_normal_height_and_checks_the_binary_supports_it(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            binary = root / 'arnis'
            binary.write_bytes(b'fake')
            cells = [{'cell_key': '0', 'bbox': {'south': 55, 'west': 11, 'north': 55.005, 'east': 11.01}}]
            grid = argparse.Namespace(cells_for_bbox=lambda *a, **kw: cells)
            args = argparse.Namespace(scale=1, cell_regions=2, max_cells=1, threads=1,
                                      job=root / 'job', bbox='55,11,55.005,11.01', meld_source=root,
                                      arnis=binary, danish_buildings=None, osm_file=None, caves=False,
                                      signage='full', land_mask=None, coast_buffer_m=2000, keep_bbox=[])
            with patch.object(bridge, 'external_meld', return_value=(grid, {})), \
                    patch.object(bridge, 'probe') as probe, patch('fetch.bounds'):
                bridge.plan(args)
            probe.assert_called_once_with(binary.resolve(), ['--one-world-height', 'vanilla'])
            doc = bridge.read_json(args.job / 'plan.json')
            self.assertEqual((doc['schema'], doc['world_height']), (bridge.SCHEMA, 'vanilla'))

    def test_resume_skips_completed_cells_and_retries_failed_cells(self):
        with tempfile.TemporaryDirectory() as name:
            job = Path(name)
            doc = {'schema': 1, 'arnis': {'path': 'arnis.exe'}, 'world_name': 'Test',
                   'threads': 1, 'scale': 1, 'signage': 'full', 'caves': False,
                   'danish_buildings': {'path': 'buildings.json'},
                   'cells': [{'id': str(i), 'meld_cell': str(i), 'bbox': [55, 11, 55.001, 11.001],
                              'status': s} for i, s in enumerate(['complete', 'failed', 'pending', 'skipped_ocean'])]}
            bridge.atomic_json(job / 'plan.json', doc)
            args = argparse.Namespace(job=job, limit=1, timeout=60, credentials_file=None)
            with patch.object(bridge, 'verify_inputs'), patch.object(bridge, 'probe'), \
                    patch.object(bridge, 'validate_result'), patch.object(bridge, 'world_coverage'), \
                    patch.object(bridge.subprocess, 'run') as process:
                process.return_value.returncode = 0
                bridge.run(args)
                self.assertEqual(process.call_count, 1)
                self.assertNotIn('DATAFORDELER_API_KEY', process.call_args.kwargs['env'])
            self.assertEqual([c['status'] for c in bridge.read_json(job/'plan.json')['cells']],
                             ['complete', 'complete', 'pending', 'skipped_ocean'])
            with patch.object(bridge, 'verify_inputs'), patch.object(bridge, 'probe'), \
                    patch.object(bridge, 'validate_result'), patch.object(bridge, 'world_coverage'), \
                    patch.object(bridge.subprocess, 'run') as process:
                process.return_value.returncode = 2
                with self.assertRaisesRegex(ValueError, 'exit code 2'):
                    bridge.run(args)
            self.assertEqual(bridge.read_json(job/'plan.json')['cells'][2]['status'], 'failed')
            self.assertFalse((job/'runner.lock').exists())

    def test_final_edge_waits_for_last_cell(self):
        with tempfile.TemporaryDirectory() as name:
            job=Path(name)
            doc={'schema':2,'arnis':{'path':'fake'},'world_name':'Test',
                 'threads':1,'scale':1,'signage':'full','caves':True,
                 'danish_buildings':{'path':'buildings.json'},'seal_void_edges':True,
                 'cells':[{'id':str(i),'meld_cell':str(i),'bbox':[55,11,55.001,11.001],
                           'status':'pending'} for i in range(2)]}
            bridge.atomic_json(job/'plan.json',doc)
            args=argparse.Namespace(job=job,limit=1,timeout=60,credentials_file=None)
            with patch.object(bridge,'verify_inputs'),patch.object(bridge,'probe'), \
                    patch.object(bridge,'validate_result'),patch.object(bridge,'world_coverage'), \
                    patch.object(bridge,'finalize_edges') as finalize, \
                    patch.object(bridge.subprocess,'run') as process:
                process.return_value.returncode=0
                bridge.run(args);finalize.assert_not_called()
                bridge.run(args);finalize.assert_called_once()

    def test_result_requires_world_manifest_covering_cell(self):
        with tempfile.TemporaryDirectory() as name:
            world = Path(name)
            (world/'level.dat').write_bytes(b'test')
            bridge.atomic_json(world/'arnis_one_world.json', {'areas': [
                {'min_lat': 55, 'min_lon': 11, 'max_lat': 55.01, 'max_lon': 11.01}]})
            bridge.validate_result(world, {'bbox': [55, 11, 55.001, 11.001]})
            with self.assertRaisesRegex(ValueError, 'does not cover'):
                bridge.validate_result(world, {'bbox': [56, 11, 56.001, 11.001]})

    def test_large_resume_reads_world_manifest_once_and_still_detects_missing_area(self):
        with tempfile.TemporaryDirectory() as directory:
            job = Path(directory)
            world = job/'worlds'/'Test'
            world.mkdir(parents=True)
            (world/'level.dat').write_bytes(b'test')
            areas = [{'min_lat':55, 'max_lat':55.001,
                      'min_lon':11+i*.001, 'max_lon':11+(i+1)*.001} for i in range(2000)]
            bridge.atomic_json(world/'arnis_one_world.json', {'areas':areas})
            cells = [{'status':'complete','bbox':[55,a['min_lon'],55.001,a['max_lon']]} for a in areas]
            doc = {'arnis':{'path':'unused'},'world_name':'Test','cells':cells}
            bridge.atomic_json(job/'plan.json',doc)
            args = argparse.Namespace(job=job,limit=None,timeout=60,credentials_file=None)
            with patch.object(bridge,'verify_inputs'), patch.object(bridge,'probe'), \
                    patch.object(bridge,'read_json',wraps=bridge.read_json) as read:
                bridge.run(args)
                manifest_reads = [c for c in read.call_args_list if Path(c.args[0]).name=='arnis_one_world.json']
                self.assertEqual(len(manifest_reads),1)
            cells[-1]['bbox']=[56,11,56.001,11.001]
            bridge.atomic_json(job/'plan.json',doc)
            with patch.object(bridge,'verify_inputs'), patch.object(bridge,'probe'):
                with self.assertRaisesRegex(ValueError,'does not cover'):
                    bridge.run(args)


if __name__ == '__main__':
    unittest.main()
