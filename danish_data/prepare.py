"""Prepare a terrain-independent Arnis supplement from official Danish JSON extracts."""
import argparse
from collections import Counter
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import zipfile

import ijson
from pyproj import Transformer
from shapely import force_2d, from_wkt
from shapely.errors import GEOSException
from shapely.geometry import box
from shapely.ops import transform


WALLS = {"1": "brick", "2": "concrete", "4": "timber_framing", "5": "wood",
         "6": "concrete", "8": "metal", "12": "glass"}
# Only unambiguous material mappings; unknown codes stay unset.
ROOFS = {"1": "tar_paper", "2": "tar_paper", "3": "fibercement", "10": "fibercement", "11": "plastic", "4": "concrete", "5": "roof_tiles", "6": "metal",
         "7": "thatch", "12": "glass",
         "20": "green_roof"}
USES = {"110": "farm", "120": "detached", "130": "terrace", "131": "terrace",
        "132": "terrace", "140": "apartments", "150": "dormitory", "160": "residential",
        "320": "office", "321": "office", "322": "retail", "510": "house", "910": "garage",
        "920": "carport", "930": "shed"}


def identity(value):
    return str(value or "").strip().rstrip("/").rsplit("/", 1)[-1].lower()


def entrance_tags(address, geodanmark_id, geometry, at):
    """TD is near a door; TK only identifies the road-facing facade. Never use TN."""
    point = address.get('_arnis_adgangspunkt')
    if not isinstance(point, dict) or not active(point, '8', at):
        return {}
    if not identity(address.get('adgangspunkt')) or identity(point.get('id_lokalId')) != identity(address['adgangspunkt']):
        return {}
    standard = point.get('oprindelse_tekniskStandard')
    if standard not in ('TD', 'TK'):
        return {}
    # A shared BBR address is not a door on every shed on the property.
    if identity(address.get('geoDanmarkBygning')) != geodanmark_id:
        return {}
    position = point.get('position')
    if not isinstance(position, dict) or position.get('crs') != 25832:
        return {}
    try:
        p = force_2d(from_wkt(position.get('wkt', '')))
        if p.geom_type != 'Point' or p.is_empty or not p.is_valid:
            return {}
        lon, lat = Transformer.from_crs(25832, 4326, always_xy=True).transform(p.x, p.y)
        if not (math.isfinite(lon) and math.isfinite(lat)):
            return {}
        # The official point belongs inside this building, typically ~3 m in.
        if not geometry.covers(from_wkt(f'POINT ({lon} {lat})')):
            return {}
    except (ValueError, TypeError, GEOSException):
        return {}
    return {'arnis:entrance:lat': str(lat), 'arnis:entrance:lon': str(lon),
            'arnis:entrance:standard': standard}


def active(row, status, at):
    if str(row.get("status")) != status:
        return False
    for prefix in ("registrering", "virkning"):
        for suffix, before in (("Fra", True), ("Til", False)):
            value = row.get(prefix + suffix)
            if not value:
                continue
            date = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if date.tzinfo is None:
                raise ValueError("Source timestamps must have a timezone")
            if (before and date > at) or (not before and date <= at):
                return False
    return True


@contextmanager
def stream(path):
    path = Path(path)
    if path.suffix.lower() == ".zip":
        with zipfile.ZipFile(path) as archive:
            members = [m for m in archive.infolist() if m.filename.lower().endswith(".json")]
            if len(members) != 1:
                raise ValueError("Each ZIP must contain exactly one entity JSON file")
            with archive.open(members[0]) as src:
                yield src
    else:
        with path.open("rb") as src:
            yield src


def rows(path):
    # Datafordeler Current entity downloads are JSON arrays. Fail on a wrong
    # envelope rather than reporting a misleading successful empty import.
    with stream(path) as src:
        first = src.read(1)
        while first and first.isspace():
            first = src.read(1)
        if first != b"[":
            raise ValueError("Expected a Datafordeler entity JSON array")
    with stream(path) as src:
        yield from ijson.items(src, "item", use_float=True)


def lookup(path, needed, status, at):
    result = {}
    if path:
        for row in rows(path):
            key = identity(row.get("id_lokalId"))
            if key in needed and active(row, status, at):
                if key in result:
                    raise ValueError("Ambiguous current register versions for " + key)
                result[key] = row
    return result


def bbr_tags(row):
    tags = {"arnis:bbr_id": identity(row["id_lokalId"])}
    for field, key, table in (("byg032YdervæggensMateriale", "building:material", WALLS),
                              ("byg033Tagdækningsmateriale", "roof:material", ROOFS),
                              ("byg021BygningensAnvendelse", "building", USES)):
        value = table.get(str(row.get(field)))
        if value:
            tags[key] = value
    levels = row.get("byg054AntalEtager")
    if levels is not None:
        number = float(levels)
        if math.isfinite(number) and number.is_integer() and 1 <= number <= 200:
            tags["building:levels"] = str(int(number))
    year = row.get("byg026Opførelsesår")
    if year and str(year).isdigit() and 1000 <= int(year) <= 2100:
        tags["start_date"] = str(year)
    return tags


def prepare(geodanmark, bbr=None, dar=None, bbox=None, at=None):
    at = at or datetime.now(timezone.utc)
    if at.tzinfo is None:
        raise ValueError("--at requires a timezone")
    # Input WKT is ETRS89 / UTM32 (25832); Z is deliberately discarded.
    to_ll = Transformer.from_crs(25832, 4326, always_xy=True)
    area = box(bbox[1], bbox[0], bbox[3], bbox[2])
    selected, seen, counts = [], set(), Counter()
    for row in rows(geodanmark):
        counts["geodanmark_rows"] += 1
        if not active(row, "Anlagt", at) or row.get("geometristatus") != "Endelig" or row.get("metode3D") == "Under terræn":
            counts["inactive_or_unfinalized"] += 1
            continue
        polygon = transform(to_ll.transform, force_2d(from_wkt(row["geometri"])))
        if polygon.is_empty or not polygon.intersects(area):
            continue
        if polygon.geom_type not in ("Polygon", "MultiPolygon") or not polygon.is_valid:
            counts["invalid_geometry"] += 1
            continue
        key = identity(row.get("id_lokalId"))
        if not key or key in seen:
            raise ValueError("Missing or duplicate current GeoDanmark identity")
        seen.add(key)
        selected.append((key, row, polygon))
    bbr_rows = lookup(bbr, {identity(r.get("BBRUUID")) for _, r, _ in selected}, "6", at)
    primary_ids = {identity(r.get("husnummer")) for r in bbr_rows.values()}
    dar_rows, building_addresses = {}, {}
    if dar:
        for address in rows(dar):
            dar_id = identity(address.get('id_lokalId'))
            building_id = identity(address.get('geoDanmarkBygning'))
            if not active(address, '3', at) or (dar_id not in primary_ids and building_id not in seen):
                continue
            if not dar_id or dar_id in dar_rows:
                raise ValueError('Ambiguous current DAR house number')
            dar_rows[dar_id] = address
            if building_id in seen:
                building_addresses.setdefault(building_id, []).append(address)
    elements, ids = [], set()

    def osm_id(key):
        # Separate from real OSM IDs and Overture's 2^63 namespace.
        value = (1 << 60) | (int.from_bytes(hashlib.sha256(key.encode()).digest()[:8], "big") & ((1 << 60) - 1))
        if value in ids:
            raise ValueError("Synthetic ID collision")
        ids.add(value)
        return value

    def way(key, ring, tags):
        node_ids = []
        for index, (lon, lat, *_) in enumerate(list(ring.coords)[:-1]):
            node_id = osm_id(f"{key}/node/{index}")
            node_ids.append(node_id)
            elements.append({"type": "node", "id": node_id, "lon": lon, "lat": lat})
        node_ids.append(node_ids[0])
        way_id = osm_id(key)
        elements.append({"type": "way", "id": way_id, "nodes": node_ids, "tags": tags})
        return way_id

    for key, row, geometry in sorted(selected, key=lambda item: item[0]):
        tags = {"building": "yes", "arnis:geodanmark_id": key}
        record = bbr_rows.get(identity(row.get("BBRUUID")))
        if record:
            tags.update(bbr_tags(record))
            counts["bbr_matches"] += 1
            address = dar_rows.get(identity(record.get("husnummer")))
            if address:
                tags["arnis:dar_id"] = identity(address["id_lokalId"])
                if address.get("husnummertekst"):
                    tags["addr:housenumber"] = address["husnummertekst"]
                full_address = address.get('adgangsadressebetegnelse')
                if isinstance(full_address, str) and full_address.strip():
                    # Preserve the official address; never infer street/postcode.
                    tags['arnis:address'] = ' '.join(full_address.split())
                counts["dar_matches"] += 1
                hint = entrance_tags(address, key, geometry, at)
                tags.update(hint)
        hints = []
        for address in sorted(building_addresses.get(key, []), key=lambda r: identity(r['id_lokalId'])):
            hint = entrance_tags(address, key, geometry, at)
            full_address = address.get('adgangsadressebetegnelse')
            if hint and isinstance(full_address, str) and full_address.strip():
                hints.append({'address': ' '.join(full_address.split()),
                              'lat': float(hint['arnis:entrance:lat']),
                              'lon': float(hint['arnis:entrance:lon']),
                              'standard': hint['arnis:entrance:standard']})
                counts['entrance_hints_' + hint['arnis:entrance:standard']] += 1
        if hints:
            tags['arnis:entrances'] = json.dumps(hints, ensure_ascii=False, allow_nan=False)
        elif 'arnis:entrance:standard' in tags:
            counts['entrance_hints_' + tags['arnis:entrance:standard']] += 1
        polygons = [geometry] if geometry.geom_type == "Polygon" else list(geometry.geoms)
        if len(polygons) == 1 and not polygons[0].interiors:
            way(key + "/outline", polygons[0].exterior, tags)
        else:
            members = []
            for p, polygon in enumerate(polygons):
                for i, ring in enumerate([polygon.exterior, *polygon.interiors]):
                    members.append({"type": "way", "ref": way(f"{key}/{p}/{i}", ring, {}),
                                    "role": "outer" if i == 0 else "inner"})
            elements.append({"type": "relation", "id": osm_id(key + "/relation"),
                             "tags": {**tags, "type": "multipolygon"}, "members": members})
    counts["selected_buildings"] = len(selected)
    sources = []
    for label, path in (("GeoDanmark", geodanmark), ("BBR", bbr), ("DAR", dar)):
        if path:
            with Path(path).open("rb") as src:
                sha = hashlib.file_digest(src, "sha256").hexdigest()
            sources.append({"register": label, "file": Path(path).name, "sha256": sha})
    return {"arnis_dk": {"schema": "arnis-dk.buildings/1", "bbox": bbox, "at": at.isoformat(),
                          "sources": sources, "counts": dict(counts),
                          "attribution": "GeoDanmark (Klimadatastyrelsen og kommunerne); BBR; Danmarks Adresseregister"},
            "elements": elements}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--geodanmark", required=True, help="GeoDanmark Bygning JSON or ZIP; EPSG:25832 WKT")
    parser.add_argument("--bbr", help="BBR Bygning entity JSON or ZIP")
    parser.add_argument("--dar", help="DAR Husnummer entity JSON or ZIP")
    parser.add_argument("--bbox", required=True, help="min_lat,min_lon,max_lat,max_lon (WGS84)")
    parser.add_argument("--at", help="ISO timestamp with timezone; default now")
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    try:
        bounds = [float(value) for value in args.bbox.split(",")]
        if len(bounds) != 4 or not all(math.isfinite(n) for n in bounds) or not (-90 <= bounds[0] < bounds[2] <= 90 and -180 <= bounds[1] < bounds[3] <= 180):
            raise ValueError("Invalid bbox")
        at = datetime.fromisoformat(args.at.replace("Z", "+00:00")) if args.at else None
        result = prepare(args.geodanmark, args.bbr, args.dar, bounds, at)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(result, ensure_ascii=False, allow_nan=False), encoding="utf-8")
        print(json.dumps(result["arnis_dk"]["counts"], indent=2))
    except (ValueError, OSError, KeyError) as error:
        parser.exit(1, str(error) + "\n")


if __name__ == "__main__":
    main()
