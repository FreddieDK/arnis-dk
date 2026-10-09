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
4. BBR hentes med GeoDanmarks `BBRUUID`. DAR hentes både med BBR's `husnummer`
   og via `geoDanmarkBygning.in` for alle valgte GeoDanmark-bygninger, så øvrige
   husnumre på samme bygning kommer med. Hvert filter har højst 100 ID'er.
   En tom liste medfører ingen forespørgsel. Ukendte returnerede links afvises.
   Identiske DAR-poster fra de to søgeveje samles; modstridende eller flere
   aktuelle versioner inden for samme søgevej afvises.
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

Supplementet får en JSON-liste i `arnis:entrances`, med `address`, `lat`, `lon`
og `standard` for hvert kvalificeret husnummer på den præcise GeoDanmark-bygning.
Denne kobling kræver ikke, at adressen er BBR's hovedadresse, eller at bygningen
har et BBR-match. De ældre enkeltfelter `arnis:entrance:lat`,
`arnis:entrance:lon` og `arnis:entrance:standard` bevares som bagudkompatibilitet.
En ny generator foretrækker listen, så hovedadressen ikke indsættes to gange.
Efter bygningsmerge anvender
`src/danish_entrances.rs` samme `ProjectionSpec` som resten af Arnis og:

- Bevarer alle eksisterende OSM-entrance/door-annotationer på omridset.
- Accepterer kun en nærliggende væg, højst 6 meter plus én blok til afrunding.
- Afviser uklare valg mellem vægge, punkter ved hjørner og afklippede kortkanter.
- Afviser vægge, hvis trinnet udenfor går ind i en anden kendt bygning.
- Indsætter hver sikker, adskilt DAR-indgang som en normal `entrance=yes`
  node med egen adresse på omridset. Multipolygoner og gårdrum bevares.

Arnis' eksisterende mapped-entrance-kode bygger derefter selve døren og bruger
indgangen som anker for adresseskiltet. Indvendige dørpunkter hentes ikke;
de ekstra husnumre findes via deres præcise GeoDanmark-link. Det er stadig
ikke en komplet registrering af alle fysiske indgange. Afviste hints falder tilbage
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

Skiltplaceringen prøver først ved siden af døren som før og dernæst punkter
på den samme bygnings rasteriserede omrids, højst fire blokke til siden og
tre blokke i dybden. Den prøver også én blok lavere og op til to blokke højere,
hvis den oprindelige højde er glas eller mangler massiv væg. Alle skiltets
sider skal stadig have fast underlag og fri plads foran. Adresser kræver
fortsat `arnis:address`; manglende eller tvetydigt matchede adresser gættes ikke.

Fejlen ved Herrestræde 1C kunne reproduceres i regionfilen: døren stod i en
skrå rastervæg, hvor en side kun havde diagonal kontakt til døren. Efter
alle facade- og dekorationspas udfylder `finish_doorway` kun luftceller
umiddelbart ved siden af dørens to blokke. Rettelsen gælder alle planlagte
indgange: DAR, OSM, automatisk placerede indgange og dobbeltdøre. Den arbejder
kun på intakte dørpar og bevarer begge dørblade ved dobbeltdøre. Materialet tages fra den
tilstødende væg i samme højde, så fx stensoklen fortsætter ind til døren.
Bygningens vægmateriale bruges kun som fallback. Eksisterende facadeblokke
og overligger bevares; en overligger tilføjes kun, hvis den mangler.
Der tilføjes ikke længere hele tre blokke høje træstolper. Selve døren og
passagen bevares. Regressionstestene bruger den rapporterede bygnings skrå
omrids både med og uden DAR-tag, og kontrollerer enkelt- og dobbeltdøre i alle
fire retninger: lukkede sider, bevaret facade/overligger samt fri passage.

Skure og garager brugte tidligere `generate_special_doors`, som placerede
dørblokke uden `facing` og uden at registrere dem i `entrance_plans`. De bruger
nu `plan_special_door` og samme `render_entrance`/`finish_doorway` som andre
indgange. Deres eksisterende valg af dørposition bevares, men retningen beregnes
fra vægsegmentet og omridsets orientering; dobbeltdøre får modsatte hængsler.
Facadedekoration og adresseskiltets anker kender dermed også disse døre.


Verificeret på samme Slagelse-område med samme OSM- og danske inputfiler:

- 967 intakte dørpar i det nye kort; alle 966 tidligere intakte dørpar er bevaret.
- Døre med luft i et af de fire sidefelter: 598 før, 0 efter.
- 306 tidligere ikke-orienterede dørpar har nu eksplicit retning.
- 505 almindelige adresseskilte mod 345 før; alle kontrolleret for korrekt
  skiltblok, lysende hvid tekst og bevaret fuld adresse ved Herrestræde 1C.
- 299 element-/bygningstests bestod (2 ignoreret); release-build og GUI-check bestod.

Auditten kontrollerer dørens to sidefelter i begge højder og sammenholder
blokdata fra de to verdensfiler. Den er ikke en visuel Minecraft-kontrol og
beviser ikke, at hele bygningens facade er uden andre huller. To isolerede
nedre dørblokke og ét dørpar uden eksplicit retning fra andre genereringsveje
findes stadig i området; de isolerede blokke tælles ikke som intakte dørpar.
Det uorienterede par har ingen tomme sidefelter ved standardretningen.

Herrestræde 1C er kontrolleret igen: skiltet indeholder "Herrestræde 1C," og
"4200 Slagelse" på hver sin linje, og passagen er fri. Facade og overligger
bevares ved karmreparationen. Manglende adresseskilte kan fortsat skyldes
manglende/ikke-entydig registerkobling, manglende facadeanker eller manglende
plads til alle tekstens sider; der opfindes ingen adresser eller underlag.

### Billedskilte i survival

`WorldEditor::place_map_decal_ex` skriver nu `Fixed=0` på almindelige og
lysende item frames. Tidligere `Fixed=1` gjorde billedskiltene modstandsdygtige
over for survival-slag og lod dem overleve uden blokken bag billedet.
Nu kan et slag fjerne billedet, og fjernes den bærende blok direkte bag rammen,
forsvinder rammen ved spillets normale fysikkontrol. Et tomt frame følger
Minecrafts normale adfærd. `Invisible=1` og `ItemDropChance=0` bevares, så
grafikken vises uden synlig ramme og ikke efterlader et løst kort-item.
Almindelige adresseskilte er sign-blokke og var allerede nedbrydelige.

Rettelsen kræver intet serverplugin og gælder nygenererede Java-verdener.
Eksisterende rammer ændres ikke automatisk. Man kan rette en afgrænset del
af et gammelt testkort ved at stå ved skiltene og køre begge kommandoer med
operatørrettigheder (de ændrer alle rammer af den angivne type inden for 16 blokke):

```mcfunction
/execute as @e[type=minecraft:item_frame,distance=..16] run data merge entity @s {Fixed:0b}
/execute as @e[type=minecraft:glow_item_frame,distance=..16] run data merge entity @s {Fixed:0b}
```

Fysikken er afprøvet på en isoleret lokal Paper 26.2-testserver med samtlige
underlagsmaterialer fundet bag rammerne i Slagelse-kortet, inklusive halve
blokke, trapper og glas. Rammerne blev hængende med underlag; ikke-creative
skade fjernede billedet, mens en låst kontrolramme modstod skaden. Efter
fjernelse af underlaget forsvandt alle testens rammer. Det er en automatisk
spiltest, ikke en visuel test med en indlogget spiller. Testserveren er stoppet.

I det nye Slagelse-testkort er alle 930 billedskilterammer kontrolleret med
`Fixed=0`; placering, kort-id, rotation og grafik er bevaret. De 505 normale
adresseskiltes blokentiteter er uændrede. 70 world-editor-tests bestod, samt
release-build og GUI-check. Fysiktesten dækkede 45 underlagsmaterialer.

### Flere adresser og indgange på en samlet OSM-bygning

Et konkret eksempel er OSM-bygning 604610077: OSM har ét omrids, mens
GeoDanmark har tre bygninger med adresserne Oehlenschlægersgade 2,
Herrestræde 3 og Herrestræde 7, alle 4200 Slagelse. Den gamle 1:1-regel
afviste både bygningsoplysninger og adresser her.

`src/danish_addresses.rs` laver nu en separat adressekobling før den normale
bygningsmerge. Et kvalificeret TD/TK-punkt med fuld adresse kan tilknyttes, når
mindst 95 procent af dets GeoDanmark-bygnings areal ligger i præcis ét OSM-omrids.
`building:part` er udelukket. Dette overfører alene adressepunkter og deres
oprindelige vægsegmenter som det interne JSON-tag `arnis:address_hints`;
det ændrer ikke reglerne for materialer, etageantal eller omrids. De rå
Datafordeler-filer og supplementets schema er uændrede.

`src/danish_entrances.rs` vurderer alle punkter mod det oprindelige omrids,
inden nogen knuder indsættes. En valgt indgang skal også være højst to meter
(mindst to blokke) fra den oprindelige GeoDanmark-væg. De eksisterende tjek
for hjørner, afstand, nabobygninger og kortkant gælder stadig. Kandidater,
som havner mindre end tre blokke fra hinanden, afvises som tvetydige.
Eksisterende OSM-indgange bevares fortsat. Godkendte knuder indsættes bagfra
langs hvert segment, så ringens rækkefølge bevares, og hver knude får sin
egen `arnis:address`. TK er stadig et facadehint, ikke en opmålt dørplacering.

Bygningsgeneratoren sender derefter hvert sådant døranker til
`generate_entrance_address_sign` efter facade- og karmarbejdet. Det fælles
bygningsskilt undertrykkes, når der er individuelle adresseskilte på
indgangsknuderne. Bygninger uden individuelle adressepunkter bruger fortsat
det eksisterende bygningsskilt. Et skilt med lang tekst kan bruge flere linjer
eller to skilte; adressens tegn bevares.

Det nye Slagelse-kort blev verificeret med identiske datafiler og område:
913 almindelige adresseskilte mod 505 før. De tre ovennævnte adresser er
kontrolleret ved hver sin dør. 1.233 intakte dørpar havde ingen tomme sidefelter.
Alle 1.492 Rust-tests bestod (18 ignoreret), samt release-build og GUI-check.
Dette er kontrol af verdensfilen, ikke visuel kontrol i Minecraft.

Manglende eller tvetydige registerkoblinger, eksisterende OSM-indgange uden
en sikker individuel adressekobling og manglende fysisk skiltplads kan stadig
give døre uden adresse. En fælles adresse gentages ikke ukritisk ved alle døre.

### Flere DAR-husnumre på én GeoDanmark-bygning

En GeoDanmark-bygning kan have flere DAR-husnumre, selv om den tilknyttede
BBR-bygning kun peger på ét hovedhusnummer. Den tidligere hentning manglede
derfor nogle af bygningens indgange. Dette er rettet generelt, uden
adresse- eller koordinatspecifikke undtagelser.

`fetch.py` følger nu både BBR's hovedhusnummer og DAR's `geoDanmarkBygning`.
Samme snapshot-tid, statuskontrol, pagination og batchgrænse gælder begge veje.
`prepare.py` grupperer alle aktive DAR-husnumre efter det præcise bygnings-ID
og skriver de kvalificerede punkter i `arnis:entrances` (beskrevet ovenfor).
BBR-materialer og det eksisterende hovedadressefelt ændres ikke af ekstra
husnumre. Gamle snapshotfiler indeholder ikke de manglende adresser: hent et
nyt supplement med `fetch.py`, før kortet genereres igen.

`AddressHint::from_source` læser listen, fjerner dubletter og afviser tomme
adresser/ukendte kvalitetsklasser. Gamle enkeltfelter bruges kun, når listen
ikke findes. Projektionsbestemte `walls` læses ikke fra kildelisten; de dannes
af GeoDanmark-omridset ved adressekoblingen. Listen følger både nye danske
bygninger, entydige 1:1-match og adresser inde i større OSM-omrids. Eksisterende
OSM-indgange, hjørner, fællesvægge og kolliderende dørpositioner behandles med
de samme konservative regler som før.

Verificeret på Slagelse-området med samme OSM-fil og nyt dansk udtræk:
1.298 almindelige adresseskilte mod 913 før. Flere husnumre på samme
bygning har hver sin dør og fulde adresse. Alle 1.614 intakte dørpar havde lukkede
sidefelter. De 22 Python-tests og 14 danske Rust-tests bestod, samt release-build
og GUI-check. Det er kontrol af den gemte verden; TK-punkterne angiver fortsat
facader og garanterer ikke den præcise placering af en virkelig dør.

### Træer på byens belægninger

Træer må ikke bruge belægning som rodsted, blot fordi
satellitdata viser en trækrone over stedet. `ground_generation.rs` bruger nu
kun den eksisterende kontrol for naturligt underlag til de ekstra træer;
undtagelsen for glat sten, stenmursten og revnede stenmursten er fjernet.

`scattered_tree_ground` i `element_processing/tree.rs` kontrollerer både den
ønskede position og den endelige stammeposition efter træpakkens flytning til
et fast gitter. En forseglet vej-/pladskolonne afvises altid. Eksisterende
underlag skal være græs, jord, grov jord, podzol, mos, mudder, dyrket jord,
sand, sneblok eller mycelium. Dermed afvises også fliser, beton, grus og stier.
Sand og øvrige naturlige underlag bevares af hensyn til strand- og skovhabitater.
Før terrænet er udfyldt tillades en tom kolonne, undtagen når land cover angiver
bebyggelse; et allerede tegnet græsbed kan stadig bruges i et bebygget område.

OSM's eksplicitte `natural=tree` og `natural=tree_row` skal nu også overholde
samme krav til naturligt underlag. Undtagelsen for registrerede gadetræer på
belægning er fjernet efter brugerens ønske: ved La Viva stod tæt registrerede
træer ellers tilbage efter den første rettelse. Disse registreringer alene
giver ikke længere adgang til en vej-, fortovs- eller pladskolonne. Et separat
kortlagt græsbed/jordareal kan stadig have træer. Træer inde i bygninger,
under broer og på vand afvises som hidtil. Skov-/parkernes
tæthedsindstillinger, terrænhøjder og Mapterhorn-data er uændrede. Ændringen
gælder nygenererede kort og kræver ingen ny API-nøgle eller serverplugin.

Kontrol med identisk Slagelse-bbox, OSM-fil og dansk supplement: løvblokke
faldt fra 471.075 til 445.348 og log-blokke fra 75.865 til 72.067. Disse er
bloktal, ikke antal træer (bygninger kan også indeholde træstammer). Konkrete
stammer på stenmursten ved bl.a. X=501, Y=-50, Z=195 er fjernet i den nye
verdensfil. Alle 1.298 adresseskilte er identiske, og antal dørblokke er uændret.
53 trætests bestod (1 ignoreret), samt release-build og GUI-check. Testene
omfatter spredte/canopy-træer på forskellige belægninger, naturlige underlag
og (i denne første test) bevarelse af et eksplicit kortlagt gadetræ. Ingen visuel Minecraft-kontrol
er udført af hele området.

Efter fjernelse af gadetræernes belægningsundtagelse er rækken ved La Viva
kontrolleret i den nye verdensfil: 17 tidligere stammekolonner på stenmursten
er nu luft over uændret belægning. Løvblokke i hele området faldt yderligere
fra 445.348 til 175.013; det er ikke et antal træer. Alle 1.298 tidligere
adresseskilte er identiske, og fire ekstra skilte er kommet til, hvor der nu
er plads. De opdaterede tests afviser også kortlagte træer på belægning og
bekræfter, at kortlagte træer stadig kan stå på græs. 53 trætests bestod
(1 ignoreret), samt release-build og GUI-check.

### Lavere trætæthed på byens grønne arealer

`src/trees/density.rs` indsamler OSM-områder for by og skov én gang, før
genereringen begynder. `landuse=residential/commercial/retail/industrial`
angiver byområde. `landuse=forest` og `natural=wood` fritages altid fra
udtyndingen, også når de ligger inde i et byområde. Multipolygonernes
delstykker samles, og indre huller regnes ikke som skov.

`WorldEditor::urban_tree_density_allows` bruger desuden land-cover-klassen
for bebyggelse på stedet eller ved otte prøver omkring stedet, 24 meter væk
langs akserne (diagonalerne ligger længere væk). Det gør reglen anvendelig
på plæner ved byen, selv når OSM mangler et byområde. Uden disse tegn på by
ændres tæthedsvalget ikke; land-cover-trædække alene udløser ingen udtynding.
Manglende skovkortlægning kan derfor stadig give udtynding langs bebyggelse.

I byområdet beholdes 70 procent af de mulige stammepositioner med en fast,
koordinatbaseret hash. Det er en ekstra moderat udtynding af både spredte,
satellitbaserede og kortlagte træer. Det betyder ikke nødvendigvis præcis
30 procent færre træer eller løvblokke i et bestemt kort. Skovens eksisterende
arts-, størrelses- og tæthedsvalg ændres ikke. Kravet om naturligt rodsted og
forbuddet mod træer på belægning gælder fortsat alle træer.

Reglen kontrolleres én gang pr. faktisk stammeposition: efter træpakkens
flytning til gitteret, eller ved den oprindelige position for proceduretræer
og småskalatræer. Samme områdeoplysninger deles mellem hovededitor og
fliseeditorer og frigives før gemning. Hashen bruger en separat saltværdi
og ændrer ikke andre tilfældighedsvalg. Tests dækker by/skov-overlap,
skovhuller, bebyggelsesdata, ens resultater på tværs af fliser og uændret
accept af træpositioner i skov.

Slagelse-testen med identisk område og input gav 133.322 løvblokke mod
175.013 før (ca. 24 procent færre i hele kortet). Fjernede stammer på græs
er kontrolleret, bl.a. ved X=638, Y=-47, Z=827. De 1.302 tidligere
adresseskilte er identiske, og to ekstra har fået plads. Byudtrækket har
ingen eksplicitte OSM-skovområder; skovbevarelsen er derfor verificeret med
kontrollerede testområder, ikke med en stor skov i denne Slagelse-verden.
56 trætests og derefter alle fire målrettede tæthedstests bestod, samt
release-build og GUI-check. Kontrollen af verdenen er automatisk, ikke visuel.
