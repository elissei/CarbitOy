# Tiedusteluluonnoksen käsitallennus selaimessa

Yhden korjaamon työntekijä voi tallentaa keskeneräisen WORK_INQUIRY-tiedustelun
nykyisellä [DRAFT-API:lla](persistent-draft-http-checkpoint-4b-3a.md).
OWNER ja SERVICE_MANAGER käsittelevät vain omia luonnoksiaan nykyisen
kirjautumisen, MFA:n ja korjaamojäsenyyden perusteella.

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
3. Sulje lomake ja valitse **Näytä / päivitä luonnokset**. Valitse oma luonnos
   jatkaaksesi. Tämä toimii myös sivun lataamisen tai uuden kirjautumisen jälkeen.
4. Muokkaa ja tallenna samalla painikkeella. Tallennus käyttää luettua revisionia.
   Toisen luonnoksen tai palvelinversion avaaminen pyytää luvan korvata
   tallentamattoman tai epäselvän lomakkeen sisällön. Peruminen säilyttää sen.

Luettelo näyttää vain WORK_INQUIRYt palvelimen enintään sadan aktiivisen luonnoksen
joukosta. Myös LABOUR_ENTRYt lasketaan API-rajaan. Rajan täyttyessä käyttöliittymä
ilmoittaa, ettei luettelo välttämättä sisällä kaikkia tiedusteluluonnoksia.
Tässä ei lisätä palvelimen suodatusta tai sivutusta.

## Tallennus, virheet ja yksityisyys

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
  (palvelimen metadatasäilytyksen vähimmäisaika). Tarkista silloin oma lista.
  Sivun reload kadottaa muistissa olevan epäselvän pyynnön; tarkista lista ennen
  uuden luonnoksen luomista. Käyttöliittymä ei lupaa sivujen yli jatkuvaa uusintaa.
- Vanhentunutta tai hylättyä luonnosta ei luoda automaattisesti uudelleen.
  Verkko-, palvelu- tai tallennusvirhe ei tyhjennä avointa lomaketta.
- Istunnon lukitus peittää tiedot. Vahvistettu uloskirjautuminen ja käyttäjänvaihto
  tyhjentävät edellisen käyttäjän lomakkeen, listan ja uusintapyynnön muistista.
  Sisältöä ei tallenneta localStorageen, sessionStorageen tai muuhun pysyvään
  selainvarastoon. Tallentamattomat muutokset menetetään reloadissa.

## Rajaus ja testit

Nykyinen uuden tiedustelun **Tallenna tiedustelu** säilyy ennallaan. Palvelimelle
tallennetussa/avatussa luonnoksessa lopullinen tallennuspainike on estetty:
luonnoksen muunnosta tai lähettämistä ei ole tässä toteutettu. Aloita erillinen
uusi tiedustelu käyttääksesi nykyistä polkua; luonnos säilyy palvelimella.

Ei autosavea, LABOUR_ENTRY-editoria, discard-UI:ta, muunnosta, lopullista työajan
lähetystä, hyväksyntää, integraatioita, WD-3:a, SEC-2B:tä tai uusia monen korjaamon
ominaisuuksia. Backendin sisältö-, käyttöoikeus-, revision- ja retention-sopimus
säilyy. Aiemmat README-, käyttöönottovarmennus- ja SEC-varaukset säilyvät;
tämä ei vahvista DRAFTin alkuperäistä hyväksyntää tai tuotantovalmiutta.

`tests/test_webapp_browser_drafts.py` käyttää oikeaa Chromiumia ja sovelluksen
lifespania HTTPS:llä sekä tilapäisiä SQLite-kantoja. Virheinjektio katkaisee vain
yksittäisen vastauksen, pidättää avausvastauksen hallitulla selainkellolla tai
epäonnistuttaa oikean tallennustransaktion.
Nykyisten selain-/auth-polkujen ja DRAFT-HTTP:n kohdennetut regressiot säilyvät
erillisinä moduuleina. Testien ajotulokset kirjataan ulkoiseen todisteraporttiin.
