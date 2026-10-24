"""
T-93 regression — the key=value detector must see UNQUOTED credential values.

The window regex required quotes around the value
(`apiKey: "..."`), so the `.env`/config forms the rule exists for
(`apiKey=...`, `apiKey: ...`) were invisible to `sensitive_key_value`: only the
provider-key and free-text entropy patterns could still catch them.

The bare alternative must not turn ordinary code into findings, so a bare value
is accepted only when it is credential-shaped (a `+ = /` symbol or at least four
digits) and not an expression (attribute path, call, interpolation).

Literal credentials here are assembled at runtime so this file's own source
never reads as a key=value pair to the repository's secret scanner.
"""
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "9router_WatchEdit"))

TOK = "a1b2c3d4e5f6g7h8i9j0"        # 20 chars: under the 32-char entropy floor
HEX = "3f9c2b7a1e5d4086"


def _kv(text: str, name: str = "settings.env"):
    from core.secret_scanner import _kv_scan
    findings = []
    _kv_scan(text, Path(name), findings)
    return findings


# --------------------------------------------------------------- detected forms
# Explicit `ids` keep the assembled credential text out of the pytest node ids,
# which would otherwise be persisted into .pytest_cache and re-detected by a
# repository tree scan.
DETECTED = [
    ("double quoted", 'apiKey="%s"' % TOK, "apiKey"),
    ("single quoted", "apiKey='%s'" % TOK, "apiKey"),
    ("quoted with spaces", 'password: "%s"' % (TOK + " " + HEX), "password"),
    ("bare after =", "apiKey=" + TOK, "apiKey"),
    ("bare after :", "apiKey: " + HEX, "apiKey"),
    ("bare after spaced =", "apiKey = " + TOK, "apiKey"),
    ("bare clientSecret", "clientSecret=" + TOK, "clientSecret"),
    ("bare hex password", "password: " + HEX, "password"),
]

DETECTED_IDS = ["dq", "sq", "quoted-spaces", "bare-eq", "bare-colon",
                "bare-spaced-eq", "bare-client-secret", "bare-hex-password"]


@pytest.mark.parametrize("label,text,key", DETECTED, ids=DETECTED_IDS)
def test_kv_detected(label, text, key):
    findings = _kv(text)
    assert findings, f"{label}: {text!r} must be detected"
    assert findings[0].reason == "sensitive_key_value"
    assert key in findings[0].detail


# ------------------------------------------------------------ code is not data
NOT_DETECTED = [
    ("attribute path", "consecutive_auth: self.consecutive_auth"),
    ("enum member", "AUTH: AvailabilityState.AUTH_REJECTED"),
    ("powershell assignment", "apiKey = ConvertTo-PlainText"),
    ("function call", "auth = classify_probe_result"),
    ("env dereference", 'apiKey=os.environ["API_KEY"]'),
    ("yaml interpolation", "apiKey: ${DB_PASSWORD}"),
    ("f-string", "refresh_token: canary_refresh_token()"),
    ("short value", "apiKey=short"),
    ("fake fixture value", "apiKey=test-apikey"),
]


@pytest.mark.parametrize("label,text", NOT_DETECTED,
                         ids=["attribute-path", "enum-member", "ps-assignment",
                              "function-call", "env-deref", "yaml-interpolation",
                              "fstring", "short", "fake-fixture"])
def test_kv_not_detected(label, text):
    assert _kv(text) == [], f"{label}: {text!r} must not be flagged"


def test_bare_code_value_ending_in_call_is_not_flagged():
    """Regression shape seen in this repo: a value that is a call expression
    with an argument list must never match the bare alternative."""
    assert _kv("apiKey = ConvertTo-PlainText $secureKey") == []
    assert _kv('"refresh_token": self.refresh_token') == []


# ---------------------------------------------------------------- file level
def test_scan_file_flags_unquoted_env_file(tmp_path):
    from core.secret_scanner import scan_file
    env = tmp_path / "settings.env"
    env.write_text("apiKey=" + TOK + "\n", encoding="utf-8")
    findings = scan_file(env, tmp_path)
    assert any(f.reason == "sensitive_key_value" for f in findings)


def test_scan_file_leaves_source_code_clean(tmp_path):
    from core.secret_scanner import scan_file
    src = tmp_path / "app.py"
    src.write_text(
        'config = {"consecutive_auth": self.consecutive_auth}\n'
        'if self.availability == AvailabilityState.AUTH_REJECTED:\n'
        '    pass\n',
        encoding="utf-8",
    )
    assert scan_file(src, tmp_path) == []
