#!/usr/bin/env python3
"""
engine_identity_gate.py - client-identity gate for the installed 9router engine.

Reads the declaration in ``9router_WatchEdit/core/engine_identity.py`` and
verifies that every provider whose upstream checks the client is installed with
its official identity. This is the mechanism that keeps a recurrence from
happening: the check runs from ``apply-update.ps1`` after every install and from
the app at startup, so neither a fresh package nor a hand-made install can keep
a stale identity for long.

    python tools/engine_identity_gate.py                # check, repair if needed
    python tools/engine_identity_gate.py --check-only   # report, never write
    python tools/engine_identity_gate.py --json         # machine-readable

A legacy install (one whose package predates the identity) is repaired in
place, with the replaced chunk backed up under
``%LOCALAPPDATA%\\9router_WatchEdit\\engine\\rollback``. An install whose chunk
already declares its own ``buildHeaders`` is never rewritten: that is a package
to rebuild, and the tool says so instead of masking it.

Exit codes: 0 = identity present (or repaired), 1 = missing and not repairable,
2 = no engine installed at that path.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "9router_WatchEdit"))

from core import engine_identity as ei  # noqa: E402


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--install", default=None, help="9router install root")
    parser.add_argument("--check-only", action="store_true",
                        help="report state without repairing")
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    args = parser.parse_args(argv)

    if args.check_only:
        states = ei.check(args.install)
        result = {
            "outcome": ("UNAVAILABLE" if any(s.state == ei.IDENTITY_UNAVAILABLE for s in states)
                        else "OK" if ei.all_ok(states) else "MISSING"),
            "states": states,
            "backups": [],
            "messages": [state.summary() for state in states],
        }
    else:
        result = ei.ensure(args.install)

    if args.json:
        print(json.dumps({
            "outcome": result["outcome"],
            "backups": result["backups"],
            "providers": [
                {
                    "provider": state.provider,
                    "state": state.state,
                    "chunk": state.chunk,
                    "missing": state.missing_tokens,
                    "stale": state.stale_tokens,
                    "detail": state.detail,
                }
                for state in result["states"]
            ],
        }, indent=2))
    else:
        for message in result["messages"]:
            print(f"  {message}")
        for backup in result["backups"]:
            print(f"  backup kept: {backup}")
        print(f"  client identity: {result['outcome']}")

    if result["outcome"] == "UNAVAILABLE":
        return 2
    return 0 if ei.all_ok(result["states"]) else 1


if __name__ == "__main__":
    sys.exit(main())
