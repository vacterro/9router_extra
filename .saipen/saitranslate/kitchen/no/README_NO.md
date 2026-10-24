# 9Router Extra / Scanner- og integrasjonssuite

Versjon: **v0.2.0**
Prosjekt: 9router_extra
Sti: `V:\___VAC\__K\__CODE\_PY\_9router_extra\`

## Oversikt
Dette repositoriet inneholder utvidelser, verktøy, sikkerhetskopier av tilstand og patcher for 9Router:
1. **Leverandørbro for WorkBuddy AI (`wb/hy3`)**
   - Direkte integrasjon med WorkBuddy AI-skykompletteringer (`https://www.workbuddy.ai/v2/chat/completions`).
   - Støtter Hunyuan-resonneringsmodellen `hy3` (tvinger `stream: true`, bruker typede innholdsblokker, `reasoning_effort: high`, `reasoning_summary: auto`).
   - Automatisk import av lokal token fra `%LOCALAPPDATA%\CodeBuddyExtension\Data\Public\auth\workbuddy-desktop-ai.info`.
   - Støtter tokenoppdatering og kvotesporing.
2. **Retting av Antigravity Gemini 3.8 Flash og dynamisk nivåoppløsning**
   - Retter 404-problemet i oppstrømstjenesten: Antigravity oppstrøms krever `gemini-3.8-flash-tiered(high|medium|low)`.
   - Legger til dynamisk mønstermatching slik at enhver `gemini-*-flash-(high|medium|low)` mappes til format med nivå uten 404.
3. **Legger til på starten av SAIFREN-kombinasjonen**
   - Konfigurert `wb/hy3` som første modell og `ag/gemini-3.8-flash-high` som andre modell i `SAIFREN`.
4. **Patcher for AgentRouter og Cline**
   - Innebygd AgentRouter-leverandør, videresending av klientens User-Agent, oppløsning av Anthropic-basens URL og rutingsrettinger for Cline.
5. **Motor for sikkerhetskopiering og migrering av tilstand**
   - Øyeblikksbilde av alle 36 forbindelser, 13 tilpassede noder (B.AI, Dahl, Vyce, osv.) og kombinasjoner.

## Struktur
- `apply-update.ps1` - Oppdateringsskript med ett klikk som trygt stopper 9router, tar sikkerhetskopi, oppgraderer til v0.5.65-extra, gjenoppretter tilstanden og starter 9router på nytt.
- `packages/`
  - `9router-0.5.65-extra.tgz` - Fullt bygd og patchet v0.5.65-pakke, klar for global installasjon.
  - `9router-0.5.59-agentrouter.tgz` - Eldre v0.5.59-pakke.
- `patches/`
  - `9router-0.5.65-extra-unified.patch` - Samlet patch mot v0.5.65 oppstrøms.
  - `9router-extra-unified.patch` - Samlet patch mot v0.5.59.
- `backup/`
  - `export_state.js` & `restore_state.js` - SQLite-verktøy for sikkerhetskopiering og synkronisering av tilstand
    (privat tilstand lagres nå i `%LOCALAPPDATA%\9router_WatchEdit\backups\private\`, aldri i dette repositoriet).

## Kjør 9Router-kildetestene (felle for vitest-invokasjon, T-21)
Next.js-dashboardens kildekode finnes i `%APPDATA%\9router\source`. Vitest-stienealiasene
(`@/` → `src`, `open-sse` → `open-sse`) er bare definert i
`tests/vitest.config.js`.

**Ikke kjør binærfilen fra kilderoten:**
```powershell
# WRONG: bypasses tests/vitest.config.js, every "@/..." import fails
.\tests\node_modules\.bin\vitest.cmd run tests/unit/model-test-routing.test.js
#   -> Error: Cannot find package '@/shared/constants/config' imported from
#      src/app/api/models/test/ping.js
```

Bruk den konfigurasjonsbevisste invokasjonen i stedet:
```powershell
# RIGHT: loads tests/vitest.config.js (aliases resolve)
npm --prefix tests test -- unit/model-test-routing.test.js
```

En `vitest.config.js` på rotnivå som re-eksporterer testkonfigurasjonen gjør også binærfilen
i roten brukbar; den beholdes i kildetreet slik at begge invokasjonene er like.
`tests/unit/model-routing.test.js` har en eksisterende `EPERM` uten sammenheng under
nedmontering av `os.tmpdir()` i dette miljøet — den feiler likt ved begge kallene og er ikke et aliasproblem.

## Sikkerhetsmodell
Dette repositoriet inneholder ingen hemmeligheter og kan trygt gis til eksterne kodeagenter.
Hemmeligheter finnes bare i det lokale kjøringslaget (`%LOCALAPPDATA%\9router_WatchEdit\` og 9Router-installasjonen).
Som standard bruker WatchEdit `SECRETS: LOCKED`; driftsoperasjoner mot leverandører krever eksplisitt opplåsing (en autorisasjon støttet av operativsystemet eller et valgfritt hvelv med hovedpassord).
Se **SECURITY_LOCAL.md** for hele modellen, verktøyet som lager SAFE-delinger
(`python tools/build_safe_share.py`), hemmelighetsskanneren og verktøyene for private sikkerhetskopier.
  - `data-snapshot/` - Fullstendig databasekopi av `%APPDATA%\9router\db\data.sqlite`.

## Hvordan installere oppdateringen
Når oppgavene dine i 9router er ferdige, kjører du bare:
```powershell
.\apply-update.ps1
```
Dette gjør følgende:
1. Stopper den kjørende 9router-instansen.
2. Oppretter en sikkerhetskopi med tidsstempel i `%APPDATA%\9router_backup_<timestamp>`.
3. Installerer `9router-0.5.65-extra.tgz` globalt.
4. Synkroniserer alle forbindelser og oppdaterer SAIFREN-kombinasjonen.
5. Starter 9router på nytt i tray-modus.

<!-- translation-model: qwen3:14b contract:structured-markdown-v2 -->
<!-- source-digest: README.md sha256:082c6cba0dbf49757cbab042ad6e9fec1162ed12091176b2cef3ed1ffe6c5c4c -->
