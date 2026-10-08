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

### 2026-10-09 – paikallisen web-työjonon ja luotettavuuskorjausten dokumentointi

- **Tavoite:** Päivittää olemassa oleva Saaga-dokumentaatio paikallisen web-sovelluksen toteutukseen ilman rinnakkaisen dokumentointirakenteen luomista.
- **Lähtötilanne / koodin commit:** Dokumentoitava lähde on `feature/web-app`, commit `5be66f9f8978ae29e16f0eb2754a5220c8a4fcc9`. Docs-worktree oli puhdas `visuals`-haaralla commitissa `eecd6a1`; WSL:n Git toimi, joten Windowsin `prunable`-merkintä ei edellyttänyt metatietojen korjaamista.
- **Tehdyt muutokset:** Lähdehaara tuotiin tavallisella mergellä `541ffec5ffa6e25dff0143ab40f2a0876e1c038d`. Ainoa konflikti oli `.gitignore`: molempien haarojen tietokanta-, ympäristö-, Saaga- ja Obsidian-ohitukset säilytettiin. Tekninen dokumentaatio päivitettiin WSL:ssä komennolla `saaga run update --backend codex`; ajo valmistui onnistuneesti, 10 vaihetta / 11 min 31 s. Arkkitehtuuri, työaikamallien kuvaus ja web-työjonon kuvaus päivitettiin, ja Saaga generoi baselinen sekä navigaation. Lopputarkistuksessa web-kuvaukseen täsmennettiin paikalliskäytön rajat, avoimet julkaisu- ja tallennusratkaisut, 422-virhepolku ja selaimen lomakesidonta; generoituun navigaatioon ei tehty käsimuutoksia.
- **Perustelut ja vaihtoehdot:** Tässä tehtävässä lähdehaara on käyttäjän hyväksymänä poikkeuksena `feature/web-app`, ei yleisohjeen `main`. Olemassa oleva `visuals`/`carbitoy-docs` säilyy dokumentoinnin paikkana; koodikehityksen worktreeitä ei muuteta.
- **Tarkistus ja tulos:** Korjauscommitin aiemmassa paikallisessa web-worktreen tarkistuksessa 151 testiä läpäisi. Mukana oli kuusi Chromium-selaintestiä sekä erillinen vanhan SQLite-skeeman yhteensopivuustesti: olemassa olevat tiedot ja tapahtumat säilyivät, vanhojen tapahtumien `work_version` jäi tyhjäksi (`NULL`). Tulos on korjauscommitin testitulos, ei tämän dokumentointiajon uusi testiajo.
- **Havainnot / avoimet kysymykset:** Toteutus käyttää paikallista SQLite-tallennusta, atomisia muutos- ja tapahtumakirjoituksia, yhteistä lukutransaktiota, versionumerolla suojattuja päivityksiä ja pysyvää luontipyynnön idempotenssiavainta. Uudet tapahtumat sisältävät muutoksen jälkeisen version; migraatio ei keksi vanhoille tapahtumille versioita. Selain torjuu myöhäiset vastaukset ja päällekkäiset lähetykset. Odotettavat työaikalaskennan virheet palauttavat 422:n muuttamatta työtä tai historiaa.
- **Seuraava askel:** Käyttö pysyy paikallisena ilman kirjautumista; Autodata-työaika kirjataan manuaalisesti. Vercel-julkaisu ja pysyvä pilvitietokanta ovat avoimia ratkaisuja, eivät toteutettuja integraatioita. Mahdollinen verkkotestaus ja julkaisu edellyttävät erillistä päätöstä suojaus- ja tallennusratkaisusta.
- **Linkit / tuloksen commit:** Korjaus `5be66f9f8978ae29e16f0eb2754a5220c8a4fcc9`; lähdemerge `541ffec5ffa6e25dff0143ab40f2a0876e1c038d`; [paikallinen web-työjono](../../saaga-docs/features/local-work-queue.md); [dokumentointipäätös P-003](paatokset.md#p-003--web-version-dokumentoinnin-lähde-ja-rajaus). Dokumentointicommit yksilöidään tämän merkinnän Git-historiassa. Ei pushia tai julkaisua.

### 2026-10-05 – dokumentointijärjestelmän käyttöönotto

- **Tavoite:** Koota tekninen dokumentaatio, visualisoinnit ja omat muistiinpanot samaan versionhallittuun kokonaisuuteen.
- **Lähtötilanne:** Koodin commit `1a02bfb`; dokumentointiin luotiin erillinen `visuals`-worktree.
- **Tehdyt muutokset:** Saaga otettiin käyttöön WSL:ssä Codex-backendilla. Ensimmäinen dokumentointiajo loi teknisen dokumentaation, baseline-tiedoston ja `AGENTS.md`-ohjeen. Dokumentointikansio avattiin Obsidian-holviksi Windowsissa.
- **Perustelut:** Erillinen worktree mahdollistaa koodikehityksen ja dokumentoinnin rinnakkain. Saaga ylläpitää teknistä kuvausta, omat muistiinpanot säilyttävät tavoitteet ja päätösten taustan.
- **Tarkistus ja tulos:** Saagan Codex-yhteensopivuustarkistus onnistui. Init-ajo valmistui: 15 vaihetta, 32 min 13 s. Git-status osoitti muutoksiksi dokumentaation, agenttiohjeen ja Saagan ajokansion ohituksen.
- **Havainto:** npm:n `1.0.0-alpha.7`-paketissa ei ollut Codex-tukea. Saaga rakennettiin lähdekoodin commitista `c3570d8739255cf381c60b8f029f7fc4ba52e905`; versionumero yksin ei yksilöi käytettyä toteutusta.
- **Seuraava askel:** Täydentää päiväkirjaa ja päätöslokia kehityksen edetessä sekä päivittää tekninen dokumentaatio koodimuutosten jälkeen.
- **Tulos:** [Ensimmäinen Saaga-dokumentaatio, commit b1fdfaf](https://github.com/elissei/CarbitOy/commit/b1fdfaf).
