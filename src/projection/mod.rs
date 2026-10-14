pub mod utm;
pub mod web_mercator;

pub use utm::Utm32Projection;
pub use web_mercator::WebMercatorProjection;

use crate::coordinate_system::cartesian::XZBBox;
use crate::coordinate_system::geographic::LLBBox;
use crate::coordinate_system::transformation::CoordTransformer;
use std::fmt;
use std::str::FromStr;

/// Trait for converting between WGS84 geographic coordinates and a projected
/// coordinate system used in Minecraft world generation.
#[allow(dead_code)]
pub trait Projection {
    /// Convert WGS84 latitude/longitude (degrees) to projected (x, z) in meters
    /// (or blocks, depending on scale).
    fn forward(&self, lat: f64, lon: f64) -> (f64, f64);

    /// Convert projected (x, z) back to WGS84 latitude/longitude (degrees).
    fn inverse(&self, x: f64, z: f64) -> (f64, f64);
}

/// Available map projection variants.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum ProjectionKind {
    /// Web Mercator (EPSG:3857-like) projection with a local origin offset.
    WebMercator,
    /// UTM zone 32N as a fixed block frame (`X = E - 600000`,
    /// `Z = 6200000 - N`). One World only.
    Utm32,
    /// Simple local coordinate system (no geographic projection).
    Local,
}

impl fmt::Display for ProjectionKind {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            ProjectionKind::WebMercator => write!(f, "web_mercator"),
            ProjectionKind::Utm32 => write!(f, "utm32"),
            ProjectionKind::Local => write!(f, "local"),
        }
    }
}

impl FromStr for ProjectionKind {
    type Err = String;

    fn from_str(s: &str) -> Result<Self, Self::Err> {
        match s.to_lowercase().as_str() {
            "web_mercator" | "webmercator" | "mercator" => Ok(ProjectionKind::WebMercator),
            // `mcdk` is the name worlds and plans made before October 2026 carry.
            "utm32" | "mcdk" => Ok(ProjectionKind::Utm32),
            "local" => Ok(ProjectionKind::Local),
            other => Err(format!("unknown projection kind: '{other}'")),
        }
    }
}

/// The block frame of a projected run.
#[derive(Debug, Clone, Copy, PartialEq)]
pub enum WorldProjection {
    WebMercator(WebMercatorProjection),
    Utm32(Utm32Projection),
}

impl WorldProjection {
    /// Block `(x, z)` as fractions; north is -Z.
    pub fn forward(&self, lat: f64, lon: f64) -> (f64, f64) {
        match self {
            Self::WebMercator(p) => (p.x_for_lon(lon), p.z_for_lat(lat)),
            Self::Utm32(p) => p.forward(lat, lon),
        }
    }

    /// Latitude and longitude of a block position.
    pub fn inverse(&self, x: f64, z: f64) -> (f64, f64) {
        match self {
            Self::WebMercator(p) => (p.lat_for_z(z), p.lon_for_x(x)),
            Self::Utm32(p) => p.inverse(x, z),
        }
    }
}

/// How one run maps lat/lon to block coordinates. Every consumer goes
/// through this so they cannot disagree.
#[derive(Debug, Clone, Copy, PartialEq)]
pub struct ProjectionSpec {
    pub kind: ProjectionKind,
    /// Web Mercator origin; `None` is the bbox centre.
    pub origin: Option<(f64, f64)>,
    pub scale: f64,
    /// Blocks of geometry kept past the world edge when clipping, so elements
    /// crossing it are built whole.
    pub clip_pad: i32,
    /// The run's block rectangle `[min_x, min_z, max_x, max_z]` when it is
    /// fixed in blocks. UTM bends parallels and meridians, so a lat/lon bbox
    /// and a block rectangle are different areas there and the rectangle is
    /// the one that counts. Web Mercator derives it from the bbox as before.
    pub rect: Option<[i32; 4]>,
}

impl ProjectionSpec {
    #[cfg(test)]
    pub fn local(scale: f64) -> Self {
        Self {
            kind: ProjectionKind::Local,
            origin: None,
            scale,
            clip_pad: 0,
            rect: None,
        }
    }

    pub fn from_args(args: &crate::args::Args) -> Self {
        match &args.one_world_run {
            Some(run) => Self {
                kind: run.projection,
                origin: Some((run.origin_lat, run.origin_lon)),
                scale: args.scale,
                clip_pad: crate::one_world::CLIP_PAD_BLOCKS,
                rect: Some(run.rect),
            },
            None => Self {
                kind: args.projection,
                origin: None,
                scale: args.scale,
                clip_pad: 0,
                rect: None,
            },
        }
    }

    pub fn mercator(&self, llbbox: &LLBBox) -> WebMercatorProjection {
        let (lat, lon) = self.origin.unwrap_or_else(|| {
            (
                (llbbox.min().lat() + llbbox.max().lat()) / 2.0,
                (llbbox.min().lng() + llbbox.max().lng()) / 2.0,
            )
        });
        WebMercatorProjection::new(lat, lon, self.scale)
    }

    /// The frame of a projected run.
    pub fn frame(&self, llbbox: &LLBBox) -> WorldProjection {
        match self.kind {
            ProjectionKind::Utm32 => WorldProjection::Utm32(Utm32Projection::new(self.scale)),
            _ => WorldProjection::WebMercator(self.mercator(llbbox)),
        }
    }

    pub fn transformer(&self, llbbox: &LLBBox) -> Result<(CoordTransformer, XZBBox), String> {
        match self.kind {
            ProjectionKind::Local => CoordTransformer::llbbox_to_xzbbox(llbbox, self.scale),
            ProjectionKind::WebMercator => {
                CoordTransformer::with_projection(llbbox, self.scale, self.mercator(llbbox))
            }
            ProjectionKind::Utm32 => {
                let frame = self.frame(llbbox);
                let rect = match self.rect {
                    Some([x0, z0, x1, z1]) => XZBBox::rect_from_min_max(x0, z0, x1, z1)?,
                    None => block_bounds(&frame, llbbox)?,
                };
                CoordTransformer::with_frame(llbbox, frame, rect)
            }
        }
    }

    /// The lat/lon bbox to fetch OSM for: the area plus the clip margin, so a
    /// feature standing just past the edge still reaches in.
    pub fn fetch_bbox(&self, llbbox: &LLBBox) -> LLBBox {
        if self.clip_pad <= 0 || self.kind == ProjectionKind::Local {
            return *llbbox;
        }
        let Ok((_, rect)) = self.transformer(llbbox) else {
            return *llbbox;
        };
        llbbox_for_rect(&self.frame(llbbox), &self.clip_bbox(&rect)).unwrap_or(*llbbox)
    }

    pub fn clip_bbox(&self, xzbbox: &XZBBox) -> XZBBox {
        if self.clip_pad <= 0 {
            return xzbbox.clone();
        }
        XZBBox::rect_from_min_max(
            xzbbox.min_x().saturating_sub(self.clip_pad),
            xzbbox.min_z().saturating_sub(self.clip_pad),
            xzbbox.max_x().saturating_add(self.clip_pad),
            xzbbox.max_z().saturating_add(self.clip_pad),
        )
        .unwrap_or_else(|_| xzbbox.clone())
    }
}

pub const CHUNK_BLOCKS: i32 = 16;

pub const WORLD_BORDER_BLOCKS: f64 = 29_999_984.0;

/// Checked before the edges are cast to integers.
pub(crate) fn check_projected_edges(edges: [f64; 4]) -> Result<(), String> {
    if edges.iter().any(|v| !v.is_finite()) {
        return Err("bounding box is outside the Web Mercator domain".to_string());
    }
    if edges.iter().any(|v| v.abs() > WORLD_BORDER_BLOCKS) {
        return Err(
            "this area lies past the Minecraft world border in this world's frame".to_string(),
        );
    }
    Ok(())
}

/// Rounds a projected edge to the block grid. Within 1e-6 of an integer it is
/// that integer, which absorbs the float noise of an inverse-projected edge.
pub(crate) fn snap_edge(v: f64, ceil: bool) -> i32 {
    let r = v.round();
    if (v - r).abs() < 1e-6 {
        return r as i32;
    }
    if ceil {
        v.ceil() as i32
    } else {
        v.floor() as i32
    }
}

/// Points sampled along each edge in a frame that bends lines: a 100 km edge
/// is then traced to a few centimetres.
const EDGE_SAMPLES: usize = 128;

/// About a centimetre, which covers that chord error.
const OUTLINE_MARGIN_DEG: f64 = 1e-7;

/// West, east, north and south block edges of a lat/lon bbox. In UTM they
/// bound the bbox's slightly curved outline.
fn projected_edges(proj: &WorldProjection, llbbox: &LLBBox) -> [f64; 4] {
    let (s, w, n, e) = (
        llbbox.min().lat(),
        llbbox.min().lng(),
        llbbox.max().lat(),
        llbbox.max().lng(),
    );
    if let WorldProjection::WebMercator(p) = proj {
        return [
            p.x_for_lon(w),
            p.x_for_lon(e),
            p.z_for_lat(n),
            p.z_for_lat(s),
        ];
    }
    let mut edges = [
        f64::INFINITY,
        f64::NEG_INFINITY,
        f64::INFINITY,
        f64::NEG_INFINITY,
    ];
    for i in 0..=EDGE_SAMPLES {
        let t = i as f64 / EDGE_SAMPLES as f64;
        let (lat, lon) = (s + (n - s) * t, w + (e - w) * t);
        for (la, lo) in [(lat, w), (lat, e), (s, lon), (n, lon)] {
            let (x, z) = proj.forward(la, lo);
            edges = [
                edges[0].min(x),
                edges[1].max(x),
                edges[2].min(z),
                edges[3].max(z),
            ];
        }
    }
    edges
}

/// The blocks a lat/lon bbox touches, not snapped to chunks.
fn block_bounds(proj: &WorldProjection, llbbox: &LLBBox) -> Result<XZBBox, String> {
    let [x_w, x_e, z_n, z_s] = projected_edges(proj, llbbox);
    check_projected_edges([x_w, x_e, z_n, z_s])?;
    let x_min = snap_edge(x_w, false);
    let z_min = snap_edge(z_n, false);
    XZBBox::rect_from_min_max(
        x_min,
        z_min,
        (snap_edge(x_e, true) - 1).max(x_min),
        (snap_edge(z_s, true) - 1).max(z_min),
    )
}

/// The lat/lon bbox around the block-space rectangle from `(x0, z0)` to
/// `(x1, z1)`, grown by `margin` degrees.
pub fn enclosing_llbbox(
    proj: &WorldProjection,
    (x0, z0): (f64, f64),
    (x1, z1): (f64, f64),
    margin: f64,
) -> Result<LLBBox, String> {
    let (mut s, mut w, mut n, mut e) = (
        f64::INFINITY,
        f64::INFINITY,
        f64::NEG_INFINITY,
        f64::NEG_INFINITY,
    );
    for i in 0..=EDGE_SAMPLES {
        let t = i as f64 / EDGE_SAMPLES as f64;
        let (x, z) = (x0 + (x1 - x0) * t, z0 + (z1 - z0) * t);
        for (bx, bz) in [(x0, z), (x1, z), (x, z0), (x, z1)] {
            let (lat, lon) = proj.inverse(bx, bz);
            s = s.min(lat);
            n = n.max(lat);
            w = w.min(lon);
            e = e.max(lon);
        }
    }
    LLBBox::new(s - margin, w - margin, n + margin, e + margin)
}

/// Snaps a bbox outward to whole chunks. Returns the inclusive block rectangle
/// and the lat/lon bbox covering it. In Web Mercator the axes are separable,
/// so the two describe the same area; in UTM the bbox reaches a little past
/// the rectangle's corners.
pub fn snap_bbox_to_chunks(
    proj: &WorldProjection,
    llbbox: &LLBBox,
) -> Result<(XZBBox, LLBBox), String> {
    let [x_w, x_e, z_n, z_s] = projected_edges(proj, llbbox);
    check_projected_edges([x_w, x_e, z_n, z_s])?;

    let floor_chunk = |v: f64| (snap_edge(v, false) as f64 / CHUNK_BLOCKS as f64).floor() as i32;
    let ceil_chunk = |v: f64| (snap_edge(v, true) as f64 / CHUNK_BLOCKS as f64).ceil() as i32;

    let cx0 = floor_chunk(x_w);
    let mut cx1 = ceil_chunk(x_e);
    let cz0 = floor_chunk(z_n);
    let mut cz1 = ceil_chunk(z_s);
    if cx1 <= cx0 {
        cx1 = cx0 + 1;
    }
    if cz1 <= cz0 {
        cz1 = cz0 + 1;
    }

    let min_x = cx0 * CHUNK_BLOCKS;
    let max_x_excl = cx1 * CHUNK_BLOCKS;
    let min_z = cz0 * CHUNK_BLOCKS;
    let max_z_excl = cz1 * CHUNK_BLOCKS;
    check_projected_edges([
        min_x as f64,
        max_x_excl as f64,
        min_z as f64,
        max_z_excl as f64,
    ])?;

    let xzbbox = XZBBox::rect_from_min_max(min_x, min_z, max_x_excl - 1, max_z_excl - 1)?;
    let effective = llbbox_for_rect(proj, &xzbbox)?;
    Ok((xzbbox, effective))
}

/// Grows a block rectangle `[min_x, min_z, max_x, max_z]` outward to whole chunks.
pub fn snap_rect_to_chunks([x0, z0, x1, z1]: [i32; 4]) -> Result<XZBBox, String> {
    if x1 < x0 || z1 < z0 {
        return Err(format!("the block rectangle {x0},{z0},{x1},{z1} is empty"));
    }
    check_projected_edges([x0 as f64, x1 as f64, z0 as f64, z1 as f64])?;
    let floor = |v: i32| v.div_euclid(CHUNK_BLOCKS) * CHUNK_BLOCKS;
    // Past the last block, rounded up to the next chunk edge.
    let end = |v: i32| (v + CHUNK_BLOCKS).div_euclid(CHUNK_BLOCKS) * CHUNK_BLOCKS;
    let (min_x, min_z) = (floor(x0), floor(z0));
    let (max_x_excl, max_z_excl) = (end(x1), end(z1));
    check_projected_edges([
        min_x as f64,
        max_x_excl as f64,
        min_z as f64,
        max_z_excl as f64,
    ])?;
    XZBBox::rect_from_min_max(min_x, min_z, max_x_excl - 1, max_z_excl - 1)
}

/// The lat/lon bbox covering a block rectangle, outer edges included.
pub fn llbbox_for_rect(proj: &WorldProjection, xzbbox: &XZBBox) -> Result<LLBBox, String> {
    match proj {
        WorldProjection::WebMercator(p) => LLBBox::new(
            p.lat_for_z(xzbbox.max_z() as f64 + 1.0),
            p.lon_for_x(xzbbox.min_x() as f64),
            p.lat_for_z(xzbbox.min_z() as f64),
            p.lon_for_x(xzbbox.max_x() as f64 + 1.0),
        ),
        WorldProjection::Utm32(_) => enclosing_llbbox(
            proj,
            (xzbbox.min_x() as f64, xzbbox.min_z() as f64),
            (xzbbox.max_x() as f64 + 1.0, xzbbox.max_z() as f64 + 1.0),
            OUTLINE_MARGIN_DEG,
        ),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn wm(lat: f64, lon: f64, scale: f64) -> WorldProjection {
        WorldProjection::WebMercator(WebMercatorProjection::new(lat, lon, scale))
    }

    fn utm32(scale: f64) -> WorldProjection {
        WorldProjection::Utm32(Utm32Projection::new(scale))
    }

    #[test]
    fn test_projection_kind_display() {
        assert_eq!(ProjectionKind::WebMercator.to_string(), "web_mercator");
        assert_eq!(ProjectionKind::Utm32.to_string(), "utm32");
        assert_eq!(ProjectionKind::Local.to_string(), "local");
    }

    #[test]
    fn test_projection_kind_from_str() {
        assert_eq!(
            "web_mercator".parse::<ProjectionKind>().unwrap(),
            ProjectionKind::WebMercator
        );
        assert_eq!(
            "webmercator".parse::<ProjectionKind>().unwrap(),
            ProjectionKind::WebMercator
        );
        assert_eq!(
            "mercator".parse::<ProjectionKind>().unwrap(),
            ProjectionKind::WebMercator
        );
        assert_eq!(
            "local".parse::<ProjectionKind>().unwrap(),
            ProjectionKind::Local
        );
        assert_eq!(
            "LOCAL".parse::<ProjectionKind>().unwrap(),
            ProjectionKind::Local
        );
    }

    #[test]
    fn test_projection_kind_from_str_invalid() {
        assert!("unknown".parse::<ProjectionKind>().is_err());
    }

    #[test]
    fn test_projection_kind_roundtrip() {
        for kind in [
            ProjectionKind::WebMercator,
            ProjectionKind::Utm32,
            ProjectionKind::Local,
        ] {
            let s = kind.to_string();
            let parsed: ProjectionKind = s.parse().unwrap();
            assert_eq!(parsed, kind);
        }
    }

    #[test]
    fn snapped_rect_sits_on_chunk_edges_and_covers_the_request() {
        let proj = wm(48.1372, 11.5755, 1.0);
        let req = LLBBox::new(48.130, 11.560, 48.145, 11.590).unwrap();
        let (rect, eff) = snap_bbox_to_chunks(&proj, &req).unwrap();
        assert_eq!(rect.min_x().rem_euclid(16), 0);
        assert_eq!(rect.min_z().rem_euclid(16), 0);
        assert_eq!((rect.max_x() + 1).rem_euclid(16), 0);
        assert_eq!((rect.max_z() + 1).rem_euclid(16), 0);
        assert!(eff.min().lat() <= req.min().lat());
        assert!(eff.max().lat() >= req.max().lat());
        assert!(eff.min().lng() <= req.min().lng());
        assert!(eff.max().lng() >= req.max().lng());
        assert!(rect.min_x() < 0 && rect.max_x() > 0);
        assert!(rect.min_z() < 0 && rect.max_z() > 0);
    }

    #[test]
    fn effective_bbox_reprojects_to_the_same_rect() {
        let proj = wm(-33.8688, 151.2093, 0.7);
        let req = LLBBox::new(-33.880, 151.190, -33.850, 151.230).unwrap();
        let (rect, eff) = snap_bbox_to_chunks(&proj, &req).unwrap();
        let spec = ProjectionSpec {
            kind: ProjectionKind::WebMercator,
            origin: Some((-33.8688, 151.2093)),
            scale: 0.7,
            clip_pad: 0,
            rect: None,
        };
        let (_, again) = spec.transformer(&eff).unwrap();
        assert_eq!(
            (again.min_x(), again.min_z(), again.max_x(), again.max_z()),
            (rect.min_x(), rect.min_z(), rect.max_x(), rect.max_z())
        );
        assert_eq!(llbbox_for_rect(&proj, &rect), Ok(eff));
    }

    #[test]
    fn adjacent_requests_tile_without_gap_or_overlap() {
        let proj = wm(52.52, 13.405, 1.0);
        let west = LLBBox::new(52.510, 13.390, 52.530, 13.405).unwrap();
        let (rw, effw) = snap_bbox_to_chunks(&proj, &west).unwrap();
        let east = LLBBox::new(52.510, effw.max().lng(), 52.530, 13.420).unwrap();
        let (re, _) = snap_bbox_to_chunks(&proj, &east).unwrap();
        assert_eq!(re.min_x(), rw.max_x() + 1);
        assert_eq!(re.min_z(), rw.min_z());
    }

    #[test]
    fn a_degenerate_request_still_gets_one_chunk() {
        let proj = wm(10.0, 10.0, 1.0);
        let req = LLBBox::new(10.0, 10.0, 10.000001, 10.000001).unwrap();
        let (rect, _) = snap_bbox_to_chunks(&proj, &req).unwrap();
        assert_eq!(rect.max_x() - rect.min_x() + 1, 16);
        assert_eq!(rect.max_z() - rect.min_z() + 1, 16);
    }

    #[test]
    fn snapped_areas_stay_inside_the_world_border() {
        // At scale 4 the border is about 67 degrees east of the origin.
        let proj = wm(0.0, 0.0, 4.0);
        let request = |east: f64| {
            LLBBox::new(
                0.0,
                proj.inverse(east - 100.0, 0.0).1,
                0.001,
                proj.inverse(east, 0.0).1,
            )
            .unwrap()
        };
        // The border is a chunk edge, so a request just inside snaps onto it.
        assert_eq!(WORLD_BORDER_BLOCKS as i32 % CHUNK_BLOCKS, 0);
        let (rect, _) = snap_bbox_to_chunks(&proj, &request(WORLD_BORDER_BLOCKS - 5.0)).unwrap();
        assert_eq!(rect.max_x() + 1, WORLD_BORDER_BLOCKS as i32);
        let err = snap_bbox_to_chunks(&proj, &request(WORLD_BORDER_BLOCKS + 5.0)).unwrap_err();
        assert!(err.contains("world border"), "{err}");
    }

    #[test]
    fn a_utm32_area_covers_the_request_and_its_bbox_covers_the_area() {
        let proj = utm32(1.0);
        // Slagelse town; UTM is turned about 1.9 degrees against the meridians here.
        let req = LLBBox::new(55.38167, 11.30991, 55.43070, 11.41153).unwrap();
        let (rect, eff) = snap_bbox_to_chunks(&proj, &req).unwrap();
        assert_eq!(rect.min_x().rem_euclid(16), 0);
        assert_eq!((rect.max_z() + 1).rem_euclid(16), 0);
        for (lat, lon) in [
            (req.min().lat(), req.min().lng()),
            (req.min().lat(), req.max().lng()),
            (req.max().lat(), req.min().lng()),
            (req.max().lat(), req.max().lng()),
        ] {
            let (x, z) = proj.forward(lat, lon);
            assert!(x >= rect.min_x() as f64 && x <= rect.max_x() as f64 + 1.0);
            assert!(z >= rect.min_z() as f64 && z <= rect.max_z() as f64 + 1.0);
        }
        for (x, z) in [
            (rect.min_x(), rect.min_z()),
            (rect.max_x() + 1, rect.min_z()),
            (rect.min_x(), rect.max_z() + 1),
            (rect.max_x() + 1, rect.max_z() + 1),
        ] {
            let (lat, lon) = proj.inverse(x as f64, z as f64);
            assert!(lat >= eff.min().lat() && lat <= eff.max().lat());
            assert!(lon >= eff.min().lng() && lon <= eff.max().lng());
        }
        // The town sits at X = E - 600000, Z = 6200000 - N.
        assert!(rect.min_x() <= 49051 && rect.max_x() >= 49051);
        assert!(rect.min_z() <= 57773 && rect.max_z() >= 57773);
    }

    #[test]
    fn a_block_rect_is_kept_exactly_in_utm32() {
        let rect = snap_rect_to_chunks([48128, 56832, 49151, 57855]).unwrap();
        assert_eq!(
            (rect.min_x(), rect.min_z(), rect.max_x(), rect.max_z()),
            (48128, 56832, 49151, 57855)
        );
        let grown = snap_rect_to_chunks([-5, 17, 0, 31]).unwrap();
        assert_eq!(
            (grown.min_x(), grown.min_z(), grown.max_x(), grown.max_z()),
            (-16, 16, 15, 31)
        );
        assert!(snap_rect_to_chunks([10, 0, 9, 0]).is_err());

        let spec = ProjectionSpec {
            kind: ProjectionKind::Utm32,
            origin: None,
            scale: 1.0,
            clip_pad: 0,
            rect: Some([48128, 56832, 49151, 57855]),
        };
        let eff = llbbox_for_rect(&utm32(1.0), &rect).unwrap();
        let (t, again) = spec.transformer(&eff).unwrap();
        assert_eq!(
            (again.min_x(), again.min_z(), again.max_x(), again.max_z()),
            (48128, 56832, 49151, 57855)
        );
        let p = t.transform_point(
            crate::coordinate_system::geographic::LLPoint::new(55.4036, 11.354).unwrap(),
        );
        assert_eq!((p.x, p.z), (49051, 57773));
    }

    #[test]
    fn neighbouring_utm32_rects_share_their_edge() {
        let proj = utm32(1.0);
        let west = XZBBox::rect_from_min_max(0, 0, 1023, 1023).unwrap();
        let east = XZBBox::rect_from_min_max(1024, 0, 2047, 1023).unwrap();
        let (bw, be) = (
            llbbox_for_rect(&proj, &west).unwrap(),
            llbbox_for_rect(&proj, &east).unwrap(),
        );
        // In UTM the boxes overlap a little; the shared block edge lies in both.
        for z in [0.0, 512.0, 1024.0] {
            let (lat, lon) = proj.inverse(1024.0, z);
            for b in [&bw, &be] {
                assert!(lon >= b.min().lng() && lon <= b.max().lng());
                assert!(lat >= b.min().lat() && lat <= b.max().lat());
            }
        }
    }

    #[test]
    fn snap_edge_absorbs_float_noise_only() {
        assert_eq!(snap_edge(16.0000000001, false), 16);
        assert_eq!(snap_edge(15.9999999999, false), 16);
        assert_eq!(snap_edge(15.9999999999, true), 16);
        assert_eq!(snap_edge(15.4, false), 15);
        assert_eq!(snap_edge(15.4, true), 16);
        assert_eq!(snap_edge(-3.7, false), -4);
        assert_eq!(snap_edge(-3.7, true), -3);
    }

    #[test]
    fn a_one_world_fetch_covers_the_clip_margin() {
        let spec = ProjectionSpec {
            kind: ProjectionKind::Utm32,
            origin: None,
            scale: 1.0,
            clip_pad: 64,
            rect: Some([48128, 57344, 49151, 58367]),
        };
        let area = llbbox_for_rect(
            &utm32(1.0),
            &XZBBox::rect_from_min_max(48128, 57344, 49151, 58367).unwrap(),
        )
        .unwrap();
        let fetch = spec.fetch_bbox(&area);
        for (x, z) in [(48064.0, 57280.0), (49216.0, 58432.0)] {
            let (lat, lon) = utm32(1.0).inverse(x, z);
            assert!(lat >= fetch.min().lat() && lat <= fetch.max().lat());
            assert!(lon >= fetch.min().lng() && lon <= fetch.max().lng());
        }
        assert_eq!(ProjectionSpec::local(1.0).fetch_bbox(&area), area);
    }

    #[test]
    fn clip_bbox_grows_by_the_pad() {
        let spec = ProjectionSpec {
            kind: ProjectionKind::WebMercator,
            origin: Some((0.0, 0.0)),
            scale: 1.0,
            clip_pad: 64,
            rect: None,
        };
        let rect = XZBBox::rect_from_min_max(-160, 0, 159, 319).unwrap();
        let clip = spec.clip_bbox(&rect);
        assert_eq!(
            (clip.min_x(), clip.min_z(), clip.max_x(), clip.max_z()),
            (-224, -64, 223, 383)
        );
        assert_eq!(ProjectionSpec::local(1.0).clip_bbox(&rect).min_x(), -160);
    }
}
