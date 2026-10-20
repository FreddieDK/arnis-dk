"""Download a bounded Danish building supplement from Datafordeler GraphQL v2."""
import argparse
from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import tempfile
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from pyproj import Transformer
from prepare import active, identity, prepare


COMMON = 'id_lokalId status registreringFra registreringTil virkningFra virkningTil'
FIELDS = {
    'GEODKV': COMMON + ' BBRUUID geometristatus metode3D geometri { wkt crs }',
    'BBR': COMMON + ' husnummer byg021BygningensAnvendelse byg054AntalEtager '
           'byg032YdervaeggensMateriale byg033Tagdaekningsmateriale byg026Opfoerelsesaar',
    'DAR': COMMON + ' husnummertekst adgangsadressebetegnelse adgangspunkt geoDanmarkBygning',
    'DAR_POINT': COMMON + ' oprindelse_tekniskStandard position { wkt crs }',
}
ENTITIES = {'GEODKV': 'GEODKV_Bygning', 'BBR': 'BBR_Bygning', 'DAR': 'DAR_Husnummer',
            'DAR_POINT': 'DAR_Adressepunkt'}
ALIASES = {'byg032YdervaeggensMateriale': 'byg032YdervæggensMateriale',
           'byg033Tagdaekningsmateriale': 'byg033Tagdækningsmateriale',
           'byg026Opfoerelsesaar': 'byg026Opførelsesår'}


def load_key(path=None):
    key = os.environ.get('DATAFORDELER_API_KEY', '').strip()
    if path is not None:
        values = dict(line.strip().split('=', 1) for line in Path(path).read_text(encoding='utf-8-sig').splitlines()
                      if line.strip() and not line.lstrip().startswith('#') and '=' in line)
        key = values.get('DATAFORDELER_API_KEY', '').strip().strip('\"\'')
    if not key or any(c.isspace() for c in key):
        raise ValueError('Set DATAFORDELER_API_KEY or use --credentials-file with a local .env file')
    return key


# One geometry query covers at most REQUEST_KM2; a larger area is asked for in tiles
# of TILE_KM by TILE_KM. Only the geometry queries depend on the area: the linked
# registers are asked for by identity, 100 at a time.
REQUEST_KM2 = 10
TILE_KM = 3
AREA_KM2 = 100


def bounds(value, limit=REQUEST_KM2):
    numbers = [float(n) for n in value.split(',')]
    if len(numbers) != 4 or not all(math.isfinite(n) for n in numbers):
        raise ValueError('Expected four finite bbox coordinates')
    south, west, north, east = numbers
    if not (54 <= south < north <= 58 and 7 <= west < east <= 16):
        raise ValueError('The download bbox must be in Denmark (WGS84 lat,lon)')
    if (north - south) * 111.32 * (east - west) * 111.32 * math.cos(math.radians((south+north)/2)) > limit:
        raise ValueError(f'Download at most {limit} square kilometres per request')
    return numbers


def envelopes(bbox):
    """WKT rectangles in EPSG:25832 that together cover `bbox`, each under REQUEST_KM2."""
    south, west, north, east = bbox
    rows = max(1, math.ceil((north - south) * 111.32 / TILE_KM))
    cols = max(1, math.ceil((east - west) * 111.32 * math.cos(math.radians(south)) / TILE_KM))
    to_utm = Transformer.from_crs(4326, 25832, always_xy=True)
    result = []
    for row in range(rows):
        s, n = south + (north - south) * row / rows, south + (north - south) * (row + 1) / rows
        for col in range(cols):
            w, e = west + (east - west) * col / cols, west + (east - west) * (col + 1) / cols
            # Datafordeler requires the field's native CRS. A densified envelope avoids
            # dropping edge buildings when the WGS84 rectangle is rotated by projection.
            left, bottom, right, top = to_utm.transform_bounds(w, s, e, n, densify_pts=21)
            result.append(f'POLYGON (({left} {bottom},{right} {bottom},{right} {top},{left} {top},{left} {bottom}))')
    return result


def unique(rows, register):
    """Each record once. Neighbouring tiles both return the features on their border."""
    seen, result = {}, []
    for row in rows:
        row_id = identity(row.get('id_lokalId'))
        if row_id and row_id in seen:
            if seen[row_id] != row:
                raise ValueError(f'{register}: tiles returned different versions of one record')
            continue
        if row_id:
            seen[row_id] = row
        result.append(row)
    return result


ATTEMPTS = 5
# Datafordeler now and then fails one page of a long pagination with this error, and
# the same request succeeds a moment later. A dense town cell asks for hundreds of pages.
TRANSIENT_GRAPHQL = {'Unexpected Execution Error'}


def graphql_messages(errors):
    return sorted({str(e.get('message') if isinstance(e, dict) else e)[:200] for e in errors})


def request_page(register, query, variables, key):
    # Never include the authenticated URL or raw HTTP exception in logs/errors.
    endpoint = 'DAR' if register == 'DAR_POINT' else register
    url = f'https://graphql.datafordeler.dk/{endpoint}/v2?' + urlencode({'apiKey': key})
    body = json.dumps({'query': query, 'variables': variables}).encode()
    for attempt in range(ATTEMPTS):
        retry = attempt < ATTEMPTS - 1
        request = Request(url, data=body, headers={'Content-Type': 'application/json', 'User-Agent': 'Arnis-DK/0.1'})
        try:
            with urlopen(request, timeout=60) as response:
                raw = response.read(16 * 1024 * 1024 + 1)
            if len(raw) > 16 * 1024 * 1024:
                raise ValueError(f'{register}: response exceeds size limit')
            result = json.loads(raw)
            if result.get('errors'):
                messages = graphql_messages(result['errors'])
                if retry and set(messages) <= TRANSIENT_GRAPHQL:
                    time.sleep(2 ** attempt)
                    continue
                raise ValueError(f'{register}: GraphQL rejected the query: {"; ".join(messages)} '
                                 '(no partial data accepted)')
            return result
        except HTTPError as error:
            if error.code in (429, 500, 502, 503, 504) and retry:
                time.sleep(2 ** attempt)
                continue
            raise ValueError(f'{register}: HTTP {error.code}; check Datafordeler access/service status') from None
        except (URLError, TimeoutError):
            if retry:
                time.sleep(2 ** attempt)
                continue
            raise ValueError(f'{register}: connection failed or timed out') from None
        except json.JSONDecodeError:
            raise ValueError(f'{register}: invalid JSON response') from None


def download_rows(register, where, at, key, transport=request_page):
    entity = ENTITIES[register]
    query = f'''query Buildings($where: {entity}FilterInput!, $at: DafDateTime!, $after: String) {{
      {entity}(first: 100, after: $after, registreringstid: $at, virkningstid: $at, where: $where) {{
        nodes {{ {FIELDS[register]} }} pageInfo {{ hasNextPage endCursor }}
      }}
    }}'''
    cursor, cursors, rows = None, set(), []
    for _ in range(1000):
        doc = transport(register, query, {'where': where, 'at': at.isoformat(), 'after': cursor}, key)
        if doc.get('errors'):
            raise ValueError(f'{register}: partial GraphQL response rejected')
        try:
            page = doc['data'][entity]
            nodes, info = page['nodes'], page['pageInfo']
            more = info['hasNextPage']
        except (KeyError, TypeError):
            raise ValueError(f'{register}: missing result or pagination metadata') from None
        if not isinstance(nodes, list) or not all(isinstance(row, dict) for row in nodes) or type(more) is not bool:
            raise ValueError(f'{register}: invalid response shape')
        rows.extend(nodes)
        if not more:
            return rows
        cursor = info.get('endCursor')
        if not nodes or not isinstance(cursor, str) or not cursor or cursor in cursors:
            raise ValueError(f'{register}: pagination did not advance')
        cursors.add(cursor)
    raise ValueError(f'{register}: page limit reached; reduce bbox')


def linked_rows(register, ids, at, key, transport, field='id_lokalId'):
    ids = sorted({identity(value) for value in ids if identity(value)})
    result = []
    for start in range(0, len(ids), 100):
        batch = ids[start:start+100]
        rows = download_rows(register, {field: {'in': batch}}, at, key, transport)
        if any(identity(row.get(field)) not in batch for row in rows):
            raise ValueError(f'{register}: response contains an unrequested identity')
        result.extend(rows)
    return result


def fetch(bbox, key, at=None, transport=request_page):
    bbox = bounds(','.join(map(str, bbox)), AREA_KM2)
    at = at or datetime.now(timezone.utc)
    if at.tzinfo is None:
        raise ValueError('Snapshot time must have a timezone')
    outlines = unique([row for wkt in envelopes(bbox) for row in download_rows(
        'GEODKV', {'geometri': {'intersects': {'wkt': wkt, 'crs': 25832}},
                   'status': {'eq': 'Anlagt'}, 'geometristatus': {'eq': 'Endelig'}}, at, key, transport)],
        'GEODKV')
    converted = []
    for row in outlines:
        if not active(row, 'Anlagt', at) or row.get('geometristatus') != 'Endelig' or row.get('metode3D') == 'Under terræn':
            continue
        geometry = row.get('geometri')
        if not isinstance(geometry, dict) or geometry.get('crs') != 25832 or not isinstance(geometry.get('wkt'), str):
            raise ValueError('GeoDanmark geometry must be WKT in EPSG:25832')
        converted.append({**row, 'geometri': geometry['wkt']})
    bbr = linked_rows('BBR', [row.get('BBRUUID') for row in converted], at, key, transport)
    bbr = [{ALIASES.get(k, k): v for k, v in row.items()} for row in bbr if active(row, '6', at)]
    dar = linked_rows('DAR', [row.get('husnummer') for row in bbr], at, key, transport)
    # BBR names only the building's primary house number. Other entrances can
    # have their own DAR house numbers on the exact same GeoDanmark building.
    related = linked_rows('DAR', [row.get('id_lokalId') for row in converted], at, key,
                          transport, field='geoDanmarkBygning')
    dar_index = {}
    for source in (dar, related):
        seen = set()
        for row in source:
            if not active(row, '3', at):
                continue
            dar_id = identity(row.get('id_lokalId'))
            if not dar_id or dar_id in seen or (dar_id in dar_index and dar_index[dar_id] != row):
                raise ValueError('DAR: ambiguous current house number')
            seen.add(dar_id)
            dar_index[dar_id] = row
    dar = [dar_index[k] for k in sorted(dar_index)]
    points = linked_rows('DAR_POINT', [row.get('adgangspunkt') for row in dar], at, key, transport)
    point_index = {}
    for point in points:
        if not active(point, '8', at):
            continue
        point_id = identity(point['id_lokalId'])
        if point_id in point_index:
            raise ValueError('DAR: ambiguous current access point')
        point_index[point_id] = point
    # Embed the exact linked record in the normalized local Husnummer snapshot.
    dar = [{**row, '_arnis_adgangspunkt': point_index.get(identity(row.get('adgangspunkt')))} for row in dar]
    return converted, bbr, dar, at


def atomic_json(path, document):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary = tempfile.mkstemp(prefix=path.name+'.', suffix='.tmp', dir=path.parent)
    try:
        with os.fdopen(handle, 'w', encoding='utf-8') as stream:
            json.dump(document, stream, ensure_ascii=False, allow_nan=False)
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--bbox', required=True, help='min_lat,min_lon,max_lat,max_lon; max 100 km2, asked for in tiles of 3 x 3 km')
    parser.add_argument('--credentials-file', type=Path, help='Local .env file; otherwise read DATAFORDELER_API_KEY')
    parser.add_argument('--output', required=True, type=Path)
    parser.add_argument('--hedges', action=argparse.BooleanOptionalAction, default=True,
                        help='Add GeoDanmark hedges and scrub; untyped hedges need DATAFORSYNINGEN_TOKEN')
    args = parser.parse_args()
    try:
        bbox = bounds(args.bbox, AREA_KM2)
        key = load_key(args.credentials_file)
        outlines, bbr, dar, at = fetch(bbox, key)
        # Each successful run is an immutable local snapshot, usable by prepare.py offline.
        snapshot = args.output.parent / (args.output.stem + '-sources-' + at.strftime('%Y%m%dT%H%M%S%fZ'))
        paths = [snapshot / name for name in ('GeoDanmark-Bygning.json', 'BBR-Bygning.json', 'DAR-Husnummer.json')]
        for path, rows in zip(paths, (outlines, bbr, dar)):
            atomic_json(path, rows)
        result = prepare(*paths, bbox=bbox, at=at)
        if args.hedges:
            from hedges import add, load_token
            token = load_token(args.credentials_file)
            if not token:
                print('Note: no DATAFORSYNINGEN_TOKEN; only hedges GeoDanmark types as living are added.')
            add(result, bbox, outlines, key, at, token)
        result['arnis_dk']['download'] = {'service': 'Datafordeler GraphQL v2', 'snapshot': snapshot.name}
        atomic_json(args.output, result)
        print(json.dumps(result['arnis_dk']['counts'], indent=2))
        print('Prepared supplement:', args.output)
    except (ValueError, OSError, KeyError, TypeError) as error:
        # Do not expose request URLs, credentials or provider response bodies.
        parser.exit(1, f'Building download failed: {error}\n')


if __name__ == '__main__':
    main()
