# 9Router Extra / Скенер и интеграционен пакет

Версия:**v0.2.0**
Проект: 9router_extra
Път:`V:\___VAC\__K\__CODE\_PY\_9router_extra\`

## Обобщение
Този репозиторий съдържа разширения, инструменти, състояние за резервно копие и патчи за 9Router:
1. **WorkBuddy AI(`wb/hy3`)Провайдър мост**
   - Пълна интеграция с облака на WorkBuddy AI(`https://www.workbuddy.ai/v2/chat/completions`).
   - Поддържа модела за разсъждение Hunyuan`hy3` (forces`stream: true`, типирани блокове съдържание,`reasoning_effort: high`, `reasoning_summary: auto`).
   - Автоматично внасяне на локален токен от`%LOCALAPPDATA%\CodeBuddyExtension\Data\Public\auth\workbuddy-desktop-ai.info`.
   - Поддръжка за освежаване на токена и следене на квотата.
2. **Fix & Динамично ниво на решаване на Antigravity Gemini 3.8 Flash**
   - Поправка на проблема с 404 в горния поток: горният Antigravity изисква`gemini-3.8-flash-tiered(high|medium|low)`.
   - Добавя динамичен модел за съвпадение, така че всеки`gemini-*-flash-(high|medium|low)`се съпоставя към стъпаловиден формат без 404.
3. **Добавяне в началото на комбинацията SAIFREN**
   - Конфигурирано`wb/hy3`като първия модел и`ag/gemini-3.8-flash-high`като втория модел в`SAIFREN`.
4. **Кръпки за AgentRouter и Cline**
   - Нативен AgentRouter провайдър, пренасяне на потребителския агент от клиента, разрешаване на основния URL на Anthropic и поправки за маршрутизация на Cline.
5. **Състояние: Резервно копие и миграционен двигател**
   - Снимка на всички 36 връзки, 13 персонализирани възли(B.AI, Dahl, Vyce и др.), и комбинации.

## Структура
- `apply-update.ps1`- Скрипт за 1 клик за безопасно спиране, резервно копие, обновяване до v0.5.65-extra, възстановяване на състоянието и рестартиране на 9router.
- `packages/`
  - `9router-0.5.65-extra.tgz`- Пълно изградено, патчирано v0.5.65 пакет, готов за глобално инсталиране.
  - `9router-0.5.59-agentrouter.tgz`- Стар пакет v0.5.59.
- `patches/`
  - `9router-0.5.65-extra-unified.patch`- Обединен патч срещу v0.5.65 upstream.
  - `9router-extra-unified.patch`- Обединен патч срещу v0.5.59.
- `backup/`
  - `export_state.js` & `restore_state.js`- Инструменти за резервно копие и синхронизация на SQLite състояние
    (частно състояние сега целеустремено към`%LOCALAPPDATA%\9router_WatchEdit\backups\private\`, никога този репозиторий).

## Изпълняване на тестовете от източника на 9Router(ловушка за извикване на vitest, T-21)
Източникът на панелът за управление на Next.js се намира в`%APPDATA%\9router\source`. неговият vitest
алиаси за пътища(`@/` → `src`, `open-sse` → `open-sse`)са дефинирани САМО в
`tests/vitest.config.js`.

**Не извиквайте двоичния файл от корен на източника:**
```powershell
# WRONG: bypasses tests/vitest.config.js, every "@/..." import fails
.\tests\node_modules\.bin\vitest.cmd run tests/unit/model-test-routing.test.js
#   -> Error: Cannot find package '@/shared/constants/config' imported from
#      src/app/api/models/test/ping.js
```

Използвайте вместо това извикването, което зарежда конфигурацията:
```powershell
# RIGHT: loads tests/vitest.config.js (aliases resolve)
npm --prefix tests test -- unit/model-test-routing.test.js
```

Ниво на корена`vitest.config.js`който преподава конфигурацията на тестовете, също прави
кореновият двоичен файл да работи; той се съхранява в дървото на източника, така че и двете извиквания да съвпадат.
`tests/unit/model-routing.test.js`има нерелевантна предварително съществуваща`EPERM`на
`os.tmpdir()`почистване в това окружение — проваля се идентично при двете
извиквания и не е проблем с alias.

## Модел на сигурност
Този репозиторий е без тайни и е безопасен за предаване на външни кодови агенти.
Тайните съществуват само в локалния слой на изпълнение(`%LOCALAPPDATA%\9router_WatchEdit\`и
9Router инсталация). WatchEdit се изпълнява`SECRETS: LOCKED`по подразбиране; живи операции на доставчици
изискват явно разблокиране(Грант, подпомогнат от ОС, или опционален сейф с главна парола).
Виж**SECURITY_LOCAL.md**за пълния модел, инструмента за създаване на SAFE споделки
(`python tools/build_safe_share.py`), сканиращият секретен инструмент и инструментите за личен резервен копие.
  - `data-snapshot/`- Пълно копие на базата данни за`%APPDATA%\9router\db\data.sqlite`.

## Как да приложите обновлението
Когато активните ви задачи в 9router се приключат, просто изпълнете:
```powershell
.\apply-update.ps1
```
Това ще:
1. Затвори текущата инстанция на 9router.
2. Създаде временно маркирано резервно копие в`%APPDATA%\9router_backup_<timestamp>`.
3. Инсталирай`9router-0.5.65-extra.tgz`глобално.
4. Синхронизирайте всички връзки и актуализирайте комбинацията SAIFREN.
5. Пуснете 9router отново в режим на областта за уведомления.

<!-- translation-model: qwen3:14b contract:structured-markdown-v2 -->
<!-- source-digest: README.md sha256:082c6cba0dbf49757cbab042ad6e9fec1162ed12091176b2cef3ed1ffe6c5c4c -->
