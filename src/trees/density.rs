//! Moderate thinning in urban green spaces; explicit woods always keep their density.
use crate::osm_parser::{ProcessedElement, ProcessedMemberRole, ProcessedWay};
use geo::{BoundingRect, Intersects, LineString, Point, Polygon, Rect};

fn relation_rings(
    r: &crate::osm_parser::ProcessedRelation,
    role: ProcessedMemberRole,
) -> Vec<LineString<f64>> {
    let mut parts: Vec<_> = r
        .members
        .iter()
        .filter(|m| m.role == role)
        .map(|m| m.way.nodes.clone())
        .collect();
    crate::element_processing::merge_way_segments(&mut parts);
    parts
        .into_iter()
        .filter_map(|nodes| {
            ring(&ProcessedWay {
                id: r.id,
                tags: Default::default(),
                nodes,
            })
        })
        .collect()
}

struct Area {
    bounds: Rect<f64>,
    polygon: Polygon<f64>,
}

impl Area {
    fn contains(&self, p: &Point<f64>) -> bool {
        self.bounds.intersects(p) && self.polygon.intersects(p)
    }
}

#[derive(Default)]
pub struct TreeDensityAreas {
    urban: Vec<Area>,
    forest: Vec<Area>,
}

fn ring(w: &ProcessedWay) -> Option<LineString<f64>> {
    let a = w.nodes.first()?;
    let b = w.nodes.last()?;
    (w.nodes.len() >= 4 && (a.x, a.z) == (b.x, b.z)).then(|| {
        LineString::from(
            w.nodes
                .iter()
                .map(|n| (n.x as f64, n.z as f64))
                .collect::<Vec<_>>(),
        )
    })
}

impl TreeDensityAreas {
    pub fn collect(elements: &[ProcessedElement]) -> Self {
        let mut result = Self::default();
        for e in elements {
            let tags = e.tags();
            let forest = tags.get("landuse").map(String::as_str) == Some("forest")
                || tags.get("natural").map(String::as_str) == Some("wood");
            let urban = matches!(
                tags.get("landuse").map(String::as_str),
                Some("residential" | "commercial" | "retail" | "industrial")
            );
            if !forest && !urban {
                continue;
            }
            let polygons = match e {
                ProcessedElement::Way(w) => ring(w)
                    .map(|r| vec![Polygon::new(r, vec![])])
                    .unwrap_or_default(),
                ProcessedElement::Relation(r) => {
                    let holes = relation_rings(r, ProcessedMemberRole::Inner);
                    relation_rings(r, ProcessedMemberRole::Outer)
                        .into_iter()
                        .map(|outer| {
                            let p = Polygon::new(outer, vec![]);
                            let inner = holes
                                .iter()
                                .filter(|h| h.0.first().is_some_and(|c| p.intersects(&Point(*c))))
                                .cloned()
                                .collect();
                            Polygon::new(p.exterior().clone(), inner)
                        })
                        .collect()
                }
                _ => vec![],
            };
            let destination = if forest {
                &mut result.forest
            } else {
                &mut result.urban
            };
            for polygon in polygons {
                if let Some(bounds) = polygon.bounding_rect() {
                    destination.push(Area { bounds, polygon });
                }
            }
        }
        result
    }

    pub fn is_forest(&self, x: i32, z: i32) -> bool {
        let p = Point::new(x as f64, z as f64);
        self.forest.iter().any(|a| a.contains(&p))
    }

    pub fn is_urban(&self, x: i32, z: i32) -> bool {
        let p = Point::new(x as f64, z as f64);
        self.urban.iter().any(|a| a.contains(&p))
    }
}

/// Independent of shape/species RNG, identical across passes and tile boundaries.
pub fn keep_urban_tree(x: i32, z: i32) -> bool {
    crate::land_cover::coord_hash(x ^ 0x5542, z ^ 0x5452) % 100 < 70
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::osm_parser::{ProcessedMember, ProcessedNode, ProcessedRelation};
    use std::collections::HashMap;
    use std::sync::Arc;

    fn square(id: u64, min: i32, max: i32, key: &str, value: &str) -> ProcessedWay {
        ProcessedWay {
            id,
            tags: HashMap::from([(key.into(), value.into())]),
            nodes: [(min, min), (max, min), (max, max), (min, max), (min, min)]
                .into_iter()
                .map(|(x, z)| ProcessedNode {
                    id: 0,
                    x,
                    z,
                    tags: HashMap::new(),
                })
                .collect(),
        }
    }

    #[test]
    fn urban_areas_and_nested_woods_remain_distinct_including_relation_holes() {
        let urban = ProcessedElement::Way(square(1, 0, 100, "landuse", "residential"));
        let forest = ProcessedElement::Relation(ProcessedRelation {
            id: 2,
            tags: HashMap::from([("natural".into(), "wood".into())]),
            members: vec![
                ProcessedMember {
                    role: ProcessedMemberRole::Outer,
                    way: Arc::new(square(3, 20, 80, "", "")),
                },
                ProcessedMember {
                    role: ProcessedMemberRole::Inner,
                    way: Arc::new(square(4, 40, 60, "", "")),
                },
            ],
        });
        let areas = TreeDensityAreas::collect(&[urban, forest]);
        assert!(areas.is_urban(30, 30) && areas.is_forest(30, 30));
        assert!(areas.is_urban(50, 50) && !areas.is_forest(50, 50));
        assert!(!areas.is_urban(110, 110));
        let woods = TreeDensityAreas::collect(&[ProcessedElement::Way(square(
            5, 0, 50, "landuse", "forest",
        ))]);
        assert!(woods.is_forest(25, 25) && !woods.is_urban(25, 25));
    }

    #[test]
    fn urban_keep_rate_is_moderate_and_repeatable() {
        let count = (-100..100)
            .flat_map(|x| (-100..100).map(move |z| (x, z)))
            .filter(|&(x, z)| {
                let a = keep_urban_tree(x, z);
                assert_eq!(a, keep_urban_tree(x, z));
                a
            })
            .count();
        assert!((27000..29000).contains(&count), "{count}");
    }

    #[test]
    fn land_cover_fallback_agrees_across_tiles_and_does_not_thin_rural_tree_cover() {
        use crate::coordinate_system::{cartesian::XZBBox, geographic::LLBBox};
        use crate::land_cover::{LandCoverData, LC_BUILT_UP, LC_TREE_COVER};
        use crate::world_editor::WorldEditor;
        let bounds = XZBBox::rect_from_min_max(0, 0, 100, 100).unwrap();
        let tile_bounds = XZBBox::rect_from_min_max(10, 10, 70, 70).unwrap();
        let ll = LLBBox::new(55.0, 11.0, 55.001, 11.002).unwrap();
        for class in [LC_BUILT_UP, LC_TREE_COVER] {
            let ground = Arc::new(crate::ground::Ground::new_flat_land_cover_test(
                LandCoverData {
                    grid: vec![vec![class; 2]; 2],
                    width: 2,
                    height: 2,
                    cells_per_meter: 1.0,
                    water_distance: vec![vec![0; 2]; 2],
                    water_blend_cache: Default::default(),
                },
                101,
                101,
            ));
            let mut main = WorldEditor::new(std::env::temp_dir(), &bounds, ll);
            let mut tile = WorldEditor::new(std::env::temp_dir(), &tile_bounds, ll);
            main.set_ground(Arc::clone(&ground));
            tile.set_ground(ground);
            tile.set_ground_origin(0, 0);
            for x in 15..65 {
                for z in 15..65 {
                    let wanted = class != LC_BUILT_UP || keep_urban_tree(x, z);
                    assert_eq!(main.urban_tree_density_allows(x, z), wanted);
                    assert_eq!(tile.urban_tree_density_allows(x, z), wanted);
                }
            }
        }
    }

    #[test]
    fn editor_thins_urban_grass_but_never_a_wood_inside_the_town() {
        use crate::coordinate_system::{cartesian::XZBBox, geographic::LLBBox};
        use crate::world_editor::WorldEditor;
        let bounds = XZBBox::rect_from_min_max(0, 0, 100, 100).unwrap();
        let ll = LLBBox::new(55.0, 11.0, 55.001, 11.002).unwrap();
        let mut editor = WorldEditor::new(std::env::temp_dir(), &bounds, ll);
        let areas = TreeDensityAreas::collect(&[
            ProcessedElement::Way(square(1, 0, 100, "landuse", "residential")),
            ProcessedElement::Way(square(2, 20, 80, "landuse", "forest")),
        ]);
        editor.set_tree_density(Arc::new(areas));
        let mut rejected = 0;
        for x in 1..100 {
            for z in 1..100 {
                if (20..=80).contains(&x) && (20..=80).contains(&z) {
                    assert!(editor.urban_tree_density_allows(x, z), "wood at {x},{z}");
                } else {
                    assert_eq!(
                        editor.urban_tree_density_allows(x, z),
                        keep_urban_tree(x, z)
                    );
                    rejected += usize::from(!editor.urban_tree_density_allows(x, z));
                }
            }
        }
        assert!(rejected > 1000);
        assert!(editor.urban_tree_density_allows(110, 110));
    }
}
