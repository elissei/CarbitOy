# Carbit Oy – nykyiset työnkulut ja päätösketjut

Tilannekuva 6.10.2026, lähdekoodi [`4de26b4`](https://github.com/elissei/CarbitOy/tree/4de26b4284280fdd8c3867e01e8c38bcad718115). Kaaviot renderöityvät GitHubissa ja Obsidianissa. Vain toteutettu logiikka; toimittajatilaukset, CRM, Autodata ja selain-UI eivät kuulu tähän tilannekuvaan.

> Tämä sivu säilyttää 6.10.2026 työnkulut. Uudempi domain-toteutus
> ja suunnitelmien tilat: [Domain-vaihe 1](domain-vaihe-1.md).
> Uusi parivaihtokonfiguraatio ei lisää Autodata-aikavalintaa näihin ketjuihin.

## Kokonaisketju

```mermaid
flowchart TD
  A["Asiakkaan viesti"] --> B["Fake- tai OpenAI-tulkitsija"]
  B --> C{"HUMAN_REQUIRED?"}
  C -->|Kyllä| H["Ihmisen käsittely: ei ehdotusta"]
  C -->|Ei| D["READY: työ ja varaosatarve"]
  D --> E["Ehdotus: ehdokkaat ja työn hinta"]
  E --> F["Ihminen valitsee ehdokkaan"]
  F --> G["Erillinen finalize_demo_quote-kutsu"]
  G --> I["Valinnan tarkistus ja hinnoittelu"]
  I --> J["FinalQuote"]
  J --> K["Hintateksti"]
  J --> L["Valinnainen AI-työkuvaus"]
```

Kutsuja antaa ajoneuvon, työajan, tuotteet ja toimittajatarjoukset. Demo hyväksyy yhden varaosatarpeen. Hintateksti ja työkuvaus pyydetään erikseen; demo ei yhdistä niitä tai lähetä tarjousta asiakkaalle. Poikkeukset välittyvät kutsujalle.

## 13 · Viestin tulkinta ja ihmishaara

`process_customer_message() ja OpenAI-tulkinnan validointi`

```mermaid
flowchart TD
  N0["Kutsu interpreter.interpret(message)"]
  N0 -->|Virhe| E0["Kutsuvirhe välittyy ulos"]
  N1["OpenAI-polku: JSON-objekti + Pydantic / Fake-polku: valmis tulkinta"]
  N0 --> N1
  N1 -->|Virhe| E1["JSON- tai validointivirhe / Ei intake-tulosta"]
  N2{"status == HUMAN_REQUIRED?"}
  N1 --> N2
  N2 -->|Kyllä| E2["Kyllä: work_item = None / Ei varaosatarpeen muuntoa"]
  N3["READY: muunna tulkinta työtehtäväksi"]
  N2 -->|Ei| N3
  N3 -->|Virhe| E3["Muuntovirhe välittyy ulos"]
  N4["Palauta CustomerIntakeResult / Tulkinta + työtehtävä"]
  N3 --> N4
```

READY vaatii työn ja vähintään yhden osatyypin. Diagnoositarve vaatii HUMAN_REQUIRED-tilan. AI-prompti ohjaa epävarmat pyynnöt ihmiselle, mutta jokaista promptiehtoa ei validoida koodissa. Virheitä ei muuteta automaattisesti ihmishaaraksi.

Lähteet: [app/customer_intake_workflow.py](https://github.com/elissei/CarbitOy/blob/4de26b4284280fdd8c3867e01e8c38bcad718115/app/customer_intake_workflow.py), [domain/models/customer_request.py](https://github.com/elissei/CarbitOy/blob/4de26b4284280fdd8c3867e01e8c38bcad718115/domain/models/customer_request.py), [integrations/ai/openai_customer_request_interpreter.py](https://github.com/elissei/CarbitOy/blob/4de26b4284280fdd8c3867e01e8c38bcad718115/integrations/ai/openai_customer_request_interpreter.py).

## 14 · Tulkinnasta varaosatarpeeksi

`build_work_item_from_customer_request() · jokainen osatyyppi käsitellään`

```mermaid
flowchart TD
  N0{"Onko tulkinta READY?"}
  N0 -->|Ei| E0["Ei: ValueError"]
  N1{"Onko operation annettu?"}
  N0 -->|Kyllä| N1
  N1 -->|Ei| E1["Ei: ValueError"]
  N2{"Jokaiselle part_types-alkiolle: löytyykö yksikkö? / Tuettu avain: brake_pad"}
  N1 -->|Kyllä| N2
  N2 -->|Ei| E2["Ei: ValueError / Ei osittaista tulosta"]
  N3["Lisää tarve: tyyppi + akseli / required_quantity = 1, required_unit = SET"]
  N2 -->|Kyllä| N3
  N4["Kaikki osatyypit käsitelty → palauta WorkItem / Työ + akseli + muodostetut tarpeet"]
  N3 --> N4
```

Jokaisesta brake_pad-esiintymästä tulee oma tarve; duplikaatteja ei poisteta. UNKNOWN-akseli kopioidaan sellaisenaan. Demo hyväksyy myöhemmin vain yhden tarpeen. Suoraan kutsuttu muuntaja ei itse tarkista osatyyppilistan tyhjyyttä.

Lähteet: [domain/rules/customer_request_work_item.py](https://github.com/elissei/CarbitOy/blob/4de26b4284280fdd8c3867e01e8c38bcad718115/domain/rules/customer_request_work_item.py), [domain/models/customer_request.py](https://github.com/elissei/CarbitOy/blob/4de26b4284280fdd8c3867e01e8c38bcad718115/domain/models/customer_request.py), [domain/models/part_requirement.py](https://github.com/elissei/CarbitOy/blob/4de26b4284280fdd8c3867e01e8c38bcad718115/domain/models/part_requirement.py).

## 15 · Intake yhdistyy ehdotukseen

`process_demo_customer_message() → create_demo_proposal()`

```mermaid
flowchart TD
  N0["Kutsu process_customer_message()"]
  N0 -->|Virhe| E0["Tulkinta- tai muuntovirhe / Välittyy kutsujalle"]
  N1{"Onko intake.work_item olemassa?"}
  N0 --> N1
  N1 -->|Ei| E1["Ei: palauta intake / proposal = None"]
  N2{"Kaikki kuusi ehdotussyötettä annettu? / Yksikään ei saa olla None"}
  N1 -->|Kyllä| N2
  N2 -->|Ei| E2["Ei: ValueError"]
  N3{"Onko varaosatarpeita täsmälleen yksi?"}
  N2 -->|Kyllä| N3
  N3 -->|Ei| E3["Ei: ValueError"]
  N4["Työkustannus = tunnit × 90 EUR / Muodosta varaosaehdokkaat"]
  N3 -->|Kyllä| N4
  N4 -->|Virhe| E4["Validointi- tai sääntövirhe / Välittyy kutsujalle"]
  N5["Palauta intake + RawProposal / Ei lopullista tarjousta tai osavalintaa"]
  N4 --> N5
```

Kuusi syötettä: proposal_id, vehicle, labour_time, products, offers, created_at. Tyhjät tuote- ja tarjouslistat sallitaan: ehdokaslista voi olla tyhjä. Demo ei lajittele ehdokkaita eikä hae työaikaa tai tarjouksia itse.

Lähteet: [app/demo_intake_workflow.py](https://github.com/elissei/CarbitOy/blob/4de26b4284280fdd8c3867e01e8c38bcad718115/app/demo_intake_workflow.py), [app/demo_workflow.py](https://github.com/elissei/CarbitOy/blob/4de26b4284280fdd8c3867e01e8c38bcad718115/app/demo_workflow.py), [domain/rules/labour_cost.py](https://github.com/elissei/CarbitOy/blob/4de26b4284280fdd8c3867e01e8c38bcad718115/domain/rules/labour_cost.py).

## 16 · Ihmisen valinnan tarkistukset

`finalize_demo_quote() → create_demo_final_quote() → select_candidate_from_proposal()`

```mermaid
flowchart TD
  N0["Ihminen antaa ehdokkaan, valitsijan ja ajan / Kutsuja käynnistää erillisen viimeistelyn"]
  N1{"Kuuluuko ehdokas proposal.candidates-listaan?"}
  N0 --> N1
  N1 -->|Ei| E1["Ei: ValueError"]
  N2{"Onko proposal_id ei-tyhjä strip()-käsittelyn jälkeen?"}
  N1 -->|Kyllä| N2
  N2 -->|Ei| E2["Ei: ValueError"]
  N3{"Onko selected_by ei-tyhjä strip()-käsittelyn jälkeen?"}
  N2 -->|Kyllä| N3
  N3 -->|Ei| E3["Ei: ValueError"]
  N4["Muodosta HumanSelection / Ehdokkaan tiedot + valitsija + valinta-aika"]
  N3 -->|Kyllä| N4
  N5["Kutsu create_final_quote() / Hinnoittelu ja mallin validointi"]
  N4 --> N5
```

Jäsenyys tarkistetaan listan in-operaatiolla eli mallien yhtäsuuruudella. Tarkistus ei todenna valitsijan henkilöllisyyttä. Valinta tallennetaan palautettavaan malliin; pysyvää tietokantatallennusta tai toimittajan ostotilausta ei tehdä.

Lähteet: [app/demo_intake_workflow.py](https://github.com/elissei/CarbitOy/blob/4de26b4284280fdd8c3867e01e8c38bcad718115/app/demo_intake_workflow.py), [app/demo_workflow.py](https://github.com/elissei/CarbitOy/blob/4de26b4284280fdd8c3867e01e8c38bcad718115/app/demo_workflow.py), [domain/rules/human_selection.py](https://github.com/elissei/CarbitOy/blob/4de26b4284280fdd8c3867e01e8c38bcad718115/domain/rules/human_selection.py).

## 17 · Lopullisen tarjouksen hinnoittelu

`create_final_quote() · laskenta tehdään domain-säännöissä`

```mermaid
flowchart TD
  N0{"Onko valinnan total_price > 0?"}
  N0 -->|Ei| E0["Ei: ValueError"]
  N1["Varaosan myyntihinta = ostohinta × 1.30 / Pyöristä 0.01: ROUND_HALF_UP"]
  N0 -->|Kyllä| N1
  N2["Tarjouksen summa = työn hinta + varaosan myyntihinta"]
  N1 --> N2
  N3{"FinalQuote: ehdotustunnus ja ostohinta täsmäävät?"}
  N2 --> N3
  N3 -->|Ei| E3["Ei: validointivirhe"]
  N4{"Valuutat samat ja kokonaissumma oikein?"}
  N3 -->|Kyllä| N4
  N4 -->|Ei| E4["Ei: validointivirhe"]
  N5["Palauta FinalQuote / Valinta + hinnat + ajoneuvo + työ + aikaleima"]
  N4 -->|Kyllä| N5
```

Kerroin 1.30 on 30 prosentin lisä ostohintaan, ei 30 prosentin myyntikate. Käytetään valinnan kokonaisostohintaa, ei yksikköhintaa. Tässä ketjussa ei lasketa erillistä ALV-erittelyä. Malli tarkistaa summan mutta ei itse uudelleenlaske kerrointa.

Lähteet: [domain/rules/part_sale_price.py](https://github.com/elissei/CarbitOy/blob/4de26b4284280fdd8c3867e01e8c38bcad718115/domain/rules/part_sale_price.py), [domain/rules/final_quote.py](https://github.com/elissei/CarbitOy/blob/4de26b4284280fdd8c3867e01e8c38bcad718115/domain/rules/final_quote.py), [domain/models/final_quote.py](https://github.com/elissei/CarbitOy/blob/4de26b4284280fdd8c3867e01e8c38bcad718115/domain/models/final_quote.py).

## Esittäminen ja AI-raja

```mermaid
flowchart TD
  Q["FinalQuote"] --> T["format_final_quote: hintateksti"]
  Q --> P["present_final_quote: esitystiedot"]
  P --> C{"Presenter-toteutus?"}
  C -->|Fake| F["Annettu työkuvaus"]
  C -->|OpenAI| A["Lähetä työ, akseli ja osatyypit"]
  A --> B{"Vastaus tyhjä trimmaamisen jälkeen?"}
  B -->|Kyllä| E["ValueError"]
  B -->|Ei| D["Työkuvaus"]
  F --> O["QuotePresentationOutput"]
  D --> O
```

AI-työkuvaukseen ei lähetetä ajoneuvon tunnuksia, hintoja tai työaikaa. Hintateksti tulostaa euron ja ALV-merkinnän kiinteästi; tässä ketjussa ei tehdä erillistä verolaskentaa. Kutsuja yhdistää hintatekstin ja työkuvauksen. Verkkovirheet välittyvät ulos, eikä retry- tai fallback-haaraa ole.
