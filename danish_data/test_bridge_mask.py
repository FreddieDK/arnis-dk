import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from bridge_mask import BridgeMask, fetch_document
import meld_bridge


class BridgeMaskTests(unittest.TestCase):
    def document(self, tags=None):
        return {'arnis_dk_bridge_coverage': [54,10,57,13], 'elements': [
            {'type':'way', 'id':1, 'tags':tags or {'highway':'motorway','bridge':'yes'},
             'geometry':[{'lat':55,'lon':11},{'lat':55,'lon':11.2}]}]}

    def test_long_bridge_midspan_retained_but_distant_sea_not_retained(self):
        for tags in [{'highway':'motorway','bridge':'yes'},
                     {'railway':'rail','bridge':'viaduct'}]:
            mask=BridgeMask(self.document(tags),[54,10,57,13])
            self.assertTrue(mask.intersects([54.999,11.09,55.001,11.11]))
            self.assertTrue(mask.intersects([55.001,11.09,55.0015,11.11]))
            self.assertFalse(mask.intersects([55.03,11.09,55.04,11.11]))

    def test_non_bridge_and_tunnel_not_retained(self):
        mask=BridgeMask(self.document({'highway':'motorway','bridge':'no','tunnel':'yes'}),[54,10,57,13])
        self.assertFalse(mask.intersects([54.999,11.09,55.001,11.11]))

    def test_incomplete_or_uncovered_data_fails_closed(self):
        for change in [{'remark':'runtime error'},{'arnis_dk_bridge_coverage':[55,11,55.1,11.1]}]:
            doc=self.document();doc.update(change)
            with self.assertRaises(ValueError):BridgeMask(doc,[54,10,57,13])
        doc=self.document();doc['elements'][0]['geometry']=[]
        with self.assertRaises(ValueError):BridgeMask(doc,[54,10,57,13])

    def test_changed_snapshot_refuses_resume(self):
        with tempfile.TemporaryDirectory() as name:
            p=Path(name)/'bridges.json';p.write_text(json.dumps(self.document()))
            doc={'schema':2,'meld_source':name,'meld_hashes':{},'bridge_mask':meld_bridge.input_record(p)}
            p.write_text('{}')
            with patch.object(meld_bridge,'external_meld',return_value=(None,{})):
                with self.assertRaisesRegex(ValueError,'bridge_mask changed'):
                    meld_bridge.verify_inputs(doc)

    def test_download_failure_never_becomes_empty_mask(self):
        with patch('bridge_mask.urllib.request.urlopen',side_effect=OSError('offline')),patch('bridge_mask.time.sleep'):
            with self.assertRaisesRegex(ValueError,'Unable to fetch'):
                fetch_document([54,10,57,13])


if __name__=='__main__':unittest.main()
