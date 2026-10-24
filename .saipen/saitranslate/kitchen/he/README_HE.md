# 9Router Extra / סורר וחבילת אינטגרציה

גרסה: **v0.2.0**
פרויקט: 9router_extra
נתיב: `V:\___VAC\__K\__CODE\_PY\_9router_extra\`

## סקירה כללית
מאגר קוד זה כולל הרחבות, כלים, גיבויי מצב ותיקונים עבור 9Router:
1. **גשר ספק WorkBuddy AI (`wb/hy3`)**
   - חיבור ישיר להשלמות בענן של WorkBuddy AI (`https://www.workbuddy.ai/v2/chat/completions`).
   - תמיכה במודל ההסקה Hunyuan `hy3` (מאלץ `stream: true`, בלוקי תוכן מוגדרים, `reasoning_effort: high`, `reasoning_summary: auto`).
   - ייבוא אוטומטי של אסימון מקומי מ-`%LOCALAPPDATA%\CodeBuddyExtension\Data\Public\auth\workbuddy-desktop-ai.info`.
   - תמיכה ברענון האסימון ובמעקב אחרי מכסת השימוש.
2. **תיקון Antigravity Gemini 3.8 Flash ופתרון דינמי של רמות**
   - מתקן תקלת 404 במקור: Antigravity דורש `gemini-3.8-flash-tiered(high|medium|low)`.
   - מוסיף התאמת תבניות דינמית, כך שכל `gemini-*-flash-(high|medium|low)` ממופה לתבנית המדורגת ללא 404.
3. **הוספת SAIFREN Combo לתחילת הרשימה**
   - מגדיר את `wb/hy3` כמודל הראשון ואת `ag/gemini-3.8-flash-high` כמודל השני ב-`SAIFREN`.
4. **תיקוני AgentRouter ו-Cline**
   - ספק AgentRouter מובנה, העברת User-Agent של הלקוח, פתרון כתובת הבסיס של Anthropic ותיקוני ניתוב עבור Cline.
5. **מנוע גיבוי והעברת מצב**
   - תמונת מצב של כל 36 החיבורים, 13 צמתים מותאמים (B.AI, Dahl, Vyce ועוד) והקומבוים.

## מבנה
- `apply-update.ps1` - סקריפט עדכון בלחיצה אחת שעוצר בבטחה, גובה גיבוי, מעדכן לגרסה 0.5.65-extra, משחזר את המצב ומפעיל מחדש את 9router.
- `packages/`
  - `9router-0.5.65-extra.tgz` - חבילה מובנית ומתוקנת במלואה, גרסה 0.5.65 המוכנה להתקנה גלובלית.
  - `9router-0.5.59-agentrouter.tgz` - חבילה מהגרסה הישנה 0.5.59.
- `patches/`
  - `9router-0.5.65-extra-unified.patch` - תיקון מאוחד עבור גרסה 0.5.65 במקור.
  - `9router-extra-unified.patch` - תיקון מאוחד עבור גרסה 0.5.59.
- `backup/`
  - `export_state.js` & `restore_state.js` - כלי גיבוי וסנכרון למצב SQLite
    (המצב הפרטי נשמר כעת ב-`%LOCALAPPDATA%\9router_WatchEdit\backups\private\`, ולעולם אינו נשמר במאגר זה).

## הרצת בדיקות המקור של 9Router (מלכודת הפעלת vitest, T-21)
מקור הלוח המחוונים של Next.js נמצא ב-`%APPDATA%\9router\source`. מגדירי הנתיבים של vitest
(`@/` → `src`, `open-sse` → `open-sse`) מוגדרים **רק** ב-
`tests/vitest.config.js`.

**אין להפעיל את הקובץ הבינארי משורש המקור:**
```powershell
# WRONG: bypasses tests/vitest.config.js, every "@/..." import fails
.\tests\node_modules\.bin\vitest.cmd run tests/unit/model-test-routing.test.js
#   -> Error: Cannot find package '@/shared/constants/config' imported from
#      src/app/api/models/test/ping.js
```

יש להשתמש בהפעלה שמכירה את קובץ התצורה:
```powershell
# RIGHT: loads tests/vitest.config.js (aliases resolve)
npm --prefix tests test -- unit/model-test-routing.test.js
```

קובץ `vitest.config.js` ברמת השורש שמייצא מחדש את תצורת הבדיקות גם מאפשר
לקובץ הבינארי שבשורש לעבוד; הוא נשמר בעץ המקור כדי ששתי ההפעלות יתנהגו באותו אופן.
ל-`tests/unit/model-routing.test.js` כבר קיימת כשלה `EPERM` שאינה קשורה
בפירוק של `os.tmpdir()` בסביבה זו — היא נכשלת באופן זהה בשתי
ההפעלות ואינה בעיית נתיבים.

## מודל אבטחה
מאגר קוד זה נטול סודות ובטוח למסירה לסוכני קוד חיצוניים.
סודות קיימים רק בשכבת ההרצה המקומית (`%LOCALAPPDATA%\9router_WatchEdit\` והתקנת
9Router). WatchEdit פועל כברירת מחדל במצב `SECRETS: LOCKED`; פעולות ספק פעילות
דורשות אישור מפורש (הרשאה הנתמכת במערכת ההפעלה או כספת אופציונלית הנעולה בסיסמת מאסטר).
למודל המלא, בונה שיתוף SAFE (`python tools/build_safe_share.py`), סורק הסודות
וכלי גיבוי פרטי, ראו **SECURITY_LOCAL.md**.
  - `data-snapshot/` - העתקה מלאה של מסד הנתונים מ-`%APPDATA%\9router\db\data.sqlite`.

## כיצד להחיל את העדכון
כאשר כל המשימות הפעילות שלך ב-9router מסתיימות, פשוט הרץ:
```powershell
.\apply-update.ps1
```
פעולה זו תבצע:
1. תעצור את מופע 9router הפעיל.
2. תיצור גיבוי עם חותמת זמן ב-`%APPDATA%\9router_backup_<timestamp>`.
3. תתקין `9router-0.5.65-extra.tgz` ברמה גלובלית.
4. תסנכרן את כל החיבורים ותעדכן את הקומבו של SAIFREN.
5. תפעיל מחדש את 9router במצב מגש.

<!-- translation-model: qwen3:14b contract:structured-markdown-v2 -->
<!-- source-digest: README.md sha256:082c6cba0dbf49757cbab042ad6e9fec1162ed12091176b2cef3ed1ffe6c5c4c -->
