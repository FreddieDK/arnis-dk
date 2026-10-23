import json
import os
from pathlib import Path
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch
from urllib.error import HTTPError, URLError

from shapely import from_wkt
from shapely.ops import unary_union

from fetch import (ALIASES, AREA_KM2, ENTITIES, REQUEST_KM2, atomic_json, bounds, download_rows,
                   fetch, linked_rows, load_key, progress_printer, request_page)


NOW = datetime(2026, 10, 9, tzinfo=timezone.utc)
BBOX = [55.400, 11.350, 55.403, 11.355]


def page(register, rows, more=False, cursor=None):
    return {'data': {ENTITIES[register]: {'nodes': rows, 'pageInfo': {
        'hasNextPage': more, 'endCursor': cursor}}}}


class Response:
    """What urlopen returns, for a JSON document."""
    def __init__(self, document):
        self.raw = json.dumps(document).encode()

    def read(self, limit):
        return self.raw[:limit]

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class FetchTests(unittest.TestCase):
    def test_access_point_is_joined_by_its_id_with_same_snapshot(self):
        records = {
            'GEODKV':[{'id_lokalId':'geo','status':'Anlagt','geometristatus':'Endelig','BBRUUID':'bbr',
                      'geometri':{'crs':25832,'wkt':'POLYGON EMPTY'}}],
            'BBR':[{'id_lokalId':'bbr','status':'6','husnummer':'dar'}],
            'DAR':[{'id_lokalId':'dar','status':'3','adgangspunkt':'POINT-1','geoDanmarkBygning':'geo'}],
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
                   'DAR': [{'id_lokalId': 'dar-1', 'status': '3', 'husnummertekst': '16','geoDanmarkBygning':'geo-1'}]}
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

    def test_secondary_house_numbers_are_fetched_without_primary_bbr_link(self):
        def transport(register, query, variables, key):
            self.assertEqual(variables['at'], NOW.isoformat())
            if register == 'GEODKV':
                return page(register, [{'id_lokalId':'geo','status':'Anlagt','geometristatus':'Endelig',
                                       'geometri':{'crs':25832,'wkt':'POLYGON EMPTY'}}])
            if register == 'DAR':
                self.assertEqual(variables['where'], {'geoDanmarkBygning':{'in':['geo']}})
                return page(register, [{'id_lokalId':f'dar-{n}','status':'3',
                                       'geoDanmarkBygning':'geo','adgangspunkt':f'point-{n}'} for n in (14,16)])
            if register == 'DAR_POINT':
                return page(register, [{'id_lokalId':f'point-{n}','status':'8'} for n in (14,16)])
            self.fail('No BBR request without a BBR link')
        _,_,dar,_ = fetch(BBOX,'secret',NOW,transport)
        self.assertEqual([r['id_lokalId'] for r in dar], ['dar-14','dar-16'])
        self.assertTrue(all(r['_arnis_adgangspunkt'] for r in dar))
        with self.assertRaisesRegex(ValueError,'unrequested'):
            linked_rows('DAR',['geo'],NOW,'secret',lambda register,*_:page(register,[
                {'id_lokalId':'dar','geoDanmarkBygning':'neighbour'}]),field='geoDanmarkBygning')

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
        self.assertEqual(sorted(len(batch) for batch in batches), [5, 100, 100])
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

    def test_a_large_area_is_asked_for_in_tiles_and_each_building_kept_once(self):
        # About 8 x 8 km: nine tiles of under 3 x 3 km.
        bbox = [55.400, 11.300, 55.472, 11.427]
        shared = {'id_lokalId': 'geo-1', 'BBRUUID': 'bbr-1', 'status': 'Anlagt',
                  'geometristatus': 'Endelig', 'geometri': {'crs': 25832, 'wkt': 'POLYGON EMPTY'}}
        tiles, linked = [], []
        def transport(register, query, variables, key):
            if register == 'GEODKV':
                tiles.append(from_wkt(variables['where']['geometri']['intersects']['wkt']))
                # Every tile returns the building on its border.
                return page(register, [dict(shared)])
            linked.append(variables['where'])
            return page(register, [{'id_lokalId': 'bbr-1', 'status': '6'}] if register == 'BBR' else [])
        geo, bbr, _, _ = fetch(bbox, 'key', NOW, transport)
        self.assertEqual(len(tiles), 9)
        self.assertTrue(all(tile.area < REQUEST_KM2 * 1e6 for tile in tiles))
        self.assertGreater(unary_union(tiles).area, 60e6)
        self.assertEqual([row['id_lokalId'] for row in geo], ['geo-1'])
        self.assertEqual(len(bbr), 1)
        self.assertEqual(linked[0], {'id_lokalId': {'in': ['bbr-1']}})

    def test_tiles_that_return_different_versions_of_one_record_stop(self):
        bbox = [55.400, 11.300, 55.472, 11.427]
        versions = iter(range(100))
        def transport(register, *_):
            return page(register, [{'id_lokalId': 'geo-1', 'status': 'Anlagt', 'geometristatus': 'Endelig',
                                    'BBRUUID': f'bbr-{next(versions)}',
                                    'geometri': {'crs': 25832, 'wkt': 'POLYGON EMPTY'}}])
        with self.assertRaisesRegex(ValueError, 'different versions'):
            fetch(bbox, 'key', NOW, transport)

    def test_a_request_stays_under_ten_and_a_download_under_a_hundred_square_kilometres(self):
        self.assertEqual(len(bounds('55.400,11.300,55.472,11.427', AREA_KM2)), 4)
        with self.assertRaisesRegex(ValueError, '10 square'):
            bounds('55.400,11.300,55.472,11.427')
        with self.assertRaisesRegex(ValueError, '100 square'):
            fetch([55.0, 11.0, 55.2, 11.3], 'key', NOW, lambda *_: self.fail('No request'))

    def test_progress_follows_each_phase_to_its_end(self):
        records = {
            'GEODKV': [{'id_lokalId': 'geo', 'status': 'Anlagt', 'geometristatus': 'Endelig', 'BBRUUID': 'bbr',
                        'geometri': {'crs': 25832, 'wkt': 'POLYGON EMPTY'}}],
            'BBR': [{'id_lokalId': 'bbr', 'status': '6', 'husnummer': 'dar'}],
            'DAR': [{'id_lokalId': 'dar', 'status': '3', 'adgangspunkt': 'point', 'geoDanmarkBygning': 'geo'}],
            'DAR_POINT': [{'id_lokalId': 'point', 'status': '8'}]}
        heard = []
        fetch([55.400, 11.300, 55.472, 11.427], 'key', NOW, lambda register, *_: page(register, records[register]),
              progress=lambda *call: heard.append(call))
        phases = list(dict.fromkeys(phase for phase, _, _ in heard))
        self.assertEqual(phases, ['buildings', 'bbr', 'addresses', 'building-addresses', 'entrances'])
        for phase in phases:
            calls = [(done, total) for name, done, total in heard if name == phase]
            self.assertEqual(calls[0][0], 0)
            self.assertEqual(calls[-1][0], calls[-1][1])
        self.assertEqual([c for c in heard if c[0] == 'buildings'][-1], ('buildings', 9, 9))

    def test_the_progress_lines_show_about_every_tenth_and_the_end(self):
        report = progress_printer()
        with patch('builtins.print') as printed:
            for done in range(0, 101):
                report('heights', done, 100)
            report('bbr', 0, 0)
        lines = [call.args[0] for call in printed.call_args_list]
        self.assertEqual(lines[0], 'progress heights 0 100')
        self.assertEqual(lines[-2], 'progress heights 100 100')
        self.assertEqual(len(lines), 12)
        self.assertEqual(lines[-1], 'progress bbr 0 0')

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

    def test_a_page_the_service_fails_now_and_then_is_asked_again(self):
        failed = {'errors': [{'message': 'Unexpected Execution Error', 'path': ['BBR_Bygning']}]}
        answers = [failed, failed, page('BBR', [{'id_lokalId': 'bbr'}])]
        with patch('fetch.urlopen', side_effect=lambda *a, **k: Response(answers.pop(0))), \
                patch('fetch.time.sleep') as sleep:
            result = request_page('BBR', '{}', {}, 'secret')
        self.assertEqual(result['data'][ENTITIES['BBR']]['nodes'], [{'id_lokalId': 'bbr'}])
        self.assertEqual(sleep.call_count, 2)
        with patch('fetch.urlopen', side_effect=lambda *a, **k: Response(failed)), \
                patch('fetch.time.sleep'), self.assertRaises(ValueError) as caught:
            request_page('BBR', '{}', {}, 'secret')
        self.assertIn('GraphQL rejected the query: Unexpected Execution Error', str(caught.exception))

    def test_a_rejected_query_is_not_asked_again(self):
        rejected = {'errors': [{'message': 'The field `nope` does not exist'}]}
        with patch('fetch.urlopen', return_value=Response(rejected)) as opened, \
                patch('fetch.time.sleep'), self.assertRaises(ValueError) as caught:
            request_page('BBR', '{}', {}, 'secret')
        self.assertEqual(opened.call_count, 1)
        self.assertIn('`nope` does not exist', str(caught.exception))
        self.assertNotIn('secret', str(caught.exception))

    def test_through_the_mcdk_cache_no_key_is_sent(self):
        sent = []

        def opened(request, timeout):
            sent.append(request.full_url)
            return Response(page('BBR', []))
        cache = {'MCDK_CACHE_URL': 'http://10.0.40.40/'}
        with patch.dict(os.environ, cache), patch('fetch.urlopen', side_effect=opened):
            os.environ.pop('DATAFORDELER_API_KEY', None)
            request_page('BBR', '{}', {}, 'secret')
            request_page('DAR_POINT', '{}', {}, '')
            self.assertEqual(load_key(), '')
        self.assertEqual(sent, ['http://10.0.40.40/graphql.datafordeler.dk/BBR/v2',
                                'http://10.0.40.40/graphql.datafordeler.dk/DAR/v2'])
        with patch.dict(os.environ), patch('fetch.urlopen', side_effect=opened):
            os.environ.pop('MCDK_CACHE_URL', None)
            request_page('BBR', '{}', {}, 'secret')
        self.assertEqual(sent[-1], 'https://graphql.datafordeler.dk/BBR/v2?apiKey=secret')

    def test_through_the_mcdk_cache_every_query_asks_for_the_start_of_the_month(self):
        # The cache keys a query on its body; `at` = now would make every body new.
        asked = []

        def transport(register, query, variables, key):
            asked.append(variables['at'])
            return page(register, [])
        with patch.dict(os.environ, {'MCDK_CACHE_URL': 'http://10.0.40.40'}):
            *_, at = fetch(BBOX, '', transport=transport)
        self.assertEqual((at.day, at.hour, at.minute, at.second, at.microsecond, at.utcoffset()),
                         (1, 0, 0, 0, 0, timedelta(0)))
        self.assertEqual(set(asked), {at.isoformat()})

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
