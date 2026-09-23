"""
9router_WatchEdit - OpenCode Local Free bridge server (OCF-001)

Loopback-ONLY OpenAI-compatible surface that 9Router reaches through a
custom provider node:

    9Router  ->  http://127.0.0.1:<port>/v1   (openai-compatible node, prefix ocf)
             ->  this server (shape gate + cancellation + bounded budget)
             ->  official `opencode run` child process (official runtime path)
             ->  OpenCode free model

What this server is NOT: it never talks to the remote Console/Zen endpoint
itself and it never imitates OpenCode client attestation. All model access
happens inside the official CLI, which is the only execution boundary.

Honest capability contract:
  * NON-STREAM ONLY. The CLI JSON event stream carries complete text parts, not
    deltas, so `stream: true` is answered with a typed
    UNSUPPORTED_BY_OPENCODE_FREE_BRIDGE error instead of fabricated chunks.
    `/v1/models` advertises `"streaming": false` explicitly.
  * Token usage is not reported by the CLI bridge: `usage` fields are
    placeholders (0) and the trusted evidence lives in the `x_ocf` block.

Error -> fallback contract (matches 9Router combo semantics): every failure is
a 4xx/5xx status so SAIFREN falls through to the next model immediately. No
failure mode can wedge a combo: bounded budget, bounded output, process-tree
kill, and bridge-level cooldowns for quota/upstream rejection.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

APP_DIR = Path(__file__).resolve().parents[1]
if str(APP_DIR) not in sys.path:
    sys.path.insert(0, str(APP_DIR))

from config import (  # noqa: E402
    DEFAULT_ROUTER_BASE_URL,
    OCF_BRIDGE_DEFAULT_PORT,
    OCF_BRIDGE_HOST,
    OCF_PROVIDER_NAME,
    OCF_PROVIDER_PREFIX,
)
from core.opencode_bridge import (  # noqa: E402
    BRIDGE_BROKEN,
    BRIDGE_BUSY,
    BRIDGE_CANCELLED,
    BRIDGE_MODEL_UNAVAILABLE,
    BRIDGE_OK,
    BRIDGE_QUOTA,
    BRIDGE_RUNTIME_MISSING,
    BRIDGE_TIMEOUT,
    BRIDGE_UPSTREAM_REJECTED,
    UNSUPPORTED_BY_OPENCODE_FREE_BRIDGE,
    BridgeHealth,
    BridgeContext,
    OpenCodeBridge,
    resolve_runtime,
)
from core.redaction import redact_text  # noqa: E402

MAX_BODY_BYTES = 2_000_000
JSON_HEADERS = {"Content-Type": "application/json"}
# Bridge state -> (HTTP status, error code). Statuses are chosen so 9Router's
# combo fallback (and account fallback rules) continue to the next model.
STATE_HTTP: Dict[str, Tuple[int, str]] = {
    UNSUPPORTED_BY_OPENCODE_FREE_BRIDGE: (400, UNSUPPORTED_BY_OPENCODE_FREE_BRIDGE),
    BRIDGE_QUOTA: (429, "BRIDGE_QUOTA"),
    BRIDGE_MODEL_UNAVAILABLE: (404, "BRIDGE_MODEL_UNAVAILABLE"),
    BRIDGE_UPSTREAM_REJECTED: (503, "CLIENT_BOUND_FREE_TIER"),
    BRIDGE_RUNTIME_MISSING: (503, "BRIDGE_RUNTIME_MISSING"),
    BRIDGE_TIMEOUT: (504, "BRIDGE_TIMEOUT"),
    BRIDGE_BROKEN: (502, "BRIDGE_BROKEN"),
    BRIDGE_CANCELLED: (503, "BRIDGE_CANCELLED"),
    BRIDGE_BUSY: (503, "BRIDGE_BUSY"),
}
STATE_MESSAGE = {
    UNSUPPORTED_BY_OPENCODE_FREE_BRIDGE: (
        "This request shape cannot be represented through the local OpenCode "
        "free bridge; fall back to the next model."
    ),
    BRIDGE_QUOTA: "OpenCode free usage limit reached; retry later.",
    BRIDGE_MODEL_UNAVAILABLE: "Requested OpenCode free model is no longer available.",
    BRIDGE_UPSTREAM_REJECTED: (
        "The official OpenCode runtime itself was rejected by the free tier "
        "(client-bound tier); the direct bridge lane is unavailable."
    ),
    BRIDGE_RUNTIME_MISSING: "Official OpenCode runtime is unavailable on this machine.",
    BRIDGE_TIMEOUT: "Local OpenCode bridge exceeded its bounded time budget.",
    BRIDGE_BROKEN: "Local OpenCode bridge failed (crash or malformed output).",
    BRIDGE_CANCELLED: "Local OpenCode bridge request was cancelled.",
    BRIDGE_BUSY: "Local OpenCode bridge is at its in-flight request limit; retry shortly.",
}


class OcfBridgeService:
    """Owns the bridge, the model eligibility gate and the in-flight registry."""

    def __init__(
        self,
        bridge: Optional[OpenCodeBridge] = None,
        eligible_models: Optional[List[str]] = None,
        name: str = OCF_PROVIDER_NAME,
        dynamic: bool = False,
    ):
        self.bridge = bridge or OpenCodeBridge()
        self.name = name
        self._eligible_models: List[str] = list(eligible_models or [])
        self._dynamic = bool(dynamic) and not eligible_models
        self._models_refreshed_at = 0.0
        self._lock = threading.Lock()
        self._request_slots = threading.BoundedSemaphore(value=1)
        self._active_requests: Dict[int, threading.Event] = {}
        self._active_lock = threading.Lock()
        self._next_request_id = 0
        self._recent: List[Dict[str, Any]] = []
        self.refresh_models_if_due()

    # -- inventory -----------------------------------------------------------
    def set_eligible_models(self, models: List[str]) -> None:
        with self._lock:
            self._eligible_models = [str(m) for m in models if str(m).strip()]
        # The bridge only ever runs models proven eligible by the registry.
        self.bridge.allowed_models = set(self._eligible_models) or None

    def eligible_models(self) -> List[str]:
        with self._lock:
            return list(self._eligible_models)

    def refresh_models_if_due(self, ttl_seconds: float = 30.0) -> None:
        """Dynamic mode: re-read the scanner-managed registry at most every TTL.

        Nothing here live-calls a model: eligibility comes from catalog evidence
        plus the bridge health cache, so refreshing the list is cheap and cannot
        burn free-tier quota.
        """
        if not self._dynamic:
            return
        now = time.time()
        if now - self._models_refreshed_at < ttl_seconds:
            return
        self._models_refreshed_at = now
        ids: List[str] = []
        try:
            from core.ocf_registry import OcfRegistry
            registry = OcfRegistry()
            ids = [entry.model_id for entry in registry.routing_eligible(self.bridge.health)]
        except Exception:
            ids = []
        self.set_eligible_models(ids)

    # -- lifecycle -----------------------------------------------------------
    def cancel_in_flight(self) -> None:
        with self._active_lock:
            events = list(self._active_requests.values())
        for event in events:
            event.set()

    def reset_cancellation(self) -> None:
        # Kept for callers of the old service API. Per-request events are born
        # clear and discarded on completion, so cancellation cannot stick.
        return None

    # -- request surface -----------------------------------------------------
    def models_payload(self) -> Dict[str, Any]:
        provider = self.bridge.health.provider_status()
        data = []
        for model_id in self.eligible_models():
            status = self.bridge.health.model_status(model_id)
            data.append({
                "id": model_id,
                "object": "model",
                "owned_by": OCF_PROVIDER_PREFIX,
                "streaming": False,
                "x_ocf": {
                    "canonical_id": f"{OCF_PROVIDER_PREFIX}/{model_id}",
                    "bridge_state": status.get("state", ""),
                    "blocked": bool(status.get("blocked")),
                    "last_ok_at": status.get("last_ok_at", ""),
                    "streaming": False,
                },
            })
        return {
            "object": "list",
            "data": data,
            "x_ocf": {
                "provider": self.name,
                "prefix": OCF_PROVIDER_PREFIX,
                "streaming": False,
                "bridge_state": provider.get("state", ""),
                "bridge_blocked": bool(provider.get("blocked")),
                "runtime_available": self.bridge.runtime.available,
                "version": self.bridge.version(),
            },
        }

    def health_payload(self) -> Dict[str, Any]:
        diagnostics = self.bridge.diagnostics()
        diagnostics.update({
            "provider": self.name,
            "prefix": OCF_PROVIDER_PREFIX,
            "eligible_models": self.eligible_models(),
            "streaming": False,
            "recent": list(self._recent[-10:]),
        })
        return diagnostics

    def _note(self, result) -> None:
        entry = {
            "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "model_id": result.model_id,
            "state": result.state,
            "error_class": result.error_class,
            "duration_ms": round(result.duration_ms, 1),
            "tool_attempts": result.tool_attempts,
            "attempts": result.attempts,
        }
        with self._lock:
            self._recent.append(entry)
            if len(self._recent) > 50:
                del self._recent[:-50]

    def chat_completion(self, payload: Dict[str, Any], timeout: Optional[float] = None):
        """Return (http_status, body_dict). Never raises, never lies.

        Request-shape rejection happens BEFORE any child process exists: an
        unsupported request consumes no OpenCode quota.
        """
        if not self._request_slots.acquire(blocking=False):
            return 503, {
                "error": {
                    "message": STATE_MESSAGE[BRIDGE_BUSY],
                    "type": "server_error",
                    "code": BRIDGE_BUSY,
                    "bridge_state": BRIDGE_BUSY,
                    "retryable": True,
                },
                "x_ocf": {"state": BRIDGE_BUSY},
            }
        with self._active_lock:
            request_id = self._next_request_id
            self._next_request_id += 1
            cancel_event = threading.Event()
            self._active_requests[request_id] = cancel_event
        try:
            result = self.bridge.run_request(
                payload, timeout=timeout, cancel_event=cancel_event,
            )
            self._note(result)
        finally:
            with self._active_lock:
                self._active_requests.pop(request_id, None)
            self._request_slots.release()
        if result.ok:
            content = result.text
            return 200, {
                "id": f"ocf-bridge-{int(time.time() * 1000)}",
                "object": "chat.completion",
                "created": int(time.time()),
                "model": str(payload.get("model") or ""),
                "choices": [{
                    "index": 0,
                    "message": {"role": "assistant", "content": content},
                    "finish_reason": "stop",
                }],
                # Placeholder: the CLI bridge does not expose trustworthy token
                # accounting. Values are 0 rather than fabricated counts.
                "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
                "x_ocf": {
                    "state": BRIDGE_OK,
                    "session_id": result.session_id,
                    "duration_ms": round(result.duration_ms, 1),
                    "tool_attempts_rejected": result.tool_attempts,
                    "attempts": result.attempts,
                    "version": result.version,
                    "streaming": False,
                },
            }
        status, code = STATE_HTTP.get(result.state, (502, "BRIDGE_BROKEN"))
        message = STATE_MESSAGE.get(result.state, STATE_MESSAGE[BRIDGE_BROKEN])
        if result.error_class:
            message = f"{message} [{redact_text(result.error_class)[:80]}]"
        return status, {
            "error": {
                "message": message,
                "type": "server_error" if status >= 500 else "invalid_request_error",
                "code": code,
                "bridge_state": result.state,
                "retryable": result.state != BRIDGE_CANCELLED,
            },
            "x_ocf": {"state": result.state, "duration_ms": round(result.duration_ms, 1)},
        }


class _Handler(BaseHTTPRequestHandler):
    server_version = "ocf-bridge/1"
    protocol_version = "HTTP/1.1"

    # -- plumbing ------------------------------------------------------------
    def log_message(self, fmt, *args):  # noqa: D102 - silence default access log
        return

    def _send(self, status: int, body: Dict[str, Any]) -> None:
        raw = json.dumps(body, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        for key, value in JSON_HEADERS.items():
            self.send_header(key, value)
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        try:
            self.wfile.write(raw)
        except (BrokenPipeError, ConnectionResetError):
            # The HTTP response is sent only after its child request finishes.
            # A late broken pipe must never cancel a different request.
            pass

    def _read_body(self) -> Optional[Dict[str, Any]]:
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            return None
        if length <= 0 or length > MAX_BODY_BYTES:
            return None
        try:
            raw = self.rfile.read(length)
            payload = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError, OSError):
            return None
        return payload if isinstance(payload, dict) else None

    # -- routes --------------------------------------------------------------
    def do_GET(self):  # noqa: N802 - BaseHTTPRequestHandler API
        path = self.path.split("?", 1)[0].rstrip("/") or "/"
        service: OcfBridgeService = self.server.service
        service.refresh_models_if_due()
        if path in ("/health", "/v1/health", "/healthz"):
            self._send(200, service.health_payload())
        elif path in ("/v1/models", "/models"):
            self._send(200, service.models_payload())
        elif path in ("/", "/v1"):
            self._send(200, {
                "provider": service.name,
                "prefix": OCF_PROVIDER_PREFIX,
                "streaming": False,
                "endpoints": ["/health", "/v1/models", "/v1/chat/completions"],
            })
        else:
            self._send(404, {"error": {"message": "unknown route", "code": "not_found"}})

    def do_POST(self):  # noqa: N802 - BaseHTTPRequestHandler API
        path = self.path.split("?", 1)[0].rstrip("/")
        service: OcfBridgeService = self.server.service
        if path not in ("/v1/chat/completions", "/chat/completions"):
            self._send(404, {"error": {"message": "unknown route", "code": "not_found"}})
            return
        payload = self._read_body()
        if payload is None:
            self._send(400, {"error": {
                "message": "malformed or oversized JSON body",
                "type": "invalid_request_error",
                "code": "bad_request",
            }})
            return
        service.refresh_models_if_due()
        status, body = service.chat_completion(payload)
        self._send(status, body)


class OcfBridgeServer(ThreadingHTTPServer):
    daemon_threads = True
    # Windows SO_REUSEADDR lets a SECOND process bind the same port, silently
    # splitting requests between two bridges. One loopback bridge per port: a
    # duplicate start must fail loudly instead.
    allow_reuse_address = os.name != "nt"

    def __init__(self, address, service: OcfBridgeService):
        super().__init__(address, _Handler)
        self.service = service


def build_service(eligible_models: Optional[List[str]] = None,
                  bridge: Optional[OpenCodeBridge] = None) -> OcfBridgeService:
    context = BridgeContext.default()
    context.ensure()
    resolved = bridge if bridge is not None else OpenCodeBridge(
        context=context, runtime=resolve_runtime(), health=BridgeHealth(context.health_file),
    )
    service = OcfBridgeService(bridge=resolved, eligible_models=eligible_models)
    if eligible_models:
        service.set_eligible_models(eligible_models)
    return service


def serve(port: int = OCF_BRIDGE_DEFAULT_PORT,
          eligible_models: Optional[List[str]] = None,
          bridge: Optional[OpenCodeBridge] = None,
          dynamic: bool = True) -> OcfBridgeServer:
    service = build_service(eligible_models=eligible_models, bridge=bridge)
    service._dynamic = bool(dynamic) and not eligible_models
    # Loopback only: an ocf bridge must never be reachable off-machine.
    server = OcfBridgeServer((OCF_BRIDGE_HOST, int(port)), service)
    return server


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="OpenCode Local Free bridge (loopback OpenAI-compatible surface, non-stream only)"
    )
    parser.add_argument("--port", type=int, default=OCF_BRIDGE_DEFAULT_PORT,
                        help=f"loopback port (default {OCF_BRIDGE_DEFAULT_PORT})")
    parser.add_argument("--models", type=str, default="",
                        help="comma separated eligible free model ids (default: registry snapshot)")
    parser.add_argument("--serve", action="store_true", help="run the HTTP server")
    parser.add_argument("--canary", type=str, default="",
                        help="run one official CLI canary for this free model id and exit")
    parser.add_argument("--check", action="store_true",
                        help="print runtime/bridge diagnostics and exit")
    parser.add_argument("--router-url", type=str, default=DEFAULT_ROUTER_BASE_URL,
                        help="local 9Router URL used to resolve eligible models")
    args = parser.parse_args(argv)

    eligible = [m.strip() for m in args.models.split(",") if m.strip()]
    if not eligible:
        eligible = _registry_models(args.router_url)

    context = BridgeContext.default()
    context.ensure()
    bridge = OpenCodeBridge(context=context, runtime=resolve_runtime(),
                            health=BridgeHealth(context.health_file))

    if args.check:
        print(json.dumps({"diagnostics": bridge.diagnostics(),
                          "eligible_models": eligible}, indent=2))
        return 0

    if args.canary:
        result = bridge.canary(args.canary)
        print(json.dumps(result.as_dict(), indent=2))
        return 0 if result.ok else 1

    if not args.serve:
        parser.error("choose --serve, --canary or --check")

    service = OcfBridgeService(bridge=bridge, eligible_models=eligible,
                                   dynamic=not bool(eligible))
    server = OcfBridgeServer((OCF_BRIDGE_HOST, args.port), service)
    print(f"{OCF_PROVIDER_NAME} bridge listening on http://{OCF_BRIDGE_HOST}:{args.port}/v1")
    print(f"eligible models: {eligible or 'none'}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        service.cancel_in_flight()
        server.server_close()
    return 0


def _registry_models(router_url: str) -> List[str]:
    """Best-effort: only scanner-managed, bridge-healthy free models."""
    try:
        from core.ocf_registry import OcfRegistry
        registry = OcfRegistry()
        return [entry.model_id for entry in registry.routing_eligible()]
    except Exception:
        return []


if __name__ == "__main__":
    sys.exit(main())
