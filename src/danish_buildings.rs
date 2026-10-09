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
}

/// Adds only footprints without positive-area overlaps. Attribute enrichment is
/// limited to unambiguous one-to-one matches with IoU >= 0.6; OSM values win.
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
    for (element, matched) in incoming.into_iter().zip(matches) {
        let Some((shape, overlaps)) = matched else {
            continue;
        };
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
    let (added, filled, skipped) = merge(elements, incoming, true);
    println!("Danish buildings: {added} added, {filled} enriched, {skipped} overlapping footprints omitted");
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
        ProcessedElement::Way(ProcessedWay {
            id,
            tags: HashMap::from([("building".into(), "yes".into())]),
            nodes: [
                (x, z),
                (x + size, z),
                (x + size, z + size),
                (x, z + size),
                (x, z),
            ]
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
