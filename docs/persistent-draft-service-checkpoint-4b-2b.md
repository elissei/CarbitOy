# CP4B-2B: yksityisten luonnosten repository ja palvelu

**Nykytila:** [riippumattoman katselmoinnin kovennus](persistent-draft-hardening-checkpoint-4b-2b.md)
korvaa alla olevan alkuperäisen checkpointin avain-, replay- ja siivousrajaukset
sekä avoimen säilytysalkupisteen. Migraatio 3, eksplisiittinen `DraftKeyring`,
päättyneiden luonnosten metadata-replay ja rajattu offline-ylläpito on toteutettu.
Kehityspäätös on enintään 90 päivää päättymisestä; yrittäjän vahvistus on edelleen
avoin. Alla säilyy aiemman vaiheen päätös- ja testihistoria.

Toteutus on sisäinen palvelukerros WORK_INQUIRY- ja LABOUR_ENTRY-luonnoksille.
HTTP-reittejä, selainta, autosavea, muunnosta työtiedusteluksi tai työajan
lähetystä ei lisätä. Palvelu ei lähetä asiakasviestejä tai tarjouksia eikä
kutsu CRM:ää, Autodataa, toimittajia tai OpenClaw'ta.

Pohjana ovat [CP4B-2A:n skeema](persistent-draft-schema-checkpoint-4b-2a.md),
[korjaamojäsenyydet](workshop-foundation-checkpoint-4b-1.md) ja
[auth/business-järjestystakuu](workshop-auth-business-hardening.md).
Erillistä CP4B-2A:n ulkoista katselmointiraporttia ei löytynyt repositorysta.
TOCTOU-katselmoinnin havainto ja korjaus on kuvattu kovennusdokumentissa.

## Rajapinta ja server-konteksti

`DraftRepository` ottaa olemassa olevan `WorkRepository`-instanssin ja
palvelimen pysyvän fingerprint-avaimen. `DraftService` tarjoaa:

- `create_work_inquiry`: tyyppi määräytyy palvelumetodista.
- `create_labour_entry`: tyyppi määräytyy palvelumetodista; työviite ja
  alkuperäinen versio tarkistetaan korjaamorajauksen jälkeen.
- `read` / `list`: vain omat tällä hetkellä käytettävissä olevat aktiiviluonnokset.
- `save`: odotettu revision, request-UUID ja keskeneräinen editoripayload.
- `discard`: odotettu revision ja request-UUID; välitön sisältöpoisto.
- `cleanup_expired`: sisäinen, enintään 100 oman vanhentuneen sisältörivin erä.

Caller käyttää `resolve_workshop_context`-adapteria palvelimen AuthServicellä
ja aidolla MFA-istunnolla. Pelkkä käyttäjäolio, JSON-konteksti, tokeniton
offline-konteksti tai globaali rooli ei riitä luonnospalveluun.
Organisaatio, korjaamo, auth-varasto ja employee UUID otetaan tästä kontekstista.
Omistajaa tai scopea ei voi antaa palvelumetodien ylimääräisinä parametreina.
LABOUR_ENTRYn clientin odotettu työversio ei ole auktoriteetti: palvelin
tarkistaa nykyisen version ja tallentaa tietokannasta luetun arvon.

Uudet eksplisiittiset oikeudet ovat READ_OWN_DRAFT ja WRITE_OWN_DRAFT.
OWNER ja SERVICE_MANAGER voivat käsitellä **omia** luonnoksiaan voimassa
olevalla eksplisiittisellä jäsenyydellä. IT_ADMIN ei saa luonnossisältöä eikä
OpenClaw/SERVICE saa henkilösessiota tai luonnospääsyä. OWNER ei saa toisen
työntekijän sisältöä. Muiden business-operaatioiden nykyiset oikeudet säilyvät.

Jokainen luku, lista, kirjoitus, retry ja cleanup käyttää
`WorkRepository.operation`: nykyinen sessio/MFA/roolikatto/auth-versio tarkistetaan
auth-kirjoitusvarauksen alla, tämän jälkeen business-transaktio tarkistaa
jäsenyysversion ja aktiivisen toimipisteen. Auth-varaus pysyy business-
commitin/rollbackin yli. Ei toista auth-kirjoitusta, uutta lukitusjärjestystä
tai auth-virheestä offline-fallbackia.

SQL rajaa aina organisaation, korjaamon, auth-varaston ja omistajan.
Puuttuva, vieras, hylätty tai vanhentunut sisältö palauttaa saman
`DraftNotFoundError('draft unavailable')`-virheen. Vieras työviite labour-
luonnoksen luonnissa antaa saman virheen kuin puuttuva työ; versiokonflikti
tarkistetaan vasta oman korjaamon työn löytymisen jälkeen.

## Sisältö, revision ja vanheneminen

CP4B-2A:n suljetut kenttälistat, merkkirajat, 32768 UTF-8-tavun yläraja,
skeemaversio 1 ja incomplete-input-säännöt säilyvät. Palvelukerros hyväksyy
vain tavallisen dictin ja sallittuihin kenttiin merkkijonon/nullin.
Tuntemattomat kentät, hinnat, omistajuus-/scope-kentät, credential-kentät,
listat/objektit, NUL ja kelvoton Unicode hylätään. JSON kanonisoidaan ennen
fingerprintin muodostusta ja SQL-kirjoitusta. Payload jätetään snapshotin
repr-esityksestä pois.

Luonnoksen UUID generoidaan SQLite-oletusarvosta, ei clientistä. Luonti
kirjoittaa rekisterin, sisällön, vanhan luontikuitin ja uuden request-kuitin
samassa transaktiossa. Alkuperäinen labour-työ/versio, omistaja ja korjaamo
pysyvät muuttumattomina. Myöhempi työn muutos ei uudelleenkohdista luonnosta;
lopullisen labour-lähetyksen versionkonflikti jää myöhempään vaiheeseen.

Sisältömuutos tekee `UPDATE ... WHERE revision=?`-CAS:n, kasvattaa revisionia
yhdellä ja asettaa sisältötallennuksen ajaksi `unixepoch('now')`.
Kilpailevista eri sisältöä kirjoittavista saman revisionin tallennuksista yksi
onnistuu. Häviäjä saa `DraftRevisionConflictError`-virheen ilman osittaista
sisältöä, kuittia tai auditointia. Muuttumaton sisältö ei muuta revisionia tai
määräaikaa; kaksi muuttumatonta hyväksyntää voi käyttää samaa revisionia.

Seitsemän UTC-päivän sääntö, generoitu expires_at ja täsmällinen `>`-raja
pysyvät. Avainten järjestys tai escaping ei ole sisältömuutos. Luku, lista,
muuttumaton tallennus ja kuittireplay eivät jatka säilytystä. Palvelimen
UTC-kellon luotettavuusvaatimus säilyy; tämä vaihe ei toteuta clock-rollback-
suojausta. Vanhentunut sisältö on poissa käyttörajapinnasta ennen siivousta.

## Lost-response-retry ja metadata

Migraatio 2 lisää `draft_request_receipts`-taulun CREATE/SAVE/DISCARD-operaatioille.
Request-UUID on yksikäsitteinen auth-varaston, työntekijän ja korjaamon sisällä
myös eri operaatiotyyppien välillä. Kuitilla on composite-FK muuttumattomaan
luonnoksen omistajaan, korjaamoon ja tyyppiin. Kuitit ovat immutable myös REPLACEssä.

Request fingerprint on domain-rajattu HMAC-SHA256 kanonisesta requestista ja
palvelimen scopesta. Se sitoo operaation, editoriarvot, työviitteen/alkuperäisen
version tai draft UUID:n/odotetun revisionin. Metadatassa ei ole alkuperäistä
asiakaspayloadia tai salaamattomasta vähäentropisesta sisällöstä laskettua
tavallista SHA-hashia. Auditointi ei sisällä myöskään tätä HMACia; se käyttää
edelleen vain CP4B-2A:n metadata-kenttiä.

Sama nonce ja sama fingerprint palauttaa alkuperäisen `DraftReceipt`-
hyväksyntämetadatan, vaikka sisältöä on myöhemmin muokattu. Sama nonce eri
requestilla antaa `DraftIdempotencyConflictError`-virheen. Kuitin vastauksessa
on ID, tyyppi, hyväksytty revision, tila, viimeinen hyväksytty sisältöaika ja
sen määräaika. Se **ei ole vanhan customer-payloadin snapshot**: nykyinen sisältö
luetaan erikseen, jolloin myös nykyinen revision näkyy.

CREATE/SAVE-replay vaatii nykyisin aktiivisen, vanhenemattoman oman luonnoksen.
Peruminen, jäsenyysversion muutos, roolin alentaminen, disabled tai auth_version-
muutos tarkistetaan ennen kuittia. DISCARDin sama omistajan nonce voi kuitata
jo onnistuneen hylkäyksen ilman sisältöä; se ei mahdollista tombstonen yleistä
luettelointia, lukua, palautusta tai muuttamista.

Fingerprint-avain on erillinen palvelimen konfiguroima vähintään 32 tavun
salaisuus. Sille ei ole demo-/ephemeral-oletusta. Avainta ei tallenneta DB:hen,
auditointiin tai asiakasvastaukseen; tässä vaiheessa ei provisionoida oikeaa
avainta eikä muuteta ympäristömuuttujia. Sama avain tarvitaan restartissa ja
palautuksessa kuittien eliniän ajan. Avaimen vaihtaminen ilman suunnitelmaa
johtaa vanhojen noncejen konfliktivirheisiin, ei auktorisoinnin ohitukseen.
Avainrotation/keyring-suunnittelu kuuluu käyttöönoton valmisteluun.

CP4B-2A:n vanhoissa luontikuiteissa ei ole alkuperäistä fingerprintiä.
Migraatio säilyttää ne ennallaan. Niitä ei täydennetä nykyisestä, mahdollisesti
muutetusta payloadista. Tällaisen aktiivisen luonnoksen vanhan nonce-replay
antaa konfliktin; luonnosta voi lukea/muokata uusilla nonceilla. Vanhentunut
legacy-replay on geneerisesti unavailable. Käyttäjää tai membershipiä ei luoda.

## Hylkäys, auditointi ja rajattu cleanup

Tämän tehtävän nimenomainen hylkäyssopimus täydentää aiemman vaiheen OPEN-
poistokysymystä: nykyinen omistaja saa hylätä oman aktiivisen revisioninsa ja
poistaa sisällön heti. Palautustoimintoa tai yrittäjän delegointia ei toteuteta.

DISCARD tallentaa immutable-kuitin accepted_revision = nykyinen revision + 1
ja poistaa sisältörivin samassa transaktiossa. Säilytysajan alkua ei muuteta.
V2-näkymä piilottaa jo kuitatun hylkäyksen, ja lisätriggeri estää sen päivittämisen.
Sisällön DELETE sallitaan ennen expiryä **vain** matching DISCARD-kuitilla,
jossa on sama draft ID, aikaleima ja revision + 1. Muut aktiivisen sisällön
poistot pysyvät estettyinä. Rekisterin ja receiptien immutable-rakenteita ei avata.

V1:n CONTENT_PURGED-audit kirjautuu myös hylkäyspoistossa. Immutable DISCARD-
kuitti erottaa nimenomaisen hylkäyksen retention-poistosta ilman audit-taulun
uudelleenkirjoitusta tai customer-payloadia. Luonnoksen omistaja on auditin
kohde, ei väite ylläpitotoimen suorittajasta. Kaikki audit-/kuittirivit ja
sisältörollbackit ovat samassa business-transaktiossa.

`cleanup_expired(context, batch_size=1..100)` käyttää samaa nykyistä
WRITE_OWN_DRAFT-valtuutusta ja poistaa vain kontekstin oman työntekijän saman
korjaamon vanhentuneita sisältörivejä. Se ei lue eikä raportoi muiden omistajien
sisältöä tai määriä. Lukko-/SQL-budjetti tulee WorkRepository.operationista;
rajatonta silmukkaa, HTTP-endpointia tai ajastinta ei ole.

Perutun omistajan tai koko ympäristön siivousta ei ratkaista human-authorization-
bypassilla, owner oversightilla tai OpenClaw-tilillä. Autonominen ylläpito
tarvitsee erillisen, tarkastetun maintenance-executor-mallin ennen käyttöönottoa.
Kaikki vanhentuneet sisällöt pysyvät silti heti käyttäjille inaccessible.

SQL DELETE ei takaa forensista ylikirjoitusta SQLite-freelisteissä, WALissa tai
backupissa. Ei automaattista VACUUMia, secure-delete-konfiguraation muutosta
tai muutosta backup-rutiineihin. Lopullisiin work-/labour-/event-/retry-
tauluihin ei kohdistu luonnospoistoja.

## Säilytyspäätös: 90 päivää, alkupiste vahvistamatta

Tehtävä määrää dokumentoimaan metadata-/receipt-retentioniksi **90 päivää**
sekä backup-enimmäisajan. Tarkkaa alkupistettä ei annettu, ja siitä pyydettiin
tarkennus ennen siihen sidottavaa poistomenettelyä:

- Ehdotus: 90 UTC-päivää luonnoksen päättymisestä (hylkäys, expiry tai myöhemmin
  muunnos). Näin aktiivisen pitkään muokatun luonnoksen luontikuitti ei katoa.
- Backup-ehdotus: enintään 90 päivää backupin ottamisesta.
- Vaihtoehto: 90 päivää kunkin metadata-/kuittirivin luomisesta, mikä tarvitsee
  erikseen ratkaistun retry-sopimuksen pitkään aktiivisille luonnoksille.

Alkupistettä ei oleteta hyväksytyksi. **Metadatan ajastettua 90 päivän poistoa
ei toteuteta ennen tätä päätöstä.** Nykyiset immutable-tombstonet ja kuitit
säilyvät. Päätöksen jälkeen tarvitaan rajoitettu, FK-/immutability-rajoja
säilyttävä metadatan poistosuunnitelma ja rajattu retry-ikkuna; niitä ei saa
korvata triggerien poiskytkennällä. Backup-rutiinit säilyvät tässä vaiheessa.

Asiakasviestit, tarjoukset ja hyväksynnät kuuluvat erilliseen tulevaan
evidentiary historyyn, eivät luonnosretentioniin. Niille ei tässä oleteta
seitsemän tai 90 päivän säilytystä eikä rakenneta arkistoa.

## Migraatio, palautus ja katselmointi

Draft-ledger hyväksyy prefixit tyhjä / {1} / {1,2}. Migraatio 2 lisää uuden
kuittitaulun/indeksit sekä hylkäyksen tarkennetun DELETE-guardin, update-guardin
ja näkymän. V1:n payload-, UUID-, owner-, scope-, revision-, aikaleima- ja
FK-rajoitteet säilyvät. Vanhoja rivejä, versioita tai kuittifingerprinttejä ei
arvata tai kirjoiteta uudelleen. DDL ja version 2 ledger-rivi committaavat
samassa alustustransaktiossa; testattu myös kesken version 2 epäonnistuva rollback.

V2:n jälkeen vanha CP4B-2A-koodi ei hyväksy draft-ledgerin versiota 2.
Rollback tarvitsee quiescent-ikkunan, varmennetun preimage-backupin sekä
myöhemmän datan ja turvallisuuspäätösten täsmäytyksen. Älä poista ledger-riviä
tai vaihda auth-store UUID:ta korjauksena. Auth/business-palautus noudattaa
kovennusdokumentin perumis-, membership-, MFA- ja session-invalidation-ohjeita.
Palautus ei saa elvyttää hylättyä sisältöä tai aloittaa sen retentionia uudestaan.

Migraatiot ajetaan tässä työssä vain tilapäisiin testikantoihin.
Business-dokumentit, `var/carbit.db`, CP4B-1-backup ja muu aikaisempi työ säilytetään.
Tästä ei seuraa tuotantokäyttöönotto tai CP4B-3:n toteutuslupa.

Riippumattomassa katselmoinnissa tarkistettavat kohdat: live-auth-varaus jokaisessa
operaatiossa/replayssä, owner-scope jokaisessa SELECTissä, HMAC-konfiguraatio,
CAS + idempotency ennen stale-revision-virhettä, exact expiry, discard-kuitin
atomisuus, audit ilman payloadia sekä ylläpidon ja 90 päivän politiikan rajat.
Checkpointin viimeistely odottaa yllä olevan säilytysalkupisteen päätöstä.

## Paikallinen varmennus 2026-10-11

- Lähtötaso: `.venv\Scripts\python.exe -m pytest -q`, **1345 passed**, 1 warning
  (392,70 s).
- Luonnos-, palvelu- ja v1/v2-migraatiosarja: **255 passed**, 1 warning (59,61 s).
- Lopullinen koko regressio: `.venv\Scripts\python.exe -m pytest -q`,
  **1476 passed**, 1 warning (448,61 s). Olemassa olevat enum-parametroidut
  oikeustestit kattavat myös uudet oikeudet. Sama Starlette/httpx-deprecation
  esiintyi jo lähtötasossa.
- DELETE/WAL-testit kattavat omistajan/korjaamon/orgin eristyksen, tokenittoman
  kontekstin kiellon, IT_ADMIN/SERVICE-kiellon, revoke/regrant-version, session/
  rooli/disabled/auth_version/membership-perumiset ennen replaytä ja commitin
  aikana, kilpailevan CAS:n, saman noncen kilpailun, restart-replayn,
  fingerprint-konfliktin, exact-expiryn, muuttumattoman sisällön, hylkäyksen,
  bounded-owner-cleanupin sekä create/save/discard/cleanup-commitvirheen rollbackin.
- Migration-2-testit todentavat aidon version 1 datan säilymisen, idempotentin
  uusinnan, tuntemattoman/epäjatkuvan ledgerin kiellon ja DDL-rollbackin.
- Python-syntaksi, dokumenttilinkit ja `git diff --check` tarkistettiin.

Haara on `feature/web-app`, HEAD `e93aeb31bf474fcbcbf633a69c7d33e32ba1f96d`.
Staging-, commit-, push-, merge- tai deploy-toimia ei tehty eikä käyttäjiä
provisionoitu testifixtuurien ulkopuolella. Tuotantokantoja ei avattu.
Molemmat business-dokumentit, `var/carbit.db`, CP4B-1-backup ja kaikki tämän
vaiheen ulkopuoliset aiemmat muutokset säilyivät SHA-256-varmennettuina.
