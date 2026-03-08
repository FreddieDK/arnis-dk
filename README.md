# Arnis DK

A fork of [Arnis](https://github.com/louis-e/arnis) with Denmark-specific enhancements. Generates Minecraft Java Edition (1.17+) and Bedrock Edition worlds from real-world geography, with Danish terrain support and improved coastal water handling.

## What's different from upstream Arnis?

### DHM High-Resolution Terrain
Uses **DHM (Danmarks Hojdemodel)** from Dataforsyningen, Denmark's national elevation model at 0.4m resolution. This replaces the default AWS terrain tiles with far more detailed elevation data, including:
- Accurate coastal terrain and cliffs
- Sea level detection with automatic water fill below sea level
- Coastline-driven ocean generation, with optional dataset-backed water polygons for more reliable coastal rendering in harbors and reclaimed waterfront areas
- Gaussian-smoothed terrain for natural-looking landscapes

### Other improvements
- **Water rendering fix**: Water polygon ways now use scanline rasterization instead of flood fill, fixing rendering of concave water bodies
- **Ocean handling fix**: `natural=coastline` ways are converted into ocean polygons, and `--land-polygons` can now use `water_polygons.shp` as a dataset-backed coastline mask when OSM coastline inference is not enough
- **Tiny building filter**: Structures with a footprint smaller than 4x4 blocks are skipped, removing out-of-place sheds and utility boxes
- **DHM request retry logic**: Automatic retries with backoff for elevation data requests

## Usage

### CLI with DHM terrain
```
cargo run --no-default-features -- \
  --terrain \
  --output-dir="/path/to/.minecraft/saves" \
  --bbox="55.395,11.330,55.415,11.370" \
  --dhm-token="YOUR_DATAFORSYNINGEN_TOKEN"
```

### Coastline dataset-backed oceans
For coastal city tests, download [water-polygons-split-4326.zip](https://osmdata.openstreetmap.de/download/water-polygons-split-4326.zip) from the official [OSM water polygons dataset page](https://osmdata.openstreetmap.de/data/water-polygons.html), extract water_polygons.shp, and pass it with:
```
cargo run --no-default-features -- \
  --terrain \
  --output-dir="/path/to/.minecraft/saves" \
  --bbox="55.670,12.560,55.695,12.620" \
  --land-polygons="/path/to/water_polygons.shp"
```

### API keys

| Flag | Env variable | Where to get it |
|------|-------------|-----------------|
| `--dhm-token` | `DHM_TOKEN` | [dataforsyningen.dk](https://dataforsyningen.dk) - create a profile and generate a token |

The DHM token is optional. Without it, Arnis DK falls back to its default terrain source.

## CLI flags

| Flag | Default | Description |
|------|---------|-------------|
| `--bbox` | *required* | Bounding box: `min_lat,min_lng,max_lat,max_lng` |
| `--output-dir` | *required (Java)* | Directory where the world is created |
| `--bedrock` | `false` | Generate Bedrock Edition (.mcworld) instead of Java |
| `--terrain` | `false` | Enable terrain elevation |
| `--scale` | `1.0` | World scale in blocks per meter |
| `--ground-level` | `-62` | Base ground level Y coordinate |
| `--interior` | `true` | Generate building interiors |
| `--roof` | `true` | Generate building roofs |
| `--fillground` | `false` | Fill ground with stone below surface |
| `--city-boundaries` | `true` | Detect urban areas for stone ground |
| `--dhm-token` | - | Dataforsyningen token for DHM terrain |
| `--land-polygons` | - | Path to an extracted OSM coastline polygon shapefile (`water_polygons.shp` recommended) for dataset-backed ocean masking; keep the extracted shapefile local rather than committing it |
| `--debug` | `false` | Enable debug output |
| `--timeout` | - | Flood fill timeout in seconds |

## Disclaimer

This fork was developed with the assistance of AI.

Terrain data from [Dataforsyningen](https://dataforsyningen.dk) (DHM) is provided by the Danish government. If you use this data, you must credit the source in accordance with its terms of use.

## License
Copyright (c) 2022-2025 Louis Erbkamm (louis-e)

Licensed under the Apache License, Version 2.0. See [LICENSE](LICENSE) for details.

Based on [Arnis](https://github.com/louis-e/arnis) by louis-e.



