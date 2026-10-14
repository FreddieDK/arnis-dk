# Large-area generation with Meld

This experimental CLI integration divides an area into cells. Arnis-DK
generates each cell into the same Java world using **One World**, including
Danish buildings, address signs and optional caves. Cells run sequentially,
with multiple generator threads inside each cell. Interrupted jobs can be
resumed.

New jobs place cells on the grid of the classic minecraftdanmark.dk map, so
every building has the X and Z that `/atp` gives for its address (see
[coordinates](../README.md#coordinates)). Planning with `--frame meld` instead
uses the Web Mercator grid of an external [Meld](https://github.com/Teddy563/meld)
checkout: its grid planner only, not its GUI, updater or region-merging
workflow. Meld remains a separate dependency; its source is not bundled here.

## Setup

Follow the [Danish data setup guide](README.md) to build the CLI, activate a
Python 3.12+ environment and configure a Datafordeler API key. Only
`--frame meld` needs Meld, installed separately. The compatible grid version is:

```sh
git clone https://github.com/Teddy563/meld.git .local/vendor/meld
git -C .local/vendor/meld checkout 4152dcb4b1a3d322c7674c567332b039c3f05c7b
```

## Plan an area

Run from the Arnis-DK repository root with the Python environment activated.
The example uses the Windows executable; on Linux/macOS, replace
`target/release/arnis.exe` with `target/release/arnis`.

```sh
python danish_data/meld_bridge.py plan --arnis target/release/arnis.exe --job .local/meld-jobs/example --bbox "55.670,12.560,55.690,12.590" --cell-regions 2 --threads 4 --caves
```

Planning writes `plan.json` without downloading building/terrain data or
generating a world. Bounding boxes are **south, west, north, east** in WGS84.

- `--cell-regions 2` uses cells of 1,024 by 1,024 blocks, aligned to the
  minecraftdanmark.dk grid so that no two cells share a region file. The
  supported range is 1–4 regions per side; edge cells may be smaller. Each cell
  is passed to Arnis as an exact block rectangle with `--block-rect`.
- `--frame meld --meld-source .local/vendor/meld` plans on Meld's Web Mercator
  grid instead, as jobs did before.
- `--scale 1` represents approximately one metre per block. Smaller scales
  cover more geographic area per cell.
- `--threads` controls Rust worker threads inside each cell.
- `--caves` enables cave generation. `--signage full` is the default.
- `--world-height vanilla` (default) uses the normal build height, Y -64 to 319,
  with sea level at Y 63. `--world-height earth` uses the extended height instead.
- Hedges and scrub are added by default (see
  [hedges and scrub](README.md#hedges-and-scrub)). Running such a plan needs
  `DATAFORSYNINGEN_TOKEN` in the credentials file; `--no-hedges` plans without them.
- `--max-cells` limits retained cells (default 1,000, maximum 20,000).
- `--land-mask FILE` skips open-sea cells. See [ocean filtering](OCEAN.md).
- `--country FILE` keeps the map to one country. The file is a GeoJSON outline
  that includes the territorial waters, such as the OpenStreetMap boundary of
  Denmark (relation 50046).
  - Cells with none of the country's land within the coast buffer are skipped,
    unless a bridge or `--keep-bbox` keeps them.
  - In cells that reach across the border, chunks on another country's land are
    removed after generation. The map then follows the land border to within
    16 blocks, and the bedrock edge is sealed along the cut.
  - Needs `--land-mask` and the default `--frame mcdk`.

Each live Danish data request must cover at most 10 km², including the cell's
data margin. Use smaller cells if a plan exceeds this limit. Cells include
extra data around their edges for geometry processing.

## Generate or resume

```sh
python danish_data/meld_bridge.py run --job .local/meld-jobs/example --credentials-file .local/datafordeler.env
```

Repeat the same command to resume. Finished cells and `skipped_ocean` cells are
skipped. Add `--limit 1` to generate only one unfinished cell, or `--timeout 14400`
to allow up to four hours per Arnis process (default: one hour).

Danish data is downloaded per cell using the plan's fixed snapshot time and
reused on retries. The bridge stops on a failed cell; running it again retries
unfinished work. A background service or automatic whole-cell retry scheduler
is not included in this repository's CLI.

The world is saved under the job's **`worlds/Arnis-DK Meld`** directory. Preserve
the complete folder, including `data/` and `entities/`. New jobs use the normal
build height, so Bedrock players joining through Geyser can see the world and
animals spawn as in an ordinary world. At scale 1 it holds Danish terrain with
room for caves below sea level; land above about 240 m is flattened. Jobs planned
before this option, and `--world-height earth`, use the extended build height
(-2032 to 2031), which needs Minecraft Java 1.21.4+ and the included datapack and
is out of Geyser's range. A job always keeps the height its world was created
with. Keep the world closed in Minecraft and on servers while generating.

## Files, consistency and recovery

- The plan pins the generator, the Meld grid files (with `--frame meld`) and
  optional inputs by SHA-256.
  Changed inputs require the original files or a new job directory.
- A job lock prevents two bridge processes from writing simultaneously. After
  a hard interruption, check that the recorded process has stopped before
  removing a stale `runner.lock`.
- Each cell has an `arnis.log` and local register snapshots. A failed cell may
  need to be generated again. Saving is not transactional across the entire
  world; use new job worlds and back up valuable results.
- Keep jobs, data and credentials under `.local/`. The API key is not stored
  in the plan or passed to the Rust generator.
- For prepared data, pass `--danish-buildings FILE` and optionally
  `--osm-file FILE` when planning. The supplement must cover every cell,
  including padding. You are responsible for equivalent coverage in the OSM file.

## Why not replace Meld's executable directly?

This applies to `--frame meld`.

The supported Meld version expects CLI options such as `--master-origin-lat/lng`,
`--seed` and `--elevation-min/max` that this Arnis version does not provide.
Its coordinates and region-copying workflow also differ from Arnis One World,
and its region merge does not copy `data/map_*.dat` for image signs.

This bridge therefore imports only Meld's grid package. Arnis controls world
coordinates, the shared elevation mapping, chunk boundaries and map IDs. Do
not use Meld's region-copying merge on worlds created by this integration.

Reference implementation:
[`arnis_cmd.py`](https://github.com/Teddy563/meld/blob/4152dcb4b1a3d322c7674c567332b039c3f05c7b/src/arnis_cmd.py),
[`merge.py`](https://github.com/Teddy563/meld/blob/4152dcb4b1a3d322c7674c567332b039c3f05c7b/src/merge.py).

## Validation and limitations

Automated tests cover pinned inputs, locking, failure recovery, ocean skipping
and generated-area coverage. File-level checks can verify chunks and map
references, but cannot establish that every building or terrain seam looks
correct. Inspect representative cells and boundaries in Minecraft before
relying on a large generation. Complete nationwide output has not been validated.

When `--land-mask` is used, planning also fetches and pins OSM bridge geometry.
The default coastal buffer and bridge corridors both retain 2,000 metres of
water, rounded out to whole cells. See [ocean selection](OCEAN.md).
