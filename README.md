# 9Router Extra / Scanner & Integration Suite

Version: **v0.1.0**
Project: 9router_extra
Path: `V:\___VAC\__K\__CODE\_PY\_9router_extra\`

## Overview
This repository contains extensions, tools, state backups, and patches for 9Router:
1. **WorkBuddy AI (`wb/hy3`) Provider Bridge**
   - Direct integration with WorkBuddy AI cloud completions (`https://www.workbuddy.ai/v2/chat/completions`).
   - Supports Hunyuan reasoning model `hy3` (forces `stream: true`, typed content blocks, `reasoning_effort: high`, `reasoning_summary: auto`).
   - Local token auto-import from `%LOCALAPPDATA%\CodeBuddyExtension\Data\Public\auth\workbuddy-desktop-ai.info`.
   - Token refresh and quota tracking support.
2. **Antigravity Gemini 3.8 Flash Fix & Dynamic Tier Resolution**
   - Fixes upstream 404 issue: upstream Antigravity requires `gemini-3.8-flash-tiered(high|medium|low)`.
   - Adds dynamic pattern matching so any `gemini-*-flash-(high|medium|low)` maps to tiered format without 404.
3. **SAIFREN Combo Prepend**
   - Configured `wb/hy3` as the 1st model and `ag/gemini-3.8-flash-high` as 2nd model in `SAIFREN`.
4. **AgentRouter & Cline Patches**
   - Native AgentRouter provider, client User-Agent forwarding, Anthropic base URL resolution, and Cline routing fixes.
5. **State Backup & Migration Engine**
   - Snapshot of all 36 connections, 13 custom nodes (B.AI, Dahl, Vyce, etc.), and combos.

## Structure
- `apply-update.ps1` - 1-click update script to safely stop, backup, upgrade to v0.5.65-extra, restore state, and restart 9router.
- `packages/`
  - `9router-0.5.65-extra.tgz` - Fully built, patched v0.5.65 package ready for global install.
  - `9router-0.5.59-agentrouter.tgz` - Legacy v0.5.59 package.
- `patches/`
  - `9router-0.5.65-extra-unified.patch` - Unified patch against v0.5.65 upstream.
  - `9router-extra-unified.patch` - Unified patch against v0.5.59.
- `backup/`
  - `export_state.js` & `restore_state.js` - SQLite state backup and sync tools
    (private state now targets `%LOCALAPPDATA%\9router_WatchEdit\backups\private\`, never this repository).

## Running the 9Router source tests (vitest invocation trap, T-21)
The Next.js dashboard source lives in `%APPDATA%\9router\source`. Its vitest
path aliases (`@/` → `src`, `open-sse` → `open-sse`) are defined ONLY in
`tests/vitest.config.js`.

**Do NOT invoke the binary from the source root:**
```powershell
# WRONG: bypasses tests/vitest.config.js, every "@/..." import fails
.\tests\node_modules\.bin\vitest.cmd run tests/unit/model-test-routing.test.js
#   -> Error: Cannot find package '@/shared/constants/config' imported from
#      src/app/api/models/test/ping.js
```

Use the config-aware invocation instead:
```powershell
# RIGHT: loads tests/vitest.config.js (aliases resolve)
npm --prefix tests test -- unit/model-test-routing.test.js
```

A root-level `vitest.config.js` that re-exports the tests config also makes the
root binary work; it is kept in the source tree so both invocations agree.
`tests/unit/model-routing.test.js` has an unrelated pre-existing `EPERM` on
`os.tmpdir()` teardown in this environment — it fails identically under both
invocations and is not an alias problem.

## Security model
This repository is secret-free and safe to hand to external coding agents.
Secrets live only in the local runtime layer (`%LOCALAPPDATA%\9router_WatchEdit\` and the
9Router install). WatchEdit runs `SECRETS: LOCKED` by default; live provider operations
require an explicit unlock (OS-backed grant or optional master-password vault).
See **SECURITY_LOCAL.md** for the full model, the SAFE share builder
(`python tools/build_safe_share.py`), the secret scanner, and private-backup tooling.
  - `data-snapshot/` - Full database copy of `%APPDATA%\9router\db\data.sqlite`.

## How to Apply Update
When your active tasks in 9router are finished, simply run:
```powershell
.\apply-update.ps1
```
This will:
1. Stop the running 9router instance.
2. Create a timestamped backup in `%APPDATA%\9router_backup_<timestamp>`.
3. Install `9router-0.5.65-extra.tgz` globally.
4. Synchronize all connections and update the SAIFREN combo.
5. Restart 9router in tray mode.
