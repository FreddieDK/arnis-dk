# Danske bygninger: hentning og import

Terræn, højder og kyster tilhører upstream Arnis/Mapterhorn. Denne mappe håndterer
kun GeoDanmark-bygninger og tilknyttede BBR-/DAR-egenskaber.

## Dataflow

1. `fetch.py` læser en API-nøgle fra `DATAFORDELER_API_KEY` eller en lokal fil
   valgt med `--credentials-file`. Nøglen sendes kun til de faste HTTPS-endpoints
   på `graphql.datafordeler.dk`. Ingen nøgler eller autentificerede URL'er gemmes
   i output eller udskrives i fejlbeskeder.
2. Bbox er WGS84 i rækkefølgen syd, vest, nord, øst. Området valideres til dansk
   geografi og højst 10 km². Pyproj omregner en kantfortættet envelope til
   EPSG:25832: GeoDanmarks `intersects`-filter kræver feltets eget CRS.
3. `GEODKV_Bygning` vælges med `geometri.intersects`, status `Anlagt` og
   geometristatus `Endelig`. Bygninger under terræn udelades. `prepare.py`
   foretager den endelige afgrænsning mod det oprindelige WGS84-område.
4. BBR hentes med GeoDanmarks `BBRUUID`; DAR hentes med BBR's `husnummer`.
   Der sendes højst 100 UUID'er i hvert `id_lokalId.in`-filter. En tom liste
   medfører ingen forespørgsel. Ukendte returnerede UUID'er afvises.
5. Alle forespørgsler bruger samme UTC-tidspunkt som `registreringstid` og
   `virkningstid`. Pagination bruger `first: 100`, `after` og
   `pageInfo { hasNextPage endCursor }`. Manglende/stagnerende pagination eller
   GraphQL-fejl afviser hele kørslen; delvise data accepteres ikke.
6. Geometri skal returneres som `{ wkt, crs: 25832 }`. WKT og de tre BBR-felter
   `byg032YdervaeggensMateriale`, `byg033Tagdaekningsmateriale` og
   `byg026Opfoerelsesaar` normaliseres til det eksisterende `prepare.py`-format.
7. Der gemmes et lokalt snapshot med de tre begrænsede kildelister, hvorefter
   `prepare.py` bygger samme `arnis-dk.buildings/1`-format som for filudtræk.
   SHA-256 i `sources` refererer til de normaliserede kildelister. `download`
   angiver tjenesten og snapshotmappen. Resultatfilen erstattes atomisk, når
   hele forløbet er lykkedes.

HTTP-kald har 60 sekunders timeout og højst tre forsøg ved forbindelsesfejl,
429 og midlertidige serverfejl. 401/403, ugyldige forespørgsler og datavalideringsfejl
medfører stop. Der er en sikkerhedsgrænse på 1.000 sider pr. forespørgsel og
16 MiB pr. HTTP-svar. `fetch.py` er et separat CLI-forberedelsestrin, ikke en
automatisk del af Rust-generatoren, GUI'en eller mapgen-serverne.

## API og kilder

Verificeret 9. oktober 2026 med læsende forespørgsler:

| Register | Endpoint | Felter |
| --- | --- | --- |
| GeoDanmark | `https://graphql.datafordeler.dk/GEODKV/v2` | Bygnings-ID, BBRUUID, status/tider, geometristatus, metode3D, WKT/CRS |
| BBR | `https://graphql.datafordeler.dk/BBR/v2` | Bygnings-ID, husnummer-ID, status/tider, anvendelse, etager, materialer, opførelsesår |
| DAR | `https://graphql.datafordeler.dk/DAR/v2` | Husnummer-ID, status/tider, husnummertekst |

Der hentes ikke ejere, beboere eller BBR-enheder. Ældre dokumentation viser
BBR/DAR v1; disse adresser svarede 404 under testen. Brug v2.

- [GeoDanmark GraphQL](https://datafordeler.dk/dataoversigt/geodanmark-vektor/geodanmark-vektor-graphql/)
- [BBR GraphQL](https://datafordeler.dk/dataoversigt/bygnings-og-boligregistret-bbr/bbr-graphql/)
- [DAR GraphQL](https://datafordeler.dk/dataoversigt/danmarks-adresseregister-dar/dar-graphql/)
- Offentlige skemaer: [GeoDanmark](https://datafordeler.dk/GraphQLSchema/GEODKV.graphql), [BBR](https://datafordeler.dk/GraphQLSchema/BBR.graphql), [DAR](https://datafordeler.dk/GraphQLSchema/DAR.graphql).

## Test og faktisk generering

`python -m unittest discover -s danish_data -p "test_*.py"` kører uden netværk
eller rigtige nøgler. Testene dækker sammenkædning, koordinater, feltnavne,
pagination, batchgrænsen, ufuldstændige svar, beskyttelse af nøglen i fejlbeskeder
og bevarelse af en eksisterende resultatfil ved fejl.

Et rigtigt udtræk den 9. oktober 2026 for bbox
`55.400,11.350,55.403,11.355` i Slagelse gav 158 GeoDanmark-poster i den
transformerede søgeomkreds og 147 bygninger efter den præcise afgrænsning.
129 havde match i både BBR og DAR. En efterfølgende lokal Arnis 3.2.0-generering
med supplementet lykkedes: 1 dansk bygningsomrids tilføjet, 46 eksisterende
bygninger suppleret. 145 overlappende danske omrids blev ikke indsat som ekstra
bygninger; dette tal omfatter de 46, hvor egenskaber kunne suppleres.

Dette bekræfter hentning, import og gemning af verdenen. Det er ikke en visuel
kontrol i Minecraft af hver bygning, og det dokumenterer ikke præcise dørplaceringer.
Rådata, nøgler, logs og testverden er lokale og indgår ikke i Git.


### Gentaget med Arnis 3.3.0

Den 9. oktober 2026 blev samme bbox og gemte OSM-udtræk genereret med både
upstreams officielle Windows-udgave v3.3.0 og Arnis DK bygget på samme tag
(`c96872e0fa21573402d1b731ea87f796e8caa1d2`). Begge brugte Mapterhorn,
skala 1 og lokal projektion, med `--map-item=false --signage=none`.
DK-kørslen fik desuden det samme danske registerudtræk som ovenfor.

Begge verdener blev gemt korrekt med samme geografiske grænser og størrelse
(316 x 334 blokke). DK-resultatet var fortsat 1 tilføjet og 46 supplerede
bygninger samt 145 udeladte overlappende omrids. Dette er en kontrol af import
og generering, ikke en visuel kvalitetsvurdering i Minecraft.

Tilpasningen til 3.3.0 bruger upstreams `ProjectionSpec::from_args` ved import
og bevarer oprydning af One World-kørsler ved importfejl. Terrænlogikken er
uændret fra upstream. Kontrol: 1.480 Rust-tests bestod (18 ignoreret), og alle
16 Python-tests bestod.
