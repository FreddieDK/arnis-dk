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

# 2: older runners must not generate skipped_ocean cells. 3: older runners
# would ignore world_height and create the world with the extended height.
# 4: older runners would leave out the hedges. 5: older runners would plant
# minecraftdanmark.dk cells in a Web Mercator world. 6: older runners would
# leave another country's land standing in border cells.
SCHEMA = 6
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


def height_flags(document):
    """Normal build height (Y -64 to 319, sea level at Y 63), which Bedrock players
    joining through Geyser can see. Plans from before world_height keep their world's."""
    return ['--one-world-height', 'vanilla'] if document.get('world_height') == 'vanilla' else []


def frame_flags(cell):
    """Cells planned on the minecraftdanmark.dk grid are exact block rectangles."""
    rect = cell.get('rect')
    if not rect:
        return []
    return ['--projection', 'mcdk', '--block-rect', ','.join(map(str, rect))]


def mcdk_cells(bounds, scale, regions):
    """Cells as squares of whole region files in the minecraftdanmark.dk frame
    (UTM zone 32, X = E - 600000, Z = 6200000 - N), with the lat/lon bbox around each."""
    import mcdk
    return [{'cell_key': f'mcdk {rect[0]},{rect[1]}', 'rect': rect,
             'bbox': dict(zip(['south', 'west', 'north', 'east'], mcdk.rect_bbox(rect, scale)))}
            for rect in mcdk.cells(bounds, scale, regions)]


def probe(executable, extra=()):
    result = subprocess.run([str(executable), '--help'], capture_output=True, text=True,
                            encoding='utf-8', errors='replace', timeout=30, check=True)
    required = ['--one-world', '--world-name', '--danish-buildings', '--mode', '--signage', '--world-type']
    required += [flag for flag in extra if flag.startswith('--')]
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
    from ocean_mask import LandMask
    mask_record = input_record(args.land_mask) if args.land_mask else None
    mask = LandMask(mask_record['path']) if mask_record else None
    if not math.isfinite(args.coast_buffer_m) or not 0 <= args.coast_buffer_m <= 10000:
        raise ValueError('Coast buffer must be between 0 and 10000 metres')
    keep_areas = [bbox(value) for value in args.keep_bbox]
    country_path = getattr(args, 'country', None)
    country_record = input_record(country_path) if country_path else None
    country = None
    if country_record:
        if not mask:
            raise ValueError('--country needs --land-mask, which tells land from sea')
        if getattr(args, 'frame', 'mcdk') != 'mcdk':
            raise ValueError('--country needs --frame mcdk')
        from national_mask import CountryMask
        country = CountryMask(country_record['path'], mask)
    bridge_document = None
    bridges = None
    if mask:
        from bridge_mask import BridgeMask, fetch_document
        source = getattr(args, 'bridge_mask', None)
        bridge_document = read_json(source) if source else fetch_document(bounds)
        bridges = BridgeMask(bridge_document, bounds)
    world_height = getattr(args, 'world_height', 'vanilla')
    if world_height not in ('vanilla', 'earth'):
        raise ValueError('World height must be vanilla or earth')
    frame = getattr(args, 'frame', 'mcdk')
    if frame not in ('mcdk', 'meld'):
        raise ValueError('Frame must be mcdk or meld')
    s, w, n, e = bounds
    if frame == 'mcdk':
        grid, hashes = None, None
        probe(args.arnis.resolve(), height_flags({'world_height': world_height}) + ['--block-rect'])
        cells = mcdk_cells(bounds, args.scale, args.cell_regions)
        if len(cells) > (200000 if mask else args.max_cells):
            raise ValueError(f'{len(cells)} cells exceed --max-cells {args.max_cells}; '
                             'reduce the area or raise the limit')
    else:
        if not getattr(args, 'meld_source', None):
            raise ValueError('--frame meld needs --meld-source')
        grid, hashes = external_meld(args.meld_source)
        probe(args.arnis.resolve(), height_flags({'world_height': world_height}))
        origin = {'lat': s, 'lon': w}
        cells = grid.cells_for_bbox(dict(south=s, west=w, north=n, east=e), origin,
                                   args.scale, args.cell_regions,
                                   max_cells=200000 if mask else args.max_cells)
    supplement = input_record(args.danish_buildings) if args.danish_buildings else None
    coverage = read_json(supplement['path'])['arnis_dk']['bbox'] if supplement else None
    tasks = []
    for i, cell in enumerate(cells):
        b = cell['bbox']
        if 'rect' in cell:
            # The rectangle is the area; its bbox reaches a little past the request.
            area = [b['south'], b['west'], b['north'], b['east']]
        else:
            area = [max(s, b['south']), max(w, b['west']), min(n, b['north']), min(e, b['east'])]
        data_bbox = padded(area, args.scale)
        intersects_keep = any(area[0] <= k[2] and area[2] >= k[0] and area[1] <= k[3]
                              and area[3] >= k[1] for k in keep_areas)
        bridge_corridor = bool(bridges and bridges.intersects(data_bbox))
        ocean = bool(mask and not intersects_keep and not bridge_corridor
                     and mask.is_open_sea(data_bbox, args.coast_buffer_m))
        abroad = foreign = False
        if country and not ocean:
            inside, own, foreign = country.classify(cell['rect'], args.scale, args.coast_buffer_m)
            # Only the country's own land and coast, its keep areas and its bridges.
            abroad = not inside or not (own or intersects_keep or bridge_corridor)
        ocean = ocean or abroad
        if ocean:
            pass  # No Danish data request or coverage requirement for skipped sea cells.
        elif supplement:
            if not coverage or not contains(coverage, data_bbox):
                raise ValueError('Prepared Danish supplement must cover all cells INCLUDING padding')
        else:
            download_bounds(','.join(map(str, data_bbox)))  # retains the 10 km2 safety limit
        task = {'id': f'{i:05d}', 'meld_cell': cell['cell_key'], 'bbox': area,
                'data_bbox': data_bbox, 'status': 'skipped_ocean' if ocean else 'pending'}
        if 'rect' in cell:
            task['rect'] = cell['rect']
        if abroad:
            task['skip_reason'] = 'abroad'
        elif foreign:
            task['foreign_land'] = True
        tasks.append(task)
    if not tasks:
        raise ValueError('Meld returned an empty plan')
    kept = sum(c['status'] != 'skipped_ocean' for c in tasks)
    if kept > args.max_cells:
        raise ValueError(f'{kept} retained cells exceed --max-cells {args.max_cells}; reduce the area or raise the limit')
    if mask_record and digest(mask_record['path']) != mask_record['sha256']:
        raise ValueError('Land mask changed while planning; retry with an unchanged file')
    document = {'schema': SCHEMA, 'snapshot': datetime.now(timezone.utc).isoformat(),
                'frame': frame,
                'meld_source': str(args.meld_source.resolve()) if grid else None,
                'meld_hashes': hashes,
                'arnis': input_record(args.arnis), 'bbox': bounds, 'scale': args.scale,
                'threads': args.threads, 'caves': args.caves, 'signage': args.signage,
                'world_height': world_height,
                'hedges': bool(getattr(args, 'hedges', True)) and supplement is None,
                'danish_buildings': supplement,
                'land_mask': mask_record, 'coast_buffer_m': args.coast_buffer_m,
                'keep_bbox': keep_areas,
                'osm_file': input_record(args.osm_file) if args.osm_file else None,
                'country': country_record,
                'world_name': WORLD_NAME, 'cells': tasks, 'seal_void_edges': True}
    job.mkdir(parents=True)
    if bridge_document is not None:
        atomic_json(job / 'bridge-mask.json', bridge_document)
        document['bridge_mask'] = input_record(job / 'bridge-mask.json')
    atomic_json(job / 'plan.json', document)
    abroad_count = sum(c.get('skip_reason') == 'abroad' for c in tasks)
    border = sum(bool(c.get('foreign_land')) for c in tasks)
    print(f'Planned {kept} cells; skipped {len(tasks)-kept-abroad_count} open-sea cells (void).')
    if country_record:
        print(f'Country: {abroad_count} cells abroad skipped; {border} border cells lose their foreign land.')
    print('Bridge snapshot pinned when using a land mask; no world generated.')
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
    if document.get('schema') not in (1, 2, 3, 4, 5, SCHEMA):
        raise ValueError('Unsupported job format')
    if document.get('meld_source'):
        _, hashes = external_meld(document['meld_source'])
        if hashes != document['meld_hashes']:
            raise ValueError('External Meld grid changed; use the original checkout to resume')
    for key in ['arnis', 'danish_buildings', 'osm_file', 'land_mask', 'bridge_mask', 'country']:
        record = document.get(key)
        if record and digest(record['path']) != record['sha256']:
            raise ValueError(f'{key} changed since planning; create a new job')


def supplement_for_cell(document, cell, folder, key, token=None):
    if document.get('danish_buildings'):
        return Path(document['danish_buildings']['path'])
    from fetch import fetch
    from prepare import prepare
    output = folder / 'buildings.json'
    if output.exists():
        existing = read_json(output)
        if (existing['arnis_dk']['bbox'] != cell['data_bbox']
                or existing['arnis_dk']['at'] != document['snapshot']
                or bool(document.get('hedges')) != ('hedges' in existing['arnis_dk'])):
            raise ValueError('Cached Danish supplement has a different area, snapshot or hedges')
        return output
    outlines, bbr, dar, at = fetch(cell['data_bbox'], key,
                                  at=datetime.fromisoformat(document['snapshot']))
    paths = [folder / name for name in ['GeoDanmark.json', 'BBR.json', 'DAR.json']]
    for path, rows in zip(paths, [outlines, bbr, dar]):
        atomic_json(path, rows)
    supplement = prepare(*paths, bbox=cell['data_bbox'], at=at)
    if document.get('hedges'):
        from hedges import add
        add(supplement, cell['data_bbox'], outlines, key, at, token)
    atomic_json(output, supplement)
    return output


def command(document, cell, job, supplement):
    cmd = [document['arnis']['path'], '--one-world', '--world-name', document['world_name'],
           '--output-dir', str(job / 'worlds'), '--bbox', ','.join(map(str, cell['bbox'])),
           '--scale', str(document['scale']), '--mode', 'geo-terrain',
           '--world-type', 'void',
           '--signage', document['signage'], '--map-item=false',
           '--danish-buildings', str(supplement)] + height_flags(document) + frame_flags(cell)
    if document.get('osm_file'):
        cmd += ['--file', document['osm_file']['path']]
    if document['caves']:
        cmd.append('--caves')
    return cmd


def world_coverage(world):
    """The recorded areas, as lat/lon boxes and as block rectangles."""
    from shapely.geometry import box
    from shapely.strtree import STRtree
    if not (world / 'level.dat').is_file():
        raise ValueError('Arnis did not produce level.dat')
    manifest = read_json(world / 'arnis_one_world.json')
    if not manifest.get('areas'):
        raise ValueError('Arnis did not record a completed area')
    areas = manifest['areas']
    return {'bbox': STRtree([box(a['min_lon'], a['min_lat'], a['max_lon'], a['max_lat'])
                             for a in areas]),
            'rect': STRtree([box(a['min_x'], a['min_z'], a['max_x'] + 1, a['max_z'] + 1)
                             for a in areas if 'min_x' in a])}


def validate_result(world, cell, coverage=None):
    from shapely.geometry import box
    if coverage is None:
        coverage = world_coverage(world)
    if cell.get('rect'):
        x0, z0, x1, z1 = cell['rect']
        tree, requested = coverage['rect'], box(x0 + .5, z0 + .5, x1 + .5, z1 + .5)
    else:
        s, w, n, e = cell['bbox']
        tree, requested = coverage['bbox'], box(w + 1e-8, s + 1e-8, e - 1e-8, n - 1e-8)
    if not len(tree.query(requested, predicate='covered_by')):
        raise ValueError('World manifest does not cover the generated cell')


def void_foreign_land(document, job, world, cell, cache):
    """Remove the chunks of a border cell that lie on another country's land, and
    note them for the void-edge seal."""
    if 'mask' not in cache:
        from national_mask import CountryMask
        from ocean_mask import LandMask
        cache['mask'] = CountryMask(document['country']['path'], LandMask(document['land_mask']['path']))
    from national_mask import remove_chunks
    chunks = cache['mask'].foreign_chunks(cell['rect'], document['scale'])
    path = job / 'national-mask.json'
    noted = read_json(path) if path.exists() else {'chunks': []}
    known = {tuple(c) for c in noted['chunks']}
    noted['chunks'] += [list(c) for c in chunks if tuple(c) not in known]
    # Noted before removal: an interrupted removal is finished by the next run.
    atomic_json(path, noted)
    cell['masked_chunks'] = remove_chunks(world, chunks) + cell.get('masked_chunks', 0)


def run(args):
    from fetch import load_key
    job = args.job.resolve()
    if args.limit is not None and args.limit < 1:
        raise ValueError('Limit must be positive')
    with job_lock(job):
        document = read_json(job / 'plan.json')
        verify_inputs(document)
        probe(document['arnis']['path'], height_flags(document))
        allowed = {'pending', 'running', 'failed', 'complete', 'skipped_ocean'}
        if any(c['status'] not in allowed for c in document['cells']):
            raise ValueError('Unknown cell status in plan')
        cells = [c for c in document['cells'] if c['status'] not in ('complete', 'skipped_ocean')]
        world = job / 'worlds' / document['world_name']
        if cells and (job / 'void-edge-seal.json').exists():
            from seal_void_edges import restore
            restore(job)  # Never retain an old outer wall inside an extension.
        if any(c['status'] == 'complete' for c in document['cells']):
            coverage = world_coverage(world)
            for cell in document['cells']:
                if cell['status'] == 'complete':
                    validate_result(world, cell, coverage)
        if not cells:
            finalize_edges(document, job)
            print('No pending land/coastal cells; nothing generated (open sea stays void).')
            return
        key = None if document.get('danish_buildings') else load_key(args.credentials_file)
        token = None
        if document.get('hedges'):
            from hedges import load_token
            token = load_token(args.credentials_file)
            if not token:
                raise ValueError('This plan grows real hedges, which need DATAFORSYNINGEN_TOKEN '
                                 'for the Danish height model; add it to the credentials file')
        env = os.environ.copy()
        # Credentials are only consumed by Python, never passed to the generator.
        for name in ['DATAFORDELER_API_KEY', 'DATAFORSYNINGEN_TOKEN', 'ARNIS_DK_BUILDINGS', 'MAPILLARY_TOKEN']:
            env.pop(name, None)
        env['RAYON_NUM_THREADS'] = str(document['threads'])
        mask_cache = {}
        for cell in cells[:args.limit]:
            folder = job / 'cells' / cell['id']
            folder.mkdir(parents=True, exist_ok=True)
            cell['status'] = 'running'
            atomic_json(job / 'plan.json', document)
            print(f"Generating cell {cell['id']} ({cell['meld_cell']})", flush=True)
            try:
                supplement = supplement_for_cell(document, cell, folder, key, token)
                with (folder / 'arnis.log').open('w', encoding='utf-8') as log:
                    result = subprocess.run(command(document, cell, job, supplement), env=env,
                                            stdout=log, stderr=subprocess.STDOUT, timeout=args.timeout,
                                            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
                if result.returncode:
                    raise ValueError(f'Arnis failed with exit code {result.returncode}; see cell log')
                validate_result(world, cell)
                if cell.get('foreign_land'):
                    void_foreign_land(document, job, world, cell, mask_cache)
                cell['status'] = 'complete'
            except BaseException:
                cell['status'] = 'failed'
                atomic_json(job / 'plan.json', document)
                raise
            atomic_json(job / 'plan.json', document)
        if all(c['status'] in ('complete', 'skipped_ocean') for c in document['cells']):
            finalize_edges(document, job)
        skipped = sum(c['status'] == 'skipped_ocean' for c in document['cells'])
        print(f"Complete: {sum(c['status'] == 'complete' for c in document['cells'])}/{len(document['cells'])-skipped}; skipped ocean: {skipped}")
        print(f'World: {world}')


def finalize_edges(document, job):
    # Request files let an already running cloud job opt in without racing the
    # worker's atomic plan updates. Only seal the complete, combined footprint.
    requested = document.get('seal_void_edges') or (job / 'seal-void-edges.request').exists()
    if requested and any(c['status'] == 'complete' for c in document['cells']):
        from seal_void_edges import seal
        seal(job)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='action', required=True)
    p = commands.add_parser('plan', help='Plan the cells; fetch bridge corridors but do not generate a world')
    p.add_argument('--frame', choices=['mcdk', 'meld'], default='mcdk',
                   help='mcdk: the coordinates of the classic minecraftdanmark.dk map (UTM zone 32, '
                        'X = E - 600000, Z = 6200000 - N), cells as squares of whole region files '
                        '(default); meld: the Web Mercator grid of an external Meld checkout')
    p.add_argument('--meld-source', type=Path, help='External Meld checkout; needed for --frame meld')
    p.add_argument('--arnis', type=Path, required=True)
    p.add_argument('--job', type=Path, required=True)
    p.add_argument('--bbox', required=True)
    p.add_argument('--cell-regions', type=int, default=2)
    p.add_argument('--scale', type=float, default=1.0)
    p.add_argument('--max-cells', type=int, default=1000)
    p.add_argument('--threads', type=int, default=min(8, max(1, (os.cpu_count() or 2) // 2)))
    p.add_argument('--danish-buildings', type=Path, help='Optional prepared supplement covering all padded cells')
    p.add_argument('--osm-file', type=Path, help='Optional OSM JSON covering all padded cells; otherwise use Arnis download')
    p.add_argument('--land-mask', type=Path, help='Prepared ocean_mask.py file; skip open-sea cells as void')
    p.add_argument('--coast-buffer-m', type=float, default=2000, help='Retain a coastal sea strip (default 2000 metres, plus cell padding)')
    p.add_argument('--bridge-mask', type=Path, help='Prepared bridge snapshot; otherwise fetch OSM bridge corridors when a land mask is used')
    p.add_argument('--keep-bbox', action='append', default=[], help='Always generate this south,west,north,east area, e.g. a bridge; repeatable')
    p.add_argument('--country', type=Path,
                   help='GeoJSON outline of one country with its territorial waters (e.g. OSM relation 50046 '
                        'for Denmark). Cells abroad are skipped, and chunks on foreign land in border cells '
                        'are voided after generation. Needs --land-mask and --frame mcdk')
    p.add_argument('--caves', action='store_true')
    p.add_argument('--signage', choices=['none', 'basic', 'full'], default='full')
    p.add_argument('--hedges', action=argparse.BooleanOptionalAction, default=True,
                   help='Grow GeoDanmark hedges and scrub that the height model confirms (default); '
                        'needs DATAFORSYNINGEN_TOKEN when running')
    p.add_argument('--world-height', choices=['vanilla', 'earth'], default='vanilla',
                   help='vanilla: Y -64 to 319 with sea level at Y 63, visible to Bedrock players '
                        'through Geyser (default); earth: the extended height (Y -2032 to 2031)')
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
