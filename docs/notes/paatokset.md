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
