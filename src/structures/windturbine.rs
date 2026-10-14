//! Wind turbines at OSM power=generator generator:source=wind features, built
//! to their tagged size: a tapered tower, a nacelle and three blades.
//!
//! A single bundled model used to stand 197 blocks tall everywhere. Danish
//! onshore turbines are mostly far smaller (median tip height 74 m in OSM),
//! and on the normal build height the model's top was cut off flat wherever
//! the ground lay above 59 m.

use std::collections::HashMap;

use crate::block_definitions::{Block, LIGHT_GRAY_CONCRETE, WHITE_CONCRETE};
use crate::land_cover::coord_hash;
use crate::world_editor::WorldEditor;

/// A typical Danish onshore turbine, from the tagged ones in OSM.
const DEFAULT_HUB_M: f64 = 50.0;
const DEFAULT_ROTOR_M: f64 = 52.0;
/// Hub height over rotor diameter of tagged Danish turbines (median).
const HUB_PER_ROTOR: f64 = 0.94;
/// `height` is the tip height; the hub sits at about this share of it.
const HUB_PER_TIP: f64 = 0.65;
/// The blades stay inside the tile halo (64 blocks), so none clips at a seam.
const MAX_RADIUS_BLOCKS: f64 = 60.0;
/// Blocks left free under the world's ceiling.
const CEILING_GAP: i32 = 2;
/// Smallest hub, in blocks, still worth a tower.
const MIN_HUB_BLOCKS: f64 = 6.0;

/// Hub height and rotor diameter in metres.
#[derive(Debug, Clone, Copy, PartialEq)]
pub struct TurbineSize {
    pub hub_m: f64,
    pub rotor_m: f64,
}

fn metres(tags: &HashMap<String, String>, key: &str) -> Option<f64> {
    tags.get(key)
        .and_then(|v| {
            v.trim()
                .trim_end_matches('m')
                .trim()
                .replace(',', ".")
                .parse::<f64>()
                .ok()
        })
        .filter(|v| v.is_finite() && *v > 0.0)
}

/// The turbine's size from `height:hub`, `rotor:diameter` and `height` (the
/// tip height), filling in what is missing from Danish proportions.
pub fn size_from_tags(tags: &HashMap<String, String>) -> TurbineSize {
    let (hub, rotor, tip) = (
        metres(tags, "height:hub"),
        metres(tags, "rotor:diameter"),
        metres(tags, "height"),
    );
    let (hub_m, rotor_m) = match (hub, rotor, tip) {
        (Some(h), Some(r), _) => (h, r),
        (Some(h), None, _) => (h, h / HUB_PER_ROTOR),
        (None, Some(r), Some(t)) if t > r => (t - r / 2.0, r),
        (None, Some(r), _) => (r * HUB_PER_ROTOR, r),
        (None, None, Some(t)) => (t * HUB_PER_TIP, 2.0 * t * (1.0 - HUB_PER_TIP)),
        (None, None, None) => (DEFAULT_HUB_M, DEFAULT_ROTOR_M),
    };
    TurbineSize {
        hub_m: hub_m.clamp(10.0, 250.0),
        rotor_m: rotor_m.clamp(5.0, 250.0),
    }
}

/// Hub height and rotor radius in blocks, shrunk together where the real size
/// would reach past the ceiling or out of the tile halo. `None` when too
/// little room is left for a turbine at all.
fn fit(size: TurbineSize, scale: f64, base_y: i32, ceiling: i32) -> Option<(f64, f64)> {
    let hub = size.hub_m * scale;
    // The blades clear the ground.
    let radius = (size.rotor_m * scale / 2.0).min(hub - 2.0).max(1.0);
    let room = f64::from(ceiling - CEILING_GAP - base_y);
    let shrink = 1.0_f64
        .min(MAX_RADIUS_BLOCKS / radius)
        .min(room / (hub + radius));
    let (hub, radius) = (hub * shrink, radius * shrink);
    (hub >= MIN_HUB_BLOCKS).then_some((hub, radius))
}

/// Builds the turbine standing at (x, z). The rotor faces west, into the
/// prevailing wind, with the blades at a different angle on every turbine.
pub fn place(editor: &mut WorldEditor, x: i32, z: i32, tags: &HashMap<String, String>) {
    let base = editor.get_absolute_y(x, 1, z);
    let Some((hub, radius)) = fit(
        size_from_tags(tags),
        editor.scale(),
        base,
        crate::world_editor::world_max_y(),
    ) else {
        return;
    };
    let mut put = |block: Block, bx: i32, by: i32, bz: i32| {
        editor.set_block_absolute(block, bx, by, bz, None, Some(&[]));
    };
    let top = base + hub.round() as i32;

    // Tapered round tower.
    let r_base = (radius / 14.0).clamp(0.5, 3.0);
    let r_top = (r_base * 0.65).max(0.5);
    for y in base..top {
        let t = f64::from(y - base) / hub.max(1.0);
        let r = r_base + (r_top - r_base) * t;
        let reach = r.ceil() as i32;
        for dx in -reach..=reach {
            for dz in -reach..=reach {
                if f64::from(dx * dx + dz * dz) <= r * r + 0.25 {
                    put(WHITE_CONCRETE, x + dx, y, z + dz);
                }
            }
        }
    }

    // Nacelle along the wind, a little more of it behind the tower than in front.
    let half = if radius < 12.0 {
        0
    } else {
        (radius * 0.05).clamp(1.0, 3.0).round() as i32
    };
    let length = (radius * 0.22).clamp(3.0, 12.0).round() as i32;
    let front = (f64::from(length) * 0.35).ceil() as i32;
    for dx in -front..=(length - front) {
        for dy in -half..=half {
            for dz in -half..=half {
                put(WHITE_CONCRETE, x + dx, top + dy, z + dz);
            }
        }
    }

    // Hub and blades in the north-south plane just west of the nacelle.
    let hx = x - front - 1;
    for dy in -half..=half {
        for dz in -half..=half {
            put(LIGHT_GRAY_CONCRETE, hx, top + dy, z + dz);
        }
    }
    let start = (coord_hash(x, z) % 120) as f64;
    let root = (radius * 0.07).clamp(1.0, 4.0);
    for k in 0..3 {
        let angle = (start + 120.0 * f64::from(k)).to_radians();
        let (sin, cos) = angle.sin_cos();
        let mut t = f64::from(half) + 0.5;
        while t <= radius {
            let width = root + (1.0 - root) * (t / radius);
            let mut s = -(width - 1.0) / 2.0;
            while s <= (width - 1.0) / 2.0 + 1e-9 {
                let dz = t * cos - s * sin;
                let dy = t * sin + s * cos;
                put(
                    WHITE_CONCRETE,
                    hx,
                    top + dy.round() as i32,
                    z + dz.round() as i32,
                );
                s += 0.5;
            }
            t += 0.4;
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn tags(pairs: &[(&str, &str)]) -> HashMap<String, String> {
        pairs
            .iter()
            .map(|(k, v)| (k.to_string(), v.to_string()))
            .collect()
    }

    #[test]
    fn the_size_comes_from_the_tags_or_danish_proportions() {
        let full = size_from_tags(&tags(&[("height:hub", "80"), ("rotor:diameter", "90 m")]));
        assert_eq!(
            full,
            TurbineSize {
                hub_m: 80.0,
                rotor_m: 90.0
            }
        );
        // `height` is the tip height in Danish data.
        let tip = size_from_tags(&tags(&[("height", "100")]));
        assert!(
            (tip.hub_m + tip.rotor_m / 2.0 - 100.0).abs() < 1e-9,
            "{tip:?}"
        );
        let rotor_and_tip = size_from_tags(&tags(&[("rotor:diameter", "60"), ("height", "100")]));
        assert_eq!(
            rotor_and_tip,
            TurbineSize {
                hub_m: 70.0,
                rotor_m: 60.0
            }
        );
        let untagged = size_from_tags(&HashMap::new());
        assert_eq!(
            untagged,
            TurbineSize {
                hub_m: 50.0,
                rotor_m: 52.0
            }
        );
    }

    #[test]
    fn a_turbine_keeps_its_size_where_it_fits() {
        let size = TurbineSize {
            hub_m: 50.0,
            rotor_m: 52.0,
        };
        let (hub, radius) = fit(size, 1.0, 100, 319).unwrap();
        assert_eq!((hub, radius), (50.0, 26.0));
    }

    #[test]
    fn a_turbine_on_high_ground_shrinks_under_the_ceiling() {
        // Ground at 135 m on the normal build height: the floor is at Y 198.
        let size = TurbineSize {
            hub_m: 120.0,
            rotor_m: 120.0,
        };
        let (hub, radius) = fit(size, 1.0, 199, 319).unwrap();
        assert!(199.0 + hub + radius <= 317.0 + 1e-9, "{hub} {radius}");
        assert!((hub / radius - 2.0).abs() < 1e-9, "proportions kept");
    }

    #[test]
    fn the_rotor_stays_inside_the_tile_halo() {
        let size = TurbineSize {
            hub_m: 150.0,
            rotor_m: 160.0,
        };
        let (_, radius) = fit(size, 1.0, 0, 2031).unwrap();
        assert!(radius <= MAX_RADIUS_BLOCKS + 1e-9);
    }

    #[test]
    fn no_room_means_no_turbine() {
        let size = TurbineSize {
            hub_m: 50.0,
            rotor_m: 52.0,
        };
        assert!(fit(size, 1.0, 310, 319).is_none());
    }

    #[test]
    fn a_built_turbine_stands_on_the_ground_under_the_ceiling() {
        use crate::coordinate_system::cartesian::XZBBox;
        let xz = XZBBox::rect_from_min_max(0, 0, 200, 200).unwrap();
        let mut editor = crate::element_processing::building_test_support::test_editor(&xz);
        let tall = tags(&[("height:hub", "140"), ("rotor:diameter", "130")]);
        place(&mut editor, 100, 100, &tall);
        let ceiling = crate::world_editor::world_max_y();
        let base = editor.get_absolute_y(100, 1, 100);
        let mut highest = i32::MIN;
        for y in base..=ceiling {
            for z in 30..=170 {
                for x in 80..=120 {
                    if editor.get_block_absolute(x, y, z).is_some() {
                        highest = highest.max(y);
                    }
                }
            }
        }
        assert!(highest <= ceiling - CEILING_GAP, "{highest}");
        assert!(
            editor.get_block_absolute(100, base, 100).is_some(),
            "the tower stands on the ground"
        );
    }
}
