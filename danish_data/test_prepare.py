import copy
from datetime import datetime, timezone
import json
from pathlib import Path
import tempfile
import unittest

from prepare import active, bbr_tags, prepare, rows


class PrepareTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.at = datetime(2026, 9, 20, tzinfo=timezone.utc)
        self.outline = {"id_lokalId": "GEODK-1", "BBRUUID": "BBR-1", "status": "Anlagt",
                        "geometristatus": "Endelig", "metode3D": "Terræn",
                        "geometri": "POLYGON Z ((650000 6150000 300,650010 6150000 300,650010 6150010 300,650000 6150010 300,650000 6150000 300))"}
        self.bbr = {"id_lokalId": "bbr-1", "status": "6", "husnummer": "DAR-1",
                    "byg054AntalEtager": 2, "byg032YdervæggensMateriale": "1",
                    "byg033Tagdækningsmateriale": "5", "byg026Opførelsesår": 1920}
        self.dar = {"id_lokalId": "dar-1", "status": "3", "husnummertekst": "12A"}

    def tearDown(self):
        self.tmp.cleanup()

    def run_prepare(self, outlines=None, bbr=None, dar=None):
        paths = []
        for label, data in (("geodk", outlines if outlines is not None else [self.outline]),
                            ("bbr", bbr if bbr is not None else [self.bbr]),
                            ("dar", dar if dar is not None else [self.dar])):
            path = self.root / (label + ".json")
            path.write_text(json.dumps(data), encoding="utf-8")
            paths.append(path)
        return prepare(*paths, bbox=[55, 11, 56, 12], at=self.at)

    def test_uuid_joins_and_no_height_from_absolute_z(self):
        doc = self.run_prepare()
        tags = next(e["tags"] for e in doc["elements"] if e["type"] == "way")
        self.assertEqual(tags["building:levels"], "2")
        self.assertEqual(tags["roof:material"], "roof_tiles")
        self.assertEqual(tags["addr:housenumber"], "12A")
        self.assertNotIn("height", tags)
        nodes = [e for e in doc["elements"] if e["type"] == "node"]
        self.assertTrue(all(55 < n["lat"] < 56 and 11 < n["lon"] < 12 for n in nodes))
        self.assertFalse(any("entrance" in e.get("tags", {}) for e in doc["elements"]))

    def test_decommissioned_and_future_records_are_not_used(self):
        old_bbr = {**self.bbr, "status": "10"}
        doc = self.run_prepare(bbr=[old_bbr])
        self.assertFalse(any("building:levels" in e.get("tags", {}) for e in doc["elements"]))
        future = {**self.outline, "virkningFra": "2027-01-01T00:00:00Z"}
        self.assertEqual(self.run_prepare(outlines=[future])["elements"], [])
        self.assertFalse(active({**self.dar, "virkningTil": self.at.isoformat()}, "3", self.at))

    def test_missing_or_duplicate_register_links_are_not_guessed(self):
        with self.assertRaisesRegex(ValueError, "Ambiguous"):
            self.run_prepare(bbr=[self.bbr, copy.deepcopy(self.bbr)])
        doc = self.run_prepare(bbr=[])
        self.assertEqual(doc["arnis_dk"]["counts"]["selected_buildings"], 1)
        self.assertNotIn("bbr_matches", doc["arnis_dk"]["counts"])

    def test_courtyard_is_an_inner_relation_ring(self):
        row = {**self.outline, "geometri": "POLYGON ((650000 6150000,650030 6150000,650030 6150030,650000 6150030,650000 6150000),(650005 6150005,650005 6150025,650025 6150025,650025 6150005,650005 6150005))"}
        doc = self.run_prepare(outlines=[row])
        relation = next(e for e in doc["elements"] if e["type"] == "relation")
        self.assertEqual([m["role"] for m in relation["members"]], ["outer", "inner"])
        self.assertEqual(relation["tags"]["building:levels"], "2")

    def test_repeatable_ids_and_unknown_materials(self):
        self.assertEqual(self.run_prepare(), self.run_prepare())
        tags = bbr_tags({**self.bbr, "byg032YdervæggensMateriale": "90", "byg033Tagdækningsmateriale": "90"})
        self.assertNotIn("building:material", tags)
        self.assertNotIn("roof:material", tags)

    def test_wrong_envelope_fails(self):
        path = self.root / "wrong.json"
        path.write_text('{"items": []}', encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "array"):
            list(rows(path))

    def access(self, standard='TD', x=650005, y=6150003):
        return {**self.dar, 'adgangspunkt': 'point-1', 'geoDanmarkBygning': 'GEODK-1',
                '_arnis_adgangspunkt': {'id_lokalId': 'point-1', 'status': '8',
                    'oprindelse_tekniskStandard': standard,
                    'position': {'crs': 25832, 'wkt': f'POINT ({x} {y})'}}}

    def test_qualified_access_points_are_hints_not_unprojected_osm_doors(self):
        for standard in ('TD', 'TK'):
            doc = self.run_prepare(dar=[self.access(standard)])
            tags = next(e['tags'] for e in doc['elements'] if e['type']=='way')
            self.assertEqual(tags['arnis:entrance:standard'], standard)
            self.assertTrue(55 < float(tags['arnis:entrance:lat']) < 56)
            self.assertFalse(any('entrance' in e.get('tags', {}) for e in doc['elements']))

    def test_complete_address_preserves_danish_text_without_guessing_missing_fields(self):
        doc=self.run_prepare(dar=[{**self.dar,'adgangsadressebetegnelse':'  Herrestræde 1C,\n4200 Slagelse  '}])
        tags=next(e['tags'] for e in doc['elements'] if e['type']=='way')
        self.assertEqual(tags['arnis:address'],'Herrestræde 1C, 4200 Slagelse')
        self.assertFalse(any('arnis:address' in e.get('tags',{}) for e in self.run_prepare()['elements']))

    def test_unqualified_wrong_building_and_broken_access_links_are_ignored(self):
        cases = [self.access(s) for s in ('TN','UF','TA')]
        cases += [self.access(x=650100), {**self.access(), 'geoDanmarkBygning':'other'},
                  {**self.access(), 'adgangspunkt':'other'}, {**self.access(), 'geoDanmarkBygning':None}]
        for field,value in [('status','9'),('position',{'crs':4326,'wkt':'POINT (11 55)'}),
                            ('position',{'crs':25832,'wkt':'broken'}),
                            ('virkningTil',self.at.isoformat())]:
            row=self.access();row['_arnis_adgangspunkt'][field]=value;cases.append(row)
        for row in cases:
            doc=self.run_prepare(dar=[row])
            self.assertFalse(any('arnis:entrance:lat' in e.get('tags',{}) for e in doc['elements']))


    def test_multiple_house_numbers_on_one_building_preserve_individual_points(self):
        addresses=[]
        for n,x in [(14,650003),(16,650007)]:
            row=self.access(x=x)
            row.update(id_lokalId=f'dar-{n}',adgangsadressebetegnelse=f'Testvej {n}, 4200 Slagelse')
            addresses.append(row)
        for bbr in ([],[self.bbr]):
            doc=self.run_prepare(bbr=bbr,dar=addresses)
            tags=next(e['tags'] for e in doc['elements'] if e['type']=='way')
            hints=json.loads(tags['arnis:entrances'])
            self.assertEqual([h['address'] for h in hints],['Testvej 14, 4200 Slagelse','Testvej 16, 4200 Slagelse'])
            self.assertNotEqual(hints[0]['lon'],hints[1]['lon'])
            self.assertEqual(doc['arnis_dk']['counts']['entrance_hints_TD'],2)
        invalid=copy.deepcopy(addresses)
        invalid[0]['geoDanmarkBygning']='neighbour'
        invalid[1]['_arnis_adgangspunkt']['oprindelse_tekniskStandard']='TN'
        tags=next(e['tags'] for e in self.run_prepare(dar=invalid)['elements'] if e['type']=='way')
        self.assertNotIn('arnis:entrances',tags)
        with self.assertRaisesRegex(ValueError,'Ambiguous'):
            self.run_prepare(dar=addresses+[addresses[0]])


if __name__ == "__main__":
    unittest.main()
