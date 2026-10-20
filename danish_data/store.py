"""A local store of Danish data in fixed tiles, so that a map needs no download.

Tiles are 3 x 3 km squares of the UTM zone 32 grid (EPSG:25832). A tile keeps, raw, what
fetch.py downloads for it: the GeoDanmark buildings that touch it with their BBR and DAR
records, and its GeoDanmark hedges and scrub, each untyped hedge already measured against
the height model. `supplement` assembles the supplement fetch.py would make for a bbox from
the tiles under it, offline: seconds instead of minutes.

    python danish_data/store.py build --store DIR --bbox S,W,N,E [--bbox ...]
    python danish_data/store.py supplement --store DIR --bbox S,W,N,E --output FILE

`build` fetches the tiles the store lacks; stop it and run it again to continue, and add
`--older-than DAYS` to fetch old tiles again. `supplement --fetch-missing` fetches and
keeps the tiles a bbox lacks, so a store can also fill itself as maps are made.
"""
import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
import gzip
import json
import math
import os
from pathlib import Path
import tempfile
import time

from pyproj import Transformer
from shapely import from_wkt
from shapely.geometry import box

from fetch import AREA_KM2, atomic_json, bounds, fetch, load_key, progress_printer, request_page
from prepare import identity, prepare
import hedges

SCHEMA = 'arnis-dk.store/1'
# 9 km², under fetch.REQUEST_KM2, so one tile is one geometry query.
TILE_M = 3000
FIELDS = 'id_lokalId status geometristatus geometri { wkt crs }'
REGISTERS = ('geodanmark', 'bbr', 'dar', 'hedges', 'scrub')
TO_UTM = Transformer.from_crs(4326, 25832, always_xy=True)
TO_WGS84 = Transformer.from_crs(25832, 4326, always_xy=True)


def tile_keys(bbox):
    """The tiles under a WGS84 bbox (south, west, north, east), named by their south-west
    corner in kilometres, such as '573_6222'."""
    south, west, north, east = bbox
    x0, y0, x1, y1 = TO_UTM.transform_bounds(west, south, east, north, densify_pts=21)
    return [f'{x // 1000}_{y // 1000}'
            for x in range(math.floor(x0 / TILE_M) * TILE_M, math.ceil(x1 / TILE_M) * TILE_M, TILE_M)
            for y in range(math.floor(y0 / TILE_M) * TILE_M, math.ceil(y1 / TILE_M) * TILE_M, TILE_M)]


def square(key):
    x, y = (int(value) * 1000 for value in key.split('_'))
    return x, y, x + TILE_M, y + TILE_M


def tile_path(store, key):
    return Path(store) / 'tiles' / key.split('_')[0] / f'{key}.json.gz'


def load(store, key):
    with gzip.open(tile_path(store, key), 'rt', encoding='utf-8') as src:
        doc = json.load(src)
    if doc.get('schema') != SCHEMA or doc.get('tile') != key:
        raise ValueError(f'Tile {key} is not a tile of this store')
    return doc


def save(store, doc):
    """Writes a tile atomically: a reader never sees half a tile."""
    path = tile_path(store, doc['tile'])
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary = tempfile.mkstemp(prefix=path.name + '.', suffix='.tmp', dir=path.parent)
    try:
        with os.fdopen(handle, 'wb') as raw, gzip.GzipFile(fileobj=raw, mode='wb', mtime=0) as stream:
            stream.write(json.dumps(doc, ensure_ascii=False, allow_nan=False).encode('utf-8'))
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def fetch_tile(key, api_key, token=None, transport=request_page, opener=None):
    """Everything fetch.py downloads for one tile, kept raw, with the hedges measured."""
    x0, y0, x1, y1 = square(key)
    wkt = f'POLYGON (({x0} {y0},{x1} {y0},{x1} {y1},{x0} {y1},{x0} {y0}))'
    west, south, east, north = TO_WGS84.transform_bounds(x0, y0, x1, y1, densify_pts=21)
    outlines, bbr, dar, at = fetch([south, west, north, east], api_key, None, transport, areas=[wkt])
    hedge_rows = hedges.download('GEODKV_Hegn', FIELDS + ' hegnstype', wkt, at, api_key, transport)
    scrub_rows = hedges.download('GEODKV_KratBevoksning', FIELDS, wkt, at, api_key, transport)
    heights = hedges.HeightModel(token, opener) if token else None
    return {'schema': SCHEMA, 'tile': key, 'snapshot': at.isoformat(), 'height_model': bool(token),
            'geodanmark': outlines, 'bbr': bbr, 'dar': dar, 'hedges': hedge_rows, 'scrub': scrub_rows,
            'shares': hedges.measure(hedge_rows, outlines, at, heights)}


def build(store, keys, api_key, token=None, workers=2, older_than=None, progress=None,
          transport=request_page, opener=None, log=print):
    """Fetches the tiles of `keys` that the store lacks, or that are older than the
    timedelta `older_than`, `workers` at a time. Returns (fetched, failed): a failed tile
    is logged and left for the next run, so one bad tile never stops a long build."""
    cutoff = time.time() - older_than.total_seconds() if older_than else None

    def due(key):
        path = tile_path(store, key)
        return not path.exists() or (cutoff is not None and path.stat().st_mtime < cutoff)
    todo = [key for key in dict.fromkeys(keys) if due(key)]
    failed = []
    if progress:
        progress('tiles', 0, len(todo))
    with ThreadPoolExecutor(max(1, workers)) as pool:
        jobs = {pool.submit(fetch_tile, key, api_key, token, transport, opener): key for key in todo}
        for done, job in enumerate(as_completed(jobs), 1):
            try:
                save(store, job.result())
            except Exception as error:  # noqa: BLE001 - one tile must not end a day-long build
                failed.append(jobs[job])
                log(f'Tile {jobs[job]} failed: {error}')
            if progress:
                progress('tiles', done, len(todo))
    return todo, failed


def supplement(store, bbox, output, fetch_missing=None, workers=4, progress=None,
               transport=request_page, opener=None, log=print):
    """The supplement fetch.py makes for `bbox`, assembled from the store and written to
    `output`. `fetch_missing`, a function returning (api_key, token), lets the store fetch
    and keep the tiles it lacks; without it a missing tile is an error."""
    keys = tile_keys(bbox)
    missing = [key for key in keys if not tile_path(store, key).exists()]
    if missing:
        if fetch_missing is None:
            raise ValueError(f'The store lacks {len(missing)} of the {len(keys)} tiles under this bbox')
        api_key, token = fetch_missing()
        _, failed = build(store, missing, api_key, token, workers, None, progress, transport, opener, log)
        if failed:
            raise ValueError(f'{len(failed)} tiles could not be downloaded')
    if progress:
        progress('assembling', 0, 1)
    docs = sorted((load(store, key) for key in keys), key=lambda doc: doc['snapshot'])
    # A record on a tile border is in both tiles; oldest first, so the newest version wins.
    merged, shares = {name: {} for name in REGISTERS}, {}
    for doc in docs:
        for name in REGISTERS:
            merged[name].update((identity(row.get('id_lokalId')), row) for row in doc[name])
        shares.update(doc['shares'])
    at = datetime.fromisoformat(docs[-1]['snapshot'])
    output = Path(output)
    sources = output.parent / (output.stem + '-sources-store')
    paths = [sources / name for name in ('GeoDanmark-Bygning.json', 'BBR-Bygning.json', 'DAR-Husnummer.json')]
    for path, name in zip(paths, ('geodanmark', 'bbr', 'dar')):
        atomic_json(path, list(merged[name].values()))
    result = prepare(*paths, bbox=bbox, at=at)
    # fetch.py asks for the hedges and the buildings that cut them back within the bbox's
    # projected envelope; the tiles reach further, so keep the same rows.
    south, west, north, east = bbox
    area = box(*TO_UTM.transform_bounds(west, south, east, north, densify_pts=21))

    def near(rows, key=lambda row: row['geometri']['wkt']):
        return [row for row in rows.values() if from_wkt(key(row)).intersects(area)]
    pieces, scrub, counts = hedges.select(near(merged['hedges']), near(merged['scrub']),
                                          near(merged['geodanmark'], key=lambda row: row['geometri']),
                                          at, shares=shares)
    hedges.append_elements(result, pieces, scrub)
    meta = result['arnis_dk']
    meta['hedges'] = {'height_model': all(doc['height_model'] for doc in docs), 'counts': counts}
    if any(doc['height_model'] for doc in docs):
        meta['attribution'] += '; Danmarks Højdemodel (Klimadatastyrelsen)'
    meta['store'] = {'tiles': len(keys), 'oldest': docs[0]['snapshot'], 'newest': docs[-1]['snapshot']}
    atomic_json(output, result)
    if progress:
        progress('assembling', 1, 1)
    return result


def build_reporter(every=25):
    """Progress for a long build: a line every `every` tiles, with the time left."""
    started = time.monotonic()

    def report(phase, done, total):
        if done and done != total and done % every:
            return
        left = (time.monotonic() - started) / done * (total - done) if done else 0
        print(f'{done}/{total} tiles, about {timedelta(seconds=round(left))} left', flush=True)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest='command', required=True)
    build_args = commands.add_parser('build', help='Fetch the tiles under one or more bboxes')
    build_args.add_argument('--bbox', required=True, action='append', help='S,W,N,E; may be repeated')
    build_args.add_argument('--workers', type=int, default=2)
    build_args.add_argument('--older-than', type=float, metavar='DAYS', help='Also fetch tiles older than this')
    assemble_args = commands.add_parser('supplement', help='Assemble a supplement for a bbox from the store')
    assemble_args.add_argument('--bbox', required=True, help=f'S,W,N,E; max {AREA_KM2} km2')
    assemble_args.add_argument('--output', required=True, type=Path)
    assemble_args.add_argument('--fetch-missing', action='store_true', help='Fetch and keep tiles the store lacks')
    assemble_args.add_argument('--workers', type=int, default=4)
    for command in (build_args, assemble_args):
        command.add_argument('--store', required=True, type=Path)
        command.add_argument('--credentials-file', type=Path,
                             help='Local .env file; otherwise DATAFORDELER_API_KEY and DATAFORSYNINGEN_TOKEN')
    args = parser.parse_args()

    def credentials():
        token = hedges.load_token(args.credentials_file)
        if not token:
            print('Note: no DATAFORSYNINGEN_TOKEN; only hedges GeoDanmark types as living are added.')
        return load_key(args.credentials_file), token
    try:
        if args.command == 'build':
            boxes = [bounds(value, math.inf) for value in args.bbox]
            keys = list(dict.fromkeys(key for bbox in boxes for key in tile_keys(bbox)))
            older = timedelta(days=args.older_than) if args.older_than is not None else None
            todo, failed = build(args.store, keys, *credentials(), args.workers, older, build_reporter())
            print(f'{len(keys)} tiles in the store area; fetched {len(todo) - len(failed)}, {len(failed)} failed.')
            if failed:
                parser.exit(1, 'Run build again to retry the failed tiles.\n')
        else:
            result = supplement(args.store, bounds(args.bbox, AREA_KM2), args.output,
                                credentials if args.fetch_missing else None, args.workers, progress_printer())
            print(json.dumps(result['arnis_dk']['counts'], indent=2))
            print('Prepared supplement:', args.output)
    except (ValueError, OSError, KeyError, TypeError) as error:
        # Do not expose request URLs, credentials or provider response bodies.
        parser.exit(1, f'Store failed: {error}\n')


if __name__ == '__main__':
    main()
