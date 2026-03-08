use crate::clipping::clip_water_ring_to_bbox;
use crate::coordinate_system::{
    cartesian::{XZBBox, XZPoint},
    geographic::{LLBBox, LLPoint},
    transformation::CoordTransformer,
};
use crate::element_processing::water_areas::fill_water_polygons;
use crate::osm_parser::ProcessedNode;
use crate::world_editor::WorldEditor;
use shapefile::{Point, PolygonRing, Shape, ShapeReader};
use std::collections::HashMap;
use std::path::{Path, PathBuf};

pub fn generate_oceans_from_land_polygons(
    editor: &mut WorldEditor,
    dataset_path: &Path,
    llbbox: &LLBBox,
    xzbbox: &XZBBox,
    scale: f64,
) -> Result<bool, String> {
    let polygons = load_external_water_polygons(dataset_path, llbbox, xzbbox, scale)?;

    if polygons.outers.is_empty() {
        return Ok(false);
    }

    println!(
        "External coastline dataset: {} outer rings, {} inner rings",
        polygons.outers.len(),
        polygons.inners.len()
    );
    fill_water_polygons(editor, &polygons.outers, &polygons.inners);
    Ok(true)
}

#[derive(Default)]
struct ExternalWaterPolygons {
    outers: Vec<Vec<XZPoint>>,
    inners: Vec<Vec<XZPoint>>,
}

fn load_external_water_polygons(
    dataset_path: &Path,
    llbbox: &LLBBox,
    xzbbox: &XZBBox,
    scale: f64,
) -> Result<ExternalWaterPolygons, String> {
    let shapefile_path = resolve_shapefile_path(dataset_path)?;
    let dataset_kind = detect_dataset_kind(&shapefile_path);
    let (transformer, _) = CoordTransformer::llbbox_to_xzbbox(llbbox, scale)?;
    let mut reader = ShapeReader::from_path(&shapefile_path)
        .map_err(|e| format!("Failed to open coastline polygon shapefile: {e}"))?;

    let mut polygons = ExternalWaterPolygons::default();
    let mut land_rings: Vec<Vec<XZPoint>> = Vec::new();
    let mut record_id: u64 = 2_000_000_000;

    for shape_result in reader.iter_shapes() {
        let shape =
            shape_result.map_err(|e| format!("Failed reading coastline polygon shape: {e}"))?;
        let Shape::Polygon(polygon) = shape else {
            continue;
        };

        for ring in polygon.rings() {
            let points = ring.points();
            if !ring_intersects_bbox(points, llbbox) {
                continue;
            }

            let mut nodes = Vec::with_capacity(points.len() + 1);
            for (idx, point) in points.iter().enumerate() {
                let llpoint = match LLPoint::new(point.y, point.x) {
                    Ok(llpoint) => llpoint,
                    Err(_) => continue,
                };
                let xz = transformer.transform_point(llpoint);
                nodes.push(ProcessedNode {
                    id: record_id + idx as u64,
                    tags: HashMap::new(),
                    x: xz.x,
                    z: xz.z,
                });
            }

            if nodes.len() < 3 {
                record_id = record_id.wrapping_add(10_000);
                continue;
            }

            close_ring(&mut nodes);

            if let Some(clipped_ring) = clip_water_ring_to_bbox(&nodes, xzbbox) {
                let ring_points: Vec<XZPoint> =
                    clipped_ring.iter().map(ProcessedNode::xz).collect();
                if ring_points.len() >= 4 {
                    match dataset_kind {
                        DatasetKind::Water => match ring {
                            PolygonRing::Outer(_) => polygons.outers.push(ring_points),
                            PolygonRing::Inner(_) => {
                                if !ring_touches_bbox(&ring_points, xzbbox) {
                                    polygons.inners.push(ring_points);
                                }
                            }
                        },
                        DatasetKind::Land => {
                            if matches!(ring, PolygonRing::Outer(_)) {
                                land_rings.push(ring_points);
                            }
                        }
                    }
                }
            }

            record_id = record_id.wrapping_add(10_000);
        }
    }

    if matches!(dataset_kind, DatasetKind::Land) {
        if land_rings.is_empty() {
            return Ok(ExternalWaterPolygons::default());
        }
        polygons.outers.push(bbox_ring(xzbbox));
        polygons.inners = land_rings;
    }

    Ok(polygons)
}

#[derive(Copy, Clone)]
enum DatasetKind {
    Water,
    Land,
}

fn detect_dataset_kind(path: &Path) -> DatasetKind {
    let stem = path
        .file_stem()
        .and_then(|stem| stem.to_str())
        .unwrap_or_default()
        .to_ascii_lowercase();

    if stem.contains("water_polygons") || stem.contains("water-polygons") {
        DatasetKind::Water
    } else {
        DatasetKind::Land
    }
}

fn resolve_shapefile_path(path: &Path) -> Result<PathBuf, String> {
    if path.is_file() {
        if path
            .extension()
            .and_then(|ext| ext.to_str())
            .is_some_and(|ext| ext.eq_ignore_ascii_case("shp"))
        {
            return Ok(path.to_path_buf());
        }
        return Err(format!(
            "Unsupported coastline polygon file '{}'. Provide an extracted .shp file.",
            path.display()
        ));
    }

    if path.is_dir() {
        let mut candidates: Vec<PathBuf> = std::fs::read_dir(path)
            .map_err(|e| {
                format!(
                    "Failed to read coastline polygon directory '{}': {e}",
                    path.display()
                )
            })?
            .filter_map(|entry| entry.ok().map(|e| e.path()))
            .filter(|candidate| {
                candidate
                    .extension()
                    .and_then(|ext| ext.to_str())
                    .is_some_and(|ext| ext.eq_ignore_ascii_case("shp"))
            })
            .collect();
        candidates.sort_by_key(|candidate| {
            let stem = candidate
                .file_stem()
                .and_then(|stem| stem.to_str())
                .unwrap_or_default()
                .to_ascii_lowercase();
            if stem.contains("water_polygons") || stem.contains("water-polygons") {
                0
            } else if stem.contains("land_polygons") || stem.contains("land-polygons") {
                1
            } else {
                2
            }
        });
        if let Some(candidate) = candidates.into_iter().next() {
            return Ok(candidate);
        }
        return Err(format!(
            "No .shp file found in coastline polygon directory '{}'.",
            path.display()
        ));
    }

    Err(format!(
        "Coastline polygon path '{}' does not exist.",
        path.display()
    ))
}

fn ring_intersects_bbox(points: &[Point], llbbox: &LLBBox) -> bool {
    if points.is_empty() {
        return false;
    }

    let mut min_lat = f64::MAX;
    let mut min_lng = f64::MAX;
    let mut max_lat = f64::MIN;
    let mut max_lng = f64::MIN;

    for point in points {
        min_lat = min_lat.min(point.y);
        min_lng = min_lng.min(point.x);
        max_lat = max_lat.max(point.y);
        max_lng = max_lng.max(point.x);
    }

    !(max_lat < llbbox.min().lat()
        || min_lat > llbbox.max().lat()
        || max_lng < llbbox.min().lng()
        || min_lng > llbbox.max().lng())
}

fn close_ring(nodes: &mut Vec<ProcessedNode>) {
    if nodes.is_empty() {
        return;
    }

    let first = nodes[0].clone();
    let needs_close = nodes
        .last()
        .map(|last| last.x != first.x || last.z != first.z)
        .unwrap_or(false);

    if needs_close {
        let mut closing = first.clone();
        closing.id = first.id;
        nodes.push(closing);
    } else if let Some(last) = nodes.last_mut() {
        last.id = first.id;
    }
}

fn ring_touches_bbox(ring: &[XZPoint], xzbbox: &XZBBox) -> bool {
    ring.iter().any(|point| {
        point.x <= xzbbox.min_x()
            || point.x >= xzbbox.max_x()
            || point.z <= xzbbox.min_z()
            || point.z >= xzbbox.max_z()
    })
}

fn bbox_ring(xzbbox: &XZBBox) -> Vec<XZPoint> {
    vec![
        XZPoint::new(xzbbox.min_x(), xzbbox.min_z()),
        XZPoint::new(xzbbox.max_x(), xzbbox.min_z()),
        XZPoint::new(xzbbox.max_x(), xzbbox.max_z()),
        XZPoint::new(xzbbox.min_x(), xzbbox.max_z()),
        XZPoint::new(xzbbox.min_x(), xzbbox.min_z()),
    ]
}

#[cfg(test)]
mod dataset_tests {
    use super::*;

    #[test]
    fn local_copenhagen_water_dataset_produces_rings() {
        let dataset = Path::new("data/land-polygons/water_polygons.shp");
        if !dataset.exists() {
            return;
        }

        let llbbox = LLBBox::new(55.67, 12.56, 55.695, 12.62).unwrap();
        let (_, xzbbox) = CoordTransformer::llbbox_to_xzbbox(&llbbox, 1.0).unwrap();
        let polygons = load_external_water_polygons(dataset, &llbbox, &xzbbox, 1.0).unwrap();

        println!(
            "local dataset debug: {} outers, {} inners",
            polygons.outers.len(),
            polygons.inners.len()
        );
        assert!(!polygons.outers.is_empty());
    }

    #[test]
    fn local_copenhagen_water_dataset_places_water_at_known_sea_point() {
        let dataset = Path::new("data/land-polygons/water_polygons.shp");
        if !dataset.exists() {
            return;
        }

        let llbbox = LLBBox::new(55.67, 12.56, 55.695, 12.62).unwrap();
        let (transformer, xzbbox) = CoordTransformer::llbbox_to_xzbbox(&llbbox, 1.0).unwrap();
        let tempdir = tempfile::tempdir().unwrap();
        let mut editor = WorldEditor::new(tempdir.path().to_path_buf(), &xzbbox, llbbox);

        generate_oceans_from_land_polygons(&mut editor, dataset, &llbbox, &xzbbox, 1.0).unwrap();

        for (lat, lng) in [
            (55.682, 12.615),
            (55.682, 12.619),
            (55.689, 12.619),
            (55.674, 12.617),
            (55.691, 12.612),
        ] {
            let sample = transformer.transform_point(LLPoint::new(lat, lng).unwrap());
            assert!(editor.check_for_block_absolute(
                sample.x,
                0,
                sample.z,
                Some(&[crate::block_definitions::WATER]),
                None,
            ));
        }
    }
}
