"""
W2-003 / SRC-001:R0008 regressions — private backup must be byte-preserving.

Round-trips arbitrary binary payloads (all byte values, DPAPI-shaped blobs,
empty files, nested secure layout) through create_private_backup and verifies
exact byte/size/SHA-256 equality on extract. Existing encryption/wrong-password
behavior keeps passing.
"""
import hashlib
import os
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
TOOLS = REPO_ROOT / "tools"
sys.path.insert(0, str(TOOLS))

VAULT_LIBS = True
try:
    import argon2.low_level  # noqa: F401
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM  # noqa: F401
except Exception:
    VAULT_LIBS = False

pytestmark = pytest.mark.skipif(not VAULT_LIBS, reason="vault libraries not installed")


def _isolate_sources(monkeypatch, cpb, data_dir: Path) -> None:
    for sub in ("config", "secure", "nested"):
        (data_dir / sub).mkdir(parents=True, exist_ok=True)
    (data_dir / "config" / "settings.json").write_text('{"trusted_os_unlock": false}', encoding="utf-8")
    monkeypatch.setattr(cpb, "HEALTH_CACHE_FILE", data_dir / "health_cache.json")
    monkeypatch.setattr(cpb, "PRESETS_FILE", data_dir / "presets.json")
    monkeypatch.setattr(cpb, "SETTINGS_FILE", data_dir / "settings.json")
    monkeypatch.setattr(cpb, "SECURE_DIR", data_dir / "secure")
    monkeypatch.setattr(cpb, "LOCALAPPDATA_DIR", data_dir)
    monkeypatch.setattr(cpb, "PRIVATE_BACKUP_DIR", data_dir / "backups" / "private")


def _make_binary_sources(data_dir: Path) -> dict:
    """Returns entry name -> expected bytes. All sources live under secure/ so
    _collect_sources() picks them up (incl. one nested deep layout)."""
    all_bytes = bytes(range(256))
    dpapi_shaped = bytes.fromhex("01000000d08c9ddf0115d1118c7a00c04fc297eb") + os.urandom(48)
    empty = b""
    random_blob = os.urandom(4096)
    nested = data_dir / "secure" / "nested" / "deep" / "blob.bin"
    nested.parent.mkdir(parents=True, exist_ok=True)
    nested.write_bytes(random_blob)
    (data_dir / "secure" / "grant.blob").write_bytes(dpapi_shaped)
    (data_dir / "secure" / "all_bytes.bin").write_bytes(all_bytes)
    (data_dir / "secure" / "empty.bin").write_bytes(empty)
    return {
        "grant.blob": dpapi_shaped,
        "all_bytes.bin": all_bytes,
        "empty.bin": empty,
        "blob.bin": random_blob,
    }


def test_round_trip_binary_exact(tmp_path, monkeypatch):
    import create_private_backup as cpb
    data_dir = tmp_path / "localdata"
    _isolate_sources(monkeypatch, cpb, data_dir)
    expected = _make_binary_sources(data_dir)

    out = cpb.create_private_backup("master-pass-123", out_dir=tmp_path / "private_backups")
    assert out.exists() and out.suffix == ".wvault"

    restored_dir = tmp_path / "restored"
    restored = cpb.extract_private_backup(out, "master-pass-123", restored_dir)
    by_name = {p.name: p for p in restored}

    for name, original in expected.items():
        assert name in by_name, f"{name} missing from restore: {sorted(by_name)}"
        raw = by_name[name].read_bytes()
        assert raw == original, f"{name} not byte-exact"
        assert len(raw) == len(original)
        assert hashlib.sha256(raw).hexdigest() == hashlib.sha256(original).hexdigest()

    # nested secure layout round-trips with its relative path preserved
    nested_restored = restored_dir / "secure" / "nested" / "deep" / "blob.bin"
    assert nested_restored.exists()
    assert nested_restored.read_bytes() == expected["blob.bin"]
    assert "blob.bin" in by_name


def test_extract_fails_closed_on_tamper(tmp_path, monkeypatch):
    import zipfile
    import create_private_backup as cpb
    data_dir = tmp_path / "localdata"
    _isolate_sources(monkeypatch, cpb, data_dir)
    _make_binary_sources(data_dir)

    out = cpb.create_private_backup("master-pass-123", out_dir=tmp_path / "private_backups")

    # Tamper: corrupt payload bytes inside the zip container
    tampered = tmp_path / "tampered.wvault"
    with zipfile.ZipFile(out) as zin, zipfile.ZipFile(tampered, "w") as zout:
        for item in zin.namelist():
            data = zin.read(item)
            if item == "payload.vault":
                data = data[:-4] + b"XXXX"
            zout.writestr(item, data)

    with pytest.raises(Exception):
        cpb.extract_private_backup(tampered, "master-pass-123", tmp_path / "restored2")


def test_extract_rejects_repo_destination(tmp_path, monkeypatch):
    import create_private_backup as cpb
    data_dir = tmp_path / "localdata"
    _isolate_sources(monkeypatch, cpb, data_dir)
    _make_binary_sources(data_dir)
    out = cpb.create_private_backup("master-pass-123", out_dir=tmp_path / "private_backups")
    with pytest.raises(ValueError):
        cpb.extract_private_backup(out, "master-pass-123", REPO_ROOT)


def test_text_sources_still_round_trip(tmp_path, monkeypatch):
    import create_private_backup as cpb
    data_dir = tmp_path / "localdata"
    _isolate_sources(monkeypatch, cpb, data_dir)
    _make_binary_sources(data_dir)

    out = cpb.create_private_backup("master-pass-123", out_dir=tmp_path / "private_backups")
    restored_dir = tmp_path / "restored_text"
    cpb.extract_private_backup(out, "master-pass-123", restored_dir)
    restored_settings = restored_dir / "config" / "settings.json"
    assert restored_settings.exists()
    assert restored_settings.read_text(encoding="utf-8") == '{"trusted_os_unlock": false}'


def test_wrong_password_still_rejected(tmp_path, monkeypatch):
    import create_private_backup as cpb
    data_dir = tmp_path / "localdata"
    _isolate_sources(monkeypatch, cpb, data_dir)
    _make_binary_sources(data_dir)

    out = cpb.create_private_backup("master-pass-123", out_dir=tmp_path / "private_backups")
    with pytest.raises(Exception):
        cpb.extract_private_backup(out, "wrong-password", tmp_path / "restored3")
