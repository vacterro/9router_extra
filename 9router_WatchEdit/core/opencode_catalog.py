"""
9router_WatchEdit - OpenCode Zen Public Catalog Discovery (SRC-004)

Independent discovery source for the CURRENT OpenCode Zen model inventory
published at https://opencode.ai/zen/v1/models. Completely isolated from
RouterClient provider discovery and from the "opencode-go" provider identity.

Key safety properties:
- Catalog discovery proves "OpenCode advertises this model" -- it NEVER proves
  "this local 9Router can route it". Catalog-only rows are configured=False and
  routing_eligible=False.
- Free classification is evidence-based: only explicit "-free" model id
  convention (free_reason="explicit_free_model_id") yields FREE_CANDIDATE.
  Unknown cost stays UNKNOWN, never FREE, never PAID.
- A failed refresh never destroys the last known good snapshot.
"""
import json
import shutil
import subprocess
import threading
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Set

import httpx

from config import LOCALAPPDATA_DIR, PRIVATE_STORAGE_AVAILABLE

ZEN_MODELS_URL = "https://opencode.ai/zen/v1/models"
OPENCODE_PROVIDER_NAMESPACE = "opencode"
OPENCODE_SOURCE_TAG = "OPENCODE_ZEN_PUBLIC"
FREE_SUFFIX = "-free"
FREE_REASON_EXPLICIT_ID = "explicit_free_model_id"

# Refresh TTL: repeated UI refreshes within this window reuse the snapshot and
# never hit opencode.ai (SRC-004 UI requirement: 10-30 min band).
CATALOG_TTL_SECONDS = 15 * 60

# States
API_OK = "API_OK"
API_FAILED = "API_FAILED"
CLI_OK = "CLI_OK"
CLI_MISSING = "CLI_MISSING"
CLI_FAILED = "CLI_FAILED"
CATALOG_CHANGED = "CATALOG_CHANGED"

CLI_TIMEOUT_SECONDS = 20.0
HTTP_TIMEOUT_SECONDS = 8.0

SNAPSHOT_SCHEMA_VERSION = 2

_SNAPSHOT_SCHEMA_KEYS = {
    "schema_version", "fetched_at", "models", "free_ids", "statuses"
}


def catalog_events(diff: "CatalogDiff") -> List[Dict[str, str]]:
    """Structured catalog change events (SRC-004 §5). Read-only surface: the
    caller decides what to do with them; nothing here mutates combos,
    providers, credentials or routing."""
    events: List[Dict[str, str]] = []
    for mid in diff.added:
        events.append({"type": "added", "model_id": mid,
                       "canonical_id": f"{OPENCODE_PROVIDER_NAMESPACE}/{mid}"})
    for mid in diff.removed:
        events.append({"type": "removed", "model_id": mid,
                       "canonical_id": f"{OPENCODE_PROVIDER_NAMESPACE}/{mid}"})
    for mid in diff.newly_free:
        events.append({
            "type": "newly_free",
            "model_id": mid,
            "canonical_id": f"{OPENCODE_PROVIDER_NAMESPACE}/{mid}",
            "message": f"NEW FREE OPENCODE MODEL: {OPENCODE_PROVIDER_NAMESPACE}/{mid}",
        })
    for mid in diff.no_longer_free:
        events.append({"type": "no_longer_free", "model_id": mid,
                       "canonical_id": f"{OPENCODE_PROVIDER_NAMESPACE}/{mid}"})
    return events


@dataclass
class ZenCatalogModel:
    model_id: str
    canonical_id: str                     # opencode/<model-id>
    provider_namespace: str = OPENCODE_PROVIDER_NAMESPACE
    source: str = OPENCODE_SOURCE_TAG
    free_candidate: bool = False
    free_reason: Optional[str] = None     # evidence preserved (SRC-004 §2)
    observed_at: str = ""
    configured: bool = False
    routing_eligible: bool = False

    @property
    def cost_state(self) -> str:
        return "FREE_CANDIDATE" if self.free_candidate else "UNKNOWN"


@dataclass
class CatalogDiff:
    added: List[str] = field(default_factory=list)
    removed: List[str] = field(default_factory=list)
    newly_free: List[str] = field(default_factory=list)
    no_longer_free: List[str] = field(default_factory=list)


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def classify_free(model_id: str) -> (bool, Optional[str]):
    """Evidence-based free classification.

    Strong explicit evidence only: the model id advertises a free variant by
    the conventional "-free" suffix. Everything else stays UNKNOWN. Future API
    billing metadata, when published, must be preferred by the caller before
    this heuristic (see parse_catalog).
    """
    if model_id.endswith(FREE_SUFFIX):
        return True, FREE_REASON_EXPLICIT_ID
    return False, None


def parse_catalog(payload) -> List[ZenCatalogModel]:
    """Parse the Zen /v1/models payload.

    Accepts {"models":[{"id":...},...]} and {"data":[{"id":...},...]} shapes.
    Raises ValueError on malformed JSON, non-dict payload, unexpected schema,
    or model entries without a usable string id. Never guesses.
    """
    if isinstance(payload, (str, bytes)):
        try:
            payload = json.loads(payload)
        except json.JSONDecodeError as ex:
            raise ValueError(f"malformed JSON: {ex}") from ex
    if not isinstance(payload, dict):
        raise ValueError("unexpected schema: payload is not an object")
    raw_models = payload.get("models")
    if raw_models is None:
        raw_models = payload.get("data")
    if not isinstance(raw_models, list):
        raise ValueError("unexpected schema: no models/data array")

    out: List[ZenCatalogModel] = []
    seen: Set[str] = set()
    observed = _utc_now_iso()
    for entry in raw_models:
        if not isinstance(entry, dict):
            raise ValueError("unexpected schema: model entry is not an object")
        model_id = entry.get("id") or entry.get("name")
        if not isinstance(model_id, str) or not model_id.strip():
            raise ValueError("unexpected schema: model entry has no id")
        model_id = model_id.strip()
        if model_id in seen:
            continue
        seen.add(model_id)

        free_candidate, free_reason = classify_free(model_id)
        # Future-proof hook: if the API begins to publish explicit zero-cost
        # billing metadata, that metadata wins over the naming heuristic.
        pricing = entry.get("pricing")
        if isinstance(pricing, dict):
            keys = [key for key in ("free", "isFree") if key in pricing]
            values = [pricing[key] for key in keys]
            boolean_values = {value for value in values if isinstance(value, bool)}
            if len(boolean_values) > 1:
                free_candidate, free_reason = False, "conflicting_billing_metadata"
            elif boolean_values == {True}:
                free_candidate, free_reason = True, "explicit_billing_metadata"
            elif boolean_values == {False}:
                if pricing.get("verified") is True or pricing.get("paid") is True:
                    free_candidate, free_reason = False, "explicit_paid_metadata"
                else:
                    free_candidate, free_reason = False, "ambiguous_billing_metadata"
            elif keys:
                free_candidate, free_reason = False, "ambiguous_billing_metadata"

        out.append(ZenCatalogModel(
            model_id=model_id,
            canonical_id=f"{OPENCODE_PROVIDER_NAMESPACE}/{model_id}",
            free_candidate=free_candidate,
            free_reason=free_reason,
            observed_at=observed,
        ))
    return out


def diff_snapshots(previous: List[ZenCatalogModel], current: List[ZenCatalogModel]) -> CatalogDiff:
    prev_by_id = {m.model_id: m for m in previous}
    cur_by_id = {m.model_id: m for m in current}
    prev_free = {m.model_id for m in previous if m.free_candidate}
    cur_free = {m.model_id for m in current if m.free_candidate}
    return CatalogDiff(
        added=sorted(set(cur_by_id) - set(prev_by_id)),
        removed=sorted(set(prev_by_id) - set(cur_by_id)),
        newly_free=sorted(cur_free - prev_free),
        no_longer_free=sorted(prev_free - cur_free),
    )


class OpenCodeCatalogDiscovery:
    """Public Zen catalog discovery with snapshot persistence and TTL cache.

    Thread-safety: refresh() is serialized through an internal lock; a second
    concurrent refresh() returns the in-flight/completed result instead of
    starting an overlapping HTTP fetch (SRC-004: never overlapping fetches).
    """

    def __init__(
        self,
        snapshot_file: Optional[Path] = None,
        ttl_seconds: float = CATALOG_TTL_SECONDS,
        http_timeout: float = HTTP_TIMEOUT_SECONDS,
        cli_timeout: float = CLI_TIMEOUT_SECONDS,
        transport: Optional[httpx.BaseTransport] = None,
    ):
        # ponytail: non-secret public data; falls back to devnull (read-only
        # degenerate mode) when private storage is unavailable, mirroring
        # config.py fail-closed semantics.
        self.snapshot_file = snapshot_file if snapshot_file is not None else (
            LOCALAPPDATA_DIR / "opencode_catalog.json"
        )
        self.ttl_seconds = ttl_seconds
        self.http_timeout = http_timeout
        self.cli_timeout = cli_timeout
        self.transport = transport
        self._refresh_lock = threading.Lock()
        # Deterministically initialized BEFORE any thread can observe it:
        # late joiners of an in-flight refresh read this under the lock.
        self._last_refresh_result: Dict[str, object] = {
            "fresh": False, "status": API_FAILED, "changed": False,
            "diff": None, "fetch": False, "error_class": "not_refreshed",
        }

        self.models: List[ZenCatalogModel] = []
        self.free_ids: List[str] = []
        self.statuses: Dict[str, str] = {}
        self.last_success_at: str = ""
        self.last_failure: Dict[str, str] = {}
        self.last_diff: Optional[CatalogDiff] = None
        self.cli_result: Optional[Dict[str, object]] = None
        self.previous_models: List[ZenCatalogModel] = []
        self._load_snapshot()

    # ------------------------------------------------------------ snapshot io
    def _load_snapshot(self) -> None:
        try:
            raw = self.snapshot_file.read_text(encoding="utf-8")
            data = json.loads(raw)
            if not isinstance(data, dict) or not _SNAPSHOT_SCHEMA_KEYS.issubset(data):
                return
            if data.get("schema_version") != SNAPSHOT_SCHEMA_VERSION:
                # Old/incomplete snapshot: not trusted as diff baseline; keep
                # nothing rather than mis-diffing against an unknown shape.
                return
            models = []
            for m in data.get("models", []):
                if not isinstance(m, dict) or not isinstance(m.get("model_id"), str):
                    continue
                known = {f for f in ZenCatalogModel.__dataclass_fields__}
                models.append(ZenCatalogModel(**{k: v for k, v in m.items() if k in known}))
            self.models = models
            self.free_ids = list(data.get("free_ids", []))
            self.statuses = dict(data.get("statuses", {}))
            self.last_success_at = data.get("fetched_at", "")
            self.previous_models = []
            prev = data.get("previous_models")
            if isinstance(prev, list):
                for m in prev:
                    if isinstance(m, dict) and isinstance(m.get("model_id"), str):
                        known = {f for f in ZenCatalogModel.__dataclass_fields__}
                        self.previous_models.append(ZenCatalogModel(**{k: v for k, v in m.items() if k in known}))
            self.last_diff = None
            diff = data.get("last_diff")
            if isinstance(diff, dict):
                self.last_diff = CatalogDiff(
                    added=list(diff.get("added", []) or []),
                    removed=list(diff.get("removed", []) or []),
                    newly_free=list(diff.get("newly_free", []) or []),
                    no_longer_free=list(diff.get("no_longer_free", []) or []),
                )
            self.last_failure = {}
            failure = data.get("last_failure")
            if isinstance(failure, dict) and isinstance(failure.get("class"), str):
                self.last_failure = {"at": str(failure.get("at", "")), "class": failure["class"]}
            self.cli_result = None
            cli = data.get("cli_result")
            if isinstance(cli, dict) and isinstance(cli.get("status"), str):
                self.cli_result = cli
        except Exception:
            # unreadable/corrupt snapshot: start empty, never crash, never
            # treat as a catalog failure (no fetch has been attempted yet).
            self.models = []
            self.free_ids = []
            self.statuses = {}
            self.last_success_at = ""
            self.previous_models = []
            self.last_diff = None
            self.last_failure = {}
            self.cli_result = None

    def _save_snapshot(self) -> None:
        doc = {
            "schema_version": SNAPSHOT_SCHEMA_VERSION,
            "fetched_at": self.last_success_at,
            "models": [asdict(m) for m in self.models],
            "free_ids": sorted(self.free_ids),
            "statuses": self.statuses,
            "previous_models": [asdict(m) for m in self.previous_models],
            "last_diff": asdict(self.last_diff) if self.last_diff else None,
            "last_failure": self.last_failure,
        }
        try:
            self.snapshot_file.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.snapshot_file.with_suffix(".tmp")
            tmp.write_text(json.dumps(doc, indent=2), encoding="utf-8")
            tmp.replace(self.snapshot_file)
        except Exception:
            # Persistence failure must not surface as a catalog failure: the
            # in-memory snapshot remains authoritative for this session.
            pass

    # ------------------------------------------------------------ public api
    @property
    def is_fresh(self) -> bool:
        return not self._ttl_expired()

    def _ttl_expired(self) -> bool:
        if not self.last_success_at or not self.models:
            return True
        try:
            last = datetime.strptime(self.last_success_at, "%Y-%m-%dT%H:%M:%SZ").replace(
                tzinfo=timezone.utc
            )
        except ValueError:
            return True
        age = (datetime.now(timezone.utc) - last).total_seconds()
        return age < 0 or age > self.ttl_seconds

    def refresh(self, force: bool = False) -> Dict[str, object]:
        """Refresh the catalog. Returns a status dict; never raises.

        Within TTL and not forced: returns the cached snapshot (API_OK/STALE
        semantics handled by the caller through 'fresh' key).
        Concurrent calls join the in-flight refresh rather than stacking HTTP
        fetches (dedup requirement).
        """
        if not force and not self._ttl_expired():
            return {"fresh": True, "status": API_OK, "changed": False, "diff": None,
                    "fetch": False}

        # Dedup: whoever holds the refresh lock performs the fetch; late
        # joiners (or the second thread of a race) reuse its result.
        if not self._refresh_lock.acquire(blocking=False):
            # Another refresh is in flight: wait for it and adopt its outcome.
            with self._refresh_lock:
                return self._last_refresh_result.copy()

        try:
            result = self._refresh_locked()
        except Exception:
            result = {
                "fresh": False, "status": API_FAILED, "changed": False,
                "diff": None, "fetch": False, "error_class": "unexpected_refresh_error",
            }
            self.last_failure = {"at": _utc_now_iso(), "class": "unexpected_refresh_error"}
            self.statuses["api"] = API_FAILED
        self._last_refresh_result = result
        self._refresh_lock.release()
        return result.copy()

    def _refresh_locked(self) -> Dict[str, object]:
        api_status = API_FAILED
        models: List[ZenCatalogModel] = []
        error_class = ""
        try:
            with httpx.Client(
                timeout=self.http_timeout,
                follow_redirects=False,
                trust_env=True,
                transport=self.transport,
            ) as client:
                res = client.get(ZEN_MODELS_URL)
            if res.status_code != 200:
                error_class = f"http_{res.status_code}"
            else:
                models = parse_catalog(res.text)
                api_status = API_OK
        except ValueError as ex:
            error_class = "malformed_payload"
        except httpx.TimeoutException:
            error_class = "timeout"
        except Exception as ex:
            error_class = "network_error"

        if api_status != API_OK:
            # NEVER delete the previous good snapshot on failure (SRC-004 §1/§4).
            self.statuses["api"] = API_FAILED
            self.last_failure = {
                "at": _utc_now_iso(),
                "class": error_class,
            }
            self._save_snapshot()
            return {"fresh": False, "status": API_FAILED, "changed": False,
                    "diff": None, "fetch": True, "error_class": error_class}

        previous = list(self.models)
        diff = diff_snapshots(previous, models)
        # A first-ever load (previous empty) establishes baseline, not a real
        # catalog change. Suppress false NEW events and CATALOG_CHANGED.
        changed = bool(previous) and bool(diff.added or diff.removed or diff.newly_free or diff.no_longer_free)

        self.previous_models = list(self.models)
        self.models = models
        self.free_ids = sorted(m.model_id for m in models if m.free_candidate)
        self.last_success_at = _utc_now_iso()
        self.statuses["api"] = API_OK
        self.last_diff = diff if changed else None
        self._save_snapshot()

        status = CATALOG_CHANGED if changed else API_OK
        result = {"fresh": True, "status": status, "changed": changed,
                  "diff": diff if changed else None, "fetch": True}
        if changed:
            result["events"] = catalog_events(diff)
        return result

    # ------------------------------------------------------------ CLI cross-check
    def cli_cross_check(self) -> Dict[str, object]:
        """Secondary evidence pass via 'opencode models opencode --refresh --verbose'.

        Non-fatal when the executable is missing; bounded timeout; no shell;
        stdout/stderr captured without credential logging (output is parsed for
        canonical model ids only; raw text never persisted).
        """
        exe = shutil.which("opencode")
        if not exe:
            self.statuses["cli"] = CLI_MISSING
            return {"status": CLI_MISSING, "ids": [], "matches": None}

        try:
            proc = subprocess.run(
                [exe, "models", "opencode", "--refresh", "--verbose"],
                capture_output=True,
                text=True,
                timeout=self.cli_timeout,
                shell=False,
                # Windows: no console window flash for a hidden helper process.
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except subprocess.TimeoutExpired:
            self.statuses["cli"] = CLI_FAILED
            return {"status": CLI_FAILED, "ids": [], "matches": None,
                    "error_class": "cli_timeout"}
        except Exception:
            self.statuses["cli"] = CLI_FAILED
            return {"status": CLI_FAILED, "ids": [], "matches": None,
                    "error_class": "cli_error"}

        if proc.returncode != 0:
            self.statuses["cli"] = CLI_FAILED
            return {"status": CLI_FAILED, "ids": [], "matches": None,
                    "api_only": [], "cli_only": [],
                    "error_class": f"cli_exit_{proc.returncode}"}

        ids = sorted(
            {
                line.strip().split()[0]
                for line in (proc.stdout or "").splitlines()
                if line.strip().startswith(f"{OPENCODE_PROVIDER_NAMESPACE}/")
            }
        )
        self.statuses["cli"] = CLI_OK
        api_ids = {m.canonical_id for m in self.models}
        cli_result = {
            "status": CLI_OK,
            "ids": ids,
            "matches": sorted(api_ids & set(ids)),
            "api_only": sorted(api_ids - set(ids)),
            "cli_only": sorted(set(ids) - api_ids),
        }
        self.cli_result = cli_result
        self._save_snapshot()
        return cli_result

    def ui_status(self) -> str:
        """Compact OK / STALE / FAILED rollup for the catalog panel."""
        if self.statuses.get("api") == API_FAILED:
            return "FAILED"
        if self.statuses.get("api") == API_OK and not self._ttl_expired():
            return "OK"
        return "STALE"
