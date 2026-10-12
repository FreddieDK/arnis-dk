"""Seal the final One World footprint with two underground bedrock columns.

Operates offline, with Minecraft's session lock, atomic region replacement and
reversible backups. Never run against an active server or an unfinished job.
"""
import argparse
from contextlib import contextmanager
import gzip
import hashlib
import io
import json
import math
import os
from pathlib import Path
import shutil
import struct
import time
import uuid
import zlib

import nbtlib
import numpy as np
from nbtlib import Byte, Compound, Int, List, LongArray, String
from shapely.geometry import Point, box
from shapely.ops import unary_union
from shapely.prepared import prep

from meld_bridge import atomic_json, read_json

TERRAIN = set(('grass_block dirt coarse_dirt rooted_dirt podzol mycelium sand '
    'red_sand gravel clay mud packed_mud stone deepslate granite diorite andesite '
    'tuff calcite snow_block sandstone red_sandstone moss_block bedrock').split())


def checksum(path):
    with Path(path).open('rb') as f:
        return hashlib.file_digest(f, 'sha256').hexdigest()


@contextmanager
def world_lock(world):
    with (world/'session.lock').open('a+b') as f:
        try:
            if os.name == 'nt':
                import msvcrt
                f.seek(0); msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.lockf(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise ValueError('World is in use; stop Minecraft/Arnis first') from None
        try:
            yield
        finally:
            if os.name == 'nt':
                f.seek(0); msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.lockf(f, fcntl.LOCK_UN)


def edge_columns(areas):
    """Union first: shared tile seams cannot become walls. Bounds are inclusive."""
    rectangles=[]
    for a in areas:
        x,z,X,Z=(a[k] for k in ('min_x','min_z','max_x','max_z'))
        if any(not isinstance(v,int) for v in (x,z,X,Z)) or x>X or z>Z:
            raise ValueError('Invalid manifest area')
        if x%16 or z%16 or (X+1)%16 or (Z+1)%16:
            raise ValueError('Expected chunk-aligned One World areas')
        rectangles.append(box(x,z,X+1,Z+1))
    if not rectangles:
        raise ValueError('No completed world areas')
    footprint=unary_union(rectangles)
    shell=prep(footprint.difference(footprint.buffer(-2,join_style=2)))
    boundary=footprint.boundary
    lines=list(boundary.geoms) if hasattr(boundary,'geoms') else [boundary]
    candidates=set()
    for line in lines:
        coords=list(line.coords)
        for (x,z),(X,Z) in zip(coords,coords[1:]):
            for cx in range(math.floor((min(x,X)-2)/16), math.floor((max(x,X)+2)/16)+1):
                for cz in range(math.floor((min(z,Z)-2)/16), math.floor((max(z,Z)+2)/16)+1):
                    candidates.add((cx,cz))
    regions={}
    for cx,cz in sorted(candidates):
        columns=[(x,z) for z in range(16) for x in range(16)
                 if shell.contains(Point(cx*16+x+.5,cz*16+z+.5))]
        if columns:
            regions.setdefault((cx//32,cz//32),{})[(cx%32,cz%32)]=columns
    return regions


def decode_states(section):
    states=section.get('block_states')
    if not states:
        return [Compound({'Name':String('minecraft:air')})],np.zeros(4096,dtype=np.uint16)
    palette=list(states['palette'])
    if not palette:
        raise ValueError('Empty block palette')
    if len(palette)==1:
        return palette,np.zeros(4096,dtype=np.uint16)
    bits=max(4,(len(palette)-1).bit_length());per=64//bits
    data=states.get('data',[])
    if len(data)!=(4096+per-1)//per:
        raise ValueError('Unsupported block-state packing')
    indices=np.arange(4096,dtype=np.uint64)
    words=np.asarray(data,dtype=np.int64).view(np.uint64)
    values=((words[indices//per] >> ((indices%per)*bits)) & ((1<<bits)-1)).astype(np.uint16)
    if values.max()>=len(palette):raise ValueError('Invalid palette index')
    return palette,values


def encode_states(section,palette,values):
    states=Compound({'palette':List[Compound](palette)})
    if len(palette)>1:
        bits=max(4,(len(palette)-1).bit_length());per=64//bits
        padded=np.zeros(((4096+per-1)//per)*per,dtype=np.uint64)
        padded[:4096]=values
        words=np.bitwise_or.reduce(padded.reshape(-1,per) << (np.arange(per,dtype=np.uint64)*bits),axis=1)
        states['data']=LongArray(words.view(np.int64))
    section['block_states']=states


def seal_chunk(chunk,columns):
    if int(chunk.get('DataVersion',0))<2860 or 'sections' not in chunk:
        raise ValueError('Expected a modern Java chunk with sections')
    sections={int(s['Y']):s for s in chunk['sections']}
    decoded={y:decode_states(s) for y,s in sections.items()}
    if not sections:raise ValueError('Missing terrain at the expected world edge')
    floor=min(sections)*16
    col_indices=np.array([z*16+x for x,z in columns])
    heights=np.full(len(columns),floor-1,dtype=np.int32)
    for sy in sorted(sections,reverse=True):
        palette,values=decoded[sy]
        names=[str(p['Name']).removeprefix('minecraft:') for p in palette]
        terrain=np.array([n in TERRAIN or n.endswith('_ore') for n in names])
        candidates=np.where(terrain[values.reshape(16,256)[:,col_indices]],
                            np.arange(sy*16,sy*16+16)[:,None],floor-1).max(axis=0)
        heights=np.maximum(heights,candidates)
        if np.all(heights>=floor):break
    if np.any(heights<floor):raise ValueError('No terrain found in an edge column')
    tops=dict(zip(columns,heights.tolist()))
    changed=0
    for sy in range(floor//16,max(tops.values())//16+1):
        section=sections.get(sy,Compound({'Y':Int(sy)}))
        palette,values=decoded.get(sy,decode_states(section))
        rock=next((i for i,p in enumerate(palette) if str(p['Name'])=='minecraft:bedrock'),None)
        if rock is None:rock=len(palette);palette.append(Compound({'Name':String('minecraft:bedrock')}))
        selected=np.zeros((16,256),dtype=bool)
        selected[:,col_indices]=np.arange(sy*16,sy*16+16)[:,None]<heights
        dirty=selected.ravel() & (values!=rock)
        if np.any(dirty):
            changed+=int(np.count_nonzero(dirty));values[dirty]=rock
            encode_states(section,palette,values);sections[sy]=section
    if changed:
        chunk['sections']=List[Compound]([sections[y] for y in sorted(sections)])
        chunk['isLightOn']=Byte(0)
        for section in sections.values():
            section.pop('SkyLight',None);section.pop('BlockLight',None)
        for key in ('block_entities','block_ticks','fluid_ticks'):
            if key in chunk:
                chunk[key]=List[Compound]([v for v in chunk[key] if not (
                    (int(v['x'])%16,int(v['z'])%16) in tops and
                    floor<=int(v['y'])<tops[int(v['x'])%16,int(v['z'])%16])])
    return changed


def region_payloads(path):
    raw=path.read_bytes()
    if len(raw)<8192:raise ValueError('Truncated region header')
    chunks={};timestamps=raw[4096:8192]
    for i in range(1024):
        loc=int.from_bytes(raw[i*4:i*4+4],'big');offset=(loc>>8)*4096;size=(loc&255)*4096
        if not loc:continue
        if offset<8192 or not size or offset+size>len(raw):raise ValueError('Invalid region location')
        length=int.from_bytes(raw[offset:offset+4],'big')
        if not 1<=length<=size-4:raise ValueError('Invalid chunk length')
        chunks[i]=raw[offset+4:offset+4+length]
    return chunks,timestamps


def region_bytes(chunks,timestamps):
    header=bytearray(4096);body=bytearray();sector=2
    for i,payload in sorted(chunks.items()):
        size=(len(payload)+4+4095)//4096
        if size>255:raise ValueError('External chunk storage is unsupported')
        header[i*4:i*4+4]=((sector<<8)|size).to_bytes(4,'big')
        data=len(payload).to_bytes(4,'big')+payload
        body.extend(data);body.extend(b'\0'*(size*4096-len(data)));sector+=size
    return header+timestamps+body


def edit_region(path,columns):
    chunks,timestamps=region_payloads(path);timestamps=bytearray(timestamps);count=0
    for (cx,cz),cols in columns.items():
        i=cz*32+cx
        if i not in chunks:raise ValueError(f'Missing boundary chunk: {path.name} {cx},{cz}')
        payload=chunks[i]
        if payload[0] not in (1,2,3):raise ValueError('Unsupported/external chunk compression')
        data={1:gzip.decompress,2:zlib.decompress,3:lambda b:b}[payload[0]](payload[1:])
        chunk=nbtlib.File.parse(io.BytesIO(data))
        changed=seal_chunk(chunk,cols)
        if changed:
            output=io.BytesIO();chunk.write(output)
            chunks[i]=b'\x02'+zlib.compress(output.getvalue())
            timestamps[i*4:i*4+4]=int(time.time()).to_bytes(4,'big');count+=changed
    return (region_bytes(chunks,timestamps) if count else None),count


def replace_bytes(path,data):
    temp=path.with_name(path.name+'.edge-tmp')
    with temp.open('wb') as f:f.write(data);f.flush();os.fsync(f.fileno())
    if path.exists():shutil.copymode(path,temp)
    os.replace(temp,path)


def restore(job):
    """Undo only a pristine generated-world seal, before an additive extension."""
    state_path=job/'void-edge-seal.json'
    if not state_path.exists():return
    state=read_json(state_path)
    if state['phase']=='restored':return
    world=job/'worlds'/read_json(job/'plan.json')['world_name']
    backup=Path(state['backup'])
    with world_lock(world):
        records=[read_json(p) for p in sorted(backup.glob('*.json'))]
        # Verify every file before restoring any: do not erase later player edits.
        for r in records:
            if checksum(world/'region'/r['file']) not in (r['before'],r['after']):
                raise ValueError('Sealed world changed; refusing to overwrite later edits')
            if checksum(backup/r['file'])!=r['before']:raise ValueError('Invalid edge backup')
        for r in records:
            replace_bytes(world/'region'/r['file'],(backup/r['file']).read_bytes())
        state['phase']='restored';atomic_json(state_path,state)


def seal(job):
    doc=read_json(job/'plan.json')
    if any(c['status'] not in ('complete','skipped_ocean') for c in doc['cells']):
        raise ValueError('Finish all selected cells before sealing the final edge')
    world=job/'worlds'/doc['world_name'];state_path=job/'void-edge-seal.json'
    with world_lock(world):
        manifest=world/'arnis_one_world.json';digest=checksum(manifest)
        old=read_json(state_path) if state_path.exists() else None
        if old and old['phase']!='restored':
            if old['manifest_sha256']!=digest:raise ValueError('Restore the previous seal before extending the world')
            if old['phase']=='complete':return old
            state=old
        else:
            backup=job/'void-edge-backups'/uuid.uuid4().hex
            backup.mkdir(parents=True)
            state={'phase':'applying','manifest_sha256':digest,'backup':str(backup),'thickness':2}
            atomic_json(state_path,state)
        backup=Path(state['backup']);regions=edge_columns(read_json(manifest)['areas'])
        total=0
        for index,((rx,rz),cols) in enumerate(sorted(regions.items()),1):
            path=world/'region'/f'r.{rx}.{rz}.mca';record=backup/(path.name+'.json')
            if record.exists():
                r=read_json(record);current=checksum(path)
                if current==r['after']:total+=r['blocks'];continue
                if current!=r['before']:raise ValueError('Region changed during interrupted seal')
            data,count=edit_region(path,cols)
            if data is not None:
                original=backup/path.name
                if not original.exists():shutil.copy2(path,original)
                before=checksum(path)
                if checksum(original)!=before:raise ValueError('Backup differs from unsealed region')
                atomic_json(record,{'file':path.name,'before':before,
                    'after':hashlib.sha256(data).hexdigest(),'blocks':count})
                replace_bytes(path,data)
                if checksum(path)!=read_json(record)['after']:raise ValueError('Region verification failed')
                total+=count
            if index%25==0:print(f'Void edge: {index}/{len(regions)} regions',flush=True)
        state.update(phase='complete',regions=len(regions),blocks=total)
        atomic_json(state_path,state)
        print(f'Void edge sealed: {len(regions)} boundary regions, {total} bedrock blocks',flush=True)
        return state


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--job',required=True,type=Path)
    p.add_argument('--restore',action='store_true')
    args=p.parse_args()
    from meld_bridge import job_lock
    with job_lock(args.job):
        (restore if args.restore else seal)(args.job.resolve())


if __name__=='__main__':main()
