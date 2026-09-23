"""
PERF-001 regression - bounded reusable transport + cancellation for live fan-out.

Defect (audit/3.md PERF-001): every active connection constructed its own
short-lived httpx.Client (P+4 one-request clients per refresh, defeating
keep-alive), and Lock/Close could not stop already-started or queued live calls.

Contract under test: one pass-scoped bounded httpx.Client is reused across the
provider set; cancellation/LOCKED stops scheduling new provider work and queued
futures are cancelled; cancelled/LOCKED work is never authoritative EMPTY.
"""
import threading

import httpx
import pytest

from core.classification import CatalogState
from core.discovery import ModelDiscovery


class _Security:
    def __init__(self):
        self.locked = False

    def is_locked(self):
        return self.locked

    def require_live(self, op):
        if self.locked:
            from core.security import LiveAccessLockedError
            raise LiveAccessLockedError("locked")


class _Client:
    """RouterClient double whose live call uses the injected shared client."""

    def __init__(self):
        self.security = _Security()
        self.clients_seen = []
        self.calls = 0
        self._lock = threading.Lock()

    def get_combos(self):
        return []

    def get_providers(self):
        return [
            {"id": f"conn-{i}", "provider": "p", "name": f"P{i}", "isActive": True,
             "providerSpecificData": {"prefix": "p"}}
            for i in range(6)
        ]

    def get_provider_nodes(self):
        return []

    def get_kv_scoped(self):
        return []

    def get_catalog_models(self):
        return []

    def get_connection_live_models_detailed(self, cid, http_client=None, should_cancel=None):
        with self._lock:
            self.calls += 1
            if http_client is not None:
                self.clients_seen.append(id(http_client))
        if should_cancel is not None and should_cancel():
            return ("CANCELLED", [])
        return ("OK", [{"id": "m1"}])


def test_one_shared_client_reused_across_providers():
    client = _Client()
    discovery = ModelDiscovery(client)
    snap = discovery.build_snapshot(query_live=True)
    # All live calls used ONE pass-scoped client (same identity).
    assert client.calls >= 2
    assert len(set(client.clients_seen)) == 1, "a per-connection client was constructed"
    assert "conn-0" in snap.live_outcomes


def test_cancelled_pass_never_produces_authoritative_empty():
    client = _Client()
    discovery = ModelDiscovery(client)
    # Lock before the pass: cancellation makes every call CANCELLED.
    client.security.locked = True
    snap = discovery.build_snapshot(query_live=True)
    # No connection may be recorded as an authoritative EMPTY catalog.
    assert CatalogState.EMPTY_MODEL_CATALOG not in set(snap.catalog_states.values())
