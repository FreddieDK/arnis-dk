//! Turn qualified DAR access points into ordinary upstream entrance nodes.
//! A TD point is near a door, TK only hints at a facade; neither is an exact survey.
use super::addresses::{AddressHint, HINTS};
use super::footprint;
use crate::args::Args;
use crate::bresenham::bresenham_line;
use crate::coordinate_system::geographic::{LLBBox, LLPoint};
use crate::osm_parser::{ProcessedElement, ProcessedMemberRole, ProcessedNode, ProcessedWay};
use geo::{Contains, MultiPolygon, Point};
use std::collections::{HashMap, HashSet};
use std::sync::Arc;

pub(super) fn ways(element: &ProcessedElement) -> Vec<&ProcessedWay> {
    match element {
        ProcessedElement::Way(w) => vec![w],
        ProcessedElement::Relation(r) => r
            .members
            .iter()
            .filter(|m| {
                matches!(
                    m.role,
                    ProcessedMemberRole::Outer | ProcessedMemberRole::Inner
                )
            })
            .map(|m| m.way.as_ref())
            .collect(),
        _ => vec![],
    }
}

#[derive(Clone, Copy, Debug)]
struct Candidate {
    way: u64,
    segment: usize,
    x: i32,
    z: i32,
    distance: f64,
    nx: f64,
    nz: f64,
}

fn choose(
    element: &ProcessedElement,
    shape: &MultiPolygon<f64>,
    point: (i32, i32),
    scale: f64,
) -> Option<Candidate> {
    let outlines = ways(element);
    // Real OSM entrance/door annotations always win, including explicit "no".
    if outlines.iter().any(|w| {
        w.nodes
            .iter()
            .any(|n| n.tags.contains_key("entrance") || n.tags.contains_key("door"))
    }) {
        return None;
    }
    if !shape.contains(&Point::new(point.0 as f64, point.1 as f64)) {
        return None;
    }
    let mut candidates = vec![];
    let mut corner_distance = f64::INFINITY;
    for way in outlines {
        for (segment, pair) in way.nodes.windows(2).enumerate() {
            let (a, b) = (&pair[0], &pair[1]);
            let dx = (b.x - a.x) as f64;
            let dz = (b.z - a.z) as f64;
            let len = dx.hypot(dz);
            if len < 4.0 {
                continue;
            }
            // Project before rasterizing; do not scan long, far-away walls.
            let t = (((point.0 - a.x) as f64 * dx + (point.1 - a.z) as f64 * dz) / (len * len))
                .clamp(0.0, 1.0);
            let distance = ((a.x as f64 + t * dx) - point.0 as f64)
                .hypot(a.z as f64 + t * dz - point.1 as f64);
            if distance > 6.0 * scale + 1.0 {
                continue;
            }
            let line = bresenham_line(a.x, 0, a.z, b.x, 0, b.z);
            if line.len() < 5 {
                continue;
            }
            let &(x, _, z) = line.iter().min_by_key(|(x, _, z)| {
                (*x as i64 - point.0 as i64).pow(2) + (*z as i64 - point.1 as i64).pow(2)
            })?;
            // Do not relocate an ambiguous corner point along a different wall.
            if (x - a.x).abs().max((z - a.z).abs()) < 2 || (x - b.x).abs().max((z - b.z).abs()) < 2
            {
                corner_distance = corner_distance.min(distance);
                continue;
            }
            let mut nx = -dz / len;
            let mut nz = dx / len;
            if shape.contains(&Point::new(x as f64 + nx * 1.5, z as f64 + nz * 1.5)) {
                nx = -nx;
                nz = -nz;
            }
            if shape.contains(&Point::new(x as f64 + nx * 1.5, z as f64 + nz * 1.5)) {
                continue;
            }
            candidates.push(Candidate {
                way: way.id,
                segment,
                x,
                z,
                distance,
                nx,
                nz,
            });
        }
    }
    candidates.sort_by(|a, b| {
        a.distance
            .total_cmp(&b.distance)
            .then(a.way.cmp(&b.way))
            .then(a.segment.cmp(&b.segment))
    });
    let best = *candidates.first()?;
    if corner_distance <= best.distance + scale.max(1.0) {
        return None;
    }
    // Similar distances to distinct walls mean the register point is inconclusive.
    if candidates.iter().skip(1).any(|c| {
        c.distance - best.distance < scale.max(1.0)
            && (c.x - best.x).abs().max((c.z - best.z).abs()) > 2
    }) {
        return None;
    }
    Some(best)
}

fn insert(element: &mut ProcessedElement, c: Candidate, id: u64) {
    let way = match element {
        ProcessedElement::Way(w) => w,
        ProcessedElement::Relation(r) => {
            let member = r.members.iter_mut().find(|m| m.way.id == c.way).unwrap();
            Arc::make_mut(&mut member.way)
        }
        _ => return,
    };
    way.nodes.insert(
        c.segment + 1,
        ProcessedNode {
            id,
            x: c.x,
            z: c.z,
            tags: HashMap::from([
                ("entrance".into(), "yes".into()),
                ("source:entrance".into(), "DAR".into()),
            ]),
        },
    );
}

fn address_node(element: &mut ProcessedElement, id: u64, address: &str) {
    if address.trim().is_empty() {
        return;
    }
    let set = |way: &mut ProcessedWay| {
        if let Some(node) = way.nodes.iter_mut().find(|n| n.id == id) {
            node.tags.insert("arnis:address".into(), address.into());
        }
    };
    match element {
        ProcessedElement::Way(w) => set(w),
        ProcessedElement::Relation(r) => {
            for member in &mut r.members {
                set(Arc::make_mut(&mut member.way));
            }
        }
        _ => {}
    }
}

pub(super) fn apply(
    elements: &mut [ProcessedElement],
    args: &Args,
    bbox: LLBBox,
) -> Result<(), String> {
    let (transform, bounds) =
        crate::projection::ProjectionSpec::from_args(args).transformer(&bbox)?;
    let shapes: Vec<_> = elements.iter().map(footprint).collect();
    let mut used: HashSet<_> = elements
        .iter()
        .flat_map(|e| {
            ways(e)
                .into_iter()
                .flat_map(|w| w.nodes.iter().map(|n| n.id))
        })
        .collect();
    used.extend(elements.iter().map(|e| e.id()));
    let mut next_id = u64::MAX;
    let (mut td, mut tk) = (0, 0);
    for i in 0..elements.len() {
        let tags = elements[i].tags();
        if tags.contains_key("building:part") {
            continue;
        }
        let hints: Vec<AddressHint> = tags
            .get(HINTS)
            .and_then(|s| serde_json::from_str(s).ok())
            .unwrap_or_else(|| AddressHint::from_source(tags));
        let mut planned = vec![];
        for hint in hints {
            let (lat, lon) = (hint.lat, hint.lon);
            if !lat.is_finite()
                || !lon.is_finite()
                || !matches!(hint.standard.as_str(), "TD" | "TK")
            {
                continue;
            }
            if lat < bbox.min().lat()
                || lat > bbox.max().lat()
                || lon < bbox.min().lng()
                || lon > bbox.max().lng()
            {
                continue;
            }
            let p = transform.transform_point(LLPoint::new(lat, lon)?);
            let Some(shape) = &shapes[i] else {
                continue;
            };
            let Some(c) = choose(&elements[i], shape, (p.x, p.z), args.scale) else {
                continue;
            };
            if !hint.permits_wall(c.x, c.z, args.scale) {
                continue;
            }
            // An edge introduced by clipping is not a real building facade.
            if c.x <= bounds.min_x() + 1
                || c.x >= bounds.max_x() - 1
                || c.z <= bounds.min_z() + 1
                || c.z >= bounds.max_z() - 1
            {
                continue;
            }
            // Reject party walls and doorsteps entering any other known building.
            let blocked = shapes.iter().enumerate().any(|(j, s)| {
                j != i
                    && s.as_ref().is_some_and(|s| {
                        [0.5, 1.5, 2.5].iter().any(|d| {
                            s.contains(&Point::new(c.x as f64 + c.nx * d, c.z as f64 + c.nz * d))
                        })
                    })
            });
            if blocked {
                continue;
            }
            planned.push((c, hint));
        }
        // Different addresses that collapse onto one doorway are not a safe match.
        let conflicted: Vec<_> = planned
            .iter()
            .enumerate()
            .map(|(a, (c, _))| {
                planned.iter().enumerate().any(|(b, (other, _))| {
                    a != b && (c.x - other.x).abs().max((c.z - other.z).abs()) < 3
                })
            })
            .collect();
        let mut planned: Vec<_> = planned
            .into_iter()
            .zip(conflicted)
            .filter_map(|(p, conflict)| (!conflict).then_some(p))
            .collect();
        // Insert from the end of each original segment, preserving the ring order.
        planned.sort_by_key(|(c, _)| {
            let way = ways(&elements[i])
                .into_iter()
                .find(|w| w.id == c.way)
                .unwrap();
            let a = &way.nodes[c.segment];
            std::cmp::Reverse((
                c.way,
                c.segment,
                (c.x as i64 - a.x as i64).pow(2) + (c.z as i64 - a.z as i64).pow(2),
            ))
        });
        for (c, hint) in planned {
            let standard = hint.standard;
            while used.contains(&next_id) {
                next_id -= 1;
            }
            insert(&mut elements[i], c, next_id);
            address_node(&mut elements[i], next_id, &hint.address);
            used.insert(next_id);
            if standard == "TD" {
                td += 1;
            } else {
                tk += 1;
            }
            if args.debug {
                println!(
                    "DAR entrance: {} {} {standard} x={} z={}",
                    elements[i].kind(),
                    elements[i].id(),
                    c.x,
                    c.z
                );
            }
        }
    }
    println!("Danish entrances: {td} door hints (TD), {tk} facade hints (TK) placed; existing OSM entrances preserved");
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    fn rectangle() -> ProcessedElement {
        ProcessedElement::Way(ProcessedWay {
            id: 1,
            tags: HashMap::from([("building".into(), "house".into())]),
            nodes: [(0, 0), (20, 0), (20, 20), (0, 20), (0, 0)]
                .into_iter()
                .enumerate()
                .map(|(i, (x, z))| ProcessedNode {
                    id: i as u64,
                    tags: HashMap::new(),
                    x,
                    z,
                })
                .collect(),
        })
    }
    #[test]
    fn qualified_point_chooses_near_wall_and_inserts_mapped_node() {
        let mut e = rectangle();
        let shape = footprint(&e).unwrap();
        let c = choose(&e, &shape, (7, 3), 1.0).unwrap();
        assert_eq!((c.x, c.z), (7, 0));
        insert(&mut e, c, 100);
        assert!(crate::element_processing::buildings::outline_has_mapped_entrance(ways(&e)[0]));
        assert!(choose(&e, &shape, (7, 17), 1.0).is_none());
    }
    #[test]
    fn central_outside_and_ambiguous_corner_points_do_not_guess_a_door() {
        let e = rectangle();
        let shape = footprint(&e).unwrap();
        for p in [(10, 10), (-1, 8), (3, 3)] {
            assert!(choose(&e, &shape, p, 1.0).is_none(), "{p:?}");
        }
    }

    fn hinted_rectangle() -> (ProcessedElement, Args) {
        use clap::Parser;
        let args = Args::parse_from(["arnis", "--bbox", "55,11,55.001,11.002"]);
        let bbox = args.bbox.unwrap();
        let (transform, _) = crate::projection::ProjectionSpec::from_args(&args)
            .transformer(&bbox)
            .unwrap();
        let mut e = rectangle();
        let ProcessedElement::Way(w) = &mut e else {
            unreachable!()
        };
        for n in &mut w.nodes {
            n.x += 20;
            n.z += 20;
        }
        w.tags.extend(HashMap::from([
            ("arnis:entrance:standard".into(), "TD".into()),
            (
                "arnis:entrance:lat".into(),
                (55.001 - 23.25 / transform.scale_factor_z() * 0.001).to_string(),
            ),
            (
                "arnis:entrance:lon".into(),
                (11.0 + 27.25 / transform.scale_factor_x() * 0.002).to_string(),
            ),
        ]));
        (e, args)
    }
    #[test]
    fn end_to_end_hint_projection_and_party_wall_rejection() {
        let (e, args) = hinted_rectangle();
        let mut elements = vec![e.clone()];
        apply(&mut elements, &args, args.bbox.unwrap()).unwrap();
        let n = ways(&elements[0])[0]
            .nodes
            .iter()
            .find(|n| n.tags.contains_key("entrance"))
            .unwrap();
        assert_eq!((n.x, n.z), (27, 20));
        let mut neighbour = rectangle();
        let ProcessedElement::Way(w) = &mut neighbour else {
            unreachable!()
        };
        w.id = 2;
        for n in &mut w.nodes {
            n.x += 20;
        }
        let mut blocked = vec![e, neighbour];
        apply(&mut blocked, &args, args.bbox.unwrap()).unwrap();
        assert!(!ways(&blocked[0])[0]
            .nodes
            .iter()
            .any(|n| n.tags.contains_key("entrance")));
    }
    #[test]
    fn separate_addresses_keep_their_own_doors_and_ring_order() {
        let (mut e, args) = hinted_rectangle();
        let (transform, _) = crate::projection::ProjectionSpec::from_args(&args)
            .transformer(&args.bbox.unwrap())
            .unwrap();
        let mut a = AddressHint::from_tags(e.tags()).unwrap();
        a.address = "Testvej 1, 4200 Slagelse".into();
        let mut b = a.clone();
        b.address = "Testvej 3, 4200 Slagelse".into();
        b.lon += 6.0 / transform.scale_factor_x() * 0.002;
        if let ProcessedElement::Way(w) = &mut e {
            w.tags.insert(
                HINTS.into(),
                serde_json::to_string(&vec![b.clone(), a.clone()]).unwrap(),
            );
        }
        let before = footprint(&e).unwrap();
        let mut elements = vec![e.clone()];
        apply(&mut elements, &args, args.bbox.unwrap()).unwrap();
        let doors: Vec<_> = ways(&elements[0])[0]
            .nodes
            .iter()
            .filter(|n| n.tags.contains_key("arnis:address"))
            .collect();
        assert_eq!(doors.len(), 2);
        assert_eq!(
            (
                doors[0].x,
                doors[0].z,
                doors[0].tags["arnis:address"].as_str()
            ),
            (27, 20, a.address.as_str())
        );
        assert_eq!(
            (
                doors[1].x,
                doors[1].z,
                doors[1].tags["arnis:address"].as_str()
            ),
            (33, 20, b.address.as_str())
        );
        use geo::Area;
        assert_eq!(
            before.unsigned_area(),
            footprint(&elements[0]).unwrap().unsigned_area()
        );
        assert_ne!(doors[0].id, doors[1].id);
        // Competing labels at effectively the same position must not be guessed.
        b.lon = a.lon;
        if let ProcessedElement::Way(w) = &mut e {
            w.tags
                .insert(HINTS.into(), serde_json::to_string(&vec![a, b]).unwrap());
        }
        let mut conflict = vec![e.clone()];
        apply(&mut conflict, &args, args.bbox.unwrap()).unwrap();
        assert!(!ways(&conflict[0])[0]
            .nodes
            .iter()
            .any(|n| n.tags.contains_key("entrance")));
        if let ProcessedElement::Way(w) = &mut e {
            w.nodes[1].tags.insert("entrance".into(), "yes".into());
        }
        let mut osm = vec![e];
        apply(&mut osm, &args, args.bbox.unwrap()).unwrap();
        assert_eq!(
            ways(&osm[0])[0].nodes.len(),
            5,
            "existing OSM entrances remain authoritative"
        );
    }

    #[test]
    fn source_address_list_places_each_door_on_added_and_matched_buildings() {
        let (mut source, args) = hinted_rectangle();
        let (transform, _) = crate::projection::ProjectionSpec::from_args(&args)
            .transformer(&args.bbox.unwrap())
            .unwrap();
        let mut a = AddressHint::from_tags(source.tags()).unwrap();
        a.address = "Testvej 14A, 4200 Slagelse".into();
        let mut b = a.clone();
        b.address = "Testvej 16, 4200 Slagelse".into();
        b.lon += 6.0 / transform.scale_factor_x() * 0.002;
        if let ProcessedElement::Way(w) = &mut source {
            w.tags.insert(
                "arnis:entrances".into(),
                serde_json::to_string(&vec![a.clone(), b.clone(), a]).unwrap(),
            );
        }
        for matched in [false, true] {
            let mut elements = vec![];
            if matched {
                let mut existing = source.clone();
                if let ProcessedElement::Way(w) = &mut existing {
                    w.id = 2;
                    w.tags = HashMap::from([("building".into(), "house".into())]);
                }
                elements.push(existing);
            }
            super::super::merge(&mut elements, vec![source.clone()], true);
            apply(&mut elements, &args, args.bbox.unwrap()).unwrap();
            let doors: Vec<_> = ways(&elements[0])[0]
                .nodes
                .iter()
                .filter(|n| n.tags.contains_key("arnis:address"))
                .collect();
            assert_eq!(doors.len(), 2, "matched={matched}");
            assert_eq!((doors[1].x, doors[1].z), (33, 20));
            assert_eq!(doors[1].tags["arnis:address"], b.address);
        }
    }

    #[test]
    fn multipolygon_keeps_its_geometry_and_gets_one_outline_entrance() {
        use crate::osm_parser::{ProcessedMember, ProcessedRelation};
        let (e, args) = hinted_rectangle();
        let ProcessedElement::Way(w) = e else {
            unreachable!()
        };
        let tags = w.tags.clone();
        let mut elements = vec![ProcessedElement::Relation(ProcessedRelation {
            id: 10,
            tags,
            members: vec![ProcessedMember {
                role: ProcessedMemberRole::Outer,
                way: Arc::new(w),
            }],
        })];
        let before = footprint(&elements[0]).unwrap();
        apply(&mut elements, &args, args.bbox.unwrap()).unwrap();
        assert_eq!(
            ways(&elements[0])[0]
                .nodes
                .iter()
                .filter(|n| n.tags.contains_key("entrance"))
                .count(),
            1
        );
        use geo::Area;
        assert_eq!(
            before.unsigned_area(),
            footprint(&elements[0]).unwrap().unsigned_area()
        );
    }
}
