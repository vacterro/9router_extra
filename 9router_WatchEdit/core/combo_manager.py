"""
9router_WatchEdit - Stable Combo Model, Diff Engine & Preset Manager
Maintains canonical identity decoupling from widget rows, computes clear visual diffs,
and manages local fallback presets.
"""
from dataclasses import dataclass, field, asdict
from datetime import datetime
import json
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
            removed = [m for m in self.baseline_models if m not in server_models]
            added = [m for m in server_models if m not in self.baseline_models]
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
        count = 0
        for cid in canonical_ids:
            if self.remove_model(cid):
                count += 1
        return count

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

    removed = [m for m in original if m not in mod_set]
    added = [(m, modified.index(m)) for m in modified if m not in orig_set]

    # Compute index shifts for items present in both
    common_orig = [m for m in original if m in mod_set]
    moved = []
    for m in common_orig:
        old_idx = original.index(m)
        new_idx = modified.index(m)
        if old_idx != new_idx:
            moved.append((m, old_idx, new_idx))

    return ComboDiff(removed=removed, added=added, moved=moved)

class PresetManager:
    def __init__(self, presets_file: Path = PRESETS_FILE):
        self.presets_file = presets_file
        self.presets: Dict[str, Preset] = {}
        self.load()

    def load(self):
        if self.presets_file.exists():
            try:
                data = json.loads(self.presets_file.read_text(encoding="utf-8"))
                for name, item in data.items():
                    self.presets[name] = Preset(
                        name=name,
                        models=item.get("models", []),
                        description=item.get("description", ""),
                        updated_at=item.get("updated_at", ""),
                    )
                return
            except Exception:
                pass

        # Seed with defaults
        now = datetime.now().isoformat()
        for name, models in DEFAULT_PRESETS.items():
            self.presets[name] = Preset(
                name=name,
                models=models,
                description=f"Built-in preset: {name}",
                updated_at=now,
            )
        self.save()

    def save(self):
        temp = self.presets_file.with_suffix(".tmp")
        try:
            data = {name: asdict(p) for name, p in self.presets.items()}
            temp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
            temp.replace(self.presets_file)
        except Exception:
            if temp.exists():
                try:
                    temp.unlink()
                except Exception:
                    pass

    def save_preset(self, name: str, models: List[str], description: str = "") -> Preset:
        now = datetime.now().isoformat()
        p = Preset(name=name, models=list(models), description=description, updated_at=now)
        self.presets[name] = p
        self.save()
        return p

    def delete_preset(self, name: str) -> bool:
        if name in self.presets:
            del self.presets[name]
            self.save()
            return True
        return False

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
