"""Ceiling rule for 9router's per-provider thinking mode.

The knob (settings.providerThinking) is shared by every model of a provider,
while each model has its own level ladder. Choosing the UNION ceiling broke the
router for real: nvidia's union contains "max", the app clamps max -> xhigh for
models without max, and nim's vLLM endpoint accepts only low/medium/high, so
"nvidia/nemotron-3-ultra-550b-a55b"-style requests came back HTTP 400
literal_error on body.reasoning_effort. The rule under test therefore takes the
INTERSECTION: the highest level every reasoning model of a provider declares.

The "mixed" case is the deliberate red control -- union-max answers "max" there
and that is exactly the answer these tests reject.
"""
import json
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "tools" / "thinking_levels_max.mjs"

# provider -> models, chosen for their declared ladders in the 9router source:
#   claude-opus-4-8        -> claude-adaptive  [none low medium high max]
#   glm-5.3                -> zai onOff        [none thinking]
#   gpt-5.2                -> openai           [none minimal low medium high xhigh]
#   gpt-3.5-turbo          -> no reasoning     (null ladder)
FIXTURE = {
    "all-max": ["claude-opus-4-8", "claude-haiku-4.5"],
    "xhigh-only": ["gpt-5.2"],
    "onoff-only": ["glm-5.3"],
    "mixed": ["claude-opus-4-8", "glm-5.3", "gpt-5.2"],
    "no-reasoning": ["gpt-3.5-turbo"],
}


def _run(fixture, tmp_path):
    src = tmp_path / "provider_models.json"
    src.write_text(json.dumps(fixture), encoding="utf-8")
    res = subprocess.run(
        ["node", str(SCRIPT), str(src)],
        capture_output=True, text=True, check=True,
    )
    return json.loads(res.stdout)


def test_ceiling_is_the_ladder_intersection(tmp_path):
    patch = _run(FIXTURE, tmp_path)["patch"]
    assert patch["all-max"] == {"mode": "max"}
    assert patch["xhigh-only"] == {"mode": "xhigh"}
    assert patch["onoff-only"] == {"mode": "thinking"}


def test_empty_intersection_falls_back_to_high_not_max(tmp_path):
    # The union ceiling here is "max"; the intersection is empty. "max" is the
    # value that produced the HTTP 400 literal_error on body.reasoning_effort.
    patch = _run(FIXTURE, tmp_path)["patch"]
    assert patch["mixed"] == {"mode": "high"}


def test_provider_without_reasoning_models_is_skipped(tmp_path):
    patch = _run(FIXTURE, tmp_path)["patch"]
    assert "no-reasoning" not in patch


def test_none_is_never_selected(tmp_path):
    patch = _run(FIXTURE, tmp_path)["patch"]
    assert all(v["mode"] != "none" for v in patch.values())


if __name__ == "__main__":
    import pytest

    raise SystemExit(pytest.main([__file__, "-q"]))
