import random

from core.combo_manager import StableCombo, compute_combo_diff


class NoRepeatedSearchList(list):
    """Fails if an implementation falls back to per-item linear list search."""

    def __contains__(self, value):
        raise AssertionError("linear membership search used")

    def index(self, value, *args):
        raise AssertionError("linear index search used")


def _reference_diff(original, modified):
    original_set = set(original)
    modified_set = set(modified)
    removed = [model for model in original if model not in modified_set]
    added = [(model, modified.index(model)) for model in modified if model not in original_set]
    moved = []
    for model in [item for item in original if item in modified_set]:
        old_index = original.index(model)
        new_index = modified.index(model)
        if old_index != new_index:
            moved.append((model, old_index, new_index))
    return removed, added, moved


def test_linear_diff_matches_legacy_duplicate_semantics():
    rng = random.Random(3003)
    alphabet = ["a", "b", "c", "d"]
    for _ in range(500):
        original = [rng.choice(alphabet) for _ in range(rng.randrange(15))]
        modified = [rng.choice(alphabet) for _ in range(rng.randrange(15))]
        actual = compute_combo_diff(original, modified)
        assert (actual.removed, actual.added, actual.moved) == _reference_diff(original, modified)


def test_large_diff_and_divergence_avoid_repeated_list_searches():
    original = NoRepeatedSearchList(f"p/m{i}" for i in range(10_000))
    modified = NoRepeatedSearchList(original[1:] + original[:1])
    diff = compute_combo_diff(original, modified)
    assert not diff.removed and not diff.added
    assert len(diff.moved) == 10_000

    combo = StableCombo("c1", "large", list(original))
    combo.baseline_models = original
    changes = combo.describe_server_divergence("large", None, modified)
    assert changes == ["model order changed"]


def test_remove_models_is_linear_and_preserves_sequential_duplicate_removal():
    combo = StableCombo("c1", "duplicates", ["a", "b", "a", "c", "a"])
    combo.models = NoRepeatedSearchList(combo.models)
    removed = combo.remove_models(["a", "a", "missing"])
    assert removed == 2
    assert combo.models == ["b", "c", "a"]
