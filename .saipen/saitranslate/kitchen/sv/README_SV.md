# 9Router Extra / Skanner- och integrationssvit

Version: **v0.2.0**
Projekt: 9router_extra
Sökväg: `V:\___VAC\__K\__CODE\_PY\_9router_extra\`

## Översikt
Det här repot innehåller tillägg, verktyg, tillståndssäkringar och patchar för 9Router:
1. **WorkBuddy AI (`wb/hy3`) leverantörsbro**
   - Direkt integrering med WorkBuddy AI-molnkompletteringar (`https://www.workbuddy.ai/v2/chat/completions`).
   - Stöder Hunyuan-resonansmodellen `hy3` (tvingar `stream: true`, typade innehållsblock, `reasoning_effort: high`, `reasoning_summary: auto`).
   - Automatisk import av lokal token från `%LOCALAPPDATA%\CodeBuddyExtension\Data\Public\auth\workbuddy-desktop-ai.info`.
   - Stöd för tokenförnyelse och kvotspårning.
2. **Antigravity Gemini 3.8 Flash Fix & Dynamic Tier Resolution**
   - Åtgärdar ett 404-fel i uppströmskedjan: den uppströmsmodifierade Antigravity kräver `gemini-3.8-flash-tiered(high|medium|low)`.
   - Lägger till dynamisk mönstermatchning så att alla `gemini-*-flash-(high|medium|low)` mappas till det nivåindelade formatet utan 404.
3. **Infogning i början av SAIFREN-kombinationen**
   - Konfigurerar `wb/hy3` som första modellen och `ag/gemini-3.8-flash-high` som andra modellen i `SAIFREN`.
4. **Patchar för AgentRouter och Cline**
   - Inbyggd AgentRouter-leverantör, vidarebefordring av klientens User-Agent, upplösning av Anthropic-bas-URL och Cline-routningsfixar.
5. **Motor för säkerhetskopiering och migrering av status**
   - Ögonblicksbild av alla 36 anslutningar, 13 anpassade noder (B.AI, Dahl, Vyce med mera) och kombinationer.

## Struktur
- `apply-update.ps1` – uppdateringsskript med ett klick som säkert stoppar, säkerhetskopierar, uppgraderar till v0.5.65-extra, återställer tillståndet och startar om 9router.
- `packages/`
  - `9router-0.5.65-extra.tgz` – ett fullständigt byggt och patchat paket för v0.5.65, redo för global installation.
  - `9router-0.5.59-agentrouter.tgz` – äldre v0.5.59-paket.
- `patches/`
  - `9router-0.5.65-extra-unified.patch` – enhetlig patch mot uppströmsversionen v0.5.65.
  - `9router-extra-unified.patch` – enhetlig patch mot v0.5.59.
- `backup/`
  - `export_state.js` & `restore_state.js` – verktyg för säkerhetskopiering och synkronisering av SQLite-tillstånd
    (privat tillstånd lagras nu i `%LOCALAPPDATA%\9router_WatchEdit\backups\private\`, aldrig i repot).

## Kör 9Router-källkodens tester (vitest-anropsfälla, T-21)
Källkoden till Next.js-dashboarden ligger i `%APPDATA%\9router\source`. Dess vitest-sökvägsalias
(`@/` → `src`, `open-sse` → `open-sse`) definieras ENDAST i
`tests/vitest.config.js`.

**Kör INTE binärfilen från källkodens rotkatalog:**
```powershell
# WRONG: bypasses tests/vitest.config.js, every "@/..." import fails
.\tests\node_modules\.bin\vitest.cmd run tests/unit/model-test-routing.test.js
#   -> Error: Cannot find package '@/shared/constants/config' imported from
#      src/app/api/models/test/ping.js
```

Använd i stället det konfigurationsmedvetna anropet:
```powershell
# RIGHT: loads tests/vitest.config.js (aliases resolve)
npm --prefix tests test -- unit/model-test-routing.test.js
```

Med en `vitest.config.js` på rotnivån som återexporterar testkonfigurationen fungerar även
rotbinärfilen; den ligger kvar i källträdet så att båda anropen är överensstämmande.
`tests/unit/model-routing.test.js` har ett orelaterat, befintligt `EPERM` vid nedrivning av
`os.tmpdir()` i den här miljön – den misslyckas på samma sätt med båda anropen och är inte ett
aliasproblem.

## Säkerhetsmodell
Det här repot innehåller inga hemligheter och är säkert att lämna till externa kodningsagenter.
Hemligheter finns endast i den lokala körningslagret (`%LOCALAPPDATA%\9router_WatchEdit\` och
9Router-installationen). WatchEdit kör `SECRETS: LOCKED` som standard; aktiva leverantörsåtgärder
kräver en explicit upplåsning (OS-baserat beviljande eller ett valfritt huvudlösenordsvalv).
Se **SECURITY_LOCAL.md** för hela modellen, byggaren för säkra delningar
(`python tools/build_safe_share.py`), hemlighetsskannern och verktygen för privata säkerhetskopior.
  - `data-snapshot/` – fullständig databaskopia av `%APPDATA%\9router\db\data.sqlite`.

## Så tillämpar du uppdateringen
När dina pågående uppgifter i 9router är klara kan du helt enkelt köra:
```powershell
.\apply-update.ps1
```
Det här gör följande:
1. Stoppar den pågående 9router-instansen.
2. Skapar en tidsstämpeld säkerhetskopia i `%APPDATA%\9router_backup_<timestamp>`.
3. Installerar `9router-0.5.65-extra.tgz` globalt.
4. Synkroniserar alla anslutningar och uppdaterar SAIFREN-kombinationen.
5. Startar om 9router i fackläge.
<!-- translation-model: zcode-agent translation (not local Ollama pipeline) -->
<!-- source-digest: README.md sha256:082c6cba0dbf49757cbab042ad6e9fec1162ed12091176b2cef3ed1ffe6c5c4c -->
