#!/usr/bin/env python3
"""
check_git_history.py - Git history secret audit.

Reports exactly one of:
    NO GIT REPOSITORY          (worktree scan only)
    CLEAN HISTORY              (no secret patterns in any committed blob)
    SECRET MATERIAL EXISTS IN HISTORY
                                (+ explicit remediation plan, NO history rewrite)

Usage:
    python tools/check_git_history.py
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "9router_WatchEdit"))

from core.secret_scanner import (  # noqa: E402
    PRIVATE_NAME_RE, Finding, _fingerprint, format_report, scan_file,
)


def _git(args, repo_root=None, **kw):
    root = str(repo_root) if repo_root else str(REPO_ROOT)
    return subprocess.run(["git", "-C", root] + args, capture_output=True, text=True, **kw)


def _git_bytes(args, repo_root=None) -> subprocess.CompletedProcess:
    """T-91: blob CONTENT is read as bytes, never through ``text=True``.

    ``text=True`` decodes the child's stdout with the locale codec; for an
    undecodable blob that raises inside subprocess' reader thread, which leaves
    ``stdout`` as ``None`` and the whole history audit dies with a TypeError
    instead of reporting anything. Bytes also let the binary guard inspect the
    real content before any decode decision is made.
    """
    root = str(repo_root) if repo_root else str(REPO_ROOT)
    return subprocess.run(["git", "-C", root] + args, capture_output=True)


def check_history(repo_root: Path = None) -> int:
    repo_root = Path(repo_root) if repo_root else REPO_ROOT
    is_repo = _git(["rev-parse", "--is-inside-work-tree"], repo_root).returncode == 0
    if not is_repo:
        print("NO GIT REPOSITORY")
        print("No history to scan. Current worktree can be scanned with tools/secret_scan.py.")
        print("When a repository is initialized, install the pre-commit hook (see SECURITY_LOCAL.md).")
        return 0

    print("Scanning full commit history for secret patterns ...")
    listing = _git(["rev-list", "--all"], repo_root)
    if listing.returncode != 0:
        print("GIT ERROR: rev-list failed", file=sys.stderr)
        return 2
    commits = [c for c in listing.stdout.split() if c.strip()]

    findings = []
    seen_blobs = set()
    # T-94: every PRIVATE-named holder of a blob, keyed by blob sha. Commits
    # arrive newest-first, so a `git mv credentials.json notes.txt` used to be
    # scanned only once, under the benign new name, and the private historical
    # name was dropped by the per-sha dedup below.
    private_names = {}
    tmp_dir = repo_root / ".git_scan_tmp"
    tmp_dir.mkdir(exist_ok=True)
    for commit in commits:
        ls = _git(["ls-tree", "-r", commit], repo_root)
        for line in ls.stdout.splitlines():
            parts = line.split()
            if len(parts) < 4:
                continue
            mode, otype, sha, path = parts[0], parts[1], parts[2], " ".join(parts[3:])
            if otype != "blob":
                continue
            if PRIVATE_NAME_RE.search(path.replace("\\", "/")):
                private_names.setdefault(sha, []).append(f"{commit[:10]}:{path}")
            if sha in seen_blobs:
                continue
            seen_blobs.add(sha)
            cat = _git_bytes(["cat-file", "blob", sha], repo_root)
            if cat.returncode != 0:
                # T-91: an unreadable blob is reported, never treated as clean
                findings.append(Finding(
                    f"{commit[:10]}:{path}", "unreadable_blob",
                    "blob content could not be read; cannot prove safe", "-"))
                continue
            raw = cat.stdout or b""
            if b"\x00" in raw[:1024]:
                continue  # binary blob: content scan skipped; names reported below
            try:
                blob_text = raw.decode("utf-8")
            except UnicodeDecodeError as ex:
                # T-91: previously text=True either crashed the audit or left an
                # empty string, which scanned as a clean blob. Report instead.
                findings.append(Finding(
                    f"{commit[:10]}:{path}", "undecodable_blob",
                    f"not valid UTF-8 ({ex.reason}); cannot prove safe", "-"))
                continue
            holders = private_names.get(sha)
            # keep a private name's suffix so JSON blobs keep structured scanning
            suffix = Path(holders[0].rsplit(":", 1)[1] if holders else path).suffix or ".txt"
            tmp = tmp_dir / f".scan_{sha[:12]}{suffix}"
            tmp.write_text(blob_text, encoding="utf-8")
            try:
                for f in scan_file(tmp, tmp_dir):
                    # the temp name is an artifact, never a real path: name
                    # findings come from `private_names`, not from this scan
                    if f.reason == "private_filename":
                        continue
                    # attribute the finding to the original commit + path
                    findings.append(Finding(
                        file=f"{commit[:10]}:{path}",
                        reason=f.reason, detail=f.detail, fingerprint=f.fingerprint))
            finally:
                tmp.unlink(missing_ok=True)

    # T-94: private BY NAME material is reported independently of the content
    # scan, so a rename (or a binary blob) can never hide it.
    for sha, holders in private_names.items():
        for label in holders:
            findings.append(Finding(label, "private_filename",
                                    "filename matches private material policy",
                                    _fingerprint(label.rsplit(":", 1)[1])))


    import shutil as _shutil
    _shutil.rmtree(tmp_dir, ignore_errors=True)

    if findings:
        print("SECRET MATERIAL EXISTS IN HISTORY")
        print(format_report(findings))
        print(
            "\nREMEDIATION PLAN (do NOT silently rewrite history):\n"
            " 1. Rotate/revoke every credential whose fingerprint appears above.\n"
            " 2. Remove the files from future commits (git rm + .gitignore).\n"
            " 3. Only after rotation, consider 'git filter-repo' to purge blobs —\n"
            "    coordinate with all clones/remotes first.\n"
            " 4. If this repository was ever published while secrets existed, treat them as compromised."
        )
        return 1

    print("CLEAN HISTORY")
    print(f"Scanned {len(seen_blobs)} unique blobs across {len(commits)} commits: no secret patterns found.")
    return 0


if __name__ == "__main__":
    sys.exit(check_history())
