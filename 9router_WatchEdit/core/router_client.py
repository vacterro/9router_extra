"""
9router_WatchEdit - 9Router REST API & SQLite Fallback Client
Handles machineId-based CLI token generation, provider discovery, model pinging,
and safe combo updates.
"""
import hashlib
import json
import sqlite3
import shutil
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple, Any
import httpx

from config import (
    DEFAULT_ROUTER_BASE_URL,
    ROUTER_DB_PATH,
    MACHINE_ID_FILE,
    CLI_SECRET_FILE,
    CLI_TOKEN_SALT,
    CLI_TOKEN_HEADER,
    BACKUP_DIR,
    PROBE_MAX_TOKENS,
    PROBE_PROMPT,
    redact_secrets,
)
from core.security import LiveAccessLockedError, SecurityManager, get_default_security

class RouterClient:
    def __init__(
        self,
        base_url: str = DEFAULT_ROUTER_BASE_URL,
        db_path: Path = ROUTER_DB_PATH,
        security: Optional[SecurityManager] = None,
    ):
        self.base_url = base_url.rstrip("/")
        self.db_path = db_path
        self._cached_cli_token: Optional[str] = None
        self._cached_api_key: Optional[str] = None
        # Live-access gate: every network method refuses while SECRETS: LOCKED.
        self.security = security or get_default_security()

    def _require_live(self, operation: str) -> None:
        self.security.require_live(operation)

    @staticmethod
    def _sanitize_connection(conn: Dict[str, Any]) -> Dict[str, Any]:
        """Sanitizing boundary: 9Router /api/providers returns full connection
        objects that may embed provider credentials (apiKey, accessToken, ...).
        WatchEdit only ever consumes the allowlisted fields below, and raw
        responses are never persisted."""
        data = conn.get("providerSpecificData") or {}
        prefix = data.get("prefix", "") if isinstance(data, dict) else ""
        return {
            "id": conn.get("id", ""),
            "provider": conn.get("provider", ""),
            "name": conn.get("name") or conn.get("id", ""),
            "isActive": bool(conn.get("isActive", True)),
            "providerSpecificData": {"prefix": prefix},
            "updatedAt": conn.get("updatedAt", ""),
        }

    def get_cli_token(self) -> str:
        """Derives the x-9r-cli-token from machine-id and auth/cli-secret."""
        if self._cached_cli_token:
            return self._cached_cli_token

        raw_id = ""
        if MACHINE_ID_FILE.exists():
            try:
                raw_id = MACHINE_ID_FILE.read_text(encoding="utf-8").strip()
            except Exception:
                pass

        cli_secret = ""
        if CLI_SECRET_FILE.exists():
            try:
                cli_secret = CLI_SECRET_FILE.read_text(encoding="utf-8").strip()
            except Exception:
                pass

        to_hash = raw_id + CLI_TOKEN_SALT + cli_secret
        token = hashlib.sha256(to_hash.encode("utf-8")).hexdigest()[:16]
        self._cached_cli_token = token
        return token

    def get_api_key(self) -> Optional[str]:
        """Fetches active 9Router API key from data.sqlite for Bearer authorization."""
        if self._cached_api_key:
            return self._cached_api_key

        if not self.db_path.exists():
            return None

        try:
            conn = sqlite3.connect(f"file:{self.db_path}?mode=ro", uri=True)
            cursor = conn.cursor()
            cursor.execute("SELECT key FROM apiKeys ORDER BY createdAt ASC")
            rows = cursor.fetchall()
            conn.close()
            if rows:
                self._cached_api_key = rows[0][0]
                return self._cached_api_key
        except Exception:
            pass
        return None

    def _get_headers(self, include_bearer: bool = False) -> Dict[str, str]:
        headers = {
            "Content-Type": "application/json",
            CLI_TOKEN_HEADER: self.get_cli_token(),
        }
        if include_bearer:
            key = self.get_api_key()
            if key:
                headers["Authorization"] = f"Bearer {key}"
        return headers

    def is_server_reachable(self, timeout: float = 3.0) -> bool:
        """Quick health check against 9Router."""
        self._require_live("is_server_reachable")
        try:
            with httpx.Client(timeout=timeout) as client:
                res = client.get(f"{self.base_url}/api/version")
                return res.status_code in (200, 401)
        except Exception:
            return False

    # -------------------------------------------------------------
    # COMBOS API
    # -------------------------------------------------------------
    def get_combos(self) -> List[Dict[str, Any]]:
        """Fetch all combos via API, fallback to SQLite."""
        self._require_live("get_combos")
        try:
            headers = self._get_headers()
            with httpx.Client(timeout=5.0) as client:
                res = client.get(f"{self.base_url}/api/combos", headers=headers)
                if res.status_code == 200:
                    data = res.json()
                    return data.get("combos", [])
        except Exception:
            pass
        return self._get_combos_sqlite()

    def create_combo(self, name: str, models: List[str], kind: Optional[str] = None) -> Optional[Dict[str, Any]]:
        """Create new combo via API."""
        self._require_live("create_combo")
        try:
            headers = self._get_headers()
            payload = {"name": name, "models": models, "kind": kind}
            with httpx.Client(timeout=5.0) as client:
                res = client.post(f"{self.base_url}/api/combos", headers=headers, json=payload)
                if res.status_code in (200, 201):
                    return res.json()
        except Exception:
            pass
        return None

    def update_combo(self, combo_id: str, name: str, models: List[str], kind: Optional[str] = None) -> Optional[Dict[str, Any]]:
        """
        Updates combo via 9Router REST API (triggers resetComboRotation in engine memory).
        FAILS CLOSED: Never automatically falls back to direct SQLite mutation.
        """
        self._require_live("update_combo")
        try:
            headers = self._get_headers()
            payload = {"name": name, "models": models, "kind": kind}
            with httpx.Client(timeout=8.0) as client:
                res = client.put(f"{self.base_url}/api/combos/{combo_id}", headers=headers, json=payload)
                if res.status_code in (200, 204):
                    return res.json() if res.content else {"id": combo_id, "name": name, "models": models}
        except Exception:
            pass
        return None

    def rename_combo(self, combo_id: str, new_name: str, current_models: List[str], kind: Optional[str] = None) -> Optional[Dict[str, Any]]:
        """Renames a combo through the official update API."""
        return self.update_combo(combo_id=combo_id, name=new_name, models=current_models, kind=kind)

    def delete_combo(self, combo_id: str) -> bool:
        """
        Deletes combo via 9Router REST API.
        FAILS CLOSED: Never automatically falls back to direct SQLite mutation.
        """
        self._require_live("delete_combo")
        try:
            headers = self._get_headers()
            with httpx.Client(timeout=8.0) as client:
                res = client.delete(f"{self.base_url}/api/combos/{combo_id}", headers=headers)
                return res.status_code in (200, 204)
        except Exception:
            pass
        return False

    def offline_recovery_update_combo(
        self,
        combo_id: str,
        name: str,
        models: List[str],
        kind: Optional[str] = None,
        allow_offline_wal_mutation: bool = False,
    ) -> Optional[Dict[str, Any]]:
        """
        EMERGENCY OFFLINE RECOVERY ONLY.
        Directly mutates combos table in data.sqlite ONLY when 9Router process is verified OFFLINE.
        Refuses to run if 9Router API is reachable or allow_offline_wal_mutation is False.
        """
        if not allow_offline_wal_mutation:
            raise PermissionError("Offline recovery requires explicit allow_offline_wal_mutation=True")
        if self.is_server_reachable():
            raise RuntimeError("Cannot perform direct SQLite mutation while 9Router server is live!")
        return self._update_combo_sqlite(combo_id, name, models, kind)

    def offline_recovery_delete_combo(
        self,
        combo_id: str,
        allow_offline_wal_mutation: bool = False,
    ) -> bool:
        """EMERGENCY OFFLINE RECOVERY ONLY for combo deletion."""
        if not allow_offline_wal_mutation:
            raise PermissionError("Offline recovery requires explicit allow_offline_wal_mutation=True")
        if self.is_server_reachable():
            raise RuntimeError("Cannot perform direct SQLite mutation while 9Router server is live!")
        return self._delete_combo_sqlite(combo_id)


    # -------------------------------------------------------------
    # PROVIDERS & MODELS API
    # -------------------------------------------------------------
    def get_providers(self) -> List[Dict[str, Any]]:
        """Fetch all provider connections (sanitized: credentials never leave this method)."""
        self._require_live("get_providers")
        try:
            headers = self._get_headers()
            with httpx.Client(timeout=8.0) as client:
                res = client.get(f"{self.base_url}/api/providers", headers=headers)
                if res.status_code == 200:
                    data = res.json()
                    return [self._sanitize_connection(c) for c in data.get("connections", [])]
        except Exception:
            pass
        return [self._sanitize_connection(c) for c in self._get_providers_sqlite()]

    def get_provider_nodes(self) -> List[Dict[str, Any]]:
        """Fetch all compatible provider nodes."""
        self._require_live("get_provider_nodes")
        try:
            headers = self._get_headers()
            with httpx.Client(timeout=8.0) as client:
                res = client.get(f"{self.base_url}/api/provider-nodes", headers=headers)
                if res.status_code == 200:
                    data = res.json()
                    return data.get("nodes", [])
        except Exception:
            pass
        return self._get_provider_nodes_sqlite()

    def get_catalog_models(self) -> List[Dict[str, Any]]:
        """Fetch models registered in 9Router catalog."""
        self._require_live("get_catalog_models")
        try:
            headers = self._get_headers()
            with httpx.Client(timeout=10.0) as client:
                res = client.get(f"{self.base_url}/api/models", headers=headers)
                if res.status_code == 200:
                    data = res.json()
                    return data.get("models", [])
        except Exception:
            pass
        return []

    def get_connection_live_models_detailed(self, connection_id: str) -> Tuple[str, List[Dict[str, Any]]]:
        """
        Fetches live upstream models with explicit outcome status:
        ('OK', models), ('TIMEOUT', []), ('NOT_SUPPORTED', []), or ('FAILED', [])
        """
        self._require_live("get_connection_live_models")
        try:
            headers = self._get_headers()
            with httpx.Client(timeout=12.0) as client:
                res = client.get(f"{self.base_url}/api/providers/{connection_id}/models", headers=headers)
                if res.status_code == 200:
                    data = res.json()
                    return ("OK", data.get("models", []))
                elif res.status_code in (404, 405, 501):
                    return ("NOT_SUPPORTED", [])
                else:
                    return ("FAILED", [])
        except httpx.TimeoutException:
            return ("TIMEOUT", [])
        except Exception:
            return ("FAILED", [])

    def get_connection_live_models(self, connection_id: str) -> List[Dict[str, Any]]:
        """Fetch live upstream models for a specific connection."""
        status, models = self.get_connection_live_models_detailed(connection_id)
        return models if status == "OK" else []

    # -------------------------------------------------------------
    # PROBE & TEST EXECUTION
    # -------------------------------------------------------------
    def ping_model_fast(self, model: str, timeout: float = 15.0) -> Dict[str, Any]:
        """Calls 9Router's internal POST /api/models/test."""
        self._require_live("ping_model_fast")
        headers = self._get_headers()
        payload = {"model": model, "kind": "llm"}
        start = datetime.now()
        try:
            with httpx.Client(timeout=timeout) as client:
                res = client.post(f"{self.base_url}/api/models/test", headers=headers, json=payload)
                elapsed_ms = (datetime.now() - start).total_seconds() * 1000.0
                try:
                    data = res.json()
                except Exception:
                    data = {"error": res.text}
                return {
                    "ok": data.get("ok", res.status_code == 200),
                    "status": res.status_code,
                    "latencyMs": data.get("latencyMs", elapsed_ms),
                    "error": data.get("error"),
                    "raw_response": res.text,
                    "json_response": data,
                }
        except httpx.TimeoutException:
            elapsed_ms = (datetime.now() - start).total_seconds() * 1000.0
            return {
                "ok": False,
                "status": 408,
                "latencyMs": elapsed_ms,
                "error": "Request timed out",
                "is_timeout": True,
                "raw_response": "",
                "json_response": None,
            }
        except Exception as ex:
            elapsed_ms = (datetime.now() - start).total_seconds() * 1000.0
            return {
                "ok": False,
                "status": 0,
                "latencyMs": elapsed_ms,
                "error": str(ex),
                "raw_response": str(ex),
                "json_response": None,
            }

    def probe_chat_completion(self, model: str, timeout: float = 15.0) -> Dict[str, Any]:
        """
        Sends minimal completion to POST /v1/chat/completions for deep operational triage.
        Returns raw status, full response body and latency.
        """
        self._require_live("probe_chat_completion")
        headers = self._get_headers(include_bearer=True)
        payload = {
            "model": model,
            "messages": [{"role": "user", "content": PROBE_PROMPT}],
            "max_tokens": PROBE_MAX_TOKENS,
            "stream": False,
        }
        start = datetime.now()
        try:
            with httpx.Client(timeout=timeout) as client:
                res = client.post(f"{self.base_url}/v1/chat/completions", headers=headers, json=payload)
                elapsed_ms = (datetime.now() - start).total_seconds() * 1000.0
                raw_text = res.text
                try:
                    data = res.json()
                except Exception:
                    data = None
                return {
                    "ok": res.status_code == 200,
                    "status": res.status_code,
                    "latencyMs": elapsed_ms,
                    "raw_response": raw_text,
                    "json_response": data,
                    "is_timeout": False,
                }
        except httpx.TimeoutException:
            elapsed_ms = (datetime.now() - start).total_seconds() * 1000.0
            return {
                "ok": False,
                "status": 408,
                "latencyMs": elapsed_ms,
                "raw_response": "Request timed out",
                "json_response": None,
                "is_timeout": True,
            }
        except Exception as ex:
            elapsed_ms = (datetime.now() - start).total_seconds() * 1000.0
            return {
                "ok": False,
                "status": 0,
                "latencyMs": elapsed_ms,
                "raw_response": str(ex),
                "json_response": None,
                "is_timeout": False,
            }

    # -------------------------------------------------------------
    # SQLITE FALLBACK IMPLEMENTATIONS
    # -------------------------------------------------------------
    def _create_db_backup(self):
        """Creates timestamped backup of data.sqlite before direct modification."""
        if not self.db_path.exists():
            return
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        backup_file = BACKUP_DIR / f"data_{timestamp}.sqlite"
        try:
            shutil.copy2(self.db_path, backup_file)
        except Exception:
            pass

    def _get_combos_sqlite(self) -> List[Dict[str, Any]]:
        if not self.db_path.exists():
            return []
        combos = []
        try:
            conn = sqlite3.connect(f"file:{self.db_path}?mode=ro", uri=True)
            cursor = conn.cursor()
            cursor.execute("SELECT id, name, kind, models, createdAt, updatedAt FROM combos ORDER BY createdAt ASC")
            for row in cursor.fetchall():
                models = []
                try:
                    models = json.loads(row[3]) if row[3] else []
                except Exception:
                    pass
                combos.append({
                    "id": row[0],
                    "name": row[1],
                    "kind": row[2],
                    "models": models,
                    "createdAt": row[4],
                    "updatedAt": row[5],
                })
            conn.close()
        except Exception:
            pass
        return combos

    def _update_combo_sqlite(self, combo_id: str, name: str, models: List[str], kind: Optional[str] = None) -> Optional[Dict[str, Any]]:
        if not self.db_path.exists():
            return None
        self._create_db_backup()
        now = datetime.now().isoformat()
        try:
            conn = sqlite3.connect(str(self.db_path), timeout=10.0)
            cursor = conn.cursor()
            cursor.execute(
                "UPDATE combos SET name = ?, kind = ?, models = ?, updatedAt = ? WHERE id = ?",
                (name, kind, json.dumps(models), now, combo_id)
            )
            conn.commit()
            conn.close()
            return {"id": combo_id, "name": name, "models": models, "kind": kind, "updatedAt": now}
        except Exception:
            return None

    def _delete_combo_sqlite(self, combo_id: str) -> bool:
        if not self.db_path.exists():
            return False
        self._create_db_backup()
        try:
            conn = sqlite3.connect(str(self.db_path), timeout=10.0)
            cursor = conn.cursor()
            cursor.execute("DELETE FROM combos WHERE id = ?", (combo_id,))
            changes = conn.total_changes
            conn.commit()
            conn.close()
            return changes > 0
        except Exception:
            return False

    def _get_providers_sqlite(self) -> List[Dict[str, Any]]:
        if not self.db_path.exists():
            return []
        providers = []
        try:
            conn = sqlite3.connect(f"file:{self.db_path}?mode=ro", uri=True)
            cursor = conn.cursor()
            cursor.execute("SELECT id, provider, name, isActive, data, createdAt, updatedAt FROM providerConnections")
            for row in cursor.fetchall():
                data_obj = {}
                try:
                    data_obj = json.loads(row[4]) if row[4] else {}
                except Exception:
                    pass
                providers.append({
                    "id": row[0],
                    "provider": row[1],
                    "name": row[2],
                    "isActive": bool(row[3]),
                    "providerSpecificData": data_obj.get("providerSpecificData", {}),
                    "createdAt": row[5],
                    "updatedAt": row[6],
                })
            conn.close()
        except Exception:
            pass
        return providers

    def _get_provider_nodes_sqlite(self) -> List[Dict[str, Any]]:
        if not self.db_path.exists():
            return []
        nodes = []
        try:
            conn = sqlite3.connect(f"file:{self.db_path}?mode=ro", uri=True)
            cursor = conn.cursor()
            cursor.execute("SELECT id, type, name, data, createdAt, updatedAt FROM providerNodes")
            for row in cursor.fetchall():
                data_obj = {}
                try:
                    data_obj = json.loads(row[3]) if row[3] else {}
                except Exception:
                    pass
                nodes.append({
                    "id": row[0],
                    "type": row[1],
                    "name": row[2],
                    "prefix": data_obj.get("prefix", ""),
                    "baseUrl": data_obj.get("baseUrl", ""),
                    "apiType": data_obj.get("apiType", ""),
                    "createdAt": row[4],
                    "updatedAt": row[5],
                })
            conn.close()
        except Exception:
            pass
        return nodes

    def get_kv(self) -> List[Tuple[str, str]]:
        """Fetch all key-value entries from 9Router SQLite (read-only mode)."""
        return self._get_kv_sqlite()

    def _get_kv_sqlite(self) -> List[Tuple[str, str]]:
        if not self.db_path.exists():
            return []
        try:
            conn = sqlite3.connect(f"file:{self.db_path}?mode=ro", uri=True)
            cursor = conn.cursor()
            cursor.execute("SELECT key, value FROM kv")
            rows = cursor.fetchall()
            conn.close()
            return [(str(r[0]), str(r[1])) for r in rows]
        except Exception:
            return []
