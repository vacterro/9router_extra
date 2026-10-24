# 9Router Extra / skannaus- ja integraatiopaketti

Versio: **v0.2.0**
Projekti: 9router_extra
Polku: `V:\___VAC\__K\__CODE\_PY\_9router_extra\`

## Yleiskatsaus
Tämä repositorio sisältää 9Routerille tarkoitetut laajennukset, työkalut, tilavarmuuskopiot ja korjaukset:
1. **WorkBuddy AI (`wb/hy3`) -palveluyhteys**
   - Suora integrointi WorkBuddy AI:n pilvipalveluun (`https://www.workbuddy.ai/v2/chat/completions`).
   - Tukee Hunyuan-päättelymallia `hy3` (asettaa `stream: true`, käyttää tyypitettyjä sisältölohkoja sekä arvoja `reasoning_effort: high` ja `reasoning_summary: auto`).
   - Tokenin automaattinen tuonti paikallisesta tiedostosta `%LOCALAPPDATA%\CodeBuddyExtension\Data\Public\auth\workbuddy-desktop-ai.info`.
   - Tokenin päivitys ja käyttökiintiön seuranta tuettu.
2. **Antigravity Gemini 3.8 Flash -korjaus ja dynaaminen tasojen määrittäminen**
   - Korjaa palvelinpuolen 404-virheen: Antigravity vaatii `gemini-3.8-flash-tiered(high|medium|low)`.
   - Lisää dynaamisen mallikuvioiden sovituksen, jolloin kaikki `gemini-*-flash-(high|medium|low)`-kuviot mapataan tasomuotoon ilman 404-virhettä.
3. **SAIFREN-kombinaation laajentaminen alkuun**
   - Asettaa `wb/hy3`-mallin ensimmäiseksi ja `ag/gemini-3.8-flash-high`-mallin toiseksi `SAIFREN`-kombinaatioon.
4. **AgentRouter- ja Cline-korjaukset**
   - Sisäänrakennettu AgentRouter-palveluntarjoaja, asiakaspuolen User-Agent -otsakkeen välitys, Anthropicin perus-URL-osoitteen määrittäminen ja Cline-reitityskorjaukset.
5. **Tilojen varmuuskopiointi- ja siirtymämoottori**
   - Tallentaa kaikkien 36 yhteyden, 13 mukautetun solmun (B.AI, Dahl, Vyce jne.) ja kombinaatioiden tilannekuvat.

## Rakenne
- `apply-update.ps1` - Yhden napsautuksen päivitysskriptti, joka pysäyttää 9routerin turvallisesti, varmuuskopioi sen, päivittää versioon v0.5.65-extra, palauttaa tilan ja käynnistää 9routerin uudelleen.
- `packages/`
  - `9router-0.5.65-extra.tgz` - Täysin rakennettu ja korjattu v0.5.65-paketti, valmis globaaliin asennukseen.
  - `9router-0.5.59-agentrouter.tgz` - Vanhempi v0.5.59-paketti.
- `patches/`
  - `9router-0.5.65-extra-unified.patch` - Yhdistetty korjaus v0.5.65-versioon.
  - `9router-extra-unified.patch` - Yhdistetty korjaus v0.5.59-versioon.
- `backup/`
  - `export_state.js` & `restore_state.js` - SQLite-tilojen varmuuskopiointi- ja synkronointityökalut
    (yksityinen tila tallennetaan nyt hakemistoon `%LOCALAPPDATA%\9router_WatchEdit\backups\private\`, ei koskaan tähän repositorioon).

## 9Routerin lähdekoodin testien suorittaminen (vitest-kutsun ansa, T-21)
Next.js-hallintapaneelin lähdekoodi sijaitsee hakemistossa `%APPDATA%\9router\source`. Vitestin polkujen aliakset (`@/` → `src`, `open-sse` → `open-sse`) määritellään VAIN tiedostossa
`tests/vitest.config.js`.

**Älä kutsu suoritettavaa ohjelmaa lähdekoodin juurikansiosta:**
```powershell
# WRONG: bypasses tests/vitest.config.js, every "@/..." import fails
.\tests\node_modules\.bin\vitest.cmd run tests/unit/model-test-routing.test.js
#   -> Error: Cannot find package '@/shared/constants/config' imported from
#      src/app/api/models/test/ping.js
```

Käytä sen sijaan määritystiedoston läpi johdatettua kutsua:
```powershell
# RIGHT: loads tests/vitest.config.js (aliases resolve)
npm --prefix tests test -- unit/model-test-routing.test.js
```

Juuritasolla sijaitseva `vitest.config.js`, joka vie uudelleen testien määritykset, tekee myös juuriosan ohjelmasta toimivan. Se säilytetään lähdekoodipuussa, jotta molemmat kutsut käyttäisivät samoja määrityksiä.
`tests/unit/model-routing.test.js`-testillä on tässä ympäristössä riippumaton, aiemmin ollut `EPERM` `os.tmpdir()`-hakemiston tyhjentämisen yhteydessä. Testi epäonnistuu samalla tavalla molemmilla kutsuilla, eikä kyse ole aliaksista.

## Turvallisuusmalli
Tässä repositoriossa ei ole salaisuuksia, joten sen voi turvallisesti luovuttaa ulkoisille koodaustoagenteille.
Salaisuuksia säilytetään vain paikallisessa suoritusympäristössä (`%LOCALAPPDATA%\9router_WatchEdit\` ja 9Routerin asennus). WatchEdit toimii oletuksena tilassa `SECRETS: LOCKED`; palveluntarjoajien suorien toimintojen käyttö edellyttää nimenomaista lukituksen avaamista (käyttöjärjestelmän tukema lupa tai valinnainen pääsalasanasäilö).
Koko malli, SAFE-osuuden rakentava työkalu
(`python tools/build_safe_share.py`), salaisuuksien skanneri sekä yksityisten varmuuskopioiden työkalut on kuvattu tiedostossa **SECURITY_LOCAL.md**.
  - `data-snapshot/` - Koko tietokannan kopio tiedostosta `%APPDATA%\9router\db\data.sqlite`.

## Päivityksen asentaminen
Kun 9routerissa käynnissä olevat tehtävät ovat valmiit, suorita vain:
```powershell
.\apply-update.ps1
```
Tämä tekee seuraavasti:
1. Lopettaa käynnissä olevan 9router-instanssin.
2. Luo aikaleimattu varmuuskopio hakemistoon `%APPDATA%\9router_backup_<timestamp>`.
3. Asentaa `9router-0.5.65-extra.tgz`-paketin globaalisti.
4. Synkronoi kaikki yhteydet ja päivittää SAIFREN-kombinaation.
5. Käynnistää 9routerin uudelleen ilmoitusalueella.

<!-- translation-model: qwen3:14b contract:structured-markdown-v2 -->
<!-- source-digest: README.md sha256:082c6cba0dbf49757cbab042ad6e9fec1162ed12091176b2cef3ed1ffe6c5c4c -->
