use crate::coordinate_system::geographic::LLBBox;
use crate::osm_parser::ProcessedElement;
use crate::progress::emit_gui_progress_update;
use colored::Colorize;
use reqwest::blocking::Client;
use serde::Deserialize;
use std::collections::HashMap;
use std::time::Duration;

// ============================================================================
// GraphQL response types
// ============================================================================

#[derive(Debug, Deserialize)]
struct GraphQlResponse {
    data: Option<GraphQlData>,
    errors: Option<Vec<GraphQlError>>,
}

#[derive(Debug, Deserialize)]
struct GraphQlError {
    message: String,
}

#[derive(Debug, Deserialize)]
struct GraphQlData {
    #[serde(rename = "BBR_Bygning")]
    bbr_bygning: Option<GraphQlBygningResult>,
}

#[derive(Debug, Deserialize)]
struct GraphQlBygningResult {
    nodes: Vec<GraphQlBygning>,
    #[serde(rename = "pageInfo")]
    page_info: PageInfo,
}

#[derive(Debug, Deserialize)]
struct PageInfo {
    #[serde(rename = "hasNextPage")]
    has_next_page: bool,
    #[serde(rename = "endCursor")]
    end_cursor: Option<String>,
}

#[derive(Debug, Deserialize)]
struct GraphQlBygning {
    #[serde(rename = "byg021BygningensAnvendelse")]
    anvendelse: Option<String>,
    #[serde(rename = "byg032YdervaeggensMateriale")]
    ydervaegs_materiale: Option<String>,
    #[serde(rename = "byg033Tagdaekningsmateriale")]
    tagdaekning: Option<String>,
    #[serde(rename = "byg054AntalEtager")]
    antal_etager: Option<i32>,
    #[serde(rename = "byg404Koordinat")]
    koordinat: Option<GraphQlKoordinat>,
}

#[derive(Debug, Deserialize)]
struct GraphQlKoordinat {
    wkt: Option<String>,
}

// ============================================================================
// Internal types
// ============================================================================

/// A BBR building with its lat/lon centroid and relevant properties.
struct BbrBuilding {
    lat: f64,
    lon: f64,
    antal_etager: Option<i32>,
    ydervaegs_materiale: Option<i32>,
    tagdaekning: Option<i32>,
    anvendelse: Option<i32>,
}

// ============================================================================
// Coordinate conversion helpers (WGS84 <-> ETRS89/UTM32N)
// ============================================================================

/// Convert WGS84 (lat, lon) to ETRS89/UTM32N (easting, northing).
/// BBR's `byg404Koordinat` and spatial filters use EPSG:25832.
fn wgs84_to_utm32n(lat: f64, lon: f64) -> (f64, f64) {
    let a = 6378137.0_f64; // WGS84 semi-major axis
    let f = 1.0 / 298.257223563; // WGS84 flattening
    let k0 = 0.9996; // UTM scale factor
    let lon0 = 9.0_f64; // Central meridian for UTM zone 32

    let e2 = 2.0 * f - f * f;
    let e_prime2 = e2 / (1.0 - e2);

    let lat_rad = lat.to_radians();
    let lon_rad = lon.to_radians();
    let lon0_rad = lon0.to_radians();

    let n = a / (1.0 - e2 * lat_rad.sin().powi(2)).sqrt();
    let t = lat_rad.tan();
    let c = e_prime2 * lat_rad.cos().powi(2);
    let a_coeff = (lon_rad - lon0_rad) * lat_rad.cos();

    // Meridional arc
    let e2_2 = e2 * e2;
    let e2_3 = e2_2 * e2;
    let m = a
        * ((1.0 - e2 / 4.0 - 3.0 * e2_2 / 64.0 - 5.0 * e2_3 / 256.0) * lat_rad
            - (3.0 * e2 / 8.0 + 3.0 * e2_2 / 32.0 + 45.0 * e2_3 / 1024.0) * (2.0 * lat_rad).sin()
            + (15.0 * e2_2 / 256.0 + 45.0 * e2_3 / 1024.0) * (4.0 * lat_rad).sin()
            - (35.0 * e2_3 / 3072.0) * (6.0 * lat_rad).sin());

    let easting = k0
        * n
        * (a_coeff
            + (1.0 - t * t + c) * a_coeff.powi(3) / 6.0
            + (5.0 - 18.0 * t * t + t.powi(4) + 72.0 * c - 58.0 * e_prime2) * a_coeff.powi(5)
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

/// Convert ETRS89/UTM32N (easting, northing) to WGS84 (lat, lon).
fn utm32n_to_wgs84(easting: f64, northing: f64) -> (f64, f64) {
    let a = 6378137.0_f64;
    let f: f64 = 1.0 / 298.257223563;
    let k0: f64 = 0.9996;
    let lon0: f64 = 9.0;

    let e2: f64 = 2.0 * f - f * f;
    let e1: f64 = (1.0 - (1.0 - e2).sqrt()) / (1.0 + (1.0 - e2).sqrt());
    let e_prime2: f64 = e2 / (1.0 - e2);

    let x = easting - 500000.0;
    let y = northing;

    let m = y / k0;
    let mu = m / (a * (1.0 - e2 / 4.0 - 3.0 * e2 * e2 / 64.0 - 5.0 * e2.powi(3) / 256.0));

    let phi1 = mu
        + (3.0 * e1 / 2.0 - 27.0 * e1.powi(3) / 32.0) * (2.0 * mu).sin()
        + (21.0 * e1 * e1 / 16.0 - 55.0 * e1.powi(4) / 32.0) * (4.0 * mu).sin()
        + (151.0 * e1.powi(3) / 96.0) * (6.0 * mu).sin()
        + (1097.0 * e1.powi(4) / 512.0) * (8.0 * mu).sin();

    let n1 = a / (1.0 - e2 * phi1.sin().powi(2)).sqrt();
    let t1 = phi1.tan();
    let c1 = e_prime2 * phi1.cos().powi(2);
    let r1 = a * (1.0 - e2) / (1.0 - e2 * phi1.sin().powi(2)).powf(1.5);
    let d = x / (n1 * k0);

    let lat = phi1
        - (n1 * t1 / r1)
            * (d * d / 2.0
                - (5.0 + 3.0 * t1 * t1 + 10.0 * c1 - 4.0 * c1 * c1 - 9.0 * e_prime2) * d.powi(4)
                    / 24.0
                + (61.0 + 90.0 * t1 * t1 + 298.0 * c1 + 45.0 * t1.powi(4)
                    - 252.0 * e_prime2
                    - 3.0 * c1 * c1)
                    * d.powi(6)
                    / 720.0);

    let lon = (d - (1.0 + 2.0 * t1 * t1 + c1) * d.powi(3) / 6.0
        + (5.0 - 2.0 * c1 + 28.0 * t1 * t1 - 3.0 * c1 * c1 + 8.0 * e_prime2 + 24.0 * t1.powi(4))
            * d.powi(5)
            / 120.0)
        / phi1.cos();

    (lat.to_degrees(), lon.to_degrees() + lon0)
}

// ============================================================================
// BBR GraphQL API
// ============================================================================

/// Build the GraphQL query for fetching buildings within a bounding box.
/// The spatial filter uses ETRS89/UTM32N coordinates (EPSG:25832).
fn build_bbr_query(bbox: LLBBox, cursor: Option<&str>) -> String {
    // Convert WGS84 bbox corners to UTM32N for the spatial filter
    let (min_e, min_n) = wgs84_to_utm32n(bbox.min().lat(), bbox.min().lng());
    let (max_e, max_n) = wgs84_to_utm32n(bbox.max().lat(), bbox.max().lng());

    // BBR requires bitemporal point-in-time filters
    let now = {
        use std::time::SystemTime;
        let d = SystemTime::now()
            .duration_since(SystemTime::UNIX_EPOCH)
            .unwrap()
            .as_secs();
        let days = d / 86400;
        // Simple date calculation from epoch days
        let mut y = 1970i32;
        let mut remaining = days as i32;
        loop {
            let days_in_year = if y % 4 == 0 && (y % 100 != 0 || y % 400 == 0) {
                366
            } else {
                365
            };
            if remaining < days_in_year {
                break;
            }
            remaining -= days_in_year;
            y += 1;
        }
        let leap = y % 4 == 0 && (y % 100 != 0 || y % 400 == 0);
        let mdays = [
            31,
            if leap { 29 } else { 28 },
            31,
            30,
            31,
            30,
            31,
            31,
            30,
            31,
            30,
            31,
        ];
        let mut m = 0u32;
        for &md in &mdays {
            if remaining < md {
                break;
            }
            remaining -= md;
            m += 1;
        }
        format!("{:04}-{:02}-{:02}T00:00:00Z", y, m + 1, remaining + 1)
    };

    let after_clause = match cursor {
        Some(c) => format!(r#", after: "{}""#, c),
        None => String::new(),
    };

    // Use GraphQL aliases for fields with Danish characters (æ) to avoid encoding issues.
    format!(
        "{{\n\
         \x20 BBR_Bygning(\n\
         \x20   first: 500{after}\n\
         \x20   registreringstid: \"{now}\"\n\
         \x20   virkningstid: \"{now}\"\n\
         \x20   where: {{\n\
         \x20     status: {{ eq: \"6\" }}\n\
         \x20     byg404Koordinat: {{\n\
         \x20       within: {{ wkt: \"POLYGON(({min_e} {min_n}, {max_e} {min_n}, {max_e} {max_n}, {min_e} {max_n}, {min_e} {min_n}))\", crs: 25832 }}\n\
         \x20     }}\n\
         \x20   }}\n\
         \x20 ) {{\n\
         \x20   pageInfo {{ hasNextPage endCursor }}\n\
         \x20   nodes {{\n\
         \x20     byg021BygningensAnvendelse\n\
         \x20     byg032YdervaeggensMateriale\n\
         \x20     byg033Tagdaekningsmateriale\n\
         \x20     byg054AntalEtager\n\
         \x20     byg404Koordinat {{ wkt }}\n\
         \x20   }}\n\
         \x20 }}\n\
         }}",
        now = now,
        after = after_clause,
        min_e = min_e as i64,
        min_n = min_n as i64,
        max_e = max_e as i64,
        max_n = max_n as i64,
    )
}

/// Fetch all buildings from the BBR GraphQL API for a given bounding box.
/// Handles pagination automatically.
fn fetch_bbr_buildings(
    bbox: LLBBox,
    api_key: &str,
) -> Result<Vec<BbrBuilding>, Box<dyn std::error::Error>> {
    let client = Client::builder()
        .timeout(Duration::from_secs(120))
        .build()?;

    let endpoint = "https://graphql.datafordeler.dk/BBR/v1";

    let mut all_buildings = Vec::new();
    let mut cursor: Option<String> = None;
    let mut page = 0u32;

    loop {
        page += 1;
        let query = build_bbr_query(bbox, cursor.as_deref());

        let url = format!("{endpoint}?apiKey={api_key}");
        let resp = client
            .post(&url)
            .json(&serde_json::json!({ "query": query }))
            .send()?;

        if !resp.status().is_success() {
            let status = resp.status();
            let body_text = resp.text().unwrap_or_default();
            return Err(format!("BBR GraphQL API returned status {status}: {body_text}").into());
        }

        let body: GraphQlResponse = resp.json()?;

        if let Some(errors) = &body.errors {
            let msgs: Vec<&str> = errors.iter().map(|e| e.message.as_str()).collect();
            return Err(format!("BBR GraphQL errors: {}", msgs.join("; ")).into());
        }

        let result = body
            .data
            .and_then(|d| d.bbr_bygning)
            .ok_or("BBR GraphQL returned no data")?;

        for bygning in &result.nodes {
            // Parse the POINT WKT string: "POINT (easting northing)"
            let (lat, lon) = match &bygning.koordinat {
                Some(koord) => match koord.wkt.as_deref().and_then(parse_point_coordinate) {
                    Some(coords) => coords,
                    None => continue,
                },
                None => continue,
            };

            all_buildings.push(BbrBuilding {
                lat,
                lon,
                antal_etager: bygning.antal_etager,
                ydervaegs_materiale: bygning
                    .ydervaegs_materiale
                    .as_ref()
                    .and_then(|s| s.parse().ok()),
                tagdaekning: bygning.tagdaekning.as_ref().and_then(|s| s.parse().ok()),
                anvendelse: bygning.anvendelse.as_ref().and_then(|s| s.parse().ok()),
            });
        }

        if !result.page_info.has_next_page {
            break;
        }
        cursor = result.page_info.end_cursor;
        if cursor.is_none() {
            break;
        }

        // Safety limit to avoid infinite loops
        if page >= 100 {
            println!(
                "{}",
                "Warning: BBR pagination limit reached (50000 buildings). Some buildings may be missing."
                    .yellow()
            );
            break;
        }
    }

    Ok(all_buildings)
}

/// Parse a WKT POINT string like "POINT (530500 6147880)" from ETRS89/UTM32N
/// and convert to WGS84 (lat, lon).
fn parse_point_coordinate(coord_str: &str) -> Option<(f64, f64)> {
    // Format: "POINT (easting northing)" or "POINT(easting northing)"
    let inner = coord_str
        .strip_prefix("POINT")
        .map(|s| s.trim())
        .and_then(|s| s.strip_prefix('('))
        .and_then(|s| s.strip_suffix(')'))?;

    let parts: Vec<&str> = inner.trim().split_whitespace().collect();
    if parts.len() != 2 {
        return None;
    }

    let easting: f64 = parts[0].parse().ok()?;
    let northing: f64 = parts[1].parse().ok()?;

    let (lat, lon) = utm32n_to_wgs84(easting, northing);
    Some((lat, lon))
}

// ============================================================================
// BBR code translation helpers
// ============================================================================

/// Translate BBR wall material code to an OSM-compatible building:colour tag value.
/// Uses hex codes for accuracy since the renderer supports them via color_text_to_rgb_tuple.
fn bbr_wall_material_to_colour(code: i32) -> Option<&'static str> {
    match code {
        1 => Some("#b5451b"),  // Mursten (brick) - Danish red/brown brick
        2 => Some("#c8c0b8"),  // Letbeton (lightweight concrete) - light warm gray
        3 => Some("#b0b0b0"),  // Fibercement (fiber cement) - neutral gray
        4 => Some("#c8a050"),  // Bindingsværk (timber frame) - ochre/yellow
        5 => Some("#a07040"),  // Træ (wood) - warm brown
        6 => Some("#909090"),  // Beton (concrete) - medium gray
        7 => Some("#d8c8a0"),  // Natursten (natural stone) - sandstone
        8 => Some("#808890"),  // Metal - cool gray
        10 => Some("#d0e8f0"), // Glas (glass) - light blue tint
        11 => Some("#e8e0d0"), // Kalksandsten (calcium silicate) - off-white
        12 => Some("#f0e8d0"), // Puds (stucco/render) - cream/off-white
        _ => None,
    }
}

/// Translate BBR roof material code to OSM-compatible roof tags.
/// Sets both roof:shape (biggest visual impact) and roof:material where applicable.
fn bbr_roof_to_tags(code: i32) -> HashMap<&'static str, &'static str> {
    let mut tags = HashMap::new();
    match code {
        1 => {
            // Built-up/flat roof
            tags.insert("roof:shape", "flat");
        }
        2 => {
            // Tagpap (roofing felt) - typically flat or low-slope
            tags.insert("roof:shape", "flat");
        }
        3 => {
            // Fibercement (fiber cement panels) - gabled in Denmark
            tags.insert("roof:shape", "gabled");
        }
        4 => {
            // Cementsten (cement tiles) - gabled
            tags.insert("roof:shape", "gabled");
            tags.insert("roof:material", "tile");
        }
        5 => {
            // Tegl (clay tiles) - classic Danish gabled roof
            tags.insert("roof:shape", "gabled");
            tags.insert("roof:material", "tile");
        }
        6 => {
            // Metal
            tags.insert("roof:shape", "gabled");
            tags.insert("roof:material", "metal");
        }
        7 => {
            // Stråtag (thatch) - traditional Danish farmhouse
            tags.insert("roof:shape", "hipped");
            tags.insert("roof:material", "thatch");
        }
        10 => {
            // Glas (glass)
            tags.insert("roof:material", "glass");
        }
        11 => {
            // PVC
            tags.insert("roof:shape", "flat");
        }
        12 => {
            // Skifer (slate)
            tags.insert("roof:shape", "hipped");
            tags.insert("roof:material", "slate");
        }
        20 => {
            // Grønt tag (green/living roof)
            tags.insert("roof:shape", "flat");
        }
        _ => {}
    }
    tags
}

/// Translate BBR building use code to an OSM building type.
fn bbr_anvendelse_to_building_type(code: i32) -> Option<&'static str> {
    match code {
        // Residential
        110 => Some("house"),              // Stuehus (farmhouse dwelling)
        120 => Some("house"),              // Fritliggende enfamiliehus (detached)
        121 => Some("semidetached_house"), // Sammenbygget enfamiliehus
        130 => Some("apartments"),         // Række/kæde/dobbelthus (terraced)
        131 | 132 => Some("apartments"),   // Row houses
        140 => Some("apartments"),         // Etagebolig (multi-story)
        150 => Some("residential"),        // Kollegium (dormitory)
        160 => Some("residential"),        // Døgninstitution
        185..=190 => Some("residential"),  // Other residential
        // Commercial / retail
        310..=319 => Some("commercial"),
        320..=329 => Some("office"),
        330..=339 => Some("hotel"),
        340..=349 => Some("commercial"), // Restaurant etc
        350..=359 => Some("retail"),
        360..=369 => Some("commercial"), // Shopping/commercial
        370..=379 => Some("commercial"),
        390..=399 => Some("commercial"),
        // Industrial
        410..=419 => Some("industrial"), // Factory/manufacturing
        420..=429 => Some("industrial"), // Workshop
        430..=439 => Some("warehouse"),  // Warehouse/storage
        440..=449 => Some("industrial"),
        // Public / institutional
        510..=519 => Some("school"),     // Grundskole (primary school)
        520..=529 => Some("university"), // Videregående uddannelse
        530..=539 => Some("hospital"),
        540..=549 => Some("public"), // Daycare
        550..=559 => Some("school"),
        585 => Some("church"), // Kirke (church)
        590..=599 => Some("public"),
        // Culture / sports
        610..=619 => Some("public"), // Cultural
        620..=629 => Some("public"), // Library, museum
        // Infrastructure
        710..=719 => Some("industrial"), // Energy/utility
        720..=729 => Some("industrial"), // Water/sewage
        // Agricultural
        910..=919 => Some("farm"),
        920..=929 => Some("farm_auxiliary"),
        930 => Some("shed"),   // Udhus (outbuilding)
        940 => Some("garage"), // Garage
        950 => Some("shed"),   // Anneks (annexe)
        _ => None,
    }
}

// ============================================================================
// Main enrichment logic
// ============================================================================

/// Fetch BBR data via GraphQL and inject missing tags into OSM building elements.
pub fn enrich_with_bbr(
    elements: &mut [ProcessedElement],
    bbox: LLBBox,
    api_key: &str,
) -> Result<(), Box<dyn std::error::Error>> {
    println!("{}", "Fetching BBR building data...".bold());
    emit_gui_progress_update(8.0, "Fetching BBR building data...");

    let bbr_buildings = fetch_bbr_buildings(bbox, api_key)?;
    if bbr_buildings.is_empty() {
        println!("No BBR buildings found for this area.");
        return Ok(());
    }
    println!(
        "Found {} BBR buildings. Matching to OSM data...",
        bbr_buildings.len()
    );

    // Compute the Minecraft coordinate range across all building ways so we can
    // reverse-map x/z back to approximate lat/lon.
    let mut all_x = Vec::new();
    let mut all_z = Vec::new();
    for elem in elements.iter() {
        if let ProcessedElement::Way(way) = elem {
            if way.tags.contains_key("building") || way.tags.contains_key("building:part") {
                for node in &way.nodes {
                    all_x.push(node.x);
                    all_z.push(node.z);
                }
            }
        }
    }

    if all_x.is_empty() {
        return Ok(());
    }

    let global_min_x = *all_x.iter().min().unwrap() as f64;
    let global_max_x = *all_x.iter().max().unwrap() as f64;
    let global_min_z = *all_z.iter().min().unwrap() as f64;
    let global_max_z = *all_z.iter().max().unwrap() as f64;

    let x_range = (global_max_x - global_min_x).max(1.0);
    let z_range = (global_max_z - global_min_z).max(1.0);

    let lat_min = bbox.min().lat();
    let lat_max = bbox.max().lat();
    let lon_min = bbox.min().lng();
    let lon_max = bbox.max().lng();

    let mut matched = 0u32;
    let mut enriched = 0u32;

    for elem in elements.iter_mut() {
        let way = match elem {
            ProcessedElement::Way(w) => w,
            _ => continue,
        };

        if !way.tags.contains_key("building") && !way.tags.contains_key("building:part") {
            continue;
        }

        if way.nodes.is_empty() {
            continue;
        }

        // Compute centroid in Minecraft coords
        let min_x = way.nodes.iter().map(|n| n.x).min().unwrap() as f64;
        let max_x = way.nodes.iter().map(|n| n.x).max().unwrap() as f64;
        let min_z = way.nodes.iter().map(|n| n.z).min().unwrap() as f64;
        let max_z = way.nodes.iter().map(|n| n.z).max().unwrap() as f64;

        let cx = (min_x + max_x) / 2.0;
        let cz = (min_z + max_z) / 2.0;

        // Reverse-map to approximate lat/lon
        let norm_x = (cx - global_min_x) / x_range;
        let norm_z = (cz - global_min_z) / z_range;

        let approx_lon = lon_min + norm_x * (lon_max - lon_min);
        let approx_lat = lat_max - norm_z * (lat_max - lat_min);

        // Find nearest BBR building within threshold (~30m ≈ 0.00027 degrees)
        // Larger threshold to account for coordinate approximation from Minecraft coords
        let threshold = 0.00027;
        let threshold_sq = threshold * threshold;

        let mut best_dist_sq = f64::MAX;
        let mut best_bbr: Option<&BbrBuilding> = None;

        for bbr in &bbr_buildings {
            let dlat = bbr.lat - approx_lat;
            let dlon = bbr.lon - approx_lon;
            let dist_sq = dlat * dlat + dlon * dlon;
            if dist_sq < best_dist_sq && dist_sq < threshold_sq {
                best_dist_sq = dist_sq;
                best_bbr = Some(bbr);
            }
        }

        if let Some(bbr) = best_bbr {
            matched += 1;
            let mut did_enrich = false;

            // BBR is authoritative for Denmark — always apply its data.
            // building:levels — always override since BBR is the official source
            if let Some(levels) = bbr.antal_etager {
                if levels >= 1 {
                    way.tags
                        .insert("building:levels".to_string(), levels.to_string());
                    did_enrich = true;
                }
            }

            // building:colour — always set from BBR wall material
            if let Some(code) = bbr.ydervaegs_materiale {
                if let Some(colour) = bbr_wall_material_to_colour(code) {
                    way.tags
                        .insert("building:colour".to_string(), colour.to_string());
                    did_enrich = true;
                }
            }

            // Roof tags — always set from BBR roof material
            if let Some(code) = bbr.tagdaekning {
                let roof_tags = bbr_roof_to_tags(code);
                for (key, value) in roof_tags {
                    way.tags.insert(key.to_string(), value.to_string());
                    did_enrich = true;
                }
            }

            // Building type — always refine from BBR
            if let Some(code) = bbr.anvendelse {
                if let Some(btype) = bbr_anvendelse_to_building_type(code) {
                    way.tags.insert("building".to_string(), btype.to_string());
                    did_enrich = true;
                }
            }

            if did_enrich {
                enriched += 1;
            }
        }
    }

    println!("BBR enrichment: matched {matched} buildings, enriched {enriched} with new tags.");

    Ok(())
}

// ============================================================================
// DHM (Danmarks Højdemodel) high-resolution terrain
// ============================================================================

use crate::coordinate_system::transformation::geo_distance;
use crate::elevation_data::ElevationData;

/// Fetch high-resolution elevation data from DHM via Dataforsyningen WCS.
/// Returns an ElevationData grid matching the Minecraft world dimensions.
pub fn fetch_dhm_elevation(
    bbox: &LLBBox,
    scale: f64,
    ground_level: i32,
    token: &str,
) -> Result<ElevationData, Box<dyn std::error::Error>> {
    println!("{}", "Fetching DHM high-resolution terrain...".bold());
    emit_gui_progress_update(12.0, "Fetching DHM terrain data...");

    let (base_scale_z, base_scale_x) = geo_distance(bbox.min(), bbox.max());
    let grid_width = (base_scale_x.floor() * scale) as usize;
    let grid_height = (base_scale_z.floor() * scale) as usize;

    if grid_width == 0 || grid_height == 0 {
        return Err("Grid dimensions are zero".into());
    }

    // Convert bbox to UTM32N for the WCS request
    let (min_e, min_n) = wgs84_to_utm32n(bbox.min().lat(), bbox.min().lng());
    let (max_e, max_n) = wgs84_to_utm32n(bbox.max().lat(), bbox.max().lng());

    // Cap request size to avoid huge downloads — limit to 2048 pixels per side.
    // The DHM WCS can return very large images at native 0.4m resolution.
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

    // Retry on transient failures (timeouts, 502/503/504).
    // First tries 3 quick attempts with short backoff, then waits 60s and
    // repeats — up to 5 rounds before giving up.
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

    // Check if we got an error response (XML) instead of a TIFF
    if content_type.contains("xml") || (bytes.len() > 5 && bytes[0] == b'<') {
        let text = String::from_utf8_lossy(&bytes[..bytes.len().min(500)]);
        return Err(format!("DHM WCS returned error: {text}").into());
    }

    println!(
        "Received {} bytes of DHM terrain data. Parsing...",
        bytes.len()
    );
    emit_gui_progress_update(15.0, "Processing DHM terrain...");

    // Parse the GeoTIFF using the tiff crate
    let cursor = std::io::Cursor::new(bytes.to_vec());
    let mut decoder = tiff::decoder::Decoder::new(cursor)
        .map_err(|e| format!("Failed to decode GeoTIFF: {e}"))?;

    let (tiff_width, tiff_height) = decoder
        .dimensions()
        .map_err(|e| format!("Failed to read TIFF dimensions: {e}"))?;

    // Read elevation values
    let image_data = decoder
        .read_image()
        .map_err(|e| format!("Failed to read TIFF image data: {e}"))?;

    let raw_heights: Vec<f64> = match image_data {
        tiff::decoder::DecodingResult::F32(data) => data.iter().map(|&v| v as f64).collect(),
        tiff::decoder::DecodingResult::F64(data) => data.to_vec(),
        tiff::decoder::DecodingResult::U8(data) => data.iter().map(|&v| v as f64).collect(),
        tiff::decoder::DecodingResult::U16(data) => data.iter().map(|&v| v as f64).collect(),
        tiff::decoder::DecodingResult::I16(data) => data.iter().map(|&v| v as f64).collect(),
        _ => {
            return Err("Unsupported TIFF pixel format".into());
        }
    };

    println!(
        "DHM TIFF: {}x{} pixels, resampling to {}x{} grid...",
        tiff_width, tiff_height, grid_width, grid_height
    );

    // Resample the TIFF data to match the Minecraft grid dimensions
    let mut height_grid: Vec<Vec<f64>> = vec![vec![0.0; grid_width]; grid_height];
    let nodata = -9999.0; // Common nodata value for DHM

    for gz in 0..grid_height {
        for gx in 0..grid_width {
            // Map grid coords to TIFF pixel coords
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

            // Replace nodata with 0 (sea level)
            height_grid[gz][gx] = if h <= nodata { 0.0 } else { h };
        }
    }

    // Apply Gaussian blur to smooth micro-terrain from DHM's high resolution.
    // Without this, buildings/roads clip into small elevation bumps.
    // Use stronger blur than the default elevation system since DHM has more detail.
    let grid_size = (grid_width.min(grid_height) as f64).max(1.0);
    let sigma = 7.0 * (grid_size / 100.0).sqrt();
    println!("Smoothing DHM terrain (sigma={:.1})...", sigma);
    let height_grid = dhm_gaussian_blur(&height_grid, sigma);

    // Find elevation range
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

    // Convert to Minecraft Y coordinates using same logic as elevation_data.rs
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

    // Compute Minecraft Y for real-world sea level (0m elevation)
    let sea_level_y = if height_range > 0.0 && min_h < 0.5 {
        let sea_relative = (0.0 - min_h) / height_range;
        let sea_scaled = sea_relative * scaled_range;
        Some(
            ((ground_level as f64 + sea_scaled).round() as i32)
                .clamp(ground_level, MAX_Y - TERRAIN_HEIGHT_BUFFER),
        )
    } else {
        None // No sea-level areas in this bbox
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

/// Apply a separable Gaussian blur to a 2D height grid.
/// This smooths out micro-terrain so buildings and roads don't clip into small bumps.
fn dhm_gaussian_blur(grid: &[Vec<f64>], sigma: f64) -> Vec<Vec<f64>> {
    let h = grid.len();
    if h == 0 {
        return vec![];
    }
    let w = grid[0].len();

    // Build 1D Gaussian kernel
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
    for v in &mut kernel {
        *v /= sum;
    }

    // Horizontal pass
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

    // Vertical pass
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
