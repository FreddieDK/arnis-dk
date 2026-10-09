//! Address-only associations for a Danish building contained in a larger OSM outline.
//! These hints never transfer building style or assign one address to the whole outline.
use super::{footprint, Index};
use crate::osm_parser::ProcessedElement;
use geo::{Area, BooleanOps};
use serde::{Deserialize, Serialize};
use std::collections::{BTreeMap, HashMap};

pub(super) const HINTS: &str = "arnis:address_hints";

#[derive(Clone, Debug, Deserialize, Serialize)]
pub(super) struct AddressHint {
    pub address: String,
    pub lat: f64,
    pub lon: f64,
    pub standard: String,
    #[serde(default)]
    pub walls: Vec<[i32; 4]>,
}

impl AddressHint {
    pub fn from_tags(tags: &HashMap<String, String>) -> Option<Self> {
        let lat = tags.get("arnis:entrance:lat")?.parse::<f64>().ok()?;
        let lon = tags.get("arnis:entrance:lon")?.parse::<f64>().ok()?;
        let standard = tags.get("arnis:entrance:standard")?;
        (lat.is_finite() && lon.is_finite() && matches!(standard.as_str(), "TD" | "TK")).then(
            || Self {
                address: tags.get("arnis:address").cloned().unwrap_or_default(),
                lat,
                lon,
                standard: standard.clone(),
                walls: vec![],
            },
        )
    }
    pub fn permits_wall(&self, x: i32, z: i32, scale: f64) -> bool {
        self.walls.is_empty()
            || self.walls.iter().any(|&[ax, az, bx, bz]| {
                let (dx, dz) = ((bx - ax) as f64, (bz - az) as f64);
                let length2 = dx * dx + dz * dz;
                if length2 == 0.0 {
                    return false;
                }
                let t = (((x - ax) as f64 * dx + (z - az) as f64 * dz) / length2).clamp(0.0, 1.0);
                (x as f64 - ax as f64 - t * dx).hypot(z as f64 - az as f64 - t * dz)
                    <= 2.0 * scale.max(1.0)
            })
    }
}

pub(super) fn attach(
    existing: &mut [ProcessedElement],
    incoming: &[ProcessedElement],
    index: &Index,
) {
    let mut collected: BTreeMap<usize, Vec<AddressHint>> = BTreeMap::new();
    for source in incoming {
        let Some(mut hint) =
            AddressHint::from_tags(source.tags()).filter(|h| !h.address.trim().is_empty())
        else {
            continue;
        };
        let Some(shape) = footprint(source) else {
            continue;
        };
        let owners: Vec<_> = index
            .overlaps(&shape)
            .into_iter()
            .filter_map(|(i, _)| {
                if existing[i].tags().contains_key("building:part") {
                    return None;
                }
                let coverage =
                    shape.intersection(&index.shapes[&i]).unsigned_area() / shape.unsigned_area();
                (coverage >= 0.95).then_some(i)
            })
            .collect();
        // The snapped entrance must also stay on this Danish building's wall.
        hint.walls = super::entrances::ways(source)
            .into_iter()
            .flat_map(|w| w.nodes.windows(2).map(|p| [p[0].x, p[0].z, p[1].x, p[1].z]))
            .collect();
        if owners.len() == 1 {
            // Competing containing outlines are ambiguous; proximity is not enough.
            collected.entry(owners[0]).or_default().push(hint);
        }
    }
    for (i, mut hints) in collected {
        hints.sort_by(|a, b| {
            a.address
                .cmp(&b.address)
                .then(a.lat.total_cmp(&b.lat))
                .then(a.lon.total_cmp(&b.lon))
        });
        hints.dedup_by(|a, b| a.address == b.address && a.lat == b.lat && a.lon == b.lon);
        let tags = match &mut existing[i] {
            ProcessedElement::Way(w) => &mut w.tags,
            ProcessedElement::Relation(r) => &mut r.tags,
            _ => continue,
        };
        tags.insert(
            HINTS.into(),
            serde_json::to_string(&hints).expect("finite address coordinates"),
        );
    }
}
