# CARBIT Oy — CP-WD-2

Checkpoint-tila (2026-10-14): READY rajattuun viiden tiedoston WD-checkpointiin.
Käyttäjä vahvisti erottelun jälkeisen nykytyöpuun kohdennetun ajon (91 passed)
ja eristetyn HEAD + WD -snapshotin WD-testimoduulin ajon (89 passed).
Codex ei suorittanut näitä testejä. Täyttä eristettyä regressiota ei ole vahvistettu.
READY ei ole staging-, commit- tai tuotantokäyttölupa.

Tämä vaihe toteuttaa vain sovelluskerroksen tietosopimuksen ja puhtaat
domainadapterit. WD-3–WD-6, tietokantamigraatiot, HTTP/editori, hyväksyntäsilta
ja tarjouskooste eivät kuulu toteutukseen. Nykyisiä 3A/3B-sääntöjä ei muuteta.

## Lähtötiedot

Tarkistetut rakenteet: domainin WorkPlan/PlannedWork, WorkItem, PartRequirement,
Vehicle, LabourTime ja LabourPlan, services/labour_planning.py ja labour_pricing.py,
appin intake- ja demotyönkulut sekä webappin draft-, labour- ja hyväksytyn
työveloituksen sopimukset/palvelut. Nykyinen RawProposal on yksityökohtainen;
sen laajentaminen kuuluu myöhempään vaiheeseen.

CP-WD-1:n suunnittelutuloksia ei löytynyt projektin docs- tai app-aineistosta.
Yksityistä keskusteluhistoriaa ei luettu eikä muutettu. Toteutuksen lähtökohtana
ovat käyttäjän tehtävässä vahvistamat 11 liiketoimintapäätöstä. WD-1-aineiston
vastaavuus pitää tarkistaa ennen WD-3:n toteutuslupaa.

## Sopimus

`app/work_plan_contract.py` sisältää Pydantic-mallit. Kaikki sovellusmallit ovat
frozen-malleja, kokoelmat tupleja ja tuntemattomat kentät hylätään. Sopimuksen
JSON voidaan palauttaa `WorkPlanSnapshot.model_validate_json`-kutsulla.
Desimaalit tallentuvat JSONissa täsmällisinä merkkijonoina; bool/float, ei-äärelliset
sekä ei-positiiviset alkuperäiset tunnit hylätään. Uusia riippuvuuksia ei tarvita.

| Rakenne | Merkitys |
| --- | --- |
| WorkPlanSnapshot.schema_version = 2 | Uusi, erikseen käyttöön otettava sovellussopimus. Ei nykyisen draft-payloadin version korotus. |
| organization_id, workshop_id, inquiry_id, plan_id | Korjaamo-/organisaatiokonteksti ja yhden tiedustelun looginen suunnitelma. Tunnukset eivät itsessään valtuuta käyttäjää. |
| revision_id, revision, previous_revision_id | Suunnitelman muuttumattoman version tunnus, numero ja edeltäjä. Myöhemmät revisiot vaativat edeltäjän. |
| SourceRevision | V1_DRAFT- tai INQUIRY-lähtöversion viite. Lähtörevision numero on riippumaton suunnitelman revisiosta. Ei luonnoksen konversiota. |
| VehicleData, customer_description | Keskeneräinen tiedustelu sallii puuttuvat tiedot ja tyhjän works-kokoelman. |
| WorkData | Pysyvä work_id, erillinen uniikki display_order, kuvaus, operaatio ja akseli-/puolirajaus. Puuttuvat tiedot ovat None. UNKNOWN on eksplisiittinen domainarvo. |
| PartNeed | Työn sisäinen pysyvä requirement_id sekä nykyisen PartRequirementin kentät. Määrä on positiivinen kokonaisluku tai None. Ei varaosavalintaa tai tilausta. |
| TimeData | Pysyvä time_id, täydellisen ajan covered_work_ids, side, alkuperäiset tunnit ja aikalähteen tiedot. Keskeneräinen aikatieto sallitaan sopimuksessa. |
| selected_time_ids | Eksplisiittiset aikalähdevalinnat. Adapteri ei valitse vaihtoehtoja automaattisesti. |
| PlanRevisionRef | Organisaation, korjaamon, tiedustelun, loogisen suunnitelman, revision ja sovellussisällön SHA-256-tunnisteen sidonta. |
| WorkReviewRecord | Työkohtainen toimihenkilön CHECKED-merkintä, tarkistaja ja aikavyöhykkeellinen aika. Ei asiakashyväksyntä. |
| historical_labour_approval / LabourApprovalRecord | Historiallinen päätösviite: decision_id, labour_revision_id, suunnitelman revisioviite ja domainin LabourReviewDecision. Ei nykyisen revision hyväksyntätila tai hyväksyntäportti. |

ID:t antaa kutsuja. Säilyvien töiden tunnukset säilytetään revisioiden,
järjestysmuutosten ja sarjallistamisen läpi. Poistettujen töiden tunnuksia ei
käytetä uusille töille. requirement_id on työn sisäinen; time_id on suunnitelman
sisäinen. Adapterit eivät generoi tunnuksia tai rakenna niitä listan indeksistä.
Sopimus tarkistaa yhden snapshotin yksikäsitteisyyden ja viitteet; myöhempi
säilytyskerros varmistaa elinkaaren yli pysyvyyden ja yhden loogisen suunnitelman
tiedustelua kohti. Numerojärjestyksen jatkuvuus ja edeltäjäketjun olemassaolo
vaativat myöhemmin tallennuskerroksen tarkistukset.

`content_id` lasketaan koko sovellussnapshotista ilman tarkistuksia ja
LabourPlan-päätöstä. Se sisältää myös näyttöjärjestyksen, kuvaukset, varaosatarpeet,
lähtöversion ja revisiokontekstin. Töiden ja aikojen kokoelmajärjestys sekä
desimaalien vastaavat esitykset eivät vaikuta tunnisteeseen. Sovelluksen tunniste
on eri asia kuin domainin LabourPlan.content_id; molemmat tarvitaan myöhemmin.
Johdettu content_id/revision_ref lasketaan uudelleen, niitä ei syötetä snapshotiin.

Työtarkistus on nykyinen vain, kun tietueen sisältö ja sisäinen PlanRevisionRef
on uudelleenvalidoitu, työ-ID kuuluu nykyisiin töihin ja koko revisioviite täsmää.
Nykyinen työ-/revisiokonteksti validoidaan erikseen ennen vertailua. Frozen-malli
ei yksin riitä: model_copy(update=...) voi ohittaa validoinnin. Virheellinen
tarkistustietue jätetään laskennasta pois; muiden töiden kelvolliset merkinnät
voivat silti olla nykyisiä. Puuttuva merkintä
tarkoittaa tarkistamatonta työtä. `unchecked_work_ids` listaa nämä näyttöjärjestyksessä;
`all_works_checked` on false myös tyhjälle tiedustelulle. Uusi revisio tai mikä
tahansa sisältömuutos vanhentaa kaikki vanhan snapshotin työmerkinnät. Tämä on
tarkoituksella varovainen sopimus: merkinnät säilyvät historiallisina, mutta niitä
ei automaattisesti siirretä uuteen revisioon.

Työmerkinnät eivät muuta LabourPlan-hyväksyntää. historical_labour_approval sisältää
ainoastaan historiallisen päätösviitteen. Sen APPROVED-status, viitteen olemassaolo
tai edes täsmäävä revisioviite ja domainin sisältötunniste eivät ole hyväksyntäportti.
WD-2:n adapteri ei siirrä viitettä LabourPlan.review_decision-kenttään. Adapterin
palauttama LabourPlan pysyy PENDING-tilassa, ja require_approved_final_hours sekä
require_final_approval_ready kieltäytyvät hyväksynnästä myös silloin, kun kaikki
työkohtaiset merkinnät ovat kelvollisia. Nykyisen domainin voimassa olevan
päätöksen tarkistus ja luotetun sovelluspalvelun toiminta ovat eri asia kuin
historiallisen viitteen lukeminen. WD-5:n tulee
myöhemmin tarkistaa sekä PlanRevisionRef että domainpäätöksen sisältötunniste,
sääntöversio, alkuperäinen summa ja nykyinen hyväksyntä sekä työkohtaiset tarkistukset.
Tämä vaihe ei toteuta sitä hyväksyntäsiltaa tai suunnitelman hyväksymistoimintoa.
Asiakkaan vahvistusta tai tarjousversion hyväksyntää ei mallinneta työmerkinnäksi.

## Adapterit

`app/work_plan_adapters.py` sisältää:

- `missing_work_data`: rakenteiset puuttuvien tietojen polut editorin myöhempää käyttöä varten.
- `to_domain_work_plan`: ajoneuvo ja tunnistetut työt näyttöjärjestyksessä, pysyvät työ-ID:t ja työkohtaiset varaosatarpeet nykyisiin domainmalleihin.
- `to_domain_labour_plan`: samat työt, alkuperäiset ajat ja eksplisiittiset valinnat nykyiseen LabourPlaniin ilman päätösten siirtämistä.

Tyhjä tiedustelu tai puuttuva ajoneuvon pakollinen tieto, operaatio, akseli, puoli
tai lisätyn varaosatarpeen kenttä aiheuttaa `IncompleteWorkPlanError`-virheen.
Sen `missing` sisältää kenttäpolut ja syyn. Puuttuvaan kohtaan ei tehdä tekaistua
WorkItemiä. Puuttuva side jätetään sovellukseen; tarvittaessa kutsuja voi antaa
nykyisen domainin eksplisiittisen UNKNOWN-arvon.

Aikatietueen puuttuvat tunnit/lähde tai manuaalisen Autodata-ajan kuvaus/kirjaaja
estävät LabourTimeEntry-projektion samalla rakenteisella virheellä. Kokonaan
puuttuva aika tuottaa nykyisen LabourPlanin kattavuusongelmat; osasummaa ei esitetä
valmiina. Sovellustietoja ei muuteta. Adapterit validoivat myös model_copy-kutsulla
mahdollisesti ilman validointia rakennetut syötteet uudelleen.

Pari-/sarjavaihdon alkuperäinen kokonaisaika käytetään kerran. Varaosamäärä ja
yksikkö eivät kerro työaikaa. Vastakkaisten puolten erillisiä aikoja ei summata
pari-ajaksi domainin suojauksen ohi. Eksplisiittinen yhdistetty aika voi kattaa
molemmat työt. Domainin kattavuus-, päällekkäisyys- ja ihmistarkistussäännöt
pysyvät voimassa; adapteri ei tee päällekkäisyysvähennystä.

## Yhteensopivuus ja rajat

Nykyiseen webappiin ei lisätä importtia, reittiä tai palvelukutsua. V1-luonnosten
editor_json, schema-versio 1, revision merkitys ja sallitut kentät pysyvät ennallaan.
Nykyiset tallennus-, HTTP- ja hyväksytyn työveloituksen palvelut eivät vaihda
uuteen sopimukseen tässä vaiheessa. Legacy-hinnoittelu pysyy ennallaan, mukaan
lukien 1,60 h → 144,00 €, veroton 114,74 € ja ALV 29,26 €.

WD-2 ei muodosta työveloituksen rahasummaa, tarjousversiota tai asiakashyväksyntää.
Se valmistaa monityöisen domain-LabourPlanin nykyisten sääntöjen mukaisesti.
Yksi myöhemmin hyväksytty kokonaissumma kulkee nykyisen hyväksytyn työveloituksen
säännöillä vasta erikseen luvitetun hyväksyntäsillan kautta. Nykyinen
OriginalWorkTimeRecord-tallennusmalli kuvaa yhden ajan työtä kohti; tätä ei
laajenneta yhdistettyjen aikojen tallennukseksi WD-2:ssa.

Ei tietokantamigraatiota, asiakkaan viestien lukua/lähetystä, ostotilausta,
CRM-toimintoa, yksityisen historian muutosta tai Gitin stage/commit/push/merge/deploy-toimia.

## Testaus (2026-10-12)

Kohdennettu sarja: **56 läpäissyttä testiä**. Kattavuus: useat työt, pysyvät ID:t,
järjestäminen/poistaminen/lisääminen, työkohtaiset varaosatarpeet, pari-/sarja- ja
yhdistetty kokonaisaika, domainin kattavuussuojaukset, puuttuvat tiedot,
työkohtaiset tarkistukset, hyväksyntöjen erottelu, muuttuneen revision/sisällön
sidonta, JSON/desimaalit, v1-luonnokset ja nykyinen hinnoittelu.

Koko regressiosarja: **2005 läpäissyttä testiä**, 1 varoitus, 939,56 s.
Varoitus on aiemmin havaittu Starlette TestClientin HTTPX-deprekaatiovaroitus;
riippuvuuksia ei muutettu. Lopullinen kohdennettu ajo myös projektin sisäisellä
testiapurilla: **56 läpäissyttä testiä**, 0,67 s.

```powershell
.\.venv\Scripts\python.exe -m pytest -q tests/test_work_plan_contract.py
.\.venv\Scripts\python.exe -m pytest -p tests.wd2_test_runtime -q tests
git diff --check
```

`tests/wd2_test_runtime.py` on erikseen käyttöön otettava testiapuri. Se ohjaa
TEMP/TMP/TMPDIR- ja tempfile-polut sekä pytestin basetempin projektin
`.test-tmp/wd2-regression-*`-hakemistoon. Basetemp on aina uusi alihakemisto;
käyttäjän olemassa olevaa hakemistoa ei anneta pytestin siivottavaksi.
Tavallinen testiajo ja tuotantokonfiguraatio eivät muutu. Olemassa olevat
tietokantatestit käyttävät eristettyjä testikantoja ja selaintestit paikallisia
testipalvelimia; tuotanto- tai käyttäjätietokantoja ei avata testipalvelimina.

Ensimmäinen koko sarjan ajo keskeytettiin, koska pytestin oletusväliaikaispolku
voi sijaita projektin ulkopuolella. Korjattu regressioajo käyttää yllä mainittua
testiapuria. Keskeytyneen ajon projektin ulkopuolisia väliaikaistiedostoja ei
poistettu tämän työn yhteydessä. Käyttäjän viimeisimmän rajauksen jälkeen
komentoja ei yritetty uudelleen sandboxin ulkopuolella.

Komentotyökalun aiempi käynnistysvirhe ei osoittanut WD-2:n tarvitsevan
järjestelmänvalvojan oikeuksia. Esimerkiksi seuraava luku-komento epäonnistui
ennen PowerShellin käynnistymistä:

```powershell
rg --files --hidden -g AGENTS.md -g '!\.git/**'; rg -n 'WD-1|WD-2|monity|WorkPlan' docs services webapp domain tests; Get-Content domain/models/work_plan.py, domain/models/work_item.py, domain/models/labour_plan.py, domain/models/labour_time.py, services/labour_planning.py, services/labour_pricing.py -Encoding UTF8
```

Työkalun täsmällinen virhe:

```text
exec_command failed: CreateProcess { message: "Rejected(\"Failed to create unified exec process: helper_unknown_error: setup refresh had errors\")" }
```

Sandboxin ulkopuolista suoritusta pyydettiin aiemmin tämän työkalun
käynnistysvirheen vuoksi. Käyttäjän tarkennuksen jälkeen tiedostojen toteutus
ja onnistuneet testiajot tehtiin sandboxissa.

## WD-3:n valmistelu erillisellä luvalla

1. Tarkista CP-WD-1:n suunnitelman vastaavuus tähän sopimukseen.
2. Suunnittele yhden tiedustelun loogisen suunnitelman yksikäsitteisyys ja
   korjaamo-/organisaatiorajaus nykyisten valtuutusten pohjalta.
3. Suunnittele muuttumattomat revisiot, edeltäjäketju, ID:iden säilyminen,
   optimistic concurrency ja tallennuksen atomisuus.
4. Ratkaise työkohtaisten tarpeiden, vaihtoehtoisten/yhdistettyjen aikojen,
   kattavuusviitteiden ja valintojen säilytys; säilytä desimaalit täsmällisinä.
5. Suunnittele työmerkintöjen sekä LabourPlan-päätösten erilliset viitteet ja
   historia. V1-payloadit säilyvät luettavina ilman automaattista tulkintamuutosta.
6. Tee migraation ja palautuksen testisuunnitelma erillisiin testikantoihin.

WD-3:n toteutusta ei aloiteta tällä luvalla.

## CP-WD-2.1 — auditointilöydösten rajattu korjaus (2026-10-13)

Korjaukset koskevat vain sovellussopimusta ja sen testejä:

1. unchecked_work_ids ja all_works_checked käyttävät uudelleenvalidoituja
   työtarkistuksia. Kopioitu tyhjä tarkistaja, virheellinen status,
   aikavyöhykkeetön aika, virheellinen/puuttuva työ-ID tai virheellinen,
   vieraan kontekstin tai vanhentunut revisioviite eivät merkitse työtä tarkistetuksi.
   Laskenta ei muokkaa alkuperäisiä merkintöjä.
2. PartNeed.required_quantity vaatii strict=True, gt=0 tai None. Nolla,
   negatiiviset arvot, bool, float ja numeromerkkijono hylätään. Myös adapterin
   uudelleenvalidointi hylkää kopioimalla ohitetun määrärajoituksen. Nykyistä
   domainin PartRequirement-mallia ei muuteta.
3. Tallennetun viitteen kanoninen nimi on historical_labour_approval.
   LabourApprovalRecord-luokan nimi ja tietosisältö säilyvät. Vanha WD-2:n
   labour_approval-avain hyväksytään vain luku-aliasena historialliselle kentälle.
   Samanaikaisesti annetut vanha ja uusi avain hylätään epäselvänä syötteenä.
   Serialisointi tuottaa uuden nimen; vanhaa snapshot.labour_approval-attribuuttia
   ei tarjota. Sovelluksen content_id ei muutu tämän nimeämisen vuoksi, sillä
   historiallinen viite ja työtarkistukset jätetään edelleen hashin ulkopuolelle.
4. Hyväksyntäporttien kielteiset testit kattavat muuttamattoman, muutetun ja
   nollatunteihin muutetun historiallisen päätöksen sekä täsmäävän että vanhan
   sovellusrevision. Myös kelvolliset nykyiset työtarkistukset ja sama domainin
   sisältötunniste jättävät adapterin tuloksen hyväksymättömäksi.

Yhteensopivuus: schema_version pysyy arvossa 2. Uusi lukija hyväksyy vanhan
WD-2:n avaimen; vanha WD-2-lukija ei hyväksy uutta kanonista kenttänimeä.
WD-2:n sisäisten kuluttajien tulee päivittää kentän käyttö. Nykyinen webapp ei
käytä tätä sopimusta, ja v1-luonnokset, HTTP-rajapinnat sekä legacy-hinnoittelu
pysyvät ennallaan. Sovelluskerroksen määrärajoitus tiukkenee tarkoituksella;
virheelliset vanhat WD-2-määrät vaativat korjausta, eivät automaattista muunnosta.

Kohdennettu testiajo projektin sisäisillä väliaikaispoluilla:

```powershell
.\.venv\Scripts\python.exe -m pytest -p tests.wd2_test_runtime -q tests/test_work_plan_contract.py
```

Tulos: **91 läpäissyttä testiä**, 0,98 s.

Koko regressiosarja samalla projektin sisäisellä testiapurilla:

```powershell
.\.venv\Scripts\python.exe -m pytest -p tests.wd2_test_runtime -q tests
```

Tulos: **2040 läpäissyttä testiä**, 1 aiemmin havaittu Starlette TestClientin
HTTPX-deprekaatiovaroitus, 813,00 s (13 min 33 s). Riippuvuuksia ei muutettu.
git diff --check onnistui; uusien, vielä untracked-tiedostojen whitespace
tarkistettiin lisäksi suoraan niiden sisällöstä. Adapteri, testiapuri ja
tehtävään kuulumattomat tarkastetut lähdetiedostot säilyivät muuttumattomina.

WD-5:n hyväksyntäsiltaa, uutta hyväksyntäprosessia, WD-3:n tallennusta tai
migraatioita ei toteuteta. WD-3:n suunnittelun avoimet elinkaarikysymykset ja
WD-1-aineiston vastaavuuden tarkistus säilyvät yllä kuvatulla tavalla.

## Testiriippuvuuksien erottelu (2026-10-14)

Tämä on testien rakenteellinen erottelu. Jäädytettyä WD-2/WD-2.1-sopimusta,
adaptereita, domainia, tuotantopalveluita tai liiketoimintasääntöjä ei muuteta.
QA-1-checkpoint säilyy muuttumattomana.

Siirretyt testit tiedostoon tests/test_webapp_draft_work_plan_integration.py:

- test_v1_drafts_keep_their_payload_shape_and_schema_version: suora riippuvuus
  webapp.draft_models-moduulista (DraftSnapshot, DraftType, editor_json).
- test_current_storage_labour_contract_has_identical_domain_projection:
  build_storage_labour_plan-importin epäsuora DRAFT-riippuvuus nykyisessä työpuussa:
  webapp.labour_service -> webapp.database -> webapp.draft_migrations.
  Sama importketju tuo myös commitoimattomia WORKSHOP-moduuleja.

Molempien testien nimet ja rungot odotuksineen säilyivät muuttumattomina.
Niissä ei ollut parametrisaatioita. WD-testimoduulista poistettiin vain nämä
kaksi testiä ja niiden kolme web-importtia: webapp.draft_models,
webapp.labour_models ja webapp.labour_service. Moduulin kuvaus päivitettiin.
Integraatiomoduuli käyttää alkuperäisiä snapshot-, time- ja work-apufunktioita
WD-testimoduulista; fixturelogiikkaa ei kopioida tai muuteta.

Staattinen jako on 29 WD-testifunktiota ja kaksi siirrettyä integraatiotestifunktiota,
yhteensä samat 31 kuin ennen erottelua. Tämä ei ole pytestin keräysmäärä.
Aiempien osioiden 56/2005- ja 91/2040-tulokset ovat historiallisia ennen erottelua
kirjattuja tuloksia, eivät tämän muutoksen tai eristetyn snapshotin ajovarmennusta.

WD-testimoduulin jäljelle jäävät projektiriippuvuudet ovat WD:n sopimus/adapterit
ja nykyiseen HEADiin kuuluvat domain- ja labour_planning/labour_pricing-moduulit.
Webin persistenssi-, DRAFT-, WORKSHOP- tai SEC-moduuleja ei importoida.

Tulevan WD-only-checkpointin täsmällinen tiedostorajaus:

- app/work_plan_contract.py
- app/work_plan_adapters.py
- tests/test_work_plan_contract.py
- tests/wd2_test_runtime.py
- docs/work-plan-contract-checkpoint-wd-2.md

Uusi tests/test_webapp_draft_work_plan_integration.py jää tämän listan ulkopuolelle
ja seuraamattomaksi myöhempään asianmukaiseen DRAFT-checkpointiin asti.
WORKSHOP-, DRAFT- tai SEC-tuotantotiedostoja ei tuoda WD-checkpointiin.

Kohdennettu nykytyöpuun ajokomento, jota yritettiin:

~~~powershell
.\.venv\Scripts\python.exe -B -m pytest -p no:cacheprovider -q tests/test_work_plan_contract.py tests/test_webapp_draft_work_plan_integration.py
~~~

Komento estyi ennen Python-prosessin luontia:

~~~text
exec_command failed: CreateProcess { message: "Rejected(\"Failed to create unified exec process: helper_unknown_error: setup refresh had errors\")" }
~~~

Codexin tässä historiallisessa yrityksessä pytestin keräystä ja testejä ei suoritettu;
exit-koodia tai ajotuloksia ei saatu. Erottelussa tehtiin staattinen testirunkojen
ja riippuvuuksien vertailu. Erottelun tekohetkellä nykytyöpuun integraatioajo ja
HEAD + WD -snapshotin varmennus olivat vielä avoimia, ja tila oli NEEDS REVIEW.
Myöhempi käyttäjävarmennus ja nykyinen checkpoint-tila kirjataan alla.
Pohja-HEAD on cdb872bcadd4730b72cbec5e4901ed889807f761.
Tämä erottelu ei anna staging-, commit-, tuotantokäyttö-, WD-3- tai SEC-2B-toteutuslupaa.

## Käyttäjän vahvistama erottelun jälkeinen varmennus (2026-10-14)

Seuraavat tulokset ovat käyttäjän raportoimia paikallisia testiajoja. Codex ei
suorittanut testejä tässä dokumentaatiopäivityksessä eikä väitä historiallisia
helper_unknown_error-käynnistysyrityksiä onnistuneiksi ajoiksi.

Nykyisen työpuun WD- ja erilliset integraatiotestit:

~~~powershell
.\.venv\Scripts\python.exe -B -m pytest -p no:cacheprovider -q tests/test_work_plan_contract.py tests/test_webapp_draft_work_plan_integration.py
~~~

Käyttäjän vahvistama tulos: 91 passed, 0.72 s. Tämä sisältää molemmat siirretyt
integraatiotestit ja varmentaa niiden toiminnan nykyisessä laajemmassa työpuussa.

Eristetty snapshot rakennettiin Git-archivesta täsmälleen HEAD-commitista
cdb872bcadd4730b72cbec5e4901ed889807f761. Käyttäjä lisäsi vain yllä luetellut
viisi WD-checkpointin tiedostoa ja varmisti seuraavien tiedostojen puuttumisen:

- webapp/draft_models.py
- webapp/draft_service.py
- webapp/workshop_context.py
- tests/test_webapp_draft_work_plan_integration.py

Snapshotin juuresta ajettiin repositoryn virtuaaliympäristön tulkilla:

~~~powershell
python -B -m pytest -p no:cacheprovider -p tests.wd2_test_runtime -q tests/test_work_plan_contract.py
~~~

Käyttäjän vahvistama tulos: 89 passed, 0.48 s. Eristetty ajovarmennus koskee vain
WD-testimoduulia, ei koko snapshotin regressiosarjaa. Täyttä eristettyä regressiota
ei ilmoitettu ajetuksi. Käyttäjä ei ilmoittanut näille ajoille exit-koodeja tai
varoitustietoja erikseen; niitä ei päätellä historiallisista ajoista.
Käyttäjän mukaan stagingia tai committia ei tehty.

Staattinen import-ketjun tarkistus tukee eristystä: sopimus, adapterit ja WD-testit
käyttävät vain WD:n omia moduuleja sekä HEADissa olevia domain- ja
labour_planning/labour_pricing-moduuleja. Riippuvuutta commitoimattomaan DRAFT-,
WORKSHOP- tai SEC-koodiin ei löytynyt. Työtarkistukset ja historiallinen labour-viite
eivät muutu hyväksynnäksi. Tarjouskooste, asiakashyväksyntä ja tarjousversioiden
säilytys eivät edelleenkään kuulu WD-2/WD-2.1-toteutukseen.

### Testiapurin sisällyttäminen ja eristyksen rajat

Päätös: tests/wd2_test_runtime.py kuuluu viiden tiedoston WD-checkpointiin. Se on
valinnainen, -p-valinnalla aktivoitava testiapuri, jota dokumentoidut ajot käyttävät.
Pelkkä tiedoston lisääminen ei muuta tavallista pytest-ajoa tai tuotantokoodia.

Apuri johtaa projektijuuren oman tiedostonsa ratkaistusta polusta, tarkistaa
.test-tmp-polun pysymisen sen alla ja luo uuden wd2-regression-alahakemiston.
Se muuttaa prosessin TEMP/TMP/TMPDIR-, tempfile.tempdir-, bytecode- ja pytestin
basetemp-asetukset. Vain uusi pytest-alahakemisto annetaan basetempiksi;
käyttäjän olemassa olevaa hakemistoa ei anneta pytestin siivottavaksi. Apuri ei
siivoa luomaansa scratch-hakemistoa ajon lopuksi eikä ole yleinen tiedostojärjestelmän
sandbox tai kaikkien mahdollisten testikirjoitusten esto.

Käyttäjän raportissa ei ole moduulien todellisten __file__-polkujen, sys.pathin
tai apurin luoman scratch-polun ajonaikaista lokivarmennusta. Snapshotista
suoritettu komento, puuttuvien DRAFT-tiedostojen tarkistus ja staattinen import-ketju
tukevat eristystä, mutta moduulien latauspolkuja ei ole itsenäisesti todennettu.
Lisävarmennuksessa tulee varmistaa sopimuksen, adapterien, testimoduulin ja
apurin latautuminen snapshotista sekä scratch-polun sijainti snapshotin alla.
Alkuperäiseen työpuuhun osoittava PYTHONPATH, .pth tai muu import-ohitus ei saa
korvata snapshotin moduuleja. Tätä lisävarmennusta ei suoritettu tässä tehtävässä.

Nykyinen tila: READY rajattuun WD-checkpointiin käyttäjän vahvistamien kohdennettujen
ajojen ja lähdekoodin riippuvuustarkistuksen perusteella. Täysi eristetty regressio
ja yllä kuvattu latauspolkujen ajonaikainen varmennus ovat erillisiä avoimia
varmennuksia; niitä ei merkitä tehdyiksi. Molemmat integraatiotestit säilyvät
tests/test_webapp_draft_work_plan_integration.py-tiedostossa WD-listan ulkopuolella
myöhempää DRAFT-checkpointia varten.

Tuotantovalmiutta ei hyväksytä. Jäädytetyt domain- ja WD-säännöt säilyvät;
WD-3:a tai SEC-2B:tä ei aloiteta. Staging ja commit tarvitsevat erillisen luvan.
