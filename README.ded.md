# 9Router Extra / Сканер да интеграционный набор

Версия: **v0.2.0**
Проект: 9router_extra
Путь: `V:\___VAC\__K\__CODE\_PY\_9router_extra\`

## Коротко
Репа держит расширения, тулзы, бекапы состояния и патчи для 9Router:
1. **Мост к провайдеру WorkBuddy AI (`wb/hy3`)**
   - Прямая интеграция с облачными completions WorkBuddy AI (`https://www.workbuddy.ai/v2/chat/completions`).
   - Держит рассуждающую модель Hunyuan `hy3` (включает `stream: true`, типизированные блоки контента, `reasoning_effort: high`, `reasoning_summary: auto`).
   - Локальный токен подтягивается сам из `%LOCALAPPDATA%\CodeBuddyExtension\Data\Public\auth\workbuddy-desktop-ai.info`.
   - Есть обновление токена и учёт квоты.
2. **Починка Antigravity Gemini 3.8 Flash и разбор уровня на лету**
   - Лечит 404 наверху: верхний Antigravity требует `gemini-3.8-flash-tiered(high|medium|low)`.
   - Добавлен разбор шаблонов на лету, так что любой `gemini-*-flash-(high|medium|low)` ложится в формат с уровнем и 404 не лезет.
3. **Комбо SAIFREN с новой головы**
   - `wb/hy3` встаёт первой моделью, `ag/gemini-3.8-flash-high` — второй, в комбо `SAIFREN`.
4. **Патчи AgentRouter и Cline**
   - Встроенный провайдер AgentRouter, перенос клиентского User-Agent, разбор базового URL Anthropic и починки маршрутизации Cline.
5. **Движок бекапа состояния и миграции**
   - Снимок всех 36 соединений, 13 своих узлов (B.AI, Dahl, Vyce и прочие) и комбо.

## Что где
- `apply-update.ps1` - апдейт в один клик: гасит 9router, снимает бекап, катит на v0.5.65-extra, возвращает состояние и поднимает 9router обратно.
- `packages/`
  - `9router-0.5.65-extra.tgz` - Полностью собранный и пропатченный пакет v0.5.65, готов к глобальной установке.
  - `9router-0.5.59-agentrouter.tgz` - Древний пакет v0.5.59.
- `patches/`
  - `9router-0.5.65-extra-unified.patch` - Единый патч поверх верхнего v0.5.65.
  - `9router-extra-unified.patch` - Единый патч поверх v0.5.59.
- `backup/`
  - `export_state.js` & `restore_state.js` - Инструменты для бекапа и синка состояния SQLite
    (приватное состояние теперь уезжает в `%LOCALAPPDATA%\9router_WatchEdit\backups\private\`, и в эту репу — никогда).

## Тесты исходников 9Router (ловушка вызова vitest, T-21)
Исходники Next.js-дашборда лежат в `%APPDATA%\9router\source`. Алиасы путей vitest
(`@/` → `src`, `open-sse` → `open-sse`) заданы ТОЛЬКО в
`tests/vitest.config.js`.

**НЕ запускай бинарь из корня исходников:**
```powershell
# WRONG: bypasses tests/vitest.config.js, every "@/..." import fails
.\tests\node_modules\.bin\vitest.cmd run tests/unit/model-test-routing.test.js
#   -> Error: Cannot find package '@/shared/constants/config' imported from
#      src/app/api/models/test/ping.js
```

Бери вызов, который подгружает конфиг:
```powershell
# RIGHT: loads tests/vitest.config.js (aliases resolve)
npm --prefix tests test -- unit/model-test-routing.test.js
```

Корневой `vitest.config.js`, который заново экспортирует конфиг тестов, чинит и корневой бинарь; он остаётся в дереве исходников, чтобы оба вызова совпадали.
У `tests/unit/model-routing.test.js` тут свой, давно привычный `EPERM` на уборке `os.tmpdir()` — он валится одинаково при обоих вызовах и к алиасам отношения не имеет.

## Модель безопасности
Секретов в репе нет, отдать её внешним кодовым агентам безопасно.
Секреты живут только в локальном слое исполнения (`%LOCALAPPDATA%\9router_WatchEdit\` и
сама установка 9Router). По умолчанию WatchEdit живёт в `SECRETS: LOCKED`; живые
операции с провайдером требуют явного вскрытия (разрешения от ОС или опционального
сейфа с главным паролем).
Полная модель, SAFE-шредер
(`python tools/build_safe_share.py`), сканер секретов и инструменты приватных бекапов
описаны в **SECURITY_LOCAL.md**.
  - `data-snapshot/` - Полная копия базы `%APPDATA%\9router\db\data.sqlite`.

## Как накатить апдейт
Когда текущие задачи в 9router закрыты, просто запусти:
```powershell
.\apply-update.ps1
```
Он:
1. Гасит работающий экземпляр 9router.
2. Складывает бекап с меткой времени в `%APPDATA%\9router_backup_<timestamp>`.
3. Ставит `9router-0.5.65-extra.tgz` глобально.
4. Синкает все соединения и обновляет комбо SAIFREN.
5. Поднимает 9router в трее.
