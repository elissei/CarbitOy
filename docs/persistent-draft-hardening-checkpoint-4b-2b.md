# CP4B-2B: katselmoinnin kovennus

Tämä vaihe korjaa [alkuperäisen palveluvaiheen](persistent-draft-service-checkpoint-4b-2b.md)
siivous-, avainkierto- ja kuittitoistorajat. Copilotin tehtävässä toimitettu
katselmointiyhteenveto oli PASS WITH RESERVATIONS; suoraa hyväksikäyttöä ei ollut
vahvistettu. Erillistä katselmointitiedostoa tai AGENTS.md:ää ei löytynyt
repositoriosta eikä tarkistetuista ylähakemistoista.

HTTP, selain, CP4B-3, muunnos ja labour submission eivät sisälly vaiheeseen.
Tuotantokantoja, paikallista var/carbit.db:tä, backup-rutiineja, käyttäjiä ja
molempia business-dokumentteja ei muuteta. Asiakasviestit, tarjoukset ja hyväksynnät
kuuluvat erilliseen tulevaan todistushistoriaan. Niitä ei poisteta luonnossiivoissa
eikä niiden sisältöä kopioida tekniseen auditointiin.

## Säilytyspäätös ja skeema 3

Hyväksytty kehityspolitiikka: sisältö vanhenee 604800 UTC-sekuntia viimeisestä
hyväksytystä sisältömuutoksesta. Tekninen metadata ja kuitit säilyvät aktiivisen
luonnoksen ajan ja enintään 90 UTC-päivää sen päättymisestä. Yrittäjän lopullinen
vahvistus on edelleen avoin; tämä ei anna uusia oversight-oikeuksia.

Migraatio 3 lisää:

- `draft_request_receipts.fingerprint_key_id`: uusille kuiteille pakollinen
  palvelimen avaintunniste. V2-kuittien NULL säilyy muuttamatta; alkuperäistä
  avainta ei voi päätellä eikä fingerprintiä kirjoiteta uudelleen.
- `draft_terminations`: muuttumaton päättymissyy ja UTC-aika, ei sisältöä.
  DISCARDED käyttää atomisen discard-kuitin recorded_at-aikaa. EXPIRED käyttää
  täsmälleen expires_at-aikaa, vaikka fyysinen poisto tehdään myöhemmin.
  CONVERTED säilyttää tulevan version olemassa olevan muunnoskuitin converted_at-
  sopimuksen. Tässä ei toteuteta muunnosta.
- `draft_termination_status`: myös vielä fyysisesti olemassa olevan vanhentuneen
  sisällön looginen päättymisaika näkyy heti palvelulle.
- `draft_retention_policy`: yksi keskitetty business-DB:n politiikkarivi,
  oletus 7776000 sekuntia. Luotettu ylläpitokoodi syöttää yhden yhteisen
  `DraftRetentionPolicy(metadata_days=...)`-asetuksen (1–90 päivää). Sen tulee
  olla sama kaikissa ylläpitoprosesseissa. Ylläpitotransaktio tallentaa politiikan;
  palvelun kuittihorisontti ja SQL-poistoguard käyttävät samaa DB-arvoa.

Indeksit tukevat päättymisjärjestystä ja avaintunnisteiden inventointia.
V1/V2-taulujen omistaja-, korjaamo-, payload-, revision-, timestamp- ja FK-rajat
säilyvät. UPDATE/REPLACE pysyy kiellettynä muuttumattomalle metadatalle. DELETE
sallitaan vain päättyneelle luonnokselle säilytysajan jälkeen ja sisällön jo
poistuttua. Politiikkariviä ei voi poistaa eikä arvoa asettaa yli 90 päivän.

Vanhojen jo poistettujen sisältöjen expiry johdetaan CREATED/CONTENT_SAVED-
auditin viimeisen tallennuslauseen server-ajasta +604800, ei CONTENT_PURGED-
tapahtuman myöhemmästä ajasta. Muunnosta ei päätellä pelkästä valmistelevasta
kuittirivistä ilman CONVERTED-tilaa/tapahtumaa. Jos historiallinen invariantti
on rikki eikä päättymisaikaa voida johtaa, NOT NULL pysäyttää migraation.
Migraation DDL, backfill ja ledger-rivi committaavat atomisesti; toisto on no-op.
Tuntematon tai epäjatkuva ledger estää alustuksen. Vanha koodi ei hyväksy versiota
3; palautus tarvitsee varmennetun preimagen ja myöhempien muutosten täsmäytyksen.

## Rajattu sisäinen ylläpito

`DraftMaintenance` on luotettu offline-komponentti, jota ei rekisteröidä reitiksi
eikä asiakkaan permissioniksi. Se ei käytä istuntoa tai auth-tietokantaa, eikä
omistajan jäsenyyden tai tilin käytöstä poisto estä vanhentuneen sisällön poistoa.
Sillä ei ole luku-, listaus- tai palautusrajapintaa. Tulos on ainoastaan määrä.
Yhteyden SQLite-authorizer estää payload_json-lukemisen, asiakastietojen lukemisen
sekä muutokset lopullisiin business-tauluihin ja jäsenyyksiin.

- `cleanup_expired(batch_size=1..100)` poistaa enintään annetun määrän fyysisiä
  sisältörivejä, joissa expires_at <= palvelimen nyt. Aktiivinen vanhentumaton
  luonnos säilyy riippumatta omistajan auth-tilasta. Triggerit tallentavat
  metadata-only CONTENT_PURGED-auditin ja alkuperäisen päättymisajan atomisesti.
- `cleanup_metadata(batch_size=2..100)` poistaa enintään annetun määrän
  teknisiä metadatarivejä yhteensä. Suuren luonnoksen kaikkia kuitteja ei poisteta
  yhdellä rajattomalla operaatiolla. Aja sisältösiivous ensin. Säilytysajan
  ylittänyt tekninen audit ja kuitit poistetaan ensin; registry ja termination
  poistuvat yhdessä vasta lapsirivien poistuttua, kahden rivin budjetilla.
  Alle kahden rivin erä hylätään, jotta lopullinen tombstone voi poistua.

Fyysisten sisältörivien ja metadatarivien budjetit ovat eri asiat: yhden sisältö-
rivin poisto voi tuottaa kaksi uutta metadatariviä. Metadatapoiston budjetti
laskee myös termination-cascaden. Säilytysajan ylittäneen auditin poistosta ei
luoda uutta ikuisesti säilyvää draft-auditia. Siivousajon kestoa/määrää voi
valvoa ilman luonnostunnisteita tai asiakaspayloadia.

Ylläpidossa on BEGIN IMMEDIATE, enintään viiden sekunnin busy-timeout,
progress-handler ja commitin aikaraja. Poistot ja politiikkapäivitys rollbackataan
kokonaan virheessä. Ylläpito ei ota auth-lukkoa eikä kutsu authia business-lukon
sisältä. Näin sillä ei ole auth/business-lukitusinversiota. Business-tallennukset
ja jäsenyyden muutokset serialisoituvat SQLite-business-writer-lukolla; auth-tilin
muutos voi committautua siivouksen aikana, koska siivous ei tee käyttäjävaltuutusta.
Käyttäjien palveluoperaatioiden
[auth → business -varaus](workshop-auth-business-hardening.md) säilyy ennallaan.

Ajastinta, HTTP-endpointia tai yleistä privileged readeria ei lisätty. Ylläpitäjän
on järjestettävä molempien rajattujen operaatioiden toistuva ajo ja valvonta.
Pelkkä callable ei takaa määräaikaista fyysistä poistoa, jos sitä ei ajeta.
SQL DELETE ei forensisesti ylikirjoita freelistejä, WALia tai backupeja.

## HMAC-konfiguraatio ja hallittu avainkierto

`DraftRepository` vaatii eksplisiittisen `DraftKeyring`-instanssin. Ei raw-key-
oletusavainta tai konfiguraation puuttuessa toimivaa fallbackia.
`DraftKeyring.from_environment(mapping)` hyväksyy luotetun palvelinkonfiguraation:

- `CARBIT_DRAFT_HMAC_ACTIVE_KEY_ID`: uusien kuittien avaintunniste.
- `CARBIT_DRAFT_HMAC_KEYS`: JSON-kartta avaintunnisteesta strict-base64-salaisuuteen.
- `CARBIT_DRAFT_HMAC_LEGACY_KEY_ID`: vain tarvittaessa eksplisiittinen v2-kuittien
  NULL-tunnisteen alkuperäisen avaimen mapping.

Tunniste on 1–64 ASCII-kirjainta, numeroa, `_` tai `-`, keyringissä enintään 32
avainta. Salaisuus on vähintään 32 tavua. Operaattorin tulee hankkia se CSPRNG:stä;
pelkistä tavuista ei voida todistaa entropiaa. Komponentti torjuu myös ilmeisen
yksipuoliset avaimet. Tässä ei generoida oikeita avaimia eikä muuteta ympäristöä.
Konfiguraatiovirheet eivät sisällä salaisuutta; avaimia ei tallenneta SQL:ään,
auditiin, lähdekoodiin tai repriin. HMAC-SHA256:n kanoninen request-domain ja
server-derived scope säilyvät version 2 kanssa yhteensopivina.

Kierron järjestys: jaa uusi ja kaikki tarvittavat vanhat avaimet kaikille
prosesseille, vaihda aktiivinen tunniste, tarkista kuittien inventaario, poista
vanha avain vasta kun yhtään sitä tarvitsevaa aktiivista/retained kuittia ei ole
ja sallittujen backupien palautus ei enää tarvitse sitä. Aktiivinen luonnos voi
pitää luontikuittinsa tarpeellisena yli 90 päivää: avaimia ei saa poistaa pelkän
avaimen iän perusteella. Jokainen valtuutettu palveluoperaatio tarkistaa
tarvittavien avaintunnisteiden saatavuuden samassa business-snapshotissa ja
epäonnistuu suljetusti, jos kierto tai palautus on hukannut avaimen. Ylläpito-
poisto ei tarvitse HMAC-avaimia. Replay käyttää alkuperäistä avaintunnistetta;
uusi kuitti käyttää aktiivista. Kuitteja ei re-HMACata kierron yhteydessä.

## Täsmällinen replay-sopimus CP4B-3:lle

CREATE/SAVE/DISCARD-kuitti löytyy vain nykyisen valtuutetun omistajan,
auth-store-namespacen, organisaation ja korjaamon piiristä. Ensin validoidaan
nykyinen istunto, MFA/expiry/auth_version, roolikatto, jäsenyysversio ja aktiivinen
korjaamokonteksti. IT_ADMIN tai service-identiteetti ei saa yksityisiä kuitteja
eikä sisältöä. Nonce, scope tai ownership ei tule asiakkaalta valtuutuksena.

Sama nonce ja alkuperäinen sormenjälki palauttavat alkuperäisen hyväksynnän
metadata-ackin myös expiry/discardin ja fyysisen sisältöpoiston jälkeen.
`DraftReceipt.state`, `revision`, `last_content_saved_at` ja `expires_at` kuvaavat
**alkuperäistä hyväksyntää**. Erilliset `termination_reason` / `terminated_at`
kuvaavat nykyistä päättymistä; ne ovat aktiiviselle luonnokselle NULL.
Ack ei ole editorin snapshot. Payloadia, vanhaa editorisisältöä tai lopullista
business-recordia ei palauteta. Uutta luonnosta, revisionia, audit-eventiä tai
määraikaa ei synny kuittitoistosta. Muuttunut validi payload, operation,
draft-type, parent work/version tai odotettu revision samalla noncella antaa
`DraftIdempotencyConflictError` ilman osittaista kirjoitusta.

Sisällön read/list ja uusi save/discard pysyvät kiellettyinä päättyneelle
luonnokselle. Tunnistamaton/ulkopuolinen/vanhentunut sisältö antaa saman
`DraftNotFoundError`-virheen; auth- tai jäsenyyskiellossa kuittihistoriaa ei tutkita.
Kuittireplayn horisontti päättyy täsmälleen terminated_at + configured retention,
vaikka fyysinen metadatapoisto olisi myöhässä. Tällöin olemassa olevan vanhan
kuitin retry antaa generic unavailable -virheen.

**Rajattu idempotenssihorisontti:** kaikkien teknisten nonce-rivien poistamisen
jälkeen palvelin ei voi erottaa muinaista uudelleen käytettyä UUID:ta uudesta
pyynnöstä. Elinikäinen duplicate-estäminen vaatisi säilyttää teknistä metadataa
hyväksytyn enimmäisajan yli. CP4B-3:n asiakkaan on uusittava nonce uudelle
operaatiolle ja rajoitettava automaattiset retries tähän horisonttiin; ääretöntä
kuittitakuuta ei luvata. V1:n fingerprintittömän creation-kuitin retry pysyy
conflictina; alkuperäistä requestia ei arvata nykyisestä sisällöstä.

## Backup- ja palautusvaatimukset

Backupin enimmäisikä on 90 päivää **sen luomisesta**. Backup-rutiineja ei tässä
muuteta. Tämä on eri kello kuin luonnoksen päättymisestä laskettu metadata-aika.
Backup voi sisältää jo aktiivikannasta poistettua dataa: online-poisto ei väitä
sisällön poistuneen vanhoista backupeista. Avainten escrow/secret-manager-säilytys
kattaa myös kaikkien vielä sallittujen backupien tarvitseman avaininventaarion.

Palautus tapahtuu offline/quiescent-ikkunassa varmennetusta, sallitun ikäisestä
backupista. Säilytä ennen palautusta nykyisen kannan preimage ja päättymis-/
perumishistoria täsmäytystä varten. Palauta oikea keyring ja legacy-mapping,
tarkista namespace/FK/integrity/schema-ledger, täsmäytä backupin jälkeen tehdyt
discardit ja auth-/jäsenyysperumiset, invalidoi tarpeen mukaan istunnot/MFA-
palautustiedot kovennusdokumentin mukaisesti, ja aja rajatut sisältö-/metadata-
poistot luotettavalla nykyisellä UTC-ajalla ennen palvelun avaamista.

Älä vaihda auth-store UUID:ta, nollaa aikaleimoja, poista migraatioversiota,
generoi korvaavaa legacy-avainta tai reaktivoi poistettua sisältöä palautuksena.
Jos tarvittava vanha avain tai palautuksen jälkeinen revocation/discard-
täsmäytys puuttuu, pidä palvelu suljettuna ja ratkaise palautus operaattoritasolla.
Erillisten auth/business-kantojen palautusta ei tehdä uudeksi yhdistetyksi
deploymentiksi; niillä ei edelleenkään ole hajautettua commitia.

## Tämän kovennuksen tiedostot ja varmennus

Muokatut aiemmin työpuussa olleet tiedostot:

- `webapp/draft_repository.py`
- `webapp/draft_models.py`
- `webapp/draft_migrations.py`
- `webapp/README.md`
- `tests/test_webapp_draft_service.py`
- `tests/test_webapp_draft_migrations.py`
- `tests/test_webapp_draft_migration_2.py`
- `docs/persistent-draft-service-checkpoint-4b-2b.md`

Uudet tiedostot:

- `webapp/draft_keys.py`
- `webapp/draft_hardening_migrations.py`
- `webapp/draft_maintenance.py`
- `tests/test_webapp_draft_hardening.py`
- `tests/test_webapp_draft_migration_3.py`
- `docs/persistent-draft-hardening-checkpoint-4b-2b.md`

Lähtötaso: koko regressio 1476 passed, 1 warning, 453,98 sekuntia.
Laaja kohdennettu palvelu-/skeema-/v1-v3-/kovennussarja: 363 passed,
1 warning, 106,90 sekuntia. Viimeisen eräkokorajan jälkeen kohdennetut
batch/deadline/historical-replay-testit: 28 passed, 78 deselected,
1 warning, 13,53 sekuntia. Varoitus on myös lähtötasossa esiintynyt
Starlette/httpx-deprecation; riippuvuuksia ei muuteta tämän tehtävän sivutyönä.

Deterministiset DELETE/WAL-testit kattavat session-, role-, disabled-,
auth_version- ja membership-perumiset päättyneen luonnoksen replayn aikana,
perutun omistajan siivouksen, rinnakkaiset auth-/membership-muutokset,
commit- ja aikaraja-rollbackin, kokonaisrivibudjetin, tarkan expiry/metadata-
rajan, aktiivisten kuittien säilymisen yli 90 päivän, kierron, muuttuneen
fingerprintin ja puuttuvan avaimen, eksplisiittisen v2 legacy-mappingin sekä
backup-replayn. Skeematestit
kattavat migraation toiston, v2-historian säilymisen, vanhan cleanupin oikean
expiry-alkupisteen, DDL-rollbackin sekä tulevan conversion-receipt-viitteen
säilymisen ilman lopullisten business-rivien poistoa. Python-syntaksi,
dokumenttilinkit ja `git diff --check` tarkistettiin.

Lopullinen koko regressio: `.venv\Scripts\python.exe -m pytest -q`,
**1586 passed**, 1 warning, 501,75 sekuntia (110 testiä enemmän kuin lähtötaso).
Toteutus on valmis riippumattomaan katselmointiin. Käyttöönotto vaatii erikseen
avainten jakelun/escrow'n, siivouksen ajastuksen ja valvonnan sekä hyväksyntöjen
varmistamisen. Yrittäjän lopullista 90 päivän vahvistusta ei oleteta annetuksi.

Lopullinen haara: `feature/web-app`, HEAD
`e93aeb31bf474fcbcbf633a69c7d33e32ba1f96d`. Työpuussa on 19 muokattua
seurattua ja 28 untracked-tiedostoa; staging on tyhjä. Muutokset sisältävät
säilytetyn aiemman työn, eikä koko Git-status kuvaa yksin tätä kovennusta.
Molemmat business-dokumentit, `var/carbit.db`, CP4B-1-backup ja kaikki
kovennuksen ulkopuoliset aiemmat muutokset varmennettiin SHA-256:lla ennallaan.
Migraatiot ja palautukset ajettiin vain väliaikaisiin testikantoihin.
Stage-, commit-, push-, merge-, deploy- tai oikeiden käyttäjien provisioning-
toimia ei tehty. Tuotantokantoja tai backup-rutiineja ei muutettu.
