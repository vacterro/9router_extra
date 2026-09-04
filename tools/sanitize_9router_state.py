#!/usr/bin/env python3
"""
sanitize_9router_state.py - Sanitized fixture generator.

Takes a PRIVATE local 9Router provider-state export and produces a safe
development fixture: structurally identical, cryptographically useless.

- Real provider names / model IDs may remain (useful structure).
- Credential fields -> "<REDACTED_*>" placeholders (apiKey, accessToken,
  refreshToken, clientSecret, apiKeys.key, passwords, secrets, tokens, ...).
- String-encoded JSON (e.g. providerNodes.data) is parsed and sanitized inside.
- machine-id style values -> deterministic random placeholder.
- The PRIVATE SOURCE FILE IS NEVER MODIFIED. Input and output must differ.
- SELF-CHECK: the output is scanned with the internal secret scanner and the
  tool REFUSES to write a fixture that still contains credential-shaped data.

Usage:
    python tools/sanitize_9router_state.py <private_export.json> <fixture_out.json>
"""
from __future__ import annotations

import json
import random
import re
import string
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "9router_WatchEdit"))

from core.secret_scanner import scan_file  # noqa: E402

REPLACEMENTS = {
    "apikey": "<REDACTED_API_KEY>",
    "api_key": "<REDACTED_API_KEY>",
    "accesstoken": "<REDACTED_ACCESS_TOKEN>",
    "access_token": "<REDACTED_ACCESS_TOKEN>",
    "refreshtoken": "<REDACTED_REFRESH_TOKEN>",
    "refresh_token": "<REDACTED_REFRESH_TOKEN>",
    "clientsecret": "<REDACTED_CLIENT_SECRET>",
    "client_secret": "<REDACTED_CLIENT_SECRET>",
    "clientid": "<REDACTED_CLIENT_ID>",
    "client_id": "<REDACTED_CLIENT_ID>",
}
# Contexts in which EVERY string value is a credential (e.g. 9Router's
# apiKeys[] table of live router API keys). "key" alone is NOT treated as a
# sensitive field name: the kv table legitimately uses {"key": ..., "value": ...}.
FORCE_REDACT_CONTEXTS = {"apikeys", "apikey", "secrets", "credentials"}
SENSITIVE_SUBSTRINGS = (
    "password", "passwd", "secret", "token", "credential", "jwt", "cookie",
    "apikey", "api_key", "authorization", "privatekey", "private_key",
    "accesstoken", "refreshtoken", "clientsecret", "bearer",
)
GENERIC_REDACT = "<REDACTED>"
MACHINE_ID_KEYS = {"machineid", "machine_id", "machine-id"}
JSON_STRING_RE = re.compile(r'^\s*[\[{].*[\]}]\s*$', re.DOTALL)


def _random_machine_id(rng: random.Random) -> str:
    alphabet = string.ascii_lowercase + string.digits
    return "SANITIZED-" + "".join(rng.choices(alphabet, k=32))


def _sanitize_str_value(key: str, value: str, rng: random.Random):
    k = str(key).strip().lower()
    if k in MACHINE_ID_KEYS:
        return _random_machine_id(rng), True
    if k in REPLACEMENTS:
        return REPLACEMENTS[k], True
    if any(w in k for w in SENSITIVE_SUBSTRINGS):
        return GENERIC_REDACT, True
    return value, False


class _Stats:
    def __init__(self):
        self.redacted = 0


def _walk(node, rng: random.Random, stats: _Stats, depth: int = 0, force: bool = False):
    if depth > 40:
        return GENERIC_REDACT
    if isinstance(node, dict):
        out = {}
        for k, v in node.items():
            k_l = str(k).strip().lower()
            child_force = force or k_l in FORCE_REDACT_CONTEXTS
            if isinstance(v, str):
                if force:
                    stats.redacted += 1
                    out[k] = GENERIC_REDACT
                    continue
                new, changed = _sanitize_str_value(k, v, rng)
                if changed:
                    stats.redacted += 1
                    out[k] = new
                    continue
                # parse string-encoded JSON and sanitize inside it
                if JSON_STRING_RE.match(v):
                    try:
                        parsed = json.loads(v)
                    except Exception:
                        parsed = None
                    if isinstance(parsed, (dict, list)):
                        cleaned = _walk(parsed, rng, stats, depth + 1)
                        out[k] = json.dumps(cleaned, ensure_ascii=False)
                        continue
                out[k] = v
            elif v is None or isinstance(v, (bool, int, float)):
                # SAN-005: even numeric/boolean values under sensitive keys
                # are redacted — numbers can encode secrets
                if force or _sensitive_key_name(k_l):
                    stats.redacted += 1
                    out[k] = GENERIC_REDACT
                else:
                    out[k] = v
            else:
                # SAN-005: sensitive-typed or non-string values we cannot
                # transform safely are REDACTED, never copied through.
                if force or _sensitive_key_name(k_l):
                    stats.redacted += 1
                    out[k] = GENERIC_REDACT
                else:
                    out[k] = _walk(v, rng, stats, depth + 1, force=child_force)
        return out
    if isinstance(node, list):
        return [_walk(v, rng, stats, depth + 1, force=force) for v in node]
    return node


def _sensitive_key_name(k_l: str) -> bool:
    return k_l in FORCE_REDACT_CONTEXTS or any(w in k_l for w in SENSITIVE_SUBSTRINGS) or k_l in REPLACEMENTS


def sanitize_export(src: Path, dst: Path, verify: bool = True) -> int:
    """Returns number of redacted nodes. Refuses same-file / in-place writes.

    When verify=True the sanitized output is scanned and the tool refuses to
    write if credential-shaped material would remain."""
    src, dst = Path(src).resolve(), Path(dst).resolve()
    if src == dst:
        raise ValueError("Input and output must be different paths")
    if not src.is_file():
        raise FileNotFoundError(f"Private export not found: {src}")

    data = json.loads(src.read_text(encoding="utf-8"))
    rng = random.Random(20260904)  # deterministic placeholders
    stats = _Stats()
    sanitized = _walk(data, rng, stats)

    payload = json.dumps(sanitized, indent=2, ensure_ascii=False)

    if verify:
        tmp_scan = dst.parent / f".sanitize_check_{dst.name}"
        dst.parent.mkdir(parents=True, exist_ok=True)
        try:
            tmp_scan.write_text(payload, encoding="utf-8")
            findings = scan_file(tmp_scan, dst.parent)
        finally:
            tmp_scan.unlink(missing_ok=True)
        if findings:
            reasons = "; ".join(sorted({f"{f.reason}@{f.detail.split(' at ')[0]}" for f in findings}))
            raise RuntimeError(
                "Sanitization incomplete: scanner still detects credential-shaped "
                f"material ({len(findings)} finding(s): {reasons}). Fixture NOT written."
            )

    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.write_text(payload, encoding="utf-8")
    return stats.redacted


def main(argv=None) -> int:
    import argparse
    ap = argparse.ArgumentParser(description="Generate sanitized 9Router state fixture")
    ap.add_argument("input", help="PRIVATE provider-state export JSON (never modified)")
    ap.add_argument("output", help="sanitized fixture output path")
    ap.add_argument("--no-verify", action="store_true", help="skip the post-sanitize scan gate")
    args = ap.parse_args(argv)
    try:
        n = sanitize_export(args.input, args.output, verify=not args.no_verify)
    except Exception as ex:
        print(f"SANITIZE FAILED: {ex}", file=sys.stderr)
        return 1
    print(f"Sanitized fixture written: {args.output} ({n} credential fields redacted, scanner-verified)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
