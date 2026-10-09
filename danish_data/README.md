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
| DAR | `https://graphql.datafordeler.dk/DAR/v2` | Husnummer-ID, husnummertekst, GeoDanmark-link og adgangspunkt; Adressepunkt-position og teknisk standard |

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


## DAR-styrede indgange og indbyggede skilte (9. oktober 2026)

`fetch.py` henter nu `adgangspunkt` og `geoDanmarkBygning` på de samme
UUID-koblede DAR_Husnummer-poster og følger adgangspunktets UUID til
`DAR_Adressepunkt` på DAR/v2. Samme snapshot-tid og 100-ID-batchgrænse gælder.
Punktet skal have status 8 (i brug). Den fulde punktpost gemmes i det lokale,
normaliserede DAR-Husnummer-snapshot som `_arnis_adgangspunkt`; dette er vores
eget indlejringsfelt, ikke et officielt felt i Husnummer-udtrækket. Gamle
udtræk uden punktpost virker fortsat, men forbedrer ikke dørplaceringen.

`prepare.py` accepterer kun TD (ved indgangsdør) og TK (ved vejvendt facade).
TN (blot indenfor bygningen), UF og TA bruges ikke til døre. Punkt-ID skal
matche Husnummerets adgangspunkt, og `geoDanmarkBygning` skal matche den
aktuelle GeoDanmark-bygnings ID. Dette forhindrer, at et fælles husnummer på
BBR-bygninger giver hoveddøre på alle ejendommens garager og udhuse. Punktets
EPSG:25832-geometri skal være et gyldigt punkt inde i bygningspolygonen.

Supplementet får `arnis:entrance:lat`, `arnis:entrance:lon` og
`arnis:entrance:standard`. Disse føres med gennem den eksisterende entydige
OSM/GeoDanmark-sammenkædning. Efter bygningsmerge anvender
`src/danish_entrances.rs` samme `ProjectionSpec` som resten af Arnis og:

- Bevarer alle eksisterende OSM-entrance/door-annotationer på omridset.
- Accepterer kun en nærliggende væg, højst 6 meter plus én blok til afrunding.
- Afviser uklare valg mellem vægge, punkter ved hjørner og afklippede kortkanter.
- Afviser vægge, hvis trinnet udenfor går ind i en anden kendt bygning.
- Indsætter højst én DAR-indgang pr. matchet bygning som en normal `entrance=yes`
  node på omridset. Multipolygoner og gårdrum bevares.

Arnis' eksisterende mapped-entrance-kode bygger derefter selve døren og bruger
indgangen som anker for husnummerskiltet. Vi ændrer ikke upstreams generelle
facade- eller skiltegenerator. Indvendige dørpunkter og alle øvrige adresser på
en bygning hentes ikke; dette er en forbedring af den BBR-koblede adgang,
ikke en komplet registrering af alle indgange. Afviste hints falder tilbage
til Arnis' normale dørvalg. TD og TK ligger typisk ca. 3 meter inde i bygningen;
selv TD giver derfor ikke en garanti for præcis dørplacering i blokgitteret.

Brug `--debug` for loglinjer med DAR-indgangens bygnings-ID, TD/TK og X/Z.
`--signage=full` aktiverer upstreams egne husnumre, bygningsskilte og offentlig
skiltning. Bevar `entities/` og `data/map_*.dat` sammen med `region/`, hvis den
færdige verden flyttes. Denne ændring deployer ingen plugins og tilkobler
ikke Creative-/GeoGuessr-workers.

Kontrol omfatter Python-tests for de nye UUID-links, status, forkert bygning,
CRS og kvalitetsklasser samt Rust-tests for placering, eksisterende indgange,
tvetydige punkter, nabovægge og multipolygoner.

Kilder: [DAR teknisk standard](https://danmarksadresser.dk/adressedata/datakvalitet-hele-landet/teknisk-standard-adgangspunkter),
[DAR livscyklus](https://danmarksadresser.dk/adressedata/kodelister/livscyklus),
[officielt GraphQL-skema](https://datafordeler.dk/GraphQLSchema/DAR.graphql).


Slagelse-kontrol: samme 1.011 x 1.001-blokke-område blev genereret med
`--signage=full`. 29 TD- og 869 TK-hints i supplementet blev til 1 TD- og
175 TK-indgange efter importens filtre. Alle 176 blev bekræftet som døre med
begge halvdele i de gemte regionfiler. Arnis rapporterede 235 husnumre og
138 bygningsnavneskilte. Alle 1.165 map-item-frame-referencer kunne findes i
verdens kortdata. Test: 1.484 Rust-tests bestod (18 ignoreret), 19 Python-tests
bestod; release-build og GUI-check bestod. Der mangler fortsat visuel kontrol
i Minecraft mod de virkelige indgange.


### Almindelige adresseskilte og karm ved skrå vægge

DAR_Husnummer-feltet `adgangsadressebetegnelse` gemmes som `arnis:address` på
bygningen og kopieres gennem den samme entydige bygningsmerge. Manglende
vejnavne og postnumre gættes ikke. Det er adgangsadressen uden lejlighed,
etage eller beboeroplysninger.

Ved `--signage=full` bruger `generate_building_signage` den komplette tekst
på et almindeligt `spruce_wall_sign` med en normal sign-blokentitet og lysende
hvid tekst (`front_text.color=white`, `has_glowing_text=1`). Det bruger vanilla
glow-ink-effekten uden plugin. Andre tekstskiltes standard ændres ikke. Skiltet
placeres ved siden af døren på en eksisterende massiv væg, aldrig i selve
åbningen. Det erstatter husnummerets kortbillede, når placeringen lykkes;
andre Arnis-skilte bruger fortsat upstreams billedskilte. Basic/none får ikke
adresseskilte. Uden fuld adresse bevares eksisterende husnummerskiltning.

Teksten ombrydes til højst 15 tegn pr. linje og fire linjer pr. skilt. Op til
to skilte bruges, øverst først, hvis hele adressen kræver flere linjer. Der
skal være plads til alle sider; ellers bruges det gamle husnummer-fallback.
Teksten afkortes ikke. Danske bogstaver bevares som Unicode i NBT-teksten.

Fejlen ved Herrestræde 1C kunne reproduceres i regionfilen: døren stod i en
skrå rastervæg, hvor en side kun havde diagonal kontakt til døren. Efter
alle facade- og dekorationspas udfylder `finish_dar_doorway` kun luftceller
umiddelbart ved siden af DAR-dørens to blokke. Materialet tages fra den
tilstødende væg i samme højde, så fx stensoklen fortsætter ind til døren.
Bygningens vægmateriale bruges kun som fallback. Eksisterende facadeblokke
og overligger bevares; en overligger tilføjes kun, hvis den mangler.
Der tilføjes ikke længere hele tre blokke høje træstolper. Selve døren og
passagen bevares; andre indgange får ikke denne behandling. Regressionstesten
bruger den rapporterede bygnings skrå omrids og kontrollerer lukkede sider,
bevaret facade/overligger samt fri passage foran/bagved døren.


Verificeret på samme Slagelse-område: 345 almindelige adresseskilte i den
færdige verdensfil. Skiltet ved Herrestræde 1C indeholder præcis
"Herrestræde 1C," og "4200 Slagelse" på hver sin linje. Den tidligere åbne
karmcelle (312,-49,482) er udfyldt, begge dørhalvdele er bevaret, og felterne
foran og bagved døren er fri. Efter den diskrete karmrettelse er den gamle
sokkel, facade og overligger bevaret, og kun to sideblokke tilføjes ved 1C:
brosten nederst og granstamme ovenover. Alle 345 adresseskilte er kontrolleret
for lysende hvid tekst i NBT. 296 element-/bygningstests bestod (2 ignoreret)
og 70 world-editor-tests bestod; release-build og GUI-check bestod.
Kontrollen er foretaget i verdensfilerne, ikke visuelt i Minecraft.
