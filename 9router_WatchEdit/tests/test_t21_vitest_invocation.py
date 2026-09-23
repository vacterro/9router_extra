"""T-21 — 9Router source root vitest invocation trap.

The audited defect: invoking the vitest binary from the SOURCE ROOT
(`.\\tests\\node_modules\\.bin\\vitest.cmd ...`) does not load
`tests/vitest.config.js`, so the `@/` and `open-sse` path aliases are missing
and every test importing `@/shared/constants/config` (or any `@/...` path)
fails with ERR_MODULE_NOT_FOUND. The config-aware invocation
(`npm --prefix tests test -- ...`) works.

Acceptance (per the ticket): the root invocation either loads the aliases or
tooling/docs reject it clearly, and model-test-routing passes without an
unresolved `@/shared/constants/config`.

This test is OFFLINE and deterministic:
  * it always checks the repository-side DOCUMENTED contract (README names the
    trap and the correct invocation);
  * when the deployed 9Router source checkout is present it additionally proves
    the alias-resolving root config exists and re-exports the tests config.

The source checkout lives OUTSIDE the repository (`%APPDATA%\\9router\\source`),
so the live half is skipped when absent rather than failing.
"""
import os
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
README = REPO_ROOT / "README.md"

SOURCE_ROOT = Path(os.environ.get("APPDATA", "")) / "9router" / "source"


def test_readme_documents_the_trap_and_the_working_invocation():
    text = README.read_text(encoding="utf-8")
    assert "vitest invocation trap" in text, "README must name the T-21 trap"
    # The exact wrong invocation is called out.
    assert "tests\\node_modules\\.bin\\vitest.cmd" in text, (
        "README must name the root-binary invocation that bypasses the config"
    )
    # The config-aware invocation is prescribed.
    assert "npm --prefix tests test" in text, (
        "README must prescribe the config-aware invocation"
    )
    # The exact error string the trap produces is documented.
    assert "@/shared/constants/config" in text, (
        "README must record the exact unresolved-alias error"
    )


@pytest.mark.skipif(
    not (SOURCE_ROOT / "tests" / "vitest.config.js").is_file(),
    reason="deployed 9Router source checkout not present",
)
def test_source_root_alias_config_resolves():
    tests_config = SOURCE_ROOT / "tests" / "vitest.config.js"
    body = tests_config.read_text(encoding="utf-8")
    # The tests config owns the two aliases the trap breaks.
    assert "@/ " in body.replace("@/", "@/ ") or '"@/"' in body or "/^@\\//" in body
    assert "open-sse" in body

    root_config = SOURCE_ROOT / "vitest.config.js"
    assert root_config.is_file(), (
        "the source root needs a vitest.config.js that re-exports the tests "
        "config, so the root binary loads the aliases (T-21)"
    )
    root_body = root_config.read_text(encoding="utf-8")
    assert "tests/vitest.config.js" in root_body, (
        "the root config must re-export tests/vitest.config.js"
    )
