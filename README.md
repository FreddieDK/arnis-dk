# Arnis DK

Dette projekt bygger på **officiel Arnis v3.2.0** og supplerer dens bygninger med
danske registerdata. Terræn og kyster følger upstream. Mapterhorn er den normale
terrænkilde i Danmark; Arnis beholder sin AWS-reserve ved fejl. Den tidligere
direkte DHM/WCS-integration, DHM-token og lokale kystrettelser er fjernet.

## Byg og kør

Rust-versionen er fastlagt i `rust-toolchain.toml`. Kør fra projektmappen:

```powershell
cargo build --release --locked --no-default-features
.\target\release\arnis.exe --bbox "55.398,11.349,55.410,11.369" --output-dir .local/worlds/Slagelse
```

Terræn er aktiveret som standard. `--mode geo-only` giver fladt terræn.
`cargo run --release --locked` bygger og starter upstreams GUI. På Windows køres
GUI-buildet i en Visual Studio Developer PowerShell med Windows SDK (RC.EXE) på PATH.
Se også [upstreams dokumentation](README.upstream.md).

## Danske bygninger

Importen læser officielle **Current JSON-entitetsudtræk** fra Datafordeleren:

| Register og entitet | Bidrag |
| --- | --- |
| GeoDanmark Vektor / Bygning | Omrids, inklusive multipolygoner og gårdrum |
| BBR / Bygning | Anvendelse, etager, materialer, opførelsesår |
| DAR / Husnummer | Husnummer knyttet til BBR-bygningens adresse-id |

Hver inputfil skal være et JSON-array eller en ZIP med præcis én JSON-fil.
GeoDanmarks `geometri` skal være WKT i **EPSG:25832**. Der foretages ingen
automatisk detektion af koordinatsystemet. BBR og DAR er valgfrie.
Download data gennem [Datafordeleren](https://datafordeler.dk/).
Der er endnu **ingen automatisk hentning af danske registre** i Arnis eller en
filvælger i GUI'en; udtræk forberedes med værktøjet nedenfor. Gem udtræk og
eventuelle adgangsnøgler under den Git-ignorerede `.local/`.

```powershell
python -m venv .local/venv
.\.local\venv\Scripts\python.exe -m pip install -r danish_data/requirements.txt
.\.local\venv\Scripts\python.exe danish_data/prepare.py --geodanmark .local/data/Bygning.zip --bbr .local/data/BBR-Bygning.zip --dar .local/data/Husnummer.zip --bbox "55.398,11.349,55.410,11.369" --output .local/data/slagelse-buildings.json
.\target\release\arnis.exe --bbox "55.398,11.349,55.410,11.369" --danish-buildings .local/data/slagelse-buildings.json --output-dir .local/worlds/Slagelse
```

Til GUI'en sættes `ARNIS_DK_BUILDINGS` til den forberedte fils absolutte sti,
før appen startes. Den bruges under verdensgenereringen; upstreams 3D-preview
viser fortsat OSM/Overture. Den forberedte fils bbox skal dække hele det valgte område. Uden filen fungerer Arnis som upstream. En angivet,
men ugyldig fil giver fejl i stedet for tavst at udelade danske bygninger.

Sammenkædningen bruger `GeoDanmark.BBRUUID -> BBR.id_lokalId` og
`BBR.husnummer -> DAR.id_lokalId`. Der gættes ikke på den nærmeste bygning eller
adresse. Kun gældende registreringer med GeoDanmark-status `Anlagt`, BBR-status
`6` og DAR-status `3` anvendes. `--at` kan fastlåse udvælgelsestidspunktet.
Et Current-udtræk indeholder ikke nødvendigvis historiske versioner.

OSM-omrids og eksisterende oplysninger bevares. Entydige polygonmatch med mindst
60 % intersection-over-union kan få manglende egenskaber fra BBR/DAR; ukendte
materialekoder bliver ikke oversat. `building=yes` kan få en kendt BBR-type.
GeoDanmark-omrids tilføjes, når de ikke overlapper eksisterende bygninger.
Berøring langs en fælles kant er tilladt. Tvetydige overlap udelades, hvilket
bevidst kan efterlade manglende tilbygninger. Overture supplerer efter denne
fletning og kontrolleres også mod danske omrids og multipolygoner.

GeoDanmarks absolutte Z-koter tolkes **ikke** som bygningshøjder. Etageantal er
registerdata, mens Arnis' deraf afledte højde stadig er et skøn. DAR tilføjer
husnummer, men opretter ikke nye døre eller påstår præcise dørplaceringer.
Importen håndterer ikke BBR-enheder, ejeroplysninger eller persondata.

Den forberedte JSON gemmer kildefilernes navne, SHA-256, tidspunkt, optællinger
og kildeangivelse. Bevar denne fil sammen med verden ved distribution, og følg
de konkrete datasæts vilkår. Der følger ingen registerudtræk eller nøgler med repoet.

## Kontrol og kilder

```powershell
cargo test --locked --no-default-features
.\.local\venv\Scripts\python.exe -m unittest discover -s danish_data -p "test_*.py"
```

- [Arnis v3.2.0](https://github.com/louis-e/arnis/releases/tag/v3.2.0)
- [BBR's materialer og anvendelseskoder](https://bbr.dk/kodelister)
- [DAR's statuskoder](https://danmarksadresser.dk/adressedata/kodelister/livscyklus)
- [Datafordelerens grunddatamodel](https://grunddatamodel.datafordeler.dk/)

Arnis-koden er Apache-2.0. Upstreams [LICENSE](LICENSE) og [NOTICE](NOTICE) bevares.
