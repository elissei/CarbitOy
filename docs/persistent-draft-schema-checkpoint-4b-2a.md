# CP4B-2A: pysyvien luonnosten skeema ja migraatio

Tämä vaihe lisää **vain tietokantapohjan**. Repository-CRUDia, HTTP-reittejä,
selain-autosavea, ajastettua siivoojaa tai käyttäjäprovisionointia ei toteuteta.
Nykyinen selain jatkaa muistissa olevien lomakkeiden käyttöä. Luonnoksen
tallennus ei luo työtä, työaikakirjausta, tarjousta, viestiä tai ostoa.

Lähtökohtina ovat [CP4B-1](workshop-foundation-checkpoint-4b-1.md),
[auth/business-kovennus](workshop-auth-business-hardening.md) ja
[yrittäjän avoimet kysymykset](business/yrittajan-kysymykset.md).
Kaksi business-dokumenttia säilytetään muuttamattomina. Tämän tehtävän
nimenomainen seitsemän päivän hyväksyntä täydentää niiden vielä OPEN-tilassa
olevaa säilytyskysymystä; muut kysymykset eivät muutu päätöksiksi.

## Hyväksytty säilytys ja rajaukset

Aktiivinen luonnos vanhenee seitsemän UTC-kalenteripäivän kuluttua viimeisestä
hyväksytystä sisältötallennuksesta. UTC:ssa tämä on 604800 sekuntia; paikallinen
kesä-/talviaika ei muuta määräaikaa. Timestamps tallennetaan UTC Unix -sekunteina,
joten tarkkuus on yksi sekunti. Samassa sekunnissa tehdyt sisältötallennukset
saavat eri revision mutta saman määräajan.

Lukeminen, avaaminen, muuttumaton retry, kuitin kirjoittaminen tai tilan muutos
eivät jatka säilytystä. JSONin avainten järjestys, whitespace ja saman merkin
Unicode-escape eivät ole sisältömuutoksia. Puuttuva kenttä ja eksplisiittinen
JSON-null ovat eri editoriarvoja. `expires_at` on generoitu sarake:
`last_content_saved_at + 604800`; sitä ei voi asettaa itse.

`active_drafts` sisältää vain ACTIVE-rivit, joilla
`expires_at > unixepoch('now')`. Tasatarkalla määräajalla rivi katoaa näkymästä
ilman siivousta. Vanhentunutta sisältöä ei voi päivittää tai elvyttää.
Määräaika arvioidaan jokaisen SQL-lauseen palvelinajan perusteella, ei selaimen
ajan tai siivousajon perusteella. Tämä on lukemisen tietokantaraja; jo tuotetun
verkkovastauksen toimittaminen ei ole reaaliaikainen katkaisumekanismi.

Palvelimen UTC-kellon on oltava luotettava. Seinäkellon siirtäminen taaksepäin
voi palauttaa vielä poistamattoman rivin aikavertailun sisään. Kelloa ei saa
peruuttaa palautuksessa vanhenemisten ohittamiseksi; jos ympäristö ei takaa
luotettavaa aikaa, käyttöönotto tarvitsee erillisen kellon/tombstone-suojauksen.
Tämä vaihe ei muuta authin kellomallia.

2–4 tunnin muistutukset ja 4–24 tunnin huomiomerkit jäävät tuleviin vaiheisiin.
Nimenomaisen hylkäyksen ja palautuksen politiikka on OPEN: DISCARDED-tilaa tai
palautuspolkua ei lisätä. CONVERTED on tulevan lopullisen tallennuksen
kuittivalmius, ei nyt käytössä oleva toiminto tai hyväksyntä lähettää tarjouksia.

## Taulut, viitteet ja muuttumattomuus

Migraatio `draft_schema_migrations` versio 1 lisää tyhjät taulut:

| Taulu | Sisältö ja raja |
| --- | --- |
| `draft_registry` | Muuttumaton UUID, organisaatio, korjaamo, auth-varaston UUID, omistajatyöntekijän UUID, luonnostyyppi, payload-skeemaversio 1, luontiaika ja LABOUR_ENTRYn alkuperäinen työviite/versio. Ei asiakassisältöä. |
| `draft_contents` | Poistettava JSON-editorisisältö, revision, ACTIVE/CONVERTED, viimeisen sisältötallennuksen aika ja generoitu määräaika. |
| `draft_creation_receipts` | Luontipyynnön UUID, luonnoksen tunniste ja muuttumaton omistaja-/korjaamo-/tyyppirajaus. Ei alkuperäistä payloadia, HTTP-vastausta tai tunnistusproofia. |
| `draft_conversion_receipts` | Muunnospyynnön UUID, luonnoksen hyväksytty revision ja lopullisen työn ID/versio samalla korjaamorajalla. Ei hintoja tai asiakassisältöä. |
| `draft_audit_events` | Vain tapahtuman ID, luonnoksen UUID, revision, tapahtumatyyppi ja UTC-aika. Luonnoksen omistaja ja scope johdetaan muuttumattomasta rekisteristä; merkintä ei väitä omistajan suorittaneen ylläpitopoistoa. |

SQLite generoi uuden v4-UUID:n `draft_registry.draft_id`-oletusarvona. CHECK
varmistaa kanonisen pienikirjaimisen UUID-muodon ja v4/variant-bitit.
Tulevan palvelinkoodin pitää jättää `draft_id` INSERTistä pois; SQL ei voi
todistaa validin eksplisiittisesti annetun UUID:n alkuperää. Selain ei saa
määrittää UUID:ta, omistajaa, scopea, tyyppiä, skeemaversiota, tilaa tai aikaa.

Rekisteririvejä, kuitteja ja auditointia ei voi UPDATElla tai DELETEllä muuttaa.
Myös REPLACE estyy nykyisen `recursive_triggers=ON`-käytännön ansiosta.
`draft_contents.draft_id` on muuttumaton. Sisältö aloittaa revisionista 1 ja
palvelimen nykyisestä ajasta; sisältömuutos vaatii täsmälleen revision + 1
ja nykyisen ajan. Vanhempaa tallennusaikaa ei hyväksytä. Muuttumaton sisältö
ei saa kasvattaa revisionia tai muuttaa säilytysajan alkua.

Tyypit ovat WORK_INQUIRY ja LABOUR_ENTRY. Ensimmäisellä ei ole työviitettä.
Jälkimmäinen vaatii olemassa olevan korjaamokohdistetun työn ja sen luontihetken
nykyisen positiivisen version. Tätä alkuperäistä versiota ei muuteta, vaikka
työ myöhemmin muuttuu. Varsinainen versionristiriidan käsittely kuuluu 2B:hen.

Korjaamo ja organisaatio muodostavat composite-FK:n `workshops`-tauluun.
Omistajaviite `(auth_store_id, owner_employee_id, workshop_id)` kohdistuu
olemassa olevaan business-DB:n `workshop_memberships`-riviin. Luonti vaatii
aktiivisen jäsenyyden, korjaamon ja organisaation. Jäsenyyden peruminen ei
poista sisältöä eikä siirrä omistusta; aktiivinäkymä piilottaa rivin heti.
Auth-varaston UUID erottaa riippumattomien auth-varastojen käyttäjät.
Auth-tauluihin ei luoda kuvitteellisia cross-database-FK:ita.

Työviite `(work_id, workshop_id)` käyttää `inquiry_workshop_assignments`-FK:ta.
Väärän korjaamon tai organisaation työ ei kelpaa labour-luonnokseen tai
muunnoskuittiin. FK:t eivät poista lopullisia töitä: niissä ei ole CASCADEa
työtietoihin. Metadata viittaa rekisteriin eikä poistettavaan sisältöriviin.

## Payload-skeema 1

JSON on objekti ja enintään 32768 UTF-8-tavua, mukaan lukien escapet ja
whitespace. Sallitut kentät ovat puuttuvia, null tai merkkijonoja; tyhjät ja
keskeneräiset syötteet sallitaan. Numeroita, listoja, sisäkkäisiä objekteja,
duplikaattiavaimia, NUL-merkkejä ja muita kenttiä ei hyväksytä.

| Tyyppi | Kenttä | Merkkiraja |
| --- | --- | --- |
| WORK_INQUIRY | `registration` | 20 |
| WORK_INQUIRY | `customer_description` | 4000 |
| LABOUR_ENTRY | `hours` | 64; editoriteksti, esimerkiksi `1,` |
| LABOUR_ENTRY | `source` | 32; keskeneräinen editorivalinta, ei lopullinen enum-validointi |
| LABOUR_ENTRY | `work_description` | 2000 |
| LABOUR_ENTRY | `autodata_reference` | 500; viiteteksti, ei integraatio |

Lopullisten työtietojen validointi ja hinnoittelu pysyvät nykyisessä
business-polussa. Luonnokseen ei sallita laskettuja hintoja, osto-ohjeita,
tilitietoja, sessio-/CSRF-/MFA-proofeja tai omistaja-/valtuutuskenttiä.
Vapaatekstin merkitystä SQL ei voi luokitella: myöhemmän editorin on myös
ohjeistettava, ettei asiakaskuvaus ole tunnistussalaisuuksien tallennuspaikka.

Auditoinnin triggerit kirjaavat CREATED, CONTENT_SAVED, CONVERTED ja
CONTENT_PURGED ilman payloadia. Avainten järjestyksen/escapingin muuttaminen
ei tuota sisältötallennustapahtumaa. Delegoitua tekijää tai valvontavaltuutta
ei mallinneta eikä anneta.

## Tulevan CRUDin sitovat rajat

SQLite-näkymä ja FK:t eivät ole käyttäjäkohtainen RLS-järjestelmä. Tulevan
repositoryn jokainen luku, tallennus ja retry tarvitsee `WorkRepository.operation`:
auth-kirjoitusvaraus → business-transaktio → jäsenyysversion tarkistus →
sisältö/kuitti → auth-ajan tarkistus → business commit/rollback → auth-varauksen
vapautus. Nykyistä toteutusta ei muuteta tai ohiteta tässä vaiheessa.

Lisäksi kaikki haut ja kuittireplayt rajataan palvelinkontekstin
organisaatioon, korjaamoon, auth-varastoon ja **omaan työntekijä-UUID:hen**.
ACTIVE-luku käyttää `active_drafts`-näkymää; raw `draft_contents` ei ole
käyttäjälukujen rajapinta. OWNER ei saa toisen työntekijän luonnoksia.
Optimistinen tallennus käyttää odotettua revisionia WHERE-ehdossa ja tarkistaa
rowcountin. Triggeri valvoo revisionin askelta, mutta ei korvaa tätä CAS-tarkistusta.

Luonti kirjoittaa rekisterin, sisällön ja luontikuitin samassa transaktiossa.
Muunnos kirjoittaa nykyisen business-operaation, muunnoskuitin ja
ACTIVE → CONVERTED -tilamuutoksen samassa transaktiossa. Tilamuutos vaatii
olemassa olevan saman revisionin kuitin, kasvattaa revisionia yhdellä eikä
siirrä sisältötallennuksen aikaa. Muunnoskuitti vaatii olemassa olevan saman
korjaamon työn nykyisellä versiolla; labour-muunnoksessa saman alkuperäisen työn
ja alkuperäistä suuremman version. Skeema ei yksin pakota vastakkaista
suuntaa (jokainen kuitti → tilamuutos), vaan tuleva transaktio hoitaa kokonaisuuden.

Kuitit tukevat pysyviä, omistaja-/scope-rajattuja retry-UUID:ita ilman
asiakassisällön monistamista. Ne eivät vielä määritä HTTP:n retry-protokollaa,
muuttuneen luontisisällön konfliktisopimusta tai lopullista vastaussnapshotia.
Ne tarvitsevat suunnittelun ennen 2B:tä. Säilynyt kuitti ei oikeuta lukemaan
vanhentunutta sisältöä tai luomaan poistettua UUID:ta uudelleen.

## Rajattu sisältöpoisto

`EXPIRED_CONTENT_CLEANUP_SQL` on testattu SQL-sopimus, ei käynnissä oleva työ
tai uusi repository-CRUD. Se poistaa vanhentuneet sisältörivit indeksin
järjestyksessä enintään **100 rivin** erissä. Parametrin on oltava kokonaisluku
1–100; null, negatiivinen, nolla, merkkijono, desimaali tai liian suuri arvo
poistaa nolla riviä. DELETE-triggeri estää ei-vanhentuneen sisällön poiston.
Jäsenyyden peruminen ei estä myöhempää ylläpitosiivousta.

Poisto jättää sisältövapaan UUID-rekisterin, kuitit ja auditoinnin paikalleen.
Niihin viittaava asiakaspayload poistuu `draft_contents`-taulusta; rekisteri
ei ole avattava luonnos eikä poistettu sisältö ole palautettavissa sovelluksesta.
Tombstone/audit estää saman luonnoksen sisällön uudelleenluonnin. Lopulliset
työt, työajat, tapahtumat ja business-retryt säilyvät.

Tuleva luotettu ylläpitosuorittaja tarvitsee erillisen käyttöönoton, rajatun
transaktion/SQL-aikabudjetin ja varmuuskopio-/palautussuunnitelman. Tässä ei
lisätä authista irrallista käyttäjäkirjoituspolkua tai taustaprosessia.
Indeksi/riviraja rajoittaa eräkokoa; se ei lupaa kovaa seinäkellokestoa hitaalla
levyllä. Jatkuvaa siivoussilmukkaa tai lukitusbudjetin ohitusta ei sallita.

SQL DELETE poistaa sisältörivin tietokannan elävistä tiedoista. Se ei takaa
forensista ylikirjoitusta SQLite-freelisteissä, WALissa tai varmuuskopioissa.
Näiden sekä metadatan säilytys-/hävityspolitiikka tarvitsee erillisen päätöksen.
Automaattista VACUUMia tai rajoittamatonta secure-erase-työtä ei käynnistetä.

## Indeksit, migraatio ja rollback

Omistajaindeksi alkaa korjaamosta, auth-varastosta, omistajasta ja tyypistä;
työviiteindeksi tukee labour-viitteen FK:ta. Sisältöindeksi
`(expires_at,draft_id)` palvelee vanhenemisrajausta ja rajattua poistoa.
Kuittien composite-PK:t erottavat eri omistajien/korjaamojen request-UUID:t;
draft_id-UNIQUE estää kaksi luonti-/muunnoskuittia samalle luonnokselle.
Muunnoksen työindeksi ja auditoinnin `(draft_id,event_id)` tukevat historiaa/FK:ita.

`WorkRepository._initialize` suorittaa migraation nykyisten labour- ja
workshop-migraatioiden jälkeen samalla BEGIN IMMEDIATE -transaktiolla.
Skeema ja versioloki committaavat yhdessä. Uusinta ei muuta mitään.
Tuntematon versio, puuttuva transaktio tai poissa olevat foreign_keys /
recursive_triggers estävät migraation. SQLite tarvitsee JSON-funktiot,
generoidut sarakkeet ja unixepoch-funktion (SQLite 3.38+ kyseisillä ominaisuuksilla;
testattu kehityksen 3.49.1:llä). DDL-virhe peruuttaa myös aiemmat alustuksen DDL:t.

Historiaan ei lisätä draft-viitteitä, oletusomistajia tai oletuskorjaamoja.
Vanhojen tiedustelujen ID:t/versiot, labour-versiot, tapahtumat, retryt ja
jäsenyydet eivät muutu. Myös kohdistamattomat vanhat työt jäävät kohdistamattomiksi.
Auth-migraatiota, auth-DB:tä tai oikeita jäsenyyksiä ei muuteta.

Tässä työssä migraatiot suoritetaan **vain testien tilapäistietokannoissa**.
`var/carbit.db`:tä ei avata kirjoittavalla sovelluksella. Seuraava tarkoituksellinen
sovelluksen alustus asentaisi sinne tyhjät luonnostaulut; ennen sitä pysäytä
kirjoittajat, ota varmennettu business-backup ja tarkista DB-polku sekä eheys.
Muiden ympäristöjen käyttöönotto tarvitsee oman hyväksynnän.

Skeema on lisäävä: nykyinen CP4B-1-koodi voi käyttää vanhoja business-tauluja
lisätaulujen rinnalla, mutta ei toteuta luonnoksia, siivousta tai uusia rajoitteita
niihin. Tämä ei anna lupaa palauttaa TOCTOU-kovennusta edeltävää koodia.
Down-migraatiota tai DROP-komentoja ei toimiteta. Jos palataan preimage-backupiin
myöhemmän käytön jälkeen, säilytä ja täsmäytä uusi data erikseen; älä menetä
uudempia työtietoja tai palauta vanhentuneita luonnoksia uudeksi sisällöksi.
Auth/business-parin palautus noudattaa kovennusdokumentin perumisten ja
istuntojen tarkistusmenettelyä. Vanha backup ei elvytä vanhaa säilytysaikaa.

## Avoimet päätökset ja varmennus

Skeeman lisääminen tyhjänä ei tarvitse uusia liiketoimintavaltuuksia.
Ennen oikean käytön avaamista tarvitaan:

- Sisältövapaan identiteetti-, kuitti- ja auditointimetadatan säilytysajan ja
  backup-/WAL-hävityksen hyväksyntä. Seitsemän päivää ei automaattisesti ole
  lopullisten työtietojen, auditoinnin tai retry-tombstonen säilytysaika.
- Hylättyjen luonnosten palautus/poisto (Y-007) ennen tällaisen toiminnon lisäämistä.
- Valvonta, delegointi sekä tarjouksen/työmääräyksen hyväksyntä pysyvät OPEN.
- Erillinen 2B-suunnitelma ja toteutuslupa sekä oikeat hyväksytyt käyttäjäjäsenyydet.

Testit kattavat tuoreen ja vanhan kannan, pre-2A CP4B-1:n, uusinnan, rollbackin,
historian säilymisen, UUID/omistaja/scope/type/version-immutabilityn, suljetun
JSON-skeeman, vieraat työviitteet, alkuperäisen työversion, revision-CAS:n,
muuttumattoman retryn, tarkan vanhenemisrajan, jäsenyyden/korjaamon/orgin eston,
kuittirajat, rajatun poiston ja osittaisen transaktion rollbackin.

Varmennus 2026-10-11:

- Lähtötaso: `.venv\Scripts\python.exe -m pytest -q`, **1215 passed**, 1 warning
  (405,10 s).
- Lopulliset luonnosskeematestit: sama komento polulla
  `tests/test_webapp_draft_migrations.py`, **130 passed**, 1 warning (6,29 s).
- Yhteinen luonnos-/korjaamo-/labour-migraatiosarja ennen viimeisiä lisätestejä:
  **223 passed**, 1 warning (11,95 s).
- Lopullinen koko regressiosarja: `.venv\Scripts\python.exe -m pytest -q`,
  **1345 passed**, 1 warning (420,06 s). Se sisältää myös aiemmat auth/business-
  concurrency- ja selainregressiot. Varoitus on sama olemassa ollut
  Starlette/httpx-deprecation kuin lähtötasossa.
- EXPLAIN QUERY PLAN vahvisti rajatun poistokyselyn käyttävän
  `idx_draft_contents_expiry`-indeksiä. Python-syntaksi, dokumenttilinkit ja
  `git diff --check` tarkistettiin.

Haara on `feature/web-app`, HEAD `e93aeb31bf474fcbcbf633a69c7d33e32ba1f96d`.
Staging-, commit-, push-, merge- tai deploy-toimia ei tehty. Aiemmat CP4B-1-
ja kovennusmuutokset säilytettiin. Molemmat business-dokumentit,
`var/carbit.db` ja sen CP4B-1-varmuuskopio säilyivät SHA-256-varmennettuina
muuttumattomina. Tuotantotietokantoja ei käytetty.
