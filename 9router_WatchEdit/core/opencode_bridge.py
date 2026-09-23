"""
9router_WatchEdit - OpenCode Local Free bridge (OCF-001)

Execution boundary for `ocf/<model-id>`: the official local `opencode` CLI,
never a handcrafted request to the remote Console/Zen endpoint.

Contract (three separated facts):
  1. PUBLIC CATALOG EVIDENCE   - OpenCode advertises a model as free.
  2. LOCAL BRIDGE CAPABILITY   - THIS machine can invoke that model through the
                                 official OpenCode CLI (proven by a canary).
  3. ROUTING ELIGIBILITY       - the request shape can be represented through
                                 the bridge without losing semantics.
Only (1) + (2) + (3) together make a model routable as `ocf/<id>`.

Non-negotiable boundary:
  * No header/User-Agent/session/cookie/device attestation is forged anywhere.
  * No 9Router request is sent directly to a client-bound free-tier endpoint.
  * The user's GLOBAL OpenCode configuration is never modified: the bridge runs
    the CLI inside a private runtime root (its own XDG_* root) plus a dedicated
    bridge-local `opencode.json`.

Canary states:
  BRIDGE_OK                 official CLI returned valid model text
  BRIDGE_QUOTA              free usage limit exhausted / retry later
  BRIDGE_MODEL_UNAVAILABLE  requested free model no longer available
  BRIDGE_UPSTREAM_REJECTED  the official CLI itself got client-bound FreeTierError
  BRIDGE_RUNTIME_MISSING    opencode executable unavailable
  BRIDGE_BROKEN             malformed output / crash
  BRIDGE_TIMEOUT            bounded timeout exceeded
  UNSUPPORTED_BY_OPENCODE_FREE_BRIDGE   request shape not representable

Streaming: the CLI JSON event stream carries COMPLETE text parts, not deltas
(verified on opencode 1.18.31). The bridge therefore advertises a NON-STREAM
only lane and never fabricates incremental chunks.

Evidence for the bridge-local CLI shape (opencode 1.18.31, live canary):
  * tools present in the request are REQUIRED by the upstream free-tier gate;
    a tool-less agent request is rejected with the same FreeTierError, so the
    bridge cannot use a "deny everything" agent.
  * `permission: {"*": "ask", "skill": "deny"}` keeps the official agentic
    request shape while making every tool call auto-rejected inside the CLI
    ("auto-rejecting"), so no shell, filesystem or subagent action can execute;
    `skill` is denied outright because it is the reflex tool of weak free
    models and a rejected tool call ends the turn with no text.
  * stdin MUST be DEVNULL, otherwise the CLI blocks on an inherited pipe.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from config import (
    OCF_UPSTREAM_PREFIX,
    OPENCODE_BRIDGE_DIR,
    PRIVATE_STORAGE_AVAILABLE,
    _UNAVAILABLE_ROOT,
    _is_inside,
    REPO_ROOT,
)
from core.redaction import redact_text

# ----------------------------------------------------------------- canary states
BRIDGE_OK = "BRIDGE_OK"
BRIDGE_QUOTA = "BRIDGE_QUOTA"
BRIDGE_MODEL_UNAVAILABLE = "BRIDGE_MODEL_UNAVAILABLE"
BRIDGE_UPSTREAM_REJECTED = "BRIDGE_UPSTREAM_REJECTED"
BRIDGE_RUNTIME_MISSING = "BRIDGE_RUNTIME_MISSING"
BRIDGE_BROKEN = "BRIDGE_BROKEN"
BRIDGE_TIMEOUT = "BRIDGE_TIMEOUT"
BRIDGE_CANCELLED = "BRIDGE_CANCELLED"
BRIDGE_BUSY = "BRIDGE_BUSY"
UNSUPPORTED_BY_OPENCODE_FREE_BRIDGE = "UNSUPPORTED_BY_OPENCODE_FREE_BRIDGE"

# States that mean the whole provider lane is unhealthy (bridge-level cooldown).
PROVIDER_LEVEL_STATES = frozenset({BRIDGE_UPSTREAM_REJECTED, BRIDGE_RUNTIME_MISSING})

CANARY_PROMPT = "Reply with exactly: OK"
CANARY_MODEL = "mimo-v2.5-free"

DEFAULT_TIMEOUT_SEC = 120.0
VERSION_TIMEOUT_SEC = 20.0
MAX_STDOUT_BYTES = 262_144
MAX_STDERR_BYTES = 65_536
KILL_GRACE_SEC = 5.0

# Cooldowns (seconds) applied to the bridge health cache.
COOLDOWN_QUOTA_SEC = 15 * 60
COOLDOWN_UPSTREAM_SEC = 30 * 60
COOLDOWN_UNAVAILABLE_MODEL_SEC = 30 * 60
COOLDOWN_BROKEN_SEC = 2 * 60

# Bridge-local OpenCode configuration. Never written anywhere else.
BRIDGE_CONFIG = {
    "$schema": "https://opencode.ai/config.json",
    "autoupdate": False,
    "compaction": {"auto": False},
    # Keeps the official agentic request shape (the upstream free-tier gate
    # REJECTS a tool-less request) while auto-rejecting every tool call: nothing
    # can execute. `skill` is denied outright: it is the tool weak free models
    # reflexively call (a rejected tool call ends the official turn with no
    # text), and denying it does not remove the agentic request shape.
    "permission": {"*": "ask", "skill": "deny"},
    "agent": {
        "title": {"disable": True},
        "summary": {"disable": True},
    },
}

# Client-bound evidence produced by the free tier when an arbitrary client (or a
# tool-less request) hits the Zen endpoint.
CLIENT_BOUND_MARKERS = (
    "can only be used from within opencode",
    "freetiererror",
)
QUOTA_MARKERS = (
    "freetier quota",
    "free tier quota",
    "usage limit",
    "quota exceeded",
    "rate limit",
    "too many requests",
)
MODEL_UNAVAILABLE_MARKERS = (
    "providermodelnotfounderror",
    "model not found",
    "no longer available",
)


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def is_free_model_id(model_id: str) -> bool:
    """Catalog naming heuristic ONLY (evidence, never routing permission)."""
    return str(model_id or "").strip().endswith("-free")


def bare_model_id(model_id: str) -> str:
    """Bare upstream model id: `ocf/foo`, `opencode/foo` and `foo` all -> `foo`."""
    raw = str(model_id or "").strip()
    if "/" in raw:
        prefix, rest = raw.split("/", 1)
        if prefix in ("ocf", OCF_UPSTREAM_PREFIX):
            return rest
    return raw


def cli_model_id(model_id: str) -> str:
    """`ocf/foo`, `opencode/foo` and `foo` all map to CLI model `opencode/foo`.

    The mapping is a pure prefix rewrite: no model id is invented, renamed or
    resolved through a remote catalog.
    """
    raw = str(model_id or "").strip()
    if not raw:
        return ""
    if "/" in raw:
        prefix, rest = raw.split("/", 1)
        if prefix in ("ocf", OCF_UPSTREAM_PREFIX):
            return f"{OCF_UPSTREAM_PREFIX}/{rest}" if rest else ""
        # Foreign namespace: preserve verbatim (caller rejects non-free models).
        return raw
    return f"{OCF_UPSTREAM_PREFIX}/{raw}"


# ------------------------------------------------------------------ shape gate
_TEXT_PART_TYPES = ("text", "input_text")


def classify_request_shape(payload: Any) -> Tuple[bool, str]:
    """Return (eligible, reason) for the bridge's representable request shapes.

    Eligible: textual system/user/assistant history, no tool definitions, no
    forced tool_choice, no image/file/audio parts, one choice, streaming off.
    Everything else is a typed fallback condition; the caller MUST NOT invoke
    OpenCode for it (no quota burn, no silent semantics loss).
    """
    if not isinstance(payload, dict):
        return False, "payload_not_object"

    if payload.get("stream"):
        # Verified: the CLI event stream has no trustworthy deltas.
        return False, "stream_not_supported"

    messages = payload.get("messages")
    if not isinstance(messages, list) or not messages:
        return False, "messages_missing"
    for index, message in enumerate(messages):
        if not isinstance(message, dict):
            return False, f"message_{index}_not_object"
        role = message.get("role")
        if role not in ("system", "user", "assistant"):
            return False, f"role_unsupported:{role}"
        content = message.get("content")
        if isinstance(content, str):
            continue
        if isinstance(content, list):
            for part in content:
                if isinstance(part, str):
                    continue
                if not isinstance(part, dict):
                    return False, f"message_{index}_part_not_object"
                part_type = str(part.get("type") or "")
                if part_type not in _TEXT_PART_TYPES:
                    return False, f"non_text_part:{part_type or 'unknown'}"
            continue
        if content is None:
            return False, f"message_{index}_content_missing"
        return False, f"message_{index}_content_unsupported"

    tools = payload.get("tools")
    if isinstance(tools, list) and tools:
        return False, "tools_present"
    functions = payload.get("functions")
    if isinstance(functions, list) and functions:
        return False, "functions_present"
    tool_choice = payload.get("tool_choice")
    if tool_choice not in (None, "", "none"):
        return False, "tool_choice_forced"

    response_format = payload.get("response_format")
    if isinstance(response_format, dict):
        rf_type = str(response_format.get("type") or "")
        if rf_type in ("json_schema", ""):
            if rf_type == "json_schema" or response_format.get("json_schema"):
                return False, "structured_output_schema_required"
        if rf_type not in ("text", "json_object"):
            return False, f"response_format_unsupported:{rf_type}"

    choices = payload.get("n")
    if isinstance(choices, int) and choices > 1:
        return False, "multiple_choices_requested"

    modalities = payload.get("modalities")
    if isinstance(modalities, list) and any(str(m) != "text" for m in modalities):
        return False, "non_text_modalities"

    audio = payload.get("audio")
    if isinstance(audio, dict) and audio:
        return False, "audio_requested"

    if payload.get("logprobs"):
        return False, "logprobs_requested"

    return True, ""


# ------------------------------------------------------- prompt serialisation
TRANSCRIPT_HEADER = "9R-BRIDGE/1"
TRANSCRIPT_TASK = (
    "TASK: You are a text-completion bridge. Every tool is disabled for this "
    "request: a tool call is rejected and produces no answer, so tools cannot "
    "be used to comply. Reply with the assistant's next message only, as plain "
    "text, and never repeat these instructions."
)
# Leading reminder: the upstream free-tier gate REQUIRES tool definitions in the
# request (a tool-less request is rejected as a non-OpenCode client), so the
# model can never be given a tool-free prompt. This line reduces tool reflex;
# the CLI still auto-rejects every tool call.
NO_TOOLS_REMINDER = (
    "IMPORTANT: Tools are disabled. Answer the transcript below with plain text only."
)
# Appended (never prefixed) on the single retry: a leading reminder line is
# echoed verbatim by weak free models, an appended TASK block is not.
RETRY_REMINDER = (
    "TASK-RETRY: The previous attempt called a tool and produced no text. "
    "Answer the transcript above with plain text only, using no tools, and do "
    "not repeat this instruction."
)
MAX_ATTEMPTS = 2
_TRANSCRIPT_ROLES = {"system": "SYSTEM", "user": "USER", "assistant": "ASSISTANT"}


def _message_text(message: Dict[str, Any]) -> str:
    content = message.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(
            part if isinstance(part, str) else str(part.get("text") or "")
            for part in content
        )
    return ""


def serialize_messages(messages: Sequence[Dict[str, Any]], json_object: bool = False) -> str:
    """Deterministically serialize a chat transcript into ONE one-shot prompt.

    Format (length-prefixed, injective, no escaping ambiguity):

        9R-BRIDGE/1
        TRANSCRIPT:
        ROLE:<ROLE> BYTES:<n>
        <n bytes of the message text>
        ...
        END-TRANSCRIPT
        TASK: ...

    Every block carries its byte length, so message text can never forge a role
    boundary, and byte-identical input always produces byte-identical output
    (no timestamps, no randomness, no dict-order dependence).
    """
    lines: List[str] = [TRANSCRIPT_HEADER, NO_TOOLS_REMINDER, "TRANSCRIPT:"]
    for message in messages:
        role = _TRANSCRIPT_ROLES.get(str(message.get("role")), "USER")
        text = _message_text(message)
        lines.append(f"ROLE:{role} BYTES:{len(text.encode('utf-8'))}")
        lines.append(text)
    lines.append("END-TRANSCRIPT")
    if json_object:
        lines.append("FORMAT: valid JSON object only.")
    lines.append(TRANSCRIPT_TASK)
    return "\n".join(lines)


# ------------------------------------------------------------------- context
@dataclass(frozen=True)
class BridgeContext:
    """Private bridge runtime root: config/data/cache/state/work, all outside
    the repository and outside the user's normal projects."""

    root: Path

    @classmethod
    def default(cls) -> "BridgeContext":
        if PRIVATE_STORAGE_AVAILABLE:
            return cls(OPENCODE_BRIDGE_DIR)
        # Fail-closed: no private storage means the bridge cannot exist. The
        # sentinel root is never writable, so every method degrades cleanly.
        return cls(_UNAVAILABLE_ROOT / "opencode_bridge")

    @property
    def xdg_config(self) -> Path:
        return self.root / "xdg" / "config"

    @property
    def xdg_data(self) -> Path:
        return self.root / "xdg" / "data"

    @property
    def xdg_cache(self) -> Path:
        return self.root / "xdg" / "cache"

    @property
    def xdg_state(self) -> Path:
        return self.root / "xdg" / "state"

    @property
    def work_dir(self) -> Path:
        return self.root / "work"

    @property
    def config_file(self) -> Path:
        return self.xdg_config / "opencode" / "opencode.json"

    @property
    def health_file(self) -> Path:
        return self.root / "bridge_health.json"

    def is_usable(self) -> bool:
        return PRIVATE_STORAGE_AVAILABLE and not _is_inside(self.root, REPO_ROOT)

    def ensure(self) -> bool:
        """Create the private layout and the bridge-local config. Idempotent.

        Writes ONLY inside this private root; the user's global OpenCode
        configuration is untouched.
        """
        if not self.is_usable():
            return False
        try:
            for path in (self.xdg_config / "opencode", self.xdg_data, self.xdg_cache,
                         self.xdg_state, self.work_dir):
                path.mkdir(parents=True, exist_ok=True)
            wanted = json.dumps(BRIDGE_CONFIG, indent=2, sort_keys=True)
            current = ""
            if self.config_file.exists():
                current = self.config_file.read_text(encoding="utf-8")
                try:
                    if json.loads(current) == BRIDGE_CONFIG:
                        return True
                except ValueError:
                    pass
            self.config_file.write_text(wanted, encoding="utf-8")
            return True
        except OSError:
            return False

    def env(self, base_env: Optional[Dict[str, str]] = None) -> Dict[str, str]:
        """Environment for the child CLI: bridge-local XDG root only."""
        env = dict(base_env if base_env is not None else os.environ)
        # A shell-exported PWD would leak the parent's working directory into the
        # child and make the official CLI treat THAT as the project root. The
        # bridge context must never resolve to the user's real project.
        for leaked in ("PWD", "OLDPWD"):
            env.pop(leaked, None)
        env["XDG_CONFIG_HOME"] = str(self.xdg_config)
        env["XDG_DATA_HOME"] = str(self.xdg_data)
        env["XDG_CACHE_HOME"] = str(self.xdg_cache)
        env["XDG_STATE_HOME"] = str(self.xdg_state)
        # Deterministic, non-interactive child. NoTelemetry is honoured by
        # OpenCode; it never changes request semantics.
        env["OPENCODE_DISABLE_AUTOUPDATE"] = "1"
        env["CI"] = "1"
        return env


@dataclass
class BridgeRuntime:
    """Resolved official OpenCode executable (never a hardcoded user path)."""

    executable: Optional[str] = None
    version: str = ""
    source: str = ""

    @property
    def available(self) -> bool:
        return bool(self.executable)


def resolve_runtime(env: Optional[Dict[str, str]] = None) -> BridgeRuntime:
    """Resolve `opencode` from PATH (which respects PATHEXT on Windows)."""
    exe = shutil.which("opencode", path=(env or os.environ).get("PATH"))
    if not exe:
        return BridgeRuntime()
    return BridgeRuntime(executable=exe, source="PATH")


def _probe_version(runtime: BridgeRuntime, timeout: float = VERSION_TIMEOUT_SEC) -> str:
    if not runtime.available:
        return ""
    try:
        proc = subprocess.run(
            [runtime.executable, "--version"],
            capture_output=True,
            text=True,
            timeout=timeout,
            shell=False,
            stdin=subprocess.DEVNULL,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except Exception:
        return ""
    if proc.returncode != 0:
        return ""
    return (proc.stdout or "").strip().splitlines()[0][:64] if proc.stdout else ""


# --------------------------------------------------------------------- result
@dataclass
class BridgeResult:
    state: str
    text: str = ""
    model_id: str = ""
    cli_model: str = ""
    error_class: str = ""
    detail: str = ""
    session_id: str = ""
    duration_ms: float = 0.0
    tool_attempts: int = 0
    attempts: int = 1
    exit_code: Optional[int] = None
    version: str = ""

    @property
    def ok(self) -> bool:
        return self.state == BRIDGE_OK

    def as_dict(self) -> Dict[str, Any]:
        return {
            "state": self.state,
            "model_id": self.model_id,
            "cli_model": self.cli_model,
            "error_class": self.error_class,
            "detail": redact_text(self.detail)[:400],
            "session_id": self.session_id,
            "duration_ms": round(self.duration_ms, 1),
            "tool_attempts": self.tool_attempts,
            "attempts": self.attempts,
            "exit_code": self.exit_code,
            "version": self.version,
            "ok": self.ok,
        }


# --------------------------------------------------------------------- health
class BridgeHealth:
    """Bridge-local health/cooldown cache. Never a routing authority on its own:
    it only records observed bridge evidence with TTLs."""

    def __init__(self, path: Optional[Path] = None):
        self.path = path if path is not None else BridgeContext.default().health_file
        self._lock = threading.Lock()
        self.provider: Dict[str, Any] = {}
        self.models: Dict[str, Dict[str, Any]] = {}
        self._load()

    # -- io
    def _load(self) -> None:
        try:
            if not self.path.exists():
                return
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        if not isinstance(data, dict):
            return
        provider = data.get("provider")
        models = data.get("models")
        if isinstance(provider, dict):
            self.provider = provider
        if isinstance(models, dict):
            self.models = {
                str(k): v for k, v in models.items() if isinstance(v, dict)
            }

    def _save(self) -> bool:
        doc = {"version": 1, "provider": self.provider, "models": self.models}
        tmp = self.path.with_suffix(".tmp")
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp.write_text(json.dumps(doc, indent=2, sort_keys=True), encoding="utf-8")
            tmp.replace(self.path)
            return True
        except OSError:
            try:
                if tmp.exists():
                    tmp.unlink()
            except OSError:
                pass
            return False

    # -- reads
    @staticmethod
    def _cooldown_active(entry: Dict[str, Any]) -> bool:
        until = entry.get("cooldown_until") or 0
        try:
            return float(until) > time.time()
        except (TypeError, ValueError):
            return False

    def model_blocked(self, model_id: str) -> bool:
        with self._lock:
            entry = self.models.get(str(model_id))
            if not isinstance(entry, dict):
                return False
            return self._cooldown_active(entry)

    def provider_blocked(self) -> bool:
        with self._lock:
            return self._cooldown_active(self.provider)

    def model_status(self, model_id: str) -> Dict[str, Any]:
        with self._lock:
            entry = dict(self.models.get(str(model_id)) or {})
        entry["blocked"] = self._cooldown_active(entry)
        return entry

    def provider_status(self) -> Dict[str, Any]:
        with self._lock:
            entry = dict(self.provider)
        entry["blocked"] = self._cooldown_active(entry)
        return entry

    # -- writes
    def record(self, model_id: str, state: str, *, duration_ms: float = 0.0,
               error_class: str = "") -> None:
        # A caller cancelling its own request says nothing about model health.
        if state == BRIDGE_CANCELLED:
            return
        now = time.time()
        with self._lock:
            entry = dict(self.models.get(str(model_id)) or {})
            entry.update({
                "state": state,
                "checked_at": _utc_now_iso(),
                "checked_at_epoch": now,
                "duration_ms": round(duration_ms, 1),
                "error_class": error_class,
            })
            cooldown = {
                BRIDGE_QUOTA: COOLDOWN_QUOTA_SEC,
                BRIDGE_MODEL_UNAVAILABLE: COOLDOWN_UNAVAILABLE_MODEL_SEC,
                BRIDGE_TIMEOUT: COOLDOWN_BROKEN_SEC,
                BRIDGE_BROKEN: COOLDOWN_BROKEN_SEC,
                BRIDGE_RUNTIME_MISSING: COOLDOWN_BROKEN_SEC,
            }.get(state, 0)
            if cooldown:
                entry["cooldown_until"] = now + cooldown
                entry["cooldown_seconds"] = cooldown
            else:
                entry.pop("cooldown_until", None)
                entry.pop("cooldown_seconds", None)
            if state == BRIDGE_OK:
                entry["last_ok_at"] = _utc_now_iso()
                entry["last_ok_at_epoch"] = now
            self.models[str(model_id)] = entry

            if state in PROVIDER_LEVEL_STATES:
                self.provider.update({
                    "state": state,
                    "checked_at": _utc_now_iso(),
                    "cooldown_until": now + COOLDOWN_UPSTREAM_SEC,
                    "cooldown_seconds": COOLDOWN_UPSTREAM_SEC,
                    "error_class": error_class,
                })
            elif state == BRIDGE_OK:
                self.provider.update({
                    "state": BRIDGE_OK,
                    "checked_at": _utc_now_iso(),
                    "last_ok_at": _utc_now_iso(),
                    "cooldown_until": 0,
                    "error_class": "",
                })
            elif state not in (UNSUPPORTED_BY_OPENCODE_FREE_BRIDGE,):
                self.provider.update({
                    "state": state,
                    "checked_at": _utc_now_iso(),
                    "error_class": error_class,
                })
        self._save()

    def clear(self) -> None:
        with self._lock:
            self.provider = {}
            self.models = {}
        self._save()


# -------------------------------------------------------------------- bridge
def _bounded_read(stream, limit: int, chunk_size: int = 65536) -> bytes:
    """Drain a pipe up to `limit` bytes, then keep draining without storing.

    Continuous draining matters: a truncated read that stopped early would let
    the child block forever on a full pipe. Uses read1() when available so a
    chunk is returned as soon as it exists instead of waiting for a full read.
    """
    reader = getattr(stream, "read1", None) or stream.read
    kept: List[bytes] = []
    total = 0
    truncated = False
    while True:
        try:
            chunk = reader(chunk_size)
        except Exception:
            break
        if not chunk:
            break
        if total < limit:
            keep = chunk[: max(0, limit - total)]
            if keep:
                kept.append(keep)
                total += len(keep)
            if len(keep) < len(chunk):
                truncated = True
        else:
            truncated = True
    data = b"".join(kept)
    return data + (b"\n[TRUNCATED]" if truncated else b"")


def _kill_process_tree(proc: "subprocess.Popen") -> None:
    """Terminate the child AND its process tree (Windows safe, argv array)."""
    if proc.poll() is not None:
        return
    if os.name == "nt":
        try:
            subprocess.run(
                ["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                capture_output=True, timeout=KILL_GRACE_SEC, shell=False,
                stdin=subprocess.DEVNULL,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except Exception:
            pass
    try:
        proc.kill()
    except Exception:
        pass
    try:
        proc.wait(timeout=KILL_GRACE_SEC)
    except Exception:
        pass


def _classify_error_blob(blob: str) -> Tuple[str, str]:
    """Map CLI error evidence onto a bridge state without guessing."""
    lower = (blob or "").lower()
    for marker in CLIENT_BOUND_MARKERS:
        if marker in lower:
            return BRIDGE_UPSTREAM_REJECTED, "client_bound_free_tier"
    for marker in QUOTA_MARKERS:
        if marker in lower:
            return BRIDGE_QUOTA, "free_usage_limit"
    for marker in MODEL_UNAVAILABLE_MARKERS:
        if marker in lower:
            return BRIDGE_MODEL_UNAVAILABLE, "model_unavailable"
    return BRIDGE_BROKEN, "unknown_cli_error"


class OpenCodeBridge:
    """Runs ONE fresh official OpenCode one-shot per bridge request."""

    def __init__(
        self,
        context: Optional[BridgeContext] = None,
        runtime: Optional[BridgeRuntime] = None,
        health: Optional[BridgeHealth] = None,
        timeout: float = DEFAULT_TIMEOUT_SEC,
        allowed_models: Optional[Sequence[str]] = None,
        agent: str = "build",
    ):
        self.context = context or BridgeContext.default()
        self.runtime = runtime if runtime is not None else resolve_runtime()
        self.health = health if health is not None else BridgeHealth(self.context.health_file)
        self.timeout = float(timeout)
        self.agent = agent
        self.max_attempts = MAX_ATTEMPTS
        self.allowed_models = set(allowed_models) if allowed_models else None
        self._version_probed = False

    # -- diagnostics ---------------------------------------------------------
    def version(self) -> str:
        if not self._version_probed:
            self.runtime.version = self.runtime.version or _probe_version(self.runtime)
            self._version_probed = True
        return self.runtime.version

    def diagnostics(self) -> Dict[str, Any]:
        """Operator-facing runtime facts. Never contains credentials."""
        return {
            "runtime_available": self.runtime.available,
            "runtime_source": self.runtime.source,
            "version": self.version(),
            "context_root": str(self.context.root) if self.context.is_usable() else "",
            "provider_state": self.health.provider_status().get("state", ""),
            "provider_blocked": self.health.provider_blocked(),
        }

    # -- gate ----------------------------------------------------------------
    def model_allowed(self, model_id: str) -> Tuple[bool, str]:
        bare = bare_model_id(model_id)
        if self.allowed_models is not None and bare not in self.allowed_models:
            return False, "model_not_bridge_eligible"
        if not cli_model_id(model_id):
            return False, "model_id_missing"
        return True, ""

    # -- execution -----------------------------------------------------------
    def _run(self, model_id: str, prompt: str, timeout: Optional[float],
             cancel_event: Optional[threading.Event]) -> BridgeResult:
        """Bounded attempts: a tool-refused run that produced no text is retried
        ONCE with an explicit no-tool reminder. Never more than MAX_ATTEMPTS
        children, and never a retry after a quota/upstream/cooldown state."""
        result = self._run_once(model_id, prompt, timeout, cancel_event)
        # NOTE: the model cooldown recorded by the first attempt must not gate
        # this retry -- it was created by the very failure being retried.
        if (self.max_attempts > 1
                and result.state == BRIDGE_BROKEN
                and result.error_class == "empty_output"
                and result.tool_attempts > 0
                and not self.health.provider_blocked()):
            retry = self._run_once(model_id, f"{prompt}\n{RETRY_REMINDER}", timeout, cancel_event)
            retry.attempts = 2
            if not retry.ok:
                retry.detail = (
                    f"tool-refused run returned no text after {self.max_attempts} attempts"
                )
            return retry
        return result

    def _run_once(self, model_id: str, prompt: str, timeout: Optional[float],
                  cancel_event: Optional[threading.Event]) -> BridgeResult:
        started = time.time()
        bare = bare_model_id(model_id)
        cli_model = cli_model_id(model_id)
        budget = float(timeout if timeout is not None else self.timeout)
        result = BridgeResult(state=BRIDGE_BROKEN, model_id=bare, cli_model=cli_model)

        if not self.runtime.available:
            result.state = BRIDGE_RUNTIME_MISSING
            result.error_class = "runtime_missing"
            result.detail = "official opencode executable not found on PATH"
            self.health.record(bare, result.state, error_class=result.error_class)
            return result

        if not self.context.ensure():
            result.state = BRIDGE_RUNTIME_MISSING
            result.error_class = "bridge_context_unavailable"
            result.detail = "private bridge runtime root is unavailable"
            self.health.record(bare, result.state, error_class=result.error_class)
            return result

        argv = [
            self.runtime.executable,
            "run",
            "--pure",
            "--format", "json",
            "--dir", str(self.context.work_dir),
            "--agent", self.agent,
            "-m", cli_model,
            prompt,
        ]
        env = self.context.env()
        proc = None
        try:
            proc = subprocess.Popen(
                argv,
                stdin=subprocess.DEVNULL,      # CLI blocks on an inherited pipe
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                cwd=str(self.context.work_dir),
                env=env,
                shell=False,                   # argv array: no shell interpolation
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except OSError as ex:
            result.state = BRIDGE_RUNTIME_MISSING
            result.error_class = type(ex).__name__
            result.detail = "could not start the official opencode process"
            self.health.record(bare, result.state, error_class=result.error_class)
            return result

        out_box: Dict[str, bytes] = {}
        err_box: Dict[str, bytes] = {}

        def _reader(stream, box: Dict[str, bytes], limit: int) -> None:
            box["data"] = _bounded_read(stream, limit)

        threads = [
            threading.Thread(target=_reader, args=(proc.stdout, out_box, MAX_STDOUT_BYTES), daemon=True),
            threading.Thread(target=_reader, args=(proc.stderr, err_box, MAX_STDERR_BYTES), daemon=True),
        ]
        for thread in threads:
            thread.start()

        cancelled = False
        deadline = time.time() + budget
        while True:
            if proc.poll() is not None:
                break
            if cancel_event is not None and cancel_event.is_set():
                cancelled = True
                _kill_process_tree(proc)
                break
            if time.time() >= deadline:
                _kill_process_tree(proc)
                result.state = BRIDGE_TIMEOUT
                result.error_class = "timeout"
                result.detail = f"bridge budget of {int(budget)}s exceeded"
                break
            time.sleep(0.05)

        for thread in threads:
            thread.join(timeout=KILL_GRACE_SEC)

        stdout = (out_box.get("data") or b"").decode("utf-8", errors="replace")
        stderr = (err_box.get("data") or b"").decode("utf-8", errors="replace")
        result.duration_ms = (time.time() - started) * 1000.0
        result.exit_code = proc.returncode

        if cancelled:
            result.state = BRIDGE_CANCELLED
            result.error_class = "cancelled"
            result.detail = "bridge request cancelled; child process tree terminated"
            return result

        if result.state == BRIDGE_TIMEOUT:
            self.health.record(bare, result.state, duration_ms=result.duration_ms,
                               error_class=result.error_class)
            return result

        text, session_id, tool_attempts, error_blob = _parse_events(stdout)
        result.text = text
        result.session_id = session_id
        result.tool_attempts = tool_attempts

        if error_blob:
            state, error_class = _classify_error_blob(error_blob)
            result.state = state
            result.error_class = error_class
            result.detail = error_blob[:400]
            self.health.record(bare, state, duration_ms=result.duration_ms,
                               error_class=error_class)
            if session_id:
                self._delete_session(session_id)
            return result

        if result.exit_code not in (0, None) and not text:
            result.state = BRIDGE_BROKEN
            result.error_class = f"exit_{result.exit_code}"
            result.detail = (stderr.strip().splitlines() or [""])[-1][:200]
            self.health.record(bare, result.state, duration_ms=result.duration_ms,
                               error_class=result.error_class)
            if session_id:
                self._delete_session(session_id)
            return result

        if not text.strip():
            result.state = BRIDGE_BROKEN
            result.error_class = "empty_output"
            result.detail = "official CLI returned no assistant text"
            self.health.record(bare, result.state, duration_ms=result.duration_ms,
                               error_class=result.error_class)
            if session_id:
                self._delete_session(session_id)
            return result

        result.state = BRIDGE_OK
        result.version = self.version()
        self.health.record(bare, result.state, duration_ms=result.duration_ms)
        if session_id:
            # Best-effort cleanup of the bridge-created session.
            self._delete_session(session_id)
        return result

    def _delete_session(self, session_id: str) -> None:
        exe = self.runtime.executable
        if not exe or not session_id:
            return
        try:
            subprocess.run(
                [exe, "session", "delete", session_id],
                capture_output=True, text=True, timeout=VERSION_TIMEOUT_SEC,
                shell=False, stdin=subprocess.DEVNULL,
                cwd=str(self.context.work_dir), env=self.context.env(),
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except Exception:
            pass  # cleanup is best-effort and never fails the completion

    # -- public api ----------------------------------------------------------
    def complete(
        self,
        model_id: str,
        messages: Sequence[Dict[str, Any]],
        *,
        json_object: bool = False,
        timeout: Optional[float] = None,
        cancel_event: Optional[threading.Event] = None,
    ) -> BridgeResult:
        bare = bare_model_id(model_id)
        allowed, reason = self.model_allowed(model_id)
        if not allowed:
            return BridgeResult(state=UNSUPPORTED_BY_OPENCODE_FREE_BRIDGE,
                                model_id=bare, cli_model=cli_model_id(model_id),
                                error_class=reason, detail=reason)
        bare = bare_model_id(model_id)
        if self.health.provider_blocked():
            # Report the state that caused the cooldown, not a guess.
            provider = self.health.provider_status()
            return BridgeResult(state=str(provider.get("state") or BRIDGE_UPSTREAM_REJECTED),
                                model_id=bare, cli_model=cli_model_id(model_id),
                                error_class="provider_cooldown",
                                detail="bridge provider cooldown active")
        if self.health.model_blocked(bare):
            entry = self.health.model_status(bare)
            return BridgeResult(state=entry.get("state") or BRIDGE_BROKEN,
                                model_id=bare, cli_model=cli_model_id(model_id),
                                error_class="model_cooldown",
                                detail="bridge model cooldown active")
        prompt = serialize_messages(messages, json_object=json_object)
        return self._run(model_id, prompt, timeout, cancel_event)

    def canary(self, model_id: str = CANARY_MODEL, *,
               timeout: Optional[float] = 60.0) -> BridgeResult:
        """One minimal official-path invocation against one advertised free model."""
        result = self._run(bare_model_id(model_id), CANARY_PROMPT, timeout, None)
        result.version = self.version()
        return result

    def run_request(self, payload: Dict[str, Any], *,
                    timeout: Optional[float] = None,
                    cancel_event: Optional[threading.Event] = None) -> BridgeResult:
        """Shape-gated entry point used by the loopback server."""
        eligible, reason = classify_request_shape(payload)
        if not eligible:
            return BridgeResult(state=UNSUPPORTED_BY_OPENCODE_FREE_BRIDGE,
                                model_id=bare_model_id(str(payload.get("model") or "")),
                                cli_model=cli_model_id(str(payload.get("model") or "")),
                                error_class=reason,
                                detail=f"{UNSUPPORTED_BY_OPENCODE_FREE_BRIDGE}: {reason}")
        response_format = payload.get("response_format")
        json_object = isinstance(response_format, dict) and \
            str(response_format.get("type")) == "json_object"
        return self.complete(
            str(payload.get("model") or ""),
            payload.get("messages") or [],
            json_object=json_object,
            timeout=timeout,
            cancel_event=cancel_event,
        )


def _parse_events(stdout: str) -> Tuple[str, str, int, str]:
    """Parse the CLI JSONL event stream.

    Returns (final_text, session_id, tool_attempt_count, error_blob).
    Unicode-safe: each line is decoded independently and malformed lines are
    skipped rather than aborting the whole response.
    """
    text_parts: List[str] = []
    session_id = ""
    tool_attempts = 0
    error_blob = ""
    for line in stdout.splitlines():
        line = line.strip()
        if not line or not line.startswith("{"):
            continue
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if not isinstance(event, dict):
            continue
        if not session_id and isinstance(event.get("sessionID"), str):
            session_id = event["sessionID"]
        event_type = event.get("type")
        if event_type == "text":
            part = event.get("part") or {}
            if isinstance(part, dict) and isinstance(part.get("text"), str):
                text_parts.append(part["text"])
        elif event_type == "tool_use":
            tool_attempts += 1
        elif event_type == "error":
            error_blob = json.dumps(event.get("error") or {}, ensure_ascii=False)
    return "".join(text_parts).strip(), session_id, tool_attempts, error_blob


def bridge_summary(bridge: "OpenCodeBridge", model_ids: Sequence[str]) -> Dict[str, Any]:
    """Compact rollup for UI/WatchView surfaces."""
    diagnostics = bridge.diagnostics()
    rows = []
    for model_id in model_ids:
        status = bridge.health.model_status(model_id)
        rows.append({
            "model_id": model_id,
            "canonical_id": f"ocf/{model_id}",
            "bridge_state": status.get("state", ""),
            "last_ok_at": status.get("last_ok_at", ""),
            "blocked": bool(status.get("blocked")),
            "cooldown_until": status.get("cooldown_until", 0),
        })
    return {
        "bridge": diagnostics,
        "models": rows,
    }
