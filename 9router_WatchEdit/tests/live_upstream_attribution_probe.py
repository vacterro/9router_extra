"""T-110 live probe: upstream attribution against the running 9Router engine.

READ-ONLY. It touches no combo: it only reads /api/version, /api/combos and
the local process list, then predicts the failover chain of a REAL published
combo and cross-checks the member list against what the engine holds.

Run: WATCHEDIT_LIVE_ACCESS=1 python 9router_WatchEdit/tests/live_upstream_attribution_probe.py
"""
import os
import sys
from pathlib import Path

os.environ.setdefault("WATCHEDIT_LIVE_ACCESS", "1")

APP = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP))

from core.combo_failover import failover_report
from core.history import HealthCache
from core.router_client import RouterClient
from core.upstream_attribution import attribute_upstream, owner_summary


def main():
    client = RouterClient()
    print("base_url:", client.base_url)

    attribution = attribute_upstream(client)
    print("owner_summary:", owner_summary(attribution))
    print("attribution:", attribution)

    combos = client.get_combos()
    print("combos:", len(combos))
    if not combos:
        print("RESULT: FAIL (the live engine published no combo)")
        return 1

    combo = combos[0]
    members = list(combo.get("models") or [])
    print("combo:", combo.get("name"), "engine models:", members)

    cache = HealthCache()
    health = {}
    for cid in members:
        record = cache.get(cid)
        if record is not None:
            health[cid] = record
    report = failover_report(members, health, combo_name=combo.get("name") or "")
    print("failover summary:", report["summary"])
    for row in report["members"]:
        print(
            f"  {row['position']:>2} {row['canonical_id']} "
            f"{row['availability']} -> {row['expectation_text']}"
        )

    ok = (
        attribution["state"] == "LIVE"
        and report["member_count"] == len(members)
        and report["published_members"] == members
    )
    print("RESULT:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
