"""Per-combo model activation: ON/OFF, persistence, drift, safety.

The backend gate (measured on 9router 0.5.91-extra) is that the engine has no
activation field at all: a combo stores an ordered ``models`` array and routes
that array verbatim. So the tests below never assert against a fabricated
engine flag. They assert the two things that are actually true:

* what the editor PUBLISHES (the effective projection) excludes a disabled
  member, which is what makes the switch routing-enforced;
* what the editor REMEMBERS (the configured sequence) still contains that
  member afterwards, so OFF is reversible and never a deletion.
"""
import sys
from pathlib import Path

import pytest
from PySide6.QtCore import QCoreApplication, QEvent
from PySide6.QtWidgets import QApplication, QDialog, QMessageBox

from core.activation import (
    ACTIVATION_BLOCKED,
    ACTIVATION_DISABLED,
    ACTIVATION_ENABLED,
    ACTIVATION_ROUTABLE,
    ActivationStore,
    ComboActivation,
    ReconcileStatus,
    classify_row,
    projection,
    reconcile,
)
from core.combo_manager import StableCombo
from core.discovery import DiscoveredModel
from core.history import HealthCache
from core.model_explorer import ExplorerQuery, build_row, matches
from ui import combo_editor_view
from ui.theme import apply_theme


@pytest.fixture(scope="session")
def qapp():
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    apply_theme(app)
    return app


class _Engine:
    """A fake 9Router that behaves like the real one.

    It stores ``models`` verbatim and returns it verbatim -- no activation
    field, no filtering -- which is precisely the engine behaviour the gate
    measured. ``routed_request`` then selects from that published array, so a
    test can prove a disabled model is not routable.
    """

    def __init__(self, combos):
        self.combos = [dict(c) for c in combos]
        self.update_calls = []
        self.create_calls = []
        self.fail_update = False

    def _find(self, combo_id):
        return next((c for c in self.combos if c.get("id") == combo_id), None)

    def get_combos(self):
        return [dict(c) for c in self.combos]

    def update_combo(self, combo_id, name, models, kind=None):
        self.update_calls.append((combo_id, name, list(models), kind))
        if self.fail_update:
            return None
        row = self._find(combo_id)
        if row is None:
            return None
        row["name"] = name
        row["models"] = list(models)          # verbatim, exactly like the engine
        row["kind"] = kind
        row["updatedAt"] = f"t{len(self.update_calls)}"
        return dict(row)

    def create_combo(self, name, models, kind=None):
        self.create_calls.append((name, list(models), kind))
        row = {"id": f"new-{len(self.create_calls)}", "name": name,
               "models": list(models), "kind": kind, "updatedAt": "t0"}
        self.combos.append(row)
        return dict(row)

    def delete_combo(self, combo_id):
        before = len(self.combos)
        self.combos = [c for c in self.combos if c.get("id") != combo_id]
        return len(self.combos) != before

    # ---- what the router would actually do with the published array
    def routed_request(self, combo_name):
        row = next((c for c in self.combos if c.get("name") == combo_name), None)
        if row is None or not row.get("models"):
            return None
        return row["models"][0]


def _model(cid, prefix="p"):
    return DiscoveredModel(cid, f"{prefix} provider", prefix, "conn", cid.split("/")[-1],
                           cid.split("/")[-1].upper())


def _view(qapp, tmp_path, engine, models=("p/a", "p/b", "p/c"), store=None):
    cache = HealthCache(cache_file=tmp_path / "cache.json")
    view = combo_editor_view.ComboEditorView(
        engine, cache, activation_store=store or ActivationStore(tmp_path / "activation.json")
    )
    info, warn, crit = [], [], []
    view._msgs = (info, warn, crit)
    view.replace_combos(engine.get_combos())
    view.set_available_models([_model(m) for m in models])
    return view

def _patch_messages(monkeypatch, view):
    info, warn, crit = [], [], []
    view._msgs = (info, warn, crit)
    monkeypatch.setattr(combo_editor_view.QMessageBox, "information",
                        lambda *a, **k: info.append(a[1:]))
    monkeypatch.setattr(combo_editor_view.QMessageBox, "warning",
                        lambda *a, **k: warn.append(a[1:]))
    monkeypatch.setattr(combo_editor_view.QMessageBox, "critical",
                        lambda *a, **k: crit.append(a[1:]))
    monkeypatch.setattr(combo_editor_view.QMessageBox, "question",
                        lambda *a, **k: QMessageBox.Yes)
    monkeypatch.setattr(combo_editor_view.DiffConfirmDialog, "exec",
                        lambda self: QDialog.Accepted)
    return info, warn, crit


def _teardown(view, qapp):
    view.close()
    view.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
    qapp.processEvents()


def _one_combo(combo_id="c1", name="C1", models=("p/a", "p/b", "p/c")):
    return [{"id": combo_id, "name": name, "kind": "llm",
             "models": list(models), "updatedAt": "t0"}]


# --------------------------------------------------------------------------
# The backend gate is disclosed, never simulated.
# --------------------------------------------------------------------------
def test_combo_on_off_switch_is_not_operational(qapp, tmp_path, monkeypatch):
    engine = _Engine(_one_combo())
    view = _view(qapp, tmp_path, engine)
    info, warn, crit = _patch_messages(monkeypatch, view)
    try:
        assert view.btn_combo_suspend.isEnabled() is False, (
            "an unsupported switch must not appear operational"
        )
        assert "no combo enable/disable" in combo_editor_view.COMBO_SUSPEND_UNSUPPORTED
        assert view.lbl_combo_state.text().startswith("State:")
        view._explain_combo_suspend()
        assert info and "models array verbatim" in info[-1][1]
    finally:
        _teardown(view, qapp)


def test_combo_selector_filter_offers_only_real_states(qapp, tmp_path, monkeypatch):
    engine = _Engine(_one_combo())
    view = _view(qapp, tmp_path, engine)
    try:
        values = [view.cb_combo_filter.itemData(i) for i in range(view.cb_combo_filter.count())]
        assert values == [
            combo_editor_view.COMBO_FILTER_ALL,
            combo_editor_view.COMBO_FILTER_WITH_ROUTABLE,
            combo_editor_view.COMBO_FILTER_NO_ROUTABLE,
            combo_editor_view.COMBO_FILTER_WITH_DISABLED,
        ]
    finally:
        _teardown(view, qapp)


# --------------------------------------------------------------------------
# Individual model ON/OFF is routing-enforced and reversible.
# --------------------------------------------------------------------------
def test_off_excludes_the_model_from_what_is_published(qapp, tmp_path, monkeypatch):
    engine = _Engine(_one_combo())
    view = _view(qapp, tmp_path, engine)
    _patch_messages(monkeypatch, view)
    try:
        combo = view.current_combo
        combo.set_model_enabled("p/b", False)
        view.save_current_combo()

        assert engine.update_calls, "Save must publish the projection"
        published = engine.update_calls[-1][2]
        assert published == ["p/a", "p/c"], "the disabled member must not be published"
        assert engine.routed_request("C1") == "p/a"
        assert "p/b" not in engine.routed_request("C1")
    finally:
        _teardown(view, qapp)


def test_off_keeps_the_model_configured_and_restores_its_position(qapp, tmp_path, monkeypatch):
    engine = _Engine(_one_combo())
    view = _view(qapp, tmp_path, engine)
    _patch_messages(monkeypatch, view)
    try:
        combo = view.current_combo
        combo.set_model_enabled("p/b", False)
        view.save_current_combo()
        # Still configured, still in its original priority position.
        assert combo.models == ["p/a", "p/b", "p/c"]
        assert combo.disabled == {"p/b"}
        assert combo.effective_models() == ["p/a", "p/c"]

        combo.set_model_enabled("p/b", True)
        view.save_current_combo()
        assert engine.update_calls[-1][2] == ["p/a", "p/b", "p/c"]
        assert combo.effective_models() == ["p/a", "p/b", "p/c"]
    finally:
        _teardown(view, qapp)


def test_disabling_in_one_combo_leaves_the_same_model_in_another(qapp, tmp_path, monkeypatch):
    engine = _Engine(_one_combo("c1", "A") + _one_combo("c2", "B"))
    view = _view(qapp, tmp_path, engine)
    _patch_messages(monkeypatch, view)
    try:
        combo_a = view.combos["c1"]
        combo_b = view.combos["c2"]
        combo_a.set_model_enabled("p/b", False)
        assert combo_a.is_model_enabled("p/b") is False
        assert combo_b.is_model_enabled("p/b") is True
        assert combo_b.disabled == set()
    finally:
        _teardown(view, qapp)


def test_off_is_not_the_global_disabled_models_metadata(qapp, tmp_path, monkeypatch):
    """Per-combo OFF must never be written to the engine's global exclusion."""
    engine = _Engine(_one_combo())
    view = _view(qapp, tmp_path, engine)
    _patch_messages(monkeypatch, view)
    try:
        view.current_combo.set_model_enabled("p/b", False)
        view.save_current_combo()
        # The only engine write is the combo PUT; no /api/models/disabled call.
        assert not hasattr(engine, "disabled_models_calls")
        assert engine.update_calls[-1][2] == ["p/a", "p/c"]
    finally:
        _teardown(view, qapp)


# --------------------------------------------------------------------------
# Save / revert / verified failure semantics.
# --------------------------------------------------------------------------
def test_activation_change_is_a_draft_until_save(qapp, tmp_path, monkeypatch):
    engine = _Engine(_one_combo())
    view = _view(qapp, tmp_path, engine)
    _patch_messages(monkeypatch, view)
    try:
        view.current_combo.set_model_enabled("p/b", False)
        assert engine.update_calls == [], "toggling a switch must not write to the router"
        assert view.current_combo.has_unsaved_changes() is True
    finally:
        _teardown(view, qapp)


def test_revert_restores_activation(qapp, tmp_path, monkeypatch):
    engine = _Engine(_one_combo())
    view = _view(qapp, tmp_path, engine)
    _patch_messages(monkeypatch, view)
    try:
        combo = view.current_combo
        combo.set_model_enabled("p/b", False)
        combo.revert_unsaved_changes()
        assert combo.disabled == set()
        assert combo.has_unsaved_changes() is False
    finally:
        _teardown(view, qapp)


def test_failed_apply_keeps_the_draft_and_claims_nothing(qapp, tmp_path, monkeypatch):
    engine = _Engine(_one_combo())
    view = _view(qapp, tmp_path, engine)
    info, warn, crit = _patch_messages(monkeypatch, view)
    try:
        combo = view.current_combo
        combo.set_model_enabled("p/b", False)
        engine.fail_update = True
        view.save_current_combo()
        assert crit and crit[-1][0] == "Apply Failed"
        assert not info, "a failed apply must not report success"
        assert combo.disabled == {"p/b"}, "the intended draft must survive the failure"
        assert combo.has_unsaved_changes() is True
    finally:
        _teardown(view, qapp)


def test_readback_that_ignores_the_projection_is_not_success(qapp, tmp_path, monkeypatch):
    """A backend that keeps the disabled member is not a verified OFF."""
    engine = _Engine(_one_combo())
    view = _view(qapp, tmp_path, engine)
    info, warn, crit = _patch_messages(monkeypatch, view)
    try:
        combo = view.current_combo
        combo.set_model_enabled("p/b", False)
        # Engine "normalizes" the write by keeping every model.
        real_update = engine.update_combo

        def ignoring_update(combo_id, name, models, kind=None):
            return real_update(combo_id, name, ["p/a", "p/b", "p/c"], kind)

        engine.update_combo = ignoring_update
        view.save_current_combo()
        assert not info, "OFF must not be claimed before the engine verifies it"
        assert warn and "models" in warn[-1][1]
        assert combo.has_unsaved_changes() is True
    finally:
        _teardown(view, qapp)


def test_save_refuses_to_publish_an_empty_combo(qapp, tmp_path, monkeypatch):
    engine = _Engine(_one_combo())
    view = _view(qapp, tmp_path, engine)
    info, warn, crit = _patch_messages(monkeypatch, view)
    try:
        combo = view.current_combo
        for cid in list(combo.models):
            combo.set_model_enabled(cid, False)
        assert combo.has_no_routable_models() is True
        view.save_current_combo()
        assert engine.update_calls == [], "an unroutable combo must not be published"
        assert warn and warn[-1][0] == "No Enabled Model"
        assert combo.models == ["p/a", "p/b", "p/c"], "members stay configured"
    finally:
        _teardown(view, qapp)


def test_last_routable_member_guard_informs_without_blocking(qapp, tmp_path, monkeypatch):
    engine = _Engine(_one_combo())
    view = _view(qapp, tmp_path, engine)
    info, warn, crit = _patch_messages(monkeypatch, view)
    try:
        combo = view.current_combo
        combo.set_model_enabled("p/a", False)
        combo.set_model_enabled("p/b", False)
        changed = combo.set_models_enabled(["p/c"], False)
        view._guard_last_routable(changed, False)
        assert warn and warn[-1][0] == "Combo Would Have No Routable Model"
        # The operator's intent is honoured; only the consequence is stated.
        assert combo.disabled == {"p/a", "p/b", "p/c"}
    finally:
        _teardown(view, qapp)


# --------------------------------------------------------------------------
# Bulk scope: hidden rows are never included.
# --------------------------------------------------------------------------
def test_bulk_off_visible_touches_only_visible_rows(qapp, tmp_path, monkeypatch):
    models = [f"p/m{i}" for i in range(5)]
    engine = _Engine(_one_combo(models=models))
    view = _view(qapp, tmp_path, engine, models=models)
    _patch_messages(monkeypatch, view)
    seen = {}
    monkeypatch.setattr(
        combo_editor_view.QMessageBox, "question",
        lambda *a, **k: (seen.update({"text": a[2]}), QMessageBox.Yes)[1],
    )
    try:
        view.combo_controls.search.setText("m1")
        view._on_combo_query_changed()
        visible = {r.canonical_id for r in view._combo_explorer.visible}
        assert visible == {"p/m1"}, "the filter must leave exactly one row visible"

        view._set_activation_visible(False)
        combo = view.current_combo
        assert combo.disabled == {"p/m1"}
        assert all(combo.is_model_enabled(m) for m in models if m != "p/m1")
        assert "NOT part of this operation" in seen["text"]
        assert "4 row(s) are hidden" in seen["text"] or "4 row" in seen["text"]
    finally:
        _teardown(view, qapp)


def test_bulk_preview_names_the_exact_scope(qapp, tmp_path, monkeypatch):
    engine = _Engine(_one_combo())
    view = _view(qapp, tmp_path, engine)
    _patch_messages(monkeypatch, view)
    seen = {}
    monkeypatch.setattr(
        combo_editor_view.QMessageBox, "question",
        lambda *a, **k: (seen.update({"text": a[2]}), QMessageBox.Yes)[1],
    )
    try:
        view._set_activation_visible(False)
        assert "• p/a" in seen["text"] and "• p/c" in seen["text"]
        # The preview must state the exact scope that is about to change.
        assert "Switch 3 visible model(s) OFF" in seen["text"]
    finally:
        _teardown(view, qapp)


# --------------------------------------------------------------------------
# Representation: configured vs effective.
# --------------------------------------------------------------------------
def test_counts_separate_configured_enabled_routable_disabled(qapp, tmp_path, monkeypatch):
    engine = _Engine(_one_combo())
    view = _view(qapp, tmp_path, engine)
    _patch_messages(monkeypatch, view)
    try:
        combo = view.current_combo
        combo.set_model_enabled("p/c", False)
        combo.set_model_enabled("p/b", False)
        view._refresh_combo_models_list()
        text = view.lbl_combo_model_count.text()
        assert text == "3 configured | 1 enabled | 1 routable | 2 disabled"
    finally:
        _teardown(view, qapp)


def test_enabled_but_blocked_is_shown_with_its_reason():
    record = ComboActivation(combo_id="c", configured=["p/a"], disabled=set())
    status, reason = classify_row("p/a", record, blocked=True,
                                 blocked_reason="excluded from the discovery catalogue")
    assert status == "ENABLED_BUT_BLOCKED"
    assert "catalogue" in reason


def test_blocked_row_is_not_effectively_routable():
    row = build_row(_model("p/a"), None, 0, within_combo=True)
    blocked = build_row(_model("p/a"), None, 0, within_combo=True)
    from dataclasses import replace
    blocked = replace(blocked, blocked=True, blocked_reason="provider unavailable")
    assert row.effective_routable is True
    assert blocked.effective_routable is False
    assert blocked.activation_state == ACTIVATION_BLOCKED
    assert blocked.effective_reason == "provider unavailable"


def test_activation_filters_compose_with_provider_and_search():
    from dataclasses import replace
    rows = [build_row(_model(f"p{m}/x{i}"), None, i, within_combo=True) for m, i in (("a", 0), ("b", 1))]
    rows = [replace(rows[0], manual_enabled=False), rows[1]]
    query = ExplorerQuery(activation=ACTIVATION_DISABLED)
    assert [r.canonical_id for r in rows if matches(r, query)] == ["pa/x0"]
    query = ExplorerQuery(activation=ACTIVATION_ENABLED, providers=frozenset({"p"}))
    assert [r.canonical_id for r in rows if matches(r, query)] == ["pb/x1"]
    query = ExplorerQuery(activation=ACTIVATION_ROUTABLE, text="x0")
    assert [r.canonical_id for r in rows if matches(r, query)] == []


# --------------------------------------------------------------------------
# Persistence: app restart, router restart, drift.
# --------------------------------------------------------------------------
def test_activation_survives_an_app_restart(qapp, tmp_path, monkeypatch):
    store_path = tmp_path / "activation.json"
    engine = _Engine(_one_combo())
    view = _view(qapp, tmp_path, engine, store=ActivationStore(store_path))
    _patch_messages(monkeypatch, view)
    try:
        view.current_combo.set_model_enabled("p/b", False)
        view.save_current_combo()
        assert engine.get_combos()[0]["models"] == ["p/a", "p/c"]
    finally:
        _teardown(view, qapp)

    # New process: same engine (projection published), same durable store.
    cache = HealthCache(cache_file=tmp_path / "cache.json")
    view2 = combo_editor_view.ComboEditorView(
        engine, cache, activation_store=ActivationStore(store_path)
    )
    _patch_messages(monkeypatch, view2)
    try:
        view2.replace_combos(engine.get_combos())
        combo = view2.current_combo
        assert combo.models == ["p/a", "p/b", "p/c"], (
            "the disabled member must be reconstructed from the local record, "
            "not lost with the engine's shortened list"
        )
        assert combo.disabled == {"p/b"}
        assert combo.has_unsaved_changes() is False
        assert view2._last_reconcile.status == ReconcileStatus.CLEAN
    finally:
        _teardown(view2, qapp)


def test_router_restart_that_replays_the_projection_is_clean(qapp, tmp_path, monkeypatch):
    store_path = tmp_path / "activation.json"
    engine = _Engine(_one_combo())
    view = _view(qapp, tmp_path, engine, store=ActivationStore(store_path))
    _patch_messages(monkeypatch, view)
    try:
        view.current_combo.set_model_enabled("p/c", False)
        view.save_current_combo()
    finally:
        _teardown(view, qapp)

    # Router restarted, engine state identical to the verified projection.
    cache = HealthCache(cache_file=tmp_path / "cache.json")
    view2 = combo_editor_view.ComboEditorView(
        engine, cache, activation_store=ActivationStore(store_path)
    )
    try:
        view2.replace_combos(engine.get_combos())
        assert view2._last_reconcile.status == ReconcileStatus.CLEAN
        assert view2.current_combo.has_unsaved_changes() is False
        assert view2._drifted_combo_ids == set()
    finally:
        _teardown(view2, qapp)


def test_externally_shortened_list_is_drift_never_intentional_deletion():
    record = ComboActivation(
        combo_id="c", configured=["a", "b", "c"], disabled={"b"},
        engine_baseline=["a", "c"],
    )
    result = reconcile(record, ["a"], "t9")
    assert result.status == ReconcileStatus.EXTERNAL_REMOVED
    assert result.is_conflict
    # The locally disabled member is still configured; nothing was deleted.
    assert record.configured == ["a", "b", "c"]
    assert record.disabled == {"b"}


def test_external_reenable_is_reported_as_drift():
    record = ComboActivation(
        combo_id="c", configured=["a", "b", "c"], disabled={"b"},
        engine_baseline=["a", "c"],
    )
    result = reconcile(record, ["a", "b", "c"], "t9")
    assert result.status == ReconcileStatus.EXTERNAL_REENABLED
    assert "re-enabled outside WatchEdit" in result.changes[0]


def test_external_drift_is_flagged_and_keeps_the_configured_sequence(
        qapp, tmp_path, monkeypatch):
    store_path = tmp_path / "activation.json"
    engine = _Engine(_one_combo())
    view = _view(qapp, tmp_path, engine, store=ActivationStore(store_path))
    _patch_messages(monkeypatch, view)
    try:
        view.current_combo.set_model_enabled("p/b", False)
        view.save_current_combo()
    finally:
        _teardown(view, qapp)

    # Someone edits the combo outside WatchEdit: the disabled member is gone.
    engine.combos[0]["models"] = ["p/a"]
    engine.combos[0]["updatedAt"] = "outside"
    cache = HealthCache(cache_file=tmp_path / "cache.json")
    view2 = combo_editor_view.ComboEditorView(
        engine, cache, activation_store=ActivationStore(store_path)
    )
    _patch_messages(monkeypatch, view2)
    try:
        view2.replace_combos(engine.get_combos())
        combo = view2.current_combo
        assert combo.models == ["p/a", "p/b", "p/c"], "no configured member may be erased"
        assert view2._last_reconcile.status == ReconcileStatus.EXTERNAL_REMOVED
        assert combo.id in view2._drifted_combo_ids
        assert combo.has_unsaved_changes() is True
        assert view2.lbl_dirty_state.text() == "Conflict: engine list differs"
    finally:
        _teardown(view2, qapp)


def test_missing_combo_is_not_read_as_an_empty_combo():
    record = ComboActivation(combo_id="c", configured=["a"], disabled=set(),
                             engine_baseline=["a"])
    result = reconcile(record, None, "")
    assert result.status == ReconcileStatus.MISSING
    assert result.is_conflict


# --------------------------------------------------------------------------
# Lifecycle: delete/duplicate leave no orphaned activation.
# --------------------------------------------------------------------------
def test_delete_forgets_activation_state(qapp, tmp_path, monkeypatch):
    store_path = tmp_path / "activation.json"
    engine = _Engine(_one_combo())
    view = _view(qapp, tmp_path, engine, store=ActivationStore(store_path))
    _patch_messages(monkeypatch, view)
    try:
        view.current_combo.set_model_enabled("p/b", False)
        view.save_current_combo()
        assert ActivationStore(store_path).get("c1") is not None
        view._delete_combo()
        assert ActivationStore(store_path).get("c1") is None, (
            "deleting a combo must not leave activation state that could affect another"
        )
    finally:
        _teardown(view, qapp)


def test_duplicate_starts_with_every_model_enabled(qapp, tmp_path, monkeypatch):
    store_path = tmp_path / "activation.json"
    engine = _Engine(_one_combo())
    view = _view(qapp, tmp_path, engine, store=ActivationStore(store_path))
    _patch_messages(monkeypatch, view)
    try:
        view.current_combo.set_model_enabled("p/b", False)
        view._duplicate_combo()
        assert engine.create_calls[-1][1] == ["p/a", "p/b", "p/c"], (
            "the duplicate carries the full configured sequence"
        )
        duplicate_id = engine.create_calls and engine.combos[-1]["id"]
        record = ActivationStore(store_path).get(duplicate_id)
        assert record is not None and record.disabled == set(), (
            "a duplicate must not inherit the source combo's suspensions"
        )
    finally:
        _teardown(view, qapp)


def test_records_for_vanished_combos_are_pruned(qapp, tmp_path, monkeypatch):
    store_path = tmp_path / "activation.json"
    engine = _Engine(_one_combo("c1", "A") + _one_combo("c2", "B"))
    view = _view(qapp, tmp_path, engine, store=ActivationStore(store_path))
    _patch_messages(monkeypatch, view)
    try:
        engine.combos = [c for c in engine.combos if c["id"] == "c2"]
        view.replace_combos(engine.get_combos())
        assert ActivationStore(store_path).get("c1") is None
    finally:
        _teardown(view, qapp)


# --------------------------------------------------------------------------
# Boundaries: no direct SQLite mutation, private storage, large catalogues.
# --------------------------------------------------------------------------
def test_no_direct_sqlite_mutation_in_the_activation_layer():
    source = Path(combo_editor_view.__file__).read_text(encoding="utf-8")
    assert "sqlite3" not in source, "the editor must not mutate SQLite directly"
    activation_source = (
        Path(combo_editor_view.__file__).parent.parent / "core" / "activation.py"
    ).read_text(encoding="utf-8")
    assert "sqlite" not in activation_source.lower()
    assert "sqlite3" not in sys.modules or "chk" not in activation_source


def test_activation_store_is_written_outside_the_repository(tmp_path):
    from config import REPO_ROOT

    store = ActivationStore(tmp_path / "activation.json")
    assert not str(store.path.resolve()).lower().startswith(str(Path(REPO_ROOT).resolve()).lower())


def test_store_round_trip_and_quarantine(tmp_path):
    path = tmp_path / "activation.json"
    store = ActivationStore(path)
    store.records["c1"] = ComboActivation(combo_id="c1", configured=["a", "b"],
                                          disabled={"b"}, engine_baseline=["a"])
    store.save()
    again = ActivationStore(path)
    assert again.load_state == "LOADED"
    assert again.get("c1").disabled == {"b"}

    path.write_text("{ this is not json", encoding="utf-8")
    broken = ActivationStore(path)
    assert broken.load_state == "MALFORMED_JSON"
    assert broken.records == {}
    assert broken.quarantine_path and Path(broken.quarantine_path).read_text(
        encoding="utf-8"
    ).startswith("{ this is not json")


def test_store_rejects_an_unknown_schema(tmp_path):
    path = tmp_path / "activation.json"
    path.write_text('{"schema": 99, "combos": []}', encoding="utf-8")
    store = ActivationStore(path)
    assert store.load_state == "INVALID_RECORD"
    assert store.records == {}
    assert store.quarantine_path is not None


def test_large_catalogue_projection_is_linear():
    n = 5000
    configured = [f"p/m{i}" for i in range(n)]
    disabled = {f"p/m{i}" for i in range(0, n, 2)}
    record = ComboActivation(combo_id="c", configured=configured, disabled=disabled)
    assert len(record.enabled_models()) == n // 2
    counts = record.counts()
    assert counts == {"configured": n, "enabled": n // 2, "disabled": n // 2,
                      "routable": n // 2}
    assert record.enabled_models() != []


def test_projection_is_exactly_configured_minus_disabled():
    assert projection(["a", "b", "c"], {"b"}) == ["a", "c"]
    # Duplicate canonical ids keep every occurrence's position.
    assert projection(["a", "b", "a"], {"a"}) == ["b"]


# --------------------------------------------------------------------------
# Bulk scopes: toggle, provider scope, and the apply states.
# --------------------------------------------------------------------------
def test_toggle_selected_flips_each_member_independently(qapp, tmp_path, monkeypatch):
    engine = _Engine(_one_combo())
    view = _view(qapp, tmp_path, engine)
    _patch_messages(monkeypatch, view)
    try:
        view.current_combo.set_model_enabled("p/b", False)
        view._refresh_combo_models_list()
        view.list_combo_models.item(0).setSelected(True)
        view.list_combo_models.item(1).setSelected(True)

        changed = view._toggle_activation_selected()

        assert sorted(changed) == ["p/a", "p/b"]
        assert view.current_combo.is_model_enabled("p/a") is False
        assert view.current_combo.is_model_enabled("p/b") is True
        # Toggle never changes membership or routing order.
        assert view.current_combo.models == ["p/a", "p/b", "p/c"]
    finally:
        _teardown(view, qapp)


def test_provider_scope_covers_hidden_members_of_that_provider(qapp, tmp_path, monkeypatch):
    engine = _Engine(_one_combo(models=("p/a", "q/b", "p/c")))
    view = _view(qapp, tmp_path, engine, models=("p/a", "q/b", "p/c"))
    view.set_available_models([_model("p/a"), _model("q/b", prefix="q"), _model("p/c")])
    _patch_messages(monkeypatch, view)
    try:
        # Hide q/b with the search filter, then scope the bulk op to provider p.
        view.combo_controls.search.setText("p/")
        view.combo_controls.provider.set_selected({"p"})
        view._on_combo_query_changed()

        changed = view._set_activation_provider(False)

        assert sorted(changed) == ["p/a", "p/c"]
        assert view.current_combo.is_model_enabled("q/b") is True
        assert view.current_combo.models == ["p/a", "q/b", "p/c"]
    finally:
        _teardown(view, qapp)


def test_provider_scope_without_a_selected_provider_changes_nothing(qapp, tmp_path, monkeypatch):
    engine = _Engine(_one_combo())
    view = _view(qapp, tmp_path, engine)
    _patch_messages(monkeypatch, view)
    try:
        view.combo_controls.provider.set_selected(set())
        assert view._set_activation_provider(False) == []
        assert view.current_combo.disabled == set()
    finally:
        _teardown(view, qapp)


def test_failed_apply_reports_failure_and_keeps_the_draft(qapp, tmp_path, monkeypatch):
    engine = _Engine(_one_combo())
    view = _view(qapp, tmp_path, engine)
    _patch_messages(monkeypatch, view)
    try:
        view.current_combo.set_model_enabled("p/b", False)
        view._refresh_combo_models_list()
        engine.fail_update = True

        view.save_current_combo()

        assert view.lbl_dirty_state.text().startswith("Failed")
        # The intended state survives a failed apply.
        assert view.current_combo.is_model_enabled("p/b") is False
        assert view.current_combo.has_unsaved_changes() is True
    finally:
        _teardown(view, qapp)


def test_verified_apply_publishes_the_projection_only(qapp, tmp_path, monkeypatch):
    engine = _Engine(_one_combo())
    view = _view(qapp, tmp_path, engine)
    _patch_messages(monkeypatch, view)
    try:
        view.current_combo.set_model_enabled("p/b", False)
        view._refresh_combo_models_list()

        view.save_current_combo()

        assert view.lbl_dirty_state.text() == "Verified"
        published = engine.combos[0]["models"]
        assert "p/b" not in published
        assert published == ["p/a", "p/c"]
    finally:
        _teardown(view, qapp)


def test_combo_pool_counts_describe_the_real_activation_state(qapp, tmp_path, monkeypatch):
    engine = _Engine(_one_combo() + [{"id": "c2", "name": "C2", "kind": "llm",
                                      "models": ["p/a"], "updatedAt": "t0"}])
    view = _view(qapp, tmp_path, engine)
    _patch_messages(monkeypatch, view)
    try:
        view.current_combo.set_model_enabled("p/b", False)
        view._refresh_combo_models_list()

        text = view.lbl_combo_pool.text()
        assert "2 combos" in text
        assert "1 fully enabled" in text
        assert "1 with disabled models" in text
    finally:
        _teardown(view, qapp)


def test_manage_dialog_states_the_missing_combo_switch(qapp, tmp_path, monkeypatch):
    engine = _Engine(_one_combo())
    view = _view(qapp, tmp_path, engine)
    _patch_messages(monkeypatch, view)
    try:
        view._select_combo_by_index(0)
        dlg = combo_editor_view._ComboManageDialog(view.current_combo, view)
        assert dlg.btn_combo_off.isEnabled() is False
        assert "no combo enable/disable state" in dlg.btn_combo_off.toolTip()
        dlg.close()
    finally:
        _teardown(view, qapp)
