"""
9router_WatchEdit - Stable Combo Model, Diff Engine & Preset Manager
Maintains canonical identity decoupling from widget rows, computes clear visual diffs,
and manages local fallback presets.
"""
from collections import Counter
from dataclasses import dataclass, field, asdict
from datetime import datetime
import enum
import json
import os
from pathlib import Path
from typing import Dict, List, Optional, Tuple, Any

from config import PRESETS_FILE
from core.history import HealthCache
from core.classification import HealthState

@dataclass
class ComboDiff:
    removed: List[str] = field(default_factory=list)
    added: List[Tuple[str, int]] = field(default_factory=list)  # (model, new_index)
    moved: List[Tuple[str, int, int]] = field(default_factory=list)  # (model, old_idx, new_idx)

    def is_empty(self) -> bool:
        return not self.removed and not self.added and not self.moved

    def format_text(self) -> str:
        lines = []
        if self.removed:
            lines.append("REMOVE:")
            for m in self.removed:
                lines.append(f"  - {m}")
        if self.added:
            lines.append("ADD:")
            for m, idx in self.added:
                lines.append(f"  + {m} (at #{idx + 1})")
        if self.moved:
            lines.append("MOVE:")
            for m, old_i, new_i in self.moved:
                lines.append(f"  ~ {m}: #{old_i + 1} -> #{new_i + 1}")
        if not lines:
            return "No changes detected."
        return "\n".join(lines)

@dataclass
class Preset:
    name: str
    models: List[str]
    description: str = ""
    updated_at: str = ""

DEFAULT_PRESETS = {
    "SAIFREN_FREE": [
        "cog/swe-1.6-slow",
        "ag/gemini-3.8-flash-high",
        "wb/hy3",
        "bai/deepseek-v4-flash",
        "oc/muse-spark-1.3-contributor-free",
        "moyuu/deepseek-v4-flash",
        "oc/deepseek-v4-flash-free",
    ],
    "SAIFREN_FAST": [
        "ag/gemini-3.8-flash-high",
        "cl/z-ai/glm-5.3-flash",
        "cl/deepseek/deepseek-v4-flash",
        "wb/hy4-preview",
        "moyuu/glm-5.3-flash",
    ],
    "CODING_FREE": [
        "cog/swe-1.6-slow",
        "ag/gemini-3.8-flash-high",
        "wb/hy3",
        "cl/deepseek/deepseek-v4-flash",
        "ocg/deepseek-v4-flash",
        "dahl/deepseek-ai/DeepSeek-V4-Flash-0731",
    ],
    "EMERGENCY_FALLBACK": [
        "ag/gemini-3.8-flash-high",
        "wb/hy3",
        "gorouter/claude-opus-5-thinking",
        "tb/claude-opus-5-thinking",
    ],
}

class StableCombo:
    """
    Mutable in-memory representation of a 9Router combo.
    Enforces stable model identity and canonical ordering.
    """
    def __init__(
        self,
        combo_id: str,
        name: str,
        models: List[str],
        kind: Optional[str] = None,
        updated_at: str = "",
        baseline_models: Optional[List[str]] = None,
        baseline_updated_at: Optional[str] = None,
        baseline_name: Optional[str] = None,
        baseline_kind: Optional[str] = None,
    ):
        self.id = combo_id
        self.name = name
        self.models: List[str] = list(models)
        self.kind = kind
        self.updated_at = updated_at
        self.baseline_models: List[str] = list(baseline_models) if baseline_models is not None else list(models)
        self.baseline_updated_at: str = baseline_updated_at if baseline_updated_at is not None else updated_at
        self.baseline_name: str = baseline_name if baseline_name is not None else name
        self.baseline_kind: Optional[str] = baseline_kind if baseline_kind is not None else kind

    def clone(self) -> "StableCombo":
        return StableCombo(
            combo_id=self.id,
            name=self.name,
            models=list(self.models),
            kind=self.kind,
            updated_at=self.updated_at,
            baseline_models=list(self.baseline_models),
            baseline_updated_at=self.baseline_updated_at,
            baseline_name=self.baseline_name,
            baseline_kind=self.baseline_kind,
        )

    def mark_clean(self, new_updated_at: str = ""):
        """Marks current local state (name, kind, models) as clean baseline."""
        self.baseline_models = list(self.models)
        self.baseline_name = self.name
        self.baseline_kind = self.kind
        if new_updated_at:
            self.baseline_updated_at = new_updated_at
            self.updated_at = new_updated_at

    def has_unsaved_changes(self) -> bool:
        """Returns True if local model sequence, name, or kind differs from baseline."""
        return (
            self.models != self.baseline_models
            or self.name != self.baseline_name
            or self.kind != self.baseline_kind
        )

    @staticmethod
    def _normalize_kind(kind: Optional[str]) -> Optional[str]:
        return kind or None

    def server_field_mismatches(
        self,
        server_name: Optional[str],
        server_kind: Optional[str],
        server_models: Optional[List[str]],
    ) -> List[str]:
        """CORE-004: fields where read-back diverges from the local applied state.

        Returns the names of mismatching mutable fields (``name``, ``kind``,
        ``models``); an empty list means full equality. Kind is compared with
        the project's normalization (None and "" are equivalent). This is the
        complete APPLY/VERIFY contract used before ``mark_clean``: a partial
        backend write/normalization must not be laundered into a clean baseline.
        """
        mismatches: List[str] = []
        if server_name != self.name:
            mismatches.append("name")
        if self._normalize_kind(server_kind) != self._normalize_kind(self.kind):
            mismatches.append("kind")
        if list(server_models or []) != list(self.models):
            mismatches.append("models")
        return mismatches

    def check_server_conflict(
        self,
        server_name: str,
        server_kind: Optional[str],
        server_models: List[str],
        server_updated_at: str = "",
    ) -> bool:
        """
        Returns True if server state diverges from local baseline (optimistic concurrency conflict).
        Checks models, name, kind, and updatedAt (if present).
        """
        if server_models != self.baseline_models:
            return True
        if server_name != self.baseline_name:
            return True
        if (server_kind or None) != (self.baseline_kind or None):
            return True
        if server_updated_at and self.baseline_updated_at and server_updated_at != self.baseline_updated_at:
            return True
        return False

    def describe_server_divergence(
        self,
        server_name: str,
        server_kind: Optional[str],
        server_models: List[str],
        server_updated_at: str = "",
    ) -> List[str]:
        """Human-readable field-level divergence between server state and local baseline."""
        changes: List[str] = []
        if server_name != self.baseline_name:
            changes.append(f"name '{self.baseline_name}' -> '{server_name}'")
        if (server_kind or None) != (self.baseline_kind or None):
            changes.append(f"kind '{self.baseline_kind}' -> '{server_kind}'")
        if server_models != self.baseline_models:
            baseline_set = set(self.baseline_models)
            server_set = set(server_models)
            removed = [m for m in self.baseline_models if m not in server_set]
            added = [m for m in server_models if m not in baseline_set]
            if removed:
                changes.append(f"models removed: {', '.join(removed)}")
            if added:
                changes.append(f"models added: {', '.join(added)}")
            if not removed and not added:
                changes.append("model order changed")
        if server_updated_at and self.baseline_updated_at and server_updated_at != self.baseline_updated_at:
            changes.append(f"updatedAt '{self.baseline_updated_at}' -> '{server_updated_at}'")
        return changes

    def revert_unsaved_changes(self):
        """Reverts local state to baseline sequence."""
        self.models = list(self.baseline_models)
        self.name = self.baseline_name
        self.kind = self.baseline_kind

    def add_model(self, canonical_id: str, index: Optional[int] = None) -> bool:
        """Adds a model if not already present, or moves it to specified index."""
        if canonical_id in self.models:
            return False
        if index is not None and 0 <= index <= len(self.models):
            self.models.insert(index, canonical_id)
        else:
            self.models.append(canonical_id)
        return True

    def remove_model(self, canonical_id: str) -> bool:
        if canonical_id in self.models:
            self.models.remove(canonical_id)
            return True
        return False

    def remove_models(self, canonical_ids: List[str]) -> int:
        remaining = Counter(canonical_ids)
        kept = []
        removed = 0
        for model in self.models:
            if remaining[model] > 0:
                remaining[model] -= 1
                removed += 1
            else:
                kept.append(model)
        if removed:
            self.models[:] = kept
        return removed

    def move_up(self, canonical_id: str) -> bool:
        if canonical_id not in self.models:
            return False
        idx = self.models.index(canonical_id)
        if idx > 0:
            self.models[idx], self.models[idx - 1] = self.models[idx - 1], self.models[idx]
            return True
        return False

    def move_down(self, canonical_id: str) -> bool:
        if canonical_id not in self.models:
            return False
        idx = self.models.index(canonical_id)
        if idx < len(self.models) - 1:
            self.models[idx], self.models[idx + 1] = self.models[idx + 1], self.models[idx]
            return True
        return False

    def reorder_model(self, canonical_id: str, new_index: int) -> bool:
        if canonical_id not in self.models:
            return False
        idx = self.models.index(canonical_id)
        if idx == new_index:
            return False
        self.models.pop(idx)
        self.models.insert(new_index, canonical_id)
        return True

    def move_healthy_to_top(self, health_cache: HealthCache) -> ComboDiff:
        """Moves healthy models (FREE_USE, PAID) to the top, maintaining relative order."""
        old_list = list(self.models)
        healthy = []
        unhealthy = []
        for m in self.models:
            rec = health_cache.get(m)
            if rec and rec.is_healthy():
                healthy.append(m)
            else:
                unhealthy.append(m)
        self.models = healthy + unhealthy
        return compute_combo_diff(old_list, self.models)

    def remove_dead_models(self, health_cache: HealthCache) -> ComboDiff:
        """Removes models marked DEAD by health cache."""
        old_list = list(self.models)
        self.models = [m for m in self.models if not (health_cache.get(m) and health_cache.get(m).is_dead())]
        return compute_combo_diff(old_list, self.models)

def compute_combo_diff(original: List[str], modified: List[str]) -> ComboDiff:
    """Computes detailed diff between two model lists."""
    orig_set = set(original)
    mod_set = set(modified)
    orig_index = {}
    for index, model in enumerate(original):
        orig_index.setdefault(model, index)
    mod_index = {}
    for index, model in enumerate(modified):
        mod_index.setdefault(model, index)

    removed = [m for m in original if m not in mod_set]
    added = [(m, mod_index[m]) for m in modified if m not in orig_set]

    # Duplicate semantics are intentional: removed/added preserve every list
    # occurrence, and moved repeats once per original occurrence while using
    # the first index for that model, matching list.index's old behavior.
    common_orig = [m for m in original if m in mod_set]
    moved = []
    for m in common_orig:
        old_idx = orig_index[m]
        new_idx = mod_index[m]
        if old_idx != new_idx:
            moved.append((m, old_idx, new_idx))

    return ComboDiff(removed=removed, added=added, moved=moved)

class PresetPersistenceError(Exception):
    """CORE-002: ONE truthful contract for preset persistence.

    Raised on ANY failed persistence stage instead of returning normally:
    serialization, parent-directory creation, temporary-file write/flush/fsync,
    or the atomic replace. ``stage`` names the stage, ``path`` the file it
    applies to, and ``cleanup_error`` (when set) records that the temporary
    artifact could not be removed after the primary failure -- the primary
    failure is always the raised one, never swallowed.
    """

    def __init__(
        self,
        stage: str,
        path: Any,
        cause: Optional[BaseException] = None,
        cleanup_error: Optional[BaseException] = None,
    ):
        self.stage = stage
        self.path = str(path)
        self.cause = cause
        self.cleanup_error = cleanup_error
        message = f"Preset persistence failed at stage '{stage}' ({self.path})"
        if cause is not None:
            message += f": {type(cause).__name__}: {cause}"
        if cleanup_error is not None:
            message += (
                f"; temporary-file cleanup also failed: "
                f"{type(cleanup_error).__name__}: {cleanup_error}"
            )
        super().__init__(message)


class PresetLoadState(str, enum.Enum):
    """CORE-002: distinguishable PresetManager.load() outcomes.

    ABSENT (seed defaults) is materially different from MALFORMED/UNREADABLE
    (preserve or quarantine the existing bytes, then seed). A malformed file
    is never silently treated as absent and overwritten.
    """

    ABSENT = "ABSENT"                    # no presets file at all -> seed defaults
    LOADED = "LOADED"                    # valid presets loaded
    MALFORMED_JSON = "MALFORMED_JSON"    # malformed / truncated JSON
    INVALID_RECORD = "INVALID_RECORD"    # bad preset structure
    IO_ERROR = "IO_ERROR"                # file exists but could not be read


class PresetManager:
    QUARANTINE_SUFFIX = ".corrupt"

    def __init__(self, presets_file: Path = PRESETS_FILE):
        self.presets_file = presets_file
        self.presets: Dict[str, Preset] = {}
        # CORE-002 explicit load state: always inspectable, never implied.
        self.load_state: PresetLoadState = PresetLoadState.ABSENT
        self.load_error: str = ""
        self.quarantine_path: Optional[str] = None
        self.load()

    # ------------------------------------------------------------- loading
    def _quarantine_target(self) -> Path:
        """Deterministic recovery name (first free when one already exists)."""
        base = self.presets_file.with_name(self.presets_file.name + self.QUARANTINE_SUFFIX)
        if not base.exists():
            return base
        n = 2
        while True:
            candidate = base.with_name(base.name + f".{n}")
            if not candidate.exists():
                return candidate
            n += 1

    def _seed_defaults(self) -> None:
        now = datetime.now().isoformat()
        for name, models in DEFAULT_PRESETS.items():
            self.presets[name] = Preset(
                name=name,
                models=models,
                description=f"Built-in preset: {name}",
                updated_at=now,
            )

    def load(self) -> PresetLoadState:
        """Load presets, reporting ONE explicit outcome.

        ABSENT seeds defaults. MALFORMED/UNREADABLE preserves the original
        bytes (quarantine) before any replacement; if the bytes cannot be
        preserved, the source is left untouched, defaults live only in memory,
        and the state surfaces the failure. Defaults are never written over a
        file whose bytes could not first be preserved.
        """
        self.load_state = PresetLoadState.ABSENT
        self.load_error = ""
        self.quarantine_path = None
        self.presets = {}

        if not self.presets_file.exists():
            self._seed_defaults()
            self.load_state = PresetLoadState.ABSENT
            self._try_seed_persist()
            return self.load_state

        try:
            raw_bytes = self.presets_file.read_bytes()
        except OSError as ex:
            self.load_state = PresetLoadState.IO_ERROR
            self.load_error = f"{type(ex).__name__}: {ex}"
            self._seed_defaults()
            return self.load_state

        try:
            data = json.loads(raw_bytes.decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as ex:
            self.load_state = PresetLoadState.MALFORMED_JSON
            self.load_error = f"{type(ex).__name__}: {ex}"
            return self._preserve_and_seed(raw_bytes)

        if not isinstance(data, dict):
            self.load_state = PresetLoadState.INVALID_RECORD
            self.load_error = "presets root is not a JSON object"
            return self._preserve_and_seed(raw_bytes)

        try:
            parsed: Dict[str, Preset] = {}
            for name, item in data.items():
                if not isinstance(item, dict):
                    raise ValueError(f"preset {name!r} is not an object")
                parsed[str(name)] = Preset(
                    name=str(name),
                    models=item.get("models", []),
                    description=item.get("description", ""),
                    updated_at=item.get("updated_at", ""),
                )
        except (TypeError, ValueError) as ex:
            self.load_state = PresetLoadState.INVALID_RECORD
            self.load_error = f"{type(ex).__name__}: {ex}"
            return self._preserve_and_seed(raw_bytes)

        self.presets = parsed
        self.load_state = PresetLoadState.LOADED
        return self.load_state

    def _preserve_and_seed(self, raw_bytes: bytes) -> PresetLoadState:
        """Preserve malformed bytes, then seed in memory. Never destroy evidence.

        If the bytes cannot be preserved, the source is left exactly as it was
        and defaults stay memory-only (no save over unpreserved corruption).
        """
        target = self._quarantine_target()
        try:
            target.write_bytes(raw_bytes)
        except OSError as quarantine_failure:
            self._seed_defaults()
            self.quarantine_path = None
            self.load_error += (
                "; quarantine failed, original bytes left untouched: "
                f"{type(quarantine_failure).__name__}: {quarantine_failure}"
            )
            return self.load_state
        self.quarantine_path = str(target)
        self._seed_defaults()
        self._try_seed_persist()
        return self.load_state

    def _try_seed_persist(self) -> None:
        """Seed persistence must not crash construction (CORE-001 degrade);
        the failure is surfaced via ``load_error`` and explicit saves raise."""
        try:
            self.save()
        except PresetPersistenceError as ex:
            self.load_error = (self.load_error + "; " if self.load_error else "") + str(ex)

    # ------------------------------------------------------------- saving
    def save(self) -> None:
        """Atomically persist presets.

        Success returns None; ANY failure raises PresetPersistenceError naming
        the failed stage. Failures are never swallowed and a failed write can
        never look like success. A leftover temporary file is never treated as
        authoritative state.
        """
        try:
            data = {name: asdict(p) for name, p in self.presets.items()}
        except Exception as ex:
            raise PresetPersistenceError("serialize", self.presets_file, ex) from ex
        try:
            payload = json.dumps(data, indent=2, ensure_ascii=False)
        except Exception as ex:
            raise PresetPersistenceError("serialize", self.presets_file, ex) from ex

        try:
            self.presets_file.parent.mkdir(parents=True, exist_ok=True)
        except OSError as ex:
            raise PresetPersistenceError("mkdir", self.presets_file, ex) from ex

        temp = self.presets_file.with_suffix(".tmp")
        try:
            with open(temp, "w", encoding="utf-8") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
        except OSError as ex:
            cleanup_error = self._cleanup_temp(temp)
            raise PresetPersistenceError("write", temp, ex, cleanup_error) from ex
        try:
            os.replace(temp, self.presets_file)
        except OSError as ex:
            cleanup_error = self._cleanup_temp(temp)
            raise PresetPersistenceError("replace", self.presets_file, ex, cleanup_error) from ex

    @staticmethod
    def _cleanup_temp(temp: Path) -> Optional[BaseException]:
        try:
            if temp.exists():
                temp.unlink()
        except OSError as ex:
            return ex
        return None

    def save_preset(self, name: str, models: List[str], description: str = "") -> Preset:
        """Persist a preset; in-memory state is rolled back on failure.

        The new preset becomes visible only after persistence succeeds, so a
        failed save can never leave memory and disk divergent with an
        in-memory entry the next restart loses.
        """
        now = datetime.now().isoformat()
        p = Preset(name=name, models=list(models), description=description, updated_at=now)
        previous = self.presets.get(name)
        had_previous = name in self.presets
        self.presets[name] = p
        try:
            self.save()
        except PresetPersistenceError:
            if had_previous:
                self.presets[name] = previous
            else:
                self.presets.pop(name, None)
            raise
        return p

    def delete_preset(self, name: str) -> bool:
        """Delete a preset; in-memory state is restored on persistence failure."""
        if name not in self.presets:
            return False
        previous = self.presets[name]
        del self.presets[name]
        try:
            self.save()
        except PresetPersistenceError:
            self.presets[name] = previous
            raise
        return True

    def compare_with_combo(
        self,
        preset_name: str,
        live_combo_models: List[str],
        health_cache: HealthCache,
    ) -> List[Dict[str, Any]]:
        """Compares a preset with live combo and annotates health."""
        preset = self.presets.get(preset_name)
        if not preset:
            return []

        live_set = set(live_combo_models)
        results = []
        for m in preset.models:
            rec = health_cache.get(m)
            state = rec.state if rec else HealthState.UNKNOWN.value
            results.append({
                "model": m,
                "in_combo": m in live_set,
                "state": state,
                "latency_ms": rec.latency_ms if rec else 0.0,
                "is_dead": rec.is_dead() if rec else False,
            })
        return results
from collections import Counter
