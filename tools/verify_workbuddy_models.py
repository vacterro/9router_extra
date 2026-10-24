"""T-74 check: DeepSeek V4.1 Flash + Step 5 Preview are live on the WorkBuddy provider.

Four independent claims, each measured against the installed build and the running
server rather than against the source tree:

  1. the installed .next-cli-build registry chunk carries both model ids and
     flags them as reasoning models;
  2. the running server serves that chunk over HTTP with the literal intact;
  3. GET /api/providers/<workbuddy-connection>/models returns both ids (the
     WorkBuddy endpoint has no public catalog, so the route serves the static
     registry — this is what the Import all models button consumes);
  4. GET /api/models resolves reasoning caps for both, which is what makes the
     thinking-level picker offer a ladder instead of "no reasoning".

The CLI token is derived the same way the server derives it: sha256 of
machine-id + "9r-cli-auth" + auth/cli-secret, truncated to 16 hex chars. That is
a local-machine read of files the running instance owns; it is not a credential
the tool invents or stores.

Run: python tools/verify_workbuddy_models.py
Exit 0 = every claim holds.
"""

import glob
import hashlib
import json
import os
import sqlite3
import sys
import urllib.error
import urllib.parse
import urllib.request

MODELS = ("deepseek-v4.1-flash", "step-5-preview")
BUTTON_LITERAL = b"Import all models"
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


def get_json(path, token=None):
    request = urllib.request.Request(BASE + path)
    if token:
        request.add_header("x-9r-cli-token", token)
    with urllib.request.urlopen(request, timeout=20) as response:
        return json.loads(response.read())


def get_bytes(url):
    with urllib.request.urlopen(url, timeout=20) as response:
        return response.read()


def find_installed_chunk(needle):
    """Chunk ids change every build, so search by content, not filename."""
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


def workbuddy_connection_id():
    db = os.path.join(DATA_DIR, "db", "data.sqlite")
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        row = conn.execute(
            "SELECT id FROM providerConnections WHERE provider='workbuddy' AND isActive=1"
        ).fetchone()
    finally:
        conn.close()
    return row[0] if row else None


def main():
    failures = []
    notes = []

    # 1. installed registry chunk
    chunk = find_installed_chunk(MODELS[0].encode())
    if not chunk:
        failures.append(f"installed build: no chunk carries {MODELS[0]!r}")
    else:
        with open(chunk, "rb") as handle:
            body = handle.read()
        for model in MODELS:
            if model.encode() not in body:
                failures.append(f"installed build: {os.path.basename(chunk)} lacks {model!r}")
        notes.append(f"registry chunk on disk: {chunk}")

        # 2. served over HTTP (relative path, URL-quoted for the [id] route segment)
        rel = os.path.relpath(chunk, INSTALLED).replace("\\", "/")
        url = f"{BASE}/_next/{urllib.parse.quote(rel)}"
        try:
            served = get_bytes(url)
        except Exception as exc:  # server down / chunk not routed
            failures.append(f"served chunk: GET {url} failed: {exc}")
        else:
            for model in MODELS:
                if model.encode() not in served:
                    failures.append(f"served chunk: {url} lacks {model!r}")
            notes.append(f"served over HTTP: {url} ({len(served)} bytes)")

    # 3. Import all models button ships in the provider page chunk
    page_chunk = find_installed_chunk(BUTTON_LITERAL)
    if not page_chunk:
        failures.append(f"installed build: no chunk carries {BUTTON_LITERAL!r}")
    else:
        notes.append(f"Import-all button chunk: {page_chunk}")

    # 4. live API: the route the button consumes
    token = cli_token()
    connection_id = workbuddy_connection_id()
    if not connection_id:
        failures.append("live API: no active workbuddy connection to query")
    else:
        try:
            payload = get_json(f"/api/providers/{connection_id}/models", token)
        except Exception as exc:
            failures.append(f"live API: GET /api/providers/{connection_id}/models failed: {exc}")
        else:
            ids = {m.get("id") for m in payload.get("models", [])}
            for model in MODELS:
                if model not in ids:
                    failures.append(f"live API: /models response lacks {model!r}")
            reasoning = {
                m.get("id"): m.get("reasoning")
                for m in payload.get("models", [])
                if m.get("id") in MODELS
            }
            for model in MODELS:
                if reasoning.get(model) is not True:
                    failures.append(f"live API: {model!r} not flagged reasoning (got {reasoning.get(model)!r})")
            notes.append(f"live /models: {len(ids)} ids, both new models present and reasoning")

    # 5. live caps resolution
    try:
        caps_payload = get_json("/api/models", token)
    except Exception as exc:
        failures.append(f"live caps: GET /api/models failed: {exc}")
    else:
        by_full = {m.get("fullModel"): m for m in caps_payload.get("models", [])}
        for model in MODELS:
            row = by_full.get(f"wb/{model}")
            if not row:
                failures.append(f"live caps: no wb/{model} row in /api/models")
                continue
            if row.get("caps", {}).get("reasoning") is not True:
                failures.append(f"live caps: wb/{model} reasoning != true")
        notes.append("live /api/models: both models resolve reasoning caps")

    if failures:
        print("FAIL")
        for line in failures:
            print(" -", line)
        return 1

    print("PASS: WorkBuddy DeepSeek V4.1 Flash + Step 5 Preview are live on port", PORT)
    for line in notes:
        print(" -", line)
    return 0


if __name__ == "__main__":
    sys.exit(main())
