import io
import json
import subprocess
import sys
from pathlib import Path
import tempfile
import unittest
import zlib

import numpy as np
from nbtlib import Compound, File, Int, List, String

import seal_void_edges as edge


def area(x,z,X,Z):return dict(min_x=x,min_z=z,max_x=X,max_z=Z)


def chunk():
    palette=[Compound({'Name':String('minecraft:'+n)}) for n in
             ['stone','air','diamond_ore','sand','water']]
    sections=[]
    for sy in [-2,-1,0]:
        values=np.zeros(4096,dtype=np.uint16)
        if sy==-1:
            values[4*256:8*256]=1  # A cave running through the edge.
            values[3*256:4*256]=2  # Exposed ore below that cave.
            values[14*256:15*256]=3  # Sand seabed at Y=-2.
            values[15*256:]=4        # Water surface at Y=-1.
        elif sy==0:values[:]=1
        s=Compound({'Y':Int(sy)});edge.encode_states(s,palette,values);sections.append(s)
    return File({'DataVersion':Int(4189),'xPos':Int(0),'zPos':Int(0),
                 'sections':List[Compound](sections),'block_entities':List[Compound]([])})


def block(c,x,y,z):
    section=next(s for s in c['sections'] if int(s['Y'])==y//16)
    p,v=edge.decode_states(section)
    return str(p[v[(y%16)*256+z*16+x]]['Name'])


class EdgeTests(unittest.TestCase):
    def test_shared_tile_seam_not_sealed_and_outer_wall_exactly_two_columns(self):
        regions=edge.edge_columns([area(-16,0,-1,31),area(0,0,15,31)])
        def selected(x,z):return (x%16,z%16) in regions.get((x//512,z//512),{}).get((x//16%32,z//16%32),[])
        for x in [-16,-15,14,15]:self.assertTrue(selected(x,15))
        for x in [-14,-1,0,13]:self.assertFalse(selected(x,15))
        self.assertFalse(selected(-17,15))

    def test_internal_void_hole_is_sealed_but_completed_neighbours_are_not(self):
        areas=[area(x,z,x+15,z+15) for x in [0,16,32] for z in [0,16,32] if (x,z)!=(16,16)]
        regions=edge.edge_columns(areas)
        cols=regions[0,0][0,1]
        self.assertIn((15,8),cols);self.assertIn((14,8),cols)
        self.assertNotIn((13,8),cols)

    def test_seals_caves_and_ores_but_preserves_seabed_water_and_interior(self):
        c=chunk();cols=[(x,z) for x in [0,1] for z in range(16)]
        self.assertGreater(edge.seal_chunk(c,cols),0)
        for x in [0,1]:
            for y in range(-32,-2):self.assertEqual(block(c,x,y,8),'minecraft:bedrock')
            self.assertEqual(block(c,x,-2,8),'minecraft:sand')
            self.assertEqual(block(c,x,-1,8),'minecraft:water')
            self.assertEqual(block(c,x,0,8),'minecraft:air')
        self.assertEqual(block(c,2,-13,8),'minecraft:diamond_ore')
        self.assertEqual(block(c,2,-10,8),'minecraft:air')
        self.assertEqual(edge.seal_chunk(c,cols),0)

    def test_palette_resize_and_signed_long_roundtrip(self):
        for count in [1,16,17,32,33,257]:
            p=[Compound({'Name':String('minecraft:test_'+str(i))}) for i in range(count)]
            values=(np.arange(4096)%count).astype(np.uint16)
            s=Compound();edge.encode_states(s,p,values)
            result,decoded=edge.decode_states(s)
            self.assertEqual(len(result),count);np.testing.assert_array_equal(decoded,values)

    def fixture(self,root):
        world=root/'worlds'/'test';(world/'region').mkdir(parents=True)
        (root/'plan.json').write_text(json.dumps({'world_name':'test','cells':[{'status':'complete'}]}))
        (world/'arnis_one_world.json').write_text(json.dumps({'areas':[area(0,0,15,15)]}))
        f=io.BytesIO();chunk().write(f)
        payload=b'\x02'+zlib.compress(f.getvalue())
        # Unrelated chunk outside the manifest is retained byte for byte.
        raw=edge.region_bytes({0:payload,31:payload},bytes(4096))
        path=world/'region'/'r.0.0.mca';path.write_bytes(raw)
        return world,path,raw,payload

    def test_disk_roundtrip_backup_restore_idempotence_and_unrelated_chunk(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);world,path,original,payload=self.fixture(root)
            state=edge.seal(root);self.assertEqual(state['phase'],'complete')
            self.assertNotEqual(path.read_bytes(),original)
            chunks,_=edge.region_payloads(path);self.assertEqual(chunks[31],payload)
            after=path.read_bytes();self.assertEqual(edge.seal(root),state)
            self.assertEqual(path.read_bytes(),after)
            # Simulate interruption after region replacement but before completion.
            state['phase']='applying';edge.atomic_json(root/'void-edge-seal.json',state)
            self.assertEqual(edge.seal(root)['phase'],'complete')
            self.assertEqual(path.read_bytes(),after)
            edge.restore(root);self.assertEqual(path.read_bytes(),original)
            self.assertEqual(edge.seal(root)['phase'],'complete')

    def test_rejects_world_locked_by_another_process(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);world,path,original,_=self.fixture(root)
            code="from pathlib import Path; import sys; from seal_void_edges import world_lock; w=Path(sys.argv[1]); lock=world_lock(w); lock.__enter__(); print('locked',flush=True); sys.stdin.read(1); lock.__exit__(None,None,None)"
            process=subprocess.Popen([sys.executable,'-c',code,str(world)],
                cwd=Path(edge.__file__).parent,stdin=subprocess.PIPE,stdout=subprocess.PIPE,text=True)
            try:
                self.assertEqual(process.stdout.readline().strip(),'locked')
                with self.assertRaisesRegex(ValueError,'World is in use'):edge.seal(root)
                self.assertEqual(path.read_bytes(),original)
            finally:
                process.communicate('x',timeout=10)
            self.assertEqual(process.returncode,0)

    def test_refuses_incomplete_job_and_will_not_restore_over_later_edits(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);world,path,original,_=self.fixture(root)
            p=root/'plan.json';doc=json.loads(p.read_text());doc['cells'][0]['status']='pending';p.write_text(json.dumps(doc))
            with self.assertRaisesRegex(ValueError,'Finish all'):edge.seal(root)
            self.assertEqual(path.read_bytes(),original)
            doc['cells'][0]['status']='complete';p.write_text(json.dumps(doc));edge.seal(root)
            with path.open('ab') as f:f.write(b'later edit')
            with self.assertRaisesRegex(ValueError,'later edits'):edge.restore(root)


if __name__=='__main__':unittest.main()
