# Skip open sea in large jobs

The [Meld planner](MELD.md) accepts `--land-mask` to skip cells that are entirely
open sea. They are marked `skipped_ocean` and are not sent to Datafordeler or
Arnis. One World uses `--world-type void`, so ungenerated areas remain empty
under normal Minecraft generation. Islands retain their geographic spacing.

The filter selects cells; Arnis/Mapterhorn still generates terrain and coastlines.
It does not clip generated blocks to an exact coastline or national border.

## Prepare a land mask

Activate the Python environment from the [setup guide](README.md) and install
`danish_data/requirements.txt`. Download the detailed, split **WGS84 land polygons**
from [OSM land polygons](https://osmdata.openstreetmap.de/data/land-polygons.html)
and save the archive as `.local/data/land-polygons-split-4326.zip`.
The download requires no API key and can be reused for many plans.

```sh
python danish_data/ocean_mask.py --archive .local/data/land-polygons-split-4326.zip --output .local/data/land-mask-dk.json
```

The default mask coverage is `53,6,59,17` in south/west/north/east order. It
includes Denmark, neighbouring land and small islands. Use `--bbox` to choose
another coverage area. Preparation stores source attribution and the archive's
SHA-256 in the local mask. No new Rust build is required.

## Plan and generate

Use a separate Meld checkout as described in the [Meld guide](MELD.md).
The example uses Windows; on Linux/macOS, omit `.exe` from the Arnis path.

```sh
python danish_data/meld_bridge.py plan --meld-source .local/vendor/meld --arnis target/release/arnis.exe --job .local/meld-jobs/islands --bbox "54.9,10.8,56.15,15.2" --cell-regions 4 --max-cells 20000 --land-mask .local/data/land-mask-dk.json
```

Planning reports retained and skipped cells and writes `plan.json`. It does
not download building/terrain data or start generation. Review the selection
before running:

```sh
python danish_data/meld_bridge.py run --job .local/meld-jobs/islands --credentials-file .local/datafordeler.env
```

**The example is not Denmark-only:** foreign land inside the bounding rectangle
is retained too. Restrict the selection separately if national borders matter.

## Selection rules

- `--coast-buffer-m 2000` retains a coastal strip by default. Entire cells and
  data margins are retained, so the actual strip may be wider. Only cells with
  no land within this distance can be skipped.
- The whole padded cell is checked against polygons, not just its centre.
  Small islands at cell edges and low-lying land are retained. Lakes enclosed
  by land polygons are treated as land for selection and do not become void.
- Unknown mask coverage means **keep the cell**. Invalid files stop planning;
  errors are not interpreted as ocean. Uncertain projected geometry uses a
  conservative bounding envelope.
- Coastal cells are generated in full. The edge against void follows cell and
  chunk boundaries, not a smooth line exactly two kilometres offshore.
- Mapped OSM road and railway bridges are retained automatically, with a
  2,000-metre corridor on each side. Bridge outlines tagged `man_made=bridge`
  are included too. Selection preserves whole cells; it does not create a
  perfectly smooth water edge or guarantee the accuracy of the rendered bridge.
- Planning with a land mask downloads bridge geometry from Overpass and pins
  it as `bridge-mask.json` in the job. Failed/incomplete requests stop planning
  rather than silently cutting bridges. An existing snapshot can be supplied
  using `--bridge-mask path/to/bridge-mask.json`; it must cover the planned bbox.
- For unmapped bridges or offshore installations, repeat
  `--keep-bbox "south,west,north,east"` to retain the required areas.
- Omit `--land-mask` to generate the entire selected rectangle. With a mask,
  `--max-cells` applies after ocean filtering. At most 200,000 candidate cells
  are considered and at most 20,000 may be retained.
- Resume skips both complete and `skipped_ocean` cells. Land and bridge mask hashes are checked;
  changed coast data requires a new plan. New plans use schema 2, while the
  runner also supports schema 1. Older runners reject schema 2.
- Changing selection requires a new plan. Filtering does not delete previously
  generated ocean or alter existing worlds. Server plugins with another world
  generator may change behaviour beyond the generated map.

## Validation and attribution

Tests cover small edge islands, coastal buffers, unknown coverage, lakes,
projections, explicitly retained areas, bridge corridors, changed masks and resuming without
downloads for skipped sea cells. Fewer cells do not imply the same percentage
reduction in runtime: land cells are usually more expensive than sea cells.

Coast data: © OpenStreetMap contributors,
[ODbL](https://osmdata.openstreetmap.de/info/license.html). Accuracy depends on
the coastline dataset's completeness and date. Keep downloaded archives and
prepared masks outside Git, for example under `.local/`.

## Protected edge for survival

New Meld plans finish by sealing the final boundary against void. The outer two
block columns become bedrock below the existing terrain or seabed surface, down
to the bottom of the generated terrain. Surface blocks and water are retained.
Open caves are closed at that boundary; internal tile seams are not sealed.
Enclosed ungenerated holes are boundaries too. This does not alter lake depths
or the web map's water rendering.

Sealing starts only when every selected cell is complete. It takes Minecraft's
session lock, backs up changed region files, replaces files atomically and can
resume an interrupted pass. Keep the backup outside the copied server world.
The status is recorded in `void-edge-seal.json` next to the plan. A large map's
final sealing step takes additional time after the cell counter reaches 100%.

To seal an older finished job, or retry a failed finalization:

```sh
python danish_data/seal_void_edges.py --job /path/to/job
```

Do not run against an active Minecraft server. Install the Python requirements
first. This tool expects modern Java chunks and a chunk-aligned One World
manifest; unsupported data fails rather than being discarded.

When adding cells through the Meld runner, the previous seal is automatically
restored before generation, then recalculated after completion. Restoration
refuses to overwrite regions changed after sealing (for example by players).
Manual restoration uses the same validation:

```sh
python danish_data/seal_void_edges.py --job /path/to/job --restore
```

Generation and sealing operate on the generated source world. A separately
running server copy must be updated safely afterwards; it is not edited live.
