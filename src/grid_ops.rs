//! In-place row resampling and cropping for the ground grids.

use crate::coordinate_system::geographic::LLBBox;
use crate::projection::web_mercator::{mercator_lat_deg, mercator_y_m};
use rayon::prelude::*;

/// Output cells between the points where `BlockSampler` runs the inverse
/// projection. Over 16 blocks UTM bends by far less than a millimetre.
const LATTICE: usize = 16;

/// Where each cell of a block-aligned grid reads a grid that was fetched at
/// equal lat/lon steps, for a frame whose block lines are not parallels and
/// meridians (UTM). Positions are fractional source columns and rows.
pub struct BlockSampler {
    pub out_w: usize,
    pub out_h: usize,
    lattice_w: usize,
    cols: Vec<f64>,
    rows: Vec<f64>,
}

impl BlockSampler {
    /// `geo(i, j)` is the latitude and longitude of output cell `(i, j)`. The
    /// source grid has `src_w` x `src_h` samples spanning `src_bbox` edge to edge.
    pub fn new(
        (out_w, out_h): (usize, usize),
        (src_w, src_h): (usize, usize),
        src_bbox: &LLBBox,
        geo: impl Fn(f64, f64) -> (f64, f64) + Sync,
    ) -> Self {
        let (out_w, out_h) = (out_w.max(1), out_h.max(1));
        // One node past the last cell, so every cell lies between two nodes.
        let lattice_w = (out_w - 1) / LATTICE + 2;
        let lattice_h = (out_h - 1) / LATTICE + 2;
        let (north, west) = (src_bbox.max().lat(), src_bbox.min().lng());
        let lat_span = north - src_bbox.min().lat();
        let lon_span = src_bbox.max().lng() - west;
        let col_per_deg = (src_w.max(2) - 1) as f64 / lon_span;
        let row_per_deg = (src_h.max(2) - 1) as f64 / lat_span;
        let nodes: Vec<(f64, f64)> = (0..lattice_w * lattice_h)
            .into_par_iter()
            .map(|n| {
                let (a, b) = (n % lattice_w, n / lattice_w);
                let (lat, lon) = geo((a * LATTICE) as f64, (b * LATTICE) as f64);
                ((lon - west) * col_per_deg, (north - lat) * row_per_deg)
            })
            .collect();
        Self {
            out_w,
            out_h,
            lattice_w,
            cols: nodes.iter().map(|n| n.0).collect(),
            rows: nodes.iter().map(|n| n.1).collect(),
        }
    }

    /// Source column and row of output cell `(i, j)`.
    pub fn at(&self, i: usize, j: usize) -> (f64, f64) {
        let (a, b) = (i / LATTICE, j / LATTICE);
        let tx = (i % LATTICE) as f64 / LATTICE as f64;
        let tz = (j % LATTICE) as f64 / LATTICE as f64;
        let k = b * self.lattice_w + a;
        let blend = |v: &[f64]| {
            let top = v[k] + (v[k + 1] - v[k]) * tx;
            let k2 = k + self.lattice_w;
            let bottom = v[k2] + (v[k2 + 1] - v[k2]) * tx;
            top + (bottom - top) * tz
        };
        (blend(&self.cols), blend(&self.rows))
    }

    /// The output grid in rows, each cell read with `pick(column, row)`.
    pub fn rows<T: Send>(&self, pick: impl Fn(f64, f64) -> T + Sync) -> Vec<Vec<T>> {
        (0..self.out_h)
            .into_par_iter()
            .map(|j| {
                (0..self.out_w)
                    .map(|i| {
                        let (c, r) = self.at(i, j);
                        pick(c, r)
                    })
                    .collect()
            })
            .collect()
    }

    /// `rows`, flattened row-major.
    pub fn flat<T: Send>(&self, pick: impl Fn(f64, f64) -> T + Sync) -> Vec<T> {
        self.rows(pick).into_iter().flatten().collect()
    }
}

/// The nearest cell to a fractional position, clamped to the grid.
pub fn nearest_index(col: f64, row: f64, width: usize, height: usize) -> (usize, usize) {
    let clamp = |v: f64, len: usize| (v.round().max(0.0) as usize).min(len.saturating_sub(1));
    (clamp(col, width), clamp(row, height))
}

/// Bilinear height at a fractional position; a cell next to a gap takes the
/// nearest value instead of blending into it.
pub fn bilinear(grid: &[Vec<f32>], col: f64, row: f64) -> f32 {
    let height = grid.len();
    let width = grid.first().map_or(0, Vec::len);
    if width == 0 {
        return f32::NAN;
    }
    let c = col.clamp(0.0, (width - 1) as f64);
    let r = row.clamp(0.0, (height - 1) as f64);
    let (x0, z0) = (c.floor() as usize, r.floor() as usize);
    let (x1, z1) = ((x0 + 1).min(width - 1), (z0 + 1).min(height - 1));
    let (tx, tz) = (c - x0 as f64, r - z0 as f64);
    let corners = [grid[z0][x0], grid[z0][x1], grid[z1][x0], grid[z1][x1]];
    if corners.iter().any(|v| !v.is_finite()) {
        let (x, z) = nearest_index(c, r, width, height);
        return grid[z][x];
    }
    let [a, b, d, e] = corners.map(f64::from);
    let top = a + (b - a) * tx;
    let bottom = d + (e - d) * tx;
    (top + (bottom - top) * tz) as f32
}

/// Fractional source row for output row `gz`, when rows sampled at equal
/// latitude steps are re-spaced to equal Mercator steps over the same span.
pub fn mercator_source_row(lat_top: f64, lat_bottom: f64, rows: usize, gz: usize) -> f64 {
    if rows < 2 {
        return 0.0;
    }
    let y_top = mercator_y_m(lat_top);
    let y_bottom = mercator_y_m(lat_bottom);
    let t = gz as f64 / (rows - 1) as f64;
    let lat = mercator_lat_deg(y_top + (y_bottom - y_top) * t);
    let lat_span = lat_top - lat_bottom;
    if lat_span.abs() < 1e-15 {
        return gz as f64;
    }
    ((lat_top - lat) / lat_span * (rows - 1) as f64).clamp(0.0, (rows - 1) as f64)
}

/// Order that lets rows be rewritten in place: `Some(true)` descending (every
/// row reads at or above itself), `Some(false)` ascending, `None` neither.
fn in_place_order(src: &dyn Fn(usize) -> f64, rows: usize) -> Option<bool> {
    let mut above = true;
    let mut below = true;
    for gz in 0..rows {
        let s = src(gz);
        if s > gz as f64 + 1e-9 {
            above = false;
        }
        if s < gz as f64 - 1e-9 {
            below = false;
        }
    }
    if above {
        Some(true)
    } else if below {
        Some(false)
    } else {
        None
    }
}

/// Output row `gz` becomes source row `src(gz)`, blended with `lerp`.
pub fn remap_rows_in_place<T: Copy>(
    grid: &mut Vec<Vec<T>>,
    src: impl Fn(usize) -> f64,
    lerp: impl Fn(T, T, f64) -> T,
) {
    let rows = grid.len();
    if rows < 2 {
        return;
    }
    let build = |grid: &Vec<Vec<T>>, gz: usize| -> Vec<T> {
        let s = src(gz).clamp(0.0, (rows - 1) as f64);
        let a = s.floor() as usize;
        let t = s - a as f64;
        let b = (a + 1).min(rows - 1);
        if t <= 1e-9 || a == b {
            grid[a].clone()
        } else {
            grid[a]
                .iter()
                .zip(grid[b].iter())
                .map(|(&x, &y)| lerp(x, y, t))
                .collect()
        }
    };
    match in_place_order(&|gz| src(gz), rows) {
        Some(true) => {
            for gz in (0..rows).rev() {
                let row = build(grid, gz);
                grid[gz] = row;
            }
        }
        Some(false) => {
            for gz in 0..rows {
                let row = build(grid, gz);
                grid[gz] = row;
            }
        }
        None => {
            let fresh: Vec<Vec<T>> = (0..rows).map(|gz| build(grid, gz)).collect();
            *grid = fresh;
        }
    }
}

pub fn remap_flat_rows_nearest<T: Copy>(
    grid: &mut Vec<T>,
    width: usize,
    src: impl Fn(usize) -> f64,
) {
    if width == 0 {
        return;
    }
    let rows = grid.len() / width;
    if rows < 2 {
        return;
    }
    let pick = |gz: usize| (src(gz).round() as usize).min(rows - 1);
    let mv = |grid: &mut Vec<T>, from: usize, to: usize| {
        if from != to {
            grid.copy_within(from * width..(from + 1) * width, to * width);
        }
    };
    match in_place_order(&|gz| src(gz), rows) {
        Some(true) => {
            for gz in (0..rows).rev() {
                mv(grid, pick(gz), gz);
            }
        }
        Some(false) => {
            for gz in 0..rows {
                mv(grid, pick(gz), gz);
            }
        }
        None => {
            let mut fresh = Vec::with_capacity(grid.len());
            for gz in 0..rows {
                let s = pick(gz);
                fresh.extend_from_slice(&grid[s * width..(s + 1) * width]);
            }
            *grid = fresh;
        }
    }
}

pub fn crop_rows<T>(grid: &mut Vec<Vec<T>>, x0: usize, z0: usize, width: usize, height: usize) {
    if z0 > 0 {
        grid.drain(..z0.min(grid.len()));
    }
    grid.truncate(height);
    for row in grid.iter_mut() {
        if x0 > 0 {
            row.drain(..x0.min(row.len()));
        }
        row.truncate(width);
        row.shrink_to_fit();
    }
    grid.shrink_to_fit();
}

pub fn crop_flat<T: Copy>(
    grid: &mut Vec<T>,
    src_width: usize,
    x0: usize,
    z0: usize,
    width: usize,
    height: usize,
) {
    if src_width == 0 {
        return;
    }
    let src_rows = grid.len() / src_width;
    let height = height.min(src_rows.saturating_sub(z0));
    let width = width.min(src_width.saturating_sub(x0));
    for r in 0..height {
        let from = (z0 + r) * src_width + x0;
        grid.copy_within(from..from + width, r * width);
    }
    grid.truncate(width * height);
    grid.shrink_to_fit();
}

#[cfg(test)]
mod tests {
    use super::*;

    fn lerp_f(a: f32, b: f32, t: f64) -> f32 {
        (a as f64 * (1.0 - t) + b as f64 * t) as f32
    }

    #[test]
    fn mercator_rows_sit_north_of_equirectangular_rows_in_the_north() {
        let s = mercator_source_row(48.2, 48.0, 101, 50);
        assert!(s < 50.0, "{s}");
        assert!(s > 49.0, "{s}");
        assert_eq!(mercator_source_row(48.2, 48.0, 101, 0), 0.0);
        assert!((mercator_source_row(48.2, 48.0, 101, 100) - 100.0).abs() < 1e-9);
        let s = mercator_source_row(-48.0, -48.2, 101, 50);
        assert!(s > 50.0, "{s}");
    }

    #[test]
    fn remap_is_the_identity_for_an_identity_mapping() {
        let mut g: Vec<Vec<f32>> = (0..5).map(|z| vec![z as f32; 3]).collect();
        let before = g.clone();
        remap_rows_in_place(&mut g, |gz| gz as f64, lerp_f);
        assert_eq!(g, before);
    }

    #[test]
    fn remap_blends_rows_and_keeps_the_edges() {
        let mut g: Vec<Vec<f32>> = (0..5).map(|z| vec![z as f32 * 10.0; 2]).collect();
        remap_rows_in_place(
            &mut g,
            |gz| {
                if gz == 0 || gz == 4 {
                    gz as f64
                } else {
                    gz as f64 - 0.5
                }
            },
            lerp_f,
        );
        assert_eq!(g[0], vec![0.0, 0.0]);
        assert_eq!(g[4], vec![40.0, 40.0]);
        assert!((g[2][0] - 15.0).abs() < 1e-6, "{:?}", g[2]);
        assert!((g[1][0] - 5.0).abs() < 1e-6, "{:?}", g[1]);
    }

    #[test]
    fn in_place_remap_matches_a_buffered_remap_in_both_hemispheres() {
        for (top, bottom) in [(48.2, 48.0), (-48.0, -48.2), (0.1, -0.1)] {
            let rows = 41;
            let g: Vec<Vec<f32>> = (0..rows).map(|z| vec![(z * z) as f32; 3]).collect();
            let src = |gz: usize| mercator_source_row(top, bottom, rows, gz);
            let expected: Vec<Vec<f32>> = (0..rows)
                .map(|gz| {
                    let s = src(gz);
                    let a = s.floor() as usize;
                    let b = (a + 1).min(rows - 1);
                    g[a].iter()
                        .zip(&g[b])
                        .map(|(&x, &y)| lerp_f(x, y, s - a as f64))
                        .collect()
                })
                .collect();
            let mut got = g.clone();
            remap_rows_in_place(&mut got, src, lerp_f);
            for (r, (a, b)) in got.iter().zip(&expected).enumerate() {
                assert!(
                    (a[0] - b[0]).abs() < 1e-4,
                    "span {top}..{bottom} row {r}: {a:?} vs {b:?}"
                );
            }
        }
    }

    #[test]
    fn flat_nearest_remap_moves_whole_rows() {
        let width = 2;
        let mut g: Vec<u8> = (0..10).map(|i| (i / width) as u8).collect();
        remap_flat_rows_nearest(&mut g, width, |gz| (gz as f64 - 0.6).max(0.0));
        assert_eq!(g, vec![0, 0, 0, 0, 1, 1, 2, 2, 3, 3]);
    }

    #[test]
    fn the_sampler_reads_a_turned_grid_where_the_projection_says() {
        // Source: 101 x 101 samples over one degree, value = column + 1000 * row.
        let bbox = LLBBox::new(55.0, 11.0, 56.0, 12.0).unwrap();
        let grid: Vec<Vec<f32>> = (0..101)
            .map(|r| (0..101).map(|c| (c + 1000 * r) as f32).collect())
            .collect();
        // Output cells walk a line turned against the grid.
        let geo = |i: f64, j: f64| (55.9 - j * 0.01 + i * 0.001, 11.1 + i * 0.01 + j * 0.002);
        let s = BlockSampler::new((37, 41), (101, 101), &bbox, geo);
        for (i, j) in [(0, 0), (5, 3), (16, 16), (17, 33), (36, 40)] {
            let (lat, lon) = geo(i as f64, j as f64);
            let (c, r) = s.at(i, j);
            assert!((c - (lon - 11.0) * 100.0).abs() < 1e-6, "{i},{j}: {c}");
            assert!((r - (56.0 - lat) * 100.0).abs() < 1e-6, "{i},{j}: {r}");
            let v = bilinear(&grid, c, r);
            assert!((f64::from(v) - (c + 1000.0 * r)).abs() < 1e-2, "{v}");
        }
        let out = s.rows(|c, r| bilinear(&grid, c, r));
        assert_eq!((out.len(), out[0].len()), (41, 37));
        assert_eq!(s.flat(|c, _| c).len(), 37 * 41);
    }

    #[test]
    fn bilinear_steps_around_gaps_and_clamps_to_the_grid() {
        let grid = vec![vec![0.0, 10.0], vec![20.0, 30.0]];
        assert_eq!(bilinear(&grid, 0.5, 0.5), 15.0);
        assert_eq!(bilinear(&grid, 5.0, -3.0), 10.0);
        let gap = vec![vec![0.0, 10.0], vec![f32::NAN, 30.0]];
        assert_eq!(bilinear(&gap, 0.8, 0.4), 10.0);
        assert_eq!(nearest_index(-0.7, 9.0, 2, 2), (0, 1));
    }

    #[test]
    fn crop_keeps_the_window() {
        let mut g: Vec<Vec<u8>> = (0..6)
            .map(|z| (0..6).map(|x| (z * 10 + x) as u8).collect())
            .collect();
        crop_rows(&mut g, 2, 1, 3, 4);
        assert_eq!(g.len(), 4);
        assert_eq!(g[0], vec![12, 13, 14]);
        assert_eq!(g[3], vec![42, 43, 44]);

        let mut f: Vec<u8> = (0..36).map(|i| ((i / 6) * 10 + i % 6) as u8).collect();
        crop_flat(&mut f, 6, 2, 1, 3, 4);
        assert_eq!(f, vec![12, 13, 14, 22, 23, 24, 32, 33, 34, 42, 43, 44]);
    }
}
