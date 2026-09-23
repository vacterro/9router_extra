"""
FREE Fallback tab UI tests (FREE-FALLBACK-001, milestones 6, 7, 9 and 10).

Deterministic PySide6 coverage of the operator surface:

  A  FREE Fallback is the FINAL main tab (after Models / Combo / OpenCode)
  B  the provider table is the primary surface and carries every required column
  C  sorting preserves row identity and checkbox state
  D  filters never mutate policy and never touch the network
  E  actions are disabled while an incompatible scan is running
  F  provider details follow the current selection (models are a drilldown)
  G  opening / sorting / filtering / selecting emits zero network calls
  H  bulk selection actions work on provider identity
"""
import httpx
import pytest
from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import QApplication

from core.free_provider_registry import FreeProviderRegistry
from ui.free_fallback_view import (
    MODEL_COLUMNS,
    PROVIDER_COLUMNS,
    FreeFallbackView,
)
from ui.theme import apply_theme


@pytest.fixture(scope="session")
def qapp():
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
        apply_theme(app)
    return app


@pytest.fixture
def free_window(qapp, tmp_path, monkeypatch):
    """MainWindow whose provider registry lives in tmp_path (never private appdata)."""
    from ui import main_window as mw

    def _registry(*args, **kwargs):
        return FreeProviderRegistry(path=tmp_path / "free_providers.json")

    monkeypatch.setattr(mw, "FreeProviderRegistry", _registry)
    window = mw.MainWindow()
    try:
        yield window
    finally:
        window.hide()
        window.close()


def _provider_row(provider_id: str, **overrides) -> dict:
    row = {
        "provider_id": provider_id,
        "display_name": provider_id.upper(),
        "adapter": "fake",
        "enabled": True,
        "scan_policy": "ALWAYS",
        "scan_mode": "METADATA_ONLY",
        "metadata_cost_risk": "ZERO_MONETARY_METADATA",
        "live_probe_cost_risk": "FREE_QUOTA_PROBE",
        "allow_billing_probe": False,
        "trusted_alive": False,
        "trusted_text": "-",
        "last_scan_at": "",
        "last_success_at": "",
        "last_metadata_success_at": "",
        "last_live_probe_success_at": "",
        "last_error_class": "",
        "last_error_summary": "",
        "last_status": "UNSCANNED",
        "models_discovered": 0,
        "strict_free_count": 0,
        "conditional_count": 0,
        "next_scan_due": "",
        "operator_note": "",
        "metadata_stale": True,
        "has_strict_free": False,
        "cost_safe": True,
        "live_supported": False,
        "metadata_supported": True,
        "client_bound": False,
        "adapter_transport": "test transport",
        "adapter_metadata_source": "test source",
        "badges": ["$0 METADATA"],
    }
    row.update(overrides)
    return row


def _model_row(canonical_id: str, **overrides) -> dict:
    row = {
        "canonical_id": canonical_id,
        "upstream_model_id": canonical_id.split("/", 1)[-1],
        "provider_id": canonical_id.split("/", 1)[0],
        "free_evidence": "STRICT_FREE",
        "routing": "DIRECT_ROUTABLE",
        "provider_health": "UNKNOWN",
        "scanner_managed": True,
        "last_seen": "2026-09-21T00:00:00Z",
        "saifren_eligible": True,
        "exclusion_reason": "",
    }
    row.update(overrides)
    return row


def _three_providers():
    rows = [
        _provider_row("beta", display_name="BETA", strict_free_count=2,
                      last_scan_at="2026-09-21T10:00:00Z", has_strict_free=True,
                      last_status="OK", trusted_alive=True,
                      trusted_text="trusted 7d left", cost_safe=False,
                      badges=["$0 METADATA", "TRUSTED", "STRICT FREE"]),
        _provider_row("alpha", display_name="ALPHA", strict_free_count=0,
                      last_scan_at="2026-09-20T10:00:00Z", enabled=False,
                      last_status="DISABLED", last_error_class="http_429",
                      last_error_summary="rate limited",
                      live_probe_cost_risk="POSSIBLE_BILLING", cost_safe=False),
        _provider_row("gamma", display_name="GAMMA", strict_free_count=1,
                      last_scan_at="2026-09-19T10:00:00Z", has_strict_free=True,
                      next_scan_due="2026-09-22T00:00:00Z"),
    ]
    models = {
        "beta": [_model_row("beta/one-free"), _model_row("beta/two-free")],
        "alpha": [_model_row("alpha/paid", free_evidence="PAID",
                             routing="DIRECT_ROUTABLE", saifren_eligible=False,
                             exclusion_reason="paid")],
        "gamma": [],
    }
    return rows, models


# ------------------------------------------------------------------- A, B
def test_free_fallback_is_the_final_main_tab(free_window):
    window = free_window
    titles = [window.main_tabs.tabText(i) for i in range(window.main_tabs.count())]
    assert titles == ["Models", "Combo", "OpenCode", "FREE Fallback"]
    assert window.main_tabs.currentIndex() == 0
    assert window.main_tabs.widget(window.main_tabs.count() - 1) is window.free_tab
    assert window.free_view is not None


def test_provider_table_is_the_primary_surface_with_required_columns(qapp):
    view = FreeFallbackView()
    headers = [
        view.provider_table.horizontalHeaderItem(i).text()
        for i in range(view.provider_table.columnCount())
    ]
    assert headers == PROVIDER_COLUMNS
    required = [
        "Scan", "Provider", "Status", "Scan policy", "Scan mode", "Cost risk",
        "Trusted alive", "Last scan", "Last success", "Models found", "Strict FREE",
        "Next scan", "Error / note",
    ]
    assert all(column in headers for column in required)
    # The first column is the check/enable control (checkbox state lives here).
    view.set_snapshot([_provider_row("solo")], {"solo": []})
    item = view.provider_table.item(0, 0)
    assert item.flags() & Qt.ItemIsUserCheckable
    assert item.checkState() == Qt.Unchecked
    # Provider identity is carried by the row, not by the display order.
    assert view.provider_table.item(0, 1).data(Qt.UserRole) == "solo"
    # Model drilldown columns exist too.
    model_headers = [
        view.model_table.horizontalHeaderItem(i).text()
        for i in range(view.model_table.columnCount())
    ]
    assert model_headers == MODEL_COLUMNS
    view.close()


# ---------------------------------------------------------------------- C
def test_sorting_preserves_row_identity_and_checkbox_state(qapp):
    view = FreeFallbackView()
    rows, models = _three_providers()
    view.set_snapshot(rows, models)

    # Select two providers through the visible checkbox control.
    view.provider_table.item(0, 0).setCheckState(Qt.Checked)
    view.provider_table.item(1, 0).setCheckState(Qt.Checked)
    selected = view.selected_provider_ids()
    assert len(selected) == 2

    expectations = [
        (("name", False), ["alpha", "beta", "gamma"]),
        (("name", True), ["gamma", "beta", "alpha"]),
        (("status", False), ["alpha", "beta", "gamma"]),
        (("strict_free", False), ["alpha", "gamma", "beta"]),
        (("strict_free", True), ["beta", "gamma", "alpha"]),
        (("last_scan", False), ["gamma", "alpha", "beta"]),
        (("last_scan", True), ["beta", "alpha", "gamma"]),
        (("cost_risk", False), ["beta", "gamma", "alpha"]),
        (("next_due", False), ["beta", "alpha", "gamma"]),
    ]
    for (key, desc), expected in expectations:
        view.sort_by(key, desc=desc)
        ids = view.row_provider_ids()
        assert ids == expected, (key, desc, ids)
        assert view.selected_provider_ids() == selected, (key, desc)
        for row, pid in enumerate(ids):
            check = view.provider_table.item(row, 0)
            assert (check.checkState() == Qt.Checked) == (pid in selected), (key, desc)
    view.close()


def test_header_click_sorts_by_the_clicked_column(qapp):
    view = FreeFallbackView()
    rows, models = _three_providers()
    view.set_snapshot(rows, models)

    view._on_header_clicked(1)   # Provider
    assert view.sort_key() == "name"
    view._on_header_clicked(10)  # Strict FREE
    assert view.sort_key() == "strict_free"
    assert view.row_provider_ids()[0] == "alpha"
    view._on_header_clicked(10)  # toggles to descending
    assert view.row_provider_ids()[0] == "beta"
    view.close()


# ---------------------------------------------------------------------- D
def test_filters_are_pure_and_never_touch_the_network(free_window, monkeypatch):
    window = free_window
    rows, models = _three_providers()
    window.free_view.set_snapshot(rows, models)

    calls = []
    monkeypatch.setattr(window.client, "get_catalog_models",
                        lambda: calls.append("local_catalog") or [])
    monkeypatch.setattr(window, "_free_local_catalog_fetch",
                        lambda: calls.append("kira") or [])

    def _http_spy(request):
        calls.append("http")
        return httpx.Response(200, json={"data": []})

    window.opencode_catalog.transport = httpx.MockTransport(_http_spy)
    policies = {pid: window.free_provider_registry.get(pid).scan_policy
                for pid in window.free_provider_registry.providers}

    window.main_tabs.setCurrentWidget(window.free_tab)
    window.free_view.txt_search.setText("ga")
    window.free_view.cb_has_free.setChecked(True)
    window.free_view.cb_cost_safe.setChecked(True)
    view = window.free_view
    view._on_filter_changed()
    view._on_header_clicked(1)
    view._on_header_clicked(10)
    view.provider_table.selectRow(0)
    view._on_bulk_select_all()
    view._on_bulk_select_none()

    assert calls == []
    assert {pid: window.free_provider_registry.get(pid).scan_policy
            for pid in window.free_provider_registry.providers} == policies
    # The filtered table only shows matching providers.
    assert view.row_provider_ids() in ([], ["gamma"])


def test_filters_select_the_expected_rows(qapp):
    view = FreeFallbackView()
    rows, models = _three_providers()
    view.set_snapshot(rows, models)

    view.cb_enabled.setChecked(True)
    view._on_filter_changed()
    assert view.row_provider_ids() == ["beta", "gamma"]

    view.cb_enabled.setChecked(False)
    view.cb_errors.setChecked(True)
    view._on_filter_changed()
    assert view.row_provider_ids() == ["alpha"]

    view.cb_errors.setChecked(False)
    view.cb_stale.setChecked(True)
    view._on_filter_changed()
    assert set(view.row_provider_ids()) == {"alpha", "beta", "gamma"}

    view.cb_stale.setChecked(False)
    view.cb_cost_safe.setChecked(True)
    view._on_filter_changed()
    assert set(view.row_provider_ids()) == {"gamma"}

    view.cb_cost_safe.setChecked(False)
    view.txt_search.setText("beta")
    view._on_filter_changed()
    assert view.row_provider_ids() == ["beta"]
    view.close()


# ---------------------------------------------------------------------- E
def test_actions_are_disabled_while_an_incompatible_scan_is_running(qapp):
    view = FreeFallbackView()
    rows, models = _three_providers()
    view.set_snapshot(rows, models)
    view.provider_table.item(0, 0).setCheckState(Qt.Checked)

    assert view.btn_scan.isEnabled() is True
    assert view.btn_metadata_scan.isEnabled() is True
    assert view.btn_validate.isEnabled() is True
    assert view.btn_stop.isEnabled() is False

    view.set_scan_state("METADATA")
    assert view.btn_scan.isEnabled() is False
    assert view.btn_metadata_scan.isEnabled() is False
    assert view.btn_validate.isEnabled() is False
    assert view.btn_stop.isEnabled() is True
    assert view.btn_sync.isEnabled() is False
    assert view.btn_select_all.isEnabled() is False

    view.set_scan_state(None)
    assert view.btn_scan.isEnabled() is True
    assert view.btn_stop.isEnabled() is False
    view.close()


def test_actions_require_a_selection(qapp):
    view = FreeFallbackView()
    rows, models = _three_providers()
    view.set_snapshot(rows, models)
    assert view.btn_scan.isEnabled() is False
    view._on_bulk_select_all()
    assert view.selected_provider_ids() == ["alpha", "beta", "gamma"]
    assert view.btn_scan.isEnabled() is True
    view.close()


# ---------------------------------------------------------------------- F
def test_provider_details_follow_the_current_selection(qapp):
    view = FreeFallbackView()
    rows, models = _three_providers()
    view.set_snapshot(rows, models)

    view.provider_table.selectRow(0)  # alpha after the default name sort
    assert view.current_provider_id() == "alpha"
    assert "ALPHA" in view.lbl_details_title.text()
    assert view.model_table.rowCount() == 1
    assert view.model_table.item(0, 0).text() == "alpha/paid"
    assert view.model_table.item(0, 2).text() == "PAID"
    assert "not granted" in view.lbl_cost.text()

    view.provider_table.selectRow(1)  # beta
    assert view.current_provider_id() == "beta"
    assert "BETA" in view.lbl_details_title.text()
    assert view.model_table.rowCount() == 2
    assert "trusted 7d left" in view.lbl_evidence.text()
    assert "TRUSTED" in view.lbl_badges.text()
    view.close()


def test_note_editor_and_policy_controls_emit_for_the_selected_provider(qapp):
    view = FreeFallbackView()
    rows, models = _three_providers()
    view.set_snapshot(rows, models)
    view.provider_table.selectRow(0)
    provider_id = view.current_provider_id()

    note_events = []
    policy_events = []
    view.note_changed.connect(lambda pid, text: note_events.append((pid, text)))
    view.policy_changed.connect(lambda pid, field, value: policy_events.append((pid, field, value)))

    view.txt_note.setPlainText("cheap lane")
    view.btn_save_note.click()
    assert note_events == [(provider_id, "cheap lane")]

    view.btn_disable.click()
    view.btn_metadata_only.click()
    view.btn_allow_live.click()
    for button, duration in zip(view._trust_buttons,
                               ["1h", "1d", "7d", "30d", "until_cleared"]):
        button.click()
        assert policy_events[-1] == (provider_id, "trusted_alive", duration)
    view.btn_clear_trust.click()
    view.btn_scan_now.click()
    assert (provider_id, "enabled", False) in policy_events
    assert (provider_id, "scan_mode", "METADATA_ONLY") in policy_events
    assert (provider_id, "scan_mode", "METADATA_AND_LIVE_PROBE") in policy_events
    assert policy_events[-1] == (provider_id, "trusted_alive", None)
    view.close()


# ---------------------------------------------------------------------- G
def test_opening_and_using_the_tab_does_no_background_work(free_window):
    window = free_window
    window.show()
    QApplication.processEvents()
    window.main_tabs.setCurrentWidget(window.free_tab)
    QApplication.processEvents()
    window.free_view._on_bulk_select_all()
    window.free_view._on_header_clicked(1)
    QApplication.processEvents()

    # No timer is left running (nothing polls, refreshes or reorders itself).
    active = [timer for timer in window.findChildren(QTimer) if timer.isActive()]
    assert active == []
    # The catalog/combo live paths were never touched.
    assert window._catalog_refresh_running is False
    assert window.main_tabs.currentWidget() is window.free_tab


def test_scan_request_emits_only_selected_provider_ids(qapp):
    view = FreeFallbackView()
    rows, models = _three_providers()
    view.set_snapshot(rows, models)
    requests = []
    view.scan_requested.connect(lambda ids, action: requests.append((ids, action)))

    view.provider_table.item(2, 0).setCheckState(Qt.Checked)  # one provider only
    selected = view.selected_provider_ids()
    view.btn_scan.click()
    view.btn_metadata_scan.click()
    view.btn_validate.click()

    assert requests == [
        (selected, "SCAN"),
        (selected, "METADATA"),
        (selected, "LIVE"),
    ]
    assert len(selected) == 1
    view.close()


# ---------------------------------------------------------------------- H
class _FakeComboClient:
    """Minimal combo server double for the SAIFREN tail-sync action."""

    def __init__(self, models=None):
        self.combos = [{
            "id": "c1", "name": "SAIFREN", "kind": None,
            "models": list(models or []), "updatedAt": "before",
        }]
        self.update_calls = []
        self.detailed_status = "OK"
        self.update_result = {"id": "c1"}

    def get_combos(self):
        return [{
            "id": c["id"], "name": c["name"], "kind": c["kind"],
            "models": list(c["models"]), "updatedAt": c["updatedAt"],
        } for c in self.combos]

    def get_combos_detailed(self):
        return (self.detailed_status, self.get_combos())

    def update_combo(self, combo_id, name, models, kind=None):
        self.update_calls.append((combo_id, name, list(models), kind))
        for combo in self.combos:
            if combo["id"] == combo_id:
                combo["models"] = list(models)
                combo["updatedAt"] = "after"
        return self.update_result


def _window_with_free_tail(free_window, monkeypatch, reliable):
    from core.combo_manager import StableCombo

    window = free_window
    registry = window.free_provider_registry
    registry.ensure_provider("prov-a", "Provider A", default_enabled=True)
    registry.record_metadata_result("prov-a", ok=True, models=[{
        "canonical_id": "prov-a/model-free",
        "upstream_model_id": "model-free",
        "provider_id": "prov-a",
        "free_evidence": "STRICT_FREE",
        "provider_health": "UNKNOWN",
        "routing": "DIRECT_ROUTABLE",
        "cost_risk": "FREE_QUOTA_PROBE",
        "scanner_managed": True,
    }])
    combo = StableCombo("c1", "SAIFREN", list(reliable))
    fake = _FakeComboClient(reliable)
    monkeypatch.setattr(window, "client", fake)
    monkeypatch.setattr(window.combo_editor, "combos", {"c1": combo})
    window._refresh_free_view()
    return window, fake, combo


def test_window_sync_appends_strict_free_only_at_the_bottom(free_window, monkeypatch):
    from PySide6.QtWidgets import QMessageBox

    reliable = ["ag/opus-4", "wb/gpt-5"]
    window, fake, combo = _window_with_free_tail(free_window, monkeypatch, reliable)
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.Yes)

    window._on_free_sync_requested()

    assert len(fake.update_calls) == 1
    applied = fake.update_calls[0][2]
    assert applied[:len(reliable)] == reliable          # reliable order intact
    assert applied[len(reliable):] == ["prov-a/model-free"]

    # Idempotent: a second explicit sync writes nothing new.
    window._on_free_sync_requested()
    assert len(fake.update_calls) == 1
    assert window.free_provider_registry.synced_ids("SAIFREN") == ["prov-a/model-free"]


def test_window_sync_is_cancellable_and_never_silently_mutates(free_window, monkeypatch):
    from PySide6.QtWidgets import QMessageBox

    window, fake, combo = _window_with_free_tail(free_window, monkeypatch, ["ag/opus-4"])
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.No)

    window._on_free_sync_requested()

    assert fake.update_calls == []
    assert combo.models == ["ag/opus-4"]
    assert window.free_provider_registry.synced_ids("SAIFREN") == []


def test_window_refuses_sync_when_live_combo_read_is_unavailable(free_window, monkeypatch):
    window, fake, combo = _window_with_free_tail(free_window, monkeypatch, ["ag/opus-4"])
    fake.detailed_status = "FAILED"

    window._on_free_sync_requested()

    assert fake.update_calls == []
    assert combo.models == ["ag/opus-4"]
    assert window.free_provider_registry.pending_tail_sync is None
    assert "live API read failed" in window.status_bar.currentMessage()


def test_window_recovers_after_backend_update_but_failed_registry_commit(
    free_window, monkeypatch,
):
    from PySide6.QtWidgets import QMessageBox

    reliable = ["ag/opus-4"]
    window, fake, _combo = _window_with_free_tail(free_window, monkeypatch, reliable)
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.Yes)
    registry = window.free_provider_registry
    real_save = registry.save
    saves = [0]

    def fail_second_save(*, force=False):
        saves[0] += 1
        if saves[0] == 2:
            registry.last_save_error = "injected registry write failure"
            return False
        return real_save(force=force)

    monkeypatch.setattr(registry, "save", fail_second_save)
    window._on_free_sync_requested()

    assert len(fake.update_calls) == 1
    assert registry.pending_tail_sync is not None
    assert registry.synced_ids("SAIFREN") == []
    assert "ownership commit failed" in window.status_bar.currentMessage()

    # A later explicit sync reads the live combo, completes the saved intent,
    # then notices there is no remaining tail delta to apply.
    window._on_free_sync_requested()
    assert registry.pending_tail_sync is None
    assert registry.synced_ids("SAIFREN") == ["prov-a/model-free"]
    assert len(fake.update_calls) == 1
    assert "already ends" in window.status_bar.currentMessage()


def test_legacy_opencode_sync_uses_shared_ownership_planner(free_window, monkeypatch):
    from types import SimpleNamespace
    from PySide6.QtWidgets import QMessageBox
    from ui import main_window as mw

    reliable = ["ag/reliable"]
    window, fake, combo = _window_with_free_tail(
        free_window, monkeypatch,
        ["ocf/model-free", *reliable, "oc/manual", "oc/model-free"],
    )
    registry = window.free_provider_registry
    registry.record_metadata_result("prov-a", ok=True, models=[{
        "canonical_id": "prov-a/model-free",
        "upstream_model_id": "model-free",
        "provider_id": "prov-a",
        "free_evidence": "PAID",
        "evidence_source": "paid_pricing_metadata",
        "provider_health": "UNKNOWN",
        "routing": "DIRECT_ROUTABLE",
        "cost_risk": "POSSIBLE_BILLING",
        "scanner_managed": True,
    }])
    registry.ensure_provider("opencode", default_enabled=True)
    registry.record_metadata_result("opencode", ok=True, models=[{
        "canonical_id": "ocf/model-free",
        "upstream_model_id": "model-free",
        "provider_id": "opencode",
        "free_evidence": "CLIENT_BOUND_FREE",
        "evidence_source": "opencode_catalog_current",
        "provider_health": "UNKNOWN",
        "routing": "LOCAL_BRIDGE_REQUIRED",
        "cost_risk": "FREE_QUOTA_PROBE",
        "scanner_managed": True,
    }])
    registry.mark_synced(["ocf/model-free"], "opencode", "SAIFREN")
    fake.combos[0]["models"] = ["ocf/model-free", *reliable, "oc/manual", "oc/model-free"]
    combo.models = list(fake.combos[0]["models"])
    ocf = SimpleNamespace(eligible_ids=lambda *_args: ["model-free"])
    bridge = SimpleNamespace(health=object())
    monkeypatch.setattr(window, "_ocf_registry", lambda: ocf)
    monkeypatch.setattr(window, "_ocf_bridge", lambda: bridge)
    monkeypatch.setattr(window, "_catalog_source_state", lambda: mw.SOURCE_AVAILABLE)
    monkeypatch.setattr(window, "_free_bridge_eligible_ids", lambda: ["ocf/model-free"])
    monkeypatch.setattr(window, "_mirror_ocf_inventory_to_free_registry", lambda _r: True)
    monkeypatch.setattr(
        mw, "plan_direct_free_migration",
        lambda models, _registry, _catalog: {"oc/model-free": "ocf/model-free"},
    )
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.Yes)

    window._on_sync_ocf_tail()

    assert len(fake.update_calls) == 1
    applied = fake.update_calls[0][2]
    assert applied == ["ag/reliable", "oc/manual", "ocf/model-free"]
    assert registry.pending_tail_sync is None
    assert registry.synced_ids("SAIFREN") == ["ocf/model-free"]


def test_window_surfaces_discovered_providers_without_a_free_adapter(free_window):
    from core.discovery import DiscoveredModel

    window = free_window
    window.discovered_models = [
        DiscoveredModel(
            canonical_id="gemini-cli/gemini-3-pro", provider_name="Gemini CLI",
            provider_prefix="gemini-cli", connection_id="gemini-cli",
            model_id="gemini-3-pro", display_name="gemini-3-pro",
        )
    ]
    window._refresh_free_view()

    row = next(row for row in window.free_provider_registry.provider_rows()
               if row["provider_id"] == "gemini-cli")
    assert row["enabled"] is False
    assert row["scan_mode"] == "DISABLED"
    assert row["scan_policy"] == "NEVER"
    assert "NO FREE ADAPTER" in row["badges"]
    # It is visible in the table -- not silently dropped.
    assert "gemini-cli" in window.free_view.row_provider_ids()

    # And a Scan Selected never touches it (disabled + NEVER policy).
    run = window._free_controller_obj().scan_selected(["gemini-cli"])
    assert run.wait(5.0)
    outcome = run.outcomes["gemini-cli"]
    assert outcome.status == "SKIPPED"
    assert outcome.reason == "disabled"
    assert outcome.network_calls == 0 and outcome.inference_calls == 0


def test_bulk_selection_actions_use_provider_identity(qapp):
    view = FreeFallbackView()
    rows, models = _three_providers()
    view.set_snapshot(rows, models)

    view.btn_select_all.click()
    assert view.selected_provider_ids() == ["alpha", "beta", "gamma"]
    view.btn_select_none.click()
    assert view.selected_provider_ids() == []
    view.btn_select_enabled.click()
    assert view.selected_provider_ids() == ["beta", "gamma"]
    view.btn_select_live.click()
    assert view.selected_provider_ids() == ["beta"]
    view.close()
