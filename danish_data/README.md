# Danish building data

Arnis-DK combines GeoDanmark building footprints, BBR building properties and
DAR addresses, and adds GeoDanmark's hedges and scrub where the Danish height
model confirms them. Terrain and coast rendering are handled by Arnis/Mapterhorn.

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
`DATAFORDELER_API_KEY` environment variable. For hedges, add a free
Dataforsyningen token to the same file as `DATAFORSYNINGEN_TOKEN=your-token`
(see [hedges and scrub](#hedges-and-scrub)). Keep keys and downloads under
the Git-ignored `.local/` directory.

Bounding boxes use WGS84 coordinates in **south, west, north, east** order.
A download covers at most 100 km². Datafordeler is asked for at most 10 km² at a
time: a larger area is split into tiles of 3 × 3 km, all at the same snapshot
time, and a building or hedge on a tile border is kept once. An 8 × 8 km area
takes nine tiles. For larger areas, use the [Meld integration](MELD.md), which
downloads data for individual cells.

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

`fetch.py` prints `progress <phase> <done> <total>` lines while it works, for the
phases buildings, bbr, addresses, building-addresses, entrances, hedges, heights
and selecting, so a server can show how far a download has come.

## Local store

A server that makes many worlds can keep the Danish data instead of downloading
it for each world. `store.py` saves it in tiles of 3 × 3 km on the UTM zone 32 grid
(EPSG:25832), as gzipped JSON under `<store>/tiles/`. A tile holds, unchanged, what
`fetch.py` downloads for it: the GeoDanmark buildings touching it with their BBR and
DAR records, and its hedges and scrub, with every untyped hedge already measured
against the height model.

```sh
python danish_data/store.py build --store /srv/arnis-dk-store --bbox "54.55,8.05,57.76,12.80" --bbox "54.98,14.66,55.33,15.21"
python danish_data/store.py supplement --store /srv/arnis-dk-store --bbox "55.670,12.560,55.675,12.570" --output buildings.json
```

- `supplement` assembles the supplement `fetch.py` would make for the bbox from the
  tiles under it, without a download. A record on a tile border is used once; if
  two tiles disagree, the newer one wins. For a 1.8 × 1.8 km area of Ribe it took
  5 seconds instead of 70, with the same 25,455 elements.
- `--fetch-missing` lets `supplement` download and keep the tiles the store lacks,
  so a store can also fill itself as worlds are made.
- `build` fetches the tiles of one or more bboxes that the store lacks, two at a
  time (`--workers`). Stop it and run it again to continue; a failed tile is
  reported and fetched on the next run. `--older-than DAYS` fetches older tiles
  again. The two bboxes above cover Denmark and Bornholm in about 12,900 tiles,
  most of them sea.
- `refresh --store DIR --older-than DAYS` fetches again the tiles the store holds
  that are older, and no others, so a store that fills itself keeps its size. A
  tile is replaced only once its new version is complete; a failed one keeps its
  older version. Run monthly with `--older-than 25`, for example from a timer.
- A town tile is about 0.5 MB, a rural tile far less; Denmark is estimated at
  1.5–2 GB. Keys come from `--credentials-file` or the environment, as for `fetch.py`.
- A hedge crossing a tile border is measured in each tile with that tile's
  buildings; the newer measurement is used.

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
- One exception: an OSM building drawn as a single outline over a whole block
  gives way to the GeoDanmark houses inside it. Several conditions must all hold:
  - At least three houses lie mostly inside the outline. Sheds, garages, carports
    and houses under 25 m² do not count.
  - Together the houses cover 60–110% of the outline, and no single house covers
    more than 75%.
  - The building is not a church, chapel, castle or `historic` building.

  The houses keep the outline's colour, material and floor count where the
  register has none. Without this, an old-town street drawn as one block in OSM
  became one flat building, and its houses were dropped as overlaps.
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

## Hedges and scrub

GeoDanmark maps hedges, fences and walls as `Hegn` lines, and scrub as
`KratBevoksning` areas. Most `Hegn` lines have no type (`Ikke tildelt`). Lines
typed `Levende` are hedges, and other types such as wire fences are skipped. An
untyped line becomes a hedge only where the Danish height model shows growth
along it: at least half of a 1.2 m band must stand more than 0.7 m above the
terrain. On Slagelse villa streets this kept about 55% of the untyped lines and
dropped plank and wire fences. Without a token, only lines typed `Levende` are
added.

Hedge lines and scrub areas are cut back 1 m from GeoDanmark buildings. In
Minecraft a hedge is two blocks of non-decaying oak leaves on grass, with a
one-block strip of lawn around it. It is never placed inside a building
footprint or on a road, path or other sealed surface, and it never replaces
blocks that are already there. OSM `barrier=hedge` ways follow the same rules.
Scrub areas get scattered bushes on grass.

`fetch.py` adds hedges by default; `--no-hedges` leaves them out. The height
model is downloaded only around untyped lines, in 800 m tiles at 0.8 m
resolution. Dropped requests are retried, and a lasting failure names its HTTP
status or timeout.

## Facade colours from oblique photos

`skraafoto_facades.py` measures facade and roof colours in the Danish oblique
aerial photos (skråfoto). Arnis then builds walls in those colours with
`--mapillary-facades-dir`:

```sh
python danish_data/skraafoto_facades.py --bbox 55.677,12.585,55.681,12.595 \
  --out facades --supplement buildings.json --credentials-file .local/datafordeler.env
arnis --bbox 55.677,12.585,55.681,12.595 --danish-buildings buildings.json \
  --mapillary-facades-dir facades ...
```

- **Photos:** each photo carries its camera position, rotation and lens. A point on a wall
  is therefore projected straight into the photo, with no reconstruction from photos.
- **Heights:** the Danish height model gives each wall its foot and top. A photo counts
  as seeing a wall only when the line to the camera clears the surface model.
- **Colours:**
  - Walls are sampled per metre along the wall and per metre up it.
  - Shaded walls are lifted by a shade-to-sun ratio measured over the whole run.
  - Roofs take the median colour of their footprint in the straight-down photo.
- **Matching:**
  - Arnis matches walls by node id. Each wall also carries the ids a tile-archive
    world gives its corners.
  - The register houses from `--supplement` are sampled too: those Arnis adds beside
    OSM buildings, and those it may put in place of a block.
- **OSM input:** `--osm-index` reads the OSM buildings from a local index of a
  Geofabrik extract (made with `--build-osm-index`) instead of Overpass. Multipolygon
  buildings are not in the index.
- **Credentials and speed:**
  - It needs `DATAFORSYNINGEN_TOKEN`.
  - The photo service limits each client address, so more parallel runs from one
    address do not go faster.
  - `--levels-cache` keeps each photo's colour levels between runs.

## API and implementation details

| Register | GraphQL endpoint | Data |
| --- | --- | --- |
| GeoDanmark | `https://graphql.datafordeler.dk/GEODKV/v2` | Footprints, IDs, BBR links, status and geometry metadata |
| BBR | `https://graphql.datafordeler.dk/BBR/v2` | Building use, floor counts, materials, construction year and house-number links |
| DAR | `https://graphql.datafordeler.dk/DAR/v2` | House numbers, full access addresses, building links and access points |
| Danish height model | `https://api.dataforsyningen.dk/dhm_wcs_DAF` (WCS, token) | Surface and terrain heights, for hedges and facade sampling |
| Skråfoto | `https://api.dataforsyningen.dk/rest/skraafoto_api/v1.0` (STAC, token) | Oblique and straight-down photos with their cameras, for facade and roof colours |

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

Implementation entry points: `fetch.py`, `prepare.py`, `hedges.py`,
`skraafoto_facades.py`, [`src/danish_buildings.rs`](../src/danish_buildings.rs),
[`src/mapillary/facades.rs`](../src/mapillary/facades.rs),
[`src/danish_addresses.rs`](../src/danish_addresses.rs),
[`src/danish_entrances.rs`](../src/danish_entrances.rs),
[`src/element_processing/barriers.rs`](../src/element_processing/barriers.rs) and
[`src/trees/density.rs`](../src/trees/density.rs). Use `--debug` for entrance
placement diagnostics.

## Tests and sources

```sh
python -m unittest discover -s danish_data -p "test_*.py"
cargo test --locked --no-default-features
```

The Python tests run without network access or real credentials. They cover
record linking, coordinates, pagination, hedge classification, failures,
credential handling, and the facade sampler's camera model, colours and block
houses. Rust tests cover import, the block rule, entrance placement, signs,
hedges and tree rules. Automated
checks do not replace visual inspection of buildings and cell boundaries.

- [GeoDanmark GraphQL](https://datafordeler.dk/dataoversigt/geodanmark-vektor/geodanmark-vektor-graphql/)
- [BBR GraphQL](https://datafordeler.dk/dataoversigt/bygnings-og-boligregistret-bbr/bbr-graphql/)
- [DAR GraphQL](https://datafordeler.dk/dataoversigt/danmarks-adresseregister-dar/dar-graphql/)
- Official schemas: [GeoDanmark](https://datafordeler.dk/GraphQLSchema/GEODKV.graphql), [BBR](https://datafordeler.dk/GraphQLSchema/BBR.graphql), [DAR](https://datafordeler.dk/GraphQLSchema/DAR.graphql)
- [DAR access-point standards](https://danmarksadresser.dk/adressedata/datakvalitet-hele-landet/teknisk-standard-adgangspunkter)
- [DAR lifecycle codes](https://danmarksadresser.dk/adressedata/kodelister/livscyklus)
