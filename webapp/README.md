# Carbitin paikallinen työjono

FastAPI palvelee HTML/CSS/JavaScript-käyttöliittymän ja JSON-rajapinnan
samasta palvelimesta. Käyttö on rajattu paikalliseen osoitteeseen.

## Käynnistys PowerShellissä

```powershell
Set-Location C:\Users\eliss\Projects\carbitoy-web
.\.venv\Scripts\python.exe -m uvicorn webapp.main:app --host 127.0.0.1 --port 8000
```

Avaa selaimessa http://127.0.0.1:8000. Pysäytä palvelin Ctrl+C:llä.
Virtuaaliympäristön aktivointia ei tarvita.

Uudessa ympäristössä:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-web.txt
```

## Käyttö

Luo tiedustelu rekisterinumerolla, asiakkaan alkuperäisellä kuvauksella ja
kirjaajalla. Työaikaa ei tarvita luontiin. Avaa tiedustelu listasta ja kirjaa
Autodatasta tarkistettu aika, työn kuvaus, kirjaaja ja mahdollinen viite.
Yhden ja molempien puolien ohjeajat syötetään sellaisinaan. Sovellus ei hae
Autodata-tietoja eikä päättele aikaa tai diagnoosia.

Pythonin `LabourTime` käyttää `MANUAL_AUTODATA`-lähdettä. Nykyinen
`calculate_labour_cost()` laskee työosuuden ja `split_inclusive_vat()`
erittelee ALV:n. Tuntihinta 90 €/h sisältää ALV:n 25,5 %.
Laskelma ei ole hyväksytty lopullinen tarjous.

## Web 4D: työaikojen tarkistuksen esikatselu

Käynnistä yllä oleva paikallinen palvelin ja avaa
http://127.0.0.1:8000/static/labour-review-demo.html.
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

Oikeaan dataan kytkentä odottaa käyttäjätunnistusta ja valtuutettuja sisäisiä
luku- ja kirjoitusrajapintoja. Hyväksyjä tulee palvelimen luotetusta
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

Tekijä on itse ilmoitettu nimi. Tässä versiossa ei ole kirjautumista,
käyttöoikeuksia, muita työn tiloja tai ulkoisia integraatioita. Julkinen
käyttö edellyttää erillistä suojaus- ja tallennusratkaisua. Älä käytä
kehitystestauksessa oikeita asiakastietoja.

## Testit

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
