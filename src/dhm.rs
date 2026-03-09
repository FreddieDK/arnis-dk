use crate::coordinate_system::geographic::LLBBox;
use crate::coordinate_system::transformation::geo_distance;
use crate::elevation_data::ElevationData;
use crate::progress::emit_gui_progress_update;
use colored::Colorize;
use reqwest::blocking::Client;
use std::time::Duration;

/// Convert WGS84 (lat, lon) to ETRS89/UTM32N (easting, northing).
fn wgs84_to_utm32n(lat: f64, lon: f64) -> (f64, f64) {
    let a = 6378137.0_f64;
    let f = 1.0 / 298.257223563;
    let k0 = 0.9996;
    let lon0 = 9.0_f64;

    let e2 = 2.0 * f - f * f;
    let e_prime2 = e2 / (1.0 - e2);

    let lat_rad = lat.to_radians();
    let lon_rad = lon.to_radians();
    let lon0_rad = lon0.to_radians();

    let n = a / (1.0 - e2 * lat_rad.sin().powi(2)).sqrt();
    let t = lat_rad.tan();
    let c = e_prime2 * lat_rad.cos().powi(2);
    let a_coeff = (lon_rad - lon0_rad) * lat_rad.cos();

    let e2_2 = e2 * e2;
    let e2_3 = e2_2 * e2;
    let m = a
        * ((1.0 - e2 / 4.0 - 3.0 * e2_2 / 64.0 - 5.0 * e2_3 / 256.0) * lat_rad
            - (3.0 * e2 / 8.0 + 3.0 * e2_2 / 32.0 + 45.0 * e2_3 / 1024.0)
                * (2.0 * lat_rad).sin()
            + (15.0 * e2_2 / 256.0 + 45.0 * e2_3 / 1024.0) * (4.0 * lat_rad).sin()
            - (35.0 * e2_3 / 3072.0) * (6.0 * lat_rad).sin());

    let easting = k0
        * n
        * (a_coeff
            + (1.0 - t * t + c) * a_coeff.powi(3) / 6.0
            + (5.0 - 18.0 * t * t + t.powi(4) + 72.0 * c - 58.0 * e_prime2)
                * a_coeff.powi(5)
                / 120.0)
        + 500000.0;

    let northing = k0
        * (m + n
            * t
            * (a_coeff.powi(2) / 2.0
                + (5.0 - t * t + 9.0 * c + 4.0 * c * c) * a_coeff.powi(4) / 24.0
                + (61.0 - 58.0 * t * t + t.powi(4) + 600.0 * c - 330.0 * e_prime2)
                    * a_coeff.powi(6)
                    / 720.0));

    (easting, northing)
}

/// Fetch high-resolution elevation data from DHM via Dataforsyningen WCS.
/// Returns an ElevationData grid matching the Minecraft world dimensions.
pub fn fetch_dhm_elevation(
    bbox: &LLBBox,
    scale: f64,
    ground_level: i32,
    token: &str,
    debug: bool,
) -> Result<ElevationData, Box<dyn std::error::Error>> {
    println!("{}", "Fetching DHM high-resolution terrain...".bold());
    emit_gui_progress_update(12.0, "Fetching DHM terrain data...");

    let (base_scale_z, base_scale_x) = geo_distance(bbox.min(), bbox.max());
    let grid_width = (base_scale_x.floor() * scale) as usize;
    let grid_height = (base_scale_z.floor() * scale) as usize;

    if grid_width == 0 || grid_height == 0 {
        return Err("Grid dimensions are zero".into());
    }

    let (min_e, min_n) = wgs84_to_utm32n(bbox.min().lat(), bbox.min().lng());
    let (max_e, max_n) = wgs84_to_utm32n(bbox.max().lat(), bbox.max().lng());

    let req_width = grid_width.min(2048);
    let req_height = grid_height.min(2048);

    let url = format!(
        "https://api.dataforsyningen.dk/dhm_wcs_DAF?\
         SERVICE=WCS&REQUEST=GetCoverage&VERSION=1.0.0\
         &COVERAGE=dhm_terraen\
         &CRS=EPSG:25832&RESPONSE_CRS=EPSG:25832\
         &BBOX={min_e},{min_n},{max_e},{max_n}\
         &WIDTH={req_width}&HEIGHT={req_height}\
         &FORMAT=GTiff\
         &token={token}"
    );

    let client = Client::builder()
        .timeout(Duration::from_secs(120))
        .build()?;

    let quick_retries = 3;
    let max_rounds = 5;
    let mut last_err = String::new();
    let (content_type, bytes) = 'retry: {
        for round in 1..=max_rounds {
            if round > 1 {
                println!(
                    "DHM still failing after round {}. Waiting 60s before next round...",
                    round - 1
                );
                std::thread::sleep(Duration::from_secs(60));
            }

            for attempt in 1..=quick_retries {
                if attempt > 1 {
                    let delay = Duration::from_secs(2u64.pow(attempt as u32 - 1));
                    println!(
                        "DHM request round {}/{} attempt {}/{} (retrying in {}s)...",
                        round,
                        max_rounds,
                        attempt,
                        quick_retries,
                        delay.as_secs()
                    );
                    std::thread::sleep(delay);
                }

                let resp = match client.get(&url).send() {
                    Ok(r) => r,
                    Err(e) => {
                        last_err = format!("Request failed: {e}");
                        continue;
                    }
                };

                let status = resp.status();
                if status.is_server_error() || status == 429 {
                    let body = resp.text().unwrap_or_default();
                    last_err = format!(
                        "DHM WCS returned status {status}: {}",
                        &body[..body.len().min(500)]
                    );
                    continue;
                }

                if !status.is_success() {
                    let body = resp.text().unwrap_or_default();
                    if status == reqwest::StatusCode::FORBIDDEN && debug {
                        print_dhm_auth_debug(token, &url);
                    }
                    return Err(format!(
                        "DHM WCS returned status {status}: {}",
                        &body[..body.len().min(500)]
                    )
                    .into());
                }

                let ct = resp
                    .headers()
                    .get("content-type")
                    .and_then(|v| v.to_str().ok())
                    .unwrap_or("")
                    .to_string();

                match resp.bytes() {
                    Ok(b) => break 'retry (ct, b),
                    Err(e) => {
                        last_err = format!("Failed to read response body: {e}");
                        continue;
                    }
                }
            }
        }
        return Err(format!(
            "DHM WCS failed after {max_rounds} rounds of {quick_retries} attempts: {last_err}"
        )
        .into());
    };

    if content_type.contains("xml") || (bytes.len() > 5 && bytes[0] == b'<') {
        let text = String::from_utf8_lossy(&bytes[..bytes.len().min(500)]);
        return Err(format!("DHM WCS returned error: {text}").into());
    }

    println!(
        "Received {} bytes of DHM terrain data. Parsing...",
        bytes.len()
    );
    emit_gui_progress_update(15.0, "Processing DHM terrain...");

    let cursor = std::io::Cursor::new(bytes.to_vec());
    let mut decoder = tiff::decoder::Decoder::new(cursor)
        .map_err(|e| format!("Failed to decode GeoTIFF: {e}"))?;

    let (tiff_width, tiff_height) = decoder
        .dimensions()
        .map_err(|e| format!("Failed to read TIFF dimensions: {e}"))?;

    let image_data = decoder
        .read_image()
        .map_err(|e| format!("Failed to read TIFF image data: {e}"))?;

    let raw_heights: Vec<f64> = match image_data {
        tiff::decoder::DecodingResult::F32(data) => data.iter().map(|&v| v as f64).collect(),
        tiff::decoder::DecodingResult::F64(data) => data.to_vec(),
        tiff::decoder::DecodingResult::U8(data) => data.iter().map(|&v| v as f64).collect(),
        tiff::decoder::DecodingResult::U16(data) => data.iter().map(|&v| v as f64).collect(),
        tiff::decoder::DecodingResult::I16(data) => data.iter().map(|&v| v as f64).collect(),
        _ => return Err("Unsupported TIFF pixel format".into()),
    };

    println!(
        "DHM TIFF: {}x{} pixels, resampling to {}x{} grid...",
        tiff_width, tiff_height, grid_width, grid_height
    );

    let mut height_grid: Vec<Vec<f64>> = vec![vec![0.0; grid_width]; grid_height];
    let nodata = -9999.0;

    for gz in 0..grid_height {
        for gx in 0..grid_width {
            let tx = (gx as f64 / grid_width as f64 * tiff_width as f64) as usize;
            let tz = (gz as f64 / grid_height as f64 * tiff_height as f64) as usize;
            let tx = tx.min(tiff_width as usize - 1);
            let tz = tz.min(tiff_height as usize - 1);

            let idx = tz * tiff_width as usize + tx;
            let h = if idx < raw_heights.len() {
                raw_heights[idx]
            } else {
                0.0
            };

            height_grid[gz][gx] = if h <= nodata { 0.0 } else { h };
        }
    }

    let grid_size = (grid_width.min(grid_height) as f64).max(1.0);
    let sigma = 7.0 * (grid_size / 100.0).sqrt();
    println!("Smoothing DHM terrain (sigma={:.1})...", sigma);
    let height_grid = dhm_gaussian_blur(&height_grid, sigma);

    let mut min_h = f64::MAX;
    let mut max_h = f64::MIN;
    for row in &height_grid {
        for &h in row {
            min_h = min_h.min(h);
            max_h = max_h.max(h);
        }
    }

    let height_range = max_h - min_h;
    println!(
        "DHM elevation range: {:.1}m to {:.1}m ({:.1}m total)",
        min_h, max_h, height_range
    );

    const MAX_Y: i32 = 319;
    const TERRAIN_HEIGHT_BUFFER: i32 = 15;
    let available_y_range = (MAX_Y - TERRAIN_HEIGHT_BUFFER - ground_level) as f64;
    let ideal_scaled_range = height_range * scale;

    let scaled_range = if ideal_scaled_range <= available_y_range {
        ideal_scaled_range
    } else {
        let compression = available_y_range / height_range;
        height_range * compression
    };

    let sea_level_y = if height_range > 0.0 && min_h < 0.5 {
        let sea_relative = (0.0 - min_h) / height_range;
        let sea_scaled = sea_relative * scaled_range;
        Some(
            ((ground_level as f64 + sea_scaled).round() as i32)
                .clamp(ground_level, MAX_Y - TERRAIN_HEIGHT_BUFFER),
        )
    } else {
        None
    };

    let mc_heights: Vec<Vec<i32>> = height_grid
        .iter()
        .map(|row| {
            row.iter()
                .map(|&h| {
                    let relative = if height_range > 0.0 {
                        (h - min_h) / height_range
                    } else {
                        0.0
                    };
                    let scaled = relative * scaled_range;
                    ((ground_level as f64 + scaled).round() as i32)
                        .clamp(ground_level, MAX_Y - TERRAIN_HEIGHT_BUFFER)
                })
                .collect()
        })
        .collect();

    if let Some(sly) = sea_level_y {
        println!("DHM sea level at Minecraft Y={sly}");
    }

    println!(
        "{}",
        format!(
            "DHM terrain ready: {}x{} grid, {:.1}m range",
            grid_width, grid_height, height_range
        )
        .green()
    );

    Ok(ElevationData {
        heights: mc_heights,
        width: grid_width,
        height: grid_height,
        sea_level_y,
    })
}

fn print_dhm_auth_debug(token: &str, url: &str) {
    let trimmed = token.trim();
    let leading_or_trailing_whitespace = trimmed.len() != token.len();
    let contains_internal_whitespace = trimmed.chars().any(char::is_whitespace);

    eprintln!("DHM auth debug:");
    eprintln!("  token present: {}", !token.is_empty());
    eprintln!("  token length: {}", token.len());
    eprintln!("  trimmed token length: {}", trimmed.len());
    eprintln!(
        "  leading/trailing whitespace removed by trim: {}",
        leading_or_trailing_whitespace
    );
    eprintln!(
        "  token contains whitespace after trim: {}",
        contains_internal_whitespace
    );
    eprintln!("  request URL: {}", redact_dhm_token(url));
}

fn redact_dhm_token(url: &str) -> String {
    match url.split_once("&token=") {
        Some((prefix, _)) => format!("{prefix}&token=<redacted>"),
        None => url.to_string(),
    }
}

fn dhm_gaussian_blur(grid: &[Vec<f64>], sigma: f64) -> Vec<Vec<f64>> {
    let h = grid.len();
    if h == 0 {
        return vec![];
    }
    let w = grid[0].len();

    let radius = (sigma * 3.0).ceil() as usize;
    let kernel_size = radius * 2 + 1;
    let mut kernel = vec![0.0f64; kernel_size];
    let mut sum = 0.0;
    for i in 0..kernel_size {
        let x = i as f64 - radius as f64;
        let val = (-x * x / (2.0 * sigma * sigma)).exp();
        kernel[i] = val;
        sum += val;
    }
    for value in &mut kernel {
        *value /= sum;
    }

    let mut temp = vec![vec![0.0f64; w]; h];
    for y in 0..h {
        for x in 0..w {
            let mut acc = 0.0;
            for k in 0..kernel_size {
                let sx =
                    (x as isize + k as isize - radius as isize).clamp(0, w as isize - 1) as usize;
                acc += grid[y][sx] * kernel[k];
            }
            temp[y][x] = acc;
        }
    }

    let mut result = vec![vec![0.0f64; w]; h];
    for y in 0..h {
        for x in 0..w {
            let mut acc = 0.0;
            for k in 0..kernel_size {
                let sy =
                    (y as isize + k as isize - radius as isize).clamp(0, h as isize - 1) as usize;
                acc += temp[sy][x] * kernel[k];
            }
            result[y][x] = acc;
        }
    }

    result
}


