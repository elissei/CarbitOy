# Carbit: kuvaus ja kysymykset kumppanien IT-yhteyshenkilöille

Tämä dokumentti on tarkoitettu keskusteluihin varaosatoimittajien ja korjaamojärjestelmän toimittajan IT-yhteyshenkilöiden kanssa. Se kuvaa, mitä olemme tekemässä ja mitä meidän pitää tietää ennen integraatiota.

**Tila:** suunnitelma. Yhtään integraatiota ei ole sovittu, eikä dokumentti oleta rajapintoja, hintoja tai käyttöoikeuksia. Otamme yhteyden käyttöön vain kumppanin luvalla ja sen ehdoilla.

## Mitä olemme tekemässä

Carbit on pienen autokorjaamon oma työkalu korjaustiedustelujen valmisteluun. Korjaamon henkilökunta:

1. kirjaa asiakkaan tiedustelun ja tarvittavat työt
2. hakee tarvittavat varaosat ja vertailee toimittajien saatavuutta ja hintoja
3. tarkistaa yhteensopivuuden ja valitsee osan itse
4. tilaa osan itse toimittajan omassa kanavassa
5. tekee ja hyväksyy tarjouksen

Sovellus avustaa, mutta ihminen tekee päätökset. Se ei tee tilauksia automaattisesti.

## Miten järjestelmä käyttää kumppanin palvelua

- **Tavallinen ohjelma:** yhteyden kumppanin palveluun tekee tavallinen, ennalta ohjelmoitu Python-sovellus. Se tekee vain ne kyselyt, jotka sille on ohjelmoitu ja sovittu.
- **Ei tekoälyä kumppanin järjestelmään:** tekoälyagentti ei käytä kumppanin portaalia tai rajapintaa, eikä se saa kumppanin tunnuksia.
- **Tekoäly viestikanavana:** harkitsemme tekoälypohjaista viestikanavaa korjaamon ja sovelluksen väliin, esimerkiksi tiedusteluluonnosten kirjaamiseen. Sen toimet kulkevat Carbitin sääntöjen ja ihmisen hyväksynnän kautta, eikä se ole suorassa yhteydessä kumppanin järjestelmään.
- **Kuormitus:** kyselyt tulevat ihmisen käynnistämistä työvaiheista ja ovat määrältään pieniä. Noudatamme sovittuja nopeusrajoja.
- **Tunnukset:** tunnukset säilytetään palvelimella, eikä niitä näytetä käyttäjille tai viedä selaimeen.
- **Tietoturva:** käyttäjät kirjautuvat kaksivaiheisesti, ja kirjoittavista toimista jää jälki siitä, kuka ne teki.

## Kysymykset varaosatoimittajille

**Yhteystapa**
1. Onko tuotehakuun, saatavuuteen ja nettohintoihin rajapinta? Onko siihen dokumentaatio ja testiympäristö?
2. Jos rajapintaa ei ole, sallitteko sovitun, rajatun automaattisen haun nykyisessä verkkokaupassa? Saammeko luvan kirjallisena?
3. Miten pääsy haetaan, ja kuka sen hyväksyy?

**Sisältö**
4. Voiko hakea rekisterinumerolla, VIN-koodilla, OE-numerolla tai toimittajan tuotenumerolla?
5. Palautuuko asiakaskohtainen nettohinta vai vain listahinta? Palautuuko saatavuus varastoittain ja toimitusaika?
6. Palautuuko vastauksessa yhteensopivuustieto ja tuotekuvat? Mitä näistä saa näyttää sovelluksessamme?

**Ehdot ja rajat**
7. Mitä käyttö maksaa, ja millaiset nopeus- ja määrärajat siinä on?
8. Saako hinta- ja saatavuustietoa tallentaa? Kuinka kauan, ja saako sitä näyttää vertailussa muiden toimittajien rinnalla?
9. Saako tuloksia käyttää tarjouksen pohjana?
10. Onko käyttöehdoissa kohtia, jotka estävät automaattisen käytön tai vertailun?

**Tekniikka**
11. Miten tunnistautuminen toimii, voiko tunnuksen rajata vain lukuoikeuteen, ja miten tunnus vaihdetaan?
12. Pitääkö yhteys tehdä kiinteästä IP-osoitteesta?
13. Miten rajapinnan muutoksista ja katkoista ilmoitetaan, ja kuka auttaa ongelmatilanteissa?

**Myöhemmin:** onko sähköinen tilaus mahdollinen rajapinnan kautta? Emme ole toteuttamassa sitä nyt. Haluamme vain tietää, onko se mahdollinen.

## Kysymykset korjaamojärjestelmän toimittajalle

**Rajapinnat**
1. Mitkä rajapinnat ovat oikeasti olemassa, ja onko niihin dokumentaatio ja testiympäristö?
2. Mitä ne saavat tehdä asiakkaille, ajoneuvoille, tarjouksille ja työmääräimille? Mitä voi lukea, ja mitä voi kirjoittaa?
3. Voiko tarjouksen tai työmääräimen luoda luonnoksena, jonka ihminen hyväksyy järjestelmässänne?
4. Voiko tilamuutoksista saada tiedon, esimerkiksi webhookilla, vai pitääkö niitä kysellä?

**Työajat**
5. Saako kehitteillä olevan työaikaominaisuuden työajat lukea rajapinnasta omaan ohjelmaan, vai näkyvätkö ne vain teidän käyttöliittymässänne?
6. Mitä lisenssi sallii, ja milloin ominaisuus arviolta valmistuu?
7. Säilyykö työajan lähde ja alkuperäinen arvo tiedossa? Miten parityöt ja yhdistetyt työt esitetään?

**Ehdot**
8. Mitä käyttö maksaa, ja saadaanko oikeudet ja ehdot kirjallisina?
9. Miten tunnistautuminen toimii? Voiko sovellukselle antaa oman tunnuksen ja rajatut oikeudet, ja näkyvätkö sen tekemät muutokset lokissa erikseen?
10. Mitkä ovat nopeus- ja määrärajat, ja miten rajapinnan muutoksista ilmoitetaan?

**Tiedot ja henkilötiedot**
11. Miten henkilötietojen käsittely sovitaan rajapinnan kautta: kuka on rekisterinpitäjä ja kuka käsittelijä, ja tarvitaanko sopimus?
12. Missä tiedot sijaitsevat?
13. Saammeko nykyiset tietomme ulos käyttöönoton yhteydessä tai jos sopimus päättyy, ja missä muodossa?
14. Onko testikäyttöön synteettistä dataa, jotta oikeita asiakastietoja ei tarvita?
