"""Set every 9router provider's thinking mode to its maximum available level.

Gathers every model known per provider (static registry lists, the live model
catalog, and each connection's own model list), computes the highest level ALL
of a provider's reasoning models declare (tools/thinking_levels_max.mjs, using
the 9router source's own getThinkingLevels), then PATCHes settings.providerThinking
through the app's own API. A provider's level never exceeds any of its models'
own ladder, so nothing receives a level its wire format would reject.

Usage:
    python tools/set_provider_thinking_max.py            # dry run, prints the map
    python tools/set_provider_thinking_max.py --apply    # backup + PATCH + verify

Backups land in %APPDATA%\\9router\\backups\\settings-providerThinking-<ts>.json
(full settings, so a restore is a PATCH of the saved providerThinking).
"""
import datetime
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request

BASE = "http://127.0.0.1:20128"
ROOT = os.path.dirname(os.path.abspath(__file__))
APPDATA_9R = os.path.join(os.environ["APPDATA"], "9router")
SRC = os.environ.get("NINEROUTER_SOURCE", os.path.join(APPDATA_9R, "source"))


def _token():
    with open(os.path.join(APPDATA_9R, "machine-id"), encoding="utf-8") as f:
        raw = f.read().strip()
    with open(os.path.join(APPDATA_9R, "auth", "cli-secret"), encoding="utf-8") as f:
        secret = f.read().strip()
    return hashlib.sha256((raw + "9r-cli-auth" + secret).encode("utf-8")).hexdigest()[:16]


def api(path, method="GET", body=None):
    req = urllib.request.Request(
        BASE + path,
        method=method,
        headers={"x-9r-cli-token": _token(), "Content-Type": "application/json"},
        data=json.dumps(body).encode() if body is not None else None,
    )
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read().decode("utf-8"))


def gather_provider_models():
    """provider id -> every model id 9router knows for it."""
    registry_src = subprocess.run(
        ["node", "-e",
         "import('file://%s/open-sse/providers/registry/index.js').then(m=>"
         "console.log(JSON.stringify((m.default||m.REGISTRY).map(e=>({id:e.id,alias:e.alias||null,"
         "models:Array.isArray(e.models)?e.models.map(x=>typeof x==='string'?x:(x.id||x.slug)):null})))))"
         % SRC.replace("\\", "/")],
        capture_output=True, text=True, check=True,
    ).stdout
    registry = {e["id"]: e for e in json.loads(registry_src)}
    catalog = api("/api/models")["models"]

    providers = set()
    conns = api("/api/providers")["connections"]
    for c in conns:
        if c.get("provider"):
            providers.add(c["provider"])
    providers |= set((api("/api/settings").get("providerThinking") or {}).keys())

    out = {}
    for p in sorted(providers):
        ids = set()
        for m in (registry.get(p) or {}).get("models") or []:
            if m:
                ids.add(m)
        alias = (registry.get(p) or {}).get("alias") or p
        for m in catalog:
            if m.get("provider") in (p, alias) and m.get("model"):
                ids.add(m["model"])
        out[p] = sorted(ids)

    for c in conns:
        p = c.get("provider")
        if not p or p not in out:
            continue
        try:
            ms = api("/api/providers/%s/models" % c["id"]).get("models") or []
        except (urllib.error.HTTPError, Exception):
            continue
        for m in ms:
            mid = m.get("id") or m.get("slug") or m.get("model") if isinstance(m, dict) else m
            if mid and mid not in out[p]:
                out[p].append(mid)
    return out


def compute_patch(provider_models):
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8") as f:
        json.dump(provider_models, f)
        tmp = f.name
    try:
        res = subprocess.run(
            ["node", os.path.join(ROOT, "thinking_levels_max.mjs"), tmp],
            capture_output=True, text=True, check=True,
            env=dict(os.environ, NINEROUTER_SOURCE=SRC),
        )
        return json.loads(res.stdout)
    finally:
        os.unlink(tmp)


def main():
    apply = "--apply" in sys.argv
    provider_models = gather_provider_models()
    result = compute_patch(provider_models)
    patch, report = result["patch"], result["report"]

    from collections import Counter
    print("providers:", len(patch), "modes:", dict(Counter(v["mode"] for v in patch.values())))
    for r in report:
        print("  %-58s -> %-9s [%s] %d reasoning models" % (r["provider"], r["mode"], r["rule"], r["reasoningModels"]))
    skipped = [p for p, v in provider_models.items() if not v]
    if skipped:
        print("skipped (no known models):", skipped)
    if not apply:
        print("dry run; pass --apply to backup and PATCH")
        return

    before = api("/api/settings")
    ts = datetime.datetime.now().strftime("%Y%m%dT%H%M%SZ")
    backup_dir = os.path.join(APPDATA_9R, "backups")
    os.makedirs(backup_dir, exist_ok=True)
    backup_path = os.path.join(backup_dir, "settings-providerThinking-%s.json" % ts)
    with open(backup_path, "w", encoding="utf-8") as f:
        json.dump({"backedUpAt": ts, "providerThinking": before.get("providerThinking"),
                   "fullSettings": before}, f, ensure_ascii=False, indent=1)
    print("backup:", backup_path)

    api("/api/settings", method="PATCH", body={"providerThinking": patch})
    after = api("/api/settings")
    assert (after.get("providerThinking") or {}) == patch, "stored map differs from patch"
    for k, v in before.items():
        if k != "providerThinking":
            assert after.get(k) == v, "unexpected change in settings key %r" % k
    print("VERIFIED: providerThinking patched (%d providers), all other settings unchanged" % len(patch))


if __name__ == "__main__":
    main()
