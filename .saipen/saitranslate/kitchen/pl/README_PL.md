# 9Router Extra / Zestaw narzędzi do skanowania i integracji

Wersja: **v0.2.0**
Projekt: 9router_extra
Ścieżka: `V:\___VAC\__K\__CODE\_PY\_9router_extra\`

## Przegląd
Repozytorium zawiera rozszerzenia, narzędzia, kopie zapasowe stanu oraz poprawki dla 9Router:
1. **Most dostawcy WorkBuddy AI (`wb/hy3`)**
   - Bezpośrednia integracja z chmurowym API ukończeń WorkBuddy AI (`https://www.workbuddy.ai/v2/chat/completions`).
   - Obsługa modelu rozumowego Hunyuan `hy3` (wymusza `stream: true`, typowane bloki treści, `reasoning_effort: high`, `reasoning_summary: auto`).
   - Automatyczny import lokalnego tokenu z `%LOCALAPPDATA%\CodeBuddyExtension\Data\Public\auth\workbuddy-desktop-ai.info`.
   - Obsługa odświeżania tokenu i śledzenia limitu.
2. **Poprawka Antigravity Gemini 3.8 Flash i dynamiczne określanie poziomu**
   - Usunięcie błędu 404 po stronie nadrzędnej: usługa Antigravity wymaga formatu `gemini-3.8-flash-tiered(high|medium|low)`.
   - Dynamiczne dopasowanie wzorców, dzięki czemu każdy identyfikator `gemini-*-flash-(high|medium|low)` jest mapowany na format z poziomem bez błędu 404.
3. **Wstawianie na początek kombinacji SAIFREN**
   - Skonfigurowano `wb/hy3` jako pierwszy model, a `ag/gemini-3.8-flash-high` jako drugi model w `SAIFREN`.
4. **Poprawki AgentRouter i Cline**
   - Wbudowany dostawca AgentRouter, przekazywanie User-Agent klienta, rozpoznawanie bazowego adresu URL Anthropic oraz poprawki routingu Cline.
5. **Silnik kopii zapasowych i migracji stanu**
   - Migawka wszystkich 36 połączeń, 13 węzłów niestandardowych (B.AI, Dahl, Vyce itd.) i kombinacji.

## Struktura
- `apply-update.ps1` - skrypt aktualizacji jednym kliknięciem, który bezpiecznie zatrzymuje 9router, tworzy kopię zapasową, aktualizuje do v0.5.65-extra, przywraca stan i uruchamia go ponownie.
- `packages/`
  - `9router-0.5.65-extra.tgz` - W pełni zbudowany, poprawiony pakiet v0.5.65, gotowy do instalacji globalnej.
  - `9router-0.5.59-agentrouter.tgz` - Starszy pakiet v0.5.59.
- `patches/`
  - `9router-0.5.65-extra-unified.patch` - Scalona poprawka dla wersji bazowej v0.5.65.
  - `9router-extra-unified.patch` - Scalona poprawka dla wersji v0.5.59.
- `backup/`
  - `export_state.js` & `restore_state.js` - Narzędzia do kopii zapasowej stanu SQLite i synchronizacji
    (stan prywatny jest teraz zapisywany w `%LOCALAPPDATA%\9router_WatchEdit\backups\private\`, a nigdy w tym repozytorium).

## Uruchamianie testów źródłowego kodu 9Router (pułapka wywołania vitest, T-21)
Źródło pulpitu Next.js znajduje się w `%APPDATA%\9router\source`. Aliasy ścieżek vitest
(`@/` → `src`, `open-sse` → `open-sse`) są zdefiniowane WYŁĄCZNIE w
`tests/vitest.config.js`.

**NIE uruchamiaj binarnego pliku z katalogu źródłowego:**
```powershell
# WRONG: bypasses tests/vitest.config.js, every "@/..." import fails
.\tests\node_modules\.bin\vitest.cmd run tests/unit/model-test-routing.test.js
#   -> Error: Cannot find package '@/shared/constants/config' imported from
#      src/app/api/models/test/ping.js
```

Zamiast tego użyj wywołania uwzględniającego konfigurację:
```powershell
# RIGHT: loads tests/vitest.config.js (aliases resolve)
npm --prefix tests test -- unit/model-test-routing.test.js
```

Plik `vitest.config.js` w katalogu głównym, który ponownie eksportuje konfigurację testów, sprawia również, że działa binarny plik z katalogu głównego. Jest utrzymywany w drzewie źródłowym, aby oba sposoby wywołania pozostawały zgodne.
`tests/unit/model-routing.test.js` ma w tym środowisku niepowiązany z aliasami, wcześniej istniejący błąd `EPERM` podczas kończenia testu wywołującego `os.tmpdir()` — w obu sposobach wywołania kończy się on identycznym błędem.

## Model bezpieczeństwa
To repozytorium nie zawiera sekretów i można je bezpiecznie przekazać zewnętrznym agentom programistycznym.
Sekrety znajdują się wyłącznie w lokalnej warstwie uruchomieniowej (`%LOCALAPPDATA%\9router_WatchEdit\` oraz w instalacji 9Router). WatchEdit jest domyślnie uruchamiany w trybie `SECRETS: LOCKED`; operacje dostawcy wymagają jawnego odblokowania (udostępnienie uprawnień przez system operacyjny albo opcjonalny sejf z hasłem głównym).
Zob. **SECURITY_LOCAL.md**, aby poznać pełny model, narzędzie do budowania bezpiecznej wersji do udostępnienia (`python tools/build_safe_share.py`), skaner sekretów i narzędzia do prywatnych kopii zapasowych.
  - `data-snapshot/` - Pełna kopia bazy danych `%APPDATA%\9router\db\data.sqlite`.

## Jak zastosować aktualizację
Po zakończeniu bieżących zadań w 9router wystarczy uruchomić:
```powershell
.\apply-update.ps1
```
Skrypt:
1. Zatrzymuje działającą instancję 9router.
2. Tworzy kopię zapasową z oznaczeniem czasu w `%APPDATA%\9router_backup_<timestamp>`.
3. Instaluje globalnie `9router-0.5.65-extra.tgz`.
4. Synchronizuje wszystkie połączenia i aktualizuje konfigurację SAIFREN.
5. Ponownie uruchamia 9router w trybie zasadnika.

<!-- translation-model: zcode-agent translation (not local Ollama pipeline) -->
<!-- source-digest: README.md sha256:082c6cba0dbf49757cbab042ad6e9fec1162ed12091176b2cef3ed1ffe6c5c4c -->
