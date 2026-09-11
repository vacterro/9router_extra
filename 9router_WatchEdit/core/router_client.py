"""
9router_WatchEdit - 9Router REST API & SQLite Fallback Client
Handles machineId-based CLI token generation, provider discovery, model pinging,
and safe combo updates.
"""
import hashlib
import ipaddress
import json
import sqlite3
import shutil
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple, Any
from urllib.parse import urlsplit
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

# Loopback trust boundary: RouterClient carries LOCAL 9router authentication
# material (x-9r-cli-token, local Bearer API key). Its target therefore must
# resolve syntactically to an explicit loopback IP address. DNS hostnames are
# never trusted for this boundary, and there is deliberately no override.
_LOOPBACK_V4 = ipaddress.ip_network("127.0.0.0/8")
_LOOPBACK_V6 = ipaddress.ip_address("::1")


def validate_router_base_url(value: str) -> str:
    """Authoritative loopback-only validation for the 9Router control plane URL.

    Returns a normalized base URL, or raises ValueError. Fails closed: only an
    explicitly numeric loopback host (127.0.0.0/8 or ::1) is accepted.
    """
    if not isinstance(value, str):
        raise ValueError("Router base URL must be a string")
    raw = value
    if not raw:
        raise ValueError("Router base URL must not be empty")
    # urlsplit strips some controls; reject them before parsing to avoid
    # disagreeing with the HTTP transport about the authority.
    if any(c.isspace() or ord(c) < 32 or ord(c) == 127 for c in raw) or "\\" in raw:
        raise ValueError("Router base URL must not contain whitespace, controls or backslashes")

    try:
        parts = urlsplit(raw)
    except ValueError:
        raise ValueError("Router base URL is malformed") from None

    if parts.scheme not in ("http", "https"):
        raise ValueError("Router base URL must use http or https")

    if "?" in raw or "#" in raw:
        raise ValueError("Router base URL must not contain a query or fragment")

    if parts.username is not None or parts.password is not None:
        raise ValueError("Router base URL must not contain userinfo credentials (user:pass@host)")

    host = parts.hostname
    if not host:
        raise ValueError("Router base URL has no host")

    try:
        addr = ipaddress.ip_address(host)
    except ValueError:
        raise ValueError(
            "Router base URL host must be an explicit numeric loopback IP "
            "(127.0.0.0/8 or ::1); DNS hostnames are not accepted"
        ) from None

    if addr.version == 6:
        if addr != _LOOPBACK_V6:
            raise ValueError("Router base URL host must be IPv6 loopback ::1")
    elif addr not in _LOOPBACK_V4:
        raise ValueError("Router base URL host must be IPv4 loopback 127.0.0.0/8")

    # Port range is intentionally NOT restricted: offline tests and tooling use
    # deliberately unroutable loopback ports (e.g. 99999) to force connection
    # failure. A syntactically numeric port is preserved as-is; a non-numeric
    # port is malformed and rejected.
    netloc = parts.netloc
    if netloc.startswith("["):
        close = netloc.find("]")
        if close < 0:
            raise ValueError("Malformed IPv6 authority in router base URL")
        tail = netloc[close + 1:]
        if tail and not tail.startswith(":"):
            raise ValueError("Malformed authority in router base URL")
        port_text = tail[1:]
    else:
        _, sep, port_text = netloc.partition(":")
        if not sep:
            port_text = ""
    if netloc.endswith(":") or (port_text and (not port_text.isascii() or not port_text.isdigit())):
        raise ValueError("Router base URL port must be numeric")

    authority = f"[{addr}]" if addr.version == 6 else str(addr)
    if port_text:
        authority += f":{port_text}"
    return f"{parts.scheme}://{authority}{parts.path}".rstrip("/")


def _validate_live_models_payload(data: Any) -> Optional[List[Dict[str, Any]]]:
    """Validate a decoded HTTP 200 body from the live provider models endpoint.

    Returns the models list only when the whole payload matches the live
    catalog schema: a JSON object with a `models` key whose value is a list of
    objects, each carrying a usable non-empty string `id` or `name`. The
    policy is atomic: a single malformed row rejects the entire catalog, so a
    partially valid payload can never be merged as authoritative evidence.

    Returns None for any schema violation. A genuine empty list is the only
    authoritative empty-catalog representation and is returned as [].
    """
    if not isinstance(data, dict) or "models" not in data:
        return None
    models = data["models"]
    if not isinstance(models, list):
        return None
    for row in models:
        if not isinstance(row, dict):
            return None
        row_id = row.get("id")
        row_name = row.get("name")
        has_id = isinstance(row_id, str) and row_id.strip()
        has_name = isinstance(row_name, str) and row_name.strip()
        if not has_id and not has_name:
            return None
    return models


class RouterClient:
    def __init__(
        self,
        base_url: str = DEFAULT_ROUTER_BASE_URL,
        db_path: Path = ROUTER_DB_PATH,
        security: Optional[SecurityManager] = None,
    ):
        # Loopback boundary FIRST: an invalid or non-loopback target must fail
        # here, before any machine-id / cli-secret / SQLite access and before
        # any HTTP connection can exist. This constructor is the authoritative
        # enforcement point; CLI validation is UX only.
        self._base_url = validate_router_base_url(base_url)
        self.db_path = db_path
        self._cached_cli_token: Optional[str] = None
        self._cached_api_key: Optional[str] = None
        # Live-access gate: every network method refuses while SECRETS: LOCKED.
        self.security = security or get_default_security()

    @property
    def base_url(self) -> str:
        return self._base_url

    @base_url.setter
    def base_url(self, value: str) -> None:
        # Reconfiguration has the same boundary as construction.
        self._base_url = validate_router_base_url(value)

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
        # Every client below sets follow_redirects=False explicitly: local
        # 9router credentials must never cross a redirect to another host,
        # regardless of httpx library defaults.
        try:
            with httpx.Client(timeout=timeout, follow_redirects=False, trust_env=False) as client:
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
            with httpx.Client(timeout=5.0, follow_redirects=False, trust_env=False) as client:
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
            with httpx.Client(timeout=5.0, follow_redirects=False, trust_env=False) as client:
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
            with httpx.Client(timeout=8.0, follow_redirects=False, trust_env=False) as client:
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
            with httpx.Client(timeout=8.0, follow_redirects=False, trust_env=False) as client:
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
            with httpx.Client(timeout=8.0, follow_redirects=False, trust_env=False) as client:
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
            with httpx.Client(timeout=8.0, follow_redirects=False, trust_env=False) as client:
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
            with httpx.Client(timeout=10.0, follow_redirects=False, trust_env=False) as client:
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
        ('OK', models), ('INVALID', []), ('TIMEOUT', []), ('NOT_SUPPORTED', []),
        or ('FAILED', []).

        HTTP 200 alone is not success: the decoded body must match the live
        catalog schema (see _validate_live_models_payload). A schema violation
        yields ('INVALID', []) — discovery unavailable, never an empty catalog.
        """
        self._require_live("get_connection_live_models")
        try:
            headers = self._get_headers()
            with httpx.Client(timeout=12.0, follow_redirects=False, trust_env=False) as client:
                res = client.get(f"{self.base_url}/api/providers/{connection_id}/models", headers=headers)
                if res.status_code == 200:
                    try:
                        data = res.json()
                    except Exception:
                        return ("INVALID", [])
                    models = _validate_live_models_payload(data)
                    if models is None:
                        return ("INVALID", [])
                    return ("OK", models)
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
            with httpx.Client(timeout=timeout, follow_redirects=False, trust_env=False) as client:
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

    def probe_chat_completion(
        self,
        model: str,
        timeout: float = 15.0,
        provider_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Sends minimal completion to POST /v1/chat/completions for deep operational triage.
        Returns raw status, full response body and latency.
        """
        self._require_live("probe_chat_completion")
        headers = self._get_headers(include_bearer=True)
        upstream_model = model
        if provider_id:
            from core.provider_profiles import upstream_model_id
            upstream_model = upstream_model_id(model, provider_id)
        payload = {
            "model": upstream_model,
            "messages": [{"role": "user", "content": PROBE_PROMPT}],
            "max_tokens": PROBE_MAX_TOKENS,
            "stream": False,
        }
        start = datetime.now()
        try:
            with httpx.Client(timeout=timeout, follow_redirects=False, trust_env=False) as client:
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
        """Fetch all key-value entries from 9Router SQLite (read-only mode).

        Returns (key, value) pairs with the semantic `scope` column dropped.
        Prefer `get_kv_scoped` when discovery needs the scope discriminator.
        """
        return [(k, v) for (_, k, v) in self.get_kv_scoped()]

    def get_kv_scoped(self) -> List[Tuple[str, str, str]]:
        """Fetch all kv rows preserving the semantic `scope` discriminator.

        Returns (scope, key, value) triples so discovery can distinguish
        authoritative positive scopes (e.g. `customModels`) from exclusion
        metadata (e.g. `disabledModels`) and unknown scopes.
        """
        return self._get_kv_sqlite_scoped()

    def _get_kv_sqlite(self) -> List[Tuple[str, str]]:
        return [(k, v) for (_, k, v) in self._get_kv_sqlite_scoped()]

    def _get_kv_sqlite_scoped(self) -> List[Tuple[str, str, str]]:
        if not self.db_path.exists():
            return []
        try:
            conn = sqlite3.connect(f"file:{self.db_path}?mode=ro", uri=True)
            cursor = conn.cursor()
            cursor.execute("SELECT scope, key, value FROM kv")
            rows = cursor.fetchall()
            conn.close()
            return [(str(r[0] or ""), str(r[1]), str(r[2])) for r in rows]
        except Exception:
            return []
