"""T-8 / SRC-001:R0002 CORE-002 regression — combo rename must be NAME-ONLY.

The defect: `_rename_combo` submitted the dirty LOCAL models/kind to the update
API, so a rename silently persisted unsaved model edits, and the verified path
called `mark_clean()`, which adopted those dirty models as the saved baseline.

Contract proven here (T-8 acceptance):
  * the rename request carries the authoritative server models/kind;
  * local dirty model edits survive the rename as UNSAVED (`[B,A]` stays `[B,A]`,
    `has_unsaved_changes()` is True) for reorder, kind, add and remove cases;
  * a clean combo renames without becoming dirty and adopts the verified name;
  * a failed (fail-closed) rename changes nothing local.

Offline doubles only: no server, no network, no discovery.
"""
import pytest
from PySide6.QtWidgets import QApplication

from core.history import HealthCache
from ui import combo_editor_view
from ui.combo_editor_view import ComboEditorView
from ui.theme import apply_theme

SERVER_MODELS = ["prov/a", "prov/b"]


@pytest.fixture(scope="session")
def qapp():
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    apply_theme(app)
    return app


class _RenameClient:
    """Offline combo server double that records rename requests."""

    def __init__(self, name="SAIFREN", models=None, kind=None, fail_rename=False):
        self.combos = [{
            "id": "c1",
            "name": name,
            "kind": kind,
            "models": list(SERVER_MODELS if models is None else models),
        }]
        self.server_updated = "before"
        self.after_updated = "after"
        self.rename_calls = []
        self.get_calls = 0
        self.fail_rename = fail_rename

    def get_combos(self):
        self.get_calls += 1
        return [{
            "id": c["id"],
            "name": c["name"],
            "kind": c["kind"],
            "models": list(c["models"]),
            "updatedAt": self.server_updated,
        } for c in self.combos]

    def rename_combo(self, combo_id, new_name, current_models, kind=None):
        self.rename_calls.append({
            "combo_id": combo_id,
            "new_name": new_name,
            "models": list(current_models),
            "kind": kind,
        })
        if self.fail_rename:
            return None
        for c in self.combos:
            if c["id"] == combo_id:
                c["name"] = new_name
                c["models"] = list(current_models)
                c["kind"] = kind
        self.server_updated = self.after_updated
        return {"id": combo_id}


class _FakeInput:
    target = "SAIFREN-RENAMED"

    @staticmethod
    def getText(*_args, **_kwargs):
        return (_FakeInput.target, True)


class _FakeMessageBox:
    calls = []

    @classmethod
    def _record(cls, kind):
        def _call(_parent, title, text, *args, **kwargs):
            cls.calls.append((kind, title))
            return None
        return _call

    @classmethod
    def reset(cls):
        cls.calls = []


for _kind in ("information", "warning", "critical", "question"):
    setattr(_FakeMessageBox, _kind, _FakeMessageBox._record(_kind))


def _editor(qapp, tmp_path, client):
    view = ComboEditorView(client, HealthCache(cache_file=tmp_path / "c.json"))
    view.load_combos_from_9router()
    assert view.current_combo is not None
    return view


@pytest.fixture(autouse=True)
def _no_modals(monkeypatch):
    _FakeMessageBox.reset()
    _FakeInput.target = "SAIFREN-RENAMED"
    monkeypatch.setattr(combo_editor_view, "QInputDialog", _FakeInput)
    monkeypatch.setattr(combo_editor_view, "QMessageBox", _FakeMessageBox)
    yield
    _FakeMessageBox.reset()


def test_rename_request_carries_server_models_not_dirty_local(qapp, tmp_path):
    client = _RenameClient()
    view = _editor(qapp, tmp_path, client)

    # local reorder [prov/a, prov/b] -> [prov/b, prov/a]: dirty, unsaved
    view.current_combo.models = ["prov/b", "prov/a"]
    assert view.current_combo.has_unsaved_changes()

    view._rename_combo()

    assert len(client.rename_calls) == 1
    call = client.rename_calls[0]
    assert call["models"] == SERVER_MODELS, "rename must submit the authoritative server models"
    assert call["kind"] is None, "rename must submit the authoritative server kind"
    assert call["new_name"] == _FakeInput.target
    view.close()


def test_dirty_reorder_survives_rename_as_unsaved(qapp, tmp_path):
    client = _RenameClient()
    view = _editor(qapp, tmp_path, client)
    view.current_combo.models = ["prov/b", "prov/a"]

    view._rename_combo()

    combo = view.current_combo
    assert combo.name == _FakeInput.target
    assert combo.baseline_name == _FakeInput.target
    assert combo.models == ["prov/b", "prov/a"], "local dirty order must survive rename"
    assert combo.has_unsaved_changes() is True, "dirty models must stay UNSAVED after rename"
    assert combo.baseline_models == SERVER_MODELS, "baseline must remain the server truth"
    view.close()


def test_dirty_kind_survives_rename_as_unsaved(qapp, tmp_path):
    client = _RenameClient(kind=None)
    view = _editor(qapp, tmp_path, client)
    view.current_combo.kind = "chat"
    assert view.current_combo.has_unsaved_changes()

    view._rename_combo()

    assert client.rename_calls[0]["kind"] is None, "server kind, not the dirty local kind"
    assert view.current_combo.kind == "chat"
    assert view.current_combo.has_unsaved_changes() is True
    view.close()


def test_dirty_add_and_remove_survive_rename_as_unsaved(qapp, tmp_path):
    client = _RenameClient()
    view = _editor(qapp, tmp_path, client)

    view.current_combo.models = SERVER_MODELS + ["prov/c"]  # dirty add
    view._rename_combo()
    assert client.rename_calls[0]["models"] == SERVER_MODELS
    assert view.current_combo.models == SERVER_MODELS + ["prov/c"]
    assert view.current_combo.has_unsaved_changes() is True

    view.current_combo.models = ["prov/a"]  # dirty remove
    _FakeInput.target = "SAIFREN-SECOND"
    view._rename_combo()
    assert client.rename_calls[1]["models"] == SERVER_MODELS
    assert view.current_combo.models == ["prov/a"]
    assert view.current_combo.has_unsaved_changes() is True
    view.close()


def test_clean_rename_stays_clean_and_adopts_verified_name(qapp, tmp_path):
    client = _RenameClient()
    view = _editor(qapp, tmp_path, client)
    assert view.current_combo.has_unsaved_changes() is False

    view._rename_combo()

    combo = view.current_combo
    assert client.rename_calls[0]["models"] == SERVER_MODELS
    assert combo.name == _FakeInput.target
    assert combo.has_unsaved_changes() is False
    assert combo.baseline_updated_at == "after"
    view.close()


def test_failed_rename_is_fail_closed_and_changes_nothing_local(qapp, tmp_path):
    client = _RenameClient(fail_rename=True)
    view = _editor(qapp, tmp_path, client)
    view.current_combo.models = ["prov/b", "prov/a"]
    before_name = view.current_combo.name

    view._rename_combo()

    assert view.current_combo.name == before_name, "no local name claim without server verify"
    assert view.current_combo.models == ["prov/b", "prov/a"]
    assert view.current_combo.has_unsaved_changes() is True
    assert any(kind == "critical" for kind, _title in _FakeMessageBox.calls)
    view.close()
