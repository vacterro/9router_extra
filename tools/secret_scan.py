#!/usr/bin/env python3
"""
secret_scan.py - CLI wrapper around the 9router_WatchEdit internal secret scanner.

Implementation lives in 9router_WatchEdit/core/secret_scanner.py so the
application (diagnostics, safe-share gating) and the CLI share one source
of truth.

Usage:
    python tools/secret_scan.py [--root PATH] [--json OUT.json] [--quiet]

Exit codes: 0 = clean, 1 = findings detected, 2 = usage error.
"""
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT / "9router_WatchEdit"))

from core.secret_scanner import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
