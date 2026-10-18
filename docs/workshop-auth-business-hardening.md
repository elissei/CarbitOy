# CP4B-1: auth/business-valtuutuksen kilpailutilanteen korjaus

## Varmennettu ongelma

CP4B-1:n aiempi jäsenyystarkistus toimi business-tietokannan transaktiossa,
mutta istunto, MFA, käyttäjän aktiivisuus ja globaali rooli tarkistettiin
erillisessä auth-transaktiossa ennen sitä. `TrustedWorkshopContext` jäi
snapshotiksi. Handlerin jälkeinen idle-päivitys ei voinut peruuttaa jo
committanutta työtä.

Deterministisessä kahden säikeen toistossa business-kutsua pysäytettiin
kontekstin muodostamisen jälkeen. Toinen yhteys committasi istunnon perumisen,
roolin alentamisen IT_ADMINiksi, käyttäjän eston tai `auth_version`-muutoksen.
Vanha koodi hyväksyi kirjoituksen kaikissa neljässä tapauksessa: testit
epäonnistuivat, koska odotettua kieltoa ei tullut. Jäsenyyden peruminen esti
kirjoituksen jo aiemmin. Toisto käytti vain erillisiä keksittyjä testitilejä
ja testitietokantoja.

## Toteutus ja tarkka järjestys

Palvelimen aidosti validoimasta istunnosta luodaan `WorkshopAuthBinding`.
Se sisältää sisäisen AuthService-viitteen ja ei-bearer-istuntorivin tunnisteen,
ei salasanaa, bearer-tokenia, MFA-salaisuutta tai palautuskoodia. Binding ja
sen tunniste jätetään kontekstin repr-esityksestä pois, eikä niitä palauteta
HTTP-rajapinnassa. Selain ei voi toimittaa bindingia.

Kaikki WorkRepository- ja LabourPlanService-operaatiot käyttävät yhteistä
`WorkRepository.operation`-rajaa:

1. Auth-yhteys tekee `BEGIN IMMEDIATE` ja saa SQLite-kirjoitusvarauksen.
2. Yhteys asetetaan `query_only=ON`: varaus säilyy, mutta auth-rivejä ei voi
   kirjoittaa sillä. Istunto, MFA, auth-versio, käyttäjän tila, käyttäjän UUID,
   auth-varaston tunniste ja **nykyisen** globaalin roolin oikeus tarkistetaan.
3. Vasta tämän jälkeen business-yhteys aloittaa kirjoitustransaktion
   `BEGIN IMMEDIATE` (tai lukusnapshotin `BEGIN`). Jäsenyysversio, aktiivinen
   valinta sekä organisaation ja korjaamon tila tarkistetaan samassa snapshotissa.
4. Työ, tapahtumat, työajat ja retryt käsitellään nykyisillä sopimuksilla.
5. Ennen business-commitia tarkistetaan vielä nykyinen aika, istunnon määräajat
   ja operaation aikabudjetti **saman edelleen lukitun auth-yhteyden kautta**.
   Tarkistus myös varmistaa auth-transaktion pysymisen aktiivisena ja ilman
   auth-rivimuutoksia.
6. Business commit/rollback päättyy ennen auth-varauksen vapautusta. Auth-yhteys
   tekee rollbackin; se ei tarvitse toista pysyvää commitia.

Tämä ei ole irrallinen pre-commit-recheck. SQLite estää kaikkien muiden
yhteyksien ja prosessien auth-kirjoitukset koko varauksen ajaksi myös WAL-tilassa.
Erilliset yhteydet/prosessi ja oikeat SQLite-lukot varmentavat tämän testeissä.
Auth- ja business-tietokantoja ei yhdistetä, ATTACHia ei käytetä eikä skeemaa
muuteta. Sama tiedosto tai sen hardlink-aliakset eivät kelpaa kahdeksi
tietokannaksi; identtinen konfiguraatio estää käynnistyksen ennen alustusta.

Järjestystakuut koskevat toteutuneita transaktioita, eivät HTTP-pyyntöjen
saapumisaikoja:

- Jos auth-peruminen/roolimuutos saa auth-lukon ja committaa ensin, myöhempi
  business-operaatio näkee muutoksen ja kielletään. Vanha konteksti tai retry
  ei ohita sitä. Myös jo jonossa ollut kutsu tarkistetaan vasta lukon saamisen jälkeen.
- Jos hyväksytty business-operaatio saa auth-varauksen ensin, samanaikainen
  auth-peruminen odottaa sen commitin/rollbackin ja varauksen vapautuksen.
  Onnistunut business-commit tapahtuu silloin ennen auth-perumisen commitia.
- Jäsenyyden peruminen ja kirjoitus järjestyvät business-tietokannan
  kirjoituslukon kautta. Ensin committanut peruminen estää myöhemmän
  kirjoituksen; business-lukon ensin saanut kirjoitus voi valmistua ensin.
- Pelkkä perumispyynnön käynnistäminen ei keskeytä jo valtuutettua transaktiota.
  Keskeytyminen tai lukitusaikakatkaisu ei myöskään tarkoita perumisen onnistumista.
- Luku käyttää johdonmukaista jäsenyyssnapshotia. WAL-lukijan snapshotin
  muodostamisen jälkeen committanut jäsenyyden peruminen ei muuta jo
  hyväksytyn lukusnapshotin rivejä.

HTTP-vastaus voi saapua myöhemmän perumisen jälkeen. Takuun raja on business-
transaktio, ei verkkovastauksen toimittaminen. Myöhempi idle-refresh on erillinen
kirjanpitotoimi: se ei valtuuta kirjoitusta, elvytä peruttua istuntoa tai muuta
jo committaneen kirjoituksen vastausta.

## Rajattu lukitus ja virheet

Auth-kirjoitusvarauksen SQLite-odotus on nykyisen AuthRepositoryn enintään
viisi sekuntia. Varauksen saamisen jälkeen business-operaatiolla on viiden
sekunnin monotoninen budjetti. Business-lukon ja commitin busy-timeout
rajoitetaan jäljellä olevaan budjettiin. SQLite-progress handler katkaisee
pitkän SQL-suorituksen; budjetti ja istunnon idle-/absoluuttinen määräaika
tarkistetaan ennen commitia. Aikaa ei oteta vain ennen jonotusta.

Budjetti on yhteistyöhön perustuva: se ei ole Python-koodin tai OS:n kova
reaaliaikainen keskeytysmekanismi. Varaus ei kata ulkoista I/O:ta, HTTP-kutsuja,
käyttäjävuorovaikutusta tai vastauksen lähettämistä. Yhteiseen business-lohkoon
ei saa lisätä näitä eikä uusia AuthService-kutsuja, jotka hankkisivat toisen
auth-kirjoituslukon. Nykyiset lohkot sisältävät paikalliset SQL-operaatiot ja
nykyisen työaikasuunnitelman laskennan. Kaikki SQLite-lukko-odotukset ovat
rajattuja; rajattomat retry-/odotussilmukat eivät kuulu toteutukseen.

Lukitusjärjestys on aina **auth → business**. Offline-jäsenyystoiminnot
vapauttavat lyhyen auth-lukunsa ennen business-kirjoituslukkoa. Ylläpitäjä ei
saa hankkia auth-kirjoituslukkoa jo business-kirjoituslukon sisällä.

Auth-varaus sarjallistaa myös eri käyttäjien lyhyitä business-operaatioita.
Tämä on paikallisen SQLite-mallin tietoinen kustannus. Lukitus voi aiheuttaa
503:n ruuhkassa; SQLite ei lupaa FIFO-järjestystä tai nälkiintymättömyyttä.
Määräaika tarkistetaan juuri ennen commitia, mutta OS:n/fsyncin valmistumista
ennen seinäkellomääräaikaa ei luvata. Suuri kuorma tai pitkäkestoinen työ
tarvitsee erillisen arkkitehtuuripäätöksen, ei lukitusbudjetin poistamista.

Kielto tuottaa yleisen 401/403:n, tallennus-/lukitusvirhe yleisen 503:n,
aina `Cache-Control: no-store`. Kielletty tai epäonnistunut transaktio ei
jätä työ-, tapahtuma-, työaika-, versio- tai retry-rivejä osittain voimaan.
Idle-aikaa ei päivitetä epäonnistuneen kirjoituksen perusteella.

Historialliset eksplisiittiset offline-kontekstit pysyvät luotetun
palvelinkoodin sopimuksena ja vaativat edelleen jäsenyyden. Niitä ei voi
valita HTTP:ssä. Autentikoitu tekijäkonteksti ilman auth-bindingia kielletään;
auth-virheestä ei ole fallbackia offline-kontekstiin tai demopoikkeusta.

## Kaatuminen, varmuuskopiointi ja turvallinen palautus

Tämä korjaus antaa live-valtuutuksen commit-järjestyksen, **ei kahden
tietokannan atomista varmuuskopiota tai palautusta**. Suojatun business-lohkon
auth-yhteys on read-only. Jos prosessi kaatuu, SQLite palauttaa/viimeistelee
business-transaktion omalla journalillaan ja auth-varaus vapautuu. Ei ole
osittaista pysyvää auth-kirjoitusta, jota pitäisi committaa business-kirjoituksen
kanssa. Business-commitin jälkeen puuttuva idle-refresh ei kumoa kirjoitusta.
Tuleva ominaisuus, joka kirjoittaisi molempiin tietokantoihin yhtenä
liiketoimintatoimena, tarvitsee uuden atomisuus- ja recovery-suunnitelman.

Erillinen tai vanha palautus voi silti palauttaa perutun istunnon,
käyttäjän roolin, auth-version, MFA-/palautuskooditilan tai jäsenyyden takaisin
voimaan. Pelkkä `integrity_check`, samanniminen DB tai sama auth-varaston UUID
ei havaitse tätä. Myös samalta hetkeltä otettu vanha DB-pari menettää sen
jälkeen tehdyt turvallisuuspäätökset. Migraation preimage-backup ei ole
sellaisenaan turvallinen online-rollback myöhemmän käytön jälkeen.

Turvallinen operaattorimenettely ennen palvelun avaamista palautuksen jälkeen:

1. Pysäytä kaikki web-, tausta- ja offline-kirjoittajat. Älä vaihda DB-tiedostoa
   käynnissä olevan prosessin alle. Ota ennen palautusta varmennetut suojatut
   varmuuskopiot ja säilytä nykyinen tila sekä ulkoiset luotetut perumispäätökset.
2. Valitse dokumentoitu auth/business-snapshotpari samasta quiescent-ikkunasta.
   Käytä SQLite-backupia tai täysin pysäytetyn, palautetun tietokannan kopiota;
   älä yhdistä eri hetkien DB/WAL/SHM-tiedostoja. Kirjaa polut, tiivisteet,
   skeemaversiot, auth-varaston UUID ja palautuksen hyväksyntä.
3. Tarkista molempien SQLite-eheys ja viiteavaimet sekä tietokantojen välisten
   käyttäjä-UUID-, auth-varasto- ja jäsenyysviitteiden yhteensopivuus. Säilytä
   työversiot, tapahtumat, hyväksynnät, immutable-kohdistukset ja retry-snapshotit.
   Älä korjaa historiaa hiljaisesti tai luo henkilöitä roolien perusteella.
4. Täsmäytä backupin jälkeiset perumiset, estot, rooli- ja jäsenyysmuutokset
   luotettuun palautusta edeltävään tilaan/historiaan. Mitätöi vanhat palautetut
   istunnot ja autentikointihaasteet offline-menettelyllä; uusi kirjautuminen
   ei yksin ratkaise palautunutta roolia tai jäsenyyttä. Älä luota palautettujen
   versiolaskurien oletettuun monotonisuuteen.
5. Jos turvallisuustilaa ei voi todentaa, pidä henkilöpääsy ja epävarmat
   jäsenyydet estettyinä. Oikeat tilit, roolit ja korjaamojäsenyydet tarvitsevat
   eksplisiittisen uudelleenvahvistuksen. Epävarma MFA-/palautuskooditila tarvitsee
   luotetun identiteettitarkistuksen ja uuden MFA/palautuskoodiprovisionoinnin.
   Auth-varaston UUID:n vaihtaminen ei ole korjaus tai valtuutuspäätös.
6. Avaa palvelu vasta erikseen hyväksytyn, testatun recovery-menettelyn jälkeen.
   Myös palautus aiempaan sovelluskoodiin palauttaisi sen TOCTOU-riskin.

Tämä työ ei toteuta automaattista recoverya eikä muuta oikeaa auth-varastoa,
kehityksen `var/carbit.db`:tä tai tuotantotietoja. Recovery-menettelyn
hyväksyntä ja myöhemmän kuorman kapasiteettiratkaisut jäävät erillisiksi päätöksiksi.

## Muutetut tiedostot tässä kovennuksessa

```text
webapp/auth_service.py
webapp/business_authorization.py                  (uusi)
webapp/database.py
webapp/http_access.py
webapp/labour_service.py
webapp/main.py
webapp/workshop_context.py
webapp/workshop_identity.py
webapp/workshop_repository.py
tests/test_webapp_auth_business_ordering.py        (uusi)
docs/workshop-auth-business-hardening.md           (uusi)
docs/workshop-foundation-checkpoint-4b-1.md
```

## Varmennus 2026-10-11

- Edellisen CP4B-1-ajon vertailutaso: 1053 passed.
- Ennen korjausta uusi toistotesti: 4 failed, 1 passed. Istunnon, roolin,
  disabled-tilan ja auth-version kilpailutilanne toistui; jäsenyystarkistus toimi.
- Eristys-, HTTP-portti- ja concurrency-sarja: 247 passed (106,75 s).
- Auth-virheiden delegoinnin korjauksen tarkistus: 119 passed (55,17 s).
- Eksplisiittisen roolikartan tarkistus: 11 passed (5,94 s); nykyinen oikeusjoukko
  on sama kuin aiemmin, mutta tulevia enum-oikeuksia ei myönnetä automaattisesti.
- Lopullinen koko regressiosarja: `python -m pytest -q`, **1215 passed**,
  1 warning (414,12 s). Sama Starlette/httpx-deprecation oli jo vertailutasossa.

Uudet 162 parametroitua testitapausta ajetaan DELETE- ja WAL-tiloissa.
Ne kattavat istunnon perumisen, globaalin roolin alentamisen, käyttäjän eston,
auth-version muutoksen ja jäsenyyden perumisen ennen kirjoitusta sekä
perumisen ja business-commitin molemmat järjestykset. Testit käyttävät
Event-barriereja, oikeita riippumattomia SQLite-yhteyksiä ja yhtä erillistä
prosessia; järjestystä ei päätellä sleepistä. Luonti, työaikapäivitys,
suunnitelman tallennus, hyväksyntä ja historialliset retryt tarkistetaan.

Myöhäinen idle-/absoluuttinen expiry, monotonisen budjetin ylitys, SQL-progress-
katkaisu ja injektoitu commit-virhe palauttavat kaikki business-rivit ennalleen.
Mukana ovat versiot, tapahtumat, labour-historia, kohdistukset, retryt ja
`sqlite_sequence`. Kilpailevat samannonceiset kirjoitukset committaavat vain
yhden työn/tapahtuman/retry-rivin. HTTP-kiellot eivät kosketa idle-aikaa.
Auth-varauksen read-only-rajoitus ja lukkojen vapautuminen virheessä on varmennettu.

Täysi ajo löysi ensin viisi auth-virhevastauksen tekstiregressiota uusista
exception-käsittelijöistä. Auth-reittien virheet delegoitiin takaisin entiseen
middlewareen; alkuperäisiä auth-testejä tai niiden hyväksymiskriteerejä ei muutettu.

`var/carbit.db`, aiempi CP4B-1-varmuuskopio ja molemmat untracked-
liiketoimintadokumentit säilyivät SHA-256-varmennettuina muuttamattomina.
Haara on `feature/web-app`, HEAD edelleen
`e93aeb31bf474fcbcbf633a69c7d33e32ba1f96d`. Staging-, commit-, push-, merge-,
deploy- tai tuotantotietokantatoimia ei tehty. Aiempia CP4B-1:n paikallisia
muutoksia ei poistettu tai committattu.
