# Carbit Oy – projektin dokumentaatio

## Tavoite

Projektin tavoitteena on automatisoida autokorjaamon asiakkaan työpyynnön käsittelyä, varaosavaihtoehtojen vertailua ja tarjouksen valmistelua. Dokumentaatio tukee kehitystyötä, GitHub-portfoliota ja myöhemmin opinnäytetyötä.

## Aloita tästä

- [Saagan tekninen dokumentaatio](../../saaga-docs/README.md)
- [Nykyisen toteutuksen arkkitehtuuri](../../saaga-docs/ARCHITECTURE.md)
- [Tietomallit ja käsitteet](../../saaga-docs/concepts/work-and-part-data.md)
- [Tarjousehdotusten päätöslogiikka](../../saaga-docs/features/proposal-decisions.md)
- [Toimittajaliitäntöjen toteutus](../../saaga-docs/patterns/supplier-adapters.md)
- [Arkkitehtuurikuva ja päätöspuut](../visualizations/README.md)
- [Kehityspäiväkirja ja kirjauspohja](kehityspaivakirja.md)
- [Päätösloki ja kirjauspohja](paatokset.md)

## Dokumentaation vastuut

| Sisältö | Ylläpito |
| --- | --- |
| `saaga-docs/` | Saaga: koodin tekninen dokumentaatio ja päivityksen lähtötilanne |
| `docs/notes/` | Tekijä: tavoitteet, työpäiväkirja, päätökset ja perustelut |
| `docs/visualizations/` | Erikseen päivitettävät kuvat ja PDF; lähdeversio ilmoitetaan dokumentaatiossa |

Saagan dokumentaatio kuvaa ajon kohteena ollutta koodiversiota. Kuvat ja muistiinpanot eivät päivity automaattisesti Saagan mukana. Tavoitearkkitehtuuri ei ole luettelo jo toteutetuista ominaisuuksista.

## Työskentely

Koodikehitys tapahtuu `main`-branchilla. Dokumentointityö tapahtuu `visuals`-branchilla erillisessä `carbitoy-docs`-worktreessä. Tämän worktreen Git- ja Saaga-komennot ajetaan WSL:ssä; Obsidian avaa saman kansion Windowsissa.

Ennen dokumentaation päivitystä tarkistetaan Git-status ja tuodaan tarvittavat `main`-muutokset `visuals`-branchiin. Saagan dokumentaatio päivitetään komennolla `saaga run update --backend codex`, minkä jälkeen muutokset tarkistetaan, commitoidaan ja pusketaan `visuals`-branchille. Branchien yhdistämisen mahdolliset konfliktit ratkaistaan ennen ajoa.

Konekohtaiset Obsidian-asetukset jätetään Gitin ulkopuolelle.
