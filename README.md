# Arnis DK

Dette projekt bygger på **officiel Arnis v3.3.0** og supplerer dens bygninger med
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

Danske bygningsdata kan nu hentes automatisk for et valgt område via
**Datafordeler GraphQL v2**. Importen understøtter også officielle
**Current JSON-entitetsudtræk** fra Datafordeleren:

| Register og entitet | Bidrag |
| --- | --- |
| GeoDanmark Vektor / Bygning | Omrids, inklusive multipolygoner og gårdrum |
| BBR / Bygning | Anvendelse, etager, materialer, opførelsesår |
| DAR / Husnummer + Adressepunkt | Husnummer og kvalificeret dør-/facadeplacering via adgangspunkt |

Dørplacering bruger nu DAR-adgangspunkter med standard **TD** (ved indgangsdør)
eller **TK** (ved facade mod vej). Der kræves en direkte GeoDanmark-kobling og
et punkt inde i den tilknyttede bygning. Punktet flyttes til en nærliggende,
entydig væg; eksisterende OSM-indgange bevares. **TK er et facadehint, ikke en
præcis dørmåling.** Ukvalificerede eller tvetydige punkter beholder Arnis' normale
placering. Se den tekniske vejledning for afstands- og overlapfiltre.

Brug `--signage=full` til testkort med Arnis' egne vej-/trafikskilte,
bygningsskilte og adresser. Når DAR har en fuld adgangsadresse, vises den på
et almindeligt Minecraft-vægskilt ved indgangen i stedet for billedet med
kun husnummeret. Lange adresser fordeles på op til to skilte.
Det kræver ingen ændring af serverplugins i dette
projekt. Den komplette verden skal bevare både entiteter og `data/map_*.dat`.

### Hent data til det valgte område

Opret et IT-system med en API-nøgle i Datafordeler Administration. Gem nøglen i
`.local/datafordeler.env` som `DATAFORDELER_API_KEY=...`, eller sæt miljøvariablen
`DATAFORDELER_API_KEY`. Filen `.local/datafordeler.env` må ikke committes; hele
`.local/` er Git-ignoreret. Hentning bruger nøglen direkte fra filen eller miljøet,
så den ikke skal indgå i kommandoen.

```powershell
python -m venv .local/venv
.\.local\venv\Scripts\python.exe -m pip install -r danish_data/requirements.txt
.\.local\venv\Scripts\python.exe danish_data/fetch.py --credentials-file .local/datafordeler.env --bbox "55.400,11.350,55.403,11.355" --output .local/data/herrestraede-buildings.json
.\target\release\arnis.exe --bbox "55.400,11.350,55.403,11.355" --danish-buildings .local/data/herrestraede-buildings.json --output-dir .local/worlds/Herrestraede
```

Hentningen begrænses til højst 10 km² ad gangen. Den spørger først efter
GeoDanmarks bygninger i området og henter derefter kun BBR- og DAR-poster med de
tilknyttede UUID'er. Manglende forbindelser erstattes ikke af et nærmeste-adresse-gæt.
Der hentes ingen terrændata; terrænet håndteres fortsat af Arnis/Mapterhorn.

De tre kildelister gemmes i en dateret mappe ved siden af resultatfilen. Den
forberedte fil kan genbruges direkte i Arnis uden nye registeropslag. En ny
kørsel af `fetch.py` henter et nyt snapshot; der er endnu ingen automatisk
cacheudløb eller GUI-knap. Fejl under hentning eller konvertering overskriver
ikke en tidligere færdig resultatfil.

Arnis læser fortsat det færdige supplement via `--danish-buildings` eller
`ARNIS_DK_BUILDINGS`; hentningen er et separat forberedelsestrin. Den er endnu
ikke koblet til Minecraft Danmarks Creative-/GeoGuessr-worker.

Se [danish_data/README.md](danish_data/README.md) for API-kontrakt, test og
teknisk overdragelse til næste udvikler/agent.

### Brug allerede downloadede udtræk

Hver inputfil skal være et JSON-array eller en ZIP med præcis én JSON-fil.
GeoDanmarks `geometri` skal være WKT i **EPSG:25832**. Der foretages ingen
automatisk detektion af koordinatsystemet. BBR og DAR er valgfrie.
Download data gennem [Datafordeleren](https://datafordeler.dk/).
Manuelle udtræk forberedes med værktøjet nedenfor. Gem udtræk og
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
husnummer og kan nu forbedre indgangens placering med kvalificerede
adgangspunkter; det er ikke en garanti for præcis dørplacering.
Importen håndterer ikke BBR-enheder, ejeroplysninger eller persondata.

Den forberedte JSON gemmer kildefilernes navne, SHA-256, tidspunkt, optællinger
og kildeangivelse. Bevar denne fil sammen med verden ved distribution, og følg
de konkrete datasæts vilkår. Der følger ingen registerudtræk eller nøgler med repoet.

## Kontrol og kilder

```powershell
cargo test --locked --no-default-features
.\.local\venv\Scripts\python.exe -m unittest discover -s danish_data -p "test_*.py"
```

- [Arnis v3.3.0](https://github.com/louis-e/arnis/releases/tag/v3.3.0)
- [BBR's materialer og anvendelseskoder](https://bbr.dk/kodelister)
- [DAR's statuskoder](https://danmarksadresser.dk/adressedata/kodelister/livscyklus)
- [Datafordelerens grunddatamodel](https://grunddatamodel.datafordeler.dk/)

Arnis-koden er Apache-2.0. Upstreams [LICENSE](LICENSE) og [NOTICE](NOTICE) bevares.


## Store områder med Meld (eksperimentelt)

Der findes nu en kommandolinjebro, der bruger en ekstern Meld-checkout til
områdeplanlægning og Arnis-DK One World til en fælles verden med danske
bygninger og fungerende billedskilte. Den genererer delområder sekventielt
og kan genoptage afbrudte job. Ni dele på cirka 6,5 km² er afprøvet.
Det er endnu ikke Melds parallelle GUI-workflow.

Se [opsætning, testresultater og begrænsninger](danish_data/MELD.md).
