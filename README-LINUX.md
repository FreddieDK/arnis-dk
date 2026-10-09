# Arnis-DK til Linux (CLI)

Færdigbygget til **x86_64 / Intel / AMD**, Ubuntu 22.04 eller nyere og Debian 12
eller nyere. Kræver glibc 2.35+, almindelige systembiblioteker og CA-certifikater.
Pakken er ikke til ARM/Raspberry Pi eller Alpine Linux. Rust, Node.js, Java og
en grafisk brugerflade er ikke nødvendige for selve genereringen.

## Pak ud og start

Overfør `.tar.gz` og den tilhørende `.sha256` til Linux-maskinen:

```bash
sha256sum -c arnis-dk-linux-x86_64.tar.gz.sha256
tar -xzf arnis-dk-linux-x86_64.tar.gz
cd arnis-dk-linux-x86_64
./arnis --help
```

Generér et område (bbox = syd, vest, nord, øst):

```bash
./arnis --bbox '55.670,12.560,55.675,12.570' \
  --output-dir ./worlds/test --mode geo-terrain --signage full --caves
```

Internet bruges til bl.a. OSM, Mapterhorn og vegetationsdata. Terræn kommer fra
Arnis/Mapterhorn. Træer, facader og almindelige dekorationer er indbygget i
programmet. Huler virker uden separat asset-pack; en valgfri `cave-pack` med
ekstra formationer følger ikke med. Udelad `--caves` for hurtigere generering.
Behold hele verdensmappen, inklusive `data/` og `datapacks/`, når du overfører
den til Minecraft. Generér ikke ind i en verden, som en server har åben.

## GeoDanmark, BBR og DAR

Arnis-DK læser et forberedt supplement. Det henter ikke de danske registre
automatisk ved almindelig kørsel. Python **3.11 eller nyere** kræves kun til
dataværktøjerne og Meld-integrationen. Installer Python og venv via din
distributions pakkehåndtering, og opret derefter et miljø:

```bash
python3 -m venv .venv
.venv/bin/pip install -r danish_data/requirements.txt
```

Gem din Datafordeler-nøgle lokalt i en fil, f.eks. `datafordeler.env`, med
indholdet `DATAFORDELER_API_KEY=din_noegle`. Del ikke filen. Begræns adgangen:

```bash
chmod 600 datafordeler.env
.venv/bin/python danish_data/fetch.py \
  --bbox '55.670,12.560,55.675,12.570' \
  --credentials-file datafordeler.env --output buildings.json
./arnis --bbox '55.670,12.560,55.675,12.570' \
  --output-dir ./worlds/dansk-test --mode geo-terrain \
  --danish-buildings buildings.json --signage full --caves
```

Du kan også overføre en allerede forberedt `buildings.json`; så behøver denne
maskine hverken Python eller API-nøgle til selve genereringen. Supplementet skal
dække det valgte område. Se `danish_data/README.md` for datakilder og begrænsninger.

## Større områder med ekstern Meld

Meld følger ikke med pakken. Hent den separat med Git. Nedenstående version er
den, integrationen er afprøvet imod:

```bash
git clone https://github.com/Teddy563/meld.git meld
git -C meld checkout 4152dcb4b1a3d322c7674c567332b039c3f05c7b
.venv/bin/python danish_data/meld_bridge.py plan \
  --meld-source ./meld --arnis ./arnis --job ./jobs/test \
  --bbox '55.660,12.550,55.680,12.580' --cell-regions 2 --threads 4
.venv/bin/python danish_data/meld_bridge.py run \
  --job ./jobs/test --credentials-file datafordeler.env
```

Gentag `run` for at fortsætte efter en afbrydelse. Verdenen ligger i
`jobs/test/worlds/Arnis-DK Meld`. `--caves` kan tilføjes til `plan`.
Integrationen bruger Melds områdeinddeling og Arnis One World, én del ad gangen;
den erstatter ikke binærfilen i Melds normale GUI. Se `danish_data/MELD.md`.

## Byg igen

GitHub Actions-workflowet **Linux CLI** kan startes manuelt og leverer samme
pakkeformat. Kildecommit står i `BUILD-INFO.txt`. Linux-builden bruger den
fastlagte Rust-version og `Cargo.lock`:

```bash
cargo build --release --locked --no-default-features
python3 .github/scripts/package_linux.py
```

Pakkescriptet kræver en ny/tom `dist/arnis-dk-linux-x86_64`-mappe.
Arnis-DK er en modificeret udgave af Arnis. Licenser og kildeangivelser er i
`LICENSE`, `NOTICE`, `CREDITS.html` og `TREE-ATTRIBUTION.md`.
