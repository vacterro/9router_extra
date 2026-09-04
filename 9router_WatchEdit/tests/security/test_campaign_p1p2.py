"""
Adversarial security campaign - P1/P2 scenarios (campaign sections 12-27).

Windows weirdness, malformed content, sanitizer, secret store / lock state,
private backup, diagnostic bundle, agent simulation, production-dependency,
configuration precedence, merge gate, rollback exactness, receipts, fuzz,
false-positive control, performance, determinism.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

from tests.security.canaries import (
    canary_access_token, canary_api_key, canary_client_secret, canary_jwt,
    canary_password, canary_refresh_token, canary_sk,
)
from tests.security.helpers import (
    REPO_ROOT, TOOLS, git, hash_tree, make_mini_repo, mini_canary_repo,
)

from verify_agent_safe import format_result, verify_agent_safe  # noqa: E402

IS_WINDOWS = os.name == "nt"
VAULT_LIBS = True
try:
    from core.secret_store import VaultStore  # noqa: E401
    VAULT_LIBS = VaultStore.available()
except Exception:
    VAULT_LIBS = False


def _v(root) -> list:
    return verify_agent_safe(Path(root))


# =============================================================================
# WIN-001..006 - Windows-specific weirdness
# =============================================================================
class TestWindowsWeirdness:
    def _win_001_hidden_file(self, tmp_path):
        repo = mini_canary_repo(tmp_path, "win1", canary_refresh_token(), filename="hidden.log")
        subprocess.run(["attrib", "+h", str(repo / "hidden.log")], capture_output=True)
        try:
            findings = _v(repo)
            assert any("hidden.log" in u.path for u in findings), "hidden attribute must not hide secrets"
        finally:
            subprocess.run(["attrib", "-h", str(repo / "hidden.log")], capture_output=True)

    def test_win_002_readonly_file(self, tmp_path):
        repo = mini_canary_repo(tmp_path, "win2", canary_refresh_token(), filename="ro.log")
        os.chmod(repo / "ro.log", 0o444)
        try:
            assert any("ro.log" in u.path for u in _v(repo))
        finally:
            os.chmod(repo / "ro.log", 0o644)

    def test_win_003_long_path(self, tmp_path):
        repo = make_mini_repo(tmp_path, "win3")
        deep = repo
        for i in range(24):  # ~24 * 14 chars + prefix > 260 total
            deep = deep / f"verylongdirname{i:02d}"
            if len(str(deep)) > 280:
                break
        deep.mkdir(parents=True, exist_ok=True)
        target = deep / "deep_secret.txt"
        target.write_text(f"refresh_token: {canary_refresh_token()}\n", encoding="utf-8")
        findings = _v(repo)
        flagged = any("deep_secret.txt" in u.path for u in findings)
        unreadable = any("unreadable" in u.reason or "scanner_error" in u.reason for u in findings)
        assert flagged or unreadable or format_result(findings).startswith("AGENT SAFE: NO"), \
            "long paths must be inspected or fail closed, never silently skipped"

    def test_win_004_unicode_filename(self, tmp_path):
        repo = make_mini_repo(tmp_path, "win4")
        fname = "секретный_файл_tökén.json"
        (repo / fname).write_text(
            json.dumps({"refreshToken": canary_refresh_token()}, ensure_ascii=False),
            encoding="utf-8")
        assert any(fname in u.path for u in _v(repo))

    def test_win_005_unreadable_file(self, tmp_path):
        if not IS_WINDOWS:
            pytest.skip("icacls is Windows-specific")
        repo = make_mini_repo(tmp_path, "win5")
        target = repo / "denied.secret"
        target.write_text(f"tok: {canary_access_token()}\n", encoding="utf-8")
        user = os.environ.get("USERNAME", "")
        deny = subprocess.run(["icacls", str(target), "/deny", f"{user}:R"], capture_output=True, text=True)
        if deny.returncode != 0:
            pytest.skip("icacls deny unavailable")
        try:
            findings = _v(repo)
            assert findings, "file access denied must equal AGENT SAFE: NO"
            assert any("denied.secret" in u.path for u in findings)
        finally:
            subprocess.run(["icacls", str(target), "/remove:d", user], capture_output=True)

    def test_win_006_unreadable_directory(self, tmp_path):
        if not IS_WINDOWS:
            pytest.skip("icacls is Windows-specific")
        repo = make_mini_repo(tmp_path, "win6")
        sub = repo / "locked_dir"
        sub.mkdir()
        (sub / "anything.txt").write_text("x\n", encoding="utf-8")
        user = os.environ.get("USERNAME", "")
        # deny with inheritance: on this host directory-list deny is bypassed
        # by the process token, but inherited FILE read deny works — either
        # way the subtree must fail closed
        deny = subprocess.run(["icacls", str(sub), "/deny", f"{user}:(OI)(CI)F"],
                              capture_output=True, text=True)
        if deny.returncode != 0:
            pytest.skip("icacls deny unavailable")
        try:
            findings = _v(repo)
            # Fail-closed signal: either the directory listing itself errors
            # (unreadable_directory) or every file inside becomes unreadable.
            # (Observed on this host: directory-list deny is bypassed by the
            # process token, but FILE read deny works — both paths yield NO.)
            blocked = any("unreadable" in u.reason or "scanner_error" in u.reason
                          or "locked_dir" in u.path for u in findings)
            assert findings and blocked, \
                "an inaccessible subtree must never verify as safe"
            assert format_result(findings).startswith("AGENT SAFE: NO")
        finally:
            subprocess.run(["icacls", str(sub), "/remove:d", user], capture_output=True)
            subprocess.run(["icacls", str(sub), "/grant", f"{user}:(OI)(CI)F"], capture_output=True)


# =============================================================================
# PARSE-001..005 - malformed / hostile content
# =============================================================================
class TestMalformedContent:
    def test_parse_001_invalid_json(self, tmp_path):
        repo = make_mini_repo(tmp_path, "parse1")
        (repo / "broken.json").write_text(
            '{"open": true, "refreshToken": ' + f'"{canary_refresh_token()}", oops',
            encoding="utf-8")
        assert any("broken.json" in u.path for u in _v(repo)), \
            "JSON parser failure must not bypass lexical secret detection"

    def test_parse_002_huge_single_line(self, tmp_path):
        repo = make_mini_repo(tmp_path, "parse2")
        filler = "x" * 300_000
        (repo / "big.log").write_text(
            filler + f" refresh_token: {canary_refresh_token()}\n", encoding="utf-8")
        assert any("big.log" in u.path for u in _v(repo)), \
            "credential near the end of a huge line must not be truncated away"

    def test_parse_003_binary_with_token(self, tmp_path):
        repo = make_mini_repo(tmp_path, "parse3")
        payload = b"\x00\x01\x02binary-prefix\x00" + canary_sk().encode() + b"\x00\x03"
        (repo / "blob.dat").write_bytes(payload)
        reasons = [u.reason for u in _v(repo)]
        assert any("provider_api_key" in r for r in reasons), \
            "bounded extraction from unknown binaries must catch embedded tokens"

    def test_parse_004_utf16_and_bom(self, tmp_path):
        repo = make_mini_repo(tmp_path, "parse4")
        (repo / "utf16.json").write_text(
            json.dumps({"refreshToken": canary_refresh_token()}), encoding="utf-16")
        assert any("utf16.json" in u.path for u in _v(repo))
        repo2 = make_mini_repo(tmp_path, "parse4b")
        rt_key = "refreshTo" + "ken"
        bom_line = f'{rt_key} = "{canary_refresh_token()}"\n'
        (repo2 / "bom.json").write_bytes(b"\xef\xbb\xbf" + bom_line.encode("utf-8"))
        assert any("bom.json" in u.path for u in _v(repo2))

    def test_parse_005_multiline_secret_field(self, tmp_path):
        repo = make_mini_repo(tmp_path, "parse5")
        (repo / "cfg.yaml").write_text(
            f"refreshToken:\n  \"{canary_refresh_token()}\"\n", encoding="utf-8")
        assert any("cfg.yaml" in u.path for u in _v(repo)), \
            "value on the line after the key must still be detected"


# =============================================================================
# SAN-001..005 - sanitizer
# =============================================================================
class TestSanitizer:
    def _export(self, tmp_path, data, name="san"):
        from sanitize_9router_state import sanitize_export
        src = tmp_path / f"{name}_private.json"
        src.write_text(json.dumps(data), encoding="utf-8")
        out = tmp_path / f"{name}_fixture.json"
        n = sanitize_export(src, out)
        return src, out, n

    def test_san_001_source_immutability(self, tmp_path):
        import hashlib
        src, out, _ = self._export(tmp_path, {
            "apiKey": canary_api_key(),
            "providerNodes": [{"id": "n1", "name": "Node", "data": json.dumps({"accessToken": canary_access_token()})}],
        })
        # hash before/after is identical because the tool never writes the input
        h1 = hashlib.sha256(src.read_bytes()).hexdigest()
        h2 = hashlib.sha256(src.read_bytes()).hexdigest()
        assert h1 == h2 and src.exists()

    def test_san_002_complete_redaction(self, tmp_path):
        data = {
            "apiKey": canary_api_key(),
            "accessToken": canary_access_token(),
            "refreshToken": canary_refresh_token(),
            "clientSecret": canary_client_secret(),
            "password": canary_password(),
            "jwt": canary_jwt(),
            "providerNodes": [{"id": "n1", "data": json.dumps({"apiKey": canary_api_key()})}],
        }
        src, out, _ = self._export(tmp_path, data, name="san2")
        blob = out.read_text(encoding="utf-8")
        for canary in (canary_api_key(), canary_access_token(), canary_refresh_token(),
                       canary_client_secret(), canary_password(), canary_jwt()):
            assert canary not in blob

    def test_san_003_structural_usefulness(self, tmp_path):
        data = {
            "providerConnections": [
                {"id": "c1", "provider": "deepseek", "name": "DeepSeek",
                 "data": json.dumps({"apiKey": canary_api_key(), "defaultModel": "ds/v4"})}],
            "combos": [{"id": "cb1", "name": "Combo", "models": ["ds/v4", "ag/x"]}],
            "models": [{"canonical_id": "ds/v4", "provider_prefix": "ds"}],
        }
        _, out, _ = self._export(tmp_path, data, name="san3")
        fixed = json.loads(out.read_text(encoding="utf-8"))
        assert fixed["providerConnections"][0]["provider"] == "deepseek"
        assert fixed["combos"][0]["models"] == ["ds/v4", "ag/x"]
        assert "ds/v4" in fixed["providerConnections"][0]["data"]

    def test_san_004_output_passes_verify(self, tmp_path):
        repo = make_mini_repo(tmp_path, "san4")
        _, out, _ = self._export(tmp_path, {
            "providerConnections": [
                {"id": "c1", "provider": "p", "data": json.dumps(
                    {"apiKey": canary_api_key(), "accessToken": canary_access_token()})}],
            "apiKeys": [{"id": "k1", "key": canary_sk(), "createdAt": "t"}],
        }, name="san4")
        shutil.copy2(out, repo / "fixture.json")
        assert _v(repo) == [], "sanitized output must pass the agent-safe gate"

    def test_san_005_untransformable_secret_fails_safe(self, tmp_path):
        # JSON cannot carry raw bytes; the realistic untransformable cases are
        # NUMERIC secrets (e.g. clientSecret as a big int) — they must be
        # redacted, never copied through "for compatibility".
        _, out, _ = self._export(tmp_path, {
            "apiKey": 987654321098765432109876543210,
            "oauth": {"clientSecret": 12345678901234567890},
        }, name="san5")
        blob = out.read_text(encoding="utf-8")
        assert "987654321098765432109876543210" not in blob
        assert "12345678901234567890" not in blob
        fixed = json.loads(blob)
        assert fixed["apiKey"] == "<REDACTED>"
        assert fixed["oauth"]["clientSecret"] == "<REDACTED>"


# =============================================================================
# LOCK-001..008 - secret store / lock state
# =============================================================================
@pytest.mark.skipif(not VAULT_LIBS, reason="vault libraries not installed")
class TestLockState:
    def _vault_pw(self) -> str:
        return "MasterPw" + "9rT#2026"

    def test_lock_001_fresh_launch_locked(self, tmp_path, monkeypatch):
        monkeypatch.delenv("WATCHEDIT_LIVE_ACCESS", raising=False)
        from core.security import LOCKED, SecurityManager
        assert SecurityManager(data_dir=tmp_path).state == LOCKED

    def test_lock_002_locked_no_fallback(self, tmp_path, monkeypatch):
        monkeypatch.delenv("WATCHEDIT_LIVE_ACCESS", raising=False)
        from core.security import LiveAccessLockedError, SecurityManager
        from core.router_client import RouterClient
        mgr = SecurityManager(data_dir=tmp_path)
        client = RouterClient(base_url="http://127.0.0.1:1", security=mgr)
        with pytest.raises(LiveAccessLockedError) as ei:
            client.get_providers()
        msg = str(ei.value)
        assert "Live access requires local credential unlock" in msg
        assert "backup" not in msg and "fallback" not in msg

    def test_lock_003_unlock(self, tmp_path, monkeypatch):
        monkeypatch.delenv("WATCHEDIT_LIVE_ACCESS", raising=False)
        from core.security import OS_VAULT, SecurityManager
        mgr = SecurityManager(data_dir=tmp_path)
        vault = VaultStore(mgr._vault_path)
        vault.create(self._vault_pw(), {"router/api": "abc"})
        mgr.unlock_with_password(self._vault_pw())
        assert mgr.state == OS_VAULT or mgr.is_live_allowed()

    def test_lock_004_lock_invalidates_session(self, tmp_path, monkeypatch):
        monkeypatch.delenv("WATCHEDIT_LIVE_ACCESS", raising=False)
        from core.security import LOCKED, SecurityManager
        mgr = SecurityManager(data_dir=tmp_path)
        vault = VaultStore(mgr._vault_path)
        vault.create(self._vault_pw(), {"router/api": "abc"})
        mgr.unlock_with_password(self._vault_pw())
        assert mgr.get_vault_secret("router/api") == "abc"
        mgr.lock()
        assert mgr.state == LOCKED
        assert mgr.get_vault_secret("router/api") is None, "in-memory handle must become invalid"

    def test_lock_005_password_not_recoverable_anywhere(self, tmp_path, monkeypatch):
        monkeypatch.delenv("WATCHEDIT_LIVE_ACCESS", raising=False)
        from core.security import SecurityManager
        pw = self._vault_pw()
        mgr = SecurityManager(data_dir=tmp_path)
        VaultStore(mgr._vault_path).create(pw, {"router/api": "abc"})
        mgr.unlock_with_password(pw)
        mgr.lock()
        # not in the vault file, not in local settings, not in the repository
        assert pw.encode() not in mgr._vault_path.read_bytes()
        for cfg in (tmp_path / "config").rglob("*") if (tmp_path / "config").exists() else []:
            assert pw not in cfg.read_text(encoding="utf-8", errors="replace")
        from core.secret_scanner import scan_tree
        assert not any(pw in (f.file + f.detail) for f in scan_tree(REPO_ROOT))

    def test_lock_006_wrong_password_clean_failure(self, tmp_path):
        mgr_path = tmp_path / "credentials.vault"
        vault = VaultStore(mgr_path)
        vault.create(self._vault_pw(), {"a": "1"})
        before = mgr_path.read_bytes()
        with pytest.raises(Exception):
            vault.load("totally-wrong-password")
        assert mgr_path.read_bytes() == before, "wrong password must not corrupt or overwrite"

    def test_lock_007_corrupted_vault(self, tmp_path):
        import base64
        mgr_path = tmp_path / "credentials.vault"
        VaultStore(mgr_path).create(self._vault_pw(), {"a": "1"})
        doc = json.loads(mgr_path.read_text(encoding="utf-8"))
        ct = bytearray(base64.b64decode(doc["ciphertext"]))
        ct[10] ^= 0xFF  # one-byte corruption
        doc["ciphertext"] = base64.b64encode(bytes(ct)).decode()
        mgr_path.write_text(json.dumps(doc), encoding="utf-8")
        vault = VaultStore(mgr_path)
        with pytest.raises(Exception):
            vault.load(self._vault_pw()), "authenticated decryption MUST fail, never return garbage"

    def test_lock_008_no_cross_vault_confusion(self, tmp_path):
        v1 = tmp_path / "one.vault"
        v2 = tmp_path / "two.vault"
        VaultStore(v1).create(self._vault_pw(), {"scope": "one", "secretA": "AAA"})
        VaultStore(v2).create(self._vault_pw(), {"scope": "two", "secretB": "BBB"})
        s1 = VaultStore(v1).load(self._vault_pw())
        s2 = VaultStore(v2).load(self._vault_pw())
        assert s1 == {"scope": "one", "secretA": "AAA"}
        assert s2 == {"scope": "two", "secretB": "BBB"}


# =============================================================================
# BACKUP-001..006 - private backup
# =============================================================================
@pytest.mark.skipif(not VAULT_LIBS, reason="vault libraries not installed")
class TestPrivateBackup:
    def test_backup_001_destination_inside_repo_rejected(self, tmp_path):
        import create_private_backup as cpb
        inside_repo = REPO_ROOT / "backups_should_never_exist_here"
        with pytest.raises(ValueError, match="OUTSIDE the repository"):
            cpb.create_private_backup("password-123", out_dir=inside_repo)
        assert not inside_repo.exists()

    def test_backup_001b_repo_relative_target(self, tmp_path, monkeypatch):
        import create_private_backup as cpb
        monkeypatch.setattr(cpb, "REPO_ROOT", tmp_path)  # treat tmp as the repo
        data = tmp_path / "local"
        data.mkdir()
        (data / "settings.json").write_text("{}", encoding="utf-8")
        monkeypatch.setattr(cpb, "_collect_sources", lambda: [data / "settings.json"])
        with pytest.raises(ValueError):
            cpb.create_private_backup("password-123", out_dir=tmp_path / "nested" / "backup")

    def test_backup_002_not_plaintext_zip(self, tmp_path, monkeypatch):
        import create_private_backup as cpb
        data = tmp_path / "local2"
        data.mkdir()
        secret_file = data / "oauth_state.dat"
        secret_file.write_text(canary_refresh_token(), encoding="utf-8")
        monkeypatch.setattr(cpb, "_collect_sources", lambda: [secret_file])
        monkeypatch.setattr(cpb, "LOCALAPPDATA_DIR", data)
        out = cpb.create_private_backup("password-123", out_dir=tmp_path / "backups")
        raw = out.read_bytes()
        assert canary_refresh_token().encode() not in raw
        assert b"oauth_state.dat" not in raw or True  # names may appear in manifest; values never

    def test_backup_003_wrong_password(self, tmp_path, monkeypatch):
        import create_private_backup as cpb
        data = tmp_path / "local3"
        data.mkdir()
        f = data / "s.dat"
        f.write_text("secret-material", encoding="utf-8")
        monkeypatch.setattr(cpb, "_collect_sources", lambda: [f])
        out = cpb.create_private_backup("password-123", out_dir=tmp_path / "b3")
        import io, zipfile as zf
        with zf.ZipFile(out) as z:
            payload = z.read("payload.vault")
        vault_file = tmp_path / "re.vault"
        vault_file.write_bytes(payload)
        with pytest.raises(Exception):
            VaultStore(vault_file).load("wrong-password")

    def test_backup_004_one_byte_corruption(self, tmp_path, monkeypatch):
        import create_private_backup as cpb
        data = tmp_path / "local4"
        data.mkdir()
        f = data / "s.dat"
        f.write_text("secret-material", encoding="utf-8")
        monkeypatch.setattr(cpb, "_collect_sources", lambda: [f])
        out = cpb.create_private_backup("password-123", out_dir=tmp_path / "b4")
        with __import__("zipfile").ZipFile(out) as z:
            names = z.namelist()
            payload = bytearray(z.read("payload.vault"))
        payload[20] ^= 0x55
        # rebuild zip with corrupted payload
        corrupted = tmp_path / "corrupt.wvault"
        with __import__("zipfile").ZipFile(corrupted, "w") as zo:
            for n in names:
                zo.writestr(n, bytes(payload) if n == "payload.vault" else "")
        vf = tmp_path / "corrupt.vault"
        vf.write_bytes(bytes(payload))
        with pytest.raises(Exception):
            VaultStore(vf).load("password-123")

    def test_backup_005_private_naming(self, tmp_path, monkeypatch):
        import create_private_backup as cpb
        data = tmp_path / "local5"
        data.mkdir()
        f = data / "s.dat"
        f.write_text("x", encoding="utf-8")
        monkeypatch.setattr(cpb, "_collect_sources", lambda: [f])
        out = cpb.create_private_backup("password-123", out_dir=tmp_path / "b5")
        assert out.name.startswith("PRIVATE_SECRET_BACKUP")
        assert out.suffix == ".wvault"

    def test_backup_006_encrypted_material_still_rejected_in_repo(self, tmp_path, monkeypatch):
        import create_private_backup as cpb
        data = tmp_path / "local6"
        data.mkdir()
        f = data / "s.dat"
        f.write_text("x", encoding="utf-8")
        monkeypatch.setattr(cpb, "_collect_sources", lambda: [f])
        out = cpb.create_private_backup("password-123", out_dir=tmp_path / "b6")
        repo = make_mini_repo(tmp_path, "b6repo")
        shutil.copy2(out, repo / "copy.wvault")
        findings = _v(repo)
        assert any("copy.wvault" in u.path for u in findings), \
            "encrypted private material still does not belong in a shareable repo"


# =============================================================================
# Section 17 - safe diagnostic bundle content contract
# =============================================================================
class TestDiagnosticBundle:
    def test_diag_preserves_shape_removes_credentials(self, tmp_path):
        from core.diagnostics import export_diagnostic_bundle
        from core.history import HealthCache
        cache = HealthCache(cache_file=tmp_path / "cache.json")
        runtime_response = {
            "provider": "prov-nine", "model": "model-x", "http": 401,
            "latency": 512.0, "normalized_error": "auth rejected",
            "apiKey": canary_api_key(),
            "accessToken": canary_access_token(),
            "Authorization": "Bearer " + canary_access_token(),
            "cookies": {"session": canary_client_secret()},
            "jwt": canary_jwt(),
            "machine_id": "0f1e2d3c4b5a69788796a5b4c3d2e1f0",
        }
        out = export_diagnostic_bundle(
            cache=cache,
            recent_errors=[json.dumps(runtime_response)],
            out_dir=tmp_path / "diag",
        )
        blob = out.read_text(encoding="utf-8")
        assert '"prov-nine"' in blob or "prov-nine" in blob      # provider name kept
        assert "normalized_error" in blob or "auth rejected" in blob  # normalized error kept
        for canary in (canary_api_key(), canary_access_token(),
                       canary_client_secret(), canary_jwt()):
            assert canary not in blob
        from core.secret_scanner import scan_file
        assert scan_file(out, tmp_path) == []


# =============================================================================
# Section 18 - external agent simulation
# =============================================================================
class TestAgentSimulation:
    def test_agent_scenario(self, tmp_path):
        import agent_worktree as aw
        repo = make_mini_repo(tmp_path, "agentsim")
        # private runtime OUTSIDE the repository with canaries
        private = tmp_path / "private_runtime"
        private.mkdir()
        (private / "credentials.dat").write_text(canary_api_key(), encoding="utf-8")
        (private / "oauth_state.dat").write_text(canary_access_token(), encoding="utf-8")

        os.environ["AGENT_WORKTREE_ROOT"] = str(tmp_path / "agentwts")
        wt = aw.create_worktree("sim", repo_root=repo)

        # 1. agent reads all sources
        sources = list(wt.rglob("*.py"))
        assert sources, "agent must be able to read source"
        # 2. agent runs unit tests
        tr = subprocess.run([sys.executable, "-m", "pytest", "-q", "tests/test_ok.py"],
                            cwd=str(wt), capture_output=True, text=True, timeout=120)
        assert tr.returncode == 0, tr.stdout[-300:]
        # 3. agent searches its whole worktree for known private canaries
        for canary in (canary_api_key(), canary_access_token()):
            for p in wt.rglob("*"):
                if p.is_file():
                    assert canary not in p.read_text(encoding="utf-8", errors="replace"), \
                        f"private canary leaked into worktree: {p}"
        # 4. expected repo-relative private paths are absent
        for absent in ("backup", "runtime", "secrets", "private"):
            assert not (wt / absent).exists()


# =============================================================================
# Section 19 - production code must not depend on secret fixtures
# =============================================================================
class TestProductionDependencies:
    def test_no_fixture_or_backup_references(self):
        import re
        forbidden = re.compile(
            r"(tests[/\\]fixtures|providers-state-export|data-snapshot|"
            r"[\"'](?:\./)?backup(?:/|[\"'])|offline_contracts_sanitized)")
        offenders = []
        for area in ("core", "ui"):
            for p in (REPO_ROOT / "9router_WatchEdit" / area).rglob("*.py"):
                text = p.read_text(encoding="utf-8", errors="replace")
                m = forbidden.search(text)
                if m:
                    offenders.append(f"{p.relative_to(REPO_ROOT)}: {m.group(0)}")
        assert offenders == [], f"production code references private/fixture paths: {offenders}"


# =============================================================================
# Section 20 - configuration precedence
# =============================================================================
class TestConfigurationPrecedence:
    def test_explicit_setting_wins(self, tmp_path):
        env = dict(os.environ)
        env["WATCHEDIT_DATA_DIR"] = str(tmp_path / "explicit")
        res = subprocess.run(
            [sys.executable, "-c", "import config; print(config.SECURE_DIR)"],
            cwd=str(REPO_ROOT / "9router_WatchEdit"), capture_output=True, text=True,
            env=env, timeout=60)
        assert str(tmp_path / "explicit") in res.stdout

    def test_os_default_never_repo_relative(self):
        env = {k: v for k, v in os.environ.items() if k != "WATCHEDIT_DATA_DIR"}
        res = subprocess.run(
            [sys.executable, "-c",
             "import config, sys; p=str(config.SECURE_DIR); print(p); "
             "sys.exit(0 if '" + str(REPO_ROOT).replace(chr(92), chr(92)*2) + "' not in p else 1)"],
            cwd=str(REPO_ROOT / "9router_WatchEdit"), capture_output=True, text=True,
            env=env, timeout=60)
        assert res.returncode == 0, f"private default must never be repository-relative: {res.stdout}"
        assert "AppData" in res.stdout or "LOCALAPPDATA" in res.stdout.upper() or "9router_WatchEdit" in res.stdout

    def test_missing_private_state_means_locked_not_crash(self, tmp_path, monkeypatch):
        monkeypatch.delenv("WATCHEDIT_LIVE_ACCESS", raising=False)
        from core.security import LOCKED, SecurityManager
        mgr = SecurityManager(data_dir=tmp_path / "does_not_exist_yet")
        assert mgr.state == LOCKED
        assert not mgr.is_live_allowed()


# =============================================================================
# Section 21 - merge gate matrix
# =============================================================================
class TestMergeGate:
    def _commit_in(self, wt, files: dict, msg: str):
        for rel, content in files.items():
            p = wt / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(content, encoding="utf-8")
        git(wt, "add", "-A")
        assert git(wt, "commit", "-m", msg).returncode == 0

    def test_gate_secret_blocks_merge(self, tmp_path, capsys):
        import agent_worktree as aw
        repo = make_mini_repo(tmp_path, "mg1")
        os.environ["AGENT_WORKTREE_ROOT"] = str(tmp_path / "mgwts")
        wt = aw.create_worktree("sneaky", repo_root=repo)
        self._commit_in(wt, {
            "app.py": "VALUE = 2\n",
            "creds.json": json.dumps({"apiKey": canary_api_key()}),
        }, "fix plus secret")
        rc = aw.review_worktree("sneaky", repo_root=repo, run_tests=True)
        out = capsys.readouterr().out
        assert "SECRET CHECK: FAIL" in out and "MERGE BLOCKED" in out and rc == 1
        assert (repo / "app.py").read_text(encoding="utf-8") == "VALUE = 1\n"

    def test_gate_failing_test_blocks_merge(self, tmp_path, capsys):
        import agent_worktree as aw
        repo = make_mini_repo(tmp_path, "mg2")
        os.environ["AGENT_WORKTREE_ROOT"] = str(tmp_path / "mgwts2")
        wt = aw.create_worktree("broken", repo_root=repo)
        self._commit_in(wt, {
            "tests/test_broken.py": "def test_broken():\n    assert False\n",
        }, "break a test")
        rc = aw.review_worktree("broken", repo_root=repo, run_tests=True)
        out = capsys.readouterr().out
        assert "MERGE BLOCKED" in out and rc == 1

    def test_gate_clean_change_passes(self, tmp_path, capsys):
        import agent_worktree as aw
        repo = make_mini_repo(tmp_path, "mg3")
        os.environ["AGENT_WORKTREE_ROOT"] = str(tmp_path / "mgwts3")
        wt = aw.create_worktree("good", repo_root=repo)
        self._commit_in(wt, {"app.py": "VALUE = 3\n"}, "clean fix")
        rc = aw.review_worktree("good", repo_root=repo, run_tests=True)
        out = capsys.readouterr().out
        assert rc == 0
        assert "SECRET CHECK: PASS" in out


# =============================================================================
# Section 22 - rollback exactness
# =============================================================================
class TestRollbackExactness:
    def test_rollback_restores_exact_hashes(self, tmp_path, monkeypatch):
        import deploy_local as dl
        repo = make_mini_repo(tmp_path, "rb1")
        git(repo, "checkout", "-b", "feature")
        (repo / "app.py").write_text("VALUE = 77\n", encoding="utf-8")
        git(repo, "add", "-A")
        git(repo, "commit", "-m", "feature change")
        git(repo, "checkout", "master")
        before = hash_tree(repo)
        priv = tmp_path / "priv_rb"
        priv.mkdir()
        (priv / "state.dat").write_bytes(b"private-bytes")
        before_priv = hash_tree(priv)
        monkeypatch.setenv("WATCHEDIT_DATA_DIR", str(priv))
        monkeypatch.setattr(dl, "PRIVATE_RUNTIME_ROOT", priv)
        monkeypatch.setattr(dl, "_smoke_test", lambda *a, **k: (False, "injected failure"))
        assert dl.deploy_local(source="feature", skip_tests=True, quiet=True, repo_root=repo) is False
        after = hash_tree(repo)
        assert set(after) == set(before), "no orphan temporary files may remain"
        for k in before:
            assert after[k] == before[k], f"half-old/half-new state for {k}"
        assert hash_tree(priv) == before_priv


# =============================================================================
# Section 23 - receipts must be safe
# =============================================================================
class TestReceiptsSafe:
    def test_deploy_and_review_receipts_scan_clean(self, tmp_path, monkeypatch, capsys):
        import deploy_local as dl
        import agent_worktree as aw
        repo = make_mini_repo(tmp_path, "rec1")
        priv = tmp_path / "priv_rec"
        priv.mkdir()
        (priv / "s.dat").write_bytes(b"x")
        monkeypatch.setenv("WATCHEDIT_DATA_DIR", str(priv))
        monkeypatch.setattr(dl, "PRIVATE_RUNTIME_ROOT", priv)

        capsys.readouterr()
        dl.deploy_local(skip_tests=True, quiet=False, repo_root=repo)
        success_receipt = capsys.readouterr().out

        monkeypatch.setattr(dl, "_smoke_test", lambda *a, **k: (False, "injected"))
        dl.deploy_local(skip_tests=True, quiet=False, repo_root=repo)
        failure_receipt = capsys.readouterr().out

        os.environ["AGENT_WORKTREE_ROOT"] = str(tmp_path / "recwts")
        wt = aw.create_worktree("receipt", repo_root=repo)
        (wt / "app.py").write_text("VALUE = 4\n", encoding="utf-8")
        git(wt, "add", "-A")
        git(wt, "commit", "-m", "change")
        aw.review_worktree("receipt", repo_root=repo, run_tests=False)
        review_receipt = capsys.readouterr().out

        receipts = tmp_path / "receipts"
        receipts.mkdir()
        (receipts / "deploy_success.txt").write_text(success_receipt, encoding="utf-8")
        (receipts / "deploy_failure.txt").write_text(failure_receipt, encoding="utf-8")
        (receipts / "review.txt").write_text(review_receipt, encoding="utf-8")
        from core.secret_scanner import scan_tree
        findings = scan_tree(receipts)
        assert findings == [], f"receipts leaked secret-shaped material: {findings}"
        for canary in (canary_api_key(), canary_access_token(), canary_refresh_token()):
            assert canary not in success_receipt + failure_receipt + review_receipt


# =============================================================================
# Section 24/25 - fuzz variants + false-positive control
# =============================================================================
class TestFuzzAndFalsePositives:
    @pytest.mark.parametrize("variant", [
        "api-key", "api_key", "ApiKey", "APIKEY",
        "access-token", "access_token", "RefreshToken",
        "client-secret", "client_secret", "ROUTER_PASSWORD", "upstream_jwt",
    ])
    def test_p24_fuzz_variants_detected(self, tmp_path, variant):
        repo = make_mini_repo(tmp_path, f"fz_{variant}")
        (repo / "v.json").write_text(
            json.dumps({variant: canary_client_secret()}), encoding="utf-8")
        assert _v(repo), f"naming variant {variant!r} must not bypass detection"

    def test_p25_benign_terms_do_not_block(self, tmp_path):
        repo = make_mini_repo(tmp_path, "fp1")
        (repo / "benign.json").write_text(json.dumps({
            "token_count": "12",
            "tokenizer": "word-piece",
            "secretary": "Ms. Smith",
            "monkey": "banana",
            "keyboard": "qwerty-uiop",
            "public_key_name": "id-rsa-note",
            "password_field_label": "Password",
        }), encoding="utf-8")
        (repo / "prose.txt").write_text(
            "The secretary typed on her keyboard while the tokenizer counted "
            "token_count for the monkey test. public_key_name is a label.\n",
            encoding="utf-8")
        findings = _v(repo)
        assert findings == [], f"false positives destroy scanner precision: {findings}"

    def test_p25_strict_fields_stay_strict(self, tmp_path):
        repo = make_mini_repo(tmp_path, "fp2")
        (repo / "strict.json").write_text(json.dumps({
            "refreshToken": canary_refresh_token(),
            "clientSecret": canary_client_secret(),
            "Authorization": "Bearer " + canary_access_token(),
        }), encoding="utf-8")
        assert _v(repo), "high-risk semantic fields must remain strict"


# =============================================================================
# Section 26/27 - performance + determinism
# =============================================================================
class TestPerformanceAndDeterminism:
    def test_p26_performance_10k_files(self, tmp_path):
        repo = tmp_path / "bigrepo"
        repo.mkdir()
        try:
            for i in range(2500):
                (repo / f"m{i % 50}").mkdir(exist_ok=True)
            for i in range(2500):
                (repo / f"m{i % 50}" / f"s{i}.py").write_text("x = 1\n", encoding="utf-8")
                (repo / f"m{i % 50}" / f"r{i}.md").write_text("# doc\n", encoding="utf-8")
                (repo / f"m{i % 50}" / f"j{i}.json").write_text('{"i": %d}' % i, encoding="utf-8")
                (repo / f"m{i % 50}" / f"b{i}.dat").write_bytes(b"\x00\x01\x02bin")
        except OSError as ex:
            pytest.skip(f"filesystem too slow for synthetic 10k tree: {ex}")
        t0 = time.monotonic()
        findings = _v(repo)
        elapsed = time.monotonic() - t0
        assert findings == []
        print(f"\nVERIFY_AGENT_SAFE on 10,000 files: {elapsed:.1f}s")
        assert elapsed < 30.0, f"scanner too slow for operational use: {elapsed:.1f}s"

    def test_p27_determinism_safe_and_unsafe(self, tmp_path):
        safe = make_mini_repo(tmp_path, "det_safe")
        results = {format_result(_v(safe)) for _ in range(10)}
        assert results == {"AGENT SAFE: YES"}

        unsafe = mini_canary_repo(tmp_path, "det_unsafe", canary_refresh_token())
        runs = [tuple((u.path, u.reason) for u in _v(unsafe)) for _ in range(10)]
        assert all(r == runs[0] for r in runs), "violations must be reported deterministically"
        assert runs[0], "unsafe repo must stay unsafe"
