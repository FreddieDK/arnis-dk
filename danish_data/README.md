# Danish building data

Arnis-DK combines GeoDanmark building footprints, BBR building properties and
DAR addresses. Terrain and coast rendering are handled by Arnis/Mapterhorn.

## Setup

Run these commands from the repository root. Install Rust using the project's
`rust-toolchain.toml` and Python 3.12+.

```sh
python -m venv .local/venv
```

Activate the environment on Windows PowerShell:

```powershell
.\.local\venv\Scripts\Activate.ps1
```

Or on Linux/macOS:

```sh
source .local/venv/bin/activate
```

Install the data tools and build the CLI:

```sh
python -m pip install -r danish_data/requirements.txt
cargo build --release --locked --no-default-features
```

## Download data for an area

Create an IT system and API key in Datafordeler Administration. Save the key
in `.local/datafordeler.env` as `DATAFORDELER_API_KEY=your-key`, or set the
`DATAFORDELER_API_KEY` environment variable. Keep keys and downloads under
the Git-ignored `.local/` directory.

Bounding boxes use WGS84 coordinates in **south, west, north, east** order.
Each download is limited to 10 km². For larger areas, use the
[Meld integration](MELD.md), which downloads data for individual cells.

```sh
python danish_data/fetch.py --credentials-file .local/datafordeler.env --bbox "55.670,12.560,55.675,12.570" --output .local/data/buildings.json
cargo run --release --locked --no-default-features -- --bbox "55.670,12.560,55.675,12.570" --danish-buildings .local/data/buildings.json --output-dir .local/worlds/example --signage full --caves
```

The download creates a reusable supplement and a dated folder containing the
normalized source records. Running `fetch.py` again downloads a new snapshot.
A failed download or conversion does not replace an existing finished supplement.

`fetch.py` is a separate preparation step. The Rust CLI and GUI do not fetch
Danish register data themselves. For the GUI, set `ARNIS_DK_BUILDINGS` to the
supplement's absolute path before launching Arnis. The supplement must cover
the selected area; the GUI's 3D preview still uses OSM/Overture data.

## Import existing extracts

`prepare.py` accepts Datafordeler Current JSON entity extracts. Each input must
be a JSON array, or a ZIP containing exactly one JSON file. GeoDanmark geometry
must be WKT in **EPSG:25832**; coordinate systems are not detected automatically.
BBR and DAR inputs are optional.

```sh
python danish_data/prepare.py --geodanmark .local/data/Bygning.zip --bbr .local/data/BBR-Bygning.zip --dar .local/data/Husnummer.zip --bbox "55.670,12.560,55.675,12.570" --output .local/data/buildings.json
```

Use the output with `--danish-buildings` as above. `--at` accepts a timestamp
with a timezone to select records valid at that time; a Current extract may
not contain historical versions. Extracts without embedded access-point data
can supply addresses but cannot improve door placement.

## How records are joined

- GeoDanmark's `BBRUUID` links to `BBR.id_lokalId`; BBR's `husnummer` links to
  `DAR.id_lokalId`. DAR's `geoDanmarkBygning` also supplies additional house
  numbers on the exact building. Nearest-building or nearest-address guesses
  are not used.
- Active GeoDanmark buildings require status `Anlagt` and geometry status
  `Endelig`; underground buildings are excluded. BBR status `6`, DAR house-number
  status `3` and access-point status `8` are used.
- Existing OSM footprints are retained. Unambiguous polygon matches with at
  least 60% intersection-over-union can receive missing properties. New
  GeoDanmark footprints are added only when they do not overlap existing
  buildings; shared edges are allowed. Ambiguous overlaps are omitted.
- Overture runs after the Danish merge and checks against the resulting
  footprints. Unknown material codes are not translated. Absolute GeoDanmark
  Z coordinates are not treated as building heights; heights derived from
  floor counts remain estimates.

## Entrances and address signs

DAR access points with technical standard **TD** indicate an entrance door;
**TK** indicates a facade facing the road. TN, UF and TA are not used for doors.
The point must belong to the exact GeoDanmark building, match the house number's
access-point ID and lie inside its polygon. TK is a facade hint, and even TD
does not guarantee an exact door position in Minecraft.

Qualified points are stored in the supplement's `arnis:entrances` list, with
`address`, `lat`, `lon` and `standard`. The normalized DAR snapshot embeds the
linked point as `_arnis_adgangspunkt`; this is an internal field, not an official
Husnummer field. Legacy single-point tags remain readable.

The generator preserves existing OSM entrance annotations. It selects a nearby
wall within six metres plus one block for rounding, rejecting ambiguous walls,
corners, clipped map edges and entrances facing into another building.

When several GeoDanmark buildings lie inside one OSM outline, qualified address
points can be transferred separately if at least 95% of the source building
lies inside exactly one outline. `building:part` is excluded. This transfers
address hints, not materials or floor counts. Doors must stay within two metres
(at least two blocks) of the original GeoDanmark wall. Candidates less than
three blocks apart are rejected as ambiguous.

With `--signage full`, complete DAR addresses use ordinary spruce wall signs
with glowing white text. Text wraps at up to 15 characters per line, four lines
per sign and at most two signs; it is not truncated. Signs need a solid backing
and free space beside the entrance. If there is insufficient room, the existing
house-number fallback is used. Missing or ambiguous addresses are never invented.
`basic` and `none` do not add these full-address signs.

Doorway finishing fills empty side gaps beside intact door pairs after facade
decoration, using adjacent wall materials. It preserves the doorway, existing
facade and lintel, and both leaves of double doors. Sheds and garages use the
same entrance rendering and finishing path.

Image signs use item frames with `Fixed=0`, `Invisible=1` and `ItemDropChance=0`:
they can be broken in survival and follow normal support-block physics without
dropping loose map items. Ordinary address signs are normal breakable blocks.
These settings apply to newly generated frames; existing worlds are not updated.
To update nearby old frames, an operator can run:

```mcfunction
/execute as @e[type=minecraft:item_frame,distance=..16] run data merge entity @s {Fixed:0b}
/execute as @e[type=minecraft:glow_item_frame,distance=..16] run data merge entity @s {Fixed:0b}
```

These commands affect every frame of the given type within 16 blocks. No server
plugin is required. When moving a world, keep `entities/`, `data/map_*.dat`,
`region/` and the rest of the world folder together.

## Urban trees

Tree roots require natural ground, including grass, soil, moss, mud, sand or
snow. Roads, paved plazas, sidewalks and other sealed surfaces are excluded,
including for explicitly mapped OSM trees and tree rows. Placement is checked
again after a tree template snaps to its grid. Trees inside buildings, under
bridges or on water are rejected.

Urban areas are identified from OSM residential/commercial/retail/industrial
land use and nearby built-up land-cover samples at 24 and 64 metres. Only 15%
of candidate trunk positions are retained using a deterministic coordinate
hash. This is not a guarantee of exactly 85% fewer trees or leaf blocks.

Explicit `landuse=forest` and `natural=wood` areas are exempt from thinning,
including forests inside urban areas; polygon holes are not treated as forest.
Natural-ground requirements still apply. Unmapped woodland near buildings can
still be thinned. Existing worlds are not modified automatically.

## API and implementation details

| Register | GraphQL endpoint | Data |
| --- | --- | --- |
| GeoDanmark | `https://graphql.datafordeler.dk/GEODKV/v2` | Footprints, IDs, BBR links, status and geometry metadata |
| BBR | `https://graphql.datafordeler.dk/BBR/v2` | Building use, floor counts, materials, construction year and house-number links |
| DAR | `https://graphql.datafordeler.dk/DAR/v2` | House numbers, full access addresses, building links and access points |

The importer does not request owners, residents or BBR units. It uses the same
UTC snapshot for `registreringstid` and `virkningstid` across all queries.
UUID filters and pagination use batches of 100. Geometry requests transform a
densified bounding envelope to EPSG:25832; final clipping uses the requested bbox.

HTTP requests time out after 60 seconds and retry connection failures, HTTP 429
and temporary server errors up to three attempts. Authentication errors, invalid
queries, inconsistent records and incomplete pagination stop the import. Limits
are 1,000 pages per query and 16 MiB per response. Keys are sent only to the fixed
Datafordeler HTTPS endpoints and are excluded from output and error messages.

Prepared supplements use `arnis-dk.buildings/1` and include source filenames,
SHA-256 hashes, snapshot time, counts and attribution. Preserve this metadata
when distributing a world and follow the source datasets' terms. Raw register
extracts and credentials are not included in the repository.

Implementation entry points: `fetch.py`, `prepare.py`,
[`src/danish_addresses.rs`](../src/danish_addresses.rs),
[`src/danish_entrances.rs`](../src/danish_entrances.rs) and
[`src/trees/density.rs`](../src/trees/density.rs). Use `--debug` for entrance
placement diagnostics.

## Tests and sources

```sh
python -m unittest discover -s danish_data -p "test_*.py"
cargo test --locked --no-default-features
```

The Python tests run without network access or real credentials. They cover
record linking, coordinates, pagination, failures and credential handling.
Rust tests cover import, entrance placement, signs and tree rules. Automated
checks do not replace visual inspection of buildings and cell boundaries.

- [GeoDanmark GraphQL](https://datafordeler.dk/dataoversigt/geodanmark-vektor/geodanmark-vektor-graphql/)
- [BBR GraphQL](https://datafordeler.dk/dataoversigt/bygnings-og-boligregistret-bbr/bbr-graphql/)
- [DAR GraphQL](https://datafordeler.dk/dataoversigt/danmarks-adresseregister-dar/dar-graphql/)
- Official schemas: [GeoDanmark](https://datafordeler.dk/GraphQLSchema/GEODKV.graphql), [BBR](https://datafordeler.dk/GraphQLSchema/BBR.graphql), [DAR](https://datafordeler.dk/GraphQLSchema/DAR.graphql)
- [DAR access-point standards](https://danmarksadresser.dk/adressedata/datakvalitet-hele-landet/teknisk-standard-adgangspunkter)
- [DAR lifecycle codes](https://danmarksadresser.dk/adressedata/kodelister/livscyklus)
