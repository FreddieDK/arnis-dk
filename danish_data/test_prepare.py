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


if __name__ == "__main__":
    unittest.main()
