# Large-area generation with Meld

This experimental CLI integration uses an external [Meld](https://github.com/Teddy563/meld)
checkout to divide an area into cells. Arnis-DK generates each cell into the same
Java world using **One World**, including Danish buildings, address signs and
optional caves. Cells run sequentially, with multiple generator threads inside
each cell. Interrupted jobs can be resumed.

The integration uses Meld's grid planner, not its GUI, updater or region-merging
workflow. Meld remains a separate dependency; its source is not bundled here.

## Setup

Follow the [Danish data setup guide](README.md) to build the CLI, activate a
Python 3.12+ environment and configure a Datafordeler API key. Install Meld
separately. The compatible grid version used by this integration is:

```sh
git clone https://github.com/Teddy563/meld.git .local/vendor/meld
git -C .local/vendor/meld checkout 4152dcb4b1a3d322c7674c567332b039c3f05c7b
```

## Plan an area

Run from the Arnis-DK repository root with the Python environment activated.
The example uses the Windows executable; on Linux/macOS, replace
`target/release/arnis.exe` with `target/release/arnis`.

```sh
python danish_data/meld_bridge.py plan --meld-source .local/vendor/meld --arnis target/release/arnis.exe --job .local/meld-jobs/example --bbox "55.670,12.560,55.690,12.590" --cell-regions 2 --threads 4 --caves
```

Planning writes `plan.json` without downloading building/terrain data or
generating a world. Bounding boxes are **south, west, north, east** in WGS84.

- `--cell-regions 2` uses cells about 1,024 blocks per side. The supported range
  is 1–4 regions per side; edge cells may be smaller.
- `--scale 1` represents approximately one metre per block. Smaller scales
  cover more geographic area per cell.
- `--threads` controls Rust worker threads inside each cell.
- `--caves` enables cave generation. `--signage full` is the default.
- `--max-cells` limits retained cells (default 1,000, maximum 20,000).
- `--land-mask FILE` skips open-sea cells. See [ocean filtering](OCEAN.md).

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
the complete folder, including `data/`, `entities/` and `datapacks/`. One World
uses extended build height (-2032 to 2031), requiring Minecraft Java 1.21.4+
and the included datapack. Keep the world closed in Minecraft and on servers
while generating.

## Files, consistency and recovery

- The plan pins the generator, Meld grid files and optional inputs by SHA-256.
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
