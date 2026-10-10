# Python-ympäristö: Web 4E-1

Tämä vaihe erottaa riippuvuudet ja valmistelee niiden lukituksen. Se ei asenna
tuotantopalvelinta tai tee nykyisestä sovelluksesta tuotantoturvallista.
Tunnistus, MFA, API-suojaus ja OpenClaw kuuluvat myöhempiin hyväksyttäviin vaiheisiin.

## Python ja varmennuksen tila

Tuotannon tavoite on **Ubuntu Server 24.04 LTS, AMD64 ja CPython 3.12.x**.
Python 3.12 on [Ubuntu 24.04:n oletusversio](https://documentation.ubuntu.com/release-notes/24.04/).
Käytä Ubuntun ylläpitämää, tietoturvapäivitettyä pakettia ja kirjaa tarkka
Python- ja Ubuntu-pakettiversio julkaisutietoihin. Älä vaihda järjestelmän Pythonia
tai asenna sovelluksen kirjastoja siihen; käytä erillistä virtuaaliympäristöä.

Nykyinen Windows-kehitysympäristö on CPython **3.13.5 / AMD64**. Sitä ei muuteta
tässä vaiheessa. Tuotannon 3.12-suositus ei vielä ole yhteensopivuuslupaus:
Ubuntu-lukitus, puhdas asennus ja koko testisarja on varmennettava siellä.
Windows-lukituksia ei saa käyttää Ubuntu-tuotannon varmennettuina lukituksina.

Tämän vaiheen Windows-varmennus (CPython 3.13.5): nykyinen `.venv` ja puhtaasta
dev-hash-lukituksesta asennettu erillinen ympäristö läpäisivät kumpikin **356 testiä**.
Molemmissa oli sama Starletten HTTPX-TestClientin käytöstä antama deprekaatiovaroitus;
sen vuoksi ei päivitetty paketteja. Puhdas runtime-asennus läpäisi `pip check`:n
sekä paikallisen käynnistys-, HTML-, API-, migraatio- ja historiasavutestin:
1,60 h → 144,00 €, veroton 114,74 € ja ALV 29,26 €. Runtime-ympäristössä ei ollut
OpenAI SDK:ta, HTTPX:ää, pytestiä, Playwrightia tai pip-toolsia.
Lukitusten sovellusversiot vastaavat ennestään käytetyn `.venv`:n versioita.
Kaikki 119 Python-lähde-/testitiedostoa läpäisivät myös Python 3.12:n kieliopin
staattisen tarkistuksen; tämä ei korvaa natiivin Python 3.12:n ajonaikaisia testejä.

## Riippuvuuksien ryhmät

| Lähde | Sisältö | Käyttö |
| --- | --- | --- |
| `requirements/runtime.in` | FastAPI, Pydantic, PyYAML, Uvicorn standard-lisäosineen | Web-palvelu ja sen domain/ontologia |
| `requirements/ai.in` | Runtime ja OpenAI SDK | Valinnainen nykyisten AI-adapterien ympäristö |
| `requirements/dev.in` | AI-ryhmä, HTTPX, pytest ja Playwright | Koko testisarja, myös mockatut AI- ja Chromium-testit |
| `requirements/tools.in` | pip, pip-tools ja lukitustyökalujen riippuvuudet | Erillinen lukitusten generointiympäristö |
| `requirements/baseline.txt` | Nykyisessä `.venv`:ssä havaitut tarkat sovellus-/testipakettiversiot | Resolverin rajoitteet, ei asennuslista tai Ubuntu-lukitus |
| `requirements/locks/windows-amd64-py313-*.txt` | Kokonaiset Windows-kohtaiset riippuvuusketjut ja SHA-256-tiivisteet | Windows-asennukset |
| `requirements-web.txt` | Viittaus Windowsin dev-lukitukseen | Vanhan PowerShell-asennuskomennon yhteensopivuus |

Web 4E-2:n tunnistusperusta lisää runtimeen argon2-cffi:n, cryptographyn ja PyOTP:n.
Windows-lukitukset sisältävät myös niiden tarkat välilliset riippuvuudet;
aiemmat pakettiversiot säilyvät. [Tunnistuksen rajat ja käyttömenettely](authentication-foundation.md).
Yllä kirjattu 356 testin varmennus koskee Web 4E-1:n versiota ennen tätä lisäystä.

Pydantic ja PyYAML on nyt määritelty suoriksi riippuvuuksiksi, koska Python-koodi
tuo ne itse. SQLite, Decimal ja muut standardikirjaston moduulit tulevat Pythonin
mukana. `webapp.main` ei tuo OpenAI-adaptereita; SDK ei kuulu web-palvelun
runtime-ympäristöön. Tämä ei poista adaptereita: niiden käyttäjä asentaa AI-ryhmän.
HTTPX on web-testien TestClientin ja AI SDK:n riippuvuus, ei nykyisen web-palvelun
oma asiakas. Chromium ja testityökalut eivät kuulu tuotannon web-ympäristöön.
Uvicornin nykyiset standard-lisäosat säilyvät, jotta ajonaikaisia ominaisuuksia
ei poisteta tässä riippuvuusmuutoksessa.

## Lukitusperiaate

Lähdetiedostot `.in` kuvaavat suorat riippuvuudet. Niiden `baseline.txt`-rajoitteet
säilyttävät nykyiset versiot myös välillisille riippuvuuksille. Rajoite ei asenna
pakettia, jota kohdeympäristö ei tarvitse. Lukitustiedosto sisältää kaikki
kohdeympäristön tarvitsemat paketit `==`-versioineen ja pakettiarkistojen todelliset
SHA-256-tiivisteet. Lukituksia tuottaa **pip-tools 7.6.2 / pip 25.1.1** erillisessä
ympäristössä. Sovelluksen `.venv`:iin ei asenneta lukitustyökaluja.

[pip-tools edellyttää kohdeympäristökohtaista generointia](https://pip-tools.readthedocs.io/en/stable/).
Windows-tiedostot tuotetaan Windowsin CPython 3.13:lla; Ubuntu-tiedostot tulee
tuottaa Ubuntu 24.04 AMD64:n CPython 3.12:lla. Linux tarvitsee esimerkiksi
Uvicornin `uvloop`-riippuvuuden, jota Windows ei tarvitse. Sen versiota ei ole
arvattu Windows-baselineen. Myös pytestin Windows-kohtaiset riippuvuudet ja
binääripyörät eroavat. `pydantic-core`, `httptools`, `jiter` ja `greenlet`
edellyttävät oikean Python-/alusta-ABI:n pyöriä.

Asennus käyttää aina `--require-hashes --only-binary=:all:`.
[pipin hash-tarkistus](https://pip.pypa.io/en/stable/topics/secure-installs/)
estää hyväksytystä tiivisteestä poikkeavan arkiston asentamisen. Vain valmiit
pyörät sallitaan, jotta asennus ei käynnistä lukitsematonta lähdekoodikäännöstä.
Puuttuva pyörä tai ratkaisematon riippuvuus pysäyttää menettelyn; näitä rajoja
ei poisteta hiljaisesti. Tiiviste ei yksin todista paketin turvallisuutta.

## Windows: nykyinen ja puhdas testausympäristö

Nykyisen kehitysympäristön tarkistus ei tarvitse uudelleenasennusta:

```powershell
.\.venv\Scripts\python.exe -m pip check
.\.venv\Scripts\python.exe -m pytest -q tests
git diff --check
```

Uusi puhdas ympäristö luodaan erikseen. Tarkista ensin `py -3.13 --version`
ja AMD64-tulkki. Älä korvaa toimivaa `.venv`:iä kokeiluasennuksella.

```powershell
py -3.13 -m venv .test-tmp\web4e1-clean
.\.test-tmp\web4e1-clean\Scripts\python.exe -m pip install --require-hashes --only-binary=:all: --index-url https://pypi.org/simple -r requirements-web.txt
.\.test-tmp\web4e1-clean\Scripts\python.exe -m pip check
.\.test-tmp\web4e1-clean\Scripts\python.exe -m playwright install chromium
.\.test-tmp\web4e1-clean\Scripts\python.exe -m pytest -q tests
```

Playwrightin pip-lukitus kiinnittää testiselaimen revision, mutta selainbinääri
ja käyttöjärjestelmä eivät sisälly Python-lukitukseen. Kirjaa testiselaimen versio
ja säilytä hyväksytty selainartefakti testijulkaisun mukana tarvittaessa.
Testit käyttävät väliaikaisia tietokantoja ja paikallisia palvelimia.
OpenAI-testit käyttävät mockeja eivätkä tarvitse oikeaa API-avainta.

## Ubuntu: vielä suorittamatta oleva lukituksen varmennus

Seuraavat ovat **myöhemmän Ubuntu-varmennuksen ohjeita**, eivät tässä vaiheessa
suoritettu tuotantoasennus. Tässä repositoryssa ei vielä ole hyväksyttyjä
`ubuntu2404-amd64-py312-*.txt`-lukituksia. Pelkkä Windows-resolverin alustan
vaihtaminen valitsimella ei korvaa natiivia Linux-varmennusta.

1. Valmistele Ubuntu 24.04 AMD64 -testiympäristö, jossa on Ubuntun Python 3.12
   ja sen venv-tuki. Kirjaa `python3.12 --version`, `uname -m`, `/etc/os-release`,
   Pythonin SQLite-versio ja käytetyt Ubuntu-pakettiversiot.
2. Luo erillinen lukitustyökalujen ympäristö. Ensimmäinen bootstrap käyttää
   `tools.in`:n tarkkoja versioita luotetusta PyPI:stä; sille ei väitetä ennalta
   varmennettua Ubuntu-hash-lukitusta. Tuota sitten työkalujen oma hash-lukitus:

```bash
python3.12 -m venv .test-tmp/ubuntu-lock-bootstrap
.test-tmp/ubuntu-lock-bootstrap/bin/python -m pip install --only-binary=:all: --index-url https://pypi.org/simple -r requirements/tools.in
.test-tmp/ubuntu-lock-bootstrap/bin/python -m piptools compile \
  --resolver=backtracking --strip-extras --allow-unsafe --generate-hashes \
  --no-emit-index-url --no-emit-trusted-host --index-url https://pypi.org/simple \
  --pip-args='--only-binary=:all:' \
  --output-file requirements/locks/ubuntu2404-amd64-py312-tools.txt requirements/tools.in
```

3. Asenna hash-lukittu työkaluketju uuteen ympäristöön ja generoi varsinaiset
   Ubuntu-lukitukset siellä. Windowsin sovellusversiot pysyvät baseline-rajoitteina:

```bash
python3.12 -m venv .test-tmp/ubuntu-lock-tools
.test-tmp/ubuntu-lock-tools/bin/python -m pip install --require-hashes --only-binary=:all: --index-url https://pypi.org/simple -r requirements/locks/ubuntu2404-amd64-py312-tools.txt
for dependency_profile in runtime ai dev; do
  .test-tmp/ubuntu-lock-tools/bin/python -m piptools compile \
    --resolver=backtracking --strip-extras --allow-unsafe --generate-hashes \
    --no-emit-index-url --no-emit-trusted-host --index-url https://pypi.org/simple \
    --pip-args='--only-binary=:all:' \
    --output-file "requirements/locks/ubuntu2404-amd64-py312-$dependency_profile.txt" \
    "requirements/$dependency_profile.in" || exit 1
done
```

4. Tarkista erot. Kirjaa ja tarkista Linuxille uutena valitut paketit, esimerkiksi
   `uvloop`, niiden lähteet ja lisenssit. Varmista, että runtime-, AI- ja dev-lukitusten
   yhteisillä paketeilla on samat versiot. Jos näin ei ole, pysähdy ja lisää
   havaitulle paketille tarkistettu baseline-rajoite ennen uutta generointia.
   Jos nykyinen versio ei tarjoa Python 3.12 / AMD64 -pyörää, selvitä todellinen
   yhteensopiva versio ja hyväksytä rajattu muutos; älä päivitä kaikkea automaattisesti.
5. Testaa puhdas dev-asennus ja erillinen runtime-asennus. Linuxin selaintestien
   tarvitsemat järjestelmäkirjastot ja Chromium asennetaan vain testikoneelle:

```bash
python3.12 -m venv .test-tmp/ubuntu-dev
.test-tmp/ubuntu-dev/bin/python -m pip install --require-hashes --only-binary=:all: --index-url https://pypi.org/simple -r requirements/locks/ubuntu2404-amd64-py312-dev.txt
.test-tmp/ubuntu-dev/bin/python -m pip check
.test-tmp/ubuntu-dev/bin/python -m playwright install --with-deps chromium
.test-tmp/ubuntu-dev/bin/python -m pytest -q tests
python3.12 -m venv .test-tmp/ubuntu-runtime
.test-tmp/ubuntu-runtime/bin/python -m pip install --require-hashes --only-binary=:all: --index-url https://pypi.org/simple -r requirements/locks/ubuntu2404-amd64-py312-runtime.txt
.test-tmp/ubuntu-runtime/bin/python -m pip check
CARBIT_DATABASE=.test-tmp/ubuntu-runtime-smoke.db .test-tmp/ubuntu-runtime/bin/python -m uvicorn webapp.main:app --host 127.0.0.1 --port 8001
```

   Viimeinen komento käynnistää paikallisen savutestin: tarkista toisesta päätteestä
   HTML ja API:n luonti/luku/1,60 h → 144,00 € keksityillä tiedoilla sekä käynnistyksen
   SQLite-migraatiot. Sammuta Ctrl+C:llä. Varmista, ettei runtime tarvitse testipaketteja
   tai OpenAI SDK:ta. Testaa laitteella myös uudelleenkäynnistys ja kevyen kuorman
   resurssinkäyttö; kahden ytimen ja 8 GB:n riittävyyttä ei päätellä Windows-testistä.
6. Hyväksytä Ubuntu-lukitukset ja varmennusraportti ennen tuotantoasennusta.
   Raportissa tulee olla Git-HEAD, tarkat ympäristö- ja työkalutiedot,
   `pip check`, testimäärä/tulokset, runtime-savutestin tulos ja selainversio.

## Ubuntu-tuotannon riippuvuuksien asennus hyväksymisen jälkeen

Tuotantoon asennetaan vain hyväksytty runtime-lukitus ja sovelluksen hyväksytty
versio uuteen julkaisuympäristöön. AI-lukitusta käytetään vain, jos adapterien
käyttö on erikseen hyväksytty; se ei asenna OpenClaw'ta. Testi-/lukitustyökalut ja
Chromium jäävät testikoneelle. Älä asenna kehityksen `requirements-web.txt`:tä
Ubuntu-tuotantoon. Ei `sudo pip`, `--break-system-packages` tai käynnissä olevan
ympäristön `pip install --upgrade` -päivityksiä.

Hyväksytystä Ubuntu-lukituksesta voi kerätä pyörät etukäteen testi-/julkaisukoneella
ja asentaa ilman PyPI-yhteyttä:

```bash
python3.12 -m pip download --require-hashes --only-binary=:all: --index-url https://pypi.org/simple --dest .test-tmp/ubuntu-runtime-wheels -r requirements/locks/ubuntu2404-amd64-py312-runtime.txt
python3.12 -m venv .test-tmp/release-runtime
.test-tmp/release-runtime/bin/python -m pip install --no-index --find-links .test-tmp/ubuntu-runtime-wheels --require-hashes --only-binary=:all: -r requirements/locks/ubuntu2404-amd64-py312-runtime.txt
.test-tmp/release-runtime/bin/python -m pip check
```

Pyörähakemiston on oltava puhdas ja hyväksytyn julkaisun mukana säilytetty;
hash-tarkistus on käytössä myös offline-asennuksessa. Säilytä OS-/Python-tiedot,
työkalulukitus, sovelluslukitus ja asennusraportti. Python-pakettien lukitus ei
lukitse Ubuntun paketteja, Python-tulkkia, SQLite-kirjastoa tai BIOSia.
Varsinaiset palveluhakemistot, oikeudet, systemd ja verkkosuojaus suunnitellaan
myöhemmissä vaiheissa; yllä olevat `.test-tmp`-polut eivät ole tuotantohakemistoja.

## Turvallinen riippuvuuspäivitys

1. Valitse perusteltu yksittäinen päivitys (valmistajan tietoturvatiedote tai
   tarvittava yhteensopivuuskorjaus). Tarkista ensisijaiset julkaisutiedot ja
   muuttuvat transitiiviset riippuvuudet. Haavoittuvuusskannaus vaatii erillisen luvan.
2. Muuta vain asianomaisen paketin `baseline.txt`-rajoitetta ja tarvittaessa `.in`-rajaa.
   Työkalupäivitys tehdään erikseen `tools.in`:iin. `--upgrade-package` ei ohita
   tiukkaa baseline-rajoitetta. Älä käytä yleistä `--upgrade`-päivitystä.
3. Generoi olemassa oleviin lukituksiin päivitys kummassakin natiivissa
   kohdeympäristössä hash-lukitulla työkaluketjulla. Tarkista versio- ja hash-erot,
   tarvittavat uudet paketit, ryhmien yhteiset versiot ja puuttuvat binääripyörät.
   Jos Windows ja Ubuntu tarvitsevat eri version, dokumentoi todettu syy ja käytä
   ympäristömerkkejä rajoitteissa; älä teeskennelkää yhteistä varmennusta.
4. Asenna uusiin puhtaisiin ympäristöihin hash-tarkistuksella, suorita `pip check`,
   koko testisarja ja runtime-savutesti molemmilla käyttöjärjestelmillä. Testaa
   hinnoittelu, nollatunnit, revisiot, muuttumaton historia ja selaimen regressiot.
5. Hyväksytä lukitusmuutokset ja tulokset. Julkaise uusi ympäristö vasta hyväksynnän
   jälkeen. Säilytä edellinen lukitus, pyörät ja sovellusjulkaisu palautusta varten;
   ennen myöhempiä tietokantamuutoksia varmista myös yhteensopiva varmuuskopio.

Windows-lukitusten generointi käyttää samoja compile-valitsimia kuin Ubuntu,
mutta kohdenimi on `windows-amd64-py313` ja tulkki Windowsin CPython 3.13.
Työkalut asennetaan erilliseen venv:iin tiedostosta
`requirements/locks/windows-amd64-py313-tools.txt` hash-tarkistuksella.

```powershell
py -3.13 -m venv .test-tmp\windows-lock-tools
.\.test-tmp\windows-lock-tools\Scripts\python.exe -m pip install --require-hashes --only-binary=:all: --index-url https://pypi.org/simple -r requirements/locks/windows-amd64-py313-tools.txt
foreach ($dependencyProfile in @('runtime', 'ai', 'dev', 'tools')) {
    .\.test-tmp\windows-lock-tools\Scripts\python.exe -m piptools compile --resolver=backtracking --strip-extras --allow-unsafe --generate-hashes --no-emit-index-url --no-emit-trusted-host --index-url https://pypi.org/simple --pip-args='--only-binary=:all: --retries=0 --timeout=15' --output-file "requirements/locks/windows-amd64-py313-$dependencyProfile.txt" "requirements/$dependencyProfile.in"
    if ($LASTEXITCODE -ne 0) { throw "Lukituksen generointi epäonnistui: $dependencyProfile" }
}
```

`.in`-tiedostoja tai `pip freeze` -tulosta ei käytetä valmiin tuotantolukituksen
korvikkeena. Asennus ei ratkaise sovelluksen tämänhetkisiä pääsynhallintapuutteita.
