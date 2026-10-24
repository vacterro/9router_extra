# 9Router Extra / Scanner- & Integrations-Suite

Version: **v0.2.0**
Projekt: 9router_extra
Pfad: `V:\___VAC\__K\__CODE\_PY\_9router_extra\`

## Überblick
Dieses Repository enthält Erweiterungen, Werkzeuge, Zustands-Backups und Patches für 9Router:
1. **WorkBuddy AI (`wb/hy3`) Provider-Bridge**
   - Direkte Integration mit Cloud-Completions von WorkBuddy AI (`https://www.workbuddy.ai/v2/chat/completions`).
   - Unterstützt das Hunyuan-Reasoning-Modell `hy3` (erzwingt `stream: true`, typisierte Content-Blöcke, `reasoning_effort: high`, `reasoning_summary: auto`).
   - Automatischer lokaler Token-Import aus `%LOCALAPPDATA%\CodeBuddyExtension\Data\Public\auth\workbuddy-desktop-ai.info`.
   - Unterstützung für Token-Refresh und Quota-Verfolgung.
2. **Antigravity Gemini 3.8 Flash Fix & dynamische Tier-Auflösung**
   - Behebt das Upstream-404-Problem: das Upstream-Antigravity verlangt `gemini-3.8-flash-tiered(high|medium|low)`.
   - Fügt dynamisches Muster-Matching hinzu, sodass jedes `gemini-*-flash-(high|medium|low)` ohne 404 auf das Tiered-Format abgebildet wird.
3. **SAIFREN-Combo: Modelle voranstellen**
   - `wb/hy3` als 1. Modell und `ag/gemini-3.8-flash-high` als 2. Modell in `SAIFREN` konfiguriert.
4. **AgentRouter- & Cline-Patches**
   - Nativer AgentRouter-Provider, Weiterleitung des Client-User-Agents, Auflösung der Anthropic-Basis-URL und Korrekturen am Cline-Routing.
5. **Zustands-Backup- & Migrations-Engine**
   - Snapshot aller 36 Verbindungen, 13 benutzerdefinierten Knoten (B.AI, Dahl, Vyce usw.) und Combos.

## Struktur
- `apply-update.ps1` - 1-Klick-Update-Skript, das 9router sicher stoppt, sichert, auf v0.5.65-extra aktualisiert, den Zustand wiederherstellt und neu startet.
- `packages/`
  - `9router-0.5.65-extra.tgz` - vollständig gebautes, gepatchtes v0.5.65-Paket, bereit zur globalen Installation.
  - `9router-0.5.59-agentrouter.tgz` - Legacy-Paket v0.5.59.
- `patches/`
  - `9router-0.5.65-extra-unified.patch` - vereinheitlichter Patch gegen Upstream v0.5.65.
  - `9router-extra-unified.patch` - vereinheitlichter Patch gegen v0.5.59.
- `backup/`
  - `export_state.js` & `restore_state.js` - Werkzeuge für SQLite-Zustands-Backup und -Synchronisierung
    (privater Zustand zielt jetzt auf `%LOCALAPPDATA%\9router_WatchEdit\backups\private\`, nie auf dieses Repository).

## 9Router-Quelltests ausführen (vitest-Aufruf-Falle, T-21)
Der Quellcode des Next.js-Dashboards liegt in `%APPDATA%\9router\source`. Seine vitest-Pfad-Aliase
(`@/` → `src`, `open-sse` → `open-sse`) sind NUR in `tests/vitest.config.js`
definiert.

**Die Binary NICHT aus dem Quell-Root aufrufen:**
```powershell
# WRONG: bypasses tests/vitest.config.js, every "@/..." import fails
.\tests\node_modules\.bin\vitest.cmd run tests/unit/model-test-routing.test.js
#   -> Error: Cannot find package '@/shared/constants/config' imported from
#      src/app/api/models/test/ping.js
```

Stattdessen den config-bewussten Aufruf verwenden:
```powershell
# RIGHT: loads tests/vitest.config.js (aliases resolve)
npm --prefix tests test -- unit/model-test-routing.test.js
```

Eine `vitest.config.js` auf Root-Ebene, die die Test-Config re-exportiert, lässt auch
die Root-Binary funktionieren; sie liegt im Quellbaum, damit beide Aufrufe
übereinstimmen. `tests/unit/model-routing.test.js` hat in dieser Umgebung ein
unabhängiges, vorbestehendes `EPERM` beim Teardown von `os.tmpdir()` — es schlägt
unter beiden Aufrufen identisch fehl und ist kein Alias-Problem.

## Sicherheitsmodell
Dieses Repository ist frei von Geheimnissen und kann bedenkenlos an externe Coding-Agenten weitergegeben werden.
Geheimnisse liegen ausschließlich in der lokalen Laufzeitschicht (`%LOCALAPPDATA%\9router_WatchEdit\` und der
9Router-Installation). WatchEdit läuft standardmäßig mit `SECRETS: LOCKED`; Live-Provider-Operationen
erfordern eine ausdrückliche Freischaltung (OS-gestützte Gewährung oder optionaler Master-Passwort-Tresor).
Das vollständige Modell, der Builder für sicheres Teilen
(`python tools/build_safe_share.py`), der Secret-Scanner und die Werkzeuge für private Backups stehen in
**SECURITY_LOCAL.md**.
  - `data-snapshot/` - vollständige Datenbankkopie von `%APPDATA%\9router\db\data.sqlite`.

## So wird das Update angewendet
Wenn deine aktiven Aufgaben in 9router abgeschlossen sind, führe einfach aus:
```powershell
.\apply-update.ps1
```
Dies wird:
1. Die laufende 9router-Instanz stoppen.
2. Ein zeitgestempeltes Backup in `%APPDATA%\9router_backup_<timestamp>` anlegen.
3. `9router-0.5.65-extra.tgz` global installieren.
4. Alle Verbindungen synchronisieren und die SAIFREN-Combo aktualisieren.
5. 9router mit einem Symbol im Infobereich des Systems neu starten.

<!-- translation-model: codebuff-agent in-session translation (not the local qwen3:14b pipeline) -->
<!-- source-digest: README.md sha256:082c6cba0dbf49757cbab042ad6e9fec1162ed12091176b2cef3ed1ffe6c5c4c -->
