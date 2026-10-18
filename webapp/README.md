# Carbitin paikallinen työjono

FastAPI palvelee HTML/CSS/JavaScript-käyttöliittymän ja JSON-rajapinnan
samasta HTTPS-palvelimesta. CP4A lisää selaimen kirjautumisen:
[checkpoint ja turvallisuusrajat](../docs/browser-authentication-checkpoint-4a.md).
Vain `/login`, `/login.js` ja `/login.css` ovat julkisia. Sovellus, koko `/static`
ja business-API vaativat henkilökohtaisen MFA-istunnon. Tämä ei ole tuotantohyväksyntä.
CP4B-1 vaatii lisäksi eksplisiittisen korjaamojäsenyyden ja palvelimen
ratkaiseman aktiivisen korjaamon. [Migraatio, offline-jäsenyydet ja turvallisuusrajat](../docs/workshop-foundation-checkpoint-4b-1.md).
Skeeman alustus ei myönnä jäsenyyksiä tai kohdista vanhoja töitä automaattisesti.

## Käynnistys PowerShellissä

```powershell
Set-Location C:\Users\eliss\Projects\carbitoy-web
.\.venv\Scripts\python.exe -m uvicorn webapp.main:app --host 127.0.0.1 --port 8000 --no-proxy-headers --ssl-certfile "C:\TLS\carbit-cert.pem" --ssl-keyfile "C:\TLS\carbit-key.pem"
```

Komennon TLS-polut ovat esimerkkejä: ylläpitäjä toimittaa oikeat, selaimen
luottamat sertifikaatit. Ennen käynnistystä vaaditaan kaikki kuusi asetusta:
`CARBIT_AUTH_DATABASE`, `CARBIT_ALLOWED_HOSTS`, `CARBIT_EXTERNAL_ORIGINS`,
`CARBIT_TOTP_KEYS`, `CARBIT_CSRF_ACTIVE_KEY_ID` ja `CARBIT_CSRF_KEYS`.
Katso [asetusten sopimus](../docs/http-authentication-checkpoint-1.md).
Älä käytä testisertifikaatteja tai testien evästeinjektiota oikeassa käytössä.

Avaa määritetyn HTTPS-originin `/login`, esimerkiksi
`https://127.0.0.1:8000/login`, jos se on hyväksytty origin ja sertifikaatti
kelpaa kyseiselle osoitteelle. Paljas `/` palauttaa anonyymille edelleen 401.
Pysäytä palvelin Ctrl+C:llä. Virtuaaliympäristön aktivointia ei tarvita.

Työntekijällä on oltava ylläpitäjän luotetusti provisionoima henkilökohtainen
tunnus ja valmiiksi rekisteröity MFA. Käyttöönotto tapahtuu nykyisillä sisäisillä
palveluilla hallitussa offline-menettelyssä, ei tässä selaimessa. CP4A ei lisää
käyttäjähallinnan CLI:tä, enrollment- tai palautusrajapintaa. Kirjautuminen
edellyttää salasanaa ja tunnistussovelluksen koodia tai kertakäyttöistä
palautuskoodia. OPENCLAW ei voi kirjautua ihmisenä.

Uudessa ympäristössä:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-web.txt
```

`requirements-web.txt` käyttää nyt Windows AMD64 / CPython 3.13 -kehityksen
hash-lukitusta. Tuotannon ajonaikaiset riippuvuudet, valinnaiset AI-adapterit
ja testityökalut on erotettu. Ubuntu 24.04 / Python 3.12 tarvitsee oman,
vielä varmentamattoman lukituksen. Katso [Python-ympäristön menettely](../docs/python-environment.md)
puhtaasta testauksesta, Ubuntu-varmennuksesta ja turvallisista päivityksistä.

## Käyttö

Luo tiedustelu rekisterinumerolla ja asiakkaan alkuperäisellä kuvauksella.
Työaikaa ei tarvita luontiin. Avaa tiedustelu listasta ja kirjaa
Autodatasta tarkistettu aika, työn kuvaus ja mahdollinen viite.
Kirjaajan pysyvä UUID ja historiallinen nimisnapshot tulevat palvelimen
tarkistamasta istunnosta, eivät lomakkeesta. OWNER ja SERVICE_MANAGER voivat
kirjoittaa; IT_ADMIN käyttää vain lukunäkymää.
Yhden ja molempien puolien ohjeajat syötetään sellaisinaan. Sovellus ei hae
Autodata-tietoja eikä päättele aikaa tai diagnoosia.

Pythonin `LabourTime` käyttää `MANUAL_AUTODATA`-lähdettä. Nykyinen
`calculate_labour_cost()` laskee työosuuden ja `split_inclusive_vat()`
erittelee ALV:n. Tuntihinta 90 €/h sisältää ALV:n 25,5 %.
Laskelma ei ole hyväksytty lopullinen tarjous.

## Web 4D: työaikojen tarkistuksen esikatselu

Käynnistä yllä oleva paikallinen palvelin ja avaa
määritetyn HTTPS-originin `/static/labour-review-demo.html` kirjautuneena.
Näkymä on erillinen **DEMO / ESIKATSELU**, jossa kaikki työajat,
Autodata-operaatioiden viitteet, henkilöt ja päätökset ovat keksittyjä.
Nykyinen työjono ja sen API-reitit toimivat ennallaan.

Sivu näyttää erilliset työrivit, muuttumattomat alkuperäiset ajat ja summan,
monityösuunnitelman tarkistusvaatimuksen sekä esimerkkipäätösten historian.
Esimerkkitilanteista voi valita myös puuttuvan hyväksynnän, puuttuvan ajan,
muuttuneen sääntöversion tai yhden työn suunnitelman. Puuttuvaa aikaa ei
korvata arviolla. Aiemmat ja vanhentuneet päätökset erotetaan nykyisestä.

Hyväksyntävaihtoehdon valinta ja perusteltu ajanmuutos (myös 0,00 h)
muuttavat vain esikatselua. Selain laskee hintavaikutuksen desimaalitekstistä
kokonaislukuaritmetiikalla: 90 €/h sisältää ALV:n 25,5 %, kokonaishinta
pyöristetään senteiksi, veroton hinta pyöristetään ja ALV saadaan erotuksena.
Tämä ei ole auktoritatiivinen hinnoittelurajapinta. Varaosat eivät kuulu
työveloituksen esikatseluun.

Sivu ei tee API-pyyntöjä, lue oikeaa päätöshistoriaa, kirjoita tietokantaan
tai selaimen tallennustilaan eikä muodosta tarjousta. Näytä esikatselu
-painike ei tallenna hyväksyntää. Sivun uudelleenlataus palauttaa esimerkin.
Keksityt historiassa näkyvät hyväksyjät eivät ole kirjautuneita käyttäjiä.

Oikeaan dataan kytkentä odottaa työaikasuunnitelmien HTTP- ja selainrajapintoja.
Käyttäjätunnistus ja sisäinen identiteettisilta ovat olemassa. Hyväksyjä tulee palvelimen luotetusta
kontekstista. Hyväksyntä käsitellään `LabourPlanService`-palvelussa, ja
lopullinen hinta lasketaan `ApprovedLabourPricingService`-palvelussa
`require_approved_final_hours()`-tarkistuksen jälkeen. Palvelimen on
tarkistettava versio, aktiivinen revisio, sisältötunniste ja sääntöversio;
selaimen tilamerkintä tai laskema hinta ei todista hyväksyntää.
Hintasnapshotin myöhempi tarjouskäyttö edellyttää uutta voimassaolon
tarkistusta ja atomista version tarkistusta tallennuksen yhteydessä.

## Tallennus ja rajat

SQLite-tietokanta syntyy polkuun `var/carbit.db`. `CARBIT_DATABASE` voi
osoittaa vaihtoehtoiseen paikalliseen tietokantaan. Tietokannat, lokit ja
ympäristötiedostot ohitetaan Gitissä.

`work_inquiries` säilyttää tiedustelun, `KESKEN`-tilan, version ja viimeisimmän
hinnoittelusnapshotin. `work_events` säilyttää ajan UTC-muodossa, tyypin,
tekijän ja tapahtuman tiedot; työaikatapahtumassa on koko laskentasnapshot.
Muutos ja tapahtuma tallentuvat samassa transaktiossa. Vanha versio saa
HTTP 409 -vastauksen. Avaa tällöin työ uudelleen listasta saadaksesi uusimman
version; lomakkeen tallentamattomat muutokset korvautuvat.

Työ ja sen historia luetaan samassa SQLite-lukutransaktiossa. Uusissa
tapahtumissa `work_version` kertoo muutoksen jälkeisen version. Sovelluksen
käynnistyessä vanhaan skeemaan lisätään nullable-sarake ilman tietojen
poistamista: vanhojen tapahtumien versiot jäävät `NULL`-arvoiksi.

Luontirajapinta vaatii `idempotency_key`-UUID:n. Käyttöliittymä luo avaimen
uutta tiedustelua avattaessa ja säilyttää sen uudelleenlähetyksessä.
`inquiry_creation_requests` tallentaa avaimen, validoidun pyynnön sisällön,
työn tunnisteen ja alkuperäisen luontivastauksen atomisesti. Saman avaimen
ja sisällön uusinta palauttaa alkuperäisen vastauksen myös palvelimen
uudelleenkäynnistyksen jälkeen. Eri sisältö samalla avaimella palauttaa
409:n. Uusi tiedustelulomake saa uuden avaimen, joten tarkoitukselliset
samansisältöiset tiedustelut ovat sallittuja. Uusinnan jälkeen työn
avausrajapinta palauttaa sen nykyiset tiedot.

Selaimen lomake sitoo työn tunnisteen ja version näkyviin tietoihin.
Vanhentunut avaus- tai tallennusvastaus ei vaihda valintaa. Päällekkäiset
tallennukset estetään; tallennuksen aikana toisen työn voi avata ja sen
tallennus vapautuu meneillään olevan pyynnön valmistuttua.

Uudet tekijät ovat palvelimen UUID-attribuutiota; vanhat itse ilmoitetut
tapahtumat pysyvät historiallisina ja erottuvat käyttöliittymässä. Kirjautuminen,
MFA, roolit ja business-CSRF ovat käytössä. Muita työn tiloja tai ulkoisia
integraatioita ei ole. autokorjaamo.fi on olemassa oleva CRM; CP4B-integraatio
ei kuulu tähän vaiheeseen. Älä käytä kehitystestauksessa oikeita asiakastietoja.

Istunnon päättyessä business-näkymä piilotetaan ja tallentamattomat luonnokset
säilyvät vain avoimen sivun muistissa. Tarkista tila ja kirjaudu uudelleen:
luonnokset palautuvat vain samalle UUID:lle. Käyttäjän vaihtuessa tai vahvistetun
uloskirjautumisen jälkeen ne poistetaan. Sivun lataaminen uudelleen menettää
luonnokset. Epäselviä tallennuksia ei lähetetä automaattisesti uudestaan.

## Testit

CP4B-2A:n [luonnosskeema ja migraatiorajat](../docs/persistent-draft-schema-checkpoint-4b-2a.md)
lisäävät tyhjät luonnostaulut alustuksessa. Pysyvien luonnosten CRUD, HTTP ja
selain-autosave eivät vielä ole käytössä.

CP4B-2B:n [sisäinen luonnospalvelu](../docs/persistent-draft-service-checkpoint-4b-2b.md)
lisää omistajakohtaiset repository-operaatiot ja lisämigraation kuiteille sekä
hylkäyspoistolle. [Kovennus](../docs/persistent-draft-hardening-checkpoint-4b-2b.md)
lisää sisäisen siivouksen, HMAC-avainkierron ja päättyneiden luonnosten
metadata-toiston. Tekninen säilytys on kehityspäätöksenä enintään 90 päivää
luonnoksen päättymisestä; yrittäjän lopullinen vahvistus on avoin.
HTTP/UI/autosave eivät ole käytössä.

CP4B-3A:n [luonnosten HTTP-sopimus](../docs/persistent-draft-http-checkpoint-4b-3a.md)
lisää owner-only create/list/read/save/discard-reitit. API on oletuksena pois
käytöstä; `CARBIT_DRAFT_API_ENABLED=1` vaatii erikseen konfiguroidun HMAC-keyringin.
Selain-autosavea, muunnosta ja labour submissionia ei toteuteta.

```powershell
.\.venv\Scripts\python.exe -m pytest -q
```

Web-testit käyttävät pytestin erillisiä väliaikaisia tietokantoja.
Testit kattavat luonnin ilman aikaa, domainhinnoittelun, versionristiriidan,
transaktion palautuksen, pysyvyyden ja aiempien laskentatietojen säilymisen.
Regressiotestit kattavat myös idempotenssin, samanaikaiset pyynnöt,
skeemamigraation sekä epäkelvon työajan hylkäyksen ilman muutoksia.

Selainregressiot käyttävät paikallisia tilapäisiä palvelimia ja Chromiumia.
Asenna testiselain kerran virtuaaliympäristön riippuvuuksien jälkeen:

```powershell
.\.venv\Scripts\python.exe -m playwright install chromium
```

`tests/test_webapp_browser.py` tarkistaa myöhäiset vastaukset, lähetyslukot,
katkenneen luontivastauksen uusinnan, suomenkieliset virheet ja hintojen
fi-FI-muotoilun sekä desktop- ja mobiilikulun. Kuvakaappaukset syntyvät
testin väliaikaishakemistoon.
