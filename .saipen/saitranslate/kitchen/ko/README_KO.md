# 9Router Extra / 스캐너 및 통합 스위트

버전: **v0.2.0**
프로젝트: 9router_extra
경로: `V:\___VAC\__K\__CODE\_PY\_9router_extra\`

## 개요
이 저장소에는 9Router용 확장, 도구, 상태 백업 및 패치가 포함되어 있습니다:
1. **WorkBuddy AI(`wb/hy3`) 공급자 브리지**
   - WorkBuddy AI 클라우드 completion(`https://www.workbuddy.ai/v2/chat/completions`)과의 직접 통합.
   - Hunyuan 추론 모델 `hy3` 지원(`stream: true` 강제, 타입 지정 콘텐츠 블록, `reasoning_effort: high`, `reasoning_summary: auto`).
   - `%LOCALAPPDATA%\CodeBuddyExtension\Data\Public\auth\workbuddy-desktop-ai.info`에서 로컬 토큰 자동 가져오기.
   - 토큰 갱신 및 할당량 추적 지원.
2. **Antigravity Gemini 3.8 Flash 수정 및 동적 티어 해석**
   - 업스트림 404 문제를 수정합니다: 업스트림 Antigravity는 `gemini-3.8-flash-tiered(high|medium|low)`를 요구합니다.
   - 동적 패턴 매칭을 추가하여 모든 `gemini-*-flash-(high|medium|low)`가 404 없이 티어 형식으로 매핑됩니다.
3. **SAIFREN 콤보 앞부분 삽입**
   - `SAIFREN`에서 `wb/hy3`를 1번째 모델로, `ag/gemini-3.8-flash-high`를 2번째 모델로 구성했습니다.
4. **AgentRouter 및 Cline 패치**
   - 네이티브 AgentRouter 공급자, 클라이언트 User-Agent 전달, Anthropic 베이스 URL 해석, Cline 라우팅 수정.
5. **상태 백업 및 마이그레이션 엔진**
   - 36개 연결, 13개 사용자 정의 노드(B.AI, Dahl, Vyce 등) 및 콤보의 스냅샷.

## 구조
- `apply-update.ps1` - 안전하게 중지하고 백업한 뒤 v0.5.65-extra로 업그레이드하고 상태를 복원한 다음 9router를 재시작하는 원클릭 업데이트 스크립트.
- `packages/`
  - `9router-0.5.65-extra.tgz` - 완전히 빌드되고 패치된 v0.5.65 패키지로, 전역 설치가 가능합니다.
  - `9router-0.5.59-agentrouter.tgz` - 레거시 v0.5.59 패키지.
- `patches/`
  - `9router-0.5.65-extra-unified.patch` - v0.5.65 업스트림에 대한 통합 패치.
  - `9router-extra-unified.patch` - v0.5.59에 대한 통합 패치.
- `backup/`
  - `export_state.js` 및 `restore_state.js` - SQLite 상태 백업 및 동기화 도구
    (이제 비공개 상태는 `%LOCALAPPDATA%\9router_WatchEdit\backups\private\`를 대상으로 하며, 이 저장소에는 절대 쓰지 않습니다).

## 9Router 소스 테스트 실행(vitest 호출 함정, T-21)
Next.js 대시보드 소스는 `%APPDATA%\9router\source`에 있습니다. 이 소스의 vitest
경로 별칭(`@/` → `src`, `open-sse` → `open-sse`)은 오직
`tests/vitest.config.js`에만 정의되어 있습니다.

**소스 루트에서 바이너리를 호출하지 마십시오:**
```powershell
# WRONG: bypasses tests/vitest.config.js, every "@/..." import fails
.\tests\node_modules\.bin\vitest.cmd run tests/unit/model-test-routing.test.js
#   -> Error: Cannot find package '@/shared/constants/config' imported from
#      src/app/api/models/test/ping.js
```

대신 구성을 인식하는 호출을 사용하십시오:
```powershell
# RIGHT: loads tests/vitest.config.js (aliases resolve)
npm --prefix tests test -- unit/model-test-routing.test.js
```

테스트 구성을 다시 내보내는 루트 수준의 `vitest.config.js`도 루트 바이너리를
동작하게 합니다. 두 호출이 서로 일치하도록 이 파일은 소스 트리에 유지됩니다.
`tests/unit/model-routing.test.js`에는 이 환경의 `os.tmpdir()` 정리 단계에서
무관한 기존 `EPERM`이 있습니다. 이는 두 호출 모두에서 동일하게 실패하며 별칭
문제가 아닙니다.

## 보안 모델
이 저장소는 비밀 정보가 없으며 외부 코딩 에이전트에게 전달해도 안전합니다.
비밀 정보는 로컬 런타임 계층(`%LOCALAPPDATA%\9router_WatchEdit\` 및
9Router 설치본)에만 존재합니다. WatchEdit는 기본적으로 `SECRETS: LOCKED`로 실행되며, 라이브
공급자 작업에는 명시적인 잠금 해제(운영체제 기반 허가 또는 선택적 마스터 비밀번호 보관소)가 필요합니다.
전체 모델, SAFE 공유 빌더(`python tools/build_safe_share.py`), 비밀 스캐너 및
비공개 백업 도구는 **SECURITY_LOCAL.md**를 참조하십시오.
  - `data-snapshot/` - `%APPDATA%\9router\db\data.sqlite`의 전체 데이터베이스 복사본.

## 업데이트 적용 방법
9router에서 진행 중인 작업이 끝나면 다음을 실행하기만 하면 됩니다:
```powershell
.\apply-update.ps1
```
이 작업은 다음을 수행합니다:
1. 실행 중인 9router 인스턴스를 중지합니다.
2. `%APPDATA%\9router_backup_<timestamp>`에 타임스탬프가 포함된 백업을 만듭니다.
3. `9router-0.5.65-extra.tgz`를 전역으로 설치합니다.
4. 모든 연결을 동기화하고 SAIFREN 콤보를 업데이트합니다.
5. 9router를 트레이 모드로 재시작합니다.

<!-- translation-model: codebuff-agent in-session translation (not the local qwen3:14b pipeline) -->
<!-- source-digest: README.md sha256:082c6cba0dbf49757cbab042ad6e9fec1162ed12091176b2cef3ed1ffe6c5c4c -->
