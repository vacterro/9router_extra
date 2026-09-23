"""Keep the package and human-facing release identity in lockstep."""
from pathlib import Path
import re


ROOT = Path(__file__).resolve().parents[2]
EXPECTED_RELEASE = "0.1.0"


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
    assert versions == {name: EXPECTED_RELEASE for name in versions}, versions
