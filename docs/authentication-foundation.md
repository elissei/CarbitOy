# Web 4E-2 / vaihe 1: tunnistuksen sisäinen perusta

Tämä dokumentti kuvaa sisäisen perustan alkuperäistä vaihetta.
[Checkpoint 2B-2](http-authentication-checkpoint-2b-2.md) lisää sen päälle
kirjautumisen, MFA-vahvistuksen ja istunnon HTTP-elinkaaren; rekisteröinti,
palautus ja selaimen kirjautumisnäkymä jäävät edelleen myöhempään vaiheeseen.

Web 4E-2 toteutti sisäisen tunnistusperustan ilman HTTP-reittejä. Sen päälle
toteutettu [Web 4E-3A / Checkpoint 1](http-authentication-checkpoint-1.md) suojaa
nykyiset liiketoimintareitit, HTML:n ja staattiset tiedostot MFA-istunnolla.
Kirjautumis- ja palautusreittejä ei vielä ole; selaimen normaali käyttö on
tarkoituksella estetty ennen seuraavaa tarkistuspistettä. Älä julkaise sovellusta
tuotantokäyttöön tämän vaiheen perusteella. `TrustedWorkshopContext` on yhä
sovelluksen sisäinen konteksti, ei todiste kirjautumisesta.

## Roolit ja tunnistusrajat

| Rooli | Identiteetti | Liiketoiminta | MFA-palautus |
| --- | --- | --- | --- |
| `OWNER` | Henkilökohtainen ihminen | Nykyisten HTTP-toimintojen luku ja kirjoitus; hyväksyntäsilta myöhemmin | Ei ylläpitäjän palautusoikeutta |
| `SERVICE_MANAGER` | Henkilökohtainen ihminen | Samat liiketoimintaoikeudet kuin yrittäjällä | Ei ylläpitäjän palautusoikeutta |
| `IT_ADMIN` | Erillinen henkilökohtainen ihminen | HTTP-liiketoimintatietojen luku, ei kirjoitusta tai automaattisia hyväksyntäoikeuksia eikä toisen käyttäjänä esiintymistä | Hyväksytyssä arkkitehtuurissa toisen käyttäjän palautuksen itsenäinen hallinta; oma hätäpalautus offline-menettelyllä |
| `OPENCLAW` | Palveluidentiteetti | Ei ihmiskäyttäjän oikeuksia tai työaikahyväksyntää | Ei käyttäjähallintaa, ihmiskirjautumista tai MFA-palautusoikeuksia |

**Hyväksytty arkkitehtuuripäätös, ei valmis käyttäjähallintatoteutus:** IT_ADMIN
saa itsenäisesti luoda, sulkea ja poistaa käyttäjätilejä, määrittää ja muuttaa
rooleja sekä mitätöidä istuntoja. Se saa käynnistää ja hallita toisen käyttäjän
MFA-palautusta ilman toisen liiketoimintakäyttäjän teknistä hyväksyntää.
Näihin teknisiin hallintatoimiin ei tarvita toisen ihmisen hyväksyntää.
Oman MFA:n hätäpalautus vaatii erikseen hyväksytyn offline-menettelyn.

Nykyinen provisionointi on luotetun offline-operaattorin sisäinen toiminto,
ei valmis IT_ADMIN-valtuutettu käyttäjähallintapalvelu. `revoke_session()`
mitätöi kutsujan oman istunnon. Tilien sulkemisen/poistamisen, roolimuutosten
ja muiden käyttäjien istuntojen mitätöinnin valtuutetut palvelut ovat vielä
toteuttamatta. Nykyinen MFA-reset koskee vain OWNER- ja SERVICE_MANAGER-tilejä;
hyväksytyn laajemman mallin ja turvallisen rekisteröintivaltuutuksen toteutus
on erillinen jatkotyö.

Käyttäjähallinta ei anna ylläpitäjälle käyttäjän salasanaa, MFA-salaisuutta,
palautuskoodeja tai istuntoa. Ylläpitäjän toimet kirjataan omalla tunnisteella.
Roolimuutosten epäsuora vaikutus liiketoimintavaltuuksiin ja oman tilin
roolimuutosten säännöt täsmennetään; IT_ADMIN-rooli ei itsessään oikeuta
työaikahyväksyntään. Tilin poistaminen ei saa poistaa päätöshistoriaa tai
johtaa pysyvän käyttäjätunnisteen uudelleenkäyttöön.

`User.can_approve_labour` kuvaa roolin kelpoisuutta, ei myönnä käyttöoikeutta.
Käyttäjämallia, roolitekstiä tai itse rakennettua kontekstia ei saa hyväksyä
tunnistustodisteeksi. Tulevan adapterin tulee ratkaista käyttäjä
`AuthService.require_session()`-kutsulla jokaisessa suojatussa pyynnössä ja
muodostaa siitä erikseen valtuutettu liiketoimintakonteksti. Tätä siltaa ei ole
vielä toteutettu. OpenClaw'n palvelutunnisteiden myöntämistä ei ole toteutettu.

## Moduulit ja tallennus

- `webapp/auth_models.py`: roolit, julkinen käyttäjämalli, salatut/maskatut
  kertaluonteiset palautusarvot, istuntotietomalli ja MFA-palautuskomento.
- `webapp/auth_crypto.py`: Argon2id, PyOTP, Fernet/MultiFernet ja satunnaiset tokenit.
- `webapp/auth_repository.py`: erillinen SQLite-varasto, lisäävä versioitu migraatio,
  keskitetyt yhteysasetukset ja transaktiot.
- `webapp/auth_service.py`: sisäinen salasana/MFA/istuntovirta ja ylläpitäjän palautus.

`AuthRepository` tarvitsee eksplisiittisen tietokantapolun olemassa olevaan,
käyttöoikeuksilla suojattuun hakemistoon. Suositus on erillinen auth-tietokanta
palvelutilin hallinnassa, pois staattisista tiedostoista ja repositoryn seurannasta.
Tuotantopolku ja käyttöjärjestelmäoikeudet päätetään myöhemmin. Käynnistys ei
luo käyttäjiä, avaimia tai oletussalasanoja. Web 4E-3A:n `webapp.main` alustaa
auth-varaston validoidun konfiguraation perusteella sovelluksen elinkaaressa.

Taulut ovat `auth_users`, `auth_challenges`, `auth_enrollments`,
`auth_recovery_codes`, `auth_sessions`, `auth_rate_limits`, `auth_events`,
`auth_notification_outbox`, `auth_reset_requests` ja `auth_schema_migrations`.
Ne eivät kirjoita hyväksyntähistoriaan, domainiin tai hinnoitteluun.
Migraatio toimii atomisesti ja toistettavasti myös olemassa olevan work-tietokannan
rinnalla; testit varmistavat aiempien taulujen, triggerien ja tietojen säilymisen.
Tuntematon auth-skeemaversio pysäyttää alustuksen.

Jokaisessa auth-yhteydessä käytetään `foreign_keys=ON` ja `recursive_triggers=ON`.
Kirjoitukset käyttävät `BEGIN IMMEDIATE` -transaktiota. Turvallisuustapahtumat ja
palautuspyyntöjen tulokset ovat muuttumattomia UPDATE-, DELETE- ja
INSERT OR REPLACE -operaatioita vastaan näissä yhteyksissä. Järjestelmän
ylläpitäjä tai DB-tiedostoon kirjoittava henkilö voi silti poistaa triggerit tai
muuttaa tiedoston ulkopuolelta: tämä ei ole ylläpitäjänkestävä allekirjoitettu loki.

## Salasanat, MFA ja istunnot

Salasanoissa käytetään [argon2-cffi:n PasswordHasheria](https://argon2-cffi.readthedocs.io/en/stable/api.html):
Argon2id, 64 MiB muistia, kolme iteraatiota, kaksi rinnakkaisuuskaistaa,
satunnainen 16 tavun suola ja 32 tavun tiiviste. Muistityökustannus perustuu
[RFC 9106:n pienemmän muistin vaihtoehtoon](https://www.rfc-editor.org/info/rfc9106/);
kaksi kaistaa on kohdelaitteen vuoksi valittu sovellusparametri. Mittaa viive
oikealla palvelimella ennen tuotantohyväksyntää. Uudet salasanat ovat 15–256
merkkiä, enintään 1024 UTF-8-tavua. Salasanoja ei trimmata tai lokiteta.
Tuntemattoman/estetyn tilin tarkistus käyttää dummy-tiivistettä. Hashaus tapahtuu
SQLite-kirjoituslukon ulkopuolella, enintään kaksi samanaikaista operaatiota
PasswordService-instanssia kohti. Käytä yhtä palveluinstanssia per prosessi;
prosessi- ja pyyntöjonojen rajat kuuluvat myöhempään palvelinasennukseen.

Onnistuneen salasanatarkistuksen jälkeen `check_needs_rehash()` vertaa tiivisteen
parametreja nykyiseen PasswordHasheriin. Tarvittava uusi tiiviste lasketaan
kirjoituslukon ulkopuolella. Päivitys tehdään vanhan tiivisteen ja auth-version
ehdollisella UPDATElla samassa transaktiossa `PASSWORD_REHASHED`-tapahtuman ja
MFA-haasteen kanssa. Tekninen virhe peruuttaa nämä kaikki; aiemmin varattu
kirjautumisyritys säilyy yritysrajoituksessa. Väärä salasana ei käynnistä rehashia.
Tiivisteitä tai salasanaa ei lisätä auditointitapahtumaan.

Jos toinen kutsu ehtii vaihtaa tiivisteen, annettu salasana tarkistetaan kerran
uudelleen ajantasaista tiivistettä vasten lukon ulkopuolella. Uusi kilpailu
hylätään. Käyttäjän vaihtuminen, käytöstä poisto tai auth-version muutos
hylätään heti, myös MFA-resetin yhteydessä. Pelkkä parametripäivitys ei muuta
auth-versiota, MFA:ta, palautuskoodeja tai olemassa olevia istuntoja. Se ei
myönnä istuntoa: toinen tekijä tarvitaan edelleen.

1. `begin_login()` tarkistaa salasanan ja palauttaa enintään viisi minuuttia
   voimassa olevan haasteen. Tämä haaste **ei ole istunto**.
2. Ensimmäisessä rekisteröinnissä tai ylläpitäjän MFA-palautuksen jälkeen
   `begin_mfa_enrollment()` palauttaa salaisuuden/provisionointilinkin vain
   käyttäjän oman salasanan tarkistuksen jälkeen. Tilin tunnistussalaisuuksia
   ei palauteta ylläpitäjän palautusrajapinnasta.
3. `confirm_mfa_enrollment()` vaatii toimivan TOTP-koodin, aktivoi MFA:n ja
   palauttaa kymmenen kertakäyttöistä palautuskoodia sekä ensimmäisen istunnon.
   Muissa kirjautumisissa `complete_login()` vaatii TOTP:n tai palautuskoodin
   saman salasanahaasteen jälkeen. Haaste kulutetaan atomisesti.
4. `require_session()` tarkistaa istunnon, käyttäjän tilan ja auth-version
   palvelimelta. `revoke_session()` mitätöi istunnon palvelimelta.

[PyOTP](https://pyauth.github.io/pyotp/) tuottaa/tarkistaa kuusinumeroisen,
30 sekunnin TOTP:n. Yksi viereinen aikajakso sallitaan kellovirheen varalta.
Viimeisin käytetty aikajakso säilyy SQLite-varastossa; samaa tai vanhempaa
jaksoa ei hyväksytä uudelleen, myöskään rinnakkaisissa kirjautumisissa.
Kellon synkronointi ja kellovirheen valvonta tarvitaan tuotannossa.

TOTP-salaisuudet, myös keskeneräiset rekisteröinnit, salataan
[Fernet/MultiFernetillä](https://cryptography.io/en/stable/fernet/).
Salattu sisältö sidotaan käyttäjätunnisteeseen ja käyttötarkoitukseen.
`TotpCipher` vaatii palvelimen luotetusti toimittaman avainjoukon eikä muodosta
automaattista korvaavaa avainta. Avainta ei tallenneta auth-tietokantaan, Git-repoon
tai asiakaspyyntöön. Ensimmäinen avain salaa uudet tiedot, vanhat avaimet voivat
purkaa aiempia tietoja; `rotate()` tukee uudelleensalausta. Avainten turvallinen
säilytys, käyttöoikeudet, varmuuskopiointi ja hallittu DB-avainrotaatio ovat vielä
tuotanto-operaatioiden tehtäviä. Avainten kadottaminen tekee TOTP-tiedoista
lukukelvottomia. Python-prosessissa salaisuus on lyhyesti selväkielinen.

Palautuskoodit sisältävät kukin 128 satunnaista bittiä. Vain käyttäjään sidottu
SHA-256-tiiviste tallennetaan; tämä sopii suuren entropian satunnaiskoodeille,
ei ihmisen valitsemille salasanoille. Käytetty koodi merkitään kulutetuksi samassa
transaktiossa kuin istunto. Koodeja ei voi lukea uudelleen palvelusta.
Kertaluonteiset salaisuudet/tokenit ovat tulosmalleissa `SecretStr`-kenttiä,
jotta normaali repr/JSON-serialisointi maskaa ne. Tulevan HTTPS-adapterin tulee
luovuttaa tarpeelliset kertaluonteiset arvot vain oikealle käyttäjälle ja estää
niiden lokittaminen, välimuistiin tallennus ja selaimen persistent storage.

Istuntotokenissa on 256 satunnaista bittiä, ja tietokantaan tallennetaan vain
SHA-256-tiiviste. Istunnon enimmäiskesto on kahdeksan tuntia ja joutoaika
30 minuuttia. Istunto on sidottu MFA-hetkeen ja käyttäjän auth-versioon.
Evästepolitiikka vaatii `__Host-carbit-session`, HttpOnly, Secure, SameSite=Strict
ja `Path=/` ilman Domainia. Tässä vaiheessa **evästeitä ei vielä lähetetä**.
HTTPS, CSRF/Origin-tarkistukset, cookie-luonti/poisto, kirjautumisreitit sekä
kirjautumisen jälkeinen tunnisteiden vaihto ovat tulevan adapterin tehtäviä.

## Yritysrajoitus ja auditointi

Salasana- ja MFA-yrityksillä on erilliset, pysyvät viiden minuutin laskurit:
enintään viisi yritystä tiliä tai palvelimen määrittelemää yhteysavainta kohti
ja 30 yritystä globaalisti vaihetta kohti. Myös onnistuneet yritykset kuluttavat
budjettia. `TrustedClientContext` tulee luotetusta palvelinkontekstista, ei
selaimen tekstistä tai tarkistamattomasta välityspalvelinotsakkeesta.
Vanhat laskurit siivotaan. Scope-tiivisteet ovat pseudonyymejä, eivät vahvaa
anonymisointia. TOTP-koodi ja palautuskoodi kulutetaan vain onnistumisessa;
kirjautumisyritysten epäonnistumiset ja rajoitukset säilyvät auditointina.
Transaktion teknisessä epäonnistumisessa MFA-muutokset ja istunto peruuntuvat.

`auth_events` tallentaa provisionoinnin, salasanavaiheen onnistumisen/hylkäyksen,
Argon2-parametrien päivityksen (`PASSWORD_REHASHED`),
MFA-rekisteröinnin, MFA-hylkäyksen, istunnon myöntämisen/mitätöinnin,
rajoitukset sekä MFA-palautuksen ja sen hylkäykset. Tekijä ja aikaleima tulevat
palvelimelta. Salasanoja, TOTP-salaisuuksia/koodeja, palautuskoodeja, raakaa
istuntotokenia tai asiakaspyyntöä ei lisätä tapahtumatietoihin. Palautuksen syy
ja tarkistusviite ovat sisäisiä auditointitietoja; niihin ei saa kirjata salaisuuksia.
Lokin säilytys-/tilarajat, hälytykset, request body -rajat, yleinen pääsynhallinta
ja infrastruktuurin liikenteen rajoittaminen toteutetaan erikseen. Sovellustason
yritysrajoitus ei yksin torju suurta DDoS-hyökkäystä.

## Ylläpitäjän MFA-palautus

**Nykyinen sisäinen reset ei toteuta suunniteltua turvallista hätäpalautusta.** Tekstiviite ei
todista henkilöllisyyttä teknisesti. Resetin jälkeen voimassa oleva salasana
riittää uuden MFA-rekisteröinnin aloittamiseen; rakenteiseen henkilöllisyyden
tarkistukseen sidottua kertakäyttöistä rekisteröintivaltuutusta ei vielä ole.
Tätä toimintoa ei julkaista HTTP-rajapintana eikä oteta tuotantoon tässä muodossa.
[Yhden ylläpitäjän palautussuunnitelma](mfa-emergency-recovery.md) erottaa
hyväksytyn oikeusmallin, nykyiset suojat ja vielä suunnitellut mekanismit.
IT_ADMIN voi suunnitellussa mallissa itse tehdä henkilöllisyystarkistuksen ja
myöntää valtuutuksen; toisen ihmisen teknistä hyväksyntää ei vaadita.

`reset_user_mfa(admin_session_token, MfaResetRequest)` vaatii oikean,
voimassa olevan IT_ADMIN-istunnon ja alle viisi minuuttia vanhan **TOTP**-todennuksen.
Rooli, reviewer_id, salasana, TOTP-salaisuus tai aikaleima eivät kelpaa
asiakaspyynnöstä valtuutukseksi. Palautuskoodilla kirjautunut ylläpitäjä ei saa
käynnistää tätä korkean riskin toimintoa ilman tuoretta TOTP-kirjautumista.
Yrittäjä ja huoltamopäällikkö eivät saa teknistä palautusoikeutta.

Käyttäjän henkilöllisyys tarkistetaan erillisellä menettelyllä **ennen kutsua**.
Pyyntö vaatii pysyvän tarkistusviitteen ja perustelun; koodi ei voi todistaa,
että ylläpitäjä suoritti henkilökohtaisen tarkistuksen. Menettely, hyväksyttävä
näyttö ja ilmoituskanava on päätettävä ennen oikeaa käyttöönottoa.
Ylläpitäjä ei tarvitse kohteen salasanaa eikä saa kohteen MFA-salaisuutta.

Yhdessä `BEGIN IMMEDIATE` -transaktiossa:

1. Tarkistetaan ylläpitäjän oma istunto ja kohteen odotettu auth-versio.
2. Poistetaan vanha aktiivinen/pending-MFA sekä vanhat palautuskoodit.
3. Mitätöidään kohteen kaikki aktiiviset istunnot ja salasanahaasteet.
4. Nostetaan auth-versiota ja asetetaan uusi MFA-rekisteröintitarve. Salasana ja
   liiketoimintarooli säilyvät; uutta istuntoa ei myönnetä palautustoiminnosta.
5. Tallennetaan muuttumaton auditointitapahtuma, käyttäjälle kohdistettu
   ilmoitusjonorivi ja idempotentti palautustulos.

Idempotenssi sidotaan ylläpitäjän pysyvään tunnisteeseen, request_id:hen ja
validoituun sisältöön. Sama uusintapyyntö palauttaa aiemman tuloksen ilman uutta
resetointia tai ilmoitusta, myös uuden MFA-rekisteröinnin jälkeen. Eri sisältö
samalla avaimella hylätään. Eri rinnakkaiset pyynnöt samalla auth-versiolla
tuottavat yhden palautuksen ja versionristiriidan. Tekninen epäonnistuminen
peruuttaa myös ilmoituksen; ilmoitusta ei lähetetä kesken transaktion.

**Ilmoitus on tässä vaiheessa pysyvä outbox-rivi, ei toimitettu viesti.**
Myöhempi luotettu toimittaja tarvitsee varmennetun käyttäjän oman yhteystiedon,
idempotentin lähetyksen, uusintayritykset ja toimituksen auditoinnin.
Palautuspyyntö ei voi antaa uutta ilmoitusosoitetta. Oikean käyttäjän palautusta
ei oteta tuotantokäyttöön ennen ilmoituskanavan ja tarkistusmenettelyn valmistumista.

Nykyisessä toteutuksessa IT-ylläpitäjän oma tai toisen ylläpitäjän pakotettu
MFA-palautus ja palveluidentiteetin palautus hylätään. Tämä kuvaa nykyistä
kohderajausta, ei hyväksytyn arkkitehtuurin kaikkia tulevia hallintaoikeuksia.
Omaa hätäpalautusta ei sallita ilman hyväksyttyä offline-menettelyä.
Menettelyn yksityiskohdat on suunniteltava erikseen; tässä ei ole ohitusavainta tai
kovakoodattua superkäyttäjää. Tekninen OS/root-ylläpito voi silti lukea/muuttaa
palveluprosessin tiedot: roolierottelu koskee sovelluksen valtuutuksia.

## Testaus ja jäljellä olevat integraatiot

Nykyiset Windows-hash-lukitukset sisältävät vain tarvittavat uudet paketit:
argon2-cffi 25.1.0, cryptography 50.0.2, pyotp 2.10.0 ja niiden kuusi pakettia
käsittävä riippuvuusketju. Aiemmat pakettiversiot säilyvät. Ubuntu-varmennus on
yhä suorittamatta; myös uudet CFFI/Argon2/cryptography-binääripyörät sekä Argon2:n
CPU-/muistikustannus on testattava kohteessa. Kryptografiaa ei toteuteta itse.

```powershell
.\.venv\Scripts\python.exe -m pip install --require-hashes --only-binary=:all: -r requirements-web.txt
.\.venv\Scripts\python.exe -m pip check
.\.venv\Scripts\python.exe -m pytest -q tests
git diff --check
```

Uudet testit kattavat salasanojen rajat/suolat, salauksen ja avainrotaation,
henkilö-/palveluroolit, MFA:n ennen istuntoa, replay-eston, kertakäyttöiset
palautuskoodit, vanhenemisen/mitätöinnin, pysyvät yritysrajat, ylläpitäjän
oikeudet, rinnakkaisuuden, idempotenssin, historialliset uusinnat, atomiset
rollbackit, migraatiot ja julkisten HTTP-reittien pysymisen ennallaan.
Rehash-regressiot kattavat vanhan tiivisteen päivityksen, väärän salasanan,
rinnakkaisen päivityksen, todellisen salasanamuutoksen kilpailun, rajatun
uusintatarkistuksen, MFA-resetin kilpailun ja hash-/haaste-/auditointikirjoitusten
yhteisen rollbackin.
Kaikki tunnukset/salasanat ovat keksittyjä testifixtureja; salausavaimet ja MFA-
salaisuudet generoidaan testin aikana. Repository ei sisällä oikeita tilejä.

Ennen tuotantoa tarvitaan erikseen kirjautumisen HTTP/HTTPS-/CSRF-integraatio,
palvelimella ratkaistu liiketoimintakonteksti ja auditointitekijä,
luotettu IT_ADMIN-käyttäjähallinta, salasanan vaihto/
salasanapalautus, yhden ylläpitäjän turvallisen hätäpalautuksen toteutus, MFA-ilmoitusten
toimitus, avainten ja backupien hallinta, ylläpitäjän offline-palautus sekä
Ubuntu-/laitetestaus. Tämän vaiheen palvelu ei tee mitään näistä automaattisesti.
