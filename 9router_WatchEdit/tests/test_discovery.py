"""
Tests for core/discovery.py - Comprehensive Model Discovery
"""
import pytest
from unittest.mock import MagicMock
from core.router_client import RouterClient
from core.discovery import ModelDiscovery, DiscoveredModel

def test_discovery_synthetic_mock():
    mock_client = MagicMock(spec=RouterClient)
    mock_client.get_providers.return_value = [
        {"id": "conn-1", "provider": "deepseek", "name": "DeepSeek Primary", "providerSpecificData": {}},
        {"id": "conn-2", "provider": "openai-compatible-chat-node-1", "name": "AMD_deepseek", "providerSpecificData": {"prefix": "amd"}},
    ]
    mock_client.get_provider_nodes.return_value = [
        {"id": "openai-compatible-chat-node-1", "name": "AMD", "prefix": "amd", "data": {"prefix": "amd"}},
        {"id": "openai-compatible-chat-node-2", "name": "NVIDIA NIM", "prefix": "nim", "data": {"prefix": "nim"}},
    ]
    mock_client.get_kv.return_value = [
        ("openai-compatible-chat-node-1|DeepSeek-V4-Flash|llm", "{}"),
        ("openai-compatible-chat-node-2|nim/minimaxai/minimax-m3|llm", "{}"),
        ("openai-compatible-chat-node-2|01-ai/yi-large|llm", "{}"),
    ]
    mock_client.get_catalog_models.return_value = [
        {"provider": "deepseek", "model": "deepseek-chat", "name": "DeepSeek Chat", "routedModel": "deepseek/deepseek-chat"},
    ]
    mock_client.get_combos.return_value = [
        {"id": "cb1", "name": "TestCombo", "models": ["amd/DeepSeek-V4-Flash", "extra/ghost-model"]}
    ]

    discovery = ModelDiscovery(mock_client)
    models = discovery.discover_all(include_combo_models=True)

    canon_ids = {m.canonical_id: m for m in models}

    # 1. AMD model correctly prefixed
    assert "amd/DeepSeek-V4-Flash" in canon_ids
    assert canon_ids["amd/DeepSeek-V4-Flash"].provider_prefix == "amd"
    assert canon_ids["amd/DeepSeek-V4-Flash"].is_combo_member is True

    # 2. NIM model with existing prefix not double-prefixed
    assert "nim/minimaxai/minimax-m3" in canon_ids
    assert "nim/nim/minimaxai/minimax-m3" not in canon_ids
    assert canon_ids["nim/minimaxai/minimax-m3"].model_id == "minimaxai/minimax-m3"

    # 3. NIM model without prefix correctly prefixed
    assert "nim/01-ai/yi-large" in canon_ids

    # 4. DeepSeek catalog model
    assert "deepseek/deepseek-chat" in canon_ids
    assert canon_ids["deepseek/deepseek-chat"].display_name == "DeepSeek Chat"

    # 5. Extra combo model retained
    assert "extra/ghost-model" in canon_ids
    assert canon_ids["extra/ghost-model"].is_combo_member is True

@pytest.mark.integration
def test_live_local_discovery():
    client = RouterClient()
    discovery = ModelDiscovery(client)
    models = discovery.discover_all()

    assert len(models) >= 100
    canon_set = {m.canonical_id for m in models}

    # Verify AMD models are present
    amd_models = [m for m in models if m.provider_prefix == "amd"]
    assert len(amd_models) >= 3

    # Verify NIM models are present
    nim_models = [m for m in models if m.provider_prefix == "nim"]
    assert len(nim_models) >= 10
