#!/usr/bin/env python3
"""
secret_scan.py - Internal secret scanner for the 9router_WatchEdit repository.

Detects credential-shaped material in text/JSON files BEFORE any SAFE export,
diagnostic bundle, or commit. Findings never print the secret value; only a
SHA-256 fingerprint prefix is reported for identification.

Usage:
    python tools/secret_scan.py [--root PATH] [--json OUT.json] [--quiet]

Exit codes: 0 = clean, 1 = findings detected, 2 = usage error.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import sys
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

# ---------------------------------------------------------------------------
# Detection rules
# ---------------------------------------------------------------------------

# Compound sensitive JSON/object keys (task section 13 scan list): a
# non-placeholder value under any of these is always a finding.
SENSITIVE_KEY_RE = re.compile(
    r'(?i)^(api[_-]?key|apikey|access[_-]?token|accesstoken|'
    r'refresh[_-]?token|refreshtoken|client[_-]?secret|clientsecret|'
    r'password|passwd|authorization|cookie|cookies|'
    r'private[_-]?key|jwt|bearer|credential|apikeyid)$'
)

# Generic key names: flagged ONLY when the value itself is credential-shaped
# (the 9Router kv table legitimately stores {"key": "node|model|llm", ...}).
GENERIC_KEY_RE = re.compile(r'(?i)^(key|token|auth|secret)$')

# Values that are obviously placeholders used by sanitized fixtures / examples.
# These are permitted so fixtures remain structurally useful (task sections 10-11).
PLACEHOLDER_RE = re.compile(
    r'(?i)^<?(redacted([_\-a-z0-9]*)|removed|placeholder|fake[\-_][a-z0-9\-_]*|'
    r'example[\-_][a-z0-9\-_]*|test[_\-][a-z0-9\-_]*do[_\-]not[_\-]use|'
    r'do[_\-]not[_\-]use|xxx+|\*+|\$\{[a-z0-9_\-]+\}|<your[_\-a-z0-9]*>|'
    r'changeme[\-_]*|insert[_\-].*|replace[_\-].*|n/?a|none|null|true|false|'
    r'0|1|\{\{.*\}\})>?$'
)

# Structured credential patterns in free text.
JWT_RE = re.compile(r'\beyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{4,}\b')
PRIVATE_KEY_RE = re.compile(r'-----BEGIN (?:[A-Z]+ )?PRIVATE KEY(?: BLOCK)?-----')
BEARER_RE = re.compile(r'(?i)\bbearer\s+[A-Za-z0-9_\-\.=+/]{16,}')
AUTH_HEADER_RE = re.compile(r'(?i)^\s*(authorization|x-api-key)\s*[:=]', re.MULTILINE)
PROVIDER_KEY_RE = re.compile(
    r'\b(?:sk|rk|pk|ghp|gho|ghu|ghs|xox[bpars]|AIza|9r)[\-_][A-Za-z0-9_\-]{12,}\b'
)

# High-entropy material on the same line as a credential word.
CRED_WORD_LINE_RE = re.compile(r'(?i)(secret|token|password|passwd|api[_\-]?key|credential)')
HIGH_ENTROPY_RE = re.compile(r'\b[A-Za-z0-9+/=_\-]{32,}\b')

# Token shapes that are never credentials even near credential words
# (URLs, ISO timestamps, pure numerics, composite identifiers/paths such as
# kv keys "node|model|llm" or model ids "prov/deepseek-chat-v3").
BENIGN_TOKEN_RE = re.compile(
    r'(?i)(://|[|/]|^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}|^\d[\d:\.\-]*$|'
    r'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\.sqlite)'
)

# Sensitive key-value pairs in non-JSON text (e.g. patches, source, config):
#   clientSecret: "..."   apiKey=...   "refresh_token": "..."
# Keys naming a REFERENCE (…_ref, …_id, …_name) are by design not secrets
# (task section 8: local config stores "router_credential_ref": "9router/local-api").
# Values must be >= 16 chars: real credentials are never shorter, while engine
# unit-test fixtures routinely use short memorable fakes ("test-apikey").
KV_SECRET_RE = re.compile(
    r'(?i)["\']?([a-z_]*(?:api[_-]?key|apikey|access[_-]?token|refresh[_-]?token|'
    r'client[_-]?secret|password|secret|jwt|bearer))[a-z_]*["\']?\s*[:=]\s*["\']([^"\']{16,})["\']'
)
KV_REF_SUFFIXES = ("_ref", "_id", "_name", "_url", "_path")

# Deliberately fake, verifiably non-credential test values used by this
# repository's own test suite. Exact-match false-positive allowlist.
KNOWN_TEST_FAKES = {
    "sk-abcdefghijklmnop1234",           # sequential alphabet; redaction test marker
    "super_secret_test_value_123456",    # task-19 redaction marker (never a credential)
}

BINARY_SUFFIXES = {
    ".sqlite", ".sqlite-wal", ".sqlite-shm", ".db", ".tgz", ".zip", ".gz",
    ".png", ".jpg", ".jpeg", ".ico", ".exe", ".dll", ".pyd", ".pdf", ".woff",
    ".woff2", ".ttf", ".vault", ".pem", ".key", ".pfx", ".p12", ".bin",
}

# Files that are private BY NAME regardless of content scanning capability.
PRIVATE_NAME_RE = re.compile(
    r'(?i)(^|/)(jwt[-_]secret|machine[-_]id|\.env(\..*)?|.*\.vault$|'
    r'credentials?\.json|provider[s]?[-_]state[-_]export.*\.json|.*\.sqlite(-wal|-shm)?$)'
)

MAX_FILE_BYTES = 8 * 1024 * 1024  # skip giant files with a notice instead of hanging


@dataclass
class Finding:
    file: str
    reason: str
    detail: str          # safe detail: key name or pattern id, NEVER the value
    fingerprint: str     # sha256(value)[:12]


def _entropy(s: str) -> float:
    if not s:
        return 0.0
    freq: Dict[str, int] = {}
    for ch in s:
        freq[ch] = freq.get(ch, 0) + 1
    n = len(s)
    return -sum((c / n) * math.log2(c / n) for c in freq.values())


def _fingerprint(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8", "replace")).hexdigest()[:12]


def _is_placeholder(value: str) -> bool:
    v = value.strip()
    if not v or len(v) < 3:
        return True  # empty/trivial values cannot be secrets
    if v.lower() in KNOWN_TEST_FAKES:
        return True
    return bool(PLACEHOLDER_RE.match(v))


def _credential_shaped(value: str) -> bool:
    """Heuristic: value plausibly IS a credential (for generic key names)."""
    v = value.strip()
    if len(v) < 20 or BENIGN_TOKEN_RE.search(v) or _is_placeholder(v):
        return False
    if not (any(c.isdigit() for c in v) and any(c.isalpha() for c in v)):
        return False
    return _entropy(v) >= 3.5


def _scan_json(obj: Any, path: Path, findings: List[Finding], prefix: str = "$"):
    """Walk parsed JSON; flag sensitive keys carrying non-placeholder values."""
    if isinstance(obj, dict):
        for k, v in obj.items():
            key_str = str(k)
            if isinstance(v, str):
                if SENSITIVE_KEY_RE.match(key_str):
                    if not _is_placeholder(v) and len(v.strip()) >= 8:
                        findings.append(Finding(
                            file=str(path), reason="sensitive_json_key",
                            detail=f"key '{key_str}' at {prefix}.{key_str}",
                            fingerprint=_fingerprint(v),
                        ))
                elif GENERIC_KEY_RE.match(key_str) and _credential_shaped(v):
                    findings.append(Finding(
                        file=str(path), reason="sensitive_json_key",
                        detail=f"generic key '{key_str}' with credential-shaped value at {prefix}.{key_str}",
                        fingerprint=_fingerprint(v),
                    ))
            _scan_json(v, path, findings, f"{prefix}.{key_str}")
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            _scan_json(v, path, findings, f"{prefix}[{i}]")


def _scan_text(text: str, path: Path, findings: List[Finding], structured: bool = False) -> None:
    """structured=True (JSON files): skip the free-text entropy heuristic —
    structured files are audited via key rules and pattern rules instead;
    the line heuristic only produces composite-key-name false positives there."""
    if PRIVATE_KEY_RE.search(text):
        findings.append(Finding(str(path), "private_key_block", "PEM private key header", _fingerprint("pem")))
    for lineno, line in enumerate(text.splitlines(), 1):
        for rx, rid in ((JWT_RE, "jwt_token"), (BEARER_RE, "bearer_token"),
                        (PROVIDER_KEY_RE, "provider_api_key")):
            m = rx.search(line)
            if m and not _is_placeholder(m.group(0)):
                findings.append(Finding(
                    f"{path}:{lineno}", rid,
                    f"pattern {rid} in line",
                    _fingerprint(m.group(0)),
                ))
                break
        m = KV_SECRET_RE.search(line)
        if (m and not m.group(1).lower().endswith(KV_REF_SUFFIXES)
                and not _is_placeholder(m.group(2)) and not BENIGN_TOKEN_RE.search(m.group(2))):
            findings.append(Finding(
                f"{path}:{lineno}", "sensitive_key_value",
                f"key '{m.group(1)}' with non-placeholder value",
                _fingerprint(m.group(2)),
            ))
        if AUTH_HEADER_RE.search(line):
            m = HIGH_ENTROPY_RE.search(line)
            if m and not _is_placeholder(m.group(0)):
                findings.append(Finding(
                    f"{path}:{lineno}", "authorization_header",
                    "Authorization-style header with long value",
                    _fingerprint(m.group(0)),
                ))
        elif (not structured) and CRED_WORD_LINE_RE.search(line):
            for m in HIGH_ENTROPY_RE.finditer(line):
                tok = m.group(0)
                # skip benign shapes (URLs, ISO timestamps, pure numerics);
                # require digit+letter mix: plain code identifiers (camelCase
                # function names near the word 'credentials') are not secrets
                if BENIGN_TOKEN_RE.search(tok):
                    continue
                if (not _is_placeholder(tok) and _entropy(tok) >= 3.8
                        and any(c.isdigit() for c in tok) and any(c.isalpha() for c in tok)):
                    findings.append(Finding(
                        f"{path}:{lineno}", "high_entropy_near_credential_word",
                        f"entropy={_entropy(tok):.2f} len={len(tok)}",
                        _fingerprint(tok),
                    ))
                    break


def scan_file(path: Path, root: Optional[Path] = None) -> List[Finding]:
    findings: List[Finding] = []
    rel = path.relative_to(root) if root and path.is_relative_to(root) else path  # type: ignore[attr-defined]
    name = str(rel).replace("\\", "/")

    # This scanner's own source contains detection signatures by construction.
    if path.name in ("secret_scanner.py", "secret_scan.py"):
        return findings

    # Name-based private material (binary or not): flagged without content read.
    if PRIVATE_NAME_RE.search(name):
        findings.append(Finding(str(rel), "private_filename",
                                "filename matches private material policy",
                                _fingerprint(name)))
        return findings

    if path.suffix.lower() in BINARY_SUFFIXES:
        return findings  # binaries are excluded from text scanning; builder filters them

    try:
        if path.stat().st_size > MAX_FILE_BYTES:
            findings.append(Finding(str(rel), "oversized_file_skipped",
                                    "file too large to scan; excluded from safe export", "-"))
            return findings
        raw = path.read_bytes()
    except OSError as ex:
        findings.append(Finding(str(rel), "unreadable", f"{type(ex).__name__}", "-"))
        return findings

    if b"\x00" in raw[:4096]:
        return findings  # binary without known suffix

    text = raw.decode("utf-8", "replace")
    is_json = path.suffix.lower() == ".json"
    _scan_text(text, rel, findings, structured=is_json)
    if is_json:
        try:
            _scan_json(json.loads(text), rel, findings)
        except (json.JSONDecodeError, ValueError):
            pass
    return findings


def scan_tree(root: Path, include: Optional[Iterable[Path]] = None) -> List[Finding]:
    """Scan a directory tree (or an explicit iterable of files)."""
    findings: List[Finding] = []
    if include is not None:
        for p in include:
            if p.is_file():
                findings.extend(scan_file(p, root))
        return findings
    for p in sorted(root.rglob("*")):
        if p.is_file():
            findings.extend(scan_file(p, root))
    return findings


def format_report(findings: List[Finding]) -> str:
    if not findings:
        return "CLEAN: no credential-shaped material detected."
    lines = ["SAFE EXPORT BLOCKED", "=" * 60]
    for f in findings:
        lines.append(
            f"file:       {f.file}\n"
            f"reason:     {f.reason}\n"
            f"field/pat:  {f.detail}\n"
            f"sha256-12:  {f.fingerprint}"
        )
    lines.append("=" * 60)
    lines.append(f"{len(findings)} finding(s). Remove or sanitize before sharing.")
    return "\n".join(lines)


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="9router_WatchEdit secret scanner")
    ap.add_argument("--root", default=".", help="root directory to scan")
    ap.add_argument("--json", dest="json_out", help="write findings as JSON")
    ap.add_argument("--quiet", action="store_true", help="only exit code")
    args = ap.parse_args(argv)

    root = Path(args.root).resolve()
    findings = scan_tree(root)
    if args.json_out:
        Path(args.json_out).write_text(json.dumps([asdict(f) for f in findings], indent=2),
                                       encoding="utf-8")
    if not args.quiet:
        print(format_report(findings))
    return 1 if findings else 0


if __name__ == "__main__":
    sys.exit(main())
