"""Experimental Meld grid -> Arnis-DK One World runner. Meld remains external."""
import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import importlib
import json
import math
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import types

SCHEMA = 1
WORLD_NAME = 'Arnis-DK Meld'


def digest(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def atomic_json(path, data):
    path = Path(path)
    fd, name = tempfile.mkstemp(dir=path.parent, prefix=path.name, suffix='.tmp')
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            json.dump(data, stream, ensure_ascii=False, indent=2, allow_nan=False)
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


def read_json(path):
    return json.loads(Path(path).read_text(encoding='utf-8'))


def bbox(value):
    values = list(map(float, value.split(',')))
    if len(values) != 4 or not all(math.isfinite(v) for v in values):
        raise ValueError('Expected four finite bbox coordinates')
    s, w, n, e = values
    if not (54 <= s < n <= 58 and 7 <= w < e <= 16):
        raise ValueError('Choose a WGS84 bbox in Denmark')
    return values


def padded(bounds, scale):
    s, w, n, e = bounds
    # Covers One World's 64-block geometry pad and outward chunk snapping.
    metres = max(128.0, 128.0 / scale)
    dy = metres / 111320.0
    dx = dy / math.cos(math.radians((s + n) / 2))
    return [s - dy, w - dx, n + dy, e + dx]


def contains(outer, inner):
    return (len(outer) == 4 and outer[0] <= inner[0] and outer[1] <= inner[1]
            and outer[2] >= inner[2] and outer[3] >= inner[3])


def external_meld(path):
    """Load only the external grid package, without launching Meld or its updater."""
    source = Path(path).resolve() / 'src'
    names = ['grid.py', 'coords.py', 'constants.py', '__init__.py']
    hashes = {name: digest(source / name) for name in names}
    name = '_arnis_dk_meld_' + hashlib.sha256(str(source).encode()).hexdigest()[:12]
    package = types.ModuleType(name)
    package.__path__ = [str(source)]
    sys.modules[name] = package
    grid = importlib.import_module(name + '.grid')
    return grid, hashes


def probe(executable):
    result = subprocess.run([str(executable), '--help'], capture_output=True, text=True,
                            encoding='utf-8', errors='replace', timeout=30, check=True)
    required = ['--one-world', '--world-name', '--danish-buildings', '--mode', '--signage']
    missing = [flag for flag in required if flag not in result.stdout]
    if missing:
        raise ValueError('This binary is not a compatible Arnis-DK: ' + ', '.join(missing))


def input_record(path):
    path = Path(path).resolve()
    return {'path': str(path), 'sha256': digest(path)}


def plan(args):
    from fetch import bounds as download_bounds
    if not math.isfinite(args.scale) or not 0.1 <= args.scale <= 4:
        raise ValueError('Scale must be between 0.1 and 4')
    if not 1 <= args.cell_regions <= 4 or not 1 <= args.max_cells <= 20000:
        raise ValueError('Use 1-4 regions per cell and 1-20000 maximum cells')
    if not 1 <= args.threads <= 64:
        raise ValueError('Threads must be between 1 and 64')
    job = args.job.resolve()
    if job.exists():
        raise ValueError('Choose a new job directory; use run to resume an existing job')
    bounds = bbox(args.bbox)
    grid, hashes = external_meld(args.meld_source)
    probe(args.arnis.resolve())
    s, w, n, e = bounds
    origin = {'lat': s, 'lon': w}
    cells = grid.cells_for_bbox(dict(south=s, west=w, north=n, east=e), origin,
                               args.scale, args.cell_regions, max_cells=args.max_cells)
    supplement = input_record(args.danish_buildings) if args.danish_buildings else None
    coverage = read_json(supplement['path'])['arnis_dk']['bbox'] if supplement else None
    tasks = []
    for i, cell in enumerate(cells):
        b = cell['bbox']
        area = [max(s, b['south']), max(w, b['west']), min(n, b['north']), min(e, b['east'])]
        data_bbox = padded(area, args.scale)
        if supplement:
            if not coverage or not contains(coverage, data_bbox):
                raise ValueError('Prepared Danish supplement must cover all cells INCLUDING padding')
        else:
            download_bounds(','.join(map(str, data_bbox)))  # retains the 10 km2 safety limit
        tasks.append({'id': f'{i:05d}', 'meld_cell': cell['cell_key'], 'bbox': area,
                      'data_bbox': data_bbox, 'status': 'pending'})
    if not tasks:
        raise ValueError('Meld returned an empty plan')
    document = {'schema': SCHEMA, 'snapshot': datetime.now(timezone.utc).isoformat(),
                'meld_source': str(args.meld_source.resolve()), 'meld_hashes': hashes,
                'arnis': input_record(args.arnis), 'bbox': bounds, 'scale': args.scale,
                'threads': args.threads, 'caves': args.caves, 'signage': args.signage,
                'danish_buildings': supplement,
                'osm_file': input_record(args.osm_file) if args.osm_file else None,
                'world_name': WORLD_NAME, 'cells': tasks}
    job.mkdir(parents=True)
    atomic_json(job / 'plan.json', document)
    print(f'Planned {len(tasks)} cells; no data downloaded and no world generated.')
    print(f'Run: python danish_data/meld_bridge.py run --job "{job}"')


@contextmanager
def job_lock(job):
    path = job / 'runner.lock'
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL)
    except FileExistsError:
        raise ValueError('Job is locked. Do not remove runner.lock until its process has stopped.') from None
    try:
        with os.fdopen(fd, 'w') as stream:
            stream.write(str(os.getpid()))
        yield
    finally:
        path.unlink(missing_ok=True)


def verify_inputs(document):
    if document.get('schema') != SCHEMA:
        raise ValueError('Unsupported job format')
    _, hashes = external_meld(document['meld_source'])
    if hashes != document['meld_hashes']:
        raise ValueError('External Meld grid changed; use the original checkout to resume')
    for key in ['arnis', 'danish_buildings', 'osm_file']:
        record = document.get(key)
        if record and digest(record['path']) != record['sha256']:
            raise ValueError(f'{key} changed since planning; create a new job')


def supplement_for_cell(document, cell, folder, key):
    if document.get('danish_buildings'):
        return Path(document['danish_buildings']['path'])
    from fetch import fetch
    from prepare import prepare
    output = folder / 'buildings.json'
    if output.exists():
        existing = read_json(output)
        if (existing['arnis_dk']['bbox'] != cell['data_bbox']
                or existing['arnis_dk']['at'] != document['snapshot']):
            raise ValueError('Cached Danish supplement has a different area or snapshot')
        return output
    outlines, bbr, dar, at = fetch(cell['data_bbox'], key,
                                  at=datetime.fromisoformat(document['snapshot']))
    paths = [folder / name for name in ['GeoDanmark.json', 'BBR.json', 'DAR.json']]
    for path, rows in zip(paths, [outlines, bbr, dar]):
        atomic_json(path, rows)
    atomic_json(output, prepare(*paths, bbox=cell['data_bbox'], at=at))
    return output


def command(document, cell, job, supplement):
    cmd = [document['arnis']['path'], '--one-world', '--world-name', document['world_name'],
           '--output-dir', str(job / 'worlds'), '--bbox', ','.join(map(str, cell['bbox'])),
           '--scale', str(document['scale']), '--mode', 'geo-terrain',
           '--signage', document['signage'], '--map-item=false',
           '--danish-buildings', str(supplement)]
    if document.get('osm_file'):
        cmd += ['--file', document['osm_file']['path']]
    if document['caves']:
        cmd.append('--caves')
    return cmd


def validate_result(world, cell):
    if not (world / 'level.dat').is_file():
        raise ValueError('Arnis did not produce level.dat')
    manifest = read_json(world / 'arnis_one_world.json')
    if not manifest.get('areas'):
        raise ValueError('Arnis did not record a completed area')
    s, w, n, e = cell['bbox']
    if not any(contains([a['min_lat'], a['min_lon'], a['max_lat'], a['max_lon']],
                        [s + 1e-8, w + 1e-8, n - 1e-8, e - 1e-8]) for a in manifest['areas']):
        raise ValueError('World manifest does not cover the generated cell')


def run(args):
    from fetch import load_key
    job = args.job.resolve()
    if args.limit is not None and args.limit < 1:
        raise ValueError('Limit must be positive')
    with job_lock(job):
        document = read_json(job / 'plan.json')
        verify_inputs(document)
        probe(document['arnis']['path'])
        cells = [c for c in document['cells'] if c['status'] != 'complete']
        world = job / 'worlds' / document['world_name']
        if any(c['status'] == 'complete' for c in document['cells']):
            for cell in document['cells']:
                if cell['status'] == 'complete':
                    validate_result(world, cell)
        if not cells:
            print('All cells are already complete; nothing generated.')
            return
        key = None if document.get('danish_buildings') else load_key(args.credentials_file)
        env = os.environ.copy()
        # Credentials are only consumed by Python, never passed to the generator.
        for name in ['DATAFORDELER_API_KEY', 'ARNIS_DK_BUILDINGS', 'MAPILLARY_TOKEN']:
            env.pop(name, None)
        env['RAYON_NUM_THREADS'] = str(document['threads'])
        for cell in cells[:args.limit]:
            folder = job / 'cells' / cell['id']
            folder.mkdir(parents=True, exist_ok=True)
            cell['status'] = 'running'
            atomic_json(job / 'plan.json', document)
            print(f"Generating cell {cell['id']} ({cell['meld_cell']})", flush=True)
            try:
                supplement = supplement_for_cell(document, cell, folder, key)
                with (folder / 'arnis.log').open('w', encoding='utf-8') as log:
                    result = subprocess.run(command(document, cell, job, supplement), env=env,
                                            stdout=log, stderr=subprocess.STDOUT, timeout=args.timeout,
                                            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
                if result.returncode:
                    raise ValueError(f'Arnis failed with exit code {result.returncode}; see cell log')
                validate_result(world, cell)
                cell['status'] = 'complete'
            except BaseException:
                cell['status'] = 'failed'
                atomic_json(job / 'plan.json', document)
                raise
            atomic_json(job / 'plan.json', document)
        print(f"Complete: {sum(c['status'] == 'complete' for c in document['cells'])}/{len(document['cells'])}")
        print(f'World: {world}')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='action', required=True)
    p = commands.add_parser('plan', help='Plan using an external Meld checkout; no generation')
    p.add_argument('--meld-source', type=Path, required=True)
    p.add_argument('--arnis', type=Path, required=True)
    p.add_argument('--job', type=Path, required=True)
    p.add_argument('--bbox', required=True)
    p.add_argument('--cell-regions', type=int, default=2)
    p.add_argument('--scale', type=float, default=1.0)
    p.add_argument('--max-cells', type=int, default=1000)
    p.add_argument('--threads', type=int, default=min(8, max(1, (os.cpu_count() or 2) // 2)))
    p.add_argument('--danish-buildings', type=Path, help='Optional prepared supplement covering all padded cells')
    p.add_argument('--osm-file', type=Path, help='Optional OSM JSON covering all padded cells; otherwise use Arnis download')
    p.add_argument('--caves', action='store_true')
    p.add_argument('--signage', choices=['none', 'basic', 'full'], default='full')
    p.set_defaults(function=plan)
    p = commands.add_parser('run', help='Generate sequentially, resuming incomplete cells')
    p.add_argument('--job', type=Path, required=True)
    p.add_argument('--credentials-file', type=Path)
    p.add_argument('--limit', type=int, help='Generate at most this many incomplete cells')
    p.add_argument('--timeout', type=int, default=3600, help='Maximum seconds per Arnis process')
    p.set_defaults(function=run)
    args = parser.parse_args()
    try:
        args.function(args)
    except (ValueError, OSError, KeyError, subprocess.SubprocessError) as error:
        parser.exit(1, f'Meld bridge: {error}\n')


if __name__ == '__main__':
    main()
