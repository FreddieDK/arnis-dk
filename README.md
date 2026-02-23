<img src="assets/git/banner.png" width="100%" alt="Banner">

# Arnis DK

A fork of [Arnis](https://github.com/louis-e/arnis) with Denmark-specific enhancements. Generates Minecraft Java Edition (1.17+) and Bedrock Edition worlds from real-world geography, enriched with Danish government data sources for higher-fidelity buildings and terrain.

![Minecraft Preview](assets/git/preview.jpg)

## What's different from upstream Arnis?

### BBR Building Enrichment
Integrates with **BBR (Bygnings- og Boligregistret)** — the Danish national building register — via the Datafordeler GraphQL API. When enabled, BBR data fills in missing OSM tags:
- **Number of floors** (`building:levels`)
- **Wall material** (`building:material`) — brick, concrete, wood, etc.
- **Roof material** (`roof:material`) — tile, metal, thatch, etc.
- **Building use** (`building` type) — residential, commercial, industrial, etc.

This produces more accurate and varied buildings compared to OSM data alone.

### DHM High-Resolution Terrain
Uses **DHM (Danmarks Højdemodel)** from Dataforsyningen — Denmark's national elevation model at 0.4m resolution. This replaces the default AWS terrain tiles with far more detailed elevation data, including:
- Accurate coastal terrain and cliffs
- Sea level detection with automatic water fill below sea level
- Gaussian-smoothed terrain for natural-looking landscapes

### Other improvements
- **Water rendering fix**: Water polygon ways now use scanline rasterization instead of flood fill, fixing rendering of concave water bodies
- **Tiny building filter**: Structures with a footprint smaller than 4x4 blocks are skipped, removing out-of-place sheds and utility boxes
- **DHM request retry logic**: Automatic retries with backoff for elevation data requests

## Usage

### GUI
Download the [latest release](https://github.com/louis-e/arnis/releases/) or compile with:
```
cargo run
```

### CLI (basic)
```
cargo run --no-default-features -- --terrain --output-dir="C:/YOUR_PATH/.minecraft/saves" --bbox="min_lat,min_lng,max_lat,max_lng"
```

### CLI with Danish enrichment
```
cargo run --no-default-features -- \
  --terrain \
  --output-dir="/path/to/.minecraft/saves" \
  --bbox="55.395,11.330,55.415,11.370" \
  --bbr --bbr-credentials="YOUR_DATAFORDELER_KEY" \
  --dhm-token="YOUR_DATAFORSYNINGEN_TOKEN"
```

### API keys

| Flag | Env variable | Where to get it |
|------|-------------|-----------------|
| `--bbr-credentials` | `BBR_CREDENTIALS` | [datafordeler.dk](https://datafordeler.dk) — create an IT-system and generate an API key |
| `--dhm-token` | `DHM_TOKEN` | [dataforsyningen.dk](https://dataforsyningen.dk) — create a profile and generate a token |

Both flags are optional. Without them, Arnis DK behaves identically to upstream Arnis.

### Linux server build
A GitHub Actions workflow is included to build a headless Linux binary:
1. Go to **Actions > Build Linux Binary > Run workflow**
2. Download the `arnis-linux-x86_64` artifact
3. Upload to your server, `chmod +x arnis`, and run

## All CLI flags

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
| `--bbr` | `false` | Enable BBR building enrichment (Denmark) |
| `--bbr-credentials` | — | Datafordeler API key for BBR |
| `--dhm-token` | — | Dataforsyningen token for DHM terrain |
| `--debug` | `false` | Enable debug output |
| `--timeout` | — | Flood fill timeout in seconds |

## Building from source

**GUI build** (requires Tauri dependencies):
```
cargo run
```

**CLI-only build** (no GUI dependencies):
```
cargo build --release --no-default-features
```

## Documentation

Full upstream documentation is available in the [Arnis Wiki](https://github.com/louis-e/arnis/wiki/).

## License
Copyright (c) 2022-2025 Louis Erbkamm (louis-e)

Licensed under the Apache License, Version 2.0. See [LICENSE](LICENSE) for details.

Based on [Arnis](https://github.com/louis-e/arnis) by louis-e.
