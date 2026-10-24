"""Keep the package and human-facing release identity in lockstep.

``VERSION`` is the canonical release surface (the ship procedure says so), so
the expectation is derived from it rather than hardcoded here. A hardcoded
literal silently breaks on the next version bump -- which is exactly how the
v0.1.1 release shipped with pyproject.toml still at 0.1.0 while this test kept
asserting 0.1.0 and going red.
"""
from pathlib import Path
import re


ROOT = Path(__file__).resolve().parents[2]


def _canonical_release() -> str:
    """The single source of truth every other surface must match."""
    version = (ROOT / "VERSION").read_text(encoding="utf-8").strip()
    assert re.fullmatch(r"[0-9]+(?:\.[0-9]+){2}", version), f"VERSION is not a static semver: {version!r}"
    return version


def _project_version() -> str:
    text = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    project = re.search(r"(?ms)^\[project\]\s*(.*?)(?=^\[|\Z)", text)
    assert project, "pyproject.toml is missing [project]"
    version = re.search(r'(?m)^version\s*=\s*["\']([^"\']+)["\']\s*$', project.group(1))
    assert version, "[project].version is missing or not static"
    return version.group(1)


def test_release_version_surfaces_match():
    version_file = (ROOT / "VERSION").read_text(encoding="utf-8").strip()
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    readme_match = re.search(r"(?m)^Version:\s+\*\*v?([^*]+)\*\*\s*$", readme)
    assert readme_match, "README.md is missing its release version line"
    changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    changelog_match = re.search(r"(?m)^##\s+([0-9]+(?:\.[0-9]+){2})\s*$", changelog)
    assert changelog_match, "CHANGELOG.md is missing its release heading"

    versions = {
        "pyproject.toml": _project_version(),
        "VERSION": version_file,
        "README.md": readme_match.group(1),
        "CHANGELOG.md": changelog_match.group(1),
    }
    assert versions == {name: _canonical_release() for name in versions}, versions
