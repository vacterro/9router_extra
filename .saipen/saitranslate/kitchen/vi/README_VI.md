# 9Router Extra / Bộ công cụ quét và tích hợp

Phiên bản: **v0.2.0**
Dự án: 9router_extra
Đường dẫn: `V:\___VAC\__K\__CODE\_PY\_9router_extra\`

## Tổng quan
Kho lưu trữ này chứa các tiện ích mở rộng, công cụ, bản sao lưu trạng thái và bản vá cho 9Router:
1. **Cầu nối nhà cung cấp WorkBuddy AI (`wb/hy3`)**
   - Tích hợp trực tiếp với dịch vụ hoàn thành trên nền tảng đám mây của WorkBuddy AI (`https://www.workbuddy.ai/v2/chat/completions`).
   - Hỗ trợ mô hình suy luận Hunyuan `hy3` (buộc dùng `stream: true`, các khối nội dung được định kiểu, `reasoning_effort: high`, `reasoning_summary: auto`).
   - Tự động nhập token cục bộ từ `%LOCALAPPDATA%\CodeBuddyExtension\Data\Public\auth\workbuddy-desktop-ai.info`.
   - Hỗ trợ làm mới token và theo dõi hạn mức.
2. **Bản vá Antigravity Gemini 3.8 Flash và phân giải cấp độ động**
   - Khắc phục lỗi 404 từ phía thượng nguồn: Antigravity thượng nguồn yêu cầu `gemini-3.8-flash-tiered(high|medium|low)`.
   - Bổ sung khớp mẫu động để mọi `gemini-*-flash-(high|medium|low)` đều được chuyển sang định dạng có cấp độ, tránh lỗi 404.
3. **Chèn đầu Combo SAIFREN**
   - Cấu hình `wb/hy3` làm mô hình thứ nhất và `ag/gemini-3.8-flash-high` làm mô hình thứ hai trong `SAIFREN`.
4. **Bản vá AgentRouter và Cline**
   - Bổ sung nhà cung cấp AgentRouter tích hợp sẵn, chuyển tiếp client User-Agent, phân giải Anthropic base URL và các bản sửa định tuyến của Cline.
5. **Công cụ sao lưu và di chuyển trạng thái**
   - Ảnh chụp toàn bộ 36 kết nối, 13 node tùy chỉnh (B.AI, Dahl, Vyce, v.v.) và các combo.

## Cấu trúc
- `apply-update.ps1` - Script cập nhật bằng một cú nhấp, giúp dừng an toàn, sao lưu, nâng cấp lên v0.5.65-extra, khôi phục trạng thái và khởi động lại 9router.
- `packages/`
  - `9router-0.5.65-extra.tgz` - Gói v0.5.65 đã được xây dựng đầy đủ và vá, sẵn sàng cài đặt toàn cục.
  - `9router-0.5.59-agentrouter.tgz` - Gói v0.5.59 thế hệ trước.
- `patches/`
  - `9router-0.5.65-extra-unified.patch` - Bản vá hợp nhất cho v0.5.65 thượng nguồn.
  - `9router-extra-unified.patch` - Bản vá hợp nhất cho v0.5.59.
- `backup/`
  - `export_state.js` & `restore_state.js` - Công cụ sao lưu và đồng bộ trạng thái SQLite
    (trạng thái riêng tư hiện lưu vào `%LOCALAPPDATA%\9router_WatchEdit\backups\private\`, không bao giờ nằm trong kho lưu trữ này).

## Chạy kiểm thử mã nguồn 9Router (bẫy khi gọi vitest, T-21)
Mã nguồn bảng điều khiển Next.js nằm trong `%APPDATA%\9router\source`. Các bí danh đường dẫn vitest
(`@/` → `src`, `open-sse` → `open-sse`) chỉ được định nghĩa trong
`tests/vitest.config.js`.

**KHÔNG được gọi binary từ thư mục gốc mã nguồn:**
```powershell
# WRONG: bypasses tests/vitest.config.js, every "@/..." import fails
.\tests\node_modules\.bin\vitest.cmd run tests/unit/model-test-routing.test.js
#   -> Error: Cannot find package '@/shared/constants/config' imported from
#      src/app/api/models/test/ping.js
```

Hãy dùng lệnh gọi có nạp cấu hình:
```powershell
# RIGHT: loads tests/vitest.config.js (aliases resolve)
npm --prefix tests test -- unit/model-test-routing.test.js
```

Một `vitest.config.js` ở cấp thư mục gốc tái xuất cấu hình trong thư mục thử nghiệm cũng giúp tệp nhị phân ở thư mục gốc hoạt động; tệp này được giữ trong cây mã nguồn để cả hai cách gọi cho kết quả nhất quán.
`tests/unit/model-routing.test.js` gặp lỗi `EPERM` có sẵn, không liên quan, trong bước dọn dẹp qua `os.tmpdir()` trên môi trường này — lỗi xuất hiện giống hệt với cả hai cách gọi và không phải do vấn đề bí danh.

## Mô hình bảo mật
Kho lưu trữ này không chứa bí mật và có thể an toàn giao cho các tác nhân lập trình bên ngoài.
Bí mật chỉ nằm trong lớp thời gian chạy cục bộ (`%LOCALAPPDATA%\9router_WatchEdit\` và bản cài đặt
9Router). WatchEdit chạy ở trạng thái `SECRETS: LOCKED` theo mặc định; các thao tác trực tiếp với nhà cung cấp
yêu cầu mở khóa rõ ràng (quyền được hỗ trợ bởi hệ điều hành hoặc vault mật khẩu chính tùy chọn).
Xem **SECURITY_LOCAL.md** để biết mô hình đầy đủ, trình tạo bản chia sẻ SAFE
(`python tools/build_safe_share.py`), trình quét bí mật và các công cụ sao lưu riêng tư.
  - `data-snapshot/` - Bản sao toàn bộ cơ sở dữ liệu `%APPDATA%\9router\db\data.sqlite`.

## Cách áp dụng bản cập nhật
Khi các tác vụ đang hoạt động trong 9router đã hoàn tất, chỉ cần chạy:
```powershell
.\apply-update.ps1
```
Thao tác này sẽ:
1. Dừng tiến trình 9router đang chạy.
2. Tạo bản sao lưu có dấu thời gian trong `%APPDATA%\9router_backup_<timestamp>`.
3. Cài đặt `9router-0.5.65-extra.tgz` toàn cục.
4. Đồng bộ mọi kết nối và cập nhật combo SAIFREN.
5. Khởi động lại 9router ở chế độ tray.
<!-- translation-model: zcode-agent translation (not local Ollama pipeline) -->
<!-- source-digest: README.md sha256:082c6cba0dbf49757cbab042ad6e9fec1162ed12091176b2cef3ed1ffe6c5c4c -->
