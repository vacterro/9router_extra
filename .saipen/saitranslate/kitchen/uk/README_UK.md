# 9Router Extra / Сканер та інтеграційний пакет

Версія: **v0.2.0**
Проєкт: 9router_extra
Шлях: `V:\___VAC\__K\__CODE\_PY\_9router_extra\`

## Огляд
Цей репозиторій містить розширення, інструменти, резервні копії стану та патчі для 9Router:
1. **Міст провайдера WorkBuddy AI (`wb/hy3`)**
   - Пряма інтеграція з хмарними completion-запитами WorkBuddy AI (`https://www.workbuddy.ai/v2/chat/completions`).
   - Підтримує модель міркування Hunyuan `hy3` (примусово `stream: true`, типізовані блоки вмісту, `reasoning_effort: high`, `reasoning_summary: auto`).
   - Локальне автоматичне імпортування токена з `%LOCALAPPDATA%\CodeBuddyExtension\Data\Public\auth\workbuddy-desktop-ai.info`.
   - Підтримка оновлення токена та обліку квоти.
2. **Виправлення Antigravity Gemini 3.8 Flash та динамічне визначення рівня**
   - Виправляє помилку 404 від upstream: upstream Antigravity вимагає `gemini-3.8-flash-tiered(high|medium|low)`.
   - Додає динамічне зіставлення за шаблоном, тому будь-який `gemini-*-flash-(high|medium|low)` перетворюється на tiered-формат без 404.
3. **Препенд комбо SAIFREN**
   - Налаштовано `wb/hy3` як 1-шу модель і `ag/gemini-3.8-flash-high` як 2-гу модель у `SAIFREN`.
4. **Патчі AgentRouter і Cline**
   - Вбудований провайдер AgentRouter, пересилання User-Agent клієнта, визначення базової URL-адреси Anthropic та виправлення маршрутизації Cline.
5. **Рушій резервного копіювання та міграції стану**
   - Знімок усіх 36 з'єднань, 13 користувацьких вузлів (B.AI, Dahl, Vyce тощо) та комбо.

## Структура
- `apply-update.ps1` - скрипт оновлення в 1 клік, який безпечно зупиняє, робить резервну копію, оновлює до v0.5.65-extra, відновлює стан і перезапускає 9router.
- `packages/`
  - `9router-0.5.65-extra.tgz` - повністю зібраний, пропатчений пакет v0.5.65, готовий до глобального встановлення.
  - `9router-0.5.59-agentrouter.tgz` - застарілий пакет v0.5.59.
- `patches/`
  - `9router-0.5.65-extra-unified.patch` - уніфікований патч проти upstream v0.5.65.
  - `9router-extra-unified.patch` - уніфікований патч проти v0.5.59.
- `backup/`
  - `export_state.js` і `restore_state.js` - інструменти резервного копіювання та синхронізації стану SQLite
    (приватний стан тепер спрямовується до `%LOCALAPPDATA%\9router_WatchEdit\backups\private\`, ніколи до цього репозиторію).

## Запуск тестів джерела 9Router (пастка виклику vitest, T-21)
Джерело панелі Next.js лежить у `%APPDATA%\9router\source`. Його аліаси шляхів
для vitest (`@/` → `src`, `open-sse` → `open-sse`) визначені ЛИШЕ у
`tests/vitest.config.js`.

**НЕ викликайте бінарник із кореня джерела:**
```powershell
# WRONG: bypasses tests/vitest.config.js, every "@/..." import fails
.\tests\node_modules\.bin\vitest.cmd run tests/unit/model-test-routing.test.js
#   -> Error: Cannot find package '@/shared/constants/config' imported from
#      src/app/api/models/test/ping.js
```

Натомість використовуйте виклик, який читає конфіг:
```powershell
# RIGHT: loads tests/vitest.config.js (aliases resolve)
npm --prefix tests test -- unit/model-test-routing.test.js
```

Кореневий `vitest.config.js`, який реекспортує конфіг тестів, також змушує
працювати кореневий бінарник; він зберігається в дереві джерела, щоб обидва
виклики узгоджувалися. `tests/unit/model-routing.test.js` має не пов'язану з цим
давню помилку `EPERM` під час завершення `os.tmpdir()` у цьому середовищі — вона
виникає однаково за обох викликів і не є проблемою аліасів.

## Модель безпеки
Цей репозиторій не містить секретів і безпечний для передачі зовнішнім агентам програмування.
Секрети живуть лише в локальному середовищі виконання (`%LOCALAPPDATA%\9router_WatchEdit\` та
інсталяція 9Router). WatchEdit за замовчуванням працює в режимі `SECRETS: LOCKED`; живі операції з
провайдерами вимагають явного розблокування (дозвіл на рівні ОС або опціональне сховище з головним паролем).
Повний опис моделі, збирач безпечного поширення
(`python tools/build_safe_share.py`), сканер секретів і приватний інструментарій резервного копіювання
див. у **SECURITY_LOCAL.md**.
  - `data-snapshot/` - повна копія бази даних `%APPDATA%\9router\db\data.sqlite`.

## Як застосувати оновлення
Коли ваші активні завдання в 9router завершено, просто виконайте:
```powershell
.\apply-update.ps1
```
Це:
1. Зупинить запущений екземпляр 9router.
2. Створить резервну копію з міткою часу в `%APPDATA%\9router_backup_<timestamp>`.
3. Встановить `9router-0.5.65-extra.tgz` глобально.
4. Синхронізує всі з'єднання та оновить комбо SAIFREN.
5. Перезапустить 9router у режимі трею.

<!-- translation-model: codebuff-agent in-session translation (not the local qwen3:14b pipeline) -->
<!-- source-digest: README.md sha256:082c6cba0dbf49757cbab042ad6e9fec1162ed12091176b2cef3ed1ffe6c5c4c -->
