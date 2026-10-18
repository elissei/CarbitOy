# CARBIT Oy — SEC-1.2: rajattu autentikoinnin kovennus

Päiväys: 2026-10-13. Haara: feature/web-app.
HEAD: e93aeb31bf474fcbcbf633a69c7d33e32ba1f96d.

Tila: käyttäjän suorittamat SEC-1.2-testit läpäisty; riippumaton auditointi PASS.
Tuotantovalmiutta ei ole hyväksytty, ja käyttöönoton esteet ovat edelleen voimassa.
Käyttäjän ilmoittama riippumaton SEC-1.1-auditointi: PASS WITH RESERVATIONS,
ei P0/P1-löydöksiä. Käyttäjän vahvistamat SEC-1.1-tulokset: 714 passed kohdennetusti,
2320 passed koko sarjassa ja yksi tunnettu StarletteDeprecationWarning.
Nämä lähtötulokset eivät ole SEC-1.2:n testituloksia.

## T1 — Tunnisteeton rate-limit-auditointi

Uusista AUTH_RATE_LIMITED-tapahtuman details_json-tiedoista poistettiin scope_hash
ja asiakkaan/tilin kiintiöikkunan window_start. Scope_kind on global, account tai
client. Säilyvät palvelimen määrittelemä phase, HTTP-kutsussa operation sekä
sampled/sample_window_seconds-metatiedot. Sample_key on kiinteä kategorinen
RATE:<phase>:<scope_kind>, eikä sisällä laskuritiivistettä tai henkilökohtaista
ikkunatunnistetta. Actor ja subject ovat näissä tapahtumissa edelleen tyhjiä.

Tiliä tai asiakasta ei voi yksilöidä uusista tapahtumien details-kentistä. Tämä
koskee myös sample_keytä: tunnisteen poistaminen vain scope_hash-kentästä olisi
jättänyt aiemman tiivisteen sample_key-kenttään. Merkittävien kirjautumis-, MFA-
ja revokaatiotapahtumien nykyistä auditointia ei muutettu.

Auth_rate_limits-taulun nykyiset suojatut laskuriavaimet säilyvät kiintiöiden
valvomiseksi. Tämä tehtävä ei muuta niiden skeemaa. Vanhoja muuttumattomia
auditointitapahtumia ei kirjoiteta uudelleen, poisteta tai migroida. Historiallisissa
SEC-1.1-tapahtumissa voi edelleen olla aiemmat tunnisteet; niiden mahdollinen
retentio-/poistomenettely vaatii erillisen luvan. Käyttäjän tietokantoihin ei koskettu.

## T2 — Logoutin anonyymisti kulutettavan globaalin kiintiön korjaus

Vaihtoehtojen vertailu:

| Vaihtoehto | Vaikutus |
| --- | --- |
| Globaalin logout-rajan poistaminen | Ratkaisisi tämän yhteisen kiintiön lukituksen mutta poistaisi myös kirjautuneiden hajautetun väärinkäytön kokonaisrajan. Ei valittu. |
| Globaalin varauksen siirto validoinnin jälkeen | Anonyymit/virheelliset pyynnöt eivät voi kuluttaa muiden globaalia logout-budjettia; autentikoitujen pyyntöjen kokonaisraja säilyy. Valittu. |

Toteutus on kaksivaiheinen:

1. Nykyinen tilaton transport-esitarkistus ja ASGI-yhteysosoitteeseen sidottu
   logoutin asiakaskiintiö varataan edelleen ennen session-/CSRF-tarkistuksia.
2. _check_write ja _logout validoivat edelleen nykyisen session sekä siihen
   sidotun CSRF-todisteen. _logout varmistaa myös tyhjän payloadin.
3. Vasta tämän jälkeen _logout kutsuu palvelimen sisäistä reserve_logout_global-
   menetelmää. Se varaa vain globaalin HTTP_LOGOUT-kiintiön. Asiakasta ei veloiteta
   uudelleen. Tämän menetelmän kutsuminen edellyttää luotetun palvelinadapterin
   tekemää validointia; se ei ole selaimelle tarjottu API.
4. Nykyinen revoke_session tarkistaa istunnon uudelleen atomisessa transaktiossa
   ja peruu sen. Cookie poistetaan ja 204 palautetaan vain onnistuneen perumisen jälkeen.

Oletusrajat säilyvät: logoutin asiakas- ja globaalikiintiö 120/1200 pyyntöä 60
sekunnissa. Login/MFA:n tiukat 5/30-rajoitukset 300 sekunnissa sekä palvelutason
PASSWORD/MFA-rajat säilyvät. Session/CSRF:n erillinen ylläpitokiintiö säilyy.
Forwarded-otsakkeita ei käytetä identiteettinä. Ei CSRF-, istunto- tai cookie-poikkeuksia.

Regressiot simuloivat kuudesta oikeasta ASGI-vertaisosoitteesta 12 hylättyä pyyntöä
ja lisäestot globaalin rajan ollessa kaksi. Puuttuva/virheellinen istunto, virheellinen
CSRF ja virheellinen payload eivät luo tai kasvata globaalia logout-laskuria.
Sallittu logout toisesta osoitteesta saa 204:n ja istunto peruuntuu. Testit osoittavat
myös, että asiakasraja edelleen estää liikenteen ja globaaliraja edelleen estää
liialliset validit logoutit oikealla Retry-Afterilla ilman cookie-poistoa.

Tämä korjaa anonyymin liikenteen yhteisen globaalin kiintiön kulutuksen. Se ei lupaa,
ettei saman IP/NAT-osoitteen asiakaskiintiötä voisi täyttää, eikä poista prevalidoinnin
resurssikuormaa. Asiakassuoja säilytettiin tarkoituksella. Autentikoitu väärinkäyttö
voi edelleen täyttää globaalin rajan. Eri transaktioiden välissä tapahtuva revokaatio
voi aiheuttaa vahvistamattoman vastauksen jo hyväksytyn globaalivarauksen jälkeen;
tämä varaus lasketaan, eikä kiintiötä palauteta. Aiemman version jo kuluttama
globaalilaskuri säilyy nykyisen ikkunan loppuun; sitä ei nollata automaattisesti.

## T7 — Rinnakkainen ja kahden välilehden logout

Palvelintesti pakottaa kaksi pyyntöä revokaatiokohtaan ennen kumpaakaan perumista.
Se vaatii yhden 204:n ja yhden 401:n, yhden uuden revokaatiotapahtuman, aidon
revoked_at-arvon sekä oikean cookie-käyttäytymisen. Kaksi hyväksyttyä globaalia
varausta ja kaksi asiakasvarausta todentavat, ettei asiakasbudjettia veloiteta kahdesti.

Chromium-testeissä kaksi logout-pyyntöä on yhtä aikaa kesken. Molempia voittavia
välilehtiä kokeillaan. Vain 204:n saanut välilehti saa ilmoittaa onnistumisesta;
401:n saanut näyttää vahvistamattoman tilan. Toinen testi käyttää nykyistä
BroadcastChannel-ilmoitusta ja todentaa, että toisen välilehden paikallinen lukitus
ei väitä palvelimen jo peruneen istuntoa. Molemmat polut todentavat lopullisen perumisen.
Selaimen tuotantokoodia ei muutettu.

## S6 — Aikarajan konfiguraatiovarmennus

AuthPolicy hylkää limit_window_seconds- tai maintenance_window_seconds-arvon,
joka ylittää 86400 sekuntia. Positiivisten kokonaislukujen nykyinen tarkistus
säilyy. Ei hiljaista rajaamista tai oletusarvojen muutosta. Testit hyväksyvät 1 ja
86400 ja hylkäävät 86401 ja 172800 kummallekin asetukselle; oletukset ovat edelleen
300 ja 60. Tämä vastaa nykyisen vuorokauden laskurisiivouksen enimmäisikkunaa.

## S9 — SEC-1.1:n jälkikirjaus

SEC-1.1-checkpointiin lisättiin käyttäjän vahvistamat 714/2320 passed -tulokset,
yksi tunnettu StarletteDeprecationWarning sekä logout-testin valmistelukorjaus.
Complete()-valmistelu käytti jo yhden login- ja yhden MFA-varauksen; vanhassa
testissä laskettiin viisi lisävarausta eikä neljää jäljellä olevaa. Korjattu testi
todentaa laskurit ja perumisen. SEC-1.1:n sisältö säilytetään historiallisena;
SEC-1.2:n uusi toteutus ei kuulu sen käyttäjän vahvistamiin testituloksiin.

## Täsmällinen tiedostorajaus

Muokatut:

- webapp/auth_service.py — T1, kaksivaiheinen logout-kiintiö, S6.
- webapp/http_auth.py — globaali varaus vasta session-/CSRF-/payload-validoinnin jälkeen.
- tests/test_webapp_auth_rate_limits.py — olemassa olevan logout-global-testin
  varaus simuloi nyt validin logoutin molemmat vaiheet; sen esto-odotukset säilyvät.
- docs/auth-rate-limit-checkpoint-sec-1-1.md — käyttäjän vahvistamat tulokset ja testikorjaus.

Lisätyt:

- tests/test_webapp_auth_security_sec_1_2.py — yksityisyys, aikarajat, vastustajaliikenne,
  voimassa oleva globaali raja, rinnakkainen revokaatio ja cookie-käyttäytyminen.
- tests/test_webapp_browser_security_sec_1_2.py — kahden välilehden vahvistukset.
- docs/auth-rate-limit-checkpoint-sec-1-2.md — tämä checkpoint.

Ei skeema- tai migraatiomuutoksia. Domainin 3A/3B, WD-2/WD-2.1, WorkPlan/LabourPlan,
liiketoimintatoteutus ja muut ennestään muokatut/seuraamattomat tiedostot säilyvät.
Ei riippuvuusasennuksia tai tuotantoympäristön muutoksia.

## Testikomennot ja toteutunut varmennus

Työhakemisto C:\Users\eliss\Projects\carbitoy-web.
Kohdennettu Windows PowerShell-komento, jota yritettiin ensin:

~~~powershell
.\.venv\Scripts\python.exe -B -m pytest -p tests.wd2_test_runtime -p no:cacheprovider tests/test_webapp_auth_security_sec_1_2.py tests/test_webapp_browser_security_sec_1_2.py tests/test_webapp_auth_security_sec_1_1.py tests/test_webapp_browser_security_sec_1_1.py tests/test_webapp_auth_rate_limits.py tests/test_webapp_auth_service.py tests/test_webapp_auth_repository.py tests/test_webapp_http_auth.py tests/test_webapp_http_access.py tests/test_webapp_business_csrf.py tests/test_webapp_browser.py tests/test_webapp_browser_auth.py tests/test_webapp_workshop_isolation.py tests/test_webapp_auth_business_ordering.py -q
~~~

Sen jälkeen yritetty koko nykyinen regressiosarja:

~~~powershell
.\.venv\Scripts\python.exe -B -m pytest -p tests.wd2_test_runtime -p no:cacheprovider -q
~~~

Molemmat komennot estyivät ennen Python-prosessin luontia:

~~~text
exec_command failed: CreateProcess { message: "Rejected(\"Failed to create unified exec process: helper_unknown_error: setup refresh had errors\")" }
~~~

Codexin SEC-1.2-ajoyrityksistä ei saatu keräysmäärää tai ajotuloksia:
pytest/Chromium ei käynnistynyt. Näitä epäonnistuneita yrityksiä ei kirjata
läpäistyiksi testeiksi. Uusissa moduuleissa on staattisesti yhdeksän palvelin-
ja kaksi selaintestifunktiota parametrisoituine tapauksineen; tämä ei ole keräysmäärä.
Lähdekatselmointi ja tiedostorajauksen tiivistevertailu tehtiin. Paikallisen Starletten
TestClient-lähdekoodista tarkistettiin ASGI-vertaisosoitteen testiasetuksen saatavuus.

Git-status-komento estyi samalla alustusvirheellä. Haara varmennettiin onnistuneella
git branch --show-current -komennolla; Git-tila tarkistettiin myös lukemalla indeksi
ja Git-metatiedot. Indeksi ja HEAD säilyivät muuttumattomina. Sandboxia ei ohitettu.
Nykyinen tests.wd2_test_runtime ohjaa ajon väliaikaiset tiedostot projektin
.test-tmp-hakemistoon; -B ja no:cacheprovider estävät bytecode-/pytest-cache-kirjoitukset.

## Myöhempi käyttäjän ajovarmennus ja riippumaton auditointi (U3)

Käyttäjän Windows-ympäristössä suorittamat SEC-1.2-testit:

| Testisarja | Käyttäjän vahvistama tulos | Kesto | Varoitukset |
| --- | --- | --- | --- |
| Kohdennettu SEC-1.2-regressio | 740 passed | 388.19 sekuntia | 1 tunnettu StarletteDeprecationWarning |
| Koko projektin regressio | 2346 passed | 878.41 sekuntia | 1 tunnettu StarletteDeprecationWarning |

Nämä myöhemmät onnistuneet käyttäjän ajot ovat erillisiä yllä dokumentoiduista
Codexin helper_unknown_error-virheeseen päättyneistä käynnistysyrityksistä.
Codex ei suorittanut testejä tämän dokumentaatiopäivityksen yhteydessä.

Käyttäjän ilmoittama riippumaton Claude Opus 5.5 -auditointi: PASS,
nolla uutta P0–P2-löydöstä. T1, T2, T7 ja S6 on suljettu. Auditoinnin jäljelle
jättämät P3-löydökset ovat U1, U2 ja U3; U3 ratkaistaan tällä dokumentaation
päivityksellä, joten avoimiksi jäävät U1 ja U2.

Testitulokset ja auditoinnin PASS eivät merkitse tuotantohyväksyntää:
tuotantovalmiutta ei ole hyväksytty, ja käyttöönoton esteet ovat edelleen voimassa.

## Dokumentoitavat auditointivaraukset — ei toteutuksia tässä vaiheessa

| Tunnus | Nykyinen raja ja erillisen päätöksen kohde |
| --- | --- |
| T3 | Transport-hylkäyksen auditointinäytteen haku käyttää yhä SQLite-transaktiovarausta; prevalidoinnin hajautettu kuorma ja audit contention jäävät operatiiviseksi riskiksi. Ei uutta välimuistia, auditointipalvelua tai transaktiouudistusta. |
| T4 | Auth tarkistaa Sec-Fetch-Site-arvon, jos mukana. Puuttuva Fetch Metadata sallitaan edelleen nykyisille API-asiakkaille. Pakollista otsaketta tai uusia business-reittien metadataehtoja ei toteutettu. |
| T5 | Näytteistys ei ole täydellinen tapahtumalaskuri. Uusi privacy-raja yhdistää saman phase/scope_kind-luokan estot; HTTP-näytteistys säilyy operation/status-kohtaisena. Kategorioiden törmäykset ja 4096 tunnisteen hakurajan ylitys voivat muuttaa edustavuutta. Ei uutta säilytys- tai aggregointimallia. |
| T6 | Tilikohtainen todellinen Retry-After voi paljastaa kiintiöikkunan ajoitusta. Tarkkaa arvoa ei muutettu tai peitetty tässä rajauksessa. |
| S4 | Uusia auditointi-indeksejä ei lisätty. Mahdollinen tietokantaindeksi vaatii erillisen skeemapäätöksen. |
| S5 | ASGI:n osoitetekstin IPv6-normalisointia tai verkkoaluekohtaista kiintiötä ei lisätty. |
| Operointi | Proxy-luottamuksen, ingress-rajojen, jaettujen IP-osoitteiden ja kuormitusvarmennuksen kovennus vaatii erillisen operointitehtävän. Ei käyttöönottoa tai palvelinkonfiguraatiomuutoksia. |

SEC-1.2:n kohdennettu ja täysi regressio on varmennettu käyttäjän yllä ilmoittamilla
ajoilla. Tuotantovalmiutta ei ole hyväksytty, ja käyttöönoton esteet säilyvät.
Toteutus ei edellyttänyt skeemamuutosta. Vanhojen
auditointitunnisteiden käsittely, yllä olevat lisäkovennukset ja tietokantaskeeman
muutokset edellyttävät erillistä hyväksyntää. SEC-2:ta tai WD-3:a ei aloitettu.
Stage-, commit-, push-, merge- tai deploy-toimia ei tehty.
