//! A `DecalKey` describes one image. The registry gives each distinct key a block of
//! consecutive map ids before placement, so tile threads only read it and ids are stable.

use super::region::BladeStyle;
use std::collections::{BTreeSet, HashMap, HashSet};

/// Look of a rendered text sign.
#[derive(Clone, Copy, Debug, PartialEq, Eq, Hash, PartialOrd, Ord)]
pub enum TextStyle {
    /// Shop fascia: dark strip with light lettering.
    Fascia,
    /// Street name blade.
    StreetName(BladeStyle),
    /// Small house number plate.
    HouseNumber,
    /// Platform / station name board.
    StationBoard,
    /// Name plate under a transit stop sign.
    StopName,
    /// Engraved memorial plaque.
    Plaque,
}

/// Standard traffic signs drawn from primitives.
#[allow(dead_code)]
#[derive(Clone, Copy, Debug, PartialEq, Eq, Hash, PartialOrd, Ord)]
pub enum TrafficSign {
    Stop,
    GiveWay,
    NoEntry,
    PriorityRoad,
    Crossing,
    OneWay,
    NoParking,
    DeadEnd,
    LevelCrossing,
    HighVoltage,
    Bicycle,
    Motorway,
    MotorwayEnd,
}

/// Route number shield family.
#[derive(Clone, Copy, Debug, PartialEq, Eq, Hash, PartialOrd, Ord)]
pub enum ShieldStyle {
    /// Blue plate, white text (European motorways / A-roads in many countries).
    Blue,
    /// Yellow plate, black text (German Bundesstrasse, Dutch N-roads style).
    Yellow,
    /// Green plate, white text (UK primary routes, some European E-roads).
    Green,
    /// US interstate look: red-white-blue shield.
    Interstate,
    /// White shield with black text (US highways).
    White,
}

#[derive(Clone, Debug, PartialEq, Eq, Hash, PartialOrd, Ord)]
pub enum DecalKey {
    /// A bundled pictogram by asset name.
    Pictogram(&'static str),
    /// Rendered text in a given style; `cols` tiles wide.
    Text {
        style: TextStyle,
        text: String,
        cols: u8,
    },
    Traffic(TrafficSign),
    SpeedLimit {
        value: u16,
        mph: bool,
        style: SpeedStyle,
    },
    RouteShield {
        style: ShieldStyle,
        text: String,
    },
    /// Billboard art variant, 3x2 tiles.
    Poster(u8),
    /// Advertising column poster variant, 1x2 tiles.
    ColumnPoster(u8),
    /// "You are here" board centred on a world position, 2x2 tiles.
    LocalMap {
        x: i32,
        z: i32,
    },
}

/// Speed limit sign shape.
#[derive(Clone, Copy, Debug, PartialEq, Eq, Hash, PartialOrd, Ord)]
pub enum SpeedStyle {
    /// Red-ringed disc (Vienna Convention).
    Disc,
    /// White "SPEED LIMIT" plate (USA).
    UsPlate,
    /// White "MAXIMUM" plate (Canada).
    CaPlate,
}

impl DecalKey {
    /// Tile grid the rendered image spans.
    pub fn dims(&self) -> (u32, u32) {
        match self {
            DecalKey::Text { cols, .. } => (*cols as u32, 1),
            DecalKey::Poster(_) => super::posters::BILLBOARD_TILES,
            DecalKey::ColumnPoster(_) => super::posters::COLUMN_TILES,
            DecalKey::LocalMap { .. } => (2, 2),
            _ => (1, 1),
        }
    }

    /// Whether the image is the same wherever the key appears, so a world can
    /// show one map for all of them. A "you are here" board draws its own spot.
    pub fn shareable(&self) -> bool {
        !matches!(self, DecalKey::LocalMap { .. })
    }

    /// The key's name in a world's decal index.
    pub fn index_name(&self) -> String {
        format!("{self:?}")
    }

    pub fn text(style: TextStyle, text: impl Into<String>, cols: u8) -> DecalKey {
        DecalKey::Text {
            style,
            text: text.into(),
            cols: cols.max(1),
        }
    }
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct DecalEntry {
    /// Map id of the top-left tile; tile (c, r) is `base_id + r * cols + c`.
    pub base_id: i32,
    pub cols: u32,
    pub rows: u32,
}

impl DecalEntry {
    pub fn tile_id(&self, col: u32, row: u32) -> i32 {
        self.base_id + (row * self.cols + col) as i32
    }
}

/// A world's decals by `DecalKey::index_name`: maps already written that a
/// later area can show again.
pub type DecalIndex = HashMap<String, DecalEntry>;

/// Deterministic key -> map id assignment.
#[derive(Debug, Default)]
pub struct DecalRegistry {
    entries: HashMap<DecalKey, DecalEntry>,
    ordered: Vec<DecalKey>,
    /// Keys shown with maps an earlier area of the world wrote.
    reused: HashSet<DecalKey>,
    next_id: i32,
    first_id: i32,
}

impl DecalRegistry {
    /// Ids 0 and 1 belong to the world preview map and the branding map.
    pub const FIRST_ID: i32 = 2;

    /// Assigns ids to `keys` in their sorted order.
    #[cfg(test)]
    pub fn from_keys(keys: BTreeSet<DecalKey>) -> Self {
        Self::from_keys_starting_at(keys, Self::FIRST_ID)
    }

    /// For a world that already holds maps.
    pub fn from_keys_starting_at(keys: BTreeSet<DecalKey>, first_id: i32) -> Self {
        Self::from_keys_reusing(keys, first_id, &DecalIndex::new())
    }

    /// Keys the world's index already holds keep those maps; the rest get new
    /// ids from `first_id` on. A stop sign then is one map in the whole world,
    /// not one per generated area.
    pub fn from_keys_reusing(keys: BTreeSet<DecalKey>, first_id: i32, index: &DecalIndex) -> Self {
        let mut reg = DecalRegistry {
            entries: HashMap::with_capacity(keys.len()),
            ordered: Vec::with_capacity(keys.len()),
            reused: HashSet::new(),
            next_id: first_id,
            first_id,
        };
        for key in keys {
            let (cols, rows) = key.dims();
            let known = key
                .shareable()
                .then(|| index.get(&key.index_name()))
                .flatten()
                .filter(|e| (e.cols, e.rows) == (cols, rows));
            let entry = match known {
                Some(&e) => {
                    reg.reused.insert(key.clone());
                    e
                }
                None => {
                    let e = DecalEntry {
                        base_id: reg.next_id,
                        cols,
                        rows,
                    };
                    reg.next_id += (cols * rows) as i32;
                    e
                }
            };
            reg.entries.insert(key.clone(), entry);
            reg.ordered.push(key);
        }
        reg
    }

    /// True when the key shows a map an earlier area wrote, so it is not written again.
    pub fn is_reused(&self, key: &DecalKey) -> bool {
        self.reused.contains(key)
    }

    /// The registry's shareable keys, for the world's index.
    pub fn index_entries(&self) -> impl Iterator<Item = (String, DecalEntry)> + '_ {
        self.iter()
            .filter(|(k, _)| k.shareable())
            .map(|(k, e)| (k.index_name(), e))
    }

    pub fn get(&self, key: &DecalKey) -> Option<DecalEntry> {
        self.entries.get(key).copied()
    }

    pub fn contains(&self, key: &DecalKey) -> bool {
        self.entries.contains_key(key)
    }

    pub fn len(&self) -> usize {
        self.ordered.len()
    }

    pub fn is_empty(&self) -> bool {
        self.ordered.is_empty()
    }

    /// Keys in id order.
    pub fn iter(&self) -> impl Iterator<Item = (&DecalKey, DecalEntry)> {
        self.ordered.iter().map(move |k| (k, self.entries[k]))
    }

    /// Highest assigned map id, or `FIRST_ID - 1` when empty.
    #[allow(dead_code)]
    pub fn max_id(&self) -> i32 {
        self.next_id - 1
    }

    /// Number of map files this registry produces.
    pub fn tile_count(&self) -> i32 {
        self.next_id - self.first_id
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn ids_are_dense_and_ordered() {
        let mut keys = BTreeSet::new();
        keys.insert(DecalKey::Pictogram("cafe"));
        keys.insert(DecalKey::Poster(0));
        keys.insert(DecalKey::text(TextStyle::Fascia, "Bakery", 2));
        let reg = DecalRegistry::from_keys(keys);
        assert_eq!(reg.len(), 3);
        // Ordering follows the enum: Pictogram < Text < Poster.
        let ids: Vec<i32> = reg.iter().map(|(_, e)| e.base_id).collect();
        assert_eq!(ids[0], DecalRegistry::FIRST_ID);
        assert_eq!(reg.get(&DecalKey::Pictogram("cafe")).unwrap().base_id, 2);
        let text = reg
            .get(&DecalKey::text(TextStyle::Fascia, "Bakery", 2))
            .unwrap();
        assert_eq!((text.base_id, text.cols, text.rows), (3, 2, 1));
        let poster = reg.get(&DecalKey::Poster(0)).unwrap();
        assert_eq!((poster.base_id, poster.cols, poster.rows), (5, 3, 2));
        assert_eq!(poster.tile_id(2, 1), 5 + 5);
        assert_eq!(reg.max_id(), 10);
        assert_eq!(reg.tile_count(), 9);
    }

    #[test]
    fn keys_in_the_world_index_keep_their_maps() {
        let stop = DecalKey::Traffic(TrafficSign::Stop);
        let bakery = DecalKey::text(TextStyle::Fascia, "Bakery", 2);
        let here = DecalKey::LocalMap { x: 5, z: -3 };
        let mut index = DecalIndex::new();
        index.insert(
            stop.index_name(),
            DecalEntry {
                base_id: 7,
                cols: 1,
                rows: 1,
            },
        );
        // A plate drawn at another width is a different image.
        index.insert(
            bakery.index_name(),
            DecalEntry {
                base_id: 8,
                cols: 3,
                rows: 1,
            },
        );
        index.insert(
            here.index_name(),
            DecalEntry {
                base_id: 11,
                cols: 2,
                rows: 2,
            },
        );
        let keys: BTreeSet<DecalKey> = [stop.clone(), bakery.clone(), here.clone()].into();
        let reg = DecalRegistry::from_keys_reusing(keys, 100, &index);
        assert_eq!(reg.get(&stop).unwrap().base_id, 7);
        assert!(reg.is_reused(&stop));
        assert!(!reg.is_reused(&bakery) && reg.get(&bakery).unwrap().base_id >= 100);
        // A "you are here" board draws its own spot, so it is never shared.
        assert!(!reg.is_reused(&here) && reg.get(&here).unwrap().base_id >= 100);
        assert_eq!(reg.tile_count(), 2 + 4, "only the new maps count");
        let names: Vec<String> = reg.index_entries().map(|(n, _)| n).collect();
        assert!(names.contains(&stop.index_name()) && !names.contains(&here.index_name()));
    }

    #[test]
    fn same_keys_same_ids() {
        let build = || {
            let mut keys = BTreeSet::new();
            keys.insert(DecalKey::text(TextStyle::HouseNumber, "12a", 1));
            keys.insert(DecalKey::Traffic(TrafficSign::Stop));
            keys.insert(DecalKey::LocalMap { x: 5, z: -3 });
            DecalRegistry::from_keys(keys)
        };
        let a = build();
        let b = build();
        for (k, e) in a.iter() {
            assert_eq!(b.get(k), Some(e));
        }
    }
}
