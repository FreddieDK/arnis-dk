use crate::clipping::clip_way_to_bbox;
use crate::coordinate_system::cartesian::{XZBBox, XZPoint};
use crate::element_processing::water_areas::fill_water_polygons;
use crate::floodfill_cache::CoordinateBitmap;
use crate::osm_parser::{ProcessedNode, ProcessedWay};
use crate::urban_ground::UrbanGroundLookup;
use crate::world_editor::WorldEditor;

const BOUNDARY_TOLERANCE: i32 = 1;

#[derive(Debug, Default)]
struct OceanPolygons {
    outers: Vec<Vec<XZPoint>>,
    inners: Vec<Vec<XZPoint>>,
}

pub fn generate_oceans(
    editor: &mut WorldEditor,
    coastlines: &[ProcessedWay],
    xzbbox: &XZBBox,
    dry_land_mask: &CoordinateBitmap,
    road_mask: &CoordinateBitmap,
    building_footprints: &CoordinateBitmap,
    explicit_water_mask: &CoordinateBitmap,
    urban_lookup: &UrbanGroundLookup,
) {
    let polygons = build_ocean_polygons(
        coastlines,
        xzbbox,
        dry_land_mask,
        road_mask,
        building_footprints,
        explicit_water_mask,
        urban_lookup,
    );
    if polygons.outers.is_empty() {
        return;
    }

    fill_water_polygons(editor, &polygons.outers, &polygons.inners);
}

fn build_ocean_polygons(
    coastlines: &[ProcessedWay],
    xzbbox: &XZBBox,
    dry_land_mask: &CoordinateBitmap,
    road_mask: &CoordinateBitmap,
    building_footprints: &CoordinateBitmap,
    explicit_water_mask: &CoordinateBitmap,
    urban_lookup: &UrbanGroundLookup,
) -> OceanPolygons {
    let mut paths: Vec<Vec<ProcessedNode>> =
        coastlines.iter().map(|way| way.nodes.clone()).collect();
    super::merge_way_segments(&mut paths);

    let mut outers = Vec::new();
    let mut inners = Vec::new();

    for path in paths {
        if path.len() < 2 {
            continue;
        }

        let clipped_path = clip_way_to_bbox(&path, xzbbox);
        if clipped_path.len() < 2 {
            continue;
        }

        if is_closed_path(&clipped_path) {
            inners.push(clipped_path.iter().map(ProcessedNode::xz).collect());
            continue;
        }

        if !endpoint_on_boundary(clipped_path.first().unwrap(), xzbbox)
            || !endpoint_on_boundary(clipped_path.last().unwrap(), xzbbox)
        {
            continue;
        }

        let sample = ocean_side_sample(&clipped_path, xzbbox);
        let chosen = choose_ocean_polygon(
            &clipped_path,
            xzbbox,
            sample,
            dry_land_mask,
            road_mask,
            building_footprints,
            explicit_water_mask,
            urban_lookup,
        );

        if chosen.len() >= 3 {
            outers.push(chosen);
        }
    }

    if outers.is_empty() && !inners.is_empty() {
        outers.push(bbox_ring(xzbbox));
    }

    OceanPolygons { outers, inners }
}

fn choose_ocean_polygon(
    path: &[ProcessedNode],
    xzbbox: &XZBBox,
    sample: Option<(f64, f64)>,
    dry_land_mask: &CoordinateBitmap,
    road_mask: &CoordinateBitmap,
    building_footprints: &CoordinateBitmap,
    explicit_water_mask: &CoordinateBitmap,
    urban_lookup: &UrbanGroundLookup,
) -> Vec<XZPoint> {
    let clockwise = close_path_with_boundary(path, xzbbox, true);
    let counter_clockwise = close_path_with_boundary(path, xzbbox, false);

    let clockwise_score = polygon_protected_overlap_score(
        &clockwise,
        xzbbox,
        dry_land_mask,
        road_mask,
        building_footprints,
        explicit_water_mask,
        urban_lookup,
    );
    let counter_clockwise_score = polygon_protected_overlap_score(
        &counter_clockwise,
        xzbbox,
        dry_land_mask,
        road_mask,
        building_footprints,
        explicit_water_mask,
        urban_lookup,
    );

    match clockwise_score.cmp(&counter_clockwise_score) {
        std::cmp::Ordering::Less => clockwise,
        std::cmp::Ordering::Greater => counter_clockwise,
        std::cmp::Ordering::Equal => match sample {
            Some(sample) => match (
                point_in_polygon(sample, &clockwise),
                point_in_polygon(sample, &counter_clockwise),
            ) {
                (true, false) => clockwise,
                (false, true) => counter_clockwise,
                (true, true) | (false, false) => {
                    if polygon_area(&clockwise).abs() >= polygon_area(&counter_clockwise).abs() {
                        clockwise
                    } else {
                        counter_clockwise
                    }
                }
            },
            None => {
                if polygon_area(&clockwise).abs() >= polygon_area(&counter_clockwise).abs() {
                    clockwise
                } else {
                    counter_clockwise
                }
            }
        },
    }
}

fn polygon_protected_overlap_score(
    polygon: &[XZPoint],
    xzbbox: &XZBBox,
    dry_land_mask: &CoordinateBitmap,
    road_mask: &CoordinateBitmap,
    building_footprints: &CoordinateBitmap,
    explicit_water_mask: &CoordinateBitmap,
    urban_lookup: &UrbanGroundLookup,
) -> usize {
    if polygon.len() < 3 {
        return usize::MAX;
    }

    let mut min_x = i32::MAX;
    let mut min_z = i32::MAX;
    let mut max_x = i32::MIN;
    let mut max_z = i32::MIN;
    for point in polygon {
        min_x = min_x.min(point.x);
        min_z = min_z.min(point.z);
        max_x = max_x.max(point.x);
        max_z = max_z.max(point.z);
    }

    min_x = min_x.clamp(xzbbox.min_x(), xzbbox.max_x());
    min_z = min_z.clamp(xzbbox.min_z(), xzbbox.max_z());
    max_x = max_x.clamp(xzbbox.min_x(), xzbbox.max_x());
    max_z = max_z.clamp(xzbbox.min_z(), xzbbox.max_z());

    let width = (max_x - min_x + 1).max(1);
    let height = (max_z - min_z + 1).max(1);
    let step = ((width.max(height) + 255) / 256).max(1) as usize;

    let mut score = 0usize;
    for z in (min_z..=max_z).step_by(step) {
        for x in (min_x..=max_x).step_by(step) {
            if !protected_land_contains(
                x,
                z,
                dry_land_mask,
                road_mask,
                building_footprints,
                explicit_water_mask,
                urban_lookup,
            ) {
                continue;
            }

            if point_in_polygon((x as f64 + 0.5, z as f64 + 0.5), polygon) {
                score += 1;
                if building_footprints.contains(x, z) {
                    score += 3;
                }
            }
        }
    }

    score
}

fn protected_land_contains(
    x: i32,
    z: i32,
    dry_land_mask: &CoordinateBitmap,
    road_mask: &CoordinateBitmap,
    building_footprints: &CoordinateBitmap,
    explicit_water_mask: &CoordinateBitmap,
    urban_lookup: &UrbanGroundLookup,
) -> bool {
    dry_land_mask.contains(x, z)
        || road_mask.contains(x, z)
        || building_footprints.contains(x, z)
        || (urban_lookup.is_urban(x, z) && !explicit_water_mask.contains(x, z))
}

fn is_closed_path(path: &[ProcessedNode]) -> bool {
    if path.len() < 3 {
        return false;
    }

    let first = path.first().unwrap();
    let last = path.last().unwrap();
    first.id == last.id || (first.x == last.x && first.z == last.z)
}

fn endpoint_on_boundary(node: &ProcessedNode, xzbbox: &XZBBox) -> bool {
    let min_x = xzbbox.min_x();
    let max_x = xzbbox.max_x();
    let min_z = xzbbox.min_z();
    let max_z = xzbbox.max_z();

    (node.x - min_x).abs() <= BOUNDARY_TOLERANCE
        || (node.x - max_x).abs() <= BOUNDARY_TOLERANCE
        || (node.z - min_z).abs() <= BOUNDARY_TOLERANCE
        || (node.z - max_z).abs() <= BOUNDARY_TOLERANCE
}

fn ocean_side_sample(path: &[ProcessedNode], xzbbox: &XZBBox) -> Option<(f64, f64)> {
    let min_x = xzbbox.min_x() as f64;
    let max_x = xzbbox.max_x() as f64;
    let min_z = xzbbox.min_z() as f64;
    let max_z = xzbbox.max_z() as f64;

    for segment in path.windows(2) {
        let a = &segment[0];
        let b = &segment[1];
        let dx = (b.x - a.x) as f64;
        let dz = (b.z - a.z) as f64;
        let len = (dx * dx + dz * dz).sqrt();
        if len < f64::EPSILON {
            continue;
        }

        let mid_x = (a.x + b.x) as f64 / 2.0;
        let mid_z = (a.z + b.z) as f64 / 2.0;

        // In Minecraft x/right + z/down coordinates, the coastline's "right side"
        // is (-dz, dx), matching OSM coastline direction semantics (sea on the right).
        let right_x = -dz / len;
        let right_z = dx / len;

        for distance in [2.0, 1.0, 0.5] {
            let sample_x = mid_x + right_x * distance;
            let sample_z = mid_z + right_z * distance;
            if sample_x > min_x && sample_x < max_x && sample_z > min_z && sample_z < max_z {
                return Some((sample_x, sample_z));
            }
        }
    }

    None
}

fn close_path_with_boundary(
    path: &[ProcessedNode],
    xzbbox: &XZBBox,
    clockwise: bool,
) -> Vec<XZPoint> {
    let mut polygon: Vec<XZPoint> = path.iter().map(ProcessedNode::xz).collect();
    let boundary = boundary_path_between(
        path.last().unwrap().xz(),
        path.first().unwrap().xz(),
        xzbbox,
        clockwise,
    );

    polygon.extend(boundary.into_iter().skip(1));
    polygon
}

fn boundary_path_between(
    start: XZPoint,
    end: XZPoint,
    xzbbox: &XZBBox,
    clockwise: bool,
) -> Vec<XZPoint> {
    if !clockwise {
        let mut reverse = boundary_path_between(end, start, xzbbox, true);
        reverse.reverse();
        return reverse;
    }

    let width = (xzbbox.max_x() - xzbbox.min_x()) as f64;
    let height = (xzbbox.max_z() - xzbbox.min_z()) as f64;
    let perimeter = 2.0 * (width + height);

    let start_t = boundary_position(start, xzbbox);
    let end_t = boundary_position(end, xzbbox);

    let corners = [0.0, width, width + height, 2.0 * width + height];
    let mut result = vec![clamp_to_bbox(start, xzbbox)];

    for &corner_t in &corners {
        if clockwise_between(start_t, corner_t, end_t, perimeter) {
            result.push(point_on_perimeter(corner_t, xzbbox));
        }
    }

    result.push(clamp_to_bbox(end, xzbbox));
    dedupe_points(result)
}

fn boundary_position(point: XZPoint, xzbbox: &XZBBox) -> f64 {
    let point = clamp_to_bbox(point, xzbbox);
    let x = point.x as f64;
    let z = point.z as f64;
    let min_x = xzbbox.min_x() as f64;
    let max_x = xzbbox.max_x() as f64;
    let min_z = xzbbox.min_z() as f64;
    let max_z = xzbbox.max_z() as f64;

    if (z - min_z).abs() <= BOUNDARY_TOLERANCE as f64 {
        x - min_x
    } else if (x - max_x).abs() <= BOUNDARY_TOLERANCE as f64 {
        (max_x - min_x) + (z - min_z)
    } else if (z - max_z).abs() <= BOUNDARY_TOLERANCE as f64 {
        (max_x - min_x) + (max_z - min_z) + (max_x - x)
    } else {
        2.0 * (max_x - min_x) + (max_z - min_z) + (max_z - z)
    }
}

fn point_on_perimeter(t: f64, xzbbox: &XZBBox) -> XZPoint {
    let min_x = xzbbox.min_x() as f64;
    let max_x = xzbbox.max_x() as f64;
    let min_z = xzbbox.min_z() as f64;
    let max_z = xzbbox.max_z() as f64;
    let width = max_x - min_x;
    let height = max_z - min_z;

    if t <= width {
        XZPoint::new((min_x + t).round() as i32, min_z.round() as i32)
    } else if t <= width + height {
        XZPoint::new(max_x.round() as i32, (min_z + (t - width)).round() as i32)
    } else if t <= 2.0 * width + height {
        XZPoint::new(
            (max_x - (t - width - height)).round() as i32,
            max_z.round() as i32,
        )
    } else {
        XZPoint::new(
            min_x.round() as i32,
            (max_z - (t - 2.0 * width - height)).round() as i32,
        )
    }
}

fn clockwise_between(start: f64, point: f64, end: f64, perimeter: f64) -> bool {
    let delta_to_point = (point - start).rem_euclid(perimeter);
    let delta_to_end = (end - start).rem_euclid(perimeter);
    delta_to_point > 0.0 && delta_to_point < delta_to_end
}

fn clamp_to_bbox(point: XZPoint, xzbbox: &XZBBox) -> XZPoint {
    XZPoint::new(
        point.x.clamp(xzbbox.min_x(), xzbbox.max_x()),
        point.z.clamp(xzbbox.min_z(), xzbbox.max_z()),
    )
}

fn dedupe_points(points: Vec<XZPoint>) -> Vec<XZPoint> {
    let mut deduped = Vec::with_capacity(points.len());
    for point in points {
        if deduped.last() != Some(&point) {
            deduped.push(point);
        }
    }
    deduped
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

fn point_in_polygon(point: (f64, f64), polygon: &[XZPoint]) -> bool {
    if polygon.len() < 3 {
        return false;
    }

    let mut inside = false;
    let mut prev = polygon.last().unwrap();

    for curr in polygon {
        let xi = curr.x as f64;
        let zi = curr.z as f64;
        let xj = prev.x as f64;
        let zj = prev.z as f64;

        let intersects = ((zi > point.1) != (zj > point.1))
            && (point.0 < (xj - xi) * (point.1 - zi) / (zj - zi) + xi);
        if intersects {
            inside = !inside;
        }

        prev = curr;
    }

    inside
}

fn polygon_area(polygon: &[XZPoint]) -> f64 {
    if polygon.len() < 3 {
        return 0.0;
    }

    let mut area = 0.0;
    for i in 0..polygon.len() {
        let a = polygon[i];
        let b = polygon[(i + 1) % polygon.len()];
        area += (a.x as f64 * b.z as f64) - (b.x as f64 * a.z as f64);
    }

    area / 2.0
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::collections::HashMap;

    fn coastline_way(id: u64, coords: &[(i32, i32)]) -> ProcessedWay {
        ProcessedWay {
            id,
            tags: HashMap::from([("natural".to_string(), "coastline".to_string())]),
            nodes: coords
                .iter()
                .enumerate()
                .map(|(idx, (x, z))| ProcessedNode {
                    id: id * 100 + idx as u64,
                    tags: HashMap::new(),
                    x: *x,
                    z: *z,
                })
                .collect(),
        }
    }

    fn empty_mask(bbox: &XZBBox) -> CoordinateBitmap {
        CoordinateBitmap::new(bbox)
    }

    #[test]
    fn open_coastline_uses_sea_on_the_right_side() {
        let bbox = XZBBox::rect_from_xz_lengths(10.0, 10.0).unwrap();
        let coast = coastline_way(1, &[(6, 0), (6, 10)]);

        let polygons = build_ocean_polygons(
            &[coast],
            &bbox,
            &empty_mask(&bbox),
            &empty_mask(&bbox),
            &empty_mask(&bbox),
            &empty_mask(&bbox),
            &UrbanGroundLookup::empty(),
        );

        assert_eq!(polygons.outers.len(), 1);
        assert!(point_in_polygon((2.0, 5.0), &polygons.outers[0]));
        assert!(!point_in_polygon((8.0, 5.0), &polygons.outers[0]));
    }

    #[test]
    fn protected_land_can_override_reversed_coastline_direction() {
        let bbox = XZBBox::rect_from_xz_lengths(10.0, 10.0).unwrap();
        let coast = coastline_way(2, &[(6, 10), (6, 0)]);
        let mut dry_land = CoordinateBitmap::new(&bbox);
        dry_land.set(8, 5);

        let polygons = build_ocean_polygons(
            &[coast],
            &bbox,
            &dry_land,
            &empty_mask(&bbox),
            &empty_mask(&bbox),
            &empty_mask(&bbox),
            &UrbanGroundLookup::empty(),
        );

        assert_eq!(polygons.outers.len(), 1);
        assert!(point_in_polygon((2.0, 5.0), &polygons.outers[0]));
        assert!(!point_in_polygon((8.0, 5.0), &polygons.outers[0]));
    }

    #[test]
    fn urban_land_penalty_prefers_city_side_as_dry_land() {
        let bbox = XZBBox::rect_from_xz_lengths(10.0, 10.0).unwrap();
        let coast = coastline_way(3, &[(6, 10), (6, 0)]);
        let urban_lookup = crate::urban_ground::compute_urban_ground_lookup(vec![(8, 5); 5], &bbox);

        let polygons = build_ocean_polygons(
            &[coast],
            &bbox,
            &empty_mask(&bbox),
            &empty_mask(&bbox),
            &empty_mask(&bbox),
            &empty_mask(&bbox),
            &urban_lookup,
        );

        assert_eq!(polygons.outers.len(), 1);
        assert!(point_in_polygon((2.0, 5.0), &polygons.outers[0]));
        assert!(!point_in_polygon((8.0, 5.0), &polygons.outers[0]));
    }

    #[test]
    fn closed_island_coastline_creates_bbox_ocean_hole() {
        let bbox = XZBBox::rect_from_xz_lengths(10.0, 10.0).unwrap();
        let island = coastline_way(4, &[(3, 3), (7, 3), (7, 7), (3, 7), (3, 3)]);

        let polygons = build_ocean_polygons(
            &[island],
            &bbox,
            &empty_mask(&bbox),
            &empty_mask(&bbox),
            &empty_mask(&bbox),
            &empty_mask(&bbox),
            &UrbanGroundLookup::empty(),
        );

        assert_eq!(polygons.outers.len(), 1);
        assert_eq!(polygons.inners.len(), 1);
        assert_eq!(polygons.outers[0], bbox_ring(&bbox));
        assert!(point_in_polygon((1.0, 1.0), &polygons.outers[0]));
    }
}
