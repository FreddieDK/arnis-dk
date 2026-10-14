# Arnis-DK

Generate real-world locations in Denmark as Minecraft worlds, with buildings
enriched by official Danish geodata. Arnis-DK builds on
[Arnis](https://github.com/louis-e/arnis), which provides terrain, roads,
vegetation and world generation. Terrain uses Mapterhorn; the Danish data
integration focuses on buildings and addresses.

## What it adds

| Data source | Used for |
| --- | --- |
| **GeoDanmark** | Building footprints, including courtyards and complex outlines; hedges and scrub |
| **BBR** | Building use, floor counts, wall and roof materials |
| **DAR** | Addresses and qualified entrance or facade positions |
| **Danish height model** | Telling living hedges apart from fences |

- Adds missing building footprints and fills gaps in existing building data.
- Improves entrance placement and adds full addresses on ordinary Minecraft signs.
- Reduces generated trees in urban areas while preserving forest density.
- Grows real garden hedges and scrub where GeoDanmark and the height model show them: never inside buildings or on paving, each hedge in a one-block strip of lawn.
- Builds wind turbines at their real size from OpenStreetMap (hub height, rotor diameter, tip height; typical Danish size when untagged), shrunk only where they would reach the build height.
- Renders agricultural fields as grass with sparse short grass, small flower patches and occasional low shrubs, without generated crops, hay bales or irrigation dots; mapped roads, waterways and hedges remain.
- Keeps worlds fair for [survival servers](#survival-servers): no valuable blocks, vanilla ore amounts and the normal build height for Geyser players.
- Supports Arnis features such as terrain, caves and road signs.
- Keeps coastal water and mapped bridges when skipping open sea: a 2,000-metre coastal buffer and 2,000 metres on each side of bridges.
- Seals the finished Meld world’s underground void edge with two layers of bedrock, without walls between generated cells.
- Places new Meld worlds in a fixed [UTM coordinate frame](#coordinates), so every place in Denmark has the same block coordinates in every world.

Entrance positions depend on the source data. Facade points are placement hints,
not surveyed door locations; exact doors and building heights are not guaranteed.

Landscape changes apply to newly generated areas; existing worlds need regeneration
to adopt them.

## Getting started

You need Rust and Python 3.12+. Live Danish data downloads require your own
Datafordeler API key; downloaded register extracts can also be imported. An
optional Dataforsyningen token lets the hedge check use the Danish height model.

1. Install the Python dependencies from `danish_data/requirements.txt`.
2. Use `danish_data/fetch.py` to prepare a building supplement for your area,
   or `danish_data/prepare.py` to import existing extracts.
3. Generate the world with the supplement:

```sh
cargo run --release --locked --no-default-features -- --bbox "55.670,12.560,55.675,12.570" --danish-buildings .local/data/buildings.json --output-dir .local/worlds/example --caves
```

The supplement must cover the selected area. Keep API keys and downloaded data
under the Git-ignored `.local/` directory. See the
[Danish data setup guide](danish_data/README.md) for authentication and input formats.

## Large areas

The experimental [Meld CLI integration](danish_data/MELD.md) divides large areas
into smaller jobs and generates them sequentially into one Java world using
Arnis One World. Interrupted jobs can be resumed. Meld is installed separately;
this integration uses its grid planner, not its parallel GUI workflow. New jobs
use the normal build height; see [survival servers](#survival-servers).

An optional [land mask](danish_data/OCEAN.md) skips open-sea cells, leaving void
between land areas. Country-border restrictions require a suitable selection;
the ocean filter alone does not exclude neighbouring countries.

## Coordinates

New Meld jobs use a fixed frame on UTM zone 32 (EPSG:25832): X = easting −
600,000 and Z = 6,200,000 − northing, one block per metre. (0, 0) lies near Samsø
and north is −Z. Any Danish address or survey coordinate converts to block
coordinates with that formula, and cells of whole region files tile Denmark
without gaps or overlaps.

- **Scale.** UTM keeps a metre within 0.1 % of a block across Denmark. The
  earlier Web Mercator frame was exact only at its origin; with the origin in
  Slagelse, Skagen came out 6 % too large.
- **North.** In-game north is grid north, which turns against true north from
  0° in western Jutland to about 3° in Copenhagen and 5° on Bornholm.
- **Existing worlds.** A One World keeps the frame it was created with and is
  never converted. Plan with `--frame meld` for the earlier Web Mercator grid.
- **CLI.** `--one-world --projection mcdk` creates such a world;
  `--block-rect min_x,min_z,max_x,max_z` adds an exact block rectangle to it.

## Survival servers

Arnis-DK worlds are meant to be played on, including on servers with an economy.

- **Normal build height.** New Meld jobs use Y -64 to 319 with sea level at Y 63,
  instead of Arnis One World's extended height (-2032 to 2031). Bedrock players
  joining through Geyser can then see the world, and animals spawn as often as in
  an ordinary world. At scale 1 this holds land from about -109 m to 240 m, which
  covers Denmark. Plan with `--world-height earth` for the extended height; a job
  always keeps the height its world was created with.
- **No valuable blocks.** Iron, gold, netherite, redstone, coal and copper blocks,
  anvils, hoppers and sponges are saved as cheap look-alikes such as concrete,
  terracotta, granite and nether wood. Stairs and slabs keep their shape. Players
  therefore cannot mine generated masts, bridges or facades for resources.
- **Vanilla ore amounts.** Cave ores match an ordinary world, about 18 diamond ore
  blocks per chunk instead of about 50 before.
- **No loot.** Interiors with filled chests are off unless `--interior` is given;
  leave it off for survival maps.

These rules apply to newly generated areas. Existing worlds keep their blocks,
ores and build height.

## Documentation and credits

- [Arnis usage and build instructions](README.upstream.md)
- [Danish data integration](danish_data/README.md)
- [Large-area generation with Meld](danish_data/MELD.md)
- [Ocean filtering](danish_data/OCEAN.md)

Arnis-DK retains Arnis' [Apache-2.0 license](LICENSE) and [notices](NOTICE).
Geodata remains subject to its source terms; preserve the source attribution
included in prepared building supplements.
