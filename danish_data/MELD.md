# Store områder med Meld og Arnis-DK

**Hav som void:** Med `plan --land-mask FILE` kan rene havfelter springes over
før hentning og generering. Land og som standard cirka 1 km kyststribe bevares.
Se [opsætning og begrænsninger for havfilteret](OCEAN.md).

Eksperimentel kommandolinjeintegration. Meld er en **ekstern afhængighed**:
vi bruger dets områdeplanlægning, mens Arnis-DK genererer delområderne i sin
indbyggede One World-verden. Der kopieres ikke Meld-kode ind i dette repository.

## Hvorfor ikke bare udskifte Melds arnis.exe?

Undersøgt mod `Teddy563/meld` commit
`4152dcb4b1a3d322c7674c567332b039c3f05c7b`:

- Melds normale kommando kræver bl.a. `--master-origin-lat/lng`, `--seed` og
  `--elevation-min/max`. Disse grænseflader findes ikke i vores Arnis 3.3.0.
- Meld og Arnis One World bruger forskellige koordinatprojektioner. Melds
  regionkopiering må derfor ikke anvendes på denne integrations verdensfiler.
- Melds `src/merge.py` kopierer ikke `data/map_*.dat`. Det ville ødelægge vores
  billedskilte; Meld vælger derfor selv `signage=none` som standard.
- En shim, der blot ignorerer ukendte parametre, ville ikke løse problemerne.

Kilder: [Meld](https://github.com/Teddy563/meld),
[`arnis_cmd.py`](https://github.com/Teddy563/meld/blob/4152dcb4b1a3d322c7674c567332b039c3f05c7b/src/arnis_cmd.py),
[`merge.py`](https://github.com/Teddy563/meld/blob/4152dcb4b1a3d322c7674c567332b039c3f05c7b/src/merge.py).

## Den første integration

`danish_data/meld_bridge.py` læser `src/grid.py` fra en lokal Meld-checkout.
Meld leverer geografiske delområder; Arnis-DK fastlægger verdens koordinater,
fælles højdemapping, chunkgrænser og kort-ID'er med `--one-world`.
Melds server, brugerflade, generatoropdatering og regionsammenlægning startes ikke.

Delområderne bygges **ét ad gangen**, med flere Rust-tråde inden i hvert område.
Det begrænser størrelsen af hvert generatorjob og understøtter genoptagelse.
Det er endnu ikke Melds parallelle GUI-workflow. Planlægningen accepterer
større områder, men landsdækkende generering er ikke valideret.

GeoDanmark/BBR/DAR hentes per delområde med overlap til geometri ved kanterne.
Alle registerforespørgsler bruger samme tidspunkt fra planen. Grænsen på
10 km² per dataforespørgsel bevares; vælg mindre celler ved lav skala.
OSM, Mapterhorn og canopy-data hentes og caches af Arnis som sædvanlig.
Bygningsforbedringer, adresseskilte og den reducerede bytrætæthed er vores
nuværende Arnis-DK-funktioner. Ingen DHM-integration.

## Brug

Byg Arnis-DK og installer de eksisterende Python-afhængigheder fra
`danish_data/requirements.txt`. Hent Meld separat, eksempelvis:

```powershell
git clone https://github.com/Teddy563/meld.git .local/vendor/meld
```

Opret en plan. Dette henter ikke kortdata og genererer ingen Minecraft-verden:

```powershell
.\.local\venv\Scripts\python.exe danish_data/meld_bridge.py plan --meld-source .local/vendor/meld --arnis target/release/arnis.exe --job .local/meld-jobs/slagelse --bbox "55.390,11.340,55.413,11.380" --cell-regions 2 --threads 4
```

Start eller fortsæt samme job:

```powershell
.\.local\venv\Scripts\python.exe danish_data/meld_bridge.py run --job .local/meld-jobs/slagelse --credentials-file .local/datafordeler.env
```

`--limit 1` bygger højst én ufærdig del, så integrationen kan prøves gradvist.
Gentag samme run-kommando for at fortsætte. Færdige dele springes over.
`--signage full` er standard; `--caves` kan vælges ved planlægning, men er
ikke en del af den første integrationstest og kan være betydeligt tungere.

Resultatet ligger under jobbets `worlds/Arnis-DK Meld`. Åbn hele denne mappe
som verden; behold `data/`, `datapacks/` og alle andre verdensfiler sammen.
One World bruger udvidet byggehøjde (-2032 til 2031), som kræver Java 1.21.4+
og det medfølgende datapack. Koordinaterne er forskellige fra de tidligere
enkeltkort. Verdenen skal være lukket i Minecraft/serveren under generering.

Planen binder generatorfil, Melds grid-filer og eventuelle lokale input med
SHA-256. En ændring kræver den oprindelige version eller en ny jobmappe.
Der køres aldrig to bridge-processer samtidigt på samme job. Efter en hård
procesafbrydelse kan `runner.lock` ligge tilbage: fjern den først, når den
registrerende proces er stoppet. En afbrudt del kan blive genereret igen.
Tag backup af værdifulde verdener; generation er ikke en transaktion på hele
verdensmappen, og værktøjet er beregnet til egne nye jobverdener.

Hver del har en `arnis.log` og lokale registersnapshots. API-nøglen gemmes
ikke i planen og gives ikke videre til generatoren. Brug `.local/` til job,
data, nøgler og testkort; de må ikke committes til GitHub.

Til offline gentest kan planen bruge `--danish-buildings FILE` og
`--osm-file FILE`. Supplementets deklarerede bbox skal dække alle delområder
inklusive overlap. OSM-filens tilsvarende dækning er brugerens ansvar.

## Validering og næste trin

Automatiske tests dækker ændrede input, låsning, fejl/genoptagelse, fastholdelse
af danske data/skilte og kontrol af genereret område i verdensmanifestet.
En rigtig fire-dels generering er stoppet efter første del og genoptaget:
alle 1.848 forventede chunks var til stede, 602 billedrammer havde gyldige
kortdata, og alle 184 kortfiler fra første del var uændrede. 609 almindelige
skilte fandtes i den færdige verden. Et efterfølgende run genererede intet igen.

Dette kontrollerer filintegritet og dækning; det er ikke en visuel godkendelse
af alle bygninger, vejovergange eller terrænsamlinger. Næste trin er visuel
kontrol af grænser, måling af ressourcer ved flere celler og derefter en
særskilt løsning til parallelle arbejdere/GUI med sikker koordinat- og
map-ID-håndtering. Live-servere og mapgen er ikke ændret.

En yderligere Slagelse-test dækkede cirka 6,5 km² i ni dele, med friske
GeoDanmark/BBR/DAR-udtræk per del og Arnis' normale online OSM/Mapterhorn-data.
Alle ni dele blev gennemført. De 25.280 forventede chunks fandtes i
verdensfilerne uden tomme chunks; alle 2.653 billedrammer havde gyldige
kortdata. Verdenen indeholdt 1.635 kortfiler og 4.628 almindelige skilte.
Alle 28 Python-tests bestod. Huler og parallelle arbejdere indgik ikke i testen.
