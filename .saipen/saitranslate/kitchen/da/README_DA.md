# 9Router Extra / Scanner- og integrationssuite

Version:**v0.2.0**
Projekt: 9router_extra
Sti:`V:\___VAC\__K\__CODE\_PY\_9router_extra\`

## Oversigt
Denne repository indeholder udvidelser, værktøjer, tilstandssikkerhedskopier og patches for 9Router:
1. **WorkBuddy AI (`wb/hy3`) Providerbro**
   - Direkte integration med WorkBuddy AI skybaserede kompletioner(`https://www.workbuddy.ai/v2/chat/completions`).
   - Understøtter Hunyuan-ræsonneringsmodellen`hy3` (tvinger`stream: true`, typede indholdsblokke,`reasoning_effort: high`, `reasoning_summary: auto`).
   - Lokal token automatisk import fra`%LOCALAPPDATA%\CodeBuddyExtension\Data\Public\auth\workbuddy-desktop-ai.info`.
   - Token opfriskning og kvotetracking understøttelse.
2. **Antigravity Gemini 3.8 Flash Fix & Dynamisk Tier Resolution**
   - Fikser opstrøms 404 problem: opstrøms Antigravity kræver`gemini-3.8-flash-tiered(high|medium|low)`.
   - Tilføjer dynamisk mønstergenkendelse, så enhver`gemini-*-flash-(high|medium|low)`mapper til tierformat uden 404.
3. **Forudstilling i starten af SAIFREN-kombinationen**
   - Konfigureret`wb/hy3`som 1. model og`ag/gemini-3.8-flash-high`som 2. model i`SAIFREN`.
4. **AgentRouter- og Cline-patches**
   - Indbygget AgentRouter-leverandør, videresendelse af klientens User-Agent, opløsning af Anthropics basis-URL og rettelser af Cline-routingen.
5. **Tilstandsbackup og migreringsmotor**
   - Snapshottet af alle 36 forbindelser, 13 brugeredefinerede noder(B.AI, Dahl, Vyce, osv.), og kombinationer.

## Struktur
- `apply-update.ps1`- 1-klik opdateringsskript til sikkert at stoppe, lave backup, opgradere til v0.5.65-extra, gendanne tilstand og genstarte 9router.
- `packages/`
  - `9router-0.5.65-extra.tgz`- Fuldt bygget, patchet v0.5.65-pakke klar til global installation.
  - `9router-0.5.59-agentrouter.tgz`- Arvelig v0.5.59-pakke.
- `patches/`
  - `9router-0.5.65-extra-unified.patch`- Forenet patch mod v0.5.65 upstream.
  - `9router-extra-unified.patch`- Forenet patch mod v0.5.59.
- `backup/`
  - `export_state.js` & `restore_state.js`- SQLite tilstandsbackup og synkroniseringstools
    (privat tilstand peger nu på`%LOCALAPPDATA%\9router_WatchEdit\backups\private\`, aldrig dette repository).

## Kør 9Router-kildetesterne(vitest-opkaldsfælde, T-21)
Next.js-dashboard-kilden findes i`%APPDATA%\9router\source`. Dens vitest
stien til aliaser(`@/` → `src`, `open-sse` → `open-sse`)defineres KUN i
`tests/vitest.config.js`.

**Involver ikke binæren fra kildens rod:**
```powershell
# WRONG: bypasses tests/vitest.config.js, every "@/..." import fails
.\tests\node_modules\.bin\vitest.cmd run tests/unit/model-test-routing.test.js
#   -> Error: Cannot find package '@/shared/constants/config' imported from
#      src/app/api/models/test/ping.js
```

Brug opkaldet, der indlæser konfigurationen, i stedet:
```powershell
# RIGHT: loads tests/vitest.config.js (aliases resolve)
npm --prefix tests test -- unit/model-test-routing.test.js
```

En rod-niveau`vitest.config.js`der geneksporterer testkonfigurationen, gør også
rod-binæren til at virke; den holdes i kildetræet, så begge opkald er enige.
`tests/unit/model-routing.test.js`har en uvæsentlig forudgående`EPERM`på
`os.tmpdir()`oprydning i denne miljø — det fejler identisk under begge
opkald og er ikke et aliasproblem.

## Sikkerhedsmodel
Denne repository er uden hemmeligheder og sikkert at overgive til eksterne kodningsagenter.
Hemmeligheder findes kun i den lokale runtime-lag(`%LOCALAPPDATA%\9router_WatchEdit\`og den
9Router install). WatchEdit kører`SECRETS: LOCKED` som standard; aktive udbyderhandlinger
kræver en eksplicit oplåsning(OS-understøttet tilladelse eller et valgfrit hovedadgangskodelager).
Se**SECURITY_LOCAL.md**for det fulde model, den SAFE delingsbygge
(`python tools/build_safe_share.py`), hemmelighedsscanneren og private-backup værktøjer.
  - `data-snapshot/`- Fuld databasekopier af`%APPDATA%\9router\db\data.sqlite`.

## Sådan anvendes opdatering
Når dine aktive opgaver i 9router er færdige, kør helt enkelt:
```powershell
.\apply-update.ps1
```
Dette vil:
1. Stop den kørende 9router-instans.
2. Opret en tidsstempelbackup i`%APPDATA%\9router_backup_<timestamp>`.
3. Installér`9router-0.5.65-extra.tgz`globalt.
4. Synkroniser alle forbindelser og opdatér SAIFREN-kombinationen.
5. Genstart 9router i systembakkemodus.

<!-- translation-model: qwen3:14b contract:structured-markdown-v2 -->
<!-- source-digest: README.md sha256:082c6cba0dbf49757cbab042ad6e9fec1162ed12091176b2cef3ed1ffe6c5c4c -->
