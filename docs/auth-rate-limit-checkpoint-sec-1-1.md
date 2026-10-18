# CARBIT Oy — SEC-1.1: logout, transport-esitarkistus ja estoauditointi

Päiväys: 2026-10-13. Haara: feature/web-app.
HEAD: e93aeb31bf474fcbcbf633a69c7d33e32ba1f96d.

Tila: käyttäjän myöhemmin vahvistamat SEC-1.1-ajot: 714 passed kohdennetusti
ja 2320 passed koko regressiosarjassa, yksi tunnettu StarletteDeprecationWarning.
Nämä ovat käyttäjän ilmoittamia tuloksia. Alla säilytetään tämän
agenttiympäristön estyneiden ajojen historiallinen kirjaus.
SEC-1:n käyttäjän vahvistamat tulokset, 469 passed kohdennetusti ja 2245 passed
koko sarjassa, on päivitetty SEC-1-checkpointiin. Ne eivät ole SEC-1.1:n tuloksia.

## S1 — Logout

SEC-1 jätti logoutille saman 5/30 pyynnön politiikan 300 sekunnissa kuin
kirjautumiselle ja MFA:lle. Toistuvat epäonnistuneet logout-pyynnöt saattoivat
estää myöhemmän asianmukaisen uloskirjautumisen.

Logout käyttää nyt itsenäistä HTTP_LOGOUT-nimiavaruutta ja ylläpitopolitiikkaa:
120 pyyntöä yhteysosoitetta kohti ja 1200 globaalisti 60 sekunnissa. Se ei jaa
laskureita login/MFA:n eikä session/CSRF:n kanssa. Session ja CSRF jakavat
edelleen HTTP_MAINTENANCE-kiintiön. Login/MFA:n tiukat HTTP- ja palvelukiintiöt
säilyvät.

Logout vaatii edelleen voimassa olevan istunnon, oikean Originin ja istuntoon
sidotun CSRF-todisteen. _check_write-, _logout- ja revoke_session-logiikkaa ei
muutettu. Selain ilmoitti jo ennen muutosta onnistumisesta vasta 204-vastauksen
jälkeen; tätä ehtoa säilytettiin ja testikattavuutta vahvistettiin. Hylätty tai
kadonnut vastaus näyttää nyt nimenomaisesti, ettei uloskirjautumista vahvistettu.
Paikallinen käyttöliittymälukitus tai välilehti-ilmoitus ei todista istunnon perumista.
Pyyntöä ei lähetetä automaattisesti uudelleen.

## S2 — Tilaton esitarkistus

Ennen korjausta järjestys auth-reitillä oli: asiakasavaimen muodostaminen,
rate-limit-varaus, _transport ja sen jälkeen reitin CSRF-/istunto-/MFA-tarkistukset.
Sec-Fetch-Site-tarkistusta ei ollut. Aiempi cross-site-navigaatiotesti hylkäsi
pyynnön bootstrap-otsakkeen puuttumisen vuoksi, ei Fetch Metadata -tarkistuksella.

Uusi auth-järjestys:

1. Nykyinen HTTPS-skeema ja yksi tarkasti hyväksytty Host.
2. Origin-otsakkeen lukumäärä ja tarkka sallittujen originien lista.
3. Jos Sec-Fetch-Site on mukana, sen pitää olla yksi same-origin-arvo.
   same-site, cross-site, none, yhdistetyt ja toistetut arvot hylätään.
   Otsakkeen puuttuminen on sallittu nykyisten API-asiakkaiden yhteensopivuudeksi.
4. POST-pyynnön Origin-vaatimus ja Originin yhtäpitävyys kohteen kanssa.
5. Nykyiset auth-otsakkeiden yksikäsitteisyys-, Authorization-/Transfer-Encoding-
   ja CSRF-bootstrap-merkinnän tarkistukset.
6. Nykyinen raakamuotoisten cookie-otsakkeiden syntaksi- ja kaksoiskappaletarkistus.
7. ASGI:n request.client.host-arvosta muodostettu TrustedClientContext ja kiintiövaraus.
8. Nykyiset cookie-tilan, allekirjoitetun CSRF-todisteen, istunnon ja MFA:n tarkistukset.
   POST-rungon lukeminen säilyy CSRF-tarkistuksen jälkeen.

Esitarkistus itsessään ei kirjoita tietokantaan. Esitarkistuksessa hylätty pyyntö
ei kuluta kiintiötä; sen jälkeen voidaan kirjata näytteistetty hylkäysauditointi.
Sisällöllinen CSRF-/tunnistautumishylkäys hyväksytyn transportin jälkeen kuluttaa
edelleen varatun kiintiön. Tämä säilyttää väärinkäytöneston.

Sovellus ei tulkitse X-Forwarded-For-, Forwarded- tai X-Real-IP-otsakkeita
asiakasidentiteetiksi. Nykyinen ASGI/reverse proxy -luottamusraja säilyy.
Sec-Fetch-Site on lisäsuoja, ei tunnistautumisen tai CSRF:n korvike.

## S3 — Hallittu auditointinäytteistys

Aiemmin jokainen kiintiöesto kirjasi AUTH_RATE_LIMITED-tapahtuman ja HTTP-rajalla
AUTH_HTTP_RESULT-tapahtuman. MFA-palvelupolut kirjasivat myös MFA_REJECTED-tapahtuman,
vaikka kiintiö esti itse tekijän tarkistuksen. _reserve teki lisäksi vanhojen
laskurien DELETE-siivoamisen ennen eston toteamista.

Nyt:

- AUTH_RATE_LIMITED näytteistetään vaihekohtaisesti estetyn scope-hashin ja sen
  kiintiöikkunan mukaan. Tapahtuma sisältää phase-, scope_hash- ja window_start-tiedot.
- AUTH_HTTP_RESULT-status 400, 403 ja 429 näytteistetään operaation ja statuksen mukaan.
- Näytteistysikkuna on 60 sekuntia. Näyte sisältää sample_key-, sampled=true- ja
  sample_window_seconds=60-metatiedot. Nämä eivät ole täydellisiä hylkäyslaskureita.
- Näytemerkki luetaan nykyisestä muuttumattomasta auth_events-taulusta. BEGIN IMMEDIATE
  sarjoittaa tarkistuksen ja uuden näytteen kirjauksen myös eri palveluinstansseissa.
  Ei prosessikohtaista katoavaa välimuistia, uusia tauluja, indeksejä tai migraatioita.
- Haku rajataan viimeisiin 4096 tapahtumatunnisteeseen olemassa olevan pääavaimen
  avulla. Jos merkki jää hakualueen ulkopuolelle vilkkaassa historiassa, sallitaan
  uusi edustava näyte ja sen jälkeen toistot taas näytteistetään. Tavoite on
  normaalisti yksi näyte avainta kohti 60 sekunnissa, ei ehdoton kokonaislukuraja
  kaikissa kuormitustilanteissa.
- Estetty varaus ei tee laskurien DELETE-siivoamista. Siivous tapahtuu vasta
  hyväksytyn varauksen yhteydessä. Estetty varaus ei kasvata muita kiintiöitä.
- MFA_REJECTED kirjataan edelleen jokaisesta varauksen sallimasta tekijän
  hylkäyksestä. Kiintiön estämä yritys kirjataan AUTH_RATE_LIMITED-näytteenä;
  sille ei kirjata harhaanjohtavaa tekijän hylkäystä, koska tekijää ei tarkistettu.
- PASSWORD_VERIFIED/-REJECTED, MFA_ENROLLED, SESSION_GRANTED/-REVOKED, MFA_RESET
  ja muut merkittävät tapahtumat säilyvät näytteistämättöminä. Onnistuneet
  HTTP-auth-tulokset kirjataan edelleen jokaisesta pyynnöstä. Myös HTTP-401-tulokset
  säilyvät yksittäisinä tapahtumina nykyisten kiintiöiden rajoissa.

Skeemamuutosta ei tarvittu. Toteutus vähentää estojen kirjoituksia mutta tekee
edelleen tietokantalukuja ja ottaa nykyisen SQLite-kirjoitusvarauksen.

## Retry-After ja käyttöliittymä

RateLimitError kuljettaa palvelimen laskeman positiivisen retry_after-arvon.
Se lasketaan pisimpään estävän aktiivisen scope-ikkunan jäljellä olevasta ajasta
samassa transaktiossa. HTTP-429 palauttaa tämän Retry-After-otsakkeessa myös
palvelutason tili-/MFA-rajoituksissa. Kiintiöitä ei nollata eikä lyhennetä.

Kirjautumisnäkymä ja session-CSRF-haku käyttävät yhteistä viestimuodostinta.
Kelvollinen kokonaislukumuotoinen Retry-After (1–86400 sekuntia) näytetään
sekunteina. Puuttuva, virheellinen, liian suuri tai HTTP-päivämäärämuotoinen arvo
antaa yleisen odotusviestin. Kiinteä viiden minuutin 429-viesti poistettiin.
Tämä ei aiheuta taustapollausta, automaattista uudelleenlähetystä tai paikallista
väitettä palvelimen tekemästä istunnon perumisesta.

## Tiedostot ja rajaus

Muokatut:

- webapp/auth_service.py
- webapp/http_access.py
- webapp/browser_auth/login.js
- webapp/static/app.js
- tests/test_webapp_http_auth.py
- tests/test_webapp_auth_rate_limits.py
- tests/test_webapp_browser.py
- docs/auth-rate-limit-checkpoint-sec-1.md

Lisätyt:

- tests/test_webapp_auth_security_sec_1_1.py
- tests/test_webapp_browser_security_sec_1_1.py
- docs/auth-rate-limit-checkpoint-sec-1-1.md

Uudessa palvelintestimoduulissa on 16 testifunktiota ja selainmoduulissa viisi,
monissa parametrisoituja tapauksia. Tämä on staattinen funktiomäärä, ei pytestin
keräysmäärä. Testit kattavat logout-kiintiön erillisyyden, aidon ja hylätyn
uloskirjautumisen, kadonneen vahvistuksen, preflightin järjestyksen ja kiintiöt,
estojen auditointimäärät, uudelleenkäynnistyksen/rinnakkaisuuden, MFA-auditoinnin,
historian hakurajan ja todellisen Retry-After-ajan sekä turvalliset selainviestit.

Kolme nykyistä regressio-odotusta päivitettiin uuden toiminnan mukaisiksi:
transport-hylkäyksen kiintiöttömyys, hylkäystapahtumien näytteistys ja kiinteän
viiden minuutin viestin poistuminen. Nykyinen invalid-HTTP-abuse-testi käyttää
nyt hyväksyttyä Originia, jotta se testaa kiintiön varaavaa CSRF-hylkäystä.
Tietoturvatarkistusten hylkäysodotuksia ei muutettu hyväksymisiksi.

Jäädytettyä domainia, WD-2/WD-2.1:tä, WorkPlan-/LabourPlan-sääntöjä tai
liiketoimintalogiikkaa ei muutettu. Skeemoja tai migraatioita ei muutettu.
Tehtävään kuulumattomat ennestään muokatut ja seuraamattomat tiedostot säilytettiin.

## Testiyritykset ja tulokset

Työhakemisto: C:\Users\eliss\Projects\carbitoy-web.

Ensin kohdennettu PowerShell-komento:

~~~powershell
.\.venv\Scripts\python.exe -B -m pytest -p tests.wd2_test_runtime -p no:cacheprovider tests/test_webapp_auth_security_sec_1_1.py tests/test_webapp_browser_security_sec_1_1.py tests/test_webapp_auth_rate_limits.py tests/test_webapp_auth_service.py tests/test_webapp_auth_repository.py tests/test_webapp_http_auth.py tests/test_webapp_http_access.py tests/test_webapp_business_csrf.py tests/test_webapp_browser.py tests/test_webapp_browser_auth.py tests/test_webapp_workshop_isolation.py tests/test_webapp_auth_business_ordering.py -q
~~~

Sen jälkeen koko regressiosarjan PowerShell-komento:

~~~powershell
.\.venv\Scripts\python.exe -B -m pytest -p tests.wd2_test_runtime -p no:cacheprovider -q
~~~

Molemmat estyivät ennen Python-prosessin luontia:

~~~text
exec_command failed: CreateProcess { message: "Rejected(\"Failed to create unified exec process: helper_unknown_error: setup refresh had errors\")" }
~~~

| Sarja | Keräysmäärä | Tulos |
| --- | --- | --- |
| SEC-1.1 kohdennetut ja suojausregressiot | Ei saatavilla | Ei käynnistynyt |
| Koko nykyinen regressiosarja | Ei saatavilla | Ei käynnistynyt |

Git-status-komento estyi samalla alustusvirheellä. Haara varmennettiin onnistuneella
komennolla git branch --show-current; tila tarkistettiin lisäksi Git-metatiedoista,
indeksistä ja lähdetiedostoista. Git-indeksi ja HEAD säilyivät muuttumattomina.

Testejä ei väitetä ajetuiksi tai läpäistyiksi. JavaScriptin erillinen jäsennysyritys
Node-työkalussa estyi sen code-generation-rajoitukseen; siitä ei saatu syntaksitulosta.
Varmennus tässä ympäristössä jäi lähdekatselmointiin ja tiedostorajauksen tarkistukseen.
Sandboxia ei ohitettu, oikeuksia ei korotettu eikä riippuvuuksia asennettu.
Nykyinen wd2_test_runtime-plugin pitää testien väliaikaiset kannat ja muut
väliaikaistiedostot projektissa, kun ajo saadaan käynnistymään. Pluginia ei muutettu.

## Jäljelle jäävät riskit ja erilliset päätökset

- SEC-1.1:n myöhemmät ajot on vahvistettu käyttäjän toimesta: 714 passed
  kohdennetusti ja 2320 passed koko regressiosarjassa, yksi tunnettu
  StarletteDeprecationWarning. Tämä vahvistus ei koske SEC-1.2-muutoksia.
- Jaettu NAT/proxy-osoite jakaa yhä asiakaskiintiön. ASGI-proxy-luottamus ja ingress-
  rajat on varmennettava tuotannossa. Raaka HTTP-asiakas voi väärentää Originin ja
  Fetch Metadata -otsakkeet, joten ne eivät korvaa IP-kiintiöitä tai MFA/CSRF-suojausta.
- Erillinen logout-kiintiö voi edelleen täyttyä. CSRF-todisteen ensimmäinen haku
  voi estyä ylläpitokiintiön täyttyessä. Suojauksia ei ohiteta uloskirjautumisen vuoksi.
- SQLite-transaktiovaraukset ja rajatut auditointiluvut säilyvät. Näytteistyksen
  kuormitusominaisuuksia ei ole tässä ympäristössä mitattu. 4096 tunnisteen rajan
  ylittävä muu auditointiliikenne voi lisätä näytteiden määrää.
- Näytteistys ei tuota täydellistä tapahtumakohtaista estopyyntöhistoriaa tai
  tarkkoja estomääriä. Jos tarvitaan pysyvät aggregaattimäärät, uusi
  tietokantaindeksi tai erillinen ingress-lokitus, niiden skeema-, säilytys- ja
  operointimuutokset vaativat erillisen hyväksynnän. Niitä ei toteutettu.
- Nykyiset korjaukset eivät vaatineet skeemamuutoslupaa. SEC-2:ta tai WD-3:a ei
  aloitettu; stage-, commit-, push-, merge- tai deploy-toimia ei tehty.

## Käyttäjän vahvistama jatkovarmennus ja testikorjaus

Käyttäjän ajama ensimmäinen kohdennettu SEC-1.1-sarja: 713 passed, 1 failed.
Epäonnistui test_logout_survives_strict_and_session_csrf_limit_exhaustion.
complete()-valmistelu teki jo yhden onnistuneen HTTP-loginin ja yhden HTTP-MFA:n.
Testi yritti sen jälkeen viittä lisävarausta viiden kokonaisrajaan; jäljellä oli neljä.

Korjattiin vain tämä testifunktio. Se tarkistaa valmistelun laskurit, täyttää
jäljellä olevan kiintiön ja vaatii täsmälliset 409/403-vastaukset ennen 429:ää.
Se todentaa myös session/CSRF-rajan, Retry-Afterin ja estojen laskurimuuttumattomuuden,
logoutin 204:n, cookien poistumisen, kyseisen istunnon revoked_at-arvon ja uuden
revokaatiotapahtuman. Tuotantokoodia tai muita testejä ei muutettu tässä korjauksessa.

Käyttäjän myöhemmin vahvistamat tulokset:

| Sarja | Tulos |
| --- | --- |
| SEC-1.1 kohdennettu | 714 passed |
| Koko regressiosarja | 2320 passed |

Yksi tunnettu StarletteDeprecationWarning. Ajoja ei tehty tässä agenttiympäristössä.
Riippumattoman auditoinnin käyttäjän ilmoittama arvio: PASS WITH RESERVATIONS,
ei P0/P1-löydöksiä. Tulos koskee SEC-1.1:tä, ei myöhempää SEC-1.2-toteutusta.

SEC-1.2:n rajatut muutokset kirjataan erikseen
[SEC-1.2-checkpointiin](auth-rate-limit-checkpoint-sec-1-2.md).
