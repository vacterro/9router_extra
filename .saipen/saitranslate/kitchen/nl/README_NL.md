# 9Router Extra / Scanner- en integratiesuite

Versie: **v0.2.0**
Project: 9router_extra
Pad: `V:\___VAC\__K\__CODE\_PY\_9router_extra\`

## Overzicht
Deze repository bevat uitbreidingen, tools, statusback-ups en patches voor 9Router:
1. **WorkBuddy AI (`wb/hy3`) providerbrug**
   - Directe integratie met de cloudcompleties van WorkBuddy AI (`https://www.workbuddy.ai/v2/chat/completions`).
   - Ondersteunt het Hunyuan-redeneingsmodel `hy3` (dwingt `stream: true` af, gebruikt getypte inhoudsblokken, `reasoning_effort: high`, `reasoning_summary: auto`).
   - Automatische import van de lokale token uit `%LOCALAPPDATA%\CodeBuddyExtension\Data\Public\auth\workbuddy-desktop-ai.info`.
   - Ondersteuning voor tokenvernieuwing en quotabeheer.
2. **Correctie voor Antigravity Gemini 3.8 Flash en dynamische niveauoplossing**
   - Verhelpt het upstream-404-probleem: de upstreamserver van Antigravity vereist `gemini-3.8-flash-tiered(high|medium|low)`.
   - Voegt dynamische patroonherkenning toe, zodat elk `gemini-*-flash-(high|medium|low)` naar het formaat met niveau wordt vertaald zonder 404.
3. **Voorvoeging op de SAIFREN-combo**
   - Geconfigureerd `wb/hy3` als eerste model en `ag/gemini-3.8-flash-high` als tweede model in `SAIFREN`.
4. **Patches voor AgentRouter en Cline**
   - Ingebouwde AgentRouter-provider, doorsturen van de client-User-Agent, oplossen van de Anthropic-basis-URL en routingscorrecties voor Cline.
5. **Motor voor back-ups en migratie van status**
   - Momentopname van alle 36 verbindingen, 13 aangepaste knooppunten (B.AI, Dahl, Vyce, enz.) en combinaties.

## Structuur
- `apply-update.ps1` - Update-script met één klik om 9router veilig te stoppen, een back-up te maken, te upgraden naar v0.5.65-extra, de status te herstellen en 9router opnieuw te starten.
- `packages/`
  - `9router-0.5.65-extra.tgz` - Volledig gebouwd en gepatcht v0.5.65-pakket, klaar voor globale installatie.
  - `9router-0.5.59-agentrouter.tgz` - Verouderd v0.5.59-pakket.
- `patches/`
  - `9router-0.5.65-extra-unified.patch` - Geïntegreerde patch voor v0.5.65-upstream.
  - `9router-extra-unified.patch` - Geïntegreerde patch voor v0.5.59.
- `backup/`
  - `export_state.js` & `restore_state.js` - SQLite-tools voor back-ups en synchronisatie van de status
    (privéstatus wordt nu opgeslagen in `%LOCALAPPDATA%\9router_WatchEdit\backups\private\`, nooit in deze repository).

## De broncode van 9Router testen (valkuil bij vitest-invokering, T-21)
De broncode van de Next.js-dashboard staat in `%APPDATA%\9router\source`. De vitest-padaliassen
(`@/` → `src`, `open-sse` → `open-sse`) zijn alleen gedefinieerd in
`tests/vitest.config.js`.

**Roep het binaire bestand niet aan vanaf de bronroot:**
```powershell
# WRONG: bypasses tests/vitest.config.js, every "@/..." import fails
.\tests\node_modules\.bin\vitest.cmd run tests/unit/model-test-routing.test.js
#   -> Error: Cannot find package '@/shared/constants/config' imported from
#      src/app/api/models/test/ping.js
```

Gebruik in plaats daarvan de configuratiebewuste aanroep:
```powershell
# RIGHT: loads tests/vitest.config.js (aliases resolve)
npm --prefix tests test -- unit/model-test-routing.test.js
```

Een `vitest.config.js` op hoofdniveau dat de testconfiguratie her-exporteert maakt ook het binaire
bestand in de hoofdmap werkend; het blijft in de bronboom staan zodat beide aanroepen gelijk zijn.
`tests/unit/model-routing.test.js` heeft een niet-gerelateerde, reeds aanwezige `EPERM` bij de
afbouw van `os.tmpdir()` in deze omgeving — het mislukt op dezelfde manier bij beide aanroepen en is geen aliasprobleem.

## Beveiligingsmodel
Deze repository bevat geen geheimen en kan veilig aan externe codeeragents worden gegeven.
Geheimen bestaan alleen in de lokale uitvoeringslaag (`%LOCALAPPDATA%\9router_WatchEdit\` en de 9Router-installatie).
Standaard gebruikt WatchEdit `SECRETS: LOCKED`; live bewerkingen met providers vereisen een expliciete ontgrendeling (een door het besturingssysteem ondersteunde autorisatie of een optionele kluis met hoofdwachtwoord).
Raadpleeg **SECURITY_LOCAL.md** voor het volledige model, de bouwer voor SAFE-delingen
(`python tools/build_safe_share.py`), de geheimenscanner en de tools voor privéback-ups.
  - `data-snapshot/` - Volledige databasekopie van `%APPDATA%\9router\db\data.sqlite`.

## Een update toepassen
Wanneer je actieve taken in 9router zijn voltooid, voer je eenvoudig uit:
```powershell
.\apply-update.ps1
```
Dit doet het volgende:
1. De actieve 9router-instantie stoppen.
2. Een back-up met tijdstempel maken in `%APPDATA%\9router_backup_<timestamp>`.
3. `9router-0.5.65-extra.tgz` globaal installeren.
4. Alle verbindingen synchroniseren en de SAIFREN-combo bijwerken.
5. 9router opnieuw starten in traymodus.

<!-- translation-model: qwen3:14b contract:structured-markdown-v2 -->
<!-- source-digest: README.md sha256:082c6cba0dbf49757cbab042ad6e9fec1162ed12091176b2cef3ed1ffe6c5c4c -->
