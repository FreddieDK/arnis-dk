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
                    patch.object(bridge, 'validate_result'), \
                    patch.object(bridge.subprocess, 'run') as process:
                process.return_value.returncode = 0
                bridge.run(args)
                self.assertEqual(process.call_count, 1)
                self.assertNotIn('DATAFORDELER_API_KEY', process.call_args.kwargs['env'])
            self.assertEqual([c['status'] for c in bridge.read_json(job/'plan.json')['cells']],
                             ['complete', 'complete', 'pending', 'skipped_ocean'])
            with patch.object(bridge, 'verify_inputs'), patch.object(bridge, 'probe'), \
                    patch.object(bridge, 'validate_result'), \
                    patch.object(bridge.subprocess, 'run') as process:
                process.return_value.returncode = 2
                with self.assertRaisesRegex(ValueError, 'exit code 2'):
                    bridge.run(args)
            self.assertEqual(bridge.read_json(job/'plan.json')['cells'][2]['status'], 'failed')
            self.assertFalse((job/'runner.lock').exists())

    def test_result_requires_world_manifest_covering_cell(self):
        with tempfile.TemporaryDirectory() as name:
            world = Path(name)
            (world/'level.dat').write_bytes(b'test')
            bridge.atomic_json(world/'arnis_one_world.json', {'areas': [
                {'min_lat': 55, 'min_lon': 11, 'max_lat': 55.01, 'max_lon': 11.01}]})
            bridge.validate_result(world, {'bbox': [55, 11, 55.001, 11.001]})
            with self.assertRaisesRegex(ValueError, 'does not cover'):
                bridge.validate_result(world, {'bbox': [56, 11, 56.001, 11.001]})


if __name__ == '__main__':
    unittest.main()
