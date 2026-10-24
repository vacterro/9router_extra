# 9Router Extra / 扫描器与集成套件

版本：**v0.2.0**
项目：9router_extra
路径：`V:\___VAC\__K\__CODE\_PY\_9router_extra\`

## 概述
本仓库包含 9Router 的扩展、工具、状态备份和补丁：
1. **WorkBuddy AI（`wb/hy3`）提供商桥接**
   - 与 WorkBuddy AI 云端补全（`https://www.workbuddy.ai/v2/chat/completions`）直接集成。
   - 支持 Hunyuan 推理模型 `hy3`（强制 `stream: true`、带类型的内容块、`reasoning_effort: high`、`reasoning_summary: auto`）。
   - 从 `%LOCALAPPDATA%\CodeBuddyExtension\Data\Public\auth\workbuddy-desktop-ai.info` 自动导入本地令牌。
   - 支持令牌刷新和配额跟踪。
2. **Antigravity Gemini 3.8 Flash 修复与动态层级解析**
   - 修复上游 404 问题：上游 Antigravity 要求 `gemini-3.8-flash-tiered(high|medium|low)`。
   - 增加动态模式匹配，任何 `gemini-*-flash-(high|medium|low)` 都会映射为层级格式，不再返回 404。
3. **SAIFREN 组合前置**
   - 在 `SAIFREN` 中将 `wb/hy3` 配置为第 1 个模型，`ag/gemini-3.8-flash-high` 配置为第 2 个模型。
4. **AgentRouter 与 Cline 补丁**
   - 原生 AgentRouter 提供商、客户端 User-Agent 转发、Anthropic 基础 URL 解析以及 Cline 路由修复。
5. **状态备份与迁移引擎**
   - 全部 36 个连接、13 个自定义节点（B.AI、Dahl、Vyce 等）以及组合的快照。

## 结构
- `apply-update.ps1` - 一键更新脚本：安全停止、备份、升级到 v0.5.65-extra、恢复状态并重启 9router。
- `packages/`
  - `9router-0.5.65-extra.tgz` - 已完整构建并打补丁的 v0.5.65 包，可直接全局安装。
  - `9router-0.5.59-agentrouter.tgz` - 旧版 v0.5.59 包。
- `patches/`
  - `9router-0.5.65-extra-unified.patch` - 针对 v0.5.65 上游的统一补丁。
  - `9router-extra-unified.patch` - 针对 v0.5.59 的统一补丁。
- `backup/`
  - `export_state.js` 与 `restore_state.js` - SQLite 状态备份和同步工具
    （私有状态现在写入 `%LOCALAPPDATA%\9router_WatchEdit\backups\private\`，绝不写入本仓库）。

## 运行 9Router 源码测试（vitest 调用陷阱，T-21）
Next.js 仪表盘的源码位于 `%APPDATA%\9router\source`。它的 vitest 路径别名
（`@/` → `src`，`open-sse` → `open-sse`）只在 `tests/vitest.config.js`
中定义。

**不要从源码根目录调用该二进制：**
```powershell
# WRONG: bypasses tests/vitest.config.js, every "@/..." import fails
.\tests\node_modules\.bin\vitest.cmd run tests/unit/model-test-routing.test.js
#   -> Error: Cannot find package '@/shared/constants/config' imported from
#      src/app/api/models/test/ping.js
```

请改用能读取配置的调用方式：
```powershell
# RIGHT: loads tests/vitest.config.js (aliases resolve)
npm --prefix tests test -- unit/model-test-routing.test.js
```

一个重新导出测试配置的根级 `vitest.config.js` 也能让根目录的二进制正常工作；
它保留在源码树中，以便两种调用方式保持一致。`tests/unit/model-routing.test.js`
在本环境中有一个与该问题无关的既有 `EPERM`：发生在 `os.tmpdir()` 清理阶段——
在两种调用方式下失败方式相同，并非别名问题。

## 安全模型
本仓库不含机密，可安全地交给外部编码代理。
机密只存在于本地运行时层（`%LOCALAPPDATA%\9router_WatchEdit\` 以及
9Router 安装目录）。WatchEdit 默认以 `SECRETS: LOCKED` 运行；实时的提供商操作
需要显式解锁（由操作系统支持的授权，或可选的主密码保险库）。
完整模型、SAFE 分享构建器（`python tools/build_safe_share.py`）、机密扫描器以及
私有备份工具请参见 **SECURITY_LOCAL.md**。
  - `data-snapshot/` - `%APPDATA%\9router\db\data.sqlite` 的完整数据库副本。

## 如何应用更新
当你在 9router 中的活动任务完成后，只需运行：
```powershell
.\apply-update.ps1
```
它将：
1. 停止正在运行的 9router 实例。
2. 在 `%APPDATA%\9router_backup_<timestamp>` 创建带时间戳的备份。
3. 全局安装 `9router-0.5.65-extra.tgz`。
4. 同步所有连接并更新 SAIFREN 组合。
5. 以托盘模式重启 9router。

<!-- translation-model: codebuff-agent in-session translation (not the local qwen3:14b pipeline) -->
<!-- source-digest: README.md sha256:082c6cba0dbf49757cbab042ad6e9fec1162ed12091176b2cef3ed1ffe6c5c4c -->
