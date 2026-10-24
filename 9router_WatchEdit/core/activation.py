"""Per-combo model activation -- durable configured state + engine projection.

BACKEND GATE (measured against the installed engine, 9router 0.5.91-extra)
--------------------------------------------------------------------------
The authoritative ``combos`` table is::

    id, name, kind, models, createdAt, updatedAt

``models`` is an ordered JSON array of canonical ids. There is NO ``enabled``
column on a combo and NO per-member enabled flag: ``combosRepo.updateCombo``
writes only ``name``/``kind``/``models``, and the routing path
(``getComboModels`` -> ``handleComboChat``) walks that array verbatim. The
global ``kv`` scope ``disabledModels`` is consulted only by the catalog
routes (``/api/models``, ``/api/v1/models``); the chat/search/fetch handlers
never read it, so it excludes a model from listings but does NOT stop the
router from selecting it inside a combo.

Consequences, which this module implements rather than simulates:

* Whole-combo ON/OFF has no engine mechanism. Emptying ``models`` would make
  the combo unresolvable, which is destruction, not suspension, so the UI
  discloses combo suspension as unsupported instead of faking it.
* Per-model ON/OFF inside a combo IS achievable and routing-enforced: the
  engine routes exactly the published array, so a locally disabled member is
  absent from ``models`` and therefore can never be selected.

Projection architecture
-----------------------
``configured`` (the full ordered sequence, including disabled members) is the
authoritative local record; ``disabled`` names the members excluded from it.
The published array is ``projection()`` -- configured order preserved, disabled
members removed. Nothing is ever deleted: re-enabling restores the member at
its original position because the position lives in ``configured``.

Every engine read is reconciled against the last verified projection. An
externally shortened engine list is NEVER read as "the operator deleted these";
it is reported as drift and resolved by the operator.
"""
from __future__ import annotations

import json
import os
import threading
from dataclasses import dataclass, field, replace
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple

import config as _config

#: Bumped when the on-disk shape changes; an unknown version is quarantined
#: rather than guessed at.
SCHEMA_VERSION = 1


def default_store_path() -> Path:
    """Resolved at construction, not import, so a test can redirect it."""
    return Path(_config.COMBO_ACTIVATION_FILE)


class ActivationStatus(str, Enum):
    """Where one model row stands between local intent and the engine."""

    CONFIGURED_ENABLED = "CONFIGURED_ENABLED"
    CONFIGURED_DISABLED = "CONFIGURED_DISABLED"
    ENABLED_BUT_BLOCKED = "ENABLED_BUT_BLOCKED"
    UNTESTED = "UNTESTED"
    UNSAVED = "UNSAVED"
    APPLIED = "APPLIED"


class ReconcileStatus(str, Enum):
    """Result of comparing an engine read against the local record."""

    NEW = "NEW"
    CLEAN = "CLEAN"
    PENDING_DRAFT = "PENDING_DRAFT"
    MISSING = "MISSING"
    EXTERNAL_REENABLED = "EXTERNAL_REENABLED"
    EXTERNAL_REMOVED = "EXTERNAL_REMOVED"
    EXTERNAL_ADDED = "EXTERNAL_ADDED"
    EXTERNAL_REORDERED = "EXTERNAL_REORDERED"
    EXTERNAL_REPLACED = "EXTERNAL_REPLACED"


class ActivationStoreError(Exception):
    """Any failed activation-store persistence stage, never swallowed."""

    def __init__(self, stage: str, path: Any, cause: Optional[BaseException] = None):
        self.stage = stage
        self.path = str(path)
        self.cause = cause
        message = f"Activation persistence failed at stage '{stage}' ({self.path})"
        if cause is not None:
            message += f": {type(cause).__name__}: {cause}"
        super().__init__(message)


class ReconcileConflict(Exception):
    """Raised by ``apply`` when the engine moved since the verified baseline."""

    def __init__(self, message: str, details: Sequence[str] = ()):
        super().__init__(message)
        self.details = list(details)


@dataclass
class ComboActivation:
    """Authoritative configured state for one combo, plus its verification."""

    combo_id: str
    combo_name: str = ""
    #: FULL ordered sequence, disabled members included. Authoritative.
    configured: List[str] = field(default_factory=list)
    #: Canonical ids disabled within THIS combo only. Never global.
    disabled: Set[str] = field(default_factory=set)
    #: What the engine last VERIFIED as published, with the server stamp.
    engine_baseline: List[str] = field(default_factory=list)
    engine_updated_at: str = ""

    # ------------------------------------------------------------- state
    def clone(self) -> "ComboActivation":
        return replace(
            self,
            configured=list(self.configured),
            disabled=set(self.disabled),
            engine_baseline=list(self.engine_baseline),
        )

    def enabled_models(self) -> List[str]:
        """Configured sequence minus the locally disabled members."""
        return projection(self.configured, self.disabled)

    def disabled_models(self) -> List[str]:
        """Disabled members in their configured order."""
        return [m for m in self.configured if m in self.disabled]

    def has_pending_changes(self) -> bool:
        """True when the projection differs from the verified baseline."""
        return self.enabled_models() != list(self.engine_baseline)

    def is_enabled(self, canonical_id: str) -> bool:
        return canonical_id not in self.disabled

    # ------------------------------------------------------------- mutation
    def set_enabled(self, canonical_id: str, enabled: bool) -> bool:
        """Set one member's activation. Returns True when state changed.

        Unknown ids are ignored rather than invented: activation only ever
        describes a member the combo already has.
        """
        if canonical_id not in self.configured:
            return False
        changed = (enabled and canonical_id in self.disabled) or (
            not enabled and canonical_id not in self.disabled
        )
        if enabled:
            self.disabled.discard(canonical_id)
        else:
            self.disabled.add(canonical_id)
        return changed

    def set_many(self, canonical_ids: Sequence[str], enabled: bool) -> List[str]:
        """Set activation for several members; returns the ids that changed."""
        return [c for c in canonical_ids if self.set_enabled(c, enabled)]

    def toggle(self, canonical_ids: Sequence[str]) -> List[str]:
        """Flip activation for each member; returns the ids that changed."""
        changed: List[str] = []
        for cid in canonical_ids:
            if cid in self.configured and self.set_enabled(cid, self.is_enabled(cid)):
                changed.append(cid)
        return changed

    def reset_activation(self) -> List[str]:
        """Re-enable every member. Returns the ids that were disabled."""
        restored = self.disabled_models()
        self.disabled.clear()
        return restored

    def add_model(self, canonical_id: str, index: Optional[int] = None) -> bool:
        """Add a member to the CONFIGURED sequence (enabled by default)."""
        if canonical_id in self.configured:
            return False
        if index is not None and 0 <= index <= len(self.configured):
            self.configured.insert(index, canonical_id)
        else:
            self.configured.append(canonical_id)
        return True

    def remove_model(self, canonical_id: str) -> bool:
        """Remove a member from the configured sequence (the destructive action).

        Distinct from disable, which never changes the configured sequence.
        """
        if canonical_id in self.configured:
            self.configured.remove(canonical_id)
            self.disabled.discard(canonical_id)
            return True
        return False

    def reorder(self, canonical_id: str, new_index: int) -> bool:
        if canonical_id not in self.configured:
            return False
        idx = self.configured.index(canonical_id)
        if idx == new_index:
            return False
        self.configured.pop(idx)
        self.configured.insert(new_index, canonical_id)
        return True

    def move_up(self, canonical_id: str) -> bool:
        idx = self.configured.index(canonical_id) if canonical_id in self.configured else -1
        return self.reorder(canonical_id, idx - 1) if idx > 0 else False

    def move_down(self, canonical_id: str) -> bool:
        idx = self.configured.index(canonical_id) if canonical_id in self.configured else -1
        length = len(self.configured)
        return self.reorder(canonical_id, idx + 1) if 0 <= idx < length - 1 else False

    def adopt_external(self, server_models: Sequence[str]) -> None:
        """KEEP-SERVER resolution: the engine becomes the configured truth.

        A previously disabled member the engine no longer lists is dropped,
        because the operator explicitly chose the server state.
        """
        self.configured = list(server_models)
        self.disabled = {m for m in self.disabled if m not in self.configured}

    # ------------------------------------------------------------- counts
    def counts(self, routable_ids: Optional[Set[str]] = None) -> Dict[str, int]:
        """Configured / enabled / routable / disabled counts for this combo.

        ``routable_ids`` are the ids the ENGINE will actually select
        (enabled members that are not blocked). Omitted when the caller has no
        blocking evidence -- enabled and routable then coincide, and the label
        says so rather than inventing a number.
        """
        enabled = self.enabled_models()
        result = {
            "configured": len(self.configured),
            "enabled": len(enabled),
            "disabled": len(self.configured) - len(enabled),
        }
        result["routable"] = (
            sum(1 for m in enabled if m in routable_ids) if routable_ids is not None
            else len(enabled)
        )
        return result

    # --------------------------------------------------------- persistence
    def to_dict(self) -> Dict[str, Any]:
        return {
            "combo_id": self.combo_id,
            "combo_name": self.combo_name,
            "configured": list(self.configured),
            "disabled": sorted(self.disabled),
            "engine_baseline": list(self.engine_baseline),
            "engine_updated_at": self.engine_updated_at,
        }

    @classmethod
    def from_dict(cls, raw: Any) -> "ComboActivation":
        if not isinstance(raw, dict):
            raise ValueError("activation record is not a mapping")
        combo_id = raw.get("combo_id")
        if not isinstance(combo_id, str) or not combo_id.strip():
            raise ValueError("activation record has no combo_id")
        configured = raw.get("configured") or []
        baseline = raw.get("engine_baseline") or []
        disabled = raw.get("disabled") or []
        if not isinstance(configured, list) or not all(isinstance(m, str) for m in configured):
            raise ValueError("configured must be a list of strings")
        if not isinstance(baseline, list) or not all(isinstance(m, str) for m in baseline):
            raise ValueError("engine_baseline must be a list of strings")
        if not isinstance(disabled, list) or not all(isinstance(m, str) for m in disabled):
            raise ValueError("disabled must be a list of strings")
        return cls(
            combo_id=combo_id,
            combo_name=str(raw.get("combo_name") or ""),
            configured=list(configured),
            disabled=set(disabled),
            engine_baseline=list(baseline),
            engine_updated_at=str(raw.get("engine_updated_at") or ""),
        )


def projection(configured: Sequence[str], disabled: Set[str]) -> List[str]:
    """The engine-published array: configured order, disabled members removed."""
    return [m for m in configured if m not in disabled]


@dataclass
class ReconcileResult:
    status: ReconcileStatus
    changes: List[str] = field(default_factory=list)
    record: Optional[ComboActivation] = None

    @property
    def is_conflict(self) -> bool:
        """Drift that needs an operator decision, not a silent merge."""
        return self.status in (
            ReconcileStatus.MISSING,
            ReconcileStatus.EXTERNAL_REENABLED,
            ReconcileStatus.EXTERNAL_REMOVED,
            ReconcileStatus.EXTERNAL_ADDED,
            ReconcileStatus.EXTERNAL_REORDERED,
            ReconcileStatus.EXTERNAL_REPLACED,
        )

    @property
    def is_draft(self) -> bool:
        """Local intent that has not been published yet."""
        return self.status == ReconcileStatus.PENDING_DRAFT


def reconcile(
    record: Optional[ComboActivation],
    server_models: Optional[Sequence[str]],
    server_updated_at: str = "",
) -> ReconcileResult:
    """Compare one engine read against the local activation record.

    The asymmetry matters: an engine list SHORTER than the local projection is
    always reported as drift, never as an intentional deletion of members the
    operator disabled.
    """
    if server_models is None:
        return ReconcileResult(
            ReconcileStatus.MISSING,
            ["combo not present in the engine read-back"],
            record,
        )

    server = list(server_models)
    if record is None:
        return ReconcileResult(ReconcileStatus.NEW, [], None)

    expected = record.enabled_models()
    if server == expected:
        return ReconcileResult(ReconcileStatus.CLEAN, [], record)
    if server == list(record.engine_baseline):
        # Engine unchanged, local draft pending: not drift, just unsaved work.
        return ReconcileResult(
            ReconcileStatus.PENDING_DRAFT,
            ["unsaved activation changes not yet applied"],
            record,
        )

    configured_set = set(record.configured)
    server_set = set(server)
    disabled = sorted(record.disabled)
    changes: List[str] = []

    if server == list(record.configured):
        changes.append(
            "engine has every configured model; locally disabled members were "
            f"re-enabled outside WatchEdit: {', '.join(disabled) or '(none)'}"
        )
        return ReconcileResult(ReconcileStatus.EXTERNAL_REENABLED, changes, record)

    if configured_set - server_set:
        missing = [m for m in record.configured if m not in server_set]
        changes.append(f"engine is missing models: {', '.join(missing)}")
    if server_set - configured_set:
        added = [m for m in server if m not in configured_set]
        changes.append(f"engine has models WatchEdit does not configure: {', '.join(added)}")

    if not changes:
        if sorted(server_set) == sorted(configured_set):
            changes.append("model order changed outside WatchEdit")
            return ReconcileResult(ReconcileStatus.EXTERNAL_REORDERED, changes, record)
        changes.append("engine model list was replaced outside WatchEdit")
        return ReconcileResult(ReconcileStatus.EXTERNAL_REPLACED, changes, record)

    return ReconcileResult(
        ReconcileStatus.EXTERNAL_REMOVED
        if configured_set - server_set
        else ReconcileStatus.EXTERNAL_ADDED,
        changes,
        record,
    )


class ActivationStore:
    """Durable, authoritative per-combo activation state.

    Lives outside the repository (private storage), is written atomically
    (temp + flush + fsync + replace) and quarantines an unreadable source
    rather than overwriting it, matching the HealthCache persistence contract.
    """

    QUARANTINE_SUFFIX = ".corrupt"

    def __init__(self, path: Optional[Path] = None):
        self.path = Path(path) if path is not None else default_store_path()
        self._lock = threading.RLock()
        self._save_lock = threading.Lock()
        self.records: Dict[str, ComboActivation] = {}
        self.load_state: str = "ABSENT"
        self.load_error: str = ""
        self.quarantine_path: Optional[str] = None
        self.load()

    # ------------------------------------------------------------- loading
    def _quarantine_target(self) -> Path:
        return self.path.with_name(self.path.name + self.QUARANTINE_SUFFIX)

    def _corrupt(self, state: str, error: BaseException, raw: bytes) -> None:
        self.records = {}
        self.load_state = state
        self.load_error = f"{type(error).__name__}: {error}"
        self.quarantine_path = None
        target = self._quarantine_target()
        try:
            target.write_bytes(raw)
        except OSError as quarantine_failure:
            self.load_error += (
                "; quarantine failed, original bytes left untouched: "
                f"{type(quarantine_failure).__name__}: {quarantine_failure}"
            )
            return
        self.quarantine_path = str(target)
        try:
            self.path.unlink()
        except OSError as unlink_failure:
            self.load_error += (
                "; corrupt source could not be moved aside: "
                f"{type(unlink_failure).__name__}: {unlink_failure}"
            )

    def load(self) -> None:
        if not self.path.is_file():
            self.load_state = "ABSENT"
            return
        try:
            raw_bytes = self.path.read_bytes()
        except OSError as ex:
            self.records = {}
            self.load_state = "IO_ERROR"
            self.load_error = f"{type(ex).__name__}: {ex}"
            return
        try:
            payload = json.loads(raw_bytes.decode("utf-8-sig"))
        except (ValueError, UnicodeDecodeError) as ex:
            self._corrupt("MALFORMED_JSON", ex, raw_bytes)
            return
        if not isinstance(payload, dict) or payload.get("schema") != SCHEMA_VERSION:
            self._corrupt("INVALID_RECORD", ValueError("unknown activation schema"), raw_bytes)
            return
        records: Dict[str, ComboActivation] = {}
        for raw_record in (payload.get("combos") or []):
            try:
                record = ComboActivation.from_dict(raw_record)
            except ValueError as ex:
                # One bad record must not cost the operator every other combo's
                # configured sequence.
                self._corrupt("INVALID_RECORD", ex, raw_bytes)
                return
            records[record.combo_id] = record
        self.records = records
        self.load_state = "LOADED"

    # ------------------------------------------------------------- access
    def get(self, combo_id: str) -> Optional[ComboActivation]:
        with self._lock:
            return self.records.get(combo_id)

    def all_records(self) -> Dict[str, ComboActivation]:
        with self._lock:
            return dict(self.records)

    def ensure(self, combo_id: str, combo_name: str, server_models: Sequence[str]) -> ComboActivation:
        """Return the record for a combo, seeding it from the engine if new."""
        with self._lock:
            record = self.records.get(combo_id)
            if record is None:
                record = ComboActivation(
                    combo_id=combo_id,
                    combo_name=combo_name,
                    configured=list(server_models),
                    disabled=set(),
                    engine_baseline=list(server_models),
                )
                self.records[combo_id] = record
            elif not record.combo_name:
                record.combo_name = combo_name
            return record

    def forget(self, combo_id: str) -> bool:
        """Drop a combo's activation state. Called on delete, so no orphan can
        later affect a different combo that reuses the name."""
        with self._lock:
            return self.records.pop(combo_id, None) is not None

    def prune_absent(self, present_ids: Set[str]) -> List[str]:
        """Drop records for combos the engine no longer lists."""
        with self._lock:
            gone = [cid for cid in self.records if cid not in present_ids]
            for cid in gone:
                del self.records[cid]
            return gone

    def verify_baseline(self, record: ComboActivation, server_models: Sequence[str],
                        server_updated_at: str = "") -> None:
        """Adopt a VERIFIED engine read as the record's baseline."""
        record.engine_baseline = list(server_models)
        if server_updated_at:
            record.engine_updated_at = server_updated_at

    # ---------------------------------------------------------- persistence
    def save(self) -> None:
        """Atomic write of the whole store; raises on any failed stage."""
        with self._lock:
            payload = {
                "schema": SCHEMA_VERSION,
                "combos": [r.to_dict() for r in self.records.values()],
            }
            text = json.dumps(payload, indent=2, sort_keys=True)
        with self._save_lock:
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
            except OSError as ex:
                raise ActivationStoreError("mkdir", self.path.parent, ex) from ex
            temp = self.path.with_suffix(".tmp")
            try:
                with open(temp, "w", encoding="utf-8") as handle:
                    try:
                        handle.write(text)
                        handle.flush()
                    except OSError as ex:
                        raise ActivationStoreError("write", temp, ex) from ex
                    try:
                        os.fsync(handle.fileno())
                    except OSError as ex:
                        raise ActivationStoreError("fsync", temp, ex) from ex
            except ActivationStoreError:
                raise
            except OSError as ex:
                raise ActivationStoreError("write", temp, ex) from ex
            try:
                os.replace(temp, self.path)
            except OSError as ex:
                raise ActivationStoreError("replace", self.path, ex) from ex


# --------------------------------------------------------------------------
# Row classification: configured vs effective.
# --------------------------------------------------------------------------
def classify_row(
    canonical_id: str,
    record: ComboActivation,
    *,
    blocked: bool = False,
    blocked_reason: str = "",
    tested: bool = True,
    pending: bool = False,
) -> Tuple[str, str]:
    """Return ``(status, reason)`` for one model row.

    Manual activation and effective routability are separate answers: a member
    can be ON and still not selectable, and that is always stated rather than
    hidden behind a checked box.
    """
    if not record.is_enabled(canonical_id):
        return ActivationStatus.CONFIGURED_DISABLED.value, "manually disabled in this combo"
    if pending:
        return ActivationStatus.UNSAVED.value, "activation change not yet applied"
    if blocked:
        return ActivationStatus.ENABLED_BUT_BLOCKED.value, blocked_reason or "not routable"
    if not tested:
        return ActivationStatus.UNTESTED.value, "never tested"
    return ActivationStatus.CONFIGURED_ENABLED.value, ""


#: Activation filters offered for the model explorer.
ACTIVATION_ALL = "all"
ACTIVATION_ENABLED = "enabled"
ACTIVATION_DISABLED = "disabled"
ACTIVATION_ROUTABLE = "routable"
ACTIVATION_BLOCKED = "blocked"
ACTIVATION_PENDING = "pending"
ACTIVATION_OPTIONS: Tuple[str, ...] = (
    ACTIVATION_ALL,
    ACTIVATION_ENABLED,
    ACTIVATION_DISABLED,
    ACTIVATION_ROUTABLE,
    ACTIVATION_BLOCKED,
    ACTIVATION_PENDING,
)

#: Combo activation filters. Only states the engine can actually represent are
#: offered; "Disabled" is retained because it is a supported query over the
#: operator's own state, not a claim about engine support.
COMBO_ACTIVATION_ALL = "all"
COMBO_ACTIVATION_ENABLED = "enabled"
COMBO_ACTIVATION_DISABLED = "disabled"
COMBO_ACTIVATION_NO_ROUTE = "no-route"
COMBO_ACTIVATION_OPTIONS: Tuple[str, ...] = (
    COMBO_ACTIVATION_ALL,
    COMBO_ACTIVATION_ENABLED,
    COMBO_ACTIVATION_DISABLED,
    COMBO_ACTIVATION_NO_ROUTE,
)


# --------------------------------------------------------------------------
# One runnable self-check for the non-trivial logic above (no framework).
# --------------------------------------------------------------------------
def _self_check() -> None:  # pragma: no cover - run via __main__
    rec = ComboActivation(
        combo_id="c1", combo_name="X",
        configured=["a", "b", "c"], disabled={"b"},
        engine_baseline=["a", "b", "c"],
    )
    assert projection(rec.configured, rec.disabled) == ["a", "c"]
    assert rec.has_pending_changes() is True
    assert rec.counts() == {"configured": 3, "enabled": 2, "disabled": 1, "routable": 2}

    # Re-enabling restores the original position, it does not append.
    rec.set_enabled("b", True)
    assert rec.enabled_models() == ["a", "b", "c"]
    assert rec.has_pending_changes() is False

    # Unchanged engine + local draft is unsaved work, not drift.
    rec.set_enabled("a", False)
    assert rec.enabled_models() == ["b", "c"]
    result = reconcile(rec, ["a", "b", "c"], "u2")
    assert result.is_draft and not result.is_conflict

    # Engine == the pending projection is clean.
    assert reconcile(rec, ["b", "c"], "u2").status == ReconcileStatus.CLEAN

    # An externally shortened engine list is drift, never silent deletion.
    result = reconcile(rec, ["c"], "u2")
    assert result.is_conflict and result.status == ReconcileStatus.EXTERNAL_REMOVED

    # Engine == configured means someone re-enabled everything externally.
    rec.engine_baseline = ["b", "c"]
    result = reconcile(rec, ["a", "b", "c"], "u3")
    assert result.status == ReconcileStatus.EXTERNAL_REENABLED

    rec.engine_baseline = ["a", "b", "c"]
    rec.set_enabled("a", True)
    assert reconcile(rec, ["a", "b", "c"], "u4").status == ReconcileStatus.CLEAN

    # Disabled state is per combo: combo B is untouched by combo A.
    other = ComboActivation(combo_id="c2", configured=["a", "b"], disabled=set())
    assert other.is_enabled("a") and other.is_enabled("b")

    # Unknown ids are never invented by activation.
    assert rec.set_enabled("nope/never", False) is False

    print("activation self-check OK")


if __name__ == "__main__":  # pragma: no cover
    _self_check()