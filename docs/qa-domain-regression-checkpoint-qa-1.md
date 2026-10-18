# CARBIT Oy — QA-1: hyväksytyn domainin regressiotestien palautus

Päiväys: 2026-10-13. Työhaara: feature/web-app.
Työpuun HEAD: e93aeb31bf474fcbcbf633a69c7d33e32ba1f96d.

## Tila

Palautus tehty; itsenäisen HEAD + QA-1 -snapshotin käyttäjävarmennus PASSED
2026-10-14: 173/173 kohdennettua testiä ja 1162/1162 koko snapshotin regressiotestiä
läpäisty, molempien exit-koodi 0. Täydessä ajossa yksi StarletteDeprecationWarning.
Aiemmat silloisen laajemman työpuun tulokset, 173/2210 passed ja yksi tunnettu
deprekaatiovaroitus, säilyvät erillisenä historiallisena käyttäjävarmennuksena.
QA-1 sisältää seitsemän testimoduulia ja services/proposal_builder.py-apumoduulin.
Kaikki kahdeksan palautettua Python-tiedostoa vastaavat tavutasolla hyväksytyn
commitin 06a0ee4ebfb0942877738586ea4b6ecf77531c54 Git-blobien sisältöä.

## Palautetut tiedostot ja lähde

Kaikkien seuraavien tiedostojen lähdecommit on
06a0ee4ebfb0942877738586ea4b6ecf77531c54.

| Tiedosto | Toimenpide | Testifunktioita staattisesti |
| --- | --- | ---: |
| tests/test_labour_plan.py | Puuttuva moduuli palautettu | 41 |
| tests/test_labour_review.py | Puuttuva moduuli palautettu | 28 |
| tests/test_pair_replacement.py | Puuttuva moduuli palautettu | 7 |
| tests/test_proposal_builder.py | Puuttuva moduuli palautettu | 6 |
| tests/test_side.py | Puuttuva moduuli palautettu | 2 |
| tests/test_work_plan.py | Puuttuva moduuli palautettu | 5 |
| tests/test_work_item.py | Hyväksytyt lisäykset palautettu | 7 |
| services/proposal_builder.py | Välttämätön historiallinen riippuvuus palautettu | — |

Kuudessa puuttuneessa moduulissa on 89 testifunktiota. Seitsemässä rajattuun
sarjaan kuuluvassa moduulissa on yhteensä 96 testifunktiota. Nämä ovat lähdekoodin
staattisia funktiomääriä, eivät pytestin keräysmääriä; parametrisoidut tapaukset
kasvattavat ajettavien testitapausten määrää.

## Muutokset ja poikkeamat

- test_work_item.py oli ennen tehtävää indeksin sisällön kanssa yhtäpitävä.
  Sen kolme aiempaa testifunktiota säilyvät; etujarrutestiin palautui hyväksytty
  Side.UNKNOWN-odotus ja lisäksi neljä puoleen liittyvää testifunktiota.
  Olemassa olevia odotuksia ei poistettu eikä heikennetty.
- test_labour_plan.py, test_labour_review.py ja test_proposal_builder.py tuovat
  services.proposal_builder.build_raw_proposal-funktion. Apumoduuli on siksi
  välttämätön alkuperäisten testien keräämiseksi.
- proposal_builder on alkuperäinen yhden WorkItemin legacy-apufunktio. Sen
  hinnoittelulogiikkaa tai kutsujia ei muutettu. Sitä ei kytketty WD-2:n
  monityösopimukseen, uuteen tarjousarkkitehtuuriin tai HTTP-palveluun.
- Riippuvuusmanifestit ja asennetut paketit jätettiin ennalleen. Uusia
  pakettiasennuksia ei tehty. Palautetut testit käyttävät nykyisissä manifesteissa
  jo nimettyjä pytest-, Pydantic- ja PyYAML-riippuvuuksia.
- Historiallisista tiedostoista ei tehty sisältöpoikkeamia.

## Historialliset Codexin testikäynnistysyritykset

Työhakemisto kaikissa komennoissa:
C:\Users\eliss\Projects\carbitoy-web.

Ensin yritettiin rajattua sarjaa:

~~~powershell
.venv/Scripts/python.exe -B -m pytest -p tests.wd2_test_runtime -p no:cacheprovider tests/test_labour_plan.py tests/test_labour_review.py tests/test_pair_replacement.py tests/test_proposal_builder.py tests/test_side.py tests/test_work_plan.py tests/test_work_item.py -q
~~~

Sen jälkeen yritettiin koko nykyistä regressiosarjaa:

~~~powershell
.venv/Scripts/python.exe -B -m pytest -p tests.wd2_test_runtime -p no:cacheprovider -q
~~~

Molemmat komennot estyivät ennen Python-prosessin ja pytestin käynnistymistä:

~~~text
exec_command failed: CreateProcess { message: "Rejected(\"Failed to create unified exec process: helper_unknown_error: setup refresh had errors\")" }
~~~

| Sarja | Keräysmäärä | Läpäisytulos |
| --- | --- | --- |
| Rajattu palautettujen testien sarja | Ei saatavilla: pytest ei käynnistynyt | Ei ajettu |
| Koko nykyinen regressiosarja | Ei saatavilla: pytest ei käynnistynyt | Ei ajettu |

Myös Python-version tarkistus sekä git --no-optional-locks status --short
estyivät samaan suoritusympäristön alustusvirheeseen. Paikallisen .venv:n
Scripts-hakemistosta löytyi python.exe ja pytest.exe. Virhe ei siten osoita
puuttuvaa Python-tiedostoa, puuttuvaa pakettia tai testiepäonnistumista.

Komentoihin valittiin olemassa oleva tests.wd2_test_runtime-plugin, joka ohjaa
väliaikaistiedostot projektin .test-tmp-hakemistoon. -B estää bytecode-kirjoitukset
ja no:cacheprovider poistaa pytest-välimuistin. Pluginia ei muutettu. Koska
prosessit eivät käynnistyneet, näitä ajoja tai keräystä ei tosiasiassa tapahtunut.
Sandboxia ei ohitettu eikä korotettuja oikeuksia pyydetty. Alustusvirheestä ei
voida päätellä, että korotetut oikeudet olisivat tarpeen.

## Myöhempi käyttäjävarmennus — kirjattu 2026-10-14

Käyttäjä vahvisti alkuperäisten käynnistysyritysten jälkeen seuraavat tulokset:

| Sarja | Käyttäjän suorittama ja vahvistama tulos |
| --- | --- |
| QA-1:n kohdennettu sarja | 173 passed |
| Koko silloisen työpuun regressiosarja | 2210 passed |

Käyttäjä ilmoitti yhden tunnetun deprekaatiovaroituksen. Tämä on vahvistuksen
kirjauspäivä; varsinaisten testiajojen päivämäärää ei ole vahvistettu tässä kirjauksessa.
Ajot koskivat silloista työpuuta, eivät eristettyä HEAD + QA-1 -snapshotia.
Codex ei suorittanut näitä ajoja eikä tämän dokumentaatiopäivityksen yhteydessä testejä.

Yllä säilytetty helper_unknown_error oli ennen prosessin luontia tapahtunut
suoritusympäristön alustusvirhe, ei epäonnistunut testi. Myöhempien käyttäjäajojen
perusteella sitä ei esitetä silloisen työpuun QA-1-testivarmennuksen avoimena esteenä.
Käyttäjän työpuulle antama testihyväksyntä ei korvaa itsenäisen snapshotin varmennusta
eikä merkitse turvallisuusauditointia tai tuotantovalmiuden hyväksyntää.

## Eristetyn snapshotin käyttäjävarmennus — 2026-10-14

Tila: PASSED. Käyttäjä raportoi seuraavat paikallisessa PowerShellissä suoritetut
ajot. Codex ei suorittanut testejä eikä luonut tätä snapshotia.

Snapshotin sijainti:

~~~text
C:\Users\eliss\AppData\Local\Temp\carbit-qa1-2e568bf9f04b40b5974e4b63cb0cb589\snapshot
~~~

Käyttäjä varmisti snapshotin pohjaksi täsmälleen HEAD-commitin
e93aeb31bf474fcbcbf633a69c7d33e32ba1f96d ja lisäsi vain tämän checkpointin
taulukon kahdeksan QA-1-Python-tiedostoa: seitsemän testimoduulia ja
services/proposal_builder.py. Muita commitoimattomia WD-, WORKSHOP-, DRAFT-,
SEC- tai muita muutoksia ei sisällytetty. Snapshotin rakentaminen varmennettiin.

| Sarja | Käyttäjän vahvistama tulos | Kesto | Exit-koodi |
| --- | --- | --- | --- |
| Eristetyn snapshotin kohdennettu QA-1 | 173/173 passed | 1.87 s | 0 |
| Eristetyn snapshotin koko regressio | 1162/1162 passed | 316.56 s | 0 |

Täysi ajo raportoi yhden StarletteDeprecationWarning-varoituksen.
1162 on eristetyn HEAD + QA-1 -snapshotin testimäärä; aiempi 2210 kuuluu
laajempaan silloiseen työpuuhun. Näitä täyden regression tuloksia ei yhdistetä.

Käyttäjä varmisti snapshotin rakentamisen ja testien jälkeen alkuperäisen
repositoryn haaran feature/web-app, HEADin, indeksin, Git-statuksen ja QA-1-tiedostojen
tiivisteet muuttumattomiksi. Tämä on käyttäjän ilmoittama eheysvarmennus,
ei Codexin suorittama Git-komento tai testiajo.

Eristetty ajovarmennus osoittaa QA-1:n toimivan ilman muita commitoimattomia
kokonaisuuksia. Se ei merkitse tuotantovalmiuden hyväksyntää, uutta
turvallisuusauditointia, staging- tai commit-lupaa eikä SEC-2B:n toteutuslupaa.

## Itsenäisen QA-1-snapshotin uusinta-ajon ohje

Mahdollinen uusinta suoritetaan erikseen hyväksytyn HEAD + QA-1 -snapshotin
juuressa toimivalla virtuaaliympäristöllä.
tests.wd2_test_runtime ei kuulu QA-1-checkpointiin;
QA-1-testit eivät tarvitse sitä. Yllä olevat alkuperäiset plugin-komennot säilyvät
historiallisena näyttönä, eivät itsenäisen snapshotin riippuvuusvaatimuksena.

~~~powershell
.\.venv\Scripts\python.exe -B -m pytest -p no:cacheprovider tests/test_labour_plan.py tests/test_labour_review.py tests/test_pair_replacement.py tests/test_proposal_builder.py tests/test_side.py tests/test_work_plan.py tests/test_work_item.py -q
.\.venv\Scripts\python.exe -B -m pytest -p no:cacheprovider -q
~~~

Nämä ovat uusinta-ajon ohjeita, eivät käyttäjän ajamien komentojen tarkaksi
lokiksi väitettyjä komentoja. Codex ei suorittanut niitä tässä päivityksessä.
Käyttäjän eristetyn ajon tulokset on kirjattu yllä. Uusinnan tulokset kirjataan
toteutuneen ajon perusteella; täyden sarjan testimäärää ei oleteta 2210:ksi.

## Alkuperäisen palautuksen rajaus ja päivitetyt avoimet riskit

Lähtötilanteessa indeksivertailussa oli 20 ennestään muokattua seurattua
tiedostoa eikä stagettuja muutoksia. Tarkastettujen lähde- ja dokumenttihakemistojen
inventaarissa oli 51 seuraamatonta tiedostoa. Git-tila tarkistettiin lukemalla
HEAD, indeksi ja Git-objektit; git branch --show-current onnistui.

243 olemassa olevan lähde-, resurssi-, ohje- ja asetustiedoston SHA-256-lähtötilaa
verrattiin palautuksen jälkeen. Ainoa muuttunut lähtötilanteen tiedosto oli
tehtävään kuuluva tests/test_work_item.py. Muut seitsemän historiallista
Python-tiedostoa ja tämä checkpoint ovat lisättyjä tiedostoja. Git-indeksi
säilyi muuttumattomana.

- Silloisen työpuun testivarmennus on käyttäjän vahvistama: 173/2210 passed
  ja yksi tunnettu deprekaatiovaroitus. Historialliset Codexin käynnistysyritykset
  eivät antaneet testitulosta, ja ne säilytetään erillään käyttäjän ajoista.
- Itsenäisen HEAD + QA-1 -snapshotin käyttäjävarmennus PASSED 2026-10-14:
  173/173 kohdennettua ja 1162/1162 koko regression testiä, exit-koodit 0.
  Täydessä ajossa yksi StarletteDeprecationWarning. Eristetyn ajon varmennuseste
  on poistunut; historialliset Codexin alustusvirheet säilyvät erillisinä.
- Jos myöhempi ajo osoittaa yhteensopivuusongelman, domainia ei muuteta eikä
  testiodotuksia heikennetä ilman erillistä päätöstä.
- Domainin 3A/3B-toteutusta, WD-2/WD-2.1-sopimusta tai adaptereita, HTTP- tai
  tietokantatoteutusta ei muutettu. Ennakkotilanteen muut tiedostomuutokset
  jätettiin koskemattomiksi.
- WD-3:a ei aloitettu. Migraatioita, stage-, commit-, push-, merge- tai
  deploy-toimia ei tehty. Yksityistä keskusteluhistoriaa ei muutettu.
