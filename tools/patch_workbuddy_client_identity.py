"""Restore WorkBuddy AI routing after upstream channel verification (HTTP 400 / code 11128).

Background
----------
WorkBuddy's gateway now checks that a chat request looks like it came from an
official client. 9router's workbuddy executor sent the old CodeBuddy CLI
fingerprint only (``User-Agent: CLI/2.108.1 CodeBuddy/2.108.1``,
``X-IDE-Type: CLI``). That request is rejected with::

    HTTP 400 {"code":11128,"msg":"request illegal",
              "displayTips":{"en":"... channel verification ... not sent from
                              an official client ..."}}

The official desktop client (WorkBuddy 5.8.4) sends the full identity and
session header family, and the same token/metadata is accepted:

    User-Agent: WorkBuddy/5.8.4 workbuddy-ai/5.8.4 CLI/2.161.0
    X-IDE-Type / X-IDE-Name: WorkBuddy
    X-IDE-Version: 5.8.4
    X-Agent-Intent / -Purpose / -Type: craft / conversation / main
    X-Product: SaaS, X-Domain: www.workbuddy.ai, X-Private-Data: true
    X-Conversation-ID, X-Conversation-Request-ID, X-Root-Request-ID,
    X-Conversation-Message-ID, X-Request-ID, X-Trace-ID, traceparent, b3,
    X-B3-TraceId / SpanId / Sampled

What this tool does
-------------------
The identity now ships inside the ``9router-*-extra`` package: it lives in
``open-sse/executors/workbuddy.js`` (a ``buildHeaders`` override) and in the
``workbuddy`` registry transport headers, so a fresh ``apply-update.ps1``
install already carries it. ``--check`` reports ``BUILT-IN`` for that case.

This tool remains as the runtime repair path for an install built from a
package that predates that change. It finds the compiled WorkBuddy executor
class (the one whose constructor calls ``super("workbuddy")``) and adds a
``buildHeaders`` override that emits the official desktop identity on every
request. Conversation ids are kept stable per WorkBuddy account, while
request/message/trace ids are fresh per call, matching the official client.

Idempotent: a chunk carrying the ``__9rWbConv`` marker or the built-in
identity is skipped. Each modified file is backed up under
``%LOCALAPPDATA%\\9router_WatchEdit\\engine\\rollback``.

Usage
-----
    python tools/patch_workbuddy_client_identity.py --check
    python tools/patch_workbuddy_client_identity.py --apply
    python tools/patch_workbuddy_client_identity.py --verify
    python tools/patch_workbuddy_client_identity.py --restore

``--apply`` edits the installed build; restart 9router afterwards so the new
chunk is loaded. ``--verify`` asks the running 9router to test the WorkBuddy
models through its own chat pipeline (one small live request per model).

Note: passing the channel check is not sufficient on its own. WorkBuddy also
answers ``HTTP 403 {"code":11140,"msg":"request illegal"}`` with "Your account
has been restricted due to unusual activity" when it has flagged the account
itself; that is server-side and only an appeal through the official client
clears it.
"""

import argparse
import glob
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request

INSTALL = os.environ.get("NINEROUTER_INSTALL", r"C:\nodejs\node_modules\9router")
SERVER = os.path.join(INSTALL, "app", ".next-cli-build", "server")
APPDATA_9R = os.path.join(os.environ.get("APPDATA", ""), "9router")
ROLLBACK = os.path.join(
    os.environ.get("LOCALAPPDATA", ""), "9router_WatchEdit", "engine", "rollback"
)
BASE = os.environ.get("NINEROUTER_BASE", "http://127.0.0.1:20128")

MARKER = "__9rWbConv"

# Captured from the official WorkBuddy 5.8.4 desktop client (see README).
DESKTOP_VERSION = "5.8.4"
DESKTOP_CLI_VERSION = "2.161.0"
DESKTOP_UA = (
    f"WorkBuddy/{DESKTOP_VERSION} workbuddy-ai/{DESKTOP_VERSION} "
    f"CLI/{DESKTOP_CLI_VERSION}"
)
DESKTOP_DOMAIN = "www.workbuddy.ai"

# Inserted verbatim into the compiled class. Kept as one string so the patch is
# byte-for-byte the same on every run and easy to audit.
METHOD = (
    "buildHeaders(a,b=!0,c,d){"
    "let e=super.buildHeaders(a,b,c,d);"
    'delete e["x-requested-with"],delete e["x-codebuddy-request"];'
    "let f=globalThis.__9rWbConv||(globalThis.__9rWbConv=new Map),"
    'g=(a&&a.providerSpecificData&&a.providerSpecificData.uid)||"default",'
    "h=f.get(g);h||(h=crypto.randomUUID(),f.set(g,h));"
    'let i=crypto.randomUUID().replace(/-/g,""),'
    'j=crypto.randomUUID().replace(/-/g,""),'
    'k=crypto.randomUUID().replace(/-/g,""),'
    'l=crypto.randomUUID().replace(/-/g,"").slice(0,16);'
    "return Object.assign(e,{"
    f'"User-Agent":"{DESKTOP_UA}",'
    '"X-Product":"SaaS","X-IDE-Type":"WorkBuddy","X-IDE-Name":"WorkBuddy",'
    f'"X-IDE-Version":"{DESKTOP_VERSION}",'
    '"X-Agent-Intent":"craft","X-Agent-Purpose":"conversation","X-Agent-Type":"main",'
    f'"X-Private-Data":"true","X-Domain":"{DESKTOP_DOMAIN}",'
    '"X-Conversation-ID":h,"X-Conversation-Request-ID":i,"X-Root-Request-ID":i,'
    '"X-Conversation-Message-ID":j,"X-Request-ID":j,'
    '"X-Trace-ID":k,"traceparent":"00-"+k+"-"+l+"-01","b3":k+"-"+l+"-1",'
    '"X-B3-TraceId":k,"X-B3-SpanId":l,"X-B3-Sampled":"1"},'
    "(a&&a.providerSpecificData&&a.providerSpecificData.uid)"
    '?{"X-User-Id":a.providerSpecificData.uid}:{})}'
)

ANCHOR = re.compile(r'constructor\(\)\{super\("workbuddy"\)\}(?=transformRequest\()')

# Since T-118 the identity lives in open-sse/executors/workbuddy.js and ships
# inside the 9router-*-extra package, so a fresh install already carries it.
# --check reports BUILT-IN instead of demanding a runtime patch; --apply then
# has nothing to do. The ANCHOR above no longer matches those chunks because
# buildHeaders now sits between the constructor and transformRequest.
BUILTIN = re.compile(
    r'super\("workbuddy"\)\}buildHeaders\('
    r'.{0,4000}?"X-Agent-Intent":"craft"',
    re.S,
)


def candidate_chunks():
    """Compiled server chunks that contain the WorkBuddy executor class."""
    if not os.path.isdir(SERVER):
        return []
    found = []
    for root, _dirs, files in os.walk(SERVER):
        for name in files:
            if not name.endswith(".js"):
                continue
            path = os.path.join(root, name)
            try:
                with open(path, "r", encoding="utf-8") as handle:
                    body = handle.read()
            except (OSError, UnicodeDecodeError):
                continue
            if 'super("workbuddy")' in body:
                found.append((path, body))
    return found


def backup_path(path):
    os.makedirs(ROLLBACK, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    return os.path.join(ROLLBACK, f"{os.path.basename(path)}.{stamp}.bak")


def node_syntax_ok(path):
    """Best-effort syntax gate; skipped when node is not available."""
    node = shutil.which("node")
    if not node:
        return True, "node not found, syntax check skipped"
    result = subprocess.run([node, "--check", path], capture_output=True, text=True)
    return result.returncode == 0, (result.stderr or result.stdout).strip()


def apply_patch():
    chunks = candidate_chunks()
    if not chunks:
        print(f"FAIL: no WorkBuddy executor chunk under {SERVER}")
        return 1

    changed = 0
    already = 0
    for path, body in chunks:
        if MARKER in body or BUILTIN.search(body):
            print(f"already carries the desktop identity: {path}")
            already += 1
            continue
        match = ANCHOR.search(body)
        if not match:
            print(f"SKIP: not the executor chunk: {path}")
            continue

        patched = body[: match.end()] + METHOD + body[match.end():]
        backup = backup_path(path)
        shutil.copy2(path, backup)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(patched)

        ok, detail = node_syntax_ok(path)
        if not ok:
            shutil.copy2(backup, path)
            print(f"FAIL: patched file does not parse, restored from {backup}\n{detail}")
            return 1

        changed += 1
        print(f"patched: {path}\n  backup: {backup}")

    if not changed and already:
        print("Nothing to do: the WorkBuddy identity patch is already applied.")
    elif not changed:
        print("FAIL: no WorkBuddy executor chunk could be patched")
        return 1
    else:
        print("Restart 9router to load the patched chunk.")
    return 0


def check_patch():
    chunks = candidate_chunks()
    if not chunks:
        print(f"FAIL: no WorkBuddy executor chunk under {SERVER}")
        return 1
    executor = [(path, body) for path, body in chunks
                if MARKER in body or BUILTIN.search(body) or ANCHOR.search(body)]
    if not executor:
        print(f"FAIL: no WorkBuddy executor chunk under {SERVER}")
        return 1
    missing = [path for path, body in executor
               if MARKER not in body and not BUILTIN.search(body)]
    if missing:
        for path in missing:
            print(f"NOT PATCHED: {path}")
        return 1
    for path, body in executor:
        print(f"{'BUILT-IN' if BUILTIN.search(body) else 'PATCHED'}: {path}")
    return 0


def restore_patch():
    chunks = candidate_chunks()
    if not chunks:
        print(f"FAIL: no WorkBuddy executor chunk under {SERVER}")
        return 1
    restored = 0
    for path, _body in chunks:
        pattern = os.path.join(ROLLBACK, os.path.basename(path) + ".*.bak")
        backups = sorted(glob.glob(pattern))
        if not backups:
            print(f"no backup for {path}")
            continue
        shutil.copy2(backups[-1], path)
        print(f"restored: {path} <- {backups[-1]}")
        restored += 1
    if not restored:
        return 1
    print("Restart 9router to load the restored chunk.")
    return 0


def cli_token():
    with open(os.path.join(APPDATA_9R, "machine-id"), encoding="utf-8") as handle:
        raw = handle.read().strip()
    with open(os.path.join(APPDATA_9R, "auth", "cli-secret"), encoding="utf-8") as handle:
        secret = handle.read().strip()
    return hashlib.sha256((raw + "9r-cli-auth" + secret).encode("utf-8")).hexdigest()[:16]


def verify(models):
    token = cli_token()
    failures = 0
    for model in models:
        request = urllib.request.Request(
            BASE + "/api/models/test",
            method="POST",
            headers={"x-9r-cli-token": token, "Content-Type": "application/json"},
            data=json.dumps({"model": model}).encode("utf-8"),
        )
        try:
            with urllib.request.urlopen(request, timeout=90) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except (urllib.error.URLError, OSError, ValueError) as exc:
            print(f"FAIL {model}: {exc}")
            failures += 1
            continue
        if payload.get("ok"):
            print(f"PASS {model}: {payload.get('latencyMs')} ms")
        else:
            print(f"FAIL {model}: {str(payload.get('error'))[:240]}")
            failures += 1
    return 1 if failures else 0


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--apply", action="store_true", help="patch the installed build")
    group.add_argument("--check", action="store_true", help="report patch state")
    group.add_argument("--restore", action="store_true", help="restore the newest backup")
    group.add_argument("--verify", action="store_true", help="live-test WorkBuddy models")
    parser.add_argument(
        "--model",
        action="append",
        default=None,
        help="model used by --verify (repeatable); default: deepseek + hy3",
    )
    args = parser.parse_args(argv)

    if args.apply:
        return apply_patch()
    if args.check:
        return check_patch()
    if args.restore:
        return restore_patch()
    models = args.model or ["wb/deepseek-v4.1-flash", "wb/hy3"]
    return verify(models)


if __name__ == "__main__":
    sys.exit(main())
