# Tiedusteluluonnoksen käsitallennus selaimessa

Yhden korjaamon työntekijä voi tallentaa keskeneräisen WORK_INQUIRY-tiedustelun
nykyisellä [DRAFT-API:lla](persistent-draft-http-checkpoint-4b-3a.md).
OWNER ja SERVICE_MANAGER voivat listata, avata, muokata ja nykyisellä API:lla
hylätä oman korjaamonsa yhteisiä luonnoksia. Kirjautuminen, MFA ja aktiivinen
korjaamojäsenyys vaaditaan. [Yhteiskäytön sopimus](shared-workshop-drafts.md)
erottaa yhteisen sisällön henkilökohtaisista uusintapyynnöistä.

## Kokeilu ja käyttö

API pysyy oletuksena suljettuna. Käytä vain eristettyä testiympäristöä ja sen
tilapäisiä auth/business-kantoja, henkilökohtaisia testitunnuksia ja testiavainrengasta.
Nimenomainen aktivointi on nykyisen HTTP-ohjeen `CARBIT_DRAFT_API_ENABLED=1`
sekä saman ohjeen kelvollinen palvelinpuolen HMAC-konfiguraatio. Avaimia ei
anneta selaimelle. Tämä toiminto ei muuta oletusasetusta tai tuotantoasetuksia.
Suljettu tai tavoittamaton palvelu näyttää virheen ja säilyttää lomakkeen tekstin.

1. Avaa **Uusi tiedustelu**, kirjoita rekisterinumeroa ja/tai asiakkaan kuvausta.
   Kentät saavat olla vielä keskeneräisiä. Valitse **Tallenna luonnos**.
2. Odota tallennuksen vahvistusta. Vain käsin tallennettu sisältö säilyy
   palvelimella; myöhemmät muokkaukset jäävät tallentamatta seuraavaan painallukseen.
3. Sulje lomake ja valitse **Näytä / päivitä luonnokset**. Valitse korjaamon luonnos
   jatkaaksesi. Tämä toimii myös sivun lataamisen tai uuden kirjautumisen jälkeen.
4. Muokkaa ja tallenna samalla painikkeella. Tallennus käyttää luettua revisionia.
   Toisen luonnoksen tai palvelinversion avaaminen pyytää luvan korvata
   tallentamattoman tai epäselvän lomakkeen sisällön. Peruminen säilyttää sen.

Luettelo näyttää vain WORK_INQUIRYt palvelimen enintään sadan aktiivisen luonnoksen
joukosta. Myös LABOUR_ENTRYt lasketaan API-rajaan. Rajan täyttyessä käyttöliittymä
ilmoittaa, ettei luettelo välttämättä sisällä kaikkia tiedusteluluonnoksia.
Tässä ei lisätä palvelimen suodatusta tai sivutusta.

## Tallennus, virheet ja käyttäjäkohtainen selainmuisti

- Seitsemän päivän sisältösäilytys alkaa viimeisestä hyväksytystä sisältömuutoksesta.
  Lukeminen, muuttumaton tallennus ja uusintakuitti eivät jatka aikaa.
  Lomake näyttää palvelimen ilmoittaman säilytysajan päättymisen.
- Muutokseksi lasketaan vain ero avauksen tai viimeisen tallennuksen jälkeen näytettyyn
  tekstiin. Selain voi näyttää rivinvaihdot yhtenäistettyinä; muuttamaton kenttä
  tallennetaan silti palvelimen alkuperäisenä.
- Jos palvelin ei vastaa luonnoksen avaukseen 15 sekunnissa, avaus keskeytetään.
  Lomake vapautuu, aiempi teksti säilyy eikä myöhässä saapuva vastaus korvaa sitä.
  Avaa luonnos tarvittaessa uudelleen listasta tai palvelinversion painikkeesta.
- Revisionkonflikti ei ylikirjoita palvelimen versiota tai lomakkeen tekstiä.
  Avaa palvelimen versio nimenomaisesti ennen jatkamista; automaattista mergeä ei ole.
- Katkennut vastaus voi tarkoittaa jo hyväksyttyä tallennusta. **Uusi sama tallennus**
  lähettää saman UUID:n, revision ja sisällön. Uusinta koskee alkuperäistä pyyntöä,
  vaikka lomakkeelle olisi jo kirjoitettu lisää. Myöhempi teksti säilyy lomakkeella.
  Kuittia seuraava luku vahvistaa nykyisen version; historiallinen kuitti ei yksin
  ole nykyisen sisällön todiste. Jos sisältö on välillä muuttunut, tarvitaan käyttäjän
  valitsema palvelinversion avaus. Ei automaattisia kirjoitusten uusintoja.
- Epäselvän pyynnön käsinuusinta lopetetaan viimeistään vuorokauden kohdalla
  (palvelimen metadatasäilytyksen vähimmäisaika). Tarkista silloin korjaamon lista.
  Sivun reload kadottaa muistissa olevan epäselvän pyynnön; tarkista lista ennen
  uuden luonnoksen luomista. Käyttöliittymä ei lupaa sivujen yli jatkuvaa uusintaa.
- Vanhentunutta tai hylättyä luonnosta ei luoda automaattisesti uudelleen.
  Verkko-, palvelu- tai tallennusvirhe ei tyhjennä avointa lomaketta.
- Istunnon lukitus peittää tiedot. Vahvistettu uloskirjautuminen ja käyttäjänvaihto
  tyhjentävät edellisen käyttäjän lomakkeen, listan ja uusintapyynnön muistista.
  Sisältöä ei tallenneta localStorageen, sessionStorageen tai muuhun pysyvään
  selainvarastoon. Tallentamattomat muutokset menetetään reloadissa.
  Uusi saman korjaamon käyttäjä hakee yhteiset tallennetut luonnokset uudelleen;
  edellisen käyttäjän tallentamaton teksti tai alkuperäinen uusintapyyntö ei siirry.
- Luojan jäsenyyden päättyminen ei piilota luonnosta muulta aktiiviselta
  henkilöstöltä. Poistuneen käyttäjän pääsy estetään ja nykyinen expiry säilyy.

## Luonnoksen hylkäys (DRAFT-A, 20.10.2026)

Avatun palvelinluonnoksen voi hylätä painikkeella **Hylkää luonnos**. Kuka tahansa
korjaamon kirjoitusoikeudellinen henkilöstön jäsen voi hylätä yhteisen luonnoksen.
- **Vahvistus.** Vahvistusikkuna kertoo, että kyse on korjaamon yhteisestä
  luonnoksesta, että hylkäystä ei voi perua ja että tallentamattomat muutokset
  menetetään. Peruutus ei lähetä mitään.
- **Pyyntö.** Hylkäys lähettää palvelimelle pyynnön
  `POST /api/drafts/{draft_id}/discard`, jossa on uusi pyyntötunniste ja avatun
  version `expected_revision`.
- **Onnistuminen.** Editori tyhjenee, rivi poistuu luettelosta, eikä luonnosta
  haeta uudelleen, koska päättynyt luonnos ei enää avaudu. Ennen hylkäystä
  alkanut luettelohaku mitätöidään, joten sen myöhäinen vastaus ei palauta riviä.
- **Ristiriita tai puuttuva luonnos.** Kun palvelin vastaa 409 (uudempi versio),
  404 (luonnos jo päättynyt) tai 403, mitään ei hylätä. Lomakkeen sisältö säilyy,
  ja palvelimen version voi avata ennen uutta päätöstä.
- **Epäselvä tulos.** Kun yhteys katkeaa tai palvelin ei vastaa 15 sekunnissa,
  editori lukittuu. **Uusi sama hylkäys** lähettää täsmälleen alkuperäisen
  pyynnön samalla pyyntötunnisteella. Jos palvelin oli jo hylännyt luonnoksen,
  uusinta palauttaa saman kuitin eikä tee toista hylkäystä.
  - Uusinta säilyy saman käyttäjän uudelleenkirjautumisen yli ja enintään
    vuorokauden.
  - Käyttäjänvaihto tyhjentää uusinnan.
- **Kollegan auki oleva editori.** Kollegan editori saa hylkäyksen jälkeen
  seuraavasta tallennuksesta vastauksen 404 tai 409. Kollegan teksti säilyy, eikä
  uutta luonnosta synny.
- **Uloskirjautuminen.** Uloskirjautumisen tai käyttäjänvaihdon yhteydessä tehtävä
  paikallinen tyhjennys ei koskaan hylkää luonnosta palvelimelta.

## Luoja ja viimeisin muokkaaja (DRAFT-B, 20.10.2026)

Luettelorivi ja avattu luonnos näyttävät tekstin "Luonut: X · Viimeksi
muokannut: Y". Omalla kohdalla nimen perään tulee "(sinä)".

**Muokkaaja** on viimeisimmän sisältöä muuttaneen tallennuksen tekijä:
- Muuttumaton tallennus tai kuitin uusinta ei vaihda muokkaajaa.
- Luonnoksessa, jota ei ole muokattu (revisio 1), muokkaaja on luoja.
- Jos myöhemmälle revisiolle ei löydy sisältömuutoksen kuittia (esim. ennen
  versiota 4 tallennettu aineisto), näytetään "ei tiedossa". Muokkaajaa ei
  päätellä.

**Nimet** ovat lukuhetken nykyisiä näyttönimiä:
- Palvelin hakee ne auth-kannasta (`AuthService.display_names`) vasta, kun
  valtuutettu luonnosluku on valmis.
- Haku tehdään identiteetillä `auth_store_id` + `user_id`. Toisen auth-kannan
  samannäköinen tunniste ei saa nimeä.
- Myös suljetun tai jäsenyytensä menettäneen toimijan nimi näytetään, jos tili on
  olemassa. Tämä ei anna toimijalle pääsyä luonnokseen.
- Puuttuva nimi tai nimihaun tallennusvirhe näkyy muodossa "Nimi ei saatavilla",
  eikä se estä luonnoksen avaamista eikä muuta 401- tai 403-vastausta.
- Nimet näytetään tekstinä, eivät HTML:nä.
- Selaimelle ei lähetetä käyttäjä- tai kantatunnisteita. "Minä" vaatii saman
  auth-kannan ja saman tunnisteen.
- Avoin näkymä ei päivity automaattisesti, jos nimi vaihtuu.

## Rajaus ja testit

Nykyinen uuden tiedustelun **Tallenna tiedustelu** säilyy ennallaan. Palvelimelle
tallennetussa/avatussa luonnoksessa lopullinen tallennuspainike on estetty:
luonnoksen muunnosta tai lähettämistä ei ole tässä toteutettu. Aloita erillinen
uusi tiedustelu käyttääksesi nykyistä polkua; luonnos säilyy palvelimella.

Ei autosavea, LABOUR_ENTRY-editoria, historiallisia nimitilannekuvia, muunnosta, lopullista työajan
lähetystä, hyväksyntää, integraatioita, WD-3:a, SEC-2B:tä tai uusia monen korjaamon
ominaisuuksia. Backendin käyttöoikeusraja on nyt korjaamon yhteinen; sisältö-, revision- ja
retention-sopimus säilyy. Luoja ei vaihdu, todellisen sisältömuutoksen tekijä
kirjataan, eikä muuttumaton tallennus tai kuittiuusinta vaihda viimeistä muokkaajaa. Aiemmat README-, käyttöönottovarmennus- ja SEC-varaukset säilyvät;
tämä ei vahvista DRAFTin alkuperäistä hyväksyntää tai tuotantovalmiutta.

`tests/test_webapp_browser_drafts.py` käyttää oikeaa Chromiumia ja sovelluksen
lifespania HTTPS:llä sekä tilapäisiä SQLite-kantoja. Virheinjektio katkaisee vain
yksittäisen vastauksen, pidättää avausvastauksen hallitulla selainkellolla tai
epäonnistuttaa oikean tallennustransaktion.
Nykyisten selain-/auth-polkujen ja DRAFT-HTTP:n kohdennetut regressiot säilyvät
erillisinä moduuleina. Testien ajotulokset kirjataan ulkoiseen todisteraporttiin.
