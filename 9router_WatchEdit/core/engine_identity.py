"""
9router_WatchEdit - Installed-engine client-identity self-check and repair

Some providers in the 9router engine only serve requests that carry the
identity of their official desktop client. WorkBuddy is the live example: its
gateway rejects anything that still looks like the retired CodeBuddy CLI with
``HTTP 400 {"code":11128,"msg":"request illegal"}`` and "This call failed
channel verification ... not sent from an official client."

Why this module exists
----------------------
The first fix was a patch applied to the installed, compiled engine chunk.
Every ``npm install -g`` (that is, every ``apply-update.ps1``) rewrites those
chunks, so the fix silently disappeared and the same user-visible error came
back. A fix that must be re-applied by hand after every upgrade is not a fix.

So the identity is checked against a DECLARATION rather than remembered:
``ENGINE_IDENTITIES`` lists, per provider, the signals that must be present
and the signals that must be gone, each measured inside the region of the
compiled chunk that belongs to that provider. ``check()`` reports the state,
``repair()`` re-applies the declaration to an install whose package predates
it, and ``ensure()`` does check -> repair -> check in one call.

Minification
------------
The compiled chunk is minified and is NOT stable text: Webpack hoists string
constants into variables (``let f="5.8.4",g=`WorkBuddy/${f} ...```) and drops
the header keys into single-letter locals. So the declaration holds the tokens
that survive minification (string VALUES, not the identifiers holding them),
and every check is scoped to the provider's own region so that a neighbouring
provider's headers cannot be mistaken for this one's.

Where it is called
------------------
* ``apply-update.ps1`` - after the install, so an upgrade either carries the
  identity or is repaired before the success banner is printed.
* ``MainWindow`` startup - the scan costs ~60 ms, so an install made by any
  other route is noticed on the next launch instead of at the next failed
  request.

Nothing here touches credentials: it reads and (for a legacy install only)
rewrites compiled JavaScript.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import stat
import tempfile
from pathlib import Path
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional, Sequence

#: Default install root. Overridable for tests and non-standard installs.
DEFAULT_INSTALL = os.environ.get("NINEROUTER_INSTALL", r"C:\nodejs\node_modules\9router")

#: Compiled server root inside the install.
SERVER_SUBPATH = os.path.join("app", ".next-cli-build", "server")

#: Marker an older runtime patch wrote; a chunk carrying it still counts as
#: carrying the identity.
PATCH_MARKER = "__9rWbConv"

#: Reported states.
IDENTITY_PRESENT = "PRESENT"
IDENTITY_MISSING = "MISSING"
IDENTITY_UNAVAILABLE = "UNAVAILABLE"


@dataclass(frozen=True)
class ProviderIdentity:
    """One provider's required client identity.

    A chunk is one file holding several providers, so both the required and the
    forbidden signals are only counted inside the provider's own window.
    """

    provider: str
    #: Opens the provider's region in a registry chunk.
    scope_open: str
    #: Closes it (exclusive).
    scope_close: str = "models:["
    #: Tokens that must all appear in the window (string values survive minification).
    required_tokens: Sequence[str] = ()
    #: Tokens of a superseded client that must all be ABSENT from the window.
    forbidden_tokens: Sequence[str] = ()
    #: The executor class marker, when the provider has one.
    executor_needle: str = ""
    #: Why the identity is required, for the operator-facing message.
    reason: str = ""
    #: The module that ships the identity inside the package.
    source_module: str = ""
    #: Marker written by a legacy runtime patch, if one existed.
    legacy_patch_marker: Optional[str] = PATCH_MARKER


#: The declaration. Adding a provider that starts checking client identity means
#: adding one entry here; the check, the repair and the tests all read it.
ENGINE_IDENTITIES: Sequence[ProviderIdentity] = (
    ProviderIdentity(
        provider="workbuddy",
        scope_open='workbuddy",alias:"wb"',
        scope_close="models:[",
        required_tokens=(
            # The UA is a template literal: "WorkBuddy/5.8.4 workbuddy-ai/..." gets
            # split around the hoisted version variable, so only the stable halves
            # can be required.
            "workbuddy-ai/",
            '"X-IDE-Type":"WorkBuddy"',
            '"X-IDE-Name":"WorkBuddy"',
            '"X-Agent-Intent":"craft"',
            '"X-Domain":"www.workbuddy.ai"',
        ),
        forbidden_tokens=(
            '"X-IDE-Type":"CLI"',
            "CLI/2.108.1 CodeBuddy/2.108.1",
        ),
        executor_needle='super("workbuddy")',
        reason=(
            "WorkBuddy's gateway rejects requests that do not carry the official "
            "desktop client identity with HTTP 400 code 11128 request illegal / "
            "channel verification failed"
        ),
        source_module="open-sse/executors/workbuddy.js",
    ),
)


@dataclass
class IdentityState:
    provider: str
    state: str
    chunk: Optional[str] = None
    detail: str = ""
    #: Required tokens the window does not carry.
    missing_tokens: List[str] = field(default_factory=list)
    #: Superseded tokens still present in the window.
    stale_tokens: List[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.state == IDENTITY_PRESENT

    def summary(self) -> str:
        if self.state == IDENTITY_UNAVAILABLE:
            return f"{self.provider}: UNKNOWN - {self.detail}"
        if self.state == IDENTITY_PRESENT:
            return f"{self.provider}: official client identity present"
        parts = [f"{self.provider}: identity MISSING"]
        if self.missing_tokens:
            parts.append("absent: " + ", ".join(self.missing_tokens))
        if self.stale_tokens:
            parts.append("superseded still sent: " + ", ".join(self.stale_tokens))
        if self.detail:
            parts.append(self.detail)
        return " - ".join(parts)


def server_root(install: Optional[str] = None) -> str:
    return os.path.join(install or DEFAULT_INSTALL, SERVER_SUBPATH)


def _iter_js(root: str) -> Iterable[str]:
    if not os.path.isdir(root):
        return
    for dirpath, _dirnames, filenames in os.walk(root):
        for name in filenames:
            if name.endswith(".js"):
                yield os.path.join(dirpath, name)


def _read(path: str) -> Optional[str]:
    try:
        with open(path, "r", encoding="utf-8", newline="") as handle:
            return handle.read()
    except (OSError, UnicodeError):
        return None


def provider_window(identity: ProviderIdentity, body: str) -> Optional[str]:
    """The slice of a chunk that belongs to this provider, or None."""
    start = body.find(identity.scope_open)
    if start < 0:
        return None
    end = body.find(identity.scope_close, start)
    if end < 0:
        end = min(len(body), start + 4000)
    return body[start:end]


def find_chunk(identity: ProviderIdentity, install: Optional[str] = None) -> Optional[str]:
    """Path of the compiled chunk carrying this provider's declaration.

    Chunk file names change on every build, so the lookup is by content.
    """
    for path in _iter_js(server_root(install)):
        body = _read(path)
        if body is None:
            continue
        if identity.scope_open in body or (
            identity.executor_needle and identity.executor_needle in body
        ):
            return path
    return None


def check_one(identity: ProviderIdentity, install: Optional[str] = None) -> IdentityState:
    root = server_root(install)
    if not os.path.isdir(root):
        return IdentityState(
            identity.provider, IDENTITY_UNAVAILABLE,
            detail=f"no compiled engine at {root}",
        )

    chunk = find_chunk(identity, install)
    if chunk is None:
        # The engine is installed but does not declare this provider at all.
        # That is a MISSING declaration, not an unknown one: a build that
        # dropped the provider is exactly the kind of regression this gate is
        # here to catch.
        return IdentityState(
            identity.provider, IDENTITY_MISSING,
            detail=f"engine installed but no {identity.provider} declaration found under {root}",
            missing_tokens=[identity.scope_open],
        )

    body = _read(chunk)
    if body is None:
        return IdentityState(identity.provider, IDENTITY_UNAVAILABLE, chunk=chunk,
                             detail="chunk unreadable")

    # A marker in another provider or comment does not prove an override.
    # Accept only the complete reviewed repair attached to this executor.
    repaired_executor = identity.executor_needle + "}" + _patch_method(identity)
    if identity.legacy_patch_marker and repaired_executor in body:
        return IdentityState(identity.provider, IDENTITY_PRESENT, chunk=chunk,
                             detail="legacy runtime patch marker present")

    window = provider_window(identity, body)
    if window is None:
        return IdentityState(identity.provider, IDENTITY_UNAVAILABLE, chunk=chunk,
                             detail=f"no {identity.scope_open} region in the chunk")

    missing = [token for token in identity.required_tokens if token not in window]
    # A retired marker that the executor explicitly deletes is gone from the
    # wire, not present: `delete x["x-codebuddy-request"]` must not read as stale.
    stale = [
        token for token in identity.forbidden_tokens
        if token in window and f'delete ' not in _assignment_context(window, token)
    ]

    if not missing and not stale:
        return IdentityState(identity.provider, IDENTITY_PRESENT, chunk=chunk)

    return IdentityState(
        identity.provider, IDENTITY_MISSING, chunk=chunk,
        missing_tokens=missing, stale_tokens=stale,
    )


def _assignment_context(window: str, token: str, span: int = 40) -> str:
    index = window.find(token)
    if index < 0:
        return ""
    return window[max(0, index - span): index]


def check(
    install: Optional[str] = None, identities: Optional[Sequence[ProviderIdentity]] = None
) -> List[IdentityState]:
    return [check_one(identity, install) for identity in identities or ENGINE_IDENTITIES]


def all_ok(states: Sequence[IdentityState]) -> bool:
    """True when nothing is MISSING.

    UNAVAILABLE is not a failure: an engine that is not installed at all does
    not have a wrong identity, and reporting it as broken would train the
    operator to ignore the warning.
    """
    return all(state.state != IDENTITY_MISSING for state in states)


# --- repair -----------------------------------------------------------------

def _backup_dir() -> str:
    from config import LOCALAPPDATA_DIR, PRIVATE_STORAGE_AVAILABLE
    if not PRIVATE_STORAGE_AVAILABLE:
        raise OSError("Private rollback storage unavailable")
    return str(LOCALAPPDATA_DIR / "engine" / "rollback")


def _plain_path(path: str, *, file: bool = False) -> bool:
    """Refuse links, Windows junctions and shared hard-linked chunks."""
    target = Path(path)
    if not target.is_absolute():
        return False
    try:
        for item in (target, *target.parents):
            info = item.lstat()
            if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
                return False
        info = target.stat()
        return not file or (stat.S_ISREG(info.st_mode) and info.st_nlink == 1)
    except OSError:
        return False


def backup_chunk(path: str) -> str:
    directory = _backup_dir()
    parent = Path(directory)
    while not parent.exists():
        parent = parent.parent
    if not _plain_path(str(parent)):
        raise OSError("Rollback parent is linked or unavailable")
    os.makedirs(directory, exist_ok=True)
    if not _plain_path(directory):
        raise OSError("Rollback storage is linked or unavailable")
    fd, target = tempfile.mkstemp(prefix=os.path.basename(path) + ".", suffix=".bak", dir=directory)
    os.close(fd)
    shutil.copy2(path, target)
    return target


def _node_syntax_ok(path: str) -> bool:
    node = shutil.which("node")
    if not node:
        return False
    try:
        result = subprocess.run([node, "--check", path], capture_output=True, text=True, timeout=15)
        return result.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def _patch_method(identity: ProviderIdentity) -> str:
    """The compiled `buildHeaders` override used to repair a legacy install."""
    version = "5.8.4"
    cli_version = "2.161.0"
    headers = ",".join([
        f'"User-Agent":"WorkBuddy/{version} workbuddy-ai/{version} CLI/{cli_version}"',
        '"X-Product":"SaaS"',
        '"X-IDE-Type":"WorkBuddy"',
        '"X-IDE-Name":"WorkBuddy"',
        f'"X-IDE-Version":"{version}"',
        '"X-Agent-Intent":"craft"',
        '"X-Agent-Purpose":"conversation"',
        '"X-Agent-Type":"main"',
        '"X-Private-Data":"true"',
        '"X-Domain":"www.workbuddy.ai"',
    ])
    return (
        "buildHeaders(a,b=!0,c,d){"
        "let e=super.buildHeaders(a,b,c,d);"
        'delete e["x-requested-with"],delete e["x-codebuddy-request"];'
        f"let f=globalThis.{PATCH_MARKER}||(globalThis.{PATCH_MARKER}=new Map),"
        'g=(a&&a.providerSpecificData&&a.providerSpecificData.uid)||"default",'
        "h=f.get(g);h||(h=crypto.randomUUID(),f.set(g,h));"
        "let i=crypto.randomUUID(),j=crypto.randomUUID(),"
        'k=crypto.randomUUID().replace(/-/g,""),'
        'l=crypto.randomUUID().replace(/-/g,"").slice(0,16);'
        f"return Object.assign(e,{{{headers},"
        '"X-Conversation-ID":h,"X-Conversation-Request-ID":i,"X-Root-Request-ID":i,'
        '"X-Conversation-Message-ID":j,"X-Request-ID":j,'
        '"X-Trace-ID":k,"traceparent":"00-"+k+"-"+l+"-01","b3":k+"-"+l+"-1",'
        '"X-B3-TraceId":k,"X-B3-SpanId":l,"X-B3-Sampled":"1"},'
        "(a&&a.providerSpecificData&&a.providerSpecificData.uid)"
        '?{"X-User-Id":a.providerSpecificData.uid}:{})}'
    )


def repair_one(
    identity: ProviderIdentity, install: Optional[str] = None, record: Optional[List[str]] = None
) -> IdentityState:
    """Re-apply the identity to an install whose package predates it.

    Only an executor that still runs ``constructor(){super("workbuddy")}
    transformRequest(...)`` can be patched this safely. An executor that
    already declares its own ``buildHeaders`` is a build we must not silently
    rewrite, so it is reported MISSING with that reason spelled out: the fix
    then belongs in the package, not in the installed copy.
    """
    state = check_one(identity, install)
    if state.ok or state.state == IDENTITY_UNAVAILABLE:
        return state
    if not identity.executor_needle:
        state.detail = "no executor to patch; rebuild the package instead"
        return state

    chunk = state.chunk
    body = _read(chunk) if chunk else None
    if body is None:
        return state
    if not _plain_path(chunk, file=True):
        state.detail = "linked or shared chunk refused; rebuild the package instead"
        return state

    anchor = re.compile(re.escape(identity.executor_needle) + r"\}(?=transformRequest\()")
    match = anchor.search(body)
    if not match:
        state.detail = "executor already declares buildHeaders; rebuild the package instead"
        return state

    lock = chunk + ".identity.lock"
    try:
        fd = os.open(lock, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except OSError:
        state.detail = "another repair owns this chunk or the repair lock is unavailable"
        return state
    staged = None
    try:
        os.close(fd)
        original = Path(chunk).read_bytes()
        if original.decode("utf-8") != body or not _plain_path(chunk, file=True):
            state.detail = "chunk changed during review; original preserved"
            return state
        patched = body[:match.end()] + _patch_method(identity) + body[match.end():]
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", newline="",
                                         suffix=".js", dir=os.path.dirname(chunk), delete=False) as handle:
            staged = handle.name
            handle.write(patched)
        if not _node_syntax_ok(staged):
            state.detail = "patch syntax verification unavailable or failed; original preserved"
            return state
        if not _plain_path(chunk, file=True) or Path(chunk).read_bytes() != original:
            state.detail = "chunk changed during syntax verification; original preserved"
            return state
        backup = backup_chunk(chunk)
        if Path(backup).read_bytes() != original or Path(chunk).read_bytes() != original:
            state.detail = "chunk changed during backup; current chunk preserved"
            return state
        shutil.copymode(chunk, staged)
        os.replace(staged, chunk)
        staged = None
        if record is not None:
            record.append(backup)
        return check_one(identity, install)
    except (OSError, UnicodeError) as exc:
        state.detail = "repair could not complete; original or rollback copy preserved: " + str(exc)
        return state
    finally:
        if staged is not None:
            os.unlink(staged)
        os.unlink(lock)


def repair(
    install: Optional[str] = None,
    identities: Optional[Sequence[ProviderIdentity]] = None,
    backups: Optional[List[str]] = None,
) -> List[IdentityState]:
    return [repair_one(identity, install, backups) for identity in (identities or ENGINE_IDENTITIES)]


def ensure(
    install: Optional[str] = None, identities: Optional[Sequence[ProviderIdentity]] = None
) -> Dict[str, object]:
    """Check, repair if needed, re-check. One call for a startup or a gate."""
    checked = check(install, identities)
    need = [state for state in checked if state.state == IDENTITY_MISSING]
    backups: List[str] = []
    if need:
        final = repair(install, identities, backups)
        outcome = "REPAIRED" if all(state.ok for state in final) else "REPAIR_FAILED"
    else:
        final = checked
        outcome = "UNAVAILABLE" if any(s.state == IDENTITY_UNAVAILABLE for s in checked) else "OK"
    return {
        "outcome": outcome,
        "states": final,
        "backups": backups,
        "messages": [state.summary() for state in final],
    }
