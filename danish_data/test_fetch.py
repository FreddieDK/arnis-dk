import json
import os
from pathlib import Path
import tempfile
import unittest
from datetime import datetime, timezone
from unittest.mock import patch
from urllib.error import HTTPError, URLError

from fetch import (ALIASES, ENTITIES, atomic_json, bounds, download_rows, fetch,
                   linked_rows, load_key, request_page)


NOW = datetime(2026, 10, 9, tzinfo=timezone.utc)
BBOX = [55.400, 11.350, 55.403, 11.355]


def page(register, rows, more=False, cursor=None):
    return {'data': {ENTITIES[register]: {'nodes': rows, 'pageInfo': {
        'hasNextPage': more, 'endCursor': cursor}}}}


class FetchTests(unittest.TestCase):
    def test_access_point_is_joined_by_its_id_with_same_snapshot(self):
        records = {
            'GEODKV':[{'id_lokalId':'geo','status':'Anlagt','geometristatus':'Endelig','BBRUUID':'bbr',
                      'geometri':{'crs':25832,'wkt':'POLYGON EMPTY'}}],
            'BBR':[{'id_lokalId':'bbr','status':'6','husnummer':'dar'}],
            'DAR':[{'id_lokalId':'dar','status':'3','adgangspunkt':'POINT-1'}],
            'DAR_POINT':[{'id_lokalId':'point-1','status':'8','oprindelse_tekniskStandard':'TD',
                          'position':{'wkt':'POINT (650005 6150003)','crs':25832}}]}
        def transport(register,query,variables,key):
            self.assertEqual(variables['at'],NOW.isoformat())
            if register=='DAR_POINT':
                self.assertEqual(variables['where']['id_lokalId']['in'],['point-1'])
                self.assertIn('DAR_Adressepunkt',query)
            return page(register,records[register])
        _,_,dar,_=fetch(BBOX,'secret',NOW,transport)
        self.assertEqual(dar[0]['_arnis_adgangspunkt'],records['DAR_POINT'][0])
        records['DAR_POINT'][0]['status']='9'
        self.assertIsNone(fetch(BBOX,'secret',NOW,transport)[2][0]['_arnis_adgangspunkt'])
        records['DAR_POINT'][0]['status']='8'
        records['DAR_POINT'].append(records['DAR_POINT'][0].copy())
        with self.assertRaisesRegex(ValueError,'ambiguous'):
            fetch(BBOX,'secret',NOW,transport)

    def test_bounded_query_and_uuid_joins_normalize_api_fields(self):
        calls = []
        outline = {'id_lokalId': 'geo-1', 'BBRUUID': 'BBR-1', 'status': 'Anlagt',
                   'geometristatus': 'Endelig', 'metode3D': 'Terræn',
                   'geometri': {'crs': 25832, 'wkt': 'POLYGON ((650000 6150000,650010 6150000,650010 6150010,650000 6150000))'}}
        records = {'GEODKV': [outline], 'BBR': [{'id_lokalId': 'bbr-1', 'status': '6',
                   'husnummer': 'DAR-1', 'byg032YdervaeggensMateriale': '1',
                   'byg033Tagdaekningsmateriale': '5', 'byg026Opfoerelsesaar': 1920}],
                   'DAR': [{'id_lokalId': 'dar-1', 'status': '3', 'husnummertekst': '16'}]}
        def transport(register, query, variables, key):
            calls.append((register, query, variables))
            self.assertEqual(key, 'secret')
            self.assertEqual(variables['at'], NOW.isoformat())
            self.assertNotIn('secret', query)
            return page(register, records[register])
        geo, bbr, dar, at = fetch(BBOX, 'secret', NOW, transport)
        self.assertEqual(at, NOW)
        geometry = calls[0][2]['where']['geometri']['intersects']
        self.assertEqual(geometry['crs'], 25832)
        self.assertIn('POLYGON', geometry['wkt'])
        self.assertEqual(calls[1][2]['where']['id_lokalId']['in'], ['bbr-1'])
        self.assertEqual(calls[2][2]['where']['id_lokalId']['in'], ['dar-1'])
        self.assertEqual(geo[0]['geometri'], outline['geometri']['wkt'])
        for api_name, local_name in ALIASES.items():
            self.assertEqual(bbr[0][local_name], records['BBR'][0][api_name])
        self.assertEqual(dar[0]['husnummertekst'], '16')

    def test_wrong_crs_stops_before_linked_register_requests(self):
        def transport(register, *_):
            self.assertEqual(register, 'GEODKV')
            return page(register, [{'status': 'Anlagt', 'geometristatus': 'Endelig',
                                    'geometri': {'crs': 4326, 'wkt': 'POLYGON EMPTY'}}])
        with self.assertRaisesRegex(ValueError, '25832'):
            fetch(BBOX, 'secret', NOW, transport)

    def test_empty_outline_response_never_downloads_entire_linked_register(self):
        calls = []
        def transport(register, *_):
            calls.append(register)
            return page(register, [])
        self.assertEqual(fetch(BBOX, 'secret', NOW, transport)[:3], ([], [], []))
        self.assertEqual(calls, ['GEODKV'])

    def test_id_batches_respect_official_limit_and_reject_unrequested_rows(self):
        batches = []
        def transport(register, query, variables, key):
            ids = variables['where']['id_lokalId']['in']
            batches.append(ids)
            return page(register, [{'id_lokalId': value} for value in ids])
        result = linked_rows('BBR', [str(n) for n in range(205)] + ['0', None], NOW, 'key', transport)
        self.assertEqual([len(batch) for batch in batches], [100, 100, 5])
        self.assertEqual(len(result), 205)
        with self.assertRaisesRegex(ValueError, 'unrequested'):
            linked_rows('BBR', ['one'], NOW, 'key', lambda register, *_: page(register, [{'id_lokalId': 'other'}]))

    def test_pagination_keeps_snapshot_and_fetches_all_pages(self):
        cursors = []
        def transport(register, query, variables, key):
            cursors.append(variables['after'])
            self.assertEqual(variables['at'], NOW.isoformat())
            return page(register, [{'id_lokalId': str(len(cursors))}], len(cursors) == 1, 'second')
        result = download_rows('BBR', {}, NOW, 'key', transport)
        self.assertEqual(cursors, [None, 'second'])
        self.assertEqual(len(result), 2)

    def test_partial_malformed_and_stuck_pagination_fail(self):
        responses = [
            {'errors': [{'message': 'partial'}], **page('BBR', [])},
            {'data': None},
            {'data': {'BBR_Bygning': {'nodes': [], 'pageInfo': {'hasNextPage': 'false'}}}},
            page('BBR', [], True, 'cursor'),
            page('BBR', [{'id_lokalId': 'same'}], True, 'repeated'),
        ]
        for response in responses:
            with self.subTest(response=response), self.assertRaises(ValueError):
                download_rows('BBR', {}, NOW, 'key', lambda *_: response)

    def test_invalid_bbox_and_naive_time_fail_before_network(self):
        for value in ['55,11,55,12', 'nan,11,56,12', '55,11,56,12', '0,0,0.001,0.001']:
            with self.subTest(value=value), self.assertRaises(ValueError):
                bounds(value)
        with self.assertRaisesRegex(ValueError, 'timezone'):
            fetch(BBOX, 'key', datetime(2026, 1, 1))

    def test_key_file_and_environment_do_not_need_command_line_secrets(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {}, clear=True):
            file = Path(tmp) / 'local.env'
            file.write_text('\ufeff# local only\nDATAFORDELER_API_KEY="secret"\n', encoding='utf-8')
            self.assertEqual(load_key(file), 'secret')
            with self.assertRaises(ValueError):
                load_key()
            with patch.dict(os.environ, {'DATAFORDELER_API_KEY': 'env-secret'}):
                self.assertEqual(load_key(), 'env-secret')

    def test_http_errors_never_expose_authenticated_url_or_key(self):
        error = HTTPError('https://example.invalid/?apiKey=secret', 403, 'secret', {}, None)
        with patch('fetch.urlopen', side_effect=error), self.assertRaises(ValueError) as caught:
            request_page('BBR', '{}', {}, 'secret')
        self.assertIn('403', str(caught.exception))
        self.assertNotIn('secret', str(caught.exception))
        with patch('fetch.urlopen', side_effect=URLError('secret')), patch('fetch.time.sleep'), self.assertRaises(ValueError) as caught:
            request_page('BBR', '{}', {}, 'secret')
        self.assertNotIn('secret', str(caught.exception))

    def test_failed_serialization_keeps_previous_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / 'supplement.json'
            atomic_json(output, {'old': True})
            with self.assertRaises(ValueError):
                atomic_json(output, {'bad': float('nan')})
            self.assertEqual(json.loads(output.read_text()), {'old': True})
            self.assertEqual(list(Path(tmp).iterdir()), [output])


if __name__ == '__main__':
    unittest.main()
