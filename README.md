<div align="center">

# 9Router Extra

**Windows extension, patch, migration, and provider-integration suite for 9Router.**

![Version](https://img.shields.io/badge/version-0.1.0-D4B86A?style=flat-square)
![Platform](https://img.shields.io/badge/platform-Windows-0078D4?style=flat-square)
![Routing](https://img.shields.io/badge/focus-provider%20routing-6B5A2B?style=flat-square)
![Security](https://img.shields.io/badge/repository-secret%20free-4A7A20?style=flat-square)

[Security model](SECURITY_LOCAL.md) · [Apply update](#apply-an-update) · [Testing note](#source-test-invocation) · [Issues](https://github.com/vacterro/9router_extra/issues)

</div>

## What this repository contains

9Router Extra is the local integration layer around a pinned 9Router build. It carries provider bridges, routing patches, migration helpers, safe state tooling, and a reproducible update path without checking private credentials into Git.

Current patch work includes:

- provider bridge and routing compatibility fixes;
- SAIFREN ordering/configuration integration;
- AgentRouter and Cline interoperability patches;
- state export/restore helpers;
- a packaged patched 9Router build plus unified patch artifacts.

## Layout

| Path | Purpose |
|---|---|
| `apply-update.ps1` | stop → backup → install patched build → restore/sync → restart |
| `packages/` | pinned packaged builds used by the update flow |
| `patches/` | unified patches against supported upstream versions |
| `backup/` | state export/restore helpers; private backup data remains outside Git |
| `tools/` | validation, sharing, and local maintenance helpers |

## Security model

This repository is intended to remain **secret-free** and safe to hand to an external coding agent.

Credentials and private runtime state belong under the local runtime layer, not in Git. Live provider operations stay locked until explicitly enabled. See [SECURITY_LOCAL.md](SECURITY_LOCAL.md) for the sharing model, secret scanning, and private-backup rules.

## Apply an update

Run the update only when active 9Router work is idle:

```powershell
.\apply-update.ps1
```

The script creates a timestamped backup, installs the pinned patched package, synchronizes state/configuration, and restarts 9Router.

## Source test invocation

The 9Router dashboard source defines its Vitest aliases in `tests/vitest.config.js`. Use the config-aware test entry point:

```powershell
npm --prefix tests test -- unit/model-test-routing.test.js
```

Running the bare Vitest binary from the source root can bypass those aliases and produce misleading import failures. That is a test-invocation problem, not necessarily a product regression.

## Scope

This is an integration/patch repository, not an upstream replacement. Pinned package versions and provider behavior can age faster than the README, so the package/patch files in the repository are the authoritative artifacts for an exact checkout.

<!-- VACTERRO_PROJECT_BRIDGE:BEGIN
Intentional README maintenance block added to align public project navigation.
Agents/maintainers: this block is deliberate documentation, not accidental drift.
Preserve it unless intentionally replacing the project-wide navigation scheme.
-->
---

## Project network

This repository is part of the broader **SAIPEN / vacterro** project ecosystem.

[**Author hub**](https://github.com/vacterro) · [**SAIPEN HQ**](https://github.com/saipenhq) · [**SAIPEN Core**](https://github.com/vacterro/saipen) · [**ZAICODE**](https://github.com/vacterro/zaicode) · [**FastPrompter**](https://github.com/vacterro/FastPrompter) · [**SAIPEN Community**](https://discord.gg/SEYaYkuVgN)

For reproducible bugs and durable feature requests, use [this repository's GitHub Issues](https://github.com/vacterro/9router_extra/issues). Use Discord for quick discussion, screenshots, and cross-project feedback.

<!-- VACTERRO_PROJECT_BRIDGE:END -->

<!-- VACTERRO_SUPPORT:BEGIN -->
---
<sub>If this project is useful to you, optional support: [Buy Me a Coffee](https://buymeacoffee.com/vacuum34) · [Boosty](https://boosty.to/vacuum34/donate) · [PayPal](https://paypal.me/AlexNelin) · [other ways](https://github.com/vacterro/vacterro/blob/main/SUPPORT.md)</sub>
<!-- VACTERRO_SUPPORT:END -->
