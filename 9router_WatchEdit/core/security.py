"""
9router_WatchEdit - Runtime Security Manager
Implements the two operating modes from the threat boundary model:

  DEVELOPMENT / EXTERNAL AGENT MODE (default):  SECRETS LOCKED
      - UI, presets, cached model lists, unit tests, sanitized fixtures work
      - live authenticated provider operations are refused

  LOCAL TRUSTED RUNTIME MODE:  SECRETS OS VAULT / UNLOCKED
      - explicitly unlocked by the user (OS-backed grant or vault password)
      - live 9Router operations available until lock/exit

WatchEdit itself needs no provider secrets in either mode: probing and
discovery are brokered by the local 9Router (which owns the credentials).
The unlock states only authorize WatchEdit to talk to the live local router.
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Callable, Dict, List, Optional

from config import (
    LIVE_ACCESS_ENV,
    LOCAL_SETTINGS_FILE,
    PRIVATE_STORAGE_AVAILABLE,
    SECURE_DIR,
)
from core.secret_store import DPAPIFileStore, VaultStore, dpapi_protect, dpapi_unprotect

LOCKED = "LOCKED"
OS_VAULT = "OS_VAULT"
UNLOCKED = "UNLOCKED"

GRANT_FILE = "live-access.grant"
VAULT_FILE = "credentials.vault"


class LiveAccessLockedError(RuntimeError):
    """Raised by RouterClient when live provider access is attempted while locked."""

    def __init__(self, operation: str):
        self.operation = operation
        super().__init__(
            f"Live access requires local credential unlock. (blocked operation: {operation})"
        )


class SecurityManager:
    """Owns the live-access lock state and the local unlock backends."""

    def __init__(self, data_dir: Optional[Path] = None):
        self.data_dir = Path(data_dir) if data_dir else SECURE_DIR
        # GATE 19 + CORE-001: unavailable private storage degrades to LOCKED,
        # never crashes, and NEVER invents a CWD-relative secure directory.
        if data_dir is None and not PRIVATE_STORAGE_AVAILABLE:
            self.storage_available = False
        elif not self.data_dir.is_absolute():
            self.storage_available = False
        else:
            try:
                self.data_dir.mkdir(parents=True, exist_ok=True)
                self.storage_available = True
            except OSError:
                self.storage_available = False
        self._grant_path = self.data_dir / GRANT_FILE
        self._vault_path = self.data_dir / VAULT_FILE
        self._state = LOCKED
        self._vault_secrets: Dict[str, str] = {}
        self._listeners: List[Callable[[str], None]] = []

    # ------------------------------------------------------------- observers
    @property
    def state(self) -> str:
        return self._state

    def is_live_allowed(self) -> bool:
        if os.environ.get(LIVE_ACCESS_ENV, "") == "1":
            return True
        return self._state in (OS_VAULT, UNLOCKED)

    def is_locked(self) -> bool:
        return not self.is_live_allowed()

    def require_live(self, operation: str) -> None:
        if not self.is_live_allowed():
            raise LiveAccessLockedError(operation)

    def on_state_changed(self, cb: Callable[[str], None]) -> None:
        self._listeners.append(cb)

    def _set_state(self, new_state: str) -> None:
        if new_state != self._state:
            self._state = new_state
            for cb in list(self._listeners):
                try:
                    cb(new_state)
                except Exception:
                    pass

    # ------------------------------------------------------- local settings
    def _read_local_settings(self) -> Dict:
        path = LOCAL_SETTINGS_FILE
        if not path.is_absolute() or not path.exists():
            return {}
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return {}

    def _write_local_settings(self, data: Dict) -> None:
        path = LOCAL_SETTINGS_FILE
        if not path.is_absolute():
            # CORE-001: no valid private root -> locked/read-only degradation,
            # never a CWD-relative settings write.
            return
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            existing = {}
            if path.exists():
                try:
                    existing = json.loads(path.read_text(encoding="utf-8"))
                except Exception:
                    existing = {}
            existing.update(data)
            path.write_text(json.dumps(existing, indent=2), encoding="utf-8")
        except OSError:
            # Storage degraded mid-session: settings stay unsaved, state stays LOCKED.
            pass

    @property
    def trusted_os_unlock_enabled(self) -> bool:
        return bool(self._read_local_settings().get("trusted_os_unlock", False))

    def set_trusted_os_unlock(self, enabled: bool) -> None:
        self._write_local_settings({"trusted_os_unlock": bool(enabled)})
        if enabled:
            self.try_silent_unlock()

    # ------------------------------------------------------------- unlocking
    def grant_exists(self) -> bool:
        return self._grant_path.exists()

    def vault_exists(self) -> bool:
        return self._vault_path.exists()

    def unlock_os(self) -> None:
        """OS-backed unlock: validates/creates a DPAPI grant blob for this user.

        The grant proves the operator approved live access on THIS machine for
        THIS Windows user; the blob is undecryptable anywhere else.
        """
        if not self.storage_available:
            # CORE-001: no private root -> no grant storage -> stay LOCKED.
            raise PermissionError(
                "Private storage unavailable; live access stays LOCKED (no secure root)"
            )
        if self._grant_path.exists():
            raw = dpapi_unprotect(self._grant_path.read_bytes())
            doc = json.loads(raw.decode("utf-8"))
            if doc.get("purpose") != "live-access-grant":
                raise PermissionError("Invalid live-access grant")
        else:
            doc = {
                "purpose": "live-access-grant",
                "created": time.strftime("%Y-%m-%dT%H:%M:%S"),
                "nonce": os.urandom(16).hex(),
            }
            self._grant_path.write_bytes(dpapi_protect(json.dumps(doc).encode("utf-8")))
        self._set_state(OS_VAULT)

    def try_silent_unlock(self) -> bool:
        """Auto-unlock used only when trusted_os_unlock is enabled locally."""
        if not self.trusted_os_unlock_enabled:
            return False
        try:
            if self.grant_exists():
                self.unlock_os()
                return True
            if self.vault_exists():
                return False  # vault always requires the master password
        except Exception:
            return False
        return False

    def unlock_with_password(self, password: str) -> None:
        """Vault unlock: wrong password raises VaultError and stays locked."""
        vault = VaultStore(self._vault_path)
        self._vault_secrets = vault.load(password)
        self._set_state(UNLOCKED)

    def get_vault_secret(self, name: str) -> Optional[str]:
        return self._vault_secrets.get(name)

    @property
    def vault_secret_names(self) -> List[str]:
        return sorted(self._vault_secrets.keys())

    # --------------------------------------------------------------- locking
    def lock(self) -> None:
        """Immediate lock: live provider operations stop working right away."""
        self._vault_secrets = {}
        self._set_state(LOCKED)

    def revoke_os_grant(self) -> None:
        self.lock()
        if self.storage_available and self._grant_path.exists():
            self._grant_path.unlink()


_DEFAULT_MANAGER: Optional[SecurityManager] = None


def get_default_security() -> SecurityManager:
    """Process-wide default SecurityManager (LOCKED until explicitly unlocked)."""
    global _DEFAULT_MANAGER
    if _DEFAULT_MANAGER is None:
        _DEFAULT_MANAGER = SecurityManager()
        _DEFAULT_MANAGER.try_silent_unlock()
    return _DEFAULT_MANAGER
