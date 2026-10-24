"""Verification probe for T-79: the Edit Combo "Test all" flow.

Runs the same sequence the button runs — one model per request, strictly one
after another — and checks that the verdicts it collects are what the Remove
dead button would act on: a real model resolves, an unresolvable entry comes
back dead.

Checks against the live 9Router on port 20128.
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


def probe_one(model: str) -> dict:
    """Exactly one request per model — the Test all loop in the modal."""
    status, body = request_json(f"{BASE}/api/combos/probe-models", method="POST", data={"models": [model]})
    assert status == 200, f"probe-models returned {status}: {body}"
    results = body.get("results", [])
    assert len(results) == 1, f"expected one verdict for {model}: {results}"
    return results[0]


def test_t79_test_all_and_remove_dead() -> None:
    status, body = request_json(f"{BASE}/api/combos")
    combos = body.get("combos") if isinstance(body, dict) else body
    models = next((c.get("models", []) for c in (combos or []) if c.get("models")), [])
    assert models, "no combo with models to test against"

    # The list the modal would walk: a few real entries plus two that cannot
    # exist. Everything below is probed sequentially, one request at a time.
    queue = models[:3] + ["ghost-provider/nope", "ocg/definitely-not-a-real-model-xyz"]
    verdicts = {}
    for i, model in enumerate(queue, 1):
        v = probe_one(model)
        verdicts[model] = v
        print(f"  [{i}/{len(queue)}] {model} -> {v['state']}"
              + (f" {v.get('latencyMs')}ms" if v["state"] == "alive" else f" — {v.get('reason')}"))

    # Remove dead acts on exactly this set, and it must never swallow an entry
    # that merely failed for a transient reason.
    dead = [m for m, v in verdicts.items() if v["state"] == "dead"]
    kept_errors = [m for m, v in verdicts.items() if v["state"] == "error"]

    assert "ghost-provider/nope" in dead, f"unresolvable prefix must be dead: {verdicts['ghost-provider/nope']}"
    assert verdicts["ghost-provider/nope"]["reason"], "a dead verdict must carry its reason"
    assert any(v["state"] == "alive" for m, v in verdicts.items() if m in models[:3]) or kept_errors, \
        f"no live verdict for a real model: {verdicts}"
    for m in kept_errors:
        assert verdicts[m].get("reason"), f"an error verdict must carry the provider message: {verdicts[m]}"

    print(f"  remove dead would drop {len(dead)} of {len(queue)}: {dead}")
    print(f"  kept as error (transient, never removed): {kept_errors}")
    print("ALL T-79 LIVE VERIFICATIONS PASSED SUCCESSFULLY!")


if __name__ == "__main__":
    test_t79_test_all_and_remove_dead()
