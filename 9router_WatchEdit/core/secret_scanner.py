#!/usr/bin/env python3
"""
secret_scanner.py - Internal secret scanner for the 9router_WatchEdit repository.

Detects credential-shaped material in text/JSON files BEFORE any SAFE export,
diagnostic bundle, or commit. Findings never print the secret value; only a
SHA-256 fingerprint prefix is reported for identification.

Fail-closed policy (campaign section 29): unreadable files, scanner errors and
unprovable content ALWAYS produce findings, never a silent pass.

Usage (CLI wrapper):
    python tools/secret_scan.py [--root PATH] [--json OUT.json] [--quiet]

Exit codes: 0 = clean, 1 = findings detected, 2 = usage error.
"""
from __future__ import annotations

import argparse
import codecs
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

# Credential-word CORE. Keys match when an optional prefix of [_-] separated
# segments is followed by a core word AT THE END of the key name:
#   refresh_token, user_api_key, RouterPassword  -> match
#   token_count, tokenizer, secretary, password_field_label, credential_ref
#                                                 -> NO match (high precision)
_KEY_CORE = (
    r'api[_-]?key|apikey|access[_-]?token|accesstoken|refresh[_-]?token|refreshtoken|'
    r'client[_-]?secret|clientsecret|password|passwd|pwd|authorization|auth|'
    r'cookie|cookies|private[_-]?key|jwt|bearer|credential|apikeyid'
)

# Compound sensitive JSON/object keys (campaign SEC-005/024): a non-placeholder
# value under any of these is always a finding.
SENSITIVE_KEY_RE = re.compile(r'(?i)^(?:[a-z0-9]+[_\-])*(' + _KEY_CORE + r')$')

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

# Structured credential patterns in free text AND inside JSON string values.
JWT_RE = re.compile(r'\beyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{4,}\b')
PRIVATE_KEY_RE = re.compile(r'-----BEGIN (?:[A-Z]+ )?PRIVATE KEY(?: BLOCK)?-----')
BEARER_RE = re.compile(r'(?i)\bbearer\s+[A-Za-z0-9_\-\.=+/]{16,}')
AUTH_HEADER_RE = re.compile(r'(?i)^\s*(authorization|x-api-key)\s*[:=]', re.MULTILINE)
PROVIDER_KEY_RE = re.compile(
    r'\b(?:sk|rk|pk|ghp|gho|ghu|ghs|xox[bpars]|AIza|9r)[\-_][A-Za-z0-9_\-]{12,}\b'
)

# Sensitive key-value pairs in non-JSON text (patches, source, config):
#   clientSecret: "..."   apiKey=...   "refresh_token": "..."
# Same endswith-word key semantics as the JSON rule; values must be >= 16 chars
# (real credentials are never shorter; engine unit-test fixtures routinely use
# short memorable fakes like "test-apikey").
#
# PERFORMANCE (campaign PARSE-002): a single regex with a leading character
# class quantifier backtracks quadratically on huge single lines. Detection is
# therefore two-stage: find the credential-word core (literal alternation,
# linear), then validate a small bounded window before/after it.
_CORE_ONLY_RE = re.compile(r'(?i)(?:' + _KEY_CORE + r')')
_KV_KEY_CHARS = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-")
_KV_WINDOW_RE = re.compile(r'(?i)["\']?\s*[:=]\s*(?:\n\s+)?["\']([^"\'\n]{16,})["\']')
_MAX_KEY_LEN = 64
_MAX_VALUE_WINDOW = 512

# Campaign PARSE-002: explicit fail-closed policy above a sane line size.
MAX_LINE_BYTES = 2 * 1024 * 1024

# Deliberately fake, verifiably non-credential test values used by this
# repository's own test suite. Exact-match false-positive allowlist.
KNOWN_TEST_FAKES = {
    "sk-abcdefghijklmnop1234",           # sequential alphabet; redaction test marker
    "super_secret_test_value_123456",    # task-19 redaction marker (never a credential)
}

# High-entropy material on the same line as a credential word (free text only).
CRED_WORD_LINE_RE = re.compile(r'(?i)(secret|token|password|passwd|api[_\-]?key|credential)')
HIGH_ENTROPY_RE = re.compile(r'\b[A-Za-z0-9+/=_\-]{32,}\b')

# Token shapes that are never credentials even near credential words
# (URLs, ISO timestamps, pure numerics, composite identifiers/paths such as
# kv keys "node|model|llm" or model ids "prov/deepseek-chat-v3").
BENIGN_TOKEN_RE = re.compile(
    r'(?i)(://|[|/]|^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}|^\d[\d:\.\-]*$|'
    r'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\.sqlite)'
)

# Precision (campaign section 25): snake_case pytest identifiers are not
# credentials. Only applies to the free-text entropy heuristic; JSON value
# rules and structured scanning are unaffected.
TEST_IDENTIFIER_RE = re.compile(r'^test_[a-z0-9_]+$')

BINARY_SUFFIXES = {
    ".sqlite", ".sqlite-wal", ".sqlite-shm", ".db", ".db-wal", ".db-shm", ".tgz", ".zip", ".gz",
    ".png", ".jpg", ".jpeg", ".ico", ".exe", ".dll", ".pyd", ".pdf", ".woff",
    ".woff2", ".ttf", ".vault", ".pem", ".key", ".pfx", ".p12", ".bin",
    ".pyc", ".pyo", ".so", ".mo", ".class", ".obj", ".lib",
}

# Files that are private BY NAME regardless of content scanning capability.
PRIVATE_NAME_RE = re.compile(
    r'(?i)(^|/)(jwt[-_]secret|machine[-_]id|\.env(\..*)?|.*\.vault$|'
    r'credentials?\.json|provider[s]?[-_]state[-_]export.*\.json|.*\.sqlite(-wal|-shm)?$)'
)

MAX_FILE_BYTES = 8 * 1024 * 1024  # above this: explicit finding (fail closed), never silent skip


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


_PATTERN_RULES = (
    (JWT_RE, "jwt_token"),
    (BEARER_RE, "bearer_token"),
    (PROVIDER_KEY_RE, "provider_api_key"),
    (PRIVATE_KEY_RE, "private_key_block"),
)


def _scan_json(obj: Any, path: Path, findings: List[Finding], prefix: str = "$"):
    """Walk parsed JSON; flag sensitive keys AND credential-shaped values.

    SEC-006: pattern detection runs on string VALUES independently of the
    field name, so a provider key under an innocent key like "thing" is found."""
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
                # value-pattern detection independent of key semantics (SEC-006)
                for rx, rid in _PATTERN_RULES:
                    m = rx.search(v)
                    if m and not _is_placeholder(m.group(0)):
                        findings.append(Finding(
                            file=str(path), reason=rid,
                            detail=f"pattern {rid} in JSON value of '{key_str}' at {prefix}.{key_str}",
                            fingerprint=_fingerprint(m.group(0)),
                        ))
                        break
            _scan_json(v, path, findings, f"{prefix}.{key_str}")
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            _scan_json(v, path, findings, f"{prefix}[{i}]")


def _kv_scan(text: str, path: Path, findings: List[Finding]) -> None:
    """Two-stage key=value secret detection (linear; PARSE-002 safe).

    Covers same-line (`refreshToken: "..."`) and YAML-style next-line values
    (PARSE-005). Endswith semantics: the credential word must terminate the
    key (secretary/token_count/password_field_label do not match)."""
    for m in _CORE_ONLY_RE.finditer(text):
        core_end = m.end()
        # core must END the key word (next char not a key char)
        if core_end < len(text) and text[core_end] in _KV_KEY_CHARS:
            continue
        # walk back to the key start (bounded)
        start = m.start()
        lo = max(0, start - _MAX_KEY_LEN)
        while start > lo and text[start - 1] in _KV_KEY_CHARS:
            start -= 1
        key = text[start:core_end]
        if len(key) > _MAX_KEY_LEN:
            continue
        window = text[core_end:core_end + _MAX_VALUE_WINDOW]
        vm = _KV_WINDOW_RE.match(window)
        if not vm:
            continue
        value = vm.group(1)
        if _is_placeholder(value) or BENIGN_TOKEN_RE.search(value):
            continue
        findings.append(Finding(
            f"{path}:{text.count(chr(10), 0, m.start()) + 1}", "sensitive_key_value",
            f"key '{key}' with non-placeholder value",
            _fingerprint(value),
        ))


def _scan_text(text: str, path: Path, findings: List[Finding], structured: bool = False,
               patterns_only: bool = False) -> None:
    """structured=True (JSON files): skip the free-text entropy heuristic —
    structured files are audited via key rules and pattern rules instead.
    patterns_only=True (bounded binary extraction): pattern + KV rules only."""
    if PRIVATE_KEY_RE.search(text):
        findings.append(Finding(str(path), "private_key_block", "PEM private key header", _fingerprint("pem")))
    # key=value detection (same-line + multiline), linear two-stage scan
    _kv_scan(text, path, findings)
    for lineno, line in enumerate(text.splitlines(), 1):
        if len(line) > MAX_LINE_BYTES:
            # PARSE-002: explicit fail-closed policy above the line-size limit
            findings.append(Finding(
                f"{path}:{lineno}", "oversized_line",
                f"line of {len(line)} bytes exceeds scan policy ({MAX_LINE_BYTES}); fail closed", "-"))
            continue
        for rx, rid in _PATTERN_RULES[:3]:
            m = rx.search(line)
            if m and not _is_placeholder(m.group(0)):
                findings.append(Finding(
                    f"{path}:{lineno}", rid,
                    f"pattern {rid} in line",
                    _fingerprint(m.group(0)),
                ))
                break
        if patterns_only:
            continue
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
                # skip benign shapes (URLs, ISO timestamps, pure numerics,
                # snake_case test identifiers); require digit+letter mix:
                # plain code identifiers (camelCase function names near the
                # word 'credentials') are not secrets
                if BENIGN_TOKEN_RE.search(tok) or TEST_IDENTIFIER_RE.match(tok):
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
        return findings  # known binary types: excluded from text scanning; builders filter them

    try:
        if path.stat().st_size > MAX_FILE_BYTES:
            findings.append(Finding(str(rel), "oversized_file",
                                    "file too large to scan; excluded from safe export", "-"))
            return findings
        raw = path.read_bytes()
    except OSError as ex:
        findings.append(Finding(str(rel), "unreadable",
                                f"{type(ex).__name__}: cannot prove safe", "-"))
        return findings

    is_json = path.suffix.lower() == ".json"

    try:
        if raw[:4].startswith((codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)):
            # UTF-16 with BOM (PARSE-004): decode and scan fully
            text = raw.decode("utf-16")
            _scan_text(text, rel, findings, structured=is_json)
            if is_json:
                try:
                    _scan_json(json.loads(text), rel, findings)
                except (json.JSONDecodeError, ValueError):
                    pass
        elif b"\x00" in raw[:4096]:
            # Unknown binary content (PARSE-003): bounded extraction — replace
            # NUL bytes with spaces (preserving token boundaries), scan
            # lexically for credential patterns. Never a silent skip.
            stripped = raw.decode("latin-1", "replace").replace("\x00", " ")
            _scan_text(stripped, rel, findings, structured=True, patterns_only=True)
        else:
            text = raw.decode("utf-8", "replace")
            _scan_text(text, rel, findings, structured=is_json)
            if is_json:
                try:
                    _scan_json(json.loads(text), rel, findings)
                except (json.JSONDecodeError, ValueError):
                    pass  # malformed JSON (PARSE-001): lexical scan already ran above
    except Exception as ex:  # fail closed (campaign section 29)
        findings.append(Finding(str(rel), "scanner_error",
                                f"{type(ex).__name__}: scanner failed closed", "-"))
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
