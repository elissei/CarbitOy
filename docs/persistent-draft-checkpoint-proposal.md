# DRAFT: paikallisen checkpointin rajausehdotus

Päiväys: 15.10.2026. Tila: **EHDOTUS**, ei staging- tai commit-hyväksyntä.
Tavoite on Carbitin yhden korjaamon toimitus. Tämä asiakirja kuvaa nykyistä
checkpoint-ehdokasta ja erottaa dokumentoidun sopimuksen, lähdekoodin sekä
testinäytön. DRAFTin alkuperäinen hyväksyntälähde **puuttuu** tarkastetusta
julkisesta aineistosta. Tätä ehdotusta tai testien läpäisyä ei käytetä todisteena
aiemmasta hyväksynnästä tai tuotantovalmiudesta. SECin U1/U2 ja muut avoimet
varaukset säilyvät.

Vertailukohta on nykyinen SEC-HEAD, ei sitä edeltävä haara tai scratch-ehdokas:

- Haara: `feature/web-app`.
- HEAD: `e8173e6a59a2df4b5772a31b6e1e739a02e5a740`.
- HEAD-puu: `093a3e7c8e64c898c77c0c279210294a2339d8fe`.
- SEC on käyttäjän ilmoituksen mukaisesti paikallisesti COMMITTED_VERIFIED.
- Staging oli tyhjä valmistelun alussa. Tässä tehtävässä ei tehdä stagingia.

## Sopimuspohja ja ehdotettu toiminnallinen rajaus

Julkiset lähteet ovat [skeema 2A](persistent-draft-schema-checkpoint-4b-2a.md),
[palvelu 2B](persistent-draft-service-checkpoint-4b-2b.md),
[palvelun kovennus](persistent-draft-hardening-checkpoint-4b-2b.md) ja
[HTTP-sopimus 3A](persistent-draft-http-checkpoint-4b-3a.md).
Lähteiden hyväksyntämaininnat ovat dokumentoituja sopimusväitteitä; alkuperäistä
hyväksyntätodistetta ei löytynyt. Palveludokumentin alku ilmoittaa kovennuksen
korvaavan vanhat avain-, replay-, cleanup- ja säilytysalkupisterajaukset.
HTTP-sopimus lisää myöhemmän transportin. Vanhoja vaihekohtaisia poissulkuja
ei tulkita nykyisen toteutuksen puuttumiseksi.

| Dokumentoitu sopimus | Nykyinen toteutus | Todentava testilähde |
| --- | --- | --- |
| Omat WORK_INQUIRY- ja LABOUR_ENTRY-luonnokset; keskeneräinen editoriteksti, ei lopullista työtä tai työaikaa | `draft_models.py`, `DraftService`, `DraftRepository.create/read/list/save/discard`. LABOUR_ENTRY vaatii oman korjaamon olemassa olevan työn nykyisen version luotaessa; alkuperäinen työviite ja versio säilyvät. | `test_webapp_draft_service.py`, `test_webapp_draft_http.py`; skeematestit. |
| Sisältö vanhenee 604800 UTC-sekuntia viimeisestä hyväksytystä sisältömuutoksesta | Generoitu expires_at, active_drafts-näkymän tiukka `expires_at > nyt`. Luku, muuttumaton save, kuitti/retry ja tilamuutos eivät pidennä säilytystä. Puuttuva kenttä eroaa nullista. | Skeeman semanttinen sisältövertailu ja tarkka expiry-raja; palvelu-/HTTP-elinkaari- ja replay-testit. |
| Revisionhallinta ja atomisuus | Sisältömuutos kasvattaa revisionia yhdellä; save käyttää odotettua revisionia WHERE-ehdossa ja tarkistaa rowcountin. Sisältö, kuitit ja audit ovat samassa business-transaktiossa; virhe rollbackataan. | Palvelun ja HTTP:n save/save-kilpailut, commit-virheet; save/discard molempiin järjestyksiin. |
| Uusintapyyntöjen kuitit | Omistaja-/scope-rajattu request-UUID ja kanoniseen sisältöön sidottu HMAC. Sama pyyntö kuittaa alkuperäisen hyväksyntämetadatan; muutettu validi pyyntö samalla noncella on konflikti. Historiallinen kuitti ei palauta payloadia eikä elvytä sisältöä. | Service-, hardening- ja HTTP-testien lost-response-, restart-, expiry/discard-, horisontti- ja avainkiertotapaukset. |
| Oman aktiivisen revisionin hylkäys | DISCARD-kuitti ja välitön sisältöpoisto atomisesti. Ei palautusta eikä delegointia. Save ensin: stale discard 409; discard ensin: uusi save 404. Jo hyväksytyn saven nonce voi palauttaa vain alkuperäisen metadata-ackin ja nykyisen päättymistilan. | `test_competing_save_discard_serializes_without_resurrecting_content` sekä palvelun hylkäys-/replay-testit. |
| Tallennus ja nykyinen käyttöoikeus | SQLite-skeemat v1–v3. `WorkRepository.operation`: nykyinen auth/MFA/roolikatto/auth-versio, auth-varaus → business-transaktio/jäsenyysversio → commit/rollback → varauksen vapautus. SQL rajaa organisaation, korjaamon, auth-varaston ja oman employee UUID:n. | Migraatio-, palvelu- ja HTTP-testien eristys, perumiset, vanhentunut konteksti, rollback ja varausjärjestys. |
| Oletuksena suljettu HTTP-API | `DraftApiSettings` lukee puuttuvan aktivointiasetuksen arvoksi 0. Aktivointi vaatii eksplisiittisen keyringin. Todellinen lifespan muodostaa palvelun vain aktivoituna. Rekisteröidyt reitit jäävät suljettuina 503:een nykyisen autentikoinnin ja kirjoitusten CSRF-portin jälkeen. | `test_default_startup_closes_draft_http_after_real_lifespan`: kaikki viisi reittiä, aito MFA/jäsenyys ja toimiva aktivoitu vertailu. |

Vaatimusviitteet: skeema 2A rivit 15–39, 90–130; palvelu 2B rivit 34–88 ja
131–148; kovennus rivit 15–44 ja 125–157; HTTP-sopimus rivit 14–60,
86–108, 153–200. Toteutusviitteet:
[mallit](../webapp/draft_models.py), [repository](../webapp/draft_repository.py),
[palvelu](../webapp/draft_service.py), [asetukset](../webapp/draft_settings.py),
[transport](../webapp/http_drafts.py) ja [operaatioraja](../webapp/database.py).

OWNER ja SERVICE_MANAGER tarvitsevat eksplisiittisen nykyisen jäsenyyden ja
käsittelevät vain omia luonnoksiaan. OWNER ei saa toisen työntekijän sisältöä.
IT_ADMIN sekä palveluidentiteetit eivät saa yksityisiä luonnoksia tai kuitteja.
Nykyiset organisaatio-/korjaamorajat säilytetään tietovuotojen estämiseksi;
niiden kielteinen testaaminen ei laajenna toimitusta monen korjaamon tuotteeksi.

Payloadin suljetut kentät ja 32768 UTF-8-tavun tallennusraja säilyvät.
HTTP:n runkoraja on 65536 tavua. SAVE korvaa koko editoripayloadin; ei PATCHia
tai automaattista mergeä. Hintoja, scopea, omistajaa tai credential-kenttiä ei
hyväksytä lisäkenttinä. Virheet ovat yleisiä 404/409/422/503-vastauksia,
transportin muotovirheet erikseen HTTP-sopimuksen mukaisesti; ei payloadia
virhevastauksiin tai tekniseen auditiin.

HTTP-operaatiot: POST/GET `/api/drafts`, GET/PUT `/api/drafts/{draft_id}` ja
POST `/api/drafts/{draft_id}/discard`. Suljettu API ei lataa käyttämätöntä
HMAC-konfiguraatiota. Virheellinen nimenomainen aktivointi estää käynnistyksen
ennen auth/business-kantojen alustusta. API:n sulkeminen ei estä normaalin
business-alustuksen luonnosskeemojen asennusta; käyttöönotto on erillinen asia.

## Sisäinen ylläpito ja säilytysvaraukset

Ehdokas sisältää olemassa olevan `DraftMaintenance`-komponentin ja v3-retention-
suojat, jotta päättyneet sisällöt ja tekninen metadata voidaan poistaa myös
omistajan perumisen jälkeen. Sisältöpoiston erä on 1–100 sisältöriviä;
metadatapoiston erä 2–100 fyysistä metadatariviä. Rajattu transaktio ja authorizer
estävät komponentin käytön yleisenä sisältölukijana tai lopullisten business-
tietojen muokkaajana. Ei HTTP-ylläpitoreittiä tai uutta käyttäjäoikeutta.

[Kovennuksen](persistent-draft-hardening-checkpoint-4b-2b.md) kehityspolitiikka:
metadata ja kuitit aktiivisuuden ajan sekä enintään 90 UTC-päivää päättymisestä;
palvelinpolitiikan sallittu arvo on 1–90 päivää. Yrittäjän lopullinen vahvistus
on edelleen avoin. Tämä ei muuta seitsemän päivän sisältösäilytystä eikä anna
uusia valvontaoikeuksia. Kaiken nonce-metadatan poistuttua duplicate-estotakuu
ei ole elinikäinen.

Ajastettu ja valvottu fyysinen poisto, luotettava UTC-kello, HMAC-avainten
jakelu/kierto/escrow sekä auth/business-backupien täsmäytys ovat erillisiä
käyttöönoton edellytyksiä. SQL DELETE ei lupaa forensista poistoa WALista,
freelisteistä tai backupeista. Backupeille dokumentoitu enimmäisikä on 90 päivää
niiden luomisesta, eri aikaraja kuin metadatalla. Tuotantoa tai paikallisia
oikeita kantoja ei alustettu tai migroitu tässä valmistelussa.

## Rajauksen ulkopuolella

Selain ja autosave, muistutukset, luonnoksen muunnos, lopullinen työajan lähetys,
tarjouksen muodostaminen/hyväksyntä/lähettäminen sekä CRM-, Autodata-, toimittaja-
ja muut integraatiot jäävät ulkopuolelle. CONVERTED-skeemavalmius ja tekniset
kuitit eivät toteuta muunnospolkua tai asiakasviestien todistushistoriaa.
Ei uusia monen korjaamon ominaisuuksia, delegointia tai laajempaa sisältövalvontaa.
WD-3:a tai SEC-2B:tä ei aloiteta. Yleiset tarjouksen muuttumattoman version ja
ihmisen päätösten säännöt [CLAUDE.md:ssä](../CLAUDE.md) säilyvät.

## Täsmällinen checkpoint-ehdokkaan tiedostoluettelo

Nykytilasta tarkistettu ehdotus: **27 polkua, 6 M + 21 A suhteessa SEC-HEADiin**.
Kartoituksen 26 polkua säilyvät ehdokkaassa; lisäksi tämä uusi dokumentti.
M/A ovat ehdotettuja commit-eroja, eivät staging-tilaa. Tässä tehtävässä
ainoastaan tämän dokumentin luominen on sallittu repomuutos.

| Tila | Polku | Ehdotettu muutos |
| --- | --- | --- |
| M | `webapp/database.py` | Draft-migraatioimport ja kutsu nykyiseen alustustransaktioon. |
| M | `webapp/workshop_context.py` | READ_OWN_DRAFT/WRITE_OWN_DRAFT ja nykyisten henkilöroolien oikeuskartta. |
| M | `webapp/main.py` | Draft-asetukset, palvelun koostaminen lifespanissa ja reittirekisteröinti. |
| M | `webapp/http_access.py` | Vain nykyisen SEC-HEADin päälle tuleva DRAFT-delta, eritelty alla. |
| M | `tests/test_webapp_auth_service.py` | Kolme DRAFT-polkuperhettä hyväksyttyyn reittiluetteloon; ei muuta auth-testimuutosta. |
| M | `webapp/README.md` | Aiemmat 17 lisättyä DRAFT-kuvausriviä; säilytetään nykyisinä, ajantasaisuusvaraus jää avoimeksi. |
| A | `webapp/draft_migrations.py` | Skeemat v1/v2, editorirajat, CAS-/retention-triggerit ja v3:n kutsu. |
| A | `webapp/draft_hardening_migrations.py` | V3:n avaintunnisteet, päättymiset, metadata-retention ja poistoguardit. |
| A | `webapp/draft_models.py` | Luonnostyypit, editorivalidointi, snapshot ja metadata-kuitti. |
| A | `webapp/draft_keys.py` | Eksplisiittinen palvelimen HMAC-keyring ja kierto/legacy-mapping. |
| A | `webapp/draft_repository.py` | Omistajakohtainen CRUD, CAS, idempotenssi ja rajattu oma cleanup. |
| A | `webapp/draft_service.py` | Sisäinen WORK_INQUIRY-/LABOUR_ENTRY-palvelurajapinta. |
| A | `webapp/draft_maintenance.py` | Luotettu rajattu offline-sisältö-/metadatapoisto. |
| A | `webapp/draft_settings.py` | HTTP-API:n suljettu oletus ja nimenomainen aktivointi. |
| A | `webapp/http_drafts.py` | Viisi HTTP-operaatiota, pyyntörajat, yleiset virheet ja vastausmallit. |
| A | `tests/test_webapp_draft_migrations.py` | Luonnosskeema, rajat, historia ja atomisuus. |
| A | `tests/test_webapp_draft_migration_2.py` | V2-päivitys ja rollback. |
| A | `tests/test_webapp_draft_migration_3.py` | V3-päivitys, vanhan historian päättymisaika ja retention-suojat. |
| A | `tests/test_webapp_draft_service.py` | Omistajapalvelu, perumiset, retryt, kilpailut ja virheet. |
| A | `tests/test_webapp_draft_hardening.py` | Ylläpito, avainkierto, päättynyt replay ja säilytyshorisontti. |
| A | `tests/test_webapp_draft_http.py` | HTTP-sopimus sekä uudet aidon oletuskäynnistyksen ja save/discard-kilpailun testit. |
| A | `tests/test_webapp_draft_work_plan_integration.py` | Kaksi aiemmin WD-checkpointista erotettua yhteensopivuustestiä. |
| A | `docs/persistent-draft-schema-checkpoint-4b-2a.md` | Skeemavaiheen sopimus ja historiallinen testinäyttö. |
| A | `docs/persistent-draft-service-checkpoint-4b-2b.md` | Palvelusopimus ja myöhemmän kovennuksen etusijahuomautus. |
| A | `docs/persistent-draft-hardening-checkpoint-4b-2b.md` | Uusin avain-/replay-/ylläpito-/metadata-sopimus. |
| A | `docs/persistent-draft-http-checkpoint-4b-3a.md` | Nykyinen HTTP-sopimus ja rajaukset. |
| A | `docs/persistent-draft-checkpoint-proposal.md` | Tämä nykyinen checkpoint-ehdotus ja yhdistetyn ajon näyttö. |

`webapp/http_access.py`: vertailuna HEADin tiedosto, ei aiempi SEC-ehdokas.
Työpuudiffissä on seitsemän lisättyä ja kaksi poistettua riviä:

1. Viisi uutta metodi/polku-paria: GET/POST `/api/drafts`, GET/PUT
   `/api/drafts/{draft_id}`, POST `/api/drafts/{draft_id}/discard`.
2. `is_write` hyväksyy vanhan write-arvon lisäksi draft_write-arvon;
   `is_read` vastaavasti draft_read-arvon.

Kaikki seitsemän lisättyä riviä sisältävät DRAFT-kytkennän. Uutta SEC-deltaa
tai muuta sekoittunutta muutoskokonaisuutta ei löytynyt nykyistä HEADia vasten.
Tiedoston olemassa oleva SEC-suojaus kuuluu jo vertailu-HEADiin ja säilytetään.
Tiedoston tavuihin ei koskettu valmistelussa.

Muissa viidessä seuratussa tiedostossa nykyinen HEAD-delta on DRAFTia.
`tests/test_webapp_auth_service.py` sisältää kokonaisena myös vanhoja auth-testejä,
mutta ehdotettu delta on vain yksi reittiluettelorivi. README on DRAFT-deltaa,
vaikka sen vaihekuvausten nykytilatulkinta tarvitsee erillisen korjauksen.
README:tä ei muuteta tässä tehtävässä.

## Muut keskeneräiset muutokset ja riippuvuudet

Rajauksen ulkopuoliset 14 julkista työpuumuutosta:

```text
CLAUDE.md
docs/chat-history-sync.md
tests/test_sync_chat_history.py
tools/__init__.py
tools/sync_chat_history.py
tools/history_sync/__init__.py
tools/history_sync/cli.py
tools/history_sync/common.py
tools/history_sync/compare.py
tools/history_sync/export.py
tools/history_sync/normalize.py
tools/history_sync/render.py
tools/history_sync/selection.py
tools/history_sync/storage.py
```

Business HOLD ja yksityishistoria ovat myös rajauksen ulkopuolella eikä niitä
avattu. Koko Git-status ei ole checkpoint-manifesti.

Tuotannon riippuvuusketju on HttpAccessGate → http_drafts → nykyinen
workshop-konteksti → DraftService → DraftRepository → WorkRepository.operation
→ luonnosskeemat. Skeemaimport tuo riippuvuuden myös databasea tuoviin
vanhoihin web/labour-moduuleihin API:n ollessa suljettuna.

Nykyiset auth-, business_authorization-, workshop- ja WD-sopimus-/adapterimoduulit
sekä testien auth/workshop-apurit ovat HEADiin kuuluvia riippuvuuksia, eivät
uusia DRAFT-tiedostoja. Staattinen importtiketju ei tuonut riippuvuutta yllä
lueteltuihin 14 muun työn muutokseen. Dynaaminen latauspolku tarkistetaan
testiajossa. [WD-dokumentin](work-plan-contract-checkpoint-wd-2.md) rivit
263–298 jättävät kaksi yhteensopivuustestiä nimenomaisesti myöhempään
DRAFT-checkpointiin; ne eivät toteuta muunnosta tai WD-3:a.

## Yhdistetty DRAFT-testivarmennus

Testipolut tunnistettiin nykyisistä tiedostoista ja niiden DRAFT-riippuvuuksista.
Testimäärä saatiin pytestin collect-only-ajosta, ei historiasta tai funktiolaskurista.
Valinta sisältää kaikki seitsemän yllä lueteltua DRAFT-testimoduulia sekä vain
muuttuneen reittiluettelotestin `tests/test_webapp_auth_service.py::test_only_approved_auth_routes_and_no_recovery_or_business_context_bridge`.
Tuodut auth/workshop-testimoduulit palvelevat apureina; niiden muita testejä
ei tämän vuoksi ajeta.

Keräys: **576 tapausta**, exit 0, seinäkello **2,870 s**. Ensimmäinen ja ainoa
varsinainen yhdistetty ajo: **576 passed, 1 warning in 234,87 s**, exit 0.
Koko testialiprosessin mitattu seinäkelloaika oli **235,782 s**.
Ei epäonnistuneita, virheitä, skippejä, xfail- tai xpass-tapauksia.
Varoitus oli nykyisen Starlette TestClientin httpx-deprekaatio.
Uudet kuusi tapausta olivat tässä samassa ajossa aiempien 12 kanssa,
kukin aiemmista 18 tunnisteesta mukana kerran. Erillistä 18 tapauksen ajoa
tai koko projektin regressiota ei tehty.

| Testimoduuli / valittu testi | Kerätty ja läpäissyt |
| --- | ---: |
| `tests/test_webapp_draft_migrations.py` | 130 |
| `tests/test_webapp_draft_migration_2.py` | 3 |
| `tests/test_webapp_draft_migration_3.py` | 4 |
| `tests/test_webapp_draft_service.py` | 122 |
| `tests/test_webapp_draft_hardening.py` | 106 |
| `tests/test_webapp_draft_http.py` | 208 |
| `tests/test_webapp_draft_work_plan_integration.py` | 2 |
| `tests/test_webapp_auth_service.py::test_only_approved_auth_routes_and_no_recovery_or_business_context_bridge` | 1 |
| **Yhteensä** | **576** |

Komennon sovellus-/testivalinta oli seuraava; ajot suoritettiin scratchin
`snapshot`-hakemistosta alkuperäisen repon `.venv`-tulkilla:

```powershell
# $runRoot = tämän ajon alla yksilöity ulkoinen todistehakemisto
$copy = Join-Path $runRoot 'snapshot'
$draftTests = @(
    'tests/test_webapp_draft_migrations.py',
    'tests/test_webapp_draft_migration_2.py',
    'tests/test_webapp_draft_migration_3.py',
    'tests/test_webapp_draft_service.py',
    'tests/test_webapp_draft_hardening.py',
    'tests/test_webapp_draft_http.py',
    'tests/test_webapp_draft_work_plan_integration.py',
    'tests/test_webapp_auth_service.py::test_only_approved_auth_routes_and_no_recovery_or_business_context_bridge'
)
# $python = alkuperäisen repon .venv\Scripts\python.exe
# Aliprosessin hyväksytyt eristysasetukset: ks. command.json-todisteet.
& $python -B -u -m pytest -q -ra -p no:cacheprovider -p tests.wd2_test_runtime -p sec_execution_audit -c "$copy\pytest.ini" --rootdir $copy --confcutdir $copy @draftTests --collect-only
& $python -B -u -m pytest -q -ra -p no:cacheprovider -p tests.wd2_test_runtime -p sec_execution_audit -c "$copy\pytest.ini" --rootdir $copy --confcutdir $copy @draftTests --junitxml "$runRoot\focused.xml"
```

Eristys: 258 aiemman julkisen tiedoston tavuntarkka kopio ulkoisessa scratchissa,
olemassa oleva `.venv`-tulkki, muuttamaton `tests.wd2_test_runtime` ja aiemmin
hyväksytty ajonaikainen valvoja. Eristysrikkomuksia ei havaittu. Tuodut
projektimoduulit ladattiin tästä kopiosta varmennetuilla tiivisteillä;
14 muun keskeneräisen työn muutokseen ei tullut ajonaikaista import-riippuvuutta.
Oikeita kantoja, tunnuksia tai ulkoisia palveluita ei käytetty. Skeematestit
alustivat vain synteettisiä tilapäiskantoja; erillisiä migraatiokomentoja ei ajettu.
Lähdekoodia ja testejä ei muutettu. Uusi checkpoint-dokumentti ei kuulu testattuun
258 tiedoston lähdekoodikopioon; se on tämän valmistelun ainoa uusi repotiedosto.

Todistehakemisto:
`%TEMP%\carbit-draft-checkpoint-20261015-d482677616124521ab7256df4e759110`.
Tarkat komennot, ympäristöasetukset, kaikki testitunnisteet, JUnit ja ajonaikaiset
lataus-/SQLite-polut säilytetään siellä. Viiden parametrisoidun skeematapauksen
tunnisteissa on moduulin latauksessa generoitu UUID, joten keräys- ja ajoprosessin
tarkat tunnisteet eroavat näiltä osin. Molemmat listat on säilytetty; järjestys,
moduulit ja tunnisteet vastaavat, kun vain nämä UUID:t normalisoidaan vertailussa.
Testejä ei tämän vuoksi muutettu tai uusittu. Aiempi 18 passed / 16,01 s on
edellisen rajatun ajon historiallinen näyttö, ei tämän ajon lopputulos.

## Avoimet varaukset ja hyväksyntävalmius

Kaksi P2-kattavuusaukkoa on suljettu tunnistetun aukon osalta:
aidon oletuskäynnistyksen testi ja molemmat save/discard-järjestykset on lisätty
`test_webapp_draft_http.py`:hin. Niiden mukanaolo kerran ja läpäisy varmennettiin
myös tässä yhdistetyssä ajossa. Ajo ei osoittanut avointa sovellusvikaa.
Tämä ei todista kaikkien mahdollisten käyttäytymisyhdistelmien kattavuutta tai
koko DRAFTin virheettömyyttä.

P3: `webapp/README.md` rivit 150–165 sekoittavat aiempien vaiheiden poissulut
nykyiseen aktivoitavaan HTTP-toteutukseen. Tämä ehdotus täsmentää nykyisen tilan;
vanha README säilyy ja sen korjaus on erillinen tehtävä.

Alkuperäinen hyväksyntälähde puuttuu. Metadatan lopullinen retention-vahvistus,
avainten ja palautusten operointi, ajastettu fyysinen poisto sekä luotettava
UTC-kello jäävät käyttöönoton varauksiksi. Täysi projekti-/tuotantoregressio,
tuotantoympäristön varmennus ja kaikkien yhdistelmien kattavuus eivät seuraa
DRAFT-testien läpäisystä. SECin aiemmat varaukset säilyvät.

Kokonaisuus on **valmis nykyisen rajauksen hyväksymisarvioon varauksin** ja
erikseen valtuutettavan staging-valmistelun lähtökohdaksi. Tästä DRAFT-ajosta ei
jää teknistä testi- tai riippuvuusestettä. Hyväksyttävä kohde on nykyinen
27 tiedoston ehdotus; puuttuvaa alkuperäistä hyväksyntää ei päätellä jälkikäteen.
README:n P3-varaus ja käyttöönottovaraukset on hyväksymisarviossa huomioitava.
Rajauksen hyväksyntää tai staging-valtuutusta ei anneta tällä dokumentilla.
Staging-, commit-, push-, siivous- tai käyttöönottotoimia ei tehty.

Valmistelun säilymisraja: kaikki aiemmat 258 julkista tiedostoa sekä indeksin
raakabittisisältö säilytetään; ainoastaan tämä dokumentti lisätään. Lopullinen
inventaario ja tavutiivistevertailu tallennetaan ulkoisen scratchin
`preservation.json`-tiedostoon yhdessä todisteraportin kanssa.
