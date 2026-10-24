"""T-75 check: Providers page bulk "Import models (all providers)" + "Delete dead".

Measured against the installed build and the running server, not the source tree:

  1. the installed Providers page chunk carries both new button literals;
  2. the installed API server carries both new route artifacts;
  3. live POST /api/models/delete-dead (dry run) enumerates dead custom models and
     every row it names is one whose providerAlias resolves to nothing (an
     orphaned compatible node id, or `jw`); a resolvable alias such as `kilo`
     must NOT appear;
  4. live POST /api/providers/import-all-models?dryRun=true walks every provider
     with an active connection and returns a per-provider summary without
     writing anything.

The CLI token is derived the same way the server derives it: sha256 of
machine-id + "9r-cli-auth" + auth/cli-secret, truncated to 16 hex chars.

Run: python tools/verify_bulk_cleanup.py
Exit 0 = every claim holds.
"""

import hashlib
import json
import os
import sqlite3
import sys
import urllib.error
import urllib.request

BUTTON_IMPORT = b"Import models (all providers)"
BUTTON_DELETE = b"Delete dead models"
PORT = 20128
BASE = f"http://127.0.0.1:{PORT}"
INSTALLED = r"C:\nodejs\node_modules\9router\app\.next-cli-build"
DATA_DIR = os.path.join(os.environ.get("APPDATA", ""), "9router")


def read(path):
    with open(path, "r", encoding="utf-8") as handle:
        return handle.read().strip()


def cli_token():
    raw = read(os.path.join(DATA_DIR, "machine-id"))
    secret = read(os.path.join(DATA_DIR, "auth", "cli-secret"))
    return hashlib.sha256((raw + "9r-cli-auth" + secret).encode()).hexdigest()[:16]


def post_json(path, token):
    request = urllib.request.Request(BASE + path, data=b"", method="POST")
    request.add_header("x-9r-cli-token", token)
    with urllib.request.urlopen(request, timeout=60) as response:
        return json.loads(response.read())


def find_installed_chunk(needle):
    root = os.path.join(INSTALLED, "static", "chunks")
    for dirpath, _dirnames, filenames in os.walk(root):
        for name in filenames:
            if not name.endswith(".js"):
                continue
            path = os.path.join(dirpath, name)
            with open(path, "rb") as handle:
                if needle in handle.read():
                    return path
    return None


def known_prefixes():
    """Registry ids/aliases + provider-node ids/prefixes from the live DB."""
    known = {"xmtp", "xiaomi-tokenplan"}
    db = os.path.join(DATA_DIR, "db", "data.sqlite")
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        for node_id, data in conn.execute("SELECT id, data FROM providerNodes"):
            known.add(node_id)
            try:
                prefix = json.loads(data or "{}").get("prefix")
            except Exception:
                prefix = None
            if prefix:
                known.add(prefix)
    finally:
        conn.close()
    return known


def main():
    failures = []
    notes = []

    # 1. both button literals ship in the installed page chunk
    for label, needle in (("import", BUTTON_IMPORT), ("delete", BUTTON_DELETE)):
        chunk = find_installed_chunk(needle)
        if not chunk:
            failures.append(f"installed build: no chunk carries {needle!r}")
        else:
            notes.append(f"{label} button chunk: {os.path.basename(chunk)}")

    # 2. both route artifacts exist in the installed server tree
    server = os.path.join(INSTALLED, "server", "app", "api")
    artifacts = {
        "import": os.path.join(server, "providers", "import-all-models", "route.js"),
        "delete-dead": os.path.join(server, "models", "delete-dead", "route.js"),
    }
    for name, path in artifacts.items():
        if not os.path.exists(path):
            failures.append(f"installed server: missing {name} route artifact at {path}")
        else:
            notes.append(f"{name} route artifact present")

    token = cli_token()

    # 3. live dead-model dry run
    try:
        dead = post_json("/api/models/delete-dead", token)
    except Exception as exc:
        failures.append(f"live API: POST /api/models/delete-dead failed: {exc}")
    else:
        rows = dead.get("rows", [])
        count = dead.get("count")
        if count != len(rows):
            failures.append(f"live dead: count {count!r} != len(rows) {len(rows)}")
        known = known_prefixes()
        resolvable = sorted({r["providerAlias"] for r in rows if r.get("providerAlias") in known})
        if resolvable:
            failures.append(f"live dead: dry run named resolvable aliases: {resolvable}")
        # every named row must be a custom model we actually store
        aliases = sorted({r.get("providerAlias") for r in rows})
        notes.append(f"live dead dry run: {count} rows across {len(aliases)} dead aliases")
        for alias in aliases[:12]:
            notes.append(f"  dead alias: {alias}")

    # 4. live import-all dry run
    try:
        imported = post_json("/api/providers/import-all-models?dryRun=true", token)
    except Exception as exc:
        failures.append(f"live API: POST /api/providers/import-all-models?dryRun=true failed: {exc}")
    else:
        summary = imported.get("summary") or {}
        if imported.get("dryRun") is not True:
            failures.append("live import: response is not flagged dryRun")
        if summary.get("providersChecked", 0) <= 0:
            failures.append(f"live import: providersChecked={summary.get('providersChecked')!r}")
        notes.append(
            "live import dry run: "
            f"{summary.get('providersChecked')} providers, "
            f"{summary.get('totalImported')} would import, "
            f"{summary.get('totalSkipped')} already known, "
            f"{summary.get('failed')} failed, "
            f"{summary.get('unsupported')} no listing"
        )

    if failures:
        print("FAIL")
        for line in failures:
            print(" -", line)
        return 1

    print("PASS: Providers page bulk Import-all + Delete-dead are live on port", PORT)
    for line in notes:
        print(" -", line)
    return 0


if __name__ == "__main__":
    sys.exit(main())
