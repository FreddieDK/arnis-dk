# Spring åbent hav over i store Meld-job

Med `--land-mask` springer planlæggeren hele delområder over, når de ligger
helt ude på åbent hav. De markeres `skipped_ocean` i planen og sendes hverken
til Datafordeler eller Arnis. Arnis One World bruger `--world-type void`, så
ikke-genererede områder forbliver tomme ved almindelig Minecraft-generering.
Øerne beholder deres geografiske afstand og placering i den fælles verden.

Dette ændrer kun udvælgelsen af delområder. Terræn og selve kystens udformning
genereres fortsat af Arnis/Mapterhorn; der er ingen DHM- eller kystrettelse.

## Forbered kystdata én gang

Installer projektets Python-afhængigheder. Download de detaljerede, opdelte
**WGS84-landpolygoner** fra [OSM land polygons](https://osmdata.openstreetmap.de/data/land-polygons.html).
Arkivet er omkring 930 MB ved denne tests dato. Gem det lokalt, f.eks. under
`.local/data/land-polygons-split-4326.zip`. Det er en engangsoverførsel, og det
samme arkiv kan bruges til mange planer. Download kræver ingen API-nøgle.

```powershell
.\.local\venv\Scripts\python.exe -m pip install -r danish_data/requirements.txt
.\.local\venv\Scripts\python.exe danish_data/ocean_mask.py --archive .local/data/land-polygons-split-4326.zip --output .local/data/land-mask-dk.json
```

På Linux erstattes Python-stien med din Python 3.12+-installation/venv.
Der behøves ingen ny Rust-build. Standarddækningen for masken er
`53,6,59,17`, inklusive nabolandenes land og små øer. Masken gemmes lokalt,
med kildehenvisning og arkivets SHA-256. Den fylder omkring 234 MB i denne test.
Der kopieres ikke kystdata eller private nøgler ind i Git-repositoryet.

## Planlæg og gennemse før generering

```powershell
.\.local\venv\Scripts\python.exe danish_data/meld_bridge.py plan --meld-source .local/vendor/meld --arnis target/release/arnis.exe --job .local/meld-jobs/islands --bbox "54.9,10.8,56.15,15.2" --cell-regions 4 --max-cells 20000 --land-mask .local/data/land-mask-dk.json
```

Plan-kommandoen henter ingen bygnings-/terrændata og starter ikke generering.
Den skriver antal bevarede og oversprungne felter samt en `plan.json`.
Start først det store arbejde med den sædvanlige kommando:

```powershell
.\.local\venv\Scripts\python.exe danish_data/meld_bridge.py run --job .local/meld-jobs/islands --credentials-file .local/datafordeler.env
```

## Regler og afgrænsninger

- Standard `--coast-buffer-m 1000` bevarer en havstribe omkring land. Hele
  felter og Arnis' overlap bevares, så den faktiske stribe kan være bredere.
  Kun felter helt uden land inden for denne afstand kan springes over.
- Der testes polygonoverlap for hele feltet inklusive datamargin, ikke blot
  feltets midtpunkt. Små øer ved kanten og lavtliggende land beholdes.
  Søer inde i landpolygoner behandles som land, så de heller ikke bliver void.
- Manglende dækning betyder **bevar feltet**. Ugyldige filer stopper
  planlægningen; fejl må ikke fortolkes som hav. Usikker geometri efter
  projektion erstattes konservativt med dens fulde afgrænsning.
- Masken angiver kystland, ikke broer eller havinstallationer. Brug gentagne
  `--keep-bbox "syd,vest,nord,øst"` til områder, som altid skal genereres,
  eksempelvis en lang bro eller en havvindmøllepark. De bevares også uden land.
- Felter med kyst genereres fuldt ud. Kanten mod void følger derfor
  delområder/chunks, ikke en glat linje præcis 1 km fra kysten.
- Dette er et havfilter, ikke et filter for bestemte danske øer: svensk land
  eller andre øer inden for dit valgte rektangel bevares også.
- Udelad `--land-mask` for den tidligere adfærd, hvor hele rektanglet bygges.
  Ved brug af masken gælder `--max-cells` efter havfiltrering; højst 200.000
  kandidatfelter undersøges, og højst 20.000 felter må beholdes.
- Genoptagelse springer både færdige felter og `skipped_ocean` over. Maskens
  hash kontrolleres, så ændrede kystdata kræver en ny plan. Nye planer bruger
  format 2, som gamle værktøjer afviser; det nye værktøj kan fortsat læse format 1.
- Lav en ny plan for at ændre udvælgelsen. Tidligere genererede havområder
  eller eksisterende verdener bliver ikke slettet af filteret. Serverplugins,
  der bruger en anden verdensgenerator, kan ændre adfærden uden for kortet.

## Kontrol den 9. oktober 2026

En plan for bbox `54.9,10.8,56.15,15.2`, skala 1 og fire regioner pr. felt
beholdt **5.161** felter og sprang **4.223** over ud af **9.384**.
Det er cirka **45 % færre generatorjob**; tidsbesparelsen er ikke målt og
følger ikke nødvendigvis samme procent, fordi landfelter er tungere end hav.
Hele dette område er **ikke** genereret.

Punktprøver bevarede Sjælland, Bornholm, Christiansø, Saltholm og lavtliggende
land på Lolland; et punkt ude i Østersøen blev klassificeret som åbent hav.
Automatiske tests dækker små øer ved feltkanter, kystbuffer, ukendt dækning,
søer, forkerte projektioner, tvungent bevarede områder, ændret maske og
genoptagelse uden datakald/generering for havfelter.

Kystdata: © OpenStreetMap-bidragsydere, [ODbL](https://osmdata.openstreetmap.de/info/license.html).
Havfiltreringens nøjagtighed afhænger af kystdatasættets fuldstændighed og dato.
