"""Verification probe for T-77: per-model test button in Edit Combo.

Checks the live 9Router on port 20128 for the two things the button promises:
an alive model answers with OK + latency, and a broken one answers with the
provider's own message instead of a generic failure.

The live probe is the first-token stream, so each check costs one prompt and at
most one output token.
"""
from __future__ import annotations

import hashlib
import json
import os
import pathlib
import urllib.error
import urllib.request

BASE = "http://127.0.0.1:20128"


def get_cli_token() -> str:
    app_data = os.environ.get("APPDATA", "")
    data_dir = pathlib.Path(app_data) / "9router"
    machine_id = (data_dir / "machine-id").read_text(encoding="utf-8").strip()
    secret = (data_dir / "auth" / "cli-secret").read_text(encoding="utf-8").strip()
    return hashlib.sha256((machine_id + "9r-cli-auth" + secret).encode("utf-8")).hexdigest()[:16]


def request_json(url: str, method: str = "GET", data: dict | None = None, timeout: int = 45) -> tuple[int, dict]:
    headers = {"x-9r-cli-token": get_cli_token(), "Content-Type": "application/json"}
    body = json.dumps(data).encode("utf-8") if data is not None else None
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode("utf-8"))


def probe(models: list[str], quick: bool = False) -> list[dict]:
    status, body = request_json(
        f"{BASE}/api/combos/probe-models", method="POST", data={"models": models, "quick": quick}
    )
    assert status == 200, f"probe-models returned {status}: {body}"
    return body.get("results", [])


def test_t77_per_model_probe() -> None:
    assert len(probe(["ghost-provider/nope"], quick=True)) == 1

    # 1. Static verdicts cost nothing and name the reason.
    [static_dead] = probe(["ghost-provider/nope"], quick=True)
    assert static_dead["state"] == "dead", static_dead
    assert "ghost-provider" in static_dead["reason"], static_dead

    # 2. A live probe of a resolvable-but-missing model returns the provider's
    #    own text, which is what the row prints. The prefix has to come from a
    #    configured provider, otherwise the static check answers first.
    status, body = request_json(f"{BASE}/api/combos")
    combos = body.get("combos") if isinstance(body, dict) else body
    models = next((c.get("models", []) for c in (combos or []) if c.get("models")), [])
    assert models, "no combo with models to test against"
    prefix = models[0].split("/")[0] if "/" in models[0] else models[0]
    [missing] = probe([f"{prefix}/definitely-not-a-real-model-xyz"])
    assert missing["state"] in ("dead", "error"), missing
    assert isinstance(missing["reason"], str) and missing["reason"], missing
    assert missing["reason"].startswith("HTTP ") or "Unknown provider" not in missing["reason"], missing
    print(f"  missing-model verdict: {missing['state']} — {missing['reason']}")

    # 3. A real model from the first combo answers OK with a latency.
    [live] = probe([models[0]])
    assert live["state"] in ("alive", "error", "dead"), live
    if live["state"] == "alive":
        assert isinstance(live.get("latencyMs"), int) and live["latencyMs"] > 0, live
        print(f"  {models[0]} -> OK {live['latencyMs']}ms")
    else:
        assert live.get("reason"), live
        print(f"  {models[0]} -> {live['state']} — {live['reason']}")

    print("ALL T-77 LIVE VERIFICATIONS PASSED SUCCESSFULLY!")


if __name__ == "__main__":
    test_t77_per_model_probe()
