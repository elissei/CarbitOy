# CARBIT Oy — SEC-1: autentikoinnin rate-limitin korjaus

Päiväys: 2026-10-13. Haara: feature/web-app.
HEAD ennen tehtävää: e93aeb31bf474fcbcbf633a69c7d33e32ba1f96d.

Tila: käyttäjän myöhemmin vahvistama SEC-1-testivarmennus: 469 passed
kohdennetussa sarjassa ja 2245 passed koko regressiosarjassa. Tulokset ovat
käyttäjän ilmoittamia; alla säilytetään tämän agenttiympäristön estyneiden ajojen
historiallinen kirjaus. SEC-1.1:n tulokset kirjataan erilliseen checkpointiin.
Käyttäjä vahvisti QA-1:n lähtötilanteeksi 173/173 kohdennettua testiä ja
2210/2210 koko sarjan testiä PASS sekä yhden riippuvuuden deprekaatiovaroituksen.
Nämä ovat ennen SEC-1:tä vahvistetut tulokset, eivät tämän muutoksen testituloksia.

## Juurisyy

HTTP-auth-operaatioiden laskurien nimet olivat jo erilliset, mutta jokaiselle
operaatiolle käytettiin samaa tiukkaa politiikkaa: viisi pyyntöä yhteysosoitetta
kohti ja 30 globaalisti viidessä minuutissa. Myös onnistuneet session- ja
CSRF-pyynnöt laskettiin, joten useat sivulataukset tai välilehdet saattoivat
lukita tavallisen käyttäjän. Session/CSRF ei teknisesti käyttänyt samaa laskuririviä
login/MFA:n kanssa; puute oli niiden käyttämä liian tiukka rajoituspolitiikka.

Lisäksi _reserve päivitti globaalia laskuria ennen tilin ja asiakkaan rajojen
tarkistamista. Asiakaskohtaisesti estetty varaus saattoi siksi kuluttaa globaalia
kiintiötä. Tilikohtainen esto saattoi vastaavasti kuluttaa muiden scopejen
budjettia tai muuttaa niiden aikajakson alkua.

## Toteutetut muutokset

| Tiedosto | Muutos |
| --- | --- |
| webapp/auth_service.py | Erillinen ylläpitopolitiikka ja yhteinen session/CSRF-kiintiö; kaikki scope-tarkistukset ennen laskurikirjoituksia |
| tests/test_webapp_auth_rate_limits.py | Palvelu- ja HTTP-tason regressiotestit |
| tests/test_webapp_browser.py | Kaksi lisättyä oikean Chromium-selaimen regressiotestiä |
| docs/auth-rate-limit-checkpoint-sec-1.md | Tämä checkpoint |

AuthPolicy saa kolme positiivista kokonaislukua vaativaa asetusta:
maintenance_window_seconds=60, maintenance_individual_attempts=120 ja
maintenance_global_attempts=1200. Asiakaskohtainen raja ei saa ylittää globaalia.

Session ja CSRF käyttävät samaa HTTP_MAINTENANCE-nimiavaruutta. Niiden
**yhteenlaskettu** oletusraja on 120 pyyntöä yhteysosoitetta kohti ja 1200 globaalisti
60 sekunnissa. Myös virheelliset ja anonyymit ylläpitopyynnöt lasketaan. Rajat ovat
sovelluspolitiikan alkuarvoja, eivät tuotannon kuormitustestillä varmennettuja arvoja.

Login, MFA ja logout säilyttävät alkuperäiset HTTP_AUTH-operaatiokohtaiset
5/30-rajoitukset 300 sekunnissa. PASSWORD- ja MFA-palveluvaiheiden tili-, asiakas-
ja globaalit rajat säilyvät. Onnistunut tunnistautumisyritys lasketaan edelleen.

_reserve tarkistaa globaalin, mahdollisen tilin ja asiakkaan laskurit ennen
minkään laskurin kasvattamista tai aikajakson nollaamista. AuthRepositoryn
olemassa oleva BEGIN IMMEDIATE -transaktio pitää tarkistuksen ja kirjoitukset
samassa atomisessa varauksessa. Estetty varaus kirjataan auditointiin, mutta
se ei kuluta muiden scopejen kiintiötä. Auditointi ja vanhojen laskurien siivous
säilyvät. HTTP-rajalla sallittu varaus lasketaan, vaikka myöhempi Origin-, CSRF-
tai tunnistautumistarkistus hylkäisi pyynnön; tämä säilyttää väärinkäytöneston.

## IP- ja turvallisuusraja

HttpAccessGate muodostaa TrustedClientContextin request.client.host-arvosta eli
ASGI-palvelimen toimittamasta osoitteesta. Tätä koodia ei muutettu. Sovellus ei
lue X-Forwarded-For-, Forwarded- tai X-Real-IP-otsakkeita asiakkaan tunnisteeksi.

Nykyiset proxy-ohjeet säilyvät: suorassa TLS-käynnistyksessä --no-proxy-headers;
mahdollisen TLS-terminoivan proxyn tulee korvata forwarded-otsakkeet ja ASGI:n
luottaa vain nimenomaisiin proxy-osoitteisiin, ei --forwarded-allow-ips=*.
Mitään proxy- tai tuotantokonfiguraatiota ei muutettu tässä tehtävässä.

CSRF-todisteen allekirjoitus ja cookie-/Origin-/istuntosidonta, Host/HTTPS-
tarkistus, MFA-vaatimus, tokenien vanheneminen ja kertakäyttöisyys, istunnon
peruminen, roolit ja korjaamoeristys säilyvät nykyisinä. CSRF-välimuisti ja
selainsovelluksen tuotantokoodi jätettiin ennalleen.

Tämä checkpoint korvaa vanhempien HTTP-checkpointien kuvauksen siitä, että myös
session ja CSRF käyttäisivät viiden minuutin tiukkaa HTTP-auth-politiikkaa.

## Lisätty testikattavuus

Uudessa testimoduulissa on 14 testifunktiota parametrisoituine tapauksineen;
selainmoduuliin lisättiin kaksi testifunktiota. Pytestin keräystä ei saatu ajettua.

- Session/CSRF-liikenne yli vanhan viiden pyynnön rajan; login/MFA-rajojen säilyminen
  ja ylläpitopyyntöjen toiminta tiukan kiintiön täytyttyä.
- Ylläpitoendpointien yhteinen, pysyvä kiintiö sekä 60/300 sekunnin ikkunoiden erottelu.
- Estettyjen asiakas- ja tilivarausten laskurien muuttumattomuus, muiden asiakkaiden
  jäljellä oleva kiintiö ja vanhentuneen globaalin ikkunan käsittely.
- Rinnakkaiset eri asiakkaat eivät voi ylittää globaalia rajaa.
- Väärennetyt forwarded-otsakkeet eivät ohita tiukkaa IP-rajaa.
- Virheelliset ylläpitopyynnöt ovat edelleen rajoitettuja; anonyymi ylläpito ei
  myönnä istuntoa tai pääsyä liiketoimintatietoihin.
- Sama istunto-CSRF-todiste käy useaan sallittuun tallennukseen ilman uusia
  ylläpitopyyntöjä; virheellinen todiste ja peruttu istunto hylätään.
- Seitsemän peräkkäistä sivulatausta sekä seitsemän samalla istunnolla avattua
  välilehteä; yksi CSRF-haku per välilehti ja todisteen paikallinen uudelleenkäyttö.

Olemassa olevia testi- tai suojausodotuksia ei heikennetty. Nykyinen salasana/MFA-,
HTTP-suojaus-, business-CSRF-, selain- ja korjaamoeristyssarja sisällytettiin
kohdennettuun ajokomentoon.

## Testiajot ja ympäristöeste

Työhakemisto: C:\Users\eliss\Projects\carbitoy-web.

Ensin kohdennettu sarja:

~~~powershell
.venv/Scripts/python.exe -B -m pytest -p tests.wd2_test_runtime -p no:cacheprovider tests/test_webapp_auth_rate_limits.py tests/test_webapp_auth_service.py tests/test_webapp_http_auth.py tests/test_webapp_http_access.py tests/test_webapp_business_csrf.py tests/test_webapp_browser.py tests/test_webapp_browser_auth.py tests/test_webapp_workshop_isolation.py -q
~~~

Sen jälkeen koko regressiosarja:

~~~powershell
.venv/Scripts/python.exe -B -m pytest -p tests.wd2_test_runtime -p no:cacheprovider -q
~~~

Kohdennettua komentoa yritettiin myös exec_command-asetuksella login=false, joka
jättää shellin kirjautumisprofiilin pois. Kaikki yritykset estyivät ennen
Python-prosessin luontia samalla virheellä:

~~~text
exec_command failed: CreateProcess { message: "Rejected(\"Failed to create unified exec process: helper_unknown_error: setup refresh had errors\")" }
~~~

| Sarja | Keräysmäärä | Tulos |
| --- | --- | --- |
| Kohdennettu SEC-1 ja suojausregressiot | Ei saatavilla | Ei käynnistynyt |
| Koko nykyinen regressiosarja | Ei saatavilla | Ei käynnistynyt |

Kyse on suoritusympäristön alustusvirheestä, ei havaitusta testiepäonnistumisesta.
Testien ei väitetä läpäisseen. Sandboxia ei ohitettu, oikeuksia ei korotettu eikä
paketteja asennettu. Nykyinen wd2_test_runtime-plugin ohjaa testien väliaikaiset
SQLite-kannat ja muut väliaikaistiedostot projektin .test-tmp-hakemistoon, kun
ajo saadaan käynnistymään. Pluginia ei muutettu.

## Jäljelle jäävät riskit ja rajaus

- Jaettu NAT- tai proxy-osoite jakaa asiakaskohtaisen kiintiön. ASGI:n virheellinen
  proxy-luottamus voi yhdistää käyttäjät tai sallia osoitteen väärentämisen;
  sovelluksen otsakkeiden ohittaminen ei korjaa palvelimen väärää konfiguraatiota.
- Useasta osoitteesta tuleva liikenne voi yhä täyttää globaalin kiintiön.
  Auditointi ja SQLite-kirjoitukset tarvitsevat ingress-rajoituksia ja operatiivista
  seurantaa. SEC-1 ei ole yleinen DDoS-ratkaisu.
- Selain näyttää ylläpito-429:n yhteydessä edelleen konservatiivisen viiden minuutin
  odotusohjeen, vaikka ylläpitokiintiön ikkuna on nyt 60 sekuntia. Tämä ei heikennä
  suojausta; käyttöliittymän odotusohjeeseen ei tehty muutoksia tässä korjauksessa.
- SEC-1:n myöhemmät testitulokset on vahvistettu käyttäjän toimesta: 469 passed
  kohdennetusti ja 2245 passed koko regressiosarjassa. Tämä vahvistus ei koske
  myöhempää SEC-1.1-muutosta.
- Domainia, WD-2/WD-2.1:tä, liiketoimintalogiikkaa, tietokantaskeemoja tai
  migraatioita ei muutettu. Uusi ylläpitonimiavaruus käyttää nykyistä
  auth_rate_limits-taulua; historialliset tiukat rivit siivotaan nykykäytännöllä.
- Työpuun ennestään muokatut tiedostot jätettiin ennalleen, paitsi tähän tehtävään
  kuuluva auth_service.py, johon tehtiin vain yllä kuvatut rajatut muutokset.
  Git-indeksiin ja HEADiin ei kirjoitettu. Stage-, commit-, push-, merge- tai
  deploy-toimia ei tehty. SEC-2:ta tai WD-3:a ei aloitettu.

## Käyttäjän myöhemmin vahvistamat SEC-1-ajot

| Sarja | Käyttäjän vahvistama tulos |
| --- | --- |
| Kohdennettu SEC-1-sarja | 469 passed |
| Koko regressiosarja SEC-1:n jälkeen | 2245 passed |

Näitä ajoja ei tehty tässä agenttiympäristössä. Vahvistus saatiin käyttäjältä
SEC-1.1-tehtävän lähtötietona.

## SEC-1.1-jatko

Logout-kiintiö, transport-esitarkistus, estojen auditointinäytteistys ja
Retry-After-/selainviestit päivitetään erillisessä
[SEC-1.1-checkpointissa](auth-rate-limit-checkpoint-sec-1-1.md).
Edellä kuvatut SEC-1:n alkuperäiset rajoitukset säilytetään historiallisina.
