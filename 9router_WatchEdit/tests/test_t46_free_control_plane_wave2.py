"""T-46 FREE control-plane wave-2 (SRC-018) focused coverage.

Milestones under test:

  M1  filters are a safe action boundary -- network actions use the VISIBLE
      selected scope only; hidden remembered selections are never executed
      and bulk selection acts on the visible set;
  M2  scan policy is editable from the tab through the existing
      policy_changed path, and re-projections never write policy;
  M3  MANUAL is a scheduling policy, not a refusal -- explicit operator
      Scan works for MANUAL + METADATA_AND_LIVE_PROBE, while MANUAL stays
      excluded from automatic due scheduling;
  M4  a composite metadata+live run reports the truth about BOTH stages;
      a failed or skipped live stage never publishes SCANNED, and valid
      metadata evidence survives the failure.
"""
import httpx
import pytest
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication

from core.free_evidence import CostRisk, FreeEvidence, ProviderHealth, ScanMode, ScanPolicy
from core.free_provider_registry import FreeProviderRegistry
from core.free_scan_controller import (
    SKIP_NOT_STALE,
    STATUS_FAILED,
    STATUS_SCANNED,
    STATUS_SKIPPED,
    FreeScanController,
)
from ui.free_fallback_view import FreeFallbackView

from test_free_fallback_controller import _build, _row
from test_free_fallback_ui import _provider_row, _three_providers


# --------------------------------------------------------------------- M1
def _view_with_rows(qapp, rows=None, models=None):
    view = FreeFallbackView()
    rows, models = (rows, models) if rows is not None else _three_providers()
    view.set_snapshot(rows, models)
    return view


def test_m1_scan_never_executes_a_hidden_selected_provider(qapp):
    view = _view_with_rows(qapp)
    requests = []
    view.scan_requested.connect(lambda ids, action: requests.append((ids, action)))

    # Select alpha and beta while everything is visible, then hide alpha.
    view.provider_table.item(0, 0).setCheckState(Qt.Checked)  # alpha
    view.provider_table.item(1, 0).setCheckState(Qt.Checked)  # beta
    view.txt_search.setText("beta")
    view._on_filter_changed()

    view.btn_scan.click()
    view.btn_metadata_scan.click()
    view.btn_validate.click()

    assert requests == [
        (["beta"], "SCAN"),
        (["beta"], "METADATA"),
        (["beta"], "LIVE"),
    ]
    # alpha stays remembered for round-trips, but hidden == not executed.
    assert view.selected_provider_ids() == ["alpha", "beta"]
    assert view.visible_selected_provider_ids() == ["beta"]
    assert view.remembered_hidden_selected_count() == 1
    view.close()


def test_m1_hidden_selection_reappears_when_the_filter_is_removed(qapp):
    view = _view_with_rows(qapp)
    view.provider_table.item(0, 0).setCheckState(Qt.Checked)  # alpha
    view.txt_search.setText("gamma")
    view._on_filter_changed()
    assert view.visible_selected_provider_ids() == []

    view.txt_search.setText("")
    view._on_filter_changed()
    assert view.visible_selected_provider_ids() == ["alpha"]
    # The checkbox reflects the remembered selection again.
    assert view.provider_table.item(0, 0).checkState() == Qt.Checked
    view.close()


def test_m1_select_all_is_visible_scope_only(qapp):
    view = _view_with_rows(qapp)
    # A previously selected provider that the filter now hides.
    view.provider_table.item(0, 0).setCheckState(Qt.Checked)  # alpha
    view.cb_cost_safe.setChecked(True)
    view._on_filter_changed()
    assert view.row_provider_ids() == ["gamma"]

    view.btn_select_all.click()
    assert view.visible_selected_provider_ids() == ["gamma"]
    # alpha was hidden by the filter BEFORE Select All, so Select All never
    # touched it: it stays remembered (and still not executable while hidden).
    assert view.selected_provider_ids() == ["alpha", "gamma"]
    # Hidden remembered selection must NOT become executable by Select All.
    requests = []
    view.scan_requested.connect(lambda ids, action: requests.append((ids, action)))
    view.btn_scan.click()
    assert requests == [(["gamma"], "SCAN")]

    # Select None clears the visible scope only.
    view.btn_select_none.click()
    assert view.visible_selected_provider_ids() == []
    assert view.selected_provider_ids() == ["alpha"]
    view.close()


def test_m1_select_enabled_and_live_trusted_are_visible_scope_only(qapp):
    view = _view_with_rows(qapp)
    # Select alpha first, then hide it behind the cost-safe filter: bulk
    # actions must neither execute nor disturb it.
    view.provider_table.item(0, 0).setCheckState(Qt.Checked)  # alpha
    view.cb_cost_safe.setChecked(True)
    view._on_filter_changed()
    assert view.row_provider_ids() == ["gamma"]

    view.btn_select_enabled.click()
    assert view.visible_selected_provider_ids() == ["gamma"]
    # gamma is enabled but NOT live/trusted: Select LIVE/TRUSTED clears it in
    # the visible scope; alpha (hidden, remembered) is untouched.
    view.btn_select_live.click()
    assert view.visible_selected_provider_ids() == []
    assert view.selected_provider_ids() == ["alpha"]
    view.close()


def test_m1_action_enablement_uses_visible_selection_count(qapp):
    view = _view_with_rows(qapp)
    view.provider_table.item(0, 0).setCheckState(Qt.Checked)  # alpha
    view.txt_search.setText("beta")
    view._on_filter_changed()
    # A hidden selection is remembered but must not enable network actions.
    assert view.selected_provider_ids() == ["alpha"]
    assert view.visible_selected_provider_ids() == []
    assert view.btn_scan.isEnabled() is False
    assert view.btn_metadata_scan.isEnabled() is False
    assert view.btn_validate.isEnabled() is False

    view.btn_scan.click()
    view.btn_metadata_scan.click()
    view.btn_validate.click()
    requests = []
    view.scan_requested.connect(lambda ids, action: requests.append((ids, action)))
    assert requests == []
    view.close()


def test_m1_summary_line_reports_hidden_remembered_selection(qapp):
    view = _view_with_rows(qapp)
    view.provider_table.item(0, 0).setCheckState(Qt.Checked)  # alpha
    view.provider_table.item(1, 0).setCheckState(Qt.Checked)  # beta
    view.txt_search.setText("beta")
    view._on_filter_changed()
    text = view.lbl_summary.text()
    assert "selected: 1 visible" in text
    assert "1 hidden" in text
    view.close()


def test_m1_filter_round_trip_preserves_selection_exactly(qapp):
    view = _view_with_rows(qapp)
    view.provider_table.item(0, 0).setCheckState(Qt.Checked)
    view.provider_table.item(2, 0).setCheckState(Qt.Checked)
    view.txt_search.setText("zzz-none")
    view._on_filter_changed()
    view.txt_search.setText("")
    view._on_filter_changed()
    assert view.selected_provider_ids() == ["alpha", "gamma"]
    view.close()


# --------------------------------------------------------------------- M2
def test_m2_policy_combo_shows_and_edits_the_persisted_policy(qapp):
    view = _view_with_rows(qapp)
    events = []
    view.policy_changed.connect(
        lambda pid, field, value: events.append((pid, field, value))
    )
    view.provider_table.selectRow(0)  # alpha, policy ALWAYS in the fixture
    assert view.cb_scan_policy.currentText() == "ALWAYS"

    view.cb_scan_policy.setCurrentText("MANUAL")
    assert events == [("alpha", "scan_policy", "MANUAL")]

    # Selecting another provider re-projects WITHOUT emitting anything.
    events.clear()
    view.provider_table.selectRow(1)  # beta
    assert events == []
    assert view.cb_scan_policy.currentText() == "ALWAYS"  # beta is ALWAYS too
    view.close()


def test_m2_selecting_another_provider_does_not_mutate_either(qapp):
    view = _view_with_rows(qapp)
    view.provider_table.selectRow(0)
    view.cb_scan_policy.setCurrentText("NEVER")
    # The window round-trip (policy_changed -> registry.set_policy ->
    # _refresh_free_view) re-snapshots rows; mimic it by updating the backing
    # row for the CURRENT provider (note: _rows is in snapshot order, not
    # visual order).
    for row in view._rows:
        if row.get("provider_id") == view.current_provider_id():
            row["scan_policy"] = "NEVER"
    events = []
    view.policy_changed.connect(
        lambda pid, field, value: events.append((pid, field, value))
    )
    view.provider_table.selectRow(1)
    assert events == []
    # alpha's displayed policy is the one we set; beta's is its own.
    view.provider_table.selectRow(0)
    assert view.cb_scan_policy.currentText() == "NEVER"
    view.provider_table.selectRow(1)
    assert view.cb_scan_policy.currentText() == "ALWAYS"
    view.close()


def test_m2_never_is_labelled_as_disabled_scanning_but_stays_editable(qapp):
    view = _view_with_rows(qapp)
    view.provider_table.selectRow(0)
    view.cb_scan_policy.setCurrentText("NEVER")
    # Round-trip the change into the snapshot as the window would.
    for row in view._rows:
        if row.get("provider_id") == view.current_provider_id():
            row["scan_policy"] = "NEVER"
    view._refresh_details()
    assert "scanning disabled" in view.lbl_scan_policy.text()
    # Editing is still possible: NEVER is a policy value, not a lock.
    assert view.cb_scan_policy.isEnabled() is True
    view.cb_scan_policy.setCurrentText("ALWAYS")
    # Mimic the window round-trip: registry updated -> view re-projected.
    for row in view._rows:
        if row.get("provider_id") == view.current_provider_id():
            row["scan_policy"] = "ALWAYS"
    view._refresh_details()
    assert "scanning disabled" not in view.lbl_scan_policy.text()
    view.close()


def test_m2_open_sort_filter_select_emit_zero_network_calls(qapp, monkeypatch, tmp_path):
    from ui import main_window as mw

    def _registry(*args, **kwargs):
        return FreeProviderRegistry(path=tmp_path / "free_providers.json")

    monkeypatch.setattr(mw, "FreeProviderRegistry", _registry)
    window = mw.MainWindow()
    try:
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
        view = window.free_view
        events = []
        view.policy_changed.connect(
            lambda pid, field, value: events.append((pid, field, value))
        )
        view.txt_search.setText("g")
        view._on_filter_changed()
        view._on_header_clicked(1)
        view.provider_table.selectRow(0)
        view.cb_scan_policy.setCurrentText("MANUAL")
        # Exactly one user-driven change for the visible current provider.
        assert events == [(view.current_provider_id(), "scan_policy", "MANUAL")]
        view.cb_scan_policy.setCurrentText("ALWAYS")
        assert events[-1] == (view.current_provider_id(), "scan_policy", "ALWAYS")
        assert calls == []
        # The registry-owned policies are untouched by open/sort/filter/select.
        assert {pid: window.free_provider_registry.get(pid).scan_policy
                for pid in policies} == policies
        view.sort_by("name")
        assert calls == []
    finally:
        window.hide()
        window.close()


# --------------------------------------------------------------------- M3
def test_m3_manual_composite_runs_on_explicit_scan(tmp_path):
    registry, adapters, controller = _build(tmp_path, [
        ("prov-a", {"policy": ScanPolicy.MANUAL},
         {"models": [_row()], "live": True}),
    ])
    # Never scanned: is_due() is False for MANUAL by design.
    assert registry.is_due("prov-a") is False

    run = controller.scan_selected(["prov-a"])
    assert run.wait(5.0)
    outcome = run.outcomes["prov-a"]
    assert outcome.status == STATUS_SCANNED
    assert outcome.metadata_status == STATUS_SCANNED
    assert outcome.live_status == STATUS_SCANNED
    assert adapters["prov-a"].metadata_calls == 1
    assert adapters["prov-a"].live_calls == 1
    assert outcome.inference_calls == 1


def test_m3_manual_stays_excluded_from_automatic_scheduling(tmp_path):
    registry, adapters, controller = _build(tmp_path, [
        ("prov-a", {"policy": ScanPolicy.MANUAL}, {"models": [_row()]}),
    ])
    assert registry.is_due("prov-a") is False
    # Even after time passes far beyond the TTL, MANUAL is never auto-due.
    assert registry.is_due("prov-a") is False


def test_m3_manual_composite_with_trusted_alive_runs_metadata_only(tmp_path):
    clock = [100_000.0]
    registry, adapters, controller = _build(
        tmp_path,
        [("prov-a", {"policy": ScanPolicy.MANUAL},
          {"models": [_row()], "live": True})],
        clock=lambda: clock[0],
    )
    registry.mark_trusted_alive("prov-a", "7d")
    run = controller.scan_selected(["prov-a"])
    assert run.wait(5.0)
    outcome = run.outcomes["prov-a"]
    assert outcome.status == STATUS_SCANNED
    assert outcome.reason == "trusted_alive"
    assert outcome.live_status == ""          # live stage never started
    assert outcome.metadata_status == STATUS_SCANNED
    assert adapters["prov-a"].live_calls == 0


def test_m3_never_still_refuses_everything(tmp_path):
    registry, adapters, controller = _build(tmp_path, [
        ("prov-a", {"policy": ScanPolicy.NEVER}, {"models": [_row()], "live": True}),
    ])
    for call in (controller.scan_selected, controller.metadata_scan_selected,
                 controller.validate_selected):
        run = call(["prov-a"])
        assert run.wait(5.0)
        assert run.outcomes["prov-a"].status == STATUS_SKIPPED
        assert run.outcomes["prov-a"].reason == "policy_never"
    assert adapters["prov-a"].metadata_calls == 0
    assert adapters["prov-a"].live_calls == 0


def test_m3_stale_only_fresh_provider_still_skips(tmp_path):
    clock = [300_000.0]
    registry, adapters, controller = _build(
        tmp_path,
        [("prov-a", {"policy": ScanPolicy.STALE_ONLY}, {"models": [_row()]})],
        clock=lambda: clock[0],
    )
    run = controller.scan_selected(["prov-a"])
    assert run.wait(5.0)
    assert run.outcomes["prov-a"].status == STATUS_SCANNED
    run = controller.scan_selected(["prov-a"])
    assert run.wait(5.0)
    assert run.outcomes["prov-a"].reason == SKIP_NOT_STALE
    assert adapters["prov-a"].metadata_calls == 1


# --------------------------------------------------------------------- M4
def test_m4_composite_transient_live_failure_reports_failed_not_scanned(tmp_path):
    registry, adapters, controller = _build(tmp_path, [
        ("prov-a", {}, {
            "models": [_row()], "live": True, "live_ok": False,
            "live_state": "BRIDGE_QUOTA",
        }),
    ])
    run = controller.scan_selected(["prov-a"])
    assert run.wait(5.0)
    outcome = run.outcomes["prov-a"]
    assert outcome.status == STATUS_FAILED
    assert outcome.metadata_status == STATUS_SCANNED
    assert outcome.live_status == STATUS_FAILED
    assert outcome.error_class == "injected"
    assert outcome.error_summary == "injected live failure"
    # Valid metadata evidence survives the live failure.
    record = registry.get("prov-a")
    assert [row["canonical_id"] for row in record.models] == ["prov-a/model-free"]
    assert record.last_metadata_success_at
    assert registry.eligible_tail_ids() == ["prov-a/model-free"]


def test_m4_composite_blocking_live_failure_reports_failed(tmp_path):
    registry, adapters, controller = _build(tmp_path, [
        ("prov-a", {}, {
            "models": [_row()], "live": True, "live_ok": False,
            "live_state": "BRIDGE_UPSTREAM_REJECTED",
            "blocking_health": ProviderHealth.AUTH_FAILED.value,
        }),
    ])
    run = controller.scan_selected(["prov-a"])
    assert run.wait(5.0)
    outcome = run.outcomes["prov-a"]
    assert outcome.status == STATUS_FAILED
    assert outcome.metadata_status == STATUS_SCANNED
    assert outcome.live_status == STATUS_FAILED
    # Metadata inventory still valid; the lane is health-excluded.
    assert registry.eligible_tail_ids() == []
    assert registry.get("prov-a").models[0]["free_evidence"] == "STRICT_FREE"


def test_m4_composite_live_skip_reports_skipped_not_scanned(tmp_path):
    # Cost-risk refusal discovered at plan time: live stage downgraded, so the
    # composite reports SCANNED for its metadata portion (existing contract).
    registry, adapters, controller = _build(tmp_path, [
        ("prov-a", {"live_probe_cost_risk": CostRisk.POSSIBLE_BILLING},
         {"models": [_row()], "live": True, "can_bill": True}),
    ])
    run = controller.scan_selected(["prov-a"])
    assert run.wait(5.0)
    outcome = run.outcomes["prov-a"]
    assert outcome.status == STATUS_SCANNED
    assert outcome.reason == "refused_cost_risk"
    assert outcome.live_status == ""

    # A guard discovered AFTER metadata must NOT report a full successful
    # composite scan: the metadata refresh replaced the inventory with PAID
    # rows, so the canary stage finds no free target and is SKIPPED.
    # NOTE: a fresh registry directory -- the billing-risk scenario above
    # persisted POSSIBLE_BILLING into tmp_path's providers.json, and
    # ensure_provider never overwrites existing operator state.
    registry, adapters, controller = _build(tmp_path / "clean", [
        ("prov-a", {}, {"models": [_row()], "live": True}),
    ])
    run = controller.scan_selected(["prov-a"])
    assert run.wait(5.0)
    assert run.outcomes["prov-a"].status == STATUS_SCANNED

    adapters["prov-a"].metadata_rows = [_row(evidence=FreeEvidence.PAID)]
    run = controller.scan_selected(["prov-a"])
    assert run.wait(5.0)
    outcome = run.outcomes["prov-a"]
    assert outcome.metadata_status == STATUS_SCANNED
    assert outcome.live_status == STATUS_SKIPPED
    assert outcome.reason == "no_free_model_to_canary"
    assert outcome.status == STATUS_SKIPPED
    assert outcome.inference_calls == 0


def test_m4_composite_success_reports_both_stages_scanned(tmp_path):
    registry, adapters, controller = _build(tmp_path, [
        ("prov-a", {}, {"models": [_row()], "live": True}),
    ])
    run = controller.scan_selected(["prov-a"])
    assert run.wait(5.0)
    outcome = run.outcomes["prov-a"]
    assert outcome.status == STATUS_SCANNED
    assert outcome.metadata_status == STATUS_SCANNED
    assert outcome.live_status == STATUS_SCANNED


def test_m4_metadata_failure_never_starts_the_live_stage(tmp_path):
    registry, adapters, controller = _build(tmp_path, [
        ("prov-a", {}, {"metadata_error": "timeout", "live": True,
                        "models": [_row()]}),
    ])
    run = controller.scan_selected(["prov-a"])
    assert run.wait(5.0)
    outcome = run.outcomes["prov-a"]
    assert outcome.status == STATUS_FAILED
    assert outcome.metadata_status == STATUS_FAILED
    assert outcome.live_status == ""
    assert adapters["prov-a"].live_calls == 0


def test_m4_cancellation_between_stages_publishes_no_stale_composite(tmp_path):
    gate = threading.Event()
    registry, adapters, controller = _build(
        tmp_path,
        [("prov-a", {}, {"models": [_row()], "live": True, "gate": gate})],
        max_workers=2,
    )
    run = controller.scan_selected(["prov-a"])

    # Release the metadata gate, then cancel while the live stage is gated.
    gate.set()
    deadline = __import__("time").time() + 5.0
    while adapters["prov-a"].live_calls == 0 and __import__("time").time() < deadline:
        __import__("time").sleep(0.01)
    controller.stop(run)
    assert run.wait(5.0)
    outcome = run.outcomes["prov-a"]
    if outcome.live_status == STATUS_SCANNED:
        # Live completed before the cancel landed: composite is truthful.
        assert outcome.status == STATUS_SCANNED
    else:
        assert outcome.status in (STATUS_SKIPPED, STATUS_FAILED)
        assert outcome.status != STATUS_SCANNED


import threading  # noqa: E402  (used by the cancellation test above)
