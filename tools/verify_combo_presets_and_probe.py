"""Verification probe for T-76: Combo health probing, dead model cleaning, and user presets.
Checks against live 9Router running on port 20128.
"""
from __future__ import annotations

import hashlib
import json
import os
import pathlib
import urllib.request


def get_cli_token() -> str:
    app_data = os.environ.get("APPDATA", "")
    data_dir = pathlib.Path(app_data) / "9router"
    machine_id = (data_dir / "machine-id").read_text(encoding="utf-8").strip()
    secret = (data_dir / "auth" / "cli-secret").read_text(encoding="utf-8").strip()
    raw = (machine_id + "9r-cli-auth" + secret).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()[:16]


def request_json(url: str, method: str = "GET", data: dict | None = None) -> tuple[int, dict]:
    headers = {
        "x-9r-cli-token": get_cli_token(),
        "Content-Type": "application/json",
    }
    body = json.dumps(data).encode("utf-8") if data is not None else None
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode("utf-8"))


def test_t76_live_endpoints() -> None:
    base = "http://127.0.0.1:20128"

    # 1. GET user presets
    status, body = request_json(f"{base}/api/combos/user-presets")
    assert status == 200, f"Expected 200, got {status}: {body}"
    assert "presets" in body

    # 2. POST create user preset
    status, body = request_json(
        f"{base}/api/combos/user-presets",
        method="POST",
        data={"name": "T-76 Auto Test Preset", "models": ["or/claude-3-5-sonnet", "cu/gpt-4o", "or/claude-3-5-sonnet"]},
    )
    assert status == 201, f"Expected 201, got {status}: {body}"
    preset = body.get("preset", {})
    preset_id = preset.get("id")
    assert preset_id
    # Duplicates deduplicated
    assert preset.get("models") == ["or/claude-3-5-sonnet", "cu/gpt-4o"]

    # 3. GET preset by ID
    status, body = request_json(f"{base}/api/combos/user-presets/{preset_id}")
    assert status == 200
    assert body.get("preset", {}).get("name") == "T-76 Auto Test Preset"

    # 4. POST duplicate preset
    status, body = request_json(f"{base}/api/combos/user-presets/{preset_id}/duplicate", method="POST")
    assert status == 201
    dup_id = body.get("preset", {}).get("id")
    assert dup_id
    assert body.get("preset", {}).get("name") == "T-76 Auto Test Preset (Copy)"

    # 5. POST probe-models quick classification
    status, body = request_json(
        f"{base}/api/combos/probe-models",
        method="POST",
        data={"models": ["ghost-provider/nonexistent-model", "SAIFREN"], "quick": True},
    )
    assert status == 200
    results = body.get("results", [])
    assert len(results) == 2
    r0 = results[0]
    r1 = results[1]
    assert r0.get("model") == "ghost-provider/nonexistent-model"
    assert r0.get("state") == "dead"
    assert r1.get("model") == "SAIFREN"
    assert r1.get("state") == "alive"

    # 6. DELETE presets
    status, _ = request_json(f"{base}/api/combos/user-presets/{preset_id}", method="DELETE")
    assert status == 200
    status, _ = request_json(f"{base}/api/combos/user-presets/{dup_id}", method="DELETE")
    assert status == 200

    print("ALL T-76 LIVE VERIFICATIONS PASSED SUCCESSFULLY!")


if __name__ == "__main__":
    test_t76_live_endpoints()
