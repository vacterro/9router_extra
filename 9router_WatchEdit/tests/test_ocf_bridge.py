"""
OCF-001 regression suite - local OpenCode Free bridge (no real quota used).

A fake `opencode` executable/process harness supplies deterministic JSON event
streams, so the unit suite never touches the live free tier.
"""
import json
import os
import stat
import sys
import threading
import time
from pathlib import Path

import pytest

from core.classification import AvailabilityState, classify_probe_result
from core.ocf_registry import (
    DIRECT_LOCAL_BRIDGE_REQUIRED,
    DIRECT_ROUTABLE,
    OcfRegistry,
    SOURCE_AVAILABLE,
    SOURCE_UNAVAILABLE,
    canonical_for,
    plan_direct_free_migration,
    sync_saifren_bottom,
)
from core.opencode_bridge import (
    BRIDGE_BROKEN,
    BRIDGE_CANCELLED,
    BRIDGE_OK,
    BRIDGE_QUOTA,
    BRIDGE_RUNTIME_MISSING,
    BRIDGE_TIMEOUT,
    BRIDGE_UPSTREAM_REJECTED,
    UNSUPPORTED_BY_OPENCODE_FREE_BRIDGE,
    BridgeContext,
    BridgeHealth,
    BridgeRuntime,
    OpenCodeBridge,
    classify_request_shape,
    cli_model_id,
    resolve_runtime,
    serialize_messages,
)
from core.opencode_bridge_server import OcfBridgeService, _Handler

FREE_MODEL = "fake-free-model"
CLI_SCENARIOS = {
    "ok": [
        {"type": "step_start", "sessionID": "ses_fake_1"},
        {"type": "text", "sessionID": "ses_fake_1", "part": {"type": "text", "text": "OK"}},
        {"type": "step_finish", "sessionID": "ses_fake_1", "part": {"reason": "stop"}},
    ],
    "unicode": [
        {"type": "text", "sessionID": "ses_fake_2",
         "part": {"type": "text", "text": "ok ✓ tere õäöü 日本語"}},
    ],
    "quota": [
        {"type": "error", "sessionID": "ses_fake_3",
         "error": {"name": "APIError", "data": {
             "message": "FreeUsageLimitError: free usage limit reached, retry later",
             "statusCode": 429}}},
    ],
    "upstream_rejected": [
        {"type": "error", "sessionID": "ses_fake_4",
         "error": {"name": "APIError", "data": {
             "message": "Error from provider (Console): OpenCode's free tier can only be used from within OpenCode",
             "statusCode": 403,
             "responseBody": json.dumps({"type": "error", "error": {
                 "type": "FreeTierError", "message": "client bound"}})}}},
    ],
    "model_unavailable": [
        {"type": "error", "sessionID": "ses_fake_5",
         "error": {"name": "ProviderModelNotFoundError",
                   "data": {"message": "Model not found: opencode/gone-free."}}},
    ],
    "malformed": [
        "{not json at all",
        "",
        "[]",
    ],
    "tool_attempt": [
        {"type": "text", "sessionID": "ses_fake_6",
         "part": {"type": "text", "text": "done"}},
        {"type": "tool_use", "sessionID": "ses_fake_6",
         "part": {"tool": "bash", "state": {
             "status": "error",
             "error": "The user rejected permission to use this specific tool call."}}},
    ],
    # Observed live shape: a rejected tool call ENDS the official turn with no
    # assistant text at all (opencode 1.18.31 + free model tool reflex).
    "tool_only": [
        {"type": "tool_use", "sessionID": "ses_fake_7",
         "part": {"tool": "skill", "state": {
             "status": "error",
             "error": "The user rejected permission to use this specific tool call."}}},
        {"type": "step_finish", "sessionID": "ses_fake_7",
         "part": {"reason": "tool-calls"}},
    ],
}


def _write_fake_cli(tmp_path: Path, scenario: str, *, delay: float = 0.0,
                    exit_code: int = 0, record: Path = None,
                    sequence=None) -> Path:
    """Create a fake `opencode` executable that emits one scenario's events.

    `sequence` makes the fake emit a DIFFERENT scenario per invocation
    (1st call, 2nd call, ...) so bounded-retry behaviour is observable."""
    script = tmp_path / f"opencode_fake_{scenario}.py"
    events = CLI_SCENARIOS.get(scenario, [])
    counter = tmp_path / f"counter_{scenario}.txt"
    script.write_text(
        "import json, sys, time\n"
        "import os\n"
        "sys.stdout.reconfigure(encoding='utf-8')\n"
        f"scenarios = {CLI_SCENARIOS!r}\n"
        f"sequence = {list(sequence) if sequence else None!r}\n"
        f"events = {events!r}\n"
        f"delay = {delay!r}\n"
        f"record = {str(record)!r}\n"
        f"counter = {str(counter)!r}\n"
        "argv = sys.argv[1:]\n"
        "if '--version' in argv:\n"
        "    print('9.9.9-fake')\n"
        "    sys.exit(0)\n"
        "if record:\n"
        "    open(record, 'a', encoding='utf-8').write(json.dumps(argv) + '\\n')\n"
        "if sequence:\n"
        "    index = 0\n"
        "    if os.path.exists(counter):\n"
        "        index = int(open(counter, encoding='utf-8').read().strip() or 0)\n"
        "    open(counter, 'w', encoding='utf-8').write(str(index + 1))\n"
        "    name = sequence[min(index, len(sequence) - 1)]\n"
        "    events = scenarios.get(name, [])\n"
        "if delay:\n"
        "    time.sleep(delay)\n"
        "for event in events:\n"
        "    print(json.dumps(event, ensure_ascii=False), flush=True)\n"
        f"sys.exit({exit_code})\n",
        encoding="utf-8",
    )
    launcher = tmp_path / f"opencode_{scenario}.cmd"
    launcher.write_text(
        f'@echo off\r\n"{sys.executable}" "{script}" %*\r\n', encoding="utf-8",
    )
    if os.name != "nt":
        posix = tmp_path / f"opencode_{scenario}"
        posix.write_text(
            f'#!/bin/sh\nexec "{sys.executable}" "{script}" "$@"\n', encoding="utf-8",
        )
        posix.chmod(posix.stat().st_mode | stat.S_IEXEC)
        return posix
    return launcher


def _bridge(tmp_path: Path, scenario: str, models=(FREE_MODEL,), **kwargs) -> OpenCodeBridge:
    exe = _write_fake_cli(tmp_path, scenario, **kwargs)
    context = BridgeContext(tmp_path / "bridge_root")
    assert context.ensure()
    health = BridgeHealth(context.health_file)
    runtime = BridgeRuntime(executable=str(exe), source="fake")
    return OpenCodeBridge(
        context=context, runtime=runtime, health=health,
        allowed_models=list(models) or None, timeout=kwargs.pop("timeout", 10.0),
    )


def _payload(**overrides):
    body = {
        "model": f"ocf/{FREE_MODEL}",
        "messages": [
            {"role": "system", "content": "be terse"},
            {"role": "user", "content": "say OK"},
        ],
        "stream": False,
    }
    body.update(overrides)
    return body


# ---------------------------------------------------------------- A: 403 class
def test_direct_free_tier_403_is_client_bound_not_auth_or_dead():
    body = json.dumps({"type": "error", "error": {
        "type": "FreeTierError",
        "message": "Error from provider (Console): OpenCode's free tier can only be used from within OpenCode",
    }})
    record = classify_probe_result(
        status_code=403, raw_body=body, provider_prefix="oc", model_id="fake-free-model",
    )
    assert record.availability == AvailabilityState.CLIENT_BOUND_FREE_TIER
    assert record.availability != AvailabilityState.AUTH_REJECTED
    assert record.availability != AvailabilityState.ACCESS_FORBIDDEN
    assert record.availability != AvailabilityState.DEAD
    assert record.counters.consecutive_auth == 0
    assert record.counters.consecutive_model_missing == 0
    assert record.cost.value == "FREE"


def test_client_bound_detection_does_not_steal_plain_403():
    record = classify_probe_result(
        status_code=403, raw_body=json.dumps({"error": {"message": "Access forbidden"}}),
    )
    assert record.availability == AvailabilityState.ACCESS_FORBIDDEN


def test_client_bound_never_deletes_catalog_entry():
    """Classification is evidence only: the catalog row survives."""
    from core.opencode_catalog import parse_catalog

    models = parse_catalog({"models": [{"id": "fake-free-model"}]})
    record = classify_probe_result(
        status_code=403,
        raw_body="FreeTierError: OpenCode's free tier can only be used from within OpenCode",
    )
    assert record.availability == AvailabilityState.CLIENT_BOUND_FREE_TIER
    assert [m.model_id for m in models] == ["fake-free-model"]


# --------------------------------------------------------------- B: success
def test_official_bridge_success_returns_exact_text(tmp_path):
    bridge = _bridge(tmp_path, "ok")
    result = bridge.complete(FREE_MODEL, _payload()["messages"])
    assert result.state == BRIDGE_OK
    assert result.text == "OK"
    assert result.session_id == "ses_fake_1"
    assert result.version == "9.9.9-fake"


def test_bridge_maps_ocf_namespace_to_opencode_cli_model(tmp_path):
    record = tmp_path / "argv.log"
    bridge = _bridge(tmp_path, "ok", record=record)
    assert cli_model_id("ocf/foo") == "opencode/foo"
    assert cli_model_id("opencode/foo") == "opencode/foo"
    assert cli_model_id("foo") == "opencode/foo"
    result = bridge.complete("ocf/fake-free-model", _payload()["messages"])
    assert result.ok
    argv = json.loads(record.read_text(encoding="utf-8").splitlines()[0])
    # argv[0] is the `run` subcommand: the executable itself is the launcher
    # (shell=False, argv array -- no shell interpolation).
    assert argv[0] == "run"
    assert "-m" in argv and argv[argv.index("-m") + 1] == "opencode/fake-free-model"
    assert "--format" in argv and argv[argv.index("--format") + 1] == "json"
    assert bridge.runtime.executable


def test_bridge_output_is_unicode_safe(tmp_path):
    bridge = _bridge(tmp_path, "unicode")
    result = bridge.complete(FREE_MODEL, _payload()["messages"])
    assert result.state == BRIDGE_OK
    assert "õäöü" in result.text and "日本語" in result.text


def test_bridge_never_leaks_secrets_into_diagnostics(tmp_path):
    # Assembled at runtime: the repository tree must stay credential-shaped
    # literal free for the secret scanners (GATE 25).
    secret = "sk-" + "live-" + ("ab" * 12)
    bridge = _bridge(tmp_path, "upstream_rejected")
    result = bridge.complete(FREE_MODEL, [{"role": "user", "content": f"my key {secret}"}])
    payload = json.dumps(result.as_dict())
    assert secret not in payload
    assert secret not in json.dumps(bridge.diagnostics())
    assert result.state == BRIDGE_UPSTREAM_REJECTED
    assert "--user-agent" not in json.dumps(result.as_dict())


def test_bridge_prompt_contains_no_forged_client_attestation(tmp_path):
    """The bridge never constructs remote requests: only argv reaches the CLI."""
    record = tmp_path / "argv.log"
    bridge = _bridge(tmp_path, "ok", record=record)
    bridge.complete(FREE_MODEL, _payload()["messages"])
    argv_text = record.read_text(encoding="utf-8")
    for forged in ("x-opencode-session", "x-opencode-client", "user-agent", "bearer public"):
        assert forged not in argv_text.lower()


# ------------------------------------------------------- C: tool-bearing request
class _CountingBridge(OpenCodeBridge):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.child_runs = 0

    def _run(self, model_id, prompt, timeout, cancel_event):
        self.child_runs += 1
        return super()._run(model_id, prompt, timeout, cancel_event)


def test_tool_bearing_request_spawns_zero_child_processes(tmp_path):
    bridge = _bridge(tmp_path, "ok")
    counting = _CountingBridge(context=bridge.context, runtime=bridge.runtime,
                              health=bridge.health, allowed_models=[FREE_MODEL])
    result = counting.run_request(_payload(tools=[{"type": "function", "function": {"name": "x"}}]))
    assert result.state == UNSUPPORTED_BY_OPENCODE_FREE_BRIDGE
    assert result.error_class == "tools_present"
    assert counting.child_runs == 0
    forced = counting.run_request(_payload(tool_choice="required"))
    assert forced.state == UNSUPPORTED_BY_OPENCODE_FREE_BRIDGE
    assert counting.child_runs == 0


@pytest.mark.parametrize(("payload_extra", "reason"), [
    ({"messages": [{"role": "user", "content": [{"type": "image_url", "image_url": {"url": "x"}}]}]},
     "non_text_part:image_url"),
    ({"messages": [{"role": "user", "content": [{"type": "input_audio"}]}]},
     "non_text_part:input_audio"),
    ({"response_format": {"type": "json_schema", "json_schema": {"name": "s", "schema": {}}}},
     "structured_output_schema_required"),
    ({"stream": True}, "stream_not_supported"),
    ({"n": 2}, "multiple_choices_requested"),
])
def test_unsupported_shapes_are_typed_and_skip_opencode(tmp_path, payload_extra, reason):
    bridge = _bridge(tmp_path, "ok")
    counting = _CountingBridge(context=bridge.context, runtime=bridge.runtime,
                              health=bridge.health, allowed_models=[FREE_MODEL])
    result = counting.run_request(_payload(**payload_extra))
    assert result.state == UNSUPPORTED_BY_OPENCODE_FREE_BRIDGE
    assert result.error_class == reason
    assert counting.child_runs == 0


def test_shape_gate_accepts_plain_text_history():
    eligible, reason = classify_request_shape(_payload())
    assert eligible and reason == ""
    eligible, reason = classify_request_shape(_payload(
        messages=[{"role": "assistant", "content": "prior"}, {"role": "user", "content": "next"}],
    ))
    assert eligible


def test_prompt_serialisation_is_deterministic_and_injective():
    messages = [
        {"role": "system", "content": "ROLE:USER BYTES:99"},
        {"role": "user", "content": "say OK"},
        {"role": "assistant", "content": "OK"},
    ]
    first = serialize_messages(messages)
    assert first == serialize_messages(messages)
    assert "END-TRANSCRIPT" in first
    assert first.endswith("instructions.")  # the task block is LAST
    assert first.count("TASK:") == 1
    # The injected lookalike boundary cannot forge a second transcript header.
    assert first.count("TRANSCRIPT:") == 1
    assert first.count("9R-BRIDGE/1") == 1


# --------------------------------------------------------- D: stream / cancel
def test_stream_requests_are_rejected_not_faked(tmp_path):
    bridge = _bridge(tmp_path, "ok")
    status, body = _service(bridge).chat_completion(_payload(stream=True))
    assert status == 400
    assert body["error"]["code"] == UNSUPPORTED_BY_OPENCODE_FREE_BRIDGE
    assert "choices" not in body


def test_advertised_models_declare_non_stream_only(tmp_path):
    bridge = _bridge(tmp_path, "ok")
    payload = _service(bridge).models_payload()
    assert payload["x_ocf"]["streaming"] is False
    assert payload["data"][0]["streaming"] is False
    assert payload["data"][0]["id"] == FREE_MODEL


def test_cancellation_kills_child_process_tree(tmp_path):
    bridge = _bridge(tmp_path, "ok", delay=30.0)
    cancel = threading.Event()
    holder = {}

    def _run():
        holder["result"] = bridge.complete(FREE_MODEL, _payload()["messages"], cancel_event=cancel)

    worker = threading.Thread(target=_run, daemon=True)
    started = time.time()
    worker.start()
    time.sleep(1.0)
    cancel.set()
    worker.join(timeout=20)
    assert not worker.is_alive(), "cancellation must terminate the child"
    assert time.time() - started < 20
    assert holder["result"].state == BRIDGE_CANCELLED
    assert holder["result"].error_class == "cancelled"
    assert bridge.health.model_status(FREE_MODEL) == {"blocked": False}


def test_timeout_is_bounded(tmp_path):
    bridge = _bridge(tmp_path, "ok", delay=30.0)
    result = bridge.complete(FREE_MODEL, _payload()["messages"], timeout=0.6)
    assert result.state == BRIDGE_TIMEOUT
    assert result.duration_ms < 10_000


# ----------------------------------------------------------------- E: quota
def test_quota_state_cools_down_and_blocks_further_children(tmp_path):
    bridge = _bridge(tmp_path, "quota")
    counting = _CountingBridge(context=bridge.context, runtime=bridge.runtime,
                              health=bridge.health, allowed_models=[FREE_MODEL])
    first = counting.complete(FREE_MODEL, _payload()["messages"])
    assert first.state == BRIDGE_QUOTA
    assert counting.child_runs == 1
    second = counting.complete(FREE_MODEL, _payload()["messages"])
    assert second.error_class == "model_cooldown"
    assert counting.child_runs == 1, "cooldown must not spawn another child"
    assert bridge.health.model_blocked(FREE_MODEL) is True


# -------------------------------------------- F: upstream free-tier rejection
def test_upstream_free_tier_rejection_is_reported_and_cooled(tmp_path):
    bridge = _bridge(tmp_path, "upstream_rejected")
    counting = _CountingBridge(context=bridge.context, runtime=bridge.runtime,
                              health=bridge.health, allowed_models=[FREE_MODEL])
    result = counting.complete(FREE_MODEL, _payload()["messages"])
    assert result.state == BRIDGE_UPSTREAM_REJECTED
    assert result.error_class == "client_bound_free_tier"
    # No header/attestation retry: exactly one child, then a provider cooldown.
    assert counting.child_runs == 1
    again = counting.complete(FREE_MODEL, _payload()["messages"])
    assert again.state == BRIDGE_UPSTREAM_REJECTED
    assert counting.child_runs == 1
    assert bridge.health.provider_blocked() is True


def test_upstream_rejection_serves_a_fallback_status(tmp_path):
    bridge = _bridge(tmp_path, "upstream_rejected")
    status, body = _service(bridge).chat_completion(_payload())
    assert status == 503
    assert body["error"]["code"] == "CLIENT_BOUND_FREE_TIER"
    assert body["error"]["retryable"] is True


# ------------------------------------------------------- runtime / broken paths
def test_missing_runtime_is_reported_without_crashing(tmp_path):
    context = BridgeContext(tmp_path / "bridge_root")
    assert context.ensure()
    bridge = OpenCodeBridge(
        context=context, runtime=BridgeRuntime(), health=BridgeHealth(context.health_file),
        allowed_models=[FREE_MODEL],
    )
    result = bridge.complete(FREE_MODEL, _payload()["messages"])
    assert result.state == BRIDGE_RUNTIME_MISSING
    status, body = _service(bridge).chat_completion(_payload())
    assert status == 503
    assert body["error"]["code"] == "BRIDGE_RUNTIME_MISSING"


def test_malformed_output_is_bounded_failure(tmp_path):
    bridge = _bridge(tmp_path, "malformed")
    result = bridge.complete(FREE_MODEL, _payload()["messages"])
    assert result.state == BRIDGE_BROKEN
    assert result.error_class == "empty_output"


def test_resolve_runtime_uses_path_only():
    runtime = resolve_runtime({"PATH": os.environ.get("PATH", "")})
    if runtime.available:
        assert Path(runtime.executable).name.lower().startswith("opencode")
    # Never a hardcoded user-specific absolute path.
    assert "vac34" not in str(runtime.executable or "")


def test_bridge_context_stays_outside_repository(tmp_path):
    from config import OPENCODE_BRIDGE_DIR
    from core.opencode_bridge import BridgeContext as Context

    default = Context.default()
    assert default.is_usable()
    assert str(OPENCODE_BRIDGE_DIR) == str(default.root)
    assert default.config_file.parent.name == "opencode"


def test_bridge_config_keeps_official_request_shape(tmp_path):
    """A tool-less agent is rejected upstream, so the bridge keeps tool
    availability while auto-rejecting every tool call."""
    from core.opencode_bridge import BRIDGE_CONFIG, BridgeContext as Context

    context = Context(tmp_path / "bridge_root")
    context.ensure()
    assert BRIDGE_CONFIG["permission"] == {"*": "ask", "skill": "deny"}
    assert BRIDGE_CONFIG["compaction"] == {"auto": False}
    assert BRIDGE_CONFIG["agent"]["title"]["disable"] is True
    written = json.loads(context.config_file.read_text(encoding="utf-8"))
    assert written == BRIDGE_CONFIG
    # No "deny everything" tools block: that shape is rejected upstream.
    assert "tools" not in written


def test_bridge_env_and_argv_isolate_the_child_cwd(tmp_path):
    """The bridge context must never resolve to the user's real project."""
    record = tmp_path / "argv.log"
    bridge = _bridge(tmp_path, "ok", record=record)
    env = bridge.context.env({"PATH": "/x", "PWD": str(Path.cwd()), "OLDPWD": "/y"})
    assert "PWD" not in env and "OLDPWD" not in env
    assert env["XDG_CONFIG_HOME"] == str(bridge.context.xdg_config)
    assert env["XDG_DATA_HOME"] == str(bridge.context.xdg_data)

    bridge.complete(FREE_MODEL, _payload()["messages"])
    argv = json.loads(record.read_text(encoding="utf-8").splitlines()[0])
    assert "--dir" in argv
    assert argv[argv.index("--dir") + 1] == str(bridge.context.work_dir)
    assert argv[argv.index("--agent") + 1] == "build"
    assert "--pure" in argv


def test_tool_refused_run_retries_once_and_recovers(tmp_path):
    """A rejected tool call ends the official turn with no text; the bridge
    retries ONCE with an explicit no-tool reminder and recovers the text."""
    exe = _write_fake_cli(tmp_path, "tool_only", sequence=["tool_only", "ok"])
    context = BridgeContext(tmp_path / "bridge_root")
    assert context.ensure()
    bridge = OpenCodeBridge(
        context=context, runtime=BridgeRuntime(executable=str(exe), source="fake"),
        health=BridgeHealth(context.health_file), allowed_models=[FREE_MODEL],
    )
    result = bridge.complete(FREE_MODEL, _payload()["messages"])
    assert result.state == BRIDGE_OK
    assert result.text == "OK"
    assert result.attempts == 2
    assert bridge.health.model_status(FREE_MODEL)["state"] == BRIDGE_OK


def test_tool_refused_run_is_bounded_at_two_attempts(tmp_path):
    exe = _write_fake_cli(tmp_path, "tool_only", sequence=["tool_only", "tool_only"])
    context = BridgeContext(tmp_path / "bridge_root")
    assert context.ensure()
    bridge = OpenCodeBridge(
        context=context, runtime=BridgeRuntime(executable=str(exe), source="fake"),
        health=BridgeHealth(context.health_file), allowed_models=[FREE_MODEL],
    )
    result = bridge.complete(FREE_MODEL, _payload()["messages"])
    assert result.state == BRIDGE_BROKEN
    assert result.attempts == 2
    assert "2 attempts" in result.detail
    again = bridge.complete(FREE_MODEL, _payload()["messages"])
    assert again.error_class == "model_cooldown"


def test_allowed_models_gate_refuses_unlisted_models(tmp_path):
    bridge = _bridge(tmp_path, "ok", models=(FREE_MODEL,))
    result = bridge.complete("some-other-model", _payload()["messages"])
    assert result.state == UNSUPPORTED_BY_OPENCODE_FREE_BRIDGE
    assert result.error_class == "model_not_bridge_eligible"


def _service(bridge: OpenCodeBridge) -> OcfBridgeService:
    service = OcfBridgeService(bridge=bridge, eligible_models=[FREE_MODEL])
    service.set_eligible_models([FREE_MODEL])
    return service


def _fake_bridge_result(state, *, ok, error_class=""):
    from types import SimpleNamespace

    return SimpleNamespace(
        state=state, ok=ok, text="ok", model_id=FREE_MODEL,
        error_class=error_class, duration_ms=1, tool_attempts=0,
        attempts=1, session_id="", version="test",
    )


def test_ocf_admission_bounds_burst_without_starting_more_children():
    entered = threading.Event()
    release = threading.Event()
    calls = []

    class BlockingBridge:
        def run_request(self, payload, timeout=None, cancel_event=None):
            calls.append(cancel_event)
            entered.set()
            assert release.wait(timeout=3)
            return _fake_bridge_result(BRIDGE_OK, ok=True)

    service = OcfBridgeService(bridge=BlockingBridge(), eligible_models=[FREE_MODEL])
    first = {}
    worker = threading.Thread(
        target=lambda: first.setdefault("result", service.chat_completion(_payload())),
        daemon=True,
    )
    worker.start()
    assert entered.wait(timeout=2)
    gate = threading.Barrier(51)
    burst = []
    burst_lock = threading.Lock()

    def _contend():
        gate.wait(timeout=3)
        response = service.chat_completion(_payload())
        with burst_lock:
            burst.append(response)

    contenders = [threading.Thread(target=_contend, daemon=True) for _ in range(50)]
    for contender in contenders:
        contender.start()
    gate.wait(timeout=3)
    for contender in contenders:
        contender.join(timeout=3)
        assert not contender.is_alive()
    assert len(calls) == 1
    assert all(status == 503 and body["error"]["code"] == "BRIDGE_BUSY" for status, body in burst)
    release.set()
    worker.join(timeout=3)
    assert not worker.is_alive()
    assert first["result"][0] == 200


def test_ocf_cancellation_is_per_request_and_does_not_poison_next_request():
    calls = []

    class CancellationBridge:
        def run_request(self, payload, timeout=None, cancel_event=None):
            calls.append(cancel_event)
            if len(calls) == 1:
                assert cancel_event.wait(timeout=2)
                return _fake_bridge_result(BRIDGE_CANCELLED, ok=False, error_class="cancelled")
            assert not cancel_event.is_set()
            return _fake_bridge_result(BRIDGE_OK, ok=True)

    service = OcfBridgeService(bridge=CancellationBridge(), eligible_models=[FREE_MODEL])
    first = {}
    worker = threading.Thread(
        target=lambda: first.setdefault("result", service.chat_completion(_payload())),
        daemon=True,
    )
    worker.start()
    deadline = time.monotonic() + 2
    while not calls and time.monotonic() < deadline:
        time.sleep(0.005)
    assert calls
    service.cancel_in_flight()
    worker.join(timeout=2)
    assert not worker.is_alive()
    assert first["result"][0] == 503
    assert first["result"][1]["error"]["retryable"] is False
    assert service.chat_completion(_payload())[0] == 200
    assert calls[0] is not calls[1]
    assert not calls[1].is_set()


def test_late_broken_pipe_does_not_cancel_a_different_request():
    class CancellationSpy:
        calls = 0

        def cancel_in_flight(self):
            self.calls += 1

    class BrokenWriter:
        def write(self, raw):
            raise BrokenPipeError

    from types import SimpleNamespace

    handler = object.__new__(_Handler)
    handler.send_response = lambda status: None
    handler.send_header = lambda name, value: None
    handler.end_headers = lambda: None
    handler.wfile = BrokenWriter()
    service = CancellationSpy()
    handler.server = SimpleNamespace(service=service)
    handler._send(200, {"ok": True})
    assert service.calls == 0


# ----------------------------------------------- G/H/I: registry + ordering
class _CatalogModel:
    def __init__(self, model_id, free=True, reason="explicit_free_model_id"):
        self.model_id = model_id
        self.canonical_id = f"opencode/{model_id}"
        self.free_candidate = free
        self.free_reason = reason if free else None


def _registry(tmp_path: Path) -> OcfRegistry:
    registry = OcfRegistry(path=tmp_path / "ocf_registry.json")
    registry.health_file = None  # documentation only; health is injected
    return registry


def test_refresh_without_free_evidence_removes_scanner_entry(tmp_path):
    registry = _registry(tmp_path)
    registry.reconcile([_CatalogModel("a-free"), _CatalogModel("b-free")], SOURCE_AVAILABLE)
    assert registry.canonical_ids() == [canonical_for("a-free"), canonical_for("b-free")]

    result = registry.reconcile([_CatalogModel("a-free")], SOURCE_AVAILABLE)
    assert result.removed == [canonical_for("b-free")]
    assert registry.canonical_ids() == [canonical_for("a-free")]


def test_source_outage_preserves_last_known_good(tmp_path):
    registry = _registry(tmp_path)
    registry.reconcile([_CatalogModel("a-free")], SOURCE_AVAILABLE)
    before = registry.canonical_ids()

    result = registry.reconcile([], SOURCE_UNAVAILABLE)
    assert result.skipped_reason.startswith("catalog_source_unavailable")
    assert registry.canonical_ids() == before
    assert registry.snapshot()["last_reconcile"]["skipped_reason"]


def test_manual_entries_are_never_removed_by_reconcile(tmp_path):
    registry = _registry(tmp_path)
    registry.reconcile([_CatalogModel("a-free")], SOURCE_AVAILABLE)
    registry.add_manual("hand-picked-free")
    result = registry.reconcile([_CatalogModel("a-free")], SOURCE_AVAILABLE)
    assert "hand-picked-free" not in [m for m in result.removed]
    assert canonical_for("hand-picked-free") in registry.canonical_ids()
    assert canonical_for("hand-picked-free") in result.preserved_manual


def test_reconcile_is_idempotent(tmp_path):
    registry = _registry(tmp_path)
    models = [_CatalogModel("a-free"), _CatalogModel("b-free")]
    first = registry.reconcile(models, SOURCE_AVAILABLE)
    second = registry.reconcile(models, SOURCE_AVAILABLE)
    assert first.added and not second.changed
    assert registry.canonical_ids() == [canonical_for("a-free"), canonical_for("b-free")]


def test_provider_blocked_health_makes_no_model_eligible(tmp_path):
    registry = _registry(tmp_path)
    registry.reconcile([_CatalogModel("a-free")], SOURCE_AVAILABLE)
    health = BridgeHealth(tmp_path / "health.json")
    health.record("a-free", BRIDGE_UPSTREAM_REJECTED, error_class="client_bound_free_tier")
    assert registry.eligible_ids(health) == []


def test_inventory_rows_expose_separate_evidence_dimensions(tmp_path):
    registry = _registry(tmp_path)
    registry.reconcile([_CatalogModel("a-free")], SOURCE_AVAILABLE)
    health = BridgeHealth(tmp_path / "health.json")
    evidence = {"opencode/a-free": {"availability": "CLIENT_BOUND_FREE_TIER"}}
    rows = registry.inventory_rows([_CatalogModel("a-free")], evidence=evidence, health=health)
    row = rows[0]
    assert row["free_evidence"] is True
    assert row["direct_state"] == DIRECT_LOCAL_BRIDGE_REQUIRED
    assert row["canonical_id"] == "ocf/a-free"
    assert row["saifren_eligible"] is True

    rows_live = registry.inventory_rows(
        [_CatalogModel("a-free")], evidence={"opencode/a-free": {"availability": "LIVE"}}, health=health,
    )
    assert rows_live[0]["direct_state"] == DIRECT_ROUTABLE


def test_saifren_tail_keeps_reliable_routes_first_and_is_idempotent(tmp_path):
    base = ["cog/swe-1.6-slow", "ag/gemini-3.8-flash-high", "wb/hy3"]
    registry = _registry(tmp_path)
    registry.reconcile([_CatalogModel("a-free"), _CatalogModel("b-free")], SOURCE_AVAILABLE)
    eligible = registry.eligible_ids()

    first, report = sync_saifren_bottom(base, eligible)
    assert first[:3] == base
    assert first[3:] == ["ocf/a-free", "ocf/b-free"]
    assert report.appended == ["ocf/a-free", "ocf/b-free"]

    second, report2 = sync_saifren_bottom(first, eligible)
    assert second == first
    assert report2.unchanged and not report2.appended and not report2.removed_stale


def test_saifren_sync_needs_exact_ownership_and_prune_proof(tmp_path):
    registry = _registry(tmp_path)
    registry.reconcile([_CatalogModel("a-free"), _CatalogModel("b-free")], SOURCE_AVAILABLE)
    combo = ["cog/swe-1.6-slow", "ocf/a-free", "ocf/b-free", "ocf/manual", "oc/legacy-free"]
    new_models, report = sync_saifren_bottom(combo, ["a-free"])
    assert new_models == ["cog/swe-1.6-slow", "ocf/b-free", "ocf/manual", "oc/legacy-free", "ocf/a-free"]
    assert report.removed_stale == []

    confirmed, report = sync_saifren_bottom(
        combo, ["a-free"],
        scanner_owned_ids=["ocf/a-free", "ocf/b-free"],
        confirmed_prune_ids=["ocf/b-free"],
    )
    assert confirmed == ["cog/swe-1.6-slow", "ocf/manual", "oc/legacy-free", "ocf/a-free"]
    assert report.removed_stale == ["ocf/b-free"]


def test_direct_free_migration_plan_only_maps_verified_models(tmp_path):
    registry = _registry(tmp_path)
    registry.reconcile([_CatalogModel("a-free")], SOURCE_AVAILABLE)
    catalog = [_CatalogModel("a-free"), _CatalogModel("never-proven-free")]
    combo = ["oc/a-free", "oc/never-proven-free", "oc/not-free-at-all"]
    mapping = plan_direct_free_migration(combo, registry, catalog)
    assert mapping == {"oc/a-free": "ocf/a-free"}

    new_models, report = sync_saifren_bottom(
        combo, registry.eligible_ids(), migrate_direct=True, direct_migration_map=mapping,
    )
    assert new_models == [
        "oc/never-proven-free", "oc/not-free-at-all", "ocf/a-free",
    ]
    assert report.migrated_direct == [("oc/a-free", "ocf/a-free")]
