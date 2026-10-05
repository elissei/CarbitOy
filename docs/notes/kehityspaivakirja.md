# Kehityspäiväkirja

Kirjaa olennaiset työvaiheet ja niiden perustelut. Liitä mukaan commit, testi tai dokumentti, jonka perusteella tulos voidaan myöhemmin tarkistaa. Kopioi pohja uuden merkinnän alkuun.

## Kirjauspohja

### YYYY-MM-DD – työvaiheen nimi

- **Tavoite:**
- **Lähtötilanne / koodin commit:**
- **Tehdyt muutokset:**
- **Perustelut ja vaihtoehdot:**
- **Tarkistus ja tulos:**
- **Havainnot / avoimet kysymykset:**
- **Seuraava askel:**
- **Linkit / tuloksen commit:**

## Merkinnät

### 2026-10-05 – dokumentointijärjestelmän käyttöönotto

- **Tavoite:** Koota tekninen dokumentaatio, visualisoinnit ja omat muistiinpanot samaan versionhallittuun kokonaisuuteen.
- **Lähtötilanne:** Koodin commit `1a02bfb`; dokumentointiin luotiin erillinen `visuals`-worktree.
- **Tehdyt muutokset:** Saaga otettiin käyttöön WSL:ssä Codex-backendilla. Ensimmäinen dokumentointiajo loi teknisen dokumentaation, baseline-tiedoston ja `AGENTS.md`-ohjeen. Dokumentointikansio avattiin Obsidian-holviksi Windowsissa.
- **Perustelut:** Erillinen worktree mahdollistaa koodikehityksen ja dokumentoinnin rinnakkain. Saaga ylläpitää teknistä kuvausta, omat muistiinpanot säilyttävät tavoitteet ja päätösten taustan.
- **Tarkistus ja tulos:** Saagan Codex-yhteensopivuustarkistus onnistui. Init-ajo valmistui: 15 vaihetta, 32 min 13 s. Git-status osoitti muutoksiksi dokumentaation, agenttiohjeen ja Saagan ajokansion ohituksen.
- **Havainto:** npm:n `1.0.0-alpha.7`-paketissa ei ollut Codex-tukea. Saaga rakennettiin lähdekoodin commitista `c3570d8739255cf381c60b8f029f7fc4ba52e905`; versionumero yksin ei yksilöi käytettyä toteutusta.
- **Seuraava askel:** Täydentää päiväkirjaa ja päätöslokia kehityksen edetessä sekä päivittää tekninen dokumentaatio koodimuutosten jälkeen.
- **Tulos:** [Ensimmäinen Saaga-dokumentaatio, commit b1fdfaf](https://github.com/elissei/CarbitOy/commit/b1fdfaf).
