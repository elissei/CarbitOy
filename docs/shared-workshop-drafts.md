# Oman korjaamon yhteiset luonnokset (skeema 4)

Tämä on 16.10.2026 hyväksytyn tavoitteen erillinen toteutus aiemman
WORK_INQUIRY-selaincheckpointin jälkeen. OWNER ja SERVICE_MANAGER saavat oman
korjaamonsa molempien tyyppien (WORK_INQUIRY, LABOUR_ENTRY) listauksen, luvun,
muokkauksen ja nykyisen API-hylkäyksen riippumatta luojasta. Selain sisältää
edelleen vain WORK_INQUIRY-editorin. Ei uutta hylkäys-UI:ta, jakamista,
omistajuuden siirtoa, sijaisjärjestelmää, autosavea tai integraatioita.

## Identiteetti ja oikeudet

Sisältö rajataan palvelimen organization_id-, workshop_id- ja auth_store_id-arvoilla.
Kutsuja tarvitsee nykyisen aidon HUMAN/MFA-istunnon, henkilöstöroolin ja aktiivisen
korjaamojäsenyyden. Rooli ei yksin riitä. IT_ADMIN ja OPENCLAW/SERVICE eivät saa
luonnossisältöä. Luojan jäsenyyden päättyminen ei piilota luonnosta kollegalta;
poistuneen käyttäjän omat luvut, kirjoitukset ja kuittiuusinnat estetään.
Korjaamon ja organisaation aktiivisuusraja säilyy. Ei tietoa toisesta korjaamosta.

Registry.owner_employee_id tarkoittaa muuttumatonta **luojaa**, ei yksinoikeutta.
Request-kuitti sisältää **requester_employee_id**:n. Hylkäys käyttää samaa revision-
ja kuittisopimusta kuin ennen: yksi voittaja, välitön sisältöpoisto, ei palautusta.
Sisäinen omistajakohtainen cleanup ja erillinen offline-ylläpito eivät laajene.

## Revisio, tekijä ja henkilökohtainen uusintapyyntö

Todellinen sisältömuutos kasvattaa revisionia ja tallentaa pyytäjän muutoskuittiin
(content_changed=1, operation=SAVE, accepted_revision). Luoja säilyy. Saman revision
sisältömuutoksista vain yksi voittaa; häviäjä saa 409:n ilman osittaista tallennusta.
Muuttumaton SAVE (content_changed=0) ei tee uutta sisältörevisionia tai vaihda viimeistä
sisällön muokkaajaa. Creator- ja DISCARD-tiedot erotetaan SAVE-muutoksista.
Nykyinen revisionoitu audit yhdistyy muuttaneeseen kuittiin draft_id/revision-parilla.
Hylkääjä on DISCARD-kuitin pyytäjä; expiry/ylläpitopoistoa ei väitetä editorin teoksi.

Nonce-uniikkius ja kuittihaku ovat auth-varasto + pyytäjä + korjaamo -kohtaisia.
Sama UUID eri henkilöllä on itsenäinen uusi pyyntö ja käy normaalin CAS:n läpi;
se ei siirrä alkuperäistä kuittia toiselle. Samalla henkilöllä sama UUID eri
pyynnöllä on konflikti. HMAC-v1-domain ja serialisointi pysyvät ennallaan.
Vanha kuitti palauttaa vain alkuperäisen metadata-ackin ja nykyisen päättymistilan.
Se ei palauta vanhaa sisältöä, muokkaajaa tai retention-aikaa tietokantaan.

Selain uusii epäselvän pyynnön vain käsin samalla alkuperäisellä envelopella.
Kuittia seuraava GET tarkistaa nykyisen sisällön. Jos kollega on muuttanut sitä,
paikallinen teksti säilyy ja käyttäjä valitsee palvelinversion avaamisen.
Käyttäjänvaihto/logout tyhjentää edellisen henkilön editorin, listan ja retry-objektin;
yhteiset palvelinsisällöt haetaan uuden henkilön oikeuksilla. Ei pysyvää selainvarastoa.

## Migraatio ja vanha tieto

Eteenpäin menevä migraatio 4 erottaa request-kuitin pyytäjän registry-luojasta:
erilliset luonnos- ja jäsenyysviiteavaimet sekä pyytäjäkohtainen pääavain.
Vanhat pyytäjä-UUID:t, sisällöt, tekijäviitteet, revisiot, aikaleimat, HMACit ja
avaintunnisteet säilyvät. Nykyiset immutable-/discard-/key-/retention-guardit säilyvät.
active_drafts ei enää riipu luojan jäsenyyden aktiivisuudesta.

**Vanhojen kuittien content_changed on NULL:** vanhaa muuttanut/no-op-erottelua
ei päätellä jälkikäteen eikä audit-rivejä kirjoiteta uudelleen. Vanha luoja ja
kuitin pyytäjä säilyvät; ennen v4:ää ei ole itsenäistä tapahtumakohtaista muutoslippua.
Uusille kuiteille lippu on pakollinen ja vain SAVE voi olla sisältömuutos.
Tämä ei luo puuttuvaa historiallista todistusaineistoa tai ikuista audit-arkistoa.

DDL, kopiointi ja ledger ovat yhdessä nykyisessä alustustransaktiossa. Vanhat
skeemaprefixit päivitetään versioon 4; tuntematon/epäjatkuva versio estää alustuksen.
Virhe peruu päivityksen, uusinta on no-op. Vanha koodi ei hyväksy v4:ää.
Oikean kannan käyttöönotto ja varmennettu palautus suunnitellaan erikseen.

## Säilyvät rajat ja kokeilu

API pysyy oletuksena suljettuna. Kokeile vain nykyisellä nimenomaisella
CARBIT_DRAFT_API_ENABLED=1-asetuksella ja kelvollisella testiavainrenkaalla
[HTTP-ohjeen](persistent-draft-http-checkpoint-4b-3a.md) mukaan eristetyssä
ympäristössä. Ei asetusten globaalia muutosta. Seitsemän päivän sisältöexpiry,
nykyinen metadata-/kuittihorisontti, 24 h selaimen käsinuusintaraja ja avainpolitiikka
säilyvät. Historiallisia kuittifingerprinttejä ei lasketa uudelleen.

[Selainohje](manual-inquiry-drafts.md) kuvaa käytön. Ei muunnosta, lopullista
labour-lähetystä, tarjoushyväksyntää, CRM/OpenClaw-toteutusta, WD-3:a tai SEC-2B:tä.
Tekijä- ja kuittimetadata pysyy nykyisessä teknisessä säilytyksessä; ei uutta
asiakasviestien tai hyväksyntöjen todistushistoriaa. Aiemmat P3-, kattavuus-, README-,
käyttöönotto-, SEC- ja DRAFT-varaukset säilyvät. Tuotantohyväksyntää ei anneta.
