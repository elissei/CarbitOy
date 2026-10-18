# Carbit Oy – domain-vaihe 1 ja työaikalaskennan suunnitelma

Päivitetty 9.10.2026. Toteutuksen lähde:
[feature/autodata-labour, 6fb2e993](https://github.com/elissei/CarbitOy/tree/6fb2e9930e20235661aece6856c396d8db15a3a4).
Lähdehaaran testitulos: 158 läpäissyttä testiä; tätä dokumentointipäivitystä
varten testejä ei ajettu. Visuals-haaran koodia ei mergetty lähdehaarasta.

## 1. Toteutettu käsite- ja riippuvuuskartta

```mermaid
flowchart LR
  A["Axle: FRONT, REAR, BOTH, UNKNOWN"] --> W["WorkItem"]
  S["Side: LEFT, RIGHT, BOTH, NOT_APPLICABLE, UNKNOWN"] --> W
  O["Operation: BRAKE_REPLACEMENT / SHOCK_ABSORBER_REPLACEMENT"] --> W
  W --> D["side-oletus UNKNOWN: vanhat kutsut toimivat"]
  C["domain/config/pair_replacement.py"] --> R["Muuttumaton sääntö: shock_absorber + FRONT"]
  A --> R
  R --> Q["get_pair_replacement_rule: tarkka osatyyppi + akseli"]
  Q --> Y["Sääntö tai None: käytäntö määrittelemättä"]
```

Axle.BOTH tarkoittaa molempia akseleita, Side.BOTH saman akselin molempia
puolia. NOT_APPLICABLE ja UNKNOWN ovat eri tilanteita. Parivaihtokonfiguraatio
ei muuta WorkItem-oliota, varaosamäärää tai työaikaa. Se ei pakota parivaihtoa.
Muiden osatyyppien ja taka-akselin käytäntöjä ei oleteta.
Varaosien PAIR-yksikkö on erillinen määrä- ja pakkauskäsite.

Lähteet:
[Side](https://github.com/elissei/CarbitOy/blob/6fb2e9930e20235661aece6856c396d8db15a3a4/domain/models/enums.py),
[WorkItem](https://github.com/elissei/CarbitOy/blob/6fb2e9930e20235661aece6856c396d8db15a3a4/domain/models/work_item.py),
[parivaihtokonfiguraatio](https://github.com/elissei/CarbitOy/blob/6fb2e9930e20235661aece6856c396d8db15a3a4/domain/config/pair_replacement.py).

## 2. Suunniteltu LabourPlan — ei toteutettu

```mermaid
flowchart TD
  A["Yksilöidyt työt ja alkuperäiset Autodata-ajat"] --> B{"Saman operaation molemmat puolet?"}
  B -->|Kyllä| C{"Vastaava Autodata-pariaika löytyy?"}
  C -->|Ei| H["Ihmisen tarkistus: aikaa ei keksitä"]
  C -->|Kyllä| P["Valitse pariaika; puolien ajat jäävät alkuperätiedoiksi"]
  B -->|Ei| T["Erilliset ajat oletuksena"]
  P --> G["Muodosta työkohtainen aikaperusta"]
  T --> G
  G --> E{"Täsmällinen yhdistelmäaika valitaan?"}
  E -->|Kyllä| F["Korvaa sen kattamat aikaryhmät; säilytä alkuperätiedot"]
  E -->|Ei| N["Säilytä erilliset aikaryhmät"]
  F --> V["Varmista: jokainen työ laskennassa kerran; ei puuttuvia aikoja"]
  N --> V
  V --> SUM["Summaa tarkat Decimal-tunnit; ei automaattisia vähennyksiä"]
  SUM --> PRICE["Yksi verollinen työveloitus: tunnit × 90,00 EUR"]
  PRICE --> ROUND["ROUND_HALF_UP sentteihin; ALV 25,5 % erotellaan"]
```

Yhdistelmäaika voi kattaa osan töistä; ulkopuoliset työt lisätään erikseen.
Puuttuvaa pariaikaa ei ratkaista summaan siirtymällä. Suunniteltu lopullinen tarjous
edellyttää kaikkien vaadittujen työaikojen ratkaisemista.

Pyöristysehdotus: 1,3355 h + 1,3355 h = 2,6710 h → 240,39 EUR.
Työkohtaisesti pyöristettyjen hintojen 240,40 EUR summaa ei käytetä.
Nykyinen palvelu hinnoittelee yhden annetun ajan; tämä kaavio ei kuvaa
olemassa olevaa monen työn laskentaa.

## 3. Myöhemmät hyväksytyt työaikamuutokset — ei toteutettu

```mermaid
flowchart LR
  A["Alkuperäiset Autodata-ajat säilyvät"] --> B["Yrittäjä tai valtuutettu mekaanikko"]
  B --> C["Erillinen LabourTimeAdjustment"]
  C --> D["Kohdistus + uusi aika + peruste + tekijä + UTC-aikaleima"]
  D --> E["Hyväksytty aika laskentaan; aiemmat kirjaukset historiaan"]
```

Päällekkäiset purku- ja asennusvaiheet eivät vähennä aikaa automaattisesti.
Muutos tarvitsee perustelun ja valtuutetun hyväksyjän. Autodata-alkuperää
ei muuteta hyväksytyn poikkeaman lähteeksi. Työn tai aikaperustan muuttuminen
edellyttää muutoksen uudelleentarkistusta. MANUAL_AUTODATA-kirjaus,
HumanSelection ja nykyinen web-tapahtumahistoria eivät toteuta tätä hyväksyntää.

## Nykyisen käyttöpolun rajat

RawProposal ja FinalQuote kuvaavat edelleen yhden WorkItem-olion.
Demo on ennallaan ja hyväksyy yhden varaosavaatimuksen.
Tarjouspohjan tekstimuotoilija tuntee vain BRAKE_REPLACEMENT-operaation.
Uusi kohdepuoli ei vielä välity AI-työkuvauksen syötteeseen tai web-lomakkeeseen.
Domain-vaihe 1 ei ole valmis monen työn tarjous- tai Autodata-integraatio.

Katso [arkkitehtuuri](../../saaga-docs/ARCHITECTURE.md),
[käsitteet](../../saaga-docs/concepts/work-and-part-data.md),
[päätöslogiikka](../../saaga-docs/features/proposal-decisions.md) ja
[päätösloki](../notes/paatokset.md).
