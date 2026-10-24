"""T-96 regression: the pre-merge conflict-marker gate must see bare markers
and the whole tracked tree, not just 9router_WatchEdit/**/*.py."""
import importlib.util
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
GATE_PATH = REPO_ROOT / "tools" / "run_premerge_gate.py"


@pytest.fixture(scope="module")
def gate():
    spec = importlib.util.spec_from_file_location("run_premerge_gate", GATE_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize(
    "text",
    [
        "=======",
        "<<<<<<< HEAD",
        "<<<<<<<",
        ">>>>>>> feature-branch",
        ">>>>>>>",
        "||||||| merged common ancestors",
        "|||||||",
    ],
)
def test_real_conflict_markers_detected(gate, text):
    assert gate.CONFLICT_MARKER_RE.search(text), text


@pytest.mark.parametrize(
    "text",
    [
        "===========================",   # ASCII section underline (README.txt)
        "===",                          # short markdown underline
        "inline ======= text",           # not at line start
        "<<<<<<<<< not a marker",         # 9 chars is not git's 7
        "'======='",                     # quoted prose
    ],
)
def test_non_markers_are_not_flagged(gate, text):
    assert not gate.CONFLICT_MARKER_RE.search(text), text


def test_tracked_scope_covers_more_than_app_python(gate):
    """The scan set is the tracked tree: tools/ and root files included."""
    files = gate._tracked_text_files()
    if not files:
        pytest.skip("git unavailable in this environment")
    names = {str(p.relative_to(REPO_ROOT)).replace("\\", "/") for p in files}
    assert any(n.startswith("tools/") for n in names)
    assert any(n for n in names if "/" not in n)  # root-level tracked files


def test_gate_fails_on_bare_separator_in_tools_scope(gate, tmp_path):
    stale = tmp_path / "tools" / "regression.py"
    stale.parent.mkdir(parents=True)
    stale.write_text("# code from an unresolved merge\n\n=======\nmore code\n", encoding="utf-8")

    original = gate._tracked_text_files
    gate._tracked_text_files = lambda: [stale]
    try:
        assert gate.step_git_sanity() is False
    finally:
        gate._tracked_text_files = original


def test_gate_passes_on_clean_tree(gate):
    """Control: the live repo has no conflict markers (known-good input)."""
    assert gate.step_git_sanity() is True
