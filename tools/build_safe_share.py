#!/usr/bin/env python3
"""
build_safe_share.py - SAFE Share Builder

Produces the ONLY archive intended for external coding agents / LLM systems:

    dist/share/9router_WatchEdit_SAFE_<timestamp>.zip

Policy: ALLOWLIST-oriented (never a blind zip of the repository), then a
mandatory internal secret scan of every candidate file (plus optional
gitleaks when installed). Any finding ABORTS the export with
"SAFE EXPORT BLOCKED" and a safe report (file + reason + fingerprint,
never the value).

Usage:
    python tools/build_safe_share.py [--out dist/share] [--skip-gitleaks]
"""
from __future__ import annotations

import argparse
import datetime
import hashlib
import os
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path
from typing import List

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "9router_WatchEdit"))

from core.secret_scanner import format_report, scan_file  # noqa: E402

# Optional-convenience ZIP output lives OUTSIDE the repository (the repository
# itself is the primary collaboration boundary and must stay archive-free).
DEFAULT_OUT_DIR = Path(os.environ.get("WATCHEDIT_DATA_DIR")
                       or Path(os.environ.get("LOCALAPPDATA", "")) / "9router_WatchEdit") / "share"

# ---------------------------------------------------------------------------
# Allowlist: the only trees/files eligible for the SAFE archive.
# Engine-development artifacts (patches/, packages/, engine scripts) are
# deliberately NOT shareable; runtime state and backups never leave the machine.
# ---------------------------------------------------------------------------
ALLOW_DIRS = [
    "9router_WatchEdit/core",
    "9router_WatchEdit/ui",
    "9router_WatchEdit/tests",
    "9router_WatchEdit/tools",
    "tools",
    "docs",
]
ALLOW_ROOT_FILES = [
    "README.md",
    "README.txt",
    "UI.md",
    "pyproject.toml",
    "requirements.txt",
    "config.example.json",
    "SECURITY_LOCAL.md",
    ".gitignore",
    "START_WATCHEDIT.bat",
]

ALLOW_SUFFIXES = {".py", ".md", ".txt", ".json", ".toml", ".bat", ".example", ".cfg", ".ini"}

# Hard denials even inside allowed trees (defense in depth).
DENY_PATTERNS = [
    ".git/", "__pycache__/", ".pytest_cache/", ".saipen/", "backup/", "dist/",
    "packages/", "patches/",
]
DENY_NAMES = {
    ".env", "credentials.vault", "jwt-secret", "machine-id", "health_cache.json",
    "presets.json", "settings.json",
}
DENY_SUFFIXES = {
    ".sqlite", ".sqlite-wal", ".sqlite-shm", ".db", ".tgz", ".zip", ".gz",
    ".pem", ".key", ".pfx", ".p12", ".vault", ".bin", ".exe", ".dll",
    ".png", ".jpg", ".ico",
}


def _denied(rel: str) -> bool:
    parts = rel.replace("\\", "/")
    if any(p in f"/{parts}" or parts.startswith(p) or f"/{p}" in f"/{parts}" for p in DENY_PATTERNS):
        return True
    if Path(parts).name in DENY_NAMES:
        return True
    if Path(parts).suffix.lower() in DENY_SUFFIXES:
        return True
    return False


def collect_candidates() -> List[Path]:
    candidates: List[Path] = []
    for d in ALLOW_DIRS:
        base = REPO_ROOT / d
        if not base.is_dir():
            continue
        for p in sorted(base.rglob("*")):
            if not p.is_file():
                continue
            if p.suffix.lower() not in ALLOW_SUFFIXES:
                continue
            rel = str(p.relative_to(REPO_ROOT))
            if _denied(rel):
                continue
            candidates.append(p)
    for f in ALLOW_ROOT_FILES:
        p = REPO_ROOT / f
        if p.is_file() and p.suffix.lower() in ALLOW_SUFFIXES:
            candidates.append(p)
    return sorted(set(candidates))


def run_gitleaks(staging: Path) -> bool:
    """Optional external scanner. Returns True = clean, False = findings."""
    exe = shutil.which("gitleaks")
    if not exe:
        return True
    proc = subprocess.run(
        [exe, "detect", "--source", str(staging), "--no-git", "--redact", "--report-format", "json",
         "--report-path", str(staging.parent / "gitleaks_report.json")],
        capture_output=True, text=True, timeout=300,
    )
    return proc.returncode == 0


def build_safe_share(out_dir: Path = None, skip_gitleaks: bool = False, quiet: bool = False) -> Path:
    out_dir = Path(out_dir) if out_dir else DEFAULT_OUT_DIR
    candidates = collect_candidates()

    out_dir.mkdir(parents=True, exist_ok=True)
    # GATE 7: sweep stale partial artifacts from interrupted runs
    for stale in out_dir.glob(".SAFE_*.partial.zip"):
        stale.unlink(missing_ok=True)
    staging = out_dir / ".staging"
    if staging.exists():
        shutil.rmtree(staging)
    staging.mkdir(parents=True)

    try:
        for p in candidates:
            rel = p.relative_to(REPO_ROOT)
            dst = staging / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(p, dst)

        # MANDATORY gate 1: full agent-safe verification of the staged tree
        # (content scan + protected paths), performed close to mutation.
        from verify_agent_safe import verify_agent_safe
        unsafe = verify_agent_safe(staging)
        if unsafe:
            print("SAFE EXPORT BLOCKED")
            for u in unsafe:
                print(f"{u.path}\n  {u.reason}")
            return None
        findings = []
        for p in sorted(staging.rglob("*")):
            if p.is_file():
                findings.extend(scan_file(p, staging))
        if findings:
            print(format_report(findings))
            return None

        # RACE-003: hash manifest captured at validation time; every file is
        # re-hashed immediately before it enters the archive. Any staged-file
        # mutation after validation aborts the export.
        manifest = {}
        for p in sorted(staging.rglob("*")):
            if p.is_file():
                manifest[str(p)] = hashlib.sha256(p.read_bytes()).hexdigest()

        # Optional industry scanner
        if not skip_gitleaks and not run_gitleaks(staging):
            print("SAFE EXPORT BLOCKED")
            print("file: gitleaks")
            print("reason: external scanner reported likely secrets")
            print("report: dist/share/gitleaks_report.json")
            return None

        stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        zip_path = out_dir / f"9router_WatchEdit_SAFE_{stamp}.zip"
        # EXPORT-004: build under a temporary name, promote atomically on success
        tmp_zip = out_dir / f".SAFE_{stamp}.partial.zip"
        try:
            try:
                with zipfile.ZipFile(tmp_zip, "w", zipfile.ZIP_DEFLATED) as zf:
                    for p in sorted(staging.rglob("*")):
                        if not p.is_file():
                            continue
                        current = hashlib.sha256(p.read_bytes()).hexdigest()
                        if manifest.get(str(p)) != current:
                            print("SAFE EXPORT BLOCKED")
                            print(f"file: {p.relative_to(staging)}")
                            print("reason: staged file changed after validation (race protection)")
                            return None
                        try:
                            zf.write(p, p.relative_to(staging))
                        except Exception as ex:
                            print("SAFE EXPORT BLOCKED")
                            print(f"file: {p.relative_to(staging)}")
                            print(f"reason: archive write failed ({type(ex).__name__})")
                            return None
                os.replace(tmp_zip, zip_path)
            except Exception as ex:
                # EXPORT-004: mid-archive failure never raises raw and never
                # leaves a half-built archive under the final name
                print("SAFE EXPORT BLOCKED")
                print(f"reason: archive creation failed ({type(ex).__name__})")
                return None
        finally:
            if tmp_zip.exists():
                tmp_zip.unlink()
        if not quiet:
            print(f"SAFE archive created: {zip_path}")
            print(f"Files included: {sum(1 for _ in staging.rglob('*') if _.is_file())}")
        return zip_path
    finally:
        shutil.rmtree(staging, ignore_errors=True)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Build the SAFE share archive")
    ap.add_argument("--out", default=None, help=f"output directory (default {DEFAULT_OUT_DIR})")
    ap.add_argument("--skip-gitleaks", action="store_true")
    args = ap.parse_args(argv)

    result = build_safe_share(Path(args.out) if args.out else None, args.skip_gitleaks)
    return 0 if result else 1


if __name__ == "__main__":
    sys.exit(main())
