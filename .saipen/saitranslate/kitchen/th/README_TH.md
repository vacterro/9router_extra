# 9Router Extra / ชุดสแกนและการเชื่อมต่อ

เวอร์ชัน: **v0.2.0**
โปรเจกต์: 9router_extra
พาธ: `V:\___VAC\__K\__CODE\_PY\_9router_extra\`

## ภาพรวม
ที่เก็บนี้ประกอบด้วยส่วนขยาย เครื่องมือ ข้อมูลสำรองสถานะ และแพตช์สำหรับ 9Router:
1. **สะพานผู้ให้บริการ WorkBuddy AI (`wb/hy3`)**
   - เชื่อมต่อโดยตรงกับบริการเติมข้อความของ WorkBuddy AI บนคลาวด์ (`https://www.workbuddy.ai/v2/chat/completions`)
   - รองรับโมเดลการคิดเชิงเหตุผลของ Hunyuan คือ `hy3` (บังคับ `stream: true`, บล็อกเนื้อหาแบบมีชนิดข้อมูล, `reasoning_effort: high`, `reasoning_summary: auto`)
   - นำเข้าโทเคนจากเครื่องโดยอัตโนมัติที่ `%LOCALAPPDATA%\CodeBuddyExtension\Data\Public\auth\workbuddy-desktop-ai.info`
   - รองรับการรีเฟรชโทเคนและติดตามโควตา
2. **แก้ไข Antigravity Gemini 3.8 Flash และการระบุระดับแบบไดนามิก**
   - แก้ไขปัญหา 404 จากส่วนต้นทาง: upstream Antigravity ต้องใช้ `gemini-3.8-flash-tiered(high|medium|low)`
   - เพิ่มการจับคู่รูปแบบแบบไดนามิก เพื่อให้ `gemini-*-flash-(high|medium|low)` ทุกรูปแบบแมปไปยังรูปแบบ tiered โดยไม่เกิด 404
3. **เพิ่มคอมโบ SAIFREN ไว้ที่ต้นรายการ**
   - กำหนดค่า `wb/hy3` เป็นโมเดลอันดับ 1 และ `ag/gemini-3.8-flash-high` เป็นโมเดลอันดับ 2 ใน `SAIFREN`
4. **แพตช์ AgentRouter และ Cline**
   - รองรับผู้ให้บริการ AgentRouter แบบเนทีฟ ส่งต่อ User-Agent ของไคลเอนต์ การกำหนด base URL ของ Anthropic และการแก้ไข routing ของ Cline
5. **เครื่องมือสำรองและย้ายข้อมูลสถานะ**
   - สแนปช็อตการเชื่อมต่อทั้งหมด 36 รายการ โหนดกำหนดค่าเพิ่มเติม 13 รายการ (B.AI, Dahl, Vyce ฯลฯ) และคอมโบทั้งหมด

## โครงสร้าง
- `apply-update.ps1` - สคริปต์อัปเดตแบบคลิกเดียวเพื่อหยุดอย่างปลอดภัย สำรองข้อมูล อัปเกรดเป็น v0.5.65-extra กู้คืนสถานะ แล้วเริ่ม 9router ใหม่
- `packages/`
  - `9router-0.5.65-extra.tgz` - แพ็กเกจ v0.5.65 ที่สร้างเสร็จสมบูรณ์และปรับแต่งแล้ว พร้อมติดตั้งแบบทั้งระบบ
  - `9router-0.5.59-agentrouter.tgz` - แพ็กเกจรุ่นเก่า v0.5.59
- `patches/`
  - `9router-0.5.65-extra-unified.patch` - แพตช์แบบรวมสำหรับ upstream v0.5.65
  - `9router-extra-unified.patch` - แพตช์แบบรวมสำหรับ v0.5.59
- `backup/`
  - `export_state.js` และ `restore_state.js` - เครื่องมือสำรองและซิงก์สถานะ SQLite
    (ขณะนี้สถานะส่วนตัวเป้าหมายไปที่ `%LOCALAPPDATA%\9router_WatchEdit\backups\private\` ไม่ใช่ที่เก็บนี้)

## การรันการทดสอบจากซอร์สโค้ดของ 9Router (ข้อควรระวังการเรียก vitest, T-21)
ซอร์สโค้ดแดชบอร์ด Next.js อยู่ที่ `%APPDATA%\9router\source` ส่วนนามแทนพาธของ vitest (`@/` → `src`, `open-sse` → `open-sse`) ถูกกำหนดไว้เฉพาะใน
`tests/vitest.config.js`

**ห้ามเรียกใช้ไบนารีจากรูทของซอร์สโค้ด:**
```powershell
# WRONG: bypasses tests/vitest.config.js, every "@/..." import fails
.\tests\node_modules\.bin\vitest.cmd run tests/unit/model-test-routing.test.js
#   -> Error: Cannot find package '@/shared/constants/config' imported from
#      src/app/api/models/test/ping.js
```

ให้ใช้วิธีเรียกที่รู้จัก config แทน:
```powershell
# RIGHT: loads tests/vitest.config.js (aliases resolve)
npm --prefix tests test -- unit/model-test-routing.test.js
```

ไฟล์ `vitest.config.js` ที่ระดับรูทซึ่ง re-export config ของ tests จะทำให้ไบนารีที่รูททำงานได้เช่นกัน ไฟล์นี้ถูกเก็บไว้ในแหล่งซอร์สเพื่อให้ทั้งสองวิธีเรียกใช้สอดคล้องกัน
`tests/unit/model-routing.test.js` มีปัญหา `EPERM` ที่ไม่เกี่ยวข้องกันและมีมาก่อนแล้วในขั้นตอน teardown ของ `os.tmpdir()` ในสภาพแวดล้อมนี้ — เทสต์จะล้มเหลวเหมือนกันทั้งสองวิธีเรียก และไม่ใช่ปัญหาของ alias

## โมเดลความปลอดภัย
ที่เก็บนี้ไม่มีข้อมูลลับ จึงส่งให้เอเจนต์เขียนโค้ดภายนอกได้อย่างปลอดภัย
ข้อมูลลับมีอยู่เฉพาะในชั้นรันระบบในเครื่อง (`%LOCALAPPDATA%\9router_WatchEdit\` และการติดตั้ง 9Router) โดยค่าเริ่มต้น WatchEdit ทำงานด้วย `SECRETS: LOCKED`; การดำเนินการกับผู้ให้บริการแบบใช้งานจริงต้องปลดล็อกอย่างชัดเจน (สิทธิ์ที่สนับสนุนโดย OS หรือที่เก็บรหัสผ่านแบบ master-password ที่เป็นตัวเลือก)
ดู **SECURITY_LOCAL.md** สำหรับโมเดลฉบับเต็ม, ตัวสร้างชุดแบ่งปันแบบ SAFE (`python tools/build_safe_share.py`), ตัวสแกนข้อมูลลับ และเครื่องมือสำรองข้อมูลส่วนตัว
  - `data-snapshot/` - สำเนาฐานข้อมูล SQLite แบบเต็มของ `%APPDATA%\9router\db\data.sqlite`

## วิธีใช้การอัปเดต
เมื่องานที่กำลังดำเนินอยู่ใน 9router เสร็จสิ้นแล้ว เพียงรัน:
```powershell
.\apply-update.ps1
```
ขั้นตอนนี้จะ:
1. หยุดอินสแตนซ์ 9router ที่กำลังทำงาน
2. สร้างข้อมูลสำรองที่มี timestamp ใน `%APPDATA%\9router_backup_<timestamp>`
3. ติดตั้ง `9router-0.5.65-extra.tgz` แบบทั้งระบบ
4. ซิงก์การเชื่อมต่อทั้งหมดและอัปเดตคอมโบ SAIFREN
5. เริ่ม 9router ใหม่ในโหมด tray
<!-- translation-model: zcode-agent translation (not local Ollama pipeline) -->
<!-- source-digest: README.md sha256:082c6cba0dbf49757cbab042ad6e9fec1162ed12091176b2cef3ed1ffe6c5c4c -->
