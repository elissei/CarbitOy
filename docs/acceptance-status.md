# Hyväksymistilan seuranta

Tämä asiakirja seuraa vain tässä yksilöityjä hyväksymiskohtia. Se ei ole
täydellinen tuotantovalmiuden arvio. Kohdan merkitseminen suljetuksi koskee
vain kirjattua rajausta. Muita hyväksymisehtoja ei merkitä tällä täytetyiksi,
eikä asiakirja anna pilotti- tai tuotantohyväksyntää.

## E1 — uuden ympäristön perustamis- ja provisionointiketju

**Tila: SULJETTU rajatun eristetyn ketjun osalta, 17.10.2026.**

Ongelma oli, ettei täysin uuteen ympäristöön voinut perustaa organisaatiota ja
korjaamoa nykyisillä operaattoritoiminnoilla. Tämän vuoksi jäsenyys ja
korjaamokonteksti epäonnistuivat. Ratkaisu on
[uuden ympäristön perustamismenettely](new-environment-workshop-establishment.md)
(`python -m webapp.workshop_admin establish-initial-workshop`).

Ketju on osoitettu synteettisessä, eristetyssä ympäristössä:

1. uusi ympäristö, kannat alustettu sovelluksen normaalilla käynnistyksellä
2. organisaatio ja yksi ACTIVE-korjaamo (CLI `establish-initial-workshop`, tulos `ESTABLISHED`)
3. synteettinen HUMAN-henkilö ja MFA (palvelut `provision_identity`,
   `begin_login`, `begin_mfa_enrollment` ja `confirm_mfa_enrollment`)
4. jäsenyys (CLI `grant-membership`)
5. automaattinen korjaamokonteksti: yksi jäsenyys johdetaan, joten valintariviä ei tarvita
6. erillinen HTTP-login ja MFA nykyisellä bootstrap-, Origin-/CSRF- ja evästemenettelyllä
7. suojattu business-luku `GET /api/works`, tulos 200 ja tyhjä lista

Muut osoitetut kohdat:

- **Operaattori-istunnon mitätöinti.** Rekisteröinnissä operaattoriprosessiin
  syntynyt istunto mitätöitiin ennen HTTP-kirjautumista. Sama token hylättiin
  palvelussa.
- **HTTP-istunnon mitätöinti.** Logout palautti 204. Sen jälkeen suojattu luku
  antoi 401 sekä samalla asiakkaalla että vanhalla istuntoevästeellä, ja palvelu
  hylkäsi istunnon.
- **Turvallinen uusinta.** Sama perustamispyyntö jäsenyyden jälkeen palautti
  `ALREADY_ESTABLISHED` ja alkuperäisen auditointitapahtuman tunnisteen. Se ei
  muuttanut business-taulujen rivisisältöjä eikä lisännyt auditointitapahtumaa.
- **Kohdennetut testit.** Perustamistoiminnolla on omat testit
  (`tests/test_webapp_workshop_establishment.py`). Niihin kuuluu myös
  FK-rikkinäisen kannan hylkäys täsmäävässä uusinnassa.

Näytön rajat:

- Näyttö on offline-, palvelu- ja CLI-näyttöä sekä prosessinsisäisen
  HTTP-testiasiakkaan näyttöä. Testiasiakkaan `https://testserver` ei ole
  TLS-yhteyden näyttö.
- Salaisuusskannauksen tulos (21 tarkistettua arvoa, 0 osumaa) koskee vain
  harjoittelun tulos-JSONia. Se ei ole yleinen takuu salaisuuksien
  puuttumisesta raporteista, lokeista tai kannoista.
- Business-luku todennettiin vain tyhjällä työlistalla. Töitä tai kahden
  korjaamon eristystä ei kuulu tähän näyttöön.

### Avoimina säilyvät

Nämä eivät kuulu E1:n sulkemiseen:

- todellinen selain-, TLS- ja proxy-näyttö sekä kohdeympäristön Host/Origin-ketju
- ihmisen salasanan syöttö, todentajasovellus, palautuskoodien luovutus ja
  palautuskoodilla kirjautuminen
- SEC-1.2:n U1/U2-varaukset. Riippumaton auditointi jätti ne avoimiksi
  P3-löydöksiksi. Niiden sisältö ja lähde on kirjattu 20.10.2026
  [SEC-1.2:n varaustaulukkoon](auth-rate-limit-checkpoint-sec-1-2.md):
  - U1: rinnakkaiset logoutit ja globaali kiintiö, hyväksytty jäännösriski
  - U2: NAT-osoitteen jaettu asiakaskiintiö, käsitellään SEC-2:ssa.

  Kumpaakaan ei ole korjattu.
- MFA-hätäpalautuksen aiemmat varaukset ([MFA-hätäpalautus](mfa-emergency-recovery.md))

### Seuraavan selain- ja operaattorinäytön tunnetut edellytykset

Selain- ja operaattorinäyttö tilataan erikseen omassa rajauksessaan. Lähteiden
mukaan sen edellytyksiä ovat:

- pilotin kohdeympäristöpäätös
- soveltuva IT_ADMINin offline-palautusmenettelyn hyväksyntä

Näiden tilaa ei ratkaista eikä arvioida tässä asiakirjassa.

### Ei-estävä huomio (P3)

Jatkoharjoittelun skripti merkitsee epäonnistuneen vaiheen tulokseen, mutta se
ei palauta vaihevirheestä erillistä ei-nolla-virhekoodia. Jos skriptiä
myöhemmin automatisoidaan, on tarkistettava kaikki vaihetilat tai lisättävä
virhekoodi. Huomio ei estä E1:n rajattua sulkemista.

### Lähteet

Temp-lähteet ovat paikallisia työtiedostoja. Ne eivät ole pysyvä arkisto,
joten tämä asiakirja tiivistää niiden olennaisen sisällön.

| Päivämäärä | Lähde | Paikallinen polku |
| --- | --- | --- |
| 17.10.2026 | Hyväksytty jatkoharjoittelun katselmointi (reviewer) | `C:\Users\eliss\AppData\Local\Temp\carbit-provisionointi-jatko-review-20261017-codex\katselmointi-fi.md` |
| 17.10.2026 | Jatkoharjoittelun raportti, skripti ja todisteet | `C:\Users\eliss\AppData\Local\Temp\carbit-provisionointi-jatko-20261017-b5ffe290d4d841c7b523e6013a1ebad3\` (`jatkoharjoitteluraportti-fi.md`, `skripti\jatkoharjoittelu.py`, `todisteet\jatkoharjoittelu-tulokset.json`) |
| 17.10.2026 | Tilakirjauksen esteraportti, jossa valmis E1-kirjaus | `C:\Users\eliss\AppData\Local\Temp\carbit-e1-tilakirjaus-20261017-ac4e3d7860ff4b0cb8f57fabe6a09d7c\muutosraportti-fi.md` |

## IT_ADMINin oma MFA-palautus (offline, `start-self`)

**Tila: TOTEUTETTU, TESTATTU JA KATSELMOITU.** Palvelu on commitissa `c29b8c2`
ja konsoli commitissa `ad035e0`. Lisäksi P3-auditointikorjaukset ovat commitissa
`da03f7f`. Ensimmäinen synteettinen harjoitus oikealla konsolilla onnistui
20.10.2026. Menettelyä ei ole hyväksytty käyttöön.

- **Harjoitus:**
  - Mukana olivat synteettinen tili, erillinen auth-kanta ja käyttäjän oma
    PowerShell-ikkuna.
  - Kaikki hyväksymiskriteerit täyttyivät toisella kierroksella:
    `COMPLETED`, ei istuntoa ja tavallinen kirjautuminen uudella TOTP:llä.
  - Ensimmäinen kierros jäi kesken käytettävyysongelmien vuoksi.
  - Kuvaus: [MFA-hätäpalautuksen ohje](mfa-emergency-recovery.md#palautusharjoitus).
- **Avoimina säilyvät:** D3, ilmoitusten toimitus, harjoituksessa havaitut
  käytettävyyden parannukset (ei toteutettu) sekä kohdeympäristön palautus.
  Tämä kohta ei anna pilotti- tai tuotantohyväksyntää.

## Liiketoimintakäyttäjän MFA-palautus (OWNER/SERVICE_MANAGER)

**Tila: TOTEUTETTU JA TESTATTU, 17.10.2026. Palvelukerros ja auth-skeema
katselmoitu ja commitoitu 19.10.2026 (`c29b8c2`), konsoli 20.10.2026 (`ad035e0`),
P3-jatkokorjaukset 20.10.2026 (`da03f7f`).
Ketjua ei ole harjoiteltu oikealla konsolilla eikä hyväksytty käyttöön.**

- **Toteutus:** paikallinen palautusketju, jossa on kolme vaihetta:
  1. IT_ADMINin tuore TOTP-valtuutus
  2. resetointi ja kertakäyttöinen palautusvaltuutus
  3. käyttäjän oma salasana ja uusi TOTP.

  Lisäksi auth-skeema päivitettiin versioon 4. Kuvaus on
  [MFA-hätäpalautuksen ohjeessa](mfa-emergency-recovery.md#liiketoimintakäyttäjän-mfa-palautus).
- **Testit:** testit tehtiin synteettisillä tileillä eristetyissä kannoissa.
  Tulokset ovat toteutusraportissa. Testien läpäisy ei ole katselmointi.
  Palvelukerroksen katselmoinnissa korjattiin P2-virhe: IT_ADMIN-tilin
  offline-palautuksen viimeistely ei nollannut version 4 asettamaa
  palautuslippua, jolloin tili jäi suljetuksi.
- **Avoimina säilyvät:** D3, liiketoimintakäyttäjän palautusharjoitus oikealla konsolilla ja
  ilmoitusten toimitus. Tämä kohta ei laajenna E1:n sulkemista eikä anna
  pilotti- tai tuotantohyväksyntää.
