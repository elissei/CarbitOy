# Päätösloki

Kirjaa sovitut ratkaisut, niiden perusteet ja avoimet oletukset. Erota vahvistettu päätös ehdotuksesta. Yrittäjän liiketoimintasäännöt kirjataan vasta vahvistettuina tai selvästi avoimiksi merkittyinä.

## Kirjauspohja

### P-XXX – päätöksen nimi

- **Päivämäärä:**
- **Tila:** ehdotus / vahvistettu / korvattu
- **Päätös ja vahvistaja:**
- **Perustelu:**
- **Harkitut vaihtoehdot:**
- **Vaikutukset:**
- **Avoimet kysymykset:**
- **Lähteet / toteutuksen commit:**

## Päätökset

### P-004 – domain-vaiheen 1 dokumentoinnin lähde ja rajaus

- **Päivämäärä:** 2026-10-09
- **Tila:** vahvistettu, dokumentointipäivitys tarkistettavana
- **Päätös ja vahvistaja:** Käyttäjän ohje: päivitetään vain dokumentaatio visuals-haarassa. Lähde on feature/autodata-labour, commit 6fb2e9930e20235661aece6856c396d8db15a3a4.
- **Perustelu:** Domain- ja web-kehitys jatkuvat erillisissä haaroissa. Dokumentoinnin lähde ilmoitetaan täsmällisesti ilman koodin kopiointia tai mergeä.
- **Vaikutukset:** Olemassa oleva carbitoy-docs-worktree säilyy käytössä WSL:n kautta. Tämä tehtäväkohtainen käsipäivitys täydentää teknisiä dokumentteja; Saagan generoitu navigaatio ja BASELINE säilyvät ennallaan. Vanhoja PDF- ja PNG-kuvia ei esitetä uusina. Ei committia tai pushia ennen tarkistusta.
- **Lähteet:** [Domain-commit](https://github.com/elissei/CarbitOy/commit/6fb2e9930e20235661aece6856c396d8db15a3a4), [käsitekaavio](../visualizations/domain-vaihe-1.md).

### P-005 – kohdepuoli ja eksplisiittinen parivaihtokäytäntö

- **Päivämäärä:** 2026-10-09
- **Tila:** vahvistettu ja toteutettu
- **Päätös ja vahvistaja:** Käyttäjän hyväksymä vaihe 1: Side-arvot LEFT, RIGHT, BOTH, NOT_APPLICABLE ja UNKNOWN; WorkItem.side oletuksena UNKNOWN; SHOCK_ABSORBER_REPLACEMENT-operaatio.
- **Perustelu:** Puoli ja akseli ovat erillisiä käsitteitä. Vanhat kutsut säilyvät kelvollisina ilman puolitietoa.
- **Vaikutukset:** domain/config/pair_replacement.py sisältää muuttumattoman konfiguraation ja tarkan osatyyppi–akseli-haun. Vain etuiskunvaimentimien käytäntö on vahvistettu. Puuttuva sääntö merkitsee määrittelemätöntä käytäntöä, ei parivaihdon kieltoa. Sääntö ei muuta työn laajuutta, varaosamääriä tai Autodata-aikaa.
- **Avoimet kysymykset:** Muiden osatyyppien ja taka-akselin käytännöt tarvitsevat myöhemmän hyväksynnän. Puolitiedon esitys, shock absorber -tekstimuotoilu ja web-liittäminen eivät kuulu vaiheeseen 1.
- **Lähteet / toteutuksen commit:** 6fb2e993; 158 läpäissyttä testiä lähdehaarassa. RawProposal, FinalQuote, web-sovellus, hinnoittelu ja jäädytetty demo säilyivät muuttumattomina.

### P-006 – LabourPlan-työaikalaskennan hyväksytyt päälinjat

- **Päivämäärä:** 2026-10-09
- **Tila:** liiketoimintasäännöt vahvistettu; toteutus suunniteltu, ei valmis
- **Päätös ja vahvistaja:** Käyttäjän vahvistamat säännöt: saman operaation oma Autodata-pariaika korvaa puolien ajat. Jos pariaikaa ei löydy, aikaa ei keksitä ja työ jää tarkistettavaksi. Eri töiden erilliset ajat summataan oletuksena. Päällekkäisiä vaiheita ei vähennetä automaattisesti. Täsmällinen Autodata-yhdistelmäaika voidaan valita kattamiensa töiden tilalle alkuperätietoineen.
- **Perustelu:** Autodatan alkuperä ja laskennan kattavuus säilyvät tarkistettavina; jokainen työ lasketaan kerran.
- **Vaikutukset:** WorkPlan/LabourPlan ja aikaryhmien valinta ovat tulevaa toteutusta. Normaali veloitus säilyy 90,00 EUR/h sisältäen ALV 25,5 %. Nykyinen parivaihtokonfiguraatio ei toteuta näitä laskentasääntöjä.
- **Pyöristyksen suunnitteluratkaisu:** Tarkat Decimal-tunnit summataan, yksi kokonaistyöveloitus pyöristetään ROUND_HALF_UP sentteihin ja ALV erotellaan verollisesta summasta nykyisellä palvelulla. Kaksi 1,3355 tunnin työtä tuottaa 240,39 EUR, ei erikseen pyöristettyjen hintojen 240,40 EUR summaa. Tämä on dokumentoitu suunnitelma, ei toteutettu monen työn hinnoittelu.
- **Avoimet kysymykset:** Rakenteet, yhdistelmäajan kattavuuden tarkistus ja useiden töiden tarjouspolku toteutetaan erikseen. Puuttuvaa vaadittua aikaa ei esitetä nollana tai valmiina tarjouksena.
- **Lähteet:** Käyttäjän tässä kehityskeskustelussa vahvistamat kahdeksan liiketoimintasääntöä ja hyväksytty domain-suunnitelman päälinja; [suunniteltu kaavio](../visualizations/domain-vaihe-1.md).

### P-007 – ihmisen hyväksymä työaikamuutos

- **Päivämäärä:** 2026-10-09
- **Tila:** liiketoimintasääntö vahvistettu; myöhempi toteutus, ei valmis
- **Päätös ja vahvistaja:** Yrittäjä tai valtuutettu mekaanikko voi hyväksyä perustellun työaikamuutoksen. Muutos säilytetään erillisenä tietona: kohdistus, uusi absoluuttinen aika, alkuperäinen peruste, perustelu, tekijä ja aikavyöhyketietoinen UTC-aikaleima.
- **Perustelu:** Alkuperäisiä Autodata-aikoja ei ylikirjoiteta; laskutettava muutos on jäljitettävä.
- **Vaikutukset:** Tuleva LabourTimeAdjustment säilyttää myös aiemmat muutokset. Uusi hyväksyntä korvaa aiemman laskennassa, ei historiassa. Puuttuvan ajan ratkaisulle ei keksitä aiempaa aikaa. Työn laajuuden tai aikaperustan muutos edellyttää uudelleentarkistusta. Valtuutus tarkistetaan sovelluskerroksessa.
- **Avoimet kysymykset:** Tallennus ja valtuutettu hyväksyntä lisätään myöhemmin. MANUAL_AUTODATA tarkoittaa käsin kirjattua Autodata-aikaa, ei yrittäjän poikkeamaa. HumanSelection koskee varaosavalintaa. Nykyisen paikallisen web-työjonon tapahtumahistoria ei ole työaikamuutosten hyväksyntä eikä siinä ole kirjautumista.
- **Lähteet:** Käyttäjän vahvistamat työaikamuutossäännöt; [arkkitehtuuri](../../saaga-docs/ARCHITECTURE.md).

### P-003 – web-version dokumentoinnin lähde ja rajaus

- **Päivämäärä:** 2026-10-09
- **Tila:** vahvistettu
- **Päätös ja vahvistaja:** Elissein tehtäväkohtaisen ohjeen mukaan web-sovellus dokumentoidaan olemassa olevassa `carbitoy-docs`-worktreessä `visuals`-haaralla. Lähde on `feature/web-app`, tavoitecommit `5be66f9f8978ae29e16f0eb2754a5220c8a4fcc9`; tämä on poikkeus dokumentointiohjeen `main`-lähdehaaraan, ei yleinen haarakäytännön muutos.
- **Perustelu:** Yksi varsinainen dokumentointirakenne säilyttää teknisen kuvauksen, päiväkirjan ja päätösten taustan yhdessä. Tavallinen merge tuo dokumentoitavan koodin Saagan nähtäväksi häiritsemättä muiden worktreeiden kehitystä.
- **Harkitut vaihtoehdot:** Uuden dokumentointirakenteen luominen web-worktreehen ja dokumenttien kopiointi haarojen välillä jätettiin pois. `main` ei sisällä tässä tehtävässä dokumentoitavaa web-toteutusta.
- **Vaikutukset:** Git- ja Saaga-komennot ajetaan WSL:ssä. Saaga ylläpitää teknistä dokumentaatiota, tekijä päiväkirjaa ja päätöslokia; generoituun navigaatioon ei tehdä käsimuutoksia. Kuvataan SQLite, atominen kirjoitus, yhtenäinen lukutransaktio, 409-versio- ja sisältöristiriidat, pysyvä luontipyynnön idempotenssi, uusien tapahtumien `work_version`, vanhat tiedot säilyttävä migraatio, selaimen kilpailusuojaus ja odotettavien laskentavirheiden 422-vastaukset. Dokumentointimuutokset commitoidaan vain paikallisesti; ei pushia eikä julkaisua.
- **Avoimet kysymykset:** Nykyinen web-versio on paikalliskäyttöinen eikä siinä ole kirjautumista. Autodata-työaika syötetään manuaalisesti. Pysyvä pilvitietokanta, verkkokäytön suojaus ja Vercel-julkaisu ovat ratkaisematta; tämä päätös ei hyväksy tai toteuta niitä. Työkustannus ei ole hyväksytty lopullinen tarjous.
- **Lähteet / toteutuksen commit:** Korjaus `5be66f9f8978ae29e16f0eb2754a5220c8a4fcc9`, lähdemerge `541ffec5ffa6e25dff0143ab40f2a0876e1c038d` ja [kehityspäiväkirja](kehityspaivakirja.md). Korjauscommitin tarkistus: 151 läpäissyttä testiä, mukana kuusi Chromium-selaintestiä ja vanhan tietokantaskeeman yhteensopivuustesti.

### P-001 – dokumentointijärjestelmä

- **Päivämäärä:** 2026-10-05
- **Tila:** vahvistettu
- **Päätös ja vahvistaja:** Elissei hyväksyi Saagan teknisen dokumentaation ylläpitoon ja Obsidianin saman repon dokumentaation ja omien muistiinpanojen käyttöliittymäksi.
- **Perustelu:** Yhteiset Markdown-tiedostot palvelevat sekä päivittäistä kehitystä että GitHub-portfoliota ja myöhempää opinnäytetyötä.
- **Vaikutukset:** Saaga ylläpitää `saaga-docs/`-kansiota. Tekijä ylläpitää `docs/notes/`-kansiota. Visualisoinnit päivitetään erikseen.

### P-002 – dokumentoinnin erillinen worktree

- **Päivämäärä:** 2026-10-05
- **Tila:** vahvistettu
- **Päätös ja vahvistaja:** Elissein valitsemaa `visuals`-branchia käytetään erillisessä `carbitoy-docs`-worktreessä; koodikehitys jatkuu `main`-branchilla.
- **Perustelu:** Branchin vaihtaminen yhteisessä työkansiossa häiritsisi rinnakkaista koodikehitystä.
- **Vaikutukset:** Uudet koodimuutokset tuodaan dokumentointibranchiin ennen Saaga-päivitystä. Worktreen Git-komennot ajetaan WSL:ssä.
- **Avoimet kysymykset:** Milloin dokumentaatiomuutokset yhdistetään `main`-branchiin GitHub-portfolion oletusnäkymää varten?
