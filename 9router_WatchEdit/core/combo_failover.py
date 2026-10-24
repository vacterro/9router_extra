"""
9router_WatchEdit - Combo failover health diagnostics (upstream-attributed)

The failover chain is an UPSTREAM 9router behaviour, not a local control
plane decision. Measured on the installed engine
(``open-sse/services/combo.js``) the upstream walks the PUBLISHED member
list and falls back to the next member when one fails:

  * the attempt order is the rotated model list -- rotation, sticky limit and
    the circuit-breaker cooldown are upstream state, so the configured order is
    the CONFIGURED chain, not necessarily the attempt order;
  * a 4xx member failure ALWAYS falls back to the next member; a 5xx failure
    falls back only when the upstream check allows it;
  * when every member fails the upstream answers 503 "All models failed".

This module reconstructs that chain from the published member list plus the
local health history and says, per member, what the upstream is EXPECTED to
do. Health is a PREDICTION from the local cache, never a claim about what the
upstream will do: the upstream tries every member in its own order.
"""
from __future__ import annotations

from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence

#: Upstream rules this module attributes its predictions to.
UPSTREAM_FAILOVER_RULES = (
    "upstream 9router walks the published member list in order; rotation, "
    "sticky limit and circuit-breaker cooldown are upstream state",
    "a 4xx member failure always falls back to the next member; a 5xx failure "
    "falls back only when the upstream check allows it",
    "when every member fails the upstream answers 503 'All models failed'",
)

#: Health states where the member is expected to answer.
HEALTH_ROUTABLE = frozenset({"LIVE"})
#: Health states where the member is expected to fail.
HEALTH_UNROUTABLE = frozenset(
    {
        "DEAD",
        "AUTH_REJECTED",
        "ACCESS_FORBIDDEN",
        "BALANCE_REQUIRED",
        "MODEL_MISSING",
        "ROUTE_ERROR",
    }
)
#: Health states where the member may fail and the upstream fails over.
HEALTH_TEMPORARY = frozenset({"PROVIDER_ERROR", "RATE_LIMITED", "CONNECT_TIMEOUT"})
#: Health states with no evidence yet.
HEALTH_UNTESTED = frozenset({"UNKNOWN", "PENDING"})

#: What the local health history says about one member.
HEALTH_EXPECTATIONS = {
    "routable": "expected to answer",
    "unroutable": "expected to fail",
    "temporary": "may fail, upstream fails over",
    "untested": "untested",
}


def _availability_of(record: Any) -> str:
    """Read the availability state of a health record (or a plain string)."""
    if record is None:
        return "UNKNOWN"
    if isinstance(record, str):
        return record.strip().upper() or "UNKNOWN"
    value = getattr(record, "availability", None)
    if value is None and isinstance(record, Mapping):
        value = record.get("availability")
    return str(value or "UNKNOWN").strip().upper() or "UNKNOWN"


def classify_health(record: Any) -> str:
    """Classify one member's health into routable/unroutable/temporary/untested."""
    availability = _availability_of(record)
    if availability in HEALTH_ROUTABLE:
        return "routable"
    if availability in HEALTH_UNROUTABLE:
        return "unroutable"
    if availability in HEALTH_TEMPORARY:
        return "temporary"
    return "untested"


def _field(record: Any, name: str) -> Any:
    if record is None:
        return None
    if isinstance(record, Mapping):
        return record.get(name)
    return getattr(record, name, None)


def failover_report(
    members: Sequence[str],
    health: Optional[Mapping[str, Any]] = None,
    *,
    disabled: Iterable[str] = (),
    combo_name: str = "",
) -> Dict[str, Any]:
    """Build the failover diagnostics for one combo.

    Args:
        members: the PUBLISHED member list, in configured routing order. A
            member that is locally disabled is not published, so it is not in
            this list; ``disabled`` records it for the report only.
        health: mapping canonical id -> health record (the local HealthCache
            view). Missing ids are UNTESTED.
        disabled: canonical ids that are configured but locally disabled.
        combo_name: the combo's name, for the report header.

    Returns:
        A JSON-friendly dict with the per-member expectations, the predicted
        serving member, the predicted failover target and the upstream rules.
    """
    health = health or {}
    disabled_set = {str(cid) for cid in disabled}
    rows: List[Dict[str, Any]] = []
    for index, cid in enumerate(members or []):
        record = health.get(cid)
        expectation = classify_health(record)
        rows.append(
            {
                "position": index + 1,
                "canonical_id": cid,
                "availability": _availability_of(record),
                "expectation": expectation,
                "expectation_text": HEALTH_EXPECTATIONS[expectation],
                "status_code": _field(record, "status_code") or 0,
                "reason": str(_field(record, "reason") or ""),
                "note": str(_field(record, "note") or ""),
                "last_tested_at": str(_field(record, "last_tested_at") or ""),
                "routable": expectation == "routable",
            }
        )

    routable = [row for row in rows if row["routable"]]
    serving = routable[0]["canonical_id"] if routable else None
    # The member the upstream is expected to hit FIRST and fail over from is
    # the first published member without a positive health record.
    failing = next(
        (row for row in rows if row["expectation"] in ("unroutable", "temporary")),
        None,
    )
    failover_to = None
    if failing is not None:
        position = failing["position"]
        following = [row for row in rows if row["position"] > position]
        failover_to = following[0]["canonical_id"] if following else None
    # The member the upstream tries when the serving member fails.
    serving_fallback = None
    if serving is not None:
        serving_row = next(row for row in rows if row["canonical_id"] == serving)
        following = [row for row in rows if row["position"] > serving_row["position"]]
        serving_fallback = following[0]["canonical_id"] if following else None

    untested = [row["canonical_id"] for row in rows if row["expectation"] == "untested"]
    last_row = rows[-1] if rows else None
    # "No failover left" means the chain ENDS in a member that is expected to
    # fail: if the upstream gets there, it has nothing to fall back to and
    # answers 503 "All models failed".
    no_failover_left = bool(
        last_row is not None and last_row["expectation"] == "unroutable"
    )

    report: Dict[str, Any] = {
        "combo_name": combo_name,
        "published_members": list(members or []),
        "members": rows,
        "serving": serving,
        "serving_fallback": serving_fallback,
        "failover_from": failing["canonical_id"] if failing else None,
        "failover_to": failover_to,
        "untested": untested,
        "disabled": sorted(disabled_set),
        "no_failover_left": no_failover_left,
        "routable_count": len(routable),
        "member_count": len(rows),
        "upstream_rules": list(UPSTREAM_FAILOVER_RULES),
        "upstream_attribution": (
            "failover, rotation and circuit-breaker behaviour belong to the "
            "upstream 9router engine; this report only predicts it from the "
            "published member list and the local health history"
        ),
    }
    report["summary"] = summarize(report)
    return report


def summarize(report: Mapping[str, Any]) -> str:
    """One-line human summary of a failover report."""
    count = int(report.get("member_count") or 0)
    if count == 0:
        return "Failover: no published member"
    serving = report.get("serving")
    failing = report.get("failover_from")
    target = report.get("failover_to")
    fallback = report.get("serving_fallback")
    parts = [f"Failover: {count} published"]
    if serving:
        parts.append(f"serving {serving}")
    if fallback:
        fallback_row = next(
            (row for row in report.get("members") or []
             if row.get("canonical_id") == fallback),
            None,
        )
        state = "not routable" if fallback_row and not fallback_row.get("routable") else "routable"
        parts.append(f"fallback {fallback} ({state})")
    if failing:
        parts.append(f"{failing} fails over")
        parts.append(f"-> {target}" if target else "-> no member left")
    elif not serving:
        parts.append("no member proven routable")
    if report.get("no_failover_left"):
        parts.append("last member not routable")
    if report.get("untested"):
        parts.append(f"{len(report['untested'])} untested")
    disabled = report.get("disabled") or []
    if disabled:
        parts.append(f"{len(disabled)} disabled")
    return " | ".join(parts)
