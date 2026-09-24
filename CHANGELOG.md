# Changelog

All notable changes to this project are documented here. The format follows
Keep a Changelog, and this project adheres to Semantic Versioning.

## 0.1.1

### Security
- Secret scanning now covers wrapper and nested scanner-like paths instead of exempting basenames.

## 0.1.0

### Added
- 9Router provider bridge for WorkBuddy AI (`wb/hy3`) with token auto-import
  and quota tracking.
- Antigravity Gemini Flash tier resolution fix (dynamic `gemini-*-flash-*`
  to tiered-model mapping) that removes the upstream 404.
- Native AgentRouter provider support with client User-Agent forwarding,
  Anthropic base-URL resolution and Cline routing fixes.
- SAIFREN combo prepend (`wb/hy3` first, `ag/gemini-3.8-flash-high` second).
- State backup, restore and migration engine covering all connections, custom
  nodes and combos.
- WatchEdit desktop scanner suite (probe, combo editor, presets, OpenCode
  catalog) with private local secret storage.

### Security
- Secret-free repository: credentials live only in the local runtime layer.
- Secret scanner, safe-share builder and private-backup tooling.
- Offline-recovery fail-closed behaviour and DPAPI-safe backup handling.
