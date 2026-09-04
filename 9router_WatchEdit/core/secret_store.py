"""
9router_WatchEdit - Secret Store Abstraction
OS-backed secret storage for the rare case WatchEdit must own a secret itself.

Backends (preferred order, task section 5):
1. WindowsCredentialStore  - Windows Credential Manager (via ctypes, no extra deps)
2. DPAPIFileStore          - Windows DPAPI CurrentUser scope, blob files under
                             %LOCALAPPDATA%\\9router_WatchEdit\\secure\\
3. VaultStore              - OPTIONAL password-locked vault (Argon2id + AES-256-GCM
                             via the maintained `cryptography` + `argon2-cffi`
                             libraries). File lives OUTSIDE the repository.

No custom cryptography anywhere: DPAPI and Credential Manager are OS services;
the vault uses vetted constructions only. If the crypto libraries are missing,
the vault reports unavailable and callers fall back to OS-backed stores.
"""
from __future__ import annotations

import base64
import ctypes
import json
import os
import sys
import time
from ctypes import wintypes
from pathlib import Path
from typing import Dict, Optional

from config import SECURE_DIR

IS_WINDOWS = sys.platform == "win32"


class SecretStoreError(RuntimeError):
    pass


# ---------------------------------------------------------------------------
# ctypes: Windows Credential Manager
# ---------------------------------------------------------------------------
_CRED_TYPE_GENERIC = 1
_CRED_PERSIST_LOCAL_MACHINE = 2


class _CREDENTIALW(ctypes.Structure):
    _fields_ = [
        ("Flags", wintypes.DWORD),
        ("Type", wintypes.DWORD),
        ("TargetName", wintypes.LPWSTR),
        ("Comment", wintypes.LPWSTR),
        ("LastWritten", wintypes.FILETIME),
        ("CredentialBlobSize", wintypes.DWORD),
        ("CredentialBlob", ctypes.POINTER(ctypes.c_byte)),
        ("Persist", wintypes.DWORD),
        ("AttributeCount", wintypes.DWORD),
        ("Attributes", ctypes.c_void_p),
        ("TargetAlias", wintypes.LPWSTR),
        ("UserName", wintypes.LPWSTR),
    ]


def _target_name(name: str) -> str:
    return f"9router_WatchEdit/{name}"


class WindowsCredentialStore:
    """Windows Credential Manager backend (generic credentials, user-scoped)."""

    def __init__(self):
        if not IS_WINDOWS:
            raise SecretStoreError("WindowsCredentialStore requires Windows")
        self._advapi32 = ctypes.windll.advapi32

    def set_secret(self, name: str, value: str) -> None:
        blob = value.encode("utf-8")
        arr = (ctypes.c_byte * len(blob))(*blob)
        cred = _CREDENTIALW()
        cred.Flags = 0
        cred.Type = _CRED_TYPE_GENERIC
        cred.TargetName = _target_name(name)
        cred.Comment = None
        cred.CredentialBlobSize = len(blob)
        cred.CredentialBlob = ctypes.cast(arr, ctypes.POINTER(ctypes.c_byte))
        cred.Persist = _CRED_PERSIST_LOCAL_MACHINE
        if not self._advapi32.CredWriteW(ctypes.byref(cred), 0):
            raise SecretStoreError(f"CredWriteW failed (name={name!r})")

    def get_secret(self, name: str) -> Optional[str]:
        ptr = ctypes.POINTER(_CREDENTIALW)()
        if not self._advapi32.CredReadW(_target_name(name), _CRED_TYPE_GENERIC, 0, ctypes.byref(ptr)):
            return None
        try:
            c = ptr.contents
            if not c.CredentialBlobSize:
                return ""
            raw = ctypes.string_at(c.CredentialBlob, c.CredentialBlobSize)
            return raw.decode("utf-8", "replace")
        finally:
            ctypes.windll.kernel32.LocalFree(ptr)  # type: ignore[arg-type]

    def delete_secret(self, name: str) -> bool:
        return bool(self._advapi32.CredDeleteW(_target_name(name), _CRED_TYPE_GENERIC, 0))

    def has_secret(self, name: str) -> bool:
        return self.get_secret(name) is not None


# ---------------------------------------------------------------------------
# ctypes: Windows DPAPI (CurrentUser scope)
# ---------------------------------------------------------------------------
class _DATA_BLOB(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]


def dpapi_protect(data: bytes, entropy: bytes = b"") -> bytes:
    if not IS_WINDOWS:
        raise SecretStoreError("DPAPI requires Windows")
    buf = ctypes.create_string_buffer(data, len(data))
    blob_in = _DATA_BLOB(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))
    ent = ctypes.create_string_buffer(entropy, len(entropy)) if entropy else None
    blob_ent = _DATA_BLOB(len(entropy), ctypes.cast(ent, ctypes.POINTER(ctypes.c_char))) if ent else _DATA_BLOB()
    blob_out = _DATA_BLOB()
    ok = ctypes.windll.crypt32.CryptProtectData(
        ctypes.byref(blob_in), None,
        ctypes.byref(blob_ent) if ent else None,
        None, None, 0, ctypes.byref(blob_out),
    )
    if not ok:
        raise SecretStoreError("CryptProtectData failed")
    try:
        return ctypes.string_at(blob_out.pbData, blob_out.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(blob_out.pbData)  # type: ignore[arg-type]


def dpapi_unprotect(data: bytes, entropy: bytes = b"") -> bytes:
    if not IS_WINDOWS:
        raise SecretStoreError("DPAPI requires Windows")
    buf = ctypes.create_string_buffer(data, len(data))
    blob_in = _DATA_BLOB(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))
    ent = ctypes.create_string_buffer(entropy, len(entropy)) if entropy else None
    blob_ent = _DATA_BLOB(len(entropy), ctypes.cast(ent, ctypes.POINTER(ctypes.c_char))) if ent else _DATA_BLOB()
    blob_out = _DATA_BLOB()
    ok = ctypes.windll.crypt32.CryptUnprotectData(
        ctypes.byref(blob_in), None,
        ctypes.byref(blob_ent) if ent else None,
        None, None, 0, ctypes.byref(blob_out),
    )
    if not ok:
        raise SecretStoreError("CryptUnprotectData failed (wrong user or corrupted blob)")
    try:
        return ctypes.string_at(blob_out.pbData, blob_out.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(blob_out.pbData)  # type: ignore[arg-type]


def restrict_to_current_user(path: Path) -> None:
    """Best-effort NTFS ACL: remove inheritance, grant only the current user.
    Directory grants carry (OI)(CI) inheritance flags; file grants must not."""
    if not IS_WINDOWS:
        return
    try:
        import subprocess
        user = os.environ.get("USERNAME", "")
        if not user:
            return
        grant = f"{user}:(OI)(CI)F" if Path(path).is_dir() else f"{user}:F"
        subprocess.run(
            ["icacls", str(path), "/inheritance:r", "/grant:r", grant],
            capture_output=True, check=False, timeout=15,
        )
    except Exception:
        pass  # ACL hardening is defense-in-depth; DPAPI/vault crypto remains primary


class DPAPIFileStore:
    """Per-secret DPAPI-protected blob files under the local secure directory.

    Cryptography is performed by Windows (CurrentUser scope): a blob copied to
    another machine or user cannot be decrypted. No passwords, no custom crypto.
    """

    def __init__(self, secure_dir: Path = SECURE_DIR):
        self.secure_dir = Path(secure_dir)
        self.secure_dir.mkdir(parents=True, exist_ok=True)
        restrict_to_current_user(self.secure_dir)

    def _path(self, name: str) -> Path:
        safe = "".join(ch for ch in name if ch.isalnum() or ch in "-_")
        return self.secure_dir / f"{safe}.dpapi"

    def set_secret(self, name: str, value: str) -> None:
        blob = dpapi_protect(value.encode("utf-8"))
        self._path(name).write_bytes(blob)

    def get_secret(self, name: str) -> Optional[str]:
        p = self._path(name)
        if not p.exists():
            return None
        return dpapi_unprotect(p.read_bytes()).decode("utf-8", "replace")

    def delete_secret(self, name: str) -> bool:
        p = self._path(name)
        if p.exists():
            p.unlink()
            return True
        return False

    def has_secret(self, name: str) -> bool:
        return self._path(name).exists()


# ---------------------------------------------------------------------------
# Optional password-locked vault: Argon2id + AES-256-GCM
# ---------------------------------------------------------------------------
def _vault_libs_available() -> bool:
    try:
        from argon2.low_level import hash_secret_raw  # noqa: F401
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM  # noqa: F401
        return True
    except Exception:
        return False


class VaultError(SecretStoreError):
    pass


class VaultStore:
    """Password-locked vault: master password -> Argon2id -> AES-256-GCM.

    File format (JSON, stored OUTSIDE the repository):
        { "v": 1, "kdf": "argon2id", "salt": b64, "params": {...},
          "nonce": b64, "ciphertext": b64, "created": iso }

    Properties: random salt, random nonce per save, authenticated encryption,
    no plaintext temp files, password never persisted. Key material is held in
    a bytearray and overwritten on lock() (best effort under CPython).
    """

    VERSION = 1

    def __init__(self, vault_file: Path):
        self.vault_file = Path(vault_file)
        if not _vault_libs_available():
            raise VaultError(
                "Vault requires the 'cryptography' and 'argon2-cffi' packages "
                "(pip install cryptography argon2-cffi). "
                "Alternatively use the OS-backed secret store."
            )
        from argon2.low_level import Type
        self._argon_type = Type

    # -- construction helpers -------------------------------------------------
    @staticmethod
    def available() -> bool:
        return _vault_libs_available()

    # -- crypto core ----------------------------------------------------------
    def _derive_key(self, password: str, salt: bytes, params: Dict) -> bytearray:
        from argon2.low_level import hash_secret_raw
        key = hash_secret_raw(
            secret=password.encode("utf-8"),
            salt=salt,
            time_cost=int(params.get("time_cost", 3)),
            memory_cost=int(params.get("memory_cost", 65536)),
            parallelism=int(params.get("parallelism", 2)),
            hash_len=32,
            type=self._argon_type.ID,
        )
        return bytearray(key)

    def create(self, password: str, secrets: Optional[Dict[str, str]] = None) -> None:
        """Creates a NEW vault (refuses to overwrite an existing one)."""
        if self.vault_file.exists():
            raise VaultError("Refusing to overwrite existing vault")
        self._write(password, dict(secrets or {}), salt=None)

    def _write(self, password: str, secrets: Dict[str, str], salt: Optional[bytes]) -> None:
        import secrets as _secrets_mod
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        if salt is None:
            salt = os.urandom(16)
        params = {"time_cost": 3, "memory_cost": 65536, "parallelism": 2}
        key = self._derive_key(password, salt, params)
        try:
            nonce = os.urandom(12)
            plaintext = json.dumps(secrets, ensure_ascii=False).encode("utf-8")
            aad = json.dumps({"v": self.VERSION, "kdf": "argon2id"}, sort_keys=True).encode()
            ct = AESGCM(bytes(key)).encrypt(nonce, plaintext, aad)
            doc = {
                "v": self.VERSION,
                "kdf": "argon2id",
                "salt": base64.b64encode(salt).decode(),
                "params": params,
                "nonce": base64.b64encode(nonce).decode(),
                "ciphertext": base64.b64encode(ct).decode(),
                "created": time.strftime("%Y-%m-%dT%H:%M:%S"),
            }
            tmp = self.vault_file.with_suffix(".tmp")
            tmp.write_text(json.dumps(doc, indent=2), encoding="utf-8")
            tmp.replace(self.vault_file)
            restrict_to_current_user(self.vault_file)
        finally:
            self._zeroize(key)

    def load(self, password: str) -> Dict[str, str]:
        """Authenticates the master password; returns the decrypted secrets."""
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        if not self.vault_file.exists():
            raise VaultError("Vault file does not exist")
        doc = json.loads(self.vault_file.read_text(encoding="utf-8"))
        if doc.get("v") != self.VERSION or doc.get("kdf") != "argon2id":
            raise VaultError("Unsupported vault format")
        salt = base64.b64decode(doc["salt"])
        nonce = base64.b64decode(doc["nonce"])
        ct = base64.b64decode(doc["ciphertext"])
        key = self._derive_key(password, salt, doc.get("params", {}))
        try:
            aad = json.dumps({"v": self.VERSION, "kdf": "argon2id"}, sort_keys=True).encode()
            plaintext = AESGCM(bytes(key)).decrypt(nonce, ct, aad)
            return dict(json.loads(plaintext.decode("utf-8")))
        except Exception:
            raise VaultError("Vault unlock failed: wrong password or corrupted vault")
        finally:
            self._zeroize(key)

    def save(self, password: str, secrets: Dict[str, str]) -> None:
        if not self.vault_file.exists():
            raise VaultError("Vault does not exist; create it first")
        doc = json.loads(self.vault_file.read_text(encoding="utf-8"))
        salt = base64.b64decode(doc["salt"])
        self._write(password, dict(secrets), salt=salt)

    @staticmethod
    def _zeroize(key: bytearray) -> None:
        for i in range(len(key)):
            key[i] = 0
