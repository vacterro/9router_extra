"""
CORE-004 regressions - APPLY/VERIFY must cover the complete mutable state.

Defect (audit/3.md CORE-004): save_current_combo's read-back verified only
`models`, then called mark_clean(), which adopted the local name+kind into the
authoritative baseline. A backend that persisted models but normalized/ignored
name or kind therefore produced false "Verified & Saved" success and a
baseline divergent from the backend, suppressing dirty/conflict detection.

Contract under test: every mismatch (name, kind, models, missing combo) avoids
success and avoids mark_clean; exact full equality marks all fields clean.
"""
import pytest
from PySide6.QtCore import QCoreApplication, QEvent
from PySide6.QtWidgets import QApplication, QDialog

from core.combo_manager import StableCombo
from core.history import HealthCache
from ui import combo_editor_view
from ui.theme import apply_theme


@pytest.fixture(scope="session")
def qapp():
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    apply_theme(app)
    return app


class _FakeClient:
    """Read-back state is set by the test; update_combo records calls."""

    def __init__(self, readback):
        self._readback = readback
        self.update_calls = []

    def get_combos(self):
        return list(self._readback)

    def update_combo(self, combo_id, name, models, kind=None):
        self.update_calls.append((combo_id, name, list(models), kind))
        return {"id": combo_id, "name": name, "models": list(models)}


def _view(qapp, tmp_path, monkeypatch, readback):
    client = _FakeClient(readback)
    cache = HealthCache(cache_file=tmp_path / "cache.json")
    view = combo_editor_view.ComboEditorView(client, cache)
    # Baseline equals the server read-back so the optimistic-concurrency check
    # does not fire (that dialog is a separate surface); the APPLY/VVERIFY path
    # under test is then reached directly.
    server = readback[0] if readback else {"name": "LocalName", "kind": "llm", "models": ["m/1", "m/2"], "updatedAt": "after"}
    combo = StableCombo(
        combo_id="c1",
        name="LocalName",
        models=["m/1", "m/2"],
        kind="llm",
        baseline_models=list(server["models"]),
        baseline_name=server["name"],
        baseline_kind=server["kind"],
        baseline_updated_at=server["updatedAt"],
    )
    view.current_combo = combo
    info, warn = [], []
    monkeypatch.setattr(combo_editor_view.QMessageBox, "information", lambda *a: info.append(a[1:]))
    monkeypatch.setattr(combo_editor_view.QMessageBox, "warning", lambda *a: warn.append(a[1:]))
    monkeypatch.setattr(combo_editor_view.DiffConfirmDialog, "exec", lambda self: QDialog.Accepted)
    return view, client, combo, info, warn


def _teardown(view, qapp):
    view.close()
    view.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
    qapp.processEvents()


def _readback(name="LocalName", kind="llm", models=("m/1", "m/2")):
    return [{"id": "c1", "name": name, "kind": kind, "models": list(models), "updatedAt": "after"}]


def test_exact_full_equality_marks_clean_and_reports_success(qapp, tmp_path, monkeypatch):
    view, client, combo, info, warn = _view(qapp, tmp_path, monkeypatch, _readback())
    view.save_current_combo()
    assert combo.has_unsaved_changes() is False
    assert info and info[-1][0] == "Verified & Saved"
    assert not warn
    _teardown(view, qapp)


def test_same_models_but_different_name_is_not_success(qapp, tmp_path, monkeypatch):
    view, client, combo, info, warn = _view(
        qapp, tmp_path, monkeypatch, _readback(name="ServerNormalizedName")
    )
    view.save_current_combo()
    assert not info, "must not report Verified & Saved on a name mismatch"
    assert warn and "name" in warn[-1][1]
    # The mismatching local field must NOT have been adopted as clean.
    assert combo.has_unsaved_changes() is True
    assert combo.baseline_name != combo.name
    _teardown(view, qapp)


def test_same_name_and_models_but_different_kind_is_not_success(qapp, tmp_path, monkeypatch):
    view, client, combo, info, warn = _view(
        qapp, tmp_path, monkeypatch, _readback(kind="chat")
    )
    view.save_current_combo()
    assert not info
    assert warn and "kind" in warn[-1][1]
    assert combo.has_unsaved_changes() is True
    _teardown(view, qapp)


def test_reordered_or_different_models_is_not_success(qapp, tmp_path, monkeypatch):
    view, client, combo, info, warn = _view(
        qapp, tmp_path, monkeypatch, _readback(models=("m/2", "m/1"))
    )
    view.save_current_combo()
    assert not info
    assert warn and "models" in warn[-1][1]
    assert combo.has_unsaved_changes() is True
    _teardown(view, qapp)


def test_missing_combo_readback_is_not_success(qapp, tmp_path, monkeypatch):
    view, client, combo, info, warn = _view(qapp, tmp_path, monkeypatch, [])
    combo.add_model("m/3")  # ensure a dirty local state to preserve
    view.save_current_combo()
    assert not info
    assert warn
    assert combo.has_unsaved_changes() is True
    _teardown(view, qapp)


def test_kind_none_and_empty_string_are_equivalent(qapp, tmp_path, monkeypatch):
    view, client, combo, info, warn = _view(
        qapp, tmp_path, monkeypatch, _readback(kind="")
    )
    combo.kind = None
    view.save_current_combo()
    assert info and info[-1][0] == "Verified & Saved", "None and '' kind must be normal-equal"
    assert combo.has_unsaved_changes() is False
    _teardown(view, qapp)


def test_server_field_mismatches_reports_exact_fields():
    combo = StableCombo(combo_id="c1", name="N", models=["m/1"], kind="llm")
    assert combo.server_field_mismatches("N", "llm", ["m/1"]) == []
    assert combo.server_field_mismatches("X", "llm", ["m/1"]) == ["name"]
    assert combo.server_field_mismatches("N", "chat", ["m/1"]) == ["kind"]
    assert combo.server_field_mismatches("N", "llm", ["m/2"]) == ["models"]
    assert combo.server_field_mismatches("X", "chat", ["m/2"]) == ["name", "kind", "models"]
