# CP4B-3A: yksityisten luonnosten HTTP API

> **Myöhempi yhteiskäyttömuutos (skeema 4):** tämän alkuperäisen checkpointin
> omistajakohtainen sisältöraja korvautuu [korjaamon yhteisillä luonnoksilla](shared-workshop-drafts.md).
> OWNER ja SERVICE_MANAGER voivat listata, avata, muokata ja hylätä saman korjaamon
> luonnoksia. Pyynnöt ja kuitit pysyvät henkilökohtaisina. Alkuperäistä checkpointia
> ei väitetä yhteiskäyttöiseksi; muut transport-, payload- ja kuittivastauksen
> sopimukset säilyvät alla kuvatusti.

Tämä checkpoint avaa [CP4B-2B:n kovennetun palvelun](persistent-draft-hardening-checkpoint-4b-2b.md)
HTTP-transportin. Se ei toteuta selainta/autosavea, muunnosta, labour submissionia,
CRM-, Autodata- tai OpenClaw-integraatioita. Tekniset luonnoskuitit eivät ole
asiakasviestien, tarjousten tai hyväksyntöjen todistushistoria.

AGENTS.md:ää tai muuta sovellettavaa instructions-tiedostoa ei löytynyt
repositoriosta eikä tarkistetuista ylähakemistoista. Haara tarkistettiin:
`feature/web-app`, HEAD `e93aeb31bf474fcbcbf633a69c7d33e32ba1f96d`.
Kaikki aikaisempi työ säilytetään; business-dokumentteja, var/carbit.db:tä,
CP4B-1-backupia tai tuotantokantoja ei avata kirjoittavalla sovelluksella.

## Aktivointi ja palvelinkonfiguraatio

API on oletuksena pois käytöstä. `CARBIT_DRAFT_API_ENABLED` hyväksyy vain
`0` tai `1`, oletus `0`. Pois käytöstä oleva API ei lataa HMAC-avaimia eikä
estä vanhoja work/auth/static-reittejä. Validoidulle, korjaamoon valtuutetulle
käyttäjälle luonnosreitit palauttavat tällöin 503 `Draft service unavailable`.
Tunnistaminen ja kirjoitusten CSRF tarkistetaan myös API:n ollessa pois käytöstä.

Nimenomainen aktivointi ilman kelvollista konfiguraatiota pysäyttää käynnistyksen
**ennen auth/business-tietokantojen alustusta**. Käytä erikseen turvallisesti
hankittuja palvelinavaimia, ei alla olevia placeholder-arvoja:

```text
CARBIT_DRAFT_API_ENABLED=1
CARBIT_DRAFT_HMAC_ACTIVE_KEY_ID=<active-key-id>
CARBIT_DRAFT_HMAC_KEYS={"<active-key-id>":"<base64-secret-at-least-32-bytes>","<previous-key-id>":"<previous-base64-secret>"}
CARBIT_DRAFT_HMAC_LEGACY_KEY_ID=<original-v2-key-id-if-needed>
```

Ympäristöä ei muuteta eikä oikeita käyttöönottotunnisteita generoida tässä työssä.
`create_app(..., draft_settings=DraftApiSettings(...))` on testien/luotetun
palvelinkoodin dependency-injektio, ei HTTP-parametri. Asetusrepr ei sisällä
keyringiä. Konfiguraation JSON-jäsentunnisteiden duplikaatit, liian suuri
konfiguraatio (yli 32768 merkkiä) ja rekursiivinen JSON torjutaan yleisellä
salaisuuksia sisältämättömällä konfiguraatiovirheellä.

Vanhoja avaimia säilytetään kaikkien aktiivisten ja retention-ikkunassa olevien
kuittien sekä sallittujen backupien palautustarpeiden ajan. Kierrä avaimet
[CP4B-2B-ohjeen](persistent-draft-hardening-checkpoint-4b-2b.md) mukaan. Jos
konfiguraatiosta puuttuu tarvittava vanha avain, luonnosoperaatio palauttaa 503;
vanhojen work-reittien toiminta jatkuu. Avaintunnisteiden merkityksiä ei saa
muuttaa eikä kuittien fingerprinttejä re-HMACata. Ei fallback-salaisuutta,
avaimia HTTP-vastauksissa tai niiden tulostamista lokiin.

## Reitit ja pyyntöskeemat

Jokainen reitti/metodi on `HttpAccessGate`-inventaarissa. Reitit pidetään auth-
transportin tavoin pois automaattisesta OpenAPI-listasta; docs/openapi-palvelua
ei avata. Tämä dokumentti on HTTP-sopimus.

| Metodi ja polku | Pyyntö | Onnistuminen |
| --- | --- | --- |
| POST `/api/drafts` | Alla oleva kind-kohtainen CREATE | 201 metadata-kuitti |
| GET `/api/drafts` | Vain valinnainen `limit=1..100`, oletus 50 | 200 aktiivisten snapshotien array |
| GET `/api/drafts/{draft_id}` | Ei queryä | 200 aktiivinen snapshot |
| PUT `/api/drafts/{draft_id}` | `request_id`, `expected_revision`, `payload` | 200 metadata-kuitti |
| POST `/api/drafts/{draft_id}/discard` | `request_id`, `expected_revision` | 200 metadata-kuitti |

WORK_INQUIRY CREATE:

```json
{"draft_type":"WORK_INQUIRY","request_id":"<request-uuid>","payload":{"registration":"ABC-","customer_description":null}}
```

LABOUR_ENTRY CREATE:

```json
{"draft_type":"LABOUR_ENTRY","request_id":"<request-uuid>","work_id":"<existing-work-id>","original_work_version":1,"payload":{"hours":"unfinished","work_description":null}}
```

SAVE:

```json
{"request_id":"<new-save-request-uuid>","expected_revision":1,"payload":{"registration":"ABC-123"}}
```

DISCARD:

```json
{"request_id":"<discard-request-uuid>","expected_revision":2}
```

Kaikki näytetyt kentät ovat pakollisia; ylimääräiset kentät kielletään.
Payload voi olla `{}` ja sen editorikentät voivat puuttua tai olla null/tyhjiä
merkkijonoja. Numeeriset/nested-arvot eivät ole editoritekstiä. SAVE korvaa koko
editoripayloadin CAS:lla, ei merge/PATCHia. Revision on oikea JSON integer
1..9223372036854775807; boolean, merkkijono ja float eivät kelpaa.
Request/draft UUID on 36-merkkinen hyphenated UUID; kirjainkoko normalisoidaan.
Compact/URN/braced-muotoja ei hyväksytä. Work-ID säilyy legacy-yhteensopivana
1–200 merkin stringinä; palvelu tarkistaa viitteen ja todellisen alkuperäisen
työversion nykyisessä korjaamossa. `draft_type` luokitellaan vain luotaessa;
SAVE/DISCARD ei voi muuttaa sitä.

Sisältökenttien sallittu lista, tyyppikohtaiset merkkirajat, JSONin kanonisointi
ja 32768 UTF-8-tavun tallennusraja pysyvät palvelussa. Route ei kopioi niitä.
Käyttöoikeus- tai hintakenttiä, tokenia, CSRF-proofia tai purchasing instructions-
rakenteita ei hyväksytä payloadin lisäkenttinä. Editoritekstiä ei lasketa hinnaksi.

POST/PUT tarvitsee täsmälleen `application/json` tai
`application/json; charset=utf-8`. UTF-8-rungon transport-raja on **65536 tavua**;
sekä ilmoitettu Content-Length että todelliset stream-chunkit tarkistetaan.
Content-Lengthin on vastattava luettua runkoa. Duplikaattiavaimet missä tahansa
JSON-objektissa, NaN/Infinity, virheellinen UTF-8 ja liian syvä JSON hylätään.
Muut queryt ja duplikaatti-limit kielletään. Lista on rajattu, luontiajan/ID:n
mukaan järjestetty; tässä ei toteuteta pagination-cursoria tai suodattimia.

## Vastaukset ja virheet

Metadata-kuitti sisältää vain:

```text
accepted=true
draft_id, draft_type, revision, state
last_content_saved_at, expires_at
termination_reason (null / DISCARDED / EXPIRED / CONVERTED), terminated_at (null / UTC epoch)
```

`revision`, `state`, `last_content_saved_at` ja `expires_at` kuvaavat alkuperäistä
hyväksyttyä operaatiota, eivät tämänhetkistä editoritilaa. `termination_*` kertoo
nykyisen päättymisen. Ei payloadia, omistaja-/scope-tunnisteita, HMACia, avainta,
audit-historiaa tai lopullista business-recordia. CREATE retry palauttaa myös 201:
se kuittaa alkuperäisen hyväksynnän eikä väitä luonnoksen syntyneen uudelleen.

Aktiivinen snapshot sisältää `draft_id`, `draft_type`, `revision`,
`state=ACTIVE`, `payload`, `payload_schema_version=1`, `work_id`,
`original_work_version`, `created_at`, `last_content_saved_at`, `expires_at`.
Kaikki ajat ovat UTC Unix -sekunteja. Listalla on sama snapshot-skeema.
Vastaukset ovat JSONia, `Cache-Control: no-store`; onnistumisissa myös nosniff.

Virheen runko on aina yleinen `{"detail":"..."}`. Pydanticin inputia sisältäviä
virheluetteloita, stack tracea tai DB-virheen tekstiä ei palauteta.

| Status | Merkitys / detail |
| --- | --- |
| 400 | Virheellinen/epäselvä framing, Host tai turvaheader, mm. `Invalid draft request framing` |
| 401 | `Authentication required` |
| 403 | `Permission denied` tai olemassa olevan CSRF/Origin-portin yleinen virhe |
| 404 | `Draft unavailable`: puuttuva/vieras/päättynyt sisältö tai virheellinen draft UUID |
| 409 | `Draft revision conflict` tai `Draft request conflict` (olemassa oleva cookie-state-konflikti säilyy) |
| 413 | `Draft request too large` |
| 415 | `JSON draft request required` |
| 422 | `Invalid draft request` |
| 503 | `Draft service unavailable` tai existing auth/workshop-palvelun yleinen saatavuusvirhe |

Tuntemattomat polut/metodit noudattavat nykyistä routerin 404/405-käytäntöä.
Esimerkiksi POST `/api/drafts/cleanup-expired` on väärä metodi draft-ID-mallille
ja voi antaa 405:n; se ei ole ylläpitoreitti. API ei tarjoa cleanup-/keyring-/
conversion-/submission-reittejä eikä palauta toisen omistajan olemassaolotietoa.

## Tunnistaminen, CSRF ja transaktiojärjestys

Kaikki viisi operaatiota vaativat nykyisen HUMAN/MFA-istunnon. OWNER ja
SERVICE_MANAGER tarvitsevat eksplisiittisen nykyisen jäsenyyden; IT_ADMIN ei
saa edes yksityisiä listoja/metadata-replaytä. OPENCLAW/SERVICE, peruttu/
vanhentunut istunto, disabled user ja muuttunut auth_version torjutaan.
Organisaatio, workshop, auth-store ja employee UUID johdetaan
`_business_context`/`resolve_workshop_context`-mekanismista. Omistaja/scope
ei kelpaa pyyntökentäksi tai queryksi; X-User/Role/Workshop/Organization-headerit
eivät muuta valtuutusta.

Kirjoitusluokitus käy läpi nykyiset HTTPS/Host/Origin-, cookie-state-,
duplikaattiheader-, session-bound CSRF-proof- ja nykyisen roolin tarkistukset
**ennen body-parsea**. Proof saadaan nykyisestä GET `/api/auth/csrf` -endpointista
headerilla `X-Carbit-Auth: bootstrap`; POST/PUT käyttää Originia ja
`X-CSRF-Token`-proofia. Ei uusia CSRF-tunnisteita, token-transportia tai CORS-lupaa.

Palveluoperaatio suoritetaan threadpoolissa nykyisen `WorkRepository.operation`-
rajan sisällä: auth-varaus → business snapshot/writer lock → nykyinen jäsenyys
→ commit/rollback → auth-varauksen vapautus. HTTP-kerros ei ota omaa SQL-yhteyttä
eikä tee irrallista pre-commit-recheckiä. Ensin committanut revoke estää
kirjoituksen; business-varauksen ensin saanut hyväksytty kirjoitus voi committaa
ennen odottavaa revokea. Myöhempi session idle refresh on nykyinen kirjanpitotoimi,
ei jo tehdyn business-commitin valtuutus. Kielletty tai rollbackattu kirjoitus
ei jätä osittaisia luonnos-, kuitti- tai audit-rivejä eikä refreshää idle-aikaa.

## Expiry ja rajattu idempotenssi

Palvelun seitsemän päivän sisältöretention säilyy. Read/list, muuttumaton save,
kuittiretry, discard tai fyysinen cleanup ei pidennä sitä. CAS:ssa yksi saman
revisionin kilpaileva sisältömuutos voittaa. Stale revision antaa 409:n eikä
hiljaista ylikirjoitusta. Sama nonce eri validille request-fingerprintille
antaa 409:n. Historiallinen CREATE/SAVE retry expiry/discardin ja fyysisen
sisältöpoiston jälkeen palauttaa vain turvallisen alkuperäisen metadata-ackin,
revalidoinnin jälkeen. Sisältöä tai luonnosta ei palauteta/aktivoida.

Uusi SAVE/DISCARD päättyneelle luonnokselle ja READ antavat 404:n. Asiakkaan
ei pidä automaattisesti luoda korvaavaa luonnosta 404:n jälkeen. Kuitit säilyvät
aktiivisuuden ajan ja enintään konfiguroidun ajan (oletus 90 päivää) päättymisestä;
yrittäjän lopullinen 90 päivän vahvistus on edelleen avoin.

Kaiken nonce-metadatan fyysisen poistamisen jälkeen palvelin ei pysty erottamaan
muinaista CREATE UUID:ta aidosti uudesta. Tämä hyväksytyn CP4B-2B:n rajattu
idempotenssi ei ole elinikäinen duplicate-estotakuu. Tulevan asiakkaan on
lopetettava automaattiset retries säilytyshorisontissa ja käytettävä uutta noncea
vain nimenomaiseen uuteen operaatioon. Backup/recovery-, avainkierto-, fyysisen
siivouksen ajastus- ja erillisten auth/business-kantojen palautusriskit säilyvät
[kovennusdokumentin](persistent-draft-hardening-checkpoint-4b-2b.md) mukaisina.

## Tämän vaiheen tiedostot

Muokatut: `webapp/main.py`, `webapp/http_access.py`, `webapp/draft_keys.py`,
`webapp/README.md`, `tests/test_webapp_auth_service.py` (hyväksytty reittiluettelo).
Uudet: `webapp/draft_settings.py`, `webapp/http_drafts.py`,
`tests/test_webapp_draft_http.py`, tämä dokumentti.
Skeemaa tai domain-service/repository-sääntöjä ei muuteta tässä vaiheessa.

## Varmennus ja lopputilanne

Lähtötaso: `.venv\Scripts\python.exe -m pytest -q`, **1586 passed**, 1 warning,
557,84 sekuntia. Kohdennettu HTTP/auth/access/CSRF-regressio: **447 passed**,
1 warning, 196,95 sekuntia. Viimeiset transport- ja eksplisiittisen
SERVICE_MANAGER-jäsenyyden lisätestit: **8 passed**, 1 warning, 6,77 sekuntia.
Lopullinen koko regressio (`-q --tb=short`): **1788 passed**, 1 warning,
641,48 sekuntia. Uusi HTTP-testimoduuli lisää 202 testiä.

Varoitus on myös lähtötasossa esiintynyt Starlette/httpx-deprecation;
riippuvuuksia ei muuteta tämän tehtävän sivutyönä. Windowsin ympäristömuuttujan
kokoraja huomioidaan konfiguraation ylikokotestissä syöttämällä testisanakirja
suoraan avainasetusten lukijalle. Sovellukselle ei lisätä fallbackia.

DELETE/WAL-testit käyttävät tuotannon app factorya sekä väliaikaisia
auth/business-kantoja, oikeaa MFA/session/CSRF-ketjua ja eksplisiittisiä
fiktiivisiä testijäsenyyksiä. Testit kattavat molemmat luonnostyypit,
owner/workshop/organization-eristyksen, IT_ADMIN- ja palveluidentiteetin
torjunnan, vanhentuneen/puuttuvan/MFA-keskeneräisen istunnon, session/role/
disabled/auth_version/membership-perumiset ja väärennetyn auth-store-bindingin.
Deterministiset HTTP-kilpailutestit todentavat auth-varauksen säilymisen
business-commitin yli sekä saman revisionin yhden voittajan. Muut testit
kattavat CSRF:n etusijan, header/framing/JSON/body-rajat, rollbackin ilman
osittaisia business-rivejä, muuttumattoman saven, lost-response/historialliset
retryt, tarkan expiry-rajan, fyysisen sisältöpoiston ja avainkierron.

Python-syntaksi, dokumenttilinkit ja `git diff --check` tarkistettiin.
Toteutus on valmis riippumattomaan katselmointiin; API on edelleen oletuksena
pois käytöstä. Käyttöönotto vaatii nykyisten auth/jäsenyyksien lisäksi erillisen
luotetun avainkonfiguraation ja aikaisempien ylläpito-/palautusohjeiden noudattamisen.
Uutta liiketoimintapäätöstä ei tarvita tähän HTTP-transportiin. Yrittäjän lopullista
retention-vahvistusta, delegointia tai laajempaa sisältövalvontaa ei oleteta annetuksi.

Lopullinen haara on `feature/web-app`, HEAD
`e93aeb31bf474fcbcbf633a69c7d33e32ba1f96d`. Git-status sisältää **20 muokattua
seurattua ja 32 untracked-tiedostoa**, staging on tyhjä. Lukuihin sisältyy
aikaisempi työ; vain edellä nimetyt yhdeksän tiedostoa muuttuvat tässä vaiheessa.
Molemmat business-dokumentit, `var/carbit.db`, CP4B-1-backup ja kaikki muut
tämän vaiheen ulkopuoliset aiemmat tiedostomuutokset varmennettiin SHA-256:lla
ennallaan. Stage-, commit-, push-, merge-, deploy- tai oikeiden käyttäjien
provisioning-toimia ei tehty. Tuotantokantoja ja backup-rutiineja ei muutettu.
