use crate::coordinate_system::cartesian::{XZBBox, XZVector};
use crate::coordinate_system::geographic::LLBBox;
use crate::coordinate_system::transformation::CoordTransformer;

pub const MAX_JOB_DIMENSION_BLOCKS: u32 = 10_000;

#[derive(Clone, Debug)]
pub struct GenerationTile {
    pub llbbox: LLBBox,
    pub xzbbox: XZBBox,
    pub index: usize,
    pub total: usize,
}

#[derive(Clone, Debug)]
pub struct GenerationPlan {
    pub full_xzbbox: XZBBox,
    pub tiles: Vec<GenerationTile>,
}

impl GenerationPlan {
    pub fn requires_tiling(&self) -> bool {
        self.tiles.len() > 1
    }
}

pub fn build_generation_plan(full_bbox: LLBBox, scale: f64) -> Result<GenerationPlan, String> {
    build_generation_plan_with_limit(full_bbox, scale, MAX_JOB_DIMENSION_BLOCKS)
}

pub fn build_generation_plan_with_limit(
    full_bbox: LLBBox,
    scale: f64,
    max_job_dimension_blocks: u32,
) -> Result<GenerationPlan, String> {
    let (_, full_xzbbox) = CoordTransformer::llbbox_to_xzbbox(&full_bbox, scale)?;
    let full_rect = full_xzbbox.bounding_rect();
    let total_blocks_x = full_rect.total_blocks_x();
    let total_blocks_z = full_rect.total_blocks_z();

    if total_blocks_x <= max_job_dimension_blocks && total_blocks_z <= max_job_dimension_blocks {
        return Ok(GenerationPlan {
            full_xzbbox: full_xzbbox.clone(),
            tiles: vec![GenerationTile {
                llbbox: full_bbox,
                xzbbox: full_xzbbox,
                index: 1,
                total: 1,
            }],
        });
    }

    let tiles_x = total_blocks_x.div_ceil(max_job_dimension_blocks);
    let tiles_z = total_blocks_z.div_ceil(max_job_dimension_blocks);
    let total_tiles = (tiles_x * tiles_z) as usize;

    let full_lat_span = full_bbox.max().lat() - full_bbox.min().lat();
    let full_lng_span = full_bbox.max().lng() - full_bbox.min().lng();

    let mut tiles = Vec::with_capacity(total_tiles);
    let mut tile_index = 1usize;

    for tile_z in 0..tiles_z {
        let start_block_z = tile_z * max_job_dimension_blocks;
        let end_block_z = ((tile_z + 1) * max_job_dimension_blocks).min(total_blocks_z);
        let z_block_count = end_block_z - start_block_z;

        let north_ratio = start_block_z as f64 / total_blocks_z as f64;
        let south_ratio = end_block_z as f64 / total_blocks_z as f64;
        let max_lat = full_bbox.max().lat() - north_ratio * full_lat_span;
        let min_lat = full_bbox.max().lat() - south_ratio * full_lat_span;

        for tile_x in 0..tiles_x {
            let start_block_x = tile_x * max_job_dimension_blocks;
            let end_block_x = ((tile_x + 1) * max_job_dimension_blocks).min(total_blocks_x);
            let x_block_count = end_block_x - start_block_x;

            let west_ratio = start_block_x as f64 / total_blocks_x as f64;
            let east_ratio = end_block_x as f64 / total_blocks_x as f64;
            let min_lng = full_bbox.min().lng() + west_ratio * full_lng_span;
            let max_lng = full_bbox.min().lng() + east_ratio * full_lng_span;

            let llbbox = LLBBox::new(min_lat, min_lng, max_lat, max_lng)?;

            let tile_xzbbox = if x_block_count == 0 || z_block_count == 0 {
                return Err("Generated an empty tile while splitting a large area".to_string());
            } else {
                XZBBox::rect_from_xz_lengths(
                    (x_block_count - 1) as f64,
                    (z_block_count - 1) as f64,
                )? + XZVector {
                    dx: start_block_x as i32,
                    dz: start_block_z as i32,
                }
            };

            tiles.push(GenerationTile {
                llbbox,
                xzbbox: tile_xzbbox,
                index: tile_index,
                total: total_tiles,
            });
            tile_index += 1;
        }
    }

    Ok(GenerationPlan { full_xzbbox, tiles })
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn large_plan_splits_into_chunk_aligned_tiles() {
        let bbox = LLBBox::new(55.0, 12.0, 55.2, 12.3).unwrap();
        let plan = build_generation_plan(bbox, 4.0).unwrap();

        assert!(plan.requires_tiling());
        assert!(plan.tiles.len() > 1);

        for tile in &plan.tiles {
            let rect = tile.xzbbox.bounding_rect();
            assert_eq!(rect.min().x % 16, 0);
            assert_eq!(rect.min().z % 16, 0);
        }

        let first = plan.tiles.first().unwrap().xzbbox.bounding_rect();
        let last = plan.tiles.last().unwrap().xzbbox.bounding_rect();
        assert_eq!(first.min().x, 0);
        assert_eq!(first.min().z, 0);
        assert_eq!(last.max().x, plan.full_xzbbox.max_x());
        assert_eq!(last.max().z, plan.full_xzbbox.max_z());
    }
}
