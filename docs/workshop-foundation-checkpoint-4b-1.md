# CP4B-1: autokorjaamo-organisaatio ja korjaamoeristys

CP4B-1 lisää nykyiseen työjonoon organisaatio-, toimipiste- ja jäsenyysrajat.
Organisaatio tarkoittaa Carbit Oy:n **autokorjaamotoimintaa**. Autopesu ja
henkilöstövuokraus eivät kuulu tähän malliin. Tämä vaihe ei toteuta pysyviä
luonnoksia, delegointia, yhteismuokkausta tai ulkoisia integraatioita.
Liiketoimintadokumenttien avoimet kysymykset pysyvät avoimina.

## Tietomalli ja migraatiot

Liiketoimintatietokantaan lisätään erillinen `workshop_schema_migrations`
-migraatioloki ja sen versio 1:

- `repair_organizations`: autokorjaamo-organisaatiot ja niiden aktiivisuus.
- `workshops`: organisaation korjaamot, tila ACTIVE, PLANNED tai INACTIVE.
- `workshop_principals`: ei-salainen viite auth-varaston tunnisteeseen ja käyttäjän UUID:hen.
- `workshop_memberships`: eksplisiittinen jäsenyys, aktiivisuus ja kasvava versio.
- `active_user_workshops`: palvelimen hallitsema toimipistevalinta.
- `inquiry_workshop_assignments`: työn muuttumaton organisaatio- ja korjaamokohdistus.
- `scoped_inquiry_creation_requests`: uusien tiedustelujen korjaamokohtaiset retry-rivit.
- `workshop_audit_events`: offline-kohdistusten, jäsenyysmuutosten ja valintojen historia.

Auth-migraatio 2 lisää `auth_store_identity`-tauluun pysyvän satunnaisen UUID:n.
Se erottaa eri auth-varastojen jäsenyydet. Tilejä, salasanoja, MFA-tietoja,
auth-versioita, sessioita tai CSRF-mekanismia ei muuteta. Auth-varaston siirrossa
säilytetään sama tunniste; uuden riippumattoman auth-varaston tilit tarvitsevat
omat hyväksytyt jäsenyydet. Tunnisteen tarkoituksellinen muuttaminen ei kuulu tähän CLI:hin.

Skeemamigraatiot ovat transaktionaalisia, lisääviä ja toistettavia. Tuntematon
tai epäjatkuva skeemaversio estää alustuksen. Vanhoihin töihin ei lisätä
arvattuja oletuskorjaamoja: alustus jättää ne kohdistamattomiksi ja API:n
ulkopuolelle. Vanhat työ- ja tapahtumatunnisteet, versiot, hintasnapshotit,
työaikasuunnitelmat, hyväksynnät ja retry-vastaukset säilyvät ennallaan.

Työaikarivit, tapahtumat ja labour-retryt kuuluvat samaan korjaamoon niiden
olemassa olevan inquiry-viitteen kautta. Näitä muuttumattomia historiallisia
rivejä ei kirjoiteta uudelleen korjaamotunnisteen lisäämiseksi. Viiteavaimet
valvovat vanhoja suhteita ja uusia organisaatio-/korjaamosuhteita.
Korjaamokohdistusta ei voi muuttaa tai poistaa tavallisella SQL-yhteydellä;
korjaamojen väliset tiedonsiirrot tarvitsevat erillisen hyväksytyn menettelyn.

## Hyväksytyn paikallisen kehitysdatan siirto

Käyttäjä hyväksyi **vain tämän repositoryn `var/carbit.db`-kehitystietokannan**
kolmen tiedustelun ja niihin liittyvien työaika-, tapahtuma- ja retry-tietojen
kohdistuksen Workshop 1:een. Tämä ei ole tuotantotietojen tai muiden
tietokantojen siirtolupa.

Pysäytä paikallinen palvelin ennen siirtoa. Tarkista käytettävä tietokanta,
sen tiiviste ja sisältömäärät. Valitse uusi varmuuskopiopolku suojatusta
hakemistosta; aiempaa varmuuskopiota ei ylikirjoiteta.

```powershell
$databasePath = (Resolve-Path var/carbit.db).Path
$reviewedHash = (Get-FileHash -LiteralPath $databasePath -Algorithm SHA256).Hash
.\.venv\Scripts\python.exe -m webapp.workshop_admin migrate-approved-development --database $databasePath --expected-sha256 $reviewedHash --backup "C:\suojattu-kehitysbackup\carbit-before-cp4b1.db" --operator-reference "Hyväksyjä, päivämäärä ja kehitysdatan siirtopäätöksen viite"
```

Komento tarkistaa täsmällisen polun (myös symlinkit), SHA-256-preimagen,
kolme tiedustelua, kahdeksan tapahtumaa ja SQLite-eheyden. WAL-tiedoston
läsnäolo estää siirron; pysäytä kaikki tietokannan käyttäjät ensin.
`BEGIN IMMEDIATE` estää muita kirjoittajia tarkistuksen ja siirron välillä.
Ennen skeeman tai kohdistusten muuttamista tehdään SQLite-backup ja
tarkistetaan sen eheys. Siirto, kohdistukset ja auditointi tapahtuvat yhdessä
transaktiossa. Historiarivien muuttuminen tai tekninen virhe peruuttaa siirron;
varmuuskopio säilyy. Jo auditoitu valmis siirto voidaan todeta uudelleen
ilman uusia kohdistuksia tai auditointirivejä.

Workshop 1 luodaan aktiiviseksi ja Workshop 2 **PLANNED**-tilaan. Toista
korjaamoa ei aktivoida, eikä henkilöitä tai jäsenyyksiä luoda tässä siirrossa.
Muut tietokannat tarvitsevat erikseen tarkistetun kohdistuslistan ja
varmuuskopioivan siirtomenettelyn; sisäinen `assign_inquiries` hyväksyy vain
eksplisiittisen työn tunnistelistan ja kirjaa kohdistuksen.

Rollbackissa pysäytetään palvelin ja palautetaan koko varmennettu
liiketoimintatietokannan varmuuskopio. Uuden skeeman käyttö aikana syntynyt
data on otettava talteen ja sovitettava erikseen ennen palautusta.
Älä palauta vanhaa tietokantaa käynnissä olevan prosessin alle tai yhdistä
eri hetkien DB/WAL-tiedostoja. Auth-varaston versio 2 on lisäävä eikä tätä
liiketoimintatietokannan palautusta varten tarvitse poistaa auth-tietoja.

Ennen olemassa olevan oikean auth-varaston versiopäivitystä tarvitaan oma
varmennettu auth-varmuuskopio. CP3C:n vanha sovelluskoodi ei hyväksy
auth-skeemaversiota 2: sovelluksen palautus siihen vaatii yhteensopivan
koodin tai erikseen suunnitellun auth-palautuksen. Älä poista version 2
migraatiolokia tai vaihda auth-varaston tunnistetta palautuksen oikotienä.
Auth-varmuuskopio sisältää tunnistussalaisuuksia ja suojataan niiden mukaisesti.

## Jäsenyys ja aktiivinen korjaamo

Oikeaa auth-tietokantaa tai henkilöjäsenyyksiä ei provisionoida tässä työssä.
Kun oikea auth-varasto on saatavilla, ylläpitäjä tarkistaa HUMAN-tilien UUID:t,
roolit ja tilat ja hankkii nimenomaisen hyväksynnän jäsenyyksille.
OPENCLAW/SERVICE-tili tai puuttuva/estetty tili ei kelpaa henkilöjäseneksi.

Jäsenyys myönnetään erillisessä luotetussa offline-menettelyssä:

```powershell
.\.venv\Scripts\python.exe -m webapp.workshop_admin grant-membership --database "C:\suojattu\works.db" --auth-database "C:\suojattu\auth.db" --user-id "hyvaksytty-kayttajan-uuid" --workshop-id "hyvaksytty-korjaamon-uuid" --operator-reference "Hyväksyjä ja jäsenyyspäätöksen viite"
```

Komento vaatii jo olemassa olevat absoluuttiset DB-polut ja migroidut skeemat.
Se ei alusta auth-varastoa, luo tiliä tai muuta globaalia roolia. Viite ei saa
sisältää salasanaa, tokenia, MFA-salaisuutta tai muuta tunnistussalaisuutta.
`revoke-membership` käyttää samoja parametreja; peruminen kasvattaa versiota.
Myös uudelleenmyöntäminen kasvattaa versiota, jotta vanha konteksti ei elvy.

Jos käyttäjällä on täsmälleen yksi aktiivisen organisaation aktiivinen
korjaamojäsenyys eikä eksplisiittistä valintaa, se muodostaa aktiivisen
kontekstin. Usean käytettävän jäsenyyden tapauksessa pääsy evätään, kunnes
offline-operaattori tekee `select-workshop`-valinnan samoilla parametreilla.
Valinta ei luo jäsenyyttä. Peruttu valittu jäsenyys ei vaihdu hiljaisesti
toiseen korjaamoon. Selain ei voi valita korjaamoa otsakkeella, query-parametrilla
tai JSON-kentällä. Toimipistevalintojen käyttöliittymä jää myöhempään työhön.

## Valtuutus ja turvallisuusrajat

Jokainen suojattu business-pyyntö tarvitsee voimassa olevan henkilökohtaisen
MFA-istunnon, hyväksytyn globaalin roolin **ja eksplisiittisen jäsenyyden**.
OWNER/SERVICE_MANAGER-rooli ei luo jäsenyyttä eikä IT_ADMIN ohita korjaamorajaa.
Nykyiset roolit säilyvät oikeuksien ylärajana: IT_ADMIN on edelleen vain lukija.
HTTPS-, Origin-, eväste-, MFA-, istunto-, CSRF- ja yritysrajoitussäännöt säilyvät.
Jäsenyyden esitarkistuksessa evätty pyyntö ei päivitä istunnon idle-aikaa;
epäonnistunut kirjoitus ei myöskään päivitä sitä. Hyväksytty lukupyyntö
säilyttää nykyisen idle-päivityksen ennen handleria. Jos jäsenyys perutaan
tämän ja handlerin transaktion välissä, dataan pääsy estyy uudessa
tarkistuksessa, mutta aiempi auth-varaston idle-päivitys on jo tapahtunut.

Konteksti sisältää auth-varaston, käyttäjän, organisaation, korjaamon ja
jäsenyyden version. Palvelin muodostaa sen uudelleen operaatiota varten.
Liiketoimintatransaktio tarkistaa jäsenyyden, version, aktiivisen valinnan
sekä organisaation ja korjaamon tilan ennen tietojen tai retryjen lukemista.
Kirjoitustransaktion lukko sarjallistaa jäsenyyden perumisen ja kirjoituksen.
Lukutapahtuma käyttää yhtä johdonmukaista SQLite-snapshotia.

Listaus sisältää vain aktiivisen korjaamon työt. Vieras tai kohdistamaton työ
palauttaa saman 404:n kuin olematon työ myös väärällä versiolla.
Puuttuva/peruttu/epäselvä jäsenyys palauttaa yleisen 403:n ilman tietoluetteloa.
Sisäiset työaikasuunnitelma-, hyväksyntä-, historia- ja hinnoittelupolut
tarkistavat työn korjaamon ennen retry-vastausta tai versionvirhettä.

Uusien tiedustelujen idempotenssi on korjaamokohtainen. Vanhat
`inquiry_creation_requests`-rivit pysyvät muuttumattomina: ne luetaan vain
niiden työn hyväksytyn kohdistuksen kautta. Vanha sopimaton tekijä tai eri
pyyntösisältö ei saa omaksua aiempaa retry-vastausta.

Auth- ja liiketoimintatietokannat ovat edelleen erillisiä. Myöhempi
[CP4B-1-valtuutuksen kovennus](workshop-auth-business-hardening.md) pitää
auth-kirjoitusvarauksen business-commitin yli: ensin committanut auth-peruminen
estää kirjoituksen, ensin varauksen saanut hyväksytty business-transaktio
valmistuu ennen auth-perumisen commitia. Pelkkä perumispyynnön aloitus ei
keskeytä aiemmin valtuutettua transaktiota. Katso erilliset palautusvaatimukset
ja rajatut lukitusbudjetit kovennusdokumentista.
Korjaamojäsenyyden tarkistus ja muutos sen sijaan käyttävät samaa
liiketoimintatietokantaa ja kirjoituslukkoa. Sisäinen Python-konteksti on
luotetun palvelinkoodin sopimus, ei autentikointitodiste tai selaimelle annettu
capability. SQLite-tiedostoa/triggeriä muuttava OS-ylläpitäjä jää tämän rajan ulkopuolelle.

Jäljellä ovat oikean auth-ympäristön käyttöönotto ja hyväksytyt jäsenyydet,
Workshop 2:n erillinen aktivointipäätös sekä avoimet yrittäjäkysymykset.
Niitä ei ratkaista automaattisilla oletuksilla tässä vaiheessa.

## Toteutuksen tiedostot

Muutetut olemassa olevat tiedostot:

```text
webapp/README.md
webapp/auth_repository.py
webapp/database.py
webapp/http_access.py
webapp/labour_migrations.py
webapp/labour_service.py
webapp/main.py
webapp/workshop_identity.py
tests/http_auth_support.py
tests/test_webapp.py
tests/test_webapp_approved_labour_pricing.py
tests/test_webapp_auth_repository.py
tests/test_webapp_browser_auth.py
tests/test_webapp_business_csrf.py
tests/test_webapp_business_identity.py
tests/test_webapp_http_auth.py
tests/test_webapp_labour_migrations.py
tests/test_webapp_labour_service.py
```

Uudet tiedostot:

```text
webapp/workshop_admin.py
webapp/workshop_context.py
webapp/workshop_migrations.py
webapp/workshop_repository.py
tests/workshop_support.py
tests/test_webapp_workshop_isolation.py
tests/test_webapp_workshop_migrations.py
docs/workshop-foundation-checkpoint-4b-1.md
```

Kaksi aiemmin untracked-tilassa ollutta `docs/business/`-dokumenttia säilytetään
muuttamattomina. Kehitystietokanta ja sen varmuuskopio kuuluvat Gitin
ohittamaan `var/`-hakemistoon.

## Paikallinen varmennus 2026-10-11

- Lähtötilanne: `python -m pytest -q`, 992 passed, 1 warning (283,93 s).
- Lopullinen koko regressiosarja: sama komento, 1053 passed, 1 warning (311,55 s).
- Kohdennettu HTTP-portti-, eristys- ja migraatiosarja: 112 passed (30,37 s).
- 61 uutta testiä kattavat jäsenyydet, roolien ylärajat, väärennetyt ja
  vanhentuneet kontekstit, korjaamo-/organisaatiorajat, tapahtumat, työajat,
  retryt, alkuperäiset tiedot, auth-upgraden, varmuuskopiot ja rollbackin.
- Testiapurin liian varhainen DB-alustus korjattiin; luokittelematon reitti
  estää edelleen käynnistyksen ennen tietokantojen alustamista.
- Ainoa varoitus on jo lähtötilanteessa esiintynyt Starlette/httpx-deprecation.

Hyväksytty paikallinen kehitysmigraatio ajettiin testien jälkeen. Alkuperäinen
`var/carbit.db`-tiiviste oli
`7debde666a2c7ae815abe3cce8d500d56914416c9af4083b5cd4ff8d8d613ebe`.
SQLite-varmuuskopio on `var/backups/carbit-before-cp4b1-20261011.db`, SHA-256
`270d19a67a450f2420e428ee9663f56d574da00465913ce87c21ae754d905663`.
SQLite-backup varmennetaan sisällöltään ja omalla tiivisteellään; sen ei
tarvitse olla alkuperäisen tiedoston tavutarkka kopio.

Riippumaton read-only-vertailu varmisti kaikkien 11 alkuperäisen taulun rivien
säilymisen, mukaan lukien `sqlite_sequence`, migraatioloki, työversiot,
tapahtumat ja retryt. Molempien tiedostojen `integrity_check` palautti `ok`
ja `foreign_key_check` ei löytänyt virheitä. Kolme kohdistusta ja kaksi
auditointimerkintää luotiin. Workshop 1 on ACTIVE, Workshop 2 on PLANNED;
käyttäjäviitteitä ja jäsenyyksiä on nolla. Oikeaa auth-varastoa ei luotu.

Sovelluskoodi ja testit jäävät paikallisiksi muutoksiksi `feature/web-app`
-haaralle; staging-, commit-, push-, merge- tai deploy-toimia ei tehty.
