//! One World: one persistent Java world that every generation extends.
//!
//! The world pins its frame in the manifest next to `level.dat`: Web Mercator
//! around an origin, or a fixed UTM zone 32 frame. Every requested area
//! is snapped outward to whole chunks in that frame and written into the
//! existing region files, so areas generated at different times line up block
//! for block.

use crate::args::{Args, OneWorldHeight};
use crate::coordinate_system::cartesian::XZBBox;
use crate::coordinate_system::geographic::LLBBox;
use crate::elevation::ElevationAffine;
use crate::projection::{
    llbbox_for_rect, snap_bbox_to_chunks, snap_rect_to_chunks, McdkProjection, ProjectionKind,
    WebMercatorProjection, WorldProjection,
};
use crate::world_utils::{world_is_locked, SessionLock};
use serde::{Deserialize, Serialize};
use std::io::Read;
use std::path::{Path, PathBuf};

pub const MANIFEST_FILE: &str = "arnis_one_world.json";
pub const PREVIEW_DIR: &str = "arnis_one_world/previews";
pub const DEFAULT_WORLD_NAME: &str = "Arnis One World";
/// 2: merges write into empty region files. Version 1 worlds still hold
/// region template chunks outside their areas and are repaired once.
/// 3: new worlds store their elevation mapping when they are created (whole
/// Earth on the extended build height, or sea level at Y 63 on the normal one),
/// which older builds would not apply.
pub const MANIFEST_VERSION: u32 = 3;
/// 4: the world uses the UTM zone 32 frame, which an older build would
/// read as Web Mercator. Web Mercator worlds keep writing version 3.
pub const MCDK_MANIFEST_VERSION: u32 = 4;

/// How a One World maps lat/lon to blocks.
#[derive(Serialize, Deserialize, Clone, Copy, Debug, Default, PartialEq, Eq)]
#[serde(rename_all = "snake_case")]
pub enum Frame {
    /// Web Mercator around `origin_lat`/`origin_lon`, as every world before version 4.
    #[default]
    WebMercator,
    /// UTM zone 32N with `X = E - 600000`, `Z = 6200000 - N`: the coordinates of
    /// the fixed UTM block frame. `origin_lat`/`origin_lon` hold its
    /// (0, 0) near Samsø, which also anchors the climate.
    Mcdk,
}

impl Frame {
    fn is_web_mercator(&self) -> bool {
        *self == Frame::WebMercator
    }

    pub fn kind(self) -> ProjectionKind {
        match self {
            Frame::WebMercator => ProjectionKind::WebMercator,
            Frame::Mcdk => ProjectionKind::Mcdk,
        }
    }

    fn name(self) -> &'static str {
        match self {
            Frame::WebMercator => "Web Mercator",
            Frame::Mcdk => "UTM zone 32",
        }
    }
}

/// UTM zone 32 stays exact thousands of kilometres from its meridian, but the
/// frame is meant for Denmark and its neighbours.
const MCDK_MAX_LON_FROM_9E: f64 = 15.0;
const MCDK_LATS: std::ops::RangeInclusive<f64> = 45.0..=72.0;

/// Sea level of a One World with the normal build height, as in vanilla.
pub const VANILLA_SEA_LEVEL_Y: i32 = 63;

/// Geometry kept past the area edge, so an element straddling a seam is
/// built whole on both sides.
pub const CLIP_PAD_BLOCKS: i32 = 64;

const MAX_ABS_LAT: f64 = 85.0;

/// Rounds to `decimals` places. serde_json reads such short decimals back
/// exactly, but not every f64 it writes, and the first run has to use the same
/// numbers as the runs that load them.
fn stable(v: f64, decimals: i32) -> f64 {
    let f = 10f64.powi(decimals);
    (v * f).round() / f
}

/// Ground data is fetched this far past the area and cropped again, so the
/// smoothing passes (widest: built-up Gaussian, ~90 m) agree across seams.
pub fn ground_pad_blocks(scale: f64) -> i32 {
    ((100.0 * scale).ceil() as i32).max(96)
}

#[derive(Serialize, Deserialize, Clone, Debug, PartialEq)]
pub struct GeneratedArea {
    pub id: u32,
    /// Unix seconds.
    pub generated_at: u64,
    pub arnis_version: String,
    pub min_x: i32,
    pub min_z: i32,
    pub max_x: i32,
    pub max_z: i32,
    pub min_lat: f64,
    pub min_lon: f64,
    pub max_lat: f64,
    pub max_lon: f64,
    pub preview: Option<String>,
}

impl GeneratedArea {
    fn is_inside(&self, other: &XZBBox) -> bool {
        other.min_x() <= self.min_x
            && other.min_z() <= self.min_z
            && other.max_x() >= self.max_x
            && other.max_z() >= self.max_z
    }
}

#[derive(Serialize, Deserialize, Clone, Debug, PartialEq)]
pub struct Manifest {
    pub version: u32,
    pub created_with: String,
    /// Unix seconds.
    pub created_at: u64,
    #[serde(default, skip_serializing_if = "Frame::is_web_mercator")]
    pub frame: Frame,
    pub origin_lat: f64,
    pub origin_lon: f64,
    pub scale: f64,
    pub ground_level: i32,
    pub terrain: bool,
    pub disable_height_limit: bool,
    pub aws_only_elevation: bool,
    /// Already folded into `elevation`; kept so later areas are told which one applies.
    #[serde(default = "real_height", skip_serializing_if = "is_real_height")]
    pub height_multiplier: f64,
    /// Metre to Y mapping shared by every area. Set at creation; worlds from
    /// before version 3 take it from their first terrain area.
    pub elevation: Option<ElevationAffine>,
    pub next_area_id: u32,
    pub areas: Vec<GeneratedArea>,
}

fn real_height() -> f64 {
    1.0
}

fn is_real_height(multiplier: &f64) -> bool {
    *multiplier == 1.0
}

impl Manifest {
    fn new(args: &Args, frame: Frame, origin_lat: f64, origin_lon: f64) -> Self {
        let scale = stable(args.scale, 9);
        let height_multiplier = stable(args.height_multiplier, 6);
        Self {
            version: match frame {
                Frame::WebMercator => MANIFEST_VERSION,
                Frame::Mcdk => MCDK_MANIFEST_VERSION,
            },
            created_with: format!("arnis {}", env!("CARGO_PKG_VERSION")),
            created_at: unix_now(),
            frame,
            origin_lat: stable(origin_lat, 7),
            origin_lon: stable(origin_lon, 7),
            scale,
            ground_level: args.ground_level,
            terrain: args.terrain(),
            disable_height_limit: args.disable_height_limit,
            aws_only_elevation: args.aws_only_elevation,
            height_multiplier,
            // One section up, so water carved at the lowest level stays above the floor.
            elevation: args.terrain().then(|| {
                let floor = crate::ground::min_ground_level_for(args) + 16;
                if !args.disable_height_limit {
                    let mut e = ElevationAffine::sea_level(
                        scale * height_multiplier,
                        VANILLA_SEA_LEVEL_Y,
                        floor,
                    );
                    e.min_height_m = stable(e.min_height_m, 6);
                    return e;
                }
                let mut e = ElevationAffine::whole_earth(
                    scale * height_multiplier,
                    floor,
                    crate::ground::extended_max_y_for(args),
                );
                if let Some(top) = e.soft_top.as_mut() {
                    top.knee_m = stable(top.knee_m, 6);
                    // Down, so Everest stays under the ceiling.
                    top.width_blocks = stable(top.width_blocks - 5e-7, 6);
                }
                e
            }),
            next_area_id: 1,
            areas: Vec::new(),
        }
    }

    pub fn projection(&self) -> WorldProjection {
        match self.frame {
            Frame::WebMercator => WorldProjection::WebMercator(WebMercatorProjection::new(
                self.origin_lat,
                self.origin_lon,
                self.scale,
            )),
            Frame::Mcdk => WorldProjection::Mcdk(McdkProjection::new(self.scale)),
        }
    }

    pub fn path_in(world_dir: &Path) -> PathBuf {
        world_dir.join(MANIFEST_FILE)
    }

    pub fn load(world_dir: &Path) -> Result<Option<Self>, String> {
        let path = Self::path_in(world_dir);
        if !path.is_file() {
            return Ok(None);
        }
        let text = std::fs::read_to_string(&path)
            .map_err(|e| format!("Failed to read {}: {e}", path.display()))?;
        let manifest: Manifest = serde_json::from_str(&text)
            .map_err(|e| format!("{} is not a valid One World manifest: {e}", path.display()))?;
        if manifest.version > MCDK_MANIFEST_VERSION {
            return Err(format!(
                "{} was written by a newer Arnis (manifest version {}); update Arnis to extend this world.",
                path.display(),
                manifest.version
            ));
        }
        manifest
            .validate()
            .map_err(|e| format!("{} is damaged: {e}", path.display()))?;
        Ok(Some(manifest))
    }

    fn validate(&self) -> Result<(), String> {
        if !(self.origin_lat.is_finite() && self.origin_lat.abs() <= MAX_ABS_LAT) {
            return Err(format!("origin latitude {}", self.origin_lat));
        }
        if !(self.origin_lon.is_finite() && self.origin_lon.abs() <= 180.0) {
            return Err(format!("origin longitude {}", self.origin_lon));
        }
        if self.frame == Frame::Mcdk && self.version < MCDK_MANIFEST_VERSION {
            return Err(format!(
                "the UTM zone 32 frame needs manifest version {MCDK_MANIFEST_VERSION}"
            ));
        }
        crate::args::validate_scale(self.scale)?;
        crate::args::validate_height_multiplier(self.height_multiplier)?;
        if let Some(e) = &self.elevation {
            let soft_top_ok = e.soft_top.is_none_or(|t| {
                t.knee_m.is_finite() && t.width_blocks.is_finite() && t.width_blocks > 0.0
            });
            if !(e.min_height_m.is_finite()
                && e.blocks_per_meter.is_finite()
                && e.blocks_per_meter >= 0.0
                && soft_top_ok)
            {
                return Err("elevation mapping".to_string());
            }
        }
        if let Some(a) = self
            .areas
            .iter()
            .find(|a| a.min_x > a.max_x || a.min_z > a.max_z)
        {
            return Err(format!("area {} has an empty rectangle", a.id));
        }
        Ok(())
    }

    pub fn save(&self, world_dir: &Path) -> Result<(), String> {
        let path = Self::path_in(world_dir);
        let text = serde_json::to_string_pretty(self)
            .map_err(|e| format!("Failed to serialize the One World manifest: {e}"))?;
        crate::world_utils::replace_file_atomically(&path, text.as_bytes())
            .map_err(|e| format!("Failed to write {}: {e}", path.display()))
    }

    pub fn extent(&self) -> Option<XZBBox> {
        let first = self.areas.first()?;
        let mut min_x = first.min_x;
        let mut min_z = first.min_z;
        let mut max_x = first.max_x;
        let mut max_z = first.max_z;
        for a in &self.areas[1..] {
            min_x = min_x.min(a.min_x);
            min_z = min_z.min(a.min_z);
            max_x = max_x.max(a.max_x);
            max_z = max_z.max(a.max_z);
        }
        XZBBox::rect_from_min_max(min_x, min_z, max_x, max_z).ok()
    }
}

/// Chunks inside `rect` that already exist in the world's region files,
/// read from the region headers alone.
pub fn existing_chunks(world_dir: &Path, rect: &XZBBox) -> u64 {
    let (cx0, cz0) = (rect.min_x() >> 4, rect.min_z() >> 4);
    let (cx1, cz1) = (rect.max_x() >> 4, rect.max_z() >> 4);
    let region_dir = crate::world_utils::WorldLayout::of(world_dir)
        .overworld_dir(world_dir)
        .join("region");
    let mut count = 0;
    for rz in (cz0 >> 5)..=(cz1 >> 5) {
        for rx in (cx0 >> 5)..=(cx1 >> 5) {
            let path = region_dir.join(format!("r.{rx}.{rz}.mca"));
            let mut header = [0u8; 4096];
            let read = std::fs::File::open(&path).and_then(|mut f| f.read_exact(&mut header));
            if read.is_err() {
                continue;
            }
            for lz in 0..32 {
                for lx in 0..32 {
                    let (cx, cz) = (rx * 32 + lx, rz * 32 + lz);
                    let i = 4 * (lx + lz * 32) as usize;
                    if (cx0..=cx1).contains(&cx)
                        && (cz0..=cz1).contains(&cz)
                        && header[i..i + 4] != [0; 4]
                    {
                        count += 1;
                    }
                }
            }
        }
    }
    count
}

/// The One World a run lands in, carried in `Args::one_world_run`.
#[derive(Clone, Debug)]
pub struct RunContext {
    pub world_dir: PathBuf,
    /// The world's frame, as a projection kind.
    pub projection: ProjectionKind,
    /// This area's block rectangle `[min_x, min_z, max_x, max_z]`.
    pub rect: [i32; 4],
    pub origin_lat: f64,
    pub origin_lon: f64,
    pub extending: bool,
    pub elevation: Option<ElevationAffine>,
    /// Chunks of this area that already exist and are replaced.
    pub replaced_chunks: u64,
    pub area_id: u32,
}

impl RunContext {
    pub fn preview_path(&self) -> PathBuf {
        self.world_dir
            .join(PREVIEW_DIR)
            .join(format!("area-{}.png", self.area_id))
    }
}

/// A resolved run. Holds the world's session lock until dropped.
pub struct Session {
    pub created: bool,
    pub llbbox: LLBBox,
    pub lock: SessionLock,
}

fn compatibility_errors(manifest: &Manifest, args: &Args) -> Vec<String> {
    let mut errors = Vec::new();
    if args.projection == ProjectionKind::Mcdk && manifest.frame != Frame::Mcdk {
        errors.push(format!(
            "the world uses {} coordinates, and a world cannot change its frame",
            manifest.frame.name()
        ));
    }
    if (manifest.scale - args.scale).abs() > 1e-9 {
        errors.push(format!(
            "world scale {:.2} does not match the world's {:.2}",
            args.scale, manifest.scale
        ));
    }
    if manifest.ground_level != args.ground_level {
        errors.push(format!(
            "ground level {} does not match the world's {}",
            args.ground_level, manifest.ground_level
        ));
    }
    if manifest.terrain != args.terrain() {
        errors.push(format!(
            "the world was generated {} terrain, so this area must be too",
            if manifest.terrain { "with" } else { "without" }
        ));
    }
    errors
}

fn open_in_minecraft(world_dir: &Path) -> String {
    format!(
        "The One World at {} is open in Minecraft. Leave the world (or close the game) and try again.",
        world_dir.display()
    )
}

/// Opens or creates the One World at `world_dir`, locks it, and points `args`
/// at the world's frame.
pub fn prepare(world_dir: &Path, requested: &LLBBox, args: &mut Args) -> Result<Session, String> {
    if args.bedrock || args.luanti {
        return Err("One World is available for Java Edition worlds only.".to_string());
    }
    if !args.body.is_earth() {
        return Err("One World is available for Earth only.".to_string());
    }
    if args.rotation.abs() > f64::EPSILON {
        return Err(
            "One World keeps the world aligned to real-world coordinates, so rotation must be 0."
                .to_string(),
        );
    }
    if requested.min().lat() < -MAX_ABS_LAT || requested.max().lat() > MAX_ABS_LAT {
        return Err(format!(
            "One World covers latitudes up to {MAX_ABS_LAT} degrees north and south."
        ));
    }

    let fresh_dir = !world_dir.exists();
    if !fresh_dir && Manifest::load(world_dir)?.is_none() {
        let empty = std::fs::read_dir(world_dir)
            .map(|mut d| d.next().is_none())
            .unwrap_or(false);
        if !empty {
            return Err(format!(
                "{} exists but is not a One World (no {}). Choose another world name or delete the folder.",
                world_dir.display(),
                MANIFEST_FILE
            ));
        }
    }
    if world_is_locked(world_dir) {
        return Err(open_in_minecraft(world_dir));
    }
    std::fs::create_dir_all(world_dir)
        .map_err(|e| format!("Failed to create {}: {e}", world_dir.display()))?;
    let lock = SessionLock::acquire(world_dir).map_err(|_| open_in_minecraft(world_dir))?;
    let mut owned = false;
    let resolved = resolve(world_dir, requested, args, lock, &mut owned);
    if resolved.is_err() && fresh_dir {
        // Everything in it is ours once the check under the lock passed.
        // Before that only an empty folder is removed.
        let _ = if owned {
            std::fs::remove_dir_all(world_dir)
        } else {
            std::fs::remove_dir(world_dir)
        };
    }
    resolved
}

/// The part of `prepare` that runs under the world's lock.
/// `owned` is set once the folder is known to hold nothing but this run's files.
fn resolve(
    world_dir: &Path,
    requested: &LLBBox,
    args: &mut Args,
    lock: SessionLock,
    owned: &mut bool,
) -> Result<Session, String> {
    let (mut manifest, created) = match Manifest::load(world_dir)? {
        Some(manifest) => {
            if !world_dir.join("level.dat").is_file() {
                return Err(format!(
                    "{} has a One World manifest but no level.dat; the world seems damaged.",
                    world_dir.display()
                ));
            }
            let errors = compatibility_errors(&manifest, args);
            if !errors.is_empty() {
                return Err(format!(
                    "This area cannot be added to the One World at {}: {}. Change the setting, or use another world name to start a new One World.",
                    world_dir.display(),
                    errors.join("; ")
                ));
            }
            if manifest.disable_height_limit != args.disable_height_limit {
                println!(
                    "Note: One World keeps the build height it was created with ({}).",
                    if manifest.disable_height_limit {
                        "extended"
                    } else {
                        "vanilla"
                    }
                );
                args.disable_height_limit = manifest.disable_height_limit;
            }
            if manifest.height_multiplier != args.height_multiplier {
                println!(
                    "Note: One World keeps the terrain height multiplier it was created with ({}x).",
                    manifest.height_multiplier
                );
                args.height_multiplier = manifest.height_multiplier;
            }
            (manifest, false)
        }
        None => {
            // Checked again under the lock: the folder may have filled up since.
            let foreign = std::fs::read_dir(world_dir)
                .map_err(|e| format!("Failed to read {}: {e}", world_dir.display()))?
                .filter_map(Result::ok)
                .any(|entry| entry.file_name() != "session.lock");
            if foreign {
                return Err(format!(
                    "{} exists but is not a One World (no {}). Choose another world name or delete the folder.",
                    world_dir.display(),
                    MANIFEST_FILE
                ));
            }
            *owned = true;
            match args.one_world_height {
                // Room for any place on Earth, so the first area does not limit later ones.
                OneWorldHeight::Earth => {
                    if !args.disable_height_limit {
                        println!(
                            "Note: One World uses the extended build height, so any place on Earth fits."
                        );
                        args.disable_height_limit = true;
                    }
                }
                OneWorldHeight::Vanilla => {
                    println!(
                        "One World: normal build height (Y -64 to 319), sea level at Y {VANILLA_SEA_LEVEL_Y}."
                    );
                    args.disable_height_limit = false;
                }
            }
            let frame = if args.projection == ProjectionKind::Mcdk {
                println!("One World: UTM zone 32 coordinates (X = E - 600000, Z = 6200000 - N).");
                Frame::Mcdk
            } else {
                Frame::WebMercator
            };
            let (origin_lat, origin_lon) = match frame {
                Frame::WebMercator => (
                    (requested.min().lat() + requested.max().lat()) / 2.0,
                    (requested.min().lng() + requested.max().lng()) / 2.0,
                ),
                Frame::Mcdk => McdkProjection::new(1.0).inverse(0.0, 0.0),
            };
            (Manifest::new(args, frame, origin_lat, origin_lon), true)
        }
    };

    if manifest.frame == Frame::Mcdk && args.projection != ProjectionKind::Mcdk {
        println!("Note: this One World uses UTM zone 32 coordinates.");
    }
    args.scale = manifest.scale;
    let projection = manifest.projection();
    let (xzbbox, llbbox) = match args.block_rect {
        Some(rect) => {
            let xzbbox = snap_rect_to_chunks(rect)?;
            let llbbox = llbbox_for_rect(&projection, &xzbbox)?;
            (xzbbox, llbbox)
        }
        None => snap_bbox_to_chunks(&projection, requested)?,
    };
    if manifest.frame == Frame::Mcdk {
        let corners = [
            (llbbox.min().lat(), llbbox.min().lng()),
            (llbbox.max().lat(), llbbox.max().lng()),
        ];
        if corners.iter().any(|&(lat, lon)| {
            !MCDK_LATS.contains(&lat) || (lon - 9.0).abs() > MCDK_MAX_LON_FROM_9E
        }) {
            return Err("This area is too far from Denmark for the UTM zone 32 frame.".to_string());
        }
    }

    if created {
        let name = world_dir
            .file_name()
            .and_then(|n| n.to_str())
            .filter(|n| !n.trim().is_empty())
            .unwrap_or(DEFAULT_WORLD_NAME)
            .to_string();
        crate::world_utils::write_world_skeleton(world_dir, &name, false)?;
        manifest.save(world_dir)?;
    }

    if manifest.version < 2 {
        let removed = crate::world_editor::java::drop_misplaced_chunks(world_dir).map_err(|e| {
            format!(
                "Failed to repair the One World at {}: {e}",
                world_dir.display()
            )
        })?;
        println!(
            "One World: repaired a world from an earlier build ({removed} stray chunks dropped)."
        );
        manifest.version = 2;
        manifest.save(world_dir)?;
    }

    if manifest.aws_only_elevation != args.aws_only_elevation {
        println!(
            "Note: One World keeps the elevation source it was created with ({}).",
            if manifest.aws_only_elevation {
                "legacy AWS terrain"
            } else {
                "high-resolution terrain"
            }
        );
        args.aws_only_elevation = manifest.aws_only_elevation;
    }

    let replaced = if created {
        0
    } else {
        existing_chunks(world_dir, &xzbbox)
    };
    let extending = !manifest.areas.is_empty();
    let area_id = manifest.next_area_id;

    args.one_world_run = Some(RunContext {
        world_dir: world_dir.to_path_buf(),
        projection: manifest.frame.kind(),
        rect: [
            xzbbox.min_x(),
            xzbbox.min_z(),
            xzbbox.max_x(),
            xzbbox.max_z(),
        ],
        origin_lat: manifest.origin_lat,
        origin_lon: manifest.origin_lon,
        extending,
        elevation: manifest.elevation,
        replaced_chunks: replaced,
        area_id,
    });
    args.projection = manifest.frame.kind();
    args.bbox = Some(llbbox);
    // Voxy rebuilds the LOD database from the regions of one run, and both
    // facade sources replace the world's resource pack on every run.
    if args.voxy_lod {
        println!("Note: the Voxy LOD cache is off in One World mode.");
        args.voxy_lod = false;
    }
    if args.mapillary_facade_mode.places_displays() && args.mapillary_facades_wanted() {
        println!("Note: One World builds Mapillary facades as blocks; photo panels are off.");
        args.mapillary_facade_mode = crate::args::FacadeMode::Blocks;
    }
    if args.building_facades {
        println!("Note: the preset building facades are off in One World mode.");
        args.building_facades = false;
    }
    args.map_preview = true;

    println!(
        "One World: {} {} at {}",
        if created {
            "created"
        } else if extending {
            "extending"
        } else {
            "using"
        },
        world_dir
            .file_name()
            .and_then(|n| n.to_str())
            .unwrap_or(DEFAULT_WORLD_NAME),
        world_dir.display()
    );
    println!(
        "  area #{area_id}: blocks x {}..={} z {}..={} ({} x {} chunks){}",
        xzbbox.min_x(),
        xzbbox.max_x(),
        xzbbox.min_z(),
        xzbbox.max_z(),
        (xzbbox.max_x() - xzbbox.min_x() + 1) / 16,
        (xzbbox.max_z() - xzbbox.min_z() + 1) / 16,
        if replaced > 0 {
            format!(", {replaced} existing chunks will be replaced")
        } else {
            String::new()
        }
    );
    // In Web Mercator the ground scale changes with latitude; far from the
    // origin a request becomes a much larger world than its size on the map
    // suggests. UTM keeps a metre within 0.1 % of a block across Denmark.
    let centre_lat = (llbbox.min().lat() + llbbox.max().lat()) / 2.0;
    let density = manifest.origin_lat.to_radians().cos() / centre_lat.to_radians().cos();
    if manifest.frame == Frame::WebMercator && !(0.8..=1.25).contains(&density) {
        println!(
            "Note: this far from the world's origin one metre is {:.2} blocks instead of {:.2}.",
            args.scale * density,
            args.scale
        );
    }

    Ok(Session {
        created,
        llbbox,
        lock,
    })
}

/// Records a finished area and drops the ones it fully covers.
pub fn record_area(
    run: &RunContext,
    llbbox: &LLBBox,
    xzbbox: &XZBBox,
    elevation: Option<ElevationAffine>,
    preview: Option<&Path>,
) -> Result<(), String> {
    let mut manifest = Manifest::load(&run.world_dir)?.ok_or_else(|| {
        format!(
            "{} vanished during generation",
            Manifest::path_in(&run.world_dir).display()
        )
    })?;
    if manifest.elevation.is_none() {
        manifest.elevation = elevation;
    }
    let preview_rel = preview.and_then(|p| {
        p.strip_prefix(&run.world_dir)
            .ok()
            .map(|r| r.to_string_lossy().replace('\\', "/"))
    });
    let (covered, kept): (Vec<GeneratedArea>, Vec<GeneratedArea>) =
        std::mem::take(&mut manifest.areas)
            .into_iter()
            .partition(|a| a.is_inside(xzbbox));
    manifest.areas = kept;
    manifest.areas.push(GeneratedArea {
        id: run.area_id,
        generated_at: unix_now(),
        arnis_version: env!("CARGO_PKG_VERSION").to_string(),
        min_x: xzbbox.min_x(),
        min_z: xzbbox.min_z(),
        max_x: xzbbox.max_x(),
        max_z: xzbbox.max_z(),
        min_lat: llbbox.min().lat(),
        min_lon: llbbox.min().lng(),
        max_lat: llbbox.max().lat(),
        max_lon: llbbox.max().lng(),
        preview: preview_rel.clone(),
    });
    manifest.next_area_id = manifest.next_area_id.max(run.area_id.saturating_add(1));
    manifest.save(&run.world_dir)?;
    for old in covered {
        if let Some(p) = old.preview.filter(|p| Some(p) != preview_rel.as_ref()) {
            if let Some(path) = safe_preview_path(&run.world_dir, &p) {
                let _ = std::fs::remove_file(path);
            }
        }
    }
    Ok(())
}

/// Stores the elevation mapping as soon as the first terrain area has one,
/// so a run that fails later cannot leave chunks behind on another mapping.
pub fn remember_elevation(run: &RunContext, elevation: ElevationAffine) -> Result<(), String> {
    if run.elevation.is_some() {
        return Ok(());
    }
    let Some(mut manifest) = Manifest::load(&run.world_dir)? else {
        return Ok(());
    };
    if manifest.elevation.is_none() {
        manifest.elevation = Some(elevation);
        manifest.save(&run.world_dir)?;
    }
    Ok(())
}

/// Manifests can come with downloaded worlds, so their preview paths are
/// only followed inside the preview folder.
pub fn safe_preview_path(world_dir: &Path, rel: &str) -> Option<PathBuf> {
    let name = rel.strip_prefix(PREVIEW_DIR)?.strip_prefix('/')?;
    let valid = !name.is_empty()
        && name.ends_with(".png")
        && name
            .chars()
            .all(|c| c.is_ascii_alphanumeric() || c == '-' || c == '_' || c == '.')
        && !name.contains("..");
    valid.then(|| world_dir.join(PREVIEW_DIR).join(name))
}

fn unix_now() -> u64 {
    std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|d| d.as_secs())
        .unwrap_or(0)
}

#[cfg(test)]
mod tests {
    use super::*;
    use clap::Parser;

    fn args_for(bbox: &str, extra: &[&str]) -> Args {
        let mut cmd = vec!["arnis", "--output-dir", ".", "--bbox", bbox];
        cmd.extend_from_slice(extra);
        Args::parse_from(cmd)
    }

    const MUNICH: &str = "48.130,11.560,48.145,11.590";

    fn rect(args: &Args, s: &Session) -> XZBBox {
        crate::projection::ProjectionSpec::from_args(args)
            .transformer(&s.llbbox)
            .unwrap()
            .1
    }

    fn record(args: &Args, s: &Session, elevation: Option<ElevationAffine>) {
        let run = args.one_world_run.clone().unwrap();
        record_area(&run, &s.llbbox, &rect(args, s), elevation, None).unwrap();
    }

    #[test]
    fn a_new_world_gets_a_manifest_and_a_chunk_aligned_area() {
        let dir = tempfile::tempdir().unwrap();
        let world = dir.path().join("My One World");
        let req = LLBBox::from_str(MUNICH).unwrap();
        let mut args = args_for(MUNICH, &[]);
        let session = prepare(&world, &req, &mut args).unwrap();
        assert!(session.created);
        assert!(world.join("level.dat").is_file());
        assert!(!world.join("region").join("r.0.0.mca").exists());
        let manifest = Manifest::load(&world).unwrap().unwrap();
        assert!(manifest.areas.is_empty());
        let area = rect(&args, &session);
        assert_eq!(area.min_x().rem_euclid(16), 0);
        assert_eq!((area.max_z() + 1).rem_euclid(16), 0);
        let run = args.one_world_run.as_ref().unwrap();
        assert_eq!(run.area_id, 1);
        assert!(!run.extending);
        assert_eq!(
            args.projection,
            crate::projection::ProjectionKind::WebMercator
        );
        assert_eq!(args.bbox.unwrap(), session.llbbox);
    }

    #[test]
    fn recorded_areas_survive_and_a_second_run_extends() {
        let dir = tempfile::tempdir().unwrap();
        let world = dir.path().join("w");
        let mut args = args_for(MUNICH, &[]);
        let session = prepare(&world, &LLBBox::from_str(MUNICH).unwrap(), &mut args).unwrap();
        let run = args.one_world_run.clone().unwrap();
        let affine = run.elevation.expect("set when the world is created");
        let other = ElevationAffine {
            min_height_m: 500.0,
            ..affine
        };
        record(&args, &session, Some(other));
        drop(session);

        let east = "48.130,11.585,48.145,11.610";
        let mut args2 = args_for(east, &[]);
        let session2 = prepare(&world, &LLBBox::from_str(east).unwrap(), &mut args2).unwrap();
        assert!(!session2.created);
        let run2 = args2.one_world_run.as_ref().unwrap();
        assert_eq!(run2.area_id, 2);
        assert!(run2.extending);
        assert_eq!(run2.elevation, Some(affine));
        assert_eq!(run2.origin_lat, run.origin_lat);
        assert_eq!(run2.origin_lon, run.origin_lon);
        assert_eq!(run2.replaced_chunks, 0, "nothing was written yet");
    }

    #[test]
    fn an_incompatible_setting_is_refused_with_a_reason() {
        let dir = tempfile::tempdir().unwrap();
        let world = dir.path().join("w");
        let req = LLBBox::from_str(MUNICH).unwrap();
        drop(prepare(&world, &req, &mut args_for(MUNICH, &[])).unwrap());

        let err = prepare(&world, &req, &mut args_for(MUNICH, &["--scale", "2"]))
            .err()
            .unwrap();
        assert!(err.contains("world scale 2.00"), "{err}");

        let err = prepare(&world, &req, &mut args_for(MUNICH, &["--mode", "geo-only"]))
            .err()
            .unwrap();
        assert!(err.contains("with terrain"), "{err}");
    }

    const SLAGELSE: &str = "55.395,11.340,55.410,11.365";

    #[test]
    fn a_utm_world_uses_the_fixed_utm_block_coordinates() {
        let dir = tempfile::tempdir().unwrap();
        let world = dir.path().join("w");
        let req = LLBBox::from_str(SLAGELSE).unwrap();
        let mut args = args_for(SLAGELSE, &["--one-world", "--projection", "mcdk"]);
        let session = prepare(&world, &req, &mut args).unwrap();
        let manifest = Manifest::load(&world).unwrap().unwrap();
        assert_eq!(manifest.frame, Frame::Mcdk);
        assert_eq!(manifest.version, MCDK_MANIFEST_VERSION);
        // (0, 0) near Samsø.
        assert_eq!(
            (manifest.origin_lat, manifest.origin_lon),
            (55.9349761, 10.6007821)
        );
        let text = std::fs::read_to_string(Manifest::path_in(&world)).unwrap();
        assert!(text.contains("\"frame\": \"mcdk\""), "{text}");

        let run = args.one_world_run.as_ref().unwrap();
        assert_eq!(run.projection, ProjectionKind::Mcdk);
        assert_eq!(args.projection, ProjectionKind::Mcdk);
        let area = rect(&args, &session);
        assert_eq!(
            [area.min_x(), area.min_z(), area.max_x(), area.max_z()],
            run.rect
        );
        // Slagelse centre lies inside, at X = E - 600000, Z = 6200000 - N.
        let (t, _) = crate::projection::ProjectionSpec::from_args(&args)
            .transformer(&session.llbbox)
            .unwrap();
        let p = t.transform_point(
            crate::coordinate_system::geographic::LLPoint::new(55.4036, 11.354).unwrap(),
        );
        assert_eq!((p.x, p.z), (49051, 57773));
        assert!(area.contains(&p));
    }

    #[test]
    fn a_block_rect_becomes_the_area_exactly() {
        let dir = tempfile::tempdir().unwrap();
        let world = dir.path().join("w");
        let req = LLBBox::from_str(SLAGELSE).unwrap();
        let cell = "48128,56832,49151,57855";
        let flags = ["--one-world", "--projection", "mcdk", "--block-rect", cell];
        let mut args = args_for(SLAGELSE, &flags);
        let session = prepare(&world, &req, &mut args).unwrap();
        assert_eq!(
            args.one_world_run.as_ref().unwrap().rect,
            [48128, 56832, 49151, 57855]
        );
        let area = rect(&args, &session);
        assert_eq!(
            (area.min_x(), area.min_z(), area.max_x(), area.max_z()),
            (48128, 56832, 49151, 57855)
        );
        // The bbox covers the whole rectangle, corners included.
        let proj = McdkProjection::new(1.0);
        for (x, z) in [(48128.0, 56832.0), (49152.0, 57856.0), (48128.0, 57856.0)] {
            let (lat, lon) = proj.inverse(x, z);
            assert!(lat >= session.llbbox.min().lat() && lat <= session.llbbox.max().lat());
            assert!(lon >= session.llbbox.min().lng() && lon <= session.llbbox.max().lng());
        }
        record(&args, &session, None);
        drop(session);

        // The next cell east, without naming the frame again.
        let mut args2 = args_for(
            SLAGELSE,
            &["--one-world", "--block-rect", "49152,56832,50175,57855"],
        );
        let session2 = prepare(&world, &req, &mut args2).unwrap();
        assert_eq!(args2.projection, ProjectionKind::Mcdk);
        assert_eq!(rect(&args2, &session2).min_x(), 49152);
        assert!(args2.one_world_run.as_ref().unwrap().extending);
    }

    #[test]
    fn a_world_keeps_its_frame() {
        let dir = tempfile::tempdir().unwrap();
        let world = dir.path().join("w");
        let req = LLBBox::from_str(SLAGELSE).unwrap();
        drop(prepare(&world, &req, &mut args_for(SLAGELSE, &["--one-world"])).unwrap());
        let err = prepare(
            &world,
            &req,
            &mut args_for(SLAGELSE, &["--one-world", "--projection", "mcdk"]),
        )
        .err()
        .unwrap();
        assert!(err.contains("cannot change its frame"), "{err}");
        // Web Mercator worlds keep writing a manifest older builds read.
        let manifest = Manifest::load(&world).unwrap().unwrap();
        assert_eq!(manifest.frame, Frame::WebMercator);
        assert_eq!(manifest.version, MANIFEST_VERSION);
        let text = std::fs::read_to_string(Manifest::path_in(&world)).unwrap();
        assert!(!text.contains("frame"), "{text}");
    }

    #[test]
    fn the_mcdk_frame_stays_near_denmark() {
        let dir = tempfile::tempdir().unwrap();
        let world = dir.path().join("w");
        let tokyo = "35.68,139.75,35.69,139.76";
        let err = prepare(
            &world,
            &LLBBox::from_str(tokyo).unwrap(),
            &mut args_for(tokyo, &["--one-world", "--projection", "mcdk"]),
        )
        .err()
        .unwrap();
        assert!(err.contains("too far from Denmark"), "{err}");
        assert!(!world.exists());
    }

    #[test]
    fn a_manifest_claiming_mcdk_below_version_4_is_damaged() {
        let dir = tempfile::tempdir().unwrap();
        let world = dir.path().join("w");
        let req = LLBBox::from_str(SLAGELSE).unwrap();
        drop(prepare(&world, &req, &mut args_for(SLAGELSE, &["--one-world"])).unwrap());
        let path = Manifest::path_in(&world);
        let mut value: serde_json::Value =
            serde_json::from_str(&std::fs::read_to_string(&path).unwrap()).unwrap();
        value["frame"] = "mcdk".into();
        std::fs::write(&path, value.to_string()).unwrap();
        let err = Manifest::load(&world).unwrap_err();
        assert!(err.contains("damaged"), "{err}");
    }

    #[test]
    fn a_new_world_has_room_for_all_of_earth() {
        let dir = tempfile::tempdir().unwrap();
        let world = dir.path().join("w");
        let req = LLBBox::from_str(MUNICH).unwrap();
        let mut args = args_for(MUNICH, &[]);
        assert!(!args.disable_height_limit);
        drop(prepare(&world, &req, &mut args).unwrap());
        assert!(args.disable_height_limit);
        let manifest = Manifest::load(&world).unwrap().unwrap();
        assert_eq!(manifest.version, MANIFEST_VERSION);
        assert!(manifest.disable_height_limit);
        let e = manifest.elevation.unwrap();
        assert_eq!(e.y_for_metres(-430.0), -2014.0);
        assert_eq!(e.y_for_metres(520.0), -1064.0);
        assert!(e.y_for_metres(8849.0) <= 2016.0);
        // What the first run used is exactly what later runs read back.
        let run = args.one_world_run.as_ref().unwrap();
        assert_eq!(run.elevation, Some(e));
        assert_eq!(
            (run.origin_lat, run.origin_lon),
            (manifest.origin_lat, manifest.origin_lon)
        );

        let flat = dir.path().join("flat");
        let mut args = args_for(MUNICH, &["--mode", "geo-only"]);
        drop(prepare(&flat, &req, &mut args).unwrap());
        assert_eq!(Manifest::load(&flat).unwrap().unwrap().elevation, None);
    }

    #[test]
    fn a_vanilla_world_keeps_the_normal_height_with_sea_level_at_63() {
        let dir = tempfile::tempdir().unwrap();
        let world = dir.path().join("w");
        let req = LLBBox::from_str(MUNICH).unwrap();
        let mut args = args_for(MUNICH, &["--one-world-height", "vanilla"]);
        drop(prepare(&world, &req, &mut args).unwrap());
        assert!(!args.disable_height_limit);
        assert_eq!(crate::ground::extended_min_y_for(&args), -64);
        assert_eq!(crate::ground::world_top_y_for(&args), 319);
        let manifest = Manifest::load(&world).unwrap().unwrap();
        assert_eq!(manifest.version, MANIFEST_VERSION);
        assert!(!manifest.disable_height_limit);
        let e = manifest.elevation.unwrap();
        assert_eq!(e.y_for_metres(0.0), 63.0);
        assert_eq!(e.y_for_metres(171.0), 234.0);
        assert_eq!(e.ground_level, -46);
        assert_eq!(args.one_world_run.as_ref().unwrap().elevation, Some(e));

        // Later areas keep it, whatever height they ask for.
        for extra in [&[][..], &["--disable-height-limit"][..]] {
            let mut args = args_for(MUNICH, extra);
            drop(prepare(&world, &req, &mut args).unwrap());
            assert!(!args.disable_height_limit);
            assert_eq!(args.one_world_run.unwrap().elevation, Some(e));
        }

        // And an extended world stays extended.
        let tall = dir.path().join("tall");
        drop(prepare(&tall, &req, &mut args_for(MUNICH, &[])).unwrap());
        let mut args = args_for(MUNICH, &["--one-world-height", "vanilla"]);
        drop(prepare(&tall, &req, &mut args).unwrap());
        assert!(args.disable_height_limit);
        assert_eq!(
            args.one_world_run
                .unwrap()
                .elevation
                .unwrap()
                .y_for_metres(0.0),
            -1584.0
        );
    }

    #[test]
    fn the_height_multiplier_is_fixed_by_the_first_area() {
        let dir = tempfile::tempdir().unwrap();
        let req = LLBBox::from_str(MUNICH).unwrap();
        let real = dir.path().join("real");
        drop(prepare(&real, &req, &mut args_for(MUNICH, &[])).unwrap());
        let text = std::fs::read_to_string(Manifest::path_in(&real)).unwrap();
        assert!(!text.contains("height_multiplier"), "{text}");

        let world = dir.path().join("w");
        let mut args = args_for(MUNICH, &["--height-multiplier", "2"]);
        drop(prepare(&world, &req, &mut args).unwrap());
        let manifest = Manifest::load(&world).unwrap().unwrap();
        assert_eq!(manifest.height_multiplier, 2.0);
        let e = manifest.elevation.unwrap();
        assert_eq!(e.blocks_per_meter, 2.0);
        assert_eq!(e.y_for_metres(520.0) - e.y_for_metres(-430.0), 1900.0);

        let mut args = args_for(MUNICH, &[]);
        drop(prepare(&world, &req, &mut args).unwrap());
        assert_eq!(args.height_multiplier, 2.0);
        assert_eq!(args.one_world_run.unwrap().elevation, Some(e));
    }

    #[test]
    fn an_older_world_keeps_its_build_height() {
        let dir = tempfile::tempdir().unwrap();
        let world = dir.path().join("w");
        let req = LLBBox::from_str(MUNICH).unwrap();
        drop(prepare(&world, &req, &mut args_for(MUNICH, &[])).unwrap());
        let mut manifest = Manifest::load(&world).unwrap().unwrap();
        manifest.version = 2;
        manifest.disable_height_limit = false;
        manifest.elevation = None;
        manifest.save(&world).unwrap();

        let mut args = args_for(MUNICH, &["--disable-height-limit"]);
        drop(prepare(&world, &req, &mut args).unwrap());
        assert!(!args.disable_height_limit);
        assert_eq!(args.one_world_run.unwrap().elevation, None);
        assert_eq!(Manifest::load(&world).unwrap().unwrap().version, 2);
    }

    #[test]
    fn a_locked_world_is_refused_and_the_session_holds_the_lock() {
        let dir = tempfile::tempdir().unwrap();
        let world = dir.path().join("w");
        let req = LLBBox::from_str(MUNICH).unwrap();
        let session = prepare(&world, &req, &mut args_for(MUNICH, &[])).unwrap();
        let err = prepare(&world, &req, &mut args_for(MUNICH, &[]))
            .err()
            .unwrap();
        assert!(err.contains("open in Minecraft"), "{err}");
        drop(session);

        let held = SessionLock::acquire(&world).unwrap();
        assert!(prepare(&world, &req, &mut args_for(MUNICH, &[])).is_err());
        drop(held);
        assert!(prepare(&world, &req, &mut args_for(MUNICH, &[])).is_ok());
    }

    #[test]
    fn a_refused_first_run_leaves_no_folder_behind() {
        let dir = tempfile::tempdir().unwrap();
        let world = dir.path().join("w");
        // Wider than the world border at scale 4, refused after the lock is taken.
        let huge = "-10.0,-179.0,10.0,179.0";
        let err = prepare(
            &world,
            &LLBBox::from_str(huge).unwrap(),
            &mut args_for(huge, &["--scale", "4"]),
        )
        .err()
        .unwrap();
        assert!(err.contains("world border"), "{err}");
        assert!(!world.exists());
    }

    #[test]
    fn a_foreign_folder_is_not_taken_over() {
        let dir = tempfile::tempdir().unwrap();
        let world = dir.path().join("w");
        std::fs::create_dir_all(&world).unwrap();
        std::fs::write(world.join("level.dat"), b"x").unwrap();
        let err = prepare(
            &world,
            &LLBBox::from_str(MUNICH).unwrap(),
            &mut args_for(MUNICH, &[]),
        )
        .err()
        .unwrap();
        assert!(err.contains("not a One World"), "{err}");
    }

    #[test]
    fn refused_runs_create_nothing() {
        let dir = tempfile::tempdir().unwrap();
        let world = dir.path().join("w");
        let req = LLBBox::from_str(MUNICH).unwrap();
        assert!(prepare(&world, &req, &mut args_for(MUNICH, &["--rotation", "10"])).is_err());
        assert!(prepare(&world, &req, &mut args_for(MUNICH, &["--bedrock"])).is_err());
        let polar = "86.0,10.0,86.1,10.1";
        assert!(prepare(
            &world,
            &LLBBox::from_str(polar).unwrap(),
            &mut args_for(polar, &[])
        )
        .is_err());
        assert!(!world.exists());
    }

    #[test]
    fn an_area_past_the_world_border_is_refused() {
        let dir = tempfile::tempdir().unwrap();
        let world = dir.path().join("w");
        let req = LLBBox::from_str(MUNICH).unwrap();
        let mut args = args_for(MUNICH, &["--scale", "4"]);
        let session = prepare(&world, &req, &mut args).unwrap();
        record(&args, &session, None);
        drop(session);
        let far = "-33.88,151.19,-33.85,151.23";
        let err = prepare(
            &world,
            &LLBBox::from_str(far).unwrap(),
            &mut args_for(far, &["--scale", "4"]),
        )
        .err()
        .unwrap();
        assert!(err.contains("world border"), "{err}");
    }

    #[test]
    fn covered_areas_are_dropped_and_an_identical_rerun_replaces_itself() {
        let dir = tempfile::tempdir().unwrap();
        let world = dir.path().join("w");
        let req = LLBBox::from_str(MUNICH).unwrap();
        let mut args = args_for(MUNICH, &[]);
        let s1 = prepare(&world, &req, &mut args).unwrap();
        record(&args, &s1, None);
        drop(s1);
        let mut again = args_for(MUNICH, &[]);
        let s1b = prepare(&world, &req, &mut again).unwrap();
        record(&again, &s1b, None);
        drop(s1b);
        let m = Manifest::load(&world).unwrap().unwrap();
        assert_eq!(m.areas.len(), 1);
        assert_eq!(m.areas[0].id, 2);

        let big = "48.120,11.550,48.155,11.600";
        let mut args2 = args_for(big, &[]);
        let s2 = prepare(&world, &LLBBox::from_str(big).unwrap(), &mut args2).unwrap();
        record(&args2, &s2, None);
        let m = Manifest::load(&world).unwrap().unwrap();
        assert_eq!(m.areas.len(), 1);
        assert_eq!(m.areas[0].id, 3);
        assert_eq!(m.extent().unwrap().min_x(), rect(&args2, &s2).min_x());
    }

    #[test]
    fn a_damaged_manifest_is_refused() {
        let dir = tempfile::tempdir().unwrap();
        let world = dir.path().join("w");
        let req = LLBBox::from_str(MUNICH).unwrap();
        drop(prepare(&world, &req, &mut args_for(MUNICH, &[])).unwrap());
        let mut m = Manifest::load(&world).unwrap().unwrap();
        m.scale = 0.0;
        m.save(&world).unwrap();
        let err = Manifest::load(&world).unwrap_err();
        assert!(err.contains("damaged"), "{err}");
    }

    #[test]
    fn preview_paths_stay_inside_the_preview_folder() {
        let w = Path::new("/w");
        assert_eq!(
            safe_preview_path(w, "arnis_one_world/previews/area-3.png"),
            Some(w.join(PREVIEW_DIR).join("area-3.png"))
        );
        assert_eq!(
            safe_preview_path(w, "arnis_one_world/previews/../level.dat"),
            None
        );
        assert_eq!(safe_preview_path(w, "../../etc/passwd"), None);
        assert_eq!(
            safe_preview_path(w, "arnis_one_world/previews/a/b.png"),
            None
        );
        assert_eq!(safe_preview_path(w, "arnis_one_world/previews/x.txt"), None);
    }

    #[test]
    fn existing_chunks_are_read_from_the_region_headers() {
        let dir = tempfile::tempdir().unwrap();
        let region = dir.path().join("region");
        std::fs::create_dir_all(&region).unwrap();
        let mut header = vec![0u8; 8192];
        // Chunks (0, 0) and (1, 0) of region (0, 0), and (31, 31) of region (-1, -1).
        header[0..4].copy_from_slice(&[0, 0, 2, 1]);
        header[4..8].copy_from_slice(&[0, 0, 3, 1]);
        std::fs::write(region.join("r.0.0.mca"), &header).unwrap();
        let mut other = vec![0u8; 8192];
        let i = 4 * (31 + 31 * 32);
        other[i..i + 4].copy_from_slice(&[0, 0, 2, 1]);
        std::fs::write(region.join("r.-1.-1.mca"), &other).unwrap();

        let rect = XZBBox::rect_from_min_max(-16, -16, 15, 15).unwrap();
        assert_eq!(existing_chunks(dir.path(), &rect), 2);
        let rect = XZBBox::rect_from_min_max(16, 0, 31, 15).unwrap();
        assert_eq!(existing_chunks(dir.path(), &rect), 1);
        let rect = XZBBox::rect_from_min_max(512, 512, 527, 527).unwrap();
        assert_eq!(existing_chunks(dir.path(), &rect), 0);
    }

    #[test]
    fn a_manifest_without_areas_has_no_extent() {
        let dir = tempfile::tempdir().unwrap();
        let world = dir.path().join("w");
        drop(
            prepare(
                &world,
                &LLBBox::from_str(MUNICH).unwrap(),
                &mut args_for(MUNICH, &[]),
            )
            .unwrap(),
        );
        assert!(Manifest::load(&world).unwrap().unwrap().extent().is_none());
    }
}
