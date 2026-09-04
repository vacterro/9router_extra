#!/usr/bin/env python3
"""
export_diagnostics.py - CLI for the safe diagnostic bundle.

Gathers version/runtime info, health-cache classifications, discovery
outcomes and redacted recent errors into a scanner-gated JSON bundle that may
be given to external coding agents. Output goes to the LOCAL runtime layer
(%LOCALAPPDATA%\\9router_WatchEdit\\runtime\\diagnostics), never the repository.

Usage:
    python tools/export_diagnostics.py
"""
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "9router_WatchEdit"))

from core.diagnostics import export_diagnostic_bundle  # noqa: E402
from core.history import HealthCache  # noqa: E402


def main() -> int:
    try:
        cache = HealthCache()
        path = export_diagnostic_bundle(cache=cache)
    except RuntimeError as ex:
        print(str(ex), file=sys.stderr)
        return 1
    except Exception as ex:
        print(f"DIAGNOSTIC EXPORT FAILED: {ex}", file=sys.stderr)
        return 1
    print(f"Diagnostic bundle written: {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
