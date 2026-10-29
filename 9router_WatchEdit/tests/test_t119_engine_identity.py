"""T-119 - the engine client-identity self-check and its repair path.

The defect this locks down is recurrence, not a single bad build: the WorkBuddy
identity was fixed once as a patch applied to the installed compiled chunk, and
the next ``apply-update.ps1`` overwrote that chunk, so the same HTTP 400 code
11128 came back. The repair must therefore be driven by a declaration that a
gate reads on every install, and it must recognise the identity whether the
chunk is a freshly built package (constants hoisted by the minifier) or an older
install that only the runtime patch ever touched.
"""
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "9router_WatchEdit"))

from core import engine_identity as ei  # noqa: E402

IDENTITY = ei.ENGINE_IDENTITIES[0]

# A minified chunk shaped like a real build: the registry declaration with the
# desktop headers, followed by the models list that closes the region.
BUILT_CHUNK = (
    'x={workbuddy",alias:"wb",uiAlias:"wb",transport:{baseUrl:"https://www.workbuddy.ai/v2/chat/completions",'
    'forceStream:!0,thinkingFormat:"openai",headers:{"User-Agent":`WorkBuddy/${f} workbuddy-ai/${f} CLI/2.161.0",'
    '"X-Product":"SaaS","X-IDE-Type":"WorkBuddy","X-IDE-Name":"WorkBuddy","X-IDE-Version":"5.8.4",'
    '"X-Agent-Intent":"craft","X-Agent-Purpose":"conversation","X-Agent-Type":"main",'
    '"X-Private-Data":"true","X-Domain":"www.workbuddy.ai"}},models:['
    '{id:"hy3"}]};'
    'class i extends d.L{constructor(){super("workbuddy")}buildHeaders(a,b=!0,c,d,i){'
    'let j=super.buildHeaders(a,b,c,d,i);delete j["x-codebuddy-request"];return j}'
    'transformRequest(a,b,c,d){return b}}'
)

# The shape an install has before the identity ever reached it: the retired
# CodeBuddy CLI fingerprint, and an executor with no buildHeaders at all.
LEGACY_CHUNK = (
    'x={workbuddy",alias:"wb",uiAlias:"wb",transport:{baseUrl:"https://www.workbuddy.ai/v2/chat/completions",'
    'forceStream:!0,thinkingFormat:"openai",headers:{"User-Agent":"CLI/2.108.1 CodeBuddy/2.108.1",'
    '"X-Product":"SaaS","X-IDE-Type":"CLI","X-IDE-Name":"CLI","x-requested-with":"XMLHttpRequest",'
    '"x-codebuddy-request":"1"}},models:[{id:"hy3"}]};'
    'class i extends d.L{constructor(){super("workbuddy")}transformRequest(a,b,c,d){return b}}'
)

# A neighbouring provider in the same chunk that legitimately keeps the retired
# fingerprint: the check must never read it as WorkBuddy's own header.
NEIGHBOUR = (
    'y={codebuddy-cn",alias:"cbcn",transport:{headers:{"User-Agent":"CLI/2.108.1 CodeBuddy/2.108.1",'
    '"X-IDE-Type":"CLI","x-codebuddy-request":"1"}},models:[{id:"x"}]};'
)


def _install(tmp_path: Path, body: str, name: str = "5330.js") -> Path:
    root = tmp_path / "install" / "app" / ".next-cli-build" / "server" / "chunks"
    root.mkdir(parents=True)
    (root / name).write_text(body, encoding="utf-8")
    return tmp_path / "install"


def test_built_chunk_reports_the_identity_present(tmp_path):
    install = _install(tmp_path, BUILT_CHUNK + NEIGHBOUR)

    state = ei.check_one(IDENTITY, str(install))

    assert state.state == ei.IDENTITY_PRESENT, state.summary()
    assert state.ok


def test_legacy_chunk_reports_the_identity_missing_with_both_reasons(tmp_path):
    install = _install(tmp_path, LEGACY_CHUNK)

    state = ei.check_one(IDENTITY, str(install))

    assert state.state == ei.IDENTITY_MISSING
    assert state.missing_tokens, "the desktop headers must be named as absent"
    assert state.stale_tokens, "the retired CLI markers must be named as still sent"
    assert "identity MISSING" in state.summary()


def test_a_neighbour_providers_old_fingerprint_is_not_attributed_to_workbuddy(tmp_path):
    """codebuddy-cn keeps the retired UA on purpose; that is not WorkBuddy's problem."""
    install = _install(tmp_path, BUILT_CHUNK + NEIGHBOUR)

    window = ei.provider_window(IDENTITY, (install / "app/.next-cli-build/server/chunks/5330.js").read_text(encoding="utf-8"))

    assert window is not None
    assert "codebuddy-cn" not in window
    assert ei.check_one(IDENTITY, str(install)).state == ei.IDENTITY_PRESENT


def test_absent_engine_is_unavailable_not_broken(tmp_path):
    """No install is not a wrong identity, and must not raise a false alarm."""
    state = ei.check_one(IDENTITY, str(tmp_path / "nothing-here"))

    assert state.state == ei.IDENTITY_UNAVAILABLE
    assert not state.ok
    assert ei.all_ok([state]), "UNKNOWN must not read as a failure"


def test_an_installed_engine_without_the_provider_is_missing(tmp_path):
    """A build that dropped the provider is the regression this gate exists for."""
    install = _install(tmp_path, "var unrelated=1;")

    state = ei.check_one(IDENTITY, str(install))

    assert state.state == ei.IDENTITY_MISSING
    assert "no workbuddy declaration" in state.detail
    assert not ei.all_ok([state])


def test_repair_rewrites_a_legacy_chunk_and_the_recheck_passes(tmp_path, monkeypatch):
    install = _install(tmp_path, LEGACY_CHUNK)
    monkeypatch.setattr(ei, "_backup_dir", lambda: str(tmp_path / "rollback"))
    monkeypatch.setattr(ei, "_node_syntax_ok", lambda _path: True)

    backups = []
    state = ei.repair_one(IDENTITY, str(install), backups)

    assert state.state == ei.IDENTITY_PRESENT, state.summary()
    assert backups, "a repair must keep a backup of the chunk it rewrote"
    body = (install / "app/.next-cli-build/server/chunks/5330.js").read_text(encoding="utf-8")
    assert ei.PATCH_MARKER in body
    assert "WorkBuddy/5.8.4 workbuddy-ai/5.8.4 CLI/2.161.0" in body
    assert ei.check_one(IDENTITY, str(install)).ok


def test_repair_refuses_a_build_that_already_declares_buildHeaders(tmp_path, monkeypatch):
    """A chunk with its own buildHeaders is a build to fix, not to overwrite.

    Silently appending a second buildHeaders would shadow the package's own
    implementation, so the repair declines and says why.
    """
    broken = BUILT_CHUNK.replace('"X-Agent-Intent":"craft",', "")
    install = _install(tmp_path, broken)
    monkeypatch.setattr(ei, "_backup_dir", lambda: str(tmp_path / "rollback"))

    state = ei.repair_one(IDENTITY, str(install))

    assert state.state == ei.IDENTITY_MISSING
    assert "rebuild the package" in state.detail
    body = (install / "app/.next-cli-build/server/chunks/5330.js").read_text(encoding="utf-8")
    assert body == broken, "a declined repair must not touch the chunk"


def test_repair_preserves_the_original_when_the_patch_does_not_parse(tmp_path, monkeypatch):
    install = _install(tmp_path, LEGACY_CHUNK)
    monkeypatch.setattr(ei, "_backup_dir", lambda: str(tmp_path / "rollback"))
    monkeypatch.setattr(ei, "_node_syntax_ok", lambda _path: False)

    state = ei.repair_one(IDENTITY, str(install))

    assert state.state == ei.IDENTITY_MISSING
    assert "original preserved" in state.detail
    body = (install / "app/.next-cli-build/server/chunks/5330.js").read_text(encoding="utf-8")
    assert body == LEGACY_CHUNK


def test_ensure_reports_ok_without_touching_a_good_install(tmp_path):
    install = _install(tmp_path, BUILT_CHUNK)

    result = ei.ensure(str(install))

    assert result["outcome"] == "OK"
    assert result["backups"] == []
    assert ei.all_ok(result["states"])


def test_ensure_repairs_a_legacy_install_in_one_call(tmp_path, monkeypatch):
    install = _install(tmp_path, LEGACY_CHUNK)
    monkeypatch.setattr(ei, "_backup_dir", lambda: str(tmp_path / "rollback"))
    monkeypatch.setattr(ei, "_node_syntax_ok", lambda _path: True)

    result = ei.ensure(str(install))

    assert result["outcome"] == "REPAIRED"
    assert ei.all_ok(result["states"])
    assert result["backups"]


def test_the_declaration_names_the_package_module_that_carries_the_fix():
    """The durable fix lives in the package source, so the gate can name it."""
    assert IDENTITY.source_module == "open-sse/executors/workbuddy.js"
    assert "11128" in IDENTITY.reason
