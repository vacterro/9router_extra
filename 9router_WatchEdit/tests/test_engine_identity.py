"""
Regression tests for the WorkBuddy client-identity self-repair gate (T-119).

Recurrence pinned: a ``npm install -g`` of a new 9router package rewrites the
compiled engine chunks, wiping any hand-applied identity patch and silently
reintroducing HTTP 400 / code 11128 against WorkBuddy models. The fix lives in
``core/engine_identity.py``: a DECLARATION is checked after every install
(``tools/engine_identity_gate.py``) and on every launch, and ``repair``
re-applies it to a legacy install without ever rewriting a chunk that already
declares its own identity.

These tests run against an in-memory synthetic install so the declaration, the
token-window scoping, the repair path, and the refuse-to-rewrite rule are all
pinned without touching the real ``node_modules`` install.
"""
import os
import textwrap

import pytest

import core.engine_identity as ei
from core.engine_identity import (
    IDENTITY_PRESENT,
    IDENTITY_MISSING,
    IDENTITY_UNAVAILABLE,
    check_one,
    repair_one,
)

IDENTITY = ei.ENGINE_IDENTITIES[0]


def _write_chunk(install: str, subdir: str, name: str, body: str) -> str:
    root = os.path.join(install, ei.SERVER_SUBPATH, subdir)
    os.makedirs(root, exist_ok=True)
    path = os.path.join(root, name)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(body)
    return path


# A valid-JS chunk mirroring the real compiled shape (see the installed
# 1701.js). The provider config object carries scope_open
# (`"workbuddy",alias:"wb"`) and the identity tokens as header values, all
# inside the provider window that ends at the first `models:[`. The WB executor
# class extends a base and runs `constructor(){super("workbuddy")}transformRequest(...)`
# exactly as the repair anchor regex expects.
LEGACY_BODY = textwrap.dedent("""\
    /* 0.chunk.js */
    class Base{}{}
    class WB extends Base{constructor(){super("workbuddy")}transformRequest(a){return a;}}
    var reg={id:"workbuddy",alias:"wb",executor:WB,models:["wb/hy3","wb/deepseek-v4-flash"]};
""")

PRESENT_BODY = LEGACY_BODY.replace(
    'var reg={id:"workbuddy",alias:"wb"',
    'var reg={id:"workbuddy",alias:"wb",'
    'headers:{"User-Agent":"WorkBuddy/5.8.4 workbuddy-ai/5.8.4 CLI/2.161.0",\n'
    '"X-IDE-Type":"WorkBuddy","X-IDE-Name":"WorkBuddy",\n'
    '"X-Agent-Intent":"craft","X-Domain":"www.workbuddy.ai"}',
)


@pytest.fixture
def legacy_install(tmp_path):
    install = str(tmp_path / "install")
    _write_chunk(install, "chunks", "0.chunk.js", LEGACY_BODY)
    return install


@pytest.fixture
def patched_install(tmp_path):
    install = str(tmp_path / "install")
    _write_chunk(install, "chunks", "0.chunk.js", PRESENT_BODY)
    return install


def test_missing_identity_is_detected(legacy_install):
    """The declaration flags absent tokens, scoped to the provider window."""
    state = check_one(IDENTITY, install=legacy_install)
    assert state.state == IDENTITY_MISSING
    assert '"X-IDE-Type":"WorkBuddy"' in state.missing_tokens
    assert '"X-IDE-Type":"CLI"' not in state.stale_tokens


def test_present_identity_is_detected(patched_install):
    state = check_one(IDENTITY, install=patched_install)
    assert state.state == IDENTITY_PRESENT
    assert state.ok


def test_repair_writes_backed_up_and_restores(legacy_install):
    """repair re-applies the identity, keeps a backup, and the chunk still parses."""
    chunk = ei.find_chunk(IDENTITY, install=legacy_install)
    assert chunk is not None
    original = open(chunk, encoding="utf-8").read()

    backups: list = []
    state = repair_one(IDENTITY, install=legacy_install, record=backups)

    assert state.state == IDENTITY_PRESENT
    assert state.ok
    assert len(backups) == 1
    assert open(backups[0], encoding="utf-8").read() == original
    patched = open(chunk, encoding="utf-8").read()
    assert "workbuddy-ai/" in patched
    assert '"X-IDE-Type":"WorkBuddy"' in patched
    # the repaired chunk must still be syntactically valid JS
    import shutil, subprocess
    node = shutil.which("node")
    if node:
        result = subprocess.run([node, "--check", chunk], capture_output=True, text=True)
        assert result.returncode == 0, result.stderr


def test_repair_refuses_to_rewrite_a_declared_executor(tmp_path):
    """A chunk whose executor already has its own buildHeaders must not be clobbered."""
    body = textwrap.dedent("""\
        /* 2.chunk.js */
        class Base{}
        class WB extends Base{constructor(){super("workbuddy")}customHeaders(){return {};}}var reg={id:"workbuddy",alias:"wb",executor:WB,models:["wb/hy3"]};
    """)
    install = str(tmp_path / "install")
    _write_chunk(install, "chunks", "2.chunk.js", body)
    assert check_one(IDENTITY, install=install).state == IDENTITY_MISSING
    repaired = repair_one(IDENTITY, install=install, record=[])
    assert repaired.state == IDENTITY_MISSING
    assert "already declares buildHeaders" in repaired.detail


def test_unavailable_when_engine_absent(tmp_path):
    install = str(tmp_path / "empty")
    state = check_one(IDENTITY, install=install)
    assert state.state == IDENTITY_UNAVAILABLE
    assert not state.ok


def test_repair_idempotent_when_already_present(patched_install):
    """ensure() on an already-correct install reports OK and keeps no backup."""
    result = ei.ensure(install=patched_install)
    assert result["outcome"] == "OK"
    assert result["backups"] == []
    assert IDENTITY.provider in " ".join(result["messages"])


def test_check_only_never_mutates(legacy_install):
    """The --check-only path must never write the install."""
    chunk = ei.find_chunk(IDENTITY, install=legacy_install)
    before = open(chunk, encoding="utf-8").read()
    ei.check(install=legacy_install)  # read-only
    after = open(chunk, encoding="utf-8").read()
    assert before == after
