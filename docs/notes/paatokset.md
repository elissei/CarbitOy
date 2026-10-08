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
