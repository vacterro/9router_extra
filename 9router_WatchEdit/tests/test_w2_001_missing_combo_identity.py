"""
W2-001 regressions - missing selected-combo state must fail closed.

Defect (audit/3.md W2-001): absence of the selected server combo was reused as
authoritative EMPTY state. A rename with no exact-id server match synthesized
server_models=[]/server_kind=None and could persist an empty destructive
mutation; replace_combos silently dropped a dirty local draft whose combo
disappeared from an accepted snapshot.

Contract under test:
  * _rename_combo with no server id match issues ZERO update calls and leaves
    name/models/kind unchanged;
  * a dirty combo that disappears from an accepted snapshot stays recoverable
    and visibly conflicted (tombstoned), not silently discarded.
"""
import pytest
from PySide6.QtWidgets import QApplication

from core.combo_manager import StableCombo
from core.history import HealthCache
from ui import combo_editor_view
from ui.combo_editor_view import ComboEditorView
from ui.theme import apply_theme


@pytest.fixture(scope="session")
def qapp():
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    apply_theme(app)
    return app


class _Client:
    """Server double whose read-back can be made to miss the target combo."""

    def __init__(self, combos):
        self._combos = combos
        self.rename_calls = []

    def get_combos(self):
        return [dict(c) for c in self._combos]

    def rename_combo(self, combo_id, new_name, current_models, kind=None):
        self.rename_calls.append((combo_id, new_name, list(current_models), kind))
        return {"id": combo_id}


class _FakeInput:
    target = "RENAMED"

    @staticmethod
    def getText(*_a, **_k):
        return (_FakeInput.target, True)


class _FakeMessageBox:
    calls = []

    @classmethod
    def _record(cls, kind):
        def _call(_parent, title, text, *a, **k):
            cls.calls.append((kind, title))
        return _call

    @classmethod
    def reset(cls):
        cls.calls = []


for _kind in ("information", "warning", "critical", "question"):
    setattr(_FakeMessageBox, _kind, _FakeMessageBox._record(_kind))


@pytest.fixture(autouse=True)
def _no_modals(monkeypatch):
    _FakeMessageBox.reset()
    monkeypatch.setattr(combo_editor_view, "QInputDialog", _FakeInput)
    monkeypatch.setattr(combo_editor_view, "QMessageBox", _FakeMessageBox)
    yield
    _FakeMessageBox.reset()


def _view(qapp, tmp_path, combos):
    client = _Client(combos)
    view = ComboEditorView(client, HealthCache(cache_file=tmp_path / "c.json"))
    return view, client


# ---------------------------------------------------------------------------
# rename must require an authoritative exact-id match
# ---------------------------------------------------------------------------
def test_rename_refuses_when_target_absent_from_readback(qapp, tmp_path):
    # Server read-back does NOT contain c1.
    view, client = _view(qapp, tmp_path, [
        {"id": "c2", "name": "OTHER", "kind": "llm", "models": ["m/1"], "updatedAt": "t"},
    ])
    view.current_combo = StableCombo(
        combo_id="c1", name="LocalName", models=["m/1", "m/2"], kind="llm",
        baseline_models=["m/1", "m/2"], baseline_name="LocalName", baseline_kind="llm",
    )

    view._rename_combo()

    assert client.rename_calls == [], "no update may be sent when the target cannot be proven present"
    assert view.current_combo.name == "LocalName"
    assert view.current_combo.models == ["m/1", "m/2"]
    assert view.current_combo.kind == "llm"
    assert any("warning" == kind for kind, _title in _FakeMessageBox.calls)
    view.close()


def test_rename_succeeds_with_exact_id_match(qapp, tmp_path):
    view, client = _view(qapp, tmp_path, [
        {"id": "c1", "name": "LocalName", "kind": "llm", "models": ["m/1", "m/2"], "updatedAt": "t"},
    ])
    view.current_combo = StableCombo(
        combo_id="c1", name="LocalName", models=["m/1", "m/2"], kind="llm",
        baseline_models=["m/1", "m/2"], baseline_name="LocalName", baseline_kind="llm",
    )
    view._rename_combo()
    assert len(client.rename_calls) == 1
    assert client.rename_calls[0][0] == "c1"
    view.close()


# ---------------------------------------------------------------------------
# replace_combos must preserve a dirty combo that disappears
# ---------------------------------------------------------------------------
def test_dirty_combo_disappearing_stays_recoverable(qapp, tmp_path):
    view, client = _view(qapp, tmp_path, [
        {"id": "c1", "name": "C1", "kind": "llm", "models": ["m/1"], "updatedAt": "t"},
    ])
    view.replace_combos(client.get_combos())
    assert view.current_combo is not None and view.current_combo.id == "c1"

    # Make the draft dirty (unsaved local edit).
    view.current_combo.add_model("m/local-edit")
    assert view.current_combo.has_unsaved_changes()

    # An accepted snapshot no longer contains c1 (apparent external deletion).
    view.replace_combos([
        {"id": "c2", "name": "C2", "kind": "llm", "models": ["m/9"], "updatedAt": "t"},
    ])

    # The dirty draft must NOT be silently dropped.
    assert "c1" in view.combos, "dirty draft must remain recoverable"
    assert any("m/local-edit" in c.models for cid, c in view.combos.items() if cid == "c1")
    assert "c1" in view._missing_dirty_ids
    view.close()


def test_clean_disappearing_combo_reconciles_away(qapp, tmp_path):
    view, client = _view(qapp, tmp_path, [
        {"id": "c1", "name": "C1", "kind": "llm", "models": ["m/1"], "updatedAt": "t"},
    ])
    view.replace_combos(client.get_combos())
    assert view.current_combo.id == "c1"
    assert not view.current_combo.has_unsaved_changes()

    view.replace_combos([
        {"id": "c2", "name": "C2", "kind": "llm", "models": ["m/9"], "updatedAt": "t"},
    ])
    assert "c1" not in view.combos, "a clean genuinely-deleted combo may reconcile away"
    view.close()


def test_dirty_preserved_combo_missing_label(qapp, tmp_path):
    view, client = _view(qapp, tmp_path, [
        {"id": "c1", "name": "C1", "kind": "llm", "models": ["m/1"], "updatedAt": "t"},
    ])
    view.replace_combos(client.get_combos())
    view.current_combo.add_model("m/local-edit")
    view.replace_combos([
        {"id": "c2", "name": "C2", "kind": "llm", "models": ["m/9"], "updatedAt": "t"},
    ])
    # Selecting the tombstoned draft shows the conflicted dirty label.
    idx = view.cb_combo_selector.findData("c1")
    assert idx >= 0
    view.cb_combo_selector.setCurrentIndex(idx)
    assert "missing on server" in view.lbl_dirty_state.text()
    view.close()
