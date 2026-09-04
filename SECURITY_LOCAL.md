# SECURITY_LOCAL.md — Local Security Model

This repository is safe to hand **in full** to external coding agents / LLM
systems. It contains code, tests, docs and sanitized fixtures only.

**Core rule:** source code may know that a secret *exists*; it must never
*contain* the secret.

## 1. Where secrets live

| Domain | Location | Ever in repository? |
|---|---|---|
| Provider credentials, OAuth tokens | Local 9Router (`%APPDATA%\9router`) | NO — 9Router is the secret broker |
| 9Router API key / JWT secret / machine-id | Local 9Router install | NO |
| WatchEdit local config (`config\settings.json`) | `%LOCALAPPDATA%\9router_WatchEdit\config\` | NO (placeholders only: `config.example.json`) |
| Optional password vault (`secure\credentials.vault`) | `%LOCALAPPDATA%\9router_WatchEdit\secure\` | NO |
| Private backups (`PRIVATE_SECRET_BACKUP_*.wvault`) | `%LOCALAPPDATA%\9router_WatchEdit\backups\private\` | NO |
| Legacy relocated material | `%LOCALAPPDATA%\9router_WatchEdit\backups\legacy_<ts>\` | NO |

The runtime architecture is **9Router as secret broker**: WatchEdit asks the
authenticated local 9Router API to perform provider operations and receives
normalized results (`/api/models/test`, model discovery endpoints). WatchEdit
never receives provider API keys, refresh tokens or client secrets; provider
connection objects from `/api/providers` pass through a sanitizing allowlist
boundary (`RouterClient._sanitize_connection`) and raw responses are never
persisted.

## 2. Operating modes

- **DEVELOPMENT / EXTERNAL AGENT MODE (default after fresh checkout)** —
  `SECRETS: LOCKED`. UI, presets, cached model list, unit tests and sanitized
  fixtures all work. Live provider operations are refused with
  *"Live access requires local credential unlock."*
- **LOCAL TRUSTED RUNTIME MODE** — unlocked explicitly by the user
  (OS-backed grant or vault master password) via the header indicator
  `SECRETS: …` → security dialog. `Lock Now` (or closing the app) ends access
  immediately. Auto-unlock on startup exists only behind the local
  *Trust this machine* setting (never enabled by default, never in the repo).

Environment override for integration tests / headless trusted runs:
`WATCHEDIT_LIVE_ACCESS=1`.

## 3. Daily workflow

Normal development: give the repository (or the SAFE archive) to the external
agent — it sees zero real credentials, edits code and runs the offline unit
suite. You run WatchEdit locally; runtime capabilities come from the local
9Router / OS secret store.

Live testing: start WatchEdit → `SECRETS: LOCKED` → *Unlock Live Access* →
live provider tests work → close/lock.

Sharing: `python tools/build_safe_share.py` → mandatory internal secret scan
(+ optional gitleaks) → `dist/share/9router_WatchEdit_SAFE_<ts>.zip`.
Any finding aborts with `SAFE EXPORT BLOCKED` (file + reason + SHA-256
fingerprint only — never the value).

## 4. Tools

| Command | Purpose |
|---|---|
| `python tools/secret_scan.py --root .` | Internal secret scanner |
| `python tools/build_safe_share.py` | SAFE archive builder (allowlist + scan gate) |
| `python tools/export_diagnostics.py` | Safe, redacted diagnostic bundle (outside repo) |
| `python tools/sanitize_9router_state.py IN OUT` | Private export → sanitized fixture (never overwrites input) |
| `python tools/create_private_backup.py` | Encrypted `PRIVATE_SECRET_BACKUP_*.wvault` (interactive password) |
| `python tools/migrate_legacy_secrets.py [--scan-only]` | Relocate legacy in-repo private data to `%LOCALAPPDATA%` |
| `python tools/check_git_history.py` | `CLEAN HISTORY` / `SECRET MATERIAL EXISTS IN HISTORY` report |
| `python tools/pre_commit_secret_check.py` | Pre-commit secret gate |

Install the git hook after `git init`:

```
git config core.hooksPath tools/hooks
```

`.gitignore` is **not** a security boundary — it reduces accidents only.
`git add -f` of private material is never the normal workflow; if a scanner
finding is a verified false positive, document it and use `--no-verify`.

## 5. Cryptography policy

No custom cryptography. DPAPI and Windows Credential Manager are OS services
(`ctypes`, current-user scope). The optional password vault uses vetted
constructions from maintained libraries only: **Argon2id** (memory-hard KDF,
`argon2-cffi`) + **AES-256-GCM** (`cryptography`), random salt, random nonce
per save, authenticated encryption, no plaintext temp files, password never
on disk/argv/logs, key material overwritten on lock (best effort under
CPython), vault locked again on application exit. If those libraries are not
installed, the vault reports unavailable and OS-backed stores remain usable.

Install vault support: `pip install cryptography argon2-cffi`

## 6. Threat boundary (read this before granting agent access)

SAFE: sharing this repository or a SAFE archive with an external LLM/agent —
it contains no credentials.

NOT automatically safe: giving an untrusted agent arbitrary shell/code
execution on your Windows account. That account can access Windows Credential
Manager, DPAPI, `%LOCALAPPDATA%`, the running localhost 9Router and an
unlocked WatchEdit vault. Development mode assumes the *repository* is shared,
not the machine.

## 7. Engine-development artifacts kept on disk

`patches/` and `packages/` are 9Router **engine** development artifacts.
They contain no operator credentials, but `patches/` embeds upstream engine
constants (e.g. a provider OAuth `clientSecret` that ships inside the public
engine tarballs) and is therefore **excluded from every SAFE export** by the
allowlist policy. `.gitignore` keeps them out of version control; if you ever
want the directory to be literally shareable, relocate it next to the private
layer like other legacy material.

## 8. After accidental exposure

1. Disconnect the leak (revoke share, delete upload).
2. Treat every exposed credential as compromised: rotate provider API keys,
   OAuth tokens, the 9Router JWT secret (`backup/data-snapshot/jwt-secret`
   lineage), machine-id-derived CLI tokens, and any vault master password.
3. Run `python tools/secret_scan.py --root .` and
   `python tools/check_git_history.py` to locate remaining copies.
4. Purge with `git filter-repo` only after rotation, never silently.

## 9. File permissions

Private areas (`%LOCALAPPDATA%\9router_WatchEdit\secure`, vault files,
private/legacy backups) get best-effort user-only ACLs (`icacls …
/inheritance:r /grant:r <user>:(OI)(CI)F`) at creation time. Encryption
(DPAPI / vault AEAD) remains the primary control; ACLs are defense in depth.
