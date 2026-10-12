# Arnis-DK

Generate real-world locations in Denmark as Minecraft worlds, with buildings
enriched by official Danish geodata. Arnis-DK builds on
[Arnis](https://github.com/louis-e/arnis), which provides terrain, roads,
vegetation and world generation. Terrain uses Mapterhorn; the Danish data
integration focuses on buildings and addresses.

## What it adds

| Data source | Used for |
| --- | --- |
| **GeoDanmark** | Building footprints, including courtyards and complex outlines |
| **BBR** | Building use, floor counts, wall and roof materials |
| **DAR** | Addresses and qualified entrance or facade positions |

- Adds missing building footprints and fills gaps in existing building data.
- Improves entrance placement and adds full addresses on ordinary Minecraft signs.
- Reduces generated trees in urban areas while preserving forest density.
- Renders agricultural fields as grass with sparse short grass, small flower patches and occasional low shrubs, without generated crops, hay bales or irrigation dots; mapped roads, waterways and hedges remain.
- Keeps worlds fair for [survival servers](#survival-servers): no valuable blocks, vanilla ore amounts and the normal build height for Geyser players.
- Supports Arnis features such as terrain, caves and road signs.
- Keeps coastal water and mapped bridges when skipping open sea: a 2,000-metre coastal buffer and 2,000 metres on each side of bridges.
- Seals the finished Meld world’s underground void edge with two layers of bedrock, without walls between generated cells.

Entrance positions depend on the source data. Facade points are placement hints,
not surveyed door locations; exact doors and building heights are not guaranteed.

Landscape changes apply to newly generated areas; existing worlds need regeneration
to adopt them.

## Getting started

You need Rust and Python 3.12+. Live Danish data downloads require your own
Datafordeler API key; downloaded register extracts can also be imported.

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
