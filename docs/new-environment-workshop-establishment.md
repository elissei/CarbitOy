# Uuden ympäristön organisaation ja korjaamon perustaminen

Tämä offline-operaattoriohje kuvaa, miten täysin uuteen, jo alustettuun
liiketoimintakantaan perustetaan yksi autokorjaamo-organisaatio ja sen ainoa
ACTIVE-korjaamo. Organisaatio tarkoittaa Carbit Oy:n autokorjaamotoimintaa
([CP4B-1](workshop-foundation-checkpoint-4b-1.md)). Toimitus koskee yhtä
korjaamoa: toista toimipistettä, PLANNED-riviä tai monen korjaamon toimintoa ei
lisätä. Tämä ei ole pilotti- tai tuotantohyväksyntä.

## Vastuurajat

`python -m webapp.workshop_admin establish-initial-workshop` kirjoittaa yhdessä
transaktiossa vain organisaatiorivin, korjaamorivin ja yhden
`INITIAL_WORKSHOP_ESTABLISHED`-auditointitapahtuman. Komento ei avaa
auth-kantaa, luo käyttäjätiliä, jäsenyyttä, korjaamovalintaa tai työtä, siirrä
vanhaa dataa, aja migraatioita eikä muuta skeemaa.

- Auth-tilit ja MFA luodaan nykyisillä sisäisillä palveluilla
  ([CP4A](browser-authentication-checkpoint-4a.md)).
- Jäsenyys myönnetään nykyisellä `grant-membership`-komennolla, ja aktiivinen
  korjaamokonteksti muodostuu yhdestä jäsenyydestä tai `select-workshop`-valinnasta
  ([CP4B-1](workshop-foundation-checkpoint-4b-1.md)).
- `migrate-approved-development` säilyy vain tämän repositoryn hyväksytyn
  `var/carbit.db`-kehityskannan siirtona. Uusi komento ei käytä eikä löysennä sen
  polku-, preimage-, backup- tai 3/8-rajoja.
- Sovelluksen normaali käynnistys ja alustus eivät edelleenkään luo organisaatiota
  tai korjaamoa.
- `--operator-reference` on vapaatekstinen auditointiviite, ei ohjelmallisesti
  todennettu hyväksyntä. Luottamus perustuu käyttöjärjestelmän pääsynhallintaan ja
  hyväksyttyyn offline-menettelyyn.

## Esiehdot

1. **Hyväksyntäkirjaus:** hyväksyjä, päivämäärä, organisaation ja korjaamon nimet
   sekä kaksi etukäteen muodostettua UUID:ta, esimerkiksi
   `python -c "import uuid; print(uuid.uuid4())"`. Tunnisteet ja nimet tulevat
   vain tästä kirjauksesta.
2. **Kohdekanta valitaan luotetusti:** nimenomainen absoluuttinen polku uuteen
   liiketoimintakantaan, joka on eri tiedosto kuin auth-kanta. Polku- ja
   tunnistetarkistukset eivät todista, ettei kohteessa olisi esimerkiksi
   kehityskannan kopiota. Oikean kohteen valinta on operaattorin vastuulla.
3. **Kanta on alustettu nykyisellä versiolla:** normaali käynnistys alustaa sen.
   Pysäytä palvelin ennen perustamista. Tuetut ledgerit ovat täsmälleen
   `webapp_schema_migrations = {1, 2}`, `workshop_schema_migrations = {1}` ja
   `draft_schema_migrations = {1, 2, 3, 4}`, ja lisäksi tarvitaan perustamisen
   käyttämät taulut sekä auditointitaulun muuttumattomuustriggerit. Muu,
   alustamaton tai keskeneräinen skeema hylätään ennen kirjoituksia ilman
   migraatiota.
4. **Ensimmäinen perustaminen tehdään tyhjään ympäristöön:** ei töitä,
   kohdistuksia, retry-rivejä, principal-, jäsenyys- tai valintarivejä,
   organisaatioita, korjaamoja eikä aiempia korjaamoauditointitapahtumia. Vanha data
   kuuluu erikseen tarkistettuun kohdistus- ja siirtomenettelyyn.
5. **Perustaminen tehdään ennen henkilöiden provisionointia.**

## Suoritus

```powershell
.\.venv\Scripts\python.exe -m webapp.workshop_admin establish-initial-workshop --database "C:\suojattu\works.db" --organization-id "<hyväksytty UUID>" --organization-name "<hyväksytty nimi>" --workshop-id "<hyväksytty UUID>" --workshop-name "<hyväksytty nimi>" --operator-reference "<hyväksyjä, päivä ja päätöksen viite>"
```

Syötteet tarkistetaan ennen kannan avaamista:

- polku on absoluuttinen ja olemassa oleva tiedosto
- UUID:t ovat kelvollisia ja keskenään erisuuret
- nimet ovat 1–200 merkkiä (reunojen välilyönnit poistetaan)
- viite on 1–500 merkkiä, eikä se sisällä salasanaa, tokenia tai MFA-salaisuutta

Kanta avataan tilassa, joka ei luo puuttuvaa tiedostoa.

Uuden komennon omat suunnittelurajat: se hylkää hyväksytyn kehityskannan polun
(myös symlinkin tai kovan linkin kautta) ja kehityssiirron vakiotunnisteet
(`CARBIT_REPAIR_ORGANIZATION`, `WORKSHOP_1`, `WORKSHOP_2`).

Onnistunut tulos on yksi JSON-rivi, joka kirjataan hyväksyntäkirjaukseen:

```json
{"status": "ESTABLISHED", "organization_id": "…", "workshop_id": "…", "audit_event_id": 1}
```

UUID:t palautetaan kanonisessa pienaakkosmuodossa. Tuloste syntyy vasta
onnistuneen commitin jälkeen.

## Uusinta ja virhetilanteet

Kaikki tilaan perustuvat päätökset ja kirjoitukset tehdään saman
`BEGIN IMMEDIATE` -transaktion sisällä.

| Tilanne | Tulos |
| --- | --- |
| Ensimmäinen perustaminen tyhjään tuettuun kantaan | `ESTABLISHED`, kaksi riviä ja yksi auditointitapahtuma |
| Sama pyyntö (samat UUID:t ja nimet) uudelleen, myös jäsenyyksien ja töiden lisäämisen jälkeen, kun kannan viiteavaimet ovat eheät | `ALREADY_ESTABLISHED` ja alkuperäinen `audit_event_id`. Ei kirjoituksia eikä uutta tapahtumaa. Eri `--operator-reference` ei tee pyynnöstä ristiriitaista, eikä sitä kirjata. |
| Kannassa on viiteavainrikkomus (`PRAGMA foreign_key_check` ei ole tyhjä), sekä ensimmäisessä perustamisessa että täsmäävässä uusinnassa | Virhe "foreign-key check failed" (`WorkshopEstablishmentError`), ei `ALREADY_ESTABLISHED`-tulosta. Ensimmäisen perustamisen kirjoitukset perutaan. Komento ei korjaa kantaa eikä lisää auditointitapahtumaa. |
| Eri UUID tai nimi, ylimääräinen korjaamo, muuttunut aktiivisuustila, puuttuva, ristiriitainen tai useampi perustamistapahtuma, osittainen rakenne | Virhe "conflicts with this request", ei muutoksia. Rivejä ei täydennetä, aktivoida tai korjata. |
| Vanhaa tai muuta dataa ilman täsmäävää aiempaa perustamista | Virhe "requires an empty new environment", ei muutoksia |
| Alustamaton, tukematon tai keskeneräinen skeema | Virhe "supported schema", ei migraatiota eikä kirjoituksia |
| Puuttuva tai suhteellinen polku tai puuttuva argumentti | Komentorivivirhe (exit 2). Tiedostoa ei luoda. |

Kaatumisen vaikutus riippuu siitä, tapahtuiko se ennen commitia vai sen jälkeen:

- **Virhe ennen commitia** peruu koko perustamisen.
- **Vastauskatko commitin jälkeen** (esim. pääte katkesi ennen JSON-tulosta) ei
  peru jo tallennettua perustamista. Tulos on silloin epäselvä.
- **Keskeytys juuri commitin kohdalla** voi jättää kumman tahansa kokonaisen tilan.

Epäselvässä tilanteessa aja **sama komento samoilla tunnisteilla ja nimillä**
uudelleen. Tulos on joko `ESTABLISHED` tai `ALREADY_ESTABLISHED`, eikä toista
korjaamoa tai tapahtumaa synny. Käytössä olevan kannan palauttamista
varmuuskopiosta ei käytetä tämän komennon rollbackina.

## Jatkoketju

Jatkoketju ajetaan nykyisillä toiminnoilla perustamisen jälkeen:

1. Auth-tili ja MFA: `provision_identity`, `begin_login`, `begin_mfa_enrollment`,
   `confirm_mfa_enrollment` ja operaattoriprosessiin syntyneen istunnon
   `revoke_session`.
2. Jäsenyys: `grant-membership --workshop-id <perustamisen workshop_id>`.
3. Yksi jäsenyys yhdessä ACTIVE-korjaamossa muodostaa aktiivisen korjaamokontekstin
   ilman valintaa.

## Varmennuksen raja

Toiminta on testattu kohdennetuilla yksikkötesteillä
`tests/test_webapp_workshop_establishment.py` eristetyillä tilapäisillä kannoilla ja
synteettisillä tiedoilla. Testit kattavat:

- ensimmäisen perustamisen
- uusinnat
- ristiriitaiset ja osittaiset tilat
- skeemaportin
- syötteet
- puuttuvan tiedoston
- rollbackin ennen commitia ja uusinnan commitin jälkeen
- samanaikaiset pyynnöt
- CLI:n
- liitoksen perustaminen → tuotantoluokilla luotu synteettinen henkilö → jäsenyys
  → juuri tämän korjaamon ratkaiseva konteksti
- OPENCLAW-jäsenyyden hylkäyksen

**Osoitettu erillisellä operaattoriharjoittelulla (17.10.2026).** Synteettinen,
eristetty harjoittelu suoritti ketjun perustaminen → MFA → jäsenyys →
prosessinsisäinen HTTP-login, session ja oman korjaamon business-luku
CLI-, palvelu- ja prosessinsisäisen HTTP-testiasiakkaan tasolla. Myös
operaattori- ja HTTP-istunnon mitätöinti sekä perustamisen turvallinen uusinta
tarkistettiin. Harjoittelu katselmoitiin erikseen. Tiivistelmä ja lähteet:
[hyväksymistilan seuranta, E1](acceptance-status.md#e1--uuden-ympäristön-perustamis--ja-provisionointiketju).
Testiasiakkaan `https://testserver` ei ole TLS-yhteyden näyttöä.

Niitä **ei** ole vielä osoitettu:

- todellinen selain-, HTTPS/TLS- ja proxy-näyttö sekä kohdeympäristön
  Host/Origin-ketju
- ihmisen salasanan syöttö, todentajasovellus, palautuskoodien luovutus ja
  palautuskoodilla kirjautuminen

Yksikkötestit tai hyväksymistilan seuranta eivät korvaa niitä. U1/U2 ja
MFA-palautuksen varaukset säilyvät avoimina.
