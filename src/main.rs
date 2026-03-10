#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

mod args;
#[cfg(feature = "bedrock")]
mod bedrock_block_map;
mod block_definitions;
mod bresenham;
mod clipping;
mod colors;
mod coordinate_system;
mod data_processing;
mod deterministic_rng;
mod dhm;
mod element_processing;
mod elevation_data;
mod floodfill;
mod floodfill_cache;
mod ground;
mod land_polygons;
mod large_area;
mod map_renderer;
mod map_transformation;
mod osm_parser;
#[cfg(feature = "gui")]
mod progress;
mod retrieve_data;
#[cfg(feature = "gui")]
mod telemetry;
#[cfg(test)]
mod test_utilities;
mod urban_ground;
mod version_check;
mod world_editor;
mod world_utils;

use args::Args;
use clap::Parser;
use colored::*;
use coordinate_system::transformation::CoordTransformer;
use std::path::PathBuf;
use std::{env, fs, io::Write};
use world_editor::WorldFormat;

#[cfg(feature = "gui")]
mod gui;

// If the user does not want the GUI, it's easiest to just mock the progress module to do nothing
#[cfg(not(feature = "gui"))]
mod progress {
    pub fn emit_gui_error(_message: &str) {}
    pub fn emit_gui_progress_update(_progress: f64, _message: &str) {}
    pub fn emit_map_preview_ready() {}
    pub fn emit_open_mcworld_file(_path: &str) {}
    pub fn is_running_with_gui() -> bool {
        false
    }
}
#[cfg(target_os = "windows")]
use windows::Win32::System::Console::{AttachConsole, FreeConsole, ATTACH_PARENT_PROCESS};

fn tile_output_path(
    base_path: Option<&str>,
    tile_index: usize,
    total_tiles: usize,
) -> Option<String> {
    let base_path = base_path?;
    if total_tiles <= 1 {
        return Some(base_path.to_string());
    }

    if let Some(dot_index) = base_path.rfind('.') {
        Some(format!(
            "{}-tile-{:02}-of-{:02}{}",
            &base_path[..dot_index],
            tile_index,
            total_tiles,
            &base_path[dot_index..]
        ))
    } else {
        Some(format!(
            "{}-tile-{:02}-of-{:02}",
            base_path, tile_index, total_tiles
        ))
    }
}

fn write_debug_osm_dump(
    parsed_elements: &[osm_parser::ProcessedElement],
    tile_index: usize,
    total_tiles: usize,
) {
    let filename = if total_tiles > 1 {
        format!("parsed_osm_data_tile_{tile_index:02}_of_{total_tiles:02}.txt")
    } else {
        "parsed_osm_data.txt".to_string()
    };

    let mut buf =
        std::io::BufWriter::new(fs::File::create(&filename).expect("Failed to create output file"));
    for element in parsed_elements {
        writeln!(
            buf,
            "Element ID: {}, Type: {}, Tags: {:?}",
            element.id(),
            element.kind(),
            element.tags(),
        )
        .expect("Failed to write to output file");
    }
}

fn run_cli_job(
    args: &Args,
    job_bbox: coordinate_system::geographic::LLBBox,
    target_xzbbox: Option<coordinate_system::cartesian::XZBBox>,
    full_transformer: Option<&CoordTransformer>,
    generation_path: &PathBuf,
    world_format: WorldFormat,
    level_name: Option<String>,
    tile_index: usize,
    total_tiles: usize,
    save_json_path: Option<&str>,
) -> Result<(), String> {
    let raw_data = match &args.file {
        Some(file) => retrieve_data::fetch_data_from_file(file).map_err(|e| e.to_string())?,
        None => retrieve_data::fetch_data_from_overpass(
            job_bbox,
            args.debug,
            args.downloader.as_str(),
            save_json_path,
        )
        .map_err(|e| e.to_string())?,
    };

    let mut job_args = Args {
        bbox: job_bbox,
        file: args.file.clone(),
        land_polygons: args.land_polygons.clone(),
        save_json_file: save_json_path.map(str::to_string),
        path: Some(generation_path.clone()),
        bedrock: args.bedrock,
        downloader: args.downloader.clone(),
        scale: args.scale,
        ground_level: args.ground_level,
        terrain: args.terrain,
        interior: args.interior,
        roof: args.roof,
        fillground: args.fillground,
        city_boundaries: args.city_boundaries,
        dhm_token: args.dhm_token.clone(),
        debug: args.debug,
        timeout: args.timeout,
    };

    let mut ground = ground::generate_ground_data(&job_args);

    let (mut parsed_elements, mut xzbbox) = match (target_xzbbox, full_transformer) {
        (Some(tile_xzbbox), Some(transformer)) => osm_parser::parse_osm_data_with_transformer(
            raw_data,
            transformer,
            tile_xzbbox,
            args.debug,
        ),
        _ => osm_parser::parse_osm_data(raw_data, job_bbox, args.scale, args.debug),
    };
    parsed_elements
        .sort_by_key(|element: &osm_parser::ProcessedElement| osm_parser::get_priority(element));

    if args.debug {
        write_debug_osm_dump(&parsed_elements, tile_index, total_tiles);
    }

    map_transformation::transform_map(&mut parsed_elements, &mut xzbbox, &mut ground);

    let generation_options = data_processing::GenerationOptions {
        path: generation_path.clone(),
        format: world_format,
        level_name,
        spawn_point: None,
        update_spawn_after_generation: true,
    };

    data_processing::generate_world_with_options(
        parsed_elements,
        xzbbox,
        job_bbox,
        ground,
        &job_args,
        generation_options,
    )?;

    // Keep the args path pointed at the generated world in case GUI-specific code is compiled in.
    job_args.path = Some(generation_path.clone());

    Ok(())
}

fn run_cli() {
    // Configure thread pool with 90% CPU cap to keep system responsive
    floodfill_cache::configure_rayon_thread_pool(0.9);

    // Clean up old cached elevation tiles on startup
    elevation_data::cleanup_old_cached_tiles();

    let version: &str = env!("CARGO_PKG_VERSION");
    let repository: &str = env!("CARGO_PKG_REPOSITORY");
    println!(
        r#"
        ▄████████    ▄████████ ███▄▄▄▄    ▄█     ▄████████
        ███    ███   ███    ███ ███▀▀▀██▄ ███    ███    ███
        ███    ███   ███    ███ ███   ███ ███▌   ███    █▀
        ███    ███  ▄███▄▄▄▄██▀ ███   ███ ███▌   ███
      ▀███████████ ▀▀███▀▀▀▀▀   ███   ███ ███▌ ▀███████████
        ███    ███ ▀███████████ ███   ███ ███           ███
        ███    ███   ███    ███ ███   ███ ███     ▄█    ███
        ███    █▀    ███    ███  ▀█   █▀  █▀    ▄████████▀
                     ███    ███

                          version {}
                {}
        "#,
        version,
        repository.bright_white().bold()
    );

    if let Err(e) = version_check::check_for_updates() {
        eprintln!(
            "{}: {}",
            "Error checking for version updates".red().bold(),
            e
        );
    }

    let args: Args = Args::parse();

    if let Err(e) = args::validate_args(&args) {
        eprintln!("{}: {}", "Error".red().bold(), e);
        std::process::exit(1);
    }

    if args.bedrock && !cfg!(feature = "bedrock") {
        eprintln!(
            "{}: The --bedrock flag requires the 'bedrock' feature. Rebuild with: cargo build --features bedrock",
            "Error".red().bold()
        );
        std::process::exit(1);
    }

    let world_format = if args.bedrock {
        WorldFormat::BedrockMcWorld
    } else {
        WorldFormat::JavaAnvil
    };

    let (generation_path, level_name) = if args.bedrock {
        let output_dir = args
            .path
            .clone()
            .unwrap_or_else(world_utils::get_bedrock_output_directory);
        let (output_path, lvl_name) = world_utils::build_bedrock_output(&args.bbox, output_dir);
        (output_path, Some(lvl_name))
    } else {
        let base_dir = args.path.clone().unwrap();
        let world_path = match world_utils::create_new_world(&base_dir) {
            Ok(path) => PathBuf::from(path),
            Err(e) => {
                eprintln!("{} {}", "Error:".red().bold(), e);
                std::process::exit(1);
            }
        };
        println!(
            "Created new world at: {}",
            world_path.display().to_string().bright_white().bold()
        );
        (world_path, None)
    };

    if !args.bedrock {
        let max_job_dimension = large_area::MAX_JOB_DIMENSION_BLOCKS;
        let plan = match large_area::build_generation_plan(args.bbox, args.scale) {
            Ok(plan) => plan,
            Err(e) => {
                eprintln!("{} {}", "Error:".red().bold(), e);
                std::process::exit(1);
            }
        };
        let (full_transformer, _) = CoordTransformer::llbbox_to_xzbbox(&args.bbox, args.scale)
            .expect("Failed to build full-area coordinate transformer");

        if plan.requires_tiling() {
            println!(
                "{} Splitting selection into {} jobs (max tile: {} x {} blocks, full bounds: {} x {} blocks)",
                "Info:".bright_white().bold(),
                plan.tiles.len(),
                max_job_dimension,
                max_job_dimension,
                plan.full_xzbbox.bounding_rect().total_blocks_x(),
                plan.full_xzbbox.bounding_rect().total_blocks_z()
            );
        }

        for tile in &plan.tiles {
            if plan.requires_tiling() {
                println!(
                    "{} Generating tile {}/{}...",
                    "[tile]".bold(),
                    tile.index,
                    tile.total
                );
            }

            let save_json_path =
                tile_output_path(args.save_json_file.as_deref(), tile.index, tile.total);
            if let Err(e) = run_cli_job(
                &args,
                tile.llbbox,
                Some(tile.xzbbox.clone()),
                Some(&full_transformer),
                &generation_path,
                world_format,
                level_name.clone(),
                tile.index,
                tile.total,
                save_json_path.as_deref(),
            ) {
                eprintln!("{} {}", "Error:".red().bold(), e);
                std::process::exit(1);
            }
        }
    } else if let Err(e) = run_cli_job(
        &args,
        args.bbox,
        None,
        None,
        &generation_path,
        world_format,
        level_name,
        1,
        1,
        args.save_json_file.as_deref(),
    ) {
        eprintln!("{} {}", "Error:".red().bold(), e);
        std::process::exit(1);
    }

    if args.bedrock {
        println!(
            "{} Bedrock world saved to: {}",
            "Done!".green().bold(),
            generation_path.display()
        );
    }
}

fn main() {
    #[cfg(target_os = "windows")]
    unsafe {
        let _ = FreeConsole();
        let _ = AttachConsole(ATTACH_PARENT_PROCESS);
    }

    #[cfg(feature = "gui")]
    {
        let gui_mode = std::env::args().len() == 1;
        if gui_mode {
            gui::run_gui();
        }
    }

    run_cli();
}
