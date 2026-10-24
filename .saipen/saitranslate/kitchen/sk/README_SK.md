# 9Router Extra / Skenovacia a integračná sada

Verzia: **v0.2.0**
Projekt: 9router_extra
Cesta: `V:\___VAC\__K\__CODE\_PY\_9router_extra\`

## Prehľad
Tento repozitár obsahuje rozšírenia, nástroje, zálohy stavu a opravy pre 9Router:
1. **Most poskytovateľa WorkBuddy AI (`wb/hy3`)**
   - Priama integrácia s cloudovými doplneniami WorkBuddy AI (`https://www.workbuddy.ai/v2/chat/completions`).
   - Podporuje reasoningový model Hunyuan `hy3` (vynúti `stream: true`, typované bloky obsahu, `reasoning_effort: high`, `reasoning_summary: auto`).
   - Automatický import lokálneho tokenu z `%LOCALAPPDATA%\CodeBuddyExtension\Data\Public\auth\workbuddy-desktop-ai.info`.
   - Podporuje obnovenie tokenu a sledovanie kvóty.
2. **Oprava Antigravity Gemini 3.8 Flash a dynamické určovanie úrovne**
   - Odstraňuje nadriadený problém 404: nadriadená služba Antigravity vyžaduje `gemini-3.8-flash-tiered(high|medium|low)`.
   - Pridáva dynamické párovanie vzorov, takže každý `gemini-*-flash-(high|medium|low)` sa mapuje na formát s úrovňou bez chyby 404.
3. **Vloženie na začiatok kombinácie SAIFREN**
   - Nastaví `wb/hy3` ako 1. model a `ag/gemini-3.8-flash-high` ako 2. model v `SAIFREN`.
4. **Opravy AgentRouter a Cline**
   - Natívny poskytovateľ AgentRouter, presmerovanie User-Agent klienta, určenie základnej URL Anthropic a opravy smerovania Cline.
5. **Motor zálohovania a migrácie stavu**
   - Snímok všetkých 36 pripojení, 13 vlastných uzlov (B.AI, Dahl, Vyce atď.) a kombinácií.

## Štruktúra
- `apply-update.ps1` - skript aktualizácie jedným kliknutím na bezpečné zastavenie, zálohovanie, aktualizáciu na v0.5.65-extra, obnovenie stavu a reštart 9router.
- `packages/`
  - `9router-0.5.65-extra.tgz` - úplne zostavený opravený balík v0.5.65 pripravený na globálnu inštaláciu.
  - `9router-0.5.59-agentrouter.tgz` - starší balík v0.5.59.
- `patches/`
  - `9router-0.5.65-extra-unified.patch` - zjednotená oprava oproti nadriadenej verzii v0.5.65.
  - `9router-extra-unified.patch` - zjednotená oprava oproti verzii v0.5.59.
- `backup/`
  - `export_state.js` & `restore_state.js` - nástroje na zálohovanie a synchronizáciu stavu SQLite
    (súkromný stav teraz smeruje do `%LOCALAPPDATA%\9router_WatchEdit\backups\private\`, nikdy do tohto repozitára).

## Spustenie testov zdrojového kódu 9Router (pasca pri volaní vitest, T-21)
Zdrojový kód dashboardu Next.js sa nachádza v `%APPDATA%\9router\source`. Jeho aliasy
ciest vitest (`@/` → `src`, `open-sse` → `open-sse`) sú definované IBA v
`tests/vitest.config.js`.

**NEVOLAJTE binárny súbor z koreňa zdrojového kódu:**
```powershell
# WRONG: bypasses tests/vitest.config.js, every "@/..." import fails
.\tests\node_modules\.bin\vitest.cmd run tests/unit/model-test-routing.test.js
#   -> Error: Cannot find package '@/shared/constants/config' imported from
#      src/app/api/models/test/ping.js
```

Namiesto toho použite volanie, ktoré pozná konfiguráciu:
```powershell
# RIGHT: loads tests/vitest.config.js (aliases resolve)
npm --prefix tests test -- unit/model-test-routing.test.js
```

Súbor `vitest.config.js` v koreňovom priečinku, ktorý znovu exportuje konfiguráciu
testov, zároveň umožní fungovať koreňovému binárnemu súboru; nachádza sa v
zdrojovom strome, aby boli obe volania zhodné.
`tests/unit/model-routing.test.js` má v tomto prostredí nesúvisiacu existujúcu
chybu `EPERM` pri ukončení cez `os.tmpdir()` — pri oboch volaniach zlyháva
identicky a nejde o problém s aliasom.

## Bezpečnostný model
Tento repozitár neobsahuje žiadne tajné údaje a možno ho bezpečne odovzdať externým programovým agentom.
Tajné údaje existujú iba v miestnej prevádzkovej vrstve (`%LOCALAPPDATA%\9router_WatchEdit\` a
inštalácii 9Router). WatchEdit štandardne beží v režime `SECRETS: LOCKED`; živé operácie
s poskytovateľmi vyžadujú výslovné odomknutie (oprávnenie podporované OS alebo voliteľný trezor
s hlavným heslom).
Pozri **SECURITY_LOCAL.md** pre úplný model, nástroj na vytváranie bezpečnej zdieľanej
kopie (`python tools/build_safe_share.py`), skener tajných údajov a nástroje na
súkromné zálohovanie.
  - `data-snapshot/` - úplná kópia databázy `%APPDATA%\9router\db\data.sqlite`.

## Ako použiť aktualizáciu
Keď sú vaše aktívne úlohy v 9router dokončené, jednoducho spustite:
```powershell
.\apply-update.ps1
```
Týmto sa:
1. Zastaví spustená inštancia 9router.
2. Vytvorí zálohu s časovou pečiatkou v `%APPDATA%\9router_backup_<timestamp>`.
3. Globálne nainštaluje `9router-0.5.65-extra.tgz`.
4. Zosynchronizuje všetky pripojenia a aktualizuje kombináciu SAIFREN.
5. Reštartuje 9router v režime oznamovacieho priečinka.

<!-- translation-model: zcode-agent translation (not local Ollama pipeline) -->
<!-- source-digest: README.md sha256:082c6cba0dbf49757cbab042ad6e9fec1162ed12091176b2cef3ed1ffe6c5c4c -->
