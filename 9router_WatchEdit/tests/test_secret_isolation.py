"""
9router_WatchEdit - Secret Isolation & Safe External Development Mode
Acceptance tests for task sections 25 A-L (automatable subset) plus the
redaction marker requirement of section 19.

All tests run offline. WATCHEDIT_DATA_DIR / tmp_path isolate every local-layer
path so nothing ever touches the operator's real %LOCALAPPDATA% data.
"""
import json
import os
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
TOOLS = REPO_ROOT / "tools"
sys.path.insert(0, str(TOOLS))

from core.redaction import redact_exception, redact_mapping, redact_text  # noqa: E402
from core.router_client import RouterClient  # noqa: E402
from core.security import LOCKED, OS_VAULT, UNLOCKED, LiveAccessLockedError, SecurityManager  # noqa: E402
from core.secret_store import VaultStore, dpapi_protect, dpapi_unprotect  # noqa: E402

IS_WINDOWS = sys.platform == "win32"
VAULT_LIBS = VaultStore.available()

MARKER = "SUPER_SECRET_TEST_VALUE_123456"


# ---------------------------------------------------------------------------
# Realistic fake credentials are BUILT at runtime (base64/hex fragments) so
# this test source itself never contains credential-shaped literals and stays
# clean for the repo-wide scanner and SAFE share.
# ---------------------------------------------------------------------------
import base64 as _b64


def _fake_jwt() -> str:
    h = _b64.b64encode(b'{"alg":"HS256"}').decode().rstrip("=")
    p = _b64.b64encode(b'{"sub":"1234567890"}').decode().rstrip("=")
    s = _b64.urlsafe_b64encode(b"0123456789abcdef0123456789abcd").decode().rstrip("=")
    return h + "." + p + "." + s


def _fake_sk() -> str:
    return ("s" + "k-") + "TEST" + "f7c1a9d0" + "2e8b4416" + "a9c3d5e7" + "f1a2b3c4"


def _fake_refresh() -> str:
    return "1a2b3c4d" + "5e6f7081" + "92a3b4c5" + "d6e7f809" + "aabb"


def _fake_9r() -> str:
    return ("9" + "r-") + "cli-" + "0123" + "4567890" + "abcdef"


# =============================================================================
# Section 19 + 25(I): global redaction boundary
# =============================================================================
class TestRedaction:
    def test_marker_never_survives_structured_redaction(self):
        obj = {"apiKey": MARKER, "accessToken": "x" + MARKER, "nested": [{"clientSecret": MARKER}]},
        red = redact_mapping(obj)
        assert MARKER not in json.dumps(red)
        assert all(v == "<REDACTED>" for v in red[0]["nested"][0].values())

    def test_bearer_and_jwt_and_provider_keys_redacted_in_text(self):
        text = (
            f"Authorization: Bearer {MARKER}\n"
            f"probed {_fake_jwt()} for conn\n"
            f"key {_fake_sk()} and {_fake_9r()}"
        )
        red = redact_text(text)
        assert MARKER not in red
        assert "eyJ" not in red
        assert _fake_sk() not in red
        assert _fake_9r() not in red
        assert "<REDACTED>" in red

    def test_exception_redaction(self):
        class FakeError(RuntimeError):
            pass
        ex = FakeError(f"probe failed with bearer {MARKER}")
        assert MARKER not in redact_exception(ex)


# =============================================================================
# Section 5/6/23: SecretStore backends
# =============================================================================
class TestSecretStores:
    def test_dpapi_roundtrip_current_user(self):
        if not IS_WINDOWS:
            pytest.skip("DPAPI is Windows-only")
        blob = dpapi_protect(b"test-secret-bytes")
        assert b"test-secret-bytes" not in blob  # actually encrypted
        assert dpapi_unprotect(blob) == b"test-secret-bytes"

    @pytest.mark.skipif(not VAULT_LIBS, reason="vault libraries not installed")
    def test_vault_roundtrip_and_wrong_password(self, tmp_path):
        vault = VaultStore(tmp_path / "credentials.vault")
        vault.create("correct horse battery staple", {"provider/key": "value-1"})
        raw = (tmp_path / "credentials.vault").read_text(encoding="utf-8")
        assert "value-1" not in raw  # payload encrypted at rest
        assert json.loads(raw)["kdf"] == "argon2id"

        secrets = vault.load("correct horse battery staple")
        assert secrets == {"provider/key": "value-1"}

        with pytest.raises(Exception):
            vault.load("wrong password")

    def test_vault_unavailable_message(self, tmp_path, monkeypatch):
        # Simulate missing libraries
        import core.secret_store as ss
        monkeypatch.setattr(ss, "_vault_libs_available", lambda: False)
        with pytest.raises(Exception) as ei:
            VaultStore(tmp_path / "v.vault")
        assert "argon2" in str(ei.value) or "cryptography" in str(ei.value)


# =============================================================================
# Section 7/17/18 + 25(B, G, H): SecurityManager lock/unlock gating
# =============================================================================
class TestSecurityManager:
    def test_fresh_checkout_defaults_to_locked(self, tmp_path, monkeypatch):
        monkeypatch.delenv("WATCHEDIT_LIVE_ACCESS", raising=False)
        mgr = SecurityManager(data_dir=tmp_path)
        assert mgr.state == LOCKED
        assert mgr.is_locked()
        assert not mgr.is_live_allowed()

    def test_locked_client_refuses_live_operations_immediately(self, tmp_path, monkeypatch):
        monkeypatch.delenv("WATCHEDIT_LIVE_ACCESS", raising=False)
        mgr = SecurityManager(data_dir=tmp_path)
        client = RouterClient(base_url="http://127.0.0.1:1", security=mgr)
        with pytest.raises(LiveAccessLockedError) as ei:
            client.get_combos()
        assert "Live access requires local credential unlock" in str(ei.value)
        # Every brokered provider operation is gated the same way
        for call in (
            lambda: client.get_providers(),
            lambda: client.get_provider_nodes(),
            lambda: client.get_catalog_models(),
            lambda: client.create_combo("n", []),
            lambda: client.delete_combo("x"),
            lambda: client.ping_model_fast("p/m"),
            lambda: client.probe_chat_completion("p/m"),
        ):
            with pytest.raises(LiveAccessLockedError):
                call()

    def test_unlock_os_then_lock_stops_access(self, tmp_path, monkeypatch):
        if not IS_WINDOWS:
            pytest.skip("OS-backed grant uses DPAPI (Windows-only)")
        monkeypatch.delenv("WATCHEDIT_LIVE_ACCESS", raising=False)
        mgr = SecurityManager(data_dir=tmp_path)
        client = RouterClient(base_url="http://127.0.0.1:1", security=mgr)

        mgr.unlock_os()  # creates + validates grant
        assert mgr.state == OS_VAULT
        assert mgr.is_live_allowed()
        # unlocked: the gate no longer raises (request itself will fail offline,
        # so we only assert the security layer passes)
        try:
            client.get_combos()
        except LiveAccessLockedError:
            pytest.fail("unlocked client must not raise LiveAccessLockedError")
        except Exception:
            pass  # network failure is fine here

        # 25(G): locking stops live provider actions immediately
        mgr.lock()
        assert mgr.state == LOCKED
        with pytest.raises(LiveAccessLockedError):
            client.get_combos()

    @pytest.mark.skipif(not VAULT_LIBS, reason="vault libraries not installed")
    def test_unlock_with_master_password(self, tmp_path, monkeypatch):
        monkeypatch.delenv("WATCHEDIT_LIVE_ACCESS", raising=False)
        mgr = SecurityManager(data_dir=tmp_path)
        vault = VaultStore(mgr._vault_path)
        vault.create("master-pass-123", {"router/api": "abc"})
        with pytest.raises(Exception):
            mgr.unlock_with_password("wrong")
        assert mgr.state == LOCKED
        mgr.unlock_with_password("master-pass-123")
        assert mgr.state == UNLOCKED
        assert mgr.get_vault_secret("router/api") == "abc"
        mgr.lock()
        assert mgr.get_vault_secret("router/api") is None  # wiped from memory

    def test_sanitize_connection_boundary(self, tmp_path):
        """Provider credentials in /api/providers responses never escape the client."""
        mgr = SecurityManager(data_dir=tmp_path)
        client = RouterClient(base_url="http://127.0.0.1:1", security=mgr)
        fake_key = ("s" + "k-") + "reallysecret" + "value9999999"
        fake_tok = "tok-" + "reallysecret" + "value999999"
        raw = {
            "id": "c1", "provider": "someprov", "name": "Some Provider",
            "isActive": True, "updatedAt": "T1",
            "providerSpecificData": {
                "prefix": "spm",
                "apiKey": fake_key,
                "accessToken": fake_tok,
            },
        }
        clean = client._sanitize_connection(raw)
        assert clean["providerSpecificData"] == {"prefix": "spm"}
        flat = json.dumps(clean)
        assert fake_key not in flat
        assert "tok-reallysecret" not in flat


# =============================================================================
# Section 11/12/13 + 25(D, E, L): scanner, sanitizer, safe share, diagnostics
# =============================================================================
class TestScannerAndExports:
    def test_scanner_detects_fake_credentials(self, tmp_path):
        from core.secret_scanner import scan_file
        fixture = tmp_path / "sneaky.json"
        fixture.write_text(json.dumps({
            "name": "conn",
            "apiKey": _fake_sk(),
            "refreshToken": _fake_refresh(),
        }), encoding="utf-8")
        findings = scan_file(fixture, tmp_path)
        reasons = {f.reason for f in findings}
        assert "sensitive_json_key" in reasons or "provider_api_key" in reasons

        jwt_fixture = tmp_path / "jwt.txt"
        jwt_fixture.write_text(f'probe_tok = "{_fake_jwt()}"', encoding="utf-8")
        findings = scan_file(jwt_fixture, tmp_path)
        assert any(f.reason == "jwt_token" for f in findings)

        # Findings never contain the value itself
        report = "\n".join(f.detail + f.fingerprint for f in findings)
        assert _fake_sk()[:14] not in report

    def test_scanner_passes_sanitized_placeholders(self, tmp_path):
        from core.secret_scanner import scan_file
        fixture = tmp_path / "provider_state_sanitized.json"
        fixture.write_text(json.dumps({
            "name": "conn",
            "apiKey": "<REDACTED_API_KEY>",
            "accessToken": "fake-access-token",
            "clientSecret": "example-client-secret",
            "password": "TEST_PASSWORD_DO_NOT_USE",
        }), encoding="utf-8")
        assert scan_file(fixture, tmp_path) == []

    def test_scanner_source_names_are_not_exempt(self, tmp_path):
        from core.secret_scanner import scan_file
        from verify_agent_safe import verify_agent_safe

        canary = _fake_sk()
        wrapper = tmp_path / "tools" / "secret_scan.py"
        nested = tmp_path / "nested" / "secret_scanner.py"
        wrapper.parent.mkdir(parents=True)
        nested.parent.mkdir(parents=True)
        wrapper.write_text(json.dumps({"apiKey": canary}), encoding="utf-8")
        nested.write_text(json.dumps({"apiKey": canary}), encoding="utf-8")

        assert scan_file(wrapper, tmp_path)
        assert scan_file(nested, tmp_path)
        unsafe_paths = {u.path.replace("\\", "/") for u in verify_agent_safe(tmp_path)}
        assert any("tools/secret_scan.py" in path for path in unsafe_paths)
        assert any("nested/secret_scanner.py" in path for path in unsafe_paths)

    def test_saipen_op_marker_is_not_a_finding_but_bare_hex_is(self, tmp_path):
        """Precision (T-1 / SRC-001:R0015): the bare `[op: <32 lower hex>]`
        SAIOPS marker is protocol bookkeeping, not a credential, even when its
        LOG line also contains the word 'secret'/'token'. A 32-hex value NOT in
        that marker position stays flaggable."""
        from core.secret_scanner import scan_file

        # Built at runtime so this test file itself stays free of a bare
        # 32-hex literal (which the whole-tree scan must flag).
        op_hex = "4d81c0e5a29347f6b0d8e21c95a7f30" + "4"

        marker = tmp_path / "LOG.md"
        marker.write_text(
            f"- 11.09.26 [E-109] [op: {op_hex}] "
            "RUN: secret gate CLEAN, token rotation done\n",
            encoding="utf-8",
        )
        assert scan_file(marker, tmp_path) == []

        # Negative control: the SAME hex outside the [op: ] marker is flagged.
        bare = tmp_path / "bare.md"
        bare.write_text(f"token = {op_hex} secret\n", encoding="utf-8")
        reasons = {f.reason for f in scan_file(bare, tmp_path)}
        assert "high_entropy_near_credential_word" in reasons

    def test_sanitize_tool_never_touches_source(self, tmp_path):
        from sanitize_9router_state import sanitize_export
        fake_live = ("s" + "k-") + "live" + "1234567890" + "abcdef"
        src = tmp_path / "private.json"
        src.write_text(json.dumps({
            "providerNodes": [{"id": "n1", "name": "Real Node", "data": json.dumps({"apiKey": fake_live})}],
            "machine-id": "0123456789abcdef0123456789abcdef",
        }), encoding="utf-8")
        out = tmp_path / "fixture.json"
        n = sanitize_export(src, out)
        assert n >= 1
        assert fake_live in src.read_text(encoding="utf-8")      # source untouched
        assert fake_live not in out.read_text(encoding="utf-8")  # output sanitized
        with pytest.raises(ValueError):
            sanitize_export(src, src)  # same path refused

    def test_safe_share_blocks_on_fake_secrets(self, tmp_path, monkeypatch):
        """25(D): builder refuses a fixture containing fake apiKey / JWT / refresh token."""
        import build_safe_share as bss
        # Plant a credential-bearing file inside an allowed tree
        planted = REPO_ROOT / "9router_WatchEdit" / "tests" / "fixtures" / "PLANTED_secret_test.json"
        planted.parent.mkdir(parents=True, exist_ok=True)
        planted.write_text(json.dumps({
            "apiKey": _fake_sk(),
            "refreshToken": _fake_refresh(),
            "jwt": _fake_jwt(),
        }), encoding="utf-8")
        try:
            result = bss.build_safe_share(out_dir=tmp_path / "share", skip_gitleaks=True, quiet=True)
            assert result is None, "SAFE export must be BLOCKED when fake secrets are present"
        finally:
            planted.unlink(missing_ok=True)
        # After removing the plant, the share builds and the archive is clean
        result = bss.build_safe_share(out_dir=tmp_path / "share", skip_gitleaks=True, quiet=True)
        assert result is not None and result.exists()

    def test_safe_archive_contains_no_private_material(self, tmp_path):
        """25(E): no backups, SQLite, JWT secret, machine-id or vault in SAFE archive."""
        import build_safe_share as bss
        out_dir = tmp_path / "share2"
        result = bss.build_safe_share(out_dir=out_dir, skip_gitleaks=True, quiet=True)
        assert result is not None
        with zipfile.ZipFile(result) as zf:
            names = zf.namelist()
        lowered = " ".join(names).lower()
        for banned in ("backup/", ".sqlite", "jwt-secret", "machine-id", ".vault",
                       "providers-state-export", ".env", "health_cache", "presets.json",
                       "settings.json", "packages/", "patches/"):
            assert banned not in lowered, f"SAFE archive leaked: {banned}"

    def test_diagnostic_bundle_redacted_and_scanned(self, tmp_path):
        """25(L) + section 20: diagnostic export passes the secret scan, no marker."""
        from core.diagnostics import export_diagnostic_bundle
        from core.history import HealthCache
        cache = HealthCache(cache_file=tmp_path / "cache.json")
        out = export_diagnostic_bundle(
            cache=cache,
            discovery_outcomes={"conn-1": "OK"},
            recent_errors=[f"Authorization: Bearer {MARKER}", "plain failure"],
            out_dir=tmp_path / "diag",
        )
        payload = out.read_text(encoding="utf-8")
        assert MARKER not in payload
        assert "<REDACTED>" in payload
        assert "configuration_schema" in payload


# =============================================================================
# Section 9/21 + 25(K): private backup + migration
# =============================================================================
class TestPrivateBackupAndMigration:
    @pytest.mark.skipif(not VAULT_LIBS, reason="vault libraries not installed")
    def test_private_backup_encrypted_outside_repo(self, tmp_path, monkeypatch):
        """25(K): private backup is encrypted, outside repository, loudly named."""
        import create_private_backup as cpb
        # Isolate the local data dir the tool reads from
        data_dir = tmp_path / "localdata"
        for sub in ("config", "secure"):
            (data_dir / sub).mkdir(parents=True)
        (data_dir / "config" / "settings.json").write_text('{"trusted_os_unlock": false}', encoding="utf-8")
        monkeypatch.setattr(cpb, "HEALTH_CACHE_FILE", data_dir / "health_cache.json")
        monkeypatch.setattr(cpb, "PRESETS_FILE", data_dir / "presets.json")
        monkeypatch.setattr(cpb, "SETTINGS_FILE", data_dir / "settings.json")
        monkeypatch.setattr(cpb, "SECURE_DIR", data_dir / "secure")
        monkeypatch.setattr(cpb, "LOCALAPPDATA_DIR", data_dir)
        monkeypatch.setattr(cpb, "PRIVATE_BACKUP_DIR", tmp_path / "private_backups")

        out = cpb.create_private_backup("master-pass-123", out_dir=tmp_path / "private_backups")
        assert out.exists()
        assert "PRIVATE_SECRET_BACKUP" in out.name
        assert out.suffix == ".wvault"
        assert REPO_ROOT not in out.parents  # outside the repository
        with zipfile.ZipFile(out) as zf:
            payload = zf.read("payload.vault")
        assert b"trusted_os_unlock" not in payload  # encrypted, not plaintext
        vault = VaultStore(tmp_path / "reextract.vault")
        (tmp_path / "reextract.vault").write_bytes(payload)
        secrets = vault.load("master-pass-123")
        assert any("settings.json" in k for k in secrets)

    def test_migration_scan_only_detects_and_reports(self, tmp_path):
        """21: migration tool detects legacy private files without deleting in scan-only mode."""
        import migrate_legacy_secrets as mls
        fake_repo = tmp_path / "fakerepo"
        (fake_repo / "backup" / "data-snapshot").mkdir(parents=True)
        (fake_repo / "backup" / "providers-state-export.json").write_text('{"x":1}', encoding="utf-8")
        (fake_repo / "backup" / "data-snapshot" / "jwt-secret").write_text("x", encoding="utf-8")
        (fake_repo / "src").mkdir()
        (fake_repo / "src" / "app.py").write_text("print('ok')", encoding="utf-8")

        rc = mls.migrate(scan_only=True, repo_root=fake_repo, legacy_root=tmp_path / "legacy")
        assert rc == 1
        assert (fake_repo / "backup" / "providers-state-export.json").exists()  # nothing deleted

        rc = mls.migrate(scan_only=False, repo_root=fake_repo, legacy_root=tmp_path / "legacy")
        assert rc == 0
        assert not (fake_repo / "backup" / "providers-state-export.json").exists()
        moved = list((tmp_path / "legacy").rglob("jwt-secret"))
        assert moved, "private file must be relocated, not deleted"


# =============================================================================
# Section 15/16 + 25(A, J): git protection + repo cleanliness
# =============================================================================
class TestGitProtection:
    def test_precommit_scans_staged_blob_not_worktree(self, tmp_path, monkeypatch, capsys):
        import pre_commit_secret_check as pcs

        repo = tmp_path / "repo"
        repo.mkdir()

        def git(*args):
            return subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True)

        assert git("init", "-q").returncode == 0
        assert git("config", "user.name", "t").returncode == 0
        assert git("config", "user.email", "t@local").returncode == 0
        staged = repo / "config.json"
        staged.write_text(json.dumps({"apiKey": _fake_sk()}), encoding="utf-8")
        assert git("add", "config.json").returncode == 0

        staged.write_text("benign worktree copy\n", encoding="utf-8")
        monkeypatch.setattr(pcs, "REPO_ROOT", repo)

        assert pcs.main() == 1
        assert "COMMIT BLOCKED" in capsys.readouterr().out

    def test_git_history_report_tool(self):
        """25(J): history check reports a definitive verdict."""
        from check_git_history import check_history
        rc = check_history()
        assert rc in (0, 1)

    def test_repository_scan_clean_of_operator_credentials(self):
        """25(A): repo scan finds zero operator credential material.

        Engine artifacts (patches/, packages/) were relocated outside the
        repository in the direct-agent-mode wave, so the entire tree —
        including the sanitized fixtures — must scan completely clean."""
        from core.secret_scanner import scan_tree
        findings = scan_tree(REPO_ROOT)
        assert findings == [], f"unexpected findings: {[(f.file, f.reason) for f in findings]}"
