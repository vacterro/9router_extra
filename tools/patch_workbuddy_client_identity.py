"""WorkBuddy AI client identity: check, repair, and live-verify (HTTP 400 / code 11128).

Background
----------
WorkBuddy's gateway checks that a chat request came from an official client.
9router's workbuddy executor used to send only the retired CodeBuddy CLI
fingerprint (``User-Agent: CLI/2.108.1 CodeBuddy/2.108.1``, ``X-IDE-Type: CLI``)
and was rejected with::

    HTTP 400 {"code":11128,"msg":"request illegal",
              "displayTips":{"en":"... channel verification ... not sent from
                              an official client ..."}}

The official desktop client (WorkBuddy 5.8.4) sends the full identity family,
and the same token/metadata is accepted:

    User-Agent: WorkBuddy/5.8.4 workbuddy-ai/5.8.4 CLI/2.161.0
    X-IDE-Type / X-IDE-Name: WorkBuddy
    X-Agent-Intent / -Purpose / -Type: craft / conversation / main
    X-Product: SaaS, X-Domain: www.workbuddy.ai, X-Private-Data: true
    X-Conversation-ID, X-Conversation-Request-ID, X-Root-Request-ID,
    X-Conversation-Message-ID, X-Request-ID, X-Trace-ID, traceparent, b3,
    X-B3-TraceId / SpanId / Sampled

Where the fix lives now
-----------------------
The identity ships INSIDE the ``9router-*-extra`` package - in
``open-sse/executors/workbuddy.js`` (a ``buildHeaders`` override) and in the
``workbuddy`` registry transport headers - so a fresh install already carries
it. The declaration that both the upgrade gate and the app read is
``9router_WatchEdit/core/engine_identity.py``; this tool is the operator-facing
front end for that same declaration, not a second copy of it.

Automatic repair
----------------
``apply-update.ps1`` runs ``tools/engine_identity_gate.py`` after every install,
and the app checks on every launch, so an install that predates the identity is
repaired without anyone remembering to do it. Use this tool when you want to
see or force that by hand.

Usage
-----
    python tools/patch_workbuddy_client_identity.py --check
    python tools/patch_workbuddy_client_identity.py --apply
    python tools/patch_workbuddy_client_identity.py --verify
    python tools/patch_workbuddy_client_identity.py --restore

``--apply`` edits the installed build; restart 9router afterwards. ``--verify``
asks the running 9router to test WorkBuddy models through its own chat
pipeline (one small live request per model).

Note: passing the channel check is not sufficient on its own. WorkBuddy also
answers ``HTTP 403 {"code":11140,...}`` with "Your account has been restricted
due to unusual activity" when it has flagged the account itself; that is
server-side and only an appeal through the official client clears it.
"""
from __future__ import annotations

import argparse
import glob
import hashlib
import json
import os
import shutil
import sys
from pathlib import Path
import httpx

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "9router_WatchEdit"))

from core import engine_identity as ei  # noqa: E402
from core.router_client import validate_router_base_url  # noqa: E402
from config import APPDATA_ROUTER, redact_secrets  # noqa: E402

APPDATA_9R = str(APPDATA_ROUTER)
BASE = os.environ.get("NINEROUTER_BASE", "http://127.0.0.1:20128")

IDENTITY = ei.ENGINE_IDENTITIES[0]


def _backups_for(chunk: str) -> list:
    directory = ei._backup_dir()
    if not ei._plain_path(directory):
        return []
    pattern = os.path.join(directory, glob.escape(os.path.basename(chunk)) + ".*.bak")
    return sorted((path for path in glob.glob(pattern) if ei._plain_path(path,file=True)),
                  key=lambda path: (os.stat(path).st_mtime_ns,path))


def apply_patch() -> int:
    state = ei.check_one(IDENTITY)
    if state.state == ei.IDENTITY_UNAVAILABLE:
        print(f"FAIL: {state.detail}")
        return 1
    if state.ok:
        print(f"Nothing to do: {IDENTITY.provider} already carries the desktop identity ({state.chunk}).")
        return 0

    backups: list = []
    repaired = ei.repair_one(IDENTITY, None, backups)
    if not repaired.ok:
        print(f"FAIL: {IDENTITY.provider} identity could not be restored: {repaired.summary()}")
        return 1
    for backup in backups:
        print(f"patched: {repaired.chunk}\n  backup: {backup}")
    print("Restart 9router to load the patched chunk.")
    return 0


def check_patch() -> int:
    states = ei.check()
    for state in states:
        if state.ok:
            label = "BUILT-IN" if "patch marker" not in state.detail else "PATCHED"
            print(f"{label}: {state.chunk}")
        elif state.state == ei.IDENTITY_MISSING:
            print(f"NOT PATCHED: {state.chunk or state.provider}\n  {state.summary()}")
        else:
            print(f"UNAVAILABLE: {state.summary()}")
    return 0 if ei.all_ok(states) else 1


def restore_patch() -> int:
    states = ei.check()
    restored = 0
    for state in states:
        if not state.chunk:
            continue
        try:
            backups = _backups_for(state.chunk)
        except OSError:
            print("FAIL: private rollback storage unavailable")
            continue
        if not backups:
            print(f"no backup for {state.chunk}")
            continue
        current_backup, reason = ei.restore_chunk(state.chunk, backups[-1])
        if current_backup is None:
            print("FAIL: " + redact_secrets(reason))
            continue
        print(f"restored: {state.chunk} <- {backups[-1]}")
        print(f"current chunk preserved: {current_backup}")
        restored += 1
    if not restored:
        return 1
    print("Restart 9router to load the restored chunk.")
    return 0


def cli_token() -> str:
    with open(os.path.join(APPDATA_9R, "machine-id"), encoding="utf-8") as handle:
        raw = handle.read().strip()
    with open(os.path.join(APPDATA_9R, "auth", "cli-secret"), encoding="utf-8") as handle:
        secret = handle.read().strip()
    return hashlib.sha256((raw + "9r-cli-auth" + secret).encode("utf-8")).hexdigest()[:16]


def verify(models) -> int:
    try:
        base = validate_router_base_url(BASE)
    except ValueError:
        print("FAIL: verification requires an explicit numeric loopback endpoint")
        return 1
    try:
        token = cli_token()
    except (OSError,UnicodeError):
        print("FAIL: local authentication unavailable")
        return 1
    failures = 0
    for model in models:
        try:
            with httpx.Client(timeout=90,follow_redirects=False,trust_env=False) as client:
                response = client.post(base + "/api/models/test",headers={"x-9r-cli-token":token},json={"model":model})
                response.raise_for_status()
                payload = response.json()
                if not isinstance(payload,dict):
                    raise ValueError("Model test response was not an object")
        except (httpx.HTTPError, OSError, ValueError) as exc:
            print(f"FAIL {redact_secrets(model)}: {redact_secrets(str(exc))}")
            failures += 1
            continue
        if payload.get("ok"):
            print(f"PASS {redact_secrets(model)}: {redact_secrets(str(payload.get('latencyMs')))} ms")
        else:
            print(f"FAIL {redact_secrets(model)}: {redact_secrets(str(payload.get('error')))[:240]}")
            failures += 1
    return 1 if failures else 0


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--apply", action="store_true", help="patch the installed build")
    group.add_argument("--check", action="store_true", help="report identity state")
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
