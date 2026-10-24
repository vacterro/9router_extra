# 9Router إضافي / ماسح وحزمة التكامل

النسخة:**v0.2.0**
المشروع: 9router_extra
المسار:`V:\___VAC\__K\__CODE\_PY\_9router_extra\`

## نظرة عامة
يحتوي هذا المستودع على الامتدادات والأدوات والنسخ الاحتياطية للحالة والرقع الخاصة بـ 9Router:
1. **جسر مزود WorkBuddy AI (`wb/hy3`)**
   - التكامل المباشر مع إكمال WorkBuddy AI السحابي (`https://www.workbuddy.ai/v2/chat/completions`).
   - يدعم نموذج استدلال Hunyuan `hy3` (يفرض `stream: true`، وكتل المحتوى ذات الأنواع المحددة، و`reasoning_effort: high`، و`reasoning_summary: auto`).
   - استيراد الرموز المحلية تلقائيًا من`%LOCALAPPDATA%\CodeBuddyExtension\Data\Public\auth\workbuddy-desktop-ai.info`.
   - دعم تحديث الرموز وإدارة الحصص.
2. **إصلاح Gemini 3.8 Flash لـ Antigravity وحل التدرج الديناميكي**
   - إصلاح مشكلة 404 في المصدر: يتطلب Antigravity الأصلي الصيغة`gemini-3.8-flash-tiered(high|medium|low)`.
   - إضافة مطابقة نمط ديناميكية تتيح لأي`gemini-*-flash-(high|medium|low)`التوافق مع صيغة الفئات دون 404.
3. **إضافة مسبقة إلى مجموعة SAIFREN**
   - تم تكوين`wb/hy3`كنموذج أول و`ag/gemini-3.8-flash-high`كنموذج ثاني في`SAIFREN`.
4. **رقع AgentRouter وCline**
   - مزوّد AgentRouter أصلي، وإعادة توجيه User-Agent من العميل، وحل عنوان URL الأساسي لـ Anthropic، وإصلاحات توجيه Cline.
5. **محرك النسخ الاحتياطي للحالة وترحيلها**
   - لقطة لجميع الاتصالات الـ36، و13 عقدة مخصصة (B.AI وDahl وVyce وغيرها)، والمجموعات.

## الهيكل
- `apply-update.ps1` - سكربت تحديث بنقرة واحدة يوقف التشغيل بأمان، وينشئ نسخة احتياطية، ويرقّي إلى v0.5.65-extra، ويستعيد الحالة، ويعيد تشغيل 9router.
- `packages/`
  - `9router-0.5.65-extra.tgz` - حزمة v0.5.65 مبنية بالكامل ومُصححة، جاهزة للتثبيت العام.
  - `9router-0.5.59-agentrouter.tgz` - حزمة قديمة v0.5.59.
- `patches/`
  - `9router-0.5.65-extra-unified.patch` - رقعة موحدة مقابل v0.5.65 الأصلي.
  - `9router-extra-unified.patch` - رقعة موحدة مقابل v0.5.59.
- `backup/`
  - `export_state.js` و`restore_state.js` - أدوات للنسخ الاحتياطي ومزامنة حالة SQLite الخاصة
    (تستهدف الحالة الخاصة الآن `%LOCALAPPDATA%\9router_WatchEdit\backups\private\`، ولا تُخزَّن في هذا المستودع).

## تشغيل اختبارات مصدر 9Router(فخ استدعاء vitest، T-21)
مصدر لوحة تحكم Next.js موجود في`%APPDATA%\9router\source`. وأسماء المسارات المستعارة في vitest
(`@/` → `src`, `open-sse` → `open-sse`)محددة فقط في
`tests/vitest.config.js`.

**لا تُنفِّذ الملف التنفيذي من الجذر المصدر:**
```powershell
# WRONG: bypasses tests/vitest.config.js, every "@/..." import fails
.\tests\node_modules\.bin\vitest.cmd run tests/unit/model-test-routing.test.js
#   -> Error: Cannot find package '@/shared/constants/config' imported from
#      src/app/api/models/test/ping.js
```

استخدم الدعوة المُدرَّبة على الإعدادات بدلًا من ذلك:
```powershell
# RIGHT: loads tests/vitest.config.js (aliases resolve)
npm --prefix tests test -- unit/model-test-routing.test.js
```

مستند مستوى الجذر`vitest.config.js`الذي يعيد تصدير إعدادات الاختبارات يجعل أيضًا الملف التنفيذي الجذري يعمل؛ ويُحتفظ به في شجرة المصدر حتى تتسق كل الاستدعاءات.
`tests/unit/model-routing.test.js`لديها خطأ`EPERM`غير مرتبط موجود مسبقًا عند تفكيك `os.tmpdir()`في هذا البيئة — تفشل بالطريقة نفسها تحت كلا الاستدعاءين وليست مشكلة تتعلق بالأسماء المستعارة.

## نموذج الأمان
هذا المستودع خالي من الأسرار ويمكن تمريره بأمان إلى وكلاء البرمجة الخارجيين.
الأسرار موجودة فقط في طبقة التشغيل المحلية (`%LOCALAPPDATA%\9router_WatchEdit\` وتثبيت 9Router). يعمل WatchEdit افتراضيًا في وضع `SECRETS: LOCKED`؛ وتتطلب عمليات المزود الحية فتح القفل صراحةً (منح مدعوم من النظام أو خزنة كلمة مرور رئيسية اختيارية). راجع **SECURITY_LOCAL.md** للنموذج الكامل، وباني المشاركة الآمنة (`python tools/build_safe_share.py`)، وماسح الأسرار، وأدوات النسخ الاحتياطي الخاص.
  - `data-snapshot/`- نسخة كاملة من قاعدة بيانات`%APPDATA%\9router\db\data.sqlite`.

## كيفية تطبيق التحديث
عندما تنتهي مهامك النشطة في 9router، ما عليك سوى تشغيل:
```powershell
.\apply-update.ps1
```
سيؤدي هذا إلى:
1. إيقاف مثيل 9router الذي يعمل حاليًا.
2. إنشاء نسخة احتياطية تحمل طابعًا زمنيًا في`%APPDATA%\9router_backup_<timestamp>`.
3. تثبيت`9router-0.5.65-extra.tgz`عالميًا.
4. مزامنة جميع الاتصالات وتحديث مجموعة SAIFREN.
5. إعادة تشغيل 9router في وضع شريط النظام.

<!-- translation-model: qwen3:14b contract:structured-markdown-v2 -->
<!-- source-digest: README.md sha256:082c6cba0dbf49757cbab042ad6e9fec1162ed12091176b2cef3ed1ffe6c5c4c -->
