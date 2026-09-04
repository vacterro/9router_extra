# SECURITY_CLAIMS.md — Claim → Executable Test Map

Documentation claims are not evidence (campaign section 31). Every security
guarantee below maps to an executable test in `9router_WatchEdit/tests/security/`.

Run: `pytest 9router_WatchEdit/tests/security -q` (offline, synthetic canaries only).

## Claims

| Security claim | Executable test |
|---|---|
| Tracked secrets are detected | SEC-001 `test_sec_001_tracked_secret` |
| Untracked secrets are detected (filesystem, not `git ls-files`) | SEC-002 `test_sec_002_untracked_secret` |
| **Gitignored secrets are detected** (.gitignore ≠ boundary) | SEC-003 `test_sec_003_gitignored_secret` |
| Deeply nested secrets are found (no shallow traversal) | SEC-004 `test_sec_004_deeply_nested` |
| All key naming variants (apiKey/API_KEY/…) classified suspicious | SEC-005 (16 params), P24 fuzz (11 params) |
| Value patterns detected without sensitive field name | SEC-006 `test_sec_006_value_pattern_without_field_name` |
| Sensitive field names flagged regardless of value format | SEC-007 `test_sec_007_unknown_value_format` |
| Authorization headers blocked; output never reproduces them | SEC-008 `test_sec_008_authorization_header` |
| JWTs blocked (incl. inside logs/ dirs) | SEC-009 `test_sec_009_jwt` |
| PEM private keys blocked | SEC-010 `test_sec_010_private_key` |
| **Private DB cannot enter repo regardless of content** | SEC-011 `test_sec_011_sqlite_without_credentials` |
| WAL/SHM independently blocked | SEC-012 (2 params) |
| Machine identity artifacts rejected | SEC-013 `test_sec_013_machine_id` |
| Original contamination paths (backup/data-snapshot/…) regress-tested | SEC-014 (4 params) |
| Logs/exceptions redact bare high-entropy canaries | REDACT-001, REDACT-005 |
| Redaction is recursive (nested objects, arrays) | REDACT-002, REDACT-003 |
| Provider error bodies redacted, normalized errors kept | REDACT-004 |
| **Passwords never accepted on the command line** (argparse abbreviation + echo fixed) | REDACT-006 |
| No private roots may exist in repo; runtime never creates them | TREE-001, TREE-004 |
| Clean checkout boots LOCKED and passes tests offline | TREE-002/003 |
| Read-only source tree still boots (no source-tree writes) | TREE-005 |
| **External symlinks/junctions flagged without traversing** | LINK-001, LINK-002 |
| Broken links reported safely (no crash) | LINK-003 |
| Clean diagnostic exports pass; canary contamination aborts; no half-built bundle | EXPORT-001..005 |
| **History contamination detected even when current tree is clean** | GITSEC-001 |
| History reports identify commit/file/reason, never values | GITSEC-002 |
| Clean history reports CLEAN | GITSEC-003 |
| Worktrees: independent path+branch, gated on VERIFY | WT-001 |
| Unsafe main blocks worktree creation | WT-002 |
| Three parallel agents stay isolated | WT-003 (also 23-K) |
| Same-file conflicts surface (never silently resolved) | WT-004 |
| **Agent commits with secrets are MERGE BLOCKED** | WT-005 |
| Agent commits with SQLite/runtime state MERGE BLOCKED | WT-006 |
| **Deployment preserves private runtime byte-for-byte** | DEPLOY-001 (hash matrix) |
| Mid-deploy failure rolls back exactly | DEPLOY-002, ROLLBACK exactness (P22) |
| Failing tests stop deploy before start | DEPLOY-003 |
| Secret in source stops deploy | DEPLOY-004 |
| Dirty tree: refuse safely, never discard work | DEPLOY-005 |
| Locked-file/mutation failure → predictable failure + rollback | DEPLOY-006 |
| Patch fallback cannot escape root (…/abs/UNC/drive/case/rename) | PATCH-001..006 |
| **Re-verification close to mutation (no stale PASS)** | RACE-001 |
| Post-merge revalidation catches base-side contamination | RACE-002 |
| Staged-file mutation after hashing aborts export | RACE-003 |
| Hidden/read-only/long-path/Unicode files still scanned | WIN-001..004 |
| **Unreadable file/directory ⇒ AGENT SAFE: NO (fail closed)** | WIN-005, WIN-006, FAIL-CLOSED matrix |
| Malformed JSON still scanned lexically | PARSE-001 |
| Huge single lines: no truncation, linear-time, size policy fail-closed | PARSE-002 (+ perf) |
| Unknown binaries get bounded extraction (never silent skip) | PARSE-003 |
| UTF-16/BOM encodings cannot bypass | PARSE-004 |
| Multiline (YAML-style) secret fields detected | PARSE-005 |
| Sanitizer never mutates the private source | SAN-001 |
| Sanitizer removes every canary class | SAN-002 |
| Sanitized fixture preserves structural contracts | SAN-003 |
| Sanitized output passes the agent-safe gate | SAN-004 |
| Untransformable secrets (numeric/big-int) redacted, never copied | SAN-005 |
| Fresh launch is LOCKED; unlock/lock lifecycle | LOCK-001..004 |
| Password recoverable from nowhere (vault bytes/settings/repo) | LOCK-005 |
| Wrong password: clean failure, no corruption | LOCK-006 |
| Corrupted vault: authenticated decryption fails | LOCK-007 |
| No cross-vault confusion | LOCK-008 |
| Private backup: outside repo (enforced), encrypted, loud name, corrupted/wrong-pw fail | BACKUP-001..005 |
| **Encrypted private material still rejected in shareable repo** | BACKUP-006 |
| Diagnostic bundle: shape preserved, credentials removed, independently scanned | Section 17 |
| Simulated agent: works, finds zero canaries, no repo-relative private paths | Section 18 |
| Production code never depends on fixtures/backup paths | Section 19 |
| Config precedence: explicit > OS default; never repo-relative; missing = LOCKED | Section 20 (3 tests) |
| Merge gate: secret FAIL / failing-test FAIL / clean PASS | Section 21 (3 tests) |
| Rollback restores exact hashes; no orphans | Section 22 |
| Receipts scan clean (no secrets in any output) | Section 23 |
| False-positive control (token_count/secretary/monkey…) | P25 |
| Performance: 10,000-file tree verified < 30 s (measured, printed) | P26 |
| Determinism: 10 identical runs safe & unsafe | P27 |
| **Master E2E: full flow, canaries never leak (A–H)** | Section 28 `test_full_flow_invariants` |
| Scanner exception / git failure / oversized / unknown type ⇒ fail closed | Section 29 matrix |

## Skips (with reasons)

- `WT-002`-style `SystemExit` skips: none currently.
- `os.symlink` tests (LINK-002/003) skip when Windows Developer Mode is off
  (junctions, which need no privileges, are covered by LINK-001).
- `icacls` tests (WIN-005/006) skip if the ACL tool is unavailable.
- Hyper-V agent lab (section 34): documented in SECURITY_LOCAL.md, not CI —
  the VM exercise is a manual strong-isolation demonstration.
