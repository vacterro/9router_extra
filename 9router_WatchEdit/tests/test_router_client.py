"""
Tests for core/router_client.py
"""
import pytest
from core.router_client import RouterClient

def test_cli_token_generation():
    client = RouterClient()
    token = client.get_cli_token()
    assert isinstance(token, str)
    assert len(token) == 16
    # Must be deterministic for the same machine
    token2 = client.get_cli_token()
    assert token == token2

@pytest.mark.integration
@pytest.mark.local_trusted
def test_local_connection_inspection():
    client = RouterClient()
    # Test reading combos (either from API or SQLite fallback)
    combos = client.get_combos()
    assert isinstance(combos, list)
    if combos:
        c = combos[0]
        assert "id" in c
        assert "name" in c
        assert "models" in c
        assert isinstance(c["models"], list)

    # Test reading providers
    providers = client.get_providers()
    assert isinstance(providers, list)
    assert len(providers) > 0
