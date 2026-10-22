//! Optional Danish building supplement. Terrain always stays with upstream Arnis.
use crate::args::Args;
use crate::coordinate_system::geographic::LLBBox;
use crate::osm_parser::{self, ProcessedElement, ProcessedMemberRole, ProcessedWay};
use geo::{Area, BooleanOps, BoundingRect, Contains, LineString, MultiPolygon, Polygon};
use std::collections::{HashMap, HashSet};
#[path = "danish_addresses.rs"]
mod addresses;
#[path = "danish_entrances.rs"]
mod entrances;

fn ring(way: &ProcessedWay) -> LineString<f64> {
    LineString::from(
        way.nodes
            .iter()
            .map(|n| (n.x as f64, n.z as f64))
            .collect::<Vec<_>>(),
    )
}

fn footprint(element: &ProcessedElement) -> Option<MultiPolygon<f64>> {
    if !element.tags().contains_key("building") && !element.tags().contains_key("building:part") {
        return None;
    }
    let polygons = match element {
        ProcessedElement::Way(w) => vec![Polygon::new(ring(w), vec![])],
        ProcessedElement::Relation(r) => {
            let holes: Vec<_> = r
                .members
                .iter()
                .filter(|m| m.role == ProcessedMemberRole::Inner)
                .map(|m| ring(&m.way))
                .collect();
            r.members
                .iter()
                .filter(|m| m.role == ProcessedMemberRole::Outer)
                .map(|m| {
                    let exterior = Polygon::new(ring(&m.way), vec![]);
                    let interiors = holes
                        .iter()
                        .filter(|h| {
                            h.0.first()
                                .is_some_and(|p| exterior.contains(&geo::Point(*p)))
                        })
                        .cloned()
                        .collect();
                    Polygon::new(exterior.exterior().clone(), interiors)
                })
                .collect()
        }
        _ => return None,
    };
    let result = MultiPolygon(polygons);
    (result.unsigned_area() > 0.0).then_some(result)
}

#[derive(Default)]
struct Index {
    cells: HashMap<(i32, i32), Vec<usize>>,
    shapes: HashMap<usize, MultiPolygon<f64>>,
}

fn cells(shape: &MultiPolygon<f64>) -> Vec<(i32, i32)> {
    let Some(b) = shape.bounding_rect() else {
        return vec![];
    };
    let mut result = Vec::new();
    for x in (b.min().x as i32).div_euclid(64)..=(b.max().x as i32).div_euclid(64) {
        for z in (b.min().y as i32).div_euclid(64)..=(b.max().y as i32).div_euclid(64) {
            result.push((x, z));
        }
    }
    result
}

impl Index {
    fn insert(&mut self, index: usize, shape: MultiPolygon<f64>) {
        for cell in cells(&shape) {
            self.cells.entry(cell).or_default().push(index);
        }
        self.shapes.insert(index, shape);
    }

    fn overlaps(&self, shape: &MultiPolygon<f64>) -> Vec<(usize, f64)> {
        let candidates: HashSet<_> = cells(shape)
            .iter()
            .filter_map(|c| self.cells.get(c))
            .flatten()
            .copied()
            .collect();
        let area = shape.unsigned_area();
        let mut result = Vec::new();
        for index in candidates {
            let other = &self.shapes[&index];
            let intersection = shape.intersection(other).unsigned_area();
            // Shared edges have zero area and do not count as duplicate buildings.
            if intersection > 0.0 {
                result.push((
                    index,
                    intersection / (area + other.unsigned_area() - intersection),
                ));
            }
        }
        result.sort_by_key(|(i, _)| *i);
        result
    }

    fn remove(&mut self, index: usize) {
        if let Some(shape) = self.shapes.remove(&index) {
            for cell in cells(&shape) {
                if let Some(list) = self.cells.get_mut(&cell) {
                    list.retain(|&i| i != index);
                }
            }
        }
    }
}

/// An OSM building gives way to the register houses inside it when it is a whole block
/// drawn as one outline: at least this many houses lie mostly inside it, not counting
/// outbuildings or anything under [`BLOCK_HOUSE_MIN_AREA`] (a villa drawn with its garage
/// is one building, not a block)...
const BLOCK_MIN_HOUSES: usize = 3;
const BLOCK_HOUSE_MIN_AREA: f64 = 25.0;
const OUTBUILDINGS: [&str; 8] = [
    "shed", "garage", "garages", "carport", "greenhouse", "roof", "hut", "outbuilding",
];
/// ...each with at least this share of its own area inside the block...
const BLOCK_HOUSE_INSIDE: f64 = 0.5;
/// ...and at most this share in other OSM buildings (outlines differ by a few decimetres)...
const BLOCK_HOUSE_ELSEWHERE: f64 = 0.05;
/// ...and together the houses cover at least this share of the block, without stacking
/// (at most the second share), and none covers most of it alone (a building with an annex).
const BLOCK_COVERED: (f64, f64) = (0.6, 1.1);
const BLOCK_ONE_HOUSE: f64 = 0.75;

/// Single buildings that may well be split in the register (a nave and a tower) but whose
/// OSM outline and tags describe the whole.
fn splittable(element: &ProcessedElement) -> bool {
    let tags = element.tags();
    !tags.contains_key("building:part")
        && !tags.contains_key("historic")
        && !tags.get("building").is_some_and(|v| {
            matches!(
                v.as_str(),
                "church" | "cathedral" | "chapel" | "castle" | "mosque" | "synagogue" | "temple"
            )
        })
}

/// The OSM buildings that are blocks of register houses (see [`BLOCK_MIN_HOUSES`]), each
/// with the incoming houses that replace it.
fn blocks(
    existing: &[ProcessedElement],
    incoming: &[ProcessedElement],
    index: &Index,
    matches: &[Option<(MultiPolygon<f64>, Vec<(usize, f64)>)>],
) -> HashMap<usize, Vec<usize>> {
    let house = |j: usize, area: f64| {
        area >= BLOCK_HOUSE_MIN_AREA
            && !incoming[j]
                .tags()
                .get("building")
                .is_some_and(|v| OUTBUILDINGS.contains(&v.as_str()))
    };
    let mut inside: HashMap<usize, Vec<(usize, f64, bool)>> = HashMap::new();
    for (j, matched) in matches.iter().enumerate() {
        let Some((shape, overlaps)) = matched else {
            continue;
        };
        let area = shape.unsigned_area();
        // The shared area back from the IoU: iou = s / (a + b - s).
        let shared: Vec<(usize, f64)> = overlaps
            .iter()
            .map(|&(i, iou)| (i, iou * (area + index.shapes[&i].unsigned_area()) / (1.0 + iou)))
            .collect();
        let total: f64 = shared.iter().map(|(_, s)| s).sum();
        if let Some(&(i, s)) = shared.iter().max_by(|a, b| a.1.total_cmp(&b.1)) {
            if s >= BLOCK_HOUSE_INSIDE * area && total - s <= BLOCK_HOUSE_ELSEWHERE * area {
                inside.entry(i).or_default().push((j, s, house(j, area)));
            }
        }
    }
    inside
        .into_iter()
        .filter(|(i, houses)| {
            let area = index.shapes[i].unsigned_area();
            let covered: f64 = houses.iter().map(|(_, s, _)| s).sum();
            let largest = houses.iter().map(|(_, s, _)| *s).fold(0.0, f64::max);
            houses.iter().filter(|(_, _, main)| *main).count() >= BLOCK_MIN_HOUSES
                && splittable(&existing[*i])
                && covered >= BLOCK_COVERED.0 * area
                && covered <= BLOCK_COVERED.1 * area
                && largest <= BLOCK_ONE_HOUSE * area
        })
        .map(|(i, houses)| (i, houses.into_iter().map(|(j, _, _)| j).collect()))
        .collect()
}

/// A house that replaces a block keeps the block's look where the register says nothing.
fn inherit(house: &mut ProcessedElement, block: &ProcessedElement) {
    let tags = match house {
        ProcessedElement::Way(w) => &mut w.tags,
        ProcessedElement::Relation(r) => &mut r.tags,
        _ => return,
    };
    for key in [
        "building:colour",
        "roof:colour",
        "building:material",
        "roof:material",
        "building:levels",
    ] {
        if let Some(value) = block.tags().get(key) {
            tags.entry(key.to_string()).or_insert_with(|| value.clone());
        }
    }
    if tags.get("building").is_some_and(|v| v == "yes") {
        if let Some(value) = block.tags().get("building").filter(|v| v.as_str() != "yes") {
            tags.insert("building".into(), value.clone());
        }
    }
}

/// Adds only footprints without positive-area overlaps. Attribute enrichment is
/// limited to unambiguous one-to-one matches with IoU >= 0.6; OSM values win.
/// With `enrich` (the Danish register), an OSM block drawn as one outline over several
/// register houses is replaced by those houses (see [`BLOCK_MIN_HOUSES`]): otherwise a
/// whole street of old houses became one flat block.
fn merge(
    existing: &mut Vec<ProcessedElement>,
    incoming: Vec<ProcessedElement>,
    enrich: bool,
) -> (usize, usize, usize) {
    let mut index = Index::default();
    for (i, element) in existing.iter().enumerate() {
        if let Some(shape) = footprint(element) {
            index.insert(i, shape);
        }
    }
    let matches: Vec<_> = incoming
        .iter()
        .map(|element| {
            footprint(element).map(|p| {
                let overlaps = index.overlaps(&p);
                (p, overlaps)
            })
        })
        .collect();
    if enrich {
        addresses::attach(existing, &incoming, &index);
    }
    let mut uses: HashMap<usize, usize> = HashMap::new();
    for (_, overlaps) in matches.iter().flatten() {
        for (i, _) in overlaps {
            *uses.entry(*i).or_default() += 1;
        }
    }
    let (mut added, mut filled, mut skipped) = (0, 0, 0);
    let split = if enrich {
        blocks(existing, &incoming, &index, &matches)
    } else {
        HashMap::new()
    };
    let mut incoming: Vec<Option<ProcessedElement>> = incoming.into_iter().map(Some).collect();
    if !split.is_empty() {
        for &block in split.keys() {
            index.remove(block);
        }
        // The houses go in first, so nothing else claims their ground; a duplicate house
        // in the register is dropped like any other overlap.
        let mut houses = Index::default();
        let mut taken = 0;
        let mut order: Vec<_> = split.iter().collect();
        order.sort();
        for (&block, members) in order {
            for &j in members {
                let shape = matches[j].as_ref().unwrap().0.clone();
                let mut element = incoming[j].take().unwrap();
                if !houses.overlaps(&shape).is_empty() {
                    skipped += 1;
                    continue;
                }
                inherit(&mut element, &existing[block]);
                houses.insert(existing.len(), shape.clone());
                index.insert(existing.len(), shape);
                existing.push(element);
                added += 1;
                taken += 1;
            }
        }
        println!(
            "Danish buildings: {} OSM blocks drawn as one outline gave way to {taken} register houses",
            split.len()
        );
    }
    for (element, matched) in incoming.into_iter().zip(matches) {
        let (Some(element), Some((shape, overlaps))) = (element, matched) else {
            continue;
        };
        let overlaps: Vec<_> = overlaps
            .into_iter()
            .filter(|(i, _)| !split.contains_key(i))
            .collect();
        if overlaps.is_empty() && index.overlaps(&shape).is_empty() {
            index.insert(existing.len(), shape);
            existing.push(element);
            added += 1;
        } else {
            skipped += 1;
            if enrich && overlaps.len() == 1 {
                let (i, iou) = overlaps[0];
                if iou < 0.6 || uses[&i] != 1 || existing[i].tags().contains_key("building:part") {
                    continue;
                }
                let tags = match &mut existing[i] {
                    ProcessedElement::Way(w) => &mut w.tags,
                    ProcessedElement::Relation(r) => &mut r.tags,
                    _ => continue,
                };
                let mut changed = false;
                for key in [
                    "building:levels",
                    "building:material",
                    "roof:material",
                    "start_date",
                    "addr:housenumber",
                    "arnis:geodanmark_id",
                    "arnis:bbr_id",
                    "arnis:dar_id",
                    "arnis:address",
                    "arnis:entrance:lat",
                    "arnis:entrance:lon",
                    "arnis:entrance:standard",
                    "arnis:entrances",
                ] {
                    if let Some(value) = element.tags().get(key) {
                        if !tags.contains_key(key) {
                            tags.insert(key.into(), value.clone());
                            changed = true;
                        }
                    }
                }
                if tags.get("building").is_some_and(|v| v == "yes") {
                    if let Some(value) = element
                        .tags()
                        .get("building")
                        .filter(|v| v.as_str() != "yes")
                    {
                        tags.insert("building".into(), value.clone());
                        changed = true;
                    }
                }
                filled += usize::from(changed);
            }
        }
    }
    if !split.is_empty() {
        let mut position = 0;
        existing.retain(|_| {
            let keep = !split.contains_key(&position);
            position += 1;
            keep
        });
    }
    (added, filled, skipped)
}

pub fn apply(
    elements: &mut Vec<ProcessedElement>,
    args: &Args,
    bbox: LLBBox,
) -> Result<(), String> {
    let Some(path) = &args.danish_buildings else {
        return Ok(());
    };
    if args.skip_objects() {
        return Ok(());
    }
    let file = std::fs::File::open(path).map_err(|e| format!("Danish building input: {e}"))?;
    let value: serde_json::Value = serde_json::from_reader(std::io::BufReader::new(file))
        .map_err(|e| format!("Invalid Danish building JSON: {e}"))?;
    if value["arnis_dk"]["schema"] != "arnis-dk.buildings/1" {
        return Err(
            "Use danish_data/prepare.py to create an arnis-dk.buildings/1 supplement".into(),
        );
    }
    let bounds: [f64; 4] = serde_json::from_value(value["arnis_dk"]["bbox"].clone())
        .map_err(|_| "Danish supplement is missing its preparation bbox")?;
    let coverage = LLBBox::new(bounds[0], bounds[1], bounds[2], bounds[3])?;
    let epsilon = 1e-9;
    if bbox.min().lat() < coverage.min().lat() - epsilon
        || bbox.min().lng() < coverage.min().lng() - epsilon
        || bbox.max().lat() > coverage.max().lat() + epsilon
        || bbox.max().lng() > coverage.max().lng() + epsilon
    {
        return Err(
            "Prepare a Danish building supplement covering the entire selected bbox".into(),
        );
    }
    let raw: osm_parser::OsmData = serde_json::from_value(value).map_err(|e| e.to_string())?;
    let (incoming, _, _, _) = osm_parser::parse_osm_data(
        raw,
        bbox,
        args.debug,
        &crate::projection::ProjectionSpec::from_args(args),
    );
    let identities: HashSet<_> = elements.iter().map(|e| (e.kind(), e.id())).collect();
    if incoming
        .iter()
        .any(|e| identities.contains(&(e.kind(), e.id())))
    {
        return Err("Danish supplement has an ID collision with existing OSM data".into());
    }
    // Real hedges and scrub join as they are; the merge only takes building footprints.
    let (greenery, incoming): (Vec<_>, Vec<_>) = incoming
        .into_iter()
        .partition(|e| e.tags().contains_key("arnis:hedge"));
    let (added, filled, skipped) = merge(elements, incoming, true);
    println!("Danish buildings: {added} added, {filled} enriched, {skipped} overlapping footprints omitted");
    if !greenery.is_empty() {
        println!("Danish hedges and scrub: {}", greenery.len());
        elements.extend(greenery);
    }
    entrances::apply(elements, args, bbox)?;
    Ok(())
}

/// Includes multipolygon courtyards in the overlap check, unlike the upstream
/// centroid/bounding-box filter. Used when a Danish supplement is enabled.
pub fn merge_overture(elements: &mut Vec<ProcessedElement>, incoming: Vec<ProcessedElement>) {
    merge(elements, incoming, false);
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::osm_parser::{ProcessedMember, ProcessedNode, ProcessedRelation};
    use std::sync::Arc;

    fn building(id: u64, x: i32, z: i32, size: i32) -> ProcessedElement {
        rect(id, x, z, size, size)
    }

    fn rect(id: u64, x: i32, z: i32, w: i32, h: i32) -> ProcessedElement {
        ProcessedElement::Way(ProcessedWay {
            id,
            tags: HashMap::from([("building".into(), "yes".into())]),
            nodes: [(x, z), (x + w, z), (x + w, z + h), (x, z + h), (x, z)]
            .into_iter()
            .map(|(x, z)| ProcessedNode {
                id: 0,
                x,
                z,
                tags: HashMap::new(),
            })
            .collect(),
        })
    }

    #[test]
    fn overlap_is_enriched_once_and_osm_material_wins() {
        let mut osm = building(1, 0, 0, 10);
        let mut dk = building(2, 0, 0, 10);
        if let ProcessedElement::Way(w) = &mut osm {
            w.tags.insert("building:material".into(), "wood".into());
        }
        if let ProcessedElement::Way(w) = &mut dk {
            w.tags.insert("building:material".into(), "brick".into());
            w.tags.insert("building:levels".into(), "2".into());
        }
        let mut elements = vec![osm];
        assert_eq!(merge(&mut elements, vec![dk.clone()], true), (0, 1, 1));
        assert_eq!(merge(&mut elements, vec![dk], true), (0, 0, 1));
        assert_eq!(elements[0].tags()["building:material"], "wood");
        assert_eq!(elements[0].tags()["building:levels"], "2");
        assert_eq!(elements.len(), 1);
    }

    #[test]
    fn adjacent_buildings_survive_but_duplicate_inputs_do_not() {
        let mut elements = vec![building(1, 0, 0, 10)];
        assert_eq!(
            merge(
                &mut elements,
                vec![building(2, 10, 0, 10), building(3, 10, 0, 10)],
                true
            ),
            (1, 0, 1)
        );
    }

    #[test]
    fn ambiguous_matches_do_not_copy_metadata() {
        let mut elements = vec![building(1, 0, 0, 10)];
        let mut one = building(2, 0, 0, 10);
        if let ProcessedElement::Way(w) = &mut one {
            w.tags.insert("building:levels".into(), "3".into());
        }
        merge(&mut elements, vec![one, building(3, 0, 0, 10)], true);
        assert!(!elements[0].tags().contains_key("building:levels"));
    }

    #[test]
    fn contained_addresses_do_not_assign_one_buildings_style_to_a_whole_row() {
        let mut one = building(2, 0, 0, 10);
        let mut two = building(3, 10, 0, 10);
        for (element, address, lon) in [
            (&mut one, "Testvej 1, 4200 Slagelse", "11.1"),
            (&mut two, "Testvej 3, 4200 Slagelse", "11.2"),
        ] {
            if let ProcessedElement::Way(w) = element {
                w.tags.extend(HashMap::from([
                    ("arnis:address".into(), address.into()),
                    ("arnis:entrance:lat".into(), "55.1".into()),
                    ("arnis:entrance:lon".into(), lon.into()),
                    ("arnis:entrance:standard".into(), "TK".into()),
                    ("building:material".into(), "brick".into()),
                ]));
            }
        }
        let incoming = vec![one, two];
        let mut elements = vec![building(1, 0, 0, 20)];
        merge(&mut elements, incoming.clone(), true);
        let hints: Vec<addresses::AddressHint> =
            serde_json::from_str(&elements[0].tags()[addresses::HINTS]).unwrap();
        assert_eq!(hints.len(), 2);
        assert!(!elements[0].tags().contains_key("arnis:address"));
        assert!(!elements[0].tags().contains_key("building:material"));
        let mut ambiguous = vec![building(1, 0, 0, 20), building(4, 0, 0, 20)];
        merge(&mut ambiguous, incoming, true);
        assert!(ambiguous
            .iter()
            .all(|e| !e.tags().contains_key(addresses::HINTS)));
    }

    fn tag(mut element: ProcessedElement, key: &str, value: &str) -> ProcessedElement {
        if let ProcessedElement::Way(w) = &mut element {
            w.tags.insert(key.into(), value.into());
        }
        element
    }

    fn ids(elements: &[ProcessedElement]) -> Vec<u64> {
        let mut ids: Vec<_> = elements.iter().map(|e| e.id()).collect();
        ids.sort();
        ids
    }

    fn courtyard_block(id: u64) -> ProcessedElement {
        let ProcessedElement::Way(outer) = building(id * 100, 0, 0, 30) else {
            unreachable!()
        };
        let ProcessedElement::Way(inner) = building(id * 100 + 1, 10, 10, 10) else {
            unreachable!()
        };
        ProcessedElement::Relation(ProcessedRelation {
            id,
            tags: HashMap::from([
                ("building".into(), "apartments".into()),
                ("building:colour".into(), "yellow".into()),
            ]),
            members: vec![
                ProcessedMember {
                    role: ProcessedMemberRole::Outer,
                    way: Arc::new(outer),
                },
                ProcessedMember {
                    role: ProcessedMemberRole::Inner,
                    way: Arc::new(inner),
                },
            ],
        })
    }

    #[test]
    fn a_block_drawn_as_one_outline_gives_way_to_its_register_houses() {
        let block = tag(
            tag(rect(1, 0, 0, 30, 10), "building", "apartments"),
            "building:colour",
            "red",
        );
        let houses = vec![
            tag(rect(2, 0, 0, 10, 10), "building:levels", "3"),
            rect(3, 10, 0, 10, 10),
            tag(rect(4, 20, 0, 10, 10), "building", "house"),
        ];
        let mut elements = vec![block];
        assert_eq!(merge(&mut elements, houses, true), (3, 0, 0));
        assert_eq!(ids(&elements), [2, 3, 4], "the block itself is gone");
        let by_id = |id| elements.iter().find(|e| e.id() == id).unwrap().tags();
        assert!(elements
            .iter()
            .all(|e| e.tags()["building:colour"] == "red"));
        assert_eq!(by_id(2)["building:levels"], "3");
        assert_eq!(by_id(3)["building"], "apartments");
        assert_eq!(by_id(4)["building"], "house", "the register's own type wins");
    }

    #[test]
    fn a_courtyard_block_splits_and_its_courtyard_stays_open() {
        let houses = vec![
            rect(2, 0, 0, 30, 10),
            rect(3, 0, 20, 30, 10),
            rect(4, 0, 10, 10, 10),
            rect(5, 20, 10, 10, 10),
            building(6, 12, 12, 5),
        ];
        let mut elements = vec![courtyard_block(1)];
        assert_eq!(merge(&mut elements, houses, true), (5, 0, 0));
        assert_eq!(ids(&elements), [2, 3, 4, 5, 6]);
        assert_eq!(elements[0].tags()["building:colour"], "yellow");
    }

    #[test]
    fn a_partly_covered_building_or_a_landmark_keeps_its_osm_outline() {
        // Two small houses cover a sixth of the building: it is not a block of them.
        let mut elements = vec![rect(1, 0, 0, 30, 10)];
        assert_eq!(
            merge(&mut elements, vec![building(2, 0, 0, 5), building(3, 10, 0, 5)], true),
            (0, 0, 2)
        );
        assert_eq!(ids(&elements), [1]);
        let mut church = vec![tag(rect(1, 0, 0, 20, 10), "building", "church")];
        merge(&mut church, vec![building(2, 0, 0, 10), building(3, 10, 0, 10)], true);
        assert_eq!(ids(&church), [1]);
        // Overture footprints never split an OSM building.
        let mut overture = vec![rect(1, 0, 0, 20, 10)];
        assert_eq!(
            merge(&mut overture, vec![building(2, 0, 0, 10), building(3, 10, 0, 10)], false),
            (0, 0, 2)
        );
    }

    #[test]
    fn a_house_reaching_into_another_osm_building_does_not_count_for_the_block() {
        // House 3 lies half in the block and half in its neighbour: only house 2 is the
        // block's, and one house is not enough.
        let mut elements = vec![rect(1, 0, 0, 20, 10), rect(9, 20, 0, 10, 10)];
        merge(&mut elements, vec![building(2, 0, 0, 10), building(3, 15, 0, 10)], true);
        assert_eq!(ids(&elements), [1, 9]);
    }

    #[test]
    fn stacked_register_houses_or_a_house_with_its_annex_leave_the_osm_building_alone() {
        let mut elements = vec![rect(1, 0, 0, 20, 10)];
        let houses = vec![building(2, 0, 0, 10), building(3, 0, 0, 10), building(4, 10, 0, 10)];
        assert_eq!(merge(&mut elements, houses, true), (0, 0, 3));
        assert_eq!(ids(&elements), [1]);
        let mut elements = vec![rect(1, 0, 0, 40, 10)];
        let houses = vec![rect(2, 0, 0, 32, 10), rect(3, 32, 0, 4, 10), rect(4, 36, 0, 4, 10)];
        merge(&mut elements, houses, true);
        assert_eq!(ids(&elements), [1]);
    }

    #[test]
    fn a_villa_drawn_with_its_garage_and_shed_is_not_a_block() {
        let mut elements = vec![rect(1, 0, 0, 20, 10)];
        let houses = vec![
            tag(building(2, 0, 0, 10), "building", "detached"),
            tag(rect(3, 10, 0, 5, 10), "building", "garage"),
            tag(rect(4, 15, 0, 5, 10), "building", "shed"),
        ];
        merge(&mut elements, houses, true);
        assert_eq!(ids(&elements), [1]);
        // Nor are two houses and a lean-to under the smallest house size.
        let mut elements = vec![rect(1, 0, 0, 25, 10)];
        let houses = vec![building(2, 0, 0, 10), building(3, 10, 0, 10), rect(4, 20, 0, 4, 5)];
        merge(&mut elements, houses, true);
        assert_eq!(ids(&elements), [1]);
    }

    #[test]
    fn courtyards_stay_empty_unless_a_separate_building_exists() {
        let ProcessedElement::Way(outer) = building(1, 0, 0, 30) else {
            unreachable!()
        };
        let ProcessedElement::Way(inner) = building(2, 5, 5, 20) else {
            unreachable!()
        };
        let relation = ProcessedElement::Relation(ProcessedRelation {
            id: 3,
            tags: outer.tags.clone(),
            members: vec![
                ProcessedMember {
                    role: ProcessedMemberRole::Outer,
                    way: Arc::new(outer),
                },
                ProcessedMember {
                    role: ProcessedMemberRole::Inner,
                    way: Arc::new(inner),
                },
            ],
        });
        let mut elements = vec![relation];
        assert_eq!(
            merge(
                &mut elements,
                vec![building(4, 10, 10, 5), building(5, 0, 0, 4)],
                false
            ),
            (1, 0, 1)
        );
    }
}

#[cfg(test)]
mod integration_tests {
    use super::*;
    use clap::Parser;

    #[test]
    fn python_prepared_registers_reach_the_arnis_parser() {
        let source = std::path::Path::new(env!("CARGO_MANIFEST_DIR"))
            .join("danish_data/fixtures/synthetic.json");
        let args = Args::parse_from([
            "arnis",
            "--bbox",
            "55,11,56,12",
            "--danish-buildings",
            source.to_str().unwrap(),
        ]);
        let mut elements = Vec::new();
        apply(&mut elements, &args, args.bbox.unwrap()).unwrap();
        assert_eq!(elements.len(), 1);
        let tags = elements[0].tags();
        assert_eq!(tags["building:levels"], "2");
        assert_eq!(tags["roof:material"], "roof_tiles");
        assert_eq!(tags["addr:housenumber"], "12A");
        assert_eq!(tags["arnis:bbr_id"], "bbr-1");
        assert!(!tags.contains_key("height"));
        assert!(apply(
            &mut Vec::new(),
            &args,
            LLBBox::new(55.0, 10.0, 56.0, 12.0).unwrap()
        )
        .is_err());
    }

    #[test]
    fn real_hedges_and_scrub_join_alongside_the_buildings() {
        let source = std::path::Path::new(env!("CARGO_MANIFEST_DIR"))
            .join("danish_data/fixtures/synthetic.json");
        let mut supplement: serde_json::Value =
            serde_json::from_str(&std::fs::read_to_string(source).unwrap()).unwrap();
        let elements = supplement["elements"].as_array_mut().unwrap();
        let mut node = |id: u64, lat: f64, lon: f64| {
            elements.push(serde_json::json!({"type": "node", "id": id, "lat": lat, "lon": lon}));
            id
        };
        let a = node(9001, 55.4731, 11.3729);
        let b = node(9002, 55.4731, 11.3731);
        let hedge = serde_json::json!({"type": "way", "id": 9100, "nodes": [a, b],
            "tags": {"barrier": "hedge", "arnis:hedge": "geodanmark"}});
        elements.push(hedge);
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("supplement.json");
        std::fs::write(&path, supplement.to_string()).unwrap();
        let args = Args::parse_from([
            "arnis",
            "--bbox",
            "55,11,56,12",
            "--danish-buildings",
            path.to_str().unwrap(),
        ]);
        let mut elements = Vec::new();
        apply(&mut elements, &args, args.bbox.unwrap()).unwrap();
        assert_eq!(elements.len(), 2, "the building and the hedge");
        assert!(elements
            .iter()
            .any(|e| e.tags().get("barrier").is_some_and(|v| v == "hedge")));
    }

    #[test]
    fn slagelse_defaults_to_real_terrain_from_mapterhorn() {
        use crate::elevation::selector::{select_provider, SourceMode};
        let args = Args::parse_from(["arnis", "--bbox", "55.398,11.349,55.410,11.369"]);
        assert!(matches!(args.mode, crate::args::GenerationMode::GeoTerrain));
        assert!(!args.aws_only_elevation);
        assert_eq!(
            select_provider(&args.bbox.unwrap(), SourceMode::Auto).name(),
            "mapterhorn"
        );
    }
}
