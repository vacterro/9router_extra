"""T-110: upstream attribution + combo failover health diagnostics.

Two claims are under test, and both are grounded in the installed engine
rather than in a local fiction:

* the FAILOVER prediction is derived from the PUBLISHED member list and the
  local health history, and it is attributed to the upstream rules, not to a
  local control plane;
* the ATTRIBUTION report degrades honestly: an unreachable upstream is
  UNKNOWN unless a positive proof of "no runtime owner" exists, and a live
  upstream reports the version it answers with.
"""
import sys
from pathlib import Path

import pytest

APP = Path(__file__).resolve().parents[1]
if str(APP) not in sys.path:
    sys.path.insert(0, str(APP))

from core import combo_failover
from core.combo_failover import (
    UPSTREAM_FAILOVER_RULES,
    classify_health,
    failover_report,
    summarize,
)
from core.upstream_attribution import (
    ATTRIBUTION_LIVE,
    ATTRIBUTION_OFFLINE,
    ATTRIBUTION_UNKNOWN,
    attribute_upstream,
    owner_summary,
)


class _Record:
    """Minimal stand-in for a ModelHealthRecord."""

    def __init__(self, availability, status_code=0, reason="", last_tested_at=""):
        self.availability = availability
        self.status_code = status_code
        self.reason = reason
        self.last_tested_at = last_tested_at
        self.note = ""


# --------------------------------------------------------------- classify
@pytest.mark.parametrize("availability,expected", [
    ("LIVE", "routable"),
    ("DEAD", "unroutable"),
    ("AUTH_REJECTED", "unroutable"),
    ("BALANCE_REQUIRED", "unroutable"),
    ("MODEL_MISSING", "unroutable"),
    ("RATE_LIMITED", "temporary"),
    ("PROVIDER_ERROR", "temporary"),
    ("CONNECT_TIMEOUT", "temporary"),
    ("UNKNOWN", "untested"),
    ("PENDING", "untested"),
])
def test_classify_health_covers_every_state(availability, expected):
    assert classify_health(_Record(availability)) == expected


def test_classify_health_accepts_plain_state_and_missing_record():
    assert classify_health("live") == "routable"
    assert classify_health(None) == "untested"
    assert classify_health({"availability": "DEAD"}) == "unroutable"


# --------------------------------------------------------------- report
def test_report_uses_published_members_only():
    """A disabled member is configured, not published: it is not in the chain."""
    health = {"p/a": _Record("LIVE"), "p/b": _Record("DEAD")}
    report = failover_report(
        ["p/a", "p/b"], health, disabled=["p/c"], combo_name="demo"
    )
    assert report["published_members"] == ["p/a", "p/b"]
    assert report["member_count"] == 2
    assert report["disabled"] == ["p/c"]
    assert "p/c" not in [m["canonical_id"] for m in report["members"]]


def test_report_predicts_serving_and_failover_target():
    health = {
        "p/a": _Record("DEAD", status_code=500, reason="upstream 500"),
        "p/b": _Record("LIVE"),
        "p/c": _Record("RATE_LIMITED", status_code=429),
    }
    report = failover_report(["p/a", "p/b", "p/c"], health, combo_name="demo")
    assert report["serving"] == "p/b"
    assert report["failover_from"] == "p/a"
    assert report["failover_to"] == "p/b"
    assert report["routable_count"] == 1
    # The last member may still fail temporarily: the chain does not end in a
    # guaranteed failure, so there IS a fallback left.
    assert report["no_failover_left"] is False
    assert report["serving_fallback"] == "p/c"


def test_report_flags_a_dead_last_member_as_no_failover_left():
    health = {"p/a": _Record("DEAD"), "p/b": _Record("DEAD")}
    report = failover_report(["p/a", "p/b"], health)
    assert report["serving"] is None
    assert report["failover_from"] == "p/a"
    assert report["failover_to"] == "p/b"
    assert report["no_failover_left"] is True


def test_report_marks_untested_members_without_claiming_failure():
    report = failover_report(["p/a", "p/b"], {})
    assert report["untested"] == ["p/a", "p/b"]
    assert report["serving"] is None
    assert report["routable_count"] == 0


def test_report_on_empty_combo_is_honest():
    report = failover_report([], {})
    assert report["member_count"] == 0
    assert report["serving"] is None
    assert report["summary"] == "Failover: no published member"


def test_report_carries_the_upstream_rules_it_is_attributed_to():
    report = failover_report(["p/a"], {})
    assert report["upstream_rules"] == list(UPSTREAM_FAILOVER_RULES)
    assert "upstream" in report["upstream_attribution"]


def test_summary_mentions_serving_and_failover():
    health = {"p/a": _Record("DEAD"), "p/b": _Record("LIVE")}
    text = summarize(failover_report(["p/a", "p/b"], health))
    assert "2 published" in text
    assert "serving p/b" in text
    assert "p/a fails over" in text
    assert "-> p/b" in text

def test_summary_mentions_disabled_members():
    text = summarize(failover_report(["p/a"], {"p/a": _Record("LIVE")}, disabled=["p/z"]))
    assert "1 disabled" in text


# ----------------------------------------------------------- attribution
class _FakeClient:
    def __init__(self, base_url="http://127.0.0.1:8080", reachable=True, version="0.5.91"):
        self.base_url = base_url
        self._reachable = reachable
        self._version = version

    def is_server_reachable(self, timeout=3.0):
        return self._reachable

    def get_server_version(self, timeout=3.0):
        return self._version


def test_attribution_reports_live_upstream_with_version(monkeypatch):
    monkeypatch.setattr(
        "core.router_client.runtime_owner_processes",
        lambda base_url: [{"pid": 4242, "name": "node.exe"}],
    )
    report = attribute_upstream(_FakeClient())
    assert report["state"] == ATTRIBUTION_LIVE
    assert report["reachable"] is True
    assert report["version"] == "0.5.91"
    assert report["port"] == 8080
    assert report["owner_processes"] == [{"pid": 4242, "name": "node.exe"}]


def test_attribution_is_unknown_when_the_owner_check_is_indeterminate(monkeypatch):
    monkeypatch.setattr(
        "core.router_client.runtime_owner_processes", lambda base_url: None
    )
    report = attribute_upstream(_FakeClient(reachable=False))
    assert report["state"] == ATTRIBUTION_UNKNOWN
    assert report["reachable"] is False
    assert any("indeterminate" in note for note in report["notes"])


def test_attribution_is_offline_only_with_a_positive_owner_proof(monkeypatch):
    monkeypatch.setattr(
        "core.router_client.runtime_owner_processes", lambda base_url: []
    )
    report = attribute_upstream(_FakeClient(reachable=False))
    assert report["state"] == ATTRIBUTION_OFFLINE
    assert report["owner_processes"] == []


def test_attribution_survives_a_refused_probe(monkeypatch):
    monkeypatch.setattr(
        "core.router_client.runtime_owner_processes", lambda base_url: []
    )

    class _Refusing(_FakeClient):
        def is_server_reachable(self, timeout=3.0):
            raise RuntimeError("live access locked")

    report = attribute_upstream(_Refusing())
    assert report["state"] == ATTRIBUTION_UNKNOWN
    assert any("refused" in note for note in report["notes"])


def test_attribution_without_a_client_does_not_claim_reachability(monkeypatch):
    monkeypatch.setattr(
        "core.router_client.runtime_owner_processes", lambda base_url: []
    )
    report = attribute_upstream(None)
    assert report["reachable"] is False
    assert report["state"] == ATTRIBUTION_UNKNOWN
    assert any("no router client" in note for note in report["notes"])


def test_owner_summary_is_one_line():
    text = owner_summary({
        "base_url": "http://127.0.0.1:8080",
        "state": ATTRIBUTION_LIVE,
        "version": "0.5.91",
        "owner_processes": [{"pid": 42, "name": "node.exe"}],
    })
    assert text.startswith("Upstream: http://127.0.0.1:8080 LIVE")
    assert "v0.5.91" in text
    assert "node.exe#42" in text
    assert "\n" not in text


# ------------------------------------------------------------ version read
def _version_client(monkeypatch, tmp_path, body, status=200):
    """A RouterClient whose HTTP layer answers /api/version from ``body``."""
    import httpx

    from core.router_client import RouterClient

    router = RouterClient(
        base_url="http://127.0.0.1:43123", db_path=tmp_path / "router.db"
    )

    def handler(request):
        assert request.url.path == "/api/version"
        return httpx.Response(status, json=body)

    real_client = httpx.Client

    def fake_client(**kwargs):
        kwargs.pop("transport", None)
        return real_client(transport=httpx.MockTransport(handler), **kwargs)

    monkeypatch.setattr("core.router_client.httpx.Client", fake_client)
    return router


def test_get_server_version_reads_the_live_current_version(monkeypatch, tmp_path):
    router = _version_client(
        monkeypatch, tmp_path,
        {"currentVersion": "0.5.91-extra", "latestVersion": "0.5.104",
         "hasUpdate": False},
    )
    assert router.get_server_version() == "0.5.91-extra"


def test_get_server_version_accepts_the_legacy_version_key(monkeypatch, tmp_path):
    router = _version_client(monkeypatch, tmp_path, {"version": "0.5.9"})
    assert router.get_server_version() == "0.5.9"


def test_get_server_version_is_none_without_a_version(monkeypatch, tmp_path):
    router = _version_client(monkeypatch, tmp_path, {"hasUpdate": False})
    assert router.get_server_version() is None


def test_get_server_version_is_none_on_a_non_200(monkeypatch, tmp_path):
    router = _version_client(monkeypatch, tmp_path, {"version": "0.5.9"}, status=503)
    assert router.get_server_version() is None
